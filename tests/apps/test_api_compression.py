#!/usr/bin/env python3
"""
tests.apps.test_api_compression
-------------------------------

Offline tests for gzip response compression on both HTTP transports, and for
its interaction with conditional requests.

Covered on the FastAPI stack (``httpx.ASGITransport`` against the real
``create_app`` middleware order):

* a JSON body of at least 1 KiB is gzipped when the client sends
  ``Accept-Encoding: gzip``, and stays identity otherwise (``identity``,
  ``br``, ``gzip;q=0``, no header);
* ``*`` and a weighted ``gzip;q=0.5`` admit gzip;
* a body below the 1 KiB threshold is never compressed and keeps an exact
  ``Content-Length``;
* the compressed response carries ``Content-Encoding: gzip``, drops
  ``Content-Length`` (the encoded length is unknown until the stream ends),
  and decompresses to the identity body;
* ``Vary: Accept-Encoding`` is present on identity, gzip and ``304``
  responses, and an existing ``Vary`` value is extended, not replaced;
* SSE (``text/event-stream``) and ``/downloads`` files are never compressed;
* the ``ETag`` is computed from the *uncompressed* representation, so it is
  identical for a gzip and an identity response, and ``If-None-Match`` still
  answers ``304`` in both directions;
* security headers, ``X-Request-ID`` and (with ``--cors``) the CORS headers
  coexist with the compressed body;
* the standalone middleware handles chunked bodies, a declared
  ``Content-Length``, a pre-existing ``Content-Encoding``, a ``304`` and a
  downstream app that ends mid-stream.

The embedded stdlib engine is tested for parity over a real loopback socket
(temp home, hermetic fakes): same gzip decision, same ``Vary``, the same
``ETag`` in both encoding modes, ``Content-Length`` matching the bytes on the
wire, and no compression for small bodies or ``/downloads``.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
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
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    CompressionMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.common.compression import (
    COMPRESSION_MIN_BYTES,
    accepts_gzip,
    gzip_bytes,
    is_compressible_status,
    is_json_media_type,
)
from novel_downloader.apps.common.conditional_get import (
    ETAG_ENDPOINTS,
    make_strong_etag,
)
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as eserver

#: The reference payload used for "large" cases: the real site registry
#: serialises to several KiB, comfortably above the 1 KiB threshold.
LARGE_PATH = "/api/v1/sites"

#: A response that is always small: health carries a status and a version.
SMALL_PATH = "/api/v1/health"


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
def downloads_dir(tmp_path: Path) -> Path:
    """A ``/downloads`` directory holding one file above the gzip threshold."""
    directory = tmp_path / "downloads"
    directory.mkdir()
    (directory / "big.txt").write_bytes(b"payload-" * 512)
    return directory


@pytest.fixture
def app(
    downloads_dir: Path,
    fake_manager: FakeManager,
    fake_music_components: None,
    history_dir: Path,
) -> FastAPI:
    application = create_app(output_dir=downloads_dir, rate_limit=None)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def cors_app(
    downloads_dir: Path,
    fake_manager: FakeManager,
    fake_music_components: None,
    history_dir: Path,
) -> FastAPI:
    application = create_app(output_dir=downloads_dir, cors=True, rate_limit=None)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def cors_client(cors_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vary_tokens(headers: httpx.Headers | dict[str, str]) -> set[str]:
    """Split a ``Vary`` header into lower-cased tokens."""
    raw = headers.get("vary") or ""
    return {token.strip().lower() for token in raw.split(",") if token.strip()}


async def _raw_get(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
) -> tuple[httpx.Response, bytes]:
    """Send a request and return the response plus the bytes on the wire.

    ``httpx`` decompresses ``response.content`` transparently, so the raw
    transport bytes are read from the stream to prove the body really is a
    gzip member rather than just labelled as one.
    """
    request = client.build_request(method, path, headers=headers)
    response = await client.send(request, stream=True)
    try:
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
        return response, raw
    finally:
        await response.aclose()


def _scope(
    path: str = LARGE_PATH,
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
            {"type": "http.response.start", "status": status, "headers": list(headers)}
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return downstream


def _streaming_downstream(
    chunks: list[bytes],
    *,
    headers: Iterable[tuple[bytes, bytes]] = ((b"content-type", b"application/json"),),
) -> Any:
    """ASGI app streaming several body chunks without a declared length."""

    async def downstream(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": list(headers)}
        )
        for index, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
            )

    return downstream


def _endless_downstream(head: Iterable[tuple[bytes, bytes]], chunk: bytes) -> Any:
    """ASGI app that emits one non-final chunk and then returns."""

    async def downstream(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": list(head)}
        )
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    return downstream


def _head_only_downstream(head: Iterable[tuple[bytes, bytes]]) -> Any:
    """ASGI app that emits a head and then returns without any body message."""

    async def downstream(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": list(head)}
        )

    return downstream


# ---------------------------------------------------------------------------
# Shared policy helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("gzip", True),
        ("GZIP", True),
        ("gzip, deflate, br", True),
        ("br, gzip;q=0.5", True),
        ("*", True),
        ("br, *;q=0.1", True),
        ("identity", False),
        ("br", False),
        ("gzip;q=0", False),
        ("gzip;q=0, *;q=1", False),
        ("gzip;q=not-a-number", False),
        ("", False),
        (None, False),
    ],
)
def test_accepts_gzip(header: str | None, expected: bool) -> None:
    assert accepts_gzip(header) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("application/vnd.api+json", True),
        ("text/event-stream", False),
        ("application/octet-stream", False),
        ("text/plain", False),
        ("", False),
        (None, False),
    ],
)
def test_is_json_media_type(value: str | None, expected: bool) -> None:
    assert is_json_media_type(value) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, True),
        (201, True),
        (204, False),
        (205, False),
        (206, False),
        (304, False),
        (404, False),
        (500, False),
    ],
)
def test_is_compressible_status(status: int, expected: bool) -> None:
    assert is_compressible_status(status) is expected


def test_gzip_bytes_is_a_deterministic_gzip_member() -> None:
    data = b"x" * 4096
    encoded = gzip_bytes(data)

    assert encoded[:2] == b"\x1f\x8b"
    assert gzip.decompress(encoded) == data
    assert gzip_bytes(data) == encoded  # zlib leaves the MTIME field at zero


# ---------------------------------------------------------------------------
# REST: negotiation and body framing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_json_is_gzipped_and_etag_covers_identity(
    client: httpx.AsyncClient,
) -> None:
    identity = await client.get(LARGE_PATH, headers={"Accept-Encoding": "identity"})
    gzip_response, raw = await _raw_get(client, LARGE_PATH, {"Accept-Encoding": "gzip"})

    identity_body = identity.content
    assert len(identity_body) >= COMPRESSION_MIN_BYTES

    # Identity: unchanged body, exact length, no encoding.
    assert identity.status_code == 200
    assert "content-encoding" not in identity.headers
    assert int(identity.headers["content-length"]) == len(identity_body)
    assert "accept-encoding" in _vary_tokens(identity.headers)

    # Gzip: a real gzip member that decodes to the identity representation.
    assert gzip_response.status_code == 200
    assert gzip_response.headers["content-encoding"] == "gzip"
    assert "content-length" not in gzip_response.headers
    assert "accept-encoding" in _vary_tokens(gzip_response.headers)
    assert raw[:2] == b"\x1f\x8b"
    assert gzip.decompress(raw) == identity_body

    # The validator is computed over the uncompressed representation.
    etag = gzip_response.headers["etag"]
    assert etag == identity.headers["etag"]
    assert etag == make_strong_etag(identity_body)


@pytest.mark.asyncio
async def test_small_json_is_not_compressed(client: httpx.AsyncClient) -> None:
    gzip_response, raw = await _raw_get(client, SMALL_PATH, {"Accept-Encoding": "gzip"})

    # ``/health`` carries a live uptime, so compare structure, not bytes.
    assert gzip_response.status_code == 200
    assert "content-encoding" not in gzip_response.headers
    assert int(gzip_response.headers["content-length"]) == len(raw)
    assert len(raw) < COMPRESSION_MIN_BYTES
    assert json.loads(raw)["status"] == "ok"
    assert "accept-encoding" in _vary_tokens(gzip_response.headers)


@pytest.mark.asyncio
async def test_large_response_is_compressed_without_an_etag(
    client: httpx.AsyncClient, fake_manager: FakeManager
) -> None:
    """Compression is not tied to the conditional-GET endpoint set."""
    for index in range(40):
        fake_manager.completed.append(
            DownloadTask(title=f"Book {index}", site="demo", book_id=f"b{index}")
        )

    identity = await client.get(
        "/api/v1/tasks", headers={"Accept-Encoding": "identity"}
    )
    gzip_response, raw = await _raw_get(
        client, "/api/v1/tasks", {"Accept-Encoding": "gzip"}
    )

    assert len(identity.content) >= COMPRESSION_MIN_BYTES
    assert "etag" not in identity.headers
    assert gzip_response.headers["content-encoding"] == "gzip"
    assert "etag" not in gzip_response.headers
    assert gzip.decompress(raw) == identity.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accept_encoding",
    ["identity", "br", "gzip;q=0", "compress;q=1, gzip;q=0"],
)
async def test_ineligible_accept_encoding_gets_identity(
    client: httpx.AsyncClient, accept_encoding: str
) -> None:
    response, raw = await _raw_get(
        client, LARGE_PATH, {"Accept-Encoding": accept_encoding}
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(raw)
    assert "accept-encoding" in _vary_tokens(response.headers)


@pytest.mark.asyncio
async def test_wildcard_accept_encoding_admits_gzip(
    client: httpx.AsyncClient,
) -> None:
    response, raw = await _raw_get(client, LARGE_PATH, {"Accept-Encoding": "*"})

    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(raw)


# ---------------------------------------------------------------------------
# REST: conditional requests across encodings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(ETAG_ENDPOINTS))
async def test_etag_is_identical_under_both_encodings(
    client: httpx.AsyncClient, path: str
) -> None:
    identity = await client.get(path, headers={"Accept-Encoding": "identity"})
    negotiated = await client.get(path, headers={"Accept-Encoding": "gzip"})

    assert identity.status_code == negotiated.status_code == 200
    assert identity.headers["etag"] == negotiated.headers["etag"]
    assert identity.headers["etag"] == make_strong_etag(identity.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(ETAG_ENDPOINTS))
async def test_if_none_match_returns_304_in_both_directions(
    client: httpx.AsyncClient, path: str
) -> None:
    identity = await client.get(path, headers={"Accept-Encoding": "identity"})
    etag = identity.headers["etag"]

    gzip_conditional = await client.get(
        path, headers={"If-None-Match": etag, "Accept-Encoding": "gzip"}
    )
    identity_conditional = await client.get(
        path, headers={"If-None-Match": etag, "Accept-Encoding": "identity"}
    )

    for response in (gzip_conditional, identity_conditional):
        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == etag
        assert "content-encoding" not in response.headers
        assert "content-length" not in response.headers
        assert "content-type" not in response.headers
        assert "accept-encoding" in _vary_tokens(response.headers)


@pytest.mark.asyncio
async def test_304_keeps_cache_security_and_request_id_headers(
    client: httpx.AsyncClient,
) -> None:
    first = await client.get(LARGE_PATH, headers={"Accept-Encoding": "gzip"})

    second = await client.get(
        LARGE_PATH,
        headers={
            "If-None-Match": first.headers["etag"],
            "Accept-Encoding": "gzip",
            REQUEST_ID_HEADER: "gzip-cond-1",
        },
    )

    assert second.status_code == 304
    assert second.headers["cache-control"] == "private, max-age=60"
    assert second.headers[REQUEST_ID_HEADER] == "gzip-cond-1"
    assert "accept-encoding" in _vary_tokens(second.headers)
    for name, value in SECURITY_HEADERS:
        assert second.headers[name] == value


@pytest.mark.asyncio
async def test_security_and_request_id_headers_on_compressed_response(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        LARGE_PATH,
        headers={"Accept-Encoding": "gzip", REQUEST_ID_HEADER: "gzip-200-1"},
    )

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers[REQUEST_ID_HEADER] == "gzip-200-1"
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value


# ---------------------------------------------------------------------------
# REST: exclusions (SSE, downloads, HEAD)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_is_never_compressed(client: httpx.AsyncClient) -> None:
    """``sites=`` searches nothing, so only the final ``done`` frame is sent."""
    response = await client.get(
        "/api/v1/search/stream",
        params={"keyword": "x", "sites": ""},
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response.headers
    assert b"event: done" in response.content
    assert "accept-encoding" in _vary_tokens(response.headers)


@pytest.mark.asyncio
async def test_downloads_are_never_compressed(
    client: httpx.AsyncClient, downloads_dir: Path
) -> None:
    expected = (downloads_dir / "big.txt").read_bytes()
    assert len(expected) >= COMPRESSION_MIN_BYTES

    response = await client.get(
        "/downloads/big.txt", headers={"Accept-Encoding": "gzip"}
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(expected)
    assert response.content == expected
    assert "accept-encoding" not in _vary_tokens(response.headers)


@pytest.mark.asyncio
async def test_head_is_not_compressed(client: httpx.AsyncClient) -> None:
    response = await client.head(LARGE_PATH, headers={"Accept-Encoding": "gzip"})

    # FastAPI serves HEAD for a GET route; regardless, there is no body to
    # encode and the head stays the identity representation's.
    assert response.status_code in (200, 405)
    assert "content-encoding" not in response.headers
    assert "accept-encoding" in _vary_tokens(response.headers)


@pytest.mark.asyncio
async def test_cors_headers_coexist_with_compression(
    cors_client: httpx.AsyncClient,
) -> None:
    response = await cors_client.get(
        LARGE_PATH,
        headers={
            "Accept-Encoding": "gzip",
            "Origin": "http://localhost:3000",
        },
    )

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["access-control-allow-origin"] == "*"
    assert "etag" in {
        token.strip().lower()
        for token in response.headers["access-control-expose-headers"].split(",")
    }
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value


# ---------------------------------------------------------------------------
# REST: middleware mechanics (standalone ASGI harness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_compresses_a_chunked_body_across_the_threshold() -> None:
    chunks = [b"a" * 700, b"b" * 700]
    middleware = CompressionMiddleware(
        _streaming_downstream(chunks), min_bytes=COMPRESSION_MIN_BYTES
    )

    status, headers, body = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert "content-length" not in headers
    assert gzip.decompress(body) == b"".join(chunks)
    assert headers["vary"] == "Accept-Encoding"


@pytest.mark.asyncio
async def test_middleware_passes_a_small_stream_through_unchanged() -> None:
    chunks = [b"a" * 10, b"b" * 10]
    middleware = CompressionMiddleware(
        _streaming_downstream(chunks), min_bytes=COMPRESSION_MIN_BYTES
    )

    status, headers, body = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert "content-encoding" not in headers
    assert body == b"".join(chunks)
    assert headers["vary"] == "Accept-Encoding"


@pytest.mark.asyncio
async def test_middleware_preserves_content_length_below_the_threshold() -> None:
    body = b"{}" * 20
    middleware = CompressionMiddleware(
        _fixed_downstream(
            body,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        ),
        min_bytes=4096,
    )

    status, headers, received = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert headers["content-length"] == str(len(body))
    assert "content-encoding" not in headers
    assert received == body
    assert headers["vary"] == "Accept-Encoding"


@pytest.mark.asyncio
async def test_middleware_drops_content_length_when_compressing() -> None:
    body = b"a" * 4096
    middleware = CompressionMiddleware(
        _fixed_downstream(
            body,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        )
    )

    status, headers, received = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert "content-length" not in headers
    assert gzip.decompress(received) == body


@pytest.mark.asyncio
async def test_middleware_leaves_non_json_content_uncompressed() -> None:
    body = b"a" * 4096
    middleware = CompressionMiddleware(
        _fixed_downstream(
            body, headers=[(b"content-type", b"application/octet-stream")]
        )
    )

    status, headers, received = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert "content-encoding" not in headers
    assert received == body
    assert headers["vary"] == "Accept-Encoding"


@pytest.mark.asyncio
async def test_middleware_never_compresses_a_downstream_304() -> None:
    middleware = CompressionMiddleware(_fixed_downstream(b"", status=304))

    status, headers, body = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 304
    assert body == b""
    assert "content-encoding" not in headers
    assert headers["vary"] == "Accept-Encoding"


@pytest.mark.asyncio
async def test_middleware_respects_an_existing_content_encoding() -> None:
    body = b"a" * 4096
    middleware = CompressionMiddleware(
        _fixed_downstream(
            body,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-encoding", b"br"),
            ],
        )
    )

    status, headers, received = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert headers["content-encoding"] == "br"
    assert received == body


@pytest.mark.asyncio
async def test_middleware_leaves_non_api_paths_untouched() -> None:
    body = b"a" * 4096
    middleware = CompressionMiddleware(
        _fixed_downstream(body, headers=[(b"content-type", b"application/json")])
    )

    status, headers, received = await _run_asgi(
        middleware,
        _scope(
            path="/downloads/big.txt",
            headers=[(b"accept-encoding", b"gzip")],
        ),
    )

    assert status == 200
    assert "content-encoding" not in headers
    assert "vary" not in headers
    assert received == body


@pytest.mark.asyncio
async def test_middleware_extends_an_existing_vary_header() -> None:
    body = b"a" * 4096
    middleware = CompressionMiddleware(
        _fixed_downstream(
            body,
            headers=[
                (b"content-type", b"application/json"),
                (b"vary", b"Origin"),
            ],
        )
    )

    _status, headers, _body = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert _vary_tokens(headers) == {"origin", "accept-encoding"}


@pytest.mark.asyncio
async def test_middleware_closes_a_stream_that_ends_without_a_final_chunk() -> None:
    middleware = CompressionMiddleware(
        _endless_downstream([(b"content-type", b"application/json")], b"a" * 4096)
    )

    status, headers, received = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(received) == b"a" * 4096


@pytest.mark.asyncio
async def test_middleware_finishes_a_head_only_response() -> None:
    middleware = CompressionMiddleware(
        _head_only_downstream([(b"content-type", b"application/json")])
    )

    status, headers, body = await _run_asgi(
        middleware, _scope(headers=[(b"accept-encoding", b"gzip")])
    )

    assert status == 200
    assert body == b""
    assert "content-encoding" not in headers
    assert headers["vary"] == "Accept-Encoding"


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
    (downloads / "big.txt").write_bytes(b"payload-" * 512)

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
        yield SimpleNamespace(port=port, home=home, downloads=downloads)
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


def test_embedded_gzip_parity(embedded: SimpleNamespace) -> None:
    identity_status, identity_headers, identity_body = _request(
        embedded.port, LARGE_PATH, {"Accept-Encoding": "identity"}
    )
    gzip_status, gzip_headers, gzip_body = _request(
        embedded.port, LARGE_PATH, {"Accept-Encoding": "gzip"}
    )

    assert identity_status == gzip_status == 200
    assert len(identity_body) >= COMPRESSION_MIN_BYTES
    assert "content-encoding" not in identity_headers
    assert int(identity_headers["content-length"]) == len(identity_body)

    assert gzip_headers["content-encoding"] == "gzip"
    # The embedded engine knows the whole encoded body, so it declares the
    # compressed length instead of dropping the header.
    assert int(gzip_headers["content-length"]) == len(gzip_body)
    assert gzip_body[:2] == b"\x1f\x8b"
    assert gzip.decompress(gzip_body) == identity_body

    identity_etag = identity_headers["etag"]
    assert gzip_headers["etag"] == identity_etag
    assert identity_etag == make_strong_etag(identity_body)
    assert "accept-encoding" in identity_headers["vary"].lower()
    assert "accept-encoding" in gzip_headers["vary"].lower()


def test_embedded_304_is_identical_in_both_encodings(
    embedded: SimpleNamespace,
) -> None:
    _status, identity_headers, _body = _request(
        embedded.port, LARGE_PATH, {"Accept-Encoding": "identity"}
    )
    etag = identity_headers["etag"]

    status, headers, body = _request(
        embedded.port,
        LARGE_PATH,
        {"Accept-Encoding": "gzip", "If-None-Match": etag},
    )

    assert status == 304
    assert body == b""
    assert headers["etag"] == etag
    assert "content-encoding" not in headers
    assert "content-length" not in headers
    assert "content-type" not in headers
    assert headers["cache-control"] == "private, max-age=60"
    assert "accept-encoding" in headers["vary"].lower()
    assert headers[REQUEST_ID_HEADER.lower()]
    for name, value in SECURITY_HEADERS:
        assert headers[name.lower()] == value

    # And the reverse direction: a validator read from a gzip response still
    # yields 304 for an identity client.
    _status, gzip_headers, _body = _request(
        embedded.port, LARGE_PATH, {"Accept-Encoding": "gzip"}
    )
    reverse_status, reverse_headers, reverse_body = _request(
        embedded.port,
        LARGE_PATH,
        {"Accept-Encoding": "identity", "If-None-Match": gzip_headers["etag"]},
    )
    assert reverse_status == 304
    assert reverse_body == b""
    assert reverse_headers["etag"] == etag


def test_embedded_small_json_is_not_compressed(embedded: SimpleNamespace) -> None:
    status, headers, body = _request(
        embedded.port, SMALL_PATH, {"Accept-Encoding": "gzip"}
    )

    assert status == 200
    assert "content-encoding" not in headers
    assert int(headers["content-length"]) == len(body)
    assert "accept-encoding" in headers["vary"].lower()


def test_embedded_br_and_identity_fall_back(embedded: SimpleNamespace) -> None:
    for accept_encoding in ("br", "identity", "gzip;q=0"):
        status, headers, body = _request(
            embedded.port, LARGE_PATH, {"Accept-Encoding": accept_encoding}
        )
        assert status == 200
        assert "content-encoding" not in headers
        assert int(headers["content-length"]) == len(body)


def test_embedded_downloads_are_not_compressed(embedded: SimpleNamespace) -> None:
    expected = (embedded.downloads / "big.txt").read_bytes()

    status, headers, body = _request(
        embedded.port, "/downloads/big.txt", {"Accept-Encoding": "gzip"}
    )

    assert status == 200
    assert "content-encoding" not in headers
    assert int(headers["content-length"]) == len(expected)
    assert body == expected
