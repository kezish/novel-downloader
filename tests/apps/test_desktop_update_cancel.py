#!/usr/bin/env python3
"""
tests.apps.test_desktop_update_cancel
-------------------------------------

Cancellation of the desktop client's update download.

Two layers are covered:

* ``updater.download_asset`` polls an injected ``should_cancel`` probe between
  chunks: a cancelled transfer raises :class:`DownloadCancelled`, never
  retries, and keeps its resumable ``.part`` (or drops it when ``resume`` is
  off, like every other non-resume failure).  A cancel that lands once the
  published bytes are all on disk loses to completion.
* The bridge exposes ``cancel_update_download``: the flag is set, the worker is
  joined with a bound, the terminal state is ``active=False, cancelled=True``
  with no error, and the next download starts from a clean slate.  Completion
  wins over a late cancel, a repeated cancel is idempotent, and closing the
  window cancels whatever is still running.

The release feed, the transfer and the installer are fakes; ``NOVEL_HOME`` and
the preference file stay inside ``tmp_path``, so nothing here touches the
network or a real user profile.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.common import updater
from novel_downloader.apps.common.updater import (
    DownloadCancelled,
    ReleaseAsset,
    UpdateInfo,
)
from novel_downloader.apps.desktop import webview_app as wa
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.infra.i18n import get_translation

_PAYLOAD = bytes(range(256)) * 2
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
    """In-memory preference file, with the automatic check switched off."""
    stored: dict[str, Any] = {
        "auto_check_updates": False,
        "last_update_check": str(time.time()),
    }
    monkeypatch.setattr(wa, "_load_prefs", lambda: dict(stored))
    monkeypatch.setattr(wa, "_save_prefs", lambda values: stored.update(values) or True)
    return stored


def _asset() -> ReleaseAsset:
    return ReleaseAsset(
        name=_ASSET_NAME,
        url=f"https://example.invalid/download/{_ASSET_NAME}",
        size=len(_PAYLOAD),
        sha256=_SHA,
    )


def _info(*, latest: str = "9.0.0") -> UpdateInfo:
    chosen = _asset()
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


def _updates_dir() -> Path:
    return wa._updates_dir()


def _names() -> set[str]:
    return {entry.name for entry in _updates_dir().iterdir()}


def _wait_until(predicate: Any, *, timeout: float = 5.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class _ChunkedResponse:
    """A fake ``urlopen`` result that serves small chunks and counts reads."""

    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.status = 200
        self.headers = {"Content-Length": str(len(payload))}
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        # Hand out a handful of bytes per call so the chunk loop iterates.
        return self._stream.read(min(size, 8) if size and size > 0 else size)

    @property
    def consumed(self) -> int:
        """How many payload bytes have been handed out so far."""
        return int(self._stream.tell())

    def __enter__(self) -> _ChunkedResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _ProbingTransfer:
    """``wa.download_asset`` replacement that honours ``should_cancel``.

    It writes one byte per step, reporting progress, so a cancel lands in the
    middle of an obviously incomplete ``.part``.  ``steps`` bounds a transfer
    that is never cancelled.
    """

    def __init__(self, *, delay: float = 0.002, steps: int = 400) -> None:
        self.delay = delay
        self.steps = steps
        self.calls = 0
        self.entered = threading.Event()

    def __call__(
        self,
        _url: str,
        dest: Path,
        _expected_sha256: str | None = None,
        _mirror: Any = None,
        **kwargs: Any,
    ) -> Path:
        self.calls += 1
        root = Path(kwargs["root"])
        final = root / Path(dest).name
        part = final.with_name(f"{final.name}.part")
        probe = kwargs.get("should_cancel") or (lambda: False)
        progress = kwargs.get("progress")
        written = 0
        with part.open("ab") as handle:
            for _ in range(self.steps):
                if probe():
                    raise DownloadCancelled("cancelled by the test")
                handle.write(_PAYLOAD[written : written + 1])
                written += 1
                if progress is not None:
                    progress(written, len(_PAYLOAD))
                self.entered.set()
                time.sleep(self.delay)
        part.replace(final)
        return final


class _InstantTransfer:
    """Completes at once, so a cancel can only ever be too late."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        _url: str,
        dest: Path,
        _expected_sha256: str | None = None,
        _mirror: Any = None,
        **kwargs: Any,
    ) -> Path:
        self.calls += 1
        root = Path(kwargs["root"])
        final = root / Path(dest).name
        final.write_bytes(_PAYLOAD)
        progress = kwargs.get("progress")
        if progress is not None:
            progress(len(_PAYLOAD), len(_PAYLOAD))
        return final


class _FinishingTransfer:
    """Ignores the probe and holds its success until released.

    Models the race the bridge must resolve: the last chunk is already in
    flight, the file is complete, and a cancel arrives just before the worker
    publishes ``ok``.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        _url: str,
        dest: Path,
        _expected_sha256: str | None = None,
        _mirror: Any = None,
        **kwargs: Any,
    ) -> Path:
        root = Path(kwargs["root"])
        final = root / Path(dest).name
        final.write_bytes(_PAYLOAD)
        self.entered.set()
        if not self.release.wait(5.0):
            raise OSError("the test never released the transfer")
        return final


# ---------------------------------------------------------------------------
# download_asset: the probe, the partial file, and completion
# ---------------------------------------------------------------------------


def test_download_asset_cancels_between_chunks_and_keeps_the_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _ChunkedResponse(_PAYLOAD)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: response)

    def probe() -> bool:
        # Cancelled only once bytes are on disk: the check must be inside the
        # chunk loop, not just before the request.
        return response.reads >= 2

    with pytest.raises(DownloadCancelled):
        updater.download_asset(
            f"https://example.invalid/{_ASSET_NAME}",
            Path(_ASSET_NAME),
            expected_sha256=_SHA,
            root=tmp_path,
            resume=True,
            should_cancel=probe,
        )

    final = tmp_path / _ASSET_NAME
    part = tmp_path / f"{_ASSET_NAME}.part"
    assert not final.exists(), "a cancelled transfer must not produce a final file"
    assert part.is_file(), "a resumable cancel keeps the .part for the next call"
    assert 0 < part.stat().st_size < len(_PAYLOAD)


def test_download_asset_cancelled_before_the_first_byte_never_opens_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("a cancelled transfer must not open a connection")

    monkeypatch.setattr(updater.urllib.request, "urlopen", forbidden)

    with pytest.raises(DownloadCancelled):
        updater.download_asset(
            "https://example.invalid/a.bin",
            Path("a.bin"),
            root=tmp_path,
            resume=True,
            should_cancel=lambda: True,
        )

    assert not list(tmp_path.iterdir())


def test_download_asset_cancel_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens: list[int] = []

    def opener(*_args: Any, **_kwargs: Any) -> _ChunkedResponse:
        opens.append(1)
        return _ChunkedResponse(_PAYLOAD)

    monkeypatch.setattr(updater.urllib.request, "urlopen", opener)

    with pytest.raises(DownloadCancelled):
        updater.download_asset(
            "https://example.invalid/a.bin",
            Path("a.bin"),
            root=tmp_path,
            resume=True,
            attempts=3,
            should_cancel=lambda: True,
        )

    assert opens == [], "a cancellation is terminal, not a retryable failure"


def test_download_asset_cancel_without_resume_drops_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as any other non-resume failure: no prefix is reused."""
    response = _ChunkedResponse(_PAYLOAD)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: response)

    with pytest.raises(DownloadCancelled):
        updater.download_asset(
            "https://example.invalid/a.bin",
            Path("a.bin"),
            root=tmp_path,
            resume=False,
            should_cancel=lambda: response.reads >= 1,
        )

    assert not (tmp_path / "a.bin").exists()
    assert not (tmp_path / "a.bin.part").exists()


def test_download_asset_completion_wins_over_a_late_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every published byte is on disk: verify + rename still runs."""
    response = _ChunkedResponse(_PAYLOAD)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *_a, **_k: response)

    written = updater.download_asset(
        "https://example.invalid/a.bin",
        Path("a.bin"),
        expected_sha256=_SHA,
        root=tmp_path,
        resume=True,
        expected_size=len(_PAYLOAD),
        # True only once the whole body has been handed out.
        should_cancel=lambda: response.consumed >= len(_PAYLOAD),
    )

    assert written == tmp_path / "a.bin"
    assert written.read_bytes() == _PAYLOAD
    assert not (tmp_path / "a.bin.part").exists()


def test_a_raising_probe_does_not_cancel_the_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken callback means "keep going", never a silent cancellation."""
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *_a, **_k: _ChunkedResponse(_PAYLOAD)
    )

    def probe() -> bool:
        raise RuntimeError("probe is broken")

    written = updater.download_asset(
        "https://example.invalid/a.bin",
        Path("a.bin"),
        expected_sha256=_SHA,
        root=tmp_path,
        should_cancel=probe,
    )

    assert written.read_bytes() == _PAYLOAD


# ---------------------------------------------------------------------------
# bridge: cancel a running transfer, retry, idempotence
# ---------------------------------------------------------------------------


def test_cancel_stops_the_transfer_leaving_no_final_file(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0), "the transfer never started"

    result = bridge.cancel_update_download()

    assert result["ok"] is True
    assert result["cancelled"] is True
    assert result["active"] is False
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["ok"] is False
    assert state["cancelled"] is True
    assert state["error"] is None, "a cancel is not a failure"
    assert state["version"] == "9.0.0"
    assert bridge._downloaded is None
    assert bridge.update_state()["downloaded"] is False

    names = _names()
    assert _ASSET_NAME not in names, "a cancelled transfer leaves no final file"
    assert f"{_ASSET_NAME}.part" in names, "the resumable partial stays"


def test_a_cancelled_transfer_can_be_retried_and_completes(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)
    assert bridge.cancel_update_download()["cancelled"] is True

    monkeypatch.setattr(wa, "download_asset", _InstantTransfer())
    retried = bridge.download_update()

    assert retried["ok"] is True
    state = bridge.update_download_state()
    assert state["ok"] is True
    assert state["cancelled"] is False, "the stale flag must not survive a retry"
    assert bridge.update_state()["downloaded"] is True
    assert Path(str(retried["path"])).read_bytes() == _PAYLOAD


def test_cancel_is_idempotent(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)

    first = bridge.cancel_update_download()
    second = bridge.cancel_update_download()
    third = bridge.cancel_update_download()

    for reply in (first, second, third):
        assert reply["ok"] is True
        assert reply["cancelled"] is True
        assert reply["active"] is False
    assert bridge.update_download_state()["cancelled"] is True
    assert transfer.calls == 1


def test_cancel_with_nothing_running_is_harmless(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(_info())

    reply = bridge.cancel_update_download()

    assert reply["ok"] is True
    assert reply["cancelled"] is False
    assert bridge._download_cancel.is_set() is False, "an idle cancel sets no flag"
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["cancelled"] is False

    # The next download is not poisoned by it.
    monkeypatch.setattr(wa, "download_asset", _InstantTransfer())
    assert bridge.download_update()["ok"] is True


def test_the_background_download_can_be_cancelled(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-download path goes through the same slot and flag."""
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    bridge._maybe_auto_download()
    assert transfer.entered.wait(5.0), "the background transfer never started"

    reply = bridge.cancel_update_download()

    assert reply["cancelled"] is True
    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["cancelled"] is True
    assert state["error"] is None
    assert bridge._downloaded is None
    names = _names()
    assert _ASSET_NAME not in names
    assert f"{_ASSET_NAME}.part" in names


def test_a_synchronous_manual_download_can_be_cancelled(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking ``download_update`` call is stopped by the same flag."""
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())
    results: list[dict[str, Any]] = []

    worker = threading.Thread(target=lambda: results.append(bridge.download_update()))
    worker.start()
    assert transfer.entered.wait(5.0), "the manual transfer never started"

    reply = bridge.cancel_update_download()

    # No worker thread is joinable for a synchronous call, so the request may
    # report "pending" and let the caller's own poll see the terminal state.
    assert reply["cancelled"] is True
    worker.join(5.0)
    _wait_until(
        lambda: not bridge.update_download_state()["active"],
        message="the cancelled manual transfer never unwound",
    )

    assert results and results[0]["ok"] is False
    assert results[0]["cancelled"] is True
    state = bridge.update_download_state()
    assert state["cancelled"] is True
    assert state["error"] is None
    assert bridge._downloaded is None
    assert _ASSET_NAME not in _names()


# ---------------------------------------------------------------------------
# races
# ---------------------------------------------------------------------------


def test_a_late_cancel_does_not_overwrite_a_completed_transfer(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion wins: the verified file is reported, never "cancelled"."""
    transfer = _FinishingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)

    replies: list[dict[str, Any]] = []

    def cancel() -> None:
        replies.append(bridge.cancel_update_download())

    thread = threading.Thread(target=cancel)
    thread.start()
    # Let the cancel raise the flag and start waiting, then let the worker
    # publish its success: the cancel arrived after the last chunk.
    _wait_until(
        lambda: bridge._download_cancel.is_set(),
        message="the cancel flag was never raised",
    )
    transfer.release.set()
    thread.join(5.0)

    assert replies and replies[0]["cancelled"] is False
    assert replies[0]["ok"] is True
    state = bridge.update_download_state()
    assert state["ok"] is True
    assert state["cancelled"] is False
    assert state["error"] is None
    assert bridge._downloaded is not None
    assert Path(str(bridge._downloaded.path)).read_bytes() == _PAYLOAD


def test_cancel_after_completion_keeps_the_verified_file(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = _InstantTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    _wait_until(
        lambda: not bridge._download_active,
        message="the transfer never finished",
    )

    reply = bridge.cancel_update_download()

    assert reply["cancelled"] is False
    assert reply["ok"] is True
    state = bridge.update_download_state()
    assert state["ok"] is True
    assert state["cancelled"] is False
    assert bridge._downloaded is not None
    assert (bridge.update_state()["downloaded"]) is True


# ---------------------------------------------------------------------------
# closing the window
# ---------------------------------------------------------------------------


def test_closing_the_window_cancels_the_running_transfer(
    prefs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    transfer = _ProbingTransfer()
    monkeypatch.setattr(wa, "download_asset", transfer)
    bridge = _bridge(_info())

    assert bridge.download_update_async()["started"] is True
    assert transfer.entered.wait(5.0)

    assert bridge._on_window_closing() is True

    state = bridge.update_download_state()
    assert state["active"] is False
    assert state["cancelled"] is True
    assert state["error"] is None
    assert bridge._downloaded is None, "a cancelled transfer installs nothing"
    names = _names()
    assert _ASSET_NAME not in names
    assert f"{_ASSET_NAME}.part" in names


# ---------------------------------------------------------------------------
# front end: the cancel button, the live region, the copy
# ---------------------------------------------------------------------------


def test_the_bar_offers_a_cancel_button_while_downloading() -> None:
    assert 'id="update-cancel" data-i18n="Cancel download" hidden' in APP_HTML
    assert '$("#update-cancel").onclick' in APP_HTML
    assert 'api("cancel_update_download")' in APP_HTML
    # Visibility follows the live state instead of a local guess.
    assert "function setUpdateCancelVisible(visible)" in APP_HTML
    assert "setUpdateCancelVisible(!!st.active);" in APP_HTML
    # The cancel button stays clickable while the other actions are busy.
    busy = APP_HTML.split("function setUpdateBusy(busy)", 1)[1].split("}", 1)[0]
    assert "update-cancel" not in busy


def test_the_cancelled_outcome_is_announced_in_a_live_region() -> None:
    assert 'id="update-status" role="status" aria-live="polite"' in APP_HTML
    assert "function setUpdateStatus(text)" in APP_HTML
    assert 'setUpdateStatus(t("Update download cancelled"))' in APP_HTML
    # A cancelled poll result is terminal, not an idle transfer to wait out.
    assert "if (st.ok || st.error || st.cancelled) return st;" in APP_HTML
    # The install click must not toast a failure for a cancel it caused.
    handler = APP_HTML.split('$("#update-install").onclick', 1)[1]
    assert "if (st && st.cancelled) return;" in handler


def test_the_cancel_copy_is_translated_in_zh_cn() -> None:
    catalog = wa._i18n_catalog()
    for key in ("Cancel download", "Update download cancelled"):
        assert key in catalog, f"missing front-end key: {key}"
        assert f'data-i18n="{key}"' in APP_HTML or f't("{key}")' in APP_HTML

    zh = get_translation("zh_CN")
    assert zh.gettext("Cancel download") == "取消下载"
    assert zh.gettext("Update download cancelled") == "已取消更新包下载"
