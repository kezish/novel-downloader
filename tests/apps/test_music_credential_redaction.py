#!/usr/bin/env python3
"""Credential-redaction tests for the music module.

Every music-facing surface (logs, CLI stderr/stdout, CLI ``--json``, REST /
embedded JSON, exception messages and the persisted cookie/credential files)
must never echo a platform credential.  The fakes below embed the three
shapes that used to leak:

* the NetEase session cookie (``MUSIC_U=...``),
* an ``Authorization: Bearer`` token,
* URL userinfo (``https://user:pass@host/...``) plus a signed query string.

The tests also pin the other half of the contract: a normal failure stays
diagnosable (platform name, HTTP status and host survive the redaction).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.cli.commands import music as music_cmd
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.music import aggregate, weapi
from novel_downloader.apps.music.providers import creds
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.providers.health import health_check
from novel_downloader.apps.music.serialize import (
    redact_credentials,
    redact_headers,
    sanitize_reason,
    track_dict,
)
from novel_downloader.infra.logger import setup_logging
from novel_downloader.infra.paths import PACKAGE_NAME

#: Every secret marker used below; a redaction test asserts none survives.
MUSIC_U = "MUSIC_USECRET123"
BEARER = "BEARERTOKEN456"
PASSWORD = "PASSWORD789"
QUERY_TOKEN = "QUERYTOKEN000"
SECRETS = (MUSIC_U, BEARER, PASSWORD, QUERY_TOKEN)

#: One provider failure containing all three credential shapes.
EXC_TEXT = (
    "HTTP 403 from https://alice:" + PASSWORD + "@cdn.example.com/a.mp3"
    "?token=" + QUERY_TOKEN + "&expires=1700000000"
    " Cookie: MUSIC_U=" + MUSIC_U + "; __csrf=csrf"
    " Authorization: Bearer " + BEARER
)


def assert_clean(text: object) -> None:
    """Assert no credential marker appears in ``text``."""
    rendered = str(text)
    for secret in SECRETS:
        assert secret not in rendered, f"leaked {secret!r} in {rendered!r}"


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


@pytest.fixture()
def isolated_loggers() -> Iterator[None]:
    """Give each test an empty package logger (same pattern as the CLI tests)."""
    package_logger = logging.getLogger(PACKAGE_NAME)
    saved_handlers = package_logger.handlers[:]
    saved_level = package_logger.level
    saved_propagate = package_logger.propagate

    for handler in package_logger.handlers[:]:
        with suppress(Exception):
            handler.close()
    package_logger.handlers.clear()
    package_logger.setLevel(logging.DEBUG)
    package_logger.propagate = False
    try:
        yield
    finally:
        for handler in package_logger.handlers[:]:
            with suppress(Exception):
                handler.close()
        package_logger.handlers.clear()
        package_logger.handlers.extend(saved_handlers)
        package_logger.setLevel(saved_level)
        package_logger.propagate = saved_propagate


def test_music_log_records_are_redacted_at_debug_and_warning(
    isolated_loggers: None, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """DEBUG and WARNING lines (console + file) never carry the secrets."""
    logger = setup_logging(
        log_dir=tmp_path,
        log_filename="music",
        console=True,
        file=True,
        console_level="DEBUG",
        file_level="DEBUG",
    )
    logger.debug("music debug request: %s", EXC_TEXT)
    logger.debug("music bare cookie MUSIC_U=%s", MUSIC_U)
    logger.warning("music health failed: %s", EXC_TEXT)
    for handler in logger.handlers:
        handler.flush()

    stderr = capsys.readouterr().err
    log_file = (tmp_path / "music.log").read_text(encoding="utf-8")

    assert_clean(stderr)
    assert_clean(log_file)
    # The diagnostic part survives.
    assert "HTTP 403" in log_file
    assert "cdn.example.com/a.mp3" in log_file
    assert "music debug request" in log_file
    assert "music health failed" in log_file


# --------------------------------------------------------------------------- #
# shared redaction helpers / serialization
# --------------------------------------------------------------------------- #


def test_redact_credentials_masks_all_three_shapes() -> None:
    cleaned = redact_credentials(EXC_TEXT)

    assert_clean(cleaned)
    assert "HTTP 403" in cleaned
    assert "cdn.example.com/a.mp3" in cleaned


def test_sanitize_reason_keeps_platform_and_status() -> None:
    reason = sanitize_reason("QQ音乐 搜索失败（接口不可达或已变更）：" + EXC_TEXT)

    assert_clean(reason)
    assert "QQ音乐" in reason
    assert "HTTP 403" in reason


def test_track_dict_redacts_credential_headers() -> None:
    track = Track(
        provider="qianqian",
        id="1",
        name="Song",
        http_headers={
            "Cookie": "MUSIC_U=" + MUSIC_U + "; __csrf=csrf",
            "Authorization": "Bearer " + BEARER,
            "Referer": "https://cdn.example.com/a?token=" + QUERY_TOKEN,
            "User-Agent": "Mozilla/5.0",
        },
    )

    payload = track_dict(track)

    assert payload["http_headers"]["Cookie"] == "***"
    assert payload["http_headers"]["Authorization"] == "***"
    assert QUERY_TOKEN not in payload["http_headers"]["Referer"]
    assert "***" in payload["http_headers"]["Referer"]
    # Non-secret headers are kept as-is.
    assert payload["http_headers"]["User-Agent"] == "Mozilla/5.0"
    assert_clean(payload["http_headers"])


def test_redact_headers_keeps_the_mapping_shape() -> None:
    headers = redact_headers({"cookie": "MUSIC_U=" + MUSIC_U})

    assert headers == {"cookie": "***"}


# --------------------------------------------------------------------------- #
# provider errors: health / aggregate
# --------------------------------------------------------------------------- #


def test_health_check_error_is_redacted_and_keeps_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bad:
        def search(self, keyword: str, limit: int = 1) -> Any:
            raise RuntimeError(EXC_TEXT)

    monkeypatch.setattr(
        "novel_downloader.apps.music.providers.create",
        lambda key, **kwargs: Bad(),
    )

    result = health_check("qq", record_stats=False)

    assert result["ok"] is False
    assert_clean(result["error"])
    assert "HTTP 403" in result["error"]
    assert "RuntimeError" in result["error"]


def test_aggregate_platform_error_redacts_cookie_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))

    class Bad:
        def search(self, keyword: str, limit: int = 30, offset: int = 0) -> Any:
            raise RuntimeError(EXC_TEXT)

    monkeypatch.setattr(aggregate, "available", lambda: [("qq", "QQ")])
    monkeypatch.setattr(aggregate, "create", lambda key: Bad())
    aggregate.clear_failure_cache()
    try:
        result = aggregate.search_all("晴天", timeout=2.0)
    finally:
        aggregate.clear_failure_cache()

    error = result.platforms[0].error
    assert_clean(error)
    assert "HTTP 403" in error
    assert "cdn.example.com/a.mp3" in error


# --------------------------------------------------------------------------- #
# REST / embedded result payloads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_api_health_error_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def health(key: str, *, timeout: float = 5.0) -> dict[str, Any]:
        return {"ok": False, "elapsed_ms": 3, "error": EXC_TEXT}

    components = SimpleNamespace(
        available_labels=lambda: [("qq", "QQ")],
        health_check=health,
    )
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)

    items = await api_routes.music_health(platform="qq", timeout=1.0)

    assert items[0]["ok"] is False
    assert_clean(items[0]["error"])
    assert "HTTP 403" in items[0]["error"]


def test_api_download_result_reason_is_redacted() -> None:
    item = api_routes._music_result_item(
        SimpleNamespace(status="fail", reason=EXC_TEXT, path="", bytes_=0)
    )

    assert_clean(item["reason"])
    assert item["reason"].startswith("HTTP 403")


# --------------------------------------------------------------------------- #
# CLI: stderr, table and --json
# --------------------------------------------------------------------------- #


class _ThrowingProvider:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def search(self, keyword: str, *, limit: int) -> Any:
        raise self.exc


class _FakeProvider:
    def __init__(self, tracks: list[Any]) -> None:
        self.tracks = tracks

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        return list(self.tracks), len(self.tracks)


class _FakeEngine:
    """Minimal engine surface used by the search/health/download commands."""

    login_expired = type("FakeLoginExpired", (Exception,), {})

    def __init__(
        self,
        *,
        provider: Any,
        labels: list[tuple[str, str]],
        health_results: dict[str, dict[str, Any]] | None = None,
        download_records: dict[int, Any] | None = None,
    ) -> None:
        self.provider = provider
        self._labels = labels
        self.health_results = health_results or {}
        self.download_records = download_records or {}
        self.settings = SimpleNamespace(load_settings=lambda: {})
        self.download_calls: list[dict[str, Any]] = []

    def labels(self) -> list[tuple[str, str]]:
        return list(self._labels)

    def platform_keys(self) -> list[str]:
        return sorted(key for key, _label in self._labels)

    def platform_note(self, platform: str) -> str:
        return ""

    def create(self, platform: str) -> Any:
        return self.provider

    def default_download_dir(self) -> str:
        return ""

    def download_options(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def download_many(
        self, provider: Any, tracks: list[Any], options: Any, **kwargs: Any
    ) -> tuple[Any, ...]:
        self.download_calls.append({"tracks": list(tracks), "options": options})
        on_result = kwargs.get("on_result")
        for index, result in self.download_records.items():
            if on_result is not None:
                on_result(index, tracks[index], result)
        return 0, 0, len(tracks), [("Song 1", EXC_TEXT)], None

    def health_check(self, key: str, keyword: str) -> dict[str, Any]:
        return self.health_results.get(key, {"ok": True, "elapsed_ms": 1, "error": ""})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    MusicCmd.register(subparsers)
    return parser


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, engine: _FakeEngine) -> None:
    monkeypatch.setattr(music_cmd, "_require_engine", lambda: engine)
    args = _parser().parse_args(argv)
    args.func(args)


def _track(index: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id{index}",
        name=f"Song {index}",
        artists="Artist",
        album="Album",
        duration_s=185,
        vip=False,
        provider="qq",
        path="",
    )


def test_cli_search_stderr_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _FakeEngine(
        provider=_ThrowingProvider(RuntimeError(EXC_TEXT)),
        labels=[("qq", "QQ")],
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "--platform", "qq", "keyword"], monkeypatch, engine)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert_clean(err)
    assert "Search failed" in err
    assert "HTTP 403" in err


def test_cli_health_table_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _FakeEngine(
        provider=_FakeProvider([]),
        labels=[("qq", "QQ")],
        health_results={"qq": {"ok": False, "elapsed_ms": 2, "error": EXC_TEXT}},
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health", "--platform", "qq"], monkeypatch, engine)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err)
    assert "HTTP 403" in captured.out + captured.err


def test_cli_health_json_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _FakeEngine(
        provider=_FakeProvider([]),
        labels=[("qq", "QQ")],
        health_results={"qq": {"ok": False, "elapsed_ms": 2, "error": EXC_TEXT}},
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health", "--platform", "qq", "--json"], monkeypatch, engine)

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert_clean(json.dumps(payload, ensure_ascii=False))
    assert payload["platforms"][0]["error"].startswith("HTTP 403")


def test_cli_download_json_and_stderr_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    engine = _FakeEngine(
        provider=_FakeProvider([_track()]),
        labels=[("qq", "QQ")],
        download_records={
            0: SimpleNamespace(status="fail", reason=EXC_TEXT, path="", bytes_=0)
        },
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(
            [
                "music",
                "download",
                "--platform",
                "qq",
                "--keyword",
                "keyword",
                "--yes",
                "--json",
                "--out",
                str(tmp_path),
            ],
            monkeypatch,
            engine,
        )

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert_clean(captured.out + captured.err)
    payload = json.loads(captured.out)
    assert_clean(json.dumps(payload, ensure_ascii=False))
    assert payload["results"][0]["reason"].startswith("HTTP 403")


# --------------------------------------------------------------------------- #
# exception messages and chains (weapi)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def json(self) -> Any:
        raise ValueError("not json")


def test_weapi_non_json_error_does_not_echo_body() -> None:
    with pytest.raises(weapi.NeteaseError) as excinfo:
        weapi.NeteaseClient._parse(
            _FakeResponse("<html>bad request Cookie: MUSIC_U=" + MUSIC_U + "</html>")
        )

    assert_clean(str(excinfo.value))
    assert "返回非 JSON" in str(excinfo.value)


def test_weapi_login_error_and_cause_are_redacted() -> None:
    client = weapi.NeteaseClient()
    client.account = lambda: (_ for _ in ()).throw(RuntimeError(EXC_TEXT))  # type: ignore[method-assign]

    with pytest.raises(weapi.NeteaseError) as excinfo:
        client.login_by_cookie("MUSIC_U=abc")

    error = excinfo.value
    assert_clean(str(error))
    assert_clean(repr(error))
    assert error.__cause__ is not None
    assert_clean(str(error.__cause__))
    assert "校验登录态失败" in str(error)
    assert "HTTP 403" in str(error)


# --------------------------------------------------------------------------- #
# session.json / platform_creds.json: silent failure + permissions
# --------------------------------------------------------------------------- #


def test_missing_creds_file_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(creds, "_FILE", str(tmp_path / "missing.json"))

    assert creds.load() == {}
    captured = capsys.readouterr()
    assert captured.out + captured.err == ""


def test_corrupt_creds_file_is_silent_and_does_not_echo_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "platform_creds.json"
    target.write_text('{"qq": {"cookies": {"MUSIC_U": "' + MUSIC_U, encoding="utf-8")
    monkeypatch.setattr(creds, "_FILE", str(target))

    assert creds.load() == {}
    captured = capsys.readouterr()
    assert captured.out + captured.err == ""
    assert_clean(captured.out + captured.err)


def test_corrupt_session_file_is_silent_and_not_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "session.json"
    session.write_text('{"MUSIC_U": "' + MUSIC_U, encoding="utf-8")

    client = weapi.NeteaseClient(cookie_file=str(session))

    assert client.logged_in is False
    captured = capsys.readouterr()
    assert captured.out + captured.err == ""
    assert_clean(captured.out + captured.err)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_creds_file_is_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "platform_creds.json"
    monkeypatch.setattr(creds, "_FILE", str(target))

    creds.set("qq", {"cookies": {"MUSIC_U": MUSIC_U}})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_session_cookie_file_is_owner_only(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    client = weapi.NeteaseClient(cookie_file=str(session))
    client.s.cookies.set("MUSIC_U", MUSIC_U, domain=".music.163.com")
    client.save_cookies()

    assert stat.S_IMODE(session.stat().st_mode) == 0o600


def test_creds_write_is_atomic_and_creates_no_tmp_residue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "platform_creds.json"
    monkeypatch.setattr(creds, "_FILE", str(target))

    creds.set("qq", {"cookies": {"MUSIC_U": MUSIC_U}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "qq": {"cookies": {"MUSIC_U": MUSIC_U}}
    }
    assert not Path(str(target) + ".tmp").exists()
