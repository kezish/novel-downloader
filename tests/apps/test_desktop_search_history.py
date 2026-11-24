#!/usr/bin/env python3
"""
tests.apps.test_desktop_search_history
--------------------------------------

Behavioural tests for the desktop client's recent-search list.

The list is runtime state stored in ``desktop_prefs.json`` under
``search_history``.  It mirrors the Android ``SearchHistoryStore`` semantics:
at most ten terms, newest first, case-insensitive de-duplication, a 200
character cap, atomic writes and a corrupt file that loads as empty instead of
breaking the search screen.

The tests cover the pure codec, the bridge contract, the export/import
exclusion and the embedded page's DOM/JS contract.  No network access and no
windowed interaction happen here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.apps.desktop.webview_app import (
    ApiBridge,
    _i18n_catalog,
    _normalise_search_query,
    _record_search_history,
    _sanitize_search_history,
    _settings_export_document,
    _settings_import_values,
    _stored_search_history,
)

#: ``data-i18n`` keys the chip row depends on.
_HISTORY_KEYS = (
    "Recent searches",
    "Clear recent searches",
    "Search for {query}",
    "Recent searches cleared",
    "Could not clear the recent searches",
    "Could not save the recent searches",
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``_prefs_path()`` at a fresh file inside ``tmp_path``."""
    import novel_downloader.apps.desktop.webview_app as wa

    path = tmp_path / "profile" / "desktop_prefs.json"
    monkeypatch.setattr(wa, "_prefs_path", lambda: path)
    return path


def _queries(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["query"]) for entry in entries]


def _js_block(start_needle: str, end_needle: str) -> str:
    start = APP_HTML.index(start_needle)
    return APP_HTML[start : APP_HTML.index(end_needle, start)]


def _js_handler(needle: str) -> str:
    """Return one event handler: from ``needle`` to the next blank line."""
    start = APP_HTML.index(needle)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


# ---------------------------------------------------------------------------
# codec: normalisation
# ---------------------------------------------------------------------------


def test_queries_are_trimmed_collapsed_and_capped() -> None:
    assert _normalise_search_query("  三体  ") == "三体"
    assert _normalise_search_query("Harry\t\nPotter") == "Harry Potter"
    assert _normalise_search_query("   ") == ""
    assert _normalise_search_query("\t\n") == ""
    assert _normalise_search_query(123) == ""
    assert _normalise_search_query(None) == ""

    overlong = "x" * 500
    assert len(_normalise_search_query(overlong)) == 200


def test_sanitize_orders_newest_first_and_dedupes_case_insensitively() -> None:
    entries = [
        {"query": "  Harry   Potter ", "searched_at": 1_000},
        {"query": "harry potter", "searched_at": 2_000},
        {"query": "三体", "searched_at": 3_000},
    ]

    result = _sanitize_search_history(entries)

    # Newest first; the most recent spelling of the duplicate wins.
    assert _queries(result) == ["三体", "harry potter"]
    assert result[1]["searched_at"] == 2_000


def test_sanitize_keeps_incoming_order_for_equal_timestamps() -> None:
    result = _sanitize_search_history(
        [
            {"query": "first", "searched_at": 5},
            {"query": "second", "searched_at": 5},
        ]
    )

    assert _queries(result) == ["first", "second"]


def test_record_prepends_a_new_term_and_moves_a_repeat_to_the_front() -> None:
    initial: list[dict[str, Any]] = []
    initial = _record_search_history(initial, "三体", 1_000)
    initial = _record_search_history(initial, "球状闪电", 2_000)

    assert _queries(initial) == ["球状闪电", "三体"]

    repeated = _record_search_history(initial, "  三体 ", 3_000)

    assert _queries(repeated) == ["三体", "球状闪电"]
    assert len(repeated) == 2
    assert repeated[0]["searched_at"] == 3_000


def test_record_ignores_blank_queries_and_leaves_the_list_unchanged() -> None:
    entries = _record_search_history([], "三体", 1_000)

    for blank in ("", "   ", "\t\n", None, 42):
        assert _record_search_history(entries, blank, 9_999) == entries


def test_history_is_capped_and_the_oldest_term_is_evicted() -> None:
    entries: list[dict[str, Any]] = []
    for index in range(12):
        entries = _record_search_history(entries, f"term-{index}", 1_000 + index)

    assert len(entries) == 10
    assert _queries(entries)[0] == "term-11"
    assert _queries(entries)[-1] == "term-2"


# ---------------------------------------------------------------------------
# codec: invalid stored values
# ---------------------------------------------------------------------------


def test_non_list_and_non_object_values_load_as_empty() -> None:
    for value in (None, 0, "x", {}, [], [None, 1, "term", {"query": 1}]):
        assert _sanitize_search_history(value) == []


def test_malformed_entries_are_skipped_individually() -> None:
    result = _sanitize_search_history(
        [
            {"query": "", "searched_at": 1},
            {"query": "  ", "searched_at": 2},
            {"query": {"nested": True}, "searched_at": 3},
            {"query": 123, "searched_at": 4},
            {"query": "kept", "searched_at": 5},
        ]
    )

    assert _queries(result) == ["kept"]


def test_timestamps_are_coerced_to_non_negative_integers() -> None:
    result = _sanitize_search_history(
        [
            {"query": "missing"},
            {"query": "negative", "searched_at": -7},
            {"query": "text", "searched_at": "not-a-number"},
            {"query": "numeric-string", "searched_at": "42"},
            {"query": "boolean", "searched_at": True},
            {"query": "float", "searched_at": 1.5},
        ]
    )

    by_query = {str(entry["query"]): entry["searched_at"] for entry in result}
    assert by_query["missing"] == 0
    assert by_query["negative"] == 0
    assert by_query["text"] == 0
    assert by_query["numeric-string"] == 42
    assert by_query["boolean"] == 0
    assert by_query["float"] == 0


def test_stored_history_reads_only_the_search_history_key() -> None:
    assert _stored_search_history({}) == []
    assert _stored_search_history({"search_history": 42}) == []
    assert _queries(
        _stored_search_history(
            {"search_history": [{"query": "三体", "searched_at": 1}]}
        )
    ) == ["三体"]


# ---------------------------------------------------------------------------
# bridge: persistence, dedupe, cap, clear
# ---------------------------------------------------------------------------


def test_recorded_search_survives_a_new_bridge(prefs: Path) -> None:
    recorded = ApiBridge().record_search_history("三体")

    assert recorded["ok"] is True
    assert _queries(recorded["history"]) == ["三体"]

    restored = ApiBridge().get_search_history()

    assert restored["ok"] is True
    assert _queries(restored["history"]) == ["三体"]
    assert isinstance(restored["history"][0]["searched_at"], int)


def test_bridge_dedupes_and_moves_a_repeat_to_the_front(prefs: Path) -> None:
    bridge = ApiBridge()
    bridge.record_search_history("三体")
    bridge.record_search_history("球状闪电")

    repeated = bridge.record_search_history("  三体  ")

    assert _queries(repeated["history"]) == ["三体", "球状闪电"]
    assert _queries(bridge.get_search_history()["history"]) == ["三体", "球状闪电"]


def test_bridge_ignores_a_blank_search_and_writes_nothing(prefs: Path) -> None:
    result = ApiBridge().record_search_history("   \t ")

    assert result["ok"] is True
    assert result["history"] == []
    assert not prefs.exists()


def test_bridge_caps_the_history_at_ten(prefs: Path) -> None:
    bridge = ApiBridge()
    for index in range(12):
        bridge.record_search_history(f"term-{index}")

    history = bridge.get_search_history()["history"]

    assert len(history) == 10
    assert _queries(history)[0] == "term-11"


def test_initial_state_ships_the_recent_searches(prefs: Path) -> None:
    ApiBridge().record_search_history("三体")

    state = ApiBridge().get_initial_state()

    assert _queries(state["search_history"]) == ["三体"]


def test_clear_removes_the_key_and_the_terms(prefs: Path) -> None:
    bridge = ApiBridge()
    bridge.record_search_history("三体")
    bridge.record_search_history("球状闪电")

    cleared = bridge.clear_search_history()

    assert cleared == {"ok": True, "history": [], "error": None}
    assert bridge.get_search_history()["history"] == []
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert "search_history" not in stored


def test_special_characters_survive_the_round_trip(prefs: Path) -> None:
    query = '他说："你好" \\ 路径 😀'

    ApiBridge().record_search_history(query)

    assert _queries(ApiBridge().get_search_history()["history"]) == [query]


def test_the_write_is_atomic_and_leaves_no_temporary_file(prefs: Path) -> None:
    ApiBridge().record_search_history("三体")

    names = sorted(path.name for path in prefs.parent.iterdir())
    assert prefs.name in names
    assert not [name for name in names if name.endswith(".tmp")]
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert _queries(stored["search_history"]) == ["三体"]


# ---------------------------------------------------------------------------
# bridge: corrupt / unwritable storage
# ---------------------------------------------------------------------------


def test_a_corrupt_prefs_file_loads_empty_and_is_repaired(
    prefs: Path,
) -> None:
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('{"theme": "dark", this is not json', encoding="utf-8")

    assert ApiBridge().get_search_history() == {
        "ok": True,
        "history": [],
        "error": None,
    }

    result = ApiBridge().record_search_history("三体")

    assert result["ok"] is True
    assert _queries(result["history"]) == ["三体"]
    # The unreadable bytes are preserved next to the repaired file.
    assert prefs.with_name(prefs.name + ".broken").is_file()
    assert _queries(ApiBridge().get_search_history()["history"]) == ["三体"]


def test_a_wrong_typed_search_history_value_falls_back_to_empty(
    prefs: Path,
) -> None:
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        json.dumps({"theme": "dark", "search_history": {"query": "三体"}}),
        encoding="utf-8",
    )

    assert ApiBridge().get_search_history()["history"] == []


def test_a_failed_write_reports_not_ok_and_keeps_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    target = tmp_path / "prefs-is-a-directory"
    target.mkdir()
    monkeypatch.setattr(wa, "_prefs_path", lambda: target)

    result = wa.ApiBridge().record_search_history("三体")

    assert result["ok"] is False
    assert _queries(result["history"]) == ["三体"]
    assert target.is_dir()
    # The lock sidecar may exist; the write target itself must be untouched.
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "prefs-is-a-directory",
        "prefs-is-a-directory.lock",
    ]


def test_bad_query_types_never_raise(prefs: Path) -> None:
    result = ApiBridge().record_search_history(None)  # type: ignore[arg-type]

    assert result["ok"] is True
    assert result["history"] == []


# ---------------------------------------------------------------------------
# export / import: runtime state never travels
# ---------------------------------------------------------------------------


def test_recent_searches_are_excluded_from_the_export_whitelist() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    assert "search_history" not in wa._SETTINGS_FILE_KEYS

    document = _settings_export_document(
        {
            "theme": "dark",
            "search_history": [{"query": "三体", "searched_at": 1}],
        },
        "1.2.3",
    )

    assert set(document["settings"]) == set(wa._SETTINGS_FILE_KEYS)
    assert "search_history" not in document["settings"]


def test_import_rejects_a_smuggled_search_history_field() -> None:
    document = _settings_export_document({}, "1.2.3")
    document["settings"]["search_history"] = [{"query": "三体", "searched_at": 1}]

    with pytest.raises(ValueError, match="search_history"):
        _settings_import_values(document)


def test_importing_other_preferences_leaves_recent_searches_alone(
    prefs: Path, tmp_path: Path
) -> None:
    ApiBridge().record_search_history("三体")
    source = tmp_path / "import.json"
    source.write_text(json.dumps(_settings_export_document({}, "1.2.3")), "utf-8")

    result = ApiBridge().import_settings(str(source))

    assert result["ok"] is True, result["error"]
    assert _queries(ApiBridge().get_search_history()["history"]) == ["三体"]


# ---------------------------------------------------------------------------
# DOM contract
# ---------------------------------------------------------------------------


def test_the_chip_row_sits_directly_under_the_search_box() -> None:
    box = APP_HTML.index('id="q"')
    row = APP_HTML.index('id="search-history"')
    progress = APP_HTML.index('id="search-progress"')

    assert box < row < progress


def test_the_chip_row_exposes_named_accessible_controls() -> None:
    assert re.search(
        r'id="search-history"[^>]*class="search-history"[^>]*hidden',
        APP_HTML,
    ) or re.search(
        r'class="search-history"[^>]*id="search-history"[^>]*hidden',
        APP_HTML,
    )
    assert re.search(
        r'id="search-history-chips"[^>]*role="group"[^>]*'
        r'aria-labelledby="search-history-label"',
        APP_HTML,
    )
    assert re.search(
        r'id="search-history-label"[^>]*data-i18n="Recent searches"',
        APP_HTML,
    )
    assert re.search(r'<button[^>]*id="btn-clear-history"', APP_HTML)
    assert 'data-i18n="Clear recent searches"' in APP_HTML


def test_chips_are_buttons_with_a_query_and_an_accessible_name() -> None:
    renderer = _js_block(
        "function renderSearchHistory()", "function applySearchHistory"
    )

    assert 'type="button"' in renderer
    assert 'class="history-chip"' in renderer
    assert "data-history-query=" in renderer
    assert 'aria-label="${esc(t("Search for {query}")' in renderer
    assert "box.hidden = searchHistory.length === 0;" in renderer


def test_clicking_a_chip_fills_the_box_and_searches() -> None:
    click = _js_handler('$("#search-history-chips").addEventListener("click"')

    assert 'closest("[data-history-query]")' in click
    assert '$("#q").value = chip.dataset.historyQuery;' in click
    assert '$("#q").focus();' in click
    assert "doSearch();" in click


def test_the_clear_button_empties_the_stored_history_and_the_row() -> None:
    clear = _js_handler('$("#btn-clear-history").onclick')

    assert 'call("clear_search_history")' in clear
    assert "applySearchHistory([]);" in clear
    # The button disappears with the row; focus returns to the search box.
    assert '$("#q").focus();' in clear
    assert 'toast(t("Recent searches cleared"), "ok");' in clear
    assert 't("Could not clear the recent searches")' in clear


def test_a_search_is_remembered_only_after_the_backend_started_it() -> None:
    searcher = _js_block("async function doSearch()", "/* match-telemetry-start */")

    started = searcher.index("if (!job || !job.search_id)")
    recorded = searcher.index("rememberSearch(kw);")
    assert started < recorded
    # The early blank guard still comes first.
    assert searcher.index("if (!kw) return;") < recorded
    remember = _js_block("function rememberSearch(query)", "/* Each chip is a native")
    assert 'api("record_search_history", query)' in remember
    assert "applySearchHistory(res.history)" in remember


def test_boot_paints_the_stored_history_without_starting_a_search() -> None:
    boot = _js_block(
        "function boot()", "if (window.pywebview && window.pywebview.api) boot();"
    )

    assert "applySearchHistory(state.search_history);" in boot
    assert "applyStoredSearchFilters(state.search_filters);" in boot


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------


def test_history_copy_is_published_to_the_front_end() -> None:
    catalog = _i18n_catalog()

    for key in _HISTORY_KEYS:
        assert key in catalog, key
        assert catalog[key] and catalog[key] != "", key

    for key in ("Recent searches", "Clear recent searches"):
        assert f'data-i18n="{key}"' in APP_HTML, key
    for key in ("Search for {query}", "Recent searches cleared"):
        assert f't("{key}")' in APP_HTML, key


def test_history_copy_has_a_chinese_translation() -> None:
    po = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "novel_downloader"
        / "locales"
        / "zh_CN"
        / "LC_MESSAGES"
        / "messages.po"
    ).read_text(encoding="utf-8")

    for key in _HISTORY_KEYS:
        assert f'msgid "{key}"' in po, key
        block = po.split(f'msgid "{key}"', 1)[1].split("\n\n", 1)[0]
        assert 'msgstr ""' not in block, f"{key} is untranslated"
