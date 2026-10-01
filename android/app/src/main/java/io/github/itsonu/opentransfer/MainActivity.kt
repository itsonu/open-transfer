package io.github.itsonu.opentransfer

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.MimeTypeMap
import android.webkit.RenderProcessGoneDetail
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import kotlin.concurrent.thread

/**
 * A full-screen WebView showing the UI served by this device's own Python server, plus the
 * few things a web page can't do: pick files from anywhere, scan QR codes, open received
 * files in other apps, and notify while in the background (see [TransferService]).
 */
class MainActivity : ComponentActivity() {

    private sealed class Screen {
        data object Loading : Screen()
        data object Stopped : Screen()
        data class Failed(val message: String) : Screen()
        data class Page(val port: Int) : Screen()
    }

    private lateinit var root: ViewGroup
    private lateinit var webView: WebView
    private lateinit var loading: View
    private lateinit var progress: ProgressBar
    private lateinit var status: TextView
    private lateinit var action: Button

    private val main = Handler(Looper.getMainLooper())

    /** What is on screen (written on the main thread, read by the monitor thread). */
    @Volatile
    private var shown: Screen = Screen.Loading

    @Volatile
    private var monitoring = false
    private var monitor: Thread? = null

    private var destroyed = false
    private var webViewDead = false
    private var clearHistoryOnLoad = false
    private var askedPermissions = false
    private var waitingForPermission = false

    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var allowMultiple = true

    private val requestPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            // Whatever the answer: without storage access (Android 10) files go to the
            // app's own folder, without notifications offers only show inside the app.
            waitingForPermission = false
            startService()
        }

    private val pickFiles =
        registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            val callback = fileCallback
            fileCallback = null
            val chosen = if (allowMultiple) uris else uris.take(1)
            callback?.onReceiveValue(if (chosen.isEmpty()) null else chosen.toTypedArray())
        }

    private val backInWebView = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            if (!webViewDead && webView.canGoBack()) {
                webView.goBack()
            } else {
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContentView(R.layout.activity_main)
        root = findViewById(R.id.root)
        webView = findViewById(R.id.web)
        loading = findViewById(R.id.loading)
        progress = findViewById(R.id.progress)
        status = findViewById(R.id.status)
        action = findViewById(R.id.action)

        // Edge-to-edge: keep everything clear of the status/navigation bars, display
        // cutouts and the on-screen keyboard.
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or
                    WindowInsetsCompat.Type.displayCutout() or
                    WindowInsetsCompat.Type.ime(),
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        askedPermissions = savedInstanceState?.getBoolean(KEY_ASKED_PERMISSIONS) ?: false
        setUpWebView()
        onBackPressedDispatcher.addCallback(this, backInWebView)
        render(Screen.Loading)
    }

    override fun onStart() {
        super.onStart()
        TransferService.uiStarted()
        Notifications.clearOffers(this)
        if (!waitingForPermission) ensureStarted()
        startMonitor()
    }

    override fun onStop() {
        TransferService.uiStopped()
        stopMonitor()
        super.onStop()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean(KEY_ASKED_PERMISSIONS, askedPermissions)
    }

    override fun onDestroy() {
        destroyed = true
        stopMonitor()
        fileCallback?.onReceiveValue(null)
        fileCallback = null
        destroyWebView()
        super.onDestroy()
        // The service (and Python) keep running: this device stays visible to others.
    }

    // ------------------------------------------------------------------ service

    private fun missingPermissions(): List<String> {
        val wanted = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (Build.VERSION.SDK_INT == Build.VERSION_CODES.Q) {
            wanted += Manifest.permission.WRITE_EXTERNAL_STORAGE
        }
        return wanted.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
    }

    private fun ensureStarted() {
        val missing = missingPermissions()
        if (missing.isNotEmpty() && !askedPermissions) {
            askedPermissions = true
            requestPermissions.launch(missing.toTypedArray())
            if (Build.VERSION.SDK_INT == Build.VERSION_CODES.Q) {
                // Android 10 needs the storage answer before Python creates its folder.
                waitingForPermission = true
                return
            }
        }
        startService()
    }

    private fun startService() {
        val state = TransferService.state
        if (state == TransferService.State.STARTING || state == TransferService.State.RUNNING) return
        try {
            TransferService.start(this)
        } catch (e: Exception) {
            Log.e(LOG_TAG, "Couldn't start the service", e)
            render(Screen.Failed(e.message ?: e.javaClass.simpleName))
        }
    }

    /**
     * Watches the service from a background thread and switches between the native
     * loading/error screens and the web UI. Once the port is known it waits for
     * `/api/health` so the WebView never shows a connection error during startup.
     */
    private fun startMonitor() {
        if (monitor?.isAlive == true) return
        monitoring = true
        monitor = thread(name = "ot-monitor", isDaemon = true) {
            while (monitoring) {
                val wanted = wantedScreen()
                if (wanted != shown) main.post { render(wanted) }
                try {
                    Thread.sleep(if (shown is Screen.Page) 1000L else 250L)
                } catch (e: InterruptedException) {
                    break
                }
            }
        }
    }

    private fun stopMonitor() {
        monitoring = false
        monitor?.interrupt()
        monitor = null
    }

    private fun wantedScreen(): Screen {
        val state = TransferService.state
        val port = TransferService.port
        return when {
            state == TransferService.State.RUNNING && port > 0 -> {
                val current = shown
                if (current is Screen.Page && current.port == port) {
                    current
                } else if (isHealthy(port)) {
                    Screen.Page(port)
                } else {
                    Screen.Loading
                }
            }
            state == TransferService.State.FAILED -> Screen.Failed(TransferService.error.orEmpty())
            state == TransferService.State.STOPPED && TransferService.stoppedByUser -> Screen.Stopped
            else -> Screen.Loading
        }
    }

    private fun isHealthy(port: Int): Boolean =
        try {
            val connection = URL("http://127.0.0.1:$port/api/health").openConnection() as HttpURLConnection
            connection.connectTimeout = 1000
            connection.readTimeout = 2000
            connection.useCaches = false
            try {
                connection.responseCode == 200
            } finally {
                connection.disconnect()
            }
        } catch (e: IOException) {
            false
        }

    private fun render(screen: Screen) {
        if (destroyed || webViewDead) return
        if (screen is Screen.Page) {
            val current = shown
            if (current !is Screen.Page || current.port != screen.port) {
                clearHistoryOnLoad = true
                webView.loadUrl("http://127.0.0.1:${screen.port}/")
            }
            shown = screen
            webView.visibility = View.VISIBLE
            loading.visibility = View.GONE
            return
        }
        if (shown is Screen.Page) {
            webView.stopLoading()
            webView.loadUrl("about:blank")
        }
        shown = screen
        webView.visibility = View.INVISIBLE
        loading.visibility = View.VISIBLE
        when (screen) {
            is Screen.Failed -> {
                val detail = screen.message
                val text = if (detail.isBlank()) {
                    getString(R.string.start_failed)
                } else {
                    getString(R.string.start_failed) + "\n\n" + detail
                }
                showMessage(text, getString(R.string.retry))
            }
            Screen.Stopped -> showMessage(getString(R.string.stopped), getString(R.string.start_again))
            else -> showMessage(getString(R.string.starting), null)
        }
    }

    private fun showMessage(text: String, button: String?) {
        status.text = text
        progress.visibility = if (button == null) View.VISIBLE else View.GONE
        if (button == null) {
            action.visibility = View.GONE
            action.setOnClickListener(null)
        } else {
            action.text = button
            action.visibility = View.VISIBLE
            action.setOnClickListener {
                render(Screen.Loading)
                try {
                    TransferService.start(this)
                } catch (e: Exception) {
                    Log.e(LOG_TAG, "Couldn't start the service", e)
                }
            }
        }
    }

    // ------------------------------------------------------------------ web view

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun setUpWebView() {
        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            setSupportMultipleWindows(false)
            javaScriptCanOpenWindowsAutomatically = false
        }
        webView.addJavascriptInterface(Bridge(), "OpenTransferAndroid")
        webView.webViewClient = Client()
        webView.webChromeClient = Chrome()
        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            onDownload(url, contentDisposition, mimeType)
        }
    }

    private fun destroyWebView() {
        if (!::webView.isInitialized) return
        root.removeView(webView)
        webView.destroy()
    }

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
            if (request == null) return false
            val uri: Uri = request.url ?: return false
            val url = uri.toString()
            if (LocalPolicy.isLocalPage(url) || LocalPolicy.isInternalScheme(uri.scheme)) return false
            // Not this device's own UI: open in the browser (or the matching app), and
            // never inside this WebView, which has the native bridge.
            if (request.isForMainFrame) openExternally(uri)
            return true
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            if (clearHistoryOnLoad && LocalPolicy.isLocalPage(url)) {
                clearHistoryOnLoad = false
                view?.clearHistory()
            }
            backInWebView.isEnabled = view?.canGoBack() == true
        }

        override fun doUpdateVisitedHistory(view: WebView?, url: String?, isReload: Boolean) {
            backInWebView.isEnabled = view?.canGoBack() == true
        }

        override fun onReceivedError(
            view: WebView?,
            request: WebResourceRequest?,
            error: WebResourceError?,
        ) {
            if (request == null || !request.isForMainFrame) return
            if (LocalPolicy.isLocalPage(request.url?.toString())) {
                Log.w(LOG_TAG, "Loading the UI failed: ${error?.description}")
                // Back to the loading screen; the monitor reloads once /api/health answers.
                render(Screen.Loading)
            }
        }

        override fun onRenderProcessGone(view: WebView?, detail: RenderProcessGoneDetail?): Boolean {
            Log.e(LOG_TAG, "The WebView renderer went away (crashed: ${detail?.didCrash()})")
            if (view === webView) {
                webViewDead = true
                recreate() // fresh WebView; the service and Python keep running
            }
            return true
        }
    }

    private inner class Chrome : WebChromeClient() {
        override fun onShowFileChooser(
            view: WebView?,
            filePathCallback: ValueCallback<Array<Uri>>?,
            fileChooserParams: WebChromeClient.FileChooserParams?,
        ): Boolean {
            if (filePathCallback == null) return false
            fileCallback?.onReceiveValue(null)
            fileCallback = filePathCallback
            allowMultiple = fileChooserParams?.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE
            try {
                pickFiles.launch(mimeTypes(fileChooserParams?.acceptTypes))
            } catch (e: ActivityNotFoundException) {
                fileCallback = null
                filePathCallback.onReceiveValue(null)
                toast(R.string.no_file_picker)
            }
            return true
        }
    }

    // <input accept="image/png,.pdf"> → MIME types for the document picker ("*/*" if none).
    private fun mimeTypes(accept: Array<String>?): Array<String> {
        val types = accept.orEmpty()
            .flatMap { it.split(',') }
            .map { it.trim().lowercase(Locale.ROOT) }
            .filter { it.isNotEmpty() }
            .mapNotNull {
                if (it.startsWith(".")) {
                    MimeTypeMap.getSingleton().getMimeTypeFromExtension(it.substring(1))
                } else if ('/' in it) {
                    it
                } else {
                    null
                }
            }
            .distinct()
        return if (types.isEmpty()) arrayOf("*/*") else types.toTypedArray()
    }

    private fun onDownload(url: String?, contentDisposition: String?, mimeType: String?) {
        val own = LocalPolicy.ownFileName(url)
        if (own != null) {
            // The owner's files are already in Download/Open Transfer: just open it.
            openOwnFile(own)
            return
        }
        if (url == null || !URLUtil.isNetworkUrl(url)) {
            toast(R.string.download_failed)
            return
        }
        try {
            val name = URLUtil.guessFileName(url, contentDisposition, mimeType)
            val request = DownloadManager.Request(Uri.parse(url))
                .setTitle(name)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            if (!mimeType.isNullOrBlank()) request.setMimeType(mimeType)
            CookieManager.getInstance().getCookie(url)?.let { request.addRequestHeader("Cookie", it) }
            getSystemService(DownloadManager::class.java).enqueue(request)
            toast(R.string.download_started)
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Download failed for $url", e)
            toast(R.string.download_failed)
        }
    }

    private fun openExternally(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE))
        } catch (e: ActivityNotFoundException) {
            Log.w(LOG_TAG, "No app for $uri")
        }
    }

    // ------------------------------------------------------------------ bridge

    private fun openOwnFile(name: String?) {
        val safe = name?.takeIf { LocalPolicy.isSafeFileName(it) }
        if (safe == null) {
            Log.w(LOG_TAG, "Refusing to open '$name'")
            return
        }
        val file = File(TransferService.storageDir ?: Places.publicStorageDir(), safe)
        if (!file.isFile) {
            toast(R.string.file_missing)
            return
        }
        val uri = try {
            FileProvider.getUriForFile(this, "$packageName.files", file)
        } catch (e: IllegalArgumentException) {
            Log.w(LOG_TAG, "No FileProvider path for $file", e)
            toast(R.string.file_missing)
            return
        }
        val mime = MimeTypeMap.getSingleton()
            .getMimeTypeFromExtension(file.extension.lowercase(Locale.ROOT))
            ?: "application/octet-stream"
        val view = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            startActivity(view)
        } catch (e: ActivityNotFoundException) {
            val any = Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "*/*")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            try {
                startActivity(Intent.createChooser(any, null))
            } catch (e2: ActivityNotFoundException) {
                toast(R.string.no_app_for_file)
            }
        }
    }

    private fun startQrScan() {
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
        try {
            GmsBarcodeScanning.getClient(this, options)
                .startScan()
                .addOnSuccessListener { barcode -> deliverScan(barcode.rawValue) }
                .addOnCanceledListener { deliverScan(null) }
                .addOnFailureListener { e ->
                    Log.w(LOG_TAG, "QR scan failed", e)
                    deliverScan(null)
                }
        } catch (e: Exception) {
            Log.w(LOG_TAG, "QR scanner unavailable", e)
            deliverScan(null)
        }
    }

    private fun deliverScan(text: String?) {
        if (destroyed || webViewDead) return
        val detail = if (text == null) "null" else JSONObject.quote(text)
        webView.evaluateJavascript(
            "window.dispatchEvent(new CustomEvent('ot-native-scan',{detail:$detail}))",
            null,
        )
    }

    private fun toast(message: Int) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    /** `window.OpenTransferAndroid` — called by the web UI on a WebView binder thread. */
    inner class Bridge {
        @JavascriptInterface
        fun scanQr() {
            main.post { startQrScan() }
        }

        @JavascriptInterface
        fun openFile(name: String?) {
            main.post { openOwnFile(name) }
        }

        @JavascriptInterface
        fun platformInfo(): String =
            JSONObject()
                .put("form", Places.deviceForm(this@MainActivity))
                .put("model", Build.MODEL)
                .put("manufacturer", Build.MANUFACTURER)
                .put("sdk", Build.VERSION.SDK_INT)
                .toString()
    }

    private companion object {
        const val LOG_TAG = "OpenTransfer"
        const val KEY_ASKED_PERMISSIONS = "asked_permissions"
    }
}
