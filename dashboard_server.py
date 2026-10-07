"""
dashboard_server.py
====================
Lightweight FastAPI server that serves a live JSON feed consumed by
the monitoring dashboard (dashboard.html).

Run alongside the main agent:
  uvicorn dashboard_server:app --port 8090

Then open dashboard.html in a browser.

Install:
  pip install fastapi uvicorn
"""

import os, sys, json, time, subprocess, ipaddress, threading, collections
from datetime import datetime
from pathlib  import Path
from urllib.request import urlopen
from fastapi  import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(title="Safety1271 Dashboard")

import conditions_monitor
import location_service


@app.get("/api/location")
def get_location():
    """Operating location plus the airport, field elevation, timezone and ATC feed derived from it."""
    return JSONResponse(location_service.resolve())


@app.post("/api/location")
def set_location(payload: dict):
    """Set a manual location (airport code or coordinates) for this session, or {"auto": true} for aircraft GPS."""
    if payload.get("auto"):
        location_service.set_auto()
    elif payload.get("airport"):
        code = str(payload["airport"]).strip()[:4]
        airport = location_service.find_airport(code) if code.isalnum() else None
        if not airport:
            raise HTTPException(status_code=404, detail="Airport code not found")
        location_service.set_manual(airport["lat"], airport["lon"], f"{airport['icao']} — {airport['name']}")
    else:
        try:
            lat, lon = float(payload["lat"]), float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Provide an airport code or lat and lon")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=422, detail="Coordinates out of range")
        location_service.set_manual(lat, lon, f"{lat:.4f}, {lon:.4f}")
    return JSONResponse(location_service.resolve(force=True))


@app.on_event("startup")
def _start_conditions_monitor():
    conditions_monitor.start()


@app.get("/api/conditions")
def get_conditions():
    """Latest nearby-airport weather, wind trend and NWS alerts."""
    return JSONResponse(conditions_monitor.get_snapshot())

INCIDENT_DIR = Path(os.getenv("INCIDENT_DIR", "./incidents"))
LOG_FILE     = next(iter(sorted(Path(".").glob("safety1271_*.log"))), None)
COMPOUND_NAME = os.getenv("COMPOUND_NAME", "Gloo Church & School Campus")
BRIDGE_URL = os.getenv("MSDK_BRIDGE_URL", "http://172.20.10.2:8080").rstrip("/")
BRIDGE_CACHE = Path(__file__).with_name("bridge_last.json")   # last phone address set from the page
try:
    BRIDGE_URL = json.loads(BRIDGE_CACHE.read_text(encoding="utf-8"))["url"]
except (OSError, ValueError, KeyError):
    pass
VIDEO_URL = os.getenv("RTMP_STREAM_URL", "")
VIDEO_SOURCE_MODE = os.getenv("SAFETY1271_VIDEO_MODE", "live").strip().lower()
if VIDEO_SOURCE_MODE not in {"live", "simulated"}:
    raise ValueError("SAFETY1271_VIDEO_MODE must be 'live' or 'simulated'")
TELEMETRY_MAX_AGE_MS = 10000
ATC_FEED_MAX = 100
ATC_FIELD_MAX = 600
RELAY_MAX_WIDTH = 960           # browser preview only; keeps the relay fast
RELAY_MIN_INTERVAL_S = 0.125    # ~8 frames per second to the browser
_atc_feed = []   # newest first; filled by atc_listener_agent via /api/atc
_atc_live = {"proc": None, "stream_url": os.getenv("LIVEATC_STREAM_URL", "https://d.liveatc.net/kden1_app_fin1"), "on": False}
ATC_CLIP_SECONDS = 30


def _atc_live_on() -> bool:
    proc = _atc_live["proc"]
    return proc is not None and proc.poll() is None


def _stop_atc_live():
    proc = _atc_live["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
    _atc_live["proc"] = None
    _atc_live["on"] = False


@app.post("/api/atc/live")
async def set_atc_live(payload: dict):
    """Start or stop the live ATC listener. The audio may be turned on or off separately."""
    on = payload.get("on") is True
    if on == _atc_live_on():
        _atc_live["on"] = on
        return JSONResponse({"on": on})
    _stop_atc_live()
    if on:
        _atc_live["proc"] = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("atc_listener_agent.py")),
             "--mode", "stream", "--stream", _atc_live["stream_url"]],
            cwd=str(Path(__file__).parent))
        _atc_live["on"] = True
    return JSONResponse({"on": _atc_live["on"]})


@app.get("/api/atc/live")
def get_atc_live():
    """Whether the live listener is on, where its audio comes from, and the airport's radio frequencies."""
    loc = location_service.resolve()
    atc = loc.get("atc") or {}
    airport = loc.get("airport") or {}
    freqs = []
    for f in (airport.get("frequencies") or []):
        mhz = f.get("mhz")
        freqs.append({"type": f.get("type", ""), "name": f.get("description", ""), "mhz": mhz})
    return JSONResponse({"on": _atc_live_on(), "stream_url": _atc_live["stream_url"],
                         "feed_name": (atc.get("feed") or {}).get("name"),
                         "airport": {"icao": airport.get("icao"), "name": airport.get("name")}, "frequencies": freqs})
_ts_seen = {"value": None, "changed_at": 0.0}   # phone telemetry timestamp liveness, immune to clock skew
COMMAND_MAX_CHARS = 300
COMMAND_HISTORY_MAX = 20
_command = {"active": None, "history": [], "next_id": 1}   # operator search directive from the Command Center

# Zone names the Command Center can recognise in an operator command (matches COMPOUND_ZONES labels).
ZONE_HINTS = {
    "West entrance":      ("west entrance", "west door", "west gate"),
    "Main entrance":      ("main entrance", "front entrance", "front door"),
    "Parking lot":        ("parking lot", "parking", "car park"),
    "Children's playground": ("playground",),
    "Rear / loading dock": ("loading dock", "rear"),
}

# Demo replay: pre-recorded footage clips, scripted agent reveals (see demo_runner.py)
DEMO_MANIFEST = Path(__file__).with_name("demo_manifest.json")
DEMO_DIR = Path(__file__).with_name("demo_clips")
_demo = {"active": None, "started_at": None, "proc": None}
PARKING_HISTORY_MAX = 30
_latest_frame = {"t": 0.0, "jpeg": None}   # newest frame sent to the viewer, read by stream_analyst.py
_narration = {"latest": None, "history": []}   # running commentary from stream_analyst.py
NARRATION_HISTORY_MAX = 20
_parking = {"latest": None, "history": []}   # newest count from the patrol agent, for the parking team
PREFLIGHT_CACHE = Path(__file__).with_name("preflight_last.json")   # survives dashboard restarts

# In-memory state — populated by agent via module import
_state = {
    "compound_name":    COMPOUND_NAME,
    "status":           "unknown",
    "video_source_mode": VIDEO_SOURCE_MODE,
    "drone_state":      "unknown",
    "current_zone":     "",
    "battery_pct":      None,
    "altitude_m":       None,
    "heading_deg":      None,
    "bridge_connected": False,
    "aircraft_connected": False,
    "telemetry_fresh": False,
    "telemetry_updated_at_ms": None,
    "frames_analyzed":  0,
    "safety_flags":     0,
    "security_flags":   0,
    "alerts_sent":      0,
    "emergencies":      0,
    "holds_executed":   0,
    "uptime_s":         0,
    "start_time":       datetime.now().isoformat(),
    "last_incident":    None,
    "recent_alerts":    [],
    "zones_cleared":    {},
    "preflight":        None,  # latest preflight report pushed via /api/preflight
    "demo_mode":        False,
    "demo_title":       None,
}

try:
    _state["preflight"] = json.loads(PREFLIGHT_CACHE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    pass


def _demo_entries() -> list:
    return json.loads(DEMO_MANIFEST.read_text(encoding="utf-8"))["scenarios"] if DEMO_MANIFEST.exists() else []


def _stop_demo():
    proc = _demo["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
    _demo.update({"active": None, "started_at": None, "proc": None})
    _narration.update({"latest": None, "history": []})
    _state["recent_alerts"] = [a for a in _state["recent_alerts"] if not a.get("demo")]


def _apply_demo_telemetry():
    """While a replay runs, show flagged pre-recorded heading and altitude; battery and state stay the aircraft's own."""
    entry = next((s for s in _demo_entries() if s["id"] == _demo["active"]), {})
    title = entry.get("title", _demo["active"])
    target = (_command["active"] or {}).get("target_zone")
    _state.update({
        "demo_mode": True, "demo_title": title,
        "status": "demo replay",
        "bridge_connected": True, "aircraft_connected": True, "telemetry_fresh": True,
        "altitude_m": 15.0,
        # Fixed so compass directions stay consistent with the clip; set heading_deg per scenario in the manifest.
        "heading_deg": float(entry.get("heading_deg", 0)) % 360, "heading_simulated": True,
        "current_zone": target or _state.get("current_zone") or "Patrol",
    })


def update_state():
    """Read actual bridge telemetry; hold last-known values, flagged stale, rather than blanking them."""
    _state.update({
        "bridge_connected": False, "aircraft_connected": False,
        "telemetry_fresh": False,
        "drone_state": "unknown", "status": "bridge offline",
    })
    try:
        with urlopen(f"{BRIDGE_URL}/status", timeout=2) as response:
            status = json.load(response)
        updated = status.get("telemetry_updated_at_ms")
        # The phone clock can differ from this laptop's, so compare the timestamp to its own
        # previous value: fresh only if it changed within the allowed window.
        if isinstance(updated, (int, float)):
            if updated != _ts_seen["value"]:
                # First sighting only records the value; it counts as fresh once it has changed.
                if _ts_seen["value"] is not None:
                    _ts_seen["changed_at"] = time.time()
                _ts_seen["value"] = updated
            fresh = (time.time() - _ts_seen["changed_at"]) * 1000 <= TELEMETRY_MAX_AGE_MS
        else:
            fresh = False
        connected_flag = status.get("aircraft_connected") is True
        # The MSDK KeyConnection flag sticks false after a reconnect; fresh advancing telemetry is the real signal.
        connected = connected_flag or fresh
        live = connected and fresh
        new_state = {
            "bridge_connected": True,
            "aircraft_connected": connected,
            "telemetry_fresh": live,
            "telemetry_stale": not live and status.get("battery_pct") is not None,
            "status": "live" if live else "telemetry unavailable",
            "drone_state": status.get("state", "connected") if live else "unknown",
        }
        if updated is not None:
            new_state["telemetry_updated_at_ms"] = updated
        # Keep the last real reading when the link drops; only overwrite with newer live data.
        for key, src in (("battery_pct", "battery_pct"), ("altitude_m", "altitude_m"), ("heading_deg", "heading_deg"),
                         ("latitude_deg", "latitude_deg"), ("longitude_deg", "longitude_deg")):
            if live and status.get(src) is not None:
                new_state[key] = status[src]
        _state.update(new_state)
    except Exception:
        pass
    _state["uptime_s"] = int(
        (datetime.now() -
         datetime.fromisoformat(_state["start_time"])).total_seconds()
    )
    _state.update({"demo_mode": False, "demo_title": None, "heading_simulated": False})
    if _demo["active"]:
        _apply_demo_telemetry()
    # Count recent incident files
    try:
        incident_files = sorted(INCIDENT_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
        if incident_files:
            _state["last_incident"] = incident_files[0].name
    except Exception:
        pass


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).with_name("dashboard.html"))


@app.get("/api/bridge")
def get_bridge():
    return JSONResponse({"url": BRIDGE_URL})


@app.post("/api/bridge")
async def set_bridge(payload: dict):
    """Point the dashboard at the phone after a network change. Private IPv4 addresses only; the port is fixed."""
    global BRIDGE_URL
    try:
        ip = ipaddress.ip_address(str(payload.get("host", "")).strip())
    except ValueError:
        raise HTTPException(status_code=422, detail="Enter an address such as 192.168.1.20")
    if ip.version != 4 or not ip.is_private or ip.is_loopback:
        raise HTTPException(status_code=422, detail="Use the phone's address on your local network")
    BRIDGE_URL = f"http://{ip}:8080"
    BRIDGE_CACHE.write_text(json.dumps({"url": BRIDGE_URL}), encoding="utf-8")
    return JSONResponse({"url": BRIDGE_URL})


@app.get("/api/status")
def get_status():
    update_state()
    return JSONResponse(_state)


# MediaMTX holds each new RTSP reader until the next keyframe (~25 s on this camera), so one reader stays open
# for the life of the server and every consumer (browser relay, live narration) reads its frames.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
NARRATION_FRAME_WIDTH = 1280
_live = {"jpeg": None, "t": 0.0, "recent": collections.deque(maxlen=24)}
_live_lock = threading.Lock()


def _live_reader():
    import cv2
    capture = None
    last_relay = last_keep = 0.0
    while True:
        try:
            if not VIDEO_URL:
                time.sleep(5)
                continue
            if capture is None:
                capture = cv2.VideoCapture(VIDEO_URL, cv2.CAP_FFMPEG)
                if not capture.isOpened():
                    capture.release()
                    capture = None
                    time.sleep(2)
                    continue
            if not capture.grab():          # always drain the stream so MediaMTX never discards for us
                capture.release()
                capture = None
                time.sleep(1)
                continue
            now = time.time()
            if _demo["active"] or now - last_relay < RELAY_MIN_INTERVAL_S:
                continue
            ok, frame = capture.retrieve()
            if not ok:
                continue
            last_relay = now
            _state["frames_analyzed"] = _state.get("frames_analyzed", 0) + 1
            height, width = frame.shape[:2]
            small = frame if width <= RELAY_MAX_WIDTH else cv2.resize(frame, (RELAY_MAX_WIDTH, int(height * RELAY_MAX_WIDTH / width)))
            encoded, image = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not encoded:
                continue
            jpeg = image.tobytes()
            _latest_frame.update({"t": now, "jpeg": jpeg})
            keep = None
            if now - last_keep >= 0.5:
                last_keep = now
                keep = frame if width <= NARRATION_FRAME_WIDTH else cv2.resize(frame, (NARRATION_FRAME_WIDTH, int(height * NARRATION_FRAME_WIDTH / width)))
            with _live_lock:
                _live["jpeg"], _live["t"] = jpeg, now
                if keep is not None:
                    _live["recent"].append((now, keep))
        except Exception:
            if capture is not None:
                capture.release()
            capture = None
            time.sleep(2)


@app.on_event("startup")
def _start_live_reader():
    threading.Thread(target=_live_reader, daemon=True, name="live-reader").start()


@app.get("/api/video")
def get_video():
    """Relay the configured RTSP feed, or the demo clip while a replay runs."""
    if not VIDEO_URL and not _demo["active"]:
        raise HTTPException(status_code=503, detail="Video stream not configured")
    try:
        import cv2
    except ImportError as error:
        raise HTTPException(status_code=503, detail="OpenCV not installed") from error

    def demo_frames():
        entry = next((s for s in _demo_entries() if s["id"] == _demo["active"]), None)
        clip = DEMO_DIR / entry["file"] if entry else None
        capture = cv2.VideoCapture(str(clip)) if clip and clip.exists() else None
        try:
            if capture is None or not capture.isOpened():
                time.sleep(1)
                return
            fps = capture.get(cv2.CAP_PROP_FPS) or 15
            started = time.time()
            shown = 0
            while _demo["active"] == entry["id"]:
                ok, frame = capture.read()
                if not ok:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)   # loop the clip
                    continue
                shown += 1
                if frame.shape[1] > 960:   # stock clips are often 1080p or 4K; keep the relay light
                    frame = cv2.resize(frame, (960, int(frame.shape[0] * 960 / frame.shape[1])))
                encoded, image = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if encoded:
                    _latest_frame.update({"t": time.time(), "jpeg": image.tobytes()})
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + image.tobytes() + b"\r\n"
                delay = started + shown / fps - time.time()
                if delay > 0:
                    time.sleep(delay)
        finally:
            if capture is not None:
                capture.release()

    def live_frames():
        last_t = 0.0
        while not _demo["active"]:
            with _live_lock:
                jpeg, t = _live["jpeg"], _live["t"]
            if jpeg is None or t == last_t or time.time() - t > 10:   # nothing new, or the feed has stopped
                time.sleep(0.05)
                continue
            last_t = t
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    def frames():
        while True:
            if _demo["active"]:
                yield from demo_frames()
            else:
                yield from live_frames()

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/frame/latest")
def latest_frame():
    """Newest frame the viewer is seeing (demo or live); 404 if none in the last 5 s."""
    if not _latest_frame["jpeg"] or time.time() - _latest_frame["t"] > 5:
        raise HTTPException(status_code=404, detail="No recent frame")
    return Response(_latest_frame["jpeg"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/narration")
async def post_narration(payload: dict):
    """Running commentary from the stream analyst. Text is bounded and shown as plain text."""
    skipped = payload.get("skipped") is True
    text = str(payload.get("text") or "")[:1200]
    if not skipped and not text:
        raise HTTPException(status_code=422, detail="text required")
    concern = payload.get("concern") if payload.get("concern") in ("none", "low", "medium", "high") else "none"
    entry = {
        "timestamp": datetime.now().isoformat(), "text": text, "skipped": skipped, "concern": concern,
        "lag_s": payload.get("lag_s") if isinstance(payload.get("lag_s"), (int, float)) else None,
        "directive_match": payload.get("directive_match") is True,
        "evidence": str(payload.get("evidence") or "")[:400],
        "heading_deg": float(payload["heading_deg"]) % 360 if isinstance(payload.get("heading_deg"), (int, float)) else None,
        "heading_simulated": payload.get("heading_simulated") is True,
    }
    if skipped:
        if _narration["latest"]:
            _narration["latest"]["still_since"] = entry["timestamp"]
    else:
        _narration["latest"] = entry
        _narration["history"].insert(0, entry)
        del _narration["history"][NARRATION_HISTORY_MAX:]
    return JSONResponse({"status": "ok"})


@app.get("/api/narration")
def get_narration():
    return JSONResponse(_narration)


@app.get("/api/demo")
def get_demo():
    """List replayable demo scenarios and which one is running."""
    return JSONResponse({
        "active": _demo["active"],
        "elapsed_s": round(time.time() - _demo["started_at"], 1) if _demo["active"] else None,
        "scenarios": [{"id": s["id"], "title": s["title"], "summary": s.get("summary", ""),
                       "available": (DEMO_DIR / s["file"]).exists()} for s in _demo_entries()],
    })


@app.post("/api/narrate_live")
def narrate_live():
    """Narrate what the live feed has shown over the last few seconds."""
    if _demo["active"]:
        raise HTTPException(status_code=409, detail="A demo replay is running; stop it first")
    import clip_story
    directive = (_command["active"] or {}).get("text", "")
    now = time.time()
    with _live_lock:
        recent = [(t, f) for t, f in _live["recent"] if now - t <= 9]
    try:
        result = clip_story.tell_live(directive, seconds=8, recent=recent)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    text = result["story"] if "problems" not in result else "Narration withheld: it could not be reconciled with the tracking measurements."
    _narration["latest"] = {"timestamp": datetime.now().isoformat(), "text": text, "skipped": False, "concern": "none",
                            "lag_s": None, "directive_match": bool(result.get("directive_match")), "evidence": str(result.get("match_evidence") or "")[:400],
                            "heading_deg": None, "heading_simulated": False}
    _narration["history"].insert(0, _narration["latest"])
    del _narration["history"][NARRATION_HISTORY_MAX:]
    return JSONResponse({"text": text})


@app.post("/api/demo/start")
async def start_demo(payload: dict):
    """Start a replay. Only ids from demo_manifest.json are accepted; no paths come from the caller."""
    entry = next((s for s in _demo_entries() if s["id"] == payload.get("id")), None)
    if not entry or not (DEMO_DIR / entry["file"]).exists():
        raise HTTPException(status_code=404, detail="Unknown or missing demo scenario")
    _stop_demo()
    started = time.time()
    _command["active"] = None
    _demo.update({
        "active": entry["id"], "started_at": started,
        "proc": subprocess.Popen([sys.executable, str(Path(__file__).with_name("demo_runner.py")),
                                  entry["id"], str(started)], cwd=str(Path(__file__).parent)),
    })
    return JSONResponse({"status": "started", "id": entry["id"]})


@app.post("/api/demo/stop")
def stop_demo():
    _stop_demo()
    return JSONResponse({"status": "stopped"})


@app.get("/api/log")
def get_log(lines: int = 30):
    """Return last N lines from the patrol log."""
    log_files = list(Path(".").glob("safety1271_*.log"))
    latest = max(log_files, key=lambda file: file.stat().st_mtime) if log_files else None
    if latest:
        all_lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
        return JSONResponse({"lines": all_lines[-lines:]})
    return JSONResponse({"lines": []})


@app.post("/api/alert")
async def receive_alert(payload: dict):
    """Receive alert from the agent and add to recent alerts list."""
    payload["timestamp"] = datetime.now().isoformat()
    payload["demo"] = bool(_demo["active"])
    _state["recent_alerts"].insert(0, payload)
    _state["recent_alerts"] = _state["recent_alerts"][:20]  # keep last 20
    return JSONResponse({"status": "ok"})


@app.post("/api/preflight")
async def receive_preflight(payload: dict):
    """Receive a preflight report from preflight_agent for dashboard display.

    Expected keys (all optional, kept as-is): verdict, checklist,
    weather, airspace, conditions_to_fly, next_go_window.
    """
    allowed = {
        "verdict", "checklist", "weather", "airspace",
        "conditions_to_fly", "next_go_window", "pilot_name",
    }
    report = {key: payload[key] for key in allowed if key in payload}
    report["received_at"] = datetime.now().isoformat()
    _state["preflight"] = report
    try:
        PREFLIGHT_CACHE.write_text(json.dumps(report), encoding="utf-8")
    except OSError:
        pass
    return JSONResponse({"status": "ok"})


@app.post("/api/parking")
async def set_parking(payload: dict):
    """Receive the latest car park count from the patrol agent."""
    try:
        occupied, empty = int(payload.get("occupied")), int(payload.get("empty"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="occupied and empty must be whole numbers")
    if not (0 <= occupied <= 2000 and 0 <= empty <= 2000):
        raise HTTPException(status_code=422, detail="counts out of range")
    coverage = payload.get("coverage") if payload.get("coverage") in ("whole_lot", "part_of_lot") else "part_of_lot"
    confidence = payload.get("confidence") if payload.get("confidence") in ("low", "medium", "high") else "low"
    areas = []
    for a in (payload.get("areas") or [])[:12]:
        try:
            areas.append({"name": str(a["name"])[:80], "count": max(0, min(2000, int(a["count"])))})
        except (KeyError, TypeError, ValueError):
            continue
    entry = {
        "occupied": occupied, "empty": empty, "total_seen": occupied + empty,
        "coverage": coverage, "confidence": confidence,
        "empty_areas": str(payload.get("empty_areas") or "")[:200],
        "areas": areas,
        "orientation": str(payload.get("orientation") or "unknown")[:60],
        "notes": str(payload.get("notes") or "")[:300],
        "counted_at": datetime.now().isoformat(),
        "simulated": bool(_demo["active"]),
    }
    _parking["latest"] = entry
    _parking["history"].insert(0, entry)
    del _parking["history"][PARKING_HISTORY_MAX:]
    return JSONResponse({"status": "ok"})


@app.get("/api/parking")
def get_parking():
    """Latest count plus recent history (empty spaces over time)."""
    return JSONResponse({"latest": _parking["latest"], "history": _parking["history"], "count": len(_parking["history"])})


@app.post("/api/atc")
async def receive_atc(payload: dict):
    """Receive one ATC transcript line (and the listener's verdict, if any) for the dashboard feed."""
    text_fields = ("transcript", "frequency", "severity", "category", "summary", "recommendation")
    entry = {key: str(payload.get(key, ""))[:ATC_FIELD_MAX] for key in text_fields}
    if not entry["transcript"].strip():
        raise HTTPException(status_code=422, detail="transcript is required")
    entry["severity"] = entry["severity"].upper()
    entry["flagged"] = payload.get("flagged") is True
    entry["pinned"] = False
    entry["received_at"] = datetime.now().isoformat()
    _atc_feed.insert(0, entry)
    del _atc_feed[ATC_FEED_MAX:]
    return JSONResponse({"status": "ok"})


@app.post("/api/atc/pin")
async def pin_atc(payload: dict):
    """Mark or unmark one feed line as relevant to the patrol. Identified by its received_at timestamp."""
    key = payload.get("received_at")
    entry = next((e for e in _atc_feed if e["received_at"] == key), None)
    if not entry:
        raise HTTPException(status_code=404, detail="entry not found")
    entry["pinned"] = payload.get("pinned") is True
    return JSONResponse({"pinned": entry["pinned"]})


@app.get("/api/atc")
def get_atc(limit: int = 40):
    """Return the most recent ATC feed entries, newest first."""
    return JSONResponse({"entries": _atc_feed[:max(1, min(limit, ATC_FEED_MAX))]})


def _find_zone(text: str):
    lowered = text.lower()
    return next((zone for zone, words in ZONE_HINTS.items() if any(w in lowered for w in words)), None)


@app.post("/api/command")
async def set_command(payload: dict):
    """Set the operator's search directive. It tells the vision agent what to look for; it never moves the drone."""
    text = " ".join(str(payload.get("text", "")).split())[:COMMAND_MAX_CHARS]
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    entry = {"id": _command["next_id"], "text": text, "created_at": datetime.now().isoformat(),
             "target_zone": _find_zone(text)}
    _command["next_id"] += 1
    _command["active"] = entry
    _command["history"].insert(0, entry)
    del _command["history"][COMMAND_HISTORY_MAX:]
    return JSONResponse(entry)


@app.get("/api/command")
def get_command():
    """Return the active directive and recent history."""
    return JSONResponse({"active": _command["active"], "history": _command["history"]})


@app.delete("/api/command")
def clear_command():
    """Clear the active directive; the agent returns to normal patrol."""
    _command["active"] = None
    return JSONResponse({"status": "ok"})
