"""公开曲目序列化。

:class:`~novel_downloader.apps.music.providers.base.Track` 有一个 ``extra``
字段，存的是 provider 私有对象（musicdl 的 ``SongInfo``、网易云原始响应、
带下载 cookie 的请求数据等）。这些对象既不是 JSON 可序列化的，也**不该
发给客户端**：REST/内嵌引擎过去直接用 ``dataclasses.asdict(track)`` 回包，
把整份私有数据一起吐了出去。

本模块提供唯一的公开序列化入口 :func:`track_dict`：只输出界面需要的字段，
``extra`` 永远不出现。下载仍靠 :mod:`~novel_downloader.apps.music.trackcache`
把原始对象还给引擎，所以去掉 ``extra`` 不影响 ``search_id`` 流程。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "PUBLIC_TRACK_FIELDS",
    "REASON_MAX_CHARS",
    "redact_credentials",
    "redact_headers",
    "sanitize_reason",
    "strip_url_query",
    "track_dict",
]

#: 响应里允许出现的曲目字段，顺序与 :class:`Track` 的 dataclass 字段一致。
#: ``extra`` 故意不在其中（provider 私有数据，见模块文档）。
PUBLIC_TRACK_FIELDS: tuple[str, ...] = (
    "provider",
    "id",
    "name",
    "artists",
    "album",
    "duration_s",
    "cover_url",
    "track_no",
    "ext",
    "vip",
    "download_url",
    "http_headers",
    "path",
    "lyric",
    "sources",
)

_STR_FIELDS = (
    "provider",
    "id",
    "name",
    "artists",
    "album",
    "cover_url",
    "ext",
    "download_url",
    "path",
    "lyric",
)
_INT_FIELDS = ("duration_s", "track_no")

#: Integers on the wire are durations/track numbers. A provider that returns
#: a 10**100 ``duration_s`` would otherwise put that huge number into every
#: JSON response (and some clients cannot parse it).
_INT_MAX = 2**31 - 1


def _field(track: object, name: str, default: Any) -> Any:
    """Read a public field, tolerating provider objects with broken properties."""
    try:
        return getattr(track, name, default)
    except Exception:  # noqa: BLE001 - one bad track must not break the response
        return default


def _as_str(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - __str__ may raise on provider objects
        return ""


def track_dict(track: object) -> dict[str, Any]:
    """
    Build the JSON-safe public representation of one track.

    Works on the real :class:`Track` and on provider/test doubles: every field
    is read with :func:`getattr` and coerced to its wire type, so a missing
    attribute yields the documented default instead of raising. Process-local
    attributes such as ``_alternatives`` are never read.

    :param track: any object exposing the public track attributes.
    :return: ``dict`` with exactly the keys in :data:`PUBLIC_TRACK_FIELDS`.
    """
    payload: dict[str, Any] = {}
    for name in _STR_FIELDS:
        payload[name] = _as_str(_field(track, name, ""))
    for name in _INT_FIELDS:
        payload[name] = _as_int(_field(track, name, 0))

    vip = _field(track, "vip", False)
    if isinstance(vip, str):
        vip = vip.strip().lower() in {"1", "true", "yes", "on"}
    payload["vip"] = bool(vip)

    headers = _field(track, "http_headers", {}) or {}
    if isinstance(headers, Mapping):
        payload["http_headers"] = redact_headers(headers)
    else:
        payload["http_headers"] = {}

    sources = _field(track, "sources", []) or []
    if isinstance(sources, (list, tuple, set, frozenset)):
        payload["sources"] = [_as_str(item) for item in sources]
    elif isinstance(sources, str):
        payload["sources"] = [sources] if sources else []
    else:
        payload["sources"] = []

    # Preserve the documented field order regardless of how the loops above
    # filled the mapping (``vip``/``http_headers``/``sources`` interleave).
    return {name: payload[name] for name in PUBLIC_TRACK_FIELDS}


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, min(value, _INT_MAX))
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        try:
            return max(0, min(int(value), _INT_MAX))
        except (OverflowError, ValueError):
            return 0
    if isinstance(value, str):
        try:
            return max(0, min(int(value.strip() or 0), _INT_MAX))
        except ValueError:
            return 0
    return 0


_URL_RE = re.compile(r"(?:https?|ftp)://[^\s'\"<>`]+", re.IGNORECASE)

_REDACTED = "***"

#: Cookies/credentials specific to the music platforms.  ``MUSIC_U`` is the
#: NetEase login credential; the others are values the UI stores in
#: ``platform_creds.json`` (Deezer ``arl``, QQ cookie jar, MOOV device id).
#: The shared logger redactor knows their ``name=value`` form; this pattern
#: additionally covers mapping reprs (``{'MUSIC_U': '...'}``) and the
#: ``__csrf``/``MUSIC_A`` names used only by the NetEase client.
_MUSIC_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![\w.\-])"
    r"(?P<name>[\"']?(?:MUSIC_[AU]|__csrf|__remember_me|NMTID|arl|qm_keyst|"
    r"qqmusic_key|p_skey|skey|MOOVUUID)[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&)\]}\"']+)"
)

#: ``cookies={...}`` / ``default_download_cookies={'MUSIC_U': '...'}``.
#: The jar is masked whole: its key names vary per platform and the exact
#: names are not something a log reader needs.
_COOKIE_MAP_RE = re.compile(
    r"(?i)\b(?P<name>(?:[a-z0-9]+_)*cookies)"
    r"\s*[:=]\s*\{[^{}\r\n]*\}"
)

#: Headers whose value *is* a credential, regardless of the platform.
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
    }
)

_SENSITIVE_HEADER_SUFFIX_RE = re.compile(
    r"(?i)(?:^|[-_])(?:authorization|cookie|token|secret|password|"
    r"api[-_]?key|signature)$"
)


def _redacted_cookie_map(match: re.Match[str]) -> str:
    return f"{match.group('name')}={_REDACTED}"


def _redacted_assignment(match: re.Match[str]) -> str:
    return f"{match.group('name')}{match.group('sep')}{_REDACTED}"


def redact_credentials(text: object) -> str:
    """
    Mask credentials in *text* using the shared logger rules plus the
    music-specific cookie/mapping forms.

    Safe to call on any value: it never raises.  The result keeps the shape
    of the original message (platform name, HTTP status, host/path) so a
    failure stays diagnosable.

    :param text: Any value; non-strings are converted with ``str()``.
    :return: The redacted text.
    """
    from novel_downloader.infra.logger import redact

    value = _as_str(text)
    value = _COOKIE_MAP_RE.sub(_redacted_cookie_map, value)
    value = _MUSIC_CREDENTIAL_RE.sub(_redacted_assignment, value)
    # Generic URL userinfo / Authorization / Bearer / query / name=value rules.
    return redact(value)


def _is_sensitive_header(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered in _SENSITIVE_HEADER_NAMES:
        return True
    return bool(_SENSITIVE_HEADER_SUFFIX_RE.search(lowered))


def redact_headers(headers: Mapping[Any, Any]) -> dict[str, str]:
    """
    Return a copy of *headers* with credential values replaced by ``***``.

    ``Track.http_headers`` is part of the public wire format; some vendored
    providers put their session ``Cookie`` or ``Authorization`` header there
    for the downloader.  Those values are useful to the server-side download
    but must never be echoed to a CLI/API client.

    :param headers: Header-name -> value mapping (values are stringified).
    :return: A new ``str -> str`` mapping in the original order.
    """
    out: dict[str, str] = {}
    for key, value in headers.items():
        name = _as_str(key)
        if _is_sensitive_header(name):
            out[name] = _REDACTED
        else:
            out[name] = redact_credentials(value)
    return out


def strip_url_query(text: str) -> str:
    """
    Drop query/fragment parts from URLs embedded in a message.

    Provider errors frequently quote the failing request URL, and those URLs
    can carry signed tokens (``?token=...&expires=...``).  The redacted text is
    what reaches API/embedded clients; any original exception still goes to
    the log untouched by the caller.
    """
    from urllib.parse import urlsplit, urlunsplit

    def _redact(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            if not (parts.query or parts.fragment):
                return raw
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:  # pragma: no cover - urlsplit is very forgiving
            return raw

    return _URL_RE.sub(_redact, text)


#: Maximum length of a per-track failure reason sent to API/embedded clients.
#: Matches the 200-character ceiling the aggregate searcher applies to
#: ``platforms[].error`` and the health probe applies to its ``error`` field:
#: provider exceptions can embed whole HTML bodies or multi-line tracebacks,
#: and an unbounded blob bloats every job poll for no diagnostic value.
REASON_MAX_CHARS = 200


def sanitize_reason(text: object, *, limit: int = REASON_MAX_CHARS) -> str:
    """
    Make a provider failure reason safe and bounded for the wire.

    Three transforms, applied in this order:

    1. drop URL query/fragment parts (they can carry signed tokens);
    2. redact any remaining credentials (userinfo, headers, cookies, the
       music-specific cookie names) with :func:`redact_credentials`;
    3. collapse every whitespace run (newlines, tabs, repeated spaces) into a
       single space, so a multi-line provider message becomes one readable
       line instead of breaking table/row rendering in clients;
    4. truncate to *limit* characters.

    Stripping happens before truncation on purpose: cutting a URL first could
    leave most of a token in the prefix.  The same rule is used for
    ``platforms[].error`` (search) and the health probe's ``error``, so every
    music error string crossing the wire shares one ceiling.
    """
    value = redact_credentials(strip_url_query(_as_str(text)))
    value = " ".join(value.split())
    if limit > 0 and len(value) > limit:
        value = value[:limit]
    return value
