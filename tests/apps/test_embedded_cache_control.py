#!/usr/bin/env python3
"""
tests.apps.test_embedded_cache_control
--------------------------------------

Parity tests for the ``Cache-Control`` policy shared by the REST API and the
stdlib-only embedded engine (``apps/common/cache_policy.py``).

The contract pinned here (for the same path and status code, both transports
must answer with the same directive):

============================================  ==========================
Path                                          ``Cache-Control``
============================================  ==========================
``/api/v1/sites``, ``/api/v1/history``,       ``private, max-age=60``
``/api/v1/music/platforms`` (success)
``/api/v1/tasks*``, ``/api/v1/login*``,       ``no-store``
``/api/v1/music/jobs*`` and every other
``/api/v1`` success
any ``4xx`` / ``5xx`` (API or ``/downloads``) ``no-store``
outside ``/api/v1`` and ``/downloads``        no header
``304 Not Modified``                          the ``200`` directive
``HEAD`` (embedded serves it; REST routes do   same head as the ``GET``
not register ``HEAD`` and answer ``405``)     (``405 no-store`` on REST)
============================================  ==========================

One documented difference remains: a *successful* ``/downloads`` file on the
embedded engine keeps its historical headers (no ``Cache-Control``) while REST
advertises ``public, max-age=3600``. The embedded engine serves an app-private
export directory over a loopback, token-guarded socket; the test records the
difference instead of silently accepting either behaviour.

Everything is offline: REST calls go through ``httpx.ASGITransport``, the
embedded checks use a real loopback listener.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import middleware as api_middleware
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.common.cache_policy import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    CACHE_CONTROL_PUBLIC,
    CACHE_CONTROL_RULES,
    CACHE_POLICY_PREFIXES,
    cache_control_for,
)
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.embedded import server as embedded_server

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeManager:
    """In-memory task manager so the task routes never queue real work."""

    def __init__(self, tasks: list[DownloadTask]) -> None:
        self.tasks = tasks

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": list(self.tasks), "running": [], "completed": []}

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        task = DownloadTask(title=title, site=site, book_id=book_id)
        self.tasks.append(task)
        return task

    async def close(self) -> None:
        pass


def _rest_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    raise_app_exceptions: bool = True,
) -> tuple[int, dict[str, str]]:
    """One in-process REST request; headers are lower-cased."""

    async def go() -> tuple[int, dict[str, str]]:
        transport = httpx.ASGITransport(
            app=app, raise_app_exceptions=raise_app_exceptions
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.request(
                method, path, json=json_body, headers=headers
            )
            return (
                response.status_code,
                {name.lower(): value for name, value in response.headers.items()},
            )

    return asyncio.run(go())


def _embedded_request(
    port: int,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """One request against the real embedded listener."""
    payload = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        return (
            response.status,
            raw,
            {name.lower(): value for name, value in response.getheaders()},
        )
    finally:
        connection.close()


@pytest.fixture
def transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """A REST app and a running embedded engine backed by the same state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)

    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "book.txt").write_text("exported-body", encoding="utf-8")
    monkeypatch.setattr(history_files, "_output_dir", lambda: exports)

    task = DownloadTask(title="Sample", site="qidian", book_id="42")
    manager = FakeManager([task])
    snapshot = {"pending": [task], "running": [], "completed": []}
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot", lambda: snapshot
    )

    app = create_app(output_dir=exports, rate_limit=None)
    app.dependency_overrides[get_manager] = lambda: manager

    original_cwd = os.getcwd()
    port = embedded_server.start_server(str(home), 0)
    try:
        yield SimpleNamespace(
            port=port,
            app=app,
            manager=manager,
            exports=exports,
            task=task,
        )
    finally:
        embedded_server.stop_server()
        os.chdir(original_cwd)


def _both(
    world: SimpleNamespace,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    rest_raise: bool = True,
) -> tuple[tuple[int, dict[str, str]], tuple[int, dict[str, str]]]:
    """Issue the same request through both transports.

    :return: ``((rest_status, rest_headers), (embedded_status, embedded_headers))``
    """
    rest = _rest_request(
        world.app,
        method,
        path,
        json_body=json_body,
        headers=headers,
        raise_app_exceptions=rest_raise,
    )
    status, _raw, embedded_headers = _embedded_request(
        world.port, method, path, json_body=json_body, headers=headers
    )
    return rest, (status, embedded_headers)


# ---------------------------------------------------------------------------
# The shared table itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "status", "expected"),
    [
        ("/api/v1/sites", 200, CACHE_CONTROL_PRIVATE),
        ("/api/v1/sites/", 200, CACHE_CONTROL_PRIVATE),
        ("/api/v1/history", 200, CACHE_CONTROL_PRIVATE),
        ("/api/v1/history?limit=5", 200, CACHE_CONTROL_PRIVATE),
        ("/api/v1/music/platforms", 200, CACHE_CONTROL_PRIVATE),
        ("/api/v1/tasks", 200, CACHE_CONTROL_NO_STORE),
        ("/api/v1/tasks/t-1", 200, CACHE_CONTROL_NO_STORE),
        # Live/credential state and anything else in the API tree falls back
        # to ``no-store``: a new route must not become cacheable by accident.
        ("/api/v1/login/requests", 200, CACHE_CONTROL_NO_STORE),
        ("/api/v1/music/jobs/j-1", 200, CACHE_CONTROL_NO_STORE),
        ("/api/v1/health", 200, CACHE_CONTROL_NO_STORE),
        ("/api/v1/sites-extra", 200, CACHE_CONTROL_NO_STORE),
        ("/api/v1/tasks-extra", 200, CACHE_CONTROL_NO_STORE),
        # Errors are never stored, whatever the path or status.
        ("/api/v1/sites", 404, CACHE_CONTROL_NO_STORE),
        ("/api/v1/tasks", 422, CACHE_CONTROL_NO_STORE),
        ("/api/v1/history", 500, CACHE_CONTROL_NO_STORE),
        ("/downloads/missing.txt", 404, CACHE_CONTROL_NO_STORE),
        # Successful downloads are public on REST (the embedded engine keeps
        # its historical headers; see the dedicated test below).
        ("/downloads/book.txt", 200, CACHE_CONTROL_PUBLIC),
        ("/downloads", 200, CACHE_CONTROL_PUBLIC),
        # Outside the governed prefixes: no directive at all.
        ("/", 200, None),
        ("/docs", 200, None),
        ("/openapi.json", 200, None),
        ("/downloads2/book.txt", 200, None),
    ],
)
def test_shared_table_resolves_each_case(
    path: str, status: int, expected: str | None
) -> None:
    """The resolver is the single source of truth for both transports."""
    query_free = path.split("?", 1)[0]
    assert cache_control_for(query_free, status) == expected


def test_rest_middleware_reexports_the_shared_table() -> None:
    """The REST module must not grow its own copy of the policy."""
    assert api_middleware.CACHE_CONTROL_RULES is CACHE_CONTROL_RULES
    assert api_middleware.CACHE_POLICY_PREFIXES is CACHE_POLICY_PREFIXES


# ---------------------------------------------------------------------------
# Per-path parity between the two transports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "json_body", "expected_status", "expected_cache"),
    [
        ("GET", "/api/v1/sites", None, 200, CACHE_CONTROL_PRIVATE),
        ("GET", "/api/v1/history", None, 200, CACHE_CONTROL_PRIVATE),
        ("GET", "/api/v1/health", None, 200, CACHE_CONTROL_NO_STORE),
        ("GET", "/api/v1/tasks", None, 200, CACHE_CONTROL_NO_STORE),
        ("GET", "/api/v1/login/requests", None, 200, CACHE_CONTROL_NO_STORE),
        ("GET", "/api/v1/tasks/unknown", None, 404, CACHE_CONTROL_NO_STORE),
        ("GET", "/api/v1/nope", None, 404, CACHE_CONTROL_NO_STORE),
        ("POST", "/api/v1/sites", None, 405, CACHE_CONTROL_NO_STORE),
        ("POST", "/api/v1/tasks", {}, 422, CACHE_CONTROL_NO_STORE),
        ("GET", "/downloads/missing.txt", None, 404, CACHE_CONTROL_NO_STORE),
    ],
)
def test_same_path_same_status_same_policy(
    transports: SimpleNamespace,
    method: str,
    path: str,
    json_body: Any,
    expected_status: int,
    expected_cache: str,
) -> None:
    """Same path + same status must yield the same directive on both."""
    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, method, path, json_body=json_body
    )

    assert rest_status == emb_status == expected_status, path
    assert rest_headers.get("cache-control") == expected_cache, path
    assert emb_headers.get("cache-control") == expected_cache, path


def test_music_platforms_parity_with_optional_extras(
    transports: SimpleNamespace,
) -> None:
    """200 → private on both; a missing music extra is a 503 ``no-store``."""
    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, "GET", "/api/v1/music/platforms"
    )

    assert rest_status == emb_status
    assert rest_headers.get("cache-control") == emb_headers.get("cache-control")
    expected = CACHE_CONTROL_PRIVATE if rest_status == 200 else CACHE_CONTROL_NO_STORE
    assert rest_headers.get("cache-control") == expected


def test_paths_outside_the_policy_get_no_directive(
    transports: SimpleNamespace,
) -> None:
    """``/`` is outside ``/api/v1`` and ``/downloads`` on both transports."""
    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, "GET", "/"
    )

    assert rest_status == emb_status == 404
    assert "cache-control" not in rest_headers
    assert "cache-control" not in emb_headers


# ---------------------------------------------------------------------------
# Errors, HEAD and conditional requests
# ---------------------------------------------------------------------------


def test_unauthorized_is_no_store_on_both(
    transports: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from the token gate must never be cached."""
    token_app = create_app(
        output_dir=transports.exports, rate_limit=None, token="s3cret"
    )
    token_app.dependency_overrides[get_manager] = lambda: transports.manager
    monkeypatch.setattr(embedded_server, "_auth_token", "s3cret")

    rest_state, emb_state = _both(
        SimpleNamespace(port=transports.port, app=token_app), "GET", "/api/v1/tasks"
    )

    assert rest_state[0] == emb_state[0] == 401
    assert rest_state[1].get("cache-control") == CACHE_CONTROL_NO_STORE
    assert emb_state[1].get("cache-control") == CACHE_CONTROL_NO_STORE


def test_server_error_is_no_store_on_both(
    transports: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opaque 500 path is stamped ``no-store`` by both transports."""

    def boom() -> dict[str, list[DownloadTask]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot", boom
    )
    monkeypatch.setattr(transports.manager, "snapshot", boom)

    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, "GET", "/api/v1/tasks", rest_raise=False
    )

    assert rest_status == emb_status == 500
    assert rest_headers.get("cache-control") == CACHE_CONTROL_NO_STORE
    assert emb_headers.get("cache-control") == CACHE_CONTROL_NO_STORE


def test_head_carries_the_same_directive_as_get(transports: SimpleNamespace) -> None:
    """The engine answers ``HEAD`` like ``GET``: same head, no body.

    REST's ``/api/v1`` routes do not register ``HEAD`` (only ``GET``), so the
    REST transport answers ``405`` — and that error is still ``no-store``.
    The embedded engine deliberately serves ``HEAD`` (probes work on Android),
    so the check is against its own ``GET`` head plus REST's error class.
    """
    for path, expected in (
        ("/api/v1/sites", CACHE_CONTROL_PRIVATE),
        ("/api/v1/tasks", CACHE_CONTROL_NO_STORE),
    ):
        (get_status, get_headers), _ = _both(transports, "GET", path)
        assert get_status == 200, path

        head_status, head_raw, head_headers = _embedded_request(
            transports.port, "HEAD", path
        )
        assert head_status == get_status, path
        assert head_raw == b"", path
        assert head_headers.get("cache-control") == expected, path
        assert head_headers.get("cache-control") == get_headers.get("cache-control")

        rest_head_status, rest_head_headers = _rest_request(
            transports.app, "HEAD", path
        )
        assert rest_head_status == 405, path
        assert rest_head_headers.get("cache-control") == CACHE_CONTROL_NO_STORE


def test_not_modified_keeps_the_cache_headers(transports: SimpleNamespace) -> None:
    """A 304 keeps ``Cache-Control``, ``ETag`` and ``Vary`` on both."""
    path = "/api/v1/sites"

    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, "GET", path
    )
    assert rest_status == emb_status == 200
    for headers in (rest_headers, emb_headers):
        assert headers.get("cache-control") == CACHE_CONTROL_PRIVATE
        assert headers.get("etag")
        assert "accept-encoding" in headers.get("vary", "").lower()

    rest_etag = rest_headers["etag"]
    emb_etag = emb_headers["etag"]

    rest_304_status, rest_304 = _rest_request(
        transports.app, "GET", path, headers={"If-None-Match": rest_etag}
    )
    emb_304_status, raw, emb_304 = _embedded_request(
        transports.port, "GET", path, headers={"If-None-Match": emb_etag}
    )

    assert rest_304_status == emb_304_status == 304
    assert raw == b""
    for headers in (rest_304, emb_304):
        assert headers.get("cache-control") == CACHE_CONTROL_PRIVATE
        assert headers.get("etag")
        assert "accept-encoding" in headers.get("vary", "").lower()


# ---------------------------------------------------------------------------
# The one documented difference: a successful download
# ---------------------------------------------------------------------------


def test_successful_download_keeps_the_embedded_header_set(
    transports: SimpleNamespace,
) -> None:
    """REST serves exports ``public``; the embedded engine sends no directive.

    Recorded deliberately: the embedded engine serves an app-private export
    directory to native clients, and its historical response has no
    ``Cache-Control``. ``/downloads`` *errors* still match REST (``no-store``).
    """
    (rest_status, rest_headers), (emb_status, emb_headers) = _both(
        transports, "GET", "/downloads/book.txt"
    )

    assert rest_status == emb_status == 200
    assert rest_headers.get("cache-control") == CACHE_CONTROL_PUBLIC
    assert "cache-control" not in emb_headers
