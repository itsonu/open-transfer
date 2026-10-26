package io.github.itsonu.opentransfer

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.core.content.IntentCompat
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Sends text to your other devices in one tap, with no window of its own:
 *
 * - from the **Quick Settings tile** ([ClipboardTileService]): the clipboard, read the
 *   moment this invisible activity has focus (the only way Android 10+ allows it);
 * - from the **share sheet** ("Send to my devices"): the shared text;
 * - from the **text-selection menu**: the selected text.
 *
 * Private-looking text (passwords, codes, keys) asks first. A short message says where
 * it went, then the activity closes.
 *
 * A *file* shared as text (a .txt from a file manager) isn't a clip: it's copied in and
 * opened in [MainActivity] to send like any other shared file (see [SharedFiles]).
 */
class ClipboardSendActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private var handled = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handled = savedInstanceState?.getBoolean(KEY_HANDLED) ?: false
        if (isFile()) {
            if (!handled) openAsFile()
            return
        }
        given()?.let { send(it) }
    }

    private fun isFile(): Boolean {
        val share = intent ?: return false
        val stream = IntentCompat.getParcelableExtra(share, Intent.EXTRA_STREAM, Uri::class.java)
        val text = share.getCharSequenceExtra(Intent.EXTRA_TEXT)
        return SharePolicy.isFileShare(share.action, stream != null, !text.isNullOrEmpty())
    }

    private fun openAsFile() {
        handled = true
        val app = applicationContext
        val share = intent
        thread(name = "ot-share-copy", isDaemon = true) {
            // Our access to it ends when this activity closes, so copy it first.
            val kept = SharedFiles.copyIn(app, share)
            main.post {
                if (kept == 0) {
                    done(getString(R.string.share_nothing))
                } else {
                    startActivity(Intent(app, MainActivity::class.java).setAction(MainActivity.ACTION_SHOW_SHARED))
                    finish()
                }
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean(KEY_HANDLED, handled)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // The tile: Android only lets the app in front read the clipboard.
        if (hasFocus && !handled && given() == null) {
            val text = ClipSync.readIfNew(this, force = true)
            if (text == null) {
                done(getString(R.string.clip_nothing))
            } else {
                send(text)
            }
        }
    }

    /** Text handed to us by the share sheet or the selection menu. */
    private fun given(): String? {
        val text = when (intent?.action) {
            Intent.ACTION_SEND -> intent.getCharSequenceExtra(Intent.EXTRA_TEXT)
            Intent.ACTION_PROCESS_TEXT -> intent.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)
            else -> null
        }
        return text?.toString()?.takeIf { it.isNotEmpty() }
    }

    private fun send(text: String, confirm: Boolean = false) {
        if (handled && !confirm) return
        handled = true
        val app = applicationContext
        if (TransferService.state != TransferService.State.RUNNING) TransferService.start(this)
        thread(name = "ot-clip-send", isDaemon = true) {
            // Python may still be starting (the tile works with the app closed).
            val deadline = System.currentTimeMillis() + START_WAIT_MS
            while (TransferService.state != TransferService.State.RUNNING &&
                System.currentTimeMillis() < deadline
            ) {
                Thread.sleep(100)
            }
            val result = if (TransferService.state == TransferService.State.RUNNING) {
                ClipSync.send(text, manual = true, confirm = confirm)
            } else {
                JSONObject().put("status", "error").put("reason", "stopped")
            }
            main.post {
                if (result.optString("reason") == "sensitive" && !confirm) {
                    askFirst(text)
                } else {
                    done(describe(app, result))
                }
            }
        }
    }

    private fun askFirst(text: String) {
        if (isFinishing) return
        AlertDialog.Builder(this)
            .setTitle(R.string.clip_private_title)
            .setMessage(R.string.clip_private_text)
            .setPositiveButton(R.string.clip_send_anyway) { _, _ -> send(text, confirm = true) }
            .setNegativeButton(android.R.string.cancel) { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .show()
    }

    private fun done(message: String) {
        Toast.makeText(applicationContext, message, Toast.LENGTH_LONG).show()
        finish()
    }

    companion object {
        private const val KEY_HANDLED = "handled"
        private const val START_WAIT_MS = 15_000L

        /** What happened, in a sentence. */
        fun describe(context: android.content.Context, result: JSONObject): String {
            val names = result.optJSONObject("names") ?: JSONObject()
            val delivered = names.optJSONArray("delivered")?.let { a -> List(a.length()) { a.getString(it) } } ?: emptyList()
            val queued = names.optJSONArray("queued")?.let { a -> List(a.length()) { a.getString(it) } } ?: emptyList()
            return when {
                delivered.isNotEmpty() -> context.getString(R.string.clip_sent_to, delivered.joinToString(", "))
                queued.isNotEmpty() -> context.getString(R.string.clip_waiting_for, queued.joinToString(", "))
                else -> context.getString(
                    when (result.optString("reason")) {
                        "off" -> R.string.clip_off
                        "paused" -> R.string.clip_paused
                        "no_devices" -> R.string.clip_no_devices
                        "empty" -> R.string.clip_nothing
                        "too_large" -> R.string.clip_too_large
                        "type" -> R.string.clip_type
                        "stopped" -> R.string.clip_stopped
                        else -> R.string.clip_not_sent
                    },
                )
            }
        }
    }
}
