package io.github.itsonu.opentransfer

import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.Settings
import android.util.Log
import java.io.File

/** Where things live on the device, and what this device is called. */
object Places {
    private const val LOG_TAG = "OpenTransfer"
    const val FOLDER = "Open Transfer"

    /** Download/Open Transfer — where received files go, visible in the Files app. */
    @Suppress("DEPRECATION") // Direct paths in Download/ are supported again since Android 11.
    fun publicStorageDir(): File =
        File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), FOLDER)

    /** App-specific fallback (Android/data/…/Download/Open Transfer, or internal storage). */
    fun fallbackStorageDir(context: Context): File {
        val base = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
            ?: File(context.filesDir, "downloads")
        return File(base, FOLDER)
    }

    /** Private: device identity, paired devices' keys, the session secret. */
    fun stateDir(context: Context): File = File(context.filesDir, "open-transfer")

    /**
     * The public folder if this app can actually use it the way the Python storage code
     * does (hidden work folder, `.part` files renamed into place), else the fallback.
     * Does file I/O: call it off the main thread.
     */
    fun chooseStorageDir(context: Context): File {
        val public = publicStorageDir()
        if (isUsable(public)) return public
        val fallback = fallbackStorageDir(context)
        Log.w(LOG_TAG, "Can't write to $public; saving received files to $fallback instead")
        fallback.mkdirs()
        return fallback
    }

    private fun isUsable(dir: File): Boolean {
        return try {
            val work = File(dir, ".open-transfer/incoming")
            if (!work.isDirectory && !work.mkdirs()) return false
            val stamp = System.nanoTime()
            val part = File(work, "probe-$stamp.part")
            part.writeBytes(byteArrayOf(0x4f, 0x54))
            val published = File(dir, "open-transfer-probe-$stamp.tmp")
            val renamed = part.renameTo(published)
            part.delete()
            published.delete()
            renamed
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Storage check failed for $dir", e)
            false
        }
    }

    /** The name the owner gave this device in Settings ("Galaxy Tab S9"), else the model. */
    fun deviceName(context: Context): String {
        val configured = try {
            Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME)
        } catch (e: Exception) {
            null
        }
        return configured?.trim()?.takeIf { it.isNotEmpty() } ?: Build.MODEL
    }

    fun deviceForm(context: Context): String =
        LocalPolicy.formFor(context.resources.configuration.smallestScreenWidthDp)
}
