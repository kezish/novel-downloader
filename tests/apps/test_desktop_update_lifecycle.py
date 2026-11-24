#!/usr/bin/env python3
"""
tests.apps.test_desktop_update_lifecycle
----------------------------------------

Lifecycle tests for the desktop client's background update:

* the automatic download switch, and the "exactly once" property under
  concurrent check results / repeated bridge events;
* failure and interruption recovery (the transfer slot is released, the
  progress state carries the error, and a retry works);
* the install gate: a finished background download never installs by itself,
  only an explicit ``install_update`` or the exit hook launches the installer;
* "skip this version" suppressing both the automatic download and the
  unattended install-on-exit, and a newer release restoring both;
* update-bar state synchronisation (bytes/percent/error/downloaded);
* no half-written final file when the app quits mid-transfer (a resumable
  ``.part`` is allowed by design).

The release feed, the transfer and the installer are replaced with fakes, and
``NOVEL_HOME``/the preference file stay inside ``tmp_path``; nothing here
touches the network or a real user profile.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common.updater import (
    ReleaseAsset,
    UpdateCheck,
    UpdateInfo,
)
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.desktop.web_assets import APP_HTML

_PAYLOAD = b"verified lifecycle update payload"
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_ASSET_NAME = "NovelDownloader-setup-9.0.0-x64.exe"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``app_home()`` (and therefore ``updates/``) inside ``tmp_path``."""
    target = tmp_path / "home"
    monkeypatch.setenv("NOVEL_HOME", str(target))
    return target


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory preference file, offline by default."""
    stored: dict[str, Any] = {
        # These tests drive the download/install state machine directly; an
        # automatic network check would only add noise (and reach the feed).
        "auto_check_updates": False,
        "last_update_check": str(time.time()),
    }
    monkeypatch.setattr(wa, "_load_prefs", lambda: dict(stored))
    monkeypatch.setattr(wa, "_save_prefs", lambda values: stored.update(values) or True)
    return stored


def _asset(**overrides: Any) -> ReleaseAsset:
    values: dict[str, Any] = {
        "name": _ASSET_NAME,
        "url": f"https://example.invalid/download/{_ASSET_NAME}",
        "size": len(_PAYLOAD),
        "sha256": _SHA,
    }
    values.update(overrides)
    return ReleaseAsset(**values)


def _info(*, latest: str = "9.0.0", asset: ReleaseAsset | None = None) -> UpdateInfo:
    chosen = _asset() if asset is None else asset
    return UpdateInfo(
        current="1.0.0",
        latest=latest,
        available=True,
        notes_url="https://example.invalid/notes",
        asset=chosen,
        assets=(chosen,),
    )


def _bridge(info: UpdateInfo | None = None) -> wa.ApiBridge:
    bridge = wa.ApiBridge()
    if info is not None:
        bridge._update_cache = wa._UpdateCache(
            info=info,
            error=None,
            checked_at=time.time(),
            current="1.0.0",
            platform="windows-x64",
        )
    return bridge


def _use_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")


def _recording_popen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``subprocess.Popen`` with a recorder that starts nothing."""
    calls: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> Any:
        calls.append(list(cmd))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    return calls


def _updates_dir() -> Path:
    return wa._updates_dir()


def _write_ready_file(name: str = _ASSET_NAME, *, payload: bytes = _PAYLOAD) -> Path:
    directory = _updates_dir()
    path = directory / name
    path.write_bytes(payload)
    return path


def _ready(
    name: str = _ASSET_NAME,
    *,
    version: str | None = "9.0.0",
    payload: bytes = _PAYLOAD,
) -> wa._DownloadedUpdate:
    path = _write_ready_file(name, payload=payload)
    return wa._DownloadedUpdate(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        name=path.name,
        version=version,
        size=len(payload),
    )


def _wait_until(predicate: Any, *, timeout: float = 5.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _wait_for_check(bridge: wa.ApiBridge, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._update_guard:
            thread = bridge._update_thread
        if thread is None:
            return
        thread.join(0.05)
    raise AssertionError("the update check did not finish in time")


def _wait_for_download(bridge: wa.ApiBridge, timeout: float = 5.0) -> None:
    _wait_until(
        lambda: bridge._downloaded is not None,
        timeout=timeout,
        message="the background download did not record a file",
    )


class _Transfer:
    """Scripted ``wa.download_asset`` replacement.

    ``script`` is consumed one entry per call; an empty script falls back to
    ``mode``.  Modes:

    * ``ok`` -- write the verified payload and report 100%;
    * ``fail`` -- raise before writing anything;
    * ``fail_partial`` -- write a ``.part`` file, report progress, then fail;
    * ``block`` / ``block_partial`` -- hold the transfer open until ``gate``
      is set (the partial variant writes ``.part`` bytes first).
    """

    def __init__(self, mode: str = "ok", script: list[str] | None = None) -> None:
        self.mode = mode
        self.script = list(script or [])
        self.calls = 0
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(
        self,
        url: str,
        dest: Path,
        expected_sha256: str | None = None,
        mirror: Any = None,
        **kwargs: Any,
    ) -> Path:
        mode = self.script.pop(0) if self.script else self.mode
        self.calls += 1
        root = Path(kwargs["root"])
        final = root / Path(dest).name
        part = final.with_name(final.name + ".part")
        progress = kwargs.get("progress")

        if mode in {"block", "block_partial"}:
            if mode == "block_partial":
                part.write_bytes(_PAYLOAD[:8])
                if progress is not None:
                    progress(8, len(_PAYLOAD))
            self.entered.set()
            if not self.gate.wait(5.0):
                raise OSError("the test never released the transfer")
            final.write_bytes(_PAYLOAD)
            part.unlink(missing_ok=True)
            if progress is not None:
                progress(len(_PAYLOAD), len(_PAYLOAD))
            return final
        if mode == "fail_partial":
            part.write_bytes(_PAYLOAD[:8])
            if progress is not None:
                progress(8, len(_PAYLOAD))
            raise OSError("connection reset")
        if mode == "fail":
            raise OSError("network down")
        final.write_bytes(_PAYLOAD)
        if progress is not None:
            progress(len(_PAYLOAD), len(_PAYLOAD))
        return final


# ---------------------------------------------------------------------------
# automatic download: the switch, and "exactly once"
# ---------------------------------------------------------------------------


def test_auto_download_disabled_never_transfers(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preference gates the transfer, not just the button label."""
    prefs["auto_download_updates"] = False
    transfer = _Transfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    bridge._maybe_auto_download()
    time.sleep(0.1)

    assert transfer.calls == 0
    assert bridge._download_active is False
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["ok"] is False
    assert state["error"] is None


def test_auto_download_enabled_fetches_once_for_concurrent_triggers(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four simultaneous triggers and one repeat event must fetch once."""
    transfer = _Transfer(mode="block")
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())
    barrier = threading.Barrier(4)

    def trigger() -> None:
        barrier.wait(5.0)
        bridge._maybe_auto_download()

    threads = [threading.Thread(target=trigger) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    try:
        assert transfer.entered.wait(5.0), "the first transfer never started"
        assert transfer.calls == 1
        # The reservation makes the bar show the transfer immediately.
        assert bridge.update_download_state()["active"] is True
    finally:
        transfer.gate.set()

    _wait_for_download(bridge)
    assert transfer.calls == 1

    # A repeated event after completion must not refetch either.
    bridge._maybe_auto_download()
    time.sleep(0.1)
    assert transfer.calls == 1
    assert bridge.update_download_state()["ok"] is True


def test_repeated_check_results_do_not_refetch_a_ready_update(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two completed background checks still produce one transfer."""
    transfer = _Transfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    monkeypatch.setattr(
        wa,
        "check_for_update",
        lambda *a, **k: UpdateCheck(
            info=_info(), error=None, checked_at=time.time(), source="manifest"
        ),
    )
    bridge = wa.ApiBridge()

    assert bridge._start_update_check() is True
    _wait_for_check(bridge)
    _wait_for_download(bridge)

    # Bypass the 24 h throttle: this is the second check result landing.
    assert bridge._start_update_check() is True
    _wait_for_check(bridge)
    time.sleep(0.1)

    assert transfer.calls == 1
    assert bridge._downloaded is not None
    assert bridge.update_state()["downloaded"] is True


def test_second_download_request_reuses_the_verified_file(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A double click after completion must not fetch the 60 MB asset again."""
    transfer = _Transfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    first = bridge.download_update()
    second = bridge.download_update()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["path"] == first["path"]
    assert transfer.calls == 1
    assert bridge.update_download_state()["ok"] is True

    # A file tampered with after the transfer is not reported as ready: it is
    # dropped and fetched afresh.
    Path(str(first["path"])).write_bytes(b"x" * len(_PAYLOAD))
    third = bridge.download_update()

    assert third["ok"] is True
    assert transfer.calls == 2
    assert Path(str(third["path"])).read_bytes() == _PAYLOAD


# ---------------------------------------------------------------------------
# failure / interruption recovery
# ---------------------------------------------------------------------------


def test_failed_transfer_releases_the_slot_and_can_be_retried(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped connection leaves a resumable ``.part``, not a dead end."""
    transfer = _Transfer(script=["fail_partial", "ok"])
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    failed = bridge.download_update()

    assert failed["ok"] is False
    assert "connection reset" in str(failed["error"])
    assert bridge._download_active is False
    assert bridge._downloaded is None
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["ok"] is False
    assert "connection reset" in str(state["error"])
    assert state["received"] == 8
    assert state["version"] == "9.0.0"
    assert bridge.update_state()["downloaded"] is False

    updates = _updates_dir()
    names = {entry.name for entry in updates.iterdir()}
    assert _ASSET_NAME not in names, "a failed transfer must not leave a final file"
    assert f"{_ASSET_NAME}.part" in names, "the resumable partial file stays"

    retried = bridge.download_update()

    assert retried["ok"] is True
    assert transfer.calls == 2
    assert bridge.update_download_state()["ok"] is True
    assert bridge.update_state()["downloaded"] is True
    assert Path(str(retried["path"])).read_bytes() == _PAYLOAD


def test_async_failure_is_published_and_retryable(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async entry point surfaces the error and frees the slot."""
    transfer = _Transfer(script=["fail", "ok"])
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    started = bridge.download_update_async()
    assert started["started"] is True

    _wait_until(
        lambda: not bridge.update_download_state()["active"],
        message="the failed worker never released the transfer slot",
    )
    failed = bridge.update_download_state()
    assert failed["ok"] is False
    assert "network down" in str(failed["error"])
    assert bridge._downloaded is None

    retried = bridge.download_update()
    assert retried["ok"] is True
    assert transfer.calls == 2
    assert bridge.update_download_state()["error"] is None


def test_quitting_mid_transfer_leaves_no_final_file(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted by exit: only the resumable ``.part`` may remain."""
    _use_windows(monkeypatch)
    calls = _recording_popen(monkeypatch)
    transfer = _Transfer(mode="block_partial")
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)
    updates = _updates_dir()
    names = {entry.name for entry in updates.iterdir()}
    assert _ASSET_NAME not in names
    assert f"{_ASSET_NAME}.part" in names

    # No verified record yet: quitting must not promote or launch anything.
    bridge._install_on_exit()

    assert calls == []
    assert bridge._downloaded is None
    assert not (_updates_dir() / _ASSET_NAME).exists()

    transfer.gate.set()
    _wait_for_download(bridge)
    assert Path(str(bridge._downloaded.path)).read_bytes() == _PAYLOAD


# ---------------------------------------------------------------------------
# the install gate: user confirmation or exit, never the download itself
# ---------------------------------------------------------------------------


def test_background_download_never_installs_before_the_user_confirms(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_windows(monkeypatch)
    calls = _recording_popen(monkeypatch)
    transfer = _Transfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    bridge._maybe_auto_download()
    _wait_for_download(bridge)

    assert calls == [], "a background download must not launch the installer"
    record = bridge._downloaded
    assert record is not None

    installed = bridge.install_update()

    assert installed["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == str(record.path)
    assert bridge._downloaded is None


def test_exit_install_failure_never_blocks_the_window(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken installer launch is logged and retried, never a stuck close."""
    _use_windows(monkeypatch)

    def broken_popen(_cmd: list[str], **_kwargs: Any) -> Any:
        raise OSError("installer is locked")

    monkeypatch.setattr("subprocess.Popen", broken_popen)
    bridge = _bridge(_info())
    record = _ready()
    bridge._downloaded = record

    assert bridge._on_window_closing() is True
    assert bridge._downloaded is record, "the package stays for the next quit"
    assert record.path.exists()

    def exploding_install() -> None:
        raise RuntimeError("install path is broken")

    monkeypatch.setattr(bridge, "_install_on_exit", exploding_install)
    assert bridge._on_window_closing() is True


def test_exit_install_honours_skip_until_the_release_changes(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip must cover the unattended exit install, not just the bar."""
    _use_windows(monkeypatch)
    calls = _recording_popen(monkeypatch)
    prefs["skipped_update_version"] = "9.0.0"
    bridge = _bridge(_info(latest="9.0.0"))
    record = _ready(version="9.0.0")
    bridge._downloaded = record

    bridge._install_on_exit()

    assert calls == []
    assert bridge._downloaded is record

    # The user un-skips (or a newer release lands): the next quit installs.
    prefs.pop("skipped_update_version", None)
    bridge._install_on_exit()

    assert len(calls) == 1
    assert bridge._downloaded is None


def test_portable_exit_install_does_not_schedule_a_second_close(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing hook is already closing the window; do not destroy twice."""
    _use_windows(monkeypatch)
    calls = _recording_popen(monkeypatch)
    monkeypatch.setattr(wa, "detect_install_shape", lambda: "portable")
    monkeypatch.setattr(wa.shutil, "which", lambda _name: "powershell.exe")
    bridge = _bridge(_info(latest="9.0.0"))
    scheduled: list[int] = []
    monkeypatch.setattr(bridge, "_schedule_exit", lambda: scheduled.append(1))
    bridge._downloaded = _ready("NovelDownloader-9.0.0-windows-x64.zip")

    bridge._install_on_exit()

    assert calls and calls[0][0] == "powershell.exe"
    assert scheduled == []

    # The button path still closes the window for the helper to do its work.
    bridge._downloaded = _ready("NovelDownloader-9.0.0-windows-x64.zip")
    assert bridge.install_update()["ok"] is True
    assert scheduled == [1]


# ---------------------------------------------------------------------------
# skip: automatic download stops, a newer release restores it
# ---------------------------------------------------------------------------


def test_skip_blocks_auto_download_until_a_newer_release(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _Transfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    prefs["skipped_update_version"] = "9.0.0"
    bridge = _bridge(_info(latest="9.0.0"))

    bridge._maybe_auto_download()
    time.sleep(0.1)

    assert transfer.calls == 0
    state = bridge.update_state()
    assert state["available"] is False
    assert state["skipped"] is True

    bridge._remember_check(
        _info(latest="9.1.0"),
        current="1.0.0",
        platform="windows-x64",
        checked_at=time.time(),
        error=None,
    )
    assert bridge.update_state()["available"] is True

    bridge._maybe_auto_download()
    _wait_for_download(bridge)
    assert bridge._downloaded is not None
    assert bridge._downloaded.version == "9.1.0"


# ---------------------------------------------------------------------------
# update-bar state: progress, error and the downloaded flag
# ---------------------------------------------------------------------------


def test_update_bar_state_tracks_progress_error_and_completion(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _Transfer(mode="block_partial")
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)

    running = bridge.update_download_state()
    assert running["active"] is True
    assert running["ok"] is False
    assert running["error"] is None
    assert running["received"] == 8
    assert running["total"] == len(_PAYLOAD)
    assert running["percent"] == int(8 * 100 / len(_PAYLOAD))
    assert bridge.update_state()["downloaded"] is False

    transfer.gate.set()
    _wait_until(
        lambda: bridge.update_download_state()["ok"],
        message="the transfer never completed",
    )

    done = bridge.update_download_state()
    assert done["active"] is False
    assert done["error"] is None
    assert done["percent"] == 100
    assert bridge.update_state()["downloaded"] is True
    assert bridge.update_state()["downloaded_size"] == len(_PAYLOAD)


def test_busy_async_request_is_not_reported_as_an_error(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A click during a healthy background transfer follows it, no toast."""
    transfer = _Transfer(mode="block")
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)

    second = bridge.download_update_async()

    assert second["started"] is False
    assert second["active"] is True
    assert second["error"] is None
    assert second.get("reason") == "busy"
    assert bridge.update_download_state()["active"] is True

    transfer.gate.set()
    _wait_for_download(bridge)
    assert transfer.calls == 1


def test_update_bar_reads_progress_and_errors_from_the_bridge() -> None:
    """The bar is wired to the bridge state instead of a frozen label."""
    assert 'api("update_download_state")' in APP_HTML
    assert "if (st.active)" in APP_HTML
    assert "if (st.error)" in APP_HTML
    assert "setUpdateInstallLabel(downloaded)" in APP_HTML
    assert "st.downloaded_size" in APP_HTML
    # The two buttons must react to the same state: download while running,
    # install once the verified file is on disk.
    assert "trackUpdateDownload()" in APP_HTML
    assert 't("Install and restart")' in APP_HTML
