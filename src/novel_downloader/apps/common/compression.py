#!/usr/bin/env python3
"""
novel_downloader.apps.common.compression
----------------------------------------

Shared response-compression policy for both HTTP transports.

The module imports only the standard library: the embedded engine runs under
Chaquopy, where the FastAPI stack cannot be imported, yet both transports must
decide identically whether a response is worth compressing. The FastAPI
transport applies it in :class:`novel_downloader.apps.api.middleware.
CompressionMiddleware`; the embedded engine applies the same policy inline in
``_send_json``.

Policy
------
* Only ``Accept-Encoding: gzip`` is honoured. ``br``/``zstd`` are not
  implemented, so a client asking for them (or for ``identity``) gets the
  uncompressed representation; an unknown encoding is never selected by
  accident. ``q=0`` refuses gzip explicitly, and ``*`` accepts it.
* Only JSON media types (:func:`is_json_media_type`) are compressed. An SSE
  stream (``text/event-stream``) and the ``application/octet-stream`` files
  served from ``/downloads`` therefore always stay uncompressed: both are
  either incremental or already compressed/incompressible, and buffering them
  would defeat their purpose.
* A body smaller than :data:`COMPRESSION_MIN_BYTES` is not compressed:
  below that threshold gzip overhead (a ~20 byte header/trailer plus the
  ``Content-Encoding`` header) can exceed the saving.
* ``304 Not Modified`` and the other body-less statuses are never compressed;
  they carry no representation to compress.
"""

from __future__ import annotations

import zlib

__all__ = [
    "COMPRESSION_LEVEL",
    "COMPRESSION_MIN_BYTES",
    "accepts_gzip",
    "gzip_bytes",
    "is_compressible_status",
    "is_json_media_type",
]

#: Smallest body (in bytes) worth compressing: 1 KiB. Smaller JSON payloads
#: (health, errors, a single task) gain nothing once the gzip header/trailer
#: and the extra response header are accounted for.
COMPRESSION_MIN_BYTES = 1024

#: zlib/gzip compression level. ``6`` is the usual speed/size compromise for
#: a localhost service; deterministic for the same input (the gzip header
#: written by ``zlib`` leaves the MTIME field at zero).
COMPRESSION_LEVEL = 6

#: Statuses that never carry a compressible body. ``204``/``205`` are
#: body-less by definition and ``206`` is a partial representation whose
#: range semantics a re-encoded body would break.
_NON_COMPRESSIBLE_STATUSES = frozenset({204, 205, 206})


def is_compressible_status(status: int) -> bool:
    """
    Whether a response with ``status`` may carry a compressed body.

    :param status: HTTP status code of the response.
    :return: ``True`` for a ``2xx`` status other than ``204``/``205``/``206``.
    """
    return 200 <= status < 300 and status not in _NON_COMPRESSIBLE_STATUSES


def is_json_media_type(value: str | None) -> bool:
    """
    Whether a ``Content-Type`` value denotes a JSON media type.

    ``application/json`` and any structured ``application/*+json`` type are
    accepted; parameters such as ``charset=utf-8`` are ignored. Anything else
    — notably ``text/event-stream`` for SSE and ``application/octet-stream``
    for exported files — is not compressed.

    :param value: Raw ``Content-Type`` response header, if any.
    :return: ``True`` when the media type is JSON.
    """
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def accepts_gzip(value: str | None) -> bool:
    """
    Whether an ``Accept-Encoding`` value admits a gzip response.

    Parses the comma-separated coding list with its optional ``q`` weights:
    an explicit ``gzip`` entry wins over ``*`` (so ``gzip;q=0, *;q=1``
    refuses gzip), a bare ``gzip`` means ``q=1``, and ``*`` without an
    explicit ``gzip`` admits it. A missing, empty or unparseable value admits
    nothing, so identity is served.

    :param value: Raw ``Accept-Encoding`` request header, if any.
    :return: ``True`` when gzip may be sent.
    """
    if not value:
        return False

    gzip_q: float | None = None
    wildcard_q: float | None = None
    for member in value.split(","):
        parts = member.split(";")
        coding = parts[0].strip().lower()
        if not coding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, _, raw = parameter.partition("=")
            if name.strip().lower() != "q":
                continue
            try:
                quality = float(raw.strip())
            except ValueError:
                quality = 0.0
            quality = min(max(quality, 0.0), 1.0)
        if coding == "gzip":
            gzip_q = quality
        elif coding == "*":
            wildcard_q = quality

    if gzip_q is not None:
        return gzip_q > 0
    if wildcard_q is not None:
        return wildcard_q > 0
    return False


def gzip_bytes(data: bytes, level: int = COMPRESSION_LEVEL) -> bytes:
    """
    Return ``data`` as a complete gzip member.

    Uses ``zlib`` directly (rather than :func:`gzip.compress`) so the header
    is deterministic: ``zlib`` leaves the gzip MTIME field at zero, which
    makes the encoded bytes a pure function of the input and the level.

    :param data: Uncompressed body bytes.
    :param level: zlib compression level.
    :return: A complete gzip stream, trailer included.
    """
    compressor = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()
