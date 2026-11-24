#!/usr/bin/env python3
"""
novel_downloader.apps.common.cache_policy
------------------------------------------

Shared ``Cache-Control`` policy for both HTTP transports.

The module imports only the standard library: the embedded engine runs under
Chaquopy, where the FastAPI stack cannot be imported, yet both transports must
answer with the same caching directive for the same path and status. The
FastAPI transport applies it in :class:`novel_downloader.apps.api.middleware.
CacheControlMiddleware`; the embedded engine applies it in ``_send_json``.
Keeping the table here means a new rule (or a changed max-age) cannot land on
one transport only.

Policy
------
* Reference data — ``/api/v1/sites``, ``/api/v1/history`` and
  ``/api/v1/music/platforms`` — is ``private, max-age=60``: it changes slowly,
  and ``private`` keeps a shared cache from storing a per-user answer.
* Live state below ``/api/v1/tasks`` and everything else in the API tree is
  ``no-store``. The fallback is deliberately ``no-store`` rather than "no
  header": a newly added route that forgets to opt in must not become
  cacheable by accident.
* Every ``4xx``/``5xx`` response is ``no-store``, whatever its path: an error
  must never be replayed from a cache.
* ``/downloads/*`` files are ``public, max-age=3600`` on the REST transport:
  an export is written once and never rewritten. (The embedded engine serves
  its app-private export directory to native clients and leaves successful
  file responses without a directive; its ``/downloads`` errors still get
  ``no-store`` like every other error.)
* Paths outside :data:`CACHE_POLICY_PREFIXES` (``/docs``, ``/openapi.json``,
  ``/``, ...) get no directive at all.
* Matching is boundary-aware: ``/api/v1/tasks`` also covers
  ``/api/v1/tasks/{task_id}`` but not a look-alike such as
  ``/api/v1/tasks-extra``.

A ``Cache-Control`` already set by a route (the SSE streams' ``no-cache``) is
never overridden by either transport.
"""

from __future__ import annotations

__all__ = [
    "CACHE_CONTROL_NO_STORE",
    "CACHE_CONTROL_PRIVATE",
    "CACHE_CONTROL_PUBLIC",
    "CACHE_CONTROL_RULES",
    "CACHE_POLICY_PREFIXES",
    "cache_control_for",
    "governs_path",
    "rule_for_path",
]

#: ``Cache-Control`` for reference data the client may reuse briefly.
CACHE_CONTROL_PRIVATE = "private, max-age=60"

#: ``Cache-Control`` for exported files: written once and never rewritten.
CACHE_CONTROL_PUBLIC = "public, max-age=3600"

#: ``Cache-Control`` for live data and for every error response.
CACHE_CONTROL_NO_STORE = "no-store"

#: ``path prefix`` -> ``Cache-Control`` for successful responses, in match
#: order (the first boundary-aware match wins).
CACHE_CONTROL_RULES: tuple[tuple[str, str], ...] = (
    ("/api/v1/sites", CACHE_CONTROL_PRIVATE),
    ("/api/v1/history", CACHE_CONTROL_PRIVATE),
    ("/api/v1/music/platforms", CACHE_CONTROL_PRIVATE),
    ("/api/v1/tasks", CACHE_CONTROL_NO_STORE),
    ("/downloads", CACHE_CONTROL_PUBLIC),
)

#: Path prefixes the policy governs. Requests outside them pass through
#: without any header being added.
CACHE_POLICY_PREFIXES: tuple[str, ...] = ("/api/v1", "/downloads")


def _matches(cleaned: str, prefix: str) -> bool:
    """Whether ``cleaned`` is ``prefix`` itself or below it."""
    return cleaned == prefix or cleaned.startswith(prefix + "/")


def _normalize(path: str) -> str:
    """Strip a trailing slash so ``/api/v1/tasks/`` matches ``/api/v1/tasks``."""
    return path.rstrip("/") or "/"


def governs_path(path: str) -> bool:
    """Whether the cache policy applies to ``path`` at all."""
    cleaned = _normalize(path)
    return any(_matches(cleaned, prefix) for prefix in CACHE_POLICY_PREFIXES)


def rule_for_path(path: str) -> str | None:
    """Return the configured success directive for ``path``, if any."""
    cleaned = _normalize(path)
    for prefix, value in CACHE_CONTROL_RULES:
        if _matches(cleaned, prefix):
            return value
    return None


def cache_control_for(path: str, status: int) -> str | None:
    """
    Return the ``Cache-Control`` a response should carry, or ``None``.

    ``None`` means "no directive": the path is outside
    :data:`CACHE_POLICY_PREFIXES`, so neither transport adds a header.

    :param path: Request path (query string already removed).
    :param status: Response status code.
    """
    cleaned = _normalize(path)
    if not governs_path(cleaned):
        return None
    if status >= 400:
        return CACHE_CONTROL_NO_STORE
    rule = rule_for_path(cleaned)
    return CACHE_CONTROL_NO_STORE if rule is None else rule
