#!/usr/bin/env python3
"""Static contract for the Android SAF download path under revocation.

The behavioural rules live in JVM tests (``SafRevocationTest.kt``,
``HistoryDownloadFailureTest.kt``, ``DownloadErrorsTest.kt``) because they take
plain byte streams and ``SafDocument``. What a JVM test cannot see is the glue
around them: the ``ContentResolver`` call order, the ``Context.getString``
call sites and the Compose progress state. Those are the places where a
refactor can quietly delete a guard while every unit test still passes, so
they are pinned here against the Kotlin sources and the string resources:

* the stored tree URI is re-checked against the *current* persisted grants
  immediately before a write (a cached URI is not proof of access);
* a tree that resolves to a deleted folder falls back, and a deleted
  subdirectory is rebuilt instead of being written through;
* every failure/cancellation path deletes the half-written document;
* the output close is classified as a write failure, not as a network error;
* only permission-class failures change the destination;
* the history screen clears the per-file progress entry after every attempt;
* the fallback/permission wording tells the user to pick the folder again.

Pure static scan: no Gradle, no Android SDK, no network.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA_DIR = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "lingyi" / "noveldl"
)
RES_DIR = ROOT / "android" / "app" / "src" / "main" / "res"

DOWNLOADER = JAVA_DIR / "data" / "HistoryDownloader.kt"
DIRECTORY = JAVA_DIR / "data" / "DownloadDirectory.kt"
ERRORS = JAVA_DIR / "ui" / "history" / "DownloadErrors.kt"
SCREEN = JAVA_DIR / "ui" / "history" / "HistoryScreen.kt"
STRINGS_ZH = RES_DIR / "values" / "strings.xml"
STRINGS_EN = RES_DIR / "values-en" / "strings.xml"


def read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    return path.read_text(encoding="utf-8")


def read_strings(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    values: dict[str, str] = {}
    for element in root.findall("string"):
        name = element.get("name")
        assert name, f"unnamed string in {path.name}"
        values[name] = (element.text or "").strip()
    return values


def test_the_stored_tree_is_rechecked_against_current_grants_before_writing() -> None:
    """A URI in DataStore is a cache; the persisted grant is the authority."""
    source = read(DOWNLOADER)
    assert "fun writeGrantedTreeUris(" in source
    assert "persistedUriPermissions" in source and "isWritePermission" in source

    call = source.index(
        "hasPersistedWriteAccess(treeUri.toString(), writeGrantedTreeUris(context))"
    )
    open_stream = source.index("resolver.openOutputStream(target.uri)")
    assert call < open_stream, (
        "the grant check must run before the destination is opened"
    )
    # It has to be inside downloadToDirectory's IO block, not in enqueue().
    body = source[source.index("suspend fun downloadToDirectory(") :]
    assert "withContext(Dispatchers.IO)" in body


def test_a_deleted_tree_falls_back_and_a_deleted_subdirectory_is_rebuilt() -> None:
    downloader = read(DOWNLOADER)
    directory = read(DIRECTORY)

    # The tree handle is only accepted while the folder still exists; a
    # revoked grant can also throw from the provider query itself.
    assert "DocumentFile.fromTreeUri(context, treeUri)" in downloader
    assert "it.isDirectory && it.exists()" in downloader
    assert "runCatching {" in downloader

    # A stale subdirectory handle must not be written through after the user
    # deleted NovelDL/ from another app: it is recreated instead.
    assert "it.isDirectory && it.exists" in directory
    assert "tree.createDirectory(DOWNLOAD_SUBDIRECTORY)" in directory


def test_every_failure_and_cancellation_path_removes_the_partial_document() -> None:
    source = read(DOWNLOADER)
    body = source[source.index("suspend fun downloadToDirectory(") :]

    assert "discardPartialDocument(created)" in body, (
        "cancellation leaked a partial file"
    )
    assert body.count("failureAfterCleanup<String>(") >= 3, (
        "classified, IOException and generic failure paths must all clean up"
    )
    # Cleanup is "delete, never mask": the provider's own delete error is
    # swallowed so the caller still sees the original cause.
    helper = source[source.index("internal fun discardPartialDocument(") :]
    assert "runCatching { created.delete() }" in helper
    assert "return Result.failure(error)" in helper


def test_an_output_close_failure_is_classified_as_a_write_failure() -> None:
    """Providers flush at close(); an ENOSPC/EACCES there is not a network error."""
    source = read(DOWNLOADER)

    assert "internal fun streamToDocumentAndClose(" in source
    download_body = source[source.index("suspend fun downloadToDirectory(") :]
    assert "streamToDocumentAndClose(" in download_body
    assert "output.close()" in source
    assert "classifyWriteFailure(closeError)" in source
    # The close must not replace the failure that stopped the transfer.
    assert "if (failure == null)" in source


def test_progress_stops_when_the_attempt_is_cancelled() -> None:
    """No final 100 % sample after cancellation; the file is deleted anyway."""
    source = read(DOWNLOADER)
    stream = source[source.index("internal fun streamToDocument(") :]
    final_sample = stream.index("val finalSample = DownloadProgress(")
    assert stream.rindex("checkActive()", 0, final_sample) > stream.index(
        "output.flush()"
    )
    assert "onProgress(finalSample)" in stream


def test_only_permission_class_failures_change_the_destination() -> None:
    source = read(ERRORS)
    start = source.index("internal fun shouldFallBackToSystemDownloads(")
    end = source.index("\n}\n", start)
    body = source[start:end]

    assert "is DownloadDirectoryUnavailableException" in body
    assert "is SecurityException" in body
    assert "DownloadFailureKind.Permission" in body
    for forbidden in ("NoSpace", "Provider", "Network"):
        assert forbidden not in body, (
            f"{forbidden} failures would fail at the fallback destination too "
            "or hide the real cause"
        )


def test_the_history_screen_uses_the_shared_predicate_and_clears_progress() -> None:
    screen = read(SCREEN)

    assert "shouldFallBackToSystemDownloads(error)" in screen
    # The progress entry is transient: removed for success, failure and
    # cancellation alike, so a failed row cannot keep a stale bar.
    assert "progressByFile.update { it - key }" in screen
    finally_block = screen[screen.index("} finally {") :]
    assert "progressByFile.update { it - key }" in finally_block
    # A revoked/deleted tree is reported as a destination change, not as a
    # plain failure, so the user knows where the file went.
    assert "R.string.settings_download_dir_fallback" in screen


def test_the_fallback_and_permission_copy_asks_for_a_new_folder() -> None:
    for path in (STRINGS_ZH, STRINGS_EN):
        values = read_strings(path)
        fallback = values["settings_download_dir_fallback"]
        permission = values["download_error_permission"]
        unavailable = values["settings_download_dir_unavailable"]

        assert "Downloads" in fallback, (
            f"{path.name}: fallback must name the new destination"
        )
        assert re.search(r"重新选择|pick|choose", fallback), (
            f"{path.name}: the fallback notice must tell the user to re-pick the folder"
        )
        assert re.search(r"重新选择|pick|choose", permission), (
            f"{path.name}: a permission failure must name the fix"
        )
        assert re.search(r"重新选择|pick|choose", unavailable), (
            f"{path.name}: a stale cached tree must name the fix"
        )
