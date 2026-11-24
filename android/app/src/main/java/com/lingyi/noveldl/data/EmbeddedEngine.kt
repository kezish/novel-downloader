package com.lingyi.noveldl.data

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.flow.StateFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/**
 * Environment variable the Python engine reads its bearer token from.
 *
 * Kept in sync with `novel_downloader.apps.embedded.server.TOKEN_ENV_VAR`; a
 * Python test pins the two literals together.
 */
internal const val EMBEDDED_TOKEN_ENV_VAR = "NOVEL_DOWNLOADER_EMBEDDED_TOKEN"

/** Entropy of the per-process token (bytes, before base64url encoding). */
internal const val EMBEDDED_TOKEN_BYTES = 32

/**
 * Generates a fresh bearer token: [EMBEDDED_TOKEN_BYTES] CSPRNG bytes as
 * unpadded base64url (`[A-Za-z0-9_-]{43}` for the default size).
 *
 * base64url keeps the value usable as an HTTP header value without escaping
 * and as a plain environment string.
 */
internal fun generateEmbeddedToken(random: SecureRandom = SecureRandom()): String {
    val bytes = ByteArray(EMBEDDED_TOKEN_BYTES)
    random.nextBytes(bytes)
    return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
}

/**
 * The environment entries installed into the Python interpreter for [token].
 *
 * Pure so the hand-off can be asserted on the JVM: an empty token yields no
 * entry (the engine then keeps its unauthenticated desktop/test behaviour).
 * The token is passed as an environment variable rather than a
 * `start_server` argument because an argument would land in the interpreter's
 * visible call signature/log lines, whereas the environment block is private
 * to the process (and `os.environ` here is only ever set in memory, never
 * written to disk).
 */
internal fun embeddedTokenEnvironment(token: String): Map<String, String> =
    if (token.isEmpty()) emptyMap() else mapOf(EMBEDDED_TOKEN_ENV_VAR to token)

/**
 * Boots the embedded Python engine: the full novel_downloader service layer
 * serving the REST contract on 127.0.0.1:[port] inside the app process.
 *
 * ## Failure handling
 *
 * A start is a small state machine ([EmbeddedEngineStateMachine]):
 *
 *  - the port is probed synchronously, before the caller configures the API
 *    client, and the first free candidate (8081, 8082, ...) is used;
 *  - Python initialisation and `start_server` run on a dedicated worker, then
 *    a loopback `/api/v1/health` probe must answer within
 *    [EmbeddedEnginePolicy.HEALTH_TIMEOUT_MILLIS] and survive a short settle
 *    window before the engine is reported READY;
 *  - transient failures (health timeout, port race) get one bounded retry;
 *    deterministic ones (missing wheel, broken interpreter) fail immediately
 *    with a user-visible [lastError];
 *  - a post-READY liveness probe flips a server that died right after start
 *    back to FAILED instead of leaving the UI claiming it is running;
 *  - [stop] invalidates the current generation, so a queued stop cannot be
 *    resurrected by a bootstrap that was already in flight.
 *
 * ## Loopback is not a trust boundary
 *
 * Any app on the device can reach `127.0.0.1`, so the engine is not left open:
 * a per-process random [token] is generated here, installed into the Python
 * interpreter's environment, and required by every `/api/v1` and `/downloads`
 * request. It lives in process memory only — never in a log line, a
 * preference file or the argument list — and is regenerated on every start
 * (so a stop/restart cycle invalidates the previous credential; the API
 * client reads [token] dynamically, see `Api.requestKey`).
 */
object EmbeddedEngine {
    private const val TAG = "EmbeddedEngine"

    /** The historically fixed port, kept for callers/tests that name it. */
    const val PORT = EmbeddedEnginePolicy.DEFAULT_PORT

    /** The port used when no fallback was selected. */
    const val DEFAULT_PORT = EmbeddedEnginePolicy.DEFAULT_PORT

    /**
     * Directory under `filesDir` used as the engine's writable home. The
     * engine's default `general.output_dir` (`./downloads`) resolves below
     * it, which is where the music engine also writes `<stem>.lrc`.
     */
    const val HOME_DIRECTORY = "engine_home"

    /**
     * The bearer token the engine expects from this process.
     *
     * Generated eagerly (not on the bootstrap thread) because `Api.configure`
     * can run before the engine finished starting and must already know which
     * credential to attach. Regenerated on every start (including a restart
     * after [stop]) and read dynamically by the API client, so no stale
     * credential is ever replayed.
     */
    @Volatile
    var token: String = generateEmbeddedToken()
        private set

    /**
     * Loopback port the engine will bind (or already bound). Selected before
     * the caller configures [Api], so a fallback port is visible immediately.
     */
    @Volatile
    var port: Int = DEFAULT_PORT
        private set

    /** Base URL of the active loopback endpoint (follows [port]). */
    val baseUrl: String
        get() = "http://127.0.0.1:$port/"

    /** Compatibility alias for the previous `const val BASE_URL`. */
    val BASE_URL: String
        get() = baseUrl

    private val machine = EmbeddedEngineStateMachine()

    /** Observable status for the settings screen. */
    internal val snapshots: StateFlow<EmbeddedEngineSnapshot> = machine.snapshots

    internal val status: EmbeddedEngineStatus
        get() = machine.status

    /**
     * Whether a start was scheduled. Becomes true *before* the interpreter has
     * started and the loopback port is listening, so it only means "will not
     * be scheduled twice" — use [isReady] to decide whether a request can
     * succeed.
     */
    val isStarted: Boolean
        get() = machine.isStarted

    /**
     * Whether the bootstrap finished successfully and the loopback server
     * answered its health probe. Stays false while starting, after a failed
     * start and after the server died (see [lastError]).
     */
    val isReady: Boolean
        get() = machine.isReady

    val lastError: String?
        get() = machine.lastError

    /** Structured cause of the last failure, for a localized UI message. */
    internal val lastFailure: EmbeddedEngineFailure?
        get() = machine.lastFailure

    /**
     * Serializes start and stop tasks. A single thread preserves ordering: a
     * stop submitted while a start is booting runs after it, so the old
     * credential is never carried into the restarted server.
     */
    private val lifecycle = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "embedded-engine").apply { isDaemon = true }
    }

    @Volatile
    private var lastTask: Future<*>? = null

    /** Short-timeout client for the startup liveness probe only. */
    private val healthClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(2, TimeUnit.SECONDS)
            .readTimeout(2, TimeUnit.SECONDS)
            .callTimeout(3, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build()
    }

    /**
     * Boots the engine at most once per READY/STARTING cycle. Safe (and
     * cheap) to call on the UI thread: the port probe binds and closes a
     * socket in microseconds, and the heavy bootstrap runs on the engine
     * thread. The application context is used so no Activity is retained by
     * the Python runtime for the process lifetime.
     *
     * A repeated call while STARTING or READY is a no-op (idempotent start);
     * after FAILED or STOPPED the call starts a fresh attempt with a new token.
     */
    fun ensureStarted(context: Context) {
        val appContext = context.applicationContext ?: context
        val home = appContext.filesDir.resolve(HOME_DIRECTORY).absolutePath
        val generation = machine.beginStart(port) ?: return
        // A start (first or restart) always gets a fresh credential; the API
        // client resolves it on every request, so nothing stale survives.
        token = generateEmbeddedToken()
        val selected = selectPort(port)
        if (selected == null) {
            machine.markFailed(
                generation,
                EmbeddedEngineFailure.PORT_BUSY,
                "no free loopback port in ${port}..${port + EmbeddedEnginePolicy.PORT_ATTEMPTS - 1}",
                port,
            )
            return
        }
        if (selected != port) {
            port = selected
            machine.updatePort(generation, selected)
        }
        submit { runStart(generation, appContext, home) }
    }

    /**
     * Stops the engine (settings switch moved to a remote API).
     *
     * The status flips to STOPPED immediately, so the UI never shows a running
     * engine after the switch; the Python-side shutdown is queued on the
     * lifecycle thread and cannot be raced by a new start (single thread).
     */
    fun stop() {
        if (!machine.markStopped(port)) return
        submit { stopPythonServer() }
    }

    /**
     * Schedules [bootstrap] (Python start + server start) exactly once, on
     * the "embedded-engine" thread. [home] is the engine's writable home
     * directory.
     *
     * Test seam: production uses [ensureStarted] with the full policy (port
     * fallback, health probe, retries); this overload runs the supplied block
     * as-is so the scheduling contract can be asserted on the JVM.
     */
    @VisibleForTesting
    internal fun ensureStarted(home: String, bootstrap: () -> Unit) {
        val generation = machine.beginStart(port) ?: return
        submit {
            try {
                bootstrap()
                machine.markReady(generation, port)
            } catch (t: Throwable) {
                machine.markFailed(
                    generation,
                    EmbeddedEngineFailure.PYTHON_INIT,
                    messageOf(t),
                    port,
                )
            }
        }
    }

    /** Restores the never-started state; unit tests only. */
    @VisibleForTesting
    internal fun resetForTest() {
        machine.reset()
        port = DEFAULT_PORT
        token = generateEmbeddedToken()
        lastTask = null
    }

    /**
     * Waits for the most recently scheduled lifecycle task to finish; unit
     * tests only.
     *
     * Call this before [resetForTest] so a still-running bootstrap cannot
     * write `ready`/`lastError` after the reset and leak state into the next
     * test. Returns false if the task is still running after [timeoutMs].
     */
    @VisibleForTesting
    internal fun awaitBootstrapForTest(timeoutMs: Long = 5_000): Boolean {
        val task = lastTask ?: return true
        return try {
            task.get(timeoutMs, TimeUnit.MILLISECONDS)
            true
        } catch (e: TimeoutException) {
            false
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        } catch (e: ExecutionException) {
            true
        }
    }

    /**
     * Body of the worker task: an attempt loop around [bootstrapOnce].
     *
     * Only transient causes are retried, and only once; a deterministic
     * failure ends in FAILED with its detail, which the settings screen shows.
     */
    private fun runStart(generation: Int, appContext: Context, home: String) {
        var retries = 0
        while (true) {
            val outcome = bootstrapOnce(generation, appContext, home)
            if (outcome == null) return
            // A half-started server must not keep the port (or the old
            // credential) for the retry.
            stopPythonServer()
            if (machine.status != EmbeddedEngineStatus.STARTING) return
            val recorded = machine.recordAttemptFailure(
                generation,
                outcome.failure,
                outcome.message,
                port,
            )
            if (recorded && EmbeddedEnginePolicy.shouldRetry(retries, outcome.failure)) {
                retries += 1
                sleepQuietly(EmbeddedEnginePolicy.RETRY_DELAY_MILLIS)
                continue
            }
            machine.markFailed(generation, outcome.failure, outcome.message, port)
            return
        }
    }

    /**
     * One start attempt. Returns `null` when the engine is READY (or the start
     * was superseded), otherwise the failure to record.
     */
    private fun bootstrapOnce(
        generation: Int,
        appContext: Context,
        home: String,
    ): StartFailure? {
        val python = try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(appContext))
            Python.getInstance()
        } catch (t: Throwable) {
            return StartFailure(EmbeddedEngineFailure.PYTHON_INIT, messageOf(t))
        }
        // Hand the credential over in memory, before the server binds its
        // port: the interpreter is in this process, so nothing is written to
        // disk and no argv is visible to other apps. `os.environ` (not
        // `os.putenv`) is used because the Python side reads
        // `os.environ.get(...)`.
        try {
            val environ = python.getModule("os").get("environ")
                ?: error("os.environ is unavailable in the embedded interpreter")
            embeddedTokenEnvironment(token).forEach { (name, value) ->
                environ.put(name, value)
            }
        } catch (t: Throwable) {
            return StartFailure(EmbeddedEngineFailure.PYTHON_INIT, messageOf(t))
        }
        val bound = try {
            python
                .getModule("novel_downloader.apps.embedded.server")
                .callAttr("start_server", home, port)
                .toInt()
        } catch (t: Throwable) {
            val failure = if (looksLikeAddressInUse(t)) {
                EmbeddedEngineFailure.PORT_BUSY
            } else {
                EmbeddedEngineFailure.PYTHON_INIT
            }
            return StartFailure(failure, messageOf(t))
        }
        // `start_server` binds exactly the requested port; a different value
        // means a server from an earlier run survived the stop and still has
        // the old credential. Fail instead of talking to it with a new token.
        if (bound != port) {
            return StartFailure(
                EmbeddedEngineFailure.UNKNOWN,
                "engine bound port $bound instead of the requested $port",
            )
        }
        return when (val health = awaitHealthy(bound)) {
            HealthResult.HEALTHY -> {
                if (!machine.markReady(generation, bound)) {
                    // Stopped while booting; the queued stop owns shutdown.
                    return null
                }
                confirmStillAlive(generation, bound)
                null
            }
            HealthResult.TIMEOUT -> StartFailure(
                EmbeddedEngineFailure.TIMEOUT,
                "health endpoint did not answer within " +
                    "${EmbeddedEnginePolicy.HEALTH_TIMEOUT_MILLIS} ms",
            )
            HealthResult.EXITED -> StartFailure(
                EmbeddedEngineFailure.EXITED,
                "engine stopped answering immediately after start",
            )
        }
    }

    /**
     * Polls the health endpoint, then re-probes after a short settle window so
     * a server that binds and dies immediately is not reported as READY.
     */
    private fun awaitHealthy(boundPort: Int): HealthResult {
        val answered = EmbeddedEnginePolicy.pollForHealth(
            timeoutMillis = EmbeddedEnginePolicy.HEALTH_TIMEOUT_MILLIS,
            intervalMillis = EmbeddedEnginePolicy.HEALTH_POLL_INTERVAL_MILLIS,
            probe = { probeHealth(boundPort) },
            sleep = ::sleepQuietly,
            nowMillis = System::currentTimeMillis,
        )
        if (!answered) return HealthResult.TIMEOUT
        sleepQuietly(EmbeddedEnginePolicy.HEALTH_SETTLE_MILLIS)
        return if (probeHealth(boundPort)) HealthResult.HEALTHY else HealthResult.EXITED
    }

    /**
     * One delayed liveness check after READY: catches a server that survived
     * the settle probe but crashed "immediately after start". On failure the
     * snapshot flips to FAILED/EXITED and the port is released.
     */
    private fun confirmStillAlive(generation: Int, boundPort: Int) {
        sleepQuietly(EmbeddedEnginePolicy.POST_READY_PROBE_DELAY_MILLIS)
        if (probeHealth(boundPort)) return
        if (machine.markExited(generation, "engine exited after reporting ready", boundPort)) {
            stopPythonServer()
        }
    }

    /**
     * A single health request. Any 2xx proves the loopback listener is alive
     * and authenticating; a connection refusal or timeout is just "not yet".
     */
    private fun probeHealth(boundPort: Int): Boolean = try {
        val request = Request.Builder()
            .url("http://127.0.0.1:$boundPort/api/v1/health")
            .header(DOWNLOAD_API_KEY_HEADER, token)
            .build()
        healthClient.newCall(request).execute().use { it.isSuccessful }
    } catch (t: Throwable) {
        false
    }

    /** Binds and immediately releases a loopback port to test availability. */
    private fun selectPort(preferred: Int): Int? = try {
        EmbeddedEnginePolicy.selectPort(preferred) { candidate ->
            try {
                ServerSocket().use { socket ->
                    socket.reuseAddress = true
                    // Literal IPv4: the Python server binds 127.0.0.1, so
                    // probing ::1 could report a port free that is occupied
                    // on the address the engine actually uses.
                    socket.bind(
                        InetSocketAddress(InetAddress.getByName("127.0.0.1"), candidate),
                    )
                }
                true
            } catch (e: IOException) {
                false
            }
        }
    } catch (t: Throwable) {
        null
    }

    private fun looksLikeAddressInUse(error: Throwable): Boolean {
        val text = (error.message ?: "").lowercase()
        return "address already in use" in text ||
            "errno 98" in text ||
            "errno 48" in text ||
            "10048" in text
    }

    private fun stopPythonServer() {
        if (!Python.isStarted()) return
        try {
            Python.getInstance()
                .getModule("novel_downloader.apps.embedded.server")
                .callAttr("stop_server")
        } catch (t: Throwable) {
            Log.w(TAG, "embedded server stop failed", t)
        }
    }

    private fun submit(task: () -> Unit) {
        lastTask = lifecycle.submit(
            Callable {
                try {
                    task()
                } catch (t: Throwable) {
                    Log.e(TAG, "embedded engine lifecycle task crashed", t)
                }
            },
        )
    }

    private fun messageOf(error: Throwable): String {
        val message = error.message?.trim().orEmpty()
        return (message.ifEmpty { error.javaClass.simpleName }).take(240)
    }

    private fun sleepQuietly(millis: Long) {
        try {
            Thread.sleep(millis)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private data class StartFailure(
        val failure: EmbeddedEngineFailure,
        val message: String,
    )

    private enum class HealthResult { HEALTHY, TIMEOUT, EXITED }
}
