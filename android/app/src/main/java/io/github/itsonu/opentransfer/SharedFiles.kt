package io.github.itsonu.opentransfer

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Log
import android.webkit.MimeTypeMap
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import androidx.core.content.IntentCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.security.SecureRandom
import java.util.Locale

/**
 * Photos and files shared to Open Transfer from another app's share sheet.
 *
 * [MainActivity] receives them ([receive]) and tells the page, which takes them through the
 * bridge ([take]) and fetches each one from `/__shared/<token>/<index>` on its own origin.
 * The WebView answers those requests from here ([serve]), streaming from the other app, so
 * even a large video never passes through Python or sits in memory. The page then stages
 * them like files that were picked or dropped: choose a device, send.
 *
 * Only the person's own share reaches this: the token is random and the URLs are answered
 * inside this app's WebView, never on the network.
 */
object SharedFiles {
    private const val LOG_TAG = "OpenTransfer"
    private const val KEEP_TOKENS = 4
    private const val MAX_ITEMS = 500

    /** Text-file shares are copied (see [copyIn]); bigger ones aren't worth holding. */
    private const val MAX_COPY_BYTES = 256L * 1024 * 1024

    class Item(val uri: Uri, val name: String, val mime: String, val size: Long)

    private val lock = Any()
    private var pending: List<Item> = emptyList()
    private var pendingText: String? = null

    /** Shares the page took, by token, newest last (it may still be fetching them). */
    private val served = LinkedHashMap<String, List<Item>>()

    /**
     * Keep what [intent] shares (`ACTION_SEND` / `ACTION_SEND_MULTIPLE`). Queries the other
     * app for names and sizes: call off the main thread. Returns how many were kept.
     */
    fun receive(context: Context, intent: Intent): Int {
        val resolver = context.contentResolver
        val own = context.packageName
        val type = intent.type
        val items = uris(intent)
            .filter { SharePolicy.accepts(it.scheme, it.authority, own) }
            .take(MAX_ITEMS)
            .mapIndexed { index, uri -> describe(resolver, uri, type, index) }
        val text = text(intent, hasFiles = items.isNotEmpty())
        offer(items, text)
        return items.size + (if (text != null) 1 else 0)
    }

    /** Text "Send to my devices" couldn't send itself: the page stages it to choose a device. */
    fun offerText(text: String) = offer(emptyList(), SharePolicy.sharedText(null, listOf(text)))

    /**
     * A file shared as `text/plain` (a .txt from a file manager) lands in
     * [ClipboardSendActivity], whose access to it ends when it closes — so copy it in.
     * Call off the main thread. Returns how many were kept.
     */
    fun copyIn(context: Context, intent: Intent): Int {
        val resolver = context.contentResolver
        val dir = File(context.cacheDir, "shared").apply { mkdirs() }
        // Earlier copies: the page fetched them long ago.
        val stale = System.currentTimeMillis() - 24 * 60 * 60 * 1000L
        dir.listFiles()?.filter { it.lastModified() < stale }?.forEach { it.delete() }
        val items = uris(intent)
            .filter { SharePolicy.accepts(it.scheme, it.authority, context.packageName) }
            .take(MAX_ITEMS)
            .mapIndexedNotNull { index, uri ->
                val item = describe(resolver, uri, intent.type, index)
                val copy = File(dir, SharePolicy.newToken())
                try {
                    val stream = open(resolver, uri) ?: return@mapIndexedNotNull null
                    var total = 0L
                    stream.use { input ->
                        copy.outputStream().use { output ->
                            val buffer = ByteArray(64 * 1024)
                            while (true) {
                                val read = input.read(buffer)
                                if (read < 0) break
                                total += read
                                if (total > MAX_COPY_BYTES) throw IOException("too large")
                                output.write(buffer, 0, read)
                            }
                        }
                    }
                    Item(Uri.fromFile(copy), item.name, item.mime, total)
                } catch (e: Exception) {
                    Log.w(LOG_TAG, "Couldn't copy a shared file", e)
                    copy.delete()
                    null
                }
            }
        offer(items)
        return items.size
    }

    private fun offer(items: List<Item>, text: String? = null) {
        if (items.isEmpty() && text == null) return
        synchronized(lock) {
            pending = pending + items
            if (text != null) pendingText = listOfNotNull(pendingText, text).joinToString("\n\n")
        }
        Log.i(LOG_TAG, "Shared ${items.size} file(s)${if (text != null) " and text" else ""}") // never names
    }

    /**
     * A copied image ([uri] from the clipboard), served like a share so the page fetches it
     * from `/__shared/…`: JSON `{url, mime, size}`, or "" if it isn't another app's content.
     */
    fun copiedImage(context: Context, uri: Uri, mime: String): String {
        if (!SharePolicy.accepts(uri.scheme, uri.authority, context.packageName)) return ""
        val item = describe(context.contentResolver, uri, mime, 0)
        val token = SharePolicy.newToken()
        synchronized(lock) {
            served[token] = listOf(item)
            while (served.size > KEEP_TOKENS) served.remove(served.keys.first())
        }
        return JSONObject()
            .put("url", SharePolicy.path(token, 0))
            .put("mime", item.mime)
            .put("size", item.size)
            .toString()
    }

    /** For the page (bridge): what was shared since it last asked, as JSON; "" if nothing. */
    fun take(): String {
        val token = SharePolicy.newToken()
        val (items, text) = synchronized(lock) {
            val taken = pending
            val words = pendingText
            pending = emptyList()
            pendingText = null
            if (taken.isNotEmpty()) {
                served[token] = taken
                while (served.size > KEEP_TOKENS) served.remove(served.keys.first())
            }
            taken to words
        }
        if (items.isEmpty() && text == null) return ""
        val files = JSONArray()
        items.forEachIndexed { index, item ->
            files.put(
                JSONObject()
                    .put("url", SharePolicy.path(token, index))
                    .put("name", item.name)
                    .put("mime", item.mime)
                    .put("size", item.size),
            )
        }
        return JSONObject().put("files", files).apply { if (text != null) put("text", text) }.toString()
    }

    /**
     * Answers the page's fetch of a shared file (WebViewClient.shouldInterceptRequest, on a
     * WebView thread); null for every other request, which goes to the server as usual.
     */
    fun serve(resolver: ContentResolver, request: WebResourceRequest?): WebResourceResponse? {
        val url = request?.url ?: return null
        if (!LocalPolicy.isLoopbackHost(url.host)) return null
        val path = url.path ?: return null
        if (!path.startsWith(SharePolicy.PREFIX)) return null
        val (token, index) = SharePolicy.parse(path) ?: return missing()
        val item = synchronized(lock) { served[token]?.getOrNull(index) } ?: return missing()
        val stream = try {
            open(resolver, item.uri)
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't open a shared file", e)
            null
        } ?: return missing()
        // A big file is read a range at a time as it's sent (the page asks for 1-2 MiB).
        // WebView itself skips to the range's start (seen on Android 16), but reads on to
        // the end of the file: stop the stream at the range's end.
        val header = request?.requestHeaders?.entries?.firstOrNull { it.key.equals("Range", true) }?.value
        val range = SharePolicy.range(header, item.size)
        val headers = mutableMapOf("Cache-Control" to "no-store")
        if (range == null) return WebResourceResponse(item.mime, null, 200, "OK", headers, stream)
        headers["Content-Range"] = "bytes ${range.first}-${range.last}/${item.size}"
        return WebResourceResponse(item.mime, null, 206, "Partial Content", headers, Limited(stream, range.last - range.first + 1))
    }

    /**
     * The file as it was taken: a photo or video from the media store comes with its
     * location blanked unless it's asked for whole (which needs ACCESS_MEDIA_LOCATION).
     * Without that permission it's sent as Android hands it over.
     */
    private fun open(resolver: ContentResolver, uri: Uri): InputStream? {
        if (uri.authority == MediaStore.AUTHORITY) {
            try {
                return resolver.openInputStream(MediaStore.setRequireOriginal(uri))
            } catch (e: Exception) {
                // Not allowed: as it comes.
            }
        }
        return resolver.openInputStream(uri)
    }

    /** At most [left] bytes of [input] (skips, done by WebView, don't count). */
    private class Limited(input: InputStream, private var left: Long) : FilterInputStream(input) {
        override fun read(): Int = if (left <= 0) -1 else super.read().also { if (it >= 0) left-- }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (left <= 0) return -1
            return super.read(b, off, minOf(len.toLong(), left).toInt()).also { if (it > 0) left -= it }
        }
    }

    private fun missing() =
        WebResourceResponse("text/plain", "utf-8", 404, "Not Found", emptyMap(), ByteArrayInputStream(ByteArray(0)))

    private fun uris(intent: Intent): List<Uri> {
        val found = mutableListOf<Uri>()
        when (intent.action) {
            Intent.ACTION_SEND ->
                IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java)?.let { found += it }
            Intent.ACTION_SEND_MULTIPLE ->
                IntentCompat.getParcelableArrayListExtra(intent, Intent.EXTRA_STREAM, Uri::class.java)
                    ?.let { found += it.filterNotNull() }
        }
        if (found.isEmpty()) {
            val clip = intent.clipData
            if (clip != null) for (i in 0 until clip.itemCount) clip.getItemAt(i).uri?.let { found += it }
        }
        return found.distinct()
    }

    /** EXTRA_TEXT (one or a list), ClipData text and EXTRA_SUBJECT: never dropped silently. */
    private fun text(intent: Intent, hasFiles: Boolean): String? {
        val texts = mutableListOf<String?>()
        // A list or a single value, depending on the app: a mismatch just reads as null.
        intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.let { texts += it.toString() }
        intent.getCharSequenceArrayListExtra(Intent.EXTRA_TEXT)?.forEach { texts += it?.toString() }
        intent.clipData?.let { clip -> for (i in 0 until clip.itemCount) texts += clip.getItemAt(i).text?.toString() }
        return SharePolicy.sharedText(intent.getStringExtra(Intent.EXTRA_SUBJECT), texts, hasFiles)
    }

    private fun describe(resolver: ContentResolver, uri: Uri, given: String?, index: Int): Item {
        var name: String? = null
        var size = -1L
        try {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)
                ?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        val n = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        val s = cursor.getColumnIndex(OpenableColumns.SIZE)
                        if (n >= 0 && !cursor.isNull(n)) name = cursor.getString(n)
                        if (s >= 0 && !cursor.isNull(s)) size = cursor.getLong(s)
                    }
                }
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't look up a shared file", e)
        }
        val mime = SharePolicy.mime(
            try {
                resolver.getType(uri)
            } catch (e: Exception) {
                null
            },
            given,
        )
        val extension = MimeTypeMap.getSingleton().getExtensionFromMimeType(mime)?.lowercase(Locale.ROOT)
        return Item(uri, SharePolicy.fileName(name ?: uri.lastPathSegment, index, extension), mime, size)
    }
}

/** The pure rules behind [SharedFiles] (no Android APIs, so they are unit tested). */
object SharePolicy {
    const val PREFIX = "/__shared/"
    const val MAX_TEXT = 1_000_000
    private const val MAX_NAME = 200
    private val PATH = Regex("^/__shared/([A-Za-z0-9_-]{1,64})/(\\d{1,6})$")
    private val random = SecureRandom()

    /**
     * Only content from another app. `file://` and this app's own providers would let a
     * share point us at our own private files (device keys, state), read with our rights.
     */
    fun accepts(scheme: String?, authority: String?, ownPackage: String): Boolean {
        if (scheme?.lowercase(Locale.ROOT) != "content" || authority.isNullOrEmpty()) return false
        return authority != ownPackage && !authority.startsWith("$ownPackage.")
    }

    fun path(token: String, index: Int): String = "$PREFIX$token/$index"

    /**
     * `Range: bytes=a-b` (or `a-`) of a file of [size] bytes, for the response's
     * Content-Range; null to send the whole file (no header, unknown size, anything else).
     */
    fun range(header: String?, size: Long): LongRange? {
        val match = Regex("^bytes=(\\d{1,15})-(\\d{0,15})$").matchEntire(header?.trim() ?: return null) ?: return null
        val start = match.groupValues[1].toLong()
        val end = match.groupValues[2].toLongOrNull() ?: (size - 1)
        if (size <= 0 || start >= size || end < start) return null
        return start..minOf(end, size - 1)
    }

    fun parse(path: String?): Pair<String, Int>? {
        val match = PATH.matchEntire(path ?: return null) ?: return null
        return match.groupValues[1] to match.groupValues[2].toInt()
    }

    fun newToken(): String {
        val bytes = ByteArray(18)
        random.nextBytes(bytes)
        return java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    /** The other app's name for it — never a path — or "Shared file 2.jpg". */
    fun fileName(raw: String?, index: Int, extension: String?): String {
        val base = raw.orEmpty().substringAfterLast('/').substringAfterLast('\\').trim()
        val name = if (base.isEmpty() || base == "." || base == "..") {
            "Shared file ${index + 1}" + (extension?.let { ".$it" } ?: "")
        } else {
            base
        }
        return name.take(MAX_NAME)
    }

    /**
     * What a share says in words: each text once (EXTRA_TEXT and ClipData often repeat
     * it), after the subject (a page's title) unless the text already has it. A subject
     * alone counts only when no file came with it: file managers put the name there.
     */
    fun sharedText(subject: String?, texts: List<String?>, hasFiles: Boolean = false): String? {
        val parts = texts.mapNotNull { it?.trim()?.takeIf(String::isNotEmpty) }.distinct()
        val title = subject?.trim()?.takeIf { s ->
            s.isNotEmpty() && parts.none { s in it } && (parts.isNotEmpty() || !hasFiles)
        }
        val body = parts.joinToString("\n\n")
        val all = listOfNotNull(title, body.takeIf(String::isNotEmpty)).joinToString("\n")
        return all.takeIf(String::isNotEmpty)?.take(MAX_TEXT)
    }

    /** "Send to my devices" couldn't send: clipboard sync off, paused, or nobody to send to. */
    fun opensAppInstead(reason: String): Boolean = reason in setOf("off", "paused", "no_devices")

    /** Is this a file shared as text (only a stream, no text)? Then it isn't a clip. */
    fun isFileShare(action: String?, hasStream: Boolean, hasText: Boolean): Boolean =
        action == "android.intent.action.SEND" && hasStream && !hasText

    /** The content's own type, else the share's if it's a real one (not a wildcard). */
    fun mime(content: String?, given: String?): String =
        content?.takeIf { it.isNotBlank() && '*' !in it }
            ?: given?.takeIf { it.isNotBlank() && '*' !in it }
            ?: "application/octet-stream"
}
