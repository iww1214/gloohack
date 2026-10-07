"""
ATC Chatter Listener Agent
==========================
Monitors live ATC audio, transcribes it with Whisper, filters for
drone-relevant content using Claude, and sends SMS/email alerts.

TWO AUDIO MODES:
    --mode sdr        Real-time via RTL-SDR USB dongle (best, ~$25 hardware)
    --mode stream     LiveATC.net internet stream (fallback, ~30s delay)

Dependencies:
    pip install anthropic twilio sendgrid openai-whisper pyaudio requests
    pip install pyrtlsdr          # for SDR mode only
    pip install numpy scipy       # for SDR demodulation

Hardware (SDR mode):
    RTL-SDR Blog V4 dongle (~$25 at rtl-sdr.com)
    Antenna tuned to 118–137 MHz (VHF aviation band)

Set environment variables:
    GLOO_CLIENT_ID + GLOO_CLIENT_SECRET
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_PHONE
    SENDGRID_API_KEY
    LIVEATC_URL     # e.g. https://www.liveatc.net/play/kdca.pls (stream mode)

Legal note:
    Receiving ATC audio on VHF is legal everywhere in the US.
    This agent receives, transcribes, and privately notifies you.
    It does not retransmit or publicly broadcast — that requires FCC license.
"""

import os
import io
import sys
import base64
import time
import queue
import shutil
import subprocess
import threading
import argparse
import tempfile
import wave
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from gloo_client import GlooAnthropicCompat, complete, run_agent, MODELS, GLOO_TRADITION
try:
    import whisper
except Exception:
    # Optional local STT only. A broken torch install raises OSError (DLL), not ImportError;
    # Gloo speech-to-text is the default path and needs no local model.
    whisper = None
import requests
from twilio_client import create_twilio_client, get_twilio_from_phone
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Content

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = GlooAnthropicCompat()  # Routes through Gloo AI platform
try:
    twilio_client = create_twilio_client()
except Exception as e:
    # Alerting is best-effort; a missing Twilio credential must never stop the listener.
    print(f"Twilio disabled: {e}")
    twilio_client = None

# ── Configuration ─────────────────────────────────────────────────────────────
PILOT_PHONE     = os.environ.get("PILOT_PHONE", "+1XXXXXXXXXX")
PILOT_EMAIL     = os.environ.get("PILOT_EMAIL", "pilot@example.com")
DASHBOARD_ATC_URL = os.environ.get("DASHBOARD_ATC_URL", "http://127.0.0.1:8092/api/atc")


def push_to_dashboard(transcript: str, label: str, alert: Optional[dict]) -> None:
    """Best-effort: show the transcript line on the dashboard; never block SMS/email alerts."""
    payload = {"transcript": transcript, "frequency": label, "flagged": alert is not None}
    if alert:
        for key in ("severity", "category", "summary", "recommendation"):
            payload[key] = alert.get(key, "")
    try:
        requests.post(DASHBOARD_ATC_URL, json=payload, timeout=2)
    except Exception:
        pass

# ATC frequencies to monitor — add yours based on your location
# Find local frequencies at: https://www.airnav.com/airports/
ATC_FREQUENCIES = {
    "DCA_APPROACH":    119.850,   # Reagan National Approach
    "DCA_TOWER":       132.850,   # Reagan National Tower
    "DCA_GROUND":      121.900,   # Reagan National Ground
    "IAD_APPROACH":    126.100,   # Dulles Approach
    "IAD_TOWER":       120.100,   # Dulles Tower
    "POTOMAC_TRACON":  124.650,   # TRACON covering NoVA/DC area
    "DC_ATIS":         127.050,   # DC ATIS (automated info)
    "GUARD":           121.500,   # Emergency guard frequency
}

SAMPLE_RATE     = 48000    # Hz — standard for RTL-SDR
STREAM_RATE     = 16000    # Hz — what LiveATC streams are decoded to (Whisper's native rate)
DASHBOARD_LOCATION_URL = os.environ.get("DASHBOARD_LOCATION_URL", "http://127.0.0.1:8092/api/location")
FOLLOW_CHECK_S  = int(os.environ.get("ATC_FOLLOW_SEC", "30"))   # how often to re-check where the aircraft is
FEED_LABEL      = {"value": "LiveATC"}   # shown on the dashboard next to each transcript
CHUNK_SECONDS   = 15       # Transcribe every N seconds of audio
WHISPER_MODEL   = "base"   # Options: tiny, base, small, medium, large
                           # "base" is fast and accurate enough for ATC

# ── Keywords that trigger immediate alert ────────────────────────────────────
URGENT_KEYWORDS = [
    "temporary flight restriction", "tfr", "notam",
    "mayday", "pan pan", "emergency",
    "sigmet", "airmet", "pirep", "turbulence",
    "stop altitude", "traffic alert", "drone",
    "unmanned", "uas", "sua", "special use",
    "ground stop", "flow control", "hold",
    "security", "squawk", "ident",
]

# ── Whisper (optional alternative to Gloo speech-to-text) ───────────────────────────────
whisper_model = None
if whisper is not None and os.environ.get("ATC_STT", "gloo").lower() == "whisper":
    print("Loading Whisper model...")
    whisper_model = whisper.load_model(WHISPER_MODEL)
    print(f"Whisper '{WHISPER_MODEL}' ready.")
else:
    print("Speech-to-text: Gloo (Gemini). Set ATC_STT=whisper to use local Whisper instead.")


# ── SDR Audio Capture ─────────────────────────────────────────────────────────
class SDRAudioCapture:
    """
    Captures VHF AM aviation audio via RTL-SDR USB dongle.
    Demodulates AM from the IQ samples and feeds PCM audio to a queue.

    Hardware: RTL-SDR Blog V4 (~$25)
    Antenna:  Telescopic whip or discone covering 118–137 MHz
    """

    def __init__(self, frequency_hz: float, audio_queue: queue.Queue):
        try:
            from rtlsdr import RtlSdr
            self.sdr = RtlSdr()
        except ImportError:
            raise ImportError("Install pyrtlsdr: pip install pyrtlsdr")

        self.frequency  = frequency_hz
        self.queue      = audio_queue
        self.running    = False
        self.sdr.sample_rate = SAMPLE_RATE * 25   # oversample for FM/AM
        self.sdr.center_freq = frequency_hz
        self.sdr.gain        = "auto"

    def _demodulate_am(self, iq_samples: np.ndarray) -> np.ndarray:
        """Demodulate IQ samples to AM audio (envelope detection)."""
        # AM demodulation: take magnitude of complex IQ signal
        audio = np.abs(iq_samples)
        # Downsample to SAMPLE_RATE
        factor = len(audio) // SAMPLE_RATE
        if factor > 1:
            audio = audio[::factor]
        # Normalize to int16
        audio = audio - np.mean(audio)
        if np.max(np.abs(audio)) > 0:
            audio = audio / np.max(np.abs(audio))
        return (audio * 32767).astype(np.int16)

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def _capture_loop(self):
        buffer = []
        while self.running:
            try:
                samples = self.sdr.read_samples(SAMPLE_RATE * 25 * CHUNK_SECONDS)
                audio   = self._demodulate_am(np.array(samples))
                self.queue.put(("sdr", audio, self.frequency))
            except Exception as e:
                print(f"SDR capture error: {e}")
                time.sleep(1)

    def stop(self):
        self.running = False
        self.sdr.close()


# ── LiveATC Stream Capture ────────────────────────────────────────────────────
class LiveATCCapture:
    """
    Captures ATC audio from a LiveATC.net stream URL (.pls or direct MP3).
    Decodes with ffmpeg to 16 kHz mono PCM and reconnects if the stream drops.
    """

    def __init__(self, stream_url: str, audio_queue: queue.Queue):
        self.url     = stream_url
        self.queue   = audio_queue
        self.running = False
        self.proc    = None

    @staticmethod
    def _ffmpeg() -> str:
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise RuntimeError("ffmpeg not found: install ffmpeg or run: pip install imageio-ffmpeg")

    def _resolve_pls(self, url: str) -> str:
        """Resolve .pls playlist to actual stream URL."""
        if url.endswith(".pls"):
            resp = requests.get(url, timeout=10)
            for line in resp.text.splitlines():
                if line.startswith("File1="):
                    return line.split("=", 1)[1].strip()
        return url

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._stream_loop, daemon=True)
        self.thread.start()

    def _stream_loop(self):
        need = STREAM_RATE * 2 * CHUNK_SECONDS
        while self.running:
            try:
                stream_url = self._resolve_pls(self.url)
                print(f"Connecting to LiveATC stream: {stream_url}")
                self.proc = subprocess.Popen(
                    [self._ffmpeg(), "-loglevel", "error",
                     "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "-i", stream_url,
                     "-f", "s16le", "-ac", "1", "-ar", str(STREAM_RATE), "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                while self.running:
                    data = self.proc.stdout.read(need)
                    if len(data) < need:
                        break
                    try:
                        self.queue.put_nowait(("liveatc", np.frombuffer(data, dtype=np.int16), 0.0))
                    except queue.Full:
                        pass   # stay real-time: drop a chunk rather than fall behind
            except Exception as e:
                print(f"LiveATC stream error: {e}")
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
            if self.running:
                time.sleep(5)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class ClipReplay:
    """
    Plays recorded ATC clips (16 kHz mono WAV) in a loop as if they were arriving live.
    A clip's .txt sidecar is only a fallback if speech-to-text fails. Used for demos.
    """
    GAP_S = 8

    def __init__(self, folder: str, audio_queue: queue.Queue):
        self.folder  = Path(__file__).parent / folder
        self.queue   = audio_queue
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        clips = sorted(self.folder.glob("*.wav"))
        if not clips:
            print(f"No clips found in {self.folder}")
            return
        while self.running:
            for path in clips:
                if not self.running:
                    return
                with wave.open(str(path)) as w:
                    if w.getframerate() != STREAM_RATE or w.getnchannels() != 1:
                        print(f"Skipping {path.name}: needs {STREAM_RATE} Hz mono")
                        continue
                    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                sidecar = path.with_suffix(".txt")
                preset = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else None
                try:
                    self.queue.put(("clip", audio, 0.0, preset), timeout=5)
                except queue.Full:
                    pass
                time.sleep(len(audio) / STREAM_RATE + self.GAP_S)

    def stop(self):
        self.running = False


def current_feed() -> Optional[dict]:
    """Ask the dashboard which ATC feed fits the aircraft's current location (None if unreachable)."""
    try:
        return requests.get(DASHBOARD_LOCATION_URL, timeout=10).json().get("atc") or {}
    except Exception:
        return None


def follow_location(audio_queue: queue.Queue, captures: list):
    """Keep the listener on the right feed as the aircraft moves between airports."""
    active_url = None
    announced  = None
    while True:
        atc  = current_feed()
        feed = (atc or {}).get("feed")
        key  = (feed.get("url") or feed.get("clips")) if feed else None
        if atc is not None and key != active_url:
            for c in captures:
                c.stop()
            captures.clear()
            active_url = key
            if key:
                FEED_LABEL["value"] = f"{atc['airport']} {feed.get('name', '')}".strip()
                print(f"Following aircraft: listening to {FEED_LABEL['value']}")
                capture = (LiveATCCapture(feed["url"], audio_queue) if feed.get("url")
                           else ClipReplay(feed["clips"], audio_queue))
                capture.start()
                captures.append(capture)
            elif atc.get("reason") != announced:
                announced = atc.get("reason")
                print(f"No ATC feed to follow: {announced}")
        time.sleep(FOLLOW_CHECK_S)


# ── Audio → Text ──────────────────────────────────────────────────────────────
STT_PROMPT = (
    "Transcribe this air traffic control radio transmission exactly as spoken. "
    "It is aviation phraseology: callsigns, altitudes in feet, headings, runways, 'unmanned aircraft', "
    "'NOTAM', 'TFR', 'wind shear'. Write numbers as digits. "
    "Output only the transcript text. If there is no intelligible speech, output exactly: NO_SPEECH"
)


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def _transcribe_gloo(audio: np.ndarray, sample_rate: int) -> Optional[str]:
    """Speech-to-text through the Gloo platform (Gemini). Audio goes in as a data URL, which Gloo accepts."""
    from gloo_client import GLOO_V2_BASE, _auth_headers
    data_url = "data:audio/wav;base64," + base64.b64encode(_wav_bytes(audio, sample_rate)).decode()
    resp = requests.post(
        f"{GLOO_V2_BASE}/chat/completions",
        headers=_auth_headers(),
        json={"model": MODELS["atc_stt"], "auto_routing": False, "max_tokens": 300,
              "messages": [{"role": "user", "content": [
                  {"type": "text", "text": STT_PROMPT},
                  {"type": "image_url", "image_url": {"url": data_url}}]}]},
        timeout=60)
    resp.raise_for_status()
    text = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    return None if not text or "NO_SPEECH" in text.upper() or len(text) <= 5 else text


def transcribe_audio(audio: np.ndarray, source_freq: float = 0.0, sample_rate: int = SAMPLE_RATE) -> Optional[str]:
    """Gloo speech-to-text by default; local Whisper if ATC_STT=whisper, or as a fallback when Gloo fails."""
    if len(audio) == 0:
        return None
    if os.environ.get("ATC_STT", "gloo").lower() != "whisper":
        try:
            return _transcribe_gloo(audio, sample_rate)
        except Exception as e:
            print(f"Gloo transcription error: {e}")
            if whisper_model is None:
                return None
    return _transcribe_whisper(audio, sample_rate)


def _transcribe_whisper(audio: np.ndarray, sample_rate: int) -> Optional[str]:
    """
    Convert raw audio to text using local Whisper (optional install).
    Uses aviation-specific prompt to improve accuracy of ATC terminology.
    """
    if whisper_model is None:
        return None

    # Write to temp WAV file (Whisper requires file input)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
        with wave.open(wav_path, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())

    try:
        result = whisper_model.transcribe(
            wav_path,
            language="en",
            # Aviation-specific initial prompt improves accuracy of callsigns,
            # altitudes, frequencies, and ATC phraseology significantly
            initial_prompt=(
                "ATC aviation radio transmission. "
                "Aircraft callsigns, altitudes in feet, frequencies in MHz, "
                "headings in degrees. Expect: 'cleared to', 'descend and maintain', "
                "'traffic alert', 'NOTAM', 'TFR', 'temporary flight restriction', "
                "'mayday', 'pan pan', 'squawk', 'ident', 'contact approach', "
                "'hold short', 'cleared for takeoff', 'wind shear alert'."
            ),
            fp16=False
        )
        text = result["text"].strip()
        return text if len(text) > 5 else None
    except Exception as e:
        print(f"Transcription error: {e}")
        return None
    finally:
        os.unlink(wav_path)


# ── Claude Relevance Filter ───────────────────────────────────────────────────
FILTER_SYSTEM = """
You are an aviation safety filter for a drone pilot flying a DJI Air 3 under FAA Part 107.

You receive raw ATC radio transcriptions and must decide if they contain
information that could affect drone flight safety or legality.

RELEVANT (return action: "alert"):
- TFR activations or announcements: "TFR now active", "temporary flight restriction"
- NOTAM readings: any NOTAM content read aloud by controllers
- Weather advisories: SIGMET, AIRMET, PIREPs, wind shear alerts, microburst warnings
- Emergency traffic: Mayday, Pan-Pan calls within the area
- Drone/UAS mentions: "drone activity", "UAS reported", "unmanned aircraft"
- Ground stops or flow control: signals airspace stress affecting all traffic
- Security alerts: any security-related airspace closure
- Significant traffic conflicts: could indicate airspace is unusually active

NOT RELEVANT (return action: "ignore"):
- Routine taxi clearances: "taxi to runway 1"
- Standard takeoff/landing clearances
- Frequency handoffs: "contact departure 119.85"
- Routine altitude assignments between manned aircraft
- IFR routing clearances
- Gate holds unrelated to weather/security

Respond ONLY with valid JSON, no other text:
{
  "action": "alert" | "ignore",
  "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "INFO",
  "category": "TFR" | "NOTAM" | "WEATHER" | "EMERGENCY" | "DRONE" | "SECURITY" | "TRAFFIC" | "OTHER",
  "summary": "One sentence plain-English summary for the pilot",
  "recommendation": "What the pilot should do right now",
  "full_transcript": "The exact transcript text",
  "timestamp": "ISO timestamp"
}
"""

def filter_with_claude(transcript: str, frequency: float) -> Optional[dict]:
    """
    Ask Claude if this ATC transcript is relevant to a drone pilot.
    Returns structured alert dict or None if not relevant.
    """
    # Fast pre-filter: skip if no urgent keywords present
    lower = transcript.lower()
    if not any(kw in lower for kw in URGENT_KEYWORDS):
        # Still send to Claude for full analysis but flag as low priority
        pass

    try:
        raw = complete(
            FILTER_SYSTEM,
            (f"ATC Frequency: {frequency:.3f} MHz\n"
             f"Time: {datetime.now().isoformat()}\n"
             f"Transcript: {transcript}"),
            model="atc_filter",
            max_tokens=512,
            json_output=True,
        ).strip()
        # Strip any markdown fences if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        if result.get("action") == "alert":
            result["timestamp"]  = datetime.now().isoformat()
            result["frequency"]  = f"{frequency:.3f} MHz"
            return result
        return None

    except Exception as e:
        print(f"Claude filter error: {e}")
        return None


# ── Alert Delivery ────────────────────────────────────────────────────────────
SEVERITY_EMOJI = {
    "CRITICAL": "🚨",
    "HIGH":     "⛔",
    "MEDIUM":   "⚠️",
    "INFO":     "ℹ️"
}

CATEGORY_EMOJI = {
    "TFR":       "🚫",
    "NOTAM":     "📋",
    "WEATHER":   "🌩️",
    "EMERGENCY": "🆘",
    "DRONE":     "🚁",
    "SECURITY":  "🔒",
    "TRAFFIC":   "✈️",
    "OTHER":     "📡"
}

def send_atc_alert(alert: dict):
    """Send ATC alert via SMS and email."""
    sev   = alert.get("severity", "MEDIUM")
    cat   = alert.get("category", "OTHER")
    se    = SEVERITY_EMOJI.get(sev, "⚠️")
    ce    = CATEGORY_EMOJI.get(cat, "📡")
    now   = datetime.now().strftime("%H:%M:%S")

    # ── SMS ──────────────────────────────────────────────────────────────
    sms = "\n".join([
        f"{se} ATC ALERT [{sev}] — {now}",
        f"{ce} {cat}: {alert.get('summary', '')}",
        "",
        f"FREQUENCY: {alert.get('frequency', 'Unknown')}",
        "",
        f"ACTION: {alert.get('recommendation', 'Assess before continuing flight')}",
        "",
        "Full transcript in email ↓"
    ])

    try:
        if twilio_client is None:
            print(f"SMS skipped: Twilio not configured")
            return
        twilio_client.messages.create(
            body=sms,
            from_=get_twilio_from_phone(),
            to=PILOT_PHONE
        )
        print(f"SMS sent: [{sev}] {cat}")
    except Exception as e:
        print(f"SMS failed: {e}")

    # ── Email ─────────────────────────────────────────────────────────────
    sev_color = {
        "CRITICAL": "#cc0000",
        "HIGH":     "#cc4400",
        "MEDIUM":   "#c9a84c",
        "INFO":     "#1a7a1a"
    }.get(sev, "#555")

    email_html = f"""
    <div style='font-family:Calibri,Arial,sans-serif;max-width:640px;margin:0 auto'>
        <div style='background:#1a1a2e;padding:20px 24px;border-radius:4px 4px 0 0'>
            <div style='color:#fff;font-size:20px;font-weight:700'>
                📡 ATC Live Alert — DJI Air 3 Monitor
            </div>
            <div style='color:#aaa;font-size:13px;margin-top:4px'>{alert.get("timestamp","")}</div>
        </div>

        <div style='background:{sev_color};padding:14px 24px;text-align:center'>
            <div style='color:#fff;font-size:26px;font-weight:700'>
                {se} {sev} — {ce} {cat}
            </div>
        </div>

        <div style='padding:24px;background:#fff'>
            <table style='width:100%;border-collapse:collapse;margin-bottom:20px'>
                <tr style='background:#f5f5f5'>
                    <td style='padding:10px;font-weight:700;width:140px'>Frequency</td>
                    <td style='padding:10px'>{alert.get("frequency","Unknown")}</td>
                </tr>
                <tr>
                    <td style='padding:10px;font-weight:700'>Category</td>
                    <td style='padding:10px'>{ce} {cat}</td>
                </tr>
                <tr style='background:#f5f5f5'>
                    <td style='padding:10px;font-weight:700'>Severity</td>
                    <td style='padding:10px;color:{sev_color};font-weight:700'>{sev}</td>
                </tr>
                <tr>
                    <td style='padding:10px;font-weight:700'>Summary</td>
                    <td style='padding:10px'>{alert.get("summary","")}</td>
                </tr>
            </table>

            <div style='background:#fff8e1;border-left:4px solid #c9a84c;padding:14px;margin-bottom:20px'>
                <div style='font-weight:700;margin-bottom:6px'>⚡ Recommended Action</div>
                <div>{alert.get("recommendation","")}</div>
            </div>

            <div style='background:#1a1a2e;border-radius:4px;padding:16px'>
                <div style='color:#aaa;font-size:12px;margin-bottom:8px;font-family:monospace'>
                    FULL ATC TRANSCRIPT
                </div>
                <div style='color:#00ff88;font-family:monospace;font-size:14px;
                            line-height:1.6;white-space:pre-wrap'>
{alert.get("full_transcript","")}
                </div>
            </div>

            <p style='margin-top:20px;font-size:11px;color:#aaa;border-top:1px solid #eee;padding-top:12px'>
                Live ATC monitoring is for informational purposes only. Always verify
                with official FAA sources. Pilot in command is responsible for all
                flight decisions. Cross-check at
                <a href='https://pilotweb.nas.faa.gov'>pilotweb.nas.faa.gov</a>.
            </p>
        </div>
    </div>"""

    try:
        sg   = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        mail = Mail(
            from_email="atcalert@yourdroneops.com",
            to_emails=PILOT_EMAIL,
            subject=f"[ATC {sev}] {cat} — {alert.get('summary','')[:60]}"
        )
        mail.add_content(Content("text/html", email_html))
        sg.send(mail)
        print(f"Email sent: [{sev}] {cat}")
    except Exception as e:
        print(f"Email failed: {e}")


# ── Alert Deduplication ───────────────────────────────────────────────────────
class AlertDeduplicator:
    """Prevent sending the same alert multiple times within a cooldown window."""

    def __init__(self, cooldown_seconds: int = 120):
        self.sent     = {}
        self.cooldown = cooldown_seconds

    def should_send(self, alert: dict) -> bool:
        key = f"{alert.get('category','')}-{alert.get('summary','')[:40]}"
        now = time.time()
        if key in self.sent and now - self.sent[key] < self.cooldown:
            return False
        self.sent[key] = now
        return True

    def cleanup(self):
        """Remove expired entries."""
        now = time.time()
        self.sent = {k: v for k, v in self.sent.items()
                     if now - v < self.cooldown}


# ── Processing Pipeline ───────────────────────────────────────────────────────
def processing_pipeline(audio_queue: queue.Queue, deduplicator: AlertDeduplicator):
    """
    Continuously pulls audio chunks from queue,
    transcribes, filters, and alerts.
    """
    print("Processing pipeline started — listening for ATC traffic...")

    while True:
        try:
            item = audio_queue.get(timeout=30)
            source, audio, frequency = item[:3]
            preset = item[3] if len(item) > 3 else None
            is_stream = source in ("liveatc", "clip")
            label = FEED_LABEL["value"] if is_stream else f"{frequency:.3f} MHz"

            # Step 1: Transcribe
            transcript = transcribe_audio(audio, frequency, STREAM_RATE if is_stream else SAMPLE_RATE) or preset
            if not transcript:
                continue

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                  f"{label} → {transcript[:80]}...")

            # Step 2: Filter with Claude
            alert = filter_with_claude(transcript, frequency)
            if alert and is_stream:
                alert["frequency"] = label
            push_to_dashboard(transcript, label, alert)
            if not alert:
                continue

            if source == "clip":
                continue   # simulated demo audio is shown on the dashboard only; never SMS or email

            # Step 3: Deduplicate
            if not deduplicator.should_send(alert):
                print(f"Skipping duplicate alert: {alert.get('category')}")
                continue

            # Step 4: Send
            print(f"\n{'!'*60}")
            print(f"ALERT: [{alert['severity']}] {alert['category']}")
            print(f"  {alert['summary']}")
            print(f"  → {alert['recommendation']}")
            print(f"{'!'*60}\n")
            send_atc_alert(alert)

        except queue.Empty:
            print(".", end="", flush=True)
            deduplicator.cleanup()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Pipeline error: {e}")
            time.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ATC Listener Agent for DJI Air 3")
    parser.add_argument("--mode",      choices=["sdr", "stream"], default="stream",
                        help="Audio source: sdr (RTL-SDR dongle) or stream (LiveATC)")
    parser.add_argument("--freq",      type=float, default=119.850,
                        help="Frequency in MHz for SDR mode (default: DCA approach)")
    parser.add_argument("--stream",    type=str,
                        default=os.environ.get("LIVEATC_URL", ""),
                        help="Fixed LiveATC stream URL; leave empty to follow the aircraft's location")
    parser.add_argument("--multi",     action="store_true",
                        help="SDR mode: cycle through all ATC_FREQUENCIES")
    args = parser.parse_args()

    audio_queue   = queue.Queue(maxsize=10)
    deduplicator  = AlertDeduplicator(cooldown_seconds=120)
    captures      = []

    print(f"\n{'='*60}")
    print("DJI Air 3 — ATC Listener Agent")
    print(f"Mode: {args.mode.upper()} | Pilot: {PILOT_PHONE} / {PILOT_EMAIL}")
    print(f"{'='*60}\n")

    if args.mode == "sdr":
        freq_hz = args.freq * 1e6
        print(f"Starting SDR capture on {args.freq:.3f} MHz...")
        capture = SDRAudioCapture(freq_hz, audio_queue)
        capture.start()
        captures.append(capture)

        if args.multi:
            # Rotate through frequencies — one thread per frequency
            # Note: single SDR dongle can only tune one freq at a time
            # For multi-frequency, use multiple dongles or time-division scanning
            print("Note: Multi-freq scanning with one dongle rotates every chunk.")

    else:  # stream mode
        if args.stream:
            print("Connecting to LiveATC stream (fixed URL)...")
            FEED_LABEL["value"] = "LiveATC"
            capture = LiveATCCapture(args.stream, audio_queue)
            capture.start()
            captures.append(capture)
        else:
            print("No fixed stream set: following the aircraft's location "
                  f"(feeds listed in atc_feeds.json, location from {DASHBOARD_LOCATION_URL})")
            threading.Thread(target=follow_location, args=(audio_queue, captures), daemon=True).start()

    # Run processing pipeline (blocking)
    try:
        processing_pipeline(audio_queue, deduplicator)
    except KeyboardInterrupt:
        print("\nShutting down ATC listener...")
    finally:
        for c in captures:
            c.stop()


if __name__ == "__main__":
    main()
