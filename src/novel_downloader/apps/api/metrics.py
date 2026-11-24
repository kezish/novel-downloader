#!/usr/bin/env python3
"""
novel_downloader.apps.api.metrics
---------------------------------

Process-local runtime counters exposed by ``GET /api/v1/metrics``.

The registry is deliberately small and label-free: it records request totals,
status classes, method buckets, the rejections the API itself produces
(rate-limit ``429`` and body-guard ``413``/``415``) and concurrent
Server-Sent-Events connections. It never stores a path, query string, client
IP, token or keyword, so the response has a fixed size no matter what a client
sends.

Why these dimensions and nothing else
-------------------------------------
Cardinality is the failure mode of an in-process metrics endpoint: one label
per path (or per 404 URL a scanner probes) would let a remote caller grow the
process's memory without bound and turn every scrape into a large payload.
The keys here are therefore fixed tuples:

* :data:`STATUS_CLASSES` — ``2xx``, ``3xx``, ``4xx``, ``5xx`` and ``other``;
* :data:`METHOD_BUCKETS` — the verbs the API serves plus ``other`` for
  anything else (a client may send an arbitrary method token, so the raw value
  is bucketed instead of stored).

Concurrency
-----------
Every mutation and the snapshot are guarded by one :class:`threading.Lock`.
The REST server normally runs on a single event loop, but the lock keeps the
counters exact when the app object is driven from several OS threads (tests,
embedded hosts, a hand-rolled multi-threaded ASGI server) and costs nothing
measurable at the request rates a local service sees.

Semantics of the numbers
------------------------
* ``requests.total`` counts requests *received* (including the metrics probe
  that is reading this snapshot and any still in flight).
* ``requests.completed`` counts responses *fully handled*; the per-status-class
  map sums to this value. The probe's own response is counted only after it has
  been sent, so a snapshot always shows ``completed <= total``.
* ``requests.rejections`` counts the API's own guards, not every response with
  that status: ``rate_limited`` is the sliding-window limiter's ``429`` and the
  two body counters are :class:`~novel_downloader.apps.api.middleware.
  BodySizeLimitMiddleware`'s ``413``/``415``. A ``429`` from the music job
  queue is a normal ``4xx`` in ``by_status_class`` but is not a request-rate
  rejection.
* ``sse.active`` is the number of ``text/event-stream`` responses currently
  streaming; ``sse.peak`` is the highest simultaneous value seen since start.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from novel_downloader import __version__

#: Status buckets. Every final HTTP status lands in exactly one of them;
#: ``other`` holds informational codes and anything outside the common ranges.
STATUS_CLASSES: Final[tuple[str, ...]] = ("2xx", "3xx", "4xx", "5xx", "other")

#: Method buckets. An arbitrary method token (``BREW``, a scanner's probe) is
#: folded into ``other`` so the map cannot grow with client input.
METHOD_BUCKETS: Final[tuple[str, ...]] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "other",
)

#: ``Cache-Control`` value every metrics response carries. The counts are live,
#: so no proxy or browser may store them.
METRICS_CACHE_CONTROL: Final[str] = "no-store"


def status_class(status: int) -> str:
    """Return the fixed bucket for an HTTP status code."""
    if 200 <= status <= 299:
        return "2xx"
    if 300 <= status <= 399:
        return "3xx"
    if 400 <= status <= 499:
        return "4xx"
    if 500 <= status <= 599:
        return "5xx"
    return "other"


def method_bucket(method: str) -> str:
    """Return the fixed bucket for an HTTP method token (case-insensitive)."""
    upper = method.upper()
    return upper if upper in METHOD_BUCKETS else "other"


class MetricsRegistry:
    """
    Thread-safe counters for one application process.

    A registry belongs to one :func:`novel_downloader.apps.api.create_app`
    instance, which normally means one process. Restarts start at zero;
    separate workers keep separate registries (nothing is shared or
    persisted), exactly like the rate limiter.

    :param version: Version string reported by the endpoint.
    :param monotonic: Clock used for ``uptime_s``; defaults to
        :func:`time.monotonic`, so wall-clock changes cannot move it.
    :param wall_clock: Clock used once for ``started_at``.
    """

    def __init__(
        self,
        *,
        version: str = __version__,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._version = version
        self._monotonic = monotonic
        self._started_at = wall_clock()
        self._started_mono = monotonic()
        self._lock = threading.Lock()

        self._total_requests = 0
        self._completed_requests = 0
        self._by_status: dict[str, int] = dict.fromkeys(STATUS_CLASSES, 0)
        self._by_method: dict[str, int] = dict.fromkeys(METHOD_BUCKETS, 0)
        self._rate_limited = 0
        self._body_too_large = 0
        self._unsupported_media_type = 0
        self._sse_active = 0
        self._sse_peak = 0

    # -- recording ---------------------------------------------------------

    def request_started(self, method: str) -> None:
        """Count one received request (``total`` and the method bucket)."""
        bucket = method_bucket(method)
        with self._lock:
            self._total_requests += 1
            self._by_method[bucket] += 1

    def request_finished(self, status: int) -> None:
        """Count one handled response (``completed`` and the status bucket)."""
        bucket = status_class(status)
        with self._lock:
            self._completed_requests += 1
            self._by_status[bucket] += 1

    def record_rate_limited(self) -> None:
        """Count one request rejected by the sliding-window rate limiter."""
        with self._lock:
            self._rate_limited += 1

    def record_body_rejection(self, status: int) -> None:
        """
        Count one request refused by the request-body guard.

        Only ``413`` and ``415`` are meaningful here; another status is ignored
        so a future caller cannot invent a new counter.
        """
        with self._lock:
            if status == 413:
                self._body_too_large += 1
            elif status == 415:
                self._unsupported_media_type += 1

    def stream_opened(self) -> None:
        """Count one active SSE response and update the peak."""
        with self._lock:
            self._sse_active += 1
            if self._sse_active > self._sse_peak:
                self._sse_peak = self._sse_active

    def stream_closed(self) -> None:
        """Count one SSE response that ended (never below zero)."""
        with self._lock:
            if self._sse_active > 0:
                self._sse_active -= 1

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """
        Return a consistent copy of the counters.

        Reading is side-effect free: only the clock is consulted (for
        ``uptime_s``), so a scrape never resets, rotates or increments a
        counter.
        """
        with self._lock:
            uptime = max(0.0, self._monotonic() - self._started_mono)
            return {
                "version": self._version,
                "started_at": _iso_utc(self._started_at),
                "uptime_s": round(uptime, 3),
                "requests": {
                    "total": self._total_requests,
                    "completed": self._completed_requests,
                    "by_status_class": dict(self._by_status),
                    "by_method": dict(self._by_method),
                    "rejections": {
                        "rate_limited": self._rate_limited,
                        "body_too_large": self._body_too_large,
                        "unsupported_media_type": self._unsupported_media_type,
                    },
                },
                "sse": {
                    "active": self._sse_active,
                    "peak": self._sse_peak,
                },
            }


def _iso_utc(timestamp: float) -> str:
    """Render a Unix timestamp as an RFC 3339 UTC string (``Z`` suffix)."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


class MetricsMiddleware:
    """
    Record every HTTP request and its final status.

    Installed *outside* the rate limiter and the body guard so their direct
    ``429``/``413``/``415`` answers are counted like any other response, and
    outside every layer that can answer by itself (the browser-origin gate,
    the ``/downloads`` token guard, the router's token dependency). A request
    that ends without a response head — a client disconnect, or an exception
    that reaches Starlette's outermost error layer — is recorded as ``500``,
    matching the access log.

    An SSE response is identified from its response head (``200`` +
    ``text/event-stream``); it increments the active gauge when the stream
    starts and decrements it when the ASGI call returns, which is exactly when
    the stream ended or the client disconnected. No request or response body
    is read or buffered.

    :param app: The wrapped ASGI application.
    :param metrics: The registry to update.
    """

    def __init__(self, app: ASGIApp, *, metrics: MetricsRegistry) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        self.metrics.request_started(method)
        status: int | None = None
        stream_opened = False

        async def send_with_metrics(message: Message) -> None:
            nonlocal status, stream_opened
            if message["type"] == "http.response.start":
                status = int(message.get("status", 0))
                if status == 200 and _is_event_stream(message):
                    stream_opened = True
                    self.metrics.stream_opened()
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            self.metrics.request_finished(500 if status is None else status)
            if stream_opened:
                self.metrics.stream_closed()


def _response_header(message: Message, name: bytes) -> str | None:
    """Return one raw response header value, decoded as Latin-1."""
    for raw_name, raw_value in message.get("headers") or ():
        if raw_name.lower() == name:
            return bytes(raw_value).decode("latin-1")
    return None


def _is_event_stream(message: Message) -> bool:
    """Whether this response head declares ``text/event-stream``."""
    content_type = _response_header(message, b"content-type")
    if content_type is None:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


def metrics_from_scope(scope: Scope) -> MetricsRegistry | None:
    """
    Return the registry attached to the application handling ``scope``.

    Used by middleware that produces a response on its own (the rate limiter,
    the body guard) to increment the specific rejection counter without being
    constructed with a registry. A hand-built ASGI scope in a unit test has no
    ``app`` entry, so the lookup is best-effort and returns ``None`` there.
    """
    app = scope.get("app")
    registry = getattr(getattr(app, "state", None), "metrics", None)
    return registry if isinstance(registry, MetricsRegistry) else None


def _counter_schema(description: str) -> dict[str, Any]:
    """JSON schema of one non-negative counter."""
    return {"type": "integer", "minimum": 0, "description": description}


def _fixed_counter_map_schema(
    keys: tuple[str, ...],
    description: str,
) -> dict[str, Any]:
    """JSON schema of an object whose keys are a fixed, documented set."""
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "required": list(keys),
        "properties": {key: {"type": "integer", "minimum": 0} for key in keys},
    }


#: OpenAPI schema of the ``GET /api/v1/metrics`` body. Kept next to the
#: registry so a new field cannot be returned without being documented;
#: ``tests/apps/test_api_metrics.py`` asserts the two agree.
METRICS_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "title": "MetricsResponse",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "started_at", "uptime_s", "requests", "sse"],
    "properties": {
        "version": {
            "type": "string",
            "description": "Version of the running server process.",
        },
        "started_at": {
            "type": "string",
            "format": "date-time",
            "description": (
                "Process start time as an RFC 3339 UTC timestamp. Restarting "
                "the server resets every counter and this value."
            ),
        },
        "uptime_s": {
            "type": "number",
            "minimum": 0,
            "description": (
                "Seconds since start, rounded to 3 decimals and measured on a "
                "monotonic clock (wall-clock changes cannot move it)."
            ),
        },
        "requests": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "total",
                "completed",
                "by_status_class",
                "by_method",
                "rejections",
            ],
            "properties": {
                "total": _counter_schema(
                    "Requests received by this process, including the metrics "
                    "probe itself and any request still in flight."
                ),
                "completed": _counter_schema(
                    "Requests whose response was fully handled; the status "
                    "counts below sum to this value."
                ),
                "by_status_class": _fixed_counter_map_schema(
                    STATUS_CLASSES,
                    "Completed responses by status class. 'other' collects "
                    "informational codes and anything outside 2xx-5xx.",
                ),
                "by_method": _fixed_counter_map_schema(
                    METHOD_BUCKETS,
                    "Received requests by HTTP method. Any method outside the "
                    "listed verbs is folded into 'other'.",
                ),
                "rejections": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "rate_limited",
                        "body_too_large",
                        "unsupported_media_type",
                    ],
                    "properties": {
                        "rate_limited": _counter_schema(
                            "Requests answered with the rate limiter's 429 "
                            "(a subset of the 4xx class)."
                        ),
                        "body_too_large": _counter_schema(
                            "Requests refused by the body-size guard with 413."
                        ),
                        "unsupported_media_type": _counter_schema(
                            "Requests refused by the body guard with 415 "
                            "(non-JSON Content-Type or compressed body)."
                        ),
                    },
                },
            },
        },
        "sse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["active", "peak"],
            "properties": {
                "active": _counter_schema(
                    "Server-Sent-Events responses streaming right now."
                ),
                "peak": _counter_schema(
                    "Highest simultaneous SSE stream count since start."
                ),
            },
        },
    },
}

__all__ = [
    "METRICS_CACHE_CONTROL",
    "METRICS_RESPONSE_SCHEMA",
    "METHOD_BUCKETS",
    "STATUS_CLASSES",
    "MetricsMiddleware",
    "MetricsRegistry",
    "method_bucket",
    "metrics_from_scope",
    "status_class",
]
