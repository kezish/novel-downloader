#!/usr/bin/env python3
"""
tests.infra.test_config_fuzz
----------------------------

Seeded randomized robustness tests for configuration loading and validation.

The corpus is generated once at import time from a fixed seed, so a failure is
reproducible from the case id (``case-000`` ...).  Each generated document is
written to a pytest ``tmp_path`` directory only -- the repository, the user's
app home and the developer's real ``settings.toml`` are never touched.

What the generated documents contain:

* random nested inline tables, arrays and arrays-of-tables (``[[...]]``),
* integers, floats, booleans, strings, dates/times and ``inf``/``nan``,
* known keys with deliberately wrong types (``workers = "eight"``,
  ``output_dir = 42``, ``verify_ssl = "maybe"`` ...),
* unknown keys, case variants (``Workers``/``WORKERS``), empty tables and
  empty site names,
* BOM prefixes, CRLF and mixed line endings, comments and indentation,
* a few deliberately invalid TOML documents (must raise ``ValueError``).

The invariant under test is that *no* document can produce a bare
``KeyError``/``TypeError``/``AttributeError``/``OverflowError``/``RecursionError``:

* :func:`load_config` either returns a mapping or raises ``ValueError`` /
  ``ConfigError`` (with a diagnostic that never dumps the file),
* :func:`validate_config` returns :class:`Problem` objects and never raises
  (the sole exception is the documented opt-in ``ConfigError`` in strict mode,
  which the loader re-raises as well),
* both agree on the same input: whatever loads validates, and every non-info
  validation finding reaches the log as an explicit warning,
* :class:`ConfigAdapter` normalizes field types (or raises the documented
  ``ValueError`` for structurally invalid ``book_ids``).

Curated regression cases at the bottom pin the defects this fuzzing found:
unbounded TOML integers (``OverflowError`` / the 4300-digit int-to-str cap) and
paths the OS rejects (embedded NUL, over-long Windows paths) previously escaped
``validate_config`` as a bare ``ValueError``.
"""

from __future__ import annotations

import codecs
import contextlib
import copy
import json
import logging
import os
import random
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.config import (
    ConfigAdapter,
    Problem,
    load_config,
    validate_config,
)
from novel_downloader.infra.config.validation import ConfigError

#: Fixed seed: changing it changes the corpus, so keep it when adding cases.
SEED = 20261124
#: Generated documents (plus curated cases) -- comfortably above the 50 asked
#: for, while staying fast (the whole module runs in well under a second).
GENERATED_CASES = 60

_BARE_ERRORS = (
    KeyError,
    TypeError,
    AttributeError,
    NameError,
    IndexError,
    OverflowError,
    RecursionError,
)

_GENERAL_FLAT_KEYS = (
    "raw_data_dir",
    "output_dir",
    "cache_dir",
    "request_interval",
    "workers",
    "max_connections",
    "max_rps",
    "retry_times",
    "backoff_factor",
    "timeout",
    "storage_batch_size",
    "cache_book_info",
    "cache_chapter",
    "fetch_inaccessible",
    "backend",
    "locale_style",
    "login_required",
    "update_check",
    "http2",
    "impersonate",
    "verify_ssl",
    "user_agent",
    "headers",
    "proxy",
    "proxy_user",
    "proxy_pass",
    "trust_env",
    "download_retry_times",
    "strict_config",
    "browser_assisted_sites",
)

_GENERAL_NESTED_KEYS = ("parser", "output", "debug", "processors")

_SITE_FLAT_KEYS = (
    "login_required",
    "username",
    "password",
    "cookies",
    "request_interval",
    "retry_times",
    "backoff_factor",
    "timeout",
    "max_connections",
    "max_rps",
    "workers",
    "storage_batch_size",
    "cache_book_info",
    "cache_chapter",
    "fetch_inaccessible",
    "backend",
    "locale_style",
    "http2",
    "impersonate",
    "verify_ssl",
    "user_agent",
    "headers",
    "proxy",
    "proxy_user",
    "proxy_pass",
    "trust_env",
)

_PARSER_KEYS = (
    "enable_ocr",
    "batch_size",
    "remove_watermark",
    "model_name",
    "model_dir",
    "input_shape",
    "device",
    "precision",
    "cpu_threads",
    "enable_hpi",
    "use_truncation",
    "cut_mode",
)

_OUTPUT_KEYS = (
    "formats",
    "render_missing_chapter",
    "append_timestamp",
    "filename_template",
    "include_picture",
    "split_mode",
)

_DEBUG_KEYS = ("save_html", "log_level", "log_dir")

#: Unknown keys plus case variants -- every one must be a warning, not a crash.
_NOISE_GENERAL_KEYS = (
    "worker",
    "Workers",
    "WORKERS",
    "output-dir",
    "unknown_key",
    "",
    "ключ",
    "a b",
    "request_interval ",
    "k" * 64,
)

_STRING_POOL = (
    "",
    " ",
    "false",
    "TRUE",
    "0",
    "1",
    "8",
    "2.5",
    "8.0",
    "1e3",
    "aiohttp",
    "httpx",
    "curl_cffi",
    "simplified",
    "traditional",
    "epub",
    "txt",
    "book",
    "volume",
    "INFO",
    "debug",
    "auto",
    "manual",
    "off",
    "./downloads",
    "./raw_data",
    "./logs",
    "~/novel-fuzz",
    "${NOVEL_FUZZ_UNDEFINED}",
    "%NOVEL_FUZZ_UNDEFINED%",
    "http://127.0.0.1:7890",
    "socks5://[::1",
    "CON",
    "\x00",
    "\x00tail",
    "\x01\x1f",
    "x" * 300,
    "键" * 50,
    "line\nbreak",
    'quote"backslash\\',
    "{title}_{author}",
)

_DATE_POOL = (
    "1979-05-27",
    "07:32:00",
    "1979-05-27T07:32:00Z",
    "1979-05-27T00:32:00.999999-07:00",
)

_PROCESSOR_NAMES = (
    "cleaner",
    "corrector",
    "zh_convert",
    "translator.google",
    "translator.edge",
    "third.party.plugin",
    "Cleaner",
    "",
)

#: ``remove_formt_chars`` is the documented typo; the rest are real options.
_PROCESSOR_OPTION_KEYS = (
    "remove_format_chars",
    "remove_formt_chars",
    "direction",
    "engine",
    "sleep",
    "dedupe",
    "unknown_option",
    "",
)

_INVALID_SNIPPETS = (
    "workers = ",
    "= 1",
    "[general\nworkers = 1",
    "[general]\nworkers = 1\nworkers = 2",
    '[general]\nuser_agent = "unterminated',
    "[general]\nworkers = 007",
)


@dataclass(frozen=True)
class FuzzCase:
    """One generated document plus the metadata the test needs."""

    case_id: str
    text: str
    payload: bytes
    invalid_syntax: bool = False

    @property
    def bytes(self) -> bytes:
        return self.payload


def _toml_str(value: str) -> str:
    """Encode a Python string as a TOML basic string.

    ``json.dumps`` escapes exactly the characters TOML also requires to be
    escaped (``"``, ``\\``, C0 controls, non-ASCII) and uses the same
    ``\\uXXXX`` form, so the output is a valid TOML string literal.
    """
    return json.dumps(value, ensure_ascii=True)


def _toml_float(rng: random.Random) -> str:
    return rng.choice(
        ("0.0", "-0.0", "0.5", "2.5", "-1.5", "1e3", "1e308", "1e-308", "3.25")
    )


def _toml_scalar(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.28:
        return str(rng.choice((0, 1, -1, 4, 8, 32, 4096, 65536, 10**6)))
    if roll < 0.44:
        return _toml_float(rng)
    if roll < 0.54:
        return rng.choice(("true", "false"))
    if roll < 0.80:
        return _toml_str(rng.choice(_STRING_POOL))
    if roll < 0.92:
        return rng.choice(_DATE_POOL)
    # TOML float literals; validation must reject them without converting.
    return rng.choice(("inf", "-inf", "nan"))


def _toml_value(rng: random.Random, depth: int = 0) -> str:
    if depth >= 3 or rng.random() < 0.55:
        return _toml_scalar(rng)
    if rng.random() < 0.5:
        items = [_toml_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
        return "[" + ", ".join(items) + "]"
    keys = rng.sample(_INLINE_KEYS, k=rng.randrange(0, 4))
    pairs = [f"{_toml_str(key)} = {_toml_value(rng, depth + 1)}" for key in keys]
    return "{ " + ", ".join(pairs) + " }" if pairs else "{}"


#: Inline-table keys are sampled without replacement, so a generated table is
#: always syntactically valid (duplicate keys are a TOML error).
_INLINE_KEYS = (
    "a",
    "b",
    "name",
    "book_id",
    "start_id",
    "ignore_ids",
    "workers",
    "Workers",
    "",
    "键",
    "k" * 32,
)


def _book_ids_value(rng: random.Random) -> str:
    """Return one of the shapes ``book_ids`` historically accepted or rejected."""
    return rng.choice(
        (
            _toml_str("123456"),
            "123456",
            _toml_str(""),
            "true",
            "1.5",
            '["1", 2]',
            '["1", 2, { book_id = "3" }]',
            "[[1], [2]]",
            '{ book_id = 1, start_id = "2", ignore_ids = [1, 2] }',
            "{ start_id = 1 }",
            "{ book_id = true }",
            "[]",
        )
    )


def _key_value_lines(rng: random.Random, keys: tuple[str, ...]) -> list[str]:
    chosen = rng.sample(keys, k=rng.randrange(1, min(5, len(keys)) + 1))
    return [f"{_toml_str(key)} = {_toml_value(rng)}" for key in chosen]


def _format_document(rng: random.Random, lines: list[str]) -> str:
    """Join lines with a newline style and add harmless decorations."""
    newline = rng.choice(("\n", "\n", "\r\n"))
    indent = rng.choice(("", "", "  ", "\t"))
    body_lines = []
    for line in lines:
        if rng.random() < 0.25:
            line = indent + line
        body_lines.append(line)
        # Blank lines and comments are legal between any two TOML statements.
        if rng.random() < 0.15:
            body_lines.append("")
        if rng.random() < 0.08:
            body_lines.append("# fuzz comment")
    text = newline.join(body_lines)
    if rng.random() < 0.3:
        text = "# generated by the configuration fuzz corpus" + newline + text
    if rng.random() < 0.2:
        text += newline
    if rng.random() < 0.2 and newline == "\n":
        # Mixed line endings: TOML accepts both CRLF and LF.
        text = text.replace("\n", "\r\n", rng.randrange(0, 3))
    return text


def _build_general(rng: random.Random, lines: list[str]) -> None:
    lines.append("[general]")
    lines.extend(_key_value_lines(rng, _GENERAL_FLAT_KEYS))

    nested = rng.choice((None, None, None, *_GENERAL_NESTED_KEYS))
    if nested is not None:
        # A scalar where a table is expected is a classic hand-edit mistake.
        lines.append(f"{_toml_str(nested)} = {_toml_scalar(rng)}")
    else:
        for table in rng.sample(_GENERAL_NESTED_KEYS, k=rng.randrange(0, 3)):
            if table == "parser":
                lines.append("[general.parser]")
                lines.extend(_key_value_lines(rng, _PARSER_KEYS))
            elif table == "output":
                lines.append("[general.output]")
                lines.extend(_key_value_lines(rng, _OUTPUT_KEYS))
            elif table == "debug":
                lines.append("[general.debug]")
                lines.extend(_key_value_lines(rng, _DEBUG_KEYS))
            else:
                lines.append("[[general.processors]]")
                lines.append(f"name = {_toml_str(rng.choice(_PROCESSOR_NAMES))}")
                lines.append(f"overwrite = {_toml_value(rng)}")
                for option in rng.sample(_PROCESSOR_OPTION_KEYS, k=rng.randrange(0, 4)):
                    lines.append(f"{_toml_str(option)} = {_toml_value(rng)}")

    for key in rng.sample(
        _NOISE_GENERAL_KEYS, k=rng.randrange(0, min(4, len(_NOISE_GENERAL_KEYS) + 1))
    ):
        lines.append(f"{_toml_str(key)} = {_toml_value(rng)}")


def _build_site(rng: random.Random, lines: list[str], root: str, name: str) -> None:
    lines.append(f"[{_toml_str(root)}.{_toml_str(name)}]")
    lines.extend(_key_value_lines(rng, _SITE_FLAT_KEYS))
    if rng.random() < 0.5:
        lines.append(f"book_ids = {_book_ids_value(rng)}")

    if rng.random() < 0.3:
        lines.append(f"[{_toml_str(root)}.{_toml_str(name)}.parser]")
        lines.extend(_key_value_lines(rng, _PARSER_KEYS))

    if rng.random() < 0.4:
        lines.append(f"[[{_toml_str(root)}.{_toml_str(name)}.processors]]")
        lines.append(f"name = {_toml_str(rng.choice(_PROCESSOR_NAMES))}")
        if rng.random() < 0.7:
            lines.append(f"overwrite = {_toml_value(rng)}")
        for option in rng.sample(_PROCESSOR_OPTION_KEYS, k=rng.randrange(1, 4)):
            lines.append(f"{_toml_str(option)} = {_toml_value(rng)}")


def _build_document(rng: random.Random, index: int) -> FuzzCase:
    if index % 13 == 0:
        # Empty / comment-only document: the loader returns an empty table.
        text = "# empty configuration\n" if index % 2 else ""
        return FuzzCase(f"case-{index:03d}", text, text.encode("utf-8"))

    if rng.random() < 0.08:
        snippet = rng.choice(_INVALID_SNIPPETS)
        text = _format_document(rng, snippet.split("\n"))
        return FuzzCase(
            f"case-{index:03d}",
            text,
            _with_prefix(rng, text),
            invalid_syntax=True,
        )

    lines: list[str] = []
    if rng.random() < 0.3:
        root_key = rng.choice(("version", "General", "Sites", "name"))
        lines.append(f"{_toml_str(root_key)} = {_toml_scalar(rng)}")

    if rng.random() < 0.85:
        _build_general(rng, lines)

    if rng.random() < 0.4:
        lines.append("[plugins]")
        lines.append(
            "enable_local_plugins = "
            + rng.choice(("true", "false", "1", _toml_str("yes")))
        )
        lines.append(f"local_plugins_path = {_toml_value(rng)}")
        if rng.random() < 0.4:
            lines.append(f"override_builtins = {_toml_value(rng)}")

    if rng.random() < 0.7:
        root = rng.choice(("sites", "sites", "sites", "site"))
        names = rng.sample(
            ("qidian", "Site-X", "site_x", "SITE", "站点", ""),
            k=rng.randrange(1, 3),
        )
        for name in names:
            _build_site(rng, lines, root, name)

    text = _format_document(rng, lines)
    return FuzzCase(f"case-{index:03d}", text, _with_prefix(rng, text))


def _with_prefix(rng: random.Random, text: str) -> bytes:
    payload = text.encode("utf-8")
    if rng.random() < 0.15:
        return codecs.BOM_UTF8 + payload
    return payload


def _generate_cases() -> list[FuzzCase]:
    rng = random.Random(SEED)
    return [_build_document(rng, index) for index in range(GENERATED_CASES)]


CASES = _generate_cases()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def config_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an isolated writable directory with an isolated app home."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "app-home"))
    directory = tmp_path / "configs"
    directory.mkdir()
    return directory


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _first_site(data: dict[str, Any]) -> str:
    sites = data.get("sites")
    if isinstance(sites, dict):
        for name in sites:
            if isinstance(name, str):
                return name
    return "missing-site"


def _as_problems(value: Any) -> list[Problem]:
    assert isinstance(value, list)
    for problem in value:
        assert isinstance(problem, Problem)
        assert problem.level in {"error", "warning", "info"}
        assert isinstance(problem.path, str)
        assert isinstance(problem.message, str)
    return value


def _config_warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "novel_downloader.infra.config.file_io"
        and record.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# Randomized corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_random_toml_never_raises_unexpected(
    case: FuzzCase,
    config_paths: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write(config_paths / f"{case.case_id}.toml", case.bytes)
    caplog.set_level(logging.WARNING)

    try:
        loaded = load_config(path)
    except ConfigError as exc:
        # Strict mode is document-wide: the loader and the validator must
        # refuse the same file, and neither may turn it into another error.
        for bare in _BARE_ERRORS:
            assert not isinstance(exc, bare), case.case_id
        with pytest.raises(ConfigError) as validate_exc:
            validate_config(path)
        assert str(validate_exc.value) == str(exc), case.case_id
        return
    except ValueError as exc:
        for bare in _BARE_ERRORS:
            assert not isinstance(exc, bare), (
                f"{case.case_id}: loader raised a bare {type(exc).__name__}"
            )
        # The same file must be reported as an error by path-based validation.
        problems = _as_problems(validate_config(path))
        assert any(p.level == "error" for p in problems), case.case_id
        return

    assert isinstance(loaded, dict), case.case_id

    problems = _as_problems(validate_config(loaded))
    path_problems = _as_problems(validate_config(path))

    # Both entry points see the same bytes, so the findings must match.
    assert [str(p) for p in path_problems] == [str(p) for p in problems], case.case_id

    # Whatever the loader accepts, validation reports instead of raising; each
    # non-info finding is surfaced as a warning by the loader.
    expected_warnings = sum(1 for p in problems if p.level != "info")
    assert len(_config_warning_messages(caplog)) == expected_warnings, case.case_id

    _assert_adapter_normalizes(loaded, case.case_id)


def _assert_adapter_normalizes(data: dict[str, Any], case_id: str) -> None:
    adapter = ConfigAdapter(data)
    site = _first_site(data)

    fetcher = adapter.get_fetcher_config(site)
    parser_cfg = adapter.get_parser_config(site)
    client = adapter.get_client_config(site)
    exporter = adapter.get_exporter_config(site)

    assert isinstance(fetcher.request_interval, float), case_id
    assert fetcher.request_interval >= 0.0, case_id
    assert isinstance(fetcher.retry_times, int), case_id
    assert not isinstance(fetcher.retry_times, bool), case_id
    assert fetcher.retry_times >= 0, case_id
    assert isinstance(fetcher.timeout, float) and fetcher.timeout > 0.0, case_id
    assert isinstance(fetcher.max_connections, int), case_id
    assert 1 <= fetcher.max_connections <= 4096, case_id
    assert isinstance(fetcher.verify_ssl, bool), case_id
    assert isinstance(fetcher.http2, bool), case_id
    assert fetcher.backend in {"aiohttp", "httpx", "curl_cffi"}, case_id
    assert fetcher.locale_style in {"simplified", "traditional"}, case_id
    assert fetcher.headers is None or all(
        isinstance(k, str) and isinstance(v, str) for k, v in fetcher.headers.items()
    ), case_id

    assert isinstance(parser_cfg.batch_size, int), case_id
    assert 1 <= parser_cfg.batch_size <= 65536, case_id
    assert isinstance(parser_cfg.use_truncation, bool), case_id
    assert isinstance(parser_cfg.ocr_cfg.input_shape, (tuple, type(None))), case_id

    assert isinstance(client.workers, int) and 1 <= client.workers <= 4096, case_id
    assert isinstance(client.request_interval, float), case_id
    assert isinstance(client.output_dir, str), case_id
    assert isinstance(client.save_html, bool), case_id

    assert isinstance(exporter.filename_template, str), case_id
    assert exporter.filename_template, case_id
    assert exporter.split_mode in {"book", "volume", "chapter"}, case_id

    assert adapter.get_log_level() in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }, case_id
    plugins = adapter.get_plugins_config()
    assert isinstance(plugins["enable_local_plugins"], bool), case_id
    assert isinstance(plugins["local_plugins_path"], str), case_id
    assert all(isinstance(fmt, str) for fmt in adapter.get_export_fmt(site)), case_id
    assert all(
        isinstance(proc.name, str) and proc.name
        for proc in adapter.get_processor_configs(site)
    ), case_id
    login = adapter.get_login_config(site)
    assert all(
        isinstance(key, str) and isinstance(value, str) for key, value in login.items()
    ), case_id

    # ``book_ids`` is the one place the adapter documents a ValueError for
    # structurally invalid input; anything else must not escape.
    with contextlib.suppress(ValueError):
        books = adapter.get_book_ids(site)
        assert all(isinstance(book.book_id, str) and book.book_id for book in books), (
            case_id
        )


def test_generated_corpus_covers_required_shapes() -> None:
    assert len(CASES) >= 50
    joined = "\n".join(case.text for case in CASES)

    assert any(case.bytes.startswith(codecs.BOM_UTF8) for case in CASES)
    assert any("\r\n" in case.text for case in CASES)
    assert any("[[general.processors]]" in case.text for case in CASES)
    assert any("processors]]" in case.text for case in CASES)
    assert any("= {" in case.text for case in CASES)
    assert any("= [" in case.text for case in CASES)
    assert any(date in joined for date in _DATE_POOL)
    assert any(marker in joined for marker in ("Workers", "WORKERS", "worker"))
    assert any(case.invalid_syntax for case in CASES)
    assert any(case.text == "" for case in CASES)


# ---------------------------------------------------------------------------
# Unbounded TOML integers (regression: OverflowError / int-to-str cap)
# ---------------------------------------------------------------------------

_HUGE = 10**5000
_NEGATIVE_HUGE = -_HUGE

_HUGE_INTEGER_CASES = (
    ({"general": {"request_interval": _HUGE}}, "general.request_interval"),
    ({"general": {"backoff_factor": _HUGE}}, "general.backoff_factor"),
    ({"general": {"max_rps": _HUGE}}, "general.max_rps"),
    ({"general": {"timeout": _HUGE}}, "general.timeout"),
    ({"general": {"workers": _NEGATIVE_HUGE}}, "general.workers"),
    ({"general": {"retry_times": _NEGATIVE_HUGE}}, "general.retry_times"),
    ({"general": {"storage_batch_size": _NEGATIVE_HUGE}}, "general.storage_batch_size"),
    ({"sites": {"s": {"workers": _NEGATIVE_HUGE}}}, "sites.s.workers"),
    ({"sites": {"s": {"timeout": _HUGE}}}, "sites.s.timeout"),
    (
        {"general": {"parser": {"input_shape": [_NEGATIVE_HUGE, 1, 1]}}},
        "general.parser.input_shape[0]",
    ),
)


@pytest.mark.parametrize(
    ("data", "expected_path"),
    _HUGE_INTEGER_CASES,
    ids=[case[1] for case in _HUGE_INTEGER_CASES],
)
def test_huge_integer_is_reported_not_raised(
    data: dict[str, Any], expected_path: str
) -> None:
    problems = _as_problems(validate_config(data))

    assert any(
        problem.level == "error" and problem.path == expected_path
        for problem in problems
    )
    # The diagnostic must never interpolate the 5000-digit number itself
    # (``str(big_int)`` raises above the 4300-digit interpreter cap).
    for problem in problems:
        assert len(problem.message) < 250


def test_huge_integer_normalizes_to_defaults_in_adapter() -> None:
    adapter = ConfigAdapter(
        {
            "general": {
                "request_interval": _HUGE,
                "timeout": _HUGE,
                "backoff_factor": _HUGE,
                "max_rps": _HUGE,
                "retry_times": _NEGATIVE_HUGE,
            }
        }
    )

    client = adapter.get_client_config("s")
    fetcher = client.fetcher_cfg

    assert client.request_interval == 0.5
    assert fetcher.request_interval == 0.5
    assert fetcher.timeout == 10.0
    assert fetcher.backoff_factor == 2.0
    assert fetcher.max_rps == 1000.0
    assert client.retry_times == 0  # clamped to the documented lower bound


def test_toml_integer_over_interpreter_cap_is_a_clear_value_error(
    config_paths: Path,
) -> None:
    # ``tomllib`` itself refuses to materialize the int; the loader must wrap
    # that in its own ValueError and validation must report it as a problem.
    text = "[general]\nretry_times = -" + "9" * 5000 + "\n"
    path = _write(config_paths / "huge-int.toml", text.encode("utf-8"))

    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    assert "Invalid TOML" in str(excinfo.value)

    problems = _as_problems(validate_config(path))
    assert [p.path for p in problems if p.level == "error"] == ["<file>"]


# ---------------------------------------------------------------------------
# Paths the OS rejects (regression: bare ValueError out of validate_config)
# ---------------------------------------------------------------------------

_UNUSABLE_PATH = "bad\x00path"
_OVERLONG_PATH = "x" * 40000

_UNUSABLE_DIRECTORY_CASES = (
    ({"general": {"output_dir": _UNUSABLE_PATH}}, "general.output_dir"),
    ({"general": {"cache_dir": _UNUSABLE_PATH}}, "general.cache_dir"),
    ({"general": {"raw_data_dir": _UNUSABLE_PATH}}, "general.raw_data_dir"),
    ({"general": {"debug": {"log_dir": _UNUSABLE_PATH}}}, "general.debug.log_dir"),
    (
        {
            "plugins": {
                "enable_local_plugins": True,
                "local_plugins_path": _UNUSABLE_PATH,
            }
        },
        "plugins.local_plugins_path",
    ),
)


@pytest.mark.parametrize(
    ("data", "expected_path"),
    _UNUSABLE_DIRECTORY_CASES,
    ids=[case[1] for case in _UNUSABLE_DIRECTORY_CASES],
)
def test_unusable_directory_path_is_a_problem(
    data: dict[str, Any], expected_path: str
) -> None:
    problems = _as_problems(validate_config(data))

    errors = [p.path for p in problems if p.level == "error"]
    assert errors == [expected_path]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("output_dir", _UNUSABLE_PATH),
        ("output_dir", _OVERLONG_PATH),
        ("cache_dir", _OVERLONG_PATH),
    ],
    # Explicit ids: the over-long path must not end up in PYTEST_CURRENT_TEST
    # (Windows caps environment-variable values).
    ids=["nul-output-dir", "overlong-output-dir", "overlong-cache-dir"],
)
def test_unusable_directory_load_logs_a_warning(
    key: str,
    value: str,
    config_paths: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = f"[general]\n{key} = {_toml_str(value)}\n"
    path = _write(config_paths / "unusable-dir.toml", text.encode("utf-8"))
    caplog.set_level(logging.WARNING)

    loaded = load_config(path)

    assert isinstance(loaded, dict)
    assert "Configuration problem" in caplog.text
    assert any(
        problem.level == "error" and problem.path == f"general.{key}"
        for problem in validate_config(loaded)
    )


# ---------------------------------------------------------------------------
# Encoding and line endings
# ---------------------------------------------------------------------------

_PLAIN = "[general]\nworkers = 2\n"

_ENCODING_CASES = (
    ("utf8", _PLAIN.encode("utf-8"), True),
    ("bom", codecs.BOM_UTF8 + _PLAIN.encode("utf-8"), True),
    ("crlf", _PLAIN.replace("\n", "\r\n").encode("utf-8"), True),
    ("mixed", b"[general]\r\nworkers = 2\n", True),
    ("comment_crlf", b"# c\r\n\r\n[general]\r\nworkers = 2\r\n", True),
    ("invalid_byte", b"[general]\nworkers = 2\xff\n", False),
    ("utf16_bom", codecs.BOM_UTF16_LE + _PLAIN.encode("utf-16-le"), False),
)


@pytest.mark.parametrize(
    ("label", "payload", "loads"),
    _ENCODING_CASES,
    ids=[case[0] for case in _ENCODING_CASES],
)
def test_encoding_and_line_ending_variants(
    label: str,
    payload: bytes,
    loads: bool,
    config_paths: Path,
) -> None:
    path = _write(config_paths / f"{label}.toml", payload)

    if loads:
        assert load_config(path) == {"general": {"workers": 2}}
        assert validate_config(path) == []
    else:
        with pytest.raises(ValueError):
            load_config(path)
        problems = _as_problems(validate_config(path))
        assert [p.path for p in problems if p.level == "error"] == ["<file>"]


# ---------------------------------------------------------------------------
# Unknown keys, case variants, empty tables
# ---------------------------------------------------------------------------


def test_unknown_keys_and_case_variants_are_warnings() -> None:
    data = {
        "general": {"workers": 2, "Workers": 3, "WORKERS": 3, "unknown": 1, "": 1},
        "General": {},
        "plugins": {},
    }

    problems = _as_problems(validate_config(data))
    warnings = {
        problem.path: problem for problem in problems if problem.level == "warning"
    }

    assert {
        "general.Workers",
        "general.WORKERS",
        "general.unknown",
        "general.",
        "General",
    } <= set(warnings)
    assert "Did you mean 'workers'?" in warnings["general.Workers"].fix_hint
    assert validate_config({"plugins": {}}) == []


def test_empty_document_and_comment_only(config_paths: Path) -> None:
    path = _write(config_paths / "empty.toml", b"# nothing here\n")

    assert load_config(path) == {}
    assert validate_config(path) == []
    assert validate_config({}) == []


@pytest.mark.parametrize("root", [None, 42, [], [{"general": {}}]])
def test_non_mapping_root_is_an_error_not_a_crash(root: Any) -> None:
    problems = _as_problems(validate_config(root))

    assert [p.level for p in problems] == ["error"]
    assert problems[0].path == "<root>"


# ---------------------------------------------------------------------------
# Deep nesting and oversized keys/values
# ---------------------------------------------------------------------------

_DEEP_INLINE = "general = " + "{k = " * 200 + "1" + "}" * 200
_LONG_KEY = '["general"]\n"' + "k" * 20000 + '" = 1\n'
_LONG_VALUE = '["general"]\nuser_agent = "' + "v" * 200000 + '"\n'
_NESTED_TABLES = "[general.parser." + ".".join(["level"] * 8) + "]\n" + "unknown = 1\n"


@pytest.mark.parametrize(
    "text",
    (_DEEP_INLINE, _LONG_KEY, _LONG_VALUE, _NESTED_TABLES),
    ids=["deep-inline", "long-key", "long-value", "nested-tables"],
)
def test_deep_nesting_and_oversized_key_or_value(text: str, config_paths: Path) -> None:
    path = _write(config_paths / "extreme.toml", text.encode("utf-8"))

    loaded = load_config(path)

    assert isinstance(loaded, dict)
    problems = _as_problems(validate_config(loaded))
    assert all(problem.level in {"warning", "info"} for problem in problems)
    assert _as_problems(validate_config(path)) == problems


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------

_STRICT_DOCUMENT = (
    "[general]\n"
    "strict_config = true\n"
    "\n"
    "[[general.processors]]\n"
    'name = "cleaner"\n'
    "remove_formt_chars = true\n"
)

_RELAXED_DOCUMENT = _STRICT_DOCUMENT.replace("strict_config = true\n", "")


def test_strict_processor_option_is_consistent(config_paths: Path) -> None:
    strict_path = _write(config_paths / "strict.toml", _STRICT_DOCUMENT.encode("utf-8"))

    with pytest.raises(ConfigError) as load_exc:
        load_config(strict_path)
    with pytest.raises(ConfigError) as validate_exc:
        validate_config(tomllib.loads(_STRICT_DOCUMENT))

    assert "general.processors[0].remove_formt_chars" in str(load_exc.value)
    assert str(load_exc.value) == str(validate_exc.value)


def test_relaxed_processor_option_is_a_warning(config_paths: Path) -> None:
    path = _write(config_paths / "relaxed.toml", _RELAXED_DOCUMENT.encode("utf-8"))

    loaded = load_config(path)

    problems = _as_problems(validate_config(loaded))
    assert any(
        problem.level == "warning"
        and problem.path == "general.processors[0].remove_formt_chars"
        for problem in problems
    )


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_validation_has_no_side_effects(config_paths: Path) -> None:
    data: dict[str, Any] = {
        "general": {
            "workers": "8",
            "output_dir": "./fuzz-output",
            "cache_dir": "./fuzz-cache",
            "debug": {"log_dir": "./fuzz-logs", "log_level": "debug"},
        },
        "sites": {"qidian": {"book_ids": [1, "2", {"book_id": 3}]}},
        "plugins": {"enable_local_plugins": False},
    }
    before_data = copy.deepcopy(data)
    before_files = _snapshot(config_paths)
    before_env = dict(os.environ)
    before_cwd = Path.cwd()

    first = _as_problems(validate_config(data))
    second = _as_problems(validate_config(data))

    assert data == before_data
    assert first == second
    assert _snapshot(config_paths) == before_files
    assert dict(os.environ) == before_env
    assert Path.cwd() == before_cwd
