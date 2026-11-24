#!/usr/bin/env python3
"""
tests.apps.test_api_metrics
---------------------------

Contract tests for ``GET /api/v1/metrics`` and the counters behind it.

Covered here:

* exact counter semantics (totals, status classes, method buckets, rejection
  counters, SSE active/peak) and the absence of high-cardinality labels;
* reading a snapshot is side-effect free (only the probe itself joins
  ``requests.total``);
* thread safety under concurrent HTTP requests and under direct concurrent
  registry updates;
* the rate limiter's ``429`` and the body guard's ``413``/``415`` are counted
  in their dedicated counters;
* token protection (``401`` without a credential, ``200`` with either header
  form), ``Cache-Control: no-store`` and ``X-Request-ID``;
* the OpenAPI declaration matches the payload shape;
* the embedded Android engine does not serve the endpoint (explicit ``404``).

Everything runs offline through ``httpx.ASGITransport``; the only real socket
is the loopback listener of the embedded-server test.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import middleware as api_middleware
from novel_downloader.apps.api.metrics import (
    METHOD_BUCKETS,
    METRICS_RESPONSE_SCHEMA,
    STATUS_CLASSES,
    MetricsMiddleware,
    MetricsRegistry,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask

METRICS_PATH = "/api/v1/metrics"


class FakeManager:
    """Minimal in-memory task manager double (no persistence, no workers)."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []
        self.running: list[DownloadTask] = []
        self.completed: list[DownloadTask] = []

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {
            "pending": list(self.pending),
            "running": list(self.running),
            "completed": list(self.completed),
        }

    async def close(self) -> None:
        pass


class FakeClock:
    """Deterministic ``time.monotonic`` replacement (callable)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state outside the real user profile and drop stray tokens."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


def _build_app(
    tmp_path: Path,
    fake_manager: FakeManager,
    **kwargs: Any,
) -> FastAPI:
    """Build an app with a fake task manager and rate limiting off by default."""
    kwargs.setdefault("rate_limit", None)
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> AsyncIterator[httpx.AsyncClient]:
    application = _build_app(tmp_path, fake_manager)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


def test_registry_counts_every_dimension_exactly() -> None:
    clock = FakeClock()
    registry = MetricsRegistry(
        version="9.9.9",
        monotonic=clock,
        wall_clock=lambda: 1_700_000_000.0,
    )

    registry.request_started("get")
    registry.request_started("BREW")
    registry.request_finished(200)
    registry.request_finished(301)
    registry.request_finished(404)
    registry.request_finished(503)
    registry.request_finished(100)
    registry.record_rate_limited()
    registry.record_body_rejection(413)
    registry.record_body_rejection(415)
    registry.record_body_rejection(400)  # ignored by design
    registry.stream_opened()
    registry.stream_opened()
    registry.stream_closed()
    clock.now += 2.5

    snapshot = registry.snapshot()
    assert snapshot["version"] == "9.9.9"
    assert snapshot["started_at"] == "2023-11-14T22:13:20Z"
    assert snapshot["uptime_s"] == 2.5
    requests = snapshot["requests"]
    assert requests["total"] == 2
    assert requests["completed"] == 5
    assert requests["by_status_class"] == {
        "2xx": 1,
        "3xx": 1,
        "4xx": 1,
        "5xx": 1,
        "other": 1,
    }
    assert requests["by_method"] == {
        "GET": 1,
        "POST": 0,
        "PUT": 0,
        "PATCH": 0,
        "DELETE": 0,
        "HEAD": 0,
        "OPTIONS": 0,
        "other": 1,
    }
    assert requests["rejections"] == {
        "rate_limited": 1,
        "body_too_large": 1,
        "unsupported_media_type": 1,
    }
    assert snapshot["sse"] == {"active": 1, "peak": 2}


def test_snapshot_does_not_mutate_any_counter() -> None:
    clock = FakeClock()
    registry = MetricsRegistry(monotonic=clock, wall_clock=lambda: 0.0)
    registry.request_started("GET")
    registry.request_finished(200)
    registry.record_rate_limited()
    registry.stream_opened()

    first = registry.snapshot()
    second = registry.snapshot()

    assert first == second
    assert second["requests"]["total"] == 1
    assert second["requests"]["completed"] == 1
    assert second["sse"] == {"active": 1, "peak": 1}


def test_registry_updates_are_thread_safe() -> None:
    registry = MetricsRegistry()
    threads = 8
    per_thread = 500

    def worker() -> None:
        for _ in range(per_thread):
            registry.request_started("GET")
            registry.request_finished(200)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()

    snapshot = registry.snapshot()
    expected = threads * per_thread
    assert snapshot["requests"]["total"] == expected
    assert snapshot["requests"]["completed"] == expected
    assert snapshot["requests"]["by_status_class"]["2xx"] == expected
    assert snapshot["requests"]["by_method"]["GET"] == expected


# ---------------------------------------------------------------------------
# HTTP counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_requests_are_counted_and_probe_counts_itself(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/api/v1/sites")
    await client.get("/api/v1/tasks/missing")

    response = await client.get(METRICS_PATH)
    assert response.status_code == 200
    requests = response.json()["requests"]

    # The probe is request #3: it is already in ``total``/``by_method`` but its
    # response has not been completed when the snapshot is taken.
    assert requests["total"] == 3
    assert requests["completed"] == 2
    assert requests["by_method"]["GET"] == 3
    assert requests["by_status_class"]["2xx"] == 1
    assert requests["by_status_class"]["4xx"] == 1


@pytest.mark.asyncio
async def test_reading_metrics_is_read_only(client: httpx.AsyncClient) -> None:
    await client.get("/api/v1/sites")
    first = (await client.get(METRICS_PATH)).json()
    second = (await client.get(METRICS_PATH)).json()

    # Only the probes themselves are added; nothing is reset or rotated. The
    # first probe's 200 is completed by the time the second snapshot is taken.
    assert second["requests"]["total"] == first["requests"]["total"] + 1
    assert second["requests"]["completed"] == first["requests"]["completed"] + 1
    assert second["requests"]["rejections"] == first["requests"]["rejections"]
    assert (
        second["requests"]["by_status_class"]["2xx"]
        == first["requests"]["by_status_class"]["2xx"] + 1
    )
    for key in ("3xx", "4xx", "5xx", "other"):
        assert (
            second["requests"]["by_status_class"][key]
            == first["requests"]["by_status_class"][key]
        )
    assert second["sse"] == first["sse"]


@pytest.mark.asyncio
async def test_unhandled_exception_is_counted_as_5xx(tmp_path: Path) -> None:
    application = create_app(output_dir=tmp_path / "downloads", rate_limit=None)

    @application.get("/api/v1/boom")
    async def boom() -> dict[str, Any]:
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        assert (await http_client.get("/api/v1/boom")).status_code == 500
        payload = (await http_client.get(METRICS_PATH)).json()

    assert payload["requests"]["by_status_class"]["5xx"] == 1
    assert payload["requests"]["completed"] == 1


@pytest.mark.asyncio
async def test_concurrent_http_requests_are_counted_exactly(tmp_path: Path) -> None:
    application = create_app(output_dir=tmp_path / "downloads", rate_limit=None)
    # Build the middleware stack before the threads start so Starlette's lazy
    # stack construction is not itself the concurrency being measured.
    application.middleware_stack = application.build_middleware_stack()

    threads = 8
    per_thread = 25
    errors: list[BaseException] = []

    async def hammer() -> None:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http_client:
            for _ in range(per_thread):
                response = await http_client.get("/api/v1/sites")
                assert response.status_code == 200

    def worker() -> None:
        try:
            asyncio.run(hammer())
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()

    assert not errors
    snapshot = application.state.metrics.snapshot()
    expected = threads * per_thread
    assert snapshot["requests"]["total"] == expected
    assert snapshot["requests"]["completed"] == expected
    assert snapshot["requests"]["by_status_class"]["2xx"] == expected
    assert snapshot["requests"]["by_method"]["GET"] == expected

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        payload = (await http_client.get(METRICS_PATH)).json()
    assert payload["requests"]["total"] == expected + 1


# ---------------------------------------------------------------------------
# Rejection counters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_429_is_counted(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(api_middleware, "_monotonic", clock)
    application = _build_app(
        tmp_path,
        fake_manager,
        rate_limit=2,
        rate_limit_window=60.0,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        assert (await http_client.get("/api/v1/sites")).status_code == 200
        assert (await http_client.get("/api/v1/sites")).status_code == 200
        limited = await http_client.get("/api/v1/sites")
        assert limited.status_code == 429

        # Let the window drain so the metrics probe itself is admitted.
        clock.now += 61.0
        response = await http_client.get(METRICS_PATH)

    assert response.status_code == 200
    requests = response.json()["requests"]
    assert requests["rejections"]["rate_limited"] == 1
    assert requests["by_status_class"]["4xx"] == 1
    assert requests["by_status_class"]["2xx"] == 2


@pytest.mark.asyncio
async def test_body_guard_413_and_415_are_counted(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> None:
    application = _build_app(
        tmp_path,
        fake_manager,
        max_body_bytes=32,
    )
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        too_large = await http_client.post(
            "/api/v1/tasks",
            content=json.dumps(
                {"title": "x" * 200, "site": "qidian", "book_id": "1"}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert too_large.status_code == 413

        wrong_type = await http_client.post(
            "/api/v1/tasks",
            content=b"title=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert wrong_type.status_code == 415

        payload = (await http_client.get(METRICS_PATH)).json()

    rejections = payload["requests"]["rejections"]
    assert rejections["body_too_large"] == 1
    assert rejections["unsupported_media_type"] == 1
    assert payload["requests"]["by_status_class"]["4xx"] == 2


# ---------------------------------------------------------------------------
# Authentication and headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_requires_the_configured_token(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> None:
    application = _build_app(tmp_path, fake_manager, token="s3cret")
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        anonymous = await http_client.get(METRICS_PATH)
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == "Bearer"
        assert anonymous.headers["cache-control"] == "no-store"
        assert anonymous.headers["x-request-id"]

        bearer = await http_client.get(
            METRICS_PATH, headers={"Authorization": "Bearer s3cret"}
        )
        assert bearer.status_code == 200

        api_key = await http_client.get(METRICS_PATH, headers={"X-API-Key": "s3cret"})
        assert api_key.status_code == 200

    payload = api_key.json()
    assert payload["requests"]["by_status_class"]["4xx"] == 1


@pytest.mark.asyncio
async def test_metrics_response_headers_and_identity(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        METRICS_PATH, headers={"X-Request-ID": "trace-metrics-1"}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "trace-metrics-1"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("application/json")

    payload = response.json()
    assert payload["version"] == __version__
    assert payload["started_at"].endswith("Z")
    assert payload["uptime_s"] >= 0


# ---------------------------------------------------------------------------
# No high-cardinality labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_never_contains_paths_queries_or_client_identities(
    client: httpx.AsyncClient,
) -> None:
    for index in range(20):
        await client.get(f"/api/v1/scan-{index}?token=secret-{index}&ip=1.2.3.4")
    await client.request("BREW", "/api/v1/sites")

    response = await client.get(METRICS_PATH)
    text = response.text
    assert "scan-" not in text
    assert "secret-" not in text
    assert "1.2.3.4" not in text

    requests = response.json()["requests"]
    assert set(requests["by_status_class"]) == set(STATUS_CLASSES)
    assert set(requests["by_method"]) == set(METHOD_BUCKETS)
    assert requests["by_method"]["other"] == 1
    assert requests["total"] == 22
    assert requests["by_status_class"]["4xx"] == 21


# ---------------------------------------------------------------------------
# SSE gauge
# ---------------------------------------------------------------------------


class _StreamingApp:
    """Tiny ASGI app that opens an SSE response and waits for release."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True}
        )
        self.opened.set()
        await self.release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _no_receive() -> Message:
    return {"type": "http.disconnect"}


async def _discard_send(message: Message) -> None:
    return None


@pytest.mark.asyncio
async def test_sse_gauge_tracks_active_and_peak() -> None:
    registry = MetricsRegistry(monotonic=lambda: 0.0, wall_clock=lambda: 0.0)
    first = _StreamingApp()
    second = _StreamingApp()
    scope: Scope = {"type": "http", "method": "GET", "path": "/api/v1/tasks/stream"}

    first_task = asyncio.create_task(
        MetricsMiddleware(first, metrics=registry)(scope, _no_receive, _discard_send)
    )
    await first.opened.wait()
    assert registry.snapshot()["sse"] == {"active": 1, "peak": 1}

    second_task = asyncio.create_task(
        MetricsMiddleware(second, metrics=registry)(scope, _no_receive, _discard_send)
    )
    await second.opened.wait()
    assert registry.snapshot()["sse"] == {"active": 2, "peak": 2}

    first.release.set()
    await first_task
    assert registry.snapshot()["sse"] == {"active": 1, "peak": 2}

    second.release.set()
    await second_task
    snapshot = registry.snapshot()
    assert snapshot["sse"] == {"active": 0, "peak": 2}
    assert snapshot["requests"]["completed"] == 2


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_declares_metrics_endpoint_and_schema(tmp_path: Path) -> None:
    application = create_app(output_dir=tmp_path / "downloads", rate_limit=None)
    operation = application.openapi()["paths"][METRICS_PATH]["get"]

    assert operation["tags"] == ["system"]
    assert operation["summary"]
    assert operation["description"]
    assert operation["security"] == [
        {"BearerAuth": []},
        {"ApiKeyAuth": []},
    ]
    assert any(
        parameter.get("$ref") == "#/components/parameters/RequestId"
        for parameter in operation["parameters"]
    )

    responses = operation["responses"]
    assert {"200", "401", "429", "500"} <= set(responses)
    success = responses["200"]
    assert success["description"]
    assert "Cache-Control" in success["headers"]
    assert "X-Request-ID" in success["headers"]

    schema = success["content"]["application/json"]["schema"]
    assert schema == METRICS_RESPONSE_SCHEMA
    assert set(schema["properties"]) == {
        "version",
        "started_at",
        "uptime_s",
        "requests",
        "sse",
    }
    assert set(schema["properties"]["requests"]["properties"]) == {
        "total",
        "completed",
        "by_status_class",
        "by_method",
        "rejections",
    }
    assert set(schema["properties"]["sse"]["properties"]) == {"active", "peak"}


# ---------------------------------------------------------------------------
# Embedded engine
# ---------------------------------------------------------------------------


def test_embedded_engine_answers_404_for_metrics(tmp_path: Path) -> None:
    """The Android engine implements a subset; /metrics must be an explicit 404."""
    from novel_downloader.apps.embedded import server as eserver

    port = eserver.start_server(str(tmp_path / "embedded"), 0)
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{METRICS_PATH}")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 404
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert "detail" in payload
    finally:
        eserver.stop_server()
