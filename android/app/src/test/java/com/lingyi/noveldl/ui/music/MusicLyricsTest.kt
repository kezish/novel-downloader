package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicLyricFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.file.Files

/**
 * Path rules for the lyric files the music engine writes beside an audio
 * download: how the sibling names are derived (original `.lrc` and translated
 * `.zh.lrc`), when they may be offered to the user, and how the batch summary
 * counts them.
 */
class MusicLyricsTest {

    @get:Rule
    val temp = TemporaryFolder()

    private val root = "/data/user/0/com.lingyi.noveldl/files/engine_home/downloads/music"

    private fun existing(vararg paths: String): (String) -> Boolean {
        val files = paths.toSet()
        return { it in files }
    }

    // ---------- path derivation ----------

    @Test
    fun lyricPathReplacesTheAudioExtension() {
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.mp3"),
        )
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.flac"),
        )
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.m4a"),
        )
    }

    @Test
    fun lyricPathAppendsWithoutAnExtensionAndKeepsWindowsSeparators() {
        assertEquals("$root/song.lrc", MusicLyrics.lyricPathFor("$root/song"))
        assertEquals("C:\\Music\\song.lrc", MusicLyrics.lyricPathFor("C:\\Music\\song.mp3"))
    }

    @Test
    fun lyricPathIgnoresDotsInParentDirectories() {
        assertEquals(
            "/data/a.b/song.lrc",
            MusicLyrics.lyricPathFor("/data/a.b/song.mp3"),
        )
    }

    @Test
    fun lyricPathIsEmptyForBlankInput() {
        assertEquals("", MusicLyrics.lyricPathFor(""))
        assertEquals("", MusicLyrics.lyricPathFor("   "))
        assertEquals("", MusicLyrics.lyricPathFor("$root/"))
    }

    @Test
    fun translationLyricPathAddsZhBeforeTheLrcExtension() {
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song.mp3"),
        )
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song.flac"),
        )
        // No audio extension: both siblings use the same stem.
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song"),
        )
        assertEquals(
            "C:\\Music\\song.zh.lrc",
            MusicLyrics.translationLyricPathFor("C:\\Music\\song.mp3"),
        )
    }

    @Test
    fun translationLyricPathIsEmptyForBlankInput() {
        assertEquals("", MusicLyrics.translationLyricPathFor(""))
        assertEquals("", MusicLyrics.translationLyricPathFor("   "))
        assertEquals("", MusicLyrics.translationLyricPathFor("$root/"))
    }

    // ---------- existence / containment ----------

    @Test
    fun findReturnsTheLyricWhenItExistsInsideThePrivateRoot() {
        val audio = "$root/song.mp3"
        val lyric = "$root/song.lrc"

        assertEquals(
            lyric,
            MusicLyrics.find(audio, listOf(root), existing(lyric)),
        )
    }

    @Test
    fun findReturnsNullWhenTheLyricWasNotSaved() {
        assertNull(
            MusicLyrics.find("$root/song.mp3", listOf(root), existing()),
        )
    }

    @Test
    fun findRejectsLyricsOutsideTheAppPrivateDirectories() {
        val audio = "/storage/emulated/0/Music/song.mp3"
        val lyric = "/storage/emulated/0/Music/song.lrc"

        // The file "exists", but the sandbox check runs first: it cannot be
        // reached through the FileProvider and must not become a button.
        assertNull(MusicLyrics.find(audio, listOf(root), existing(lyric)))
    }

    @Test
    fun findRejectsTraversalOutOfThePrivateRoot() {
        val audio = "$root/../secret/song.mp3"
        val lyric = "$root/../secret/song.lrc"

        assertNull(MusicLyrics.find(audio, listOf(root), existing(lyric)))
    }

    @Test
    fun findTreatsAThrowingProbeAsNoLyric() {
        val audio = "$root/song.mp3"

        assertNull(
            MusicLyrics.find(audio, listOf(root)) { throw IllegalStateException("gone") },
        )
    }

    @Test
    fun findIsEmptyForBlankAudioPaths() {
        assertNull(MusicLyrics.find("", listOf(root), existing("$root/.lrc")))
    }

    // ---------- original + translation ----------

    @Test
    fun findAllReturnsOriginalThenTranslation() {
        val audio = "$root/song.mp3"
        val original = "$root/song.lrc"
        val translation = "$root/song.zh.lrc"

        assertEquals(
            listOf(
                MusicLyricFile(MusicLyrics.LANG_ORIGINAL, original),
                MusicLyricFile(MusicLyrics.LANG_TRANSLATION, translation),
            ),
            MusicLyrics.findAll(audio, listOf(root), existing(original, translation)),
        )
    }

    @Test
    fun findAllKeepsOneButtonWhenOnlyTheOriginalWasSaved() {
        val audio = "$root/song.mp3"

        assertEquals(
            listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.lrc")),
            MusicLyrics.findAll(audio, listOf(root), existing("$root/song.lrc")),
        )
    }

    @Test
    fun findAllKeepsATranslationThatWasSavedWithoutTheOriginal() {
        // The engine normally writes the translation into `<stem>.lrc` when
        // there is no original, but a provider that only supplies `tlyric`
        // can leave just the `.zh.lrc`.
        assertEquals(
            listOf(MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/song.zh.lrc")),
            MusicLyrics.findAll("$root/song.mp3", listOf(root), existing("$root/song.zh.lrc")),
        )
    }

    @Test
    fun findAllIsEmptyWhenNeitherFileExists() {
        assertTrue(
            MusicLyrics.findAll("$root/song.mp3", listOf(root), existing()).isEmpty(),
        )
    }

    @Test
    fun findAllRejectsLyricsOutsideTheAppPrivateDirectories() {
        val audio = "/storage/emulated/0/Music/song.mp3"
        val files = existing(
            "/storage/emulated/0/Music/song.lrc",
            "/storage/emulated/0/Music/song.zh.lrc",
        )

        assertTrue(MusicLyrics.findAll(audio, listOf(root), files).isEmpty())
    }

    @Test
    fun findAllRejectsTraversalOutOfThePrivateRoot() {
        val audio = "$root/../secret/song.mp3"
        val files = existing("$root/../secret/song.lrc", "$root/../secret/song.zh.lrc")

        assertTrue(MusicLyrics.findAll(audio, listOf(root), files).isEmpty())
    }

    @Test
    fun findAllTreatsAThrowingProbeAsNoLyrics() {
        assertTrue(
            MusicLyrics.findAll("$root/song.mp3", listOf(root)) {
                throw IllegalStateException("gone")
            }.isEmpty(),
        )
    }

    @Test
    fun findAllIsEmptyForBlankAudioPaths() {
        assertTrue(MusicLyrics.findAll("", listOf(root), existing("$root/.lrc")).isEmpty())
        assertTrue(MusicLyrics.findAll("$root/", listOf(root), existing("$root/.lrc")).isEmpty())
    }

    // ---------- summary ----------

    @Test
    fun savedCountCountsOnlyResultsWithAVerifiedLyric() {
        val results = listOf(
            MusicJobResult(status = "ok", lyricPath = "$root/a.lrc"),
            MusicJobResult(status = "ok"),
            MusicJobResult(status = "skip", lyricPath = "$root/b.lrc"),
            MusicJobResult(status = "fail"),
        )

        assertEquals(2, MusicLyrics.savedCount(results))
    }

    @Test
    fun savedCountIsZeroWithoutLyrics() {
        assertEquals(0, MusicLyrics.savedCount(emptyList()))
        assertEquals(
            0,
            MusicLyrics.savedCount(listOf(MusicJobResult(status = "ok"))),
        )
    }

    @Test
    fun savedCountCountsATrackWithBothLyricsOnce() {
        val results = listOf(
            MusicJobResult(
                status = "ok",
                lyricPath = "$root/a.lrc",
                lyricPaths = listOf(
                    MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/a.lrc"),
                    MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/a.zh.lrc"),
                ),
            ),
            MusicJobResult(
                status = "skip",
                lyricPaths = listOf(
                    MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/b.zh.lrc"),
                ),
            ),
            MusicJobResult(status = "ok"),
        )

        assertEquals(2, MusicLyrics.savedCount(results))
    }

    // ---------- button count and labels ----------

    @Test
    fun aSingleOriginalRendersTheHistoricalGenericLabel() {
        val buttons = MusicLyrics.buttons(
            listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.lrc")),
            root,
        )

        assertEquals(1, buttons.size)
        assertEquals(MusicLyrics.Label.Generic, buttons.single().label)
        assertEquals("$root/song.lrc", buttons.single().path)
    }

    @Test
    fun aSingleTranslationNamesItsSlot() {
        val buttons = MusicLyrics.buttons(
            listOf(MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/song.zh.lrc")),
            root,
        )

        assertEquals(1, buttons.size)
        assertEquals(MusicLyrics.Label.Translation, buttons.single().label)
    }

    @Test
    fun originalAndTranslationRenderTwoLabelledButtonsInOrder() {
        val buttons = MusicLyrics.buttons(
            listOf(
                MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.lrc"),
                MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/song.zh.lrc"),
            ),
            root,
        )

        assertEquals(
            listOf(MusicLyrics.Label.Original, MusicLyrics.Label.Translation),
            buttons.map { it.label },
        )
        assertEquals(
            listOf("$root/song.lrc", "$root/song.zh.lrc"),
            buttons.map { it.path },
        )
    }

    @Test
    fun anUnknownSlotFallsBackToTheGenericLabel() {
        val single = MusicLyrics.buttons(
            listOf(MusicLyricFile("kana", "$root/song.lrc")),
            root,
        )
        assertEquals(MusicLyrics.Label.Generic, single.single().label)

        val pair = MusicLyrics.buttons(
            listOf(
                MusicLyricFile("kana", "$root/song.lrc"),
                MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/song.zh.lrc"),
            ),
            root,
        )
        assertEquals(
            listOf(MusicLyrics.Label.Generic, MusicLyrics.Label.Translation),
            pair.map { it.label },
        )
    }

    @Test
    fun anEmptyListRendersNoButton() {
        assertTrue(MusicLyrics.buttons(emptyList(), root).isEmpty())
    }

    // ---------- the display guard (last line before a button) ----------

    @Test
    fun aWireSuppliedPathOutsideThePrivateRootNeverBecomesAButton() {
        // A remote server may echo `lyric_paths`; the app only ever exposes
        // files it verified as inside the provider range.
        val files = listOf(
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "/storage/emulated/0/Music/song.lrc"),
            MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "/etc/passwd.lrc"),
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "../../song.lrc"),
        )

        assertTrue(MusicLyrics.buttons(files, root).isEmpty())
    }

    @Test
    fun theDisplayGuardDropsBlankAndNonLrcEntries() {
        val files = listOf(
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, ""),
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "   "),
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.mp3"),
            MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.lrc"),
        )

        assertEquals(
            listOf("$root/song.lrc"),
            MusicLyrics.buttons(files, root).map { it.path },
        )
    }

    @Test
    fun theDisplayGuardComparesTheSuffixCaseInsensitivelyAndTrims() {
        val buttons = MusicLyrics.buttons(
            listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "  $root/SONG.LRC  ")),
            root,
        )

        assertEquals(1, buttons.size)
        assertEquals("$root/SONG.LRC", buttons.single().path)
    }

    @Test
    fun theDisplayGuardRejectsACaseVariantOfTheProviderRoot() {
        val variant = "$root".uppercase() + "/song.lrc"

        assertTrue(
            MusicLyrics.buttons(
                listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, variant)),
                root,
            ).isEmpty(),
        )
    }

    // ---------- real filesystem cases ----------

    @Test
    fun thePrivateLookupFindsARealFileInsideTheRoot() {
        val rootDir = temp.newFolder("music").canonicalFile
        val audio = File(rootDir, "song.mp3").apply { writeText("audio") }
        val lyric = File(rootDir, "song.lrc").apply { writeText("[00:00]hi") }

        val found = PrivateMusicLyricLookup(listOf(rootDir.path)).find(audio.path)

        assertEquals(
            listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, lyric.path)),
            found,
        )
    }

    @Test
    fun aDirectoryNamedLikeALyricIsNotAButton() {
        val rootDir = temp.newFolder("music").canonicalFile
        File(rootDir, "song.lrc").mkdirs()
        val audio = File(rootDir, "song.mp3").apply { writeText("audio") }

        assertTrue(PrivateMusicLyricLookup(listOf(rootDir.path)).find(audio.path).isEmpty())
    }

    @Test
    fun aMissingLyricFileIsNotAButton() {
        val rootDir = temp.newFolder("music").canonicalFile
        val audio = File(rootDir, "song.mp3").apply { writeText("audio") }

        assertTrue(PrivateMusicLyricLookup(listOf(rootDir.path)).find(audio.path).isEmpty())
    }

    @Test
    fun aSymlinkWhoseTargetIsOutsideTheRootIsNotAButton() {
        val rootDir = temp.newFolder("music").canonicalFile
        val audio = File(rootDir, "song.mp3").apply { writeText("audio") }
        File(rootDir, "song.lrc").writeText("[00:00]hi")

        // Simulate the canonical resolution of `song.lrc -> ../secret.lrc`
        // without needing symlink privileges on the test machine.
        val lookup = PrivateMusicLyricLookup(
            roots = listOf(rootDir.path),
            canonicalPath = { path ->
                if (File(path).name == "song.lrc") {
                    File(rootDir.parentFile ?: rootDir, "secret.lrc").path
                } else {
                    path
                }
            },
        )

        assertTrue(lookup.find(audio.path).isEmpty())
    }

    @Test
    fun aRealSymlinkOutsideTheRootIsNotAButton() {
        val rootDir = temp.newFolder("music").canonicalFile
        val outsideDir = temp.newFolder("outside").canonicalFile
        val target = File(outsideDir, "target.lrc").apply { writeText("[00:00]hi") }
        File(rootDir, "song.mp3").writeText("audio")
        try {
            Files.createSymbolicLink(File(rootDir, "song.lrc").toPath(), target.toPath())
        } catch (e: UnsupportedOperationException) {
            Assume.assumeNoException(e)
        } catch (e: java.io.IOException) {
            Assume.assumeNoException(e)
        } catch (e: SecurityException) {
            Assume.assumeNoException(e)
        }

        val audio = File(rootDir, "song.mp3")

        assertTrue(PrivateMusicLyricLookup(listOf(rootDir.path)).find(audio.path).isEmpty())
    }

    // ---------- opener extension / containment guard ----------

    @Test
    fun theOpenerGuardAcceptsLrcSidecarsInsideTheProviderRoot() {
        assertTrue(MusicLyricOpener.isOpenableLyric(root, "$root/song.lrc"))
        assertTrue(MusicLyricOpener.isOpenableLyric(root, "$root/song.zh.lrc"))
        assertTrue(MusicLyricOpener.isOpenableLyric(root, "  $root/song.lrc  "))
    }

    @Test
    fun theOpenerGuardComparesTheLrcSuffixCaseInsensitively() {
        assertTrue(MusicLyricOpener.isOpenableLyric(root, "$root/SONG.LRC"))
        assertTrue(MusicLyricOpener.isOpenableLyric(root, "$root/SONG.ZH.LrC"))
    }

    @Test
    fun theOpenerGuardRejectsOtherSuffixesMissingPathsAndTraversal() {
        assertFalse(MusicLyricOpener.isOpenableLyric(root, ""))
        assertFalse(MusicLyricOpener.isOpenableLyric(root, "   "))
        assertFalse(MusicLyricOpener.isOpenableLyric(root, "$root/song.lrc.txt"))
        assertFalse(MusicLyricOpener.isOpenableLyric(root, "$root/song.mp3"))
        assertFalse(MusicLyricOpener.isOpenableLyric(root, "$root/song"))
        assertFalse(MusicLyricOpener.isOpenableLyric(root, "$root/../secret/song.lrc"))
        assertFalse(
            MusicLyricOpener.isOpenableLyric(root, "/storage/emulated/0/Music/song.lrc"),
        )
    }

    @Test
    fun theOpenerGuardIsCaseSensitiveAboutTheProviderRoot() {
        assertFalse(
            MusicLyricOpener.isOpenableLyric(root, "$root".uppercase() + "/song.lrc"),
        )
    }

    // ---------- intent and FileProvider contract ----------

    @Test
    fun lyricMimeIsTriedBeforePlainText() {
        assertEquals(
            listOf(MusicLyricOpener.MIME_LYRIC, MusicLyricOpener.MIME_PLAIN),
            MusicLyricOpener.MIME_TYPES,
        )
        assertEquals("text/x-lyric", MusicLyricOpener.MIME_LYRIC)
        assertEquals("text/plain", MusicLyricOpener.MIME_PLAIN)
    }

    @Test
    fun theViewIntentCarriesAReadGrantAndNoWriteGrant() {
        val flags = MusicLyricOpener.VIEW_FLAGS

        assertTrue(flags and android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
        assertEquals(0, flags and android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertEquals(
            0,
            flags and android.content.Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION,
        )
    }

    @Test
    fun theProviderAuthorityIsTheApplicationIdPlusFileprovider() {
        assertEquals(
            "com.lingyi.noveldl.fileprovider",
            MusicLyricOpener.providerAuthority("com.lingyi.noveldl"),
        )
    }

    @Test
    fun theProviderRootMapsThePrivateFilesDirectoryToTheDeclaredMusicRange() {
        val filesDir = temp.newFolder("files")

        assertEquals(
            File(filesDir, "engine_home/downloads/music").absolutePath,
            MusicLyrics.providerRoot(filesDir),
        )
        // Must stay in sync with res/xml/file_paths.xml (pinned by
        // AndroidSecurityConfigTest) and with what the opener hands out.
        assertEquals(
            "engine_home/downloads/music",
            "${EmbeddedEngine.HOME_DIRECTORY}/${MusicLyrics.EMBEDDED_MUSIC_SUBDIRECTORY}",
        )
    }

    @Test
    fun theOpenerValidatesThePathBeforeAskingTheProviderForAUri() {
        val opener = read("ui/music/MusicLyricOpener.kt")
        val code = codeOnly(opener)

        // Only the declared range goes to getUriForFile, and only through the
        // shared authority helper.
        assertTrue(code.contains("providerAuthority(context.packageName)"))
        assertTrue(code.contains("FileProvider.getUriForFile"))
        // Use the call site, not the declaration, for the ordering check.
        assertTrue(
            code.lastIndexOf("isOpenableLyric(") in 0 until code.indexOf(
                "FileProvider.getUriForFile",
            ),
        )
        // No write or persistable grant anywhere in the implementation.
        assertFalse(code.contains("FLAG_GRANT_WRITE_URI_PERMISSION"))
        assertFalse(code.contains("FLAG_GRANT_PERSISTABLE_URI_PERMISSION"))
    }

    /** Strips Kotlin comments: a comment must not satisfy or break a check. */
    private fun codeOnly(source: String): String =
        source
            .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("//[^\\n]*"), "")

    private fun read(relative: String): String {
        val start = File(checkNotNull(System.getProperty("user.dir"))).absoluteFile
        var dir: File? = start
        while (dir != null) {
            listOf(
                File(dir, "src/main/java/com/lingyi/noveldl/$relative"),
                File(dir, "app/src/main/java/com/lingyi/noveldl/$relative"),
                File(dir, "android/app/src/main/java/com/lingyi/noveldl/$relative"),
            ).firstOrNull { it.isFile }?.let { return it.readText() }
            dir = dir.parentFile
        }
        error("cannot locate $relative from $start")
    }
}
