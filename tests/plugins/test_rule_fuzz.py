#!/usr/bin/env python3
"""
tests.plugins.test_rule_fuzz
----------------------------

Seeded randomized fuzzing for the rule engine.

A rule file, a response body and a decoded JSON payload are all untrusted
inputs, so every entry point in ``plugins.rule`` may only either return a value
or raise :class:`~novel_downloader.plugins.rule.model.RuleError`.  A bare
``TypeError``/``ValueError``/``XMLSyntaxError`` escaping from page data aborts
the whole site scan instead of skipping one bad page, and a pathological
regex/selector can hang the worker because ``re`` and ``lxml`` cannot be
interrupted.

Every corpus below is generated at import time from a fixed seed; the test id
is the seed of that single example, so a failure replays exactly on its own::

    pytest "tests/plugins/test_rule_fuzz.py" -k "seed=24301"

The generator bounds every size (document depth, list lengths, selector and
regex lengths and the total page size), which is what keeps one example well
under the two-second budget asserted by the timing tests.
"""

from __future__ import annotations

import contextlib
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins.rule.engine import (
    MAX_MULTI_ITEMS,
    extract,
    extract_multi,
    json_to_str,
    node_to_str,
    parse_json,
    parse_page,
    preprocess,
    select,
    select_json,
)
from novel_downloader.plugins.rule.model import Field, RuleError, Step, parse_rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.schemas import ParserConfig

pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

#: One seed for the whole corpus.  Each example adds its own index, so the id
#: shown by pytest is enough to regenerate just that input.
SEED = 0x5EED
#: Examples per generator (the task requires at least 60 overall; each
#: generator contributes that many).
CASES = 60
ROOT = Path(__file__).resolve().parents[2]

#: Upper bound for one generated example.  A well-behaved engine handles these
#: bounded inputs in milliseconds; the margin keeps the suite stable on a slow
#: CI machine while still turning a hang into a failure.
CASE_BUDGET = 2.0


# --------------------------------------------------------------------------- #
# rule-document generator
# --------------------------------------------------------------------------- #
_ATOMS: tuple[Any, ...] = (
    None,
    True,
    False,
    0,
    1,
    -1,
    2.5,
    "",
    "x",
    "a" * 9000,
    "//a[",
    "div[",
    "(a+)+$",
    "\x00",
    "{base_url}/x/{page}",
    "javascript:alert(1)",
    "http://[bad/x",
)

_SELECTORS: tuple[str, ...] = (
    "",
    "a",
    "div#list a",
    "ul.chapters li > a",
    "//div[@id='list']//a",
    "//a/@href",
    "//a/text()",
    "(//a)",
    "(count(//a))",
    "(string(//a))",
    "(boolean(//a))",
    "div[",
    "//a[",
    "a[[",
    "a:not(",
    "*|x",
    "//a[contains(., '\x00')]",
    "a" * 200,
    "//a[" * 30,
    "[*>" * 20,
)

_ATTRS: tuple[str, ...] = (
    "text",
    "inner_text",
    "html",
    "href",
    "src",
    "",
    "a b",
    "\x00",
    "text\x00",
    "x" * 200,
)

_REGEXES: tuple[Any, ...] = (
    None,
    "",
    r"\d+",
    r"(\d{1,3})",
    r"^\s*(.+?)\s*$",
    r"(?P<dup>a)(?P<dup>b)",
    "(a+)+$",
    "(a|aa)+$",
    "[",
    "\\",
    "a" * 8200,
    "\x00",
    "(?i)ad",
    "(?:",
)

_JS_SNIPPETS: tuple[str, ...] = (
    "input",
    "input.trim()",
    "let x = 1; x + 1",
    "return input",
    "function f(){return input} f()",
    "",
    "while (true) {}",
    "input.replace('a', 'b')",
    "replace_all(input, 'a', 'b')",
    "regex_replace(input, '(a+)+$', '')",
    "regex_replace(input, 'a', 'b')",
    "\x00",
    "throw 1",
)

_PY_STEPS: tuple[str, ...] = (
    "strip_html",
    "normalize_space",
    "dedupe_lines",
    "base64_decode",
    "url_decode",
    "url_encode",
    "html_unescape",
    "strip",
    "lower",
    "upper",
    "reverse",
    "",
    "os.system",
)

_JUNK_KEYS: tuple[str, ...] = (
    "unknown",
    "__proto__",
    "constructor",
    "$ref",
    "format",
    "headers",
    "steps",
)


def _steps(rng: random.Random) -> list[Any]:
    """A short ``steps`` list mixing valid py/js entries with junk shapes."""
    out: list[Any] = []
    for _ in range(rng.randrange(0, 3)):
        lang = rng.choice(["py", "py", "js", "js", "PY", "", None])
        if lang == "py":
            out.append({"lang": "py", "name": rng.choice(_PY_STEPS)})
        elif lang == "js":
            out.append({"lang": "js", "code": rng.choice(_JS_SNIPPETS)})
        else:
            out.append(rng.choice([{"lang": lang, "code": "input"}, "x", 5]))
    return out


def _field_spec(rng: random.Random, depth: int = 0) -> Any:
    """A field spec: string selector, option object, or an invalid scalar."""
    roll = rng.random()
    if roll < 0.10:
        return rng.choice(_ATOMS)
    if roll < 0.25:
        return rng.choice(_SELECTORS)

    spec: dict[str, Any] = {}
    if rng.random() < 0.75:
        spec["selector"] = rng.choice(_SELECTORS)
    if rng.random() < 0.70:
        spec["attr"] = rng.choice(_ATTRS)
    if rng.random() < 0.45:
        spec["regex"] = rng.choice(_REGEXES)
    if rng.random() < 0.30:
        spec["group"] = rng.choice([0, 1, 2, -1, "2", 1.5, None])
    if rng.random() < 0.30:
        spec["multi"] = rng.choice([True, False, "true", 1])
    if rng.random() < 0.30:
        spec["replace"] = rng.choice([[], [["a", "b"]], "x", [[1]], [[None]], [["a"]]])
    if rng.random() < 0.30:
        spec["default"] = rng.choice(["", "d", 5, None])
    if rng.random() < 0.40:
        spec["steps"] = _steps(rng)
    if depth < 2 and rng.random() < 0.35:
        spec["fallback"] = [
            _field_spec(rng, depth + 1) for _ in range(rng.randrange(0, 3))
        ]
    return spec


def _pages_spec(rng: random.Random) -> Any:
    """A ``pages`` block, including the over-limit and malformed shapes."""
    return rng.choice(
        [
            "{base_url}/p/{page}.html",
            {
                "url": "{base_url}/p/{page}.html",
                "start": rng.choice([0, 1, 2, "3"]),
                "max_pages": rng.choice([None, 1, 3, 50, 51, "2"]),
            },
            {
                "url": "{base_url}/p/{page}.html",
                "stop_when": rng.choice(_REGEXES),
                "loop_guard": rng.choice([True, False, "yes"]),
            },
            "http://[bad/x",
            5,
            None,
            [],
        ]
    )


def _headers_spec(rng: random.Random) -> Any:
    """A headers mapping, including injection-shaped values."""
    return rng.choice(
        [
            {},
            {"User-Agent": "fuzz"},
            {"X-Bad": "a\r\nX-Evil: 1"},
            {"X-Nul": "a\x00b"},
            {"has space": "v"},
            {"User-Agent": 5},
            "not-an-object",
            [],
        ]
    )


def _json_body(rng: random.Random, depth: int) -> Any:
    """A bounded JSON-like value for ``search.json_body``."""
    roll = rng.random()
    if depth <= 0 or roll < 0.35:
        return rng.choice([None, True, 1, 1.5, "q", "k" * 300, "中文", ""])
    if roll < 0.55:
        return [_json_body(rng, depth - 1) for _ in range(rng.randrange(0, 3))]
    return {
        rng.choice(["q", "key", "k" * 300, "\x00"]): _json_body(rng, depth - 1)
        for _ in range(rng.randrange(0, 3))
    }


def _base_rule(rng: random.Random) -> dict[str, Any]:
    """A structurally valid rule with randomized (sometimes invalid) values."""
    book: dict[str, Any] = {
        "url": "{base_url}/book/{book_id}/",
        "fields": {
            name: _field_spec(rng)
            for name in ("book_name", "author", "summary", "cover_url")[
                : rng.randrange(1, 5)
            ]
        },
        "chapters": {
            "item": rng.choice(_SELECTORS) or "a",
            "title": _field_spec(rng),
            "url": _field_spec(rng),
            "id_regex": rng.choice(_REGEXES),
            "id_group": rng.choice([0, 1, 2, -1, "2"]),
            "preprocess": _steps(rng),
            "container": rng.choice([None, "", "div", "["]),
            "is_desc": rng.choice([True, False, "no"]),
        },
    }
    if rng.random() < 0.6:
        book["pages"] = _pages_spec(rng)
    if rng.random() < 0.4:
        book["format"] = rng.choice(["html", "json", "xml", ""])
    if rng.random() < 0.4:
        book["headers"] = _headers_spec(rng)

    chapter: dict[str, Any] = {
        "url": "{base_url}/chapter/{chapter_id}.html",
        "content": _field_spec(rng),
        "title": _field_spec(rng) if rng.random() < 0.8 else None,
        "drop": rng.choice([[], [r"\d+"], ["(?i)ad", "x"], "x", [5]]),
        "replace": rng.choice([[], [["a", "b"]], "x"]),
        "pages": _pages_spec(rng),
        "container": rng.choice([None, "", "div", "["]),
        "preprocess": _steps(rng),
        "format": rng.choice([None, "html", "json", "xml"]),
    }

    search: dict[str, Any] = {
        "url": "{base_url}/s?q={key}",
        "method": rng.choice(["GET", "POST", "get", 1]),
        "data": rng.choice([{}, {"k": "v"}, {"k": 5}, "x"]),
        "json_body": rng.choice([None, _json_body(rng, 4)]),
        "fields": {"url": _field_spec(rng), "title": _field_spec(rng)},
        "item": rng.choice(_SELECTORS),
        "book_id_regex": rng.choice(_REGEXES),
        "single_result_fallback": rng.choice([True, False, "x"]),
        "format": rng.choice([None, "html", "json"]),
        "headers": _headers_spec(rng),
    }

    data: dict[str, Any] = {
        "site_key": rng.choice(["fuzz", "fuzz", "", "x" * 300, 5]),
        "site_name": rng.choice(["", "Fuzz", 5]),
        "base_url": rng.choice(
            ["https://example.test", "https://example.test", "", 5, "http://[bad/x"]
        ),
        "priority": rng.choice([0, 200, -1, "5", 1.5]),
        "encoding": rng.choice(["utf-8", "gb18030", "utf8-bom", "", 5]),
        "format": rng.choice(["html", "json", "xml"]),
        "hosts": rng.choice([[], ["Example.COM"], "x", [5]]),
        "headers": _headers_spec(rng),
        "resolve": rng.choice(
            [
                {"book": r"/(\d+)", "chapter": r"/c/(\d+)"},
                {"book": "["},
                "x",
                {"unknown": 1},
            ]
        ),
        "book": book,
        "chapter": chapter,
        "search": search,
        "limits": rng.choice(
            [
                {"concurrency": 2, "min_interval": 0.1, "max_interval": 0.2},
                {"concurrency": 0},
                {"min_interval": 5, "max_interval": 1},
                {"max_attempts": 0},
                "x",
            ]
        ),
    }
    if rng.random() < 0.15:
        del data["book"]
    if rng.random() < 0.15:
        del data["chapter"]
    return data


def _collect_slots(
    node: Any, out: list[tuple[dict[str, Any], str]], depth: int = 0
) -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in list(node.items()):
            out.append((node, key))
            _collect_slots(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_slots(item, out, depth + 1)


def _mutate(rng: random.Random, data: dict[str, Any]) -> None:
    """Apply a few random mutations so the corpus explores invalid shapes."""
    for _ in range(rng.randrange(0, 3)):
        slots: list[tuple[dict[str, Any], str]] = []
        _collect_slots(data, slots)
        if not slots:
            return
        parent, key = rng.choice(slots)
        action = rng.randrange(4)
        if action == 0:
            del parent[key]
        elif action == 1:
            parent[key] = rng.choice(_ATOMS)
        elif action == 2:
            parent[rng.choice(_JUNK_KEYS)] = rng.choice(_ATOMS)
        else:
            parent[key] = _json_body(rng, 3)


def _safe_field_spec(rng: random.Random) -> dict[str, Any]:
    """A field spec that stays inside every documented value range."""
    spec: dict[str, Any] = {
        "selector": rng.choice(
            ("a", "div#list a", "//div[@id='list']//a", "//a/@href", "")
        ),
        "attr": rng.choice(["text", "href", "html", "inner_text"]),
    }
    if rng.random() < 0.5:
        spec["regex"] = rng.choice([None, "", r"\d+", r"(\d{1,3})"])
    if rng.random() < 0.3:
        spec["group"] = 1
    if rng.random() < 0.3:
        spec["default"] = "d"
    if rng.random() < 0.3:
        spec["replace"] = [[".", ""]]
    if rng.random() < 0.3:
        spec["steps"] = [
            {"lang": "py", "name": rng.choice(["strip", "lower", "normalize_space"])}
        ]
    if rng.random() < 0.3:
        spec["fallback"] = [rng.choice(("a", "//a", "div p"))]
    return spec


def _safe_rule(rng: random.Random) -> dict[str, Any]:
    """A guaranteed-valid rule, so the corpus always includes accepted specs."""
    book: dict[str, Any] = {
        "url": "{base_url}/book/{book_id}/",
        "fields": {
            "book_name": _safe_field_spec(rng),
            "author": _safe_field_spec(rng),
        },
        "chapters": {
            "item": "ul#list li a",
            "title": _safe_field_spec(rng),
            "url": {"selector": "", "attr": "href"},
            "id_regex": r"/(\d+)\.html",
            "id_group": 1,
            "is_desc": rng.choice([True, False]),
        },
        "format": rng.choice(["html", "json"]),
    }
    if rng.random() < 0.5:
        book["pages"] = {
            "url": "{base_url}/p/{page}.html",
            "max_pages": rng.choice([1, 3, 50]),
            "loop_guard": rng.choice([True, False]),
        }

    chapter: dict[str, Any] = {
        "url": "{base_url}/chapter/{chapter_id}.html",
        "content": _safe_field_spec(rng),
        "title": _safe_field_spec(rng),
        "drop": rng.choice([[], [r"\d+"]]),
        "replace": rng.choice([[], [[".", ""]]]),
    }

    data: dict[str, Any] = {
        "site_key": "fuzz-safe",
        "base_url": "https://example.test",
        "priority": 200,
        "encoding": "utf-8",
        "format": rng.choice(["html", "json"]),
        "hosts": ["example.test"],
        "book": book,
        "chapter": chapter,
        "limits": {"concurrency": 2, "min_interval": 0.1, "max_interval": 0.2},
    }
    if rng.random() < 0.5:
        data["search"] = {
            "url": "{base_url}/s?q={key}",
            "method": "GET",
            "fields": {"url": _safe_field_spec(rng)},
            "json_body": {"q": "{key}", "page": 1},
        }
    return data


def _rule_spec(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    if seed % 4 == 0:
        # Every fourth example is valid, so the corpus exercises both the
        # accepted and the rejected path.
        return _safe_rule(rng)
    data = _base_rule(rng)
    _mutate(rng, data)
    return data


# --------------------------------------------------------------------------- #
# HTML / JSON / selector generators
# --------------------------------------------------------------------------- #
_HTML_FRAGMENTS: tuple[str, ...] = (
    "<div class='x'>",
    "</div>",
    "<p>text",
    "<a href='/a'>a</a>",
    "<!-- c -->",
    "<![CDATA[x]]>",
    "&bogus;",
    "<br/>",
    ">",
    "<",
    "<!--",
    "<!DOCTYPE html>",
    "<script>var a = '<p>';</script>",
    "'><img src=x onerror=alert(1)>",
    "<p a='" + "v" * 500 + "'>",
    "<span>中文</span>",
)

_ILLEGAL_ENCODING_PAGES: tuple[str, ...] = (
    "<p>\x00x</p>",
    "<p>\x01\x02</p>",
    "<p>\ud800</p>",
    "<p>\udfff</p>",
    "\ufeff<p>x</p>",
    "<?xml version='1.0' encoding='utf-8'?><p>x</p>",
)


def _html_doc(rng: random.Random, case: int) -> str:
    """Malformed HTML with a bounded total size."""
    family = case % 9
    if family == 0:
        return "".join(rng.choice(_HTML_FRAGMENTS) for _ in range(rng.randrange(1, 15)))
    if family == 1:
        return "<div>" * rng.randrange(1, 200) + "x"
    if family == 2:
        return "<p " + "a" * rng.choice([1000, 20000]) + "='v'>x</p>"
    if family == 3:
        return "<!--" * rng.randrange(1, 2000) + "x"
    if family == 4:
        return "<" * rng.randrange(1, 5000)
    if family == 5:
        return rng.choice(_ILLEGAL_ENCODING_PAGES)
    if family == 6:
        return "<![CDATA[" + "x" * 1000 + "]]>"
    if family == 7:
        return "<html><body>" + "<p>x</p>" * 50 + "</body></html>"
    return "" if case % 2 else "   \n\t "


_JSON_PATHS: tuple[str, ...] = (
    "",
    "$",
    "a",
    "a.b",
    "a[*]",
    "a[*].b",
    "items[*].name",
    "[0]",
    "[*]",
    "a[999999999999999999999999]",
    "a[-1]",
    "a[1][2]",
    "$.a.b[0].c",
    "a[",
    "..",
    "*",
    "a[/**/]",
    "\x00",
    "k" * 300,
    "a[*][*].b",
)


def _json_text(rng: random.Random, case: int) -> str:
    """Malformed or adversarial JSON, with a bounded document size."""
    family = case % 10
    if family == 0:
        return json.dumps({"a": [{"b": i % 3} for i in range(5)]})
    if family == 1:
        depth = rng.choice([500, 2000, 5000])
        return "[" * depth + "0" + "]" * depth
    if family == 2:
        return rng.choice(
            [
                "NaN",
                "Infinity",
                "-Infinity",
                "42",
                "true",
                "null",
                '"x"',
                "{",
                "[",
                "",
                "  ",
            ]
        )
    if family == 3:
        return json.dumps({"k" * rng.choice([10000, 50000]): 1})
    if family == 4:
        return '{"a": 1, "a": 2}'
    if family == 5:
        return json.dumps({"a": "\\u0000\\ud800\\uffff"})
    if family == 6:
        return json.dumps({"items": [{"name": f"n{i}"} for i in range(80)]})
    if family == 7:
        return '{"a": "' + "x" * 20000 + '"}'
    if family == 8:
        return json.dumps({"a": {"b": {"c": [1, {"d": "e"}]}}})
    return json.dumps([None, True, 1.5, "s", [], {}])


_SELECTOR_ALPHABET = "/[]()@*.#=:,'\" abc0123>-+~|^$\\\n\t中文"


def _selector_text(rng: random.Random) -> str:
    return "".join(rng.choice(_SELECTOR_ALPHABET) for _ in range(rng.randrange(0, 121)))


_REGEX_ALPHABET = "()[]{}*+?|\\abc.^$,-09:!=<> \t"


def _regex_text(rng: random.Random) -> str:
    return "".join(rng.choice(_REGEX_ALPHABET) for _ in range(rng.randrange(0, 49)))


RULE_SPECS = [_rule_spec(SEED + i) for i in range(CASES)]
HTML_CASES = [_html_doc(random.Random(SEED + 10_000 + i), i) for i in range(CASES)]
JSON_CASES = [_json_text(random.Random(SEED + 20_000 + i), i) for i in range(CASES)]
SELECTOR_CASES = [
    _selector_text(random.Random(SEED + 30_000 + i)) for i in range(CASES)
]
REGEX_CASES = [_regex_text(random.Random(SEED + 40_000 + i)) for i in range(CASES)]

_TREE = parse_page(
    "<div id='x'><p class='c'>t</p><a href='/a'>a</a><span>s</span></div>"
)


# --------------------------------------------------------------------------- #
# 1. random rule documents
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("index", "spec"),
    list(enumerate(RULE_SPECS)),
    ids=[f"seed={SEED + i}" for i in range(CASES)],
)
def test_random_rule_spec_is_parsed_or_rule_error(
    index: int, spec: dict[str, Any]
) -> None:
    started = time.perf_counter()
    try:
        rule = parse_rule(spec)
    except RuleError:
        assert time.perf_counter() - started < CASE_BUDGET
        return
    assert isinstance(rule.site_key, str)
    assert time.perf_counter() - started < CASE_BUDGET

    # A rule that was accepted is applied to a malformed page: extraction must
    # still fail only with RuleError, never with an lxml/re error.
    rng = random.Random(SEED + index)
    page = _html_doc(rng, index)

    parser_cls = type(
        f"RuleParserFuzz{index}",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    parser = parser_cls(ParserConfig())
    for call in (
        lambda: parser.parse_book_info([page]),
        lambda: parser.parse_chapter_content([page], "1"),
    ):
        with contextlib.suppress(RuleError):
            call()
    assert time.perf_counter() - started < CASE_BUDGET


def test_rule_corpus_exercises_both_outcomes() -> None:
    """Guard against a generator that silently stopped exploring."""
    accepted = 0
    for spec in RULE_SPECS:
        try:
            parse_rule(spec)
        except RuleError:
            continue
        accepted += 1

    assert accepted > 0, "no generated rule was accepted; generator is broken"
    assert accepted < CASES, "no generated rule was rejected; generator is broken"


# --------------------------------------------------------------------------- #
# 2. malformed HTML
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "doc",
    HTML_CASES,
    ids=[f"seed={SEED + 10_000 + i}" for i in range(CASES)],
)
def test_malformed_html_never_escapes_rule_error(doc: str) -> None:
    started = time.perf_counter()
    rng = random.Random(SEED + 50_000 + len(doc))

    try:
        tree = parse_page(doc)
    except RuleError:
        # ``preprocess`` parses the same page and must classify it the same
        # way instead of leaking the lxml exception.
        with contextlib.suppress(RuleError):
            preprocess(doc, [Step(lang="py", name="strip_html")])
        assert time.perf_counter() - started < CASE_BUDGET
        return

    for selector in rng.sample(_SELECTORS, 2):
        try:
            nodes = select(tree, selector)
        except RuleError:
            continue
        for node in nodes[:2]:
            for attr in ("text", "href", "html", "inner_text", "a\x00"):
                node_to_str(node, attr)

    try:
        fld = Field.from_dict(
            {
                "selector": rng.choice(_SELECTORS),
                "attr": rng.choice(_ATTRS),
                "regex": rng.choice(_REGEXES),
            }
        )
    except RuleError:
        fld = None
    if fld is not None:
        with contextlib.suppress(RuleError):
            extract(tree, fld)
        with contextlib.suppress(RuleError):
            extract_multi(tree, fld)

    steps = [
        Step(lang="py", name=rng.choice(["strip_html", "normalize_space"])),
        Step(lang="js", code="replace_all(input, 'x', 'y')"),
    ]
    with contextlib.suppress(RuleError):
        preprocess(doc, steps, container=rng.choice([None, "div", "["]))

    assert time.perf_counter() - started < CASE_BUDGET


# --------------------------------------------------------------------------- #
# 3. malformed JSON
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    JSON_CASES,
    ids=[f"seed={SEED + 20_000 + i}" for i in range(CASES)],
)
def test_malformed_json_never_escapes_rule_error(text: str) -> None:
    started = time.perf_counter()
    rng = random.Random(SEED + 60_000 + len(text))

    try:
        doc = parse_json(text)
    except RuleError:
        assert time.perf_counter() - started < CASE_BUDGET
        return

    for path in _JSON_PATHS[: rng.randrange(1, 6)]:
        values = select_json(doc, path)
        for value in values[:3]:
            for attr in ("text", "html", "inner_text", "name", "", "\x00"):
                with contextlib.suppress(RuleError):
                    json_to_str(value, attr)
        fld = Field(selector=path, attr=rng.choice(_ATTRS))
        with contextlib.suppress(RuleError):
            extract(doc, fld, "json")
        with contextlib.suppress(RuleError):
            extract_multi(doc, fld, "json")

    assert time.perf_counter() - started < CASE_BUDGET


# --------------------------------------------------------------------------- #
# 4. selectors and regexes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "selector",
    SELECTOR_CASES,
    ids=[f"seed={SEED + 30_000 + i}" for i in range(CASES)],
)
def test_random_selector_terminates(selector: str) -> None:
    started = time.perf_counter()
    with contextlib.suppress(RuleError):
        select(_TREE, selector)
    assert time.perf_counter() - started < 0.5


_PATHOLOGICAL_SELECTORS: tuple[str, ...] = (
    "(" * 20000,
    "[" * 20000,
    "//a[" * 5000,
    "a[" * 10000,
    "(//a" + ")" * 2000,
    "//*[" + "or " * 2000 + "1]",
    "a:not(" * 2000,
    "a>" * 5000,
    "//div" + "//div" * 5000,
)


@pytest.mark.parametrize(
    "selector",
    _PATHOLOGICAL_SELECTORS,
    ids=[f"len={len(s)}" for s in _PATHOLOGICAL_SELECTORS],
)
def test_pathological_selector_terminates(selector: str) -> None:
    started = time.perf_counter()
    with contextlib.suppress(RuleError):
        select(_TREE, selector)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "pattern",
    REGEX_CASES,
    ids=[f"seed={SEED + 40_000 + i}" for i in range(CASES)],
)
def test_random_regex_is_validated_within_budget(pattern: str) -> None:
    started = time.perf_counter()
    with contextlib.suppress(RuleError):
        Field.from_dict({"selector": "a", "regex": pattern})
    assert time.perf_counter() - started < 1.0


_EVIL_REGEXES: tuple[str, ...] = (
    r"(a+)+$",
    r"(a*)*",
    r"(\d+)+",
    r"(a+){2,}",
    r"(a*b?)+",
    r"((a+))+",
    r"(a|)+",
    r"(a|a)*$",
)


@pytest.mark.parametrize("pattern", _EVIL_REGEXES)
def test_pathological_regex_is_rejected_quickly(pattern: str) -> None:
    started = time.perf_counter()
    with pytest.raises(RuleError, match="catastrophic backtracking"):
        Field.from_dict({"selector": "a", "regex": pattern})
    assert time.perf_counter() - started < 0.5


#: Runs in a separate interpreter: a regex that slips past the static guard
#: would hang the worker, and only a process boundary turns that into a test
#: failure instead of a stuck test run.  It reports ``<accepted> <worst>``.
_REGEX_PROBE = """
import random
import re
import sys
import time
import warnings

sys.path.insert(0, "src")
warnings.simplefilter("ignore")

from novel_downloader.plugins.rule.model import Field, RuleError

rng = random.Random(SEED)
alphabet = "()[]{}*+?|\\\\abc.^$,-09:!=<> \\t"
worst = 0.0
accepted = 0
for _ in range(CASES):
    pattern = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 49)))
    try:
        Field.from_dict({"selector": "a", "regex": pattern})
    except RuleError:
        continue
    accepted += 1
    started = time.perf_counter()
    re.search(pattern, "a" * 48, re.S)
    worst = max(worst, time.perf_counter() - started)

print(accepted, worst)
"""


def test_accepted_random_regexes_cannot_hang_execution() -> None:
    script = _REGEX_PROBE.replace("SEED", repr(SEED)).replace("CASES", repr(CASES))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"regex execution hung in the probe (seed={SEED})")

    elapsed = time.perf_counter() - started
    assert completed.returncode == 0, completed.stderr
    accepted_text, worst_text = completed.stdout.split()
    # If the generator drifted to "everything is rejected", the execution
    # branch above would be dead and this test would prove nothing.
    assert int(accepted_text) > 0
    assert float(worst_text) < 1.0
    assert elapsed < 10.0


# --------------------------------------------------------------------------- #
# 5. extract_multi cap
# --------------------------------------------------------------------------- #
def test_extract_multi_cap_applies_to_html() -> None:
    count = MAX_MULTI_ITEMS + 37
    page = "<div>" + "".join(f"<span>v{i}</span>" for i in range(count)) + "</div>"
    tree = parse_page(page)
    fld = Field(selector="//span", attr="text")

    started = time.perf_counter()
    values = extract_multi(tree, fld)
    elapsed = time.perf_counter() - started

    assert len(values) == MAX_MULTI_ITEMS
    assert values[0] == "v0"
    assert values[-1] == f"v{MAX_MULTI_ITEMS - 1}"
    assert elapsed < CASE_BUDGET


def test_extract_multi_cap_applies_to_json() -> None:
    count = MAX_MULTI_ITEMS + 37
    doc = {"items": [f"v{i}" for i in range(count)]}
    fld = Field(selector="items[*]")

    started = time.perf_counter()
    values = extract_multi(doc, fld, "json")
    elapsed = time.perf_counter() - started

    assert len(values) == MAX_MULTI_ITEMS
    assert values[0] == "v0"
    assert values[-1] == f"v{MAX_MULTI_ITEMS - 1}"
    assert elapsed < CASE_BUDGET
