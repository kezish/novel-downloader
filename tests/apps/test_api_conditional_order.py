#!/usr/bin/env python3
"""
tests.apps.test_api_conditional_order
-------------------------------------

Contract tests for the interaction order between conditional ``GET``
(``ETag`` / ``If-None-Match`` / ``304``) and the surrounding hardening
layers: token auth, CORS, the default-mode browser-origin gate, the per-IP
rate limiter, and the HTTP method restriction.

Documented order (outermost first, i.e. ``add_middleware`` in reverse)::

    RequestId -> Logging -> SecurityHeaders -> CORS -> CacheControl
    -> ConditionalGet -> RateLimit -> DownloadsAuth -> BodySizeLimit
    -> OriginCheck -> router (token dependency)

The security-relevant consequence is that every layer that can answer a
request *rejecting* it sits inside :class:`ConditionalGetMiddleware`, and
that middleware converts only a ``200`` into a ``304``. An unauthenticated,
cross-origin or rate-limited caller therefore always sees ``401`` / ``403`` /
``429``, never a ``304`` that would confirm the representation exists and
still matches.

Everything is offline: REST requests go through ``httpx.ASGITransport``; the
embedded checks use the stdlib engine on a loopback port.
"""

from __future__ import annotations

import http.client
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api.app import CORS_ALLOW_HEADERS, CORS_EXPOSE_HEADERS
from novel_downloader.apps.api.middleware import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    CacheControlMiddleware,
    ConditionalGetMiddleware,
    DownloadsAuthMiddleware,
    OriginCheckMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as eserver

#: Endpoint used throughout: the cheapest ETag-eligible reference route.
SITES = "/api/v1/sites"

#: Configured token for the auth-ordering tests.
TOKEN = "order-test-token"

#: A foreign (non-loopback) origin: rejected by the default-mode gate.
FOREIGN_ORIGIN = "https://evil.example"

#: A loopback origin: passes the default-mode gate.
LOOPBACK_ORIGIN = "http://127.0.0.1:5173"


# ---------------------------------------------------------------------------
# REST harness
# ---------------------------------------------------------------------------


class FakeManager:
    """In-memory task manager so POST/DELETE routes never queue real work."""

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        return DownloadTask(title=title, site=site, book_id=book_id)

    async def cancel_task(self, task_id: str) -> bool:
        return True

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": [], "running": [], "completed": []}

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def make_app(tmp_path: Path) -> Any:
    """Factory building apps with fakes and rate limiting opt-out by default."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        kwargs.setdefault("rate_limit", None)
        application = create_app(**kwargs)
        application.dependency_overrides[get_manager] = lambda: FakeManager()
        return application

    return _make


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Serve ``app`` in-process without a socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _auth(token: str = TOKEN, **extra: str) -> dict[str, str]:
    """Build request headers with a bearer token plus extras."""
    return {"Authorization": f"Bearer {token}", **extra}


async def _etag(client: httpx.AsyncClient, **headers: str) -> str:
    """Fetch the current validator for ``SITES`` and return it."""
    response = await client.get(SITES, headers=headers)
    assert response.status_code == 200, response.text
    etag = response.headers["etag"]
    assert etag
    return etag


# ---------------------------------------------------------------------------
# Structural contract: the documented order
# ---------------------------------------------------------------------------


def test_middleware_order_matches_the_documented_matrix(make_app: Any) -> None:
    """
    Pin the order the interaction tests below rely on.

    ``user_middleware[0]`` is the outermost layer (Starlette builds the stack
    in reverse). Everything that rejects a request must come *after*
    ``ConditionalGetMiddleware`` in this list, i.e. lie inside it.
    """
    full = make_app(token=TOKEN, cors=True, rate_limit=120)
    assert [mw.cls for mw in full.user_middleware] == [
        RequestIdMiddleware,
        RequestLoggingMiddleware,
        SecurityHeadersMiddleware,
        CORSMiddleware,
        CacheControlMiddleware,
        ConditionalGetMiddleware,
        RateLimitMiddleware,
        DownloadsAuthMiddleware,
        BodySizeLimitMiddleware,
    ]

    default = make_app(token=TOKEN, rate_limit=120)
    assert [mw.cls for mw in default.user_middleware] == [
        RequestIdMiddleware,
        RequestLoggingMiddleware,
        SecurityHeadersMiddleware,
        CacheControlMiddleware,
        ConditionalGetMiddleware,
        RateLimitMiddleware,
        DownloadsAuthMiddleware,
        BodySizeLimitMiddleware,
        OriginCheckMiddleware,
    ]


# ---------------------------------------------------------------------------
# Token auth beats a matching validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"X-API-Key": "wrong-token"},
    ],
    ids=["missing", "wrong-bearer", "wrong-api-key"],
)
async def test_401_beats_matching_etag(make_app: Any, headers: dict[str, str]) -> None:
    """A token-less caller gets 401, never the 304 its validator would earn."""
    async with _client(make_app(token=TOKEN)) as client:
        etag = await _etag(client, **_auth())
        response = await client.get(SITES, headers={**headers, "If-None-Match": etag})

    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers.get(REQUEST_ID_HEADER, "").strip()
    # No representation metadata may leak on the rejection.
    assert "etag" not in response.headers
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_correct_token_with_matching_etag_is_304(make_app: Any) -> None:
    """Only an authenticated request reaches the conditional layer."""
    async with _client(make_app(token=TOKEN)) as client:
        etag = await _etag(client, **_auth())
        response = await client.get(SITES, headers=_auth(**{"If-None-Match": etag}))

    assert response.status_code == 304, response.text
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == CACHE_CONTROL_PRIVATE


@pytest.mark.asyncio
async def test_wrong_token_does_not_leak_resource_existence(make_app: Any) -> None:
    """
    ``304`` vs ``401`` must not become an existence oracle.

    A matching and a non-matching validator produce the identical rejection:
    same status, same body, no ``ETag``. The only per-request difference is
    the correlation id.
    """
    async with _client(make_app(token=TOKEN)) as client:
        etag = await _etag(client, **_auth())
        matching = await client.get(
            SITES,
            headers={"Authorization": "Bearer wrong-token", "If-None-Match": etag},
        )
        non_matching = await client.get(
            SITES,
            headers={
                "Authorization": "Bearer wrong-token",
                "If-None-Match": '"0000000000000000000000000000dead"',
            },
        )

    assert matching.status_code == non_matching.status_code == 401
    assert matching.json() == non_matching.json()
    for response in (matching, non_matching):
        assert "etag" not in response.headers
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_cors_mode_401_stays_browser_readable(make_app: Any) -> None:
    """With ``--cors`` the 401 keeps the origin grant so JS can read it."""
    async with _client(make_app(token=TOKEN, cors=True)) as client:
        etag = await _etag(client, **_auth())
        response = await client.get(
            SITES,
            headers={
                "Authorization": "Bearer wrong-token",
                "If-None-Match": etag,
                "Origin": FOREIGN_ORIGIN,
            },
        )

    assert response.status_code == 401, response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert "etag" not in response.headers
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers.get(REQUEST_ID_HEADER, "").strip()


# ---------------------------------------------------------------------------
# CORS: preflight and the real 304
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_admits_if_none_match(make_app: Any) -> None:
    """``If-None-Match`` is in the allow-list and is echoed back on a preflight."""
    assert "If-None-Match" in CORS_ALLOW_HEADERS

    async with _client(make_app(cors=True)) as client:
        response = await client.options(
            SITES,
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "if-none-match,authorization",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    allowed = {
        name.strip().lower()
        for name in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"if-none-match", "authorization"} <= allowed
    assert "get" in response.headers["access-control-allow-methods"].lower()


@pytest.mark.asyncio
async def test_preflight_still_rejects_an_unlisted_header(make_app: Any) -> None:
    """The allow-list is enforced, not merely advertised."""
    async with _client(make_app(cors=True)) as client:
        response = await client.options(
            SITES,
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "if-match",
            },
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_real_304_exposes_etag_to_browser_clients(make_app: Any) -> None:
    """A browser must be able to read the refreshed validator off the 304."""
    assert "ETag" in CORS_EXPOSE_HEADERS

    async with _client(make_app(cors=True)) as client:
        etag = await _etag(client, Origin=FOREIGN_ORIGIN)
        response = await client.get(
            SITES,
            headers={"If-None-Match": etag, "Origin": FOREIGN_ORIGIN},
        )

    assert response.status_code == 304, response.text
    assert response.headers["etag"] == etag
    exposed = {
        name.strip().lower()
        for name in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"etag", "retry-after", "x-request-id"} <= exposed
    assert response.headers["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------------------
# Default mode: OriginCheck 403 beats a matching validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [FOREIGN_ORIGIN, "null", "http://127.0.0.1.evil"])
async def test_origin_gate_403_beats_conditional_get(
    make_app: Any, origin: str
) -> None:
    """Without ``--cors`` the browser-origin gate wins over the 304 path."""
    async with _client(make_app()) as client:
        etag = await _etag(client)
        response = await client.get(
            SITES, headers={"If-None-Match": etag, "Origin": origin}
        )

    assert response.status_code == 403, response.text
    assert "detail" in response.json()
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert "etag" not in response.headers
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_loopback_origin_still_reaches_the_304(make_app: Any) -> None:
    """The gate only rejects foreign origins; a local page keeps revalidating."""
    async with _client(make_app()) as client:
        etag = await _etag(client, Origin=LOOPBACK_ORIGIN)
        response = await client.get(
            SITES, headers={"If-None-Match": etag, "Origin": LOOPBACK_ORIGIN}
        )

    assert response.status_code == 304, response.text
    assert response.headers["etag"] == etag


# ---------------------------------------------------------------------------
# Rate limit 429 beats a matching validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_429_beats_conditional_get(make_app: Any) -> None:
    """Once the IP is over budget, a matching validator still gets 429."""
    async with _client(make_app(rate_limit=1, rate_limit_window=60.0)) as client:
        etag = await _etag(client)
        response = await client.get(SITES, headers={"If-None-Match": etag})

    assert response.status_code == 429, response.text
    assert response.headers["retry-after"]
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert "etag" not in response.headers


# ---------------------------------------------------------------------------
# Method restriction: only GET is conditional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_if_none_match_applies_to_get_but_not_post_or_delete(
    make_app: Any,
) -> None:
    """``*`` matches any representation, so a false 304 would be obvious."""
    async with _client(make_app()) as client:
        get_response = await client.get(SITES, headers={"If-None-Match": "*"})
        post_response = await client.post(
            "/api/v1/tasks/t1/cancel", headers={"If-None-Match": "*"}
        )
        delete_response = await client.delete(
            "/api/v1/watchlist/qidian/1", headers={"If-None-Match": "*"}
        )

    assert get_response.status_code == 304, get_response.text

    assert post_response.status_code == 200, post_response.text
    assert "etag" not in post_response.headers
    assert delete_response.status_code != 304, delete_response.text
    assert delete_response.status_code in (200, 404, 422), delete_response.text
    assert "etag" not in delete_response.headers


# ---------------------------------------------------------------------------
# Embedded engine: token gate 401 beats a matching validator
# ---------------------------------------------------------------------------


@pytest.fixture
def embedded_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Start the stdlib engine on a loopback port with a known token."""
    home = tmp_path / "embedded_home"
    (home / "downloads").mkdir(parents=True)
    monkeypatch.setenv("NOVEL_HOME", str(home))

    previous_cwd = os.getcwd()
    port = eserver.start_server(str(home), 0, token=TOKEN)
    try:
        yield SimpleNamespace(port=port, home=home)
    finally:
        eserver.stop_server()
        os.chdir(previous_cwd)


def _embedded_request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request against the real embedded listener; headers are lowered."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            body,
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"X-API-Key": "wrong-token"},
    ],
    ids=["missing", "wrong-bearer", "wrong-api-key"],
)
def test_embedded_401_beats_matching_etag(
    embedded_with_token: SimpleNamespace, headers: dict[str, str]
) -> None:
    """The embedded token gate runs before routing, so no 304 can leak."""
    port = embedded_with_token.port
    _status, first_headers, _body = _embedded_request(
        port, "GET", SITES, {"Authorization": f"Bearer {TOKEN}"}
    )
    etag = first_headers["etag"]
    assert etag

    status, response_headers, body = _embedded_request(
        port, "GET", SITES, {**headers, "If-None-Match": etag}
    )

    assert status == 401, body
    assert response_headers["www-authenticate"] == "Bearer"
    assert response_headers.get("x-request-id", "").strip()
    assert "etag" not in response_headers
    assert b'"detail"' in body


def test_embedded_correct_token_matching_etag_is_304(
    embedded_with_token: SimpleNamespace,
) -> None:
    port = embedded_with_token.port
    _status, first_headers, _body = _embedded_request(
        port, "GET", SITES, {"Authorization": f"Bearer {TOKEN}"}
    )
    etag = first_headers["etag"]

    status, headers, body = _embedded_request(
        port,
        "GET",
        SITES,
        {"Authorization": f"Bearer {TOKEN}", "If-None-Match": etag},
    )

    assert status == 304, body
    assert body == b""
    assert headers["etag"] == etag
    assert headers["cache-control"] == CACHE_CONTROL_PRIVATE
