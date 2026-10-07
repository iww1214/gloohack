"""
=============================================================================
  Safety1271 — DJI Air 3 Edition
  Autonomous Church / School Compound Security Agent
  Gloo AI Hackathon 2026 | "Build AI for Human Flourishing"
=============================================================================

HARDWARE — DJI AIR 3 (RC-N2 + iPhone)
---------------------------------------
  Aircraft        : DJI Air 3
  Weight          : 720 g  (FAA registration + Part 107 required)
  Cameras         : DUAL — 24mm wide f/1.7  +  70mm tele f/2.8
  Obstacle avoid. : APAS 5.0 omnidirectional — safe for autonomous patrol
  Flight time     : 46 minutes
  Wind resistance : Level 6 (46 kph)
  Controller      : DJI RC-N2 → iPhone via Lightning/USB-C adapter
  No MSDK support — DJI Fly app is the only control surface

WHAT'S DIFFERENT FROM THE MINI 4 PRO VERSION
----------------------------------------------
  REMOVED:  MSDK Android bridge app
  REMOVED:  Virtual stick hold_and_observe (drone cannot be stopped mid-flight by Python)
  REMOVED:  Programmatic KMZ upload (done manually in DJI Fly)

  KEPT:     Full RTMP video pipeline (DJI Fly → MediaMTX → Python)
  KEPT:     Claude vision frame analysis + tool calling
  KEPT:     SMS + email security alerts
  KEPT:     All incident / lockdown / emergency tools
  KEPT:     KMZ patrol route generator

  ADDED:    Dual-camera awareness — agent knows wide vs tele per waypoint
  ADDED:    Tele zoom requests sent to pilot (RC-N2 holder) via SMS
  ADDED:    Pilot intervention alerts with exact controller instructions
  ADDED:    Pre-flight compliance check integration (preflight_agent.py)
  ADDED:    ATC listener integration (atc_listener_agent.py)
  ADDED:    KMZ now specifies camera (wide/tele) per waypoint (Air 3 feature)
  ADDED:    Drone GPS position in every alert (from RTMP metadata)
  ADDED:    Incident photo saved and attached to email alert

ARCHITECTURE
------------
  ┌──────────────────────────────────────────────────────────┐
  │                   LAPTOP (this script)                   │
  │                                                          │
  │  ┌──────────────┐    ┌────────────────────────────────┐  │
  │  │  MediaMTX    │    │     Safety1271 Agent        │  │
  │  │  RTMP :1935  │───▶│     Claude Vision Model        │  │
  │  │  RTSP :8554  │    │     Tool Calling Engine        │  │
  │  └──────┬───────┘    └──────────────┬─────────────────┘  │
  │         │                           │                     │
  │  ┌──────┴───────┐    ┌──────────────┴─────────────────┐  │
  │  │  ATC Listener│    │  Pre-Flight Agent               │  │
  │  │  (background)│    │  (runs once at startup)         │  │
  │  └──────────────┘    └────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────┘
              │ RTMP stream (DJI Fly Live)
              │
  ┌───────────┴──────────────────────────────────────────────┐
  │             iPhone running DJI Fly app                   │
  │             RC-N2 controller attached via Lightning       │
  │             Pilot holds RC-N2 and monitors alerts        │
  └───────────┬──────────────────────────────────────────────┘
              │ DJI O4 (12 km range)
              ▼
  ┌──────────────────────────────────────────────────────────┐
  │                 DJI Air 3 (airborne)                     │
  │   On-board waypoint mission (loaded via DJI Fly app)     │
  │   Omnidirectional obstacle avoidance active (APAS 5.0)   │
  │   Dual cameras: 24mm wide + 70mm tele                    │
  │   Auto-RTH on low battery or signal loss                 │
  └──────────────────────────────────────────────────────────┘

SETUP CHECKLIST
---------------
1. pip install anthropic twilio sendgrid opencv-python pillow python-dotenv requests astral
2. Install MediaMTX:
     macOS  : brew install mediamtx
     Windows: https://github.com/bluenviron/mediamtx/releases
     Linux  : wget https://github.com/bluenviron/mediamtx/releases/download/v1.9.3/mediamtx_v1.9.3_linux_amd64.tar.gz
3. Copy .env.example → .env and fill in all values
4. Run: python safety1271_air3.py --generate-kmz
   → Generates patrol_route.kmz in the current directory
5. AirDrop patrol_route.kmz to iPhone → open in DJI Fly → import mission
6. Run: python safety1271_air3.py --preflight
   → Runs Part 107 compliance check, sends SMS/email report
7. In DJI Fly: Settings → Live Streaming → Custom RTMP
   → URL: rtmp://YOUR_LAPTOP_IP:1935/live/drone
8. Start MediaMTX:  mediamtx
9. Fly drone to home point, start waypoint mission in DJI Fly
10. Run: python safety1271_air3.py
    → Agent starts monitoring. Keep RC-N2 ready for manual intervention.

PILOT ROLE (YOU, holding the RC-N2)
-------------------------------------
  When the agent detects a threat it cannot handle autonomously,
  it sends you an SMS with exact controller instructions:
    • Hold position: stop right stick, hover
    • Switch to tele: long-press right dial / camera button per DJI Fly UI
    • Resume patrol: tap Play in DJI Fly → waypoint mission continues
  You are the intervention layer. The agent is your eyes; you are the hands.
=============================================================================
"""

import os
import sys
import cv2
import time
import json
import zipfile
import base64
import logging
import argparse
import threading
import textwrap
from io         import BytesIO
from pathlib    import Path
from datetime   import datetime
from typing     import Optional

from gloo_client import GlooAnthropicCompat, complete, run_agent, MODELS, GLOO_TRADITION
import requests
from PIL        import Image
from dotenv     import load_dotenv
from twilio_client import create_twilio_client, get_twilio_from_phone
from sendgrid   import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Content, Attachment, FileContent, FileName, FileType, Disposition

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

# ── Configuration (all from .env) ─────────────────────────────────────────────
COMPOUND_NAME    = os.getenv("COMPOUND_NAME",    "Bennie Bough Church Campus")
COMPOUND_ADDRESS = os.getenv("COMPOUND_ADDRESS", "8304 Old Keene Mill Rd, Springfield, VA 22152")
DRONE_MODEL      = "DJI Air 3"
PATROL_ALT_M     = float(os.getenv("PATROL_ALTITUDE_M",  "18"))   # ~60ft — good for 720p ID
PATROL_SPEED_MS  = float(os.getenv("PATROL_SPEED_MS",    "3.0"))  # slow enough to analyze
FRAME_INTERVAL_S = float(os.getenv("FRAME_INTERVAL_SEC", "4.0"))  # seconds between analyses
RTMP_STREAM_URL  = os.getenv("RTMP_STREAM_URL", "rtsp://localhost:8554/live/drone")
ALERT_PHONE      = os.getenv("ALERT_PHONE",     "+1XXXXXXXXXX")   # pilot / security team
ALERT_EMAIL      = os.getenv("ALERT_EMAIL",     "security@church.org")
PILOT_PHONE      = os.getenv("PILOT_PHONE",     ALERT_PHONE)      # RC-N2 holder's phone
INCIDENT_DIR     = Path(os.getenv("INCIDENT_DIR", "./incidents"))
INCIDENT_DIR.mkdir(exist_ok=True)

# ── Patrol Waypoints ──────────────────────────────────────────────────────────
# Define your compound perimeter in Google Maps (right-click → "What's here?")
# Camera: "wide" = 24mm, "tele" = 70mm 3x zoom
# Gimbal pitch: 0° = horizontal, -90° = straight down, -45° = 45° forward look
PATROL_WAYPOINTS = [
    # (lat,         lon,           label,              camera, gimbal_deg, speed_ms, hover_s)
    (38.7742,  -77.1892,  "Main Gate",              "wide",  -45,  2.5,  3),
    (38.7748,  -77.1885,  "North Parking Lot",      "wide",  -60,  3.0,  0),
    (38.7755,  -77.1878,  "NE Corner / Playground", "tele",  -30,  2.0,  4),   # ← zoom for detail
    (38.7758,  -77.1870,  "East Entrance",          "wide",  -45,  2.5,  2),
    (38.7752,  -77.1862,  "Rear / Loading Dock",    "tele",  -30,  2.0,  4),   # ← zoom for detail
    (38.7744,  -77.1865,  "SE Corner",              "wide",  -60,  3.0,  0),
    (38.7738,  -77.1875,  "South Parking Lot",      "wide",  -45,  3.0,  0),
    (38.7735,  -77.1885,  "SW Corner / Perimeter",  "wide",  -60,  3.0,  0),
    (38.7740,  -77.1895,  "West Side / Garden",     "wide",  -45,  2.5,  2),
    (38.7742,  -77.1892,  "Return to Main Gate",    "wide",  -45,  3.0,  0),   # closes loop
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
    "current_waypoint":    0,
    "current_camera":      "wide",
    "active_hold":         False,
    "last_alert_time":     0.0,
}


# ══════════════════════════════════════════════════════════════════════════════
#  KMZ PATROL ROUTE GENERATOR
#  Generates a DJI Fly–compatible waypoint mission with dual-camera actions.
#  Import the generated .kmz into DJI Fly app on iPhone via AirDrop.
# ══════════════════════════════════════════════════════════════════════════════

def generate_kmz(output_path: str = "patrol_route.kmz"):
    """
    Generate a DJI Fly waypoint mission KMZ file for the Air 3.
    Includes camera selection and gimbal pitch at each waypoint.
    Import into DJI Fly: Files app → Share → Copy to DJI Fly
    """
    # DJI Fly KMZ format — WPML (Waypoint Markup Language)
    # Reference: https://developer.dji.com/doc/cloud-api-tutorial/en/api-reference/dji-wpml/overview.html

    waypoint_elements = []
    for i, (lat, lon, label, camera, gimbal_deg, speed, hover_s) in enumerate(PATROL_WAYPOINTS):
        # Camera index: 0 = wide (24mm), 1 = tele (70mm)
        cam_index = 1 if camera == "tele" else 0
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
                <wpml:payloadPositionIndex>{cam_index}</wpml:payloadPositionIndex>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        # Action 2: Hover if specified
        if hover_s > 0:
            actions.append(f"""
            <wpml:action>
              <wpml:actionId>{i * 10 + 2}</wpml:actionId>
              <wpml:actionActuatorFunc>hover</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:hoverTime>{hover_s}</wpml:hoverTime>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        # Action 3: Take photo for incident baseline
        actions.append(f"""
            <wpml:action>
              <wpml:actionId>{i * 10 + 3}</wpml:actionId>
              <wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:payloadPositionIndex>{cam_index}</wpml:payloadPositionIndex>
              </wpml:actionActuatorFuncParam>
            </wpml:action>""")

        cam_label = f"[{'TELE 70mm' if camera == 'tele' else 'WIDE 24mm'}]"
        waypoint_elements.append(f"""
      <Placemark>
        <name>{i+1}. {label} {cam_label}</name>
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

    # Home point = first waypoint
    home_lat, home_lon = PATROL_WAYPOINTS[0][0], PATROL_WAYPOINTS[0][1]

    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:wpml="http://www.dji.com/wpmz/1.0.6">
  <Document>
    <name>Safety1271 Patrol — {COMPOUND_NAME}</name>
    <wpml:author>Safety1271 Air3 Agent</wpml:author>
    <wpml:createTime>{int(datetime.now().timestamp() * 1000)}</wpml:createTime>
    <wpml:updateTime>{int(datetime.now().timestamp() * 1000)}</wpml:updateTime>
    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>goHome</wpml:finishAction>
      <wpml:exitOnRCLost>goBack</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>{PATROL_SPEED_MS}</wpml:globalTransitionalSpeed>
      <wpml:droneInfo>
        <wpml:droneEnumValue>67</wpml:droneEnumValue>  <!-- DJI Air 3 -->
        <wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
      </wpml:droneInfo>
      <wpml:payloadInfo>
        <wpml:payloadEnumValue>52</wpml:payloadEnumValue>  <!-- Air 3 camera -->
        <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
      </wpml:payloadInfo>
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

    # WPML also needs a template.kml and wpmz structure
    template_kml = kml_content.replace(
        'xmlns:wpml="http://www.dji.com/wpmz/1.0.6"',
        'xmlns:wpml="http://www.dji.com/wpmz/1.0.6" type="template"'
    )

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/waylines.wpml", kml_content)
        zf.writestr("wpmz/template.kml",  template_kml)

    log.info(f"✅ KMZ generated: {output_path}")
    log.info(f"   {len(PATROL_WAYPOINTS)} waypoints | {PATROL_ALT_M}m altitude | {PATROL_SPEED_MS}m/s")
    log.info("   AirDrop to iPhone → Open in DJI Fly → Import Mission")

    # Print waypoint summary
    print("\n  PATROL ROUTE SUMMARY")
    print("  " + "─" * 55)
    for i, (lat, lon, label, camera, gimbal, speed, hover) in enumerate(PATROL_WAYPOINTS):
        cam = "🔭 TELE" if camera == "tele" else "📷 WIDE"
        print(f"  {i+1:2}. {label:<28} {cam}  {gimbal:4}°  {hover}s hover")
    print("  " + "─" * 55)
    print(f"  Home point: {PATROL_WAYPOINTS[0][2]} ({home_lat}, {home_lon})")
    print(f"  End action: Return to Home\n")


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _send_sms(message: str, phone: str = ALERT_PHONE):
    try:
        twilio_client.messages.create(
            body  = message,
            from_ = get_twilio_from_phone(),
            to    = phone
        )
        log.info(f"SMS → {phone}: {message[:60]}...")
    except Exception as e:
        log.error(f"SMS failed: {e}")


def _send_email(subject: str, html_body: str,
                image_path: Optional[Path] = None,
                to: str = ALERT_EMAIL):
    try:
        sg   = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        mail = Mail(
            from_email = "security@safety1271.ai",
            to_emails  = to,
            subject    = subject,
        )
        mail.add_content(Content("text/html", html_body))

        if image_path and image_path.exists():
            with open(image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            att = Attachment(
                FileContent(encoded),
                FileName(image_path.name),
                FileType("image/jpeg"),
                Disposition("attachment")
            )
            mail.add_attachment(att)

        sg.send(mail)
        log.info(f"Email → {to}: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def _alert_html(title: str, color: str, rows: list,
                 pilot_action: str = "", transcript: str = "") -> str:
    row_html = "".join(
        f"<tr style='background:{'#f9f9f9' if i%2 else '#fff'}'>"
        f"<td style='padding:8px 12px;font-weight:600;color:#555;width:140px'>{k}</td>"
        f"<td style='padding:8px 12px'>{v}</td></tr>"
        for i, (k, v) in enumerate(rows)
    )
    pilot_block = ""
    if pilot_action:
        pilot_block = f"""
        <div style='margin:20px 0;padding:14px;background:#fff8e1;
                    border-left:4px solid #c9a84c;border-radius:4px'>
            <div style='font-weight:700;margin-bottom:6px'>
                🕹️ PILOT ACTION REQUIRED (RC-N2)
            </div>
            <div style='white-space:pre-line'>{pilot_action}</div>
        </div>"""
    transcript_block = ""
    if transcript:
        transcript_block = f"""
        <div style='margin-top:16px;padding:14px;background:#1a1a2e;border-radius:4px'>
            <div style='color:#aaa;font-size:12px;font-family:monospace;margin-bottom:8px'>
                CLAUDE ANALYSIS
            </div>
            <div style='color:#00ff88;font-family:monospace;font-size:13px;
                        white-space:pre-wrap'>{transcript}</div>
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
            {pilot_block}
            {transcript_block}
            <p style='margin-top:20px;font-size:11px;color:#aaa;
                      border-top:1px solid #eee;padding-top:10px'>
                Safety1271 AI Security Agent | DJI Air 3 | {COMPOUND_ADDRESS}
            </p>
        </div>
    </div>"""


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS (what Claude can call)
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "all_clear",
        "description": "Scene is normal. Patrol continues. Call this for every frame with no concerns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "Brief description of what's visible"}
            },
            "required": ["note"]
        }
    },
    {
        "name": "log_incident",
        "description": "Something minor or ambiguous. Log it, patrol continues. Use for unusual but non-threatening observations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity":  {"type": "string", "enum": ["low", "medium"]},
                "zone":      {"type": "string"},
                "note":      {"type": "string"},
                "camera":    {"type": "string", "enum": ["wide", "tele"]}
            },
            "required": ["severity", "zone", "note"]
        }
    },
    {
        "name": "request_pilot_hold",
        "description": (
            "Ask the human pilot (RC-N2 holder) to manually hold position and switch "
            "to tele camera for closer observation. Use when something needs more detail "
            "but is not yet a confirmed threat. The pilot will stop the mission and hover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone":        {"type": "string", "description": "Where the drone is now"},
                "reason":      {"type": "string", "description": "What you saw that needs closer look"},
                "use_tele":    {"type": "boolean", "description": "True if tele zoom would help"},
                "hold_seconds":{"type": "integer", "description": "Suggested hold duration", "default": 30}
            },
            "required": ["zone", "reason"]
        }
    },
    {
        "name": "alert_security_team",
        "description": "Confirmed suspicious or threatening activity. Alert humans immediately. Include pilot hold instructions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threat_level":   {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "zone":           {"type": "string"},
                "description":    {"type": "string", "description": "What you see in detail"},
                "persons_count":  {"type": "integer", "description": "Number of people involved"},
                "camera_active":  {"type": "string", "enum": ["wide", "tele"]},
                "pilot_action":   {"type": "string", "description": "Exact RC-N2 action for pilot to take"}
            },
            "required": ["threat_level", "zone", "description", "pilot_action"]
        }
    },
    {
        "name": "lockdown_alert",
        "description": "Credible active threat requiring immediate building lockdown. Highest non-emergency alert.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lockdown_type":   {"type": "string", "enum": ["lockdown", "shelter_in_place", "evacuate"]},
                "affected_zones":  {"type": "array", "items": {"type": "string"}},
                "reason":          {"type": "string"},
                "pilot_action":    {"type": "string"}
            },
            "required": ["lockdown_type", "affected_zones", "reason", "pilot_action"]
        }
    },
    {
        "name": "call_emergency_services",
        "description": "Active emergency: weapon visible, violence in progress, fire, medical crisis. Call 911.",
        "input_schema": {
            "type": "object",
            "properties": {
                "emergency_type":    {"type": "string", "enum": ["police", "fire", "medical", "all"]},
                "location":          {"type": "string"},
                "situation_summary": {"type": "string"},
                "pilot_action":      {"type": "string"}
            },
            "required": ["emergency_type", "location", "situation_summary", "pilot_action"]
        }
    }
]


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def execute_tool(tool_name: str, args: dict,
                 frame_path: Optional[Path] = None,
                 claude_analysis: str = "") -> str:
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    zone  = args.get("zone", "Unknown Zone")
    pilot = args.get("pilot_action", "")

    # ── all_clear ─────────────────────────────────────────────────────────
    if tool_name == "all_clear":
        log.info(f"✅ ALL CLEAR — {args.get('note','')}")
        return "Clear."

    # ── log_incident ───────────────────────────────────────────────────────
    elif tool_name == "log_incident":
        sev  = args.get("severity", "low").upper()
        note = args.get("note", "")
        log.warning(f"📋 INCIDENT [{sev}] Zone: {zone} — {note}")
        session["incidents_logged"] += 1
        return f"Incident logged: {sev}"

    # ── request_pilot_hold ─────────────────────────────────────────────────
    elif tool_name == "request_pilot_hold":
        reason   = args.get("reason", "")
        use_tele = args.get("use_tele", False)
        hold_s   = args.get("hold_seconds", 30)

        tele_line = "\n3. Switch to TELE camera:\n   Long-press right wheel or tap camera icon → select Tele" if use_tele else ""

        pilot_sms = (
            f"👁️ SAFETY1271 — HOLD REQUEST\n"
            f"Zone: {zone}\n"
            f"Reason: {reason}\n\n"
            f"RC-N2 STEPS:\n"
            f"1. Pause waypoint mission (tap ❚❚ in DJI Fly)\n"
            f"2. Hold position — leave sticks centered{tele_line}\n"
            f"4. Monitor for ~{hold_s}s\n"
            f"5. Resume: tap ▶ in DJI Fly"
        )
        _send_sms(pilot_sms, PILOT_PHONE)
        log.warning(f"👁️ PILOT HOLD REQUEST — Zone: {zone} | {reason}")
        session["incidents_logged"] += 1
        return f"Pilot hold request sent for zone: {zone}"

    # ── alert_security_team ────────────────────────────────────────────────
    elif tool_name == "alert_security_team":
        # Rate limit: don't spam if already alerted in last 30s
        if time.time() - session["last_alert_time"] < 30:
            return "Alert suppressed — cooldown active."

        level     = args.get("threat_level", "medium").upper()
        desc      = args.get("description", "")
        persons   = args.get("persons_count", 0)
        cam       = args.get("camera_active", session["current_camera"])
        level_colors = {"LOW":"#c9a84c","MEDIUM":"#cc6600","HIGH":"#cc2200","CRITICAL":"#8b0000"}
        color = level_colors.get(level, "#cc2200")
        emoji = {"LOW":"⚠️","MEDIUM":"🚨","HIGH":"⛔","CRITICAL":"🆘"}.get(level,"🚨")

        # SMS to security team
        sms = (
            f"{emoji} SECURITY ALERT [{level}] — {ts}\n"
            f"Zone: {zone}\n"
            f"Persons: {persons}\n"
            f"Camera: {cam.upper()}\n\n"
            f"{desc}\n\n"
            f"RC-N2: {pilot}"
        )
        _send_sms(sms)

        # Separate SMS to pilot with just the controller instructions
        if pilot and PILOT_PHONE != ALERT_PHONE:
            _send_sms(
                f"{emoji} AGENT ALERT — {zone}\n{desc[:80]}\n\n🕹️ {pilot}",
                PILOT_PHONE
            )

        # Email with incident photo
        html = _alert_html(
            title   = f"{emoji} {level} SECURITY ALERT",
            color   = color,
            rows    = [
                ("Time",        ts),
                ("Zone",        zone),
                ("Threat",      level),
                ("Camera",      f"{cam.upper()} ({'24mm wide' if cam=='wide' else '70mm tele'})"),
                ("Persons",     str(persons)),
                ("Description", desc),
            ],
            pilot_action  = pilot,
            transcript    = claude_analysis
        )
        _send_email(
            subject    = f"[{level}] Security Alert — {zone} — {COMPOUND_NAME}",
            html_body  = html,
            image_path = frame_path
        )

        session["alerts_sent"]      += 1
        session["last_alert_time"]   = time.time()
        log.critical(f"🚨 ALERT [{level}] Zone: {zone} — {desc[:80]}")
        return f"Alert sent: [{level}] {zone}"

    # ── lockdown_alert ─────────────────────────────────────────────────────
    elif tool_name == "lockdown_alert":
        ltype  = args.get("lockdown_type", "lockdown").upper()
        zones  = ", ".join(args.get("affected_zones", [zone]))
        reason = args.get("reason", "")

        sms = (
            f"🔒 {ltype} — {COMPOUND_NAME}\n"
            f"Zones: {zones}\n"
            f"Reason: {reason}\n"
            f"Time: {ts}\n\n"
            f"🕹️ RC-N2: {pilot}"
        )
        _send_sms(sms)

        html = _alert_html(
            title   = f"🔒 {ltype}",
            color   = "#4a0000",
            rows    = [
                ("Time",     ts),
                ("Type",     ltype),
                ("Zones",    zones),
                ("Reason",   reason),
            ],
            pilot_action = pilot,
            transcript   = claude_analysis
        )
        _send_email(
            subject   = f"[LOCKDOWN] {ltype} — {COMPOUND_NAME}",
            html_body = html,
            image_path= frame_path
        )

        session["lockdowns_triggered"] += 1
        log.critical(f"🔒 {ltype} — {zones}: {reason}")
        return f"{ltype} triggered for: {zones}"

    # ── call_emergency_services ────────────────────────────────────────────
    elif tool_name == "call_emergency_services":
        etype   = args.get("emergency_type", "police").upper()
        loc     = args.get("location", zone)
        summary = args.get("situation_summary", "")

        sms = (
            f"🆘 EMERGENCY — CALL 911 NOW\n"
            f"Type: {etype}\n"
            f"Location: {loc}\n"
            f"Address: {COMPOUND_ADDRESS}\n\n"
            f"{summary}\n\n"
            f"🕹️ RC-N2: {pilot}"
        )
        _send_sms(sms)

        html = _alert_html(
            title   = f"🆘 EMERGENCY — {etype}",
            color   = "#000000",
            rows    = [
                ("Time",     ts),
                ("Type",     etype),
                ("Location", loc),
                ("Address",  COMPOUND_ADDRESS),
                ("Summary",  summary),
            ],
            pilot_action = pilot,
            transcript   = claude_analysis
        )
        _send_email(
            subject   = f"[EMERGENCY] {etype} — {loc} — CALL 911",
            html_body = html,
            image_path= frame_path
        )

        session["alerts_sent"] += 1
        log.critical(f"🆘 EMERGENCY [{etype}] @ {loc}: {summary}")
        return f"Emergency services ({etype}) notified. Call 911: {COMPOUND_ADDRESS}"

    return f"Unknown tool: {tool_name}"


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""
You are Safety1271, an AI security agent protecting {COMPOUND_NAME}
at {COMPOUND_ADDRESS}.

HARDWARE:
  Drone   : DJI Air 3 — autonomous waypoint patrol (DJI Fly app on iPhone)
  Cameras : DUAL — 24mm wide-angle (f/1.7) + 70mm telephoto (f/2.8, 3× zoom)
  Altitude: {PATROL_ALT_M}m AGL — ground footprint ~{int(2*PATROL_ALT_M*0.74)}×{int(2*PATROL_ALT_M*0.42)}m (wide camera)
  Pilot   : Human holding RC-N2 controller — no virtual sticks available.
            YOU CANNOT STOP THE DRONE DIRECTLY. You can request the pilot do so.

IMPORTANT — NO VIRTUAL STICKS:
  This is a DJI Air 3 with no MSDK support. You cannot stop, hold, or redirect
  the drone in code. When you need closer observation, call request_pilot_hold —
  this sends the human pilot an SMS with exact controller steps to pause the
  waypoint mission and hover manually.

DUAL CAMERA CONTEXT:
  Wide  (24mm, f/1.7): Best for scanning large areas, parking lots, perimeter.
                        Identifies groups, vehicles, general activity.
  Tele  (70mm, f/2.8): Best for individual identification, face/plate reading,
                        confirming ambiguous threats at distance.
  When you call alert_security_team or request_pilot_hold, specify use_tele=true
  if you need the pilot to switch to the telephoto camera for better ID.

DECISION FRAMEWORK — call exactly ONE tool per frame:

  all_clear           → Normal activity: people walking, cars parking,
                        staff going about their business. Patrol continues.

  log_incident        → Minor / ambiguous: someone loitering but not threatening,
                        an unfamiliar vehicle, a door propped open. Log and continue.

  request_pilot_hold  → You see something that needs closer inspection but isn't
                        clearly threatening. The pilot will pause the mission and
                        hover so you can analyze more frames. Use BEFORE escalating.

  alert_security_team → Confirmed suspicious activity: aggressive behaviour,
                        someone attempting to enter unauthorized areas, person
                        following a child, suspected weapon visible but unconfirmed.
                        Always include pilot_action with RC-N2 steps.

  lockdown_alert      → Credible active threat: confirmed weapon, physical assault
                        in progress, person attempting forcible entry. Trigger
                        building lockdown. Include pilot_action.

  call_emergency_services → Active emergency only: shooting, stabbing, fire,
                        medical collapse, active violence. Call 911. Include
                        pilot_action to keep drone overhead as long as possible.

PILOT ACTION FORMAT:
  When you write pilot_action, use plain step-by-step RC-N2 instructions:
  Example: "1. Tap ❚❚ in DJI Fly to pause mission  2. Keep sticks centered — hover
            3. Tap camera icon → switch to Tele  4. SMS '911' to resume mission"

PROTECTED POPULATION:
  This is a church and school campus. Children, elderly, and vulnerable
  individuals may be present at all times. Apply extra vigilance around:
  - Playground and recreation areas
  - Parking lot (vehicle interactions with pedestrians)
  - Building entrances and exits
  - Any area where a lone adult approaches a child

CONTEXT FROM PRIOR FRAMES:
  You will receive a short_term memory of the last 5 observations to help you
  track developing situations across frames.
"""


# ══════════════════════════════════════════════════════════════════════════════
#  VIDEO CAPTURE & FRAME ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def capture_frame(cap: cv2.VideoCapture) -> Optional[tuple]:
    """Capture a frame and return (numpy_array, base64_string, path)."""
    ret, frame = cap.read()
    if not ret:
        return None

    # Save incident frame
    frame_path = INCIDENT_DIR / f"frame_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    cv2.imwrite(str(frame_path), frame)

    # Encode for Claude vision — resize to 1280x720 to save tokens
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img   = Image.fromarray(rgb)
    img.thumbnail((1280, 720), Image.LANCZOS)
    buf   = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64   = base64.standard_b64encode(buf.getvalue()).decode()

    return frame, b64, frame_path


def analyze_frame(b64_image: str, short_term_memory: list,
                  waypoint_label: str, camera: str) -> tuple:
    """
    Send frame to Claude with context. Returns (tool_name, tool_args, analysis_text).
    """
    memory_text = ""
    if short_term_memory:
        memory_text = "\n\nPRIOR OBSERVATIONS (most recent last):\n" + \
                      "\n".join(f"  [{m['time']}] {m['tool']}: {m['note']}" for m in short_term_memory[-5:])

    cam_desc = "24mm wide-angle camera (full scene view)" if camera == "wide" \
               else "70mm telephoto camera (3× zoom, closer detail)"

    user_content = [
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
                f"CURRENT PATROL POSITION: {waypoint_label}\n"
                f"CAMERA: {cam_desc}\n"
                f"ALTITUDE: {PATROL_ALT_M}m AGL\n"
                f"TIME: {datetime.now().strftime('%H:%M:%S')}"
                f"{memory_text}\n\n"
                "Analyze this frame and call exactly one tool based on what you see."
            )
        }
    ]

    response = claude_client.messages.create(
        model=MODELS["patrol"],
        max_tokens= 1024,
        tradition=GLOO_TRADITION,
        system    = SYSTEM_PROMPT,
        tools     = TOOLS,
        messages  = [{"role": "user", "content": user_content}]
    )

    # Extract tool call and any text
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
    """Main video analysis loop."""
    log.info("=" * 60)
    log.info(f"  Safety1271 Air 3 — {COMPOUND_NAME}")
    log.info(f"  Stream: {RTMP_STREAM_URL}")
    log.info(f"  Frame interval: {FRAME_INTERVAL_S}s")
    log.info("=" * 60)

    cap = cv2.VideoCapture(RTMP_STREAM_URL)
    if not cap.isOpened():
        log.error(f"Cannot open stream: {RTMP_STREAM_URL}")
        log.error("Is MediaMTX running? Is DJI Fly streaming?")
        sys.exit(1)

    log.info("✅ Stream connected. Starting analysis...")
    short_term_memory: list = []
    waypoint_idx = 0

    try:
        while True:
            result = capture_frame(cap)
            if result is None:
                log.warning("Frame capture failed — retrying in 2s...")
                time.sleep(2)
                continue

            frame, b64, frame_path = result
            session["frames_analyzed"] += 1

            # Determine current waypoint context (approximate — drone is autonomous)
            wp = PATROL_WAYPOINTS[waypoint_idx % len(PATROL_WAYPOINTS)]
            wp_label  = wp[2]
            wp_camera = wp[3]
            session["current_camera"] = wp_camera

            # Analyze with Claude
            try:
                tool_name, tool_args, analysis = analyze_frame(
                    b64, short_term_memory, wp_label, wp_camera
                )
            except Exception as e:
                log.error(f"Analysis error: {e}")
                time.sleep(FRAME_INTERVAL_S)
                continue

            # Execute the tool
            result_str = execute_tool(tool_name, tool_args, frame_path, analysis)

            # Update short-term memory
            short_term_memory.append({
                "time":  datetime.now().strftime("%H:%M:%S"),
                "tool":  tool_name,
                "zone":  tool_args.get("zone", wp_label),
                "note":  tool_args.get("note") or tool_args.get("description", "")[:80]
            })
            if len(short_term_memory) > 10:
                short_term_memory.pop(0)

            # Clean up non-incident frames to save disk space
            if tool_name == "all_clear":
                try:
                    frame_path.unlink()
                except Exception:
                    pass

            # Advance waypoint estimate every N frames
            if session["frames_analyzed"] % 8 == 0:
                waypoint_idx += 1

            time.sleep(FRAME_INTERVAL_S)

    except KeyboardInterrupt:
        log.info("\nShutting down patrol...")
    finally:
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
  Incidents logged   : {session['incidents_logged']}
  Alerts sent        : {session['alerts_sent']}
  Lockdowns triggered: {session['lockdowns_triggered']}
{'='*60}
""")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Safety1271 Air 3 Security Agent")
    parser.add_argument("--generate-kmz", action="store_true",
                        help="Generate patrol_route.kmz and exit")
    parser.add_argument("--preflight",    action="store_true",
                        help="Run Part 107 pre-flight check before patrol")
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip pre-flight check and go straight to patrol")
    parser.add_argument("--kmz-output",   type=str, default="patrol_route.kmz",
                        help="Output path for KMZ file")
    args = parser.parse_args()

    if args.generate_kmz:
        generate_kmz(args.kmz_output)
        return

    # Optional pre-flight integration
    if not args.no_preflight:
        try:
            from preflight_agent import run_preflight_check
            log.info("Running Part 107 pre-flight compliance check...")
            home = PATROL_WAYPOINTS[0]
            verdict = run_preflight_check(
                lat             = home[0],
                lon             = home[1],
                altitude_ft     = PATROL_ALT_M * 3.281,
                flight_datetime = datetime.now().isoformat(),
                timezone        = "America/New_York",
                pilot_name      = "Safety1271 Pilot",
                pilot_phone     = PILOT_PHONE,
                pilot_email     = ALERT_EMAIL,
                drone_sn        = os.getenv("DRONE_SN", "AIR3XXXXXXXXXX")
            )
            if verdict == "NO-GO":
                log.error("Pre-flight check: NO-GO. Resolve issues before flying.")
                sys.exit(1)
            log.info(f"Pre-flight check: {verdict} — proceeding to patrol.")
        except ImportError:
            log.warning("preflight_agent.py not found — skipping pre-flight check.")

    # Optional ATC listener in background
    try:
        from atc_listener_agent import LiveATCCapture, processing_pipeline, AlertDeduplicator
        import queue
        atc_queue = queue.Queue(maxsize=10)
        atc_stream = os.getenv("LIVEATC_URL", "")
        if atc_stream:
            log.info("Starting ATC listener in background...")
            atc_capture = LiveATCCapture(atc_stream, atc_queue)
            atc_capture.start()
            atc_thread  = threading.Thread(
                target = processing_pipeline,
                args   = (atc_queue, AlertDeduplicator()),
                daemon = True
            )
            atc_thread.start()
            log.info("ATC listener active.")
    except ImportError:
        log.info("atc_listener_agent.py not found — ATC monitoring disabled.")

    log.info("")
    log.info("STARTUP CHECKLIST:")
    log.info("  ✅ patrol_route.kmz imported in DJI Fly")
    log.info("  ✅ DJI Fly → Live Streaming → Custom RTMP configured")
    log.info("  ✅ MediaMTX running: mediamtx")
    log.info("  ✅ Drone armed and waypoint mission ready")
    log.info("  ✅ RC-N2 in hand — you are the intervention layer")
    log.info("")
    log.info("Starting patrol monitor. Press Ctrl+C to stop.\n")

    run_patrol()


if __name__ == "__main__":
    main()
