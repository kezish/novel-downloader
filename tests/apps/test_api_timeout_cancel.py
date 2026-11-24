#!/usr/bin/env python3
"""
tests.apps.test_api_timeout_cancel
----------------------------------

Resource-leak tests for the API's blocking-endpoint deadlines and for client
disconnects (``asyncio.CancelledError``).

What is pinned here:

* ``504`` is delivered with the fixed ``detail`` and the *request* stops
  waiting at its budget, while the underlying coroutine/thread is eventually
  released (events/counters, not sleeps).
* A disconnect before the deadline propagates ``CancelledError`` instead of
  turning into a ``504`` (or a ``500``), and leaves no worker behind.
* A worker that finishes after the ``504`` has its result discarded: nothing
  is written to the music search cache and no second response is produced.
* Repeated timeouts do not accumulate OS threads: the blocking engine calls
  run on a fixed-size pool, so a burst (or a stream) of deadlines is bounded
  and other threadpool users keep their capacity.
* An operation raising ``CancelledError`` is not swallowed by the handlers'
  ``except Exception`` (``CancelledError`` is a ``BaseException``).
* The embedded stdlib transport mirrors the REST 504 wording and cancels its
  coroutine, but deliberately has no budget for music search/download (that
  difference is recorded, not "fixed").

Everything is offline: requests go through ``httpx.ASGITransport`` and both
the search plugin and the music engine are replaced with fakes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.constants import MSG_TIMED_OUT
from novel_downloader.apps.embedded import server as eserver
from novel_downloader.plugins import search as search_module
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` in-process without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def make_app(tmp_path: Path) -> Any:
    """Factory building apps whose export directory points at ``tmp_path``."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        return create_app(**kwargs)

    return _make


async def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` from the event loop without blocking it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _pool_threads() -> list[threading.Thread]:
    """Live threads of the API's bounded blocking pool."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(api_routes.BLOCKING_POOL_THREAD_PREFIX)
    ]


def _anyio_threads() -> list[threading.Thread]:
    """Live threads of anyio's shared default threadpool."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("AnyIO worker")
    ]


async def _drain_blocking_pool() -> None:
    """Wait until no previously queued pool work is left running.

    The pool is process-wide; earlier test modules may still occupy slots with
    their own (short) engine calls. Queueing ``BLOCKING_POOL_WORKERS`` no-ops
    behind them drains that backlog, so the thread-count assertions below
    measure this module's work only.
    """
    await asyncio.wait_for(
        asyncio.gather(
            *(
                api_routes._run_blocking(lambda: None)
                for _ in range(api_routes.BLOCKING_POOL_WORKERS)
            )
        ),
        timeout=30.0,
    )


async def _assert_pool_serves_again(make_app: Any, gate: _Gate) -> None:
    """Release the blocked calls and prove the pool serves requests again.

    The recovery request gets a generous budget on purpose: the pool is
    process-wide, so other test modules in the same run may still occupy a
    slot for a moment after their own workers unwind.
    """
    gate.open()
    assert await _wait_until(lambda: gate.finished_count == gate.started_count)
    app = make_app(timeouts={"music_search": 30.0})
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/music/search", json={"platform": "demo", "keyword": "x"}
        )
    assert response.status_code == 200


class _Gate:
    """A blocking engine call that records its lifecycle and waits for release."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = 0
        self.finished = 0
        self.release = threading.Event()

    def enter(self) -> int:
        with self._lock:
            self.started += 1
            return self.started

    def leave(self) -> None:
        with self._lock:
            self.finished += 1

    @property
    def started_count(self) -> int:
        with self._lock:
            return self.started

    @property
    def finished_count(self) -> int:
        with self._lock:
            return self.finished

    def open(self) -> None:
        self.release.set()


class _FakeTrack:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakePaths:
    def __init__(self, out_dir: Path) -> None:
        self._out_dir = Path(out_dir)

    def default_download_dir(self) -> Path:
        return self._out_dir


def _components(
    *,
    out_dir: Path,
    search: Any = None,
    download_mixed: Any = None,
) -> Any:
    """Build a ``_MusicComponents`` bundle backed by fakes."""
    provider = SimpleNamespace(
        search=search or (lambda keyword, limit, offset=0: ([], 0))
    )
    return api_routes._MusicComponents(
        paths=_FakePaths(out_dir),
        download_options=_FakeOptions,
        download_many=lambda *a, **k: (0, 0, 0, [], []),
        available_labels=lambda: [("demo", "Demo")],
        create=lambda platform: provider,
        platform_note=lambda platform: "",
        track_cls=_FakeTrack,
        export_m3u=lambda tracks, target: str(target),
        load_settings=lambda: {"out_dir": str(out_dir)},
        download_mixed=download_mixed or (lambda *a, **k: (0, 0, 0, [], [])),
    )


class _RecordingCache:
    """Stand-in for the music track cache that records ``put`` calls."""

    def __init__(self) -> None:
        self.puts: list[Any] = []

    def put(self, tracks: Any) -> str:
        self.puts.append(tracks)
        return "recording-search-id"

    def resolve(self, search_id: Any, fallback: Any) -> Any:
        return fallback


class _RecordingSearchCache:
    """Stand-in for ``plugins.search._CACHE`` that records writes."""

    def __init__(self) -> None:
        self.puts: list[Any] = []

    def get(self, key: Any) -> None:
        return None

    def put(self, key: Any, results: Any) -> None:
        self.puts.append((key, list(results)))


# ---------------------------------------------------------------------------
# REST: search deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_timeout_504_detail_and_coroutine_unwinds(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search over budget answers 504 and the cancelled coroutine ends."""
    started = asyncio.Event()
    unwound = asyncio.Event()

    async def hanging_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            unwound.set()
        return []

    monkeypatch.setattr(api_routes, "search", hanging_search)
    app = make_app(timeouts={"search": 0.05})

    async with _client(app) as client:
        began = time.monotonic()
        response = await client.post("/api/v1/search", json={"keyword": "x"})
        elapsed = time.monotonic() - began

        assert response.status_code == 504
        # Fixed detail: the budget, never a provider/exception string.
        assert response.json() == {
            "detail": MSG_TIMED_OUT.format(label="Search", timeout=0.05)
        }
        assert started.is_set()
        # The cancelled coroutine has already run its cleanup by the time the
        # 504 is returned: no task is left pending behind the response.
        assert unwound.is_set()
        assert elapsed < 5.0


@pytest.mark.asyncio
async def test_cancelled_search_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search cancelled at its deadline never writes the result cache."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _Searcher(BaseSearcher):
        site_name = "hangsite"

        async def _fetch_html(self, keyword: str) -> str:
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
            return ""

        def _parse_html(self, html_str: str, limit: int | None = None) -> list[Any]:
            return []

    cache = _RecordingSearchCache()
    monkeypatch.setattr(search_module, "_CACHE", cache)
    monkeypatch.setattr(search_module, "_browser_assisted_sites", lambda: frozenset())
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        lambda sites=None, load_all_if_none=True: [_Searcher],
    )

    with pytest.raises(HTTPException) as excinfo:
        await api_routes._with_timeout(
            search_module.search("x", timeout=30.0), 0.05, "Search"
        )

    assert excinfo.value.status_code == 504
    assert started.is_set()
    # The per-site task was cancelled and unwound; nothing reached the cache.
    assert cancelled.is_set()
    assert cache.puts == []


# ---------------------------------------------------------------------------
# REST: music (thread-backed) deadlines and disconnects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_search_timeout_discards_late_result(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker keeps running past the 504, but its result is dropped."""
    gate = _Gate()
    late_tracks = [object()]

    def slow_search(keyword: str, limit: int, offset: int = 0) -> tuple[list[Any], int]:
        gate.enter()
        try:
            # Outlive the request deadline, then produce a result.
            if gate.release.wait(5.0):
                return list(late_tracks), 1
            return [], 0
        finally:
            gate.leave()

    cache = _RecordingCache()
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, search=slow_search),
    )
    monkeypatch.setattr(
        "novel_downloader.apps.music.trackcache.CACHE", cache, raising=False
    )

    app = make_app(timeouts={"music_search": 0.05})
    try:
        await _drain_blocking_pool()
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/music/search", json={"platform": "demo", "keyword": "x"}
            )
            assert response.status_code == 504
            assert response.json() == {
                "detail": MSG_TIMED_OUT.format(label="Music search", timeout=0.05)
            }
            assert response.json().get("search_id") is None

            # Let the abandoned worker finish and report its late result.
            gate.open()
            assert await _wait_until(lambda: gate.finished_count == 1)
    finally:
        gate.open()

    # The late result was dropped instead of being cached or answered.
    assert cache.puts == []
    assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS


@pytest.mark.asyncio
async def test_music_download_timeout_returns_only_504(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A download that outlives its budget produces a bare 504 body."""
    gate = _Gate()
    callbacks: list[int] = []

    def slow_download(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        gate.enter()
        try:
            gate.release.wait(5.0)
            # This late callback must never reach a response.
            on_result(
                0,
                tracks[0],
                SimpleNamespace(status="ok", reason="", path="/late.mp3", bytes_=1),
            )
            callbacks.append(1)
            return len(tracks), 0, 0, [], []
        finally:
            gate.leave()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, download_mixed=slow_download),
    )

    app = make_app(timeouts={"music_download": 0.05})
    try:
        await _drain_blocking_pool()
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/music/download",
                json={"platform": "demo", "tracks": [{"id": "1"}]},
            )
            assert response.status_code == 504
            assert response.json() == {
                "detail": MSG_TIMED_OUT.format(label="Music download", timeout=0.05)
            }

            gate.open()
            assert await _wait_until(lambda: gate.finished_count == 1)
            assert callbacks == [1]
    finally:
        gate.open()

    assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS


@pytest.mark.asyncio
async def test_client_disconnect_cancels_search_without_504(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disconnecting before the deadline raises CancelledError, not a 504."""
    started = asyncio.Event()
    unwound = asyncio.Event()

    async def hanging_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            unwound.set()
        return []

    monkeypatch.setattr(api_routes, "search", hanging_search)
    app = make_app(timeouts={"search": 30.0})

    async with _client(app) as client:
        request = asyncio.create_task(
            client.post("/api/v1/search", json={"keyword": "x"})
        )
        assert await _wait_until(started.is_set)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        # Cancellation reached the endpoint: cleanup ran and no response
        # (in particular no 504) was produced.
        assert request.cancelled()
        assert unwound.is_set()


@pytest.mark.asyncio
async def test_client_disconnect_does_not_leak_music_thread(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disconnect stops the wait immediately; the worker is bounded, not leaked."""
    gate = _Gate()

    def blocking_search(
        keyword: str, limit: int, offset: int = 0
    ) -> tuple[list[Any], int]:
        gate.enter()
        try:
            gate.release.wait(10.0)
            return [], 0
        finally:
            gate.leave()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, search=blocking_search),
    )

    app = make_app(timeouts={"music_search": 30.0})
    before = len(_pool_threads())
    try:
        await _drain_blocking_pool()
        async with _client(app) as client:
            request = asyncio.create_task(
                client.post(
                    "/api/v1/music/search", json={"platform": "demo", "keyword": "x"}
                )
            )
            assert await _wait_until(lambda: gate.started_count == 1)

            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            # The disconnect did not wait for the provider: the worker is
            # still running and the request is already gone.
            assert request.cancelled()
            assert gate.finished_count == 0
            assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS

            gate.open()
            assert await _wait_until(lambda: gate.finished_count == 1)

            # The pool is reusable: a fresh request is served normally.
            response = await client.post(
                "/api/v1/music/search", json={"platform": "demo", "keyword": "x"}
            )
            assert response.status_code == 200
    finally:
        gate.open()

    assert len(_pool_threads()) <= max(before, api_routes.BLOCKING_POOL_WORKERS)


# ---------------------------------------------------------------------------
# REST: bounded threadpool under repeated/concurrent deadlines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_timeouts_do_not_accumulate_threads(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A stream of timeouts must not create one new OS thread per request.

    ``starlette.concurrency.run_in_threadpool`` hands its anyio limiter slot
    back as soon as the awaiting task is cancelled while the worker is still
    running, so each deadline used to spawn a fresh thread (unbounded). The
    dedicated pool keeps the count at its fixed size.
    """
    gate = _Gate()

    def blocking_search(
        keyword: str, limit: int, offset: int = 0
    ) -> tuple[list[Any], int]:
        gate.enter()
        try:
            gate.release.wait(10.0)
            return [], 0
        finally:
            gate.leave()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, search=blocking_search),
    )

    app = make_app(timeouts={"music_search": 0.02})
    try:
        await _drain_blocking_pool()
        async with _client(app) as client:
            for _ in range(12):
                response = await client.post(
                    "/api/v1/music/search",
                    json={"platform": "demo", "keyword": "x"},
                )
                assert response.status_code == 504

            # Every request timed out, but only the pool's slots ever ran: the
            # queue beyond them was cancelled instead of spawning a thread.
            assert gate.started_count == api_routes.BLOCKING_POOL_WORKERS
            assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS

            await _assert_pool_serves_again(make_app, gate)
    finally:
        gate.open()

    assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS


@pytest.mark.asyncio
async def test_concurrent_timeouts_keep_default_pool_free(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A burst of deadlines neither grows threads nor consumes anyio's pool."""
    gate = _Gate()

    def blocking_search(
        keyword: str, limit: int, offset: int = 0
    ) -> tuple[list[Any], int]:
        gate.enter()
        try:
            gate.release.wait(10.0)
            return [], 0
        finally:
            gate.leave()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, search=blocking_search),
    )

    app = make_app(timeouts={"music_search": 0.05})
    anyio_before = len(_anyio_threads())
    try:
        await _drain_blocking_pool()
        async with _client(app) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/music/search",
                        json={"platform": "demo", "keyword": "x"},
                    )
                    for _ in range(24)
                )
            )

            for response in responses:
                assert response.status_code == 504
                assert response.json() == {
                    "detail": MSG_TIMED_OUT.format(label="Music search", timeout=0.05)
                }

            # Bounded on both axes: at most the pool's slots ran, and the rest
            # of the burst was cancelled while still queued.
            assert gate.started_count == api_routes.BLOCKING_POOL_WORKERS
            assert len(_pool_threads()) <= api_routes.BLOCKING_POOL_WORKERS
            # The shared anyio pool (used by export/health and other code) was
            # never touched, so those endpoints keep all their capacity.
            assert len(_anyio_threads()) <= anyio_before

            await _assert_pool_serves_again(make_app, gate)
    finally:
        gate.open()


# ---------------------------------------------------------------------------
# REST: cancellation is not swallowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_timeout_propagates_cancelled_error() -> None:
    """``_with_timeout`` only maps TimeoutError; cancellation passes through."""

    async def cancelled_operation() -> None:
        raise asyncio.CancelledError

    task = asyncio.create_task(
        api_routes._with_timeout(cancelled_operation(), 5.0, "Search")
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_music_download_cancelled_error_is_not_a_502(
    make_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The download handler's ``except Exception`` must not catch cancellation."""
    gate = _Gate()

    def cancelling_download(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> Any:
        gate.enter()
        try:
            raise asyncio.CancelledError
        finally:
            gate.leave()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, download_mixed=cancelling_download),
    )

    app = make_app(timeouts={"music_download": 30.0})
    await _drain_blocking_pool()
    async with _client(app) as client:
        request = asyncio.create_task(
            client.post(
                "/api/v1/music/download",
                json={"platform": "demo", "tracks": [{"id": "1"}]},
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await request

    # The cancellation was not converted into the engine-failure 502.
    assert request.cancelled()
    assert await _wait_until(lambda: gate.finished_count == 1)


# ---------------------------------------------------------------------------
# Embedded transport: parity and recorded differences
# ---------------------------------------------------------------------------


def test_embedded_run_504_detail_and_coroutine_cancelled() -> None:
    """The embedded bridge answers the REST wording and cancels the coroutine."""
    started = threading.Event()
    cancelled = threading.Event()

    async def slow() -> str:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
        return "never"

    # Make sure the engine loop is up before the (very short) budget starts.
    eserver._ensure_loop()
    with pytest.raises(eserver._ApiError) as excinfo:
        eserver._run(slow(), timeout=0.05, label="Search")

    assert excinfo.value.status == 504
    assert excinfo.value.detail == MSG_TIMED_OUT.format(label="Search", timeout=0.05)
    assert started.wait(2.0)
    # ``future.cancel()`` is a request, not a kill: the coroutine must observe
    # it and run its cleanup on the engine loop.
    assert cancelled.wait(2.0)


def test_embedded_budgets_match_rest_except_music(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Record the transports' budgets: search/browse/jobs mirror REST, while the
    synchronous music search/download handlers have no deadline at all.

    The embedded server is a stdlib ``ThreadingHTTPServer`` request per
    thread; adding a per-request kill timer there was deliberately left out,
    so the difference is pinned instead of silently drifting.
    """
    assert api_routes.BLOCKING_TIMEOUTS["search"] == eserver._SEARCH_TIMEOUT
    assert api_routes.BLOCKING_TIMEOUTS["browse"] == eserver._BROWSE_TIMEOUT
    assert api_routes.BLOCKING_TIMEOUTS["music_download"] == eserver._MUSIC_JOB_TIMEOUT
    assert not hasattr(eserver, "_MUSIC_SEARCH_TIMEOUT")
    assert not hasattr(eserver, "_MUSIC_DOWNLOAD_TIMEOUT")

    calls: list[float] = []

    def slow_search(keyword: str, limit: int, offset: int = 0) -> tuple[list[Any], int]:
        # Longer than the REST budget this test uses for comparison, so a
        # deadline (if there were one) would have fired.
        time.sleep(0.1)
        calls.append(time.monotonic())
        return [], 0

    components = _components(out_dir=tmp_path, search=slow_search)
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    monkeypatch.setattr(
        "novel_downloader.apps.music.trackcache.CACHE", _RecordingCache(), raising=False
    )
    payload = eserver._handle_music_search({"platform": "demo", "keyword": "x"}, {}, {})

    # The handler waited for the provider to finish: no 504, no cancellation.
    assert payload["total"] == 0
    assert payload["tracks"] == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Module invariants used by the assertions above
# ---------------------------------------------------------------------------


def test_blocking_pool_is_bounded() -> None:
    """The pool size is a small, positive constant (a real upper bound)."""
    assert isinstance(api_routes.BLOCKING_POOL_WORKERS, int)
    assert 0 < api_routes.BLOCKING_POOL_WORKERS <= 32
    assert api_routes.BLOCKING_POOL_THREAD_PREFIX


@pytest.mark.asyncio
async def test_run_blocking_returns_result_and_propagates_errors() -> None:
    """Sanity: the helper is a transparent replacement for the threadpool call."""
    assert await api_routes._run_blocking(lambda value: value + 1, 41) == 42

    def boom() -> None:
        raise ValueError("engine failed")

    with pytest.raises(ValueError, match="engine failed"):
        await api_routes._run_blocking(boom)
