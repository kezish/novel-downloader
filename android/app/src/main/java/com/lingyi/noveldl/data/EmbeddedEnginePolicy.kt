package com.lingyi.noveldl.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Lifecycle of the in-process Python engine, as seen by the UI.
 *
 * The states are deliberately coarse: the UI only needs to know whether the
 * loopback port can be used ([READY]), whether a start is in flight
 * ([STARTING], show a progress hint), or why the last start failed
 * ([FAILED], show the error and offer a retry).
 */
internal enum class EmbeddedEngineStatus {
    /** Never started in this process, or explicitly stopped and not restarted. */
    IDLE,

    /** A bootstrap is scheduled/running; the loopback port is not listening yet. */
    STARTING,

    /** `start_server` bound the port and the health probe answered. */
    READY,

    /** The bootstrap failed; [EmbeddedEngineSnapshot.lastError] says why. */
    FAILED,

    /** Explicitly stopped after switching to a remote API. */
    STOPPED,
}

/**
 * Machine-readable cause of a failed engine start.
 *
 * Kept separate from [EmbeddedEngineSnapshot.lastError] (which carries the
 * transport/Python detail) so the UI can render a localized sentence per
 * cause instead of showing an English exception message.
 */
internal enum class EmbeddedEngineFailure {
    /** `Python.start`, the Chaquopy platform or the engine import failed. */
    PYTHON_INIT,

    /** No free loopback port among the candidates. */
    PORT_BUSY,

    /** `start_server` returned but the health endpoint never answered. */
    TIMEOUT,

    /** The server answered once and then died (crashed right after start). */
    EXITED,

    /** Anything else. */
    UNKNOWN,
}

/**
 * Immutable state exposed to the UI.
 *
 * Contains no credential: the bearer token must never end up in Compose state,
 * a snapshot log line or a saved-state bundle.
 */
internal data class EmbeddedEngineSnapshot(
    val status: EmbeddedEngineStatus = EmbeddedEngineStatus.IDLE,
    val failure: EmbeddedEngineFailure? = null,
    val lastError: String? = null,
    /** 1-based number of the start attempt that produced this snapshot. */
    val attempt: Int = 0,
    val port: Int = EmbeddedEnginePolicy.DEFAULT_PORT,
)

/**
 * Pure (Android-free) start policy: port candidates, bounded retries and the
 * health-probe loop.
 *
 * Extracted so it can be exercised by plain JVM unit tests, which cannot load
 * Chaquopy or an `Activity`.
 */
internal object EmbeddedEnginePolicy {

    /** The historically fixed port the client and the Python side agree on. */
    const val DEFAULT_PORT = 8081

    /**
     * How many loopback ports are tried when the default is occupied.
     *
     * Four candidates keep the whole range inside the "user port" area while
     * giving three fallbacks; the Python side binds exactly the port passed
     * in, so a busy port is never silently shared with another app.
     */
    const val PORT_ATTEMPTS = 4

    /**
     * Total start attempts (initial + one retry) for transient failures.
     *
     * Python initialisation is expensive (asset extraction can take seconds),
     * so a loop is bounded and only timeouts/port races are retried; a broken
     * interpreter or missing wheel fails once and surfaces its error.
     */
    const val MAX_START_ATTEMPTS = 2

    /** Delay before a retried start attempt. */
    const val RETRY_DELAY_MILLIS = 400L

    /** Overall deadline for the health endpoint to answer for one attempt. */
    const val HEALTH_TIMEOUT_MILLIS = 15_000L

    /** Poll interval while waiting for the health endpoint. */
    const val HEALTH_POLL_INTERVAL_MILLIS = 150L

    /**
     * A short settle window between the first successful probe and the READY
     * transition: a server that binds and immediately crashes (a Python
     * import error inside the request thread, a test stub returning) fails the
     * second probe instead of being reported as running.
     */
    const val HEALTH_SETTLE_MILLIS = 250L

    /**
     * Delay before the post-READY liveness confirmation. Catches a server
     * that dies shortly after the settle probe (the "started then exited"
     * case) without turning this into a permanent watchdog.
     */
    const val POST_READY_PROBE_DELAY_MILLIS = 750L

    /** The ports tried for a start, preferred port first. */
    fun portCandidates(preferred: Int = DEFAULT_PORT): List<Int> =
        buildList(PORT_ATTEMPTS) {
            add(preferred)
            for (offset in 1 until PORT_ATTEMPTS) add(preferred + offset)
        }

    /**
     * The first candidate for which [isFree] returns true, or `null` when all
     * of them are occupied.
     *
     * [isFree] is injected so tests never bind a socket.
     */
    fun selectPort(preferred: Int = DEFAULT_PORT, isFree: (Int) -> Boolean): Int? =
        portCandidates(preferred).firstOrNull(isFree)

    /**
     * Whether [failure] is worth a second attempt.
     *
     * A timeout can be a slow first run (asset extraction) and a port race can
     * lose to another process between the probe and the bind. A Python init
     * failure is deterministic and repeated attempts would only delay the
     * user-visible error.
     */
    fun shouldRetry(attempt: Int, failure: EmbeddedEngineFailure): Boolean =
        attempt + 1 < MAX_START_ATTEMPTS &&
            (failure == EmbeddedEngineFailure.TIMEOUT ||
                failure == EmbeddedEngineFailure.PORT_BUSY)

    /**
     * Polls [probe] until it succeeds or [timeoutMillis] elapsed.
     *
     * [nowMillis] and [sleep] are injected so the loop is deterministic in
     * tests. Returns false on timeout, never throws (a probe failure is just
     * "not ready yet").
     */
    fun pollForHealth(
        timeoutMillis: Long,
        intervalMillis: Long,
        probe: () -> Boolean,
        sleep: (Long) -> Unit,
        nowMillis: () -> Long,
    ): Boolean {
        val deadline = nowMillis() + timeoutMillis
        while (true) {
            if (probe()) return true
            val remaining = deadline - nowMillis()
            if (remaining <= 0L) return false
            sleep(minOf(intervalMillis, remaining))
        }
    }
}

/**
 * Synchronized state transitions for the engine bootstrap.
 *
 * The engine can be started from the UI thread, stopped from the settings
 * screen, and a bootstrap runs on a worker thread; every transition is
 * serialized here. A monotonically increasing [generation] makes a late
 * worker harmless: after [markStopped] or [reset], the old worker's
 * [markReady] is ignored instead of resurrecting a stopped engine.
 */
internal class EmbeddedEngineStateMachine(
    private val maxAttempts: Int = EmbeddedEnginePolicy.MAX_START_ATTEMPTS,
) {
    private val mutable = MutableStateFlow(EmbeddedEngineSnapshot())

    /** Observable status for Compose (`collectAsState`) and plain JVM tests. */
    val snapshots: StateFlow<EmbeddedEngineSnapshot> = mutable.asStateFlow()

    private var generation = 0
    private var attempts = 0

    val status: EmbeddedEngineStatus
        get() = mutable.value.status

    /** True once a start was scheduled (legacy contract of `isStarted`). */
    val isStarted: Boolean
        get() = status == EmbeddedEngineStatus.STARTING ||
            status == EmbeddedEngineStatus.READY ||
            status == EmbeddedEngineStatus.FAILED

    val isReady: Boolean
        get() = status == EmbeddedEngineStatus.READY

    val lastError: String?
        get() = mutable.value.lastError

    val lastFailure: EmbeddedEngineFailure?
        get() = mutable.value.failure

    /**
     * Marks a start as scheduled. Returns a generation token when this caller
     * owns the start, or `null` when a start is already running/finished (the
     * idempotency rule for a repeatedly tapped switch).
     *
     * A previous FAILED state is restartable: the user tapping the switch
     * again (or saving the settings page) is the explicit retry signal.
     */
    @Synchronized
    fun beginStart(port: Int): Int? {
        val current = mutable.value.status
        if (current == EmbeddedEngineStatus.STARTING || current == EmbeddedEngineStatus.READY) {
            return null
        }
        generation += 1
        attempts = 0
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.STARTING,
            attempt = 0,
            port = port,
        )
        return generation
    }

    /** Updates the chosen port while a start is in flight. */
    @Synchronized
    fun updatePort(generation: Int, port: Int) {
        if (generation != this.generation) return
        if (mutable.value.status != EmbeddedEngineStatus.STARTING) return
        mutable.value = mutable.value.copy(port = port)
    }

    /**
     * Records a failed attempt. Returns true when another attempt is allowed;
     * the caller then retries, otherwise it calls [markFailed].
     */
    @Synchronized
    fun recordAttemptFailure(
        generation: Int,
        failure: EmbeddedEngineFailure,
        message: String,
        port: Int,
    ): Boolean {
        if (generation != this.generation) return false
        attempts += 1
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.STARTING,
            failure = failure,
            lastError = message,
            attempt = attempts,
            port = port,
        )
        return attempts < maxAttempts
    }

    /** Marks the engine usable. Returns false when the start was superseded. */
    @Synchronized
    fun markReady(generation: Int, port: Int): Boolean {
        if (generation != this.generation) return false
        attempts = 0
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.READY,
            port = port,
        )
        return true
    }

    /** Marks the engine failed; a later [beginStart] may retry. */
    @Synchronized
    fun markFailed(
        generation: Int,
        failure: EmbeddedEngineFailure,
        message: String,
        port: Int,
    ) {
        if (generation != this.generation) return
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.FAILED,
            failure = failure,
            lastError = message,
            attempt = attempts,
            port = port,
        )
    }

    /**
     * Transitions READY -> FAILED when the post-start liveness probe fails.
     * Only the current generation can do this.
     */
    @Synchronized
    fun markExited(generation: Int, message: String, port: Int): Boolean {
        if (generation != this.generation) return false
        if (mutable.value.status != EmbeddedEngineStatus.READY) return false
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.FAILED,
            failure = EmbeddedEngineFailure.EXITED,
            lastError = message,
            port = port,
        )
        return true
    }

    /**
     * Marks the engine stopped. Returns true when an active start/ready state
     * was invalidated (the caller then owns the Python-side shutdown).
     */
    @Synchronized
    fun markStopped(port: Int): Boolean {
        val current = mutable.value.status
        if (current == EmbeddedEngineStatus.IDLE || current == EmbeddedEngineStatus.STOPPED) {
            return false
        }
        generation += 1
        attempts = 0
        mutable.value = EmbeddedEngineSnapshot(
            status = EmbeddedEngineStatus.STOPPED,
            port = port,
        )
        return true
    }

    /** Test-only: returns to the never-started state and invalidates workers. */
    @Synchronized
    fun reset() {
        generation += 1
        attempts = 0
        mutable.value = EmbeddedEngineSnapshot()
    }
}
