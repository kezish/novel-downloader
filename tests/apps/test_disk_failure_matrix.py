#!/usr/bin/env python3
"""
tests.apps.test_disk_failure_matrix
-----------------------------------

Cross-module failure matrix for a full disk (``ENOSPC``) and a denied
permission (``EACCES`` / ``EPERM``).

Every module that publishes user data through a temporary file is exercised at
the three places an I/O error can surface:

* **write** -- the Python-level write on the temporary stream;
* **fsync** -- flushing the temporary file to the OS;
* **replace** -- the atomic rename that publishes it.

The invariants pinned for each module and each error are:

* the previous file is byte-for-byte intact (a disk-full write never truncates
  what was already on disk);
* nothing half-written is left behind -- no undeletable ``.tmp`` next to the
  data, no half-published target;
* the failure is reported with a readable message that carries the path and
  never a traceback;
* recovery is possible: once the injected fault is gone the same call succeeds;
* bookkeeping (byte budgets, in-memory state, rollback sets, "download active"
  flags) is correct after the failure, so a retry is not silently crippled.

The modules covered, and the writer each test drives:

======================  =====================================  ===============
module                  exercised API                          artifact
======================  =====================================  ===============
``infra/config``        ``copy_default_config``                settings.toml
``infra/persistence``   ``StateManager.set_language``          state.json
``cli/commands/config`` ``config export`` / ``config import``  TOML file
``apps/music``          ``save_lyric`` / ``export_m3u`` /      .lrc, .m3u,
                        ``_download_file`` / ``download_track`` .mp3
``apps/common/updater`` ``download_asset`` /                    update asset,
                        ``save_update_state``                  throttle file
``apps/desktop``        ``_save_prefs`` / ``_download_update`` prefs.json
``cli/commands/storage`` ``storage backup`` / ``restore`` /     chapter.*.sqlite
                        ``verify``
======================  =====================================  ===============

Windows-only tests (``skipif(os.name != "nt")``) cover what POSIX cannot show:
a read-only *file* refuses ``os.replace`` there, and a file held open by
another process is a sharing violation the storage writer retries through.

No network access and no real user directory is touched; every path lives under
``tmp_path``.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import errno
import hashlib
import io
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import config as config_cmd
from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.config import ConfigCmd
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.apps.common import updater
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.music import downloader as music
from novel_downloader.apps.music.providers.base import ResolveResult, Track
from novel_downloader.infra.config import file_io
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.state import StateManager

_STAMP = "20260102T030405Z"
_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="this failure mode only exists on Windows"
)
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory permissions are needed"
)


# ---------------------------------------------------------------------------
# fault injection
# ---------------------------------------------------------------------------


def _no_space() -> OSError:
    """``write(2)`` failing because the filesystem is full."""
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def _denied(path: str | os.PathLike[str] | None = None) -> PermissionError:
    """A permission failure; the filename is kept so the message names it."""
    return PermissionError(
        errno.EACCES,
        os.strerror(errno.EACCES),
        None if path is None else os.fspath(path),
    )


_ERRORS: dict[str, Callable[[str | None], OSError]] = {
    "enospc": lambda path=None: OSError(
        errno.ENOSPC,
        os.strerror(errno.ENOSPC),
        None if path is None else os.fspath(path),
    ),
    "eacces": _denied,
}
_ERROR_IDS = ["enospc", "eacces"]


class _FailingStream:
    """A file-object proxy whose first ``write`` raises ``exc``."""

    def __init__(self, stream: Any, exc: OSError) -> None:
        self._stream = stream
        self._exc = exc
        self._failed = False

    def write(self, data: Any) -> int:
        if not self._failed:
            self._failed = True
            raise self._exc
        return int(self._stream.write(data))

    def flush(self) -> None:
        self._stream.flush()

    def fileno(self) -> int:
        return int(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> _FailingStream:
        self._stream.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._stream.__exit__(*exc_info)


#: ``skip`` steps over earlier writes performed by the same command, e.g. the
#: ``.bak`` copy that ``config import`` takes before writing the new file.
@contextmanager
def _inject_failure(
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    exc: OSError,
    *,
    skip: int = 0,
) -> Iterator[None]:
    """Make one OS write point fail once, then behave normally again."""
    calls = {"n": 0}

    def _next() -> bool:
        index = calls["n"]
        calls["n"] += 1
        return index == skip

    with monkeypatch.context() as patch:
        if point == "write":
            real_fdopen = os.fdopen

            def fake_fdopen(*args: Any, **kwargs: Any) -> Any:
                stream = real_fdopen(*args, **kwargs)
                if _next():
                    return _FailingStream(stream, exc)
                return stream

            patch.setattr(os, "fdopen", fake_fdopen)
        elif point == "fsync":
            real_fsync = os.fsync

            def fake_fsync(fd: int) -> None:
                if _next():
                    raise exc
                real_fsync(fd)

            patch.setattr(os, "fsync", fake_fsync)
        elif point == "replace":
            real_replace = os.replace

            def fake_replace(src: Any, dst: Any, **kwargs: Any) -> None:
                if _next():
                    raise exc
                real_replace(src, dst, **kwargs)

            patch.setattr(os, "replace", fake_replace)
        elif point == "create":
            # The write target cannot even be created (read-only directory).
            def fake_mkstemp(*args: Any, **kwargs: Any) -> Any:
                raise exc

            patch.setattr("tempfile.mkstemp", fake_mkstemp)
        else:  # pragma: no cover - guards a typo in a parametrization
            raise AssertionError(f"unknown injection point: {point}")
        yield


def _temp_artifacts(root: Path, suffixes: tuple[str, ...] = (".tmp",)) -> list[Path]:
    """Temporary files a failed write must never leave behind."""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(suffixes)
    )


def _assert_no_traceback(text: str) -> None:
    assert "Traceback (most recent call last)" not in text


def _names_path(text: str, path: Path) -> bool:
    """Whether *text* mentions *path*.

    ``OSError.__str__`` renders ``filename`` through ``repr``, so a Windows
    path appears with doubled separators; both spellings count as naming it.
    """
    plain = str(path)
    return plain in text or plain.replace("\\", "\\\\") in text


def _files_under(root: Path) -> list[Path]:
    """Every regular file below *root* (empty folders are harmless)."""
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# infra/config/file_io.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_file_io_atomic_write_failure_keeps_the_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    target = tmp_path / "settings.toml"
    original = b"[general]\nworkers = 2\n"
    target.write_bytes(original)

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(target))),
        pytest.raises(OSError),
    ):
        file_io.copy_default_config(target)

    assert target.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_file_io_failure_creates_no_file_for_a_new_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    target = tmp_path / "settings.toml"

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(target))),
        pytest.raises(OSError),
    ):
        file_io.copy_default_config(target)

    assert not target.exists()
    assert _temp_artifacts(tmp_path) == []


def test_file_io_succeeds_again_after_the_permission_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.toml"
    original = b"[general]\nworkers = 2\n"
    target.write_bytes(original)

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _denied(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    with pytest.raises(PermissionError):
        file_io.copy_default_config(target)
    assert target.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []

    # The same call with the fault gone must publish the bundled config.
    file_io.copy_default_config(target)
    assert target.read_bytes() == file_io.DEFAULT_CONFIG_FILE.read_bytes()
    assert _temp_artifacts(tmp_path) == []


@_WINDOWS_ONLY
def test_file_io_read_only_target_leaves_no_undeletable_temp(tmp_path: Path) -> None:
    """Windows refuses ``os.replace`` over a read-only file.

    The writer copies the target's mode onto its temporary file, so the
    cleanup path must clear that bit before unlinking -- otherwise the failed
    run leaves a ``.tmp`` nobody can delete.
    """
    target = tmp_path / "settings.toml"
    original = b"[general]\nworkers = 2\n"
    target.write_bytes(original)
    os.chmod(target, stat.S_IREAD)

    try:
        with pytest.raises(PermissionError):
            file_io.copy_default_config(target)
        assert target.read_bytes() == original
        assert _temp_artifacts(tmp_path) == []

        os.chmod(target, stat.S_IWRITE)
        file_io.copy_default_config(target)
        assert target.read_bytes() == file_io.DEFAULT_CONFIG_FILE.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE)


@_POSIX_ONLY
def test_file_io_read_only_directory_rejects_a_new_config(tmp_path: Path) -> None:
    directory = tmp_path / "readonly"
    directory.mkdir()
    os.chmod(directory, 0o500)
    try:
        with pytest.raises(PermissionError):
            file_io.copy_default_config(directory / "settings.toml")
        assert list(directory.iterdir()) == []
    finally:
        os.chmod(directory, 0o700)


def test_file_io_read_only_directory_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only directory behaviour, portable to Windows."""
    directory = tmp_path / "readonly"
    directory.mkdir()
    target = directory / "settings.toml"
    exc = _denied(str(directory))
    with _inject_failure(monkeypatch, "create", exc), pytest.raises(PermissionError):
        file_io.copy_default_config(target)

    assert list(directory.iterdir()) == []


# ---------------------------------------------------------------------------
# infra/persistence StateManager (state.json)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_state_save_failure_rolls_back_the_in_memory_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{\n  "lang": "zh_CN"\n}', encoding="utf-8")
    original = path.read_bytes()
    manager = StateManager(path)

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(path))),
        pytest.raises(OSError),
    ):
        manager.set_language("en")

    # The preference is not reported as saved when it never reached the disk.
    assert manager.get_language() == "zh_CN"
    assert path.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []

    # A later save (permissions/disk restored) publishes the new value.
    manager.set_language("en")
    assert StateManager(path).get_language() == "en"
    assert _temp_artifacts(tmp_path) == []


def test_chapter_upsert_rolls_back_when_sqlite_runs_out_of_space(
    tmp_path: Path,
) -> None:
    """SQLite's own ``SQLITE_FULL`` path: the whole transaction is undone.

    ``PRAGMA max_page_count`` caps the file at its current size, so the next
    growing write fails with ``database or disk is full`` -- the same error a
    real full disk produces.  Neither the rows of the failed statement nor the
    in-memory "needs refetch" cache may observe a partial commit, and the
    connection must stay usable once space is available again.
    """
    db = tmp_path / "chapter.raw.sqlite"
    with ChapterStorage(tmp_path, db.name) as store:
        store.upsert_chapters([{"id": "c1", "title": "T", "content": "B", "extra": {}}])
        pages = int(store.conn.execute("PRAGMA page_count").fetchone()[0])
        store.conn.execute(f"PRAGMA max_page_count = {pages}")

        with pytest.raises(sqlite3.OperationalError) as exc_info:
            store.upsert_chapters(
                [
                    {
                        "id": f"big{index}",
                        "title": "T",
                        "content": "x" * 200_000,
                        "extra": {},
                    }
                    for index in range(20)
                ]
            )
        assert "full" in str(exc_info.value).lower()

        assert store.existing_ids() == {"c1"}
        assert store.dirty_ids() == set()
        assert store.conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 1

        # Enlarging the budget again lets the same connection finish the work.
        store.conn.execute(f"PRAGMA max_page_count = {pages + 200}")
        store.upsert_chapters([{"id": "c2", "title": "T", "content": "B", "extra": {}}])
        assert store.existing_ids() == {"c1", "c2"}


# ---------------------------------------------------------------------------
# config export / import (apps/cli/commands/config.py)
# ---------------------------------------------------------------------------


def _config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ConfigCmd.register(subparsers)
    return parser


def _run_config(argv: list[str]) -> None:
    args = _config_parser().parse_args(argv)
    args.func(args)


def _quiet_ui(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Silence the human messages but keep ``ui.emit`` (the JSON channel)."""
    messages: list[str] = []

    def record(message: str, *_args: Any, **_kwargs: Any) -> None:
        messages.append(str(message))

    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(config_cmd.ui, name, record)
    return messages


@pytest.fixture()
def config_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated working directory plus an empty application home."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NOVEL_HOME", str(home))
    yield work


def _json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    _assert_no_traceback(captured.err)
    return payload


def _write_source(work: Path, name: str, text: str) -> Path:
    source = work.parent / "sources" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    return source


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_config_import_write_failure_keeps_the_active_file(
    config_work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    error_kind: str,
) -> None:
    target = config_work / "settings.toml"
    original = b"[general]\nworkers = 2\n"
    target.write_bytes(original)
    source = _write_source(config_work, "new.toml", "[general]\nworkers = 8\n")
    _quiet_ui(monkeypatch)

    # The import copies the current file to ``.bak`` first: skip that write so
    # the fault hits the write that would publish the imported document.
    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(target)), skip=1),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_config(["config", "import", "--from", str(source), "--json"])
    assert exc_info.value.code == 1

    payload = _json_line(capsys)
    assert payload["command"] == "config import"
    assert payload["status"] == "error"
    assert payload["path"] == str(target)
    assert _names_path(payload["error"], target)
    # Disk-full must never truncate the live configuration.
    assert target.read_bytes() == original
    # The backup taken before the failed write is a legitimate artifact.
    assert (config_work / "settings.toml.bak").read_bytes() == original
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_config_import_failure_creates_no_new_config(
    config_work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    error_kind: str,
) -> None:
    source = _write_source(config_work, "new.toml", "[general]\nworkers = 8\n")
    _quiet_ui(monkeypatch)
    target = config_work / "settings.toml"

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(target))),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_config(["config", "import", "--from", str(source), "--json"])
    assert exc_info.value.code == 1

    payload = _json_line(capsys)
    assert payload["status"] == "error"
    assert payload["path"] == str(target)
    assert not target.exists()
    assert not (config_work / "settings.toml.bak").exists()
    assert _temp_artifacts(tmp_path) == []


def test_config_import_human_error_names_the_path_without_a_traceback(
    config_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = config_work / "settings.toml"
    target.write_bytes(b"[general]\nworkers = 2\n")
    source = _write_source(config_work, "new.toml", "[general]\nworkers = 8\n")
    messages = _quiet_ui(monkeypatch)

    with (
        _inject_failure(monkeypatch, "fsync", _no_space(), skip=1),
        pytest.raises(SystemExit),
    ):
        _run_config(["config", "import", "--from", str(source)])

    assert messages, "the human error message must be reported"
    assert _names_path(messages[-1], target)
    assert "No space left on device" in messages[-1]
    _assert_no_traceback(messages[-1])


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_config_export_failure_keeps_the_previous_export(
    config_work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    error_kind: str,
) -> None:
    (config_work / "settings.toml").write_text(
        "[general]\nworkers = 2\n", encoding="utf-8"
    )
    out_path = config_work / "export.toml"
    original = b"# previous export\n"
    out_path.write_bytes(original)
    _quiet_ui(monkeypatch)

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(out_path)), skip=1),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_config(["config", "export", "--out", str(out_path), "--force", "--json"])
    assert exc_info.value.code == 1

    payload = _json_line(capsys)
    assert payload["command"] == "config export"
    assert payload["status"] == "error"
    assert payload["path"] == str(out_path)
    assert out_path.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []


@_WINDOWS_ONLY
def test_config_export_read_only_target_keeps_no_temp(
    config_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (config_work / "settings.toml").write_text(
        "[general]\nworkers = 2\n", encoding="utf-8"
    )
    out_path = config_work / "export.toml"
    original = b"# previous export\n"
    out_path.write_bytes(original)
    os.chmod(out_path, stat.S_IREAD)
    _quiet_ui(monkeypatch)

    try:
        with pytest.raises(SystemExit) as exc_info:
            _run_config(
                ["config", "export", "--out", str(out_path), "--force", "--json"]
            )
        assert exc_info.value.code == 1
        payload = _json_line(capsys)
        assert payload["status"] == "error"
        assert out_path.read_bytes() == original
        assert _temp_artifacts(config_work) == []
    finally:
        with contextlib.suppress(OSError):
            os.chmod(out_path, stat.S_IWRITE)


def test_config_export_read_only_directory_rejects_a_new_file(
    config_work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (config_work / "settings.toml").write_text(
        "[general]\nworkers = 2\n", encoding="utf-8"
    )
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    out_path = readonly / "export.toml"
    _quiet_ui(monkeypatch)

    with (
        _inject_failure(monkeypatch, "create", _denied(str(readonly))),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_config(["config", "export", "--out", str(out_path), "--json"])
    assert exc_info.value.code == 1

    payload = _json_line(capsys)
    assert payload["status"] == "error"
    assert list(readonly.iterdir()) == []


# ---------------------------------------------------------------------------
# music: lyrics, playlists and audio downloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_lyric_write_failure_keeps_the_old_lyrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    point: str,
    error_kind: str,
) -> None:
    audio = tmp_path / "song.mp3"
    target = tmp_path / "song.lrc"
    audio.write_bytes(b"AUDIO")
    assert music.save_lyric(str(audio), "[00:01.00]old") == [str(target)]
    original = target.read_bytes()

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(target))),
        caplog.at_level("WARNING", logger=music.__name__),
    ):
        written = music.save_lyric(str(audio), "[00:02.00]new")

    assert written == []
    assert target.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []
    # A lyric failure stays a warning, but the warning names the file.
    assert str(target) in caplog.text
    _assert_no_traceback(caplog.text)


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_playlist_write_failure_keeps_the_old_playlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    playlist = tmp_path / "list.m3u8"
    assert music.export_m3u([], str(playlist)) == str(playlist)
    original = playlist.read_bytes()

    with (
        _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(playlist))),
        pytest.raises(OSError) as exc_info,
    ):
        music.export_m3u([], str(playlist))

    assert not isinstance(exc_info.value, ValueError)
    assert playlist.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []


@_WINDOWS_ONLY
def test_lyric_write_over_a_file_held_by_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sharing violation is a warning, not a traceback, and is retryable."""
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"AUDIO")
    target = tmp_path / "song.lrc"
    music.save_lyric(str(audio), "[00:01.00]old")
    original = target.read_bytes()

    # Held open across the write so Windows reports a sharing violation.
    with builtins.open(target, "r+b"):
        with caplog.at_level("WARNING", logger=music.__name__):
            assert music.save_lyric(str(audio), "[00:02.00]new") == []
        assert target.read_bytes() == original
        assert _temp_artifacts(tmp_path) == []

    assert music.save_lyric(str(audio), "[00:02.00]new") == [str(target)]
    assert target.read_text(encoding="utf-8") == "[00:02.00]new\n"


class _StreamResponse:
    """Minimal streaming ``requests.Response`` double."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"data",),
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


def _install_fake_get(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StreamResponse]
) -> None:
    queue = list(responses)

    def fake_get(url: str, **kwargs: Any) -> _StreamResponse:
        assert queue, f"unexpected request: {url}"
        return queue.pop(0)

    monkeypatch.setattr(music.requests, "get", fake_get)


@contextmanager
def _fail_part_write(monkeypatch: pytest.MonkeyPatch, exc: OSError) -> Iterator[None]:
    """Fail the first write into a ``.part`` download file."""
    real_open = builtins.open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if (
            isinstance(file, (str, os.PathLike))
            and str(file).endswith(".part")
            and ("w" in mode or "a" in mode)
        ):
            return _FailingStream(handle, exc)
        return handle

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", fake_open)
        yield


@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_audio_download_write_failure_refunds_the_budget_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    dest = tmp_path / "song.mp3"
    part = tmp_path / "song.mp3.part"
    budget = music._ByteBudget(1280)
    body = b"B" * 512
    _install_fake_get(
        monkeypatch,
        [
            _StreamResponse(
                headers={"Content-Length": "512", "Content-Type": "audio/mpeg"},
                chunks=(body,),
            ),
            _StreamResponse(
                headers={"Content-Length": "512", "Content-Type": "audio/mpeg"},
                chunks=(body,),
            ),
        ],
    )

    with (
        _fail_part_write(monkeypatch, _ERRORS[error_kind](str(dest))),
        pytest.raises(OSError),
    ):
        music._download_file("http://fake.test/a.mp3", str(dest), budget=budget)

    # Nothing was published and the batch budget is not silently consumed.
    assert not dest.exists()
    assert budget.used == 0
    # The resumable prefix is the only leftover; never a stray ``.tmp``.
    assert part.exists()
    assert _temp_artifacts(tmp_path) == []

    size, resumed = music._download_file(
        "http://fake.test/a.mp3", str(dest), budget=budget
    )
    assert (size, resumed) == (512, 0)
    assert dest.read_bytes() == body
    assert not part.exists()
    assert budget.used == 512


class _Provider:
    """A provider double resolving every track to one direct URL."""

    def __init__(self, url: str) -> None:
        self._url = url

    def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
        return ResolveResult(url=self._url, ext="mp3", level=level or "exhigh")


@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_download_track_reports_a_readable_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    opts = music.DownloadOptions(
        out_dir=str(tmp_path),
        retries=0,
        embed_tags=False,
        embed_cover=False,
        save_lyric=False,
        skip_existing=False,
    )
    track = Track(id="t1", name="Song", artists="Artist", album="Album")
    _install_fake_get(
        monkeypatch,
        [
            _StreamResponse(
                headers={"Content-Length": "4", "Content-Type": "audio/mpeg"},
                chunks=(b"data",),
            )
        ],
    )

    with _fail_part_write(monkeypatch, _ERRORS[error_kind](str(tmp_path))):
        result = music.download_track(_Provider("http://fake.test/a.mp3"), track, opts)

    assert result.status == "fail"
    assert result.path == ""
    assert "下载失败" in result.reason
    assert "Traceback" not in result.reason
    assert not list(tmp_path.rglob("*.mp3"))
    assert _temp_artifacts(tmp_path, suffixes=(".tmp", ".mp3")) == []


# ---------------------------------------------------------------------------
# updater: asset download + throttle state
# ---------------------------------------------------------------------------


def _update_info(latest: str = "1.4.0") -> updater.UpdateInfo:
    return updater.UpdateInfo(
        current="1.0.0",
        latest=latest,
        available=True,
        notes_url=None,
        asset=None,
        assets=(),
    )


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_update_state_failure_keeps_the_previous_throttle_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    state = tmp_path / "update.json"
    updater.save_update_state(state, _update_info("1.4.0"), now=1000.0)
    original = state.read_bytes()

    with _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(state))):
        # The throttle file is best effort: a failed save must not raise.
        updater.save_update_state(state, _update_info("9.9.9"), now=2000.0)

    assert state.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []

    updater.save_update_state(state, _update_info("9.9.9"), now=2000.0)
    assert updater.load_update_state(state)["latest"] == "9.9.9"
    assert _temp_artifacts(tmp_path) == []


@contextmanager
def _fail_part_io_open(monkeypatch: pytest.MonkeyPatch, exc: OSError) -> Iterator[None]:
    """Fail a write into the updater's ``.part`` file (``Path.open``)."""
    real_open = io.open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if str(file).endswith(".part") and ("w" in mode or "a" in mode):
            return _FailingStream(handle, exc)
        return handle

    with monkeypatch.context() as patch:
        patch.setattr(io, "open", fake_open)
        yield


@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_update_download_write_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    payload = b"release bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def serve(*_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO(payload)

    monkeypatch.setattr("urllib.request.urlopen", serve)

    with (
        _fail_part_io_open(monkeypatch, _ERRORS[error_kind](str(tmp_path))),
        pytest.raises(OSError) as exc_info,
    ):
        updater.download_asset(
            "https://example.invalid/a.zip",
            tmp_path / "a.zip",
            expected_sha256=digest,
            root=tmp_path,
            attempts=1,
            resume=True,
        )

    message = str(exc_info.value)
    assert "a.zip" in message
    assert _names_path(message, tmp_path)
    _assert_no_traceback(message)
    assert not (tmp_path / "a.zip").exists()
    assert (tmp_path / "a.zip.part").exists()  # resumable prefix
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_update_download_publish_failure_recovers_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    payload = b"release bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def serve(*_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO(payload)

    monkeypatch.setattr("urllib.request.urlopen", serve)

    with (
        _inject_failure(
            monkeypatch, "replace", _ERRORS[error_kind](str(tmp_path / "a.zip"))
        ),
        pytest.raises(OSError) as exc_info,
    ):
        updater.download_asset(
            "https://example.invalid/a.zip",
            tmp_path / "a.zip",
            expected_sha256=digest,
            expected_size=len(payload),
            root=tmp_path,
            attempts=1,
            resume=True,
        )

    assert _names_path(str(exc_info.value), tmp_path)
    assert not (tmp_path / "a.zip").exists()
    # The complete, verified prefix survives: the retry needs no new bytes.
    assert (tmp_path / "a.zip.part").read_bytes() == payload

    written = updater.download_asset(
        "https://example.invalid/a.zip",
        tmp_path / "a.zip",
        expected_sha256=digest,
        expected_size=len(payload),
        root=tmp_path,
        attempts=1,
        resume=True,
    )
    assert written.read_bytes() == payload
    assert not (tmp_path / "a.zip.part").exists()


# ---------------------------------------------------------------------------
# desktop: preferences and the in-app update download
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_desktop_prefs_failure_is_reported_and_keeps_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    error_kind: str,
) -> None:
    prefs = tmp_path / "desktop_prefs.json"
    prefs.write_text('{\n  "lang": "zh_CN"\n}', encoding="utf-8")
    original = prefs.read_bytes()
    monkeypatch.setattr(wa, "_prefs_path", lambda: prefs)

    with _inject_failure(monkeypatch, point, _ERRORS[error_kind](str(prefs))):
        assert wa._save_prefs({"theme": "dark"}) is False

    assert prefs.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []

    # The bridge reports the failure, and a retry saves once the fault is gone.
    assert wa._save_prefs({"theme": "dark"}) is True
    assert json.loads(prefs.read_text(encoding="utf-8"))["theme"] == "dark"


def _bridge_info(asset: updater.ReleaseAsset) -> Any:
    info = updater.UpdateInfo(
        current="1.0.0",
        latest="9.0.0",
        available=True,
        notes_url=None,
        asset=asset,
        assets=(asset,),
    )
    return wa._UpdateCache(
        info=info,
        error=None,
        checked_at=1_700_000_000.0,
        current="1.0.0",
        platform="windows-x64",
    )


@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_desktop_update_download_failure_clears_the_active_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))
    payload = b"verified update payload"
    digest = hashlib.sha256(payload).hexdigest()
    asset = updater.ReleaseAsset(
        name="setup.exe",
        url="https://example.invalid/setup.exe",
        size=len(payload),
        sha256=digest,
    )
    bridge = wa.ApiBridge()
    bridge._update_cache = _bridge_info(asset)

    def explode(*_args: Any, **_kwargs: Any) -> Path:
        raise _ERRORS[error_kind](str(tmp_path / "home" / "updates" / "setup.exe"))

    monkeypatch.setattr(wa, "download_asset", explode)
    reply = bridge.download_update()

    assert reply["ok"] is False
    assert reply["path"] is None
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["error"]
    _assert_no_traceback(str(state["error"]))

    # The failed transfer must not wedge the single-download slot.
    def works(_url: str, dest: Path, **_kwargs: Any) -> Path:
        final = wa._updates_dir() / Path(str(dest)).name
        final.write_bytes(payload)
        return final

    monkeypatch.setattr(wa, "download_asset", works)
    reply = bridge.download_update()
    assert reply["ok"] is True
    assert bridge.update_download_state()["active"] is False


# ---------------------------------------------------------------------------
# storage: backup / restore / verify
# ---------------------------------------------------------------------------


def _storage_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _run_storage(argv: list[str]) -> None:
    args = _storage_parser().parse_args(argv)
    args.func(args)


@pytest.fixture()
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "raw"
    root.mkdir()
    paths = {
        "raw": root,
        "cache": tmp_path / "cache",
        "downloads": tmp_path / "downloads",
        "logs": tmp_path / "logs",
    }
    monkeypatch.setattr(storage, "_resolve_paths", lambda **_kwargs: paths)
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    yield root


def _seed_db(path: Path, *chapter_ids: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(path.parent, path.name) as store:
        store.upsert_chapters(
            [
                {
                    "id": chap_id,
                    "title": f"Title {chap_id}",
                    "content": f"Body {chap_id}",
                    "extra": {},
                }
                for chap_id in chapter_ids
            ]
        )


def _chapter_ids(path: Path) -> set[str]:
    with ChapterStorage(path.parent, path.name) as store:
        return store.existing_ids()


def _storage_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    _assert_no_traceback(captured.err)
    return payload


@contextmanager
def _fail_snapshot_point(
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    exc: OSError,
    *,
    skip: int = 0,
) -> Iterator[None]:
    """Inject a failure into ``storage._write_snapshot``'s publish sequence.

    ``write`` is the creation of the temporary copy (the first write the
    snapshot performs); ``fsync`` and ``replace`` are the durability and
    publish steps that follow it.
    """
    calls = {"n": 0}

    def _next() -> bool:
        index = calls["n"]
        calls["n"] += 1
        return index == skip

    with monkeypatch.context() as patch:
        if point == "write":

            def fake_mkstemp(*_args: Any, **_kwargs: Any) -> Any:
                if _next():
                    raise exc
                return real_mkstemp(*_args, **_kwargs)

            real_mkstemp = storage.tempfile.mkstemp
            patch.setattr(storage.tempfile, "mkstemp", fake_mkstemp)
        elif point == "fsync":
            real_fsync = os.fsync

            def fake_fsync(fd: int) -> None:
                if _next():
                    raise exc
                real_fsync(fd)

            patch.setattr(os, "fsync", fake_fsync)
        elif point == "replace":
            real_replace = os.replace

            def fake_replace(src: Any, dst: Any, **kwargs: Any) -> None:
                if _next():
                    raise exc
                real_replace(src, dst, **kwargs)

            patch.setattr(os, "replace", fake_replace)
        else:  # pragma: no cover - guards a typo in a parametrization
            raise AssertionError(f"unknown injection point: {point}")
        yield


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_backup_failure_leaves_the_sources_and_no_artifacts(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    error_kind: str,
) -> None:
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed_db(db, "c1", "c2")
    original = db.read_bytes()
    out = tmp_path / "backups"

    with (
        _fail_snapshot_point(monkeypatch, point, _ERRORS[error_kind](str(out))),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_storage(["storage", "backup", "--out", str(out), "--json"])
    assert exc_info.value.code == 1

    payload = _storage_json(capsys)
    assert payload["ok"] is False
    assert payload["error"] == "io_error"
    assert payload["count"] == 0 and payload["files"] == []
    assert _names_path(payload["message"], out)
    # A full disk never truncates the source, and no placeholder/half copy
    # survives the rolled-back run.
    assert db.read_bytes() == original
    assert _files_under(out) == []


@pytest.mark.parametrize("point", ["write", "fsync", "replace"])
@pytest.mark.parametrize("error_kind", _ERROR_IDS)
def test_restore_failure_rolls_back_and_leaves_no_backup_files(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    point: str,
    error_kind: str,
) -> None:
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed_db(db, "c1")
    backups = tmp_path / "backups"
    _run_storage(["storage", "backup", "--out", str(backups)])
    capsys.readouterr()  # drop the successful backup output
    backup = next(backups.rglob("chapter.raw.*.sqlite"))
    backup_bytes = backup.read_bytes()

    # The restore preserves the current database first, then writes the new
    # one; skip the preservation so the fault hits the install.
    with (
        _fail_snapshot_point(monkeypatch, point, _ERRORS[error_kind](str(db)), skip=1),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_storage(
            ["storage", "restore", "--from", str(backups), "--force", "--json"]
        )
    assert exc_info.value.code == 1

    payload = _storage_json(capsys)
    assert payload["ok"] is False
    assert payload["error"] == "io_error"
    # Rollback republishes a snapshot of the previous database (same chapters,
    # a WAL source has its committed frames folded in), so the invariant is
    # content, not the old byte layout.
    assert _chapter_ids(db) == {"c1"}
    assert not list(raw_root.rglob("*.pre-restore-*"))
    assert _temp_artifacts(raw_root) == []
    assert backup.read_bytes() == backup_bytes

    # Retrying once the fault is gone completes the restore.
    _run_storage(["storage", "restore", "--from", str(backups), "--force", "--json"])
    assert _storage_json(capsys)["restored"] == 1
    assert _chapter_ids(db) == {"c1"}
    assert backup.read_bytes() == backup_bytes


def test_backup_retries_after_the_permission_problem_clears(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed_db(db, "c1")
    original = db.read_bytes()
    out = tmp_path / "backups"

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _denied(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)

    with pytest.raises(SystemExit) as exc_info:
        _run_storage(["storage", "backup", "--out", str(out), "--json"])
    assert exc_info.value.code == 1
    assert _storage_json(capsys)["error"] == "io_error"
    assert _files_under(out) == []
    assert db.read_bytes() == original

    _run_storage(["storage", "backup", "--out", str(out), "--json"])
    payload = _storage_json(capsys)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert len(list(out.rglob("chapter.raw.*.sqlite"))) == 1
    assert db.read_bytes() == original


def test_backup_read_only_destination_reports_io_error(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed_db(db, "c1")
    original = db.read_bytes()
    out = tmp_path / "backups"
    out.mkdir()

    real_open = os.open
    prefix = str(out) + os.sep

    def denied_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        text = os.fspath(path)
        if text == str(out) or text.startswith(prefix):
            raise _denied(text)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(storage.os, "open", denied_open)

    with pytest.raises(SystemExit) as exc_info:
        _run_storage(["storage", "backup", "--out", str(out), "--json"])
    assert exc_info.value.code == 1
    payload = _storage_json(capsys)
    assert payload["error"] == "io_error"
    assert _names_path(payload["message"], out)
    assert _files_under(out) == []
    assert db.read_bytes() == original


def test_verify_reports_an_unreadable_database_with_its_path(
    raw_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed_db(db, "c1")
    original = db.read_bytes()

    real_connection = storage._source_connection

    @contextmanager
    def denied(path: Path) -> Iterator[Any]:
        if Path(path) == db:
            raise _denied(str(db))
        with real_connection(path) as conn:
            yield conn

    monkeypatch.setattr(storage, "_source_connection", denied)

    with pytest.raises(SystemExit) as exc_info:
        _run_storage(["storage", "verify", "--json"])
    assert exc_info.value.code == 1

    payload = _storage_json(capsys)
    assert payload["ok"] is False
    assert payload["corrupt"] == 1
    error = payload["errors"][0]
    # The report redacts a path under the home directory to ``~/...``.
    home = storage._home_dir()
    expected = str(db)
    if home and str(db).startswith(home + os.sep):
        expected = "~" + str(db)[len(home) :]
    assert error["path"] == expected
    assert error["reason"]
    # Verification is read-only: a denied read never writes to the source.
    assert db.read_bytes() == original
    assert _temp_artifacts(raw_root) == []


@_WINDOWS_ONLY
def test_replace_with_retry_recovers_when_a_windows_handle_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An antivirus/indexer handle is a transient sharing violation."""
    monkeypatch.setattr(storage, "_REPLACE_RETRY_INTERVAL", 0.01)
    target = tmp_path / "chapter.raw.20260102T030405Z.sqlite"
    target.write_bytes(b"old")
    source = tmp_path / ".snapshot.tmp"
    source.write_bytes(b"new")

    # The handle is released by a timer thread, so it cannot live in a ``with``.
    handle = builtins.open(target, "r+b")  # noqa: SIM115
    released = threading.Timer(0.15, handle.close)
    released.start()
    try:
        storage._replace_with_retry(source, target)
    finally:
        released.cancel()
        if not handle.closed:
            handle.close()

    assert target.read_bytes() == b"new"
    assert not source.exists()


@_WINDOWS_ONLY
def test_replace_with_retry_gives_up_and_leaves_the_source_for_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_REPLACE_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(storage, "_REPLACE_RETRY_TIMEOUT", 0.2)
    target = tmp_path / "chapter.raw.20260102T030405Z.sqlite"
    target.write_bytes(b"old")
    source = tmp_path / ".snapshot.tmp"
    source.write_bytes(b"new")

    with builtins.open(target, "r+b"), pytest.raises(PermissionError):
        storage._replace_with_retry(source, target)

    assert target.read_bytes() == b"old"
    assert source.read_bytes() == b"new"


@_WINDOWS_ONLY
def test_config_import_reports_a_file_held_by_another_process(
    config_work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sharing violation is exit 1 with the target untouched, and retryable."""
    target = config_work / "settings.toml"
    original = b"[general]\nworkers = 2\n"
    target.write_bytes(original)
    source = _write_source(config_work, "new.toml", "[general]\nworkers = 8\n")
    _quiet_ui(monkeypatch)

    # Held open across the import so Windows refuses the rename.
    with builtins.open(target, "r+b"):
        with pytest.raises(SystemExit) as exc_info:
            _run_config(["config", "import", "--from", str(source), "--json"])
        assert exc_info.value.code == 1
        payload = _json_line(capsys)
        assert payload["status"] == "error"
        assert target.read_bytes() == original
        assert _temp_artifacts(tmp_path) == []

    _run_config(["config", "import", "--from", str(source), "--json"])
    assert _json_line(capsys)["status"] == "imported"
    assert target.read_bytes() == source.read_bytes()
