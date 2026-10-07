package com.rrc.djiControlServer

import android.util.Log
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URLEncoder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Minimal pure-Kotlin RTMP video publisher.
 *
 * Receives raw H.264 in Annex B format (start-code delimited) from the DJI
 * VideoFeeder, extracts SPS/PPS, converts access units to AVCC length-prefixed
 * format, muxes them into FLV video tags, and pushes them over an RTMP
 * connection to a MediaMTX-style server.
 *
 * No audio track. Backpressure policy: drop the oldest queued frame when the
 * encoder/network falls behind, so telemetry latency never grows unbounded.
 */
class RtmpPublisher(
    private val host: String,
    private val port: Int,
    private val app: String,        // e.g. "live"
    private val streamKey: String,  // e.g. "drone?user=u&pass=p"
    private val onState: (String) -> Unit = {}
) {
    companion object {
        private const val TAG = "RtmpPublisher"
        private const val CHUNK_SIZE = 4096
        private const val QUEUE_CAPACITY = 90   // ~6s at 15fps
    }

    private val running = AtomicBoolean(false)
    private var thread: Thread? = null

    private var socket: Socket? = null
    private var input: DataInputStream? = null
    private var output: DataOutputStream? = null

    @Volatile private var sps: ByteArray? = null
    @Volatile private var pps: ByteArray? = null
    private var headerSent = false

    private var frameCount = 0L
    private var startMs = 0L

    // Queued access units awaiting send
    private val queue = ArrayBlockingQueue<ByteArray>(QUEUE_CAPACITY)

    fun start() {
        if (running.getAndSet(true)) return
        thread = Thread({ runLoop() }, "RtmpPublisher").also { it.start() }
    }

    fun stop() {
        running.set(false)
        thread?.interrupt()
        closeSocket()
    }

    /**
     * Called from the MSDK video channel for each H.264 access unit (Annex B).
     * Never blocks the caller; drops the oldest queued frame under backpressure.
     */
    fun onVideoFrame(data: ByteArray, isKeyFrame: Boolean, spsIn: ByteArray?, ppsIn: ByteArray?, pts: Long) {
        if (!running.get()) return
        if (spsIn != null) sps = spsIn
        if (ppsIn != null) pps = ppsIn
        extractConfig(data)
        if (isKeyFrame || headerSent) {
            if (!queue.offer(data)) {
                queue.poll()          // drop oldest
                queue.offer(data)
            }
        }
    }

    private fun extractConfig(frame: ByteArray) {
        if (sps != null && pps != null) return
        for (nal in splitAnnexB(frame)) {
            when (nalType(nal)) {
                7 -> if (sps == null) sps = nal
                8 -> if (pps == null) pps = nal
            }
        }
    }

    private fun runLoop() {
        while (running.get()) {
            try {
                onState("connecting")
                connect()
                onState("streaming")
                sendLoop()
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                Log.w(TAG, "publish error: ${e.message}")
            }
            closeSocket()
            onState("reconnecting")
            if (running.get()) {
                try { Thread.sleep(2000) } catch (e: InterruptedException) { break }
            }
        }
        onState("idle")
    }

    // ── RTMP connection ───────────────────────────────────────────────────

    private fun connect() {
        val s = Socket()
        s.connect(InetSocketAddress(host, port), 5000)
        s.tcpNoDelay = true
        socket = s
        input = DataInputStream(s.getInputStream())
        output = DataOutputStream(BufferedOutputStream(s.getOutputStream(), 64 * 1024))
        handshake()
        // Chunk size must be announced before any message longer than the default 128 bytes
        sendSetChunkSize(CHUNK_SIZE)
        // Standard layout: app=live, tcUrl=rtmp://host:port/live, publish name carries key + auth query
        sendRtmpCommand(0, "connect", 1.0, mapOf(
            "app" to app,
            "type" to "nonprivate",
            "tcUrl" to "rtmp://$host:$port/$app",
            "flashVer" to "FMLE/3.0"
        ))
        sendRtmpCommand(0, "createStream", 2.0, null)
        sendRtmpCommand(1, "publish", 0.0, null, listOf(streamKey, "live"))
        // Give the server a moment to process commands before media flows
        pumpInput(300)
    }

    private fun handshake() {
        val out = output!!
        val inp = input!!
        out.writeByte(3)                     // RTMP version
        val c1 = ByteArray(1536)             // zero time + random
        out.write(c1)
        out.flush()
        val s0 = inp.readByte()              // S0
        if (s0.toInt() != 3) throw Exception("bad RTMP version from server: $s0")
        val s1 = ByteArray(1536); readFully(inp, s1)
        val s2 = ByteArray(1536); readFully(inp, s2)
        out.write(s1)                        // C2 = echo S1
        out.flush()
    }

    private fun readFully(inp: DataInputStream, buf: ByteArray) {
        var off = 0
        while (off < buf.size) {
            val n = inp.read(buf, off, buf.size - off)
            if (n < 0) throw EOFException("RTMP handshake truncated")
            off += n
        }
    }

    // ── AMF0 encoding ─────────────────────────────────────────────────────

    private fun amfString(v: String): ByteArray {
        val b = v.toByteArray(Charsets.UTF_8)
        return byteArrayOf(0x02, (b.size shr 8).toByte(), b.size.toByte()) + b
    }

    private fun amfNumber(v: Double): ByteArray {
        val bits = java.lang.Double.doubleToLongBits(v)
        val b = ByteArray(9)
        b[0] = 0x00
        for (i in 0..7) b[1 + i] = (bits shr (56 - i * 8)).toByte()
        return b
    }

    private fun amfNull() = byteArrayOf(0x05)

    private fun amfEcmaArray(map: Map<String, String>): ByteArray {
        // AMF0 Object (0x03): no count prefix, terminated by 00 00 09
        var body = byteArrayOf(0x03)
        for ((k, v) in map) {
            val kb = k.toByteArray(Charsets.UTF_8)
            body += byteArrayOf((kb.size shr 8).toByte(), kb.size.toByte()) + kb + amfString(v)
        }
        return body + byteArrayOf(0, 0, 0x09)
    }

    private fun sendRtmpCommand(
        streamId: Int,
        name: String,
        txnId: Double,
        props: Map<String, String>?,
        extra: List<String> = emptyList()
    ) {
        var payload = amfString(name) + amfNumber(txnId)
        payload += if (props != null) amfEcmaArray(props) else amfNull()
        for (e in extra) payload += amfString(e)
        // Commands go on control stream (0); publish on stream 1
        writeChunks(if (name == "publish") 8 else 3, streamId, 20, payload, 0)
    }

    private fun sendSetChunkSize(size: Int) {
        val payload = byteArrayOf(
            (size shr 24).toByte(), (size shr 16).toByte(),
            (size shr 8).toByte(), size.toByte()
        )
        writeChunks(2, 0, 1, payload, 0)
    }

    // ── Chunk writer ──────────────────────────────────────────────────────

    private fun writeChunks(csid: Int, streamId: Int, typeId: Int, payload: ByteArray, timestamp: Int) {
        val out = output ?: return
        var offset = 0
        var first = true
        while (offset < payload.size || (first && payload.isEmpty())) {
            val chunkLen = minOf(CHUNK_SIZE, payload.size - offset)
            if (first) {
                // Basic header (fmt 0) — csid 2..63 fits in one byte
                out.writeByte(csid)
                // Message header: 3-byte timestamp, 3-byte length, 1-byte type, 4-byte LE stream id
                out.writeByte((timestamp shr 16) and 0xFF)
                out.writeByte((timestamp shr 8) and 0xFF)
                out.writeByte(timestamp and 0xFF)
                out.writeByte((payload.size shr 16) and 0xFF)
                out.writeByte((payload.size shr 8) and 0xFF)
                out.writeByte(payload.size and 0xFF)
                out.writeByte(typeId)
                out.writeByte(streamId and 0xFF)
                out.writeByte((streamId shr 8) and 0xFF)
                out.writeByte((streamId shr 16) and 0xFF)
                out.writeByte((streamId shr 24) and 0xFF)
                first = false
            } else {
                // fmt 3 continuation — no message header
                out.writeByte(0xC0 or csid)
            }
            if (chunkLen > 0) out.write(payload, offset, chunkLen)
            offset += chunkLen
            if (payload.isEmpty()) break
        }
        out.flush()
    }

    // ── FLV video tags ────────────────────────────────────────────────────

    private fun sendAvcSequenceHeader() {
        val s = sps ?: return
        val p = pps ?: return
        // AVCDecoderConfigurationRecord
        var record = byteArrayOf(
            0x01,                 // configurationVersion
            s[1], s[2], s[3],     // profile, profile_compat, level from SPS
            0xFF.toByte(),        // 6 bits reserved + 2 bits lengthSizeMinusOne (=3, 4-byte lengths)
            0xE1.toByte()         // 3 bits reserved + 5 bits numOfSPS (=1)
        )
        record += byteArrayOf((s.size shr 8).toByte(), s.size.toByte()) + s
        record += byteArrayOf(0x01) // numOfPPS
        record += byteArrayOf((p.size shr 8).toByte(), p.size.toByte()) + p
        val tag = byteArrayOf(0x17, 0x00, 0, 0, 0) + record   // keyframe + AVC + sequence header
        writeChunks(6, 1, 9, tag, 0)
        headerSent = true
        Log.i(TAG, "AVC sequence header sent (sps=${s.size}B pps=${p.size}B)")
    }

    private fun sendVideoFrame(annexB: ByteArray, isKeyFrame: Boolean, timestamp: Int) {
        // Convert Annex B NALs to AVCC payload
        var payload = ByteArray(0)
        for (nal in splitAnnexB(annexB)) {
            val t = nalType(nal)
            if (t == 7 || t == 8) continue            // SPS/PPS live in the sequence header
            if (t == 6) continue                      // skip SEI (DJI units can stall readers)
            payload += byteArrayOf(
                (nal.size shr 24).toByte(), (nal.size shr 16).toByte(),
                (nal.size shr 8).toByte(), nal.size.toByte()
            ) + nal
        }
        if (payload.isEmpty()) return
        val tag = byteArrayOf(
            (if (isKeyFrame) 0x17 else 0x27).toByte(),  // frame type + codec AVC
            0x01,                                        // AVC NALU
            0, 0, 0                                      // composition time offset
        ) + payload
        writeChunks(6, 1, 9, tag, timestamp)
    }

    private fun sendLoop() {
        startMs = System.currentTimeMillis()
        while (running.get()) {
            val frame = try {
                queue.poll(2, TimeUnit.SECONDS)
            } catch (e: InterruptedException) {
                break
            }
            if (frame == null) {
                // An idle socket never errors on its own; a write is how we learn the server dropped us.
                sendSetChunkSize(CHUNK_SIZE)
                pumpInput(0)
                continue
            }
            if (!headerSent) {
                val s = sps
                val p = pps
                if (s == null || p == null) {
                    extractConfig(frame)
                    if (sps == null || pps == null) continue   // wait for config
                }
                sendAvcSequenceHeader()
            }
            val ts = (System.currentTimeMillis() - startMs).toInt()
            val isKey = containsIdr(frame)
            sendVideoFrame(frame, isKey, ts)
            frameCount++
            pumpInput(0)
        }
    }

    private fun containsIdr(annexB: ByteArray): Boolean {
        for (nal in splitAnnexB(annexB)) {
            if (nalType(nal) == 5) return true
        }
        return false
    }

    /** Drain any inbound RTMP messages (acks, control) so socket buffers don't fill. */
    private fun pumpInput(maxWaitMs: Int) {
        val s = socket ?: return
        val inp = input ?: return
        try {
            s.soTimeout = maxWaitMs
            while (inp.available() > 0) {
                val b0 = inp.read()
                if (b0 < 0) break
                val csid = b0 and 0x3F
                val fmt = (b0 shr 6) and 0x03
                if (csid == 0) inp.read()             // extended 1-byte csid
                val headerLen = when (fmt) { 0 -> 11; 1 -> 7; 2 -> 3; else -> 0 }
                val hdr = ByteArray(headerLen)
                if (headerLen > 0) readFully(inp, hdr)
                if (fmt <= 1) {
                    val msgLen = ((hdr[3].toInt() and 0xFF) shl 16) or
                                 ((hdr[4].toInt() and 0xFF) shl 8) or
                                 (hdr[5].toInt() and 0xFF)
                    var remaining = msgLen
                    while (remaining > 0) {
                        val n = minOf(CHUNK_SIZE, remaining)
                        inp.skipBytes(n)
                        remaining -= n
                        if (remaining > 0) inp.read() // continuation header
                    }
                }
            }
        } catch (e: Exception) {
            // Non-fatal — input pump is best-effort
        } finally {
            try { s.soTimeout = 0 } catch (e: Exception) {}
        }
    }

    // ── H.264 helpers ─────────────────────────────────────────────────────

    private fun splitAnnexB(data: ByteArray): List<ByteArray> {
        val nals = ArrayList<ByteArray>()
        var i = 0
        var start = -1
        while (i < data.size - 3) {
            val is3 = data[i] == 0.toByte() && data[i + 1] == 0.toByte() && data[i + 2] == 1.toByte()
            val is4 = !is3 && i < data.size - 4 && data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
                      data[i + 2] == 0.toByte() && data[i + 3] == 1.toByte()
            if (is3 || is4) {
                val scLen = if (is4) 4 else 3
                if (start >= 0 && i > start) {
                    nals.add(data.copyOfRange(start, i))
                }
                start = i + scLen
                i += scLen
            } else {
                i++
            }
        }
        if (start >= 0 && start < data.size) {
            nals.add(data.copyOfRange(start, data.size))
        }
        // No start codes found — treat the whole buffer as one NAL
        if (nals.isEmpty() && data.isNotEmpty()) nals.add(data)
        return nals
    }

    private fun nalType(nal: ByteArray): Int =
        if (nal.isEmpty()) -1 else nal[0].toInt() and 0x1F

    private fun closeSocket() {
        try { socket?.close() } catch (e: Exception) {}
        socket = null
        input = null
        output = null
        headerSent = false
    }

    fun stats(): String {
        val secs = if (startMs > 0) (System.currentTimeMillis() - startMs) / 1000 else 0
        return "frames=$frameCount queue=${queue.size} up=${secs}s"
    }
}

/** Build the publish target from env-style pieces. Returns (host, port, streamKey). */
fun buildRtmpUrl(host: String, port: Int, user: String, pass: String): Triple<String, Int, String> {
    val enc = { v: String -> URLEncoder.encode(v, "UTF-8") }
    return Triple(host, port, "drone?user=${enc(user)}&pass=${enc(pass)}")
}
