package com.lingyi.noveldl.data

import com.lingyi.noveldl.ui.history.shouldFallBackToSystemDownloads
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Failure side of the SAF download: the partial document created for the
 * attempt is deleted before the error is reported, so a fallback to the system
 * Downloads directory (or a retry) never leaves a truncated file in `NovelDL/`.
 *
 * `downloadToDirectory` itself needs a `Context`/`ContentResolver`, but the
 * cleanup + result mapping is a plain function over [SafDocument], so the
 * ordering rule is covered on the JVM without a device.
 */
class HistoryDownloadFailureTest {

    @Test
    fun aPartialDocumentIsDeletedExactlyOnce() {
        val document = RecordingDeleteSafDocument()

        discardPartialDocument(document)

        assertEquals(1, document.deleteCalls)
    }

    @Test
    fun aMissingPartialDocumentIsIgnored() {
        // The failure can happen before the file was created (bad URL,
        // unavailable tree), so the cleanup must accept null.
        discardPartialDocument(null)
    }

    @Test
    fun aProviderThatThrowsOnDeleteCannotMaskTheOriginalFailure() {
        val original = DownloadFailureException(DownloadFailureKind.Permission, "grant revoked")
        val document = RecordingDeleteSafDocument(deleteError = SecurityException("provider crashed"))

        val result = failureAfterCleanup<String>(document, original)

        assertEquals("delete was still attempted", 1, document.deleteCalls)
        assertSame(original, result.exceptionOrNull())
    }

    @Test
    fun aPermissionFailureDeletesThePartialThenFallsBack() {
        // Revoked grant: the destination is unusable, so the partial document
        // in the custom tree is removed and the same download is enqueued into
        // the system Downloads directory instead.
        val document = RecordingDeleteSafDocument()
        val permission = DownloadFailureException(DownloadFailureKind.Permission)

        val result = failureAfterCleanup<String>(document, permission)

        assertEquals(1, document.deleteCalls)
        assertSame(permission, result.exceptionOrNull())
        assertTrue(shouldFallBackToSystemDownloads(result.exceptionOrNull()!!))
    }

    @Test
    fun aDeletedTreeFallsBackWithoutAPartialDocument() {
        // The unavailable-tree check runs before a file exists, so there is
        // nothing to clean up; the destination change still happens.
        val unavailable = DownloadDirectoryUnavailableException("tree deleted")

        val result = failureAfterCleanup<String>(null, unavailable)

        assertSame(unavailable, result.exceptionOrNull())
        assertTrue(shouldFallBackToSystemDownloads(result.exceptionOrNull()!!))
    }

    @Test
    fun spaceAndProviderFailuresDeleteThePartialWithoutChangingDestination() {
        for (kind in listOf(DownloadFailureKind.NoSpace, DownloadFailureKind.Provider)) {
            val document = RecordingDeleteSafDocument()
            val error = DownloadFailureException(kind, "write failed")

            val result = failureAfterCleanup<String>(document, error)

            assertEquals("$kind: partial not deleted", 1, document.deleteCalls)
            assertSame(error, result.exceptionOrNull())
            assertFalse(
                "$kind must be reported where the user chose to save",
                shouldFallBackToSystemDownloads(result.exceptionOrNull()!!),
            )
        }
    }

    @Test
    fun aNetworkFailureKeepsItsErrorMessage() {
        val document = RecordingDeleteSafDocument()
        val error = DownloadFailureException(DownloadFailureKind.Network, "connection reset")

        val result = failureAfterCleanup<String>(document, error)

        assertEquals(1, document.deleteCalls)
        assertEquals("connection reset", result.exceptionOrNull()!!.message)
        assertFalse(shouldFallBackToSystemDownloads(result.exceptionOrNull()!!))
    }

    @Test
    fun anUnclassifiedFailureStillCleansUpAndStaysReadable() {
        // For example a provider that throws a generic IOException; the
        // downloader wraps read failures with the network message before
        // reaching this point, but the cleanup rule is the same.
        val document = RecordingDeleteSafDocument()
        val error = IllegalStateException("下载目录不可用")

        val result = failureAfterCleanup<String>(document, error)

        assertEquals(1, document.deleteCalls)
        assertEquals("下载目录不可用", result.exceptionOrNull()!!.message)
        assertFalse(shouldFallBackToSystemDownloads(result.exceptionOrNull()!!))
    }
}

/**
 * In-memory [SafDocument] for the cleanup rules. Only [delete] is exercised;
 * the destination-preparation methods are unused here.
 */
private class RecordingDeleteSafDocument(
    private val deleteError: Exception? = null,
) : SafDocument {
    var deleteCalls = 0
        private set

    override val name: String? = "book.txt"
    override val isDirectory: Boolean = false
    override val exists: Boolean = true

    override fun findDirectory(displayName: String): SafDocument? = null

    override fun createDirectory(displayName: String): SafDocument? = null

    override fun listChildNames(): List<String> = emptyList()

    override fun createFile(mimeType: String, displayName: String): SafDocument? = null

    override fun delete(): Boolean {
        deleteCalls++
        deleteError?.let { throw it }
        return true
    }
}