package io.github.itsonu.opentransfer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalPolicyTest {

    @Test
    fun onlyLoopbackPagesStayInTheApp() {
        assertTrue(LocalPolicy.isLocalPage("http://127.0.0.1:5000/"))
        assertTrue(LocalPolicy.isLocalPage("http://localhost:5001/?pair=123456"))
        assertTrue(LocalPolicy.isLocalPage("http://LOCALHOST/"))
        assertTrue(LocalPolicy.isLocalPage("http://[::1]:5000/"))
        assertFalse(LocalPolicy.isLocalPage("http://192.168.1.20:5000/"))
        assertFalse(LocalPolicy.isLocalPage("https://github.com/itsonu/open-transfer"))
        assertFalse(LocalPolicy.isLocalPage("http://127.0.0.1.evil.example/"))
        assertFalse(LocalPolicy.isLocalPage("file:///sdcard/Download/x.html"))
        assertFalse(LocalPolicy.isLocalPage("javascript:alert(1)"))
        assertFalse(LocalPolicy.isLocalPage("not a url"))
        assertFalse(LocalPolicy.isLocalPage(null))
    }

    @Test
    fun internalSchemes() {
        assertTrue(LocalPolicy.isInternalScheme("blob"))
        assertTrue(LocalPolicy.isInternalScheme("about"))
        assertFalse(LocalPolicy.isInternalScheme("https"))
        assertFalse(LocalPolicy.isInternalScheme("intent"))
        assertFalse(LocalPolicy.isInternalScheme(null))
    }

    @Test
    fun safeFileNames() {
        assertTrue(LocalPolicy.isSafeFileName("photo.jpg"))
        assertTrue(LocalPolicy.isSafeFileName("Holiday 2024 (1).mov"))
        assertFalse(LocalPolicy.isSafeFileName(""))
        assertFalse(LocalPolicy.isSafeFileName("  "))
        assertFalse(LocalPolicy.isSafeFileName(null))
        assertFalse(LocalPolicy.isSafeFileName(".open-transfer"))
        assertFalse(LocalPolicy.isSafeFileName(".."))
        assertFalse(LocalPolicy.isSafeFileName("../secret"))
        assertFalse(LocalPolicy.isSafeFileName("a/b.txt"))
        assertFalse(LocalPolicy.isSafeFileName("a\\b.txt"))
        assertFalse(LocalPolicy.isSafeFileName("x".repeat(256)))
    }

    @Test
    fun ownFileDownloadsAreRecognised() {
        assertEquals("a b.txt", LocalPolicy.ownFileName("http://127.0.0.1:5000/files/a%20b.txt"))
        assertEquals("pic.jpg", LocalPolicy.ownFileName("http://127.0.0.1:5000/files/pic.jpg?inline=1"))
        assertEquals("doc.pdf", LocalPolicy.ownFileName("http://localhost:5000/download/doc.pdf"))
        assertNull(LocalPolicy.ownFileName("http://127.0.0.1:5000/api/archive"))
        assertNull(LocalPolicy.ownFileName("http://127.0.0.1:5000/files/.open-transfer"))
        assertNull(LocalPolicy.ownFileName("http://127.0.0.1:5000/files/sub%2Fdir.txt"))
        assertNull(LocalPolicy.ownFileName("http://192.168.1.20:5000/files/pic.jpg"))
        assertNull(LocalPolicy.ownFileName(null))
    }

    @Test
    fun formFollowsTheSmallestScreenWidth() {
        assertEquals("phone", LocalPolicy.formFor(411))
        assertEquals("tablet", LocalPolicy.formFor(600))
        assertEquals("tablet", LocalPolicy.formFor(800))
    }
}
