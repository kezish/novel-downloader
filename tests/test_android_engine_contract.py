#!/usr/bin/env python3
"""Static contract for the Android embedded-engine lifecycle.

The Python side of the engine already has a rich test suite; what the Gradle
gate cannot easily cover is the *Kotlin launcher's* failure handling, because
exercising Chaquopy on a JVM test runner would require an Android runtime.
This module audits the launcher sources instead, pinning the behaviour the
JVM tests in ``EmbeddedEngineStateTest.kt`` build on:

* a start is a bounded state machine (no "ready" flag that a late worker can
  resurrect after a stop);
* Python initialisation failures are contained, recorded and shown to the
  user instead of crashing the UI thread or retrying forever;
* the loopback port is probed before the API client is configured and falls
  back to the next candidate when 8081 is occupied;
* the health probe has a timeout, a settle re-probe and one bounded retry;
* a server that dies right after reporting ready flips back to FAILED;
* switching to a remote API stops the engine and switching back restarts it;
* a repeated start while starting/ready is a no-op;
* every start mints a fresh token and the API client resolves the credential
  per request, so a restart cannot keep sending the previous token.

The checks are textual on purpose: they are independent of the Android
toolchain, and a refactor that deletes one of these guards fails here even if
it happens to compile.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA_DIR = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "lingyi" / "noveldl"
)
DATA_DIR = JAVA_DIR / "data"
RES_DIR = ROOT / "android" / "app" / "src" / "main" / "res"

ENGINE = DATA_DIR / "EmbeddedEngine.kt"
POLICY = DATA_DIR / "EmbeddedEnginePolicy.kt"
API_CLIENT = DATA_DIR / "ApiClient.kt"
SETTINGS_SCREEN = JAVA_DIR / "ui" / "settings" / "SettingsScreen.kt"
MAIN_ACTIVITY = JAVA_DIR / "MainActivity.kt"


def read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    return path.read_text(encoding="utf-8")


def match(pattern: str, text: str, *, flags: int = re.S) -> re.Match[str]:
    found = re.search(pattern, text, flags)
    assert found is not None, f"pattern not found: {pattern}"
    return found


# ---------------------------------------------------------------------------
# pure policy
# ---------------------------------------------------------------------------


def test_the_start_policy_is_android_free() -> None:
    """Port/retry/probe logic must stay unit-testable on a plain JVM."""
    source = read(POLICY)
    assert "import android." not in source
    assert "import com.chaquo" not in source
    assert "import okhttp3" not in source


def test_port_candidates_cover_the_default_plus_fallbacks() -> None:
    policy = read(POLICY)
    default_port = int(match(r"DEFAULT_PORT\s*=\s*(\d+)", policy).group(1))
    attempts = int(match(r"PORT_ATTEMPTS\s*=\s*(\d+)", policy).group(1))

    assert default_port == 8081
    assert attempts >= 2, "at least one fallback port must exist"
    assert "fun portCandidates" in policy
    assert "fun selectPort" in policy
    assert "firstOrNull(isFree)" in policy

    engine = read(ENGINE)
    # The engine must actually use the selection, not hardcode 8081.
    assert "EmbeddedEnginePolicy.selectPort" in engine
    assert "machine.updatePort(generation, selected)" in engine


def test_the_port_is_selected_before_the_api_client_is_configured() -> None:
    """A fallback port must be visible to the immediately following configure."""
    for path, call in (
        (MAIN_ACTIVITY, "Api.configure("),
        (SETTINGS_SCREEN, "Api.configure("),
    ):
        source = read(path)
        start = source.index("EmbeddedEngine.ensureStarted(")
        api = source.index(call, start)
        assert start < api, f"{path.name}: ensureStarted must precede {call}"


def test_retries_are_bounded_and_only_for_transient_causes() -> None:
    policy = read(POLICY)
    attempts = int(match(r"MAX_START_ATTEMPTS\s*=\s*(\d+)", policy).group(1))
    assert attempts == 2, "expected the initial attempt plus exactly one retry"

    # shouldRetry is expression-bodied, so it ends at the next blank line
    # rather than at a closing brace.
    body = match(r"fun shouldRetry\(.*?\n\n", policy).group(0)
    assert "attempt + 1 < MAX_START_ATTEMPTS" in body
    assert "EmbeddedEngineFailure.TIMEOUT" in body
    assert "EmbeddedEngineFailure.PORT_BUSY" in body
    assert "EmbeddedEngineFailure.PYTHON_INIT" not in body, (
        "a missing wheel/interpreter is deterministic and must not be retried"
    )

    engine = read(ENGINE)
    assert "EmbeddedEnginePolicy.shouldRetry(" in engine
    assert "machine.recordAttemptFailure(" in engine


def test_the_health_probe_has_a_deadline_and_clips_the_last_sleep() -> None:
    policy = read(POLICY)
    timeout = int(
        match(r"HEALTH_TIMEOUT_MILLIS\s*=\s*([\d_]+)L", policy)
        .group(1)
        .replace("_", ""),
    )
    assert timeout > 0

    loop = match(
        r"fun pollForHealth\((?P<body>.*?)\n    \}",
        policy,
    ).group(0)
    assert "val deadline = nowMillis() + timeoutMillis" in loop
    assert "if (probe()) return true" in loop
    assert "if (remaining <= 0L) return false" in loop
    assert "sleep(minOf(intervalMillis, remaining))" in loop

    engine = read(ENGINE)
    assert "EmbeddedEnginePolicy.pollForHealth(" in engine
    assert "HEALTH_TIMEOUT_MILLIS" in engine
    assert "HealthResult.TIMEOUT" in engine


def test_a_server_that_dies_right_after_ready_is_reported_as_failed() -> None:
    engine = read(ENGINE)
    assert "confirmStillAlive" in engine
    assert "POST_READY_PROBE_DELAY_MILLIS" in engine
    assert "machine.markExited(" in engine

    policy = read(POLICY)
    assert "POST_READY_PROBE_DELAY_MILLIS" in policy
    assert "fun markExited(" in policy
    assert "EmbeddedEngineStatus.FAILED" in policy
    assert "EmbeddedEngineFailure.EXITED" in policy


def test_the_health_endpoint_is_probed_with_a_short_timeout_client() -> None:
    engine = read(ENGINE)
    assert "/api/v1/health" in engine
    # The startup probe must not inherit the 2-minute user-facing API timeout.
    assert "callTimeout(3, TimeUnit.SECONDS)" in engine
    assert "DOWNLOAD_API_KEY_HEADER, token" in engine


# ---------------------------------------------------------------------------
# error containment
# ---------------------------------------------------------------------------


def test_python_initialisation_failures_are_contained() -> None:
    engine = read(ENGINE)
    start = engine.index("fun bootstrapOnce(")
    end = engine.index("private fun awaitHealthy(", start)
    bootstrap = engine[start:end]

    assert "try {" in bootstrap
    assert "Python.start(AndroidPlatform(appContext))" in bootstrap
    assert "EmbeddedEngineFailure.PYTHON_INIT" in bootstrap
    assert "return StartFailure(" in bootstrap

    # No raw throw of a Python/Chaquopy error out of the worker: the task body
    # is wrapped by submit(), and runStart converts outcomes to markFailed.
    assert "machine.markFailed(" in engine
    assert "catch (t: Throwable)" in engine
    assert 'Log.e(TAG, "embedded engine lifecycle task crashed", t)' in engine


def test_a_failed_start_releases_the_half_started_server() -> None:
    engine = read(ENGINE)
    run_start = match(
        r"private fun runStart\((?P<body>.*?)\n    \}",
        engine,
    ).group(0)
    assert "stopPythonServer()" in run_start
    assert "machine.markFailed(" in run_start


def test_the_token_is_never_logged_and_never_an_argument() -> None:
    engine = read(ENGINE)
    for line in engine.splitlines():
        if "Log." in line:
            assert "token" not in line.lower(), (
                f"token-capable log line: {line.strip()}"
            )

    call = engine[engine.index('callAttr("start_server"') :].split(")", 1)[0]
    assert "token" not in call.lower(), "the token must go through os.environ, not argv"


def test_user_visible_start_failures_have_localized_text() -> None:
    settings = read(SETTINGS_SCREEN)
    for name in (
        "settings_standalone_starting",
        "settings_standalone_ready",
        "settings_standalone_error",
        "settings_standalone_error_port",
        "settings_standalone_error_timeout",
        "settings_standalone_error_exited",
    ):
        assert f"R.string.{name}" in settings, f"SettingsScreen does not use {name}"

    for locale in ("values", "values-en"):
        strings = read(RES_DIR / locale / "strings.xml")
        for name in (
            "settings_standalone_starting",
            "settings_standalone_ready",
            "settings_standalone_error_port",
            "settings_standalone_error_timeout",
            "settings_standalone_error_exited",
        ):
            assert f'name="{name}"' in strings, f"{locale} is missing {name}"


def test_the_settings_status_line_is_reactive() -> None:
    """A bootstrap failure on a worker thread must recompose the screen."""
    settings = read(SETTINGS_SCREEN)
    assert "EmbeddedEngine.snapshots.collectAsState()" in settings
    assert "EmbeddedEngineStatus.FAILED" in settings
    assert "EmbeddedEngineStatus.READY" in settings


# ---------------------------------------------------------------------------
# stop / restart / idempotency / token rotation
# ---------------------------------------------------------------------------


def test_start_is_idempotent_while_starting_or_ready() -> None:
    policy = read(POLICY)
    begin = match(r"fun beginStart\((?P<body>.*?)\n    \}", policy).group(0)
    assert "EmbeddedEngineStatus.STARTING" in begin
    assert "EmbeddedEngineStatus.READY" in begin
    assert "return null" in begin

    engine = read(ENGINE)
    # Both entry points consult the state machine before scheduling work.
    assert engine.count("machine.beginStart(port) ?: return") >= 2


def test_switching_to_remote_stops_the_engine_and_switching_back_restarts_it() -> None:
    settings = read(SETTINGS_SCREEN)
    toggle = match(
        r"fun setStandalone\(enabled: Boolean\)(?P<body>.*?)\n    \}",
        settings,
    ).group(0)

    enabled_block = toggle[toggle.index("if (enabled)") : toggle.index("} else {")]
    stopped_block = toggle[
        toggle.index("} else {") : toggle.index("// The toggle must take effect")
    ]
    assert "EmbeddedEngine.ensureStarted(context)" in enabled_block
    assert "EmbeddedEngine.stop()" in stopped_block

    engine = read(ENGINE)
    assert "fun stop()" in engine
    assert "machine.markStopped(port)" in engine
    assert '"stop_server"' in engine

    policy = read(POLICY)
    stopped = match(r"fun markStopped\((?P<body>.*?)\n    \}", policy).group(0)
    assert "EmbeddedEngineStatus.STOPPED" in stopped
    assert "generation += 1" in stopped

    # STOPPED (and FAILED) must be restartable; only STARTING/READY block.
    begin = match(r"fun beginStart\((?P<body>.*?)\n    \}", policy).group(0)
    assert "current == EmbeddedEngineStatus.STARTING" in begin
    assert "current == EmbeddedEngineStatus.READY" in begin


def test_a_stopped_engine_cannot_be_resurrected_by_a_late_worker() -> None:
    policy = read(POLICY)
    ready = match(r"fun markReady\((?P<body>.*?)\n    \}", policy).group(0)
    assert "if (generation != this.generation) return false" in ready

    failed = match(r"fun markFailed\((?P<body>.*?)\n    \}", policy).group(0)
    assert "if (generation != this.generation) return" in failed


def test_every_start_mints_a_fresh_token_and_the_client_reads_it_per_request() -> None:
    engine = read(ENGINE)
    ensure = match(
        r"fun ensureStarted\(context: Context\)(?P<body>.*?)\n    \}",
        engine,
    ).group(0)
    assert "token = generateEmbeddedToken()" in ensure

    api = read(API_CLIENT)
    # requestKey is computed, not snapshotted at configure time.
    request_key = match(r"val requestKey: String(?P<body>.*?)\n\n", api).group(0)
    assert "get() = effectiveRequestKey(" in request_key
    assert "engineToken = EmbeddedEngine.token" in request_key
    assert "this.requestKey =" not in api, (
        "a cached credential would go stale on restart"
    )
    assert "ApiOriginPolicy(origin) { requestKey }" in api, (
        "the interceptor must resolve the credential per request"
    )
