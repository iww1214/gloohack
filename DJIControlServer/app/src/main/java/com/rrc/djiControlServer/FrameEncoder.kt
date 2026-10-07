package com.rrc.djiControlServer

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.util.Log

/**
 * Encodes decoded NV21 camera frames to H.264 Annex B with MediaCodec.
 * Output goes to [sink]: (annexB, isKeyFrame, ptsUs). Codec-config buffers (SPS/PPS)
 * are delivered with isKeyFrame=false so RtmpPublisher can extract them.
 */
class FrameEncoder(
    private val fps: Int = 15,
    private val bitrate: Int = 2_000_000,
    private val sink: (ByteArray, Boolean, Long) -> Unit
) {
    private var codec: MediaCodec? = null
    private var width = 0
    private var height = 0
    private var lastFrameNs = 0L
    private val frameIntervalNs = 1_000_000_000L / fps
    private val info = MediaCodec.BufferInfo()

    @Synchronized
    fun encode(data: ByteArray, offset: Int, length: Int, w: Int, h: Int) {
        val now = System.nanoTime()
        if (lastFrameNs != 0L && now - lastFrameNs < frameIntervalNs) return
        lastFrameNs = now

        val ew = w - (w % 2)
        val eh = h - (h % 2)
        if (codec == null || ew != width || eh != height) startCodec(ew, eh)
        val c = codec ?: return

        val idx = c.dequeueInputBuffer(0)
        if (idx >= 0) {
            val buf = c.getInputBuffer(idx) ?: return
            buf.clear()
            val nv12 = nv21ToNv12(data, offset, w, h, ew, eh)
            buf.put(nv12)
            c.queueInputBuffer(idx, 0, nv12.size, now / 1000, 0)
        }
        drain(c)
    }

    @Synchronized
    fun stop() {
        try { codec?.stop() } catch (_: Exception) { }
        try { codec?.release() } catch (_: Exception) { }
        codec = null
    }

    private fun startCodec(w: Int, h: Int) {
        stop()
        width = w
        height = h
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
        }
        val c = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        c.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        c.start()
        codec = c
        Log.i("FrameEncoder", "H.264 encoder started ${w}x$h @${fps}fps ${bitrate}bps")
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val out = c.dequeueOutputBuffer(info, 0)
            if (out < 0) return
            val buf = c.getOutputBuffer(out)
            if (buf != null && info.size > 0) {
                val bytes = ByteArray(info.size)
                buf.position(info.offset)
                buf.get(bytes)
                val isConfig = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                val isKey = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                sink(bytes, isKey && !isConfig, info.presentationTimeUs)
            }
            c.releaseOutputBuffer(out, false)
        }
    }

    /** NV21 (Y + interleaved VU) to NV12 (Y + interleaved UV), cropped to even dimensions. */
    private fun nv21ToNv12(src: ByteArray, off: Int, w: Int, h: Int, ew: Int, eh: Int): ByteArray {
        val out = ByteArray(ew * eh * 3 / 2)
        for (row in 0 until eh) {
            System.arraycopy(src, off + row * w, out, row * ew, ew)
        }
        val ySize = ew * eh
        val srcUv = off + w * h
        for (row in 0 until eh / 2) {
            var s = srcUv + row * w
            var d = ySize + row * ew
            for (col in 0 until ew / 2) {
                out[d] = src[s + 1]
                out[d + 1] = src[s]
                s += 2
                d += 2
            }
        }
        return out
    }
}
