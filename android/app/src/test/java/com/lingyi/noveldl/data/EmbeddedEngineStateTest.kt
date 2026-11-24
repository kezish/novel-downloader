package com.lingyi.noveldl.data

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * JVM tests for the engine start policy and its state machine.
 *
 * Everything here is Android-free: the port probe is injected, the health
 * loop takes a virtual clock and the state machine is a plain synchronized
 * object. The Chaquopy bridge is exercised only through the scheduling seam
 * already covered by [EmbeddedEngineTest].
 */
class EmbeddedEngineStateTest {

    @Before
    fun resetBefore() {
        EmbeddedEngine.resetForTest()
        clearApiService()
    }

    @After
    fun resetAfter() {
        // A queued stop (no Python interpreter in a JVM test) must finish
        // before the singleton state is reset for the next test.
        assertTrue(EmbeddedEngine.awaitBootstrapForTest())
        EmbeddedEngine.resetForTest()
        clearApiService()
    }

    /** [Api] is a singleton; drop the cached Retrofit service between tests. */
    private fun clearApiService() {
        val field = Api::class.java.getDeclaredField("service")
        field.isAccessible = true
        field.set(Api, null)
    }

    // ------------------------------------------------------------------
    // port selection
    // ------------------------------------------------------------------

    @Test
    fun portCandidatesStartAtTheDefaultAndAddThreeFallbacks() {
        assertEquals(
            listOf(8081, 8082, 8083, 8084),
            EmbeddedEnginePolicy.portCandidates(8081),
        )
    }

    @Test
    fun anOccupiedDefaultPortFallsBackToTheNextFreeOne() {
        val occupied = setOf(8081, 8082)

        val selected = EmbeddedEnginePolicy.selectPort(8081) { it !in occupied }

        assertEquals(8083, selected)
    }

    @Test
    fun allPortsOccupiedYieldsNoSelection() {
        assertNull(EmbeddedEnginePolicy.selectPort(8081) { false })
    }

    @Test
    fun aFreePreferredPortIsKept() {
        assertEquals(8081, EmbeddedEnginePolicy.selectPort(8081) { true })
    }

    // ------------------------------------------------------------------
    // bounded retry policy
    // ------------------------------------------------------------------

    @Test
    fun timeoutsAndPortRacesGetOneRetry() {
        assertTrue(
            EmbeddedEnginePolicy.shouldRetry(0, EmbeddedEngineFailure.TIMEOUT),
        )
        assertTrue(
            EmbeddedEnginePolicy.shouldRetry(0, EmbeddedEngineFailure.PORT_BUSY),
        )
        assertFalse(
            EmbeddedEnginePolicy.shouldRetry(1, EmbeddedEngineFailure.TIMEOUT),
        )
        assertFalse(
            EmbeddedEnginePolicy.shouldRetry(1, EmbeddedEngineFailure.PORT_BUSY),
        )
    }

    @Test
    fun aPythonInitialisationFailureIsNotRetried() {
        assertFalse(
            EmbeddedEnginePolicy.shouldRetry(0, EmbeddedEngineFailure.PYTHON_INIT),
        )
        assertFalse(
            EmbeddedEnginePolicy.shouldRetry(0, EmbeddedEngineFailure.UNKNOWN),
        )
        assertFalse(
            EmbeddedEnginePolicy.shouldRetry(0, EmbeddedEngineFailure.EXITED),
        )
    }

    // ------------------------------------------------------------------
    // health probe loop (virtual clock)
    // ------------------------------------------------------------------

    @Test
    fun healthPollingSucceedsOnTheFirstAnsweringProbe() {
        var probeCalls = 0
        var slept = 0L

        val ready = EmbeddedEnginePolicy.pollForHealth(
            timeoutMillis = 1_000,
            intervalMillis = 100,
            probe = {
                probeCalls += 1
                true
            },
            sleep = { slept += it },
            nowMillis = { 0L },
        )

        assertTrue(ready)
        assertEquals(1, probeCalls)
        assertEquals(0L, slept)
    }

    @Test
    fun healthPollingTimesOutWithoutExceedingTheDeadline() {
        var now = 0L
        var probeCalls = 0

        val ready = EmbeddedEnginePolicy.pollForHealth(
            timeoutMillis = 300,
            intervalMillis = 100,
            probe = {
                probeCalls += 1
                false
            },
            sleep = { now += it },
            nowMillis = { now },
        )

        assertFalse(ready)
        assertEquals("the loop must stop at the deadline", 300L, now)
        assertEquals(4, probeCalls)
    }

    @Test
    fun theLastSleepIsClippedToTheRemainingTime() {
        var now = 0L
        val sleeps = mutableListOf<Long>()

        EmbeddedEnginePolicy.pollForHealth(
            timeoutMillis = 150,
            intervalMillis = 100,
            probe = { false },
            sleep = {
                sleeps += it
                now += it
            },
            nowMillis = { now },
        )

        assertEquals(listOf(100L, 50L), sleeps)
    }

    // ------------------------------------------------------------------
    // state machine
    // ------------------------------------------------------------------

    @Test
    fun theFirstStartWinsAndRepeatedStartsAreIgnored() {
        val machine = EmbeddedEngineStateMachine()
        val first = machine.beginStart(8081)

        assertNotNull(first)
        assertNull("STARTING must be idempotent", machine.beginStart(8081))
        assertTrue(machine.isStarted)
        assertFalse(machine.isReady)
        assertEquals(EmbeddedEngineStatus.STARTING, machine.status)
    }

    @Test
    fun readyIsAlsoNotRestartableButAStopMakesItStartableAgain() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!
        assertTrue(machine.markReady(generation, 8081))

        assertNull(machine.beginStart(8081))
        assertTrue(machine.isReady)

        assertTrue(machine.markStopped(8081))
        assertEquals(EmbeddedEngineStatus.STOPPED, machine.status)
        assertFalse(machine.isReady)
        assertFalse("a stopped engine is not 'started'", machine.isStarted)

        val restarted = machine.beginStart(8081)
        assertNotNull("a stop must allow a later start", restarted)
        assertEquals(EmbeddedEngineStatus.STARTING, machine.status)
    }

    @Test
    fun aLateBootstrapCannotResurrectAStoppedEngine() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!

        machine.markStopped(8081)

        assertFalse(
            "a start that finished after stop must be ignored",
            machine.markReady(generation, 8081),
        )
        assertEquals(EmbeddedEngineStatus.STOPPED, machine.status)
        assertFalse(machine.isReady)
    }

    @Test
    fun aStoppedEngineIgnoresAFailureFromTheOldWorker() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!
        machine.markStopped(8081)

        machine.markFailed(generation, EmbeddedEngineFailure.PYTHON_INIT, "late", 8081)

        assertEquals(EmbeddedEngineStatus.STOPPED, machine.status)
        assertNull(machine.lastError)
    }

    @Test
    fun attemptsAreCountedAndTheLimitIsEnforced() {
        val machine = EmbeddedEngineStateMachine(maxAttempts = 2)
        val generation = machine.beginStart(8081)!!

        val retryAllowed = machine.recordAttemptFailure(
            generation,
            EmbeddedEngineFailure.TIMEOUT,
            "first",
            8081,
        )
        assertTrue(retryAllowed)
        assertEquals(1, machine.snapshots.value.attempt)
        assertEquals("first", machine.lastError)
        assertEquals(EmbeddedEngineStatus.STARTING, machine.status)

        val secondRetry = machine.recordAttemptFailure(
            generation,
            EmbeddedEngineFailure.TIMEOUT,
            "second",
            8081,
        )
        assertFalse("two attempts is the bound", secondRetry)
        assertEquals(2, machine.snapshots.value.attempt)
    }

    @Test
    fun aFailedEngineCanBeRestartedByAnExplicitStart() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!
        machine.markFailed(generation, EmbeddedEngineFailure.PYTHON_INIT, "boom", 8081)

        assertEquals(EmbeddedEngineStatus.FAILED, machine.status)
        assertTrue("legacy isStarted includes FAILED", machine.isStarted)
        assertFalse(machine.isReady)

        assertNotNull(
            "tapping the switch again is the retry signal",
            machine.beginStart(8081),
        )
        assertEquals(EmbeddedEngineStatus.STARTING, machine.status)
    }

    @Test
    fun aServerThatDiesRightAfterReadyIsReportedAsFailed() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!
        assertTrue(machine.markReady(generation, 8081))

        assertTrue(machine.markExited(generation, "gone", 8081))
        assertEquals(EmbeddedEngineStatus.FAILED, machine.status)
        assertEquals(EmbeddedEngineFailure.EXITED, machine.lastFailure)
        assertFalse("a dead server must not read as running", machine.isReady)
    }

    @Test
    fun aLateExitSignalAfterStopIsIgnored() {
        val machine = EmbeddedEngineStateMachine()
        val generation = machine.beginStart(8081)!!
        assertTrue(machine.markReady(generation, 8081))
        machine.markStopped(8081)

        assertFalse(machine.markExited(generation, "gone", 8081))
        assertEquals(EmbeddedEngineStatus.STOPPED, machine.status)
    }

    @Test
    fun theStateFlowPublishesEveryTransition() {
        val machine = EmbeddedEngineStateMachine()
        val seen = mutableListOf<EmbeddedEngineStatus>()

        val generation = machine.beginStart(8081)!!
        seen += machine.status
        machine.recordAttemptFailure(generation, EmbeddedEngineFailure.TIMEOUT, "slow", 8081)
        seen += machine.status
        machine.markReady(generation, 8081)
        seen += machine.status
        machine.markStopped(8081)
        seen += machine.status

        assertEquals(
            listOf(
                EmbeddedEngineStatus.STARTING,
                EmbeddedEngineStatus.STARTING,
                EmbeddedEngineStatus.READY,
                EmbeddedEngineStatus.STOPPED,
            ),
            seen,
        )
    }

    // ------------------------------------------------------------------
    // EmbeddedEngine wiring: stop, idempotency, token regeneration
    // ------------------------------------------------------------------

    @Test(timeout = 10_000)
    fun stoppingTheEngineFlipsTheStatusAndAllowsASecondStart() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val secondRun = AtomicInteger(0)

        EmbeddedEngine.ensureStarted("/engine/home") {
            entered.countDown()
            release.await(2, TimeUnit.SECONDS)
        }
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        release.countDown()
        assertTrue(EmbeddedEngine.awaitBootstrapForTest())
        assertTrue(EmbeddedEngine.isReady)

        EmbeddedEngine.stop()

        assertEquals(EmbeddedEngineStatus.STOPPED, EmbeddedEngine.status)
        assertFalse(EmbeddedEngine.isReady)
        assertTrue(EmbeddedEngine.awaitBootstrapForTest())

        val restarted = CountDownLatch(1)
        EmbeddedEngine.ensureStarted("/engine/home") {
            secondRun.incrementAndGet()
            restarted.countDown()
        }
        assertTrue(restarted.await(5, TimeUnit.SECONDS))
        assertTrue(EmbeddedEngine.awaitBootstrapForTest())
        assertEquals(1, secondRun.get())
        assertTrue(EmbeddedEngine.isReady)
    }

    @Test(timeout = 10_000)
    fun aRepeatedStartWhileReadyDoesNotRunTheBootstrapAgain() {
        val runs = AtomicInteger(0)
        val first = CountDownLatch(1)

        EmbeddedEngine.ensureStarted("/engine/home") {
            runs.incrementAndGet()
            first.countDown()
        }
        assertTrue(first.await(5, TimeUnit.SECONDS))
        EmbeddedEngine.ensureStarted("/engine/home") { runs.incrementAndGet() }
        assertTrue(EmbeddedEngine.awaitBootstrapForTest())

        assertEquals(1, runs.get())
    }

    @Test
    fun theApiClientFollowsARegeneratedEngineToken() {
        Api.configure(EmbeddedEngine.baseUrl, "user-key", embeddedEngine = true)
        val first = Api.requestKey
        assertEquals(EmbeddedEngine.token, first)
        assertNotEquals("user-key", first)

        // resetForTest models a stop/restart cycle: a new token is minted for
        // the next server, and the client must resolve it without being
        // reconfigured (the interceptor reads the credential per request).
        EmbeddedEngine.resetForTest()

        assertEquals(EmbeddedEngine.token, Api.requestKey)
        assertNotEquals(first, Api.requestKey)
    }

    @Test
    fun aRemoteTargetIsUnaffectedByTokenRegeneration() {
        Api.configure(Api.DEFAULT_BASE_URL, "user-key", embeddedEngine = false)
        val before = Api.requestKey

        EmbeddedEngine.resetForTest()

        assertEquals("user-key", Api.requestKey)
        assertEquals(before, Api.requestKey)
    }
}
