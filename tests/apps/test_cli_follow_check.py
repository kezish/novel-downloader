#!/usr/bin/env python3
"""
tests.apps.test_cli_follow_check
--------------------------------

Semantics and failure-matrix tests for ``novel-cli follow check``.

The contract under test (see ``docs/guide/cli-examples.md``, section 8):

* exit ``0`` only when no book failed and every followed entry was usable;
* any per-book check failure (network, plugin, storage) exits ``1`` even when
  the other books succeeded;
* ``--json`` writes exactly one compact single-line document on stdout, with
  human diagnostics on stderr, and its ``ok`` field agrees with the exit code;
* ``--download --wait`` queues books strictly one at a time, waits for each,
  cancels a book that exceeds ``--wait-timeout`` and exits ``1`` unless every
  download completed;
* duplicate watchlist entries are folded, entries that cannot be used are
  counted but never searched or queued, and a repeated check does not queue a
  book whose new chapter was already recorded;
* Ctrl+C during ``--wait`` cancels the in-flight download through the manager,
  leaves the later books unqueued and propagates ``KeyboardInterrupt`` (the
  entry point turns that into ``Aborted.`` on stderr and exit ``1``).

Searches and the task manager are replaced with in-memory fakes, so no network
access happens and the watchlist lives in a temporary ``NOVEL_HOME``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import follow
from novel_downloader.apps.cli.commands.follow import FollowCheckCmd
from novel_downloader.apps.core.models import Status
from novel_downloader.apps.core.watchlist import WatchlistStore
from novel_downloader.plugins import search as search_module
from novel_downloader.plugins.search import UnknownSearchSiteError

#: Matches colour/attribute CSI sequences, the only escape type Rich emits.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_TERMINAL = frozenset({Status.COMPLETED, Status.CANCELLED, Status.FAILED})


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _parser(*commands: type) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


def _capture_ui(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    messages: list[str] = []
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(module.ui, name, messages.append)
    return messages


def _write_watchlist(home: Path, books: list[dict[str, Any]]) -> Path:
    path = home / "watchlist.json"
    path.write_text(json.dumps({"version": 1, "books": books}), encoding="utf-8")
    return path


def _result(site: str, book_id: str, title: str, latest_chapter: str) -> dict[str, Any]:
    return {
        "site": site,
        "book_id": book_id,
        "book_url": "",
        "cover_url": "",
        "title": title,
        "author": "",
        "latest_chapter": latest_chapter,
        "update_date": "",
        "word_count": "",
        "priority": 100,
    }


def _read_json_document(capsys: pytest.CaptureFixture[str]) -> tuple[Any, str]:
    """
    Return ``(decoded_stdout, stderr)`` and assert the single-line contract.

    ``--json`` output is one compact document followed by one newline: no
    other newline may appear, so ``jq``/line readers can consume it.
    """
    captured = capsys.readouterr()
    out = captured.out
    assert out.endswith("\n")
    assert out.count("\n") == 1, f"stdout is not a single line: {out!r}"
    return json.loads(out), captured.err


class _SearchStub:
    """Async stand-in for :func:`novel_downloader.plugins.search.search`."""

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.results = results or {}
        self.errors = errors or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        keyword: str,
        sites: Any = None,
        limit: Any = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
        nsfw: bool = False,
        sort: str = "quality",
        order: Any = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "keyword": keyword,
                "sites": list(sites or []),
                "timeout": timeout,
            }
        )
        error = self.errors.get(keyword)
        if error is not None:
            raise error
        return list(self.results.get(keyword, []))


class _FakeTask:
    def __init__(self, task_id: str, site: str, book_id: str) -> None:
        self.task_id = task_id
        self.site = site
        self.book_id = book_id
        self.status = Status.QUEUED
        self.error: str | None = None


class _FakeManager:
    """
    Offline stand-in for ``TaskManager``.

    Mirrors the three behaviours ``follow check --download --wait`` relies on:

    * ``add_task`` is idempotent for an *active* ``(site, book_id)`` (as the
      real manager is), so a duplicate can never be downloaded twice;
    * ``snapshot()`` walks a scripted status sequence per book, advancing one
      step per call (a script without a terminal status keeps the task alive,
      which is how the timeout path is exercised);
    * ``close()`` cancels the tasks still in flight, exactly like the real
      manager's shutdown.
    """

    def __init__(
        self,
        script: dict[str, list[Status]] | None = None,
        errors: dict[str, str] | None = None,
        add_errors: dict[str, str] | None = None,
        *,
        interrupt_after: int | None = None,
    ) -> None:
        self.script = script or {}
        self.errors = errors or {}
        self.add_errors = add_errors or {}
        self.interrupt_after = interrupt_after
        self.added: list[tuple[str, str]] = []
        self.cancel_calls: list[str] = []
        self.closed: list[str] = []
        self.close_calls = 0
        self.tasks: list[_FakeTask] = []
        self._steps: dict[str, int] = {}
        self._snapshots = 0
        self._seq = 0

    def _active(self, site: str, book_id: str) -> _FakeTask | None:
        for task in self.tasks:
            if (
                task.site == site
                and task.book_id == book_id
                and task.status not in _TERMINAL
            ):
                return task
        return None

    async def add_task(self, *, title: str, site: str, book_id: str) -> _FakeTask:
        self.added.append((site, book_id))
        if book_id in self.add_errors:
            raise RuntimeError(self.add_errors[book_id])
        existing = self._active(site, book_id)
        if existing is not None:
            return existing
        self._seq += 1
        task = _FakeTask(task_id=f"task-{self._seq}", site=site, book_id=book_id)
        self.tasks.append(task)
        self._steps[task.task_id] = 0
        return task

    def snapshot(self) -> dict[str, list[_FakeTask]]:
        if self.interrupt_after is not None:
            if self._snapshots >= self.interrupt_after:
                raise KeyboardInterrupt
            self._snapshots += 1
        running: list[_FakeTask] = []
        completed: list[_FakeTask] = []
        for task in self.tasks:
            sequence = self.script.get(task.book_id) or [Status.COMPLETED]
            step = self._steps[task.task_id]
            self._steps[task.task_id] = min(step + 1, len(sequence) - 1)
            task.status = sequence[step]
            if task.status is Status.FAILED:
                task.error = self.errors.get(task.book_id, "download failed")
            (completed if task.status in _TERMINAL else running).append(task)
        return {"running": running, "pending": [], "completed": completed}

    async def cancel_task(self, task_id: str) -> bool:
        self.cancel_calls.append(task_id)
        for task in self.tasks:
            if task.task_id == task_id and task.status not in _TERMINAL:
                task.status = Status.CANCELLED
                task.error = "Cancelled by user"
        return True

    async def close(self) -> None:
        self.close_calls += 1
        for task in self.tasks:
            if task.status not in _TERMINAL:
                task.status = Status.CANCELLED
                task.error = "Cancelled by user"
                self.closed.append(task.task_id)


def _patch_manager(monkeypatch: pytest.MonkeyPatch, manager: _FakeManager) -> None:
    monkeypatch.setattr(follow, "_get_manager", lambda: manager)


def _seed_books(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    books: list[tuple[str, str, str, str]],
    stub: _SearchStub,
) -> WatchlistStore:
    """Point ``NOVEL_HOME`` at ``tmp_path``, seed entries and patch search."""
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    for site, book_id, title, last_chapter in books:
        store.add(site, book_id, title=title, last_chapter=last_chapter)
    monkeypatch.setattr(search_module, "search", stub)
    return store


# ---------------------------------------------------------------------------
# no updates / JSON report shape
# ---------------------------------------------------------------------------


def test_check_no_updates_exits_zero_and_reports_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = _SearchStub(
        {"Book A": [_result("b520", "1", "Book A", "Chapter 1")]},
    )
    _seed_books(monkeypatch, tmp_path, [("b520", "1", "Book A", "Chapter 1")], stub)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    args.func(args)  # nothing failed: no SystemExit

    payload, err = _read_json_document(capsys)
    assert payload == {
        "ok": True,
        "checked": 1,
        "changed": 0,
        "failed": 0,
        "invalid": 0,
        "books": [
            {
                "site": "b520",
                "book_id": "1",
                "title": "Book A",
                "status": "up_to_date",
                "latest": "Chapter 1",
                "previous": "Chapter 1",
                "error": None,
                "command": None,
            }
        ],
    }
    # The human summary is a diagnostic: it belongs on stderr, never stdout.
    assert "No new chapters found." in err


def test_check_empty_watchlist_json_is_a_successful_empty_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    args.func(args)

    payload, _err = _read_json_document(capsys)
    assert payload == {
        "ok": True,
        "checked": 0,
        "changed": 0,
        "failed": 0,
        "invalid": 0,
        "books": [],
    }


def test_check_corrupt_watchlist_fails_without_a_json_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing was checked, so there is no report; the error says why."""
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text("{not json", encoding="utf-8")
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Could not read" in captured.err
    # The unreadable bytes stay exactly as they were.
    assert watchlist.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# failure matrix
# ---------------------------------------------------------------------------


def test_check_partial_failure_exits_one_and_json_counts_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = _SearchStub(
        {
            "Book A": [_result("b520", "1", "Book A", "Chapter 2")],
        },
        errors={"Book B": RuntimeError("site down")},
    )
    _seed_books(
        monkeypatch,
        tmp_path,
        [("b520", "1", "Book A", "Chapter 1"), ("qidian", "2", "Book B", "")],
        stub,
    )
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    payload, err = _read_json_document(capsys)
    assert payload["ok"] is False
    assert payload["checked"] == 2
    assert payload["changed"] == 1
    assert payload["failed"] == 1
    assert payload["invalid"] == 0
    books = {book["book_id"]: book for book in payload["books"]}
    assert books["1"]["status"] == "new"
    assert books["1"]["latest"] == "Chapter 2"
    assert books["1"]["previous"] == "Chapter 1"
    assert books["2"]["status"] == "error"
    assert books["2"]["error"] == "RuntimeError: site down"
    assert books["2"]["latest"] == ""
    # Per-book diagnostics and the total stay on stderr.
    assert "Check failed for qidian/2: RuntimeError: site down" in err
    assert "1 check(s) failed." in err


def test_check_every_book_failing_with_network_errors_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = _SearchStub(
        errors={
            "Book A": TimeoutError(),
            "Book B": OSError("connection reset"),
            "Book C": ConnectionError("dns failure"),
        }
    )
    _seed_books(
        monkeypatch,
        tmp_path,
        [
            ("b520", "1", "Book A", "Chapter 1"),
            ("qidian", "2", "Book B", "Chapter 2"),
            ("b520", "3", "Book C", ""),
        ],
        stub,
    )
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json", "--timeout", "0.25"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    payload, _err = _read_json_document(capsys)
    assert payload["ok"] is False
    assert payload["checked"] == 3
    assert payload["changed"] == 0
    assert payload["failed"] == 3
    assert [book["status"] for book in payload["books"]] == ["error"] * 3
    errors = [book["error"] for book in payload["books"]]
    # A bare TimeoutError stringifies to "", so the type name must survive.
    assert errors[0].startswith("TimeoutError:")
    assert "OSError: connection reset" in errors[1]
    assert "ConnectionError: dns failure" in errors[2]
    # Nothing was found, so the stored chapters are untouched.
    store = WatchlistStore()
    assert store.get("b520", "1").last_chapter == "Chapter 1"
    assert store.get("qidian", "2").last_chapter == "Chapter 2"
    # The per-book search budget is forwarded to the search engine.
    assert [call["timeout"] for call in stub.calls] == [0.25, 0.25, 0.25]


def test_check_storage_failure_is_a_per_book_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed watchlist write is reported for that book, and fails the sweep."""
    stub = _SearchStub({"Book A": [_result("b520", "1", "Book A", "Chapter 2")]})
    _seed_books(monkeypatch, tmp_path, [("b520", "1", "Book A", "Chapter 1")], stub)

    def _boom(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(WatchlistStore, "update_latest", _boom)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    payload, _err = _read_json_document(capsys)
    assert payload["ok"] is False
    assert payload["failed"] == 1
    book = payload["books"][0]
    assert book["status"] == "error"
    assert book["error"].startswith("OSError:")
    # The chapter was not persisted, so the stored value is still the old one.
    document = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))
    assert document["books"][0]["last_chapter"] == "Chapter 1"


def test_check_unknown_site_is_a_per_book_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = _SearchStub(
        errors={"Book A": UnknownSearchSiteError(["nosuch"], ["b520", "qidian"])}
    )
    _seed_books(monkeypatch, tmp_path, [("nosuch", "1", "Book A", "")], stub)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    payload, _err = _read_json_document(capsys)
    assert payload["failed"] == 1
    book = payload["books"][0]
    assert book["status"] == "error"
    assert book["site"] == "nosuch"
    assert "Unknown site(s): nosuch" in book["error"]


# ---------------------------------------------------------------------------
# duplicates / unusable entries
# ---------------------------------------------------------------------------


def test_check_folds_duplicate_watchlist_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    _write_watchlist(
        tmp_path,
        [
            {"site": "b520", "book_id": "1", "title": "Book A", "last_chapter": ""},
            {"site": "b520", "book_id": "1", "title": "Book A", "last_chapter": ""},
            {"site": "qidian", "book_id": "2", "title": "Book B", "last_chapter": ""},
        ],
    )
    stub = _SearchStub(
        {
            "Book A": [_result("b520", "1", "Book A", "Chapter 1")],
            "Book B": [_result("qidian", "2", "Book B", "Chapter 1")],
        }
    )
    monkeypatch.setattr(search_module, "search", stub)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    args.func(args)

    payload, _err = _read_json_document(capsys)
    # The duplicate is folded on read: one check, one record.
    assert payload["checked"] == 2
    assert [book["book_id"] for book in payload["books"]] == ["1", "2"]
    assert sorted(call["keyword"] for call in stub.calls) == ["Book A", "Book B"]


def test_check_counts_unusable_entries_in_the_json_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unusable entries make the sweep fail, even with nothing else wrong."""
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    watchlist = _write_watchlist(
        tmp_path,
        [
            {"site": "b520", "book_id": "1", "title": "Book A", "last_chapter": ""},
            {
                "site": "b520",
                "book_id": "../escape",
                "title": "Bad",
                "last_chapter": "",
            },
            {"site": "b520", "title": "No id", "last_chapter": ""},
        ],
    )
    before = watchlist.read_text(encoding="utf-8")
    stub = _SearchStub({"Book A": [_result("b520", "1", "Book A", "Chapter 1")]})
    monkeypatch.setattr(search_module, "search", stub)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    payload, err = _read_json_document(capsys)
    assert payload["ok"] is False
    assert payload["checked"] == 1
    assert payload["changed"] == 1
    assert payload["failed"] == 0
    assert payload["invalid"] == 2
    assert [book["book_id"] for book in payload["books"]] == ["1"]
    assert "Could not read" in err
    # Unusable entries are reported, never searched.
    assert [call["keyword"] for call in stub.calls] == ["Book A"]
    # Recording the valid book's chapter rewrites the file, but the original
    # bytes (with both unusable entries) are preserved before the rewrite.
    quarantine = tmp_path / "watchlist.json.corrupt"
    assert quarantine.read_text(encoding="utf-8") == before
    document = json.loads(watchlist.read_text(encoding="utf-8"))
    keys = [(book["site"], book.get("book_id")) for book in document["books"]]
    assert keys == [("b520", "1"), ("b520", "../escape")]


def test_check_never_queues_unusable_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    _write_watchlist(
        tmp_path,
        [
            {"site": "b520", "book_id": "1", "title": "Book A", "last_chapter": ""},
            {
                "site": "b520",
                "book_id": "../escape",
                "title": "Bad",
                "last_chapter": "",
            },
        ],
    )
    stub = _SearchStub({"Book A": [_result("b520", "1", "Book A", "Chapter 1")]})
    monkeypatch.setattr(search_module, "search", stub)
    manager = _FakeManager()
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.0)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    assert manager.added == [("b520", "1")]
    assert [call["keyword"] for call in stub.calls] == ["Book A"]
    assert "Could not read" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --json purity
# ---------------------------------------------------------------------------


def test_check_json_is_one_line_and_keeps_stdout_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = _SearchStub(
        {"Book A": [_result("b520", "1", "Book A", "Chapter 2")]},
        errors={"Book B": RuntimeError("site down")},
    )
    _seed_books(
        monkeypatch,
        tmp_path,
        [("b520", "1", "Book A", "Chapter 1"), ("qidian", "2", "Book B", "")],
        stub,
    )
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json", "--all", "--download"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    out = captured.out
    # Exactly one document, one line, JSON only.
    assert out.count("\n") == 1
    assert out.endswith("\n")
    payload = json.loads(out)
    assert payload["books"][0]["command"] == "novel-cli download --site b520 1"
    # The hint lives in the document; it is not repeated on stderr, and no
    # terminal escape sequence leaks into the diagnostic stream.
    assert "novel-cli download --site b520 1" not in captured.err
    assert "does not start downloads" not in captured.err
    assert not _ANSI_RE.search(captured.err)


# ---------------------------------------------------------------------------
# --download --wait
# ---------------------------------------------------------------------------


def _two_books_with_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> WatchlistStore:
    stub = _SearchStub(
        {
            "Book A": [_result("b520", "1", "Book A", "Chapter 2")],
            "Book B": [_result("qidian", "2", "Book B", "Chapter 2")],
        }
    )
    return _seed_books(
        monkeypatch,
        tmp_path,
        [("b520", "1", "Book A", "Chapter 1"), ("qidian", "2", "Book B", "Chapter 1")],
        stub,
    )


def test_check_wait_queues_sequentially_and_exits_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _two_books_with_updates(monkeypatch, tmp_path)
    manager = _FakeManager(
        script={
            "1": [Status.RUNNING, Status.COMPLETED],
            "2": [Status.RUNNING, Status.COMPLETED],
        }
    )
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.0)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait", "--wait-timeout", "10"])
    args.func(args)  # all completed: no SystemExit

    # Strictly one at a time, in follow-list order.
    assert manager.added == [("b520", "1"), ("qidian", "2")]
    assert manager.cancel_calls == []
    assert manager.close_calls == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # the wait mode has no machine-readable stream
    assert "Download summary: 2 completed, 0 cancelled, 0 failed." in captured.err


def test_check_wait_timeout_cancels_the_book_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _two_books_with_updates(monkeypatch, tmp_path)
    # Book 1 never finishes; book 2 completes on the first poll.
    manager = _FakeManager(script={"1": [Status.RUNNING], "2": [Status.COMPLETED]})
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.01)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait", "--wait-timeout", "0.1"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    assert manager.added == [("b520", "1"), ("qidian", "2")]
    assert manager.cancel_calls == ["task-1"]
    assert manager.close_calls == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Timed out waiting for b520/1 after 0.1 seconds." in captured.err
    # The timeout does not leak into the next book's budget.
    assert manager.tasks[1].status is Status.COMPLETED


def test_check_wait_timeout_reports_cancelled_in_the_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_books_with_updates(monkeypatch, tmp_path)
    manager = _FakeManager(script={"1": [Status.RUNNING], "2": [Status.COMPLETED]})
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.01)
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait", "--wait-timeout", "0.1"])
    with pytest.raises(SystemExit):
        args.func(args)

    joined = "\n".join(messages)
    assert "Timed out waiting for b520/1 after 0.1 seconds." in joined
    assert "Download completed: qidian/2" in joined
    assert "1 completed, 1 cancelled, 0 failed" in joined


def test_check_wait_download_failure_exits_one_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_books_with_updates(monkeypatch, tmp_path)
    manager = _FakeManager(
        script={"1": [Status.RUNNING, Status.FAILED], "2": [Status.COMPLETED]},
        errors={"1": "boom"},
    )
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.0)
    messages = _capture_ui(monkeypatch, follow)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait", "--wait-timeout", "5"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
    assert manager.added == [("b520", "1"), ("qidian", "2")]
    joined = "\n".join(messages)
    assert "Download failed for b520/1: boom" in joined
    assert "Download completed: qidian/2" in joined
    assert "1 completed, 0 cancelled, 1 failed" in joined


def test_check_json_cannot_be_combined_with_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    manager = _FakeManager()
    _patch_manager(monkeypatch, manager)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json", "--download", "--wait"])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--json cannot be combined with --wait." in captured.err
    assert manager.close_calls == 0


# ---------------------------------------------------------------------------
# idempotency and concurrency
# ---------------------------------------------------------------------------


def test_repeated_check_does_not_queue_an_already_recorded_chapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub = _SearchStub({"Book A": [_result("b520", "1", "Book A", "Chapter 2")]})
    store = _seed_books(
        monkeypatch, tmp_path, [("b520", "1", "Book A", "Chapter 1")], stub
    )
    first = _FakeManager(script={"1": [Status.RUNNING, Status.COMPLETED]})
    _patch_manager(monkeypatch, first)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.0)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait"])
    args.func(args)
    assert first.added == [("b520", "1")]
    assert store.get("b520", "1").last_chapter == "Chapter 2"

    # Second sweep: the chapter is already recorded, so nothing is queued.
    second = _FakeManager()
    _patch_manager(monkeypatch, second)
    args = parser.parse_args(["check", "--download", "--wait"])
    args.func(args)  # up to date: no failure, no queue

    assert second.added == []
    assert second.close_calls == 0


def test_check_bounds_search_concurrency_and_keeps_list_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    store = WatchlistStore()
    for index in range(8):
        store.add("b520", str(index), title=f"Book {index}", last_chapter="")

    in_flight = 0
    peak = 0

    async def search(keyword: str, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        index = keyword.split()[-1]
        return [_result("b520", index, keyword, "Chapter 1")]

    monkeypatch.setattr(search_module, "search", search)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--json"])
    args.func(args)

    payload, _err = _read_json_document(capsys)
    assert payload["checked"] == 8
    assert [book["book_id"] for book in payload["books"]] == [
        str(index) for index in range(8)
    ]
    # ``DEFAULT_CHECK_CONCURRENCY`` is 4: bounded, but not serialized.
    assert 2 <= peak <= 4


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


def test_ctrl_c_during_wait_cancels_only_the_in_flight_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Ctrl+C reaches the entry point as ``KeyboardInterrupt`` (exit 1,
    ``Aborted.``). Inside the command the manager must be closed, which
    cancels the running book; the later books were never queued.
    """
    _two_books_with_updates(monkeypatch, tmp_path)
    # First snapshot sees the running download; the next one is interrupted.
    manager = _FakeManager(script={"1": [Status.RUNNING]}, interrupt_after=1)
    _patch_manager(monkeypatch, manager)
    monkeypatch.setattr(follow, "_WAIT_POLL_INTERVAL", 0.0)
    parser = _parser(FollowCheckCmd)

    args = parser.parse_args(["check", "--download", "--wait", "--wait-timeout", "30"])
    with pytest.raises(KeyboardInterrupt):
        args.func(args)

    assert manager.added == [("b520", "1")]  # book 2 never entered the queue
    assert manager.close_calls == 1
    assert manager.closed == ["task-1"]
    assert manager.tasks[0].status is Status.CANCELLED
    assert manager.tasks[0].error == "Cancelled by user"
    # No summary is printed for an aborted run.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Download summary" not in captured.err
