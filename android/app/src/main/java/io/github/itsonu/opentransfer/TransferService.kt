package io.github.itsonu.opentransfer

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * Owns the Python device (HTTP server + discovery) for as long as this device should be
 * visible to others — independent of the activity, so rotating the screen or leaving the
 * app doesn't restart anything.
 *
 * State is published through the companion object; [MainActivity] polls it.
 */
class TransferService : Service() {

    enum class State { STOPPED, STARTING, RUNNING, STOPPING, FAILED }

    companion object {
        private const val LOG_TAG = "OpenTransfer"
        const val ACTION_START = "io.github.itsonu.opentransfer.action.START"
        const val ACTION_STOP = "io.github.itsonu.opentransfer.action.STOP"

        @Volatile
        var state: State = State.STOPPED
            private set

        /** HTTP port on 127.0.0.1 while [state] is RUNNING, else 0. */
        @Volatile
        var port: Int = 0
            private set

        /** Why the last start failed (for the activity's error screen). */
        @Volatile
        var error: String? = null
            private set

        /** True after the owner pressed "Stop" (or Android ended the service). */
        @Volatile
        var stoppedByUser: Boolean = false
            private set

        /** The folder received files are saved to (known once the service has started). */
        @Volatile
        var storageDir: File? = null
            private set

        private val visibleScreens = AtomicInteger(0)
        private val pythonLock = Any()

        val isUiVisible: Boolean
            get() = visibleScreens.get() > 0

        fun uiStarted() {
            visibleScreens.incrementAndGet()
        }

        fun uiStopped() {
            visibleScreens.updateAndGet { if (it > 0) it - 1 else 0 }
        }

        /** Start (or keep) the service; safe to call repeatedly. Call from the foreground. */
        fun start(context: Context) {
            val intent = Intent(context, TransferService::class.java).setAction(ACTION_START)
            ContextCompat.startForegroundService(context, intent)
        }

        private fun module() = Python.getInstance().getModule("open_transfer.android")

        private fun stopPython() {
            try {
                synchronized(pythonLock) {
                    if (Python.isStarted()) module().callAttr("stop")
                }
            } catch (t: Throwable) {
                Log.w(LOG_TAG, "Stopping Open Transfer failed", t)
            }
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var multicastLock: WifiManager.MulticastLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var restartAfterStop = false
    private var destroyed = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Notifications.ensureChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopByUser()
            return START_NOT_STICKY
        }
        // Started with startForegroundService(): we must call startForeground() promptly.
        if (!goForeground()) {
            stopSelf()
            return START_NOT_STICKY
        }
        when (state) {
            State.STARTING, State.RUNNING -> Unit
            State.STOPPING -> restartAfterStop = true
            State.STOPPED, State.FAILED -> launch()
        }
        return START_NOT_STICKY
    }

    /** Android 15+: dataSync services get about 6 hours a day in the background. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(LOG_TAG, "Android ended the background time for today; stopping")
        restartAfterStop = false
        stoppedByUser = true
        finishService() // onDestroy() stops Python
    }

    override fun onDestroy() {
        destroyed = true
        releaseLocks()
        if (state == State.STARTING || state == State.RUNNING) {
            state = State.STOPPING
            thread(name = "ot-python-stop", isDaemon = true) {
                stopPython()
                main.post {
                    port = 0
                    state = State.STOPPED
                }
            }
        }
        super.onDestroy()
    }

    private fun goForeground(): Boolean =
        try {
            ServiceCompat.startForeground(
                this,
                Notifications.SERVICE_ID,
                Notifications.service(this),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
            true
        } catch (e: Exception) {
            // e.g. ForegroundServiceStartNotAllowedException once the daily limit is used up.
            Log.e(LOG_TAG, "Couldn't start the foreground service", e)
            error = e.message ?: e.javaClass.simpleName
            state = State.FAILED
            false
        }

    private fun launch() {
        state = State.STARTING
        error = null
        port = 0
        stoppedByUser = false
        acquireLocks()
        val app = applicationContext
        thread(name = "ot-python-start", isDaemon = true) {
            try {
                val storage = Places.chooseStorageDir(app)
                storageDir = storage
                val stateDir = Places.stateDir(app).apply { mkdirs() }
                val listening = synchronized(pythonLock) {
                    if (!Python.isStarted()) Python.start(AndroidPlatform(app))
                    module().callAttr(
                        "start",
                        storage.absolutePath,
                        stateDir.absolutePath,
                        Places.deviceName(app),
                        Places.deviceForm(app),
                        PythonEvents(app),
                    ).toInt()
                }
                Log.i(LOG_TAG, "Open Transfer is listening on port $listening; files go to $storage")
                main.post {
                    if (state == State.STARTING) {
                        port = listening
                        state = State.RUNNING
                    }
                }
            } catch (t: Throwable) {
                Log.e(LOG_TAG, "Couldn't start Open Transfer", t)
                main.post {
                    if (state == State.STARTING) {
                        error = (t.message ?: t.javaClass.simpleName).take(600)
                        state = State.FAILED
                        if (!destroyed) finishService()
                    }
                }
            }
        }
    }

    private fun stopByUser() {
        restartAfterStop = false
        stoppedByUser = true
        when (state) {
            State.STOPPING -> Unit
            State.STARTING, State.RUNNING -> {
                state = State.STOPPING
                thread(name = "ot-python-stop", isDaemon = true) {
                    stopPython()
                    main.post {
                        port = 0
                        state = State.STOPPED
                        if (destroyed) return@post
                        if (restartAfterStop) {
                            restartAfterStop = false
                            launch()
                        } else {
                            finishService()
                        }
                    }
                }
            }
            State.STOPPED, State.FAILED -> {
                state = State.STOPPED
                finishService()
            }
        }
    }

    private fun finishService() {
        releaseLocks()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // Multicast packets are filtered out on most phones unless an app holds this lock,
    // which would make nearby-device discovery silently fail.
    private fun acquireLocks() {
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager ?: return
        try {
            val multicast = multicastLock ?: wifi.createMulticastLock("OpenTransfer:discovery").also {
                it.setReferenceCounted(false)
                multicastLock = it
            }
            if (!multicast.isHeld) multicast.acquire()
            val lock = wifiLock ?: createWifiLock(wifi).also {
                it.setReferenceCounted(false)
                wifiLock = it
            }
            if (!lock.isHeld) lock.acquire()
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't acquire Wi-Fi locks", e)
        }
    }

    @Suppress("DEPRECATION") // WIFI_MODE_FULL_LOW_LATENCY only applies while on screen.
    private fun createWifiLock(wifi: WifiManager): WifiManager.WifiLock =
        wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "OpenTransfer:transfers")

    private fun releaseLocks() {
        try {
            multicastLock?.let { if (it.isHeld) it.release() }
            wifiLock?.let { if (it.isHeld) it.release() }
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Couldn't release Wi-Fi locks", e)
        }
    }
}
