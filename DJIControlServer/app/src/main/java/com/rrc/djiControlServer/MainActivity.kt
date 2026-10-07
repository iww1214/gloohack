package com.rrc.djiControlServer

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.wifi.WifiManager
import android.os.Bundle
import android.text.format.Formatter
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView
import android.view.SurfaceHolder
import android.view.SurfaceView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import dji.sdk.keyvalue.key.ProductKey
import dji.sdk.keyvalue.key.BatteryKey
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.v5.et.create
import dji.v5.et.listen
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.manager.SDKManager
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.datacenter.camera.StreamInfo
import dji.v5.manager.interfaces.ICameraStreamManager
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.interfaces.SDKManagerCallback
import io.ktor.application.call
import io.ktor.http.ContentType
import io.ktor.response.respondText
import io.ktor.routing.get
import io.ktor.routing.routing
import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty

class MainActivity : AppCompatActivity() {
    companion object {
        private const val PORT = 8080
        private const val REQUEST_CODE = 10
    }

    private lateinit var connectionText: TextView
    private lateinit var streamText: TextView
    private var productId: Int? = null
    private var flightControllerConnected = false
    // True while the aircraft link is up. The KeyConnection listener sticks false after a reconnect,
    // so any live telemetry reading also sets this; only a real product disconnect clears it.
    private var linkUp = false
    private var productName = "DJI Mini 4 Pro"
    private var batteryPct: Int? = null
    private var altitudeM: Double? = null
    private var headingDeg: Double? = null
    private var latitudeDeg: Double? = null
    private var longitudeDeg: Double? = null
    private var telemetryUpdatedAtMs: Long? = null

    // ── Live video publishing ────────────────────────────────────────────
    private var rtmpPublisher: RtmpPublisher? = null
    private var streamState = "idle"
    private var framesReceived = 0L
    private var spsBuf: ByteArray? = null
    private var ppsBuf: ByteArray? = null
    private var lastNalWasSpsPps = false

    private var rawFrames = 0L
    private var encodedFrames = 0L
    private var encoder: FrameEncoder? = null
    private var fallbackStarted = false

    // Raw H.264 Annex B stream from CameraStreamManager (preferred path)
    private val rawStreamListener = object : ICameraStreamManager.ReceiveStreamListener {
        override fun onReceiveStream(data: ByteArray, offset: Int, length: Int, info: StreamInfo) {
            rawFrames++
            framesReceived++
            if (info.mimeType != ICameraStreamManager.MimeType.H264) {
                if (rawFrames % 90L == 1L) setStreamDetail("raw stream is ${info.mimeType}, not H264; waiting for NV21 fallback")
                return
            }
            if (rawFrames % 90L == 1L) {
                setStreamDetail("raw frames=$rawFrames key=${info.isKeyFrame} ${info.width}x${info.height}")
            }
            val payload = data.copyOfRange(offset, offset + length)
            rtmpPublisher?.onVideoFrame(payload, info.isKeyFrame, null, null, info.presentationTimeMs * 1000)
        }
    }

    // Decoded NV21 frames, re-encoded to H.264 on the phone (fallback path)
    private val nv21Listener = object : ICameraStreamManager.CameraFrameListener {
        override fun onFrame(
            frameData: ByteArray, offset: Int, length: Int, width: Int, height: Int,
            format: ICameraStreamManager.FrameFormat
        ) {
            val enc = encoder ?: return
            try {
                enc.encode(frameData, offset, length, width, height)
            } catch (e: Exception) {
                setStreamDetail("encode error: $e")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(createStatusView())
        requestPermissionsIfNeeded()
        startServer()
        initializeSdk()
        listenForProductType()
    }

    override fun onDestroy() {
        try {
            val m = MediaDataCenter.getInstance().cameraStreamManager
            m.removeReceiveStreamListener(rawStreamListener)
            m.removeFrameListener(nv21Listener)
        } catch (_: Exception) { }
        encoder?.stop()
        rtmpPublisher?.stop()
        super.onDestroy()
    }

    // ── Video streaming via CameraStreamManager raw H.264 → our RtmpPublisher ──
    private var attachAttempts = 0
    private var attachStarted = false
    private var listenerAttached = false
    @Volatile private var streamDetail = ""

    private fun setStreamDetail(msg: String) {
        streamDetail = msg
        android.util.Log.w("S1271Stream", msg)
    }

    private var previewSurface: android.view.Surface? = null
    private var previewW = 0
    private var previewH = 0

    // Binding a display surface makes the SDK start decoding the camera stream
    private fun putPreviewSurface() {
        val s = previewSurface ?: return
        if (previewW <= 0 || previewH <= 0) return
        try {
            MediaDataCenter.getInstance().cameraStreamManager.putCameraStreamSurface(
                ComponentIndexType.LEFT_OR_MAIN, s, previewW, previewH,
                ICameraStreamManager.ScaleType.CENTER_INSIDE
            )
            setStreamDetail("preview surface bound ${previewW}x$previewH")
        } catch (e: Exception) {
            setStreamDetail("preview surface not ready: ${e.message}")
        }
    }

    private fun startVideoAttachOnce() {
        if (attachStarted) return
        attachStarted = true
        updateStreamStatus("Stream: starting video attach")
        attachVideoFeed()
        watchFrames()
    }

    private var watchingFrames = false
    private var lastWatchedFrames = -1L

    // Decoded frames only flow while a surface is bound; every 5 s with the link up and no new frames, re-enable and rebind.
    private fun watchFrames() {
        if (watchingFrames) return
        watchingFrames = true
        val tick = object : Runnable {
            override fun run() {
                if (linkUp && framesReceived == lastWatchedFrames) {
                    try {
                        MediaDataCenter.getInstance().cameraStreamManager.enableStream(ComponentIndexType.LEFT_OR_MAIN, true)
                        putPreviewSurface()
                        setStreamDetail("watchFrames: no new frames, re-enabled stream and rebound surface")
                    } catch (e: Exception) {
                        setStreamDetail("watchFrames: ${e.message}")
                    }
                }
                lastWatchedFrames = framesReceived
                connectionText.postDelayed(this, 5000)
            }
        }
        connectionText.postDelayed(tick, 5000)
    }

    private fun attachVideoFeed() {
        try {
            attachAttempts++
            val mgr = MediaDataCenter.getInstance().cameraStreamManager
            if (!listenerAttached) {
                mgr.addReceiveStreamListener(ComponentIndexType.LEFT_OR_MAIN, rawStreamListener)
                listenerAttached = true
                setStreamDetail("raw listener attached on LEFT_OR_MAIN")
            }
            mgr.enableStream(ComponentIndexType.LEFT_OR_MAIN, true)
            putPreviewSurface()
            updateStreamStatus("Stream: camera stream enabled")
            ensurePublisher()
            connectionText.postDelayed({ startFallbackIfNoRaw() }, 8000)
        } catch (e: Exception) {
            setStreamDetail("attach exception: $e")
            if (!listenerAttached) {
                updateStreamStatus("Stream: ${e.message} - retrying")
                connectionText.postDelayed({ attachVideoFeed() }, 3000)
            }
        }
    }

    private fun startFallbackIfNoRaw() {
        if (rawFrames > 0L || fallbackStarted) return
        fallbackStarted = true
        try {
            encoder = FrameEncoder { annexB, isKey, ptsUs ->
                encodedFrames++
                framesReceived++
                if (encodedFrames % 90L == 1L) setStreamDetail("encoded frames=$encodedFrames key=$isKey")
                rtmpPublisher?.onVideoFrame(annexB, isKey, null, null, ptsUs)
            }
            val mgr = MediaDataCenter.getInstance().cameraStreamManager
            mgr.addFrameListener(
                ComponentIndexType.LEFT_OR_MAIN,
                ICameraStreamManager.FrameFormat.NV21,
                nv21Listener
            )
            setStreamDetail("no raw frames; NV21 + MediaCodec fallback started")
            updateStreamStatus("Stream: fallback NV21 encoder")
        } catch (e: Exception) {
            fallbackStarted = false
            setStreamDetail("fallback exception: $e")
            updateStreamStatus("Stream: fallback failed ${e.message}")
        }
    }

    // Known laptop addresses on the drone hotspot; the app tries each so no address has to be typed.
    private val laptopCandidates = listOf("172.20.10.3", "172.20.10.4", "172.20.10.5")
    private var laptopHost: String? = null
    private var laptopLookupStarted = false

    private fun ensurePublisher() {
        if (rtmpPublisher != null) return
        val host = laptopHost
        if (host == null) {
            if (!laptopLookupStarted) {
                laptopLookupStarted = true
                Thread({
                    while (laptopHost == null) {
                        for (candidate in laptopCandidates) {
                            try {
                                java.net.Socket(candidate, 1935).use { }
                                laptopHost = candidate
                                runOnUiThread { streamText.text = "Stream: laptop found at $candidate" }
                                ensurePublisher()
                                return@Thread
                            } catch (_: Exception) { }
                        }
                        try { Thread.sleep(3000) } catch (_: InterruptedException) { return@Thread }
                    }
                }, "laptopLookup").apply { isDaemon = true }.start()
            }
            return
        }
        val user = getString(R.string.rtmp_user)
        val pass = getString(R.string.rtmp_pass)
        val (h, port, key) = buildRtmpUrl(host, 1935, user, pass)
        rtmpPublisher = RtmpPublisher(h, port, "live", key) { state ->
            streamState = state
            updateStreamStatus("Stream: $state")
        }.also { it.start() }
    }

    private fun updateStreamStatus(message: String) {
        runOnUiThread { streamText.text = message }
    }

    private fun createStatusView(): LinearLayout {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(32, 32, 32, 32)
            setBackgroundColor(Color.BLACK)
        }
        val title = TextView(this).apply {
            text = "DJI Control Server (MSDK V5)"
            textSize = 22f
            setTextColor(Color.WHITE)
        }
        connectionText = TextView(this).apply {
            text = "Registering SDK..."
            textSize = 18f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            setPadding(0, 24, 0, 24)
        }
        val ip = TextView(this).apply {
            text = "Control Server IP: ${deviceIp()}:$PORT"
            textSize = 16f
            setTextColor(Color.LTGRAY)
        }
        layout.addView(title)
        layout.addView(connectionText)
        layout.addView(ip)
        streamText = TextView(this).apply {
            text = "Stream: idle"
            textSize = 14f
            setTextColor(Color.LTGRAY)
        }
        layout.addView(streamText)
        val preview = android.view.SurfaceView(this).apply {
            layoutParams = LinearLayout.LayoutParams(640, 360)
            holder.addCallback(object : android.view.SurfaceHolder.Callback {
                override fun surfaceCreated(h: android.view.SurfaceHolder) = Unit
                override fun surfaceChanged(h: android.view.SurfaceHolder, format: Int, w: Int, ht: Int) {
                    previewSurface = h.surface
                    previewW = w
                    previewH = ht
                    putPreviewSurface()
                }
                override fun surfaceDestroyed(h: android.view.SurfaceHolder) {
                    try {
                        MediaDataCenter.getInstance().cameraStreamManager.removeCameraStreamSurface(h.surface)
                    } catch (_: Exception) { }
                    previewSurface = null
                }
            })
        }
        layout.addView(preview)
        return layout
    }

    private fun initializeSdk() {
        SDKManager.getInstance().init(applicationContext, object : SDKManagerCallback {
            override fun onRegisterSuccess() {
                listenForTelemetry()
                updateStatus("SDK registered; waiting for aircraft")
            }

            override fun onRegisterFailure(error: IDJIError) =
                updateStatus("SDK registration failed: ${error.description()}")

            override fun onProductConnect(productId: Int) {
                this@MainActivity.productId = productId
                flightControllerConnected = true
                linkUp = true
                updateStatus("Connected to $productName")
                startVideoAttachOnce()
            }

            override fun onProductDisconnect(productId: Int) {
                if (this@MainActivity.productId == productId) this@MainActivity.productId = null
                flightControllerConnected = false
                linkUp = false
                attachStarted = false   // so the next connect re-attaches the video feed
                updateStatus("Aircraft disconnected")
            }

            override fun onProductChanged(productId: Int) = updateStatus("Product changed: $productId")

            override fun onInitProcess(event: DJISDKInitEvent, totalProcess: Int) {
                if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                    SDKManager.getInstance().registerApp()
                }
            }

            override fun onDatabaseDownloadProgress(current: Long, total: Long) = Unit
        })
    }

    private fun listenForProductType() {
        ProductKey.KeyProductType.create().listen(this) { type ->
            if (type != null) {
                val detectedName = type.name.replace('_', ' ')
                if (!type.name.equals("UNKNOWN", ignoreCase = true)) {
                    productName = detectedName
                }
                if (productId != null) updateStatus("Connected to $productName")
            }
        }
    }

    private fun listenForTelemetry() {
        BatteryKey.KeyChargeRemainingInPercent.create().listen(this) { value ->
            batteryPct = value
            telemetryUpdatedAtMs = System.currentTimeMillis()
            if (value != null) linkUp = true
        }
        FlightControllerKey.KeyAltitude.create().listen(this) { value ->
            altitudeM = value
            telemetryUpdatedAtMs = System.currentTimeMillis()
            if (value != null) linkUp = true
        }
        FlightControllerKey.KeyConnection.create().listen(this) { connected ->
            flightControllerConnected = connected == true
            if (flightControllerConnected) {
                linkUp = true
                if (productId != null) updateStatus("Connected to $productName")
                startVideoAttachOnce()
            }
            // A false here is unreliable (sticks after a reconnect); the poll and live readings keep linkUp honest.
        }
        FlightControllerKey.KeyCompassHeading.create().listen(this) { value ->
            headingDeg = value
            telemetryUpdatedAtMs = System.currentTimeMillis()
            if (value != null) linkUp = true
        }
        startTelemetryPoll()
    }

    private var telemetryPolling = false

    // Change listeners stay silent on a stationary aircraft, so also read current values every second
    private fun startTelemetryPoll() {
        if (telemetryPolling) return
        telemetryPolling = true
        val tick = object : Runnable {
            override fun run() {
                if (linkUp) {
                    try {
                        val km = dji.v5.manager.KeyManager.getInstance()
                        val alt = km.getValue(dji.sdk.keyvalue.key.KeyTools.createKey(FlightControllerKey.KeyAltitude))
                        val hdg = km.getValue(dji.sdk.keyvalue.key.KeyTools.createKey(FlightControllerKey.KeyCompassHeading))
                        val bat = km.getValue(dji.sdk.keyvalue.key.KeyTools.createKey(BatteryKey.KeyChargeRemainingInPercent))
                        if (alt != null) altitudeM = alt
                        if (hdg != null) headingDeg = hdg
                        if (bat != null) batteryPct = bat
                        if (alt != null || hdg != null || bat != null) linkUp = true
                        // No GPS fix reads as 0,0 or NaN; only keep a real position
                        val loc = km.getValue(dji.sdk.keyvalue.key.KeyTools.createKey(FlightControllerKey.KeyAircraftLocation3D))
                        if (loc != null && loc.latitude.isFinite() && loc.longitude.isFinite() &&
                            (Math.abs(loc.latitude) > 0.001 || Math.abs(loc.longitude) > 0.001)) {
                            latitudeDeg = loc.latitude
                            longitudeDeg = loc.longitude
                        }
                        if (alt != null || hdg != null || bat != null) telemetryUpdatedAtMs = System.currentTimeMillis()
                    } catch (e: Exception) {
                        setStreamDetail("telemetry poll error: $e")
                    }
                }
                connectionText.postDelayed(this, 1000)
            }
        }
        connectionText.postDelayed(tick, 1000)
    }

    private fun startServer() {
        embeddedServer(Netty, PORT) {
            routing {
                get("/") {
                    call.respondText("Connected", ContentType.Text.Plain)
                }
                get("/status") {
                    val connected = linkUp
                    val telemetryAvailable = batteryPct != null || altitudeM != null
                    call.respondText(
                        "{\"status\":\"ok\",\"sdk\":\"5.18.0\",\"aircraft_connected\":$connected,\"product_id\":${productId ?: "null"},\"telemetry_available\":$telemetryAvailable,\"telemetry_updated_at_ms\":${telemetryUpdatedAtMs ?: "null"},\"battery_pct\":${batteryPct ?: "null"},\"altitude_m\":${altitudeM ?: "null"},\"heading_deg\":${headingDeg ?: "null"},\"latitude_deg\":${latitudeDeg ?: "null"},\"longitude_deg\":${longitudeDeg ?: "null"}}",
                        ContentType.Application.Json
                    )
                }
                get("/stream") {
                    val stats = rtmpPublisher?.stats() ?: "not started"
                    call.respondText(
                        "{\"stream_state\":\"$streamState\",\"frames\":$framesReceived,\"stats\":\"$stats\",\"detail\":\"${streamDetail.replace("\\", "/").replace("\"", "'")}\"}",
                        ContentType.Application.Json
                    )
                }
            }
        }.start(wait = false)
    }

    private fun requestPermissionsIfNeeded() {
        val permissions = arrayOf(
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.RECORD_AUDIO
        )
        val missing = permissions.filter {
            ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), REQUEST_CODE)
        }
    }

    private fun deviceIp(): String {
        val wifi = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        return Formatter.formatIpAddress(wifi.connectionInfo.ipAddress)
    }

    private fun updateStatus(message: String) {
        runOnUiThread { connectionText.text = message }
    }
}
