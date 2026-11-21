#!/usr/bin/env python3
"""
tests.apps.test_doctor_storage
------------------------------

Contract tests for the ``doctor`` chapter-storage check.

The check is advisory and strictly read-only: it reports whether ``raw_data``
exists and is writable, counts the chapter databases, samples a few of them
with ``PRAGMA quick_check`` and ``PRAGMA user_version``, and warns about a
missing WAL checkpoint or a nearly full volume.  It must never open a database
with write semantics, so no ``-wal``/``-shm`` sidecar may appear and existing
bytes must stay identical.

Every test works under a temporary ``raw_data`` directory and pins free space,
so nothing depends on the host's real disk or the user's real data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_downloader.apps.cli.commands import doctor
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd
from novel_downloader.infra.persistence.chapter_storage import SCHEMA_VERSION

#: Free space reported by the stubbed ``disk_free`` (8 GiB, never "low").
PLENTY_FREE = 8 * 1024 * 1024 * 1024


@pytest.fixture(autouse=True)
def _plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the disk-space warning out of every test that does not set it."""
    monkeypatch.setattr(doctor, "disk_free", lambda _path: PLENTY_FREE)


def _db(root: Path, site: str, book: str) -> Path:
    return root / site / book / "chapter.raw.sqlite"


def _healthy_db(
    path: Path, *, schema_version: int = SCHEMA_VERSION, rows: int = 1
) -> None:
    """Create a small, valid chapter database (default rollback journal)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO chapters VALUES (?, ?, ?)",
            [(f"c{index}", f"t{index}", f"body {index}") for index in range(rows)],
        )
        conn.execute(f"PRAGMA user_version = {schema_version}")
        conn.commit()
    finally:
        conn.close()


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Return ``relative path -> (size, sha256)`` for every file under root."""
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = (path.stat().st_size, digest)
    return result


# ---------------------------------------------------------------------------
# Basic states
# ---------------------------------------------------------------------------


def test_storage_missing_directory_is_advisory(tmp_path: Path) -> None:
    root = tmp_path / "missing"

    check = doctor._check_storage(root)

    assert check.ok is True
    assert check.fatal is False
    assert check.key == "storage"
    assert check.data is not None
    assert check.data["exists"] is False
    assert check.data["writable"] is False
    assert check.data["databases"] == 0
    assert check.data["bytes"] == 0
    assert check.data["free_bytes"] == PLENTY_FREE


def test_storage_empty_directory_is_healthy(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    root.mkdir()

    check = doctor._check_storage(root)

    assert check.ok is True
    assert check.data is not None
    assert check.data["exists"] is True
    assert check.data["writable"] is True
    assert check.data["databases"] == 0
    assert check.data["sampled"] == 0
    assert check.data["corrupt"] == 0
    assert check.data["schema_unsupported"] == 0


def test_storage_healthy_databases_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    _healthy_db(_db(root, "site", "book-a"))
    _healthy_db(_db(root, "site", "book-b"))

    check = doctor._check_storage(root)

    assert check.ok is True
    assert check.data is not None
    assert check.data["databases"] == 2
    assert check.data["bytes"] > 0
    assert check.data["sampled"] == 2
    assert check.data["checked"] == 2
    assert check.data["corrupt"] == 0
    assert check.data["schema_unsupported"] == 0
    assert check.data["problems"] == []


def test_storage_corrupt_database_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    broken = _db(root, "site", "book")
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a sqlite database")

    check = doctor._check_storage(root)

    assert check.ok is False
    assert check.data is not None
    assert check.data["corrupt"] == 1
    assert check.data["schema_unsupported"] == 0
    problems = check.data["problems"]
    assert isinstance(problems, list)
    assert problems[0]["category"] == "integrity_failed"
    assert check.suggestion


def test_storage_future_schema_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    _healthy_db(_db(root, "site", "book"), schema_version=SCHEMA_VERSION + 1)

    check = doctor._check_storage(root)

    assert check.ok is False
    assert check.data is not None
    assert check.data["corrupt"] == 0
    assert check.data["schema_unsupported"] == 1
    problems = check.data["problems"]
    assert isinstance(problems, list)
    assert problems[0]["category"] == "schema_unsupported"
    assert str(SCHEMA_VERSION) in problems[0]["reason"]


def test_storage_read_only_directory_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "raw"
    _healthy_db(_db(root, "site", "book"))
    monkeypatch.setattr(doctor, "_storage_dir_writable", lambda _path: False)

    check = doctor._check_storage(root)

    assert check.ok is False
    assert check.data is not None
    assert check.data["writable"] is False
    assert check.data["databases"] == 1
    assert "writable" in check.detail


def test_storage_low_free_space_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "raw"
    _healthy_db(_db(root, "site", "book"))
    monkeypatch.setattr(doctor, "disk_free", lambda _path: 1024)

    check = doctor._check_storage(root)

    assert check.ok is False
    assert check.data is not None
    assert check.data["free_bytes"] == 1024
    assert check.data["low_free_bytes"] == doctor.LOW_DISK_FREE_BYTES
    assert "free" in check.detail


# ---------------------------------------------------------------------------
# Sampling and read-only guarantees
# ---------------------------------------------------------------------------


def test_storage_sampling_is_capped(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    for index in range(6):
        _healthy_db(_db(root, "site", f"book-{index}"))

    # ``small_bytes=0`` makes every database "large", forcing the sample path.
    check = doctor._check_storage(root, sample_limit=2, small_bytes=0)

    assert check.data is not None
    assert check.data["databases"] == 6
    assert check.data["sampled"] == 2
    assert check.data["checked"] == 2


def test_storage_sampling_inspects_a_small_library_in_full(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    for index in range(5):
        _healthy_db(_db(root, "site", f"book-{index}"))

    check = doctor._check_storage(root, sample_limit=3)

    assert check.data is not None
    assert check.data["databases"] == 5
    assert check.data["sampled"] == 5


def test_storage_check_is_read_only_and_creates_no_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    database = _db(root, "site", "book")
    # WAL journal mode is recorded in the file header even after a clean close
    # removed the sidecars; ``mode=ro`` alone would recreate them here.
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(database))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE chapters (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO chapters VALUES ('c1')")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    before = _snapshot(root)
    check = doctor._check_storage(root)
    after = _snapshot(root)

    assert check.ok is True
    assert check.data is not None
    assert check.data["checked"] == 1
    assert before == after
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_storage_wal_is_skipped_without_creating_shm(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    database = _db(root, "site", "book")
    _healthy_db(database)
    # A short junk WAL is not "damaged" by the checksum walk; it is simply not
    # safe to open without letting SQLite create a -shm sidecar.
    Path(f"{database}-wal").write_bytes(b"junk-wal")

    before = _snapshot(root)
    check = doctor._check_storage(root)
    after = _snapshot(root)

    assert check.ok is True
    assert check.data is not None
    assert check.data["sampled"] == 1
    assert check.data["checked"] == 0
    assert check.data["skipped"] == 1
    not_opened = check.data["not_opened"]
    assert isinstance(not_opened, list)
    assert not_opened[0]["reason"] == "wal_pending"
    assert before == after
    assert not Path(f"{database}-shm").exists()


def test_storage_wal_on_read_only_database_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "raw"
    database = _db(root, "site", "book")
    _healthy_db(database)
    Path(f"{database}-wal").write_bytes(b"junk-wal")
    monkeypatch.setattr(doctor, "_storage_file_writable", lambda _path: False)

    check = doctor._check_storage(root)

    assert check.ok is False
    assert check.data is not None
    assert check.data["wal_read_only"] == 1


# ---------------------------------------------------------------------------
# JSON/human agreement
# ---------------------------------------------------------------------------


def _doctor_args(*, as_json: bool) -> SimpleNamespace:
    return SimpleNamespace(port=1, as_json=as_json, online=False)


def _captured_ui(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    messages: dict[str, list[str]] = {
        "info": [],
        "success": [],
        "warn": [],
        "error": [],
    }
    for name, sink in messages.items():
        monkeypatch.setattr(doctor.ui, name, sink.append)
    return messages


def test_storage_json_and_human_report_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "raw"
    _healthy_db(_db(root, "site", "book-a"))
    _healthy_db(_db(root, "site", "book-b"))
    check = doctor._check_storage(root)
    assert isinstance(check, Check)
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: [check])

    DoctorCmd.run(_doctor_args(as_json=True))
    payload = json.loads(capsys.readouterr().out)
    row = payload["checks"][0]
    assert row["key"] == "storage"
    data = row["data"]
    stable = {
        "path",
        "exists",
        "writable",
        "databases",
        "bytes",
        "sampled",
        "checked",
        "corrupt",
        "schema_unsupported",
        "skipped",
        "wal_read_only",
        "free_bytes",
        "low_free_bytes",
        "problems",
        "not_opened",
    }
    assert stable <= set(data)
    assert data["databases"] == 2
    assert data["corrupt"] == 0
    assert data["schema_unsupported"] == 0
    assert data["writable"] is True
    assert data["free_bytes"] == PLENTY_FREE

    captured = _captured_ui(monkeypatch)
    DoctorCmd.run(_doctor_args(as_json=False))
    line = next(text for text in captured["success"] if "Chapter storage" in text)
    assert "2 database(s)" in line
    assert doctor.format_size(data["bytes"]) in line
    assert "problem" not in line.lower()
    assert any("All checks passed" in text for text in captured["success"])
