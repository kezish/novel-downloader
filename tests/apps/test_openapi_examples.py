#!/usr/bin/env python3
"""
tests.apps.test_openapi_examples
--------------------------------

Schema gate for the request/response examples published in ``/openapi.json``.

The examples are hand-written (in
:mod:`novel_downloader.apps.api.openapi_examples`), so this module keeps them
honest:

* every request-body example validates against the schema it is attached to;
* every response example validates against its documented response schema;
* every example is an OpenAPI ``examples`` entry with a summary and a value;
* request examples only use field names the schema declares;
* response examples only use field names the serialisers actually emit
  (``task_to_dict``, the history endpoint, ``PUBLIC_TRACK_FIELDS`` and
  ``SearchResult``);
* the required statuses (400/401/413/415/422/429/500/503/504) and the 304
  contract are documented, the 304 response stays bodyless and carries
  ``ETag``;
* ``servers`` names the real local default (``--host``/``--port``);
* no example leaks a credential.

``jsonschema`` is not a project dependency, so the small JSON-Schema subset the
document actually uses (``$ref``, ``anyOf``/``oneOf``, ``type``, ``enum``,
``const``, numeric bounds, string bounds/pattern, arrays and objects) is
implemented here. The checks are offline: the document is generated in-process
and the one HTTP call uses ``httpx.ASGITransport``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import main as api_main
from novel_downloader.apps.api.routes import task_to_dict
from novel_downloader.apps.common import history_files
from novel_downloader.apps.common.conditional_get import ETAG_ENDPOINTS
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS
from novel_downloader.schemas.search import SearchResult

#: Methods that may appear in an OpenAPI path item.
HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)

#: Error statuses the contract must illustrate.
REQUIRED_ERROR_EXAMPLES = frozenset(
    {"400", "401", "413", "415", "422", "429", "500", "503", "504"}
)

#: Derived fields the ranking pipeline adds to every final search row, plus
#: ``sources`` on grouped rows (see ``novel_downloader.plugins.search``).
SEARCH_DERIVED_FIELDS = frozenset(
    {"rank_score", "match_tier", "query_intent", "sources"}
)

#: Credential shapes that must never appear in an example value.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+(?!<)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r'(?i)"(?:password|passwd|secret|token|api[_-]?key|cookie)"\s*:\s*"'
        r"(?!<)[^\"]{8,}\""
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


# ---------------------------------------------------------------------------
# Fixtures and document helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/state outside the real user profile and drop stray tokens."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def app(tmp_path: Any) -> FastAPI:
    """Build the API app without touching the real download directory."""
    return create_app(output_dir=tmp_path / "downloads")


@pytest.fixture
def spec(app: FastAPI) -> dict[str, Any]:
    """Return the generated OpenAPI document."""
    return app.openapi()


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(path, method, operation)`` for every documented operation."""
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                yield path, method, operation


def _resolve_ref(spec: dict[str, Any], ref: str) -> Any:
    """Resolve a local ``#/...`` JSON pointer reference."""
    assert ref.startswith("#/"), ref
    node: Any = spec
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        assert isinstance(node, dict) and part in node, f"unresolvable ref {ref}"
        node = node[part]
    return node


def _request_examples(
    spec: dict[str, Any],
) -> Iterator[tuple[str, str, str, dict[str, Any], dict[str, Any]]]:
    """Yield ``(path, method, name, entry, schema)`` for request examples."""
    for path, method, operation in _operations(spec):
        body = operation.get("requestBody")
        if not isinstance(body, dict):
            continue
        media = body.get("content", {}).get("application/json")
        if not isinstance(media, dict):
            continue
        entries = media.get("examples")
        if not isinstance(entries, dict):
            continue
        schema = media.get("schema")
        assert isinstance(schema, dict) and schema, f"{method.upper()} {path}"
        for name, entry in entries.items():
            yield path, method, name, entry, schema


def _response_examples(
    spec: dict[str, Any],
) -> Iterator[tuple[str, str, str, str, dict[str, Any], dict[str, Any]]]:
    """Yield ``(path, method, code, name, entry, schema)`` for response examples."""
    for path, method, operation in _operations(spec):
        for code, response in operation.get("responses", {}).items():
            if not isinstance(response, dict):
                continue
            media = response.get("content", {}).get("application/json")
            if not isinstance(media, dict):
                continue
            entries = media.get("examples")
            if not isinstance(entries, dict):
                continue
            schema = media.get("schema")
            assert isinstance(schema, dict) and schema, (
                f"{method.upper()} {path} {code}"
            )
            for name, entry in entries.items():
                yield path, method, str(code), name, entry, schema


def _all_example_values(spec: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield ``(label, value)`` for every published example value."""
    for path, method, name, entry, _schema in _request_examples(spec):
        yield f"{method.upper()} {path} request/{name}", entry["value"]
    for path, method, code, name, entry, _schema in _response_examples(spec):
        yield f"{method.upper()} {path} {code}/{name}", entry["value"]


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validator (the subset the document uses)
# ---------------------------------------------------------------------------


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _validate(
    instance: Any,
    schema: dict[str, Any],
    spec: dict[str, Any],
    where: str,
) -> list[str]:
    """
    Validate ``instance`` against ``schema``; return human-readable failures.

    Only the keywords the generated document actually emits are implemented.
    ``format``/``title``/``description``/``examples``/``default`` are
    annotations and are ignored, matching JSON Schema semantics.
    """
    errors: list[str] = []
    if "$ref" in schema:
        return _validate(instance, _resolve_ref(spec, schema["$ref"]), spec, where)

    if "allOf" in schema:
        for branch in schema["allOf"]:
            errors.extend(_validate(instance, branch, spec, where))

    if "anyOf" in schema:
        failures = [_validate(instance, b, spec, where) for b in schema["anyOf"]]
        if all(failures):
            errors.append(f"{where}: matches no anyOf branch ({failures})")

    if "oneOf" in schema:
        matches = [
            branch
            for branch in schema["oneOf"]
            if not _validate(instance, branch, spec, where)
        ]
        if len(matches) != 1:
            errors.append(f"{where}: matches {len(matches)} oneOf branches")

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{where}: expected const {schema['const']!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} not in enum {schema['enum']!r}")

    declared_type = schema.get("type")
    if declared_type is not None:
        types = (
            [declared_type] if isinstance(declared_type, str) else list(declared_type)
        )
        checks = [_TYPE_CHECKS[name] for name in types if name in _TYPE_CHECKS]
        if checks and not any(check(instance) for check in checks):
            errors.append(f"{where}: {instance!r} is not {types}")
            return errors

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: {instance} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(
                f"{where}: {instance} <= exclusiveMinimum {schema['exclusiveMinimum']}"
            )
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(
                f"{where}: {instance} >= exclusiveMaximum {schema['exclusiveMaximum']}"
            )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{where}: does not match {schema['pattern']!r}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                errors.extend(_validate(item, items, spec, f"{where}[{index}]"))

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name in schema.get("required", []):
                if name not in instance:
                    errors.append(f"{where}: missing required {name!r}")
            for name, value in instance.items():
                if name in properties:
                    errors.extend(
                        _validate(value, properties[name], spec, f"{where}.{name}")
                    )
                    continue
                additional = schema.get("additionalProperties")
                if additional is False:
                    errors.append(f"{where}: unexpected property {name!r}")
                elif isinstance(additional, dict):
                    errors.extend(_validate(value, additional, spec, f"{where}.{name}"))
    return errors


# ---------------------------------------------------------------------------
# Document contract
# ---------------------------------------------------------------------------


def test_servers_document_the_real_local_default(spec: dict[str, Any]) -> None:
    """``servers`` must point at the host/port ``novel-api`` actually binds."""
    servers = spec.get("servers")

    assert isinstance(servers, list) and servers, "the document declares no servers"
    server = servers[0]
    assert str(server.get("description", "")).strip()

    parsed = urlsplit(server["url"])
    assert parsed.scheme == "http", server["url"]
    assert parsed.hostname == "127.0.0.1", server["url"]

    defaults = api_main._build_parser().parse_args([])
    assert defaults.host == "127.0.0.1"
    assert parsed.port == defaults.port == 8081

    # Paths already carry the /api/v1 prefix; a base URL with a path would
    # produce /api/v1/api/v1/... in generated clients.
    assert parsed.path in ("", "/"), server["url"]
    assert "api/v1" in server["description"]
    assert all(path.startswith("/api/v1/") for path in spec["paths"])


def test_examples_are_well_formed_openapi_entries(spec: dict[str, Any]) -> None:
    """Every published example needs ``summary`` + ``value``."""
    failures: list[str] = []

    for path, method, name, entry, _schema in _request_examples(spec):
        if not str(entry.get("summary", "")).strip() or "value" not in entry:
            failures.append(f"{method.upper()} {path} request/{name}")
    for path, method, code, name, entry, _schema in _response_examples(spec):
        if not str(entry.get("summary", "")).strip() or "value" not in entry:
            failures.append(f"{method.upper()} {path} {code}/{name}")

    assert not failures, f"malformed example entries: {failures}"


def test_every_request_example_matches_its_schema(spec: dict[str, Any]) -> None:
    """A drift between an example and its request schema is a client bug."""
    failures: list[str] = []
    checked = 0

    for path, method, name, entry, schema in _request_examples(spec):
        checked += 1
        failures.extend(
            _validate(entry["value"], schema, spec, f"{method.upper()} {path}#{name}")
        )

    assert checked >= 4, "the key write endpoints should publish request examples"
    assert not failures, "\n".join(failures)


def test_every_response_example_matches_its_schema(spec: dict[str, Any]) -> None:
    """Response examples are validated against the documented response schema."""
    failures: list[str] = []
    checked = 0

    for path, method, code, name, entry, schema in _response_examples(spec):
        checked += 1
        failures.extend(
            _validate(
                entry["value"],
                schema,
                spec,
                f"{method.upper()} {path} {code}#{name}",
            )
        )

    assert checked >= 10, "the key endpoints should publish response examples"
    assert not failures, "\n".join(failures)


def test_request_examples_only_use_declared_field_names(spec: dict[str, Any]) -> None:
    """Unknown request fields would be silently ignored by Pydantic."""
    failures: list[str] = []

    for path, method, name, entry, schema in _request_examples(spec):
        target = _resolve_ref(spec, schema["$ref"]) if "$ref" in schema else schema
        properties = set(target.get("properties", {}))
        value = entry["value"]
        assert isinstance(value, dict)
        unknown = set(value) - properties
        if unknown:
            failures.append(f"{method.upper()} {path}#{name}: extra {sorted(unknown)}")
        missing = set(target.get("required", [])) - set(value)
        if missing:
            failures.append(
                f"{method.upper()} {path}#{name}: missing {sorted(missing)}"
            )

    assert not failures, "\n".join(failures)


def test_key_endpoints_publish_examples(spec: dict[str, Any]) -> None:
    """The endpoints most clients integrate with all show concrete payloads."""
    request_counts: dict[tuple[str, str], int] = {}
    for path, method, _name, _entry, _schema in _request_examples(spec):
        request_counts[(path, method)] = request_counts.get((path, method), 0) + 1

    response_statuses: dict[tuple[str, str], set[str]] = {}
    for path, method, code, _name, _entry, _schema in _response_examples(spec):
        response_statuses.setdefault((path, method), set()).add(code)

    assert request_counts[("/api/v1/search", "post")] >= 2  # single + aggregate
    assert request_counts[("/api/v1/tasks", "post")] >= 1
    assert request_counts[("/api/v1/music/search", "post")] >= 2

    for path, method in (
        ("/api/v1/search", "post"),
        ("/api/v1/tasks", "post"),
        ("/api/v1/music/search", "post"),
        ("/api/v1/music/health", "get"),
        ("/api/v1/history", "get"),
    ):
        assert "200" in response_statuses[(path, method)], f"{method.upper()} {path}"


def test_required_error_statuses_and_304_are_illustrated(
    spec: dict[str, Any],
) -> None:
    """Every documented failure mode (and the bodyless 304) has an example."""
    illustrated = {
        code
        for _path, _method, code, _name, _entry, _schema in _response_examples(spec)
    }
    missing = REQUIRED_ERROR_EXAMPLES - illustrated
    assert not missing, f"statuses documented without an example: {sorted(missing)}"

    for path in sorted(ETAG_ENDPOINTS):
        operation = spec["paths"][path]["get"]
        not_modified = operation["responses"]["304"]

        assert "content" not in not_modified, f"{path} 304 declares a body"
        assert "examples" not in not_modified, f"{path} 304 has an example"
        etag = not_modified["headers"]["ETag"]
        resolved = _resolve_ref(spec, etag["$ref"]) if "$ref" in etag else etag
        assert resolved["schema"] == {"type": "string"}
        assert str(resolved.get("description", "")).strip()


def test_examples_do_not_contain_credentials(spec: dict[str, Any]) -> None:
    """Examples may show ``<token>`` placeholders, never a usable secret."""
    offenders: list[str] = []

    for label, value in _all_example_values(spec):
        text = json.dumps(value, ensure_ascii=False)
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{label}: matched {pattern.pattern}")

    assert not offenders, "\n".join(offenders)

    # The auth failure example keeps the two accepted header forms as
    # placeholders, which is the only credential-shaped content allowed.
    auth_example = spec["paths"]["/api/v1/search"]["post"]["responses"]["401"][
        "content"
    ]["application/json"]["examples"]["default"]["value"]
    assert "<token>" in auth_example["detail"]
    assert "Authorization: Bearer" in auth_example["detail"]
    assert "X-API-Key" in auth_example["detail"]


# ---------------------------------------------------------------------------
# Field names exist in the serialisers, not just in the example
# ---------------------------------------------------------------------------


def test_task_example_fields_match_the_serializer(spec: dict[str, Any]) -> None:
    """``POST /tasks`` shows exactly the keys ``task_to_dict`` emits."""
    example = spec["paths"]["/api/v1/tasks"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["queued_task"]["value"]
    emitted = task_to_dict(DownloadTask(title="t", site="qidian", book_id="1"))

    assert set(example) == set(emitted)


def test_search_row_examples_use_documented_result_fields(
    spec: dict[str, Any],
) -> None:
    """Search rows use ``SearchResult`` fields plus the documented derived keys."""
    allowed = set(SearchResult.__annotations__) | set(SEARCH_DERIVED_FIELDS)
    required = set(SearchResult.__annotations__)

    examples = spec["paths"]["/api/v1/search"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]
    assert set(examples) == {"single_site", "aggregate"}

    for name, entry in examples.items():
        payload = entry["value"]
        assert set(payload) == {"total", "results", "query_intent"}
        assert isinstance(payload["results"], list) and payload["results"]
        for row in payload["results"]:
            assert required <= set(row), (
                f"{name}: missing {sorted(required - set(row))}"
            )
            assert set(row) <= allowed, (
                f"{name}: unexpected {sorted(set(row) - allowed)}"
            )

    grouped = examples["aggregate"]["value"]["results"][0]
    assert grouped["sources"] == ["qidian", "biquge5"]


def test_music_examples_use_the_public_track_fields(spec: dict[str, Any]) -> None:
    """Tracks are exactly ``PUBLIC_TRACK_FIELDS``; ``extra`` never appears."""
    examples = spec["paths"]["/api/v1/music/search"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["examples"]

    for name, entry in examples.items():
        payload = entry["value"]
        expected = {
            "total",
            "tracks",
            "search_id",
            "platform",
            "offset",
            "supports_offset",
        }
        if name == "all_platforms":
            expected.add("platforms")
        assert set(payload) == expected, name

        for track in payload["tracks"]:
            assert set(track) == set(PUBLIC_TRACK_FIELDS), name
            assert "extra" not in track
            assert track["http_headers"] == {}

    platforms = examples["all_platforms"]["value"]["platforms"]
    assert platforms
    for item in platforms:
        assert set(item) == {"platform", "ok", "count", "error"}

    health = spec["paths"]["/api/v1/music/health"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["examples"]["mixed"]["value"]
    assert health
    for item in health:
        assert set(item) == {"platform", "ok", "elapsed_ms", "error"}


@pytest.mark.asyncio
async def test_history_example_fields_match_the_endpoint(
    app: FastAPI,
    spec: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The history example mirrors a real (offline) response, key for key."""
    exported = tmp_path / "exported"
    exported.mkdir()
    (exported / "book.txt").write_text("txt", encoding="utf-8")
    (exported / "book.epub").write_text("epub", encoding="utf-8")
    monkeypatch.setattr(history_files, "_output_dir", lambda: exported)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/history")

    assert response.status_code == 200
    payload = response.json()
    example = spec["paths"]["/api/v1/history"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["page"]["value"]

    assert set(payload) == set(example)
    assert payload["files"]
    assert set(payload["files"][0]) == set(example["files"][0])
    sample = example["files"][0]
    assert sample["download_url"] == f"/downloads/{quote(sample['name'], safe='')}"

    # The endpoint serves the configured dir, not app.state.output_dir: the
    # monkeypatch above is what keeps this test off the real exports.
    assert {item["name"] for item in payload["files"]} == {"book.txt", "book.epub"}
