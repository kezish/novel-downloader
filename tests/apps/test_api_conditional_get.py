#!/usr/bin/env python3
"""
tests.apps.test_api_conditional_get
-----------------------------------

Offline tests for conditional ``GET`` support (``ETag`` / ``If-None-Match``)
on the cacheable reference endpoints, on both transports.

Covered:

* the first request returns ``200`` with a strong ``ETag`` derived from the
  exact response body (SHA-256 prefix) and the private cache policy;
* a matching ``If-None-Match`` returns ``304`` with no body while keeping
  ``ETag``, ``Cache-Control``, ``X-Request-ID`` and the security headers;
* weak comparison (``W/"..."``), a comma-separated list and ``*`` match;
  a non-matching validator returns the full ``200`` again;
* the validator varies with the query string, so a stale value cannot produce
  a false ``304``;
* live or credential-bearing endpoints (``/tasks*``, ``/login*``,
  ``/music/jobs*``, ``/health``) never receive an ``ETag`` and ignore the
  header, and non-``GET`` requests are untouched;
* the embedded engine behaves identically (including ``304``);
* responses above the size threshold are passed through without an ``ETag``;
* the OpenAPI document declares ``304``, ``ETag`` and ``If-None-Match`` on the
  eligible operations only.

The REST harness uses ``httpx.ASGITransport`` against the real
``create_app`` stack; the embedded harness starts the stdlib server on a
loopback port with hermetic fakes. No external network access.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.middleware import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    ConditionalGetMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.common.conditional_get import (
    ETAG_ENDPOINTS,
    if_none_match_matches,
    make_strong_etag,
)
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as eserver

#: Shape of the strong validator: quoted, 32 lowercase hex characters.
ETAG_PATTERN = re.compile(r'"[0-9a-f]{32}"')


class FakeManager:
    """Minimal in-memory stand-in for the web task manager."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []
        self.running: list[DownloadTask] = []
        self.completed: list[DownloadTask] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        task = DownloadTask(title=title, site=site, book_id=book_id)
        self.pending.append(task)
        return task

    async def cancel_task(self, task_id: str) -> bool:
        return True

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {
            "pending": list(self.pending),
            "running": list(self.running),
            "completed": list(self.completed),
        }

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# REST fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def fake_music_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``/music/platforms`` independent of the optional music extra."""
    components = SimpleNamespace(
        available_labels=lambda: [("netease", "NetEase Music")],
        platform_note=lambda key: f"note for {key}",
    )
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)


@pytest.fixture
def history_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``GET /history`` at a directory with two differently sized files."""
    output_dir = tmp_path / "history-scan"
    output_dir.mkdir()
    older = output_dir / "older.txt"
    older.write_text("a", encoding="utf-8")
    newer = output_dir / "newer.epub"
    newer.write_text("bb", encoding="utf-8")
    base = 1_700_000_000.0
    os.utime(older, (base, base))
    os.utime(newer, (base + 10, base + 10))
    monkeypatch.setattr(history_files, "_output_dir", lambda: output_dir)
    return output_dir


@pytest.fixture
def app(
    tmp_path: Path,
    fake_manager: FakeManager,
    fake_music_components: None,
    history_dir: Path,
) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", rate_limit=None)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Direct ASGI harness (threshold and pass-through cases)
# ---------------------------------------------------------------------------


def _scope(
    path: str = "/api/v1/sites",
    method: str = "GET",
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _run_asgi(app: Any, scope: Scope) -> tuple[int, dict[str, str], bytes]:
    """Drive an ASGI app and return ``(status, headers, body)``."""
    store: dict[str, Any] = {}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            store["start"] = message
        else:
            store["body"] = store.get("body", b"") + message.get("body", b"")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(scope, receive, send)
    start = store["start"]
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    return int(start["status"]), headers, store.get("body", b"")


def _fixed_downstream(
    body: bytes,
    *,
    status: int = 200,
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> Any:
    """ASGI app answering one body chunk with the given head."""

    async def downstream(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": list(headers),
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return downstream


def _streaming_downstream(chunks: list[bytes]) -> Any:
    """ASGI app streaming several body chunks without a declared length."""

    async def downstream(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for index, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
            )

    return downstream


# ---------------------------------------------------------------------------
# Validator helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('"abc"', True),
        ('W/"abc"', True),
        ('"nope"', False),
        ('"nope", "abc"', True),
        ('"nope",W/"abc"', True),
        ("*", True),
        ('"nope", *', True),
        ("", False),
        (None, False),
        ("garbage", False),
    ],
)
def test_if_none_match_comparison(header: str | None, expected: bool) -> None:
    """Weak comparison matches opaque tags; ``*`` matches anything."""
    assert if_none_match_matches(header, '"abc"') is expected


def test_strong_etag_is_sha256_prefix() -> None:
    body = b'{"hello": "world"}'
    etag = make_strong_etag(body)

    assert ETAG_PATTERN.fullmatch(etag)
    assert etag == '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    assert make_strong_etag(body) == etag
    assert make_strong_etag(body + b"!") != etag


# ---------------------------------------------------------------------------
# REST: 200 / 304 behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(ETAG_ENDPOINTS))
async def test_first_request_returns_strong_etag(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.content
    etag = response.headers["etag"]
    assert ETAG_PATTERN.fullmatch(etag)
    assert etag == make_strong_etag(response.content)
    assert response.headers["cache-control"] == CACHE_CONTROL_PRIVATE


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(ETAG_ENDPOINTS))
async def test_matching_etag_returns_304_without_body(
    client: httpx.AsyncClient, path: str
) -> None:
    first = await client.get(path)
    etag = first.headers["etag"]

    second = await client.get(path, headers={"If-None-Match": etag})

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == CACHE_CONTROL_PRIVATE
    # A 304 carries no representation metadata.
    assert "content-length" not in second.headers
    assert "content-type" not in second.headers


@pytest.mark.asyncio
async def test_304_keeps_security_and_request_id_headers(
    client: httpx.AsyncClient,
) -> None:
    first = await client.get("/api/v1/sites")

    second = await client.get(
        "/api/v1/sites",
        headers={
            "If-None-Match": first.headers["etag"],
            REQUEST_ID_HEADER: "cond-req-1",
        },
    )

    assert second.status_code == 304
    assert second.headers[REQUEST_ID_HEADER] == "cond-req-1"
    assert second.headers["etag"] == first.headers["etag"]
    assert second.headers["cache-control"] == CACHE_CONTROL_PRIVATE
    for name, value in SECURITY_HEADERS:
        assert second.headers[name] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["strong", "weak"])
async def test_strong_and_weak_comparison(
    client: httpx.AsyncClient, variant: str
) -> None:
    first = await client.get("/api/v1/sites")
    etag = first.headers["etag"]

    header = etag if variant == "strong" else f"W/{etag}"
    response = await client.get("/api/v1/sites", headers={"If-None-Match": header})

    assert response.status_code == 304
    assert response.content == b""


@pytest.mark.asyncio
async def test_multiple_etags_match_on_any_member(
    client: httpx.AsyncClient,
) -> None:
    first = await client.get("/api/v1/sites")
    etag = first.headers["etag"]

    matching = await client.get(
        "/api/v1/sites",
        headers={"If-None-Match": f'"0000000000000000000000000000dead", {etag}'},
    )
    missing = await client.get(
        "/api/v1/sites",
        headers={
            "If-None-Match": (
                '"0000000000000000000000000000dead", "0000000000000000000000000000beef"'
            )
        },
    )

    assert matching.status_code == 304
    assert missing.status_code == 200
    assert missing.headers["etag"] == etag


@pytest.mark.asyncio
async def test_star_matches_the_current_representation(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/sites", headers={"If-None-Match": "*"})

    assert response.status_code == 304
    assert response.content == b""
    assert ETAG_PATTERN.fullmatch(response.headers["etag"])


@pytest.mark.asyncio
async def test_non_matching_etag_returns_full_body(
    client: httpx.AsyncClient,
) -> None:
    first = await client.get("/api/v1/sites")

    second = await client.get(
        "/api/v1/sites",
        headers={"If-None-Match": '"0000000000000000000000000000dead"'},
    )

    assert second.status_code == 200
    assert second.content == first.content
    assert second.headers["etag"] == first.headers["etag"]


@pytest.mark.asyncio
async def test_history_query_string_changes_the_validator(
    client: httpx.AsyncClient,
) -> None:
    """A validator from one query must never 304 a different representation."""
    full = await client.get("/api/v1/history")
    page = await client.get("/api/v1/history", params={"limit": 1})

    assert full.status_code == page.status_code == 200
    assert full.headers["etag"] != page.headers["etag"]

    stale = await client.get(
        "/api/v1/history",
        params={"limit": 1},
        headers={"If-None-Match": full.headers["etag"]},
    )
    fresh = await client.get(
        "/api/v1/history",
        params={"limit": 1},
        headers={"If-None-Match": page.headers["etag"]},
    )

    assert stale.status_code == 200
    assert fresh.status_code == 304


# ---------------------------------------------------------------------------
# REST: endpoints that must stay unconditional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health",
        "/api/v1/tasks",
        "/api/v1/login/requests",
        "/api/v1/watchlist",
        "/api/v1/music/jobs/does-not-exist",
    ],
)
async def test_live_or_sensitive_endpoints_have_no_etag(
    client: httpx.AsyncClient, path: str
) -> None:
    first = await client.get(path)

    assert "etag" not in first.headers
    assert first.headers.get("cache-control") != CACHE_CONTROL_PRIVATE

    second = await client.get(path, headers={"If-None-Match": "*"})

    assert second.status_code != 304
    assert "etag" not in second.headers
    assert second.content


@pytest.mark.asyncio
async def test_post_is_never_conditional(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/tasks/t1/cancel", headers={"If-None-Match": "*"}
    )

    assert response.status_code == 200
    assert "etag" not in response.headers
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_head_is_not_given_a_validator(client: httpx.AsyncClient) -> None:
    # FastAPI serves HEAD as 405 here; either way no validator is emitted, and
    # a HEAD carries no body from which to derive one.
    response = await client.head("/api/v1/sites")

    assert response.status_code in (200, 405)
    assert "etag" not in response.headers


# ---------------------------------------------------------------------------
# REST: middleware mechanics (threshold, pass-through)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_above_threshold_is_served_without_etag() -> None:
    body = b"x" * 64
    middleware = ConditionalGetMiddleware(
        _fixed_downstream(body, headers=[(b"content-length", str(len(body)).encode())]),
        max_body_bytes=16,
    )

    status, headers, received = await _run_asgi(middleware, _scope())

    assert status == 200
    assert received == body
    assert "etag" not in headers


@pytest.mark.asyncio
async def test_streaming_fallback_does_not_duplicate_body() -> None:
    chunks = [b"a" * 10, b"b" * 10]
    middleware = ConditionalGetMiddleware(
        _streaming_downstream(chunks), max_body_bytes=16
    )

    status, headers, received = await _run_asgi(middleware, _scope())

    assert status == 200
    assert received == b"".join(chunks)
    assert "etag" not in headers


@pytest.mark.asyncio
async def test_body_exactly_at_threshold_is_eligible() -> None:
    body = b"x" * 16
    middleware = ConditionalGetMiddleware(_fixed_downstream(body), max_body_bytes=16)

    status, headers, received = await _run_asgi(middleware, _scope())

    assert status == 200
    assert received == body
    assert headers["etag"] == make_strong_etag(body)


@pytest.mark.asyncio
async def test_middleware_only_wraps_configured_get_paths() -> None:
    middleware = ConditionalGetMiddleware(_fixed_downstream(b"{}"))

    _status, other_path, _body = await _run_asgi(
        middleware, _scope(path="/api/v1/tasks")
    )
    _status, post, _body = await _run_asgi(middleware, _scope(method="POST"))
    _status, head, _body = await _run_asgi(middleware, _scope(method="HEAD"))

    assert "etag" not in other_path
    assert "etag" not in post
    assert "etag" not in head


@pytest.mark.asyncio
async def test_route_level_etag_is_authoritative() -> None:
    middleware = ConditionalGetMiddleware(
        _fixed_downstream(b"{}", headers=[(b"etag", b'"route"')])
    )

    status, headers, _body = await _run_asgi(
        middleware, _scope(headers=[(b"if-none-match", b'"route"')])
    )

    assert status == 200
    assert headers["etag"] == '"route"'


# ---------------------------------------------------------------------------
# OpenAPI contract
# ---------------------------------------------------------------------------


def test_openapi_documents_304_etag_and_if_none_match(app: FastAPI) -> None:
    spec = app.openapi()
    header = spec["components"]["headers"]["ETag"]
    assert header["schema"] == {"type": "string"}
    assert "SHA-256" in header["description"]
    parameter = spec["components"]["parameters"]["IfNoneMatch"]
    assert parameter["name"] == "If-None-Match"
    assert parameter["in"] == "header"
    assert parameter["required"] is False
    assert parameter["description"].strip()
    assert parameter["schema"] == {"type": "string"}

    for path in ETAG_ENDPOINTS:
        operation = spec["paths"][path]["get"]
        responses = operation["responses"]
        not_modified = responses["304"]
        assert not_modified["description"].strip()
        assert not_modified["headers"]["ETag"] == {"$ref": "#/components/headers/ETag"}
        assert not_modified["headers"][REQUEST_ID_HEADER] == {
            "$ref": "#/components/headers/RequestId"
        }
        assert "content" not in not_modified
        assert responses["200"]["headers"]["ETag"] == {
            "$ref": "#/components/headers/ETag"
        }
        refs = {
            entry.get("$ref")
            for entry in operation.get("parameters", [])
            if isinstance(entry, dict)
        }
        assert "#/components/parameters/IfNoneMatch" in refs


def test_openapi_leaves_live_endpoints_unconditional(app: FastAPI) -> None:
    spec = app.openapi()

    for path in ("/api/v1/tasks", "/api/v1/login/requests", "/api/v1/health"):
        operation = spec["paths"][path]["get"]
        assert "304" not in operation["responses"]
        assert "ETag" not in operation["responses"]["200"].get("headers", {})
        refs = {
            entry.get("$ref")
            for entry in operation.get("parameters", [])
            if isinstance(entry, dict)
        }
        assert "#/components/parameters/IfNoneMatch" not in refs


# ---------------------------------------------------------------------------
# Embedded engine parity
# ---------------------------------------------------------------------------


@pytest.fixture()
def embedded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Start the stdlib engine with a temp home and hermetic fakes."""
    home = tmp_path / "embedded_home"
    downloads = home / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "older.txt").write_text("a", encoding="utf-8")
    (downloads / "newer.epub").write_text("bb", encoding="utf-8")

    components = SimpleNamespace(
        available_labels=lambda: [("netease", "NetEase Music")],
        platform_note=lambda key: f"note for {key}",
    )
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {"pending": [], "running": [], "completed": []},
    )
    monkeypatch.setattr(
        "novel_downloader.apps.common.history_files._output_dir", lambda: downloads
    )
    monkeypatch.setenv("NOVEL_HOME", str(home))

    previous_cwd = os.getcwd()
    port = eserver.start_server(str(home), 0)
    try:
        yield SimpleNamespace(port=port, home=home)
    finally:
        eserver.stop_server()
        os.chdir(previous_cwd)


def _request(
    port: int, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """One GET over a fresh connection; headers are lower-cased."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            body,
        )
    finally:
        connection.close()


@pytest.mark.parametrize("path", sorted(ETAG_ENDPOINTS))
def test_embedded_conditional_get_parity(embedded: SimpleNamespace, path: str) -> None:
    status, headers, body = _request(embedded.port, path)

    assert status == 200
    assert body
    etag = headers["etag"]
    assert ETAG_PATTERN.fullmatch(etag)
    assert etag == make_strong_etag(body)
    assert headers["cache-control"] == CACHE_CONTROL_PRIVATE

    second_status, second_headers, second_body = _request(
        embedded.port, path, {"If-None-Match": etag}
    )

    assert second_status == 304
    assert second_body == b""
    assert second_headers["etag"] == etag
    assert second_headers["cache-control"] == CACHE_CONTROL_PRIVATE
    assert second_headers[REQUEST_ID_HEADER.lower()]
    for name, value in SECURITY_HEADERS:
        assert second_headers[name.lower()] == value
    assert "content-length" not in second_headers
    assert "content-type" not in second_headers


def test_embedded_weak_and_list_matching(embedded: SimpleNamespace) -> None:
    _status, headers, _body = _request(embedded.port, "/api/v1/sites")
    etag = headers["etag"]

    weak = _request(embedded.port, "/api/v1/sites", {"If-None-Match": f"W/{etag}"})
    listed = _request(
        embedded.port,
        "/api/v1/sites",
        {"If-None-Match": f'"0000000000000000000000000000dead", {etag}'},
    )
    star = _request(embedded.port, "/api/v1/sites", {"If-None-Match": "*"})
    missing = _request(
        embedded.port,
        "/api/v1/sites",
        {"If-None-Match": '"0000000000000000000000000000dead"'},
    )

    assert weak[0] == listed[0] == star[0] == 304
    assert missing[0] == 200
    assert missing[2]


def test_embedded_live_endpoint_has_no_etag(embedded: SimpleNamespace) -> None:
    """Live state gets no validator, but the REST ``no-store`` directive.

    The cache policy is now shared with REST (``apps.common.cache_policy``), so
    the engine marks task state uncacheable even though it never takes part in
    conditional requests.
    """
    status, headers, body = _request(
        embedded.port, "/api/v1/tasks", {"If-None-Match": "*"}
    )

    assert status == 200
    assert body
    assert "etag" not in headers
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE


def test_embedded_history_query_changes_validator(
    embedded: SimpleNamespace,
) -> None:
    _status, full_headers, _body = _request(embedded.port, "/api/v1/history")
    page_status, page_headers, _body = _request(
        embedded.port, "/api/v1/history?limit=1"
    )

    assert page_status == 200
    assert full_headers["etag"] != page_headers["etag"]

    stale_status, _stale_headers, stale_body = _request(
        embedded.port,
        "/api/v1/history?limit=1",
        {"If-None-Match": full_headers["etag"]},
    )
    assert stale_status == 200
    assert stale_body
