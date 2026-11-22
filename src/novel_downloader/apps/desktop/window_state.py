#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.window_state
------------------------------------------

Window geometry persistence for the desktop client.

The last main-window state (size, position and maximized flag) travels in the
desktop preference file as the ``window_*`` keys; :func:`restore_window_state`
turns a decoded mapping into a safe state, and :func:`window_state_to_prefs`
renders it back.  Both are pure, so the rules are testable without a display.

Older builds kept the geometry in a standalone ``window_state.json`` inside
:func:`novel_downloader.apps.music.paths.music_data_dir`; the file helpers
here remain so that a saved position survives the upgrade.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.apps.music.paths import music_data_dir

__all__ = [
    "DEFAULT_WINDOW_HEIGHT",
    "DEFAULT_WINDOW_WIDTH",
    "MIN_RESTORED_HEIGHT",
    "MIN_RESTORED_WIDTH",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "SAVE_THROTTLE_SECONDS",
    "WINDOW_PREFS_KEYS",
    "WINDOW_STATE_FILENAME",
    "SaveThrottle",
    "WindowGeometry",
    "WindowState",
    "clamp_geometry",
    "load_window_geometry",
    "restore_window_state",
    "save_window_geometry",
    "visible_origin",
    "window_geometry_from_mapping",
    "window_state_file",
    "window_state_to_prefs",
]

logger = logging.getLogger(__name__)

#: Lower bound applied when restoring a stored geometry.  The window itself
#: enforces a stricter ``minsize``; this guard only keeps a bogus stored value
#: (e.g. a degenerate ``1x1``) from shrinking the restored window.
MIN_WINDOW_WIDTH = 400
MIN_WINDOW_HEIGHT = 300

#: Size used when a stored dimension is unusable (wrong type, negative,
#: absurd) or below what the window can actually display (:func:`run_webview_app`
#: creates the window with ``min_size=(980, 640)``).
DEFAULT_WINDOW_WIDTH = 1220
DEFAULT_WINDOW_HEIGHT = 800
MIN_RESTORED_WIDTH = 980
MIN_RESTORED_HEIGHT = 640

#: Anything larger is treated as a corrupted value rather than a real window.
#: Eight times a 4K panel is already far beyond a plausible virtual desktop.
MAX_WINDOW_DIMENSION = 16384
MAX_WINDOW_COORDINATE = 1_000_000

#: Moving/resizing fires continuously; the window is persisted at most this
#: often while the user drags it, and unconditionally when it closes.
SAVE_THROTTLE_SECONDS = 1.0

#: Prefix of every key this module reads from / writes to the desktop prefs.
_PREFS_WIDTH = "window_width"
_PREFS_HEIGHT = "window_height"
_PREFS_X = "window_x"
_PREFS_Y = "window_y"
_PREFS_MAXIMIZED = "window_maximized"

#: Every desktop-prefs key that holds window geometry.  ``reset_window_state``
#: deletes exactly these, so a reset can never touch an unrelated preference.
WINDOW_PREFS_KEYS: tuple[str, ...] = (
    _PREFS_WIDTH,
    _PREFS_HEIGHT,
    _PREFS_X,
    _PREFS_Y,
    _PREFS_MAXIMIZED,
)

#: Name of the JSON file inside :func:`music_data_dir`.
WINDOW_STATE_FILENAME = "window_state.json"


@dataclass(frozen=True)
class WindowGeometry:
    """The size and position of the main window."""

    width: int
    height: int
    #: Position offsets; ``None`` means "keep the position the window manager
    #: picks".
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True)
class WindowState:
    """The restored size/position plus the independent maximized flag."""

    geometry: WindowGeometry
    maximized: bool = False


def _int_dimension(value: Any) -> int | None:
    """A plausible stored window dimension, or ``None`` when unusable."""
    number = _coerce_int(value)
    if number is None or number < 1 or number > MAX_WINDOW_DIMENSION:
        return None
    return number


def _int_coordinate(value: Any) -> int | None:
    """
    A plausible stored window origin, or ``None`` when unusable.

    Negative values are accepted: they address monitors left of/above the
    primary.  Only non-integers and absurd magnitudes are rejected; whether
    the origin is still on a connected monitor is decided separately by
    :func:`restore_window_state`.
    """
    number = _coerce_int(value)
    if number is None or abs(number) > MAX_WINDOW_COORDINATE:
        return None
    return number


def _intersects(
    x: int,
    y: int,
    width: int,
    height: int,
    bounds: tuple[int, int, int, int],
) -> bool:
    """Whether a window rectangle overlaps the monitor bounds at all."""
    bx, by, bw, bh = bounds
    return not (x + width <= bx or x >= bx + bw or y + height <= by or y >= by + bh)


def _centered_origin(
    width: int,
    height: int,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Top-left corner that centres a window on *bounds*."""
    bx, by, bw, bh = bounds
    return bx + max(0, (bw - width) // 2), by + max(0, (bh - height) // 2)


def restore_window_state(
    mapping: Mapping[str, Any],
    virtual_bounds: tuple[int, int, int, int] | None = None,
    primary_bounds: tuple[int, int, int, int] | None = None,
) -> WindowState | None:
    """
    Validate stored ``window_*`` values and fall back to safe defaults.

    Rules, each applied independently so one corrupt field does not discard
    the rest:

    * missing state — none of the ``window_*`` keys is present — yields
      ``None`` and the window opens at its default size; a lone
      ``window_maximized`` still restores the maximized flag;
    * a dimension that is not an integer, is negative/zero, absurdly large or
      below the window minimum is replaced with the default dimension;
    * a non-integer or absurd origin is dropped (the window manager places
      the window), while an integer origin is kept — including a negative
      one, which addresses a monitor left of/above the primary;
    * an origin whose rectangle does not overlap the connected monitors at
      all is replaced by a position centred on the primary monitor;
    * an origin that is only partly off-screen is nudged back inside;
    * ``window_maximized`` is restored independently of the geometry, and
      only a real JSON ``true`` enables it.

    :param mapping: The decoded desktop preferences (or an equivalent map).
    :param virtual_bounds: ``(x, y, width, height)`` of all connected
        monitors, or ``None`` when the metrics are unavailable.
    :param primary_bounds: ``(x, y, width, height)`` of the primary monitor,
        used when the stored origin no longer overlaps any monitor.
    :return: The restored state, or ``None`` when no window keys were stored.
    """
    if not any(key in mapping for key in WINDOW_PREFS_KEYS):
        return None

    width = _int_dimension(mapping.get(_PREFS_WIDTH))
    height = _int_dimension(mapping.get(_PREFS_HEIGHT))
    if width is None or width < MIN_RESTORED_WIDTH:
        width = DEFAULT_WINDOW_WIDTH
    if height is None or height < MIN_RESTORED_HEIGHT:
        height = DEFAULT_WINDOW_HEIGHT

    x = _int_coordinate(mapping.get(_PREFS_X))
    y = _int_coordinate(mapping.get(_PREFS_Y))
    if x is None or y is None:
        x = y = None

    if virtual_bounds is not None:
        bx, by, bw, bh = virtual_bounds
        # Never restore a window larger than the screens it must appear on.
        width = min(width, max(MIN_RESTORED_WIDTH, bw))
        height = min(height, max(MIN_RESTORED_HEIGHT, bh))
        if x is not None and y is not None:
            if not _intersects(x, y, width, height, virtual_bounds):
                centre = primary_bounds or virtual_bounds
                x, y = _centered_origin(width, height, centre)
            else:
                x = max(bx, min(x, bx + bw - width))
                y = max(by, min(y, by + bh - height))

    return WindowState(
        geometry=WindowGeometry(width=width, height=height, x=x, y=y),
        maximized=mapping.get(_PREFS_MAXIMIZED) is True,
    )


def window_state_to_prefs(state: WindowState) -> dict[str, int | bool | None]:
    """
    Render a state as the desktop-prefs keys :func:`restore_window_state` reads.

    :param state: The state captured from the live window.
    :return: A JSON-compatible mapping.
    """
    return {
        _PREFS_WIDTH: state.geometry.width,
        _PREFS_HEIGHT: state.geometry.height,
        _PREFS_X: state.geometry.x,
        _PREFS_Y: state.geometry.y,
        _PREFS_MAXIMIZED: state.maximized,
    }


class SaveThrottle:
    """
    Rate limiter for geometry writes while the user drags the window.

    A single instance is reused for the life of the window; :meth:`allow`
    returns ``True`` at most once per ``min_interval`` seconds.  ``force=True``
    always wins and re-arms the interval, so the final write on close is never
    swallowed.
    """

    def __init__(
        self,
        min_interval: float = SAVE_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def allow(self, *, force: bool = False) -> bool:
        """
        Whether a write should happen now, marking the write when it returns
        ``True``.

        :param force: Bypass the interval (window closing, maximize/restore).
        """
        with self._lock:
            now = self._clock()
            if force or self._last is None or now - self._last >= self._min_interval:
                self._last = now
                return True
            return False


def clamp_geometry(
    geometry: WindowGeometry,
    bounds: tuple[int, int, int, int] | None = None,
) -> WindowGeometry:
    """
    Fit a geometry to the window-size rules and the connected monitors.

    The minimum size always applies. When *bounds* (the virtual desktop
    rectangle) is supplied, an oversized window is shrunk to the screen and
    an origin that would push the bottom-right corner off-screen is moved
    just far enough to bring it back — a geometry saved on a 2560-wide
    monitor must not reopen wider than a 1536-wide laptop panel.

    :param geometry: The geometry to clamp.
    :param bounds: ``(x, y, width, height)`` of the virtual desktop, or
        ``None`` when the metrics are unavailable.
    :return: A geometry whose size respects the minimum and, when known,
        the current screen.
    """
    width = max(geometry.width, MIN_WINDOW_WIDTH)
    height = max(geometry.height, MIN_WINDOW_HEIGHT)
    x, y = geometry.x, geometry.y
    if bounds is not None:
        bx, by, bw, bh = bounds
        width = min(width, max(MIN_WINDOW_WIDTH, bw))
        height = min(height, max(MIN_WINDOW_HEIGHT, bh))
        if x is not None and y is not None:
            x = max(bx, min(x, bx + bw - width))
            y = max(by, min(y, by + bh - height))
    return WindowGeometry(width=width, height=height, x=x, y=y)


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` when it is a plain integer, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def window_geometry_from_mapping(mapping: object) -> WindowGeometry | None:
    """
    Validate a JSON-decoded mapping as a window geometry.

    ``width`` and ``height`` must be positive integers; ``x``/``y`` are kept
    only when both are integers (any sign allowed) or both absent/``None``.
    Anything else — wrong types, missing sizes, non-mapping input — yields
    ``None`` instead of raising.

    :param mapping: Decoded JSON value.
    :return: The validated geometry, or ``None`` when unusable.
    """
    if not isinstance(mapping, Mapping):
        return None
    width = _coerce_int(mapping.get("width"))
    height = _coerce_int(mapping.get("height"))
    if width is None or height is None or width < 1 or height < 1:
        return None
    pos_x = _coerce_int(mapping.get("x"))
    pos_y = _coerce_int(mapping.get("y"))
    if pos_x is None or pos_y is None:
        pos_x = pos_y = None
    return WindowGeometry(width=width, height=height, x=pos_x, y=pos_y)


def visible_origin(
    x: int, y: int, bounds: tuple[int, int, int, int] | None
) -> tuple[int, int] | None:
    """
    Decide whether a stored window origin still lands on a monitor.

    Negative coordinates are legitimate: on multi-monitor setups a screen
    placed left of or above the primary has a negative origin, and clamping
    such a value to zero would yank the window back onto the primary screen.
    The position is only dropped when it falls outside ``bounds`` — the
    bounding rectangle of the currently connected monitors — which happens
    when the monitor the window was saved on is no longer attached.

    :param x: Stored window origin X (any sign).
    :param y: Stored window origin Y (any sign).
    :param bounds: ``(x, y, width, height)`` of the virtual desktop, or
        ``None`` when the metrics are unknown (the origin is then trusted).
    :return: ``(x, y)`` when the origin is plausibly visible, else ``None``
        meaning "let the window manager pick a position".
    """
    if bounds is None:
        return (x, y)
    bx, by, bw, bh = bounds
    if bx <= x < bx + bw and by <= y < by + bh:
        return (x, y)
    return None


def window_geometry_to_mapping(geometry: WindowGeometry) -> dict[str, int | None]:
    """
    Render a geometry as the JSON mapping :func:`window_geometry_from_mapping`
    accepts.

    :param geometry: The geometry to store.
    :return: A JSON-compatible mapping.
    """
    return {
        "width": geometry.width,
        "height": geometry.height,
        "x": geometry.x,
        "y": geometry.y,
    }


def window_state_file() -> Path:
    """Return the JSON file holding the last window geometry."""
    return music_data_dir() / WINDOW_STATE_FILENAME


def load_window_geometry(path: Path) -> WindowGeometry | None:
    """
    Read the stored geometry, clamped to the minimum window size.

    :param path: JSON file written by :func:`save_window_geometry`.
    :return: The stored geometry, or ``None`` when the file is missing,
        unreadable, corrupted or does not describe a usable geometry.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    geometry = window_geometry_from_mapping(loaded)
    if geometry is None:
        return None
    return clamp_geometry(geometry)


def save_window_geometry(path: Path, geometry: WindowGeometry) -> bool:
    """
    Persist the geometry as JSON, creating the parent directory if needed.

    :param path: Target JSON file.
    :param geometry: The geometry to store.
    :return: ``True`` when the file was written, ``False`` when the disk
        write failed (the failure is logged, never raised).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                window_geometry_to_mapping(geometry), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to save the window state to %s", path, exc_info=True)
        return False
    return True
