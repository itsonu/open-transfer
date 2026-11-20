package io.github.itsonu.opentransfer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.util.Log
import android.widget.Toast
import androidx.core.app.NotificationManagerCompat
import com.chaquo.python.Python
import org.json.JSONObject
import kotlin.concurrent.thread

/** Accept / Decline on an incoming-offer notification, without opening the app. */
class OfferActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val offer = intent.getStringExtra(EXTRA_OFFER) ?: return
        val accept = intent.action == ACTION_ACCEPT
        NotificationManagerCompat.from(context).cancel(intent.getIntExtra(EXTRA_NOTIFICATION, 0))
        val app = context.applicationContext
        val pending = goAsync()
        thread(name = "offer-decision") {
            try {
                val result = if (Python.isStarted()) {
                    JSONObject(
                        Python.getInstance().getModule("open_transfer.android")
                            .callAttr("decide", offer, accept).toString(),
                    )
                } else {
                    JSONObject().put("error", "stopped")
                }
                // Already answered, expired, or no room: say so rather than fail silently.
                val message = result.optString("message").ifEmpty {
                    if (result.has("error")) app.getString(R.string.offer_gone) else ""
                }
                if (message.isNotEmpty()) {
                    Handler(app.mainLooper).post {
                        Toast.makeText(app, message, Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                Log.w(LOG_TAG, "Couldn't answer the offer", e)
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        const val ACTION_ACCEPT = "io.github.itsonu.opentransfer.ACCEPT_OFFER"
        const val ACTION_DECLINE = "io.github.itsonu.opentransfer.DECLINE_OFFER"
        const val EXTRA_OFFER = "offer"
        const val EXTRA_NOTIFICATION = "notification"
        private const val LOG_TAG = "OpenTransfer"
    }
}
