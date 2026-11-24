#!/usr/bin/env python3
"""Boundary tests for JSON-mode selectors (dot paths) in the rule engine.

Covers the contract documented in ``docs/guide/rule-dsl.md``:

* a selector is a dot path (leading ``$``/``.`` optional; each segment carries
  zero or more ``[n]``/``[*]`` suffixes, so ``a[0][1]`` is valid);
* a trailing array is expanded into its elements, so each field sees one
  element at a time;
* ``attr`` reads the value itself (``text``/``inner_text``/``html``) or a key
  of the matched object;
* numbers/booleans are stringified, ``null`` and missing values render empty,
  which lets ``fallback`` / ``default`` take over;
* a body that is not the declared format fails loudly (JSON) or silently
  matches nothing (HTML), never with a raw ``RecursionError``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

import pytest

from novel_downloader.plugins.rule.engine import (
    MAX_MULTI_ITEMS,
    extract,
    extract_multi,
    json_to_str,
    parse_json,
    parse_page,
    select,
    select_json,
)
from novel_downloader.plugins.rule.model import Field, Rule, RuleError, parse_rule
from novel_downloader.plugins.rule.parser import RuleParser
from novel_downloader.plugins.rule.searcher import RuleSearcher
from novel_downloader.schemas import ParserConfig

# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
BOOK_DOC: dict[str, Any] = {
    "code": 0,
    "data": {
        "list": [
            {"title": "斗破苍穹", "bookId": 11, "tags": ["玄幻"], "author": "天蚕土豆"},
            {"title": "武动乾坤", "bookId": 22, "tags": [], "author": "天蚕土豆"},
        ],
        "total": 2,
    },
}

SEARCH_RULE: dict[str, Any] = {
    "site_key": "demo",
    "base_url": "https://demo.example",
    "search": {
        "url": "https://demo.example/search?q={keyword}",
        "item": "data.list",
        "book_id_regex": r"/book/(\d+)",
        "fields": {
            "title": {"selector": "name", "attr": "text"},
            "url": {"selector": "url", "attr": "text"},
        },
    },
    "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
}


def json_search_rule() -> Rule:
    """The same search rule, but reading a JSON response."""
    data = json.loads(json.dumps(SEARCH_RULE))
    data["search"]["format"] = "json"
    return parse_rule(data)


def make_searcher(rule: Rule) -> RuleSearcher:
    cls = type(
        f"RuleSearcher_{rule.site_key}",
        (RuleSearcher,),
        {
            "_rule": rule,
            "site_name": rule.site_key,
            "BASE_URL": rule.base_url,
            "priority": rule.search.priority if rule.search else rule.priority,
        },
    )
    return cls(session=None)  # type: ignore[arg-type]


def search_rule_with(**search_overrides: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(SEARCH_RULE))
    data["search"].update(search_overrides)
    return data


# --------------------------------------------------------------------------- #
# 1. missing keys and type mismatches
# --------------------------------------------------------------------------- #
def test_missing_key_returns_no_match() -> None:
    assert select_json(BOOK_DOC, "data.nope") == []
    assert select_json(BOOK_DOC, "data.list[*].nope") == []
    assert extract(BOOK_DOC, Field(selector="data.nope"), "json") == ""


@pytest.mark.parametrize("path", ["missing", "s[0]", "s[*]", "s.nested", "n.x", "b.x"])
def test_scalars_are_never_descended_or_indexed(path: str) -> None:
    """A string/int/bool is not a container: every deeper step is a no-match."""
    doc = {"s": "text", "n": 3, "b": True}
    assert select_json(doc, path) == []


def test_null_value_matches_but_renders_empty() -> None:
    doc = {"a": None, "b": {"c": None}}

    assert select_json(doc, "a") == [None]
    assert select_json(doc, "b.c") == [None]
    assert select_json(doc, "a.deeper") == []
    assert extract(doc, Field(selector="a"), "json") == ""
    # An empty value is what makes fallback/default take over.
    fld = Field(selector="a", default="dflt")
    assert extract(doc, fld, "json") == "dflt"
    inherited = Field.from_dict({"selector": "a", "fallback": ["b.c"]})
    assert extract(doc, inherited, "json") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, "42"),
        (0, "0"),
        (-3, "-3"),
        (3.5, "3.5"),
        (2**63, "9223372036854775808"),
        (True, "true"),
        (False, "false"),
        ("  padded  ", "padded"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_scalar_rendering(value: Any, expected: str) -> None:
    assert json_to_str(value, "text") == expected
    assert extract({"v": value}, Field(selector="v"), "json") == expected


def test_string_cannot_be_used_as_a_list() -> None:
    doc = {"a": "abcdef"}

    assert select_json(doc, "a[0]") == []
    assert select_json(doc, "a[-1]") == []
    assert select_json(doc, "a[*]") == []
    assert extract(doc, Field(selector="a[0]", default="x"), "json") == "x"


def test_numeric_object_keys_are_strings() -> None:
    doc = {"0": "zero", "list": {"1": "one"}}

    assert select_json(doc, "0") == ["zero"]
    assert select_json(doc, "list.1") == ["one"]


# --------------------------------------------------------------------------- #
# 2. array indexes, negative indexes and wildcards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data.list[0].title", ["斗破苍穹"]),
        ("data.list[1].title", ["武动乾坤"]),
        ("data.list[-1].title", ["武动乾坤"]),
        ("data.list[-2].title", ["斗破苍穹"]),
        ("data.list[2]", []),
        ("data.list[-3]", []),
        ("data.list[99]", []),
        ("data.list[0][0]", []),
    ],
)
def test_index_bounds(path: str, expected: list[Any]) -> None:
    assert select_json(BOOK_DOC, path) == expected


def test_index_on_empty_array_or_object_yields_nothing() -> None:
    doc = {"empty": [], "obj": {"a": 1}, "s": "x"}

    assert select_json(doc, "empty[0]") == []
    assert select_json(doc, "empty[-1]") == []
    assert select_json(doc, "obj[0]") == []
    assert select_json(doc, "s[0]") == []


@pytest.mark.parametrize("suffix", ["[x]", "[1.5]", "[]", "[99999999999999999999]"])
def test_non_numeric_and_huge_indexes_yield_nothing(suffix: str) -> None:
    doc = {"a": [10, 20]}

    assert select_json(doc, f"a{suffix}") == []
    assert extract(doc, Field(selector=f"a{suffix}", default="d"), "json") == "d"


def test_chained_indexes_descend_one_level_per_suffix() -> None:
    doc = {"matrix": [[1, 2], [3]], "deep": [[{"x": 7}]]}

    assert select_json(doc, "matrix[0][1]") == [2]
    assert select_json(doc, "matrix[1][0]") == [3]
    assert select_json(doc, "matrix[0][0]") == [1]
    assert select_json(doc, "matrix[5][0]") == []
    assert select_json(doc, "matrix[0][9]") == []
    assert select_json(doc, "deep[0][0].x") == [7]


def test_index_applies_to_every_parent_when_not_last() -> None:
    doc = {"a": [[1, 2], [3]], "groups": [{"ids": [10, 11]}, {"ids": [20]}]}

    assert select_json(doc, "a[*][0]") == [1, 3]
    assert select_json(doc, "groups.ids[0]") == [10, 20]
    assert select_json(doc, "groups[*].ids[-1]") == [11, 20]


def test_wildcard_aggregates_lists_and_object_values() -> None:
    doc = {
        "list": [{"n": 1}, {"n": 2}],
        "obj": {"first": "a", "second": "b"},
    }

    assert select_json(doc, "list[*]") == [{"n": 1}, {"n": 2}]
    assert select_json(doc, "obj[*]") == ["a", "b"]  # insertion order
    assert select_json(doc, "list[*].n") == [1, 2]
    assert select_json(doc, "obj[*].missing") == []


def test_wildcard_is_not_expanded_twice() -> None:
    doc = {"nested": [[1, 2], [3, 4]]}

    # ``[*]`` is the last segment, so no second trailing expansion happens.
    assert select_json(doc, "nested[*]") == [[1, 2], [3, 4]]
    assert select_json(doc, "nested") == [[1, 2], [3, 4]]
    assert select_json(doc, "nested[*][*]") == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "path",
    ["empty[*]", "obj[*]", "s[*]", "n[*]", "null[*]", "flag[*]"],
)
def test_wildcard_on_non_containers_yields_nothing(path: str) -> None:
    doc = {"empty": [], "obj": {}, "s": "x", "n": 1, "null": None, "flag": True}

    assert select_json(doc, path) == []


def test_multi_flag_collects_every_match() -> None:
    fld = Field(selector="data.list[*].title", multi=True)

    assert extract_multi(BOOK_DOC, fld, "json") == ["斗破苍穹", "武动乾坤"]
    # ``extract`` still reports the first non-empty match, with or without
    # ``multi``: only ``book.fields.tags`` is collected by the parser.
    assert extract(BOOK_DOC, fld, "json") == "斗破苍穹"
    assert extract(BOOK_DOC, Field(selector="data.list[*].title"), "json") == "斗破苍穹"


def test_fan_out_skips_elements_without_the_key() -> None:
    doc = {"items": [{"n": "a"}, {"other": 1}, "scalar", {"n": ""}, {"n": "b"}]}

    assert select_json(doc, "items.n") == ["a", "", "b"]
    fld = Field(selector="items.n")
    assert extract_multi(doc, fld, "json") == ["a", "b"]
    assert extract(doc, fld, "json") == "a"


# --------------------------------------------------------------------------- #
# 3. nested containers, empty containers
# --------------------------------------------------------------------------- #
def test_nested_object_and_array_chain() -> None:
    doc = {"a": {"b": {"c": [{"d": 1}, {"d": 2}]}}, "x": [{"y": [{"z": "deep"}]}]}

    assert select_json(doc, "a.b.c.d") == [1, 2]
    assert select_json(doc, "a.b.c[1].d") == [2]
    assert select_json(doc, "x.y.z") == ["deep"]
    assert select_json(doc, "x[0].y[0].z") == ["deep"]


def test_empty_array_and_empty_object() -> None:
    doc = {"arr": [], "obj": {}, "nested": {"arr": [{"k": []}]}}

    assert select_json(doc, "arr") == []
    assert select_json(doc, "arr[0]") == []
    assert select_json(doc, "arr[*]") == []
    assert select_json(doc, "obj") == [{}]
    assert select_json(doc, "obj[*]") == []
    assert select_json(doc, "obj.missing") == []
    assert select_json(doc, "nested.arr.k") == []

    assert json_to_str([], "text") == ""
    assert json_to_str({}, "text") == "{}"


def test_empty_root_document() -> None:
    assert select_json({}, "") == [{}]
    assert select_json({}, "a") == []
    assert select_json([], "") == [[]]
    assert select_json([], "a") == []
    assert extract({}, Field(selector="a", default="d"), "json") == "d"


# --------------------------------------------------------------------------- #
# 4. key names containing dots, brackets and Chinese
# --------------------------------------------------------------------------- #
def test_chinese_keys_resolve() -> None:
    doc = {"书名": "斗破苍穹", "详情": {"作者": "天蚕土豆"}}

    assert select_json(doc, "书名") == ["斗破苍穹"]
    assert select_json(doc, "详情.作者") == ["天蚕土豆"]
    assert extract(doc, Field(selector="详情.作者"), "json") == "天蚕土豆"


def test_dotted_key_cannot_be_addressed_but_does_not_crash() -> None:
    """``.`` is the path separator, so a literal ``a.b`` key is unreachable."""
    doc = {"a.b": 1, "中.文": 2, "a": {"b": 3}}

    assert select_json(doc, "a.b") == [3]  # nested lookup wins, never the literal key
    assert select_json(doc, "中.文") == []
    assert extract(doc, Field(selector="中.文", default="d"), "json") == "d"


def test_bracket_key_is_read_as_an_index_for_a_dict() -> None:
    doc = {"x[y]": 1, "x": {"y": 2}}

    # The suffix is parsed as an index, so it never reaches the literal key.
    assert select_json(doc, "x[y]") == []
    assert select_json(doc, "x.y") == [2]


def test_non_numeric_bracket_suffix_yields_nothing() -> None:
    doc = {"a": {"b": 1}}

    assert select_json(doc, "a[b]") == []
    assert select_json(doc, "a[0]") == []


def test_literal_key_with_a_stray_closing_bracket_still_resolves() -> None:
    # ``a]`` does not match the segment grammar, so the literal key is used.
    assert select_json({"a]": "v"}, "a]") == ["v"]
    assert select_json({"a[b": "v"}, "a[b") == ["v"]


@pytest.mark.parametrize("path", ["a.b", "x[y]", "中.文", "$['a.b']", "a..b", ".a.b."])
def test_awkward_paths_never_raise(path: str) -> None:
    doc = {"a": {"b": [1]}, "x[y]": 1, "中.文": 2}

    # No exception is the contract here; the result is either a miss or the
    # legal nested lookup.
    assert isinstance(select_json(doc, path), list)


# --------------------------------------------------------------------------- #
# 5. root shapes and format mismatches
# --------------------------------------------------------------------------- #
def test_root_array_document() -> None:
    raw = '[{"name": "a"}, {"name": "b"}]'
    doc = parse_page(raw, "json")

    assert doc == [{"name": "a"}, {"name": "b"}]
    assert select_json(doc, "name") == ["a", "b"]
    assert select_json(doc, "[0].name") == ["a"]
    assert select_json(doc, "[*].name") == ["a", "b"]


def test_root_scalar_document() -> None:
    assert parse_page('"just text"', "json") == "just text"
    assert parse_page("null", "json") is None
    assert select_json("just text", "x") == []
    assert extract("just text", Field(selector=""), "json") == "just text"
    assert extract(None, Field(selector="a", default="d"), "json") == "d"


def test_json_body_declared_as_html_yields_no_matches() -> None:
    """A JSON body parsed as HTML is harmless: selectors simply match nothing."""
    raw = '{"data": {"title": "x"}}'
    tree = parse_page(raw, "html")

    assert select(tree, "div.item", "html") == []
    assert extract(tree, Field(selector="div.item"), "html") == ""
    assert extract(tree, Field(selector="div.item", default="d"), "html") == "d"


def test_html_body_declared_as_json_raises() -> None:
    with pytest.raises(RuleError, match="unparseable JSON response"):
        parse_page("<html><body><div class='item'>x</div></body></html>", "json")


def test_json_rule_over_an_html_page_raises() -> None:
    searcher = make_searcher(json_search_rule())

    with pytest.raises(RuleError, match="unparseable JSON response"):
        searcher._parse_html("<html><body><div class='item'>x</div></body></html>")


def test_html_rule_over_a_json_page_finds_nothing() -> None:
    searcher = make_searcher(parse_rule(SEARCH_RULE))

    assert searcher._parse_html('{"data": {"list": [{"name": "x"}]}}') == []


@pytest.mark.parametrize(
    "json_body",
    [["kw"], "kw", 1, True],
)
def test_json_body_must_be_an_object(json_body: Any) -> None:
    data = search_rule_with(json_body=json_body)

    with pytest.raises(RuleError, match="json_body must be an object"):
        parse_rule(data)


def test_json_body_nesting_is_bounded() -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(40):
        cursor["child"] = {}
        cursor = cursor["child"]

    with pytest.raises(RuleError, match="nesting deeper than 32 levels"):
        parse_rule(search_rule_with(json_body=nested))


# --------------------------------------------------------------------------- #
# 6. large documents: coverage and truncation
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def large_payload() -> tuple[str, Any]:
    """A ~1 MiB API response: 4000 items with a long summary each."""
    items = [
        {
            "title": f"第{i}章",
            "url": f"https://api.example/read/{i}",
            "summary": "x" * 200,
        }
        for i in range(4000)
    ]
    raw = json.dumps({"data": {"items": items}}, ensure_ascii=False)
    return raw, json.loads(raw)


def test_large_json_parses_and_selects_within_budget(
    large_payload: tuple[str, Any],
) -> None:
    raw, expected = large_payload
    assert len(raw) > 900_000  # near the 1 MiB rule/response ballpark

    started = time.perf_counter()
    doc = parse_json(raw)
    titles = select_json(doc, "data.items[*].title")
    elapsed = time.perf_counter() - started

    assert doc == expected
    assert len(titles) == 4000
    assert titles[0] == "第0章"
    assert titles[-1] == "第3999章"
    # Generous bound: this only has to catch a quadratic/accidental-copy blowup.
    assert elapsed < 5.0


def test_large_json_multi_extraction_is_truncated(
    large_payload: tuple[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    _, doc = large_payload
    fld = Field(selector="data.items[*].title", multi=True)

    with caplog.at_level(
        logging.WARNING, logger="novel_downloader.plugins.rule.engine"
    ):
        values = extract_multi(doc, fld, "json")

    assert len(values) == MAX_MULTI_ITEMS == 1000
    assert values[0] == "第0章"
    assert values[-1] == "第999章"
    assert "truncating" in caplog.text


# --------------------------------------------------------------------------- #
# 7. fields sharing a path, fallback chains
# --------------------------------------------------------------------------- #
def test_two_fields_share_a_path_independently() -> None:
    doc = {"data": {"title": "  斗破苍穹 (上) ", "bookId": 11}}

    plain = Field(selector="data.title")
    grouped = Field(selector="data.title", regex=r"\((\S+)\)", group=1)
    keyed = Field(selector="data", attr="bookId")

    assert extract(doc, plain, "json") == "斗破苍穹 (上)"
    assert extract(doc, grouped, "json") == "上"
    assert extract(doc, keyed, "json") == "11"
    # Re-reading the same path must yield the same value (no shared state).
    assert extract(doc, plain, "json") == "斗破苍穹 (上)"


def test_fallback_chain_takes_the_first_non_empty_value() -> None:
    doc = {"a": {"primary": "", "secondary": "   ", "third": "命中"}}

    fld = Field.from_dict(
        {
            "selector": "a.primary",
            "fallback": ["a.missing", "a.secondary", "a.third", "a.never"],
        }
    )
    assert extract(doc, fld, "json") == "命中"


def test_fallback_string_inherits_attr() -> None:
    doc = {"a": {"href": "/x"}, "b": {"href": "/y"}}

    fld = Field.from_dict({"selector": "a", "attr": "href", "fallback": ["b"]})
    assert extract(doc, fld, "json") == "/x"
    # ``a.missing`` fails, so the fallback runs with the *inherited* attr.
    fld = Field.from_dict({"selector": "missing", "attr": "href", "fallback": ["b"]})
    assert extract(doc, fld, "json") == "/y"


def test_fallback_object_is_independent() -> None:
    doc = {"b": {"href": "/y"}}

    fld = Field.from_dict(
        {
            "selector": "missing",
            "attr": "href",
            "fallback": [{"selector": "b", "attr": "text"}],
        }
    )
    assert extract(doc, fld, "json") == '{"href": "/y"}'


def test_whitespace_only_value_falls_through_to_fallback() -> None:
    doc = {"main": "   \n ", "alt": "value"}

    fld = Field.from_dict({"selector": "main", "fallback": ["alt"]})
    assert extract(doc, fld, "json") == "value"


def test_default_is_used_only_after_the_whole_chain_fails() -> None:
    doc = {"present": "x"}

    fld = Field.from_dict(
        {
            "selector": "missing",
            "default": "outer-default",
            "fallback": [{"selector": "also-missing", "default": "inner-default"}],
        }
    )
    assert extract(doc, fld, "json") == "outer-default"
    # A fallback that does match wins over the main field's default.
    fld_hit = Field.from_dict(
        {"selector": "missing", "default": "outer", "fallback": ["present"]}
    )
    assert extract(doc, fld_hit, "json") == "x"


# --------------------------------------------------------------------------- #
# 8. type conversion table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "attr", "expected"),
    [
        (None, "text", ""),
        (None, "href", ""),
        (True, "text", "true"),
        (False, "inner_text", "false"),
        (7, "html", "7"),
        (1.25, "text", "1.25"),
        ("  x  ", "text", "x"),
        ({"a": 1}, "a", "1"),
        ({"a": 1}, "missing", ""),
        ({"a": {"b": 2}}, "text", '{"a": {"b": 2}}'),
        ({"a": {"b": 2}}, "inner_text", '{"a": {"b": 2}}'),
        ({"a": {"b": 2}}, "html", '{"a": {"b": 2}}'),
        ({"a": [1, 2]}, "a", "12"),
        ([], "text", ""),
        ([], "inner_text", ""),
        ([], "href", "[]"),
        (["x", "y"], "text", "xy"),
        (["x", "y"], "inner_text", "x\ny"),
        ([None, "", " ", "x"], "text", "x"),
        ([{"a": 1}], "text", '{"a": 1}'),  # no expansion inside json_to_str
    ],
)
def test_json_to_str_conversion_table(value: Any, attr: str, expected: str) -> None:
    assert json_to_str(value, attr) == expected


def test_object_serialization_keeps_chinese_readable() -> None:
    assert json_to_str({"中": "文"}, "text") == '{"中": "文"}'


def test_object_key_lookup_recurses_into_the_value() -> None:
    doc = {"meta": {"tags": ["玄幻", "热血"], "count": 2}}

    assert extract(doc, Field(selector="meta", attr="tags"), "json") == "玄幻热血"
    assert extract(doc, Field(selector="meta", attr="count"), "json") == "2"
    assert extract(doc, Field(selector="meta", attr="nope"), "json") == ""


def test_extract_multi_drops_empty_values() -> None:
    doc = {"tags": ["玄幻", "", None, "  ", "热血"]}
    fld = Field(selector="tags[*]", multi=True)

    assert extract_multi(doc, fld, "json") == ["玄幻", "热血"]
    assert extract(doc, fld, "json") == "玄幻"


def test_json_book_tags_are_collected_with_multi() -> None:
    """End to end: the only place the parser honours ``multi`` is ``tags``."""
    rule = parse_rule(
        {
            "site_key": "demo-json-book",
            "base_url": "https://api.example",
            "book": {
                "url": "{base_url}/book/{book_id}",
                "format": "json",
                "fields": {
                    "book_name": {"selector": "data.title"},
                    "summary": {"selector": "data.intro"},
                    "tags": {"selector": "data.tags[*]", "multi": True},
                },
                "chapters": {
                    "item": "data.chapters",
                    "title": {"selector": "name"},
                    "url": {"selector": "url"},
                },
            },
        }
    )
    payload = json.dumps(
        {
            "data": {
                "title": "斗破苍穹",
                "intro": "简介",
                "tags": ["玄幻", "热血", ""],
                "chapters": [
                    {"name": "第一章", "url": "https://api.example/read/1"},
                    {"name": "第二章", "url": "https://api.example/read/2"},
                ],
            }
        },
        ensure_ascii=False,
    )
    cls = type(
        "RuleParser_json_book",
        (RuleParser,),
        {"_rule": rule, "site_name": rule.site_key},
    )
    parser = cast(RuleParser, cls(ParserConfig()))

    info = parser.parse_book_info([payload])

    assert info is not None
    assert info["book_name"] == "斗破苍穹"
    assert info["summary"] == "简介"
    assert info["tags"] == ["玄幻", "热血"]
    assert [c["title"] for c in info["volumes"][0]["chapters"]] == ["第一章", "第二章"]


# --------------------------------------------------------------------------- #
# 9. robustness: pathological nesting must not leak RecursionError
# --------------------------------------------------------------------------- #
def test_deeply_nested_json_raises_rule_error() -> None:
    depth = 10_000
    raw = "[" * depth + "]" * depth

    with pytest.raises(RuleError, match="nested too deeply to decode"):
        parse_json(raw)


def test_deeply_nested_object_is_not_serialized() -> None:
    """``json.loads`` accepts deeper documents than ``json.dumps`` can emit."""
    root: dict[str, Any] = {}
    cursor = root
    for _ in range(10_000):
        cursor["child"] = {}
        cursor = cursor["child"]

    with pytest.raises(RuleError, match="nested too deeply to serialize"):
        json_to_str(root, "text")


def test_deeply_nested_document_still_matches_by_path() -> None:
    """Walking a path is iterative: depth only matters when dumping the value."""
    doc: Any = {"leaf": "v"}
    for _ in range(500):
        doc = {"child": doc}

    path = ".".join(["child"] * 500) + ".leaf"
    assert select_json(doc, path) == ["v"]
