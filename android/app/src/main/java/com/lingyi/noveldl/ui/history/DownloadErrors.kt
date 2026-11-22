package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import androidx.annotation.StringRes
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.DownloadDirectoryUnavailableException
import com.lingyi.noveldl.data.DownloadFailureException
import com.lingyi.noveldl.data.DownloadFailureKind

/**
 * Wording plus cause kind for one `DownloadManager.COLUMN_REASON` value.
 *
 * Every mapped reason has a message; [kind] is non-null only for causes that
 * also exist in the SAF vocabulary ([DownloadFailureKind]), so the two download
 * paths classify the same problem the same way. [DownloadManager.ERROR_UNKNOWN]
 * has no specific cause and therefore no kind.
 */
private data class DownloadManagerFailure(
    @StringRes val message: Int,
    val kind: DownloadFailureKind?,
)

/**
 * Single source of truth for the `DownloadManager.ERROR_*` table: a code must
 * never have wording without a classification (or the other way round).
 */
private fun downloadManagerFailure(reason: Int): DownloadManagerFailure? = when (reason) {
    DownloadManager.ERROR_INSUFFICIENT_SPACE -> DownloadManagerFailure(
        R.string.download_error_no_space,
        DownloadFailureKind.NoSpace,
    )
    DownloadManager.ERROR_FILE_ERROR -> DownloadManagerFailure(
        R.string.download_error_file,
        DownloadFailureKind.Provider,
    )
    DownloadManager.ERROR_FILE_ALREADY_EXISTS -> DownloadManagerFailure(
        R.string.download_error_file_exists,
        DownloadFailureKind.Provider,
    )
    DownloadManager.ERROR_DEVICE_NOT_FOUND -> DownloadManagerFailure(
        R.string.download_error_device,
        DownloadFailureKind.Provider,
    )
    DownloadManager.ERROR_HTTP_DATA_ERROR -> DownloadManagerFailure(
        R.string.download_error_http_data,
        DownloadFailureKind.Network,
    )
    DownloadManager.ERROR_UNHANDLED_HTTP_CODE -> DownloadManagerFailure(
        R.string.download_error_http_code,
        DownloadFailureKind.Network,
    )
    DownloadManager.ERROR_TOO_MANY_REDIRECTS -> DownloadManagerFailure(
        R.string.download_error_redirects,
        DownloadFailureKind.Network,
    )
    DownloadManager.ERROR_CANNOT_RESUME -> DownloadManagerFailure(
        R.string.download_error_cannot_resume,
        DownloadFailureKind.Network,
    )
    DownloadManager.ERROR_UNKNOWN -> DownloadManagerFailure(
        R.string.download_error_unknown,
        null,
    )
    else -> null
}

/**
 * Maps a `DownloadManager` failure reason (`COLUMN_REASON` of a
 * `STATUS_FAILED` download) to a user-facing string.
 *
 * The raw code ("错误码 1006") tells the user nothing; the common causes are
 * worth naming, especially "no space" and "cannot write the file" because they
 * are the ones a custom Downloads directory produces. Unknown codes keep the
 * numeric fallback in the caller.
 *
 * `DownloadManager.ERROR_*` are compile-time int constants, so this stays
 * plain JVM-testable.
 */
@StringRes
internal fun downloadFailureReasonRes(reason: Int): Int? =
    downloadManagerFailure(reason)?.message

/**
 * The cause behind a `DownloadManager` failure reason, using the same
 * vocabulary as SAF write failures. Null means the code has no specific cause
 * (or is unknown), and callers should keep the numeric fallback.
 */
internal fun downloadFailureKind(reason: Int): DownloadFailureKind? =
    downloadManagerFailure(reason)?.kind

/**
 * Maps a classified SAF download failure to the reason shown inside
 * "下载失败：file（reason）".
 *
 * Space, permission and provider failures each get wording that names the fix;
 * a network failure reuses the existing message. Kept beside
 * [downloadFailureReasonRes] so both download paths explain causes the same
 * way, and JVM-testable because it only returns resource ids.
 */
@StringRes
internal fun downloadFailureMessageRes(kind: DownloadFailureKind): Int = when (kind) {
    DownloadFailureKind.Network -> R.string.history_download_network_error
    DownloadFailureKind.NoSpace -> R.string.download_error_no_space
    DownloadFailureKind.Permission -> R.string.download_error_permission
    DownloadFailureKind.Provider -> R.string.download_error_provider
}

/**
 * Whether a failed custom-directory download may be retried into the system
 * Downloads directory.
 *
 * Only a missing/deleted tree ([DownloadDirectoryUnavailableException]) or a
 * denied write (a raw [SecurityException] from the provider, or a classified
 * [DownloadFailureKind.Permission]) can succeed there: the destination
 * changed, not the file. A full volume, a broken provider, a network/HTTP
 * failure or a cancellation would fail at the fallback destination too, and
 * silently moving the file would hide the real cause, so those are reported
 * instead.
 */
internal fun shouldFallBackToSystemDownloads(error: Throwable): Boolean = when (error) {
    is DownloadDirectoryUnavailableException -> true
    // A revoked grant can surface as a SecurityException from the provider
    // query or a createFile() call before any document exists; it is the same
    // "this directory cannot be written" case as a classified Permission
    // failure and must fall back too.
    is SecurityException -> true
    is DownloadFailureException -> error.kind == DownloadFailureKind.Permission
    else -> false
}

/**
 * The reason shown in "下载失败：file（reason）". A null/blank message (some
 * providers throw exceptions without one) becomes [unknown] instead of an
 * empty pair of parentheses, so a failed fallback still explains itself.
 */
internal fun downloadFailureReasonText(reason: String?, unknown: String): String =
    reason?.takeIf { it.isNotBlank() } ?: unknown

/** How the completion broadcast's status is reported to the user. */
internal enum class DownloadCompletionState {
    Success,
    Failed,

    /** Pending/running/paused (or an unreadable status); not a failure. */
    Ignored,
}

/**
 * Classifies a `DownloadManager.COLUMN_STATUS`. A paused download can still be
 * resumed from the notification and a running one is mid-transfer, so only
 * [DownloadManager.STATUS_FAILED] counts as a failure.
 */
internal fun downloadCompletionState(status: Int): DownloadCompletionState = when (status) {
    DownloadManager.STATUS_SUCCESSFUL -> DownloadCompletionState.Success
    DownloadManager.STATUS_FAILED -> DownloadCompletionState.Failed
    else -> DownloadCompletionState.Ignored
}
