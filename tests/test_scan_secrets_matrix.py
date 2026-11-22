"""False-positive / false-negative matrix for ``scripts/scan_secrets.py``.

Token-shaped literals are assembled from fragments at runtime so this file
itself does not contain a contiguous credential.  That matters: the ``tests/*``
allowlist only relaxes the heuristic rules (generic assignment, Bearer, proxy
userinfo), so a literal private key, AWS key, JWT or webhook here would be a
real finding when the repository is scanned.

Each section states the contract it pins:

* every documented rule fires on a realistic shape;
* placeholders, references, hashes, UUIDs, base64 images and doc examples do
  not fire;
* binary/oversized/legacy-encoded files and untracked build directories are
  handled without decoding garbage;
* output is location-only and exit codes match the implementation.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scan_secrets.py"


def _load_script() -> Any:
    """Import a non-package script by path, as the other script tests do."""
    spec = importlib.util.spec_from_file_location("scan_matrix_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


scanner = _load_script()

# Synthetic values, assembled so the test source stays clean.  ``+`` is used
# instead of implicit adjacency because the formatter would merge the latter.
AWS_KEY = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
GITHUB_CLASSIC = "gh" + "p_" + "A" * 36
GITHUB_FINE = "github" + "_pat_" + "B" * 30
SLACK_TOKEN = "xox" + "b-" + "1234567890-abcdefghij"
BEARER = "C" * 32
PASSWORD = "Sup3r" + "S3cretValue!"
ENV_SECRET = "hunter2" + "hunter2"
BASE64_SECRET = "YWJjZGVm" + "Z2hpamtsbW5vcA=="
JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + "."
    + "eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
SLACK_HOOK = (
    "https://" + "hooks." + "slack.com/services/" + "T00000000/B00000000/" + "a" * 24
)
SLACK_TRIGGER = (
    "https://" + "hooks." + "slack.com/triggers/" + "T00000000/B00000000/" + "c" * 24
)
DISCORD_HOOK = (
    "https://" + "discord.com/api/webhooks/" + "123456789012345678/" + "b" * 32
)
URL_CRED = "https://" + "alice:" + "Sup3rS3cretPw" + "@" + "db.internal/x"

PEM_KINDS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "PGP PRIVATE KEY BLOCK",
    "SSH2 ENCRYPTED PRIVATE KEY",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(base: Path) -> Path:
    """Create an empty repository under *base*."""
    root = base / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    # Byte-identical blobs keep line numbers exact on Windows.
    _git(root, "config", "core.autocrlf", "false")
    return root


def _write(root: Path, name: str, content: str | bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _make_repo(base: Path, files: dict[str, str | bytes]) -> Path:
    """Create a repository whose index lists exactly *files*."""
    root = _init_repo(base)
    for name, content in files.items():
        _write(root, name, content)
    _git(root, "add", "-A")
    return root


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------


def test_documented_rules_are_the_whole_rule_set() -> None:
    assert [rule.name for rule in scanner.RULES] == [
        "private-key",
        "aws-access-key",
        "github-token",
        "github-token",
        "slack-token",
        "bearer-token",
        "url-userinfo",
        "webhook-url",
        "jwt",
        "generic-secret",
    ]


# ---------------------------------------------------------------------------
# True positives: one realistic shape per rule (and per family)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line", "rules"),
    [
        ("src/a.py", f'aws = "{AWS_KEY}"', {"aws-access-key"}),
        ("src/a.py", f'gh = "{GITHUB_CLASSIC}"', {"github-token"}),
        ("src/a.py", f'pat = "{GITHUB_FINE}"', {"github-token"}),
        ("src/a.py", f'slack = "{SLACK_TOKEN}"', {"slack-token"}),
        ("src/a.py", f"Authorization: Bearer {BEARER}", {"bearer-token"}),
        ("src/a.py", f'password = "{PASSWORD}"', {"generic-secret"}),
        ("src/a.py", f'client_secret = "{PASSWORD}"', {"generic-secret"}),
        ("src/a.py", f'api-key: "{PASSWORD}"', {"generic-secret"}),
        ("config/.env", f"DB_PASSWORD={ENV_SECRET}", {"generic-secret"}),
        ("config/.env", f"password={ENV_SECRET}", {"generic-secret"}),
        ("config/.env", f"POSTGRES_PASSWORD={PASSWORD}", {"generic-secret"}),
        ("config/.env", f"export API_KEY={ENV_SECRET}", {"generic-secret"}),
        ("config/.env", f"SECRET_KEY={BASE64_SECRET}", {"generic-secret"}),
        ("config/.env", f'API_KEY="{BASE64_SECRET}"', {"generic-secret"}),
        ("config/app.ini", "password = Tr0ub4dor3x", {"generic-secret"}),
        ("config/app.yml", "token: 8f14e45fceea167a", {"generic-secret"}),
        (
            "deploy/.env",
            f"GITHUB_TOKEN={GITHUB_CLASSIC}",
            {"github-token", "generic-secret"},
        ),
        ("src/a.py", f"url = '{URL_CRED}'", {"url-userinfo"}),
        ("src/a.py", f"jwt = '{JWT}'", {"jwt"}),
        ("src/a.py", f"hook = '{SLACK_HOOK}'", {"webhook-url"}),
        ("src/a.py", f"hook = '{SLACK_TRIGGER}'", {"webhook-url"}),
        ("src/a.py", f"hook = '{DISCORD_HOOK}'", {"webhook-url"}),
    ],
)
def test_detects_real_credential_shapes(path: str, line: str, rules: set[str]) -> None:
    found = {finding.rule for finding in scanner.scan_text(path, line + "\n")}

    assert found == rules


@pytest.mark.parametrize("kind", PEM_KINDS)
def test_every_pem_private_key_header_is_detected(kind: str) -> None:
    marker = "-----BEGIN " + kind + "-----"

    findings = scanner.scan_text("src/key.pem", marker + "\n")

    assert [finding.rule for finding in findings] == ["private-key"]


# ---------------------------------------------------------------------------
# False positives: placeholders, refs, hashes, UUIDs, base64, prose examples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("config/.env", "API_KEY=YOUR_API_KEY"),
        ("config/.env", "API_KEY=<your-key>"),
        ("config/.env", "TOKEN=xxxxxxxx"),
        ("config/.env", "DB_PASSWORD=changeme"),
        ("config/.env", "DB_PASSWORD=your-password-here"),
        ("config/.env", "DB_PASSWORD=${DB_PASSWORD}"),
        ("config/.env", "DB_PASSWORD=$DB_PASSWORD"),
        ("config/.env", "SECRET_KEY="),
        ("config/.env", "API_KEY=sk_test_placeholder"),
        (".github/workflows/ci.yml", "  token: ${{ secrets.TOKEN }}"),
        ("src/a.py", "password = os.environ['DB_PASSWORD']"),
        ("src/a.py", "token = get_token()"),
        ("src/a.py", "token = token.strip()"),
        ("src/a.py", "client_secret = request.json()['client_secret']"),
        ("src/a.py", "const token = ++searchSeq;"),
        ("src/a.py", "password_hash = '8f14e45fceea167a'"),
        ("config/app.yml", "token_expiry: 3600"),
        ("config/app.yml", "token: null"),
        ("src/a.py", "digest = 'e3b0c44298fc1c149afbf4c8996fb924'"),
        ("src/a.py", "id = '123e4567-e89b-12d3-a456-426614174000'"),
        (
            "src/a.py",
            "icon = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg'",
        ),
        ("src/a.py", "name = 'xqzvbnmklqwrtpsdfghjklzxcvbnm'"),
        ("docs/guide.md", "See https://user:password@example.com/path"),
        (
            "src/a.py",
            "url = 'https://example.com:8080/api?token=abcdefgh&x=2'",
        ),
        ("src/a.py", "Authorization: Bearer <token>"),
        ("src/a.py", "Authorization: Bearer YOUR_API_TOKEN_123456"),
        ("src/a.py", "api_key = 'example-key-value-1234'"),
        ("src/a.py", f'password = "{PASSWORD}..."'),
    ],
)
def test_ignores_placeholders_examples_and_nonsecrets(path: str, line: str) -> None:
    assert scanner.scan_text(path, line + "\n") == []


def test_documented_example_block_is_clean() -> None:
    block = "\n".join(
        [
            "# Credentials must come from the environment, for example:",
            "API_KEY=YOUR_API_KEY",
            "Authorization: Bearer <token>",
            "proxy = https://user:password@example.com:8080/",
            "SLACK= https://hooks.slack." + "com/services/T000/B000/" + "X" * 24,
            "DISCORD= https://discord." + "com/api/webhooks/0000000000/" + "x" * 32,
            "token = ${{ secrets.API_TOKEN }}",
            "",
        ]
    )

    assert scanner.scan_text("docs/guide/credentials.md", block) == []


# ---------------------------------------------------------------------------
# Allowlist: relaxes only what it names
# ---------------------------------------------------------------------------


def test_tests_allowlist_relaxes_heuristics_but_not_tokens() -> None:
    relaxed = scanner.scan_text("tests/fixtures/app.py", f"u = '{URL_CRED}'\n")
    enforced = scanner.scan_text("tests/fixtures/app.py", f"h = '{SLACK_HOOK}'\n")
    jwt = scanner.scan_text("tests/fixtures/app.py", f"t = '{JWT}'\n")

    assert relaxed == []
    assert [finding.rule for finding in enforced] == ["webhook-url"]
    assert [finding.rule for finding in jwt] == ["jwt"]


def test_vendored_assignment_and_ticket_jwt_are_relaxed() -> None:
    vendor = "src/novel_downloader/apps/music/providers/_vendor/musicdl/x.py"

    assert scanner.scan_text(vendor, f'client_secret = "{PASSWORD}"\n') == []
    assert scanner.scan_text(vendor, f"t = '{JWT}'\n") == []
    assert [
        finding.rule for finding in scanner.scan_text(vendor, f'k = "{AWS_KEY}"\n')
    ] == ["aws-access-key"]


# ---------------------------------------------------------------------------
# File type matrix
# ---------------------------------------------------------------------------


def test_binary_and_unknown_extension_files_are_skipped(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        {
            "assets/logo.png": (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + f'k = "{AWS_KEY}"\n'.encode()
            ),
            "data/archive.zip": b"PK\x03\x04" + f'k = "{AWS_KEY}"\n'.encode(),
            # A text suffix does not save a file whose probe contains NUL.
            "assets/notes.env": b"\x00\x01" + f"DB_PASSWORD={ENV_SECRET}\n".encode(),
        },
    )

    assert scanner.scan_repository(root) == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            f"DB_PASSWORD={ENV_SECRET}\n".encode(),
            id="utf-8",
        ),
        pytest.param(
            b"\xef\xbb\xbf" + f"DB_PASSWORD={ENV_SECRET}\n".encode(),
            id="utf-8-bom",
        ),
        pytest.param(
            b"\xff\xfe" + f"DB_PASSWORD={ENV_SECRET}\n".encode("utf-16-le"),
            id="utf-16-le-bom",
        ),
        pytest.param(
            b"\xfe\xff" + f"DB_PASSWORD={ENV_SECRET}\n".encode("utf-16-be"),
            id="utf-16-be-bom",
        ),
    ],
)
def test_bom_encodings_are_decoded(tmp_path: Path, payload: bytes) -> None:
    root = _make_repo(tmp_path, {"config/.env": payload})

    assert scanner.scan_repository(root) == [
        scanner.Finding("config/.env", 1, "generic-secret")
    ]


def test_gbk_file_still_matches_ascii_credentials(tmp_path: Path) -> None:
    body = "# \u4e2d\u6587\u6ce8\u91ca\n" + f"DB_PASSWORD={ENV_SECRET}\n"
    root = _make_repo(tmp_path, {"config/app.env": body.encode("gbk")})

    assert scanner.scan_repository(root) == [
        scanner.Finding("config/app.env", 2, "generic-secret")
    ]


def test_utf16_without_bom_is_treated_as_binary(tmp_path: Path) -> None:
    payload = f"DB_PASSWORD={ENV_SECRET}\n".encode("utf-16-le")
    root = _make_repo(tmp_path, {"config/.env": payload})

    assert scanner.scan_repository(root) == []


def test_oversized_file_is_skipped_whole_not_partially_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner, "MAX_FILE_BYTES", 4096)
    body = "x" * 8192 + f"\nDB_PASSWORD={ENV_SECRET}\n"
    root = _make_repo(tmp_path, {"big.env": body})

    assert scanner.scan_repository(root) == []


def test_token_beyond_one_mebibyte_is_still_found(tmp_path: Path) -> None:
    # The worktree reader is not chunked; this pins that a match past the
    # first MiB is not lost by a chunk-boundary probe.
    filler = ("y" * 100 + "\n") * 11_000
    body = filler + f'aws = "{AWS_KEY}"\n'
    root = _make_repo(tmp_path, {"src/big.py": body})

    assert scanner.scan_repository(root) == [
        scanner.Finding("src/big.py", body.count("\n"), "aws-access-key")
    ]


def test_line_based_scanning_does_not_join_a_split_token(tmp_path: Path) -> None:
    # Documented limitation: rules run per line, so a key split across a
    # newline is not reconstructed (no real credential format does this).
    body = 'aws = "AKIA' + "ABCDEFGH" + '"\n' + '"IJKLMNOP"\n'
    root = _make_repo(tmp_path, {"src/split.py": body})

    assert scanner.scan_repository(root) == []


def test_keystore_file_names_are_reported_without_content(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        {
            "android/upload.jks": b"\x00binary",
            "deploy/id_ed25519": "",
        },
    )

    assert sorted(
        (item.path, item.line, item.rule) for item in scanner.scan_repository(root)
    ) == [
        ("android/upload.jks", 0, "sensitive-file"),
        ("deploy/id_ed25519", 0, "sensitive-file"),
    ]


# ---------------------------------------------------------------------------
# Path scope: index only, so local build output is never scanned
# ---------------------------------------------------------------------------


def test_ignored_build_and_venv_directories_are_out_of_scope(
    tmp_path: Path,
) -> None:
    root = _init_repo(tmp_path)
    _write(root, ".gitignore", ".venv/\nbuild/\ndist/\n")
    _write(root, "app.py", "print('ok')\n")
    _write(root, ".venv/lib/leak.py", f'aws = "{AWS_KEY}"\n')
    _write(root, "build/leak.env", f"DB_PASSWORD={ENV_SECRET}\n")
    _write(root, "dist/leak.py", f'k = "{GITHUB_CLASSIC}"\n')
    _git(root, "add", "-A")

    assert scanner.iter_tracked_files(root) == [".gitignore", "app.py"]
    assert scanner.scan_repository(root) == []
    assert scanner.scan_repository(root, staged=True) == []


# ---------------------------------------------------------------------------
# Output contract: path:line: rule, never the matched value
# ---------------------------------------------------------------------------


def test_cli_text_output_is_location_only(tmp_path: Path, capsys: Any) -> None:
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    root = _make_repo(
        tmp_path,
        {
            "config/.env": f"DB_PASSWORD={ENV_SECRET}\n",
            "src/key.pem": marker + "\n",
            "src/hook.py": f"hook = '{SLACK_HOOK}'\n",
            "src/url.py": f"u = '{URL_CRED}'\n",
            "src/token.py": f"t = '{JWT}'\n",
        },
    )

    code = scanner.main(["--root", str(root)])

    captured = capsys.readouterr()
    assert code == scanner.EXIT_FINDINGS == 1
    assert captured.err == ""
    findings = [
        line
        for line in captured.out.splitlines()
        if not line.startswith("scan-secrets:")
    ]
    assert len(findings) == 5
    rule_names = {rule.name for rule in scanner.RULES}
    for line in findings:
        path, lineno, rule = line.rsplit(":", 2)
        assert path
        assert lineno.strip().isdigit()
        assert rule.strip() in rule_names
    leaked = captured.out + captured.err
    for value in (ENV_SECRET, JWT, SLACK_HOOK, URL_CRED, "PRIVATE KEY"):
        assert value not in leaked


def test_cli_json_findings_carry_location_only(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"config/.env": f"DB_PASSWORD={ENV_SECRET}\n"})

    code = scanner.main(["--root", str(root), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == scanner.EXIT_FINDINGS
    assert payload["count"] == 1
    assert set(payload["findings"][0]) == {"path", "line", "rule"}
    assert payload["findings"][0]["path"] == "config/.env"
    assert ENV_SECRET not in captured.out


# ---------------------------------------------------------------------------
# Exit codes (as implemented: 0 clean, 1 findings, 1 scan error)
# ---------------------------------------------------------------------------


def test_exit_codes_are_zero_for_clean_and_one_for_findings(
    tmp_path: Path, capsys: Any
) -> None:
    assert scanner.EXIT_OK == 0
    assert scanner.EXIT_FINDINGS == 1

    clean = _make_repo(tmp_path / "clean", {"app.py": "print('ok')\n"})
    dirty = _make_repo(tmp_path / "dirty", {"app.py": f'k = "{AWS_KEY}"\n'})

    assert scanner.main(["--root", str(clean)]) == scanner.EXIT_OK
    assert scanner.main(["--root", str(dirty)]) == scanner.EXIT_FINDINGS
    capsys.readouterr()


def test_scan_error_exits_one_and_reports_on_stderr(
    tmp_path: Path, capsys: Any
) -> None:
    # A run that cannot execute shares exit code 1 with "found something";
    # only the stderr marker distinguishes them.  Pinned so a future change
    # to 2 is a deliberate, visible decision.
    code = scanner.main(["--root", str(tmp_path / "not-a-repo"), "--json"])

    captured = capsys.readouterr()
    assert code == scanner.EXIT_FINDINGS == 1
    assert captured.out == ""
    assert "scan-secrets: error:" in captured.err
    assert "Traceback" not in captured.err


def test_conflicting_flags_exit_one(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"app.py": "print('ok')\n"})

    code = scanner.main(["--root", str(root), "--staged", "--history"])

    captured = capsys.readouterr()
    assert code == scanner.EXIT_FINDINGS
    assert "--history" in captured.err
