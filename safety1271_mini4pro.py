"""
=============================================================================
  Safety1271 — DJI Mini 4 Pro Edition
  Autonomous Church / School Compound Security Agent
  Gloo AI Hackathon 2026 | "Build AI for Human Flourishing"
=============================================================================

HARDWARE — DJI MINI 4 PRO (RC-N2 + Android phone)
----------------------------------------------------
  Aircraft        : DJI Mini 4 Pro
  Weight          : 249 g  — under 250g threshold
                    NOTE: Still requires Part 107 for security patrol
                    (commercial / non-recreational use — regardless of weight)
  Camera          : 1 × 24mm, 1/1.3" CMOS, f/1.7, 4K/60fps HDR
  Obstacle avoid. : APAS 5.0 omnidirectional
  Flight time     : 34 min standard / 45 min with Battery Plus
  Wind resistance : Level 5 (38 kph)
  Controller      : DJI RC-N2 → Android phone via USB-C
  MSDK            : V5 supported — full programmatic control from Python

WHAT'S DIFFERENT FROM THE AIR 3 VERSION
-----------------------------------------
  RESTORED: MSDK Android bridge app (full drone control from Python)
  RESTORED: Virtual sticks → hold_and_observe (drone actually stops)
  RESTORED: KMZ auto-upload via MSDK (no manual DJI Fly import needed)
  RESTORED: Mission pause / resume / RTH from Python
  RESTORED: Gimbal control per waypoint via MSDK

  REMOVED:  Dual camera (Mini 4 Pro is single 24mm wide only)
  REMOVED:  request_pilot_hold (replaced by actual hold_and_observe)
  REMOVED:  Tele zoom references

  KEPT:     Full RTMP video pipeline
  KEPT:     Claude vision frame analysis + all security tools
  KEPT:     SMS + email with incident photo
  KEPT:     Pre-flight agent integration
  KEPT:     ATC listener integration
  KEPT:     TEST_MODE (demo without Android bridge — see .env)

ARCHITECTURE
------------
  ┌──────────────────────────────────────────────────────────┐
  │                   LAPTOP (this script)                   │
  │                                                          │
  │  ┌──────────────┐    ┌────────────────────────────────┐  │
  │  │  MediaMTX    │    │    Safety1271 Agent         │  │
  │  │  RTMP :1935  │───▶│    Claude Vision Model         │  │
  │  │  RTSP :8554  │    │    Tool Calling Engine         │  │
  │  └──────┬───────┘    └───────────┬────────────────────┘  │
  │         │                        │ HTTP commands          │
  │  ┌──────┴───────┐    ┌───────────┴────────────────────┐  │
  │  │  ATC Listener│    │  Pre-Flight Agent              │  │
  │  │  (background)│    │  (runs once at startup)        │  │
  │  └──────────────┘    └────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────┘
              ▲ RTMP stream          ▼ HTTP /mission /pause
              │                      │ /resume /rth /command
  ┌───────────┴──────────────────────┴──────────────────────┐
  │           Android Phone (RC-N2 attached via USB-C)       │
  │                                                          │
  │   MSDK V5 Bridge App                                     │
  │   ├── POST /mission   → upload KMZ + start patrol        │
  │   ├── POST /pause     → pause waypoint mission (hover)   │
  │   ├── POST /resume    → resume paused mission            │
  │   ├── POST /rth       → trigger Return to Home           │
  │   ├── POST /command   → virtual sticks / gimbal          │
  │   ├── GET  /status    → battery %, GPS, altitude, state  │
  │   └── RTMP push       → video stream to MediaMTX         │
  └───────────────────────────────────────────────────────────┘
                         │ DJI O4 (20 km range)
                         ▼
  ┌──────────────────────────────────────────────────────────┐
  │              DJI Mini 4 Pro (airborne)                   │
  │   Waypoint mission uploaded + started by Python          │
  │   Omnidirectional obstacle avoidance (APAS 5.0)         │
  │   Python can pause/resume/RTH/virtual-stick mid-flight   │
  │   Auto-RTH on low battery or RC signal loss              │
  └──────────────────────────────────────────────────────────┘

MSDK BRIDGE APP
---------------
  Build from DJI MSDK V5 sample:
  https://github.com/dji-sdk/Mobile-SDK-Android-V5/tree/master/SampleCode-V5
  Implement 6 HTTP endpoints (see Section 11 of the build doc).
  Key files to reference:
    WaypointMissionFragment.kt  — mission upload / start / pause / resume
    VirtualStickFragment.kt     — virtual stick commands
    GimbalFragment.kt           — gimbal control

  For the hackathon demo: set TEST_MODE=true in .env
  → All MSDK bridge calls are simulated. Agent logic runs fully.
  → Demo works with just a webcam and laptop — no drone required.

SETUP CHECKLIST
---------------
  1. pip install anthropic twilio sendgrid opencv-python pillow python-dotenv requests astral
  2. Install MediaMTX: brew install mediamtx (macOS) or see releases page
  3. Copy .env.example → .env and fill in all values
  4. Deploy MSDK Bridge App to Android phone → plug into RC-N2 via USB-C
  5. Run: python safety1271_mini4pro.py --generate-kmz
     → Generates patrol_route.kmz
  6. Run: python safety1271_mini4pro.py --preflight
     → Runs Part 107 compliance check
  7. Start MediaMTX: mediamtx
  8. Start Android bridge app — confirm it shows "Connected to aircraft"
  9. Fly drone to home point and arm
  10. Run: python safety1271_mini4pro.py
      → Agent uploads KMZ, starts mission, monitors video
=============================================================================
"""

import os
import sys
import cv2
import time
import json
import base64
import zipfile
import logging
import argparse
import threading
from io        import BytesIO
from pathlib   import Path
from datetime  import datetime
from typing    import Optional

from gloo_client import GlooAnthropicCompat, complete, run_agent, MODELS, GLOO_TRADITION
import requests
from PIL       import Image
from dotenv    import load_dotenv
from twilio_client import create_twilio_client, get_twilio_from_phone
from sendgrid   import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, Content, Attachment,
    FileContent, FileName, FileType, Disposition
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt = "%H:%M:%S",
    handlers= [
        logging.StreamHandler(),
        logging.FileHandler(f"patrol_{datetime.now().strftime('%Y%m%d')}.log")
    ]
)
log = logging.getLogger("Safety1271")

# ── Configuration ─────────────────────────────────────────────────────────────
COMPOUND_NAME    = os.getenv("COMPOUND_NAME",    "Bennie Bough Church Campus")
COMPOUND_ADDRESS = os.getenv("COMPOUND_ADDRESS", "8304 Old Keene Mill Rd, Springfield, VA 22152")
DRONE_MODEL      = "DJI Mini 4 Pro"
PATROL_ALT_M     = float(os.getenv("PATROL_ALTITUDE_M",  "15"))   # ~50ft
PATROL_SPEED_MS  = float(os.getenv("PATROL_SPEED_MS",    "2.5"))
FRAME_INTERVAL_S = float(os.getenv("FRAME_INTERVAL_SEC", "4.0"))
HOLD_FRAME_INT_S = float(os.getenv("HOLD_FRAME_INTERVAL_SEC", "1.5")) # faster during hold
RTMP_STREAM_URL  = os.getenv("RTMP_STREAM_URL", "rtsp://localhost:8554/live/drone")
MSDK_BRIDGE_URL  = os.getenv("MSDK_BRIDGE_URL", "http://192.168.1.100:8080")
TEST_MODE        = os.getenv("TEST_MODE", "false").lower() == "true"
ALERT_PHONE      = os.getenv("ALERT_PHONE",  "+1XXXXXXXXXX")
ALERT_EMAIL      = os.getenv("ALERT_EMAIL",  "security@church.org")
INCIDENT_DIR     = Path(os.getenv("INCIDENT_DIR", "./incidents"))
INCIDENT_DIR.mkdir(exist_ok=True)

# ── Patrol Waypoints ──────────────────────────────────────────────────────────
# Single camera (24mm wide) — gimbal pitch controls angle
# (lat, lon, label, gimbal_deg, speed_ms, hover_s)
PATROL_WAYPOINTS = [
    (38.7742,  -77.1892,  "Main Gate",              -45,  2.0,  3),
    (38.7748,  -77.1885,  "North Parking Lot",      -60,  2.5,  0),
    (38.7755,  -77.1878,  "NE Corner / Playground", -45,  2.0,  5),
    (38.7758,  -77.1870,  "East Entrance",          -45,  2.0,  2),
    (38.7752,  -77.1862,  "Rear / Loading Dock",    -45,  2.0,  5),
    (38.7744,  -77.1865,  "SE Corner",              -60,  2.5,  0),
    (38.7738,  -77.1875,  "South Parking Lot",      -60,  2.5,  0),
    (38.7735,  -77.1885,  "SW Corner / Perimeter",  -60,  2.5,  0),
    (38.7740,  -77.1895,  "West Side / Garden",     -45,  2.0,  2),
    (38.7742,  -77.1892,  "Return to Main Gate",    -45,  2.5,  0),
]

# ── Clients ───────────────────────────────────────────────────────────────────
claude_client = GlooAnthropicCompat()  # Routes through Gloo AI platform
twilio_client = create_twilio_client()

# ── Session State ─────────────────────────────────────────────────────────────
session = {
    "start_time":          datetime.now(),
    "frames_analyzed":     0,
    "incidents_logged":    0,
    "alerts_sent":         0,
    "lockdowns_triggered": 0,
    "holds_executed":      0,
    "current_waypoint":    0,
    "drone_state":         "patrolling",  # patrolling | holding | returning
    "last_alert_time":     0.0,
    "hold_end_time":       0.0,
}


# ══════════════════════════════════════════════════════════════════════════════
#  MSDK BRIDGE — HTTP client for Android bridge app
#  All calls are no-ops in TEST_MODE (prints instead of sending HTTP)
# ══════════════════════════════════════════════════════════════════════════════

class MSDKBridge:
    """
    HTTP client for the Android MSDK V5 Bridge App running on the phone
    attached to the RC-N2 controller.

    Build from DJI sample:
    github.com/dji-sdk/Mobile-SDK-Android-V5/tree/master/SampleCode-V5

    In TEST_MODE all calls are simulated — useful for demos without drone.
    """

    def __init__(self, base_url: str, test_mode: bool = False):
        self.base    = base_url.rstrip("/")
        self.test    = test_mode
        if test_mode:
            log.info("🧪 TEST_MODE — MSDK bridge calls will be simulated")

    def _post(self, path: str, payload: dict = {}) -> dict:
        if self.test:
            log.info(f"[TEST] MSDK {path} → {json.dumps(payload)[:80]}")
            return {"status": "ok", "simulated": True}
        try:
            resp = requests.post(
                f"{self.base}{path}",
                json    = payload,
                timeout = 5
            )
            return resp.json()
        except Exception as e:
            log.error(f"MSDK bridge error {path}: {e}")
            return {"status": "error", "message": str(e)}

    def _get(self, path: str) -> dict:
        if self.test:
            return {
                "battery_pct": 87,
                "lat": PATROL_WAYPOINTS[session["current_waypoint"] % len(PATROL_WAYPOINTS)][0],
                "lon": PATROL_WAYPOINTS[session["current_waypoint"] % len(PATROL_WAYPOINTS)][1],
                "altitude_m": PATROL_ALT_M,
                "state": session["drone_state"],
                "simulated": True
            }
        try:
            return requests.get(f"{self.base}{path}", timeout=5).json()
        except Exception as e:
            log.error(f"MSDK GET {path}: {e}")
            return {}

    def upload_and_start_mission(self, kmz_path: str) -> bool:
        """Upload KMZ file to drone via MSDK and start autonomous patrol."""
        log.info(f"Uploading mission: {kmz_path}")
        with open(kmz_path, "rb") as f:
            kmz_b64 = base64.b64encode(f.read()).decode()
        result = self._post("/mission", {"kmz_b64": kmz_b64})
        ok = result.get("status") == "ok"
        log.info(f"Mission upload: {'✅ OK' if ok else '❌ FAILED'}")
        return ok

    def pause_mission(self) -> bool:
        """Pause waypoint mission — drone hovers in current position."""
        result = self._post("/pause")
        ok = result.get("status") == "ok"
        if ok:
            session["drone_state"] = "holding"
            log.info("⏸️  Mission paused — drone hovering")
        return ok

    def resume_mission(self) -> bool:
        """Resume paused waypoint mission."""
        result = self._post("/resume")
        ok = result.get("status") == "ok"
        if ok:
            session["drone_state"] = "patrolling"
            log.info("▶️  Mission resumed — patrol continuing")
        return ok

    def virtual_stick_hold(self, duration_ms: int = 500):
        """
        Send zero-movement virtual stick commands to hold drone stationary.
        Call repeatedly while holding position — drone stays frozen in place.
        This is the core of hold_and_observe: Python replaces the RC joystick.
        """
        self._post("/command", {
            "type":        "virtual_stick",
            "pitch":       0.0,    # no forward/backward
            "roll":        0.0,    # no left/right
            "throttle":    0.0,    # no up/down
            "yaw":         0.0,    # no rotation
            "duration_ms": duration_ms
        })

    def set_gimbal(self, pitch_deg: float):
        """Set gimbal pitch angle (0=horizontal, -90=nadir/straight down)."""
        self._post("/command", {
            "type":      "gimbal",
            "pitch_deg": pitch_deg
        })

    def return_to_home(self) -> bool:
        """Trigger Return to Home — drone flies back to launch point and lands."""
        result = self._post("/rth")
        ok = result.get("status") == "ok"
        if ok:
            session["drone_state"] = "returning"
            log.info("🏠 RTH triggered — drone returning to home")
        return ok

    def get_status(self) -> dict:
        """Get current drone telemetry: battery, GPS, altitude, state."""
        return self._get("/status")


# Instantiate bridge
bridge = MSDKBridge(MSDK_BRIDGE_URL, test_mode=TEST_MODE)


# ══════════════════════════════════════════════════════════════════════════════
#  KMZ PATROL ROUTE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_kmz(output_path: str = "patrol_route.kmz"):
    """
    Generate a DJI Fly / MSDK–compatible WPML waypoint mission KMZ.
    For Mini 4 Pro: single camera, gimbal pitch per waypoint, hover + photo.
    """
    waypoint_elements = []
    for i, (lat, lon, label, gimbal_deg, speed, hover_s) in enumerate(PATROL_WAYPOINTS):
        actions = []

        # Action 1: Set gimbal pitch
        actions.append(f"""
            <wpml:action>
              <wpml:actionId>{i * 10 + 1}</wpml:actionId>
              <wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>
                <wpml:gimbalPitchRotateAngle>{gimbal_deg}</wpml:gimbalPitchRotateAngle>
                <wpml:gimbalRollRotateAngle>0</wpml:gimbalRollRotateAngle>
                <wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>
                <wpml:gimbalRotateTimeEnable>0</wpml:gimbalRotateTimeEnable>
                <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        # Action 2: Hover if needed
        if hover_s > 0:
            actions.append(f"""
            <wpml:action>
              <wpml:actionId>{i * 10 + 2}</wpml:actionId>
              <wpml:actionActuatorFunc>hover</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:hoverTime>{hover_s}</wpml:hoverTime>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        # Action 3: Photo at each waypoint
        actions.append(f"""
            <wpml:action>
              <wpml:actionId>{i * 10 + 3}</wpml:actionId>
              <wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        waypoint_elements.append(f"""
      <Placemark>
        <name>{i+1}. {label}</name>
        <Point>
          <coordinates>{lon},{lat},{PATROL_ALT_M}</coordinates>
        </Point>
        <wpml:index>{i}</wpml:index>
        <wpml:executeHeight>{PATROL_ALT_M}</wpml:executeHeight>
        <wpml:waypointSpeed>{speed}</wpml:waypointSpeed>
        <wpml:waypointHeadingParam>
          <wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode>
        </wpml:waypointHeadingParam>
        <wpml:waypointTurnParam>
          <wpml:waypointTurnMode>coordinateTurn</wpml:waypointTurnMode>
          <wpml:waypointTurnDampingDist>1.0</wpml:waypointTurnDampingDist>
        </wpml:waypointTurnParam>
        <wpml:actionGroup>
          <wpml:actionGroupId>{i}</wpml:actionGroupId>
          <wpml:actionGroupStartIndex>{i}</wpml:actionGroupStartIndex>
          <wpml:actionGroupEndIndex>{i}</wpml:actionGroupEndIndex>
          <wpml:actionGroupMode>sequence</wpml:actionGroupMode>
          <wpml:actionTrigger>
            <wpml:actionTriggerType>reachPoint</wpml:actionTriggerType>
          </wpml:actionTrigger>
          {"".join(actions)}
        </wpml:actionGroup>
      </Placemark>""")

    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:wpml="http://www.dji.com/wpmz/1.0.6">
  <Document>
    <name>Safety1271 Patrol — {COMPOUND_NAME}</name>
    <wpml:author>Safety1271 Mini4Pro Agent</wpml:author>
    <wpml:createTime>{int(datetime.now().timestamp() * 1000)}</wpml:createTime>
    <wpml:updateTime>{int(datetime.now().timestamp() * 1000)}</wpml:updateTime>
    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>goHome</wpml:finishAction>
      <wpml:exitOnRCLost>goBack</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>{PATROL_SPEED_MS}</wpml:globalTransitionalSpeed>
      <wpml:droneInfo>
        <wpml:droneEnumValue>60</wpml:droneEnumValue>  <!-- DJI Mini 4 Pro -->
        <wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
      </wpml:droneInfo>
    </wpml:missionConfig>
    <Folder>
      <name>Waypoints</name>
      <wpml:templateType>waypoint</wpml:templateType>
      <wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>
      <wpml:waylineCoordinateSysParam>
        <wpml:coordinateMode>WGS84</wpml:coordinateMode>
        <wpml:heightMode>relativeToStartPoint</wpml:heightMode>
      </wpml:waylineCoordinateSysParam>
      <wpml:autoFlightSpeed>{PATROL_SPEED_MS}</wpml:autoFlightSpeed>
      {"".join(waypoint_elements)}
    </Folder>
  </Document>
</kml>"""

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/waylines.wpml", kml_content)
        zf.writestr("wpmz/template.kml",  kml_content)

    log.info(f"✅ KMZ generated: {output_path}")
    log.info(f"   {len(PATROL_WAYPOINTS)} waypoints | {PATROL_ALT_M}m altitude | {PATROL_SPEED_MS}m/s")
    log.info("   MSDK bridge will upload this automatically at startup")

    print("\n  PATROL ROUTE SUMMARY")
    print("  " + "─" * 55)
    for i, (lat, lon, label, gimbal, speed, hover) in enumerate(PATROL_WAYPOINTS):
        print(f"  {i+1:2}. {label:<28} {gimbal:4}°  {speed}m/s  {hover}s hover")
    print("  " + "─" * 55)
    print(f"  Home point: {PATROL_WAYPOINTS[0][2]} ({PATROL_WAYPOINTS[0][0]}, {PATROL_WAYPOINTS[0][1]})")
    print(f"  End action: Return to Home\n")


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _send_sms(message: str):
    try:
        twilio_client.messages.create(
            body  = message,
            from_ = get_twilio_from_phone(),
            to    = ALERT_PHONE
        )
        log.info(f"SMS sent: {message[:60]}...")
    except Exception as e:
        log.error(f"SMS failed: {e}")


def _send_email(subject: str, html_body: str, image_path: Optional[Path] = None):
    try:
        sg   = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        mail = Mail(
            from_email = "security@safety1271.ai",
            to_emails  = ALERT_EMAIL,
            subject    = subject,
        )
        mail.add_content(Content("text/html", html_body))

        if image_path and image_path.exists():
            with open(image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            mail.add_attachment(Attachment(
                FileContent(encoded),
                FileName(image_path.name),
                FileType("image/jpeg"),
                Disposition("attachment")
            ))
        sg.send(mail)
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def _alert_html(title: str, color: str, rows: list,
                drone_action: str = "", analysis: str = "") -> str:
    row_html = "".join(
        f"<tr style='background:{'#f9f9f9' if i%2 else '#fff'}'>"
        f"<td style='padding:8px 12px;font-weight:600;color:#555;width:150px'>{k}</td>"
        f"<td style='padding:8px 12px'>{v}</td></tr>"
        for i, (k, v) in enumerate(rows)
    )
    drone_block = ""
    if drone_action:
        drone_block = f"""
        <div style='margin:20px 0;padding:14px;background:#e8f4fd;
                    border-left:4px solid #2196F3;border-radius:4px'>
            <div style='font-weight:700;margin-bottom:6px'>🚁 DRONE ACTION TAKEN</div>
            <div>{drone_action}</div>
        </div>"""
    analysis_block = ""
    if analysis:
        analysis_block = f"""
        <div style='margin-top:16px;padding:14px;background:#1a1a2e;border-radius:4px'>
            <div style='color:#aaa;font-size:12px;font-family:monospace;margin-bottom:8px'>
                CLAUDE ANALYSIS
            </div>
            <div style='color:#00ff88;font-family:monospace;font-size:13px;
                        white-space:pre-wrap'>{analysis}</div>
        </div>"""
    return f"""
    <div style='font-family:Calibri,Arial,sans-serif;max-width:640px;margin:0 auto'>
        <div style='background:#1a1a2e;padding:18px 24px;border-radius:4px 4px 0 0'>
            <div style='color:#fff;font-size:20px;font-weight:700'>
                🚁 Safety1271 — {COMPOUND_NAME}
            </div>
            <div style='color:#aaa;font-size:13px'>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
        </div>
        <div style='background:{color};padding:14px 24px;text-align:center'>
            <div style='color:#fff;font-size:24px;font-weight:700'>{title}</div>
        </div>
        <div style='padding:20px 24px;background:#fff'>
            <table style='width:100%;border-collapse:collapse'>{row_html}</table>
            {drone_block}
            {analysis_block}
            <p style='margin-top:20px;font-size:11px;color:#aaa;
                      border-top:1px solid #eee;padding-top:10px'>
                Safety1271 AI | {DRONE_MODEL} | {COMPOUND_ADDRESS}
                {'  |  ⚠️ TEST MODE — No drone commands sent' if TEST_MODE else ''}
            </p>
        </div>
    </div>"""


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "all_clear",
        "description": "Scene is normal. Patrol continues uninterrupted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"}
            },
            "required": ["note"]
        }
    },
    {
        "name": "log_incident",
        "description": "Minor or ambiguous observation. Log it, patrol continues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium"]},
                "zone":     {"type": "string"},
                "note":     {"type": "string"}
            },
            "required": ["severity", "zone", "note"]
        }
    },
    {
        "name": "hold_and_observe",
        "description": (
            "Pause the patrol mission and hold the drone in place using virtual sticks. "
            "Use when you see something unusual but not yet confirmed threatening. "
            "The drone will hover stationary while you receive more frames. "
            "After the hold, you must decide: all_clear (resume) or alert_security_team (escalate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone":           {"type": "string"},
                "reason":         {"type": "string", "description": "What you saw"},
                "hold_seconds":   {"type": "integer", "default": 30,
                                   "description": "How long to hold before auto-resume"},
                "increase_frame_rate": {"type": "boolean", "default": True,
                                        "description": "Sample frames faster during hold"}
            },
            "required": ["zone", "reason"]
        }
    },
    {
        "name": "alert_security_team",
        "description": "Confirmed suspicious or threatening activity. Alert humans immediately. Drone holds on scene.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threat_level":  {"type": "string", "enum": ["low","medium","high","critical"]},
                "zone":          {"type": "string"},
                "description":   {"type": "string"},
                "persons_count": {"type": "integer"},
                "hold_drone_on_scene": {"type": "boolean", "default": True,
                                        "description": "Keep drone hovering for live view"}
            },
            "required": ["threat_level", "zone", "description"]
        }
    },
    {
        "name": "lockdown_alert",
        "description": "Credible active threat requiring building lockdown. Drone holds overhead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lockdown_type":  {"type": "string",
                                   "enum": ["lockdown","shelter_in_place","evacuate"]},
                "affected_zones": {"type": "array", "items": {"type": "string"}},
                "reason":         {"type": "string"}
            },
            "required": ["lockdown_type", "affected_zones", "reason"]
        }
    },
    {
        "name": "call_emergency_services",
        "description": "Active emergency: weapon, violence, fire, medical crisis. Drone holds overhead indefinitely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "emergency_type":    {"type": "string",
                                      "enum": ["police","fire","medical","all"]},
                "location":          {"type": "string"},
                "situation_summary": {"type": "string"}
            },
            "required": ["emergency_type", "location", "situation_summary"]
        }
    }
]


# ══════════════════════════════════════════════════════════════════════════════
#  HOLD AND OBSERVE LOOP
#  The heart of what MSDK virtual sticks enable.
#  Runs in a thread while the main patrol loop continues analyzing frames.
# ══════════════════════════════════════════════════════════════════════════════

def _hold_loop(hold_seconds: int, faster_frames: bool):
    """
    Send virtual stick zero-movement commands repeatedly to keep drone hovering.
    Virtual sticks need continuous input — if commands stop, the drone may drift.
    """
    session["drone_state"]  = "holding"
    session["hold_end_time"] = time.time() + hold_seconds
    if faster_frames:
        session["active_hold"] = True   # signal patrol loop to use faster interval

    log.info(f"🔒 Hold active for {hold_seconds}s — sending virtual sticks...")
    deadline = time.time() + hold_seconds

    while time.time() < deadline and session["drone_state"] == "holding":
        bridge.virtual_stick_hold(duration_ms=500)
        time.sleep(0.4)   # send command every 400ms — drone expects ~10Hz

    # Auto-resume if still holding (no escalation happened)
    if session["drone_state"] == "holding":
        log.info("Hold expired — resuming patrol automatically")
        bridge.resume_mission()
        session["drone_state"]  = "patrolling"
        session["active_hold"]  = False


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def execute_tool(tool_name: str, args: dict,
                 frame_path: Optional[Path] = None,
                 claude_analysis: str = "") -> str:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    zone = args.get("zone", "Unknown Zone")

    if tool_name == "all_clear":
        log.info(f"✅ ALL CLEAR — {args.get('note','')}")
        # If hold just expired and this is the first clear frame, resume mission
        if session["drone_state"] == "holding":
            bridge.resume_mission()
            session["active_hold"] = False
        return "Clear."

    elif tool_name == "log_incident":
        sev = args.get("severity", "low").upper()
        log.warning(f"📋 [{sev}] Zone: {zone} — {args.get('note','')}")
        session["incidents_logged"] += 1
        return f"Incident logged [{sev}]"

    elif tool_name == "hold_and_observe":
        reason     = args.get("reason", "")
        hold_s     = int(args.get("hold_seconds", 30))
        faster     = bool(args.get("increase_frame_rate", True))

        log.warning(f"👁️  HOLD & OBSERVE — Zone: {zone} | {reason}")

        # Pause waypoint mission via MSDK
        bridge.pause_mission()
        session["holds_executed"] += 1

        # Start virtual stick hold in background thread
        hold_thread = threading.Thread(
            target = _hold_loop,
            args   = (hold_s, faster),
            daemon = True
        )
        hold_thread.start()

        return f"Holding at {zone} for {hold_s}s — observing"

    elif tool_name == "alert_security_team":
        if time.time() - session["last_alert_time"] < 30:
            return "Alert suppressed — cooldown active."

        level   = args.get("threat_level", "medium").upper()
        desc    = args.get("description", "")
        persons = args.get("persons_count", 0)
        hold    = args.get("hold_drone_on_scene", True)

        colors  = {"LOW":"#c9a84c","MEDIUM":"#cc6600","HIGH":"#cc2200","CRITICAL":"#8b0000"}
        emojis  = {"LOW":"⚠️","MEDIUM":"🚨","HIGH":"⛔","CRITICAL":"🆘"}
        color   = colors.get(level, "#cc2200")
        emoji   = emojis.get(level, "🚨")

        # Pause mission and hold drone on scene if requested
        drone_action = ""
        if hold and session["drone_state"] == "patrolling":
            bridge.pause_mission()
            # Start virtual stick hold for 5 minutes (security team decides when to resume)
            hold_thread = threading.Thread(
                target = _hold_loop, args = (300, False), daemon = True
            )
            hold_thread.start()
            drone_action = f"Drone holding position over {zone} — live feed active"

        # Get live telemetry for location accuracy
        status = bridge.get_status()

        sms = (
            f"{emoji} SECURITY ALERT [{level}] — {ts}\n"
            f"Zone: {zone}\n"
            f"Persons: {persons}\n"
            f"Battery: {status.get('battery_pct','?')}%\n\n"
            f"{desc}\n\n"
            f"{'🚁 Drone holding on scene for live view' if hold else '🚁 Patrol continuing'}"
        )
        _send_sms(sms)

        html = _alert_html(
            title        = f"{emoji} {level} SECURITY ALERT",
            color        = color,
            rows         = [
                ("Time",        ts),
                ("Zone",        zone),
                ("Threat",      level),
                ("Persons",     str(persons)),
                ("Battery",     f"{status.get('battery_pct','?')}%"),
                ("GPS",         f"{status.get('lat','?')}, {status.get('lon','?')}"),
                ("Altitude",    f"{status.get('altitude_m','?')}m AGL"),
                ("Description", desc),
            ],
            drone_action = drone_action,
            analysis     = claude_analysis
        )
        _send_email(
            subject    = f"[{level}] Security Alert — {zone} — {COMPOUND_NAME}",
            html_body  = html,
            image_path = frame_path
        )

        session["alerts_sent"]     += 1
        session["last_alert_time"]  = time.time()
        log.critical(f"🚨 ALERT [{level}] Zone: {zone} — {desc[:80]}")
        return f"Alert sent [{level}] — {zone}"

    elif tool_name == "lockdown_alert":
        ltype  = args.get("lockdown_type","lockdown").upper()
        zones  = ", ".join(args.get("affected_zones", [zone]))
        reason = args.get("reason","")

        # Pause mission and hold
        if session["drone_state"] == "patrolling":
            bridge.pause_mission()
            hold_thread = threading.Thread(
                target=_hold_loop, args=(600, False), daemon=True
            )
            hold_thread.start()

        _send_sms(
            f"🔒 {ltype} — {COMPOUND_NAME}\n"
            f"Zones: {zones}\nReason: {reason}\nTime: {ts}\n"
            f"🚁 Drone holding for overhead view"
        )
        _send_email(
            subject   = f"[LOCKDOWN] {ltype} — {COMPOUND_NAME}",
            html_body = _alert_html(
                title  = f"🔒 {ltype}",
                color  = "#4a0000",
                rows   = [("Time",ts),("Type",ltype),("Zones",zones),("Reason",reason)],
                drone_action = f"Drone holding overhead — {zone}",
                analysis = claude_analysis
            ),
            image_path = frame_path
        )
        session["lockdowns_triggered"] += 1
        log.critical(f"🔒 {ltype} — {zones}: {reason}")
        return f"{ltype} triggered"

    elif tool_name == "call_emergency_services":
        etype   = args.get("emergency_type","police").upper()
        loc     = args.get("location", zone)
        summary = args.get("situation_summary","")

        # Hold drone indefinitely overhead
        if session["drone_state"] == "patrolling":
            bridge.pause_mission()
            hold_thread = threading.Thread(
                target=_hold_loop, args=(3600, False), daemon=True
            )
            hold_thread.start()

        _send_sms(
            f"🆘 CALL 911 NOW — {etype}\n"
            f"Location: {loc}\nAddress: {COMPOUND_ADDRESS}\n\n"
            f"{summary}\n\n🚁 Drone holding overhead — live feed active"
        )
        _send_email(
            subject   = f"[EMERGENCY] {etype} — {loc} — CALL 911",
            html_body = _alert_html(
                title  = f"🆘 EMERGENCY — {etype}",
                color  = "#000000",
                rows   = [("Time",ts),("Type",etype),
                          ("Location",loc),("Address",COMPOUND_ADDRESS),("Summary",summary)],
                drone_action = "Drone holding overhead indefinitely for emergency services",
                analysis = claude_analysis
            ),
            image_path = frame_path
        )
        session["alerts_sent"] += 1
        log.critical(f"🆘 EMERGENCY [{etype}] @ {loc}: {summary}")
        return f"Emergency [{etype}] — drone overhead, call 911: {COMPOUND_ADDRESS}"

    return f"Unknown tool: {tool_name}"


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""
You are Safety1271, an AI security agent protecting {COMPOUND_NAME}
at {COMPOUND_ADDRESS}.

HARDWARE:
  Drone   : {DRONE_MODEL}  |  249g  |  APAS 5.0 omnidirectional obstacle avoidance
  Camera  : 24mm wide-angle, f/1.7 aperture — single camera (no telephoto)
  Altitude: {PATROL_ALT_M}m AGL — ground footprint ~{int(2*PATROL_ALT_M*0.74)}×{int(2*PATROL_ALT_M*0.42)}m
  MSDK    : V5 — Python can PAUSE, RESUME, HOLD, and RTH the drone directly.

VIRTUAL STICKS CAPABILITY:
  When you call hold_and_observe, the drone ACTUALLY STOPS mid-flight.
  Python sends zero-movement virtual stick commands via MSDK — the drone
  hovers frozen in place while you receive more frames of the same scene.
  After the hold expires (or you escalate), the mission auto-resumes.
  This is different from the Air 3 — use this confidently.

DECISION FRAMEWORK — call exactly ONE tool per frame:

  all_clear          → Normal activity. Patrol continues.
                       Also call this after a hold if threat resolved.

  log_incident       → Minor anomaly (loitering, unusual vehicle, open door).
                       Note and continue — no drone interruption.

  hold_and_observe   → Something unusual but ambiguous. STOP THE DRONE HERE.
                       Set increase_frame_rate=true for faster sampling.
                       Use BEFORE escalating — always try to confirm first.
                       After 2-3 continued suspicious frames → escalate.
                       After 2-3 normal frames → all_clear (drone resumes).

  alert_security_team → Confirmed suspicious activity. Alert humans.
                        Set hold_drone_on_scene=true to keep live overhead view.

  lockdown_alert     → Credible active threat. Trigger lockdown.
                       Drone holds overhead automatically.

  call_emergency_services → Active emergency only: weapon, violence, fire,
                            medical collapse. Drone holds overhead indefinitely.

HOLD_AND_OBSERVE IS YOUR MOST POWERFUL TOOL:
  Unlike the Air 3 (where you had to ask the human pilot to hold), here
  you control the drone directly. Use hold_and_observe freely whenever
  you're uncertain. It costs a few seconds of patrol time but prevents
  false positives. The drone is safe to hold — obstacle avoidance stays on.

PROTECTED POPULATION:
  Church and school campus. Children, elderly, vulnerable individuals
  present at all times. Extra vigilance near:
  - Playground and recreation areas (NE Corner waypoint)
  - Parking lot (vehicle / pedestrian interactions)
  - All building entrances and exits
  - Any scene involving a lone adult approaching a child → immediate hold

CONTEXT:
  You receive short_term_memory of your last 5 decisions to help track
  developing situations. Use it — a person who was 'log_incident' two
  frames ago and is now still present should be escalated to hold.
  {'⚠️  TEST_MODE ACTIVE — All drone commands are simulated.' if TEST_MODE else ''}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  VIDEO CAPTURE & FRAME ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def capture_frame(cap: cv2.VideoCapture) -> Optional[tuple]:
    ret, frame = cap.read()
    if not ret:
        return None

    frame_path = INCIDENT_DIR / f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    cv2.imwrite(str(frame_path), frame)

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    img.thumbnail((1280, 720), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    return frame, b64, frame_path


def analyze_frame(b64_image: str, short_term_memory: list,
                  waypoint_label: str) -> tuple:
    memory_text = ""
    if short_term_memory:
        memory_text = "\n\nPRIOR OBSERVATIONS (most recent last):\n" + \
                      "\n".join(f"  [{m['time']}] {m['tool']}: {m['note']}"
                                for m in short_term_memory[-5:])

    response = claude_client.messages.create(
        model=MODELS["patrol"],
        max_tokens= 1024,
        tradition=GLOO_TRADITION,
        system    = SYSTEM_PROMPT,
        tools     = TOOLS,
        messages  = [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type":       "base64",
                        "media_type": "image/jpeg",
                        "data":       b64_image
                    }
                },
                {
                    "type": "text",
                    "text": (
                        f"POSITION: {waypoint_label}\n"
                        f"DRONE STATE: {session['drone_state'].upper()}\n"
                        f"ALTITUDE: {PATROL_ALT_M}m AGL\n"
                        f"TIME: {datetime.now().strftime('%H:%M:%S')}"
                        f"{memory_text}\n\n"
                        "Analyze this frame and call exactly one tool."
                    )
                }
            ]
        }]
    )

    tool_name = "all_clear"
    tool_args = {"note": "No tool called"}
    analysis  = ""

    for block in response.content:
        if block.type == "tool_use":
            tool_name = block.name
            tool_args = block.input
        elif hasattr(block, "text"):
            analysis  = block.text

    return tool_name, tool_args, analysis


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PATROL LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_patrol():
    log.info("=" * 60)
    log.info(f"  Safety1271 {DRONE_MODEL} — {COMPOUND_NAME}")
    log.info(f"  Stream: {RTMP_STREAM_URL}")
    log.info(f"  MSDK bridge: {MSDK_BRIDGE_URL}")
    log.info(f"  TEST_MODE: {TEST_MODE}")
    log.info("=" * 60)

    # Upload and start mission via MSDK
    kmz_path = "patrol_route.kmz"
    if not Path(kmz_path).exists():
        log.info("patrol_route.kmz not found — generating...")
        generate_kmz(kmz_path)

    log.info("Uploading mission to drone via MSDK bridge...")
    if not bridge.upload_and_start_mission(kmz_path):
        log.error("Mission upload failed — check MSDK bridge connection")
        if not TEST_MODE:
            sys.exit(1)

    log.info("✅ Mission uploaded and started. Connecting to video stream...")

    cap = cv2.VideoCapture(RTMP_STREAM_URL)
    if not cap.isOpened():
        log.error(f"Cannot open stream: {RTMP_STREAM_URL}")
        log.error("Is MediaMTX running? Is the Android bridge streaming RTMP?")
        sys.exit(1)

    log.info("✅ Stream connected. Safety1271 is watching.\n")
    short_term_memory: list = []
    waypoint_idx = 0

    try:
        while True:
            # Battery check every 50 frames
            if session["frames_analyzed"] % 50 == 0 and session["frames_analyzed"] > 0:
                status = bridge.get_status()
                batt   = status.get("battery_pct", 100)
                log.info(f"🔋 Battery: {batt}%")
                if batt <= 20:
                    log.warning("⚠️  Low battery — triggering RTH")
                    bridge.return_to_home()
                    break

            result = capture_frame(cap)
            if result is None:
                log.warning("Frame capture failed — retrying...")
                time.sleep(2)
                continue

            frame, b64, frame_path = result
            session["frames_analyzed"] += 1

            wp        = PATROL_WAYPOINTS[waypoint_idx % len(PATROL_WAYPOINTS)]
            wp_label  = wp[2]

            try:
                tool_name, tool_args, analysis = analyze_frame(b64, short_term_memory, wp_label)
            except Exception as e:
                log.error(f"Analysis error: {e}")
                time.sleep(FRAME_INTERVAL_S)
                continue

            result_str = execute_tool(tool_name, tool_args, frame_path, analysis)

            short_term_memory.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "tool": tool_name,
                "zone": tool_args.get("zone", wp_label),
                "note": (tool_args.get("note") or
                         tool_args.get("description") or
                         tool_args.get("reason", ""))[:80]
            })
            if len(short_term_memory) > 10:
                short_term_memory.pop(0)

            # Clean up clear frames
            if tool_name == "all_clear":
                try:
                    frame_path.unlink()
                except Exception:
                    pass

            # Advance waypoint every ~8 frames (approximate)
            if session["frames_analyzed"] % 8 == 0:
                waypoint_idx += 1

            # Use faster interval during hold, normal otherwise
            interval = HOLD_FRAME_INT_S if session.get("active_hold") else FRAME_INTERVAL_S
            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("\nShutting down...")
    finally:
        if session["drone_state"] in ("patrolling", "holding"):
            log.info("Triggering RTH on shutdown...")
            bridge.return_to_home()
        cap.release()
        _print_session_summary()


def _print_session_summary():
    elapsed = (datetime.now() - session["start_time"]).seconds // 60
    print(f"""
{'='*60}
  SESSION SUMMARY — {COMPOUND_NAME}
{'='*60}
  Duration           : {elapsed} minutes
  Frames analyzed    : {session['frames_analyzed']}
  Holds executed     : {session['holds_executed']}
  Incidents logged   : {session['incidents_logged']}
  Alerts sent        : {session['alerts_sent']}
  Lockdowns triggered: {session['lockdowns_triggered']}
  Test mode          : {TEST_MODE}
{'='*60}
""")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Safety1271 Mini 4 Pro Agent")
    parser.add_argument("--generate-kmz", action="store_true",
                        help="Generate patrol_route.kmz and exit")
    parser.add_argument("--preflight",    action="store_true",
                        help="Run Part 107 pre-flight check and exit")
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip pre-flight check")
    parser.add_argument("--kmz-output",   type=str, default="patrol_route.kmz")
    args = parser.parse_args()

    if args.generate_kmz:
        generate_kmz(args.kmz_output)
        return

    if args.preflight:
        try:
            from preflight_agent import run_preflight_check
            home = PATROL_WAYPOINTS[0]
            run_preflight_check(
                lat             = home[0],
                lon             = home[1],
                altitude_ft     = PATROL_ALT_M * 3.281,
                flight_datetime = datetime.now().isoformat(),
                timezone        = "America/New_York",
                pilot_name      = "Safety1271 Pilot",
                pilot_phone     = ALERT_PHONE,
                pilot_email     = ALERT_EMAIL,
                drone_sn        = os.getenv("DRONE_SN", "MINI4PROXXXXXXX")
            )
        except ImportError:
            log.warning("preflight_agent.py not found.")
        return

    if not args.no_preflight:
        try:
            from preflight_agent import run_preflight_check
            log.info("Running pre-flight check...")
            home    = PATROL_WAYPOINTS[0]
            verdict = run_preflight_check(
                lat             = home[0],
                lon             = home[1],
                altitude_ft     = PATROL_ALT_M * 3.281,
                flight_datetime = datetime.now().isoformat(),
                timezone        = "America/New_York",
                pilot_name      = "Safety1271 Pilot",
                pilot_phone     = ALERT_PHONE,
                pilot_email     = ALERT_EMAIL,
                drone_sn        = os.getenv("DRONE_SN", "MINI4PROXXXXXXX")
            )
            if verdict == "NO-GO" and not TEST_MODE:
                log.error("Pre-flight NO-GO — resolve before flying.")
                sys.exit(1)
        except ImportError:
            log.warning("preflight_agent.py not found — skipping.")

    # ATC listener in background
    try:
        from atc_listener_agent import LiveATCCapture, processing_pipeline, AlertDeduplicator
        import queue
        atc_stream = os.getenv("LIVEATC_URL", "")
        if atc_stream:
            atc_q = queue.Queue(maxsize=10)
            LiveATCCapture(atc_stream, atc_q).start()
            threading.Thread(
                target=processing_pipeline,
                args=(atc_q, AlertDeduplicator()),
                daemon=True
            ).start()
            log.info("ATC listener active in background.")
    except ImportError:
        pass

    if TEST_MODE:
        log.info("⚠️  TEST_MODE — MSDK commands simulated, use webcam for video")

    # ── Dynamic Mission Manager ────────────────────────────────────────────
    # Monitors NOTAMs, TFRs, and weather every 5 min.
    # Automatically re-routes or triggers RTH when conditions change.
    # ATC listener hooks into trigger_immediate_recheck() for real-time TFR alerts.
    dynamic_manager = None
    try:
        from dynamic_mission_manager import DynamicMissionManager
        home = PATROL_WAYPOINTS[0]
        dynamic_manager = DynamicMissionManager(
            original_waypoints = PATROL_WAYPOINTS,
            bridge             = bridge,
            patrol_alt_m       = PATROL_ALT_M,
            home_lat           = home[0],
            home_lon           = home[1],
        )
        dynamic_manager.start()
        log.info("DynamicMissionManager active — real-time NOTAM/weather re-routing enabled.")

        # Hook ATC listener to trigger immediate rechecks on TFR announcements
        # (ATC listener calls this when it hears "TFR" on the radio)
        session["dynamic_manager"] = dynamic_manager

    except ImportError:
        log.warning("dynamic_mission_manager.py not found — static route only.")
    except Exception as e:
        log.warning(f"DynamicMissionManager failed to start: {e} — continuing without.")

    run_patrol()


if __name__ == "__main__":
    main()
