#!/usr/bin/env python3
"""
novel_downloader.apps.common.conditional_get
--------------------------------------------

Validation helpers for HTTP conditional requests (``ETag`` /
``If-None-Match``), shared by the FastAPI transport and the stdlib-only
embedded engine.

The module deliberately imports nothing but the standard library: the
embedded server runs under Chaquopy, where importing the FastAPI stack is not
possible, yet both transports must answer a revalidation identically.

Policy
------
* Only the reference endpoints below opt in. They are idempotent ``GET``\\ s
  whose payload is derived from slowly changing state and carries no secrets.
  Live or credential-bearing endpoints (``/tasks*``, ``/login*``,
  ``/music/jobs*``, the watchlist, ...) stay uncacheable and never see an
  ``ETag``.
* The validator is a **strong** entity tag: the quoted first 16 bytes
  (32 hex characters) of the SHA-256 of the exact response body. The body is
  the representation — a byte-identical body means a byte-identical
  representation — so a strong tag is correct here; a weak tag would only be
  needed if semantically equivalent responses could differ byte for byte
  (they cannot: the JSON serialisation is deterministic for the same request).
* ``If-None-Match`` is evaluated with the RFC 9110 weak comparison: the
  ``W/`` prefix is ignored, so both ``"<tag>"`` and ``W/"<tag>"`` match, a
  comma-separated list matches when any member does, and ``*`` matches any
  current representation.
* Responses larger than :data:`ETAG_MAX_BODY_BYTES` are not hashed: hashing
  and buffering an unbounded body would cost more than the bandwidth a 304
  saves, so such a response is streamed as before, only without an ``ETag``.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "ETAG_ENDPOINTS",
    "ETAG_MAX_BODY_BYTES",
    "if_none_match_matches",
    "make_strong_etag",
]

#: Absolute paths of the idempotent ``GET`` endpoints that take part in
#: conditional requests. They match the ``private, max-age=60`` reference
#: endpoints of the REST cache policy, and the embedded engine enables the
#: same set.
ETAG_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/api/v1/sites",
        "/api/v1/history",
        "/api/v1/music/platforms",
    }
)

#: Upper bound (in bytes) for a response body that earns an ``ETag``. The
#: reference payloads are a few KiB; a response above this limit is rare
#: (thousands of exported files) and is streamed without a validator rather
#: than buffered and hashed.
ETAG_MAX_BODY_BYTES = 512 * 1024

#: One entity tag from an ``If-None-Match`` member: optional ``W/`` prefix
#: followed by a quoted opaque tag. Values that do not match are ignored, so
#: a malformed header never causes a false ``304``.
_ENTITY_TAG = re.compile(r'(?:W/)?"(?P<opaque>[^"]*)"\Z')


def make_strong_etag(body: bytes) -> str:
    """
    Return the strong entity tag for ``body``.

    The tag is the first 16 bytes of the SHA-256 digest, hex-encoded and
    quoted (``"<32 hex chars>"``). 128 bits of a cryptographic digest make an
    accidental collision between two representations negligible, and the
    quoted form is what the HTTP grammar requires.

    :param body: The exact response body bytes that will be served.
    :return: A strong entity tag, e.g. ``'"3f2a..."'``.
    """
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def _opaque_tag(token: str) -> str | None:
    """Return the quoted opaque part of an entity tag, or ``None``."""
    match = _ENTITY_TAG.fullmatch(token.strip())
    return match.group("opaque") if match else None


def if_none_match_matches(header: str | None, etag: str) -> bool:
    """
    Whether an ``If-None-Match`` value matches the current entity tag.

    Implements the RFC 9110 weak comparison used for ``If-None-Match``: the
    weak indicator is ignored, so ``W/"x"`` and ``"x"`` are equivalent.
    ``*`` matches any current representation, a comma-separated list matches
    when any member matches, and unparseable members are ignored. An absent or
    empty header never matches.

    :param header: Raw ``If-None-Match`` request header, if any.
    :param etag: The entity tag the server would send on a ``200``.
    :return: ``True`` when a ``304 Not Modified`` should be returned.
    """
    if not header:
        return False
    current = _opaque_tag(etag)
    if current is None:  # pragma: no cover - callers only pass make_strong_etag
        return False
    for member in header.split(","):
        candidate = member.strip()
        if candidate == "*":
            return True
        if _opaque_tag(candidate) == current:
            return True
    return False
