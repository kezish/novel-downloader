package com.lingyi.noveldl.data

import com.lingyi.noveldl.ui.history.shouldFallBackToSystemDownloads
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.FileNotFoundException
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.CancellationException

/**
 * A SAF write grant that disappears while the file is being written.
 *
 * The grant can be revoked, the provider process can die, or the user can
 * delete the document/folder from another app. Each case has to end the same
 * way: no truncated file is left in `NovelDL/`, the failure is classified so
 * the UI can name it, and only a *permission* loss changes the destination.
 *
 * Everything here is a plain JVM test: the streaming loop takes byte streams
 * and the destination rules take [SafDocument], so no ContentResolver, file
 * system or SAF grant is needed.
 */
class SafRevocationTest {

    private val unavailable = "directory unavailable"
    private val createFailed = "could not create"

    // ------------------------------------------------------------------
    // Mid-write failures: cleanup + classification.
    // ------------------------------------------------------------------

    @Test
    fun aGrantRevokedMidWriteDeletesThePartialAndIsAPermissionFailure() {
        val document = StubSafDocument()
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(8 * 1024)),
                output = FailingWriteOutputStream(0, SecurityException("grant revoked")),
                totalBytes = 8L * 1024,
                onProgress = {},
                throttle = alwaysEmit(),
            )
        }

        val result = failureAfterCleanup<String>(document, failure)

        assertEquals(DownloadFailureKind.Permission, failure.kind)
        assertEquals("the half-written document was not removed", 1, document.deleteCalls)
        assertSame(failure, result.exceptionOrNull())
        assertTrue(
            "a revoked grant is the one class that may move to system Downloads",
            shouldFallBackToSystemDownloads(result.exceptionOrNull()!!),
        )
    }

    @Test
    fun aDocumentDeletedMidWriteIsAProviderFailureThatStaysInPlace() {
        val document = StubSafDocument()
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(4 * 1024)),
                output = FailingWriteOutputStream(
                    0,
                    FileNotFoundException("open failed: ENOENT (No such file or directory)"),
                ),
                totalBytes = 4L * 1024,
                onProgress = {},
                throttle = alwaysEmit(),
            )
        }

        val result = failureAfterCleanup<String>(document, failure)

        assertEquals(DownloadFailureKind.Provider, failure.kind)
        assertEquals(1, document.deleteCalls)
        assertFalse(
            "a deleted document does not make the chosen folder unusable",
            shouldFallBackToSystemDownloads(result.exceptionOrNull()!!),
        )
    }

    @Test
    fun aDisconnectedProviderIsAProviderFailureNotANetworkOne() {
        // A dead provider surfaces as an arbitrary RuntimeException from the
        // binder layer; it must never be reported as a network problem (which
        // would tell the user to check the connection).
        val errors = listOf(
            IllegalStateException("Binder transaction failed"),
            IOException("Connection to the document provider was lost"),
            RuntimeException("provider process died"),
        )

        for (error in errors) {
            val document = StubSafDocument()
            val failure = assertThrows(DownloadFailureException::class.java) {
                streamToDocumentAndClose(
                    input = ByteArrayInputStream(ByteArray(2 * 1024)),
                    output = FailingWriteOutputStream(0, error),
                    totalBytes = 2L * 1024,
                    onProgress = {},
                    throttle = alwaysEmit(),
                )
            }

            assertEquals(
                "wrong kind for ${error.message}",
                DownloadFailureKind.Provider,
                failure.kind,
            )
            failureAfterCleanup<String>(document, failure)
            assertEquals("${error.message}: partial not deleted", 1, document.deleteCalls)
            assertFalse(shouldFallBackToSystemDownloads(failure))
        }
    }

    @Test
    fun aSpaceFailureMidWriteKeepsItsClassificationAndDoesNotFallBack() {
        val document = StubSafDocument()
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(1024)),
                output = FailingWriteOutputStream(
                    0,
                    IOException("write failed: ENOSPC (No space left on device)"),
                ),
                totalBytes = 1024,
                onProgress = {},
                throttle = alwaysEmit(),
            )
        }

        failureAfterCleanup<String>(document, failure)

        assertEquals(DownloadFailureKind.NoSpace, failure.kind)
        assertEquals(1, document.deleteCalls)
        assertFalse(
            "moving the file would fail at the fallback destination too",
            shouldFallBackToSystemDownloads(failure),
        )
    }

    // ------------------------------------------------------------------
    // Close failures: providers flush at close, which is where a revoked
    // grant or a full volume is often noticed after a successful loop.
    // ------------------------------------------------------------------

    @Test
    fun aFailureWhileClosingIsClassifiedLikeAWriteFailure() {
        val cases = listOf(
            IOException("write failed: ENOSPC (No space left on device)") to
                DownloadFailureKind.NoSpace,
            SecurityException("grant revoked at close") to DownloadFailureKind.Permission,
            IOException("write failed: EACCES (Permission denied)") to
                DownloadFailureKind.Permission,
            IOException("write failed: EIO (I/O error)") to DownloadFailureKind.Provider,
        )

        for ((closeError, expected) in cases) {
            val document = StubSafDocument()
            val failure = assertThrows(DownloadFailureException::class.java) {
                streamToDocumentAndClose(
                    input = ByteArrayInputStream(ByteArray(512)),
                    output = FailingCloseOutputStream(closeError),
                    totalBytes = 512,
                    onProgress = {},
                    throttle = alwaysEmit(),
                )
            }

            assertEquals("wrong kind for ${closeError.message}", expected, failure.kind)
            failureAfterCleanup<String>(document, failure)
            assertEquals("${closeError.message}: partial not deleted", 1, document.deleteCalls)
            assertEquals(
                "only a permission loss may change the destination",
                expected == DownloadFailureKind.Permission,
                shouldFallBackToSystemDownloads(failure),
            )
        }
    }

    @Test
    fun aCloseFailureNeverMasksTheWriteFailureThatStoppedTheTransfer() {
        val document = StubSafDocument()
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(1024)),
                output = object : OutputStream() {
                    override fun write(b: Int) {
                        throw IOException("write failed: ENOSPC (No space left on device)")
                    }

                    override fun write(b: ByteArray, off: Int, len: Int) {
                        throw IOException("write failed: ENOSPC (No space left on device)")
                    }

                    override fun close() {
                        throw SecurityException("close failed after the volume filled up")
                    }
                },
                totalBytes = 1024,
                onProgress = {},
                throttle = alwaysEmit(),
            )
        }

        failureAfterCleanup<String>(document, failure)

        assertEquals(
            "the first failure names the real cause",
            DownloadFailureKind.NoSpace,
            failure.kind,
        )
        assertEquals(1, document.deleteCalls)
    }

    // ------------------------------------------------------------------
    // Progress: a failed attempt must not keep reporting.
    // ------------------------------------------------------------------

    @Test
    fun aWriteFailureStopsProgressAtTheLastCompleteChunk() {
        val output = FailingWriteOutputStream(
            failAfterChunks = 1,
            error = IOException("write failed: EIO"),
        )
        val samples = mutableListOf<DownloadProgress>()

        assertThrows(DownloadFailureException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(3 * 8 * 1024)),
                output = output,
                totalBytes = 3L * 8 * 1024,
                onProgress = samples::add,
                throttle = alwaysEmit(),
            )
        }

        // Nothing arrives after the failure, and the bar never claims 100 %
        // for a file the caller is about to delete.
        assertEquals(
            "last sample must match the bytes the provider actually accepted",
            output.acceptedBytes,
            samples.last().bytesWritten,
        )
        assertTrue(output.acceptedBytes < 3L * 8 * 1024)
        assertTrue(
            "no ghost sample after the failure: $samples",
            samples.none { it.bytesWritten >= 3L * 8 * 1024 },
        )
        assertEquals("progress must not regress", samples.sortedBy { it.bytesWritten }, samples)
    }

    @Test
    fun cancellationBeforeCompletionDoesNotEmitAFinalSample() {
        val samples = mutableListOf<DownloadProgress>()
        val cancellation = CancellationException("navigated away")
        var checks = 0

        val thrown = assertThrows(CancellationException::class.java) {
            streamToDocumentAndClose(
                input = ByteArrayInputStream(ByteArray(4)),
                output = ByteArrayOutputStream(),
                totalBytes = 4,
                onProgress = samples::add,
                // 1: before the first chunk, 2: before the EOF read,
                // 3: after the loop / before the final 100 % sample.
                checkActive = {
                    checks++
                    if (checks == 3) throw cancellation
                },
                throttle = alwaysEmit(),
            )
        }

        assertSame(cancellation, thrown)
        // The 0-byte start plus the one chunk that was actually written. A
        // completed transfer would have appended the post-flush final sample
        // (a second 4-byte 100 % reading); the cancelled one must not.
        assertEquals("a cancelled download must not flash 100 %: $samples", 2, samples.size)
        assertEquals(4L, samples.last().bytesWritten)
    }

    // ------------------------------------------------------------------
    // Documents and folders deleted from another app: rebuild, don't fail.
    // ------------------------------------------------------------------

    @Test
    fun aSubdirectoryDeletedByTheUserIsRebuiltInsteadOfReused() {
        val staleDirectory = StubSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            exists = false
        }
        val recreated = StubSafDocument(name = DOWNLOAD_SUBDIRECTORY)
        val createdFile = StubSafDocument(name = "book.txt", isDirectory = false)
        recreated.createFileResult = createdFile
        val tree = StubSafDocument(name = "Books").apply {
            findDirectoryResult = staleDirectory
            createDirectoryResult = recreated
        }

        val prepared = prepareDownloadDestination(
            tree = tree,
            desiredName = "book.txt",
            mimeType = "text/plain",
            unavailableMessage = unavailable,
            createFailedMessage = createFailed,
        )

        assertEquals(listOf(DOWNLOAD_SUBDIRECTORY), tree.createdDirectories)
        assertSame(createdFile, prepared)
        assertEquals(listOf("text/plain" to "book.txt"), recreated.createdFiles)
        assertTrue(
            "a vanished folder must not be written through",
            staleDirectory.createdFiles.isEmpty(),
        )
    }

    @Test
    fun aDocumentDeletedByTheUserIsRecreatedOnTheNextAttempt() {
        val directory = StubSafDocument(name = DOWNLOAD_SUBDIRECTORY)
        val vanished = StubSafDocument(name = "book.txt", isDirectory = false).apply {
            // The provider returns false when the user already deleted it.
            deleteResult = false
        }
        val tree = StubSafDocument(name = "Books").apply {
            findDirectoryResult = directory
        }

        // Cleanup of a document that no longer exists must not throw...
        discardPartialDocument(vanished)
        assertEquals(1, vanished.deleteCalls)

        // ...and the children list no longer contains it, so the retry
        // creates a fresh document under the original name.
        val recreated = StubSafDocument(name = "book.txt", isDirectory = false)
        directory.createFileResult = recreated
        val prepared = prepareDownloadDestination(
            tree = tree,
            desiredName = "book.txt",
            mimeType = "text/plain",
            unavailableMessage = unavailable,
            createFailedMessage = createFailed,
        )

        assertSame(recreated, prepared)
        assertNotSame(vanished, prepared)
        assertEquals(listOf("text/plain" to "book.txt"), directory.createdFiles)
    }

    @Test
    fun aTreeThatVanishesBeforeTheWriteFallsBackInsteadOfCrashing() {
        val failure = assertThrows(DownloadDirectoryUnavailableException::class.java) {
            prepareDownloadDestination(
                tree = null,
                desiredName = "book.txt",
                mimeType = "text/plain",
                unavailableMessage = unavailable,
                createFailedMessage = createFailed,
            )
        }

        val result = failureAfterCleanup<String>(null, failure)

        assertEquals(unavailable, failure.message)
        assertSame(failure, result.exceptionOrNull())
        assertTrue(shouldFallBackToSystemDownloads(result.exceptionOrNull()!!))
    }

    // ------------------------------------------------------------------
    // Re-granting: a new tree replaces the revoked one for the retry.
    // ------------------------------------------------------------------

    @Test
    fun reGrantingANewTreeMakesTheRetryUseIt() {
        val revokedTree =
            "content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FOldBooks"
        val newTree =
            "content://com.android.externalstorage.documents/tree/primary%3ADownload%2FBooks"

        // Before the user re-picks: the cached URI is stale, so the next
        // download goes to system Downloads and the settings warning is due.
        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget(revokedTree, emptySet()))
        assertTrue(customTreeUnavailable(revokedTree, emptySet()))
        assertFalse(hasPersistedWriteAccess(newTree, emptySet()))

        // After re-picking: the new grant is the one that is used, without
        // reviving the revoked URI.
        assertEquals(
            DownloadTarget.CustomTree(newTree),
            resolveDownloadTarget(newTree, setOf(newTree)),
        )
        assertFalse(customTreeUnavailable(newTree, setOf(newTree)))

        val directory = StubSafDocument(name = DOWNLOAD_SUBDIRECTORY)
        val createdFile = StubSafDocument(name = "book.txt", isDirectory = false)
        directory.createFileResult = createdFile
        val retriedTree = StubSafDocument(name = "Books").apply {
            findDirectoryResult = directory
        }

        val prepared = prepareDownloadDestination(
            tree = retriedTree,
            desiredName = "book.txt",
            mimeType = "text/plain",
            unavailableMessage = unavailable,
            createFailedMessage = createFailed,
        )

        assertSame(createdFile, prepared)
        assertEquals(listOf("text/plain" to "book.txt"), directory.createdFiles)
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    private fun alwaysEmit() = DownloadProgressThrottle(minIntervalNanos = 0, minBytes = 0)
}

/**
 * Output stream that accepts a number of chunks and then fails, standing in
 * for a provider whose write fails mid-transfer. [acceptedBytes] is what the
 * user would find in the truncated document.
 */
private class FailingWriteOutputStream(
    private val failAfterChunks: Int,
    private val error: Exception,
) : OutputStream() {
    private var chunks = 0

    var acceptedBytes = 0L
        private set

    override fun write(b: Int) {
        write(byteArrayOf(b.toByte()), 0, 1)
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
        if (chunks >= failAfterChunks) throw error
        chunks++
        acceptedBytes += len
    }
}

/** Output stream whose writes succeed but whose close (flush) fails. */
private class FailingCloseOutputStream(
    private val closeError: Exception,
) : ByteArrayOutputStream() {
    override fun close() {
        throw closeError
    }
}

/**
 * Minimal [SafDocument] for the revocation rules. The destination methods
 * record what was asked of them; unset results behave like a provider that
 * cannot create anything.
 */
private class StubSafDocument(
    override val name: String? = null,
    override val isDirectory: Boolean = true,
) : SafDocument {
    override var exists: Boolean = true

    var findDirectoryResult: SafDocument? = null
    var createDirectoryResult: SafDocument? = null
    var createFileResult: SafDocument? = null
    var deleteResult: Boolean = true

    val lookedUpDirectories = mutableListOf<String>()
    val createdDirectories = mutableListOf<String>()
    val createdFiles = mutableListOf<Pair<String, String>>()

    var deleteCalls = 0
        private set

    override fun findDirectory(displayName: String): SafDocument? {
        lookedUpDirectories += displayName
        return findDirectoryResult
    }

    override fun createDirectory(displayName: String): SafDocument? {
        createdDirectories += displayName
        return createDirectoryResult
    }

    override fun listChildNames(): List<String> = emptyList()

    override fun createFile(mimeType: String, displayName: String): SafDocument? {
        createdFiles += mimeType to displayName
        return createFileResult
    }

    override fun delete(): Boolean {
        deleteCalls++
        return deleteResult
    }
}
