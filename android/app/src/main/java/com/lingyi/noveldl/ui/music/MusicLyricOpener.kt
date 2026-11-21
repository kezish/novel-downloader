package com.lingyi.noveldl.ui.music

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.lingyi.noveldl.data.MusicLocalPaths
import java.io.File

/** What [MusicLyricOpener.open] managed to do, mapped to a message by the UI. */
enum class MusicLyricOpenResult {
    /** A viewer was found and the intent was started. */
    Opened,

    /** The `.lrc` disappeared, or it is not reachable through the provider. */
    Missing,

    /** No installed app handles `.lrc` / plain text. */
    NoApp,
}

/**
 * Opens a downloaded `.lrc` in whichever text viewer the user has installed.
 *
 * The file lives in the app's private storage, so it is handed out through
 * the existing [FileProvider] (`${applicationId}.fileprovider`); the URI grant
 * is scoped to this one file. `.lrc` has no registered MIME type on Android,
 * so `text/x-lyric` is tried first (players that know lyrics) and `text/plain`
 * second (any text editor); [MusicLyricOpenResult.NoApp] is returned only when
 * neither resolves, instead of failing silently.
 *
 * The path is re-validated here rather than trusting the caller: the URI is
 * only produced for a `.lrc` inside the single directory declared in
 * `res/xml/file_paths.xml`, so a stale or engine-supplied path outside the
 * provider range can never be exposed.
 */
object MusicLyricOpener {

    /** Common MIME type for LRC files, tried before plain text. */
    const val MIME_LYRIC: String = "text/x-lyric"

    /** Fallback MIME type understood by every text viewer. */
    const val MIME_PLAIN: String = "text/plain"

    /** MIME types in dispatch order; the first one a viewer handles wins. */
    val MIME_TYPES: List<String> = listOf(MIME_LYRIC, MIME_PLAIN)

    /**
     * Flags of the view intent: a read grant for the one content URI plus
     * [Intent.FLAG_ACTIVITY_NEW_TASK]. There is deliberately no write grant.
     */
    val VIEW_FLAGS: Int =
        Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK

    /** Authority of the app's [FileProvider] (`<applicationId>.fileprovider`). */
    fun providerAuthority(packageName: String): String = "$packageName.fileprovider"

    /**
     * Whether [lyricPath] may be handed to the provider for [providerRoot]:
     * a non-blank `.lrc` (suffix compared case-insensitively) inside the
     * provider range declared in `res/xml/file_paths.xml`.
     *
     * Kept Android-free so the guard can be exercised without a device; the
     * existence and FileProvider checks stay in [open].
     */
    fun isOpenableLyric(providerRoot: String, lyricPath: String): Boolean {
        val path = lyricPath.trim()
        return path.isNotEmpty() &&
            path.endsWith(MusicLyrics.EXTENSION, ignoreCase = true) &&
            MusicLocalPaths.isInside(providerRoot, path)
    }

    fun open(context: Context, lyricPath: String): MusicLyricOpenResult {
        // Narrower than the app-wide sandbox roots: the check is exactly the
        // range file_paths.xml exposes, so passing it guarantees the
        // getUriForFile below succeeds for an existing file. The provider
        // range also holds the audio files; only `.lrc` sidecars may be
        // handed out.
        if (!isOpenableLyric(MusicLyrics.providerRoot(context), lyricPath)) {
            return MusicLyricOpenResult.Missing
        }
        val file = File(lyricPath.trim())
        if (!file.isFile) return MusicLyricOpenResult.Missing

        // getUriForFile throws when the file is outside file_paths.xml; a
        // lyric that cannot be exposed is indistinguishable from a missing
        // one for the user, and reporting "no app" would be wrong.
        val uri = runCatching {
            FileProvider.getUriForFile(
                context,
                providerAuthority(context.packageName),
                file,
            )
        }.getOrNull() ?: return MusicLyricOpenResult.Missing

        MIME_TYPES.forEach { mime ->
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, mime)
                // Some viewers only look at the clip data even though the URI
                // is also the intent data.
                clipData = ClipData.newRawUri(null, uri)
                addFlags(VIEW_FLAGS)
            }
            try {
                context.startActivity(intent)
                return MusicLyricOpenResult.Opened
            } catch (_: ActivityNotFoundException) {
                // Try the next MIME type; some devices register only one.
            } catch (_: SecurityException) {
                // The grant was rejected; another viewer is unlikely to help.
                return MusicLyricOpenResult.NoApp
            }
        }
        return MusicLyricOpenResult.NoApp
    }
}
