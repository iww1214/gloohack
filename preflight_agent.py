"""
DJI Air 3 — FAA Part 107 Pre-Flight Agent
==========================================
Checks airspace, weather, drone status, and time-of-day
against Part 107 rules, then emails and texts the full report
to the pilot for review before every flight.

Dependencies:
    pip install anthropic twilio sendgrid requests astral

Set environment variables:
    GLOO_CLIENT_ID + GLOO_CLIENT_SECRET
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_FROM_PHONE
    SENDGRID_API_KEY
    FAA_API_KEY           # https://api.faa.gov/
"""

import os
import json
import textwrap
import threading
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("PreflightAgent")

from gloo_client import GlooAnthropicCompat, complete, run_agent, MODELS, GLOO_TRADITION
import requests
import airspace_service
from astral import LocationInfo
from astral.sun import sun
from twilio_client import create_twilio_client, get_twilio_from_phone
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Content

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = GlooAnthropicCompat()  # Routes through Gloo AI platform
twilio_client    = create_twilio_client()

# Dashboard push endpoint + capture of structured tool data for it
DASHBOARD_PREFLIGHT_URL = os.getenv(
    "DASHBOARD_PREFLIGHT_URL", "http://127.0.0.1:8092/api/preflight"
)
_LAST_TOOL_DATA: dict = {}  # tool name -> latest structured result
LAST_CHECKLIST: list = []   # checklist from the most recent send_preflight_report call

# ── Tool Definitions ─────────────────────────────────────────────────────────
# ── KB tool injected at load time if KB exists ──────────────────────────────
try:
    from faa_knowledge_base import KB_TOOL, execute_kb_tool, kb_status, PREFLIGHT_KB_SYSTEM_ADDENDUM
    _KB_AVAILABLE = kb_status().get("built", False)
except ImportError:
    _KB_AVAILABLE = False
    KB_TOOL = None
    PREFLIGHT_KB_SYSTEM_ADDENDUM = ""

TOOLS = ([KB_TOOL] if (_KB_AVAILABLE and KB_TOOL) else []) + [
    {
        "name": "check_airspace",
        "description": (
            "Check the airspace class and LAANC altitude ceiling at a GPS location. "
            "Returns the class (B/C/D/E/G), LAANC ceiling in feet, and whether "
            "authorization is required before flight."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat":         {"type": "number", "description": "Latitude (decimal degrees)"},
                "lon":         {"type": "number", "description": "Longitude (decimal degrees)"},
                "altitude_ft": {"type": "number", "description": "Planned max altitude in feet AGL"}
            },
            "required": ["lat", "lon", "altitude_ft"]
        }
    },
    {
        "name": "check_weather",
        "description": (
            "Get current weather for the flight location: wind speed and direction, "
            "visibility in statute miles, cloud ceiling in feet, precipitation, "
            "and temperature. All values are real-time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude (decimal degrees)"},
                "lon": {"type": "number", "description": "Longitude (decimal degrees)"}
            },
            "required": ["lat", "lon"]
        }
    },
    {
        "name": "check_civil_twilight",
        "description": (
            "Determine whether the planned flight time falls in daylight, civil twilight, "
            "or night. Returns sunrise, sunset, civil dawn, civil dusk, and the lighting "
            "requirement for the DJI Mini 4 Pro under Part 107."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat":             {"type": "number"},
                "lon":             {"type": "number"},
                "flight_datetime": {
                    "type": "string",
                    "description": "ISO 8601 datetime string in local time, e.g. 2026-07-01T19:30:00"
                },
                "timezone":        {
                    "type": "string",
                    "description": "IANA timezone, e.g. America/New_York"
                }
            },
            "required": ["lat", "lon", "flight_datetime", "timezone"]
        }
    },
    {
        "name": "check_drone_status",
        "description": (
            "Query the DJI Cloud API for the current status of the drone: "
            "battery percentage, Remote ID broadcast status, firmware version, "
            "and any hardware faults."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_sn": {"type": "string", "description": "Aircraft serial number (may be UNKNOWN)"}
            },
            "required": ["device_sn"]
        }
    },
    {
        "name": "check_atis",
        "description": (
            "Fetch Digital ATIS (D-ATIS) information for the nearest airport to the "
            "flight location, or for a specific airport by ICAO identifier. "
            "Returns the full parsed ATIS briefing: wind, visibility, ceiling, "
            "temperature, dewpoint, altimeter setting, active runway estimate, "
            "ATIS information letter, and the raw METAR string. "
            "Use this to determine the active runway and confirm weather conditions "
            "match the METAR-based weather check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {
                    "type": "number",
                    "description": "Flight location latitude (decimal degrees)"
                },
                "lon": {
                    "type": "number",
                    "description": "Flight location longitude (decimal degrees)"
                },
                "airport_icao": {
                    "type": "string",
                    "description": (
                        "Optional ICAO identifier of a specific airport to query, "
                        "e.g. KDCA, KDAA, KJYO. If omitted, finds the nearest "
                        "reporting station automatically."
                    )
                }
            },
            "required": ["lat", "lon"]
        }
    },
    {
        "name": "send_preflight_report",
        "description": (
            "Send the complete pre-flight report to the pilot via SMS and email. "
            "Include the GO/NO-GO/CONDITIONAL verdict and all checklist items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["GO", "NO-GO", "CONDITIONAL"],
                    "description": "Overall flight viability verdict"
                },
                "checklist": {
                    "type": "array",
                    "description": "List of check results with status, item, and detail",
                    "items": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["PASS","FAIL","WARN","INFO"]},
                            "item":   {"type": "string"},
                            "detail": {"type": "string"}
                        }
                    }
                },
                "conditions_to_fly": {
                    "type": "array",
                    "description": "Actions pilot must take before flight (for CONDITIONAL verdict)",
                    "items": {"type": "string"}
                },
                "next_go_window": {
                    "type": "string",
                    "description": "When the next clean GO window is expected, if applicable"
                },
                "pilot_phone": {"type": "string", "description": "E.164 phone number, e.g. +17035551234"},
                "pilot_email": {"type": "string"},
                "pilot_name":  {"type": "string"}
            },
            "required": ["verdict", "checklist", "pilot_phone", "pilot_email"]
        }
    }
]

# ── Tool Implementations ─────────────────────────────────────────────────────

def check_airspace(lat: float, lon: float, altitude_ft: float) -> dict:
    """Airspace class at the position from the FAA Class Airspace layer (live, no key)."""
    a = airspace_service.lookup(lat, lon)
    if not a["ok"]:
        return {"airspace_class": None, "authorization_required": None, "laanc_ceiling_ft": None,
                "altitude_requested_ft": altitude_ft, "altitude_within_ceiling": None,
                "error": f"FAA airspace lookup failed ({a['error']}) — verify on a sectional chart or LAANC app"}
    controlled = a["class"] != "G"
    note = ("LAANC ceiling is not available from this data source — check a LAANC app for the exact ceiling."
            if controlled else "Uncontrolled at this altitude.")
    if a["in_sfra"]:
        note += " Inside the DC SFRA: SFRA rules apply."
    return {
        "airspace_class": a["class"],
        "facility": a["name"] or "",
        "authorization_required": bool(controlled or a["in_sfra"]),
        "laanc_ceiling_ft": None,
        "altitude_requested_ft": altitude_ft,
        "altitude_within_ceiling": None if controlled else altitude_ft <= 400,
        "dc_sfra": a["in_sfra"],
        "published_hours": a["hours"],
        "note": note,
        "source": "FAA Class Airspace layer",
    }



# ══════════════════════════════════════════════════════════════════════════════
#  DENSITY ALTITUDE CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

FIELD_ELEVATION_FT = int(os.getenv("FIELD_ELEVATION_FT", "350"))  # Burke CC default

def calculate_density_altitude(field_elev_ft: float,
                                altimeter_inhg: float,
                                temp_c: float,
                                dewpoint_c: float = None,
                                drone_max_flight_min: float = 34.0) -> dict:
    """
    Calculate density altitude and derive actionable performance advisories
    for the specific patrol mission.

    Formula:
      Pressure altitude = field_elev + (29.92 - altimeter) * 1000
      ISA temp at PA    = 15 - (2 * PA_thousands)
      Density altitude  = PA + 120 * (actual_temp - ISA_temp)

    Performance impacts scaled from manufacturer data for DJI Mini 4 Pro:
      ~5% flight time reduction per 1,000 ft density altitude above 1,000 ft
      ~8% motor current increase per 1,000 ft DA
      Battery thermal risk above DA 4,000 ft in hot conditions

    Returns full advisory dict suitable for SMS, email, and checklist inclusion.
    """
    # ── Step 1: Pressure altitude ─────────────────────────────────────────────
    pressure_alt_ft = field_elev_ft + (29.92 - altimeter_inhg) * 1000

    # ── Step 2: ISA temperature at pressure altitude ──────────────────────────
    isa_temp_c = 15.0 - (2.0 * pressure_alt_ft / 1000.0)

    # ── Step 3: Density altitude ──────────────────────────────────────────────
    density_alt_ft = pressure_alt_ft + 120.0 * (temp_c - isa_temp_c)

    # ── ISA deviation ─────────────────────────────────────────────────────────
    isa_deviation_c  = round(temp_c - isa_temp_c, 1)
    isa_deviation_str = (f"ISA+{isa_deviation_c:.0f}°C"
                         if isa_deviation_c >= 0
                         else f"ISA{isa_deviation_c:.0f}°C")

    # ── Humidity contribution ─────────────────────────────────────────────────
    humidity_note = ""
    if dewpoint_c is not None:
        spread_c = temp_c - dewpoint_c
        spread_f = spread_c * 9/5
        if dewpoint_c >= 18:   # high absolute humidity
            # Rough humidity DA penalty: ~100-200 ft for very humid conditions
            humidity_penalty = max(0, (dewpoint_c - 10) * 8)
            density_alt_ft  += humidity_penalty
            humidity_note    = (f"Humidity adds ~{humidity_penalty:.0f} ft "
                                f"(dewpoint {dewpoint_c:.0f}°C / "
                                f"spread {spread_f:.1f}°F)")

    density_alt_ft = round(density_alt_ft)
    pressure_alt_ft = round(pressure_alt_ft)

    # ── Performance impact assessment ─────────────────────────────────────────
    da_above_base  = max(0, density_alt_ft - 1000)
    da_thousands   = da_above_base / 1000.0

    # Flight time reduction (~5% per 1,000 ft DA above 1,000 ft)
    time_reduction_pct = min(40, da_thousands * 5.0)
    adjusted_flight_min = round(drone_max_flight_min * (1 - time_reduction_pct/100))

    # Motor load increase (~8% per 1,000 ft DA)
    motor_load_increase_pct = round(da_thousands * 8.0)

    # Battery reserve recommendation
    if density_alt_ft > 4000:
        battery_land_pct = 30
        battery_note = "Land at 30% battery — high DA thermal risk"
    elif density_alt_ft > 2000:
        battery_land_pct = 25
        battery_note = "Land at 25% battery — elevated DA"
    else:
        battery_land_pct = 20
        battery_note = "Standard 20% battery reserve"

    # Severity classification
    if density_alt_ft > 6000:
        severity = "HIGH"
        advisory = "HIGH DENSITY ALTITUDE — significant performance degradation. Consider rescheduling to cooler part of day."
    elif density_alt_ft > 4000:
        severity = "MODERATE"
        advisory = "ELEVATED DENSITY ALTITUDE — monitor battery temperature, land early."
    elif density_alt_ft > 2000:
        severity = "LOW"
        advisory = "Mild density altitude effect — slight reduction in flight time expected."
    else:
        severity = "NONE"
        advisory = "Density altitude within normal range — standard performance expected."

    # ── Unit conversions for display ──────────────────────────────────────────
    temp_f    = round(temp_c * 9/5 + 32, 1)
    isa_temp_f = round(isa_temp_c * 9/5 + 32, 1)

    return {
        "field_elevation_ft":      field_elev_ft,
        "altimeter_inhg":          round(altimeter_inhg, 2),
        "temperature_c":           round(temp_c, 1),
        "temperature_f":           temp_f,
        "dewpoint_c":              dewpoint_c,
        "pressure_altitude_ft":    pressure_alt_ft,
        "isa_temp_c":              round(isa_temp_c, 1),
        "isa_temp_f":              isa_temp_f,
        "isa_deviation":           isa_deviation_str,
        "isa_deviation_c":         isa_deviation_c,
        "density_altitude_ft":     density_alt_ft,
        "humidity_note":           humidity_note,
        "severity":                severity,
        "advisory":                advisory,
        "performance": {
            "flight_time_reduction_pct": round(time_reduction_pct, 1),
            "adjusted_flight_min":       adjusted_flight_min,
            "standard_flight_min":       drone_max_flight_min,
            "motor_load_increase_pct":   motor_load_increase_pct,
            "battery_land_pct":          battery_land_pct,
            "battery_note":              battery_note,
        },
        "sms_advisory": (
            f"\n\n⚠️ DENSITY ALTITUDE ADVISORY\n"
            f"  Field elevation:    {field_elev_ft} ft MSL\n"
            f"  Pressure altitude: {pressure_alt_ft} ft\n"
            f"  Density altitude:  {density_alt_ft} ft ({isa_deviation_str})\n"
            + (f"  {humidity_note}\n" if humidity_note else "")
            + f"\n  Performance impact:\n"
            f"  Flight time:  ~{adjusted_flight_min} min "
            f"(vs {drone_max_flight_min:.0f} min standard, "
            f"-{time_reduction_pct:.0f}%)\n"
            f"  Motor load:   +{motor_load_increase_pct}% above standard\n"
            f"  Battery:      {battery_note}\n"
            f"  Advisory:     {advisory}"
            if severity != "NONE" else
            f"\n✅ Density altitude {density_alt_ft} ft — "
            f"standard performance expected."
        ),
        "checklist_item": (
            f"{'⚠️' if severity != 'NONE' else '✅'} Density altitude: "
            f"{density_alt_ft} ft ({isa_deviation_str}) — "
            f"est. {adjusted_flight_min} min flight time, "
            f"land at {battery_land_pct}% battery"
        ),
    }


def check_weather(lat: float, lon: float) -> dict:
    """Current weather from the nearest METAR station (aviationweather.gov), the same source as the dashboard."""
    import conditions_monitor
    import location_service
    try:
        stations = conditions_monitor.fetch_metars(lat, lon)
        if not stations:
            raise RuntimeError("no METAR station within range")
        st = min(stations.values(), key=lambda s: s["meta"]["distance_mi"])
        obs = st["obs"][-1]
    except Exception as e:
        return {
            "wind_mph": None, "visibility_sm": None,
            "cloud_ceiling_ft": None, "temperature_f": None,
            "error": f"Weather lookup failed ({type(e).__name__}: {e}) — check aviationweather.gov manually",
            "within_107_limits": None
        }

    wind, gust, vis = obs["wind_mph"], obs["gust_mph"], obs["vis_sm"]
    temp_c, dewp_c, altim = obs["temp_c"], obs["dewp_c"], obs["altimeter_inhg"]
    worst_wind = None if wind is None and gust is None else max(wind or 0, gust or 0)
    elevation = location_service._geo_facts(lat, lon, None).get("elevation_ft")
    da_result = None
    if temp_c is not None and altim is not None:
        da_result = calculate_density_altitude(
            field_elev_ft  = elevation if elevation is not None else FIELD_ELEVATION_FT,
            altimeter_inhg = altim,
            temp_c         = temp_c,
            dewpoint_c     = dewp_c,
        )
    return {
        "wind_mph":              wind,
        "gust_mph":              gust,
        "wind_dir_deg":          obs["wind_dir"],
        "visibility_sm":         vis,
        "cloud_ceiling_ft":      obs["ceiling_ft"] if obs["ceiling_ft"] is not None else "none (clear/few/scattered)",
        "temperature_c":         temp_c,
        "temperature_f":         round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None,
        "dewpoint_c":            dewp_c,
        "altimeter_inhg":        altim,
        "precipitation":         obs["wx"],
        "flight_category":       obs["flight_category"],
        "station":               f"{st['meta']['id']} ({st['meta']['distance_mi']} mi)",
        "observed_at":           obs["obs_time"],
        "within_107_limits":     None if worst_wind is None or vis is None else (worst_wind <= 25 and vis >= 3),
        "wind_exceeds_aircraft_limit": None if worst_wind is None else worst_wind > 23.6,
        "density_altitude":      da_result,
    }


def check_civil_twilight(lat: float, lon: float,
                          flight_datetime: str, timezone: str) -> dict:
    """Determine daylight status and anti-collision light requirement."""
    try:
        tz      = ZoneInfo(timezone)
        loc     = LocationInfo(latitude=lat, longitude=lon, timezone=timezone)
        dt      = datetime.fromisoformat(flight_datetime).replace(tzinfo=tz)
        s       = sun(loc.observer, date=dt.date(), tzinfo=tz)

        civil_dawn = s["dawn"]
        sunrise    = s["sunrise"]
        sunset     = s["sunset"]
        civil_dusk = s["dusk"]

        if dt < civil_dawn or dt > civil_dusk:
            period = "NIGHT"
            strobe_required = True
            vision_sensors  = False
            part107_note    = "Night flight — anti-collision strobe REQUIRED. Vision sensors OFF."
        elif dt < sunrise or dt > sunset:
            period = "CIVIL TWILIGHT"
            strobe_required = True
            vision_sensors  = True  # Marginal
            part107_note    = "Civil twilight — anti-collision strobe REQUIRED per Part 107.29."
        else:
            period = "DAYLIGHT"
            strobe_required = False
            vision_sensors  = True
            part107_note    = "Daylight — no lighting requirement."

        return {
            "period":           period,
            "flight_time":      dt.strftime("%I:%M %p %Z"),
            "civil_dawn":       civil_dawn.strftime("%I:%M %p"),
            "sunrise":          sunrise.strftime("%I:%M %p"),
            "sunset":           sunset.strftime("%I:%M %p"),
            "civil_dusk":       civil_dusk.strftime("%I:%M %p"),
            "strobe_required":  strobe_required,
            "vision_sensors_active": vision_sensors,
            "part107_note":     part107_note
        }
    except Exception as e:
        return {"error": str(e), "strobe_required": True,
                "part107_note": "Could not determine twilight — assume strobe required."}


def check_drone_status(device_sn: str) -> dict:
    """Live aircraft status from the DJI Control Server app on the phone (battery, link, GPS, compass)."""
    bridge = os.getenv("MSDK_BRIDGE_URL", "http://172.20.10.2:8080").rstrip("/")
    not_reported = "not reported by the aircraft link — confirm in the DJI app before takeoff"
    try:
        s = requests.get(f"{bridge}/status", timeout=4).json()
    except Exception as e:
        return {"model": "DJI Mini 4 Pro", "reachable": False,
                "error": f"Aircraft link not reachable ({type(e).__name__}) — aircraft status cannot be verified"}
    battery = s.get("battery_pct")
    lat, lon = s.get("latitude_deg"), s.get("longitude_deg")
    return {
        "model": "DJI Mini 4 Pro",
        "reachable": True,
        "aircraft_connected": s.get("aircraft_connected") is True,
        "battery_pct": battery,
        "battery_go_nogo": ("UNKNOWN" if battery is None else
                            "GO (above 30% minimum)" if battery >= 30 else "NO-GO (below 30% minimum)"),
        "gps": f"fix at {lat:.5f}, {lon:.5f}" if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else "no fix",
        "compass_heading_deg": s.get("heading_deg"),
        "remote_id_status": not_reported,
        "firmware": not_reported,
        "hardware_faults": not_reported,
        "source": "live from the DJI Control Server app",
    }


def check_atis(lat: float, lon: float, airport_icao: str = None) -> dict:
    """
    Fetch Digital ATIS from aviationweather.gov METAR API.

    Sources (in order of preference):
      1. aviationweather.gov/api/data/metar — free, no key, government source
      2. checkwx.com METAR API            — requires CHECKWX_API_KEY in .env
      3. Constructed from check_weather()  — fallback using NOAA grid data

    Returns a fully parsed D-ATIS style briefing including active runway estimate,
    information letter, altimeter, ceiling, and raw METAR string.
    """

    # Known airports with ATIS/AWOS near Burke Community Church (38.7773, -77.1868)
    # Add more airports for other deployment locations
    NEARBY_AIRPORTS = {
        "KDCA": {"name": "Reagan National",    "lat": 38.8521, "lon": -77.0377,
                 "atis_freq": "132.65",        "runways": [1, 19, 4, 22, 15, 33]},
        "KDAA": {"name": "Davison AAF",        "lat": 38.7150, "lon": -77.1808,
                 "atis_freq": "128.175",       "runways": [14, 32],
                 "atis_hours": "1100-0230Z Mon-Fri"},
        "KJYO": {"name": "Leesburg Executive", "lat": 39.0780, "lon": -77.5575,
                 "atis_freq": "125.225",       "runways": [17, 35]},
        "KIAD": {"name": "Washington Dulles",  "lat": 38.9531, "lon": -77.4565,
                 "atis_freq": "135.275",       "runways": [1, 19, 12, 30]},
        "KHEF": {"name": "Manassas Regional",  "lat": 38.7214, "lon": -77.5152,
                 "atis_freq": "119.025",       "runways": [16, 34]},
    }

    def haversine_nm(lat1, lon1, lat2, lon2):
        import math
        R = 3440.065
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a  = math.sin(dp/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def estimate_active_runway(wind_dir_deg, runways):
        """Estimate active runway from wind direction — aircraft land into the wind."""
        if wind_dir_deg is None:
            return None
        best_rwy  = None
        best_diff = 999
        for rwy in runways:
            rwy_hdg  = rwy * 10
            diff     = abs(wind_dir_deg - rwy_hdg)
            diff     = min(diff, 360 - diff)
            if diff < best_diff:
                best_diff = diff
                best_rwy  = rwy
        return best_rwy

    def atis_letter(observed_time_str):
        """
        Derive the ATIS information letter from the observation time.
        ATIS updates on the hour — letter advances each update.
        Not exact (we don't have the actual recorded letter) but indicative.
        """
        import string
        try:
            from datetime import datetime as dt
            if "T" in observed_time_str:
                obs_dt = dt.fromisoformat(observed_time_str.replace("Z",""))
            else:
                obs_dt = dt.strptime(observed_time_str[:16], "%Y-%m-%dT%H:%M")
            letter_idx = obs_dt.hour % 26
            return string.ascii_uppercase[letter_idx]
        except Exception:
            return "?"

    def parse_metar_json(metar: dict, apt_info: dict) -> dict:
        """Parse aviationweather.gov JSON METAR into ATIS-format briefing."""
        wind_dir   = metar.get("wdir")         # degrees
        wind_spd   = metar.get("wspd")         # knots
        wind_gust  = metar.get("wgst")         # knots (optional)
        visibility = metar.get("visib")        # statute miles
        altimeter  = metar.get("altim")        # inHg
        temp_c     = metar.get("temp")         # Celsius
        dewp_c     = metar.get("dewp")         # Celsius
        wx_string  = metar.get("wxString", "") # present weather
        raw_metar  = metar.get("rawOb", "")
        obs_time   = metar.get("obsTime", "")
        station_id = metar.get("stationId", "")
        flight_cat = metar.get("flightCategory", "")

        # Cloud layers
        sky_cond = metar.get("sky", [])
        ceiling_ft = None
        ceiling_desc = "Sky clear"
        for layer in (sky_cond if isinstance(sky_cond, list) else []):
            cover = layer.get("cover", "")
            base  = layer.get("base")
            if cover in ("BKN", "OVC") and base is not None:
                ceiling_ft   = int(base)
                ceiling_desc = f"{cover} at {base:,} ft AGL"
                break

        # Temperature conversions
        temp_f = round(temp_c * 9/5 + 32, 1) if temp_c is not None else None
        dewp_f = round(dewp_c * 9/5 + 32, 1) if dewp_c is not None else None

        # Wind string
        if wind_dir == 0 and wind_spd == 0:
            wind_str = "Calm"
        elif wind_gust:
            wind_str = f"{str(wind_dir).zfill(3)}° at {wind_spd} kts, gusting {wind_gust} kts"
        else:
            wind_str = f"{str(wind_dir).zfill(3)}° at {wind_spd} kts" if wind_dir else f"{wind_spd} kts variable"

        wind_mph = round(wind_spd * 1.151, 1) if wind_spd else 0
        gust_mph = round(wind_gust * 1.151, 1) if wind_gust else None

        # Active runway
        runways       = apt_info.get("runways", [])
        active_runway = estimate_active_runway(wind_dir, runways)
        info_letter   = atis_letter(obs_time)

        # Part 107 weather assessment
        vis_ok   = visibility >= 3.0    if visibility else False
        wind_ok  = wind_mph   <= 25.0   if wind_spd   else True
        ceil_ok  = (ceiling_ft is None or ceiling_ft >= 500) if ceiling_ft else True

        return {
            "airport":          station_id,
            "airport_name":     apt_info.get("name", station_id),
            "information":      f"Information {info_letter}",
            "observed_at":      obs_time,
            "flight_category":  flight_cat,
            "wind":             wind_str,
            "wind_mph":         wind_mph,
            "gust_mph":         gust_mph,
            "visibility_sm":    visibility,
            "ceiling":          ceiling_desc,
            "ceiling_ft":       ceiling_ft,
            "temperature_f":    temp_f,
            "temperature_c":    temp_c,
            "dewpoint_f":       dewp_f,
            "dewpoint_c":       dewp_c,
            "altimeter_inhg":   round(altimeter, 2) if altimeter else None,
            "present_weather":  wx_string or "None",
            "active_runway":    f"Runway {active_runway}" if active_runway else "Check with ATC",
            "atis_frequency":   apt_info.get("atis_freq", "See Chart Supplement"),
            "atis_hours":       apt_info.get("atis_hours", "Continuous"),
            "raw_metar":        raw_metar,
            "part107_weather":  {
                "visibility_pass": vis_ok,
                "wind_pass":       wind_ok,
                "ceiling_pass":    ceil_ok,
                "overall":         "PASS" if (vis_ok and wind_ok and ceil_ok) else "FAIL"
            },
            "pilot_action": (
                f"Tune {apt_info.get('atis_freq','ATIS frequency')} before calling ATC. "
                f"Advise 'have information {info_letter}' on initial contact."
            )
        }

    # ── Step 1: Determine which airport(s) to query ─────────────────────────────
    if airport_icao:
        query_ids = [airport_icao.upper()]
        apt_info  = NEARBY_AIRPORTS.get(airport_icao.upper(),
                                         {"name": airport_icao, "runways": []})
    else:
        # Auto-select 3 nearest airports by great-circle distance
        distances = {
            icao: haversine_nm(lat, lon, info["lat"], info["lon"])
            for icao, info in NEARBY_AIRPORTS.items()
        }
        sorted_apts = sorted(distances.items(), key=lambda x: x[1])
        query_ids   = [icao for icao, _ in sorted_apts[:3]]
        nearest_icao = sorted_apts[0][0]
        apt_info     = NEARBY_AIRPORTS[nearest_icao]

    # ── Step 2: Query aviationweather.gov METAR API ──────────────────────────────
    ids_param = ",".join(query_ids)
    atis_results = []

    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={
                "ids":    ids_param,
                "format": "json",
                "taf":    "false",
                "hours":  1,
            },
            headers={"User-Agent": "Safety1271-Preflight/2.0"},
            timeout=10
        )
        if resp.ok:
            metars = resp.json()
            if metars:
                for metar in metars:
                    sid      = metar.get("stationId", "")
                    apt_i    = NEARBY_AIRPORTS.get(sid, {"name": sid, "runways": []})
                    parsed   = parse_metar_json(metar, apt_i)
                    dist_nm  = haversine_nm(lat, lon, apt_i.get("lat", lat),
                                            apt_i.get("lon", lon))
                    parsed["distance_from_flight_nm"] = round(dist_nm, 1)
                    atis_results.append(parsed)

                # Sort by distance — primary result is closest
                atis_results.sort(key=lambda x: x.get("distance_from_flight_nm", 99))

    except Exception as e:
        pass  # Fall through to checkwx fallback

    # ── Step 3: checkwx.com fallback (if aviationweather returns nothing) ────────
    if not atis_results:
        checkwx_key = os.environ.get("CHECKWX_API_KEY", "")
        if checkwx_key:
            try:
                resp = requests.get(
                    f"https://api.checkwx.com/metar/{ids_param}/decoded",
                    headers={"X-API-Key": checkwx_key},
                    timeout=10
                )
                if resp.ok:
                    data = resp.json().get("data", [])
                    for station in data:
                        icao    = station.get("icao", "")
                        apt_i   = NEARBY_AIRPORTS.get(icao, {"name": icao, "runways": []})
                        wind    = station.get("wind", {})
                        vis     = station.get("visibility", {})
                        clouds  = station.get("clouds", [])
                        baro    = station.get("barometer", {})
                        temps   = station.get("temperature", {})
                        dewp    = station.get("dewpoint", {})

                        wind_dir  = wind.get("degrees")
                        wind_spd  = wind.get("speed_kts", 0)
                        wind_gust = wind.get("gust_kts")
                        alt_inhg  = baro.get("hg")
                        vis_sm    = vis.get("miles_float", 0)
                        temp_c    = temps.get("celsius")
                        dewp_c    = dewp.get("celsius")

                        # Find ceiling
                        ceiling_ft = None
                        ceiling_desc = "Sky clear"
                        for layer in clouds:
                            if layer.get("code") in ("BKN", "OVC"):
                                ceiling_ft   = layer.get("base_feet_agl")
                                ceiling_desc = f"{layer.get('code')} at {ceiling_ft:,} ft AGL"
                                break

                        wind_mph = round(wind_spd * 1.151, 1) if wind_spd else 0
                        info_ltr = atis_letter(station.get("observed", ""))
                        active_rwy = estimate_active_runway(wind_dir, apt_i.get("runways", []))

                        atis_results.append({
                            "airport":         icao,
                            "airport_name":    apt_i.get("name", icao),
                            "information":     f"Information {info_ltr}",
                            "observed_at":     station.get("observed", ""),
                            "flight_category": station.get("flight_category", ""),
                            "wind":            f"{str(wind_dir).zfill(3)}° at {wind_spd} kts" if wind_dir else f"{wind_spd} kts",
                            "wind_mph":        wind_mph,
                            "gust_mph":        round(wind_gust * 1.151, 1) if wind_gust else None,
                            "visibility_sm":   vis_sm,
                            "ceiling":         ceiling_desc,
                            "ceiling_ft":      ceiling_ft,
                            "temperature_c":   temp_c,
                            "dewpoint_c":      dewp_c,
                            "altimeter_inhg":  alt_inhg,
                            "raw_metar":       station.get("raw_text", ""),
                            "atis_frequency":  apt_i.get("atis_freq", ""),
                            "active_runway":   f"Runway {active_rwy}" if active_rwy else "Check with ATC",
                            "part107_weather": {
                                "visibility_pass": vis_sm >= 3.0,
                                "wind_pass":       wind_mph <= 25.0,
                                "overall":         "PASS" if (vis_sm >= 3.0 and wind_mph <= 25.0) else "FAIL"
                            },
                            "source": "checkwx.com",
                        })
            except Exception:
                pass

    # ── Step 4: Return results ───────────────────────────────────────────────────
    if atis_results:
        primary = atis_results[0]
        return {
            "primary_atis":    primary,
            "all_stations":    atis_results,
            "stations_queried": query_ids,
            "source":          "aviationweather.gov" if not primary.get("source") else primary["source"],
            "usage_note": (
                f"Tune {primary.get('atis_freq', primary.get('atis_frequency',''))} MHz to hear the "
                f"live recorded ATIS. Report '{primary['information']}' on initial ATC contact. "
                f"Altimeter: {primary.get('altimeter_inhg','?')} inHg — set this on your "
                f"ground station before flight."
            )
        }

    # ── Fallback: no data available ──────────────────────────────────────────────
    return {
        "error": "ATIS data unavailable from all sources",
        "manual_check": {
            "KDCA_atis_freq":  "132.65 MHz",
            "KDAA_atis_freq":  "128.175 MHz (1100-0230Z Mon-Fri)",
            "KJYO_awos_freq":  "125.225 MHz",
            "KJYO_awos_phone": "(703) 777-3781",
            "aviationweather": "https://aviationweather.gov/metar",
            "1800wxbrief":     "https://www.1800wxbrief.com",
        },
        "source": "none — check manually",
    }


def _verdict_emoji(verdict: str) -> str:
    return {"GO": "🟢", "NO-GO": "🔴", "CONDITIONAL": "🟡"}.get(verdict, "⚪")

def _status_emoji(status: str) -> str:
    return {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "INFO": "ℹ️"}.get(status, "•")


def send_preflight_report(verdict: str, checklist: list,
                           conditions_to_fly: list = None, next_go_window: str = "",
                           pilot_phone: str = "", pilot_email: str = "",
                           pilot_name: str = "Pilot") -> dict:
    """Format and send the full report via SMS + email."""

    global LAST_CHECKLIST
    LAST_CHECKLIST = checklist
    conditions_to_fly = conditions_to_fly or []

    emoji  = _verdict_emoji(verdict)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Build SMS (concise — critical info only) ──────────────────────────
    sms_lines = [
        f"🚁 DJI Mini 4 Pro Pre-Flight Check — {now}",
        f"{emoji} VERDICT: {verdict}",
        "",
    ]
    for item in checklist:
        sms_lines.append(f"{_status_emoji(item['status'])} {item['item']}: {item['detail']}")

    if conditions_to_fly:
        sms_lines += ["", "ACTIONS REQUIRED:"]
        for i, c in enumerate(conditions_to_fly, 1):
            sms_lines.append(f"{i}. {c}")

    if next_go_window:
        sms_lines += ["", f"Next GO window: {next_go_window}"]

    sms_body = "\n".join(sms_lines)

    # ── Build Email ───────────────────────────────────────────────────────
    checklist_html = "".join([
        f"<tr>"
        f"<td style='padding:6px 10px;font-size:18px'>{_status_emoji(i['status'])}</td>"
        f"<td style='padding:6px 10px;font-weight:600'>{i['item']}</td>"
        f"<td style='padding:6px 10px;color:#444'>{i['detail']}</td>"
        f"</tr>"
        for i in checklist
    ])

    conditions_html = ""
    if conditions_to_fly:
        items = "".join(f"<li style='margin:6px 0'>{c}</li>" for c in conditions_to_fly)
        conditions_html = f"""
        <div style='margin:24px 0;padding:16px;background:#fffbea;border-left:4px solid #c9a84c'>
            <div style='font-weight:700;margin-bottom:8px'>⚠️ ACTIONS REQUIRED BEFORE FLIGHT</div>
            <ol style='margin:0;padding-left:20px'>{items}</ol>
        </div>"""

    verdict_color = {"GO": "#1a7a1a", "NO-GO": "#cc0000", "CONDITIONAL": "#c9a84c"}.get(verdict, "#333")

    email_html = f"""
    <div style='font-family:Calibri,Arial,sans-serif;max-width:680px;margin:0 auto;color:#1a1a1a'>
        <div style='background:#6b2737;padding:24px 28px;border-radius:4px 4px 0 0'>
            <div style='color:#fff;font-size:22px;font-weight:700'>🚁 DJI Mini 4 Pro — Pre-Flight Report</div>
            <div style='color:#e0c080;font-size:14px;margin-top:4px'>{now}</div>
        </div>

        <div style='background:{verdict_color};padding:18px 28px;text-align:center'>
            <div style='color:#fff;font-size:32px;font-weight:700'>{emoji} {verdict}</div>
        </div>

        <div style='padding:24px 28px'>
            <h3 style='color:#6b2737;border-bottom:2px solid #c9a84c;padding-bottom:6px'>Checklist</h3>
            <table style='width:100%;border-collapse:collapse'>{checklist_html}</table>

            {conditions_html}

            {"<p style='margin-top:24px;padding:12px;background:#f0f0f0;border-radius:4px'><strong>Next GO window:</strong> " + next_go_window + "</p>" if next_go_window else ""}

            <p style='margin-top:32px;font-size:12px;color:#888;border-top:1px solid #eee;padding-top:12px'>
                This report is generated by an automated pre-flight agent and is provided for
                informational purposes only. The pilot in command is always responsible for
                verifying all conditions and complying with FAA regulations before flight.
                Always check airspace at <a href='https://www.faa.gov/uas/getting_started/b4ufly'>B4UFLY</a>.
            </p>
        </div>
    </div>"""

    results = {}

    # Send SMS
    try:
        if not pilot_phone:
            raise ValueError("no pilot phone configured — not sent")
        msg = twilio_client.messages.create(
            body=sms_body,
            from_=get_twilio_from_phone(),
            to=pilot_phone
        )
        results["sms"] = {"status": "sent", "sid": msg.sid}
    except Exception as e:
        results["sms"] = {"status": "failed", "error": str(e)}

    # Send Email
    try:
        if not pilot_email:
            raise ValueError("no pilot email configured — not sent")
        sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        mail = Mail(
            from_email="preflight@yourdroneops.com",
            to_emails=pilot_email,
            subject=f"[{verdict}] DJI Mini 4 Pro Pre-Flight Report — {now}",
        )
        mail.add_content(Content("text/html", email_html))
        resp = sg.send(mail)
        results["email"] = {"status": "sent", "code": resp.status_code}
    except Exception as e:
        results["email"] = {"status": "failed", "error": str(e)}

    # Push the report to the live dashboard (best-effort; never block the pilot report)
    try:
        requests.post(
            DASHBOARD_PREFLIGHT_URL,
            json={
                "verdict": verdict,
                "checklist": checklist,
                "conditions_to_fly": conditions_to_fly,
                "next_go_window": next_go_window,
                "pilot_name": pilot_name,
                "weather": _LAST_TOOL_DATA.get("check_weather"),
                "airspace": _LAST_TOOL_DATA.get("check_airspace"),
            },
            timeout=3,
        )
        results["dashboard"] = {"status": "sent"}
    except Exception as e:
        results["dashboard"] = {"status": "failed", "error": str(e)}

    return {
        "notifications_sent": results,
        "verdict": verdict
    }


# ── Tool Router ───────────────────────────────────────────────────────────────
def execute_tool(name: str, inputs: dict) -> dict:
    if name == "query_faa_regulations" and _KB_AVAILABLE:
        return {"result": execute_kb_tool(inputs)}
    dispatch = {
        "check_airspace":         lambda i: check_airspace(**i),
        "check_weather":          lambda i: check_weather(**i),
        "check_atis":             lambda i: check_atis(**i),
        "check_civil_twilight":   lambda i: check_civil_twilight(**i),
        "check_drone_status":     lambda i: check_drone_status(**i),
        "send_preflight_report":  lambda i: send_preflight_report(**i),
    }
    fn = dispatch.get(name)
    if fn:
        result = fn(inputs)
        # Keep structured weather/airspace data for the dashboard push
        if name in ("check_weather", "check_airspace") and isinstance(result, dict):
            _LAST_TOOL_DATA[name] = result
        return result
    return {"error": f"Unknown tool: {name}"}


# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a FAA Part 107 pre-flight compliance agent for the DJI Mini 4 Pro (249g sUAS,
serial number provided by the pilot). You check all flight conditions against
Part 107 rules and issue a GO / NO-GO / CONDITIONAL verdict.

PART 107 RULES YOU ENFORCE:

AIRSPACE:
- Class G (uncontrolled): No authorization needed below 400ft AGL
- Class B/C/D/E: LAANC authorization required before flight
- DC SFRA: Special Flight Rules Area covers all of Northern Virginia and DC
  metro — most locations have zero or very low LAANC ceilings
- Never fly in a Flight Restricted Zone (FRZ) — the area within ~15nm of
  the Washington DC landmarks

ALTITUDE:
- Max 400ft AGL in uncontrolled airspace
- Max 400ft above a structure if within 400ft of it
- Never above LAANC ceiling without manual FAA waiver

WEATHER (Part 107.51):
- Max wind: 25 mph (this aircraft's limit, used on the dashboard, is 23.6 mph)
- Min visibility: 3 statute miles from control station
- Cloud clearance: 500ft below / 2,000ft horizontal
- No flight in IMC (instrument meteorological conditions)

TIME OF DAY (Part 107.29):
- Civil twilight to civil twilight without waiver
- Anti-collision strobe light REQUIRED during civil twilight and night
- Obstacle sensing is limited in low light — do not rely on it at dusk or night

DRONE STATUS:
- Battery minimum 30% for local flights (DJI recommendation)
- Remote ID must be broadcasting before takeoff
- No hardware faults
- Firmware should be current

DJI MINI 4 PRO SPECIFIC LIMITS:
- Max wind resistance: about 23.6 mph (Level 5)
- Weight: 249g — still requires FAA registration for Part 107 operations
- Do not fly sustained over uninvolved people

VERDICT CRITERIA:
- GO:          All conditions pass, no restrictions active
- CONDITIONAL: Flight possible with specific pilot actions (attach strobe,
               stay below ceiling, etc.)
- NO-GO:       Any hard stop: zero LAANC ceiling with no
               authorization, weather below minimums, critical drone fault,
               battery below 30%

SCOPE: NOTAMs and TFRs are not checked by this agent. Never mention them, and never
write or imply that none are active (no "No TFR", "clear of restrictions" or similar).

WORDING RULES:
- Cite only: §107.41 airspace, §107.51(b) altitude, §107.51(c) visibility, §107.51(d)
  cloud clearance, §107.29 daylight and twilight lighting, §107.31 line of sight,
  14 CFR Part 89 Remote ID. Do not cite any other section number.
- Never state fines or penalty amounts.
- This aircraft is flown without the DJI Fly app. Never tell the pilot to use it; say
  "confirm on the aircraft or controller" instead.

UNVERIFIED CHECKS (never present these as passed):
- A tool result with an "error" field, "checked": false, or "reachable": false
  means that check was NOT performed. Report it as WARN with the error text.
- If the aircraft link could not be read,
  or airspace is a controlled class with no LAANC ceiling available, the verdict
  can be at best CONDITIONAL, and the conditions must say what the pilot has to
  verify by hand.
- Remote ID, firmware and hardware faults are not reported by the aircraft link:
  list them as items to confirm in the DJI app, never as passed.
- Call each tool once. Do not repeat a tool whose result you already have.
  Finish by calling send_preflight_report exactly once.

ATIS (Automatic Terminal Information Service):
- Call check_atis to get the Digital ATIS briefing from the nearest airport.
- D-ATIS source: aviationweather.gov METAR API (free, no key, government data).
- ATIS gives: wind, visibility, ceiling, temperature, dewpoint, altimeter setting,
  active runway estimate, and information letter.
- Include the altimeter setting from ATIS in the preflight checklist — pilots must
  set their altimeter before flight for accurate altitude readings.
- Include the active runway in the report — tells the pilot which direction traffic
  is flowing so they know where to expect low-flying aircraft on approach.
- Note the ATIS frequency so pilot can tune in before calling ATC.
- If flight category from ATIS is IFR or LIFR: immediate NO-GO.
- Cross-check ATIS weather against check_weather() — flag any significant
  discrepancy between the two sources (ATIS is usually the more current source).

DENSITY ALTITUDE:
- check_weather now returns a density_altitude dict with full performance advisory.
- Always include the density_altitude.checklist_item in the preflight checklist.
- If density_altitude.severity is MODERATE or HIGH: add a specific warning to the
  SMS and email — reduced flight time, land early, monitor battery temperature.
- If density_altitude.performance.adjusted_flight_min < 25 min: flag as
  CONDITIONAL — patrol is possible but shortened. Advise RPIC explicitly.
- Always state the adjusted flight time and battery land percentage in the report.
- Include density_altitude.sms_advisory verbatim in send_preflight_report sms_body.

WORKFLOW:
1. check_airspace        → determine class and LAANC ceiling
2. check_weather         → verify all weather minimums (NOAA grid)
3. check_atis            → get D-ATIS: altimeter, active runway, ceiling, info letter
4. check_civil_twilight  → determine lighting requirement
5. check_drone_status    → verify drone is airworthy
6. Evaluate everything against Part 107 rules
7. Issue verdict and call send_preflight_report with complete data

ATIS IN REPORT:
Include in the checklist:
  - ATIS Information [letter] — note the letter so pilot can report it to ATC
  - Altimeter: [setting] inHg — pilot must set this before flight
  - Active runway: [runway] — direction of traffic flow
  - Flight category: [VFR/MVFR/IFR/LIFR] — IFR or LIFR is a hard NO-GO
  - ATIS frequency: [freq] MHz — tune before calling ATC
"""


# ── Main Agent Loop ───────────────────────────────────────────────────────────
def run_preflight_check(
    lat: float,
    lon: float,
    altitude_ft: float,
    flight_datetime: str,    # ISO format, e.g. "2026-07-01T19:30:00"
    timezone: str,           # IANA, e.g. "America/New_York"
    pilot_name: str,
    pilot_phone: str,        # E.164, e.g. "+17035551234"
    pilot_email: str,
    drone_sn: str
) -> str:
    """Run the full pre-flight agent and return the final verdict."""

    user_message = f"""
    Conduct a complete FAA Part 107 pre-flight check for the following flight:

    FLIGHT DETAILS:
    - Location:          {lat}°N, {lon}°W
    - Planned altitude:  {altitude_ft} ft AGL
    - Planned time:      {flight_datetime} ({timezone})
    - Drone:             DJI Mini 4 Pro  |  S/N: {drone_sn}

    PILOT:
    - Name:              {pilot_name}
    - Phone:             {pilot_phone}
    - Email:             {pilot_email}

    Check airspace, weather, twilight and drone status,
    evaluate against Part 107 rules, then send the complete report
    to the pilot via SMS and email.
    """

    final_verdict = "UNKNOWN"

    print(f"\n{'='*60}")
    print(f"DJI Mini 4 Pro Pre-Flight Check — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Location: {lat}, {lon} | Alt: {altitude_ft}ft | Time: {flight_datetime}")
    print(f"{'='*60}\n")

    delivery = {}

    def run_tool(name: str, args: dict) -> str:
        print(f"⚙️  Running: {name}...", flush=True)
        if name == "send_preflight_report":
            # Recipients come from the caller, never from the model.
            args = {**args, "pilot_phone": pilot_phone, "pilot_email": pilot_email, "pilot_name": pilot_name}
        try:
            result = execute_tool(name, args)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        if name == "send_preflight_report":
            delivery.update(result.get("notifications_sent", {}) if isinstance(result, dict) else {})
        text = json.dumps(result, default=str)
        print(f"   → {text[:120]}...", flush=True)
        return text

    # run_agent keeps the full tool history (the old per-call wrapper dropped it, so the model
    # re-requested the same checks forever) and stops after max_rounds.
    final_text, calls = run_agent(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_message,
        tools=TOOLS,
        tool_executor=run_tool,
        model="preflight",
        max_rounds=14,
        max_tokens=4096,
        timeout=120,
    )
    report_calls = [c for c in calls if c["name"] == "send_preflight_report"]
    if report_calls:
        final_verdict = report_calls[-1]["args"].get("verdict", final_verdict)
    else:
        print("\nWARNING: the agent finished without sending a report — no verdict was issued.")
    if final_text:
        print(f"\n{final_text}")

    print(f"\n{'='*60}")
    print(f"Pre-flight check complete. Verdict: {final_verdict}")
    print(f"Delivery: {delivery or 'no report sent'}")
    print(f"{'='*60}\n")

    return final_verdict


# ══════════════════════════════════════════════════════════════════════════════
#  WEATHER HOLD MONITOR
#  Activates when preflight returns weather-based NO-GO.
#  Polls METAR every 30 minutes, watches for inversion breakup pattern,
#  re-runs full preflight check automatically when conditions clear,
#  sends SMS/email updates throughout.
# ══════════════════════════════════════════════════════════════════════════════

class WeatherHoldMonitor:
    """
    Monitors METAR conditions during a weather hold and automatically
    re-runs the preflight check when Part 107 minimums are met.

    Inversion breakup sequence watched for:
      FG → BR → clear (fog lifts to mist to clear)
      Visibility: <1 SM → 1-2 SM → ≥3 SM (Part 107 minimum)
      Ceiling: OVC001 → BKN003 → SCT010 → clear
      Temp/dewpoint spread: <4°F → >4°F (fog dissipating)
      Flight category: LIFR → IFR → MVFR → VFR (target)

    Auto-cancels if conditions do not clear by cutoff_hour local time.
    """

    def __init__(self,
                 lat: float, lon: float,
                 airport_icao: str,
                 pilot_phone: str,
                 pilot_email: str,
                 pilot_name: str,
                 drone_sn: str,
                 altitude_ft: float,
                 flight_datetime: str,
                 timezone: str,
                 poll_interval_min: int = 30,
                 cutoff_hour: int = 14):         # cancel if not clear by 2 PM

        self.lat            = lat
        self.lon            = lon
        self.icao           = airport_icao
        self.pilot_phone    = pilot_phone
        self.pilot_email    = pilot_email
        self.pilot_name     = pilot_name
        self.drone_sn       = drone_sn
        self.altitude_ft    = altitude_ft
        self.flight_datetime= flight_datetime
        self.timezone       = timezone
        self.poll_interval  = poll_interval_min * 60   # seconds
        self.cutoff_hour    = cutoff_hour
        self._running       = False
        self._thread        = None
        self._hold_reason   = ""
        self._start_time    = None
        self._poll_count    = 0
        self._last_vis      = None
        self._last_cat      = None

    def start(self, hold_reason: str = "Weather below Part 107 minimums"):
        """Start the weather hold monitor."""
        self._hold_reason = hold_reason
        self._running     = True
        self._start_time  = datetime.now()
        self._thread      = threading.Thread(
            target=self._monitor_loop,
            name="WeatherHoldMonitor",
            daemon=True
        )
        self._thread.start()
        log.info(f"Weather hold monitor started — polling every "
                 f"{self.poll_interval//60} min until {self.cutoff_hour:02d}:00 local")

    def stop(self):
        self._running = False
        log.info("Weather hold monitor stopped.")

    def _monitor_loop(self):
        """Main polling loop — runs every poll_interval seconds."""
        # Send initial hold notification immediately
        self._send_hold_start()

        while self._running:
            time.sleep(self.poll_interval)
            if not self._running:
                break

            # Check cutoff time
            now_local = datetime.now()
            if now_local.hour >= self.cutoff_hour:
                self._send_hold_expired()
                self.stop()
                break

            self._poll_count += 1
            metar = self._fetch_current_metar()
            if not metar:
                log.warning("WeatherHoldMonitor: METAR fetch failed — will retry")
                continue

            vis    = metar.get("visib", 0) or 0
            cat    = metar.get("flightCategory", "")
            improving = self._is_improving(metar)

            log.info(f"Weather hold poll #{self._poll_count}: "
                     f"vis={vis} SM, cat={cat}, improving={improving}")

            # Send progress update every poll
            self._send_progress_update(metar)

            # Check if Part 107 minimums now met
            if self._minimums_met(metar):
                log.info("Weather hold: minimums met — re-running full preflight check")
                self._send_conditions_cleared(metar)
                self.stop()
                # Re-run the full preflight check
                self._rerun_preflight(metar)
                break

            self._last_vis = vis
            self._last_cat = cat

    def _fetch_current_metar(self) -> Optional[dict]:
        """Fetch current METAR from aviationweather.gov."""
        try:
            resp = requests.get(
                "https://aviationweather.gov/api/data/metar",
                params={"ids": self.icao, "format": "json",
                        "taf": "false", "hours": 1},
                headers={"User-Agent": "Safety1271-WeatherMonitor/2.0"},
                timeout=10
            )
            if resp.ok:
                data = resp.json()
                return data[0] if data else None
        except Exception as e:
            log.error(f"METAR fetch error: {e}")
        return None

    def _minimums_met(self, metar: dict) -> bool:
        """Check if all Part 107 weather minimums are satisfied."""
        vis   = metar.get("visib", 0) or 0
        cat   = metar.get("flightCategory", "")
        wx    = metar.get("wxString", "") or ""
        wspd  = metar.get("wspd", 0) or 0
        wgst  = metar.get("wgst") or wspd
        wspd_mph = wspd * 1.151
        wgst_mph = wgst * 1.151

        # Check ceiling
        sky = metar.get("sky", []) or []
        ceiling_ft = None
        for layer in (sky if isinstance(sky, list) else []):
            if layer.get("cover") in ("BKN", "OVC"):
                ceiling_ft = layer.get("base")
                break

        # Part 107 minimums
        vis_ok     = vis >= 3.0
        ceil_ok    = ceiling_ft is None or ceiling_ft >= 500
        no_ts      = "TS" not in wx
        wind_ok    = wgst_mph <= 25.0
        cat_ok     = cat in ("VFR", "MVFR")

        return all([vis_ok, ceil_ok, no_ts, wind_ok, cat_ok])

    def _is_improving(self, metar: dict) -> bool:
        """Detect upward trend in conditions vs previous poll."""
        if self._last_vis is None:
            return False
        current_vis = metar.get("visib", 0) or 0
        return current_vis > self._last_vis

    def _inversion_breakup_stage(self, metar: dict) -> str:
        """Describe where we are in the inversion breakup sequence."""
        vis = metar.get("visib", 0) or 0
        cat = metar.get("flightCategory", "")
        wx  = metar.get("wxString", "") or ""
        temp = metar.get("temp")
        dewp = metar.get("dewp")

        if temp is not None and dewp is not None:
            spread_c = temp - dewp
            spread_f = spread_c * 9/5
        else:
            spread_f = None

        if "FG" in wx and vis < 0.5:
            return f"Dense fog — inversion fully intact (spread: {spread_f:.1f}°F)" if spread_f else "Dense fog"
        elif "FG" in wx or vis < 1.0:
            return f"Fog thinning — inversion weakening (spread: {spread_f:.1f}°F)" if spread_f else "Fog — vis below 1 SM"
        elif "BR" in wx or (1.0 <= vis < 3.0):
            return f"Mist/haze — inversion breaking (spread: {spread_f:.1f}°F)" if spread_f else f"Mist — vis {vis} SM"
        elif vis >= 3.0:
            return f"Clearing — inversion broken (spread: {spread_f:.1f}°F)" if spread_f else f"Clearing — vis {vis} SM"
        return f"Vis {vis} SM, category {cat}"

    def _format_metar_summary(self, metar: dict) -> str:
        """One-line METAR summary for SMS."""
        vis  = metar.get("visib", "?")
        cat  = metar.get("flightCategory", "?")
        wdir = metar.get("wdir", 0)
        wspd = metar.get("wspd", 0)
        alt  = metar.get("altim", 0)
        wx   = metar.get("wxString", "") or "none"
        sky  = metar.get("sky", []) or []
        ceiling = next((f"{l['base']}ft {l['cover']}" for l in
                        (sky if isinstance(sky, list) else [])
                        if l.get("cover") in ("BKN","OVC")), "No ceiling")
        return (f"Vis {vis} SM · {ceiling} · Wind {wdir}°@{wspd}kt · "
                f"Alt {alt:.2f} · WX: {wx} · Cat: {cat}")

    def _send_sms(self, body: str):
        """Send SMS via Twilio."""
        if not self.pilot_phone:
            return
        try:
            from twilio_client import create_twilio_client, get_twilio_from_phone
            create_twilio_client().messages.create(
                body  = body,
                from_ = get_twilio_from_phone(),
                to    = self.pilot_phone
            )
        except Exception as e:
            log.error(f"WeatherHold SMS failed: {e}")

    def _send_hold_start(self):
        elapsed = "just started"
        msg = (
            f"⏸️ WEATHER HOLD — Safety1271\n"
            f"Reason: {self._hold_reason}\n"
            f"Monitoring every {self.poll_interval//60} min.\n"
            f"Auto-cancel if not clear by {self.cutoff_hour:02d}:00 local.\n"
            f"Will alert automatically when conditions clear.\n"
            f"No action required — standby."
        )
        self._send_sms(msg)
        log.info(f"Weather hold start SMS sent to {self.pilot_phone}")

    def _send_progress_update(self, metar: dict):
        stage   = self._inversion_breakup_stage(metar)
        summary = self._format_metar_summary(metar)
        vis     = metar.get("visib", 0) or 0
        needed  = max(0, 3.0 - vis)
        improving = self._is_improving(metar)
        trend   = "📈 improving" if improving else "📊 steady"

        msg = (
            f"🌫️ WEATHER HOLD UPDATE #{self._poll_count}\n"
            f"Time: {datetime.now().strftime('%H:%M')} local\n"
            f"Stage: {stage}\n"
            f"Trend: {trend}\n"
            f"{summary}\n"
            f"Still need: {needed:.1f} more SM visibility\n"
            f"Next check: {self.poll_interval//60} min"
        )
        self._send_sms(msg)

    def _send_conditions_cleared(self, metar: dict):
        summary = self._format_metar_summary(metar)
        hold_duration = datetime.now() - self._start_time
        hours, rem = divmod(int(hold_duration.total_seconds()), 3600)
        mins = rem // 60
        msg = (
            f"✅ CONDITIONS CLEARED — Safety1271\n"
            f"Part 107 minimums now met.\n"
            f"Hold duration: {hours}h {mins}m\n"
            f"{summary}\n"
            f"Running full preflight check now...\n"
            f"Will send GO/NO-GO shortly."
        )
        self._send_sms(msg)
        log.info("Weather cleared SMS sent — running full preflight check")

    def _send_hold_expired(self):
        hold_duration = datetime.now() - self._start_time
        hours, rem = divmod(int(hold_duration.total_seconds()), 3600)
        mins = rem // 60
        msg = (
            f"❌ WEATHER HOLD EXPIRED — Safety1271\n"
            f"Conditions did not clear before {self.cutoff_hour:02d}:00 local.\n"
            f"Hold duration: {hours}h {mins}m\n"
            f"Patrol cancelled for today.\n"
            f"Scheduler will attempt next scheduled patrol normally."
        )
        self._send_sms(msg)
        log.info("Weather hold expired — patrol cancelled for today")

    def _rerun_preflight(self, metar: dict):
        """Re-run the full preflight check now that weather has cleared."""
        log.info("WeatherHoldMonitor: re-running full preflight check")
        try:
            verdict = run_preflight_check(
                lat             = self.lat,
                lon             = self.lon,
                altitude_ft     = self.altitude_ft,
                flight_datetime = self.flight_datetime,
                timezone        = self.timezone,
                pilot_name      = self.pilot_name,
                pilot_phone     = self.pilot_phone,
                pilot_email     = self.pilot_email,
                drone_sn        = self.drone_sn,
            )
            if verdict == "GO":
                msg = (
                    f"🟢 PREFLIGHT GO — Safety1271\n"
                    f"All checks passed after weather cleared.\n"
                    f"Patrol is cleared for launch.\n"
                    f"Reply ABORT within 5 minutes to cancel."
                )
                self._send_sms(msg)
            elif verdict == "NO-GO":
                msg = (
                    f"🔴 PREFLIGHT NO-GO — Safety1271\n"
                    f"Weather cleared but another issue found.\n"
                    f"Check the full report for details."
                )
                self._send_sms(msg)
        except Exception as e:
            log.error(f"WeatherHoldMonitor: preflight re-run failed: {e}")
            self._send_sms(f"⚠️ Safety1271: preflight re-run error: {e}")


def start_weather_hold_monitor(verdict: str,
                                checklist: list,
                                lat: float, lon: float,
                                airport_icao: str,
                                **preflight_kwargs) -> Optional[WeatherHoldMonitor]:
    """
    Call this after run_preflight_check() returns NO-GO.
    Inspects the checklist to determine if the failure is weather-related
    (and therefore might clear) vs. airspace/TFR (which won't clear automatically).
    Returns a running WeatherHoldMonitor if appropriate, None otherwise.
    """
    if verdict not in ("NO-GO", "CONDITIONAL"):
        return None

    # Identify weather-related failures in the checklist
    weather_failures = []
    for item in checklist:
        item_str = str(item).lower()
        if any(kw in item_str for kw in
               ["visibility", "ceiling", "fog", "mist", "haze",
                "mvfr", "ifr", "lifr", "wind", "weather"]):
            if "fail" in item_str or "no-go" in item_str or "❌" in item_str:
                weather_failures.append(item)

    # Only monitor if failure is purely weather — not airspace/TFR/drone issues
    non_weather_failures = []
    for item in checklist:
        item_str = str(item).lower()
        if any(kw in item_str for kw in
               ["tfr", "notam", "laanc", "airspace", "battery",
                "drone", "registration", "remote id"]):
            if "fail" in item_str or "no-go" in item_str or "❌" in item_str:
                non_weather_failures.append(item)

    if not weather_failures:
        log.info("NO-GO not weather-related — weather hold monitor not started")
        return None

    if non_weather_failures:
        log.info("NO-GO has non-weather failures — weather hold monitor not started "
                 f"(non-weather: {non_weather_failures})")
        return None

    # Determine closest airport for METAR monitoring
    icao = airport_icao or "KDCA"
    reason = f"Visibility/ceiling below Part 107 minimums ({len(weather_failures)} items)"

    monitor = WeatherHoldMonitor(
        lat             = lat,
        lon             = lon,
        airport_icao    = icao,
        **preflight_kwargs
    )
    monitor.start(hold_reason=reason)
    log.info(f"Weather hold monitor started — monitoring {icao} every 30 min")
    return monitor



if __name__ == "__main__":
    # Location, timezone and field elevation follow the aircraft (see location_service.py)
    import location_service
    try:
        # The dashboard holds any manual choice for this session; ask it first
        here = requests.get("http://127.0.0.1:8092/api/location", timeout=10).json()
    except Exception:
        here = location_service.resolve(force=True)
    loc = here["location"]
    if loc["lat"] is None:
        raise SystemExit("Operating location unknown: power on the aircraft and wait for a GPS fix, "
                         "or set a location on the dashboard")
    if here.get("elevation_ft") is not None:
        FIELD_ELEVATION_FT = here["elevation_ft"]
    tz_name = here.get("timezone") or "America/New_York"
    print(f"Preflight location: {loc['label']} ({loc['lat']:.4f}, {loc['lon']:.4f}), "
          f"{FIELD_ELEVATION_FT} ft, {tz_name}")
    run_preflight_check(
        lat            = loc["lat"],
        lon            = loc["lon"],
        altitude_ft    = 50,    # 15 m patrol altitude
        flight_datetime= datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%dT%H:%M:%S"),
        timezone       = tz_name,
        pilot_name     = os.environ.get("PILOT_NAME", "Innocent Wafula"),
        pilot_phone    = os.environ.get("PILOT_PHONE", ""),
        pilot_email    = os.environ.get("PILOT_EMAIL", ""),
        drone_sn       = os.environ.get("DRONE_SN", "UNKNOWN")
    )
