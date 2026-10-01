package io.github.itsonu.opentransfer

import android.content.Context
import android.media.MediaScannerConnection
import android.util.Log
import org.json.JSONObject

/**
 * Passed to `open_transfer.android.start()`; Python calls [onEvent] (on its own threads)
 * with `"offer"` and `"received"` events. Must stay a public class with a public method so
 * Chaquopy can call it by reflection.
 */
class PythonEvents(context: Context) {
    private val app: Context = context.applicationContext

    fun onEvent(name: String, json: String) {
        try {
            when (name) {
                "offer" -> if (!TransferService.isUiVisible) Notifications.showOffer(app, json)
                "received" -> {
                    val path = JSONObject(json).optString("path")
                    if (path.isNotEmpty()) {
                        // So received photos and videos show up in Gallery right away.
                        MediaScannerConnection.scanFile(app, arrayOf(path), null, null)
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't handle the '$name' event", e)
        }
    }

    private companion object {
        const val LOG_TAG = "OpenTransfer"
    }
}
