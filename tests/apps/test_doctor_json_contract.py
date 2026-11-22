#!/usr/bin/env python3
"""
Snapshot contract for ``novel-cli doctor --json``.

``doctor --json`` is a machine interface: scripts key off ``status``, pick a
row by its stable ``key`` and read the structured ``data``.  The document must
not drift silently, so these tests freeze

* the top-level field set (and the six fields ``--fix`` adds);
* the 16 offline check keys, their order, and the one row ``--online`` adds;
* the fields every check row carries, and the optional ones;
* the exact ``data`` field set of each check that publishes structured data
  (``data`` may be null or absent, but when present its keys are fixed);
* the JSON framing (one parseable line, no ANSI, no credential/path leaks);
* the ``status`` / exit-code agreement for required and optional failures.

The expected field sets are intentionally written out by hand: adding or
removing a field must update this file, which is the point of the snapshot.

The real ``collect_checks`` run is isolated to ``tmp_path`` (cwd and
``NOVEL_HOME``), so it creates no directories in the developer's profile and
never touches real user data.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_downloader.apps.cli.commands import doctor
from novel_downloader.apps.cli.commands.doctor import Check, DoctorCmd
from novel_downloader.plugins.search_cache import SearchCache

#: Module invoked by the subprocess harness (same entry point as ``novel-cli``).
CLI_MODULE = "novel_downloader.apps.cli.main"

#: Free space the storage/disk-space probes report (8 GiB, never "low").
PLENTY_FREE = 8 * 1024 * 1024 * 1024

#: The offline check keys, in display order.  This is the 16-check contract.
OFFLINE_KEYS: tuple[str, ...] = (
    "python",
    "package",
    "settings",
    "config",
    "dependencies",
    "output_dir",
    "log_dir",
    "disk_space",
    "cache",
    "storage",
    "search_cache",
    "sites",
    "site_plugins",
    "searchers",
    "music",
    "api_port",
)

#: ``--online`` inserts the egress probe immediately before the port check.
ONLINE_KEYS: tuple[str, ...] = (*OFFLINE_KEYS[:-1], "network", OFFLINE_KEYS[-1])

#: Fields every row of a real doctor run carries.
ROW_REQUIRED = frozenset({"key", "name", "ok", "fatal", "detail", "elapsed"})

#: Fields a row may additionally carry.
ROW_OPTIONAL = frozenset({"suggestion", "data"})

#: No other row field is part of the contract.
ROW_ALLOWED = ROW_REQUIRED | ROW_OPTIONAL

#: Top-level fields of a default (non-fix) document.
BASE_TOP_LEVEL = frozenset({"status", "warnings", "summary", "checks"})

#: Extra top-level fields ``--fix --json`` appends.
FIX_TOP_LEVEL = frozenset(
    {"dry_run", "max_age_days", "fixed", "skipped", "errors", "fix_summary"}
)

#: ``summary`` is an object with exactly these keys.
SUMMARY_FIELDS = frozenset({"total", "failures", "optional_failures", "ok"})

#: Allowed ``status`` values.
STATUSES = frozenset({"ok", "warnings", "failed"})

#: Documentation-stable data field sets.  ``None`` means the check has no
#: structured payload.  A null/absent ``data`` is allowed; when an object is
#: present its field set must be exactly the one below.
DATA_KEYS: dict[str, frozenset[str] | None] = {
    "python": None,
    "package": frozenset({"version", "installed_metadata"}),
    "settings": None,
    "config": frozenset({"path", "problems"}),
    "dependencies": frozenset({"dependencies", "python"}),
    "output_dir": None,
    "log_dir": None,
    "disk_space": frozenset({"path", "free_bytes", "minimum_bytes"}),
    "cache": frozenset({"path", "files", "bytes"}),
    "storage": frozenset(
        {
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
    ),
    "search_cache": frozenset(
        {
            "memory_entries",
            "expired_entries",
            "disk_enabled",
            "disk_entries",
            "hits",
            "misses",
            "evictions",
            "fingerprint",
        }
    ),
    "sites": None,
    "site_plugins": frozenset({"sampled", "failed"}),
    "searchers": None,
    "music": None,
    "api_port": None,
    "network": frozenset({"endpoints", "timeout"}),
}

#: ``storage.problems[].category`` values.
STORAGE_CATEGORIES = frozenset(
    {
        "integrity_failed",
        "schema_unsupported",
        "wal_damaged",
        "database_error",
        "io_error",
    }
)

#: ``storage.not_opened[].reason`` values.
STORAGE_SKIP_REASONS = frozenset({"wal_pending", "hot_journal"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _doctor_args(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "port": _free_port(),
        "as_json": True,
        "online": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _assert_keys(value: object, expected: frozenset[str], label: str) -> None:
    assert isinstance(value, dict), f"{label}: expected an object"
    assert set(value) == set(expected), (
        f"{label}: {sorted(value)} != {sorted(expected)}"
    )


def _assert_int(value: object, label: str) -> None:
    assert isinstance(value, int) and not isinstance(value, bool), label


def _assert_optional_int(value: object, label: str) -> None:
    assert value is None or (isinstance(value, int) and not isinstance(value, bool)), (
        label
    )


def _assert_str(value: object, label: str) -> None:
    assert isinstance(value, str), label


def _validate_package_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["package"] or frozenset(), "package.data")
    for field in ("version", "installed_metadata"):
        _assert_str(data[field], f"package.data.{field}")


def _validate_config_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["config"] or frozenset(), "config.data")
    _assert_str(data["path"], "config.data.path")
    problems = data["problems"]
    assert isinstance(problems, list), "config.data.problems"
    for index, problem in enumerate(problems):
        label = f"config.data.problems[{index}]"
        _assert_keys(
            problem, frozenset({"level", "path", "message", "fix_hint"}), label
        )
        assert problem["level"] in {"error", "warning", "info"}, label
        for field in ("path", "message", "fix_hint"):
            _assert_str(problem[field], f"{label}.{field}")


def _validate_dependencies_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["dependencies"] or frozenset(), "dependencies.data")
    rows = data["dependencies"]
    assert isinstance(rows, list) and rows, "dependencies.data.dependencies"
    for index, row in enumerate(rows):
        label = f"dependencies.data.dependencies[{index}]"
        _assert_keys(
            row,
            frozenset({"module", "dist", "optional", "installed", "version"}),
            label,
        )
        _assert_str(row["module"], f"{label}.module")
        _assert_str(row["dist"], f"{label}.dist")
        assert isinstance(row["optional"], bool), f"{label}.optional"
        assert isinstance(row["installed"], bool), f"{label}.installed"
        assert row["version"] is None or isinstance(row["version"], str), (
            f"{label}.version"
        )
    python_info = data["python"]
    _assert_keys(
        python_info, frozenset({"version", "implementation", "minimum"}), "python"
    )
    for field in ("version", "implementation", "minimum"):
        _assert_str(python_info[field], f"dependencies.data.python.{field}")


def _validate_disk_space_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["disk_space"] or frozenset(), "disk_space.data")
    _assert_str(data["path"], "disk_space.data.path")
    _assert_optional_int(data["free_bytes"], "disk_space.data.free_bytes")
    _assert_int(data["minimum_bytes"], "disk_space.data.minimum_bytes")


def _validate_cache_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["cache"] or frozenset(), "cache.data")
    _assert_str(data["path"], "cache.data.path")
    _assert_int(data["files"], "cache.data.files")
    _assert_int(data["bytes"], "cache.data.bytes")


def _validate_storage_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["storage"] or frozenset(), "storage.data")
    _assert_str(data["path"], "storage.data.path")
    for field in ("exists", "writable"):
        assert isinstance(data[field], bool), f"storage.data.{field}"
    for field in (
        "databases",
        "bytes",
        "sampled",
        "checked",
        "corrupt",
        "schema_unsupported",
        "skipped",
        "wal_read_only",
        "low_free_bytes",
    ):
        _assert_int(data[field], f"storage.data.{field}")
    _assert_optional_int(data["free_bytes"], "storage.data.free_bytes")

    problems = data["problems"]
    assert isinstance(problems, list), "storage.data.problems"
    for index, problem in enumerate(problems):
        label = f"storage.data.problems[{index}]"
        _assert_keys(problem, frozenset({"path", "category", "reason"}), label)
        _assert_str(problem["path"], f"{label}.path")
        assert problem["category"] in STORAGE_CATEGORIES, label
        _assert_str(problem["reason"], f"{label}.reason")

    not_opened = data["not_opened"]
    assert isinstance(not_opened, list), "storage.data.not_opened"
    for index, item in enumerate(not_opened):
        label = f"storage.data.not_opened[{index}]"
        _assert_keys(item, frozenset({"path", "reason", "detail"}), label)
        _assert_str(item["path"], f"{label}.path")
        assert item["reason"] in STORAGE_SKIP_REASONS, label
        _assert_str(item["detail"], f"{label}.detail")


def _validate_search_cache_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["search_cache"] or frozenset(), "search_cache.data")
    for field in (
        "memory_entries",
        "expired_entries",
        "disk_entries",
        "hits",
        "misses",
        "evictions",
    ):
        _assert_int(data[field], f"search_cache.data.{field}")
    assert isinstance(data["disk_enabled"], bool), "search_cache.data.disk_enabled"
    _assert_str(data["fingerprint"], "search_cache.data.fingerprint")


def _validate_site_plugins_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["site_plugins"] or frozenset(), "site_plugins.data")
    for field in ("sampled", "failed"):
        values = data[field]
        assert isinstance(values, list), f"site_plugins.data.{field}"
        for index, value in enumerate(values):
            _assert_str(value, f"site_plugins.data.{field}[{index}]")


def _validate_network_data(data: dict[str, Any]) -> None:
    _assert_keys(data, DATA_KEYS["network"] or frozenset(), "network.data")
    endpoints = data["endpoints"]
    assert isinstance(endpoints, dict), "network.data.endpoints"
    for url, result in endpoints.items():
        label = f"network.data.endpoints[{url!r}]"
        _assert_str(url, label)
        _assert_keys(result, frozenset({"ok", "status"}), label)
        assert isinstance(result["ok"], bool), f"{label}.ok"
        _assert_str(result["status"], f"{label}.status")
    timeout = data["timeout"]
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool), (
        "network.data.timeout"
    )


_DATA_VALIDATORS = {
    "package": _validate_package_data,
    "config": _validate_config_data,
    "dependencies": _validate_dependencies_data,
    "disk_space": _validate_disk_space_data,
    "cache": _validate_cache_data,
    "storage": _validate_storage_data,
    "search_cache": _validate_search_cache_data,
    "site_plugins": _validate_site_plugins_data,
    "network": _validate_network_data,
}


def _validate_row(row: dict[str, Any]) -> None:
    """Assert one ``checks[]`` entry against the frozen row contract."""
    assert set(row) <= ROW_ALLOWED, (
        f"unexpected row fields: {sorted(set(row) - ROW_ALLOWED)}"
    )
    assert set(row) >= ROW_REQUIRED, (
        f"missing row fields: {sorted(ROW_REQUIRED - set(row))}"
    )

    key = row["key"]
    assert key in DATA_KEYS, f"unknown check key: {key!r}"
    _assert_str(row["name"], f"{key}.name")
    assert row["name"], f"{key}.name"
    assert isinstance(row["ok"], bool), f"{key}.ok"
    assert isinstance(row["fatal"], bool), f"{key}.fatal"
    _assert_str(row["detail"], f"{key}.detail")
    assert row["detail"], f"{key}.detail"
    elapsed = row["elapsed"]
    assert isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool), (
        f"{key}.elapsed"
    )
    assert elapsed >= 0, f"{key}.elapsed"
    if "suggestion" in row:
        _assert_str(row["suggestion"], f"{key}.suggestion")
        assert row["suggestion"], f"{key}.suggestion"

    expected = DATA_KEYS[key]
    data = row.get("data")
    if expected is None:
        assert data is None, f"{key} must not publish data"
        return
    if data is None:
        # The implementation omits ``data`` when a check degrades; the
        # contract also tolerates an explicit null.
        return
    _assert_keys(data, expected, f"{key}.data")
    validator = _DATA_VALIDATORS.get(key)
    if validator is not None:
        validator(cast("dict[str, Any]", data))


def _validate_summary(payload: dict[str, Any]) -> None:
    checks = payload["checks"]
    assert isinstance(checks, list)
    failures = sum(1 for row in checks if not row["ok"] and row["fatal"])
    optional = sum(1 for row in checks if not row["ok"] and not row["fatal"])
    _assert_keys(payload["summary"], SUMMARY_FIELDS, "summary")
    assert payload["summary"] == {
        "total": len(checks),
        "failures": failures,
        "optional_failures": optional,
        "ok": failures == 0,
    }
    assert payload["warnings"] == optional
    assert isinstance(payload["warnings"], int) and not isinstance(
        payload["warnings"], bool
    )
    expected_status = "failed" if failures else ("warnings" if optional else "ok")
    assert payload["status"] == expected_status
    assert payload["status"] in STATUSES


# ---------------------------------------------------------------------------
# Isolation fixture: a full, real collect_checks run inside tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every side effect of a real doctor run at ``tmp_path``.

    The sample configuration makes the ``settings`` and ``config`` checks
    succeed with their documented ``data`` published.  Directory probes create
    ``./downloads`` / ``./logs`` inside ``tmp_path``; ``raw_data`` stays
    missing (the documented fresh-install state); free space and the search
    cache are pinned so the run does not depend on the host.
    """
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))

    sample = (
        files("novel_downloader.resources")
        .joinpath("config")
        .joinpath("settings.sample.toml")
        .read_text(encoding="utf-8")
    )
    (tmp_path / "settings.toml").write_text(sample, encoding="utf-8")

    monkeypatch.setattr(doctor, "_raw_data_dir", lambda: tmp_path / "raw_data")
    monkeypatch.setattr(doctor, "disk_free", lambda _path: PLENTY_FREE)
    monkeypatch.setattr(
        doctor,
        "_search_cache",
        lambda: SearchCache(disk_dir="", now=lambda: 1.0, wall_clock=lambda: 1.0),
    )
    monkeypatch.setattr(doctor, "_search_cache_fingerprint", lambda: "0123456789abcdef")
    monkeypatch.setattr(
        doctor,
        "_check_network",
        lambda: Check(
            "Network egress",
            True,
            "1/1 endpoint(s) reachable",
            fatal=False,
            key="network",
            data={
                "endpoints": {
                    "https://example.com": {"ok": True, "status": "HTTP 200"}
                },
                "timeout": doctor.PROBE_TIMEOUT,
            },
        ),
    )
    return {"root": tmp_path, "home": home}


# ---------------------------------------------------------------------------
# Field contract
# ---------------------------------------------------------------------------


def test_collect_checks_key_set_order_and_row_shape_are_frozen(
    isolated_doctor: dict[str, Path],
) -> None:
    checks = doctor.collect_checks(port=_free_port())

    assert tuple(check.key for check in checks) == OFFLINE_KEYS
    assert len(checks) == 16
    assert len({check.key for check in checks}) == 16
    assert {"storage", "search_cache"} <= {check.key for check in checks}

    for check in checks:
        assert check.elapsed is not None, f"{check.key} was not timed"
        _validate_row(check.to_dict())

    online = doctor.collect_checks(port=_free_port(), online=True)
    assert tuple(check.key for check in online) == ONLINE_KEYS
    for check in online:
        _validate_row(check.to_dict())


def test_package_data_appears_only_when_installed_metadata_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader import __version__

    monkeypatch.setattr(doctor, "dist_version", lambda _name: "0.0.0-bogus")
    drifted = doctor._check_package().to_dict()
    assert set(drifted["data"]) == DATA_KEYS["package"]
    assert drifted["data"]["version"] == __version__
    assert drifted["data"]["installed_metadata"] == "0.0.0-bogus"

    monkeypatch.setattr(doctor, "dist_version", lambda _name: __version__)
    aligned = doctor._check_package().to_dict()
    assert aligned.get("data") is None


def test_search_cache_failure_publishes_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnreadableCache:
        def diagnostics(self) -> Any:
            raise PermissionError("permission denied by contract test")

    monkeypatch.setattr(doctor, "_search_cache", lambda: _UnreadableCache())
    row = doctor._check_search_cache().to_dict()

    assert row["key"] == "search_cache"
    assert row["ok"] is False
    assert row["fatal"] is False
    assert row.get("data") is None


# ---------------------------------------------------------------------------
# Top-level document shape
# ---------------------------------------------------------------------------


def test_default_document_has_exactly_the_base_top_level_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = [
        Check("Python version", True, "3.12.0", key="python", elapsed=0.001),
        Check(
            "Chapter storage",
            False,
            "1 problem(s)",
            "run storage verify",
            fatal=False,
            key="storage",
            elapsed=0.002,
            data=_storage_data(),
        ),
    ]
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: checks)

    DoctorCmd.run(_doctor_args(as_json=True))

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == BASE_TOP_LEVEL
    assert [row["key"] for row in payload["checks"]] == ["python", "storage"]
    for row in payload["checks"]:
        _validate_row(row)
    _validate_summary(payload)
    assert payload["status"] == "warnings"
    assert payload["summary"]["failures"] == 0
    assert payload["summary"]["optional_failures"] == 1
    assert payload["summary"]["ok"] is True


def test_fix_json_adds_exactly_the_documented_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "fix-root"
    monkeypatch.setattr(doctor, "_output_dir", lambda: root / "downloads")
    monkeypatch.setattr(doctor, "_log_dir", lambda: root / "logs")
    monkeypatch.setattr(doctor, "_cache_dir", lambda: root / "cache")
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda *_a, **_k: [
            Check("Python version", True, "3.12.0", key="python", elapsed=0.001)
        ],
    )

    DoctorCmd.run(_doctor_args(as_json=True, fix=True, dry_run=True))

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == BASE_TOP_LEVEL | FIX_TOP_LEVEL
    _validate_summary(payload)
    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    assert payload["max_age_days"] == doctor.DEFAULT_FIX_MAX_AGE_DAYS
    assert payload["fixed"] == []
    assert payload["errors"] == []
    assert isinstance(payload["skipped"], list)
    assert payload["fix_summary"] == {
        "fixed": 0,
        "skipped": len(payload["skipped"]),
        "errors": 0,
    }
    for record in payload["skipped"]:
        assert set(record) == {"action", "path", "reason"}
        assert record["action"] == doctor.FIX_CREATE_DIR
        assert record["reason"] == doctor.FIX_DRY_RUN


# ---------------------------------------------------------------------------
# status / exit code agreement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("checks", "status", "exit_code", "failures", "optional_failures"),
    [
        (
            [Check("Python version", True, "3.12.0", key="python")],
            "ok",
            None,
            0,
            0,
        ),
        (
            [
                Check("Python version", True, "3.12.0", key="python"),
                Check("Disk space", False, "low", fatal=False, key="disk_space"),
            ],
            "warnings",
            None,
            0,
            1,
        ),
        (
            [
                Check("Python version", True, "3.12.0", key="python"),
                Check("Configuration", False, "broken", key="config"),
            ],
            "failed",
            1,
            1,
            0,
        ),
    ],
)
def test_status_and_exit_code_agree_with_fatal_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checks: list[Check],
    status: str,
    exit_code: int | None,
    failures: int,
    optional_failures: int,
) -> None:
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: checks)

    if exit_code is None:
        DoctorCmd.run(_doctor_args(as_json=True))
    else:
        with pytest.raises(SystemExit) as excinfo:
            DoctorCmd.run(_doctor_args(as_json=True))
        assert excinfo.value.code == exit_code

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
    assert payload["warnings"] == optional_failures
    assert payload["summary"]["failures"] == failures
    assert payload["summary"]["optional_failures"] == optional_failures
    assert payload["summary"]["ok"] is (failures == 0)


# ---------------------------------------------------------------------------
# Framing, encoding, and redaction
# ---------------------------------------------------------------------------


def test_json_is_one_parseable_line_without_ansi_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ``_mask_path_text`` turns paths under the cwd into ``./...``; running
    # inside ``tmp_path`` keeps the assertion portable (POSIX ``/tmp`` only
    # keeps its tail behind ``<abs>``).
    monkeypatch.chdir(tmp_path)
    secret = "topsecret-token-2f9c"
    leaked = (
        f"配置错误: token={secret} password: {secret} "
        f"via http://alice:{secret}@proxy.local:8080 ({tmp_path / 'private'})"
    )
    checks = [
        Check(
            "Configuration",
            False,
            leaked,
            key="config",
            elapsed=0.001,
            data={
                "path": str(tmp_path / "private" / "settings.toml"),
                "problems": [
                    {
                        "level": "error",
                        "path": "general.output_dir",
                        "message": leaked,
                        "fix_hint": f"password: {secret}",
                    }
                ],
            },
        )
    ]
    monkeypatch.setattr(doctor, "collect_checks", lambda *_a, **_k: checks)

    with pytest.raises(SystemExit) as excinfo:
        DoctorCmd.run(_doctor_args(as_json=True))
    assert excinfo.value.code == 1

    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert len(out.splitlines()) == 1
    assert "\r" not in out
    assert "\x1b" not in out

    payload = json.loads(out)
    assert set(payload) == BASE_TOP_LEVEL
    row = payload["checks"][0]
    _validate_row(row)
    assert "配置错误" in out  # ensure_ascii=False keeps CJK readable
    for marker in (secret, "s3cr3t", str(tmp_path)):
        assert marker not in out
    # The data object keeps its documented shape after redaction.
    assert set(row["data"]) == DATA_KEYS["config"]
    assert row["data"]["problems"][0]["message"].startswith("配置错误")


def test_a_fresh_process_emits_the_same_contract(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "NOVEL_HOME": str(home),
            "NOVEL_LANG": "en_US",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "COLUMNS": "200",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "doctor", "--json"],
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240.0,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    out = result.stdout
    assert out.endswith("\n")
    assert len(out.splitlines()) == 1
    assert "\x1b" not in out
    assert "Traceback" not in out

    payload = json.loads(out)
    assert set(payload) == BASE_TOP_LEVEL
    assert tuple(row["key"] for row in payload["checks"]) == OFFLINE_KEYS
    for row in payload["checks"]:
        _validate_row(row)
    _validate_summary(payload)
    # An empty home has no settings file, so a required check must fail.
    assert payload["status"] == "failed"
    assert payload["summary"]["ok"] is False


# ---------------------------------------------------------------------------
# Sample payload used by the top-level shape test
# ---------------------------------------------------------------------------


def _storage_data() -> dict[str, Any]:
    return {
        "path": "./raw_data",
        "exists": True,
        "writable": True,
        "databases": 1,
        "bytes": 4096,
        "sampled": 1,
        "checked": 1,
        "corrupt": 1,
        "schema_unsupported": 0,
        "skipped": 0,
        "wal_read_only": 0,
        "free_bytes": PLENTY_FREE,
        "low_free_bytes": doctor.LOW_DISK_FREE_BYTES,
        "problems": [
            {
                "path": "./raw_data/site/book/chapter.raw.sqlite",
                "category": "integrity_failed",
                "reason": "quick_check failed",
            }
        ],
        "not_opened": [],
    }
