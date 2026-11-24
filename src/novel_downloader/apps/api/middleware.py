#!/usr/bin/env python3
"""
novel_downloader.apps.api.middleware
------------------------------------

Request-hardening helpers shared by the REST API application.

* :class:`OriginCheckMiddleware` answers requests whose ``Origin`` header is
  not a loopback origin with ``403``. By default the API binds to
  ``127.0.0.1`` without a token: that stops *remote* clients, but not
  *browser* ones — a website the user visits can fire cross-origin requests
  at the local port (browsers always attach ``Origin`` to cross-origin
  POSTs). Non-browser clients (CLI, SDK, curl) send no ``Origin`` and pass
  unchanged; ``--cors`` removes the gate and lets CORS govern browsers.
* :class:`BodySizeLimitMiddleware` rejects oversized request bodies with
  ``413`` *before* FastAPI parses them, so a huge payload cannot be turned
  into a memory/CPU burden by the JSON decoder. Every state-changing request
  below ``/api/v1`` is covered (not just the endpoints that happened to exist
  when the cap was introduced), so a new ``POST`` route cannot silently opt
  out of the limit. The same layer refuses a body that declares a non-JSON
  ``Content-Type`` (form, multipart, ``text/plain``, ...) or a non-identity
  ``Content-Encoding`` with ``415`` before reading it: no covered route binds
  anything but JSON, so those bodies could only be rejected later at best.
* :class:`DownloadsAuthMiddleware` applies the same token check as
  ``/api/v1`` to the ``/downloads`` static mount, which is not covered by the
  router-level dependency.
* :class:`RateLimitMiddleware` keeps a sliding-window request counter per
  client IP (in memory; default 120 requests per 60 seconds). A client over
  the limit — e.g. an integration script stuck in an infinite retry loop — is
  answered with ``429`` + ``Retry-After`` instead of being able to hammer the
  local server indefinitely. ``/api/v1/health`` is exempt and stale per-IP
  entries are dropped periodically so the counter cannot grow without bound;
  a hard cap on tracked keys (``max_keys``) bounds it even inside one window.
* :class:`RequestIdMiddleware` assigns every HTTP request a correlation id:
  a client-supplied ``X-Request-ID`` is reused when it is short and made of
  safe characters, otherwise a fresh UUID4 is generated. The id is echoed on
  every response (including error responses) and prefixed to the access log,
  so a caller can name a failing request and the operator can find that exact
  request in the log without exposing any internal detail.
* :class:`RequestLoggingMiddleware` emits exactly one access-log line per
  request (``[request_id] method path status duration``). The query string is
  never logged (it may carry tokens) and response bodies are never read for
  logging, so the file content streamed from ``/downloads`` stays out of the
  log.
* :class:`CacheControlMiddleware` stamps every ``/api/v1`` and ``/downloads``
  response with the right ``Cache-Control``: short client-side caching for
  slowly changing reference data, ``no-store`` for live data and for every
  error response, long-lived public caching for exported files. A header set
  by a route (the SSE streams' ``no-cache``) is never overridden.
* :class:`ConditionalGetMiddleware` adds a strong ``ETag`` to the cacheable
  reference ``GET`` endpoints and answers a matching ``If-None-Match`` with
  ``304 Not Modified`` and no body, so a polling client transfers headers
  instead of a payload. Only explicitly listed idempotent endpoints take
  part; live and credential-bearing paths never receive a validator.
* :class:`CompressionMiddleware` gzips JSON ``/api/v1`` responses of at least
  1 KiB when the client advertised gzip, and stamps ``Vary: Accept-Encoding``
  on every governed response (including the ``304`` it passes through). It
  sits *outside* :class:`ConditionalGetMiddleware`, so the validator is
  computed from the uncompressed representation and stays identical whether
  the client asked for gzip or identity. SSE streams, ``/downloads`` files,
  body-less statuses and responses that already carry a ``Content-Encoding``
  are never compressed.
* :class:`SecurityHeadersMiddleware` adds ``X-Content-Type-Options: nosniff``
  and ``Referrer-Policy: no-referrer`` to API and download responses, and a
  sandboxing ``Content-Security-Policy`` to ``/downloads`` so an exported
  HTML/SVG file cannot execute script on the API's own origin.
* :func:`register_error_handlers` guarantees that every error response has the
  same ``{"detail": ...}`` shape and that unexpected exceptions never leak a
  stack trace to the client.

All of them are deliberately framework-light so they can be unit-tested
through ``httpx.ASGITransport`` without starting a server.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import time
import uuid
import zlib
from collections import OrderedDict, deque
from collections.abc import Iterable
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from novel_downloader.apps.common.cache_policy import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    CACHE_CONTROL_PUBLIC,
    CACHE_CONTROL_RULES,
    CACHE_POLICY_PREFIXES,
    cache_control_for,
    governs_path,
)
from novel_downloader.apps.common.compression import (
    COMPRESSION_LEVEL,
    COMPRESSION_MIN_BYTES,
    accepts_gzip,
    is_compressible_status,
    is_json_media_type,
)
from novel_downloader.apps.common.conditional_get import (
    ETAG_ENDPOINTS,
    ETAG_MAX_BODY_BYTES,
    if_none_match_matches,
    make_strong_etag,
)

from .auth import AUTH_ERROR_DETAIL, is_authorized

logger = logging.getLogger(__name__)

#: Default request-body ceiling for JSON endpoints: 1 MiB.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

#: Path prefix whose static files require the API token when one is configured.
DOWNLOADS_PREFIX = "/downloads"

#: Path prefix whose request bodies are bounded. Everything below it is
#: covered, so adding a new write endpoint cannot bypass the cap.
BODY_LIMIT_PREFIX = "/api/v1"

#: Methods whose request bodies are fully buffered by FastAPI and therefore
#: need a ceiling. ``GET``/``HEAD``/``OPTIONS`` bodies are never parsed by a
#: route and are left alone.
BODY_LIMIT_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Content encodings accepted for guarded bodies. The API parses raw JSON and
#: nothing in the stack decompresses requests, so a compressed body could not
#: work anyway. Rejecting it *before* reading means a compression bomb (a tiny
#: gzip stream expanding to gigabytes) is refused instead of being handed to any
#: future decompressor or buffered on the way.
SUPPORTED_CONTENT_ENCODINGS: frozenset[str] = frozenset({"", "identity"})

#: ``detail`` of the ``415`` response for a body whose ``Content-Type`` is not
#: JSON. The value of the header is deliberately not echoed.
UNSUPPORTED_CONTENT_TYPE_DETAIL = (
    "Unsupported Content-Type: send the request body as application/json"
)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

#: Default requests admitted per client IP per window: 120 req/min.
DEFAULT_RATE_LIMIT = 120

#: Default sliding-window length in seconds.
DEFAULT_RATE_LIMIT_WINDOW = 60.0

#: Upper bound on per-IP counters kept in memory. Periodic cleanup only runs
#: once per window, so without a hard cap a flood from many source addresses
#: could grow the dict for the whole window. ``0`` disables the cap (only
#: useful in tests).
DEFAULT_RATE_LIMIT_MAX_KEYS = 10_000

#: Paths never rate limited. Uptime monitors and orchestrators must keep
#: working even while a misbehaving client is locked out.
RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset({"/api/v1/health"})

#: Clock used by :class:`RateLimitMiddleware`. A module-level hook so tests
#: can freeze or advance time deterministically instead of sleeping.
_monotonic = time.monotonic

# ---------------------------------------------------------------------------
# Cache-Control policy
# ---------------------------------------------------------------------------

#: Requests slower than this many seconds are logged at ``WARNING``.
SLOW_REQUEST_SECONDS = 1.0

# The policy table and the resolver live in the stdlib-only shared module
# ``apps/common/cache_policy.py`` so the embedded engine — which cannot import
# this FastAPI package — applies the exact same rules. ``CACHE_CONTROL_*``,
# ``CACHE_CONTROL_RULES`` and ``CACHE_POLICY_PREFIXES`` are imported above and
# re-exported (see ``__all__``) because this is where the REST header contract
# is documented and tested.

# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------

#: Headers added to every API and download response. ``nosniff`` stops a
#: browser from re-interpreting a response as a type the server did not
#: declare; ``no-referrer`` keeps the (possibly token-bearing) API URL out of
#: ``Referer`` headers sent to third-party sites linked from a served file.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

#: ``Content-Security-Policy`` for exported files. An exported ``.html`` /
#: ``.svg`` may contain script; because ``/downloads`` shares the API origin,
#: that script could otherwise call the tokenless local API. The sandbox
#: directive blocks script execution while still letting the browser display
#: plain text and images.
DOWNLOADS_CSP = "sandbox; default-src 'none'"

# ---------------------------------------------------------------------------
# Response compression
# ---------------------------------------------------------------------------

#: Path prefixes whose JSON responses may be gzipped. Only the API tree is
#: governed: the ``/downloads`` files are already exported artifacts (often
#: already compressed) and are streamed from disk, so re-encoding them would
#: only add latency and memory use. ``/docs`` and ``/openapi.json`` are
#: deliberately outside the policy as well.
COMPRESSION_PREFIXES: tuple[str, ...] = ("/api/v1",)

# ---------------------------------------------------------------------------
# Request correlation
# ---------------------------------------------------------------------------

#: Header a client may send to name a request, and the header the API echoes
#: on every response.
REQUEST_ID_HEADER = "X-Request-ID"

#: Longest client-supplied request id accepted. Anything longer is replaced
#: with a generated id rather than truncated: an unbounded value would bloat
#: both the log line and the response header.
MAX_REQUEST_ID_LENGTH = 64

#: Characters accepted in a client-supplied id: visible ASCII without
#: whitespace, brackets or control characters. This is what keeps the value
#: from forging a second log field, injecting a header, or emitting terminal
#: escape sequences. A value that does not match is replaced, not rejected.
REQUEST_ID_PATTERN = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_REQUEST_ID_LENGTH}}}")

#: Correlation id of the request being handled in the current async context.
#: ASGI runs each request in its own task, so setting it here is isolated per
#: request; the middleware resets the token afterwards even if the app raises.
_REQUEST_ID: ContextVar[str] = ContextVar("novel_downloader_request_id", default="")


def current_request_id() -> str:
    """Return the correlation id of the request in this context, or ``""``."""
    return _REQUEST_ID.get()


def routed_path(scope: Scope) -> str:
    """
    Return the path *the router will match*, i.e. with ``root_path`` removed.

    When the application is mounted below a prefix — ``app.mount("/noveldl",
    api)``, ``uvicorn --root-path /noveldl``, or a reverse proxy that strips a
    prefix — Starlette matches routes against the path with that prefix
    removed, while ``scope["path"]`` still carries it. A prefix test on the
    raw path therefore fails to recognise a guarded tree: under
    ``root_path="/noveldl"`` the request ``/noveldl/downloads/book.txt``
    reaches the static mount (matched as ``/downloads/book.txt``) but the raw
    path does not start with ``/downloads``, so the token guard, the body cap,
    the cache policy and the sandbox CSP were all skipped. Every middleware
    that decides by path prefix must use this value instead of
    ``scope["path"]``.

    Mirrors :func:`starlette._utils.get_route_path` (which is private, so the
    few lines are duplicated rather than importing it).
    """
    path = str(scope.get("path", ""))
    root_path = str(scope.get("root_path") or "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def _new_request_id() -> str:
    """Generate a server-side correlation id."""
    return str(uuid.uuid4())


def _request_id_from_scope(scope: Scope) -> str:
    """
    Return the client's ``X-Request-ID`` when it is safe to echo, else a UUID4.

    Only the first header occurrence is considered. The value must be 1-64
    characters from :data:`REQUEST_ID_PATTERN`; anything else (non-ASCII,
    whitespace, control characters, brackets, overlong) is discarded and
    replaced by a generated id so hostile input can never reach the log.
    """
    for name, value in scope.get("headers", ()):
        if name != b"x-request-id":
            continue
        try:
            candidate: str = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if REQUEST_ID_PATTERN.fullmatch(candidate):
            return candidate
        break
    return _new_request_id()


class RequestIdMiddleware:
    """
    Correlate a request with its response and its access-log line.

    The id is resolved once, stored on ``scope["state"]["request_id"]`` (so a
    route or an error handler can read it from ``request.state``) and published
    through a :class:`~contextvars.ContextVar`. Every response gets it back in
    :data:`REQUEST_ID_HEADER`, including responses produced by inner layers
    such as the rate limiter's ``429`` or the body limit's ``413``.

    Unhandled exceptions are rendered by Starlette's outermost
    ``ServerErrorMiddleware``, *above* this layer, so the generated ``500``
    response never passes through the ``send`` wrapper; the catch-all handler
    in :func:`register_error_handlers` therefore stamps the header itself from
    the id already stored on the request state.

    This layer is installed outermost so even CORS preflight responses carry
    the header. It never reads the request body, ``Authorization`` or cookies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        # Starlette's Request.state reads and writes this exact mapping.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _REQUEST_ID.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _REQUEST_ID.reset(token)


def _content_length(scope: Scope) -> int | None:
    """Return the declared ``Content-Length`` of the request, if any."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _is_json_content_type(value: str) -> bool:
    """
    Whether a ``Content-Type`` header value denotes a JSON media type.

    ``application/json`` and any structured ``application/*+json`` type are
    accepted; parameters such as ``charset=utf-8`` are ignored. Every JSON
    route is parsed by FastAPI's JSON machinery, so a form, multipart or
    ``text/plain`` body can never bind and is refused before it is read. A
    *missing* ``Content-Type`` is left to FastAPI rather than turned into a
    ``415`` here: FastAPI does not treat a headerless body as JSON and reports
    its own ``422``, so the guard keeps the historical routing behaviour.
    """
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        return True
    return media_type.startswith("application/") and media_type.endswith("+json")


# ---------------------------------------------------------------------------
# Browser-origin gate
# ---------------------------------------------------------------------------

#: Error text for requests whose ``Origin`` is not a loopback origin.
ORIGIN_ERROR_DETAIL = (
    "Forbidden: cross-origin browser requests are not allowed on this local "
    "API. Non-browser clients (CLI, SDK, curl) are unaffected. Start the "
    "server with --cors to allow browser access from other origins."
)

#: Host name that browsers may resolve to a loopback address.
_LOCAL_HOSTNAME = "localhost"


def _is_loopback_origin(origin: str) -> bool:
    """
    Whether an ``Origin`` header value names a local (loopback) origin.

    The value must be a well-formed ``http``/``https`` origin whose host is
    ``localhost`` (case-insensitive) or a loopback IP (IPv4 ``127.0.0.0/8``
    or IPv6 ``::1``); the port is irrelevant because any local port may serve
    a helper page. Everything else — foreign hosts, look-alikes such as
    ``127.0.0.1.evil.com``, the literal ``null`` (sandboxed frames), an empty
    value and anything unparseable — is not local. Userinfo
    (``http://evil.com@127.0.0.1``) is rejected outright so the host cannot
    be smuggled past the check.
    """
    try:
        parsed = urlsplit(origin.strip())
    except ValueError:  # e.g. an IPv6 literal with invalid brackets
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host == _LOCAL_HOSTNAME:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OriginCheckMiddleware:
    """
    Answer requests with a non-loopback ``Origin`` header with ``403``.

    Threat model: by default the API binds to ``127.0.0.1`` without a token.
    That stops *remote* clients, but not *browser* ones — a website the user
    visits can still fire cross-origin requests at ``http://127.0.0.1:<port>``
    (browsers always attach ``Origin`` to cross-origin POSTs, so state-changing
    endpoints can be driven as CSRF). This gate runs closest to the router and
    closes that hole:

    * non-browser clients (CLI, SDK, curl) send no ``Origin`` and pass;
    * loopback origins (``http://127.0.0.1:any``, ``http://localhost``,
      ``http://[::1]``) pass, so locally served pages keep working;
    * every other ``Origin`` — including look-alike hosts
      (``127.0.0.1.evil.com``), userinfo tricks and the literal ``null`` —
      is answered with ``403 {"detail": ...}`` marked ``no-store``.

    Layering: it is the innermost custom layer, directly above the router.
    That keeps it *inside* the body-size limit (an oversized body keeps its
    specific ``413``), the rate limiter and the ``/downloads`` token guard,
    while still running before the router's token dependency — a forged
    origin is rejected with ``403`` even when a token is configured. Its
    ``403`` flows back out through the cache/logging layers, so it is stamped
    ``no-store`` and access-logged like every other response. ``create_app``
    does not install the gate when ``--cors`` was requested: the flag is the
    explicit opt-in for cross-origin browser access, and then CORS itself
    governs what browsers may do.

    Residual risk, by design: a same-origin DNS-rebinding page sends no
    ``Origin`` at all and is not caught by this header check; running the API
    with a token (``--token`` / ``NOVEL_API_TOKEN``) is the complete defense.

    :param app: The wrapped ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is None or _is_loopback_origin(origin):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=HTTP_403_FORBIDDEN,
            content={"detail": ORIGIN_ERROR_DETAIL},
            # Errors must never be cached, on this layer's own authority.
            headers={"Cache-Control": CACHE_CONTROL_NO_STORE},
        )
        await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """
    Reject request bodies larger than ``max_bytes`` with ``413``.

    The check runs before the application: when ``Content-Length`` already
    exceeds the limit the body is never read, and otherwise the body is read
    chunk by chunk and the request is aborted as soon as the running total
    crosses the limit (so chunked uploads without ``Content-Length`` are
    covered too). Accepted bodies are replayed to the application unchanged.
    A body declaring a ``Content-Type`` other than JSON — or a content encoding
    other than ``identity`` — is refused with ``415`` before anything is read:
    the API parses raw JSON, so a form/multipart/text body cannot bind and a
    compressed payload could only be a compression bomb.

    :param app: The wrapped ASGI application.
    :param max_bytes: Maximum accepted body size in bytes.
    :param endpoints: Explicit ``(method, path)`` pairs to guard. When omitted
        every state-changing request under :data:`BODY_LIMIT_PREFIX` is
        guarded, which is the safe default: a newly added route is covered
        automatically instead of silently accepting unbounded bodies.
    :param prefix: Path prefix used when ``endpoints`` is not supplied.
    :param methods: HTTP methods used when ``endpoints`` is not supplied.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        endpoints: Iterable[tuple[str, str]] | None = None,
        prefix: str = BODY_LIMIT_PREFIX,
        methods: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        # ``None`` means "prefix mode"; an explicit iterable keeps the exact
        # endpoint-set behaviour used by focused unit tests.
        self.endpoints = None if endpoints is None else frozenset(endpoints)
        self.prefix = "/" + prefix.strip("/")
        self.methods = frozenset(
            method.upper()
            for method in (BODY_LIMIT_METHODS if methods is None else methods)
        )

    def _applies(self, method: str, path: str) -> bool:
        """Whether this request is guarded by the size limit."""
        cleaned = path.rstrip("/") or "/"
        if self.endpoints is not None:
            return (method.upper(), cleaned) in self.endpoints
        if method.upper() not in self.methods:
            return False
        return cleaned == self.prefix or cleaned.startswith(self.prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        path = routed_path(scope)
        if not self._applies(method, path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        encodings = [
            part.strip().lower()
            for value in headers.getlist("content-encoding")
            for part in value.split(",")
        ]
        if any(encoding not in SUPPORTED_CONTENT_ENCODINGS for encoding in encodings):
            # Unsupported compressed body: reject without reading it, so a
            # compression bomb never reaches a decoder. The value is not echoed.
            response = JSONResponse(
                status_code=415,
                content={
                    "detail": (
                        "Unsupported Content-Encoding: send the request body "
                        "uncompressed (identity)"
                    )
                },
            )
            await response(scope, receive, send)
            return

        content_type = headers.get("content-type")
        if content_type is not None and not _is_json_content_type(content_type):
            # No covered route binds anything but JSON (FastAPI form models
            # are not used), so a declared form/multipart/text body is refused
            # before it is read rather than handed to a parser that would
            # reject it anyway. A bodyless request still fails here only when
            # it deliberately declares a non-JSON type.
            response = JSONResponse(
                status_code=415,
                content={"detail": UNSUPPORTED_CONTENT_TYPE_DETAIL},
            )
            await response(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # The client went away; there is nobody to answer.
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False
        buffered = bytes(body)

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send a ``413`` error response without reading the offending body."""
        response = JSONResponse(
            status_code=413,
            content={
                "detail": (
                    "Request body too large: the limit for this endpoint is "
                    f"{self.max_bytes} bytes"
                )
            },
        )
        await response(scope, receive, send)


class DownloadsAuthMiddleware:
    """
    Require the configured API token for requests under ``/downloads``.

    Static files are served by a ``Mount`` rather than a route, so the
    router-level :func:`novel_downloader.apps.api.auth.require_token` dependency
    never runs for them. This middleware closes that gap: when
    ``app.state.api_token`` is set, ``Authorization: Bearer <token>`` or
    ``X-API-Key: <token>`` is mandatory and a missing/mismatched token yields
    ``401 {"detail": ...}``. When no token is configured the request passes
    through unchanged, preserving the localhost-only default.

    :param app: The wrapped ASGI application.
    :param prefix: Path prefix to guard; defaults to ``/downloads``.
    """

    def __init__(self, app: ASGIApp, *, prefix: str = DOWNLOADS_PREFIX) -> None:
        self.app = app
        self.prefix = "/" + prefix.strip("/")

    def _guards(self, path: str) -> bool:
        """Whether ``path`` belongs to the guarded mount (not a look-alike)."""
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._guards(routed_path(scope)):
            await self.app(scope, receive, send)
            return

        state = getattr(scope.get("app"), "state", None)
        expected = getattr(state, "api_token", None)
        if not expected:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if is_authorized(
            expected,
            headers.get("authorization"),
            headers.get("x-api-key"),
        ):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={"detail": AUTH_ERROR_DETAIL},
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


class RateLimitMiddleware:
    """
    Limit each client IP to ``limit`` requests per sliding ``window`` seconds.

    A true sliding window: every admitted request is timestamped and the
    counter keeps one ``deque`` of timestamps per client IP. A request is
    admitted while fewer than ``limit`` timestamps fall inside
    ``(now - window, now]``; once the IP is over the limit it receives::

        429 {"detail": "Rate limit exceeded. Try again in <seconds>s."}

    with a ``Retry-After`` header naming how long it takes until the oldest
    admitted request slides out of the window, rounded up to a whole second
    (minimum ``1``, never more than ``window``). Rejected requests are *not*
    counted, so a client that stops hammering recovers as soon as its window
    drains.

    Design notes:

    * Purpose: a buggy integration client stuck in a request loop must not be
      able to hammer the local server indefinitely. Counting happens *before*
      authentication, so a token brute-force is throttled too.
    * ``limit <= 0`` disables the middleware entirely (pass-through). The
      public entry points never pass such a value: ``--rate-limit 0`` is
      rejected at startup and disabling is requested explicitly with
      ``--no-rate-limit`` (or ``create_app(rate_limit=None)``).
    * Paths in :data:`RATE_LIMIT_EXEMPT_PATHS` (``/api/v1/health``) are never
      limited: uptime monitors must keep working even while a client is
      locked out.
    * State is a plain in-memory ``dict`` updated without an ``await`` between
      read and write, i.e. race-free under uvicorn's single event loop and
      free of locking. It is per-process by design and deliberately neither
      shared nor persisted:

      - **Multiple workers** each keep their own counters, so one client that
        reaches ``N`` workers can be admitted up to ``N × limit`` per window
        (a single keep-alive connection stays with one worker). ``novel-api``
        always runs one process — it hands the application object to uvicorn,
        which refuses ``--workers > 1`` — so this only affects a hand-rolled
        ``uvicorn --workers`` deployment; divide ``limit`` by the worker
        count there.
      - **Restarts** start with an empty table: every client immediately
        regains a full budget. Persisting counters would let a stale lockout
        survive a restart and add a shared-storage failure mode for no
        benefit on a localhost service.
      - **Clock**: the window uses ``time.monotonic``, so NTP steps and manual
        wall-clock changes can neither unlock a client nor extend a lockout.
        If the hook were ever non-monotonic, a backwards step fails closed: an
        already-full window keeps rejecting and ``Retry-After`` stays at
        least 1.

    * The client IP is taken from the ASGI scope (the TCP peer);
      ``X-Forwarded-For`` is deliberately not honoured on a localhost
      service. Behind a reverse proxy every client therefore shares the
      proxy peer's bucket.
    * Stale per-IP entries are dropped at most once per window
      (:meth:`_maybe_cleanup`), keeping memory bounded even with many
      short-lived client IPs. Because that periodic sweep can lag a burst of
      distinct source addresses, :attr:`max_keys` imposes a hard ceiling:
      when a new key would exceed it, stale entries are swept immediately and
      then the least recently active keys are evicted. Eviction is fail-open
      per key — the evicted counter is gone, so that client starts a fresh
      window — and only a request from a *new* key triggers it, so an
      already-tracked client cannot reset its own budget by hammering.
      Recency is maintained incrementally in an ``OrderedDict`` (every
      admitted hit moves its key to the end), so admitting a new key at the
      cap is O(1) amortised instead of sorting the whole table — the cap must
      not become a CPU amplifier under an address flood.

    :param app: The wrapped ASGI application.
    :param limit: Requests admitted per client IP per window; ``<= 0``
        disables (the public entry points validate the value first).
    :param window: Sliding-window length in seconds.
    :param exempt_paths: Paths never limited; defaults to
        :data:`RATE_LIMIT_EXEMPT_PATHS`.
    :param max_keys: Maximum number of per-IP counters held in memory;
        ``<= 0`` disables the cap.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = DEFAULT_RATE_LIMIT,
        window: float = DEFAULT_RATE_LIMIT_WINDOW,
        exempt_paths: Iterable[str] | None = None,
        max_keys: int = DEFAULT_RATE_LIMIT_MAX_KEYS,
    ) -> None:
        self.app = app
        self.limit = int(limit)
        self.window = float(window)
        self.exempt_paths = frozenset(
            RATE_LIMIT_EXEMPT_PATHS if exempt_paths is None else exempt_paths
        )
        self.max_keys = int(max_keys)
        #: ``key -> timestamps`` with recency order: the first item is the least
        #: recently *admitted* request's key. Every admitted hit moves its key
        #: to the end, so eviction never has to scan or sort the table.
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._last_cleanup = 0.0

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        """Best-effort client identity for the counter (the TCP peer)."""
        client = scope.get("client")
        host = client[0] if client else None
        return str(host) if host else "unknown"

    def _is_exempt(self, path: str) -> bool:
        """Whether ``path`` is never limited (trailing slash normalised)."""
        cleaned = path.rstrip("/") or "/"
        return cleaned in self.exempt_paths

    def _register_hit(self, key: str, now: float) -> int | None:
        """
        Count one request for ``key`` at ``now``.

        :return: ``None`` when the request is admitted (its timestamp was
            recorded), otherwise the ``Retry-After`` seconds until a slot
            frees up.
        """
        if key not in self._hits:
            self._make_room(now)
        timestamps = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if self.limit > 0 and len(timestamps) >= self.limit:
            return max(math.ceil(timestamps[0] + self.window - now), 1)
        timestamps.append(now)
        # Admitted activity refreshes the key's position in the LRU order; a
        # rejected request deliberately does not, so a hammering client stays
        # close to eviction and cannot displace well-behaved keys.
        self._hits.move_to_end(key)
        self._maybe_cleanup(now)
        return None

    def _make_room(self, now: float) -> None:
        """
        Keep the counter dict under :attr:`max_keys` before adding a new key.

        Stale entries are swept first (ignoring the periodic-cleanup interval,
        because the cap is a hard bound). Because the dict is ordered by last
        admitted activity, expired entries form a prefix: popping it costs
        O(expired), not O(tracked). If the dict is still full, the least
        recently active keys are evicted the same way. An evicted key loses
        its history and therefore regains a full budget (fail-open), but it is
        the key closest to expiry anyway, and only a request from a *new* key
        can trigger eviction — a client that is already tracked, including one
        being rejected, cannot reset its own counter this way.
        """
        if self.max_keys <= 0 or len(self._hits) < self.max_keys:
            return
        cutoff = now - self.window
        while self._hits:
            timestamps = next(iter(self._hits.values()))
            if timestamps and timestamps[-1] > cutoff:
                break
            self._hits.popitem(last=False)
        if len(self._hits) < self.max_keys:
            return
        # Evict just enough keys to make room for the new one.
        excess = len(self._hits) - self.max_keys + 1
        for _ in range(excess):
            self._hits.popitem(last=False)

    def _maybe_cleanup(self, now: float) -> None:
        """Drop per-IP state that has fully slid out of the window."""
        if now - self._last_cleanup < self.window:
            return
        cutoff = now - self.window
        while self._hits:
            timestamps = next(iter(self._hits.values()))
            if timestamps and timestamps[-1] > cutoff:
                break
            self._hits.popitem(last=False)
        self._last_cleanup = now

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = routed_path(scope)
        if self.limit <= 0 or self._is_exempt(path):
            await self.app(scope, receive, send)
            return

        retry_after = self._register_hit(self._client_ip(scope), _monotonic())
        if retry_after is not None:
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Try again in {retry_after}s."
                },
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class CacheControlMiddleware:
    """
    Stamp every API response with the ``Cache-Control`` it should carry.

    Successful responses get the value of the first boundary-aware prefix rule
    in :data:`CACHE_CONTROL_RULES`:

    * ``/api/v1/sites``, ``/api/v1/history`` and ``/api/v1/music/platforms``:
      ``private, max-age=60`` — reference data the client may reuse briefly.
    * ``/api/v1/tasks...``: ``no-store`` — live task state must be refetched.
    * ``/downloads...``: ``public, max-age=3600`` — exported files are
      write-once, so exported-file URLs never change their content.

    Every other ``/api/v1`` path and every ``4xx``/``5xx`` response (413 body
    limit, 401 auth, 404, 422, 500, ...) gets ``no-store``: errors must never
    be cached. Responses outside the governed prefixes pass through without a
    header, and a ``Cache-Control`` already set by a route — the SSE streams'
    ``no-cache`` — is never overridden. The middleware only rewrites the
    response head; bodies are streamed through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        cleaned = routed_path(scope).rstrip("/") or "/"
        if not governs_path(cleaned):
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    value = cache_control_for(cleaned, int(message["status"]))
                    if value is not None:
                        headers["cache-control"] = value
            await send(message)

        await self.app(scope, receive, send_with_cache)


def _response_header(
    headers: list[tuple[bytes, bytes]],
    name: str,
) -> str | None:
    """Return the decoded value of ``name`` in a raw response header list."""
    wanted = name.lower().encode("latin-1")
    for raw_name, raw_value in headers:
        if raw_name.lower() == wanted:
            return raw_value.decode("latin-1")
    return None


def _set_response_header(
    headers: list[tuple[bytes, bytes]],
    name: str,
    value: str,
) -> None:
    """Set (or replace) one header in a raw response header list."""
    wanted = name.lower().encode("latin-1")
    encoded = value.encode("latin-1")
    for index, (raw_name, _raw_value) in enumerate(headers):
        if raw_name.lower() == wanted:
            headers[index] = (raw_name, encoded)
            return
    headers.append((name.encode("latin-1"), encoded))


def _remove_response_header(
    headers: list[tuple[bytes, bytes]],
    name: str,
) -> None:
    """Drop every occurrence of ``name`` from a raw response header list."""
    wanted = name.lower().encode("latin-1")
    headers[:] = [
        (raw_name, raw_value)
        for raw_name, raw_value in headers
        if raw_name.lower() != wanted
    ]


class ConditionalGetMiddleware:
    """
    Answer a matching ``If-None-Match`` request with ``304 Not Modified``.

    Only the endpoints in :data:`ETAG_ENDPOINTS` participate: the reference
    ``GET`` routes whose bodies are idempotent, non-sensitive and already
    marked ``private, max-age=60`` by :class:`CacheControlMiddleware`. Live
    or credential-bearing paths (``/tasks*``, ``/login*``, ``/music/jobs*``
    and everything else) never receive a validator, so a stale client cannot
    revalidate them into a false cache hit.

    For an eligible ``200`` the body is buffered (up to ``max_body_bytes``)
    and the response head is held back until the final chunk arrives; the
    strong entity tag of the exact bytes is then added as ``ETag``. A
    matching ``If-None-Match`` — weak comparison, so ``W/"..."`` matches too,
    a comma-separated list matches on any member, and ``*`` matches always —
    turns the response into ``304`` with no body. ``ETag`` is kept; the outer
    :class:`CacheControlMiddleware`, security-header and request-id layers
    stamp ``Cache-Control``, ``no-store``-free success headers and
    ``X-Request-ID`` on the 304 exactly as they would on a ``200``.

    A response whose declared or actual size exceeds ``max_body_bytes`` is
    passed through unbuffered and without an ``ETag`` (logged at ``DEBUG``):
    hashing a very large body would cost more than the bandwidth saved. A
    route that already set an ``ETag`` is left alone, and non-``200``
    responses (errors, redirects) never get one.

    The middleware only rewrites the response head and, for a 304, drops
    ``Content-Length``/``Content-Type`` (a 304 has no message body). Request
    bodies are never read.

    :param app: The wrapped ASGI application.
    :param endpoints: Paths (``routed_path`` form) that take part.
    :param max_body_bytes: Largest body that is buffered and hashed.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        endpoints: Iterable[str] = ETAG_ENDPOINTS,
        max_body_bytes: int = ETAG_MAX_BODY_BYTES,
    ) -> None:
        self.app = app
        self.endpoints = frozenset(path.rstrip("/") or "/" for path in endpoints)
        self.max_body_bytes = int(max_body_bytes)

    def _applies(self, method: str, path: str) -> bool:
        """Whether this request is an eligible conditional ``GET``."""
        return method == "GET" and (path.rstrip("/") or "/") in self.endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        if not self._applies(method, routed_path(scope)):
            await self.app(scope, receive, send)
            return

        if_none_match = Headers(scope=scope).get("if-none-match")
        start: Message | None = None
        released = False
        streaming = False
        buffered = bytearray()

        async def send_start() -> None:
            """Send the held-back response head exactly once."""
            nonlocal released
            if released or start is None:
                return
            released = True
            await send(start)

        async def capture(message: Message) -> None:
            nonlocal start, released, streaming

            if streaming:
                await send(message)
                return

            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if int(message.get("status", 0)) != 200:
                    streaming = True
                    await send(message)
                    return
                if _response_header(headers, "etag") is not None:
                    # An explicit route-level validator is authoritative.
                    streaming = True
                    await send(message)
                    return
                declared = _response_header(headers, "content-length")
                if declared is not None:
                    try:
                        too_large = int(declared) > self.max_body_bytes
                    except ValueError:
                        too_large = False
                    if too_large:
                        logger.debug(
                            "ETag skipped for %s: declared body exceeds %d bytes",
                            routed_path(scope),
                            self.max_body_bytes,
                        )
                        streaming = True
                        await send(message)
                        return
                message["headers"] = headers
                start = message
                return

            if message["type"] != "http.response.body":
                # Unexpected message (e.g. trailers): stop rewriting and let it
                # through after the head that is already held.
                streaming = True
                await send_start()
                await send(message)
                return

            chunk = message.get("body", b"")
            if start is None:
                # A body before the response head violates ASGI; do not try to
                # rewrite it (and never hold bytes that have no head to pair
                # with). Pass it through as-is.
                streaming = True
                await send(message)
                return
            if len(buffered) + len(chunk) > self.max_body_bytes:
                logger.debug(
                    "ETag skipped for %s: body exceeds %d bytes",
                    routed_path(scope),
                    self.max_body_bytes,
                )
                streaming = True
                await send_start()
                if buffered:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": bytes(buffered),
                            "more_body": True,
                        }
                    )
                    buffered.clear()
                await send(message)
                return

            buffered.extend(chunk)
            if message.get("more_body", False):
                return

            body = bytes(buffered)
            etag = make_strong_etag(body)
            headers = list(start.get("headers") or [])
            _set_response_header(headers, "ETag", etag)
            if if_none_match_matches(if_none_match, etag):
                # A 304 carries no representation: drop the body metadata but
                # keep ETag so the client can refresh its stored validator.
                _remove_response_header(headers, "Content-Length")
                _remove_response_header(headers, "Content-Type")
                _remove_response_header(headers, "Transfer-Encoding")
                start["headers"] = headers
                start["status"] = 304
                released = True
                await send(start)
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
                return
            start["headers"] = headers
            released = True
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        completed = False
        try:
            await self.app(scope, receive, capture)
            completed = True
        finally:
            if completed and not streaming and not released and start is not None:
                # The downstream app ended without a body message; terminate
                # the response so the connection cannot hang.
                await send(start)
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )


def _append_vary(headers: MutableHeaders, token: str) -> None:
    """
    Add ``token`` to the ``Vary`` header without duplicating an existing one.

    ``Vary`` is a comma-separated list; ``*`` already means "varies on
    everything", so it is left untouched. Matching is case-insensitive, as
    field names and header values in ``Vary`` are.
    """
    existing = headers.get("vary")
    if not existing:
        headers["vary"] = token
        return
    present = {part.strip().lower() for part in existing.split(",")}
    if "*" in present or token.lower() in present:
        return
    headers["vary"] = f"{existing}, {token}"


class CompressionMiddleware:
    """
    Gzip eligible ``/api/v1`` JSON responses and advertise the negotiation.

    A response is compressed only when all of the following hold: the client's
    ``Accept-Encoding`` admits gzip (:func:`accepts_gzip`, honouring ``q``
    weights and ``*``), the response is a ``2xx`` with a body, its
    ``Content-Type`` is JSON, it does not already carry a ``Content-Encoding``,
    and the body is at least :data:`COMPRESSION_MIN_BYTES` (1 KiB). If the body
    turns out smaller than that, it is released unchanged with its original
    ``Content-Length``. Once compression starts, ``Content-Length`` is removed
    (the encoded length is not known until the stream ends) and the body is
    re-encoded chunk by chunk.

    Because the remaining body is streamed through ``zlib``, SSE streams and
    exported files are excluded by media type/path rather than by buffering:
    they pass through untouched and unbuffered. ``304 Not Modified`` (and
    ``204``/``205``/``206``) are also passed through — there is no
    representation to compress — but still receive ``Vary``.

    ``Vary: Accept-Encoding`` is set on every governed response, identity ones
    included, so a shared cache never reuses a gzip body for a client that
    did not ask for it. The header is added *before* the compression decision,
    which is what makes the ``304`` produced by
    :class:`ConditionalGetMiddleware` carry it too.

    Ordering: this layer must sit *outside* :class:`ConditionalGetMiddleware`,
    which buffers the uncompressed body and derives the strong ``ETag`` from
    those exact bytes. Compressing afterwards leaves the validator unchanged
    across encodings, so an ``If-None-Match`` from either a gzip or an identity
    round trip still yields ``304``. Installing it inside would hash the gzip
    bytes and break that invariant. The cache, security-header, CORS and
    request-id layers are further out and stamp the compressed response
    exactly as they would an identity one.

    :param app: The wrapped ASGI application.
    :param min_bytes: Smallest body worth compressing (default 1 KiB).
    :param level: zlib compression level (default 6).
    :param prefixes: Path prefixes governed by the policy.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        min_bytes: int = COMPRESSION_MIN_BYTES,
        level: int = COMPRESSION_LEVEL,
        prefixes: Iterable[str] = COMPRESSION_PREFIXES,
    ) -> None:
        self.app = app
        self.min_bytes = max(int(min_bytes), 0)
        self.level = int(level)
        self.prefixes = tuple("/" + prefix.strip("/") for prefix in prefixes)

    def _governs(self, path: str) -> bool:
        """Whether the policy covers this path (boundary-aware prefix match)."""
        cleaned = path.rstrip("/") or "/"
        return any(
            cleaned == prefix or cleaned.startswith(prefix + "/")
            for prefix in self.prefixes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if not self._governs(routed_path(scope)):
            await self.app(scope, receive, send)
            return

        # A HEAD reply carries no body to transfer; its head keeps the identity
        # representation's ``Content-Length`` (as HEAD should mirror GET).
        accepted = str(scope.get("method", "")).upper() != "HEAD" and accepts_gzip(
            Headers(scope=scope).get("accept-encoding")
        )

        start: Message | None = None
        buffered = bytearray()
        compressor: Any = None
        #: ``idle`` (no head yet), ``pass`` (streaming untouched), ``hold``
        #: (head held back while the body is buffered) or ``gzip`` (streaming
        #: the re-encoded body).
        mode = "idle"
        finished = False

        async def emit_body(data: bytes, more: bool) -> None:
            await send({"type": "http.response.body", "body": data, "more_body": more})

        async def begin_gzip() -> None:
            """Turn the held head into a gzip reply and send it."""
            nonlocal mode, compressor
            assert start is not None
            headers = MutableHeaders(scope=start)
            headers["Content-Encoding"] = "gzip"
            if "content-length" in headers:
                del headers["content-length"]
            compressor = zlib.compressobj(
                self.level, zlib.DEFLATED, 16 + zlib.MAX_WBITS
            )
            mode = "gzip"
            await send(start)

        async def handle_body(body: bytes, more: bool) -> None:
            nonlocal mode, finished
            if mode == "pass":
                await emit_body(body, more)
                return
            if mode == "gzip":
                assert compressor is not None
                data = compressor.compress(body) if body else b""
                if not more:
                    data += compressor.flush()
                    finished = True
                await emit_body(data, more)
                return
            if mode == "hold":
                buffered.extend(body)
                if more and len(buffered) < self.min_bytes:
                    return
                if not more and len(buffered) < self.min_bytes:
                    # Below the threshold: release the head and body untouched.
                    mode = "pass"
                    assert start is not None
                    await send(start)
                    await emit_body(bytes(buffered), False)
                    finished = True
                    return
                await begin_gzip()
                assert compressor is not None
                data = compressor.compress(bytes(buffered))
                buffered.clear()
                if not more:
                    data += compressor.flush()
                    finished = True
                await emit_body(data, more)
                return
            # ``idle``: a body without a response head violates ASGI; never
            # hold bytes that have no head to pair with.
            mode = "pass"
            await emit_body(body, more)

        async def send_with_compression(message: Message) -> None:
            nonlocal mode, start
            message_type = message["type"]

            if message_type == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                # Added before the decision so every governed response — the
                # 304 passthrough included — advertises the negotiation.
                _append_vary(headers, "Accept-Encoding")
                status = int(message.get("status", 0))
                compressible = (
                    accepted
                    and is_compressible_status(status)
                    and "content-encoding" not in headers
                    and is_json_media_type(headers.get("content-type"))
                )
                if compressible:
                    start = message
                    mode = "hold"
                    return
                mode = "pass"
                await send(message)
                return

            if message_type == "http.response.body":
                await handle_body(
                    message.get("body", b""), bool(message.get("more_body", False))
                )
                return

            # Trailers (or an extended message): release the held head
            # untouched and let the message through.
            if mode == "hold" and start is not None:
                mode = "pass"
                await send(start)
                if buffered:
                    await emit_body(bytes(buffered), True)
                    buffered.clear()
            await send(message)

        completed = False
        try:
            await self.app(scope, receive, send_with_compression)
            completed = True
        finally:
            # Never touch the response when the application raised: the error
            # has to propagate to the outer error layer untouched.
            if completed and start is not None:
                if mode == "hold":
                    # The app ended without a final body message: serve what
                    # was buffered, compressed when it crossed the threshold.
                    if len(buffered) >= self.min_bytes:
                        await begin_gzip()
                        assert compressor is not None
                        data = compressor.compress(bytes(buffered)) + compressor.flush()
                        buffered.clear()
                        await emit_body(data, False)
                    else:
                        await send(start)
                        await emit_body(bytes(buffered), False)
                elif mode == "gzip" and not finished:
                    # The stream ended without its final chunk: close the gzip
                    # member so the client does not wait for a trailer.
                    assert compressor is not None
                    await emit_body(compressor.flush(), False)


class SecurityHeadersMiddleware:
    """
    Add defence-in-depth response headers to API and download responses.

    Every ``/api/v1`` and ``/downloads`` response receives
    :data:`SECURITY_HEADERS`; ``/downloads`` additionally receives
    :data:`DOWNLOADS_CSP`. The sandboxed policy matters because exported files
    are served from the same origin as the API: without it, a malicious book
    exported as HTML could run script in the browser and drive the tokenless
    local API. A header already set by a route is left alone.

    :param app: The wrapped ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _matches(cleaned: str, prefix: str) -> bool:
        """Whether ``cleaned`` is ``prefix`` itself or below it."""
        return cleaned == prefix or cleaned.startswith(prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        cleaned = routed_path(scope).rstrip("/") or "/"
        governs_api = self._matches(cleaned, "/api/v1")
        governs_downloads = self._matches(cleaned, DOWNLOADS_PREFIX)
        if not (governs_api or governs_downloads):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name.lower() not in headers:
                        headers[name] = value
                if governs_downloads and "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = DOWNLOADS_CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestLoggingMiddleware:
    """
    Emit exactly one access-log line per HTTP request.

    The line has a single fixed shape for every outcome::

        [3f2a...] GET /api/v1/sites 200 1.8ms

    i.e. ``[request_id] method path status duration_ms``. The id comes from
    :class:`RequestIdMiddleware`; when this middleware is driven on its own
    (unit tests, or an app that does not install the request-id layer) the
    field is ``-``. Fast requests are logged at ``INFO``; requests that took
    longer than ``slow_seconds`` (default :data:`SLOW_REQUEST_SECONDS`) are
    logged at ``WARNING`` instead, with the same line format.

    Privacy guarantees:

    * The query string is never logged — only the plain path — because query
      parameters may carry tokens or other credentials.
    * Response bodies are never read or logged; in particular the file
      content streamed from ``/downloads`` never reaches the log.

    The middleware wraps ``send`` only: request bodies pass through untouched
    and nothing is buffered. A request that ends without any response (the
    client disconnected, or an error propagated above the router to the outer
    ``ServerErrorMiddleware``) is still logged once, reported as ``500``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        slow_seconds: float = SLOW_REQUEST_SECONDS,
    ) -> None:
        self.app = app
        self.slow_seconds = float(slow_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "-"))
        # Deliberately the path only: the query string may carry secrets.
        path = str(scope.get("path", "-"))
        state = scope.get("state") or {}
        request_id = str(state.get("request_id") or "-")
        started = time.perf_counter()
        logged = False

        async def send_and_log(message: Message) -> None:
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                logged = True
                self._log(
                    method,
                    path,
                    int(message["status"]),
                    time.perf_counter() - started,
                    request_id,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_and_log)
        finally:
            if not logged:
                # No response head ever went out (client disconnect or an
                # error raised past the router): still record the request,
                # reported as the failure it was.
                self._log(method, path, 500, time.perf_counter() - started, request_id)

    def _log(
        self, method: str, path: str, status: int, elapsed: float, request_id: str
    ) -> None:
        """Emit the single access-log line: INFO, or WARNING when slow."""
        line = f"[{request_id}] {method} {path} {status} {elapsed * 1000:.1f}ms"
        if elapsed > self.slow_seconds:
            logger.warning("%s", line)
        else:
            logger.info("%s", line)


def _validation_detail(exc: RequestValidationError) -> str:
    """Render a validation failure as one readable sentence."""
    parts: list[str] = []
    for error in exc.errors():
        location = [
            str(part)
            for part in error.get("loc", ())
            if part not in ("body", "query", "path", "header")
        ]
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{'.'.join(location)}: {message}" if location else message)
    if not parts:
        return "Request validation failed"
    return "Request validation failed: " + "; ".join(parts)


def _validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """
    Return machine-readable validation errors.

    Only plain-string fields are kept: Pydantic error entries may carry a
    ``ctx`` holding the original exception or non-JSON ``input`` bytes, which
    would make the response unserialisable.
    """
    return [
        {
            "type": str(error.get("type", "")),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
        }
        for error in exc.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    """
    Register the API's error handlers.

    * ``422`` keeps the ``detail`` key but renders it as a readable sentence and
      adds an ``errors`` list with the raw Pydantic entries.
    * Any unhandled exception becomes ``500 {"detail": "Internal server error"}``
      with no traceback or exception message exposed to the caller.
    """

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": _validation_detail(exc),
                "errors": _validation_errors(exc),
            },
        )

    @app.exception_handler(Exception)
    async def _on_unhandled_error(request: Request, _exc: Exception) -> JSONResponse:
        # Never include ``str(exc)`` or a traceback: it may reveal filesystem
        # paths, tokens or provider internals to the client.
        #
        # This response is produced by Starlette's outermost error layer, *above*
        # the Cache-Control/SecurityHeaders/CORS middleware, so it would
        # otherwise be the only response class without ``no-store``, the
        # ``nosniff``/``no-referrer`` headers, and (in ``--cors`` mode) the
        # origin header a browser needs to read it. Stamp them here as well.
        #
        # The request id also has to be written here for the same reason: the
        # request-id middleware's ``send`` wrapper never sees this response.
        request_id = getattr(request.state, "request_id", None) or current_request_id()
        headers: dict[str, str] = {
            "Cache-Control": CACHE_CONTROL_NO_STORE,
            REQUEST_ID_HEADER: request_id or _new_request_id(),
            **dict(SECURITY_HEADERS),
        }
        if getattr(request.app.state, "cors_enabled", False) and request.headers.get(
            "origin"
        ):
            # Mirror CORSMiddleware: a wildcard origin is only meaningful (and
            # only emitted) when the request actually carries an Origin.
            headers["Access-Control-Allow-Origin"] = "*"
            # And let the browser read the id, as CORSMiddleware does for the
            # responses that pass through it (this one does not).
            headers["Access-Control-Expose-Headers"] = REQUEST_ID_HEADER
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers=headers,
        )


__all__ = [
    "AUTH_ERROR_DETAIL",
    "BODY_LIMIT_METHODS",
    "BODY_LIMIT_PREFIX",
    "CACHE_CONTROL_NO_STORE",
    "CACHE_CONTROL_PRIVATE",
    "CACHE_CONTROL_PUBLIC",
    "CACHE_CONTROL_RULES",
    "CACHE_POLICY_PREFIXES",
    "COMPRESSION_PREFIXES",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_LIMIT_MAX_KEYS",
    "DEFAULT_RATE_LIMIT_WINDOW",
    "DOWNLOADS_CSP",
    "DOWNLOADS_PREFIX",
    "ETAG_ENDPOINTS",
    "ETAG_MAX_BODY_BYTES",
    "MAX_REQUEST_ID_LENGTH",
    "ORIGIN_ERROR_DETAIL",
    "RATE_LIMIT_EXEMPT_PATHS",
    "REQUEST_ID_HEADER",
    "REQUEST_ID_PATTERN",
    "SECURITY_HEADERS",
    "SLOW_REQUEST_SECONDS",
    "BodySizeLimitMiddleware",
    "CacheControlMiddleware",
    "CompressionMiddleware",
    "ConditionalGetMiddleware",
    "DownloadsAuthMiddleware",
    "OriginCheckMiddleware",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "RequestLoggingMiddleware",
    "SecurityHeadersMiddleware",
    "current_request_id",
    "register_error_handlers",
]
