package io.github.itsonu.opentransfer

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
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
import java.io.IOException
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
        offer(items)
        return items.size
    }

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
                    val stream = resolver.openInputStream(uri) ?: return@mapIndexedNotNull null
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

    private fun offer(items: List<Item>) {
        if (items.isEmpty()) return
        synchronized(lock) { pending = pending + items }
        Log.i(LOG_TAG, "Shared ${items.size} file(s)") // never names: they're private
    }

    /** For the page (bridge): what was shared since it last asked, as JSON; "" if nothing. */
    fun take(): String {
        val token = SharePolicy.newToken()
        val items = synchronized(lock) {
            val taken = pending
            pending = emptyList()
            if (taken.isNotEmpty()) {
                served[token] = taken
                while (served.size > KEEP_TOKENS) served.remove(served.keys.first())
            }
            taken
        }
        if (items.isEmpty()) return ""
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
        return JSONObject().put("files", files).toString()
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
            resolver.openInputStream(item.uri)
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't open a shared file", e)
            null
        } ?: return missing()
        return WebResourceResponse(item.mime, null, 200, "OK", mapOf("Cache-Control" to "no-store"), stream)
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

    /** Is this a file shared as text (only a stream, no text)? Then it isn't a clip. */
    fun isFileShare(action: String?, hasStream: Boolean, hasText: Boolean): Boolean =
        action == "android.intent.action.SEND" && hasStream && !hasText

    /** The content's own type, else the share's if it's a real one (not image/*). */
    fun mime(content: String?, given: String?): String =
        content?.takeIf { it.isNotBlank() && '*' !in it }
            ?: given?.takeIf { it.isNotBlank() && '*' !in it }
            ?: "application/octet-stream"
}
