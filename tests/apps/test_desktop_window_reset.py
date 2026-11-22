#!/usr/bin/env python3
"""
tests.apps.test_desktop_window_reset
------------------------------------

Behavioural tests for the Settings-page "Reset window position and size"
action.

The action is one bridge method, :meth:`ApiBridge.reset_window_state`, plus a
button in the embedded page.  It clears only the five ``window_*`` keys from
``desktop_prefs.json``; every other preference stays, the legacy
``window_state.json`` is deliberately left in place, and the current window is
not moved (the default geometry applies on the next start).

Nothing here opens a window: the bridge is constructed directly and the JS
contract is checked against the embedded page source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.desktop import window_state as wstate
from novel_downloader.apps.desktop.web_assets import APP_HTML

#: Keys the reset must delete, and only these.
_WINDOW_KEYS = (
    "window_width",
    "window_height",
    "window_x",
    "window_y",
    "window_maximized",
)

#: Front-end strings the new card depends on.
_RESET_KEYS = (
    "Window",
    "The window reopens at its last size and position. Resetting clears that "
    "saved geometry; the current window stays as it is and the next start "
    "uses the default size.",
    "Reset window position and size",
    "Window position and size reset. It takes effect the next time the app starts.",
    "Could not reset the window position and size",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _prefs_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``NOVEL_MUSIC_HOME`` at a scratch directory and return the file."""
    import novel_downloader.apps.desktop.webview_app as wa

    home = tmp_path / "profile"
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(home))
    return home / wa._PREFS_FILENAME


def _write_prefs(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_prefs(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _js_handler(marker: str) -> str:
    """Return the event handler starting at ``marker`` (to the next one)."""
    start = APP_HTML.index(marker)
    end = APP_HTML.find("\n\n$(", start)
    if end == -1:
        end = APP_HTML.index("\n\n</script>", start)
    return APP_HTML[start:end]


def _settings_document(**overrides: Any) -> dict[str, Any]:
    """A valid export document with optional per-test overrides."""
    settings: dict[str, Any] = {
        "theme": "dark",
        "auto_check_updates": False,
        "auto_download_updates": False,
        "auto_install_on_exit": False,
        "search_filters": {},
        "search_sort": {"sort": "quality", "order": "desc"},
    }
    settings.update(overrides)
    return {
        "app": "novel-downloader",
        "kind": "desktop-preferences",
        "format_version": 1,
        "app_version": "1.36.0",
        "exported_at": "2026-11-16T00:00:00Z",
        "settings": settings,
    }


# ---------------------------------------------------------------------------
# the key inventory
# ---------------------------------------------------------------------------


def test_the_window_key_list_is_exactly_the_five_geometry_keys() -> None:
    """The reset deletes this tuple, so it must not gain unrelated keys."""
    assert wstate.WINDOW_PREFS_KEYS == _WINDOW_KEYS


# ---------------------------------------------------------------------------
# bridge: reset_window_state
# ---------------------------------------------------------------------------


def test_reset_deletes_only_the_window_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)
    _write_prefs(
        prefs,
        {
            "window_width": 1600,
            "window_height": 900,
            "window_x": 120,
            "window_y": 80,
            "window_maximized": True,
            "theme": "dark",
            "auto_check_updates": False,
            "search_filters": {"min_words": 1000},
            "from_a_future_build": {"nested": [1, 2]},
        },
    )
    bridge = wa.ApiBridge()

    result = bridge.reset_window_state()

    assert result == {"ok": True, "error": None}
    assert _read_prefs(prefs) == {
        "theme": "dark",
        "auto_check_updates": False,
        "search_filters": {"min_words": 1000},
        "from_a_future_build": {"nested": [1, 2]},
    }
    assert bridge.window_state_save_suppressed is True


def test_reset_is_idempotent_and_does_not_rewrite_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second click succeeds and leaves the already-clean file untouched."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "dark"})
    before = prefs.read_bytes()
    bridge = wa.ApiBridge()

    assert bridge.reset_window_state() == {"ok": True, "error": None}
    assert bridge.reset_window_state() == {"ok": True, "error": None}

    assert prefs.read_bytes() == before
    assert list(prefs.parent.glob("*.broken")) == []


def test_reset_without_a_prefs_file_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to clear is a success, and no empty file is created."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)

    result = wa.ApiBridge().reset_window_state()

    assert result == {"ok": True, "error": None}
    assert not prefs.exists()


@pytest.mark.parametrize("payload", ["{not json", "[1, 2, 3]", "null"])
def test_reset_tolerates_a_damaged_prefs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
) -> None:
    """A corrupt file is quarantined, not treated as a crash."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(payload, encoding="utf-8")

    result = wa.ApiBridge().reset_window_state()

    assert result == {"ok": True, "error": None}
    broken = prefs.with_name(prefs.name + ".broken")
    assert broken.read_text(encoding="utf-8") == payload
    assert not prefs.exists()


def test_reset_leaves_the_legacy_window_state_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The upgrade fallback is intentionally not part of the reset."""
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.music.paths import music_data_dir

    prefs = _prefs_file(monkeypatch, tmp_path)
    _write_prefs(prefs, {"window_width": 1400, "theme": "dark"})
    legacy = music_data_dir() / wstate.WINDOW_STATE_FILENAME
    legacy.write_text('{"width": 1000, "height": 700}', encoding="utf-8")
    before = legacy.read_bytes()

    assert wa.ApiBridge().reset_window_state()["ok"] is True

    assert legacy.read_bytes() == before


def test_reset_reports_an_unwritable_profile_and_keeps_saving_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed delete must not pretend the geometry is gone."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs_file(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(blocker / "music"))
    bridge = wa.ApiBridge()

    result = bridge.reset_window_state()

    assert result["ok"] is False
    assert result["error"] == wa.t("Could not reset the window position and size")
    assert bridge.window_state_save_suppressed is False


# ---------------------------------------------------------------------------
# front end: the button and its handler
# ---------------------------------------------------------------------------


def test_the_settings_page_has_a_type_button_reset_control() -> None:
    start = APP_HTML.index('id="btn-reset-window"')
    tag = APP_HTML[APP_HTML.rindex("<button", 0, start) : APP_HTML.index(">", start)]
    assert 'type="button"' in tag
    assert 'class="btn btn-ghost btn-sm"' in tag
    block = APP_HTML[start : APP_HTML.index("</button>", start)]
    assert 'data-i18n="Reset window position and size"' in block


def test_the_reset_handler_calls_the_bridge_and_reports_both_outcomes() -> None:
    handler = _js_handler('$("#btn-reset-window").onclick')

    assert 'call("reset_window_state")' in handler
    assert "r.ok !== true" in handler
    assert 't("Could not reset the window position and size")' in handler
    assert '"err"' in handler
    assert '"Window position and size reset.' in handler
    # The current window is not moved: no immediate apply is promised.
    assert "resizeTo" not in handler
    assert "moveTo" not in handler
    assert "location.reload" not in handler


# ---------------------------------------------------------------------------
# i18n coverage
# ---------------------------------------------------------------------------


def test_every_reset_string_is_in_the_bridge_catalogue() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    catalog = wa._i18n_catalog()
    missing = [key for key in _RESET_KEYS if key not in catalog]
    assert not missing, missing


def test_the_card_uses_the_catalogue_keys() -> None:
    for key in ("Window", "Reset window position and size"):
        assert f'data-i18n="{key}"' in APP_HTML
    assert f'data-i18n="{_RESET_KEYS[1]}"' in APP_HTML


# ---------------------------------------------------------------------------
# export / import: window geometry stays device-local
# ---------------------------------------------------------------------------


def test_the_export_whitelist_still_omits_window_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)
    _write_prefs(
        prefs,
        {
            "theme": "dark",
            "window_width": 1600,
            "window_height": 900,
            "window_x": 10,
            "window_y": 20,
            "window_maximized": True,
        },
    )
    target = tmp_path / "export.json"

    result = wa.ApiBridge().export_settings(str(target))

    assert result["ok"] is True, result["error"]
    document = json.loads(target.read_text(encoding="utf-8"))
    assert set(document["settings"]) == set(wa._SETTINGS_FILE_KEYS)
    assert set(_WINDOW_KEYS).isdisjoint(document["settings"])


def test_an_import_cannot_smuggle_in_a_window_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = _prefs_file(monkeypatch, tmp_path)
    _write_prefs(prefs, {"theme": "light"})
    before = prefs.read_bytes()
    document = _settings_document(window_width=999)
    source = tmp_path / "import.json"
    source.write_text(json.dumps(document), encoding="utf-8")

    result = wa.ApiBridge().import_settings(str(source))

    assert result["ok"] is False
    assert "window_width" in str(result["error"])
    assert prefs.read_bytes() == before
