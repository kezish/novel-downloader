package com.lingyi.noveldl.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.SocketTimeoutException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Retry, timeout and credential-scoping contract of [Api].
 *
 * The real wait is replaced by [Api.retrySleeper] so these tests never sleep
 * on a backoff; the recorded delays pin the policy itself.
 */
class ApiClientRetryTest {

    private lateinit var server: MockWebServer
    private val delays = CopyOnWriteArrayList<Long>()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        delays.clear()
        Api.retrySleeper = RetrySleeper { millis, _ ->
            delays += millis
            true
        }
        resetService()
    }

    @After
    fun tearDown() {
        Api.retrySleeper = CancellableRetrySleeper
        server.shutdown()
        resetService()
    }

    private fun baseUrl(): String = server.url("/").toString()

    private fun json(body: String, code: Int = 200): MockResponse =
        MockResponse()
            .setResponseCode(code)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    private val healthOk = """{"status":"ok","version":"1"}"""

    /** [Api] is a singleton; clear the cached Retrofit service between tests. */
    private fun resetService() {
        val field = Api::class.java.getDeclaredField("service")
        field.isAccessible = true
        field.set(Api, null)
    }

    // ------------------------------------------------------------------
    // Transient statuses: retried with bounded exponential backoff.
    // ------------------------------------------------------------------

    @Test
    fun serverErrorsAreRetriedWithExponentialBackoff() = runBlocking {
        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(json("""{"detail":"still starting"}""", 502))
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue("a transient 5xx must not surface to the user", result.isSuccess)
        assertEquals(3, server.requestCount)
        assertEquals(
            listOf(Api.RETRY_BASE_DELAY_MILLIS, Api.RETRY_BASE_DELAY_MILLIS * Api.RETRY_BACKOFF_MULTIPLIER),
            delays.toList(),
        )
    }

    @Test
    fun exhaustedServerErrorsMapToTheLastReadableStatus() = runBlocking {
        repeat(Api.MAX_RETRIES + 1) {
            server.enqueue(json("""{"detail":"backend still starting"}""", 503))
        }
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        val failure = result.exceptionOrNull() as ApiFailure
        assertEquals(ApiErrorCode.HTTP, failure.code)
        // The data layer only carries the detail; the UI renders
        // strings.xml/api_error_* for the code (HTTP is detail-only).
        assertEquals("backend still starting", failure.detail)
        assertEquals(Api.MAX_RETRIES + 1, server.requestCount)
        assertEquals(Api.MAX_RETRIES, delays.size)
    }

    @Test
    fun exhaustedServerErrorsWithoutDetailFallBackToTheStatusCode() = runBlocking {
        repeat(Api.MAX_RETRIES + 1) {
            server.enqueue(json("""{"oops":true}""", 500))
        }
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertEquals(ApiErrorCode.HTTP, (result.exceptionOrNull() as ApiFailure).code)
        assertEquals("HTTP 500", result.exceptionOrNull()?.message)
    }

    @Test
    fun everyFiveHundredStatusIsTreatedAsTransient() = runBlocking {
        // Representative 5xx family members, including 501/505: all are
        // retried, because the server may recover on the next attempt.
        for (code in listOf(500, 501, 502, 503, 504, 505)) {
            val before = server.requestCount
            delays.clear()
            server.enqueue(json("""{"detail":"transient"}""", code))
            server.enqueue(json(healthOk))
            Api.configure(baseUrl(), null)

            val result = Api.call { Api.service().health() }

            assertTrue("$code should be retried", result.isSuccess)
            assertEquals("$code attempts", 2, server.requestCount - before)
        }
    }

    // ------------------------------------------------------------------
    // Client errors are deterministic: replaying them only repeats the
    // failure (and a 401 replay may hammer the server after a bad key).
    // ------------------------------------------------------------------

    @Test
    fun clientErrorsAreNotRetried() = runBlocking {
        Api.configure(baseUrl(), null)

        for (code in listOf(400, 401, 403, 404, 422)) {
            val before = server.requestCount
            server.enqueue(json("""{"detail":"rejected"}""", code))

            val result = Api.call { Api.service().health() }

            assertTrue(result.isFailure)
            assertEquals(ApiErrorCode.HTTP, (result.exceptionOrNull() as ApiFailure).code)
            assertEquals("$code must not be replayed", 1, server.requestCount - before)
        }
        assertTrue("4xx must not wait for a retry: $delays", delays.isEmpty())
    }

    // ------------------------------------------------------------------
    // 429 + Retry-After.
    // ------------------------------------------------------------------

    @Test
    fun tooManyRequestsHonoursRetryAfterSeconds() = runBlocking {
        server.enqueue(
            json("""{"detail":"slow down"}""", 429)
                .setHeader("Retry-After", "2"),
        )
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isSuccess)
        assertEquals(2, server.requestCount)
        // The server hint overrides exponential backoff (300 ms).
        assertEquals(listOf(TimeUnit.SECONDS.toMillis(2)), delays.toList())
    }

    @Test
    fun retryAfterZeroMeansRetryImmediately() = runBlocking {
        server.enqueue(
            json("""{"detail":"slow down"}""", 429)
                .setHeader("Retry-After", "0"),
        )
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        assertTrue(Api.call { Api.service().health() }.isSuccess)
        assertEquals(listOf(0L), delays.toList())
    }

    @Test
    fun retryAfterLongHintsAreClippedToKeepTheDispatcherUsable() = runBlocking {
        server.enqueue(
            json("""{"detail":"slow down"}""", 429)
                .setHeader("Retry-After", "600"),
        )
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        assertTrue(Api.call { Api.service().health() }.isSuccess)
        assertEquals(listOf(Api.MAX_RETRY_AFTER_MILLIS), delays.toList())
    }

    @Test
    fun anUnparseableRetryAfterFallsBackToExponentialBackoff() = runBlocking {
        server.enqueue(
            json("""{"detail":"slow down"}""", 429)
                .setHeader("Retry-After", "soon-ish"),
        )
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        assertTrue(Api.call { Api.service().health() }.isSuccess)
        assertEquals(listOf(Api.RETRY_BASE_DELAY_MILLIS), delays.toList())
    }

    @Test
    fun tooManyRequestsIsReplayedForSideEffectingMethods() = runBlocking {
        // 429 says the request was rejected, not applied, so replaying a POST
        // cannot duplicate the operation.
        server.enqueue(
            json("""{"detail":"slow down"}""", 429)
                .setHeader("Retry-After", "1"),
        )
        server.enqueue(json("""{"task_id":"t1"}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addTask(AddTaskRequest(site = "qidian", book_id = "1"))
        }

        assertTrue(result.isSuccess)
        assertEquals(2, server.requestCount)
        assertEquals(listOf(1_000L), delays.toList())
    }

    @Test
    fun serverErrorsAreNotReplayedForSideEffectingMethods() = runBlocking {
        // A 500 may arrive after the task row was inserted; replaying would
        // queue it twice.
        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(json("""{"task_id":"t1"}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addTask(AddTaskRequest(site = "qidian", book_id = "1"))
        }

        assertTrue(result.isFailure)
        assertEquals(ApiErrorCode.HTTP, (result.exceptionOrNull() as ApiFailure).code)
        assertEquals(1, server.requestCount)
        assertTrue(delays.isEmpty())
    }

    // ------------------------------------------------------------------
    // Transport failures: same bounded retry for safe methods, classified
    // as a network error when the retries are exhausted.
    // ------------------------------------------------------------------

    @Test
    fun networkErrorsAreRetriedAndExhaustedAsANetworkFailure() = runBlocking {
        repeat(Api.MAX_RETRIES + 1) {
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        }
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        assertEquals(ApiErrorCode.NETWORK, (result.exceptionOrNull() as ApiFailure).code)
        assertEquals(Api.MAX_RETRIES + 1, server.requestCount)
        assertEquals(
            listOf(
                Api.RETRY_BASE_DELAY_MILLIS,
                Api.RETRY_BASE_DELAY_MILLIS * Api.RETRY_BACKOFF_MULTIPLIER,
            ),
            delays.toList(),
        )
    }

    @Test
    fun networkErrorsAreNotReplayedForSideEffectingMethods() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
        server.enqueue(json("""{"task_id":"t1"}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addTask(AddTaskRequest(site = "qidian", book_id = "1"))
        }

        assertTrue(result.isFailure)
        assertEquals(1, server.requestCount)
        assertTrue(delays.isEmpty())
    }

    // ------------------------------------------------------------------
    // Timeouts are a distinct transport diagnosis, never retried: a second
    // wait only delays the user's error message.
    // ------------------------------------------------------------------

    @Test
    fun connectAndReadTimeoutsAreClassifiedAsTimeouts() {
        val base = "http://10.0.2.2:8081/"

        assertEquals(
            ApiErrorCode.TIMEOUT,
            Api.transportFailure(base, SocketTimeoutException("connect timed out")).code,
        )
        assertEquals(
            ApiErrorCode.TIMEOUT,
            Api.transportFailure(base, SocketTimeoutException("read timed out")).code,
        )
    }

    @Test
    fun aReadTimeoutIsNotRetried() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        Api.configure(baseUrl(), null, connectTimeoutMillis = 1_000, readTimeoutMillis = 150)

        val result = Api.call { Api.service().health() }

        val failure = result.exceptionOrNull() as ApiFailure
        assertEquals(ApiErrorCode.TIMEOUT, failure.code)
        assertEquals(1, server.requestCount)
        assertTrue("timeouts must not trigger backoff: $delays", delays.isEmpty())
    }

    // ------------------------------------------------------------------
    // Cancellation. A coroutine cancel closes the call; a retry that is
    // already in its backoff must observe that and stop immediately instead
    // of issuing another request after the screen is gone.
    // ------------------------------------------------------------------

    @Test
    fun cancellingDuringBackoffStopsTheRetry() = runBlocking {
        val sleeping = CountDownLatch(1)
        val release = CountDownLatch(1)
        Api.retrySleeper = RetrySleeper { _, call ->
            sleeping.countDown()
            release.await(5, TimeUnit.SECONDS)
            !call.isCanceled()
        }
        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null)

        var resolved: Result<Health>? = null
        val job = launch(Dispatchers.IO) {
            resolved = Api.call { Api.service().health() }
        }

        assertTrue("backoff should have started", sleeping.await(5, TimeUnit.SECONDS))
        assertNotNull(server.takeRequest(1, TimeUnit.SECONDS))
        job.cancelAndJoin()
        release.countDown()

        assertNull(
            "a cancelled call must not issue its retry",
            server.takeRequest(500, TimeUnit.MILLISECONDS),
        )
        assertNull("a cancelled call must not resolve to a failure Result", resolved)
        assertTrue(job.isCancelled)
    }

    // ------------------------------------------------------------------
    // Concurrent calls keep independent attempt counters; one call's 5xx
    // must not make another call retry or fail.
    // ------------------------------------------------------------------

    @Test
    fun concurrentCallsKeepIndependentRetryState() = runBlocking {
        val healthAttempts = AtomicInteger()
        val sitesAttempts = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                "/api/v1/health" -> {
                    val attempt = healthAttempts.incrementAndGet()
                    if (attempt == 1) json("""{"detail":"boom"}""", 500) else json(healthOk)
                }
                "/api/v1/sites" -> {
                    sitesAttempts.incrementAndGet()
                    json("""{"search":{},"download":{}}""")
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        Api.configure(baseUrl(), null)

        val health = async(Dispatchers.IO) { Api.call { Api.service().health() } }
        val sites = async(Dispatchers.IO) { Api.call { Api.service().sites() } }
        val healthResult = health.await()
        val sitesResult = sites.await()

        assertTrue("health should recover on its own retry", healthResult.isSuccess)
        assertTrue("sites must not be affected by health's retry", sitesResult.isSuccess)
        assertEquals(2, healthAttempts.get())
        assertEquals(1, sitesAttempts.get())
    }

    // ------------------------------------------------------------------
    // Credential scoping: the per-process engine token must never be sent
    // to a remote/third-party origin, and a redirect to a CDN must not carry
    // the configured credential.
    // ------------------------------------------------------------------

    @Test
    fun embeddedModeOnARemoteOriginSendsTheUserKeyNotTheEngineToken() = runBlocking {
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), "user-key", embeddedEngine = true)

        assertTrue(Api.call { Api.service().health() }.isSuccess)

        val recorded = server.takeRequest()
        assertEquals("user-key", recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNotEquals(EmbeddedEngine.token, recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNull(recorded.getHeader("Authorization"))
    }

    @Test
    fun embeddedModeWithoutAUserKeySendsNoCredentialToARemoteOrigin() = runBlocking {
        server.enqueue(json(healthOk))
        Api.configure(baseUrl(), null, embeddedEngine = true)

        assertTrue(Api.call { Api.service().health() }.isSuccess)

        val recorded = server.takeRequest()
        assertNull(recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNull(recorded.getHeader("Authorization"))
    }

    @Test
    fun aCdnRedirectNeverReceivesTheConfiguredCredential() = runBlocking {
        val cdn = MockWebServer()
        cdn.start()
        try {
            cdn.enqueue(json(healthOk))
            server.enqueue(
                MockResponse()
                    .setResponseCode(302)
                    .setHeader("Location", cdn.url("/api/v1/health").toString()),
            )
            Api.configure(baseUrl(), "secret", embeddedEngine = true)

            assertTrue(Api.call { Api.service().health() }.isSuccess)

            val redirected = cdn.takeRequest()
            assertNull(
                "the credential must not follow a redirect to a third-party CDN",
                redirected.getHeader(DOWNLOAD_API_KEY_HEADER),
            )
            assertNull(redirected.getHeader("Authorization"))
            assertFalse(EmbeddedEngine.token == redirected.getHeader(DOWNLOAD_API_KEY_HEADER))
        } finally {
            cdn.shutdown()
        }
    }
}