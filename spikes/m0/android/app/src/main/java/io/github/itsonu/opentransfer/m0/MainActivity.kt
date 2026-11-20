// M0 spike (throwaway): can this phone host a hotspot, join one, and talk Bluetooth LE?
// Every result goes to the screen and to logcat (tag OTM0) so a script can read it.
// Bluetooth speaks the same protocol as spikes/m0/mac/otble.swift; TCP the same as
// spikes/m0/tcp_speed.py (port 47900: "send" streams N bytes, the sink reports them).

@file:Suppress("DEPRECATION", "MissingPermission")

package io.github.itsonu.opentransfer.m0

import android.Manifest
import android.app.Activity
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.text.InputType
import android.util.Log
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.util.UUID
import kotlin.concurrent.thread

private val SERVICE = UUID.fromString("7f1c0000-6f70-656e-7472-616e73666572")
private val RX = UUID.fromString("7f1c0001-6f70-656e-7472-616e73666572")
private val TX = UUID.fromString("7f1c0002-6f70-656e-7472-616e73666572")
private val CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
private const val TCP_PORT = 47900
private const val BLE_BYTES = 262_144
private const val DOT = '.'.code.toByte()

class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var out: TextView
    private lateinit var ssid: EditText
    private lateinit var pass: EditText
    private lateinit var host: EditText
    private lateinit var megabytes: EditText

    private val wifi by lazy { applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager }
    private val cm by lazy { getSystemService(ConnectivityManager::class.java) }
    private val bt by lazy { getSystemService(BluetoothManager::class.java) }

    private var hotspot: WifiManager.LocalOnlyHotspotReservation? = null
    private var joined: ConnectivityManager.NetworkCallback? = null
    private var joinedGateway: String? = null
    private var sink: ServerSocket? = null

    // ------------------------------------------------------------------ screen

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        // Android 15+ draws edge to edge: keep the buttons clear of the title and nav bars.
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 360, 24, 240) }
        fun field(hint: String, value: String = "") =
            EditText(this).apply { this.hint = hint; setText(value); inputType = InputType.TYPE_CLASS_TEXT }.also(col::addView)
        fun button(label: String, action: () -> Unit) =
            col.addView(Button(this).apply { text = label; setOnClickListener { action() } })

        button("1. Ask for permissions") { askPermissions() }
        button("2. Capabilities") { capabilities() }
        button("3. Host LocalOnlyHotspot") { startHotspot() }
        button("   Stop hotspot") { hotspot?.close(); hotspot = null; say("hotspot stopped") }
        ssid = field("SSID to join")
        pass = field("passphrase")
        button("4. Join that network (Android's dialog)") { join(ssid.text.toString(), pass.text.toString()) }
        button("   Leave it") { leave() }
        host = field("TCP host (blank = gateway)")
        megabytes = field("MB to send", "100")
        button("5. TCP sink on :$TCP_PORT") { startSink() }
        button("6. TCP send to host / gateway") { send(host.text.toString(), megabytes.text.toString().toIntOrNull() ?: 100) }
        button("7. Bluetooth: advertise + GATT server") { bleServe() }
        button("8. Bluetooth: find OT-M0, measure") { bleConnect() }
        out = TextView(this).apply { setTextIsSelectable(true); textSize = 12f }
        col.addView(out)
        setContentView(ScrollView(this).apply { addView(col) })
        say("OT M0 spike on Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}), ${Build.MANUFACTURER} ${Build.MODEL}")
    }

    private fun say(text: String) {
        Log.i("OTM0", text)
        main.post { out.append("$text\n") }
    }

    private fun askPermissions() {
        val wanted = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 31) wanted += listOf(
            Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT)
        if (Build.VERSION.SDK_INT >= 33) wanted += Manifest.permission.NEARBY_WIFI_DEVICES
        else wanted += Manifest.permission.ACCESS_FINE_LOCATION
        requestPermissions(wanted.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        perms.zip(results.toList()).forEach { (p, r) -> say("permission ${p.substringAfterLast('.')}: ${if (r == 0) "granted" else "DENIED"}") }
    }

    // ------------------------------------------------------------------ what this phone can do

    private fun capabilities() {
        say("--- capabilities")
        say("Wi-Fi on: ${wifi.isWifiEnabled}; 5 GHz: ${wifi.is5GHzBandSupported}; Wi-Fi Direct: ${wifi.isP2pSupported}")
        if (Build.VERSION.SDK_INT >= 30) say("host while on Wi-Fi (STA+AP): ${wifi.isStaApConcurrencySupported}")
        if (Build.VERSION.SDK_INT >= 31) say("join while on Wi-Fi (STA+STA local-only): ${wifi.isStaConcurrencyForLocalOnlyConnectionsSupported}")
        val adapter = bt.adapter
        say("Bluetooth: ${if (adapter == null) "none" else "on=${adapter.isEnabled}"}; " +
            "advertising: ${adapter?.isMultipleAdvertisementSupported}; 2M PHY: ${adapter?.isLe2MPhySupported}")
        interfaces()
        cm.activeNetwork?.let { cm.getLinkProperties(it) }?.let { say("default network: ${describe(it)}") }
    }

    /** Java's interface list: does the app see its own addresses (hotspot, Wi-Fi, 464XLAT)? */
    private fun interfaces() {
        try {
            NetworkInterface.getNetworkInterfaces()?.toList().orEmpty().filter { it.isUp }.forEach { nif ->
                val v4 = nif.inetAddresses.toList().filterIsInstance<Inet4Address>().map { it.hostAddress }
                if (v4.isNotEmpty()) say("interface ${nif.name}: ${v4.joinToString()}")
            }
        } catch (e: Exception) {
            say("interface list refused: $e")
        }
    }

    private fun describe(lp: LinkProperties): String {
        val gw = lp.routes.firstOrNull { it.isDefaultRoute && it.gateway is Inet4Address }?.gateway?.hostAddress
        return "${lp.interfaceName} ${lp.linkAddresses.joinToString()} gateway=$gw"
    }

    // ------------------------------------------------------------------ hosting

    private fun startHotspot() {
        say("--- starting LocalOnlyHotspot")
        try {
            wifi.startLocalOnlyHotspot(object : WifiManager.LocalOnlyHotspotCallback() {
                override fun onStarted(reservation: WifiManager.LocalOnlyHotspotReservation) {
                    hotspot = reservation
                    val (name, key) = if (Build.VERSION.SDK_INT >= 30) {
                        reservation.softApConfiguration.let { it.ssid to it.passphrase }
                    } else {
                        reservation.wifiConfiguration?.let { it.SSID to it.preSharedKey } ?: (null to null)
                    }
                    say("HOTSPOT UP  ssid=$name  passphrase=$key")
                    say("Wi-Fi QR: WIFI:T:WPA;S:$name;P:$key;;")
                    main.postDelayed({ interfaces() }, 1500)
                }
                override fun onStopped() = say("hotspot stopped by the system")
                override fun onFailed(reason: Int) = say("HOTSPOT FAILED: ${
                    mapOf(1 to "no channel", 2 to "generic", 3 to "incompatible mode (your Mobile hotspot on?)",
                        4 to "tethering disallowed")[reason] ?: reason}")
            }, main)
        } catch (e: SecurityException) {
            say("HOTSPOT REFUSED (permission): ${e.message}")
        }
    }

    // ------------------------------------------------------------------ joining

    private fun join(name: String, key: String) {
        if (name.isBlank()) return say("type the SSID first")
        leave()
        say("--- joining $name (Android shows its Connect dialog)")
        val spec = WifiNetworkSpecifier.Builder().setSsid(name).apply { if (key.isNotEmpty()) setWpa2Passphrase(key) }.build()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(spec)
            .build()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                cm.bindProcessToNetwork(network)
                say("JOINED $name; this app's traffic now goes over it")
            }
            override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) {
                joinedGateway = lp.routes.firstOrNull { it.gateway is Inet4Address && !it.gateway!!.isAnyLocalAddress }
                    ?.gateway?.hostAddress ?: lp.dhcpServerAddress?.hostAddress
                say("joined network: ${describe(lp)} (gateway for TCP: $joinedGateway)")
            }
            override fun onUnavailable() = say("JOIN FAILED: declined in the dialog, or not found")
            override fun onLost(network: Network) = say("joined network lost")
        }
        joined = callback
        cm.requestNetwork(request, callback)
    }

    private fun leave() {
        joined?.let {
            cm.bindProcessToNetwork(null)
            cm.unregisterNetworkCallback(it)
            say("left the joined network")
        }
        joined = null
        joinedGateway = null
    }

    // ------------------------------------------------------------------ TCP speed

    private fun startSink() {
        if (sink != null) return say("sink already listening on :$TCP_PORT")
        thread(name = "sink", isDaemon = true) {
            try {
                ServerSocket(TCP_PORT).use { server ->
                    sink = server
                    say("TCP sink listening on :$TCP_PORT")
                    while (true) {
                        server.accept().use { s ->
                            val started = System.nanoTime()
                            val buf = ByteArray(256 * 1024)
                            var total = 0L
                            val input = s.getInputStream()
                            while (true) { val n = input.read(buf); if (n < 0) break; total += n }
                            val ms = (System.nanoTime() - started) / 1_000_000
                            say("RECEIVED ${total / 1_048_576} MB from ${s.inetAddress.hostAddress} in $ms ms = ${rate(total, ms)}")
                        }
                    }
                }
            } catch (e: Exception) {
                say("sink stopped: $e")
                sink = null
            }
        }
    }

    private fun send(target: String, mb: Int) {
        val to = target.ifBlank { joinedGateway ?: "" }
        if (to.isBlank()) return say("no host: type one, or join a network first (its gateway is used)")
        thread(name = "send", isDaemon = true) {
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress(to, TCP_PORT), 5000)
                    val buf = ByteArray(256 * 1024) { DOT }
                    val total = mb.toLong() * 1_048_576
                    val started = System.nanoTime()
                    var left = total
                    val output = s.getOutputStream()
                    while (left > 0) { val n = minOf(left, buf.size.toLong()).toInt(); output.write(buf, 0, n); left -= n }
                    output.flush()
                    s.shutdownOutput()
                    val ms = (System.nanoTime() - started) / 1_000_000
                    say("SENT $mb MB to $to in $ms ms = ${rate(total, ms)}")
                }
            } catch (e: Exception) {
                say("SEND to $to FAILED: $e")
            }
        }
    }

    private fun rate(bytes: Long, ms: Long) = "%.1f MB/s".format(bytes / 1_048_576.0 / (maxOf(ms, 1) / 1000.0))

    // ------------------------------------------------------------------ Bluetooth: serve

    private var server: BluetoothGattServer? = null
    private var subscriber: BluetoothDevice? = null
    private var mtu = 23
    private var expect = 0
    private var got = 0
    private var toSend = 0
    private var started = 0L

    private fun bleServe() {
        val adapter = bt.adapter ?: return say("no Bluetooth")
        val advertiser = adapter.bluetoothLeAdvertiser ?: return say("this phone can't advertise (no LE advertiser)")
        val tx = BluetoothGattCharacteristic(TX, BluetoothGattCharacteristic.PROPERTY_NOTIFY, BluetoothGattCharacteristic.PERMISSION_READ)
            .apply { addDescriptor(BluetoothGattDescriptor(CCCD, BluetoothGattDescriptor.PERMISSION_READ or BluetoothGattDescriptor.PERMISSION_WRITE)) }
        val rx = BluetoothGattCharacteristic(RX,
            BluetoothGattCharacteristic.PROPERTY_WRITE or BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
            BluetoothGattCharacteristic.PERMISSION_WRITE)
        val service = BluetoothGattService(SERVICE, BluetoothGattService.SERVICE_TYPE_PRIMARY).apply {
            addCharacteristic(rx); addCharacteristic(tx)
        }
        server = bt.openGattServer(this, object : BluetoothGattServerCallback() {
            override fun onMtuChanged(device: BluetoothDevice, value: Int) { mtu = value; say("MTU $value") }
            override fun onConnectionStateChange(device: BluetoothDevice, status: Int, state: Int) =
                say(if (state == BluetoothProfile.STATE_CONNECTED) "BLE connected: ${device.address}" else "BLE disconnected")
            override fun onDescriptorWriteRequest(device: BluetoothDevice, id: Int, d: BluetoothGattDescriptor,
                                                  prepared: Boolean, needs: Boolean, offset: Int, value: ByteArray) {
                subscriber = device
                if (needs) server?.sendResponse(device, id, BluetoothGatt.GATT_SUCCESS, 0, null)
                say("subscribed")
            }
            override fun onCharacteristicWriteRequest(device: BluetoothDevice, id: Int, c: BluetoothGattCharacteristic,
                                                      prepared: Boolean, needs: Boolean, offset: Int, value: ByteArray) {
                if (needs) server?.sendResponse(device, id, BluetoothGatt.GATT_SUCCESS, 0, null)
                handle(device, tx, value)
            }
            override fun onNotificationSent(device: BluetoothDevice, status: Int) = pump(device, tx)
        })
        server?.addService(service)
        advertiser.startAdvertising(
            AdvertiseSettings.Builder().setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY).setConnectable(true).build(),
            AdvertiseData.Builder().addServiceUuid(ParcelUuid(SERVICE)).build(),
            AdvertiseData.Builder().setIncludeDeviceName(true).build(),
            object : AdvertiseCallback() {
                override fun onStartSuccess(settings: AdvertiseSettings) = say("advertising OT-M0 service")
                override fun onStartFailure(code: Int) = say("ADVERTISING FAILED: $code")
            })
    }

    private fun handle(device: BluetoothDevice, tx: BluetoothGattCharacteristic, value: ByteArray) {
        when (value.firstOrNull()) {
            'S'.code.toByte() -> { expect = String(value, 1, value.size - 1).toInt(); got = 0; started = System.nanoTime(); say("receiving $expect bytes") }
            'P'.code.toByte() -> { toSend = String(value, 1, value.size - 1).toInt(); started = System.nanoTime(); say("sending $toSend bytes"); pump(device, tx) }
            else -> if (expect > 0) {
                got += value.size
                if (got >= expect) {
                    val ms = (System.nanoTime() - started) / 1_000_000
                    say("BLE RECEIVED $got bytes in $ms ms = ${"%.1f KB/s".format(got / 1024.0 / (maxOf(ms, 1) / 1000.0))}")
                    tx.value = "R$got:$ms".toByteArray()
                    server?.notifyCharacteristicChanged(device, tx, false)
                    expect = 0
                }
            }
        }
    }

    /** One notification at a time: Android calls onNotificationSent when it may send the next. */
    private fun pump(device: BluetoothDevice, tx: BluetoothGattCharacteristic) {
        if (toSend <= 0) return
        val n = minOf(mtu - 3, 512, toSend)
        tx.value = ByteArray(n) { DOT }
        if (server?.notifyCharacteristicChanged(device, tx, false) == true) toSend -= n
        if (toSend <= 0) say("sent all")
    }

    // ------------------------------------------------------------------ Bluetooth: connect and measure

    private fun bleConnect() {
        val scanner = bt.adapter?.bluetoothLeScanner ?: return say("no Bluetooth scanner")
        say("--- scanning for OT-M0")
        val callback = object : ScanCallback() {
            override fun onScanResult(type: Int, result: ScanResult) {
                scanner.stopScan(this)
                say("found ${result.device.address}, signal ${result.rssi} dBm")
                measure(result.device)
            }
            override fun onScanFailed(code: Int) = say("SCAN FAILED: $code")
        }
        scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE)).build()),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), callback)
    }

    private fun measure(device: BluetoothDevice) {
        var rx: BluetoothGattCharacteristic? = null
        var phase = 0  // 1 upload, 2 download
        var left = 0
        var received = 0
        var mtuNow = 23
        var t0 = 0L
        fun kbs(bytes: Int) = "%.1f KB/s".format(bytes / 1024.0 / (maxOf((System.nanoTime() - t0) / 1_000_000, 1) / 1000.0))
        device.connectGatt(this, false, object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, status: Int, state: Int) {
                if (state == BluetoothProfile.STATE_CONNECTED) { say("connected"); g.requestMtu(517) }
                else { say("disconnected (status $status)"); g.close() }
            }
            override fun onMtuChanged(g: BluetoothGatt, value: Int, status: Int) { mtuNow = value; say("MTU $value"); g.discoverServices() }
            override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
                val s = g.getService(SERVICE) ?: return say("no OT-M0 service")
                rx = s.getCharacteristic(RX)
                val tx = s.getCharacteristic(TX)
                g.setCharacteristicNotification(tx, true)
                tx.getDescriptor(CCCD).also { it.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE; g.writeDescriptor(it) }
            }
            override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
                phase = 1; left = BLE_BYTES
                rx!!.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                rx!!.value = "S$BLE_BYTES".toByteArray()
                g.writeCharacteristic(rx)
            }
            override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
                if (phase != 1 || left != BLE_BYTES) return
                t0 = System.nanoTime()  // the "S" header landed: stream the data, retrying while the stack is busy
                thread(name = "ble-upload", isDaemon = true) {
                    val chunk = ByteArray(minOf(mtuNow - 3, 512)) { DOT }  // Android: attributes max 512 bytes
                    var busy = 0
                    while (left > 0 && phase == 1) {
                        val n = minOf(chunk.size, left)
                        val ok = if (Build.VERSION.SDK_INT >= 33) {
                            g.writeCharacteristic(c, chunk.copyOf(n), BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) == 0
                        } else {
                            c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                            c.value = chunk.copyOf(n)
                            g.writeCharacteristic(c)
                        }
                        if (ok) left -= n else { busy++; Thread.sleep(2) }
                    }
                    say("upload queued (stack busy $busy times)")
                }
            }
            override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
                val v = c.value ?: return
                if (phase == 1 && v.firstOrNull() == 'R'.code.toByte()) {
                    say("BLE UPLOAD $BLE_BYTES bytes = ${kbs(BLE_BYTES)} (other side: ${String(v)})")
                    phase = 2; received = 0; t0 = System.nanoTime()
                    rx!!.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                    rx!!.value = "P$BLE_BYTES".toByteArray()
                    g.writeCharacteristic(rx)
                } else if (phase == 2) {
                    received += v.size
                    if (received >= BLE_BYTES) { say("BLE DOWNLOAD $received bytes = ${kbs(received)}"); phase = 3; g.disconnect() }
                }
            }
        }, BluetoothDevice.TRANSPORT_LE)
    }

    override fun onDestroy() {
        hotspot?.close()
        leave()
        server?.close()
        sink?.close()
        super.onDestroy()
    }
}
