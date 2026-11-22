#!/usr/bin/env python3
"""Timeout / cancellation / redirect-limit tests for rule-driven fetching.

All traffic goes to a loopback mock server on an ephemeral port; no test in
this module touches the network.  The module pins four safety properties of
the rule fetch path:

* a slow page is bounded by the configured per-request timeout and surfaces as
  a readable :class:`~novel_downloader.plugins.rule.model.RuleError`;
* ``timeout`` is per page, not a deadline for the whole pagination walk, and
  the walk still stops at ``pages.max_pages`` / ``MAX_EXTRA_PAGES``;
* a redirect loop terminates at the session redirect ceiling instead of
  following the chain forever;
* ``asyncio.CancelledError`` propagates (it is not swallowed by the pagination
  guard) and releases the in-flight connection, and cancelling one concurrent
  request does not disturb another one on the same session.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from novel_downloader.infra.sessions import available_backends
from novel_downloader.infra.sessions.base import DEFAULT_MAX_REDIRECTS
from novel_downloader.plugins.base.errors import (
    RedirectLimitError,
    TransportError,
    is_transient_error,
)
from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import (
    MAX_EXTRA_PAGES,
    Rule,
    RuleError,
    parse_rule,
)
from novel_downloader.schemas import FetcherConfig


# --------------------------------------------------------------------------- #
# loopback mock server
# --------------------------------------------------------------------------- #
@dataclass
class _Route:
    status: int = 200
    body: bytes = b""
    location: str | None = None
    delay: float = 0.0


@dataclass
class _Site:
    """Routes + hit counters shared with the mock HTTP handler."""

    server: ThreadingHTTPServer
    routes: dict[str, _Route] = field(default_factory=dict)
    hits: dict[str, int] = field(default_factory=dict)
    disconnects: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def add(
        self,
        path: str,
        body: str = "",
        *,
        status: int = 200,
        location: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self.routes[path] = _Route(
            status=status,
            body=body.encode("utf-8"),
            location=location,
            delay=delay,
        )

    def record(self, path: str) -> None:
        with self._lock:
            self.hits[path] = self.hits.get(path, 0) + 1

    def hit_count(self, path: str) -> int:
        with self._lock:
            return self.hits.get(path, 0)

    def mark_disconnected(self, path: str) -> None:
        with self._lock:
            self.disconnects.add(path)

    def resolve(self, path: str) -> _Route:
        route = self.routes.get(path)
        if route is not None:
            return route
        match = re.fullmatch(r"/book/([^/]+)/(?:index_(\d+)\.html)?", path)
        if match is not None:
            page = int(match.group(2) or 1)
            body = f'<div>page {page}</div><a href="index_{page + 1}.html">next</a>'
            return _Route(body=body.encode("utf-8"))
        return _Route(status=404, body=b"missing")


class _Handler(BaseHTTPRequestHandler):
    """Serve the route table attached to the server; never logs."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
        site = cast(_Server, self.server).site
        path = urlsplit(self.path).path
        site.record(path)
        route = site.resolve(path)
        if route.delay > 0:
            time.sleep(route.delay)
        try:
            self.send_response(route.status)
            if route.location is not None:
                self.send_header("Location", route.location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            if route.body:
                self.wfile.write(route.body)
                self.wfile.flush()
        except OSError:
            # The client cancelled while the handler was sleeping / writing.
            site.mark_disconnected(path)

    def log_message(self, *args: object) -> None:
        """Keep the test output free of per-request access logs."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    site: _Site


@pytest.fixture()
def site() -> Iterator[_Site]:
    server = _Server(("127.0.0.1", 0), _Handler)
    mock = _Site(server=server)
    server.site = mock
    mock.add("/ok", "<html>ok</html>")
    mock.add("/fast", "<html>fast</html>")
    mock.add("/chapter/fast", "<article>fast body</article>")
    mock.add("/chapter/slow", "<article>slow body</article>", delay=0.6)
    mock.add("/slow", "<html>slow</html>", delay=0.6)
    # A self-redirect never terminates on its own; the session must stop it.
    mock.add("/loop", status=302, location="/loop")
    # A long but otherwise valid chain: no 200 is reachable within the limit.
    for hop in range(DEFAULT_MAX_REDIRECTS + 10):
        mock.add(f"/redirect/{hop}", status=302, location=f"/redirect/{hop + 1}")

    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    try:
        yield mock
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_fetcher(
    rule: Rule,
    *,
    backend: str = "aiohttp",
    timeout: float = 5.0,
    retry_times: int = 0,
    backoff_factor: float = 0.0,
) -> RuleFetcher:
    cls = type(
        f"RuleFetcher_{rule.site_key}_{backend}",
        (RuleFetcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "DEFAULT_BASE_URL": rule.base_url,
        },
    )
    return cast(
        RuleFetcher,
        cls(
            FetcherConfig(
                backend=backend,
                timeout=timeout,
                retry_times=retry_times,
                backoff_factor=backoff_factor,
                request_interval=0,
                max_rps=0,
            )
        ),
    )


def _chapter_rule(site: _Site, chapter_url: str) -> Rule:
    return parse_rule(
        {
            "site_key": "timeouts",
            "base_url": site.base,
            "chapter": {
                "url": chapter_url,
                "content": {"selector": "article"},
            },
        }
    )


def _paged_book_rule(site: _Site, *, max_pages: int | None) -> Rule:
    pages: dict[str, Any] = {
        "url": f"{site.base}/book/{{book_id}}/index_{{page}}.html",
        "start": 2,
    }
    if max_pages is not None:
        pages["max_pages"] = max_pages
    return parse_rule(
        {
            "site_key": "timeouts",
            "base_url": site.base,
            "book": {
                "url": f"{site.base}/book/{{book_id}}/",
                "pages": pages,
                "chapters": {"item": "a"},
            },
        }
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll a thread-safe predicate without blocking the event loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- #
# 1. a slow page times out and is reported as a readable RuleError
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_page_timeout_is_a_readable_rule_error(site: _Site, backend: str) -> None:
    rule = _chapter_rule(site, f"{site.base}/slow")
    fetcher = _make_fetcher(rule, backend=backend, timeout=0.2)
    await fetcher.init()
    try:
        with pytest.raises(RuleError) as caught:
            await fetcher.fetch_chapter_content("7", "5")
    finally:
        await fetcher.close()

    message = str(caught.value).lower()
    assert "/slow" in message
    assert "timed out" in message or "timeout" in message
    # The request reached the server, so this is a real timeout, not a
    # connection refusal that happened to mention the URL.
    assert site.hit_count("/slow") >= 1


# --------------------------------------------------------------------------- #
# 2. timeout is per page, and max_pages still bounds the walk
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_extra_page_timeout_is_not_silently_truncated(
    site: _Site, backend: str
) -> None:
    """A timed-out extra page must fail, not return a shorter book/chapter.

    The pagination guard is deliberately tolerant of a missing guessed page
    (``max_pages`` on a JSON endpoint, a 404), but a request that spent the
    whole timeout/retry budget is a different failure: silently dropping the
    page turns one interrupted request into permanently truncated content.
    """
    site.add(
        "/book/7/index_2.html",
        '<div>page 2</div><a href="index_3.html">next</a>',
        delay=0.6,
    )
    fetcher = _make_fetcher(
        _paged_book_rule(site, max_pages=2), backend=backend, timeout=0.2
    )
    await fetcher.init()
    try:
        with pytest.raises(RuleError):
            await fetcher.fetch_book_info("7")
    finally:
        await fetcher.close()

    assert site.hit_count("/book/7/index_2.html") >= 1


@pytest.mark.asyncio
async def test_extra_page_redirect_loop_is_not_silently_truncated(
    site: _Site,
) -> None:
    """The same rule applies to a deterministic redirect failure on page N."""
    site.add("/book/7/index_2.html", status=302, location="/loop")
    fetcher = _make_fetcher(_paged_book_rule(site, max_pages=2), timeout=2.0)
    await fetcher.init()
    try:
        with pytest.raises(RedirectLimitError):
            await fetcher.fetch_book_info("7")
    finally:
        await fetcher.close()

    assert site.hit_count("/loop") >= 2


@pytest.mark.asyncio
async def test_timeout_is_per_page_and_max_pages_stops_the_walk(site: _Site) -> None:
    # Each page is comfortably below the timeout, but the complete walk takes
    # longer than it: a single global deadline would fail this request.
    for page in range(2, 5):
        site.add(
            f"/book/7/index_{page}.html",
            f'<div>page {page}</div><a href="index_{page + 1}.html">next</a>',
            delay=0.12,
        )

    fetcher = _make_fetcher(_paged_book_rule(site, max_pages=4), timeout=0.3)
    await fetcher.init()
    started = time.monotonic()
    try:
        pages = await fetcher.fetch_book_info("7")
    finally:
        await fetcher.close()
    elapsed = time.monotonic() - started

    assert len(pages) == 4
    assert "page 4" in pages[-1]
    assert elapsed > 0.3, "the walk should exceed one per-request timeout"
    assert site.hit_count("/book/7/index_5.html") == 0


@pytest.mark.asyncio
async def test_global_extra_page_cap_stops_an_endless_walk(site: _Site) -> None:
    """Without ``max_pages`` the absolute cap still bounds an endless site."""
    fetcher = _make_fetcher(_paged_book_rule(site, max_pages=None))
    await fetcher.init()
    try:
        pages = await fetcher.fetch_book_info("7")
    finally:
        await fetcher.close()

    assert len(pages) == MAX_EXTRA_PAGES + 1
    assert site.hit_count(f"/book/7/index_{MAX_EXTRA_PAGES + 1}.html") == 1
    assert site.hit_count(f"/book/7/index_{MAX_EXTRA_PAGES + 2}.html") == 0


# --------------------------------------------------------------------------- #
# 3. redirect loops and over-long chains terminate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_redirect_loop_terminates_with_a_readable_error(
    site: _Site, backend: str
) -> None:
    rule = _chapter_rule(site, f"{site.base}/loop")
    fetcher = _make_fetcher(rule, backend=backend, timeout=2.0)
    await fetcher.init()
    try:
        with pytest.raises(RedirectLimitError) as caught:
            await fetcher.fetch_chapter_content("7", "5")
    finally:
        await fetcher.close()

    hits = site.hit_count("/loop")
    assert 2 <= hits <= DEFAULT_MAX_REDIRECTS + 2
    message = str(caught.value).lower()
    assert "redirect" in message
    assert "/loop" in message


@pytest.mark.asyncio
async def test_redirect_chain_beyond_the_ceiling_is_stopped(site: _Site) -> None:
    rule = _chapter_rule(site, f"{site.base}/redirect/0")
    fetcher = _make_fetcher(rule, timeout=2.0)
    await fetcher.init()
    try:
        with pytest.raises(RedirectLimitError):
            await fetcher.fetch_chapter_content("7", "5")
    finally:
        await fetcher.close()

    # It must stop around the ceiling instead of walking the whole chain.
    followed = sum(
        site.hit_count(f"/redirect/{hop}") for hop in range(DEFAULT_MAX_REDIRECTS + 10)
    )
    assert DEFAULT_MAX_REDIRECTS <= followed <= DEFAULT_MAX_REDIRECTS + 2


# --------------------------------------------------------------------------- #
# 4. cancellation propagates and releases the in-flight connection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_cancellation_propagates_and_closes_the_connection(
    site: _Site, backend: str
) -> None:
    rule = _chapter_rule(site, f"{site.base}/chapter/{{chapter_id}}")
    fetcher = _make_fetcher(rule, backend=backend, timeout=5.0)
    await fetcher.init()
    try:
        task = asyncio.create_task(fetcher.fetch_chapter_content("7", "slow"))
        assert await _wait_until(lambda: site.hit_count("/chapter/slow") >= 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The handler is still sleeping; its next write must hit a closed
        # socket.  That is the observable proof that cancellation released
        # the response/connection instead of leaking it.
        assert await _wait_until(lambda: "/chapter/slow" in site.disconnects)

        # A cancelled request must not poison the shared session.
        body = await fetcher.fetch(f"{site.base}/ok")
        assert "ok" in body
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_cancellation_during_an_extra_page_is_not_swallowed(
    site: _Site, backend: str
) -> None:
    """The pagination guard catches ``Exception``; cancellation must escape it."""
    site.add(
        "/book/7/index_2.html",
        '<div>page 2</div><a href="index_3.html">next</a>',
        delay=0.6,
    )
    fetcher = _make_fetcher(
        _paged_book_rule(site, max_pages=2), backend=backend, timeout=5.0
    )
    await fetcher.init()
    try:
        task = asyncio.create_task(fetcher.fetch_book_info("7"))
        assert await _wait_until(lambda: site.hit_count("/book/7/index_2.html") >= 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_until(lambda: "/book/7/index_2.html" in site.disconnects)

        # The same session is still usable after the cancellation.
        assert "ok" in await fetcher.fetch(f"{site.base}/ok")
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_cancelling_one_concurrent_fetch_does_not_affect_another(
    site: _Site, backend: str
) -> None:
    rule = _chapter_rule(site, f"{site.base}/chapter/{{chapter_id}}")
    fetcher = _make_fetcher(rule, backend=backend, timeout=5.0)
    await fetcher.init()
    try:
        slow = asyncio.create_task(fetcher.fetch_chapter_content("7", "slow"))
        assert await _wait_until(lambda: site.hit_count("/chapter/slow") >= 1)
        fast = asyncio.create_task(fetcher.fetch_chapter_content("7", "fast"))

        assert "fast body" in (await fast)[0]

        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow

        # The healthy request and the session itself survive the cancellation.
        again = await fetcher.fetch_chapter_content("7", "fast")
        assert "fast body" in again[0]
    finally:
        await fetcher.close()


# --------------------------------------------------------------------------- #
# 5. a timeout spends a bounded retry budget
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", available_backends())
async def test_timeout_retries_are_bounded(site: _Site, backend: str) -> None:
    rule = _chapter_rule(site, f"{site.base}/slow")
    fetcher = _make_fetcher(rule, backend=backend, timeout=0.2, retry_times=2)
    await fetcher.init()
    started = time.monotonic()
    try:
        with pytest.raises(RuleError) as caught:
            await fetcher.fetch_chapter_content("7", "5")
    finally:
        await fetcher.close()
    elapsed = time.monotonic() - started

    # The timeout is readable at the rule layer, but it is still the typed
    # transport failure so a higher-level loop does not retry the same spent
    # budget.
    assert isinstance(caught.value, TransportError)
    assert is_transient_error(caught.value) is False

    # retry_times=2 => at most one initial attempt plus two retries.  A retry
    # can time out before its request reaches the mock server, so only the
    # upper bound is exact; what matters is that the loop terminates.
    assert 1 <= site.hit_count("/slow") <= 3
    assert elapsed < 5.0, "a timed-out request must not loop forever"
