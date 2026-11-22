package com.lingyi.noveldl.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Response
import retrofit2.HttpException
import retrofit2.Retrofit
import java.io.IOException
import java.io.InterruptedIOException
import java.net.ProtocolException
import java.net.UnknownHostException
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

/**
 * Waits between a failed attempt and its retry.
 *
 * Injected so unit tests can exclude the real wait (a virtual clock) and so
 * cancellation can be observed without relying on [Thread.sleep] being
 * interruptible. The return value is `false` when [call] was cancelled while
 * waiting; the interceptor then abandons the request instead of retrying.
 */
internal fun interface RetrySleeper {
    fun sleep(millis: Long, call: Call): Boolean
}

/**
 * Production sleeper. OkHttp interceptors run synchronously on the dispatcher
 * thread, and a coroutine cancellation only closes the socket: it does not
 * interrupt a sleeping interceptor. The wait is therefore sliced and
 * [Call.isCanceled] is polled between slices, so a cancelled call stops within
 * one slice instead of blocking the dispatcher for the whole backoff.
 */
internal object CancellableRetrySleeper : RetrySleeper {
    private const val SLICE_MILLIS = 50L

    override fun sleep(millis: Long, call: Call): Boolean {
        if (millis <= 0) return !call.isCanceled()
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(millis)
        while (true) {
            if (call.isCanceled()) return false
            val remaining = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())
            if (remaining <= 0) return true
            try {
                Thread.sleep(minOf(remaining, SLICE_MILLIS))
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
        }
    }
}

/**
 * Holds the Retrofit service and classifies failures into [ApiErrorCode]s.
 *
 * No user-facing text lives here: the UI resolves the code through
 * `apiErrorMessage(...)` so both languages stay in `strings.xml`.
 */
object Api {
    const val DEFAULT_BASE_URL: String = "http://10.0.2.2:8081/"

    /** Retries after the first attempt, i.e. at most three attempts total. */
    internal const val MAX_RETRIES: Int = 2

    /** First backoff step; later steps multiply by [RETRY_BACKOFF_MULTIPLIER]. */
    internal const val RETRY_BASE_DELAY_MILLIS: Long = 300

    internal const val RETRY_BACKOFF_MULTIPLIER: Long = 3

    /** Ceiling for computed exponential backoff. */
    internal const val RETRY_MAX_DELAY_MILLIS: Long = 5_000

    /**
     * Ceiling for a server supplied `Retry-After`. A throttled endpoint must
     * not be able to park the shared dispatcher for minutes; longer hints are
     * clipped and the client decides when to try again.
     */
    internal const val MAX_RETRY_AFTER_MILLIS: Long = 10_000

    /** Test seam: replaces the real wait so retry tests do not sleep. */
    @Volatile
    internal var retrySleeper: RetrySleeper = CancellableRetrySleeper

    /**
     * Default overall deadline for one API call. `readTimeout` only bounds a
     * single socket read, so a server that trickles one byte per read could
     * otherwise keep a request alive indefinitely.
     */
    const val DEFAULT_CALL_TIMEOUT_MILLIS: Long = 120_000

    /**
     * Shared factory for every client produced by [configure]. `newBuilder()`
     * keeps the dispatcher and connection pool, so re-configuring the app
     * (settings save, standalone toggle, process start) does not strand a
     * thread pool per previous configuration.
     */
    private val baseClient: OkHttpClient = OkHttpClient.Builder()
        // OkHttp's own retry can replay a request that was written but got no
        // response. For a non-idempotent POST (adding a task, submitting a
        // login, exporting) a blind replay can duplicate the operation, so it
        // is disabled here and re-added with an explicit policy by
        // [retryInterceptor]: GET/HEAD may be replayed for transport failures
        // and transient statuses, other methods only for the statuses that
        // explicitly say the request was not processed (408/429).
        .retryOnConnectionFailure(false)
        .build()

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Volatile
    private var service: ApiService? = null

    /**
     * Response-size ceiling enforced before the converter buffers a body.
     * Exposed to unit tests so the guard can be exercised without allocating a
     * multi-megabyte payload.
     */
    @Volatile
    internal var maxResponseBytes: Long = MAX_JSON_RESPONSE_BYTES

    @Volatile
    var baseUrl: String = DEFAULT_BASE_URL
        private set

    /** Trimmed API key of the active configuration; empty when unauthenticated. */
    @Volatile
    var apiKey: String = ""
        private set

    /**
     * The credential the active configuration sends: the per-process engine
     * token when [baseUrl] is the in-process engine, otherwise the user's
     * remote [apiKey]. Empty when the target needs none.
     *
     * Kept separate from [apiKey] so a remote key can never be replayed to
     * the loopback engine and the engine token can never leave the device
     * (the origin policy also strips it on any cross-origin redirect).
     */
    @Volatile
    var requestKey: String = ""
        private set

    /**
     * Builds the Retrofit service. [baseUrl] is trimmed and normalized to end
     * with a slash; a blank value falls back to [DEFAULT_BASE_URL].
     *
     * A non-blank value that is not a usable `http(s)` URL (e.g. user-typed
     * text like "my pc") would make Retrofit's builder throw, crashing the
     * caller's coroutine; such input is contained by falling back to
     * [DEFAULT_BASE_URL] as well. Returns whether the passed URL (after the
     * blank fallback) was used as-is — `false` means the input was malformed
     * and the default is now active.
     *
     * Timeouts: connect 10 s, read/write 60 s (a search can legitimately take
     * a while; polling requests are short) and one overall call deadline of
     * [callTimeoutMillis]. They are parameters so the transport behaviour can
     * be exercised from unit tests.
     */
    @JvmOverloads
    fun configure(
        baseUrl: String,
        apiKey: String?,
        connectTimeoutMillis: Long = 10_000,
        readTimeoutMillis: Long = 60_000,
        writeTimeoutMillis: Long = 60_000,
        callTimeoutMillis: Long = DEFAULT_CALL_TIMEOUT_MILLIS,
        embeddedEngine: Boolean = false,
    ): Boolean {
        val trimmed = baseUrl.trim()
        val candidate = if (trimmed.endsWith("/")) trimmed else "$trimmed/"
        val parsed = candidate.toHttpUrlOrNull()
        val usable = trimmed.isNotEmpty() && parsed != null
        val normalized = if (usable) candidate else DEFAULT_BASE_URL
        this.baseUrl = normalized
        val key = apiKey?.trim().orEmpty()
        this.apiKey = key
        // The in-process engine authenticates with its own per-process token;
        // the user's remote key is never sent there. A remote target gets the
        // user's key and never the local token. [embeddedEngine] is the
        // persisted standalone flag, so a URL that merely looks like the
        // engine (a `adb reverse` target) keeps using the typed key.
        val effectiveKey = effectiveRequestKey(
            baseUrl = normalized,
            apiKey = key,
            embeddedEngine = embeddedEngine,
            engineToken = EmbeddedEngine.token,
            engineBaseUrl = EmbeddedEngine.BASE_URL,
        )
        this.requestKey = effectiveKey
        val origin = ApiOrigin.of(normalized.toHttpUrl())
        val client = baseClient.newBuilder()
            .connectTimeout(connectTimeoutMillis, TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMillis, TimeUnit.MILLISECONDS)
            .writeTimeout(writeTimeoutMillis, TimeUnit.MILLISECONDS)
            .callTimeout(callTimeoutMillis, TimeUnit.MILLISECONDS)
            // Size guard outermost so retries see the raw status line: a
            // transient 5xx with a large body must still be retried, and only
            // the final response is size-bounded.
            .addInterceptor(responseSizeGuard())
            .addInterceptor(retryInterceptor())
            // Network (not application) interceptor: redirects must be seen so
            // the key is not forwarded to another origin.
            .addNetworkInterceptor(ApiOriginPolicy(origin, effectiveKey).interceptor())
            .build()
        service = Retrofit.Builder()
            .baseUrl(normalized)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(ApiService::class.java)
        return usable
    }

    /** Whether [configure] has been called at least once. */
    fun isConfigured(): Boolean = service != null

    fun service(): ApiService =
        service ?: throw ApiFailure(ApiErrorCode.NOT_CONFIGURED)

    /**
     * Runs [block] and converts exceptions into a user-readable [Result].
     *
     * Failures are [ApiFailure]s carrying an [ApiErrorCode]; [ApiFailure]
     * itself is passed through unchanged so a nested `Api.call` (or
     * [service] throwing "not configured") keeps its original code.
     *
     * [CancellationException] is rethrown so that cancelled coroutines (a
     * LaunchedEffect restarting its poll, navigating away mid-request) stay
     * cancelled instead of resolving to a bogus failure result.
     */
    suspend fun <T> call(block: suspend () -> T): Result<T> = try {
        Result.success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: ApiFailure) {
        Result.failure(e)
    } catch (e: HttpException) {
        Result.failure(ApiFailure(ApiErrorCode.HTTP, readableError(e), e))
    } catch (e: SerializationException) {
        // A 200 response whose body is not the expected JSON (proxy/captive
        // portal HTML, empty bodies) surfaces as a decoding error; keep the
        // decoder's own text as the detail, the UI supplies the sentence.
        val raw = e.message?.take(160)?.ifBlank { "not JSON" } ?: "not JSON"
        Result.failure(ApiFailure(ApiErrorCode.BAD_RESPONSE, raw, e))
    } catch (e: ResponseTooLargeException) {
        Result.failure(
            ApiFailure(ApiErrorCode.BAD_RESPONSE, e.message ?: "response too large", e),
        )
    } catch (e: InsecureRedirectException) {
        Result.failure(
            ApiFailure(ApiErrorCode.INSECURE_REDIRECT, e.message ?: "insecure redirect", e),
        )
    } catch (e: IOException) {
        Result.failure(transportFailure(baseUrl, e))
    } catch (e: Exception) {
        Result.failure(
            ApiFailure(
                ApiErrorCode.UNKNOWN,
                e.message ?: e.javaClass.simpleName,
                e,
            ),
        )
    }

    /**
     * Classifies a transport failure. A loopback request can only fail because
     * the embedded engine has not bound its port yet (or died), which is a
     * different situation from a wrong/unreachable remote address.
     *
     * Outside the loopback case the socket exception is mapped to the most
     * specific [ApiErrorCode]: [ApiErrorCode.TIMEOUT] for any interruptible
     * timeout (connect/read/call), [ApiErrorCode.DNS] for an unresolvable
     * host, [ApiErrorCode.TLS] for a failed handshake, and
     * [ApiErrorCode.NETWORK] for everything else.
     */
    internal fun transportFailure(baseUrl: String, error: IOException): ApiFailure {
        val detail = error.message ?: error.javaClass.simpleName
        if (baseUrl == EmbeddedEngine.BASE_URL) {
            return ApiFailure(ApiErrorCode.ENGINE_UNREACHABLE, detail, error)
        }
        val code = when (error) {
            // SocketTimeoutException (connect/read) and OkHttp's call timeout
            // both extend InterruptedIOException.
            is InterruptedIOException -> ApiErrorCode.TIMEOUT
            is UnknownHostException -> ApiErrorCode.DNS
            is SSLException -> ApiErrorCode.TLS
            else -> ApiErrorCode.NETWORK
        }
        return ApiFailure(code, detail, error)
    }

    /**
     * Retries a request at most [MAX_RETRIES] times.
     *
     * Transport failures (connection reset, dropped keep-alive connection,
     * DNS hiccup) are retried for GET/HEAD only, with exponential backoff.
     * Timeouts and TLS failures are not retried: the connection already had
     * its chance, and a second wait would only delay the user's error.
     *
     * Transient statuses (408, 429, every 5xx) are retried after the response
     * body is closed. A POST/PUT/DELETE is replayed only for 408/429, where
     * the server explicitly says the request was not applied; a 5xx may have
     * been produced after a side effect (a task row inserted, a login
     * submitted) and must not be duplicated.
     *
     * `Retry-After` (seconds or HTTP-date) takes precedence over the computed
     * backoff and is clipped to [MAX_RETRY_AFTER_MILLIS]. The wait goes
     * through [retrySleeper] so tests can avoid real time and cancellation is
     * observed between backoff slices.
     */
    private fun retryInterceptor(): Interceptor = Interceptor { chain -> executeWithRetry(chain) }

    private fun executeWithRetry(chain: okhttp3.Interceptor.Chain): Response {
        val request = chain.request()
        val safeMethod = request.method == "GET" || request.method == "HEAD"
        // A one-shot body would make a replay fail with "cannot replay";
        // JSON bodies produced by Retrofit are replayable, but check anyway.
        val replayableBody = request.body?.isOneShot() != true
        var attempt = 0
        while (true) {
            val response: Response
            try {
                response = chain.proceed(request)
            } catch (e: IOException) {
                if (!transportFailureIsRetryable(chain.call(), safeMethod, e, attempt)) {
                    throw e
                }
                val delayMillis = backoffMillis(attempt)
                attempt += 1
                waitBeforeRetry(chain.call(), delayMillis)
                continue
            }
            if (!statusIsRetryable(chain.call(), safeMethod, replayableBody, response.code, attempt)) {
                return response
            }
            val delayMillis = retryAfterMillis(response) ?: backoffMillis(attempt)
            // Release the connection before waiting; an unread error body
            // would otherwise pin the socket for the whole backoff.
            response.close()
            attempt += 1
            waitBeforeRetry(chain.call(), delayMillis)
        }
    }

    private fun transportFailureIsRetryable(
        call: Call,
        safeMethod: Boolean,
        error: IOException,
        attempt: Int,
    ): Boolean = safeMethod && attempt < MAX_RETRIES && !call.isCanceled() &&
        error !is NonRetryableTransportFailure &&
        error !is InterruptedIOException &&
        error !is SSLException &&
        error !is ProtocolException

    private fun statusIsRetryable(
        call: Call,
        safeMethod: Boolean,
        replayableBody: Boolean,
        code: Int,
        attempt: Int,
    ): Boolean {
        if (attempt >= MAX_RETRIES || call.isCanceled() || !replayableBody) return false
        val transient = code == 408 || code == 429 || code in 500..599
        if (!transient) return false
        // 5xx may follow a partially applied non-idempotent request; only the
        // explicit "not processed" statuses are safe to replay for those.
        return safeMethod || code == 408 || code == 429
    }

    /** 300 ms, 900 ms, 2.7 s ... capped at [RETRY_MAX_DELAY_MILLIS]. */
    private fun backoffMillis(attempt: Int): Long {
        var delay = RETRY_BASE_DELAY_MILLIS
        repeat(attempt) {
            delay = (delay * RETRY_BACKOFF_MULTIPLIER).coerceAtMost(RETRY_MAX_DELAY_MILLIS)
        }
        return delay
    }

    /**
     * The server's `Retry-After` hint, or `null` when absent/unparseable.
     * Integer seconds are the common form; an RFC 1123 date is also accepted.
     * Negative or past values yield 0 (retry immediately) and long hints are
     * clipped to [MAX_RETRY_AFTER_MILLIS].
     */
    private fun retryAfterMillis(response: Response): Long? {
        val raw = response.header("Retry-After")?.trim().orEmpty()
        if (raw.isEmpty()) return null
        raw.toLongOrNull()?.let { seconds ->
            if (seconds <= 0) return 0L
            val millis = if (seconds > Long.MAX_VALUE / 1_000) Long.MAX_VALUE else seconds * 1_000
            return millis.coerceAtMost(MAX_RETRY_AFTER_MILLIS)
        }
        val at = runCatching {
            ZonedDateTime.parse(raw, DateTimeFormatter.RFC_1123_DATE_TIME)
        }.getOrNull() ?: return null
        return (at.toInstant().toEpochMilli() - System.currentTimeMillis())
            .coerceIn(0L, MAX_RETRY_AFTER_MILLIS)
    }

    private fun waitBeforeRetry(call: Call, millis: Long) {
        // A call cancelled while sleeping (including OkHttp's call timeout,
        // which cancels the call) must not be replayed. InterruptedIOException
        // keeps the failure classified as a timeout when the coroutine itself
        // is not cancelled; a cancelled coroutine never observes this value.
        if (!retrySleeper.sleep(millis, call) || call.isCanceled()) {
            throw InterruptedIOException("retry wait cancelled")
        }
    }

    /**
     * Refuses oversized success bodies before the JSON converter buffers them.
     *
     * A declared `Content-Length` is checked up front; the returned body is
     * also wrapped in [BoundedResponseBody] so a chunked response (which has
     * no length) fails once it crosses the same limit.
     */
    private fun responseSizeGuard(): Interceptor = Interceptor { chain ->
        val response = chain.proceed(chain.request())
        val limit = maxResponseBytes
        val body = response.body ?: return@Interceptor response
        if (body.contentLength() > limit) {
            response.close()
            throw ResponseTooLargeException("response body larger than $limit bytes")
        }
        response.newBuilder()
            .body(BoundedResponseBody(body, limit))
            .build()
    }

    private fun readableError(e: HttpException): String {
        // Bounded: the message is rendered as one line, but an error endpoint
        // (or a proxy) can otherwise make us buffer the whole body.
        val body = e.response()?.errorBody().readAtMost(MAX_ERROR_BODY_BYTES)
        val detail = Regex("\"detail\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"")
            .find(body)
            ?.groupValues
            ?.getOrNull(1)
            ?.let(::unescapeJsonString)
        return detail ?: "HTTP ${e.code()}"
    }

    /** Best-effort JSON string unescaping for error details. */
    private fun unescapeJsonString(raw: String): String = buildString(raw.length) {
        var i = 0
        while (i < raw.length) {
            val c = raw[i]
            if (c == '\\' && i + 1 < raw.length) {
                when (val next = raw[i + 1]) {
                    'n' -> {
                        append('\n'); i += 2
                    }
                    't' -> {
                        append('\t'); i += 2
                    }
                    'r' -> {
                        append('\r'); i += 2
                    }
                    'b' -> {
                        append('\b'); i += 2
                    }
                    'u' -> {
                        val hex = if (i + 6 <= raw.length) raw.substring(i + 2, i + 6) else null
                        val code = hex?.let { runCatching { it.toInt(16) }.getOrNull() }
                        if (code != null) {
                            append(code.toChar())
                            i += 6
                        } else {
                            append(next); i += 2
                        }
                    }
                    else -> {
                        append(next); i += 2
                    }
                }
            } else {
                append(c); i += 1
            }
        }
    }
}
