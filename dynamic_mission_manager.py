"""
dynamic_mission_manager.py
==========================
Real-time waypoint replanning based on live NOTAMs, TFRs, and weather.
Runs as a background thread alongside safety1271_mini4pro.py.

HOW IT WORKS
------------
Every N minutes (configurable), the manager:
  1. Fetches active NOTAMs + TFRs from FAA API
  2. Fetches current weather from NOAA
  3. Checks each patrol waypoint against any TFR exclusion zones
  4. Checks weather against Part 107 minimums and drone limits
  5. If the route needs changing:
       a. Pauses the current waypoint mission (MSDK bridge)
       b. Generates a modified KMZ (drops unsafe waypoints, adjusts altitude)
       c. Re-uploads and resumes the mission
       d. Sends pilot an SMS explaining what changed and why

ATC LISTENER INTEGRATION
------------------------
The ATC listener (atc_listener_agent.py) fires alerts when it hears
TFR announcements on the radio. Those alerts call:
    dynamic_manager.trigger_immediate_recheck("TFR announced on ATC")
This causes an out-of-schedule recheck within seconds rather than waiting
for the next polling cycle.

TRIGGERS AND RESPONSES
-----------------------
  TFR overlaps a waypoint      → remove affected waypoints from route
                                  re-upload with remaining waypoints
                                  SMS pilot: "Route modified — TFR active at [zone]"

  Wind 25–38 kph (approaching  → lower patrol altitude by 3m to find calmer air
  Mini 4 Pro's 38 kph limit)     re-upload with lower altitude
                                  SMS pilot: "Altitude reduced — wind advisory"

  Wind > 38 kph               → trigger RTH immediately (exceeds drone limit)
                                  SMS pilot: "RTH triggered — wind exceeds drone limit"

  Visibility < 3 SM           → trigger RTH (Part 107 minimum)
                                  SMS pilot: "RTH triggered — visibility below 3SM"

  Rain / precipitation        → trigger RTH (Mini 4 Pro is not waterproof)
                                  SMS pilot: "RTH triggered — precipitation detected"

  Presidential TFR (FDC)      → trigger RTH + hard stop (no re-route possible)
                                  SMS pilot: "RTH + STOP — Presidential TFR active"

MSDK CONSTRAINT
---------------
MSDK V5 does not support editing individual waypoints mid-flight.
The only way to change the route is:
    pause_mission() → generate new KMZ → upload_and_start_mission(new_kmz)
The drone hovers safely during the ~5-10 second re-upload window.
"""

import os
import math
import time
import json
import logging
import threading
from datetime  import datetime
from pathlib   import Path
from typing    import List, Optional, Tuple

import requests
from twilio_client import create_twilio_client, get_twilio_from_phone
from dotenv import load_dotenv
try:
    from cold_front_detector import ColdFrontDetector
    _HAS_CFD = True
except ImportError:
    _HAS_CFD = False

load_dotenv()

log = logging.getLogger("DynamicMission")

# ── Configuration ──────────────────────────────────────────────────────────
CHECK_INTERVAL_S    = int(os.getenv("DYNAMIC_CHECK_INTERVAL_SEC", "300"))  # 5 min
TFR_BUFFER_NM       = float(os.getenv("TFR_BUFFER_NM", "0.5"))             # extra margin
WIND_WARN_MPH       = 22.0   # start lowering altitude
WIND_RTH_MPH        = 24.0   # trigger RTH (Part 107 limit is 25 mph)
WIND_DRONE_LIMIT_MPH= 23.6   # Mini 4 Pro limit (38 kph = 23.6 mph)
VIS_MIN_SM          = 3.0    # Part 107 minimum visibility
ALTITUDE_REDUCTION_M= 3.0    # lower by this much when wind warns

ALERT_PHONE = os.getenv("ALERT_PHONE", "+1XXXXXXXXXX")
FAA_API_KEY = os.getenv("FAA_API_KEY", "")

twilio_client = create_twilio_client()


# ══════════════════════════════════════════════════════════════════════════════
#  GEOFENCING UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two GPS points."""
    R    = 3440.065   # Earth radius in nm
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a    = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def waypoint_in_tfr(wp_lat: float, wp_lon: float,
                    tfr_lat: float, tfr_lon: float,
                    tfr_radius_nm: float,
                    buffer_nm: float = TFR_BUFFER_NM) -> bool:
    """True if a waypoint falls inside a TFR + safety buffer."""
    dist = haversine_nm(wp_lat, wp_lon, tfr_lat, tfr_lon)
    return dist <= (tfr_radius_nm + buffer_nm)


def parse_tfr_geometry(notam_text: str) -> Optional[Tuple[float, float, float]]:
    """
    Try to extract (center_lat, center_lon, radius_nm) from a TFR NOTAM text.
    FAA NOTAM format examples:
      "A CIRCLE RADIUS OF 1 NM CENTERED AT 38.7N/077.1W"
      "WITHIN 3 NM OF 38-46-30N/077-03-00W"
    Returns None if geometry cannot be parsed (treat as full-area block).
    """
    import re
    text = notam_text.upper()

    # Pattern: DD-MM-SSN/DDD-MM-SSW or DD.DDN/DDD.DDW
    coord_patterns = [
        r'(\d{2})-(\d{2})-(\d{2})([NS])[/\s](\d{3})-(\d{2})-(\d{2})([EW])',
        r'(\d{2,3})\.(\d+)([NS])[/\s](\d{3})\.(\d+)([EW])',
    ]
    lat, lon = None, None
    for pat in coord_patterns:
        m = re.search(pat, text)
        if m:
            g = m.groups()
            if len(g) == 8:  # DMS format
                lat = int(g[0]) + int(g[1])/60 + int(g[2])/3600
                if g[3] == 'S': lat = -lat
                lon = int(g[4]) + int(g[5])/60 + int(g[6])/3600
                if g[7] == 'W': lon = -lon
            elif len(g) == 6:  # Decimal format
                lat = float(g[0]+'.'+g[1])
                if g[2] == 'S': lat = -lat
                lon = float(g[3]+'.'+g[4])
                if g[5] == 'W': lon = -lon
            break

    # Pattern: radius in NM
    radius_match = re.search(r'(\d+\.?\d*)\s*NM', text)
    radius_nm    = float(radius_match.group(1)) if radius_match else 3.0  # default 3nm

    if lat and lon:
        return (lat, lon, radius_nm)
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_active_tfrs(center_lat: float, center_lon: float,
                      radius_nm: float = 10) -> List[dict]:
    """
    Fetch active TFRs from FAA NOTAM API within search radius.
    Returns list of TFR dicts with parsed geometry where available.
    """
    tfrs = []
    try:
        resp = requests.get(
            "https://external-api.faa.gov/notamapi/v1/notams",
            params={
                "locationLatitude":  center_lat,
                "locationLongitude": center_lon,
                "locationRadius":    radius_nm,
                "pageSize":          50,
                "sortBy":            "issueDate",
                "sortOrder":         "Desc",
            },
            headers={"client_id": FAA_API_KEY},
            timeout=15
        )
        if not resp.ok:
            log.warning(f"NOTAM API error: {resp.status_code}")
            return []

        for item in resp.json().get("items", []):
            props    = item.get("properties", {})
            core     = props.get("coreNOTAMData", {})
            notam    = core.get("notam", {})
            text     = notam.get("text", "")
            is_tfr   = ("TFR" in notam.get("classification", "") or
                        "TEMPORARY FLIGHT RESTRICTION" in text.upper())
            is_pres  = "FDC" in notam.get("classification", "") and is_tfr

            if is_tfr:
                geometry = parse_tfr_geometry(text)
                tfrs.append({
                    "id":          notam.get("id", ""),
                    "text":        text,
                    "is_tfr":      True,
                    "is_presidential": is_pres,
                    "effective":   notam.get("effectiveStart", ""),
                    "expires":     notam.get("effectiveEnd", ""),
                    "geometry":    geometry,   # (lat, lon, radius_nm) or None
                })
    except Exception as e:
        log.error(f"TFR fetch error: {e}")

    return tfrs


def fetch_weather(lat: float, lon: float) -> dict:
    """Fetch current weather from NOAA."""
    try:
        pt = requests.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers={"User-Agent": "Safety1271/1.0"},
            timeout=10
        )
        if not pt.ok:
            return {}

        grid     = pt.json()["properties"]
        stations = requests.get(
            grid.get("observationStations", ""),
            headers={"User-Agent": "Safety1271/1.0"},
            timeout=10
        ).json()
        stn_url  = stations["features"][0]["id"] + "/observations/latest"
        obs      = requests.get(
            stn_url,
            headers={"User-Agent": "Safety1271/1.0"},
            timeout=10
        ).json()["properties"]

        wind_ms  = obs.get("windSpeed", {}).get("value") or 0
        vis_m    = obs.get("visibility", {}).get("value") or 9999
        precip   = obs.get("presentWeather", [])

        return {
            "wind_mph":      round(wind_ms * 2.237, 1),
            "visibility_sm": round(vis_m / 1609.34, 1),
            "precipitation": len(precip) > 0,
            "precip_types":  [p.get("weather", "") for p in precip],
            "raw":           obs
        }
    except Exception as e:
        log.error(f"Weather fetch error: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  DYNAMIC MISSION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class DynamicMissionManager:
    """
    Background thread that continuously monitors airspace and weather,
    modifying the patrol route mid-flight when conditions change.

    Usage:
        from dynamic_mission_manager import DynamicMissionManager
        from safety1271_mini4pro  import PATROL_WAYPOINTS, bridge, generate_kmz

        manager = DynamicMissionManager(
            original_waypoints = PATROL_WAYPOINTS,
            bridge             = bridge,
            patrol_alt_m       = PATROL_ALT_M,
            home_lat           = PATROL_WAYPOINTS[0][0],
            home_lon           = PATROL_WAYPOINTS[0][1],
        )
        manager.start()
        # In ATC listener alert callback:
        manager.trigger_immediate_recheck("TFR announced on ATC")
    """

    def __init__(self, original_waypoints: list, bridge,
                 patrol_alt_m: float,
                 home_lat: float, home_lon: float):
        self.original_waypoints = original_waypoints
        self.current_waypoints  = list(original_waypoints)
        self.bridge             = bridge
        self.base_alt_m         = patrol_alt_m
        self.current_alt_m      = patrol_alt_m
        self.home_lat           = home_lat
        self.home_lon           = home_lon

        self._running           = False
        self._thread            = None
        self._immediate_event   = threading.Event()
        self._last_check_time   = 0.0
        self._route_history     = []    # list of modification events

    # ── Public API ────────────────────────────────────────────────────────

    def start(self):
        """Start the background monitoring thread + cold front detector."""
        self._running = True
        self._thread  = threading.Thread(
            target = self._monitor_loop,
            name   = "DynamicMissionManager",
            daemon = True
        )
        self._thread.start()
        log.info(f"DynamicMissionManager started — checking every {CHECK_INTERVAL_S}s")

        # Start cold front detector alongside main manager
        if _HAS_CFD:
            self._cfd = ColdFrontDetector(
                airports    = ["KDCA", "KDAA", "KJYO"],
                on_alert    = self._on_cold_front_alert,
                alert_phone = os.getenv("ALERT_PHONE",""),
                alert_email = os.getenv("ALERT_EMAIL",""),
            )
            self._cfd.start()
            log.info("Cold front detector started alongside DynamicMissionManager")
        else:
            self._cfd = None
            log.warning("cold_front_detector.py not found — cold front detection disabled")

    def stop(self):
        self._running = False
        self._immediate_event.set()
        if hasattr(self, '_cfd') and self._cfd:
            self._cfd.stop()

    def trigger_immediate_recheck(self, reason: str = ""):
        """
        Called by ATC listener when a TFR is announced over the radio.
        Causes an out-of-schedule safety check within seconds.
        """
        log.warning(f"Immediate recheck triggered: {reason}")
        self._immediate_event.set()

    # ── Cold front callback ───────────────────────────────────────────────

    def _on_cold_front_alert(self, level: str, message: str):
        """
        Called by ColdFrontDetector when a frontal signal is detected.
        WATCH: log only.
        WARNING: trigger immediate safety recheck.
        URGENT: command RTH immediately.
        """
        log.warning(f"Cold front callback: [{level}] {message}")
        if level == "URGENT":
            log.critical("COLD FRONT URGENT — commanding immediate RTH")
            try:
                self.bridge.rth()
            except Exception as e:
                log.error(f"RTH command failed: {e}")
            self.trigger_immediate_recheck(f"COLD FRONT URGENT: {message}")
        elif level == "WARNING":
            self.trigger_immediate_recheck(f"COLD FRONT WARNING: {message}")
        # WATCH level: cold front detector handles SMS, no route change yet

    # ── Main loop ─────────────────────────────────────────────────────────

    def _monitor_loop(self):
        while self._running:
            # Wait for either the scheduled interval or an immediate trigger
            triggered = self._immediate_event.wait(timeout=CHECK_INTERVAL_S)
            self._immediate_event.clear()

            if not self._running:
                break

            try:
                self._run_safety_check()
            except Exception as e:
                log.error(f"Safety check error: {e}")

    def _run_safety_check(self):
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(f"[{ts}] Running dynamic safety check...")

        # ── 1. Fetch live data ─────────────────────────────────────────
        tfrs    = fetch_active_tfrs(self.home_lat, self.home_lon, radius_nm=15)
        weather = fetch_weather(self.home_lat, self.home_lon)

        # ── 2. Evaluate weather ────────────────────────────────────────
        wind_mph = weather.get("wind_mph", 0)
        vis_sm   = weather.get("visibility_sm", 10)
        precip   = weather.get("precipitation", False)

        if precip:
            log.critical("Precipitation detected — triggering RTH")
            self._rth_and_notify(
                reason   = "Precipitation detected",
                detail   = f"Types: {weather.get('precip_types', [])}. Mini 4 Pro is not waterproof.",
                hard_stop= False
            )
            return

        if wind_mph >= WIND_RTH_MPH:
            log.critical(f"Wind {wind_mph} mph >= {WIND_RTH_MPH} mph — triggering RTH")
            self._rth_and_notify(
                reason = f"Wind speed {wind_mph} mph exceeds Part 107 limit ({WIND_RTH_MPH} mph)",
                detail = "Return to home. Drone cannot safely operate.",
                hard_stop=False
            )
            return

        if vis_sm < VIS_MIN_SM:
            log.critical(f"Visibility {vis_sm} SM < {VIS_MIN_SM} SM — triggering RTH")
            self._rth_and_notify(
                reason = f"Visibility {vis_sm} SM below Part 107 minimum ({VIS_MIN_SM} SM)",
                hard_stop=False
            )
            return

        # Wind advisory — lower altitude
        new_alt = self.base_alt_m
        if wind_mph >= WIND_WARN_MPH:
            new_alt = max(10, self.base_alt_m - ALTITUDE_REDUCTION_M)
            if new_alt != self.current_alt_m:
                log.warning(f"Wind advisory {wind_mph} mph — lowering altitude {self.current_alt_m}m → {new_alt}m")

        # ── 3. Evaluate TFRs against each waypoint ─────────────────────
        blocked_zones    = []
        pres_tfr_active  = False

        for tfr in tfrs:
            if tfr.get("is_presidential"):
                pres_tfr_active = True
                log.critical(f"Presidential TFR active: {tfr['id']}")
                self._rth_and_notify(
                    reason   = f"Presidential TFR active: {tfr['id']}",
                    detail   = "Presidential security TFR — no drone operations permitted. Hard stop.",
                    hard_stop= True
                )
                return

            geometry = tfr.get("geometry")
            if not geometry:
                # Can't parse geometry — treat conservatively as full-area block
                log.warning(f"TFR {tfr['id']} geometry unparseable — assuming full area, RTH")
                self._rth_and_notify(
                    reason = f"TFR {tfr['id']} active — geometry unknown, cannot reroute safely",
                    hard_stop=False
                )
                return

            tfr_lat, tfr_lon, tfr_radius_nm = geometry
            for wp in self.current_waypoints:
                wp_lat, wp_lon, label = wp[0], wp[1], wp[2]
                if waypoint_in_tfr(wp_lat, wp_lon, tfr_lat, tfr_lon, tfr_radius_nm):
                    blocked_zones.append((label, tfr["id"], tfr_radius_nm))

        # ── 4. Determine if route change is needed ─────────────────────
        route_changed  = False
        change_reasons = []

        if blocked_zones:
            blocked_labels = [z[0] for z in blocked_zones]
            change_reasons.append(f"TFR blocks: {', '.join(blocked_labels)}")
            route_changed = True

        if abs(new_alt - self.current_alt_m) > 0.5:
            change_reasons.append(f"Altitude: {self.current_alt_m}m → {new_alt}m (wind {wind_mph} mph)")
            route_changed = True

        if not route_changed:
            log.info(f"Route clear — {len(self.current_waypoints)} waypoints, "
                     f"{self.current_alt_m}m alt, wind {wind_mph} mph, vis {vis_sm} SM")
            return

        # ── 5. Build modified waypoint list ────────────────────────────
        blocked_label_set = {z[0] for z in blocked_zones}
        safe_waypoints    = [
            wp for wp in self.original_waypoints
            if wp[2] not in blocked_label_set
        ]

        if len(safe_waypoints) < 2:
            log.critical("Too few waypoints remain after TFR exclusion — RTH")
            self._rth_and_notify(
                reason   = "TFR covers too much of patrol area — fewer than 2 safe waypoints remain",
                hard_stop= False
            )
            return

        # ── 6. Apply modifications and re-upload ───────────────────────
        log.warning(f"Route change: {', '.join(change_reasons)}")
        self._apply_route_change(
            new_waypoints = safe_waypoints,
            new_alt_m     = new_alt,
            reasons       = change_reasons,
            wind_mph      = wind_mph,
            vis_sm        = vis_sm,
        )

    def _apply_route_change(self, new_waypoints: list, new_alt_m: float,
                             reasons: list, wind_mph: float, vis_sm: float):
        """Pause mission, generate new KMZ, re-upload, resume, notify pilot."""
        ts = datetime.now().strftime("%H:%M:%S")

        # Pause current mission
        log.info("Pausing mission for route modification...")
        self.bridge.pause_mission()
        time.sleep(2)  # give drone time to stabilise in hover

        # Generate modified KMZ
        # Rebuild waypoints with new altitude
        modified = []
        for wp in new_waypoints:
            lat, lon, label = wp[0], wp[1], wp[2]
            rest = wp[3:]   # gimbal, speed, hover, etc.
            modified.append((lat, lon, label) + rest)

        kmz_path = f"patrol_modified_{datetime.now().strftime('%H%M%S')}.kmz"
        self._write_kmz(modified, new_alt_m, kmz_path)

        # Upload and start
        log.info(f"Uploading modified mission ({len(modified)} waypoints, {new_alt_m}m)...")
        ok = self.bridge.upload_and_start_mission(kmz_path)

        if ok:
            self.current_waypoints = new_waypoints
            self.current_alt_m     = new_alt_m

            removed = set(wp[2] for wp in self.original_waypoints) - \
                      set(wp[2] for wp in new_waypoints)

            self._route_history.append({
                "time":     ts,
                "reasons":  reasons,
                "removed":  list(removed),
                "alt_m":    new_alt_m,
                "wp_count": len(new_waypoints),
            })

            sms = (
                f"🔄 ROUTE MODIFIED — {ts}\n"
                f"Reason: {'; '.join(reasons)}\n"
                f"Removed zones: {', '.join(removed) if removed else 'none'}\n"
                f"New altitude: {new_alt_m}m\n"
                f"Active waypoints: {len(new_waypoints)}\n"
                f"Wind: {wind_mph} mph | Vis: {vis_sm} SM\n"
                f"Patrol resuming automatically."
            )
            self._send_sms(sms)
            log.info(f"Route change applied. Pilot notified.")
        else:
            log.error("Route re-upload failed — RTH for safety")
            self._rth_and_notify(
                reason   = "Route re-upload failed after modification attempt",
                hard_stop= False
            )

        # Clean up temp KMZ
        try:
            Path(kmz_path).unlink()
        except Exception:
            pass

    def _rth_and_notify(self, reason: str, detail: str = "",
                        hard_stop: bool = False):
        """Trigger RTH and SMS pilot with reason."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.bridge.return_to_home()

        stop_flag = "HARD STOP — do not re-launch until condition clears." if hard_stop \
                    else "Drone returning to home point."

        sms = (
            f"{'🔴 HARD STOP' if hard_stop else '🏠 RTH TRIGGERED'} — {ts}\n"
            f"Reason: {reason}\n"
            f"{detail}\n"
            f"{stop_flag}"
        )
        self._send_sms(sms)
        log.critical(f"RTH: {reason}")

    def _send_sms(self, message: str):
        try:
            twilio_client.messages.create(
                body  = message,
                from_ = get_twilio_from_phone(),
                to    = ALERT_PHONE
            )
        except Exception as e:
            log.error(f"SMS failed: {e}")

    def _write_kmz(self, waypoints: list, alt_m: float, output_path: str):
        """Generate a KMZ from modified waypoints. Reuses safety1271 KMZ format."""
        import zipfile
        elements = []
        for i, wp in enumerate(waypoints):
            lat, lon, label = wp[0], wp[1], wp[2]
            gimbal = wp[3] if len(wp) > 3 else -45
            speed  = wp[4] if len(wp) > 4 else 2.5
            hover  = wp[5] if len(wp) > 5 else 0

            actions = f"""
            <wpml:action>
              <wpml:actionId>{i*10+1}</wpml:actionId>
              <wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>
                <wpml:gimbalPitchRotateAngle>{gimbal}</wpml:gimbalPitchRotateAngle>
                <wpml:gimbalRollRotateAngle>0</wpml:gimbalRollRotateAngle>
                <wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>
                <wpml:gimbalRotateTimeEnable>0</wpml:gimbalRotateTimeEnable>
                <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
              </wpml:actionActuatorFuncParam>
            </wpml:action>"""
            if hover > 0:
                actions += f"""
            <wpml:action>
              <wpml:actionId>{i*10+2}</wpml:actionId>
              <wpml:actionActuatorFunc>hover</wpml:actionActuatorFunc>
              <wpml:actionActuatorFuncParam>
                <wpml:hoverTime>{hover}</wpml:hoverTime>
              </wpml:actionActuatorFuncParam>
            </wpml:action>"""

            elements.append(f"""
      <Placemark>
        <name>{i+1}. {label}</name>
        <Point><coordinates>{lon},{lat},{alt_m}</coordinates></Point>
        <wpml:index>{i}</wpml:index>
        <wpml:executeHeight>{alt_m}</wpml:executeHeight>
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
          {actions}
        </wpml:actionGroup>
      </Placemark>""")

        kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:wpml="http://www.dji.com/wpmz/1.0.6">
  <Document>
    <name>Safety1271 Modified — {datetime.now().strftime('%H:%M:%S')}</name>
    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>goHome</wpml:finishAction>
      <wpml:exitOnRCLost>goBack</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>2.5</wpml:globalTransitionalSpeed>
      <wpml:droneInfo>
        <wpml:droneEnumValue>60</wpml:droneEnumValue>
        <wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
      </wpml:droneInfo>
    </wpml:missionConfig>
    <Folder>
      <name>Modified Waypoints</name>
      <wpml:templateType>waypoint</wpml:templateType>
      <wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>
      <wpml:autoFlightSpeed>2.5</wpml:autoFlightSpeed>
      {"".join(elements)}
    </Folder>
  </Document>
</kml>"""
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("wpmz/waylines.wpml", kml)
            zf.writestr("wpmz/template.kml",  kml)

    def get_status(self) -> dict:
        """Return current route status for logging / display."""
        return {
            "active_waypoints": len(self.current_waypoints),
            "original_waypoints": len(self.original_waypoints),
            "current_altitude_m": self.current_alt_m,
            "modifications": len(self._route_history),
            "last_modification": self._route_history[-1] if self._route_history else None,
        }
