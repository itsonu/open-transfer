package io.github.itsonu.opentransfer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SharePolicyTest {

    @Test
    fun onlyOtherAppsContentIsAccepted() {
        val me = "io.github.itsonu.opentransfer"
        assertTrue(SharePolicy.accepts("content", "media", me))
        assertTrue(SharePolicy.accepts("content", "com.google.android.apps.photos.contentprovider", me))
        // file:// would let another app point us at our own private files (keys, state).
        assertFalse(SharePolicy.accepts("file", null, me))
        // …and so would our own FileProvider, read with our own rights.
        assertFalse(SharePolicy.accepts("content", "$me.files", me))
        assertFalse(SharePolicy.accepts("content", me, me))
        assertFalse(SharePolicy.accepts("content", null, me))
        assertFalse(SharePolicy.accepts("http", "example.com", me))
        assertFalse(SharePolicy.accepts(null, "media", me))
    }

    @Test
    fun theUrlThePageFetches() {
        assertEquals("/__shared/abc_D-9/0", SharePolicy.path("abc_D-9", 0))
        assertEquals("abc_D-9" to 3, SharePolicy.parse("/__shared/abc_D-9/3"))
        assertNull(SharePolicy.parse("/__shared/abc/"))
        assertNull(SharePolicy.parse("/__shared/abc/-1"))
        assertNull(SharePolicy.parse("/__shared/abc/1/x"))
        assertNull(SharePolicy.parse("/__shared/a.b/1"))
        assertNull(SharePolicy.parse("/__shared//1"))
        assertNull(SharePolicy.parse("/api/state"))
        assertNull(SharePolicy.parse("/__shared/abc/99999999999"))
        assertNull(SharePolicy.parse(null))
    }

    @Test
    fun tokensAreUnguessable() {
        val a = SharePolicy.newToken()
        assertEquals(a, SharePolicy.parse(SharePolicy.path(a, 0))?.first)
        assertTrue(a.length >= 20)
        assertNotEquals(a, SharePolicy.newToken())
    }

    @Test
    fun namesAreKeptButNeverAPath() {
        assertEquals("IMG_2041.jpg", SharePolicy.fileName("IMG_2041.jpg", 0, "jpg"))
        assertEquals("passwd", SharePolicy.fileName("../../etc/passwd", 0, null))
        assertEquals("x.pdf", SharePolicy.fileName("a\\b\\x.pdf", 0, "pdf"))
        assertEquals("Shared file 1.jpg", SharePolicy.fileName(null, 0, "jpg"))
        assertEquals("Shared file 2", SharePolicy.fileName("  ", 1, null))
        assertEquals("Shared file 1.png", SharePolicy.fileName("..", 0, "png"))
        assertEquals(200, SharePolicy.fileName("x".repeat(500), 0, null).length)
    }

    @Test
    fun aTextFileIsAFileNotAClip() {
        // Sharing a .txt from a file manager arrives as text/plain with only a stream.
        assertTrue(SharePolicy.isFileShare("android.intent.action.SEND", hasStream = true, hasText = false))
        assertFalse(SharePolicy.isFileShare("android.intent.action.SEND", hasStream = true, hasText = true))
        assertFalse(SharePolicy.isFileShare("android.intent.action.SEND", hasStream = false, hasText = true))
        assertFalse(SharePolicy.isFileShare("android.intent.action.PROCESS_TEXT", hasStream = true, hasText = false))
        assertFalse(SharePolicy.isFileShare(null, hasStream = true, hasText = false))
    }

    @Test
    fun wildcardTypesAreNotAType() {
        assertEquals("image/jpeg", SharePolicy.mime("image/jpeg", "image/*"))
        assertEquals("application/pdf", SharePolicy.mime(null, "application/pdf"))
        assertEquals("application/octet-stream", SharePolicy.mime(null, "image/*"))
        assertEquals("application/octet-stream", SharePolicy.mime(null, "*/*"))
        assertEquals("application/octet-stream", SharePolicy.mime("", null))
    }
}
