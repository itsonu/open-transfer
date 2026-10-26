package io.github.itsonu.opentransfer

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.PersistableBundle
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONObject

/**
 * The Android end of the Trusted Sync Network (see docs/sync.md).
 *
 * - **Receiving** works in the background: the Python engine in [TransferService] hands
 *   a clip over ([write]) and it goes straight onto the clipboard. Android allows that.
 * - **Sending** needs this app in front, because Android 10+ only lets the focused app (or
 *   the keyboard) read the clipboard. So it happens when the app comes to the front, from
 *   the Quick Settings tile, the share sheet or the text-selection menu
 *   ([ClipboardSendActivity]) — never by reading the clipboard behind the owner's back.
 */
object ClipSync {
    private const val LOG_TAG = "OpenTransfer"
    private const val LABEL = "Open Transfer"

    /** ClipDescription.EXTRA_IS_SENSITIVE (Android 13+; older versions ignore it). */
    private const val EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE"
    private val main = Handler(Looper.getMainLooper())

    /** When the clip we last read or wrote was made (ClipDescription timestamps). */
    @Volatile
    private var lastSeen = 0L

    /** Called by Python (through [PythonEvents]) on its own thread. Never logs the text. */
    fun write(context: Context, json: String) {
        val data = JSONObject(json)
        val text = data.optString("text")
        if (text.isEmpty()) return
        val sensitive = data.optBoolean("sensitive")
        val app = context.applicationContext
        main.post {
            try {
                val clip = ClipData.newPlainText(LABEL, text)
                if (sensitive) {
                    // Android 13+ hides it in the "copied" preview; keyboards don't suggest it.
                    clip.description.extras = PersistableBundle().apply {
                        putBoolean(EXTRA_IS_SENSITIVE, true)
                    }
                }
                clipboard(app)?.setPrimaryClip(clip)
                lastSeen = clipboard(app)?.primaryClipDescription?.timestamp ?: lastSeen
            } catch (e: Exception) {
                Log.w(LOG_TAG, "Couldn't write the clipboard", e)
            }
        }
    }

    private fun clipboard(context: Context): ClipboardManager? =
        context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager

    /**
     * The clipboard's text, if it changed since we last looked. Call from the main thread
     * while one of our windows has focus. Checking the description first avoids reading
     * (and Android's "pasted from your clipboard" message) when nothing new was copied.
     */
    fun readIfNew(context: Context, force: Boolean = false): String? {
        val cm = clipboard(context) ?: return null
        val description = cm.primaryClipDescription ?: return null
        val stamp = description.timestamp
        if (!force && stamp != 0L && stamp == lastSeen) return null
        if (!force && description.label?.toString() == LABEL) {
            lastSeen = stamp
            return null // one we wrote
        }
        val text = try {
            cm.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(context)?.toString()
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't read the clipboard", e)
            null
        }
        lastSeen = stamp
        return text?.takeIf { it.isNotEmpty() }
    }

    /** True when sync is on and copies go out on their own (cheap; no clipboard access). */
    fun wantsAutomatic(): Boolean =
        try {
            if (!Python.isStarted()) {
                false
            } else {
                val state = JSONObject(module().callAttr("sync_state").toString())
                state.optBoolean("active") && state.optString("mode") == "auto"
            }
        } catch (e: Exception) {
            false
        }

    /**
     * Hand [text] to the sync engine. Runs network I/O: call from a background thread.
     * Returns the engine's answer: `{status, reason?, delivered, queued, refused, names}`.
     */
    fun send(text: String, manual: Boolean, confirm: Boolean = false): JSONObject =
        try {
            JSONObject(module().callAttr("sync_copy", text, manual, confirm).toString())
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't send the clipboard", e)
            JSONObject().put("status", "error").put("reason", "failed")
        }

    private fun module() = Python.getInstance().getModule("open_transfer.android")
}
