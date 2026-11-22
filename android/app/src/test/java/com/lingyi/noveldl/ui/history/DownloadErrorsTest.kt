package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.DownloadDirectoryUnavailableException
import com.lingyi.noveldl.data.DownloadFailureException
import com.lingyi.noveldl.data.DownloadFailureKind
import com.lingyi.noveldl.data.DownloadHttpException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CancellationException

/**
 * `DownloadManager` failure reasons mapped to readable text instead of the
 * bare numeric code the completion broadcast carries, plus the fallback rules
 * that decide whether a failed custom-directory download changes destination.
 *
 * `DownloadManager`'s `ERROR_*`/`STATUS_*` values are compile-time int
 * constants, so all of this runs on a plain JVM.
 */
class DownloadErrorsTest {

    /** Every `COLUMN_REASON` value the platform defines (API 35). */
    private val knownReasons = listOf(
        DownloadManager.ERROR_UNKNOWN,
        DownloadManager.ERROR_FILE_ERROR,
        DownloadManager.ERROR_UNHANDLED_HTTP_CODE,
        DownloadManager.ERROR_HTTP_DATA_ERROR,
        DownloadManager.ERROR_TOO_MANY_REDIRECTS,
        DownloadManager.ERROR_INSUFFICIENT_SPACE,
        DownloadManager.ERROR_DEVICE_NOT_FOUND,
        DownloadManager.ERROR_CANNOT_RESUME,
        DownloadManager.ERROR_FILE_ALREADY_EXISTS,
    )

    // ------------------------------------------------------------------
    // COLUMN_REASON -> wording and cause kind.
    // ------------------------------------------------------------------

    @Test
    fun everyDownloadManagerReasonHasItsOwnWording() {
        val expected = mapOf(
            DownloadManager.ERROR_INSUFFICIENT_SPACE to R.string.download_error_no_space,
            DownloadManager.ERROR_FILE_ERROR to R.string.download_error_file,
            DownloadManager.ERROR_FILE_ALREADY_EXISTS to R.string.download_error_file_exists,
            DownloadManager.ERROR_DEVICE_NOT_FOUND to R.string.download_error_device,
            DownloadManager.ERROR_HTTP_DATA_ERROR to R.string.download_error_http_data,
            DownloadManager.ERROR_UNHANDLED_HTTP_CODE to R.string.download_error_http_code,
            DownloadManager.ERROR_TOO_MANY_REDIRECTS to R.string.download_error_redirects,
            DownloadManager.ERROR_CANNOT_RESUME to R.string.download_error_cannot_resume,
            DownloadManager.ERROR_UNKNOWN to R.string.download_error_unknown,
        )

        // The table above is the platform's whole set; a new code must be
        // added here (and to the mapper) instead of silently falling back to
        // "错误码 N".
        assertEquals("known reasons changed", expected.size, knownReasons.size)
        assertEquals("duplicate reason in the list", knownReasons.size, knownReasons.toSet().size)
        assertEquals("two reasons share one text", expected.size, expected.values.toSet().size)

        expected.forEach { (reason, message) ->
            assertEquals("wrong wording for reason $reason", message, downloadFailureReasonRes(reason))
        }
    }

    @Test
    fun everyDownloadManagerReasonClassifiesIntoItsCause() {
        val expected = mapOf(
            DownloadManager.ERROR_INSUFFICIENT_SPACE to DownloadFailureKind.NoSpace,
            DownloadManager.ERROR_FILE_ERROR to DownloadFailureKind.Provider,
            DownloadManager.ERROR_FILE_ALREADY_EXISTS to DownloadFailureKind.Provider,
            DownloadManager.ERROR_DEVICE_NOT_FOUND to DownloadFailureKind.Provider,
            DownloadManager.ERROR_HTTP_DATA_ERROR to DownloadFailureKind.Network,
            DownloadManager.ERROR_UNHANDLED_HTTP_CODE to DownloadFailureKind.Network,
            DownloadManager.ERROR_TOO_MANY_REDIRECTS to DownloadFailureKind.Network,
            DownloadManager.ERROR_CANNOT_RESUME to DownloadFailureKind.Network,
        )

        expected.forEach { (reason, kind) ->
            assertEquals("wrong kind for reason $reason", kind, downloadFailureKind(reason))
        }
        // "Unknown" has wording but no specific cause to name.
        assertNull(downloadFailureKind(DownloadManager.ERROR_UNKNOWN))
    }

    @Test
    fun aClassifiedReasonAlwaysHasWording() {
        // A kind without a message would leave the toast empty; every known
        // code must render something better than the numeric fallback.
        knownReasons.forEach { reason ->
            assertNotNull("reason $reason has no wording", downloadFailureReasonRes(reason))
            if (downloadFailureKind(reason) != null) {
                assertNotNull(
                    "reason $reason has a kind but no wording",
                    downloadFailureReasonRes(reason),
                )
            }
        }
    }

    @Test
    fun unknownReasonKeepsTheNumericFallback() {
        // The caller formats the code itself when nothing specific matches.
        for (reason in listOf(-1, 0, 999, 424242, Int.MAX_VALUE, Int.MIN_VALUE)) {
            assertNull("reason $reason", downloadFailureReasonRes(reason))
            assertNull("reason $reason", downloadFailureKind(reason))
        }
    }

    // ------------------------------------------------------------------
    // COLUMN_STATUS: only a failed download is a failure.
    // ------------------------------------------------------------------

    @Test
    fun onlyStatusFailedCountsAsAFailure() {
        assertEquals(
            DownloadCompletionState.Success,
            downloadCompletionState(DownloadManager.STATUS_SUCCESSFUL),
        )
        assertEquals(
            DownloadCompletionState.Failed,
            downloadCompletionState(DownloadManager.STATUS_FAILED),
        )
        // Queued/running and user-paused downloads are not failures: the
        // completion broadcast can arrive after a pause, and an unreadable
        // status (-1 from a failed query) must not be reported as one either.
        for (status in listOf(
            DownloadManager.STATUS_PENDING,
            DownloadManager.STATUS_RUNNING,
            DownloadManager.STATUS_PAUSED,
            -1,
            0,
            99,
        )) {
            assertEquals("status $status", DownloadCompletionState.Ignored, downloadCompletionState(status))
        }
    }

    // ------------------------------------------------------------------
    // SAF write failures -> wording.
    // ------------------------------------------------------------------

    @Test
    fun safWriteFailuresMapToTheirActionableTexts() {
        assertEquals(
            R.string.history_download_network_error,
            downloadFailureMessageRes(DownloadFailureKind.Network),
        )
        assertEquals(
            R.string.download_error_no_space,
            downloadFailureMessageRes(DownloadFailureKind.NoSpace),
        )
        assertEquals(
            R.string.download_error_permission,
            downloadFailureMessageRes(DownloadFailureKind.Permission),
        )
        assertEquals(
            R.string.download_error_provider,
            downloadFailureMessageRes(DownloadFailureKind.Provider),
        )
    }

    @Test
    fun everySafFailureKindHasItsOwnText() {
        // A new kind that silently reused another kind's text would mislabel
        // the failure the user is asked to fix.
        val resources = DownloadFailureKind.values().map(::downloadFailureMessageRes)
        assertEquals(DownloadFailureKind.values().size, resources.toSet().size)
    }

    @Test
    fun aBlankFailureMessageBecomesUnknownInsteadOfEmptyParentheses() {
        assertEquals("未知错误", downloadFailureReasonText(null, "未知错误"))
        assertEquals("未知错误", downloadFailureReasonText("", "未知错误"))
        assertEquals("未知错误", downloadFailureReasonText("   ", "未知错误"))
        assertEquals("HTTP 500", downloadFailureReasonText("HTTP 500", "未知错误"))
        assertEquals("存储空间不足", downloadFailureReasonText("存储空间不足", "未知错误"))
    }

    // ------------------------------------------------------------------
    // Fallback: only a vanished tree / denied permission can succeed in the
    // system Downloads directory; every other cause is reported in place.
    // ------------------------------------------------------------------

    @Test
    fun aMissingOrPermissionDeniedTreeFallsBackToSystemDownloads() {
        assertTrue(
            shouldFallBackToSystemDownloads(
                DownloadDirectoryUnavailableException("tree deleted"),
            ),
        )
        assertTrue(
            shouldFallBackToSystemDownloads(
                DownloadFailureException(DownloadFailureKind.Permission),
            ),
        )
        // A provider can deny createFile()/listFiles() before a document
        // exists; that raw SecurityException is the same revoked-grant case.
        assertTrue(
            shouldFallBackToSystemDownloads(SecurityException("grant revoked")),
        )
    }

    @Test
    fun spaceProviderNetworkAndCancellationNeverFallBack() {
        val terminal = listOf(
            DownloadFailureException(DownloadFailureKind.NoSpace),
            DownloadFailureException(DownloadFailureKind.Provider),
            DownloadFailureException(DownloadFailureKind.Network),
            IOException("unexpected end of stream"),
            IllegalStateException("HTTP 500"),
            CancellationException("navigated away"),
        )

        for (error in terminal) {
            assertFalse(
                "must not change the destination for ${error::class.simpleName}",
                shouldFallBackToSystemDownloads(error),
            )
        }
    }

    // ------------------------------------------------------------------
    // HTTP 4xx/5xx: readable wording carrying the code, never a fallback.
    // ------------------------------------------------------------------

    @Test
    fun http4xxAnd5xxAreReadableAndNeverFallBack() {
        for (status in listOf(400, 401, 403, 404, 409, 429, 500, 502, 503, 599)) {
            val message = "服务器返回 HTTP $status，文件没有保存"
            val error = DownloadHttpException(status = status, message = message)

            assertEquals(status, error.status)
            assertEquals(message, error.message)
            assertTrue("HTTP $status lost its code", error.message!!.contains(status.toString()))
            assertTrue(
                "catch blocks rely on the old IllegalStateException type",
                IllegalStateException::class.java.isInstance(error),
            )
            assertFalse(
                "HTTP $status must be reported, not retried elsewhere",
                shouldFallBackToSystemDownloads(error),
            )
        }
    }

    @Test
    fun aMessageOnlyHttpFailureIsStillNotAFallbackTrigger() {
        // Defense in depth: another call site may surface the status only in
        // the message. The decision must not mistake it for a destination
        // problem, and a blank/odd message must never crash it.
        val legacyErrors = listOf(
            IllegalStateException("HTTP 404"),
            IllegalStateException("Server returned HTTP 503; the file was not saved"),
            IllegalStateException(),
        )

        for (error in legacyErrors) {
            assertFalse(shouldFallBackToSystemDownloads(error))
        }
    }
}