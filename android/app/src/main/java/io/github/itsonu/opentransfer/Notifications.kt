package io.github.itsonu.opentransfer

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.text.format.Formatter
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

object Notifications {
    const val CHANNEL_SERVICE = "service"
    const val CHANNEL_OFFERS = "offers"
    const val SERVICE_ID = 1
    private const val OFFER_TIMEOUT_MS = 120_000L // offers expire on the sender after 2 min

    private val shownOffers: MutableSet<Int> = ConcurrentHashMap.newKeySet()

    fun ensureChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val service = NotificationChannel(
            CHANNEL_SERVICE,
            context.getString(R.string.channel_service),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = context.getString(R.string.channel_service_description)
            setShowBadge(false)
        }
        val offers = NotificationChannel(
            CHANNEL_OFFERS,
            context.getString(R.string.channel_offers),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = context.getString(R.string.channel_offers_description)
        }
        manager.createNotificationChannels(listOf(service, offers))
    }

    fun canNotify(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    private fun openApp(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(
            context,
            0,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    /** The persistent notification of [TransferService], with a "Stop" action. */
    fun service(context: Context): Notification {
        val stop = PendingIntent.getService(
            context,
            1,
            Intent(context, TransferService::class.java).setAction(TransferService.ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_stat_transfer)
            .setColor(ContextCompat.getColor(context, R.color.brand))
            .setContentTitle(context.getString(R.string.service_title))
            .setContentText(context.getString(R.string.service_text))
            .setContentIntent(openApp(context))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .addAction(0, context.getString(R.string.action_stop), stop)
            .build()
    }

    /** "<name> wants to send you files" — `json` is the Python side's incoming-offer view. */
    @SuppressLint("MissingPermission") // checked by canNotify()
    fun showOffer(context: Context, json: String) {
        if (!canNotify(context)) return
        val offer = JSONObject(json)
        val sender = offer.optJSONObject("from")?.optString("name")?.takeIf { it.isNotBlank() }
            ?: context.getString(R.string.someone)
        val count = offer.optJSONArray("files")?.length() ?: 0
        val size = Formatter.formatShortFileSize(context, offer.optLong("total"))
        val id = 1000 + (offer.optString("id").hashCode() and 0x7fffffff) % 1_000_000
        val notification = NotificationCompat.Builder(context, CHANNEL_OFFERS)
            .setSmallIcon(R.drawable.ic_stat_transfer)
            .setColor(ContextCompat.getColor(context, R.color.brand))
            .setContentTitle(context.getString(R.string.offer_title, sender))
            .setContentText(
                context.resources.getQuantityString(R.plurals.offer_text, count, count, size),
            )
            .setContentIntent(openApp(context))
            .setAutoCancel(true)
            .setTimeoutAfter(OFFER_TIMEOUT_MS)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        NotificationManagerCompat.from(context).notify(id, notification)
        shownOffers.add(id)
    }

    /** The app is on screen now: the web UI shows the offers itself. */
    fun clearOffers(context: Context) {
        val manager = NotificationManagerCompat.from(context)
        for (id in shownOffers.toList()) {
            manager.cancel(id)
            shownOffers.remove(id)
        }
    }
}
