package io.github.itsonu.opentransfer

import java.net.URI
import java.net.URISyntaxException
import java.util.Locale

/**
 * Pure decisions about URLs and file names (no Android APIs, so they are unit tested).
 *
 * The WebView only ever shows this device's own server on the loopback address; anything
 * else is handed to another app.
 */
object LocalPolicy {
    private val LOOPBACK_HOSTS = setOf("127.0.0.1", "localhost", "::1", "[::1]")
    private val OWN_FILE_PREFIXES = listOf("/files/", "/download/")

    fun isLoopbackHost(host: String?): Boolean =
        host != null && host.lowercase(Locale.ROOT) in LOOPBACK_HOSTS

    /** `http(s)://127.0.0.1:<port>/…` or `http(s)://localhost…`: stays inside the app. */
    fun isLocalPage(url: String?): Boolean {
        val uri = parse(url) ?: return false
        val scheme = uri.scheme?.lowercase(Locale.ROOT)
        return (scheme == "http" || scheme == "https") && isLoopbackHost(uri.host)
    }

    /** Schemes the WebView handles by itself and that must never be sent to another app. */
    fun isInternalScheme(scheme: String?): Boolean {
        val normalized = scheme?.lowercase(Locale.ROOT) ?: return false
        return normalized in INTERNAL_SCHEMES
    }

    private val INTERNAL_SCHEMES = setOf("about", "blob", "data", "javascript")

    /**
     * A plain file name inside Download/Open Transfer: no folders, nothing hidden.
     * (The Python side has its own, stricter, checks — this guards the native "open".)
     */
    fun isSafeFileName(name: String?): Boolean =
        !name.isNullOrBlank() &&
            name.length <= 255 &&
            !name.startsWith(".") &&
            '/' !in name &&
            '\\' !in name &&
            '\u0000' !in name

    /**
     * For a download of one of the owner's own files (`/files/<name>` or `/download/<name>`
     * on this device's server) return `<name>`; otherwise `null`.
     */
    fun ownFileName(url: String?): String? {
        if (!isLocalPage(url)) return null
        val path = parse(url)?.path ?: return null // already percent-decoded
        for (prefix in OWN_FILE_PREFIXES) {
            if (path.startsWith(prefix)) {
                return path.substring(prefix.length).takeIf { isSafeFileName(it) }
            }
        }
        return null
    }

    /** "tablet" for large screens (sw600dp and up, like Android's own resource buckets). */
    fun formFor(smallestScreenWidthDp: Int): String =
        if (smallestScreenWidthDp >= 600) "tablet" else "phone"

    private fun parse(url: String?): URI? =
        try {
            if (url.isNullOrBlank()) null else URI(url)
        } catch (e: URISyntaxException) {
            null
        }
}
