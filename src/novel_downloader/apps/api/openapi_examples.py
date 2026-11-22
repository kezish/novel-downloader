#!/usr/bin/env python3
"""
novel_downloader.apps.api.openapi_examples
------------------------------------------

Request/response examples for the generated OpenAPI document.

The examples live here instead of in the route decorators so a single import
feeds both the published contract (``app.py`` installs them into
``/openapi.json``) and ``tests/apps/test_openapi_examples.py``, which validates
every value against the schema it is attached to. Nothing in this module is
read at request time: it is documentation metadata only.

Error values are built from the constants the handlers actually use
(:data:`AUTH_ERROR_DETAIL`, ``MSG_*`` and the body-limit detail), so the
published example cannot drift away from the real message. Credentials are
only ever shown as ``<token>``-style placeholders.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from novel_downloader.apps.api.auth import AUTH_ERROR_DETAIL
from novel_downloader.apps.api.constants import (
    LABEL_MUSIC_DOWNLOAD,
    MSG_BROWSE_UNSUPPORTED,
    MSG_MUSIC_UNAVAILABLE,
    MSG_TIMED_OUT,
    MSG_UNKNOWN_MUSIC_PLATFORM,
)
from novel_downloader.apps.api.middleware import (
    DEFAULT_MAX_BODY_BYTES,
    UNSUPPORTED_CONTENT_TYPE_DETAIL,
)
from novel_downloader.apps.api.routes import (
    BLOCKING_TIMEOUTS,
    MAX_ACTIVE_MUSIC_JOBS,
    MSG_MUSIC_JOBS_BUSY,
)
from novel_downloader.apps.music.export_path import (
    MSG_PLAYLIST_TARGET_INVALID,
    MSG_PLAYLIST_WRITE_FAILED,
)

#: ``servers`` entries for the generated document. Every documented path
#: already starts with ``/api/v1``, so the server URL deliberately carries no
#: path: OpenAPI prefixes the base URL to the operation path, and a
#: ``.../api/v1`` base would produce ``/api/v1/api/v1/...`` in generated
#: clients. ``novel-api`` binds ``127.0.0.1:8081`` by default.
OPENAPI_SERVERS: list[dict[str, Any]] = [
    {
        "url": "http://127.0.0.1:8081",
        "description": (
            "Default local server (``novel-api`` binds 127.0.0.1:8081 and the "
            "documented operation paths already include the ``/api/v1`` base "
            "path). Change the host/port with ``--host``/``--port``; the "
            "returned ``/openapi.json`` documents the values actually served."
        ),
    }
]

#: Request-body examples keyed by ``(path, method)``. Each inner entry is an
#: OpenAPI ``examples`` object (``name -> {"summary", "value"}``).
REQUEST_EXAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/v1/search", "post"): {
        "single_site": {
            "summary": "Single-site query",
            "value": {
                "keyword": "斗破苍穹 天蚕土豆",
                "sites": ["qidian"],
                "per_site_limit": 5,
                "timeout": 5.0,
                "sort": "quality",
                "order": "desc",
                "limit": 20,
                "group": False,
            },
        },
        "aggregate": {
            "summary": "Aggregate every site, merge rows and filter by words",
            "value": {
                "keyword": "斗破苍穹",
                "sites": None,
                "per_site_limit": 10,
                "timeout": 8.0,
                "sort": "quality",
                "order": "desc",
                "limit": 50,
                "group": True,
                "min_words": 300000,
                "updated_after": "2024-01-01",
            },
        },
    },
    ("/api/v1/tasks", "post"): {
        "queued_task": {
            "summary": "Queue a download",
            "value": {
                "title": "斗破苍穹",
                "site": "qidian",
                "book_id": "1234567890",
            },
        },
    },
    ("/api/v1/music/search", "post"): {
        "single_platform": {
            "summary": "Search one platform (offset paging is supported here)",
            "value": {
                "platform": "netease",
                "keyword": "夜曲",
                "limit": 20,
                "offset": 0,
            },
        },
        "all_platforms": {
            "summary": "Aggregate search across every registered platform",
            "value": {"platform": "all", "keyword": "夜曲", "limit": 20},
        },
    },
}

#: One search row as returned by the ranked search pipeline: the
#: :class:`novel_downloader.schemas.search.SearchResult` fields plus the
#: derived ``rank_score`` / ``match_tier`` / ``query_intent`` annotations.
_SEARCH_ROW: dict[str, Any] = {
    "site": "qidian",
    "book_id": "1234567890",
    "book_url": "https://www.qidian.com/book/1234567890/",
    "cover_url": "https://example.com/covers/1234567890.jpg",
    "title": "斗破苍穹",
    "author": "天蚕土豆",
    "latest_chapter": "第1648章 大结局",
    "update_date": "2024-05-01",
    "word_count": "5.3百万",
    "priority": 1,
    "rank_score": 0.98,
    "match_tier": 4,
    "query_intent": "title",
}

#: One public music track (:func:`novel_downloader.apps.music.serialize.
#: track_dict`): exactly :data:`PUBLIC_TRACK_FIELDS`, never provider-private
#: ``extra`` data.
_MUSIC_TRACK: dict[str, Any] = {
    "provider": "netease",
    "id": "186016",
    "name": "夜曲",
    "artists": "周杰伦",
    "album": "十一月的萧邦",
    "duration_s": 226,
    "cover_url": "https://example.com/covers/186016.jpg",
    "track_no": 1,
    "ext": "mp3",
    "vip": False,
    "download_url": "",
    "http_headers": {},
    "path": "",
    "lyric": "",
    "sources": [],
}

#: One exported file entry as returned by ``GET /api/v1/history``.
_HISTORY_FILE: dict[str, Any] = {
    "name": "斗破苍穹.txt",
    "ext": "txt",
    "size": 1048576,
    "mtime": 1717000000.0,
    "download_url": f"/downloads/{quote('斗破苍穹.txt', safe='')}",
}

#: One task as returned by ``POST /api/v1/tasks`` (see ``task_to_dict``).
_TASK: dict[str, Any] = {
    "task_id": "3f2a9d4c1b6e4f8a9c0d1e2f3a4b5c6d",
    "title": "斗破苍穹",
    "site": "qidian",
    "book_id": "1234567890",
    "status": "queued",
    "chapters_done": 0,
    "chapters_total": 0,
    "progress": 0.0,
    "error": None,
    "exported_paths": {},
    "created_at": 1717000000.0,
    "finished_at": None,
    "export_stage": None,
}

#: Success-response examples keyed by ``(path, method, status)``.
RESPONSE_EXAMPLES: dict[tuple[str, str, str], dict[str, Any]] = {
    ("/api/v1/search", "post", "200"): {
        "single_site": {
            "summary": "One ranked row from the selected site",
            "value": {
                "total": 1,
                "results": [dict(_SEARCH_ROW)],
                "query_intent": "title",
            },
        },
        "aggregate": {
            "summary": "Merged row carrying every site that has the book",
            "value": {
                "total": 1,
                "results": [
                    {
                        **_SEARCH_ROW,
                        "sources": ["qidian", "biquge5"],
                    }
                ],
                "query_intent": "title",
            },
        },
    },
    ("/api/v1/tasks", "post", "200"): {
        "queued_task": {
            "summary": "Task queued for download",
            "value": dict(_TASK),
        },
    },
    ("/api/v1/music/search", "post", "200"): {
        "single_platform": {
            "summary": "Single-platform result page",
            "value": {
                "total": 1,
                "tracks": [{**_MUSIC_TRACK, "sources": ["netease"]}],
                "search_id": "0f3c9a1b2d4e4f6a",
                "platform": "netease",
                "offset": 0,
                "supports_offset": True,
            },
        },
        "all_platforms": {
            "summary": "Aggregate result with per-platform outcomes",
            "value": {
                "total": 1,
                "tracks": [
                    {
                        **_MUSIC_TRACK,
                        "sources": ["netease", "qqmusic"],
                    }
                ],
                "search_id": "6b1d0e8c7a5f4b2d",
                "platform": "all",
                "platforms": [
                    {"platform": "netease", "ok": True, "count": 1, "error": ""},
                    {
                        "platform": "qqmusic",
                        "ok": False,
                        "count": 0,
                        "error": "搜索超时（>5s）",
                    },
                ],
                "offset": 0,
                "supports_offset": False,
            },
        },
    },
    ("/api/v1/music/health", "get", "200"): {
        "mixed": {
            "summary": "One entry per probed platform",
            "value": [
                {"platform": "netease", "ok": True, "elapsed_ms": 412, "error": ""},
                {
                    "platform": "qqmusic",
                    "ok": False,
                    "elapsed_ms": 5000,
                    "error": "搜索超时（>5s）",
                },
            ],
        },
    },
    ("/api/v1/history", "get", "200"): {
        "page": {
            "summary": "Newest-first page of exported files",
            "value": {
                "total": 1,
                "offset": 0,
                "limit": 50,
                "files": [dict(_HISTORY_FILE)],
            },
        },
    },
    ("/api/v1/browse/categories", "get", "400"): {
        "unknown_site": {
            "summary": "The site is unknown or does not publish categories",
            "value": {"detail": MSG_BROWSE_UNSUPPORTED.format(site="unknown")},
        },
    },
    ("/api/v1/browse", "post", "400"): {
        "unknown_site": {
            "summary": "The site is unknown or does not publish rankings",
            "value": {"detail": MSG_BROWSE_UNSUPPORTED.format(site="unknown")},
        },
    },
    ("/api/v1/browse/categories", "get", "504"): {
        "timeout": {
            "summary": "Browse budget exceeded",
            "value": {
                "detail": MSG_TIMED_OUT.format(
                    label="Browse", timeout=BLOCKING_TIMEOUTS["browse"]
                )
            },
        },
    },
    ("/api/v1/browse", "post", "504"): {
        "timeout": {
            "summary": "Browse budget exceeded",
            "value": {
                "detail": MSG_TIMED_OUT.format(
                    label="Browse", timeout=BLOCKING_TIMEOUTS["browse"]
                )
            },
        },
    },
    ("/api/v1/search", "post", "504"): {
        "timeout": {
            "summary": "Aggregate search deadline exceeded",
            "value": {
                "detail": MSG_TIMED_OUT.format(
                    label="Search", timeout=BLOCKING_TIMEOUTS["search"]
                )
            },
        },
    },
    ("/api/v1/music/export", "post", "400"): {
        "invalid_target": {
            "summary": "Playlist target outside the music output directory",
            "value": {"detail": MSG_PLAYLIST_TARGET_INVALID},
        },
    },
    ("/api/v1/music/export", "post", "500"): {
        "write_failed": {
            "summary": "Playlist could not be written",
            "value": {"detail": MSG_PLAYLIST_WRITE_FAILED.format(error="OSError")},
        },
    },
    ("/api/v1/music/health", "get", "400"): {
        "unknown_platform": {
            "summary": "Platform key is not registered",
            "value": {
                "detail": MSG_UNKNOWN_MUSIC_PLATFORM.format(
                    platform="unknown", keys="netease, qqmusic"
                )
            },
        },
    },
    ("/api/v1/music/search", "post", "400"): {
        "unknown_platform": {
            "summary": "Platform key is not registered",
            "value": {
                "detail": MSG_UNKNOWN_MUSIC_PLATFORM.format(
                    platform="unknown", keys="netease, qqmusic"
                )
            },
        },
    },
    ("/api/v1/music/search", "post", "504"): {
        "timeout": {
            "summary": "Music search budget exceeded",
            "value": {
                "detail": MSG_TIMED_OUT.format(
                    label="Music search", timeout=BLOCKING_TIMEOUTS["music_search"]
                )
            },
        },
    },
    ("/api/v1/music/download", "post", "400"): {
        "unknown_platform": {
            "summary": "Platform key is not registered",
            "value": {
                "detail": MSG_UNKNOWN_MUSIC_PLATFORM.format(
                    platform="unknown", keys="netease, qqmusic"
                )
            },
        },
    },
    ("/api/v1/music/download", "post", "504"): {
        "timeout": {
            "summary": "Music download budget exceeded",
            "value": {
                "detail": MSG_TIMED_OUT.format(
                    label=LABEL_MUSIC_DOWNLOAD,
                    timeout=BLOCKING_TIMEOUTS["music_download"],
                )
            },
        },
    },
    ("/api/v1/music/jobs", "post", "400"): {
        "unknown_platform": {
            "summary": "Platform key is not registered",
            "value": {
                "detail": MSG_UNKNOWN_MUSIC_PLATFORM.format(
                    platform="unknown", keys="netease, qqmusic"
                )
            },
        },
    },
    ("/api/v1/music/jobs", "post", "429"): {
        "music_jobs_busy": {
            "summary": "All music worker slots are occupied",
            "value": {
                "detail": MSG_MUSIC_JOBS_BUSY.format(limit=MAX_ACTIVE_MUSIC_JOBS)
            },
        },
    },
}

#: Generic error-body examples keyed by status. Applied to every documented
#: response with that status (and its ``application/json`` media type), so the
#: same ``{"detail": ...}`` contract is shown everywhere.
ERROR_EXAMPLES: dict[str, dict[str, Any]] = {
    "401": {
        "summary": "Token configured, none supplied",
        "value": {"detail": AUTH_ERROR_DETAIL},
    },
    "413": {
        "summary": "Body above the 1 MiB ceiling",
        "value": {
            "detail": (
                "Request body too large: the limit for this endpoint is "
                f"{DEFAULT_MAX_BODY_BYTES} bytes"
            )
        },
    },
    "415": {
        "summary": "Body is not JSON",
        "value": {"detail": UNSUPPORTED_CONTENT_TYPE_DETAIL},
    },
    "422": {
        "summary": "Request validation failed",
        "value": {
            "detail": (
                "Request validation failed: per_site_limit: Input should be "
                "greater than or equal to 1"
            ),
            "errors": [
                {
                    "type": "greater_than_equal",
                    "loc": ["body", "per_site_limit"],
                    "msg": "Input should be greater than or equal to 1",
                }
            ],
        },
    },
    "429": {
        "summary": "Rate limit exceeded",
        "value": {"detail": "Rate limit exceeded. Try again in 42s."},
    },
    "500": {
        "summary": "Unhandled server error (no stack trace in the body)",
        "value": {"detail": "Internal server error"},
    },
    "503": {
        "summary": "Optional music extras are not installed",
        "value": {"detail": MSG_MUSIC_UNAVAILABLE.format(error="ModuleNotFoundError")},
    },
}

#: Extra error statuses documented per operation, with the status-specific
#: description installed on the response.
EXTRA_ERROR_RESPONSES: dict[tuple[str, str], dict[int, str]] = {
    ("/api/v1/search", "post"): {
        504: (
            "The aggregate search exceeded its overall deadline; retry with a "
            "smaller site selection or a shorter per-site timeout."
        )
    },
    ("/api/v1/browse/categories", "get"): {
        400: "The site is unknown or does not publish browse categories.",
        504: "The site did not answer within the browse budget.",
    },
    ("/api/v1/browse", "post"): {
        400: "The site is unknown or does not publish rankings/categories.",
        504: "The site did not answer within the browse budget.",
    },
    ("/api/v1/music/platforms", "get"): {
        503: "The optional music extras are not installed.",
    },
    ("/api/v1/music/export", "post"): {
        400: "The playlist target is outside the allowed export roots.",
        503: "The optional music extras are not installed.",
    },
    ("/api/v1/music/health", "get"): {
        400: "The requested platform key is not registered.",
        503: "The optional music extras are not installed.",
    },
    ("/api/v1/music/search", "post"): {
        400: "The requested platform key is not registered.",
        503: "The optional music extras are not installed.",
        504: "The provider search exceeded the music search budget.",
    },
    ("/api/v1/music/download", "post"): {
        400: "The requested platform key is not registered.",
        503: "The optional music extras are not installed.",
        504: "The download exceeded the music download budget.",
    },
    ("/api/v1/music/jobs", "post"): {
        400: "The requested platform key is not registered.",
        503: "The optional music extras are not installed.",
    },
}

#: Description installed on the error statuses added to every operation.
GLOBAL_ERROR_DESCRIPTIONS: dict[int, str] = {
    413: (
        "The request body exceeds the configured ceiling and was rejected "
        "before parsing."
    ),
    415: ("The request body is not JSON or declares a non-identity Content-Encoding."),
    500: "Unexpected server error; the body never carries a stack trace.",
}

#: Example texts are built from the same constants as the runtime responses;
#: the export is kept explicit so the test module can pin it.
__all__ = [
    "ERROR_EXAMPLES",
    "EXTRA_ERROR_RESPONSES",
    "GLOBAL_ERROR_DESCRIPTIONS",
    "OPENAPI_SERVERS",
    "REQUEST_EXAMPLES",
    "RESPONSE_EXAMPLES",
]
