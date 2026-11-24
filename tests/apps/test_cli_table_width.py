#!/usr/bin/env python3
"""Width, truncation and ANSI contract for the CLI tables.

Every human-readable listing (``search``, ``follow list``, ``music search``,
``storage``, ``sources list``, ...) ends in
:func:`novel_downloader.apps.cli.ui.render_table`, which delegates to a
full-width Rich ``Table``:

* width comes from ``COLUMNS`` when it is set, otherwise from Rich's terminal
  detection and its 80-column fallback;
* cells are measured in *display cells* (``rich.cells.cell_len``): CJK is two
  cells, a ZWJ emoji sequence is two, a combining mark is zero -- not ``len()``;
* long values are **folded** across continuation lines inside their column,
  not elided, so the full title/URL/path stays recoverable from the output;
* a non-TTY sink receives no escape sequences.

The tests below pin those rules at narrow (40), common (80) and wide (200)
widths, plus the hostile cases: a zero/one-column ``COLUMNS``, missing or
unusable ``COLUMNS``, untrusted CSI/OSC sequences embedded in a title, empty
and single-row results, and cells containing newlines/tabs.

The only CLI helper that *elides* text is ``commands.batch._clip`` (one-line
warnings); it is covered here because it is the "with an ellipsis" path.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.cells import cell_len
from rich.console import Console

from novel_downloader.apps.cli import ui
from novel_downloader.apps.cli.commands import batch, follow

#: Box-drawing characters used by Rich's default table style.
_BOX = frozenset("─│┌┐└┘├┤┬┴┼")

#: A title long enough to need folding even at a wide terminal.
_LONG_CJK = "这是一本标题非常非常长的小说" * 8
_LONG_EN = "An extremely long english novel title " * 6


class _TTYStringIO(io.StringIO):
    """A text buffer that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _console(
    monkeypatch: pytest.MonkeyPatch,
    *,
    columns: str | None = None,
    tty: bool = False,
) -> tuple[Console, io.StringIO]:
    """Build a console whose width comes from ``COLUMNS`` and capture it."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    if columns is None:
        monkeypatch.delenv("COLUMNS", raising=False)
    else:
        monkeypatch.setenv("COLUMNS", columns)
    stream = _TTYStringIO() if tty else io.StringIO()
    console = ui._build_console(file=stream)
    monkeypatch.setattr(ui, "_CONSOLE", console)
    return console, stream


def _lines(stream: io.StringIO) -> list[str]:
    """Every rendered line, title/borders/content alike, without blanks."""
    return [line for line in stream.getvalue().splitlines() if line]


def _rendered_width(line: str) -> int:
    """Display width of a line after dropping Rich's own styling escapes."""
    return cell_len(ui._ANSI_ESCAPE_RE.sub("", line))


def _compact(text: str) -> str:
    """Content characters only: no borders, padding or line breaks."""
    return "".join(ch for ch in text if ch not in _BOX and not ch.isspace())


def _bar_offsets(line: str) -> list[int]:
    """Display-cell offsets of the vertical borders in ``line``."""
    offsets: list[int] = []
    offset = 0
    for char in line:
        if char == "│":
            offsets.append(offset)
        offset += cell_len(char) if char not in _BOX else 1
    return offsets


def _junction_offsets(line: str) -> list[int]:
    """Display-cell offsets of the internal ``┼`` junctions in ``line``."""
    offsets: list[int] = []
    offset = 0
    for char in line:
        if char == "┼":
            offsets.append(offset)
        offset += cell_len(char) if char not in _BOX else 1
    return offsets


# --------------------------------------------------------------------------- #
# long titles at fixed widths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("columns", ["40", "80", "200"])
@pytest.mark.parametrize("value", [_LONG_CJK, _LONG_EN], ids=["cjk", "ascii"])
def test_long_titles_fold_inside_the_column(
    monkeypatch: pytest.MonkeyPatch, columns: str, value: str
) -> None:
    """A title never pushes the table past ``COLUMNS`` and is never lost."""
    console, stream = _console(monkeypatch, columns=columns)
    ui.render_table("长标题", ["Title"], [[value]])

    lines = _lines(stream)
    assert lines
    assert console.width > 0
    assert {_rendered_width(line) for line in lines} == {console.width}

    # Fold, not elision: every character is still there (in order), so the
    # output is recoverable even when the terminal is narrower than the title.
    assert _compact(value) in _compact(stream.getvalue())
    assert "…" not in _compact(stream.getvalue())


def test_long_table_title_stays_inside_the_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="40")
    title = "这是一个非常长的表格标题·" * 5
    ui.render_table(title, ["A", "B"], [["x", "y"]])

    lines = _lines(stream)
    assert all(_rendered_width(line) == console.width for line in lines)
    assert title in _compact(stream.getvalue())


# --------------------------------------------------------------------------- #
# hostile and missing COLUMNS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("columns", ["0", "1", "2"])
def test_tiny_columns_never_yields_a_zero_or_negative_console(
    monkeypatch: pytest.MonkeyPatch, columns: str
) -> None:
    """``COLUMNS=0`` used to make Rich's width negative and print nothing."""
    console, stream = _console(monkeypatch, columns=columns)
    assert console.width >= 1

    ui.render_table("T", ["A", "B"], [["长标题", "x"]])

    lines = _lines(stream)
    assert lines, "a non-positive width silently swallowed the whole table"
    assert all(_rendered_width(line) == console.width for line in lines)


@pytest.mark.parametrize("width", [-100, -1, 0])
def test_explicit_non_positive_width_is_clamped(width: int) -> None:
    console = ui._build_console(file=io.StringIO(), width=width)
    assert console.width >= 1


@pytest.mark.parametrize("columns", [None, "", "abc", "-5"])
@pytest.mark.parametrize("tty", [False, True])
def test_unset_or_unusable_columns_falls_back_to_a_positive_width(
    monkeypatch: pytest.MonkeyPatch, columns: str | None, tty: bool
) -> None:
    """No usable width hint: Rich detects, then falls back to 80 columns."""
    console, stream = _console(monkeypatch, columns=columns, tty=tty)
    assert console.width > 0

    ui.render_table("T", ["A", "B"], [["长", "x"]])

    lines = _lines(stream)
    assert lines
    assert all(_rendered_width(line) == console.width for line in lines)


# --------------------------------------------------------------------------- #
# display-cell measurement and alignment
# --------------------------------------------------------------------------- #
def test_wide_and_combining_characters_use_display_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="80")
    rows = [
        ["三体", "刘慈欣"],
        ["🎵 emoji", "x"],
        ["e\u0301 combining", "y"],
        ["plain ascii", "z"],
    ]
    ui.render_table("作品", ["书名", "作者"], rows)

    lines = _lines(stream)
    assert {_rendered_width(line) for line in lines} == {console.width}

    # The implementation measures cells, not code points: a line holding CJK
    # is display-wider than ``len(line)``; a ZWJ family emoji is two cells.
    cjk_line = next(line for line in lines if "三体" in line)
    assert _rendered_width(cjk_line) > len(cjk_line)
    assert cell_len("三体") == 4 == 2 * len("三体")
    assert cell_len("🎵") == 2
    assert cell_len("👨‍👩‍👧‍👦") == 2
    assert cell_len("e\u0301") == 1


def test_header_and_rows_share_the_same_column_borders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="80")
    ui.render_table(
        "T",
        ["#", "Book ID", "Title", "Author"],
        [
            ["1", "12345", "三体", "刘慈欣"],
            ["2", "very-long-id" * 10, "A very long english title", "Someone"],
        ],
    )

    lines = _lines(stream)
    assert all(_rendered_width(line) == console.width for line in lines)

    bar_rows = [_bar_offsets(line) for line in lines[1:] if line.startswith("│")]
    assert bar_rows
    assert len({tuple(offsets) for offsets in bar_rows}) == 1, (
        "a wrapped cell shifted a column border"
    )
    # Separator junctions sit exactly on the inner column borders.
    for line in lines[1:]:
        if line.startswith("├"):
            assert _junction_offsets(line) == bar_rows[0][1:-1]


# --------------------------------------------------------------------------- #
# ANSI safety
# --------------------------------------------------------------------------- #
def test_non_tty_table_is_plain_even_when_cells_contain_ansi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="80")
    assert console.is_terminal is False

    ui.render_table("T", ["A"], [["\x1b[31mred\x1b[0m"]])

    assert "\x1b" not in stream.getvalue()
    assert "red" in stream.getvalue()
    assert all(_rendered_width(line) == console.width for line in _lines(stream))


def test_untrusted_ansi_is_stripped_before_layout_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote title must not colour the terminal or shift the borders."""
    console, stream = _console(monkeypatch, columns="40", tty=True)
    assert console.is_terminal is True

    ui.render_table(
        "T",
        ["A", "B"],
        [
            ["\x1b[31mred\x1b[0m", "x"],
            ["\x1b]8;;https://example.com\x07click\x1b]8;;\x07", "y"],
        ],
    )

    output = stream.getvalue()
    assert "\x1b[31m" not in output
    assert "\x1b]8;;" not in output
    assert "red" in output
    assert "click" in output
    assert {_rendered_width(line) for line in _lines(stream)} == {console.width}


# --------------------------------------------------------------------------- #
# empty, single-row and extreme fields
# --------------------------------------------------------------------------- #
def test_empty_result_keeps_the_header_and_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="80")
    ui.render_table("No results", ["Title", "Author"], [])

    lines = _lines(stream)
    assert "No results" in lines[0]
    header = next(line for line in lines if "Title" in line)
    assert "Author" in header
    assert sum(line.startswith("┌") for line in lines) == 1
    assert sum(line.startswith("└") for line in lines) == 1
    assert all(_rendered_width(line) == console.width for line in lines)


def test_single_row_renders_one_data_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(monkeypatch, columns="80")
    ui.render_table("One", ["A", "B"], [["x", "y"]])

    lines = _lines(stream)
    data = [line for line in lines if line.startswith("│") and "x" in line]
    assert len(data) == 1
    assert all(_rendered_width(line) == console.width for line in lines)


def test_extreme_cell_fields_do_not_break_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newlines, CR, tabs and a 300-char token must not tear the frame."""
    console, stream = _console(monkeypatch, columns="60")
    ui.render_table(
        "T",
        ["A", "B"],
        [
            ["line1\nline2", "a\tb"],
            ["", "c\r\nd"],
            ["x" * 300, "\ttab"],
        ],
    )

    lines = _lines(stream)
    assert all(_rendered_width(line) == console.width for line in lines)
    assert all(line[0] in _BOX and line[-1] in _BOX for line in lines[1:])

    output = stream.getvalue()
    assert "line1" in output
    assert "line2" in output
    assert "\t" not in output and "\r" not in output


# --------------------------------------------------------------------------- #
# long URLs and paths: the prefix survives
# --------------------------------------------------------------------------- #
_URL = "https://example.com/" + "segment/" * 30 + "?q=" + "x" * 80
_PATH = "C:" + "\\Users\\someone\\very\\deep\\directory" * 5


@pytest.mark.parametrize(
    ("value", "prefix"), [(_URL, "https://"), (_PATH, "C:\\Users")], ids=["url", "path"]
)
def test_long_url_and_path_keep_their_prefix(
    monkeypatch: pytest.MonkeyPatch, value: str, prefix: str
) -> None:
    """Folding keeps the scheme/drive on one line and every character.

    This table implementation folds; it never performs the "keep the start and
    the end, elide the middle" trick. The contract that matters is the one an
    elider would also have to honour: the protocol/drive prefix is not split,
    and no character is silently dropped.
    """
    console, stream = _console(monkeypatch, columns="40")
    ui.render_table("T", ["Location"], [[value]])

    lines = _lines(stream)
    assert all(_rendered_width(line) == console.width for line in lines)
    assert any(prefix in line for line in lines), "the prefix was split across lines"

    compact = _compact(stream.getvalue())
    start = compact.index(prefix)
    assert compact[start:].startswith(value), "folding lost or reordered characters"


# --------------------------------------------------------------------------- #
# --json is independent of table width
# --------------------------------------------------------------------------- #
def _follow_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    follow.FollowListCmd.register(subparsers)
    return parser


def test_json_output_is_independent_of_columns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    title = "非常长的书名" * 20
    follow._make_store().add("b520", "1", title=title)
    parser = _follow_list_parser()

    outputs: list[str] = []
    for columns in ("40", "80", "200"):
        monkeypatch.setenv("COLUMNS", columns)
        args = parser.parse_args(["list", "--json"])
        args.func(args)
        outputs.append(capsys.readouterr().out)

    assert outputs[0] == outputs[1] == outputs[2]
    payload: list[dict[str, Any]] = json.loads(outputs[0])
    assert payload[0]["title"] == title
    assert "│" not in outputs[0]
    assert "\x1b" not in outputs[0]


# --------------------------------------------------------------------------- #
# the one eliding helper: batch._clip
# --------------------------------------------------------------------------- #
def test_batch_clip_elides_single_line_values_with_an_ellipsis() -> None:
    clipped = batch._clip("长" * 200)

    assert len(clipped) == 80
    assert clipped == "长" * 79 + "…"
    assert batch._clip("短标题") == "短标题"
