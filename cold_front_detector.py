"""
cold_front_detector.py
=======================
Detects approaching cold fronts by monitoring METAR observations every
10 minutes for the characteristic pre-frontal meteorological signature.

Cold fronts are the most dangerous weather system for small UAS because
conditions can deteriorate dramatically BEFORE they look threatening —
a warm sunny morning can become an unflyable thunderstorm within minutes
of frontal passage.

PRE-FRONTAL SIGNALS (warm sector ahead of front):
  Wind backing   — counterclockwise shift (SW→S→SE→E) as front approaches
  Pressure fall  — altimeter dropping > 0.02 inHg/hour
  Temp rising    — warm air being drawn northward ahead of the front
  Dewpoint rising— moisture increasing as warm moist air advects in
  High cloud     — cirrus (CI) layer appearing at high altitude first

FRONTAL PASSAGE SIGNALS (front overhead):
  Wind veer      — sudden clockwise shift to NW or W (often within minutes)
  Pressure rise  — sharp rise after the pre-frontal fall
  Temp drop      — cold air mass replacing warm air (can drop 10°C+ rapidly)
  Vis drop       — heavy rain, hail, or low cloud obscuration
  TS code        — thunderstorm in METAR

ALERT THRESHOLDS:
  WATCH  — 2 pre-frontal signals (front possible within 6-12 hours)
  WARNING— 3+ pre-frontal signals (front likely within 2-4 hours)
  URGENT — 1+ frontal passage signal (front is here — land immediately)

Polls: every 10 minutes (configurable via FRONT_CHECK_INTERVAL_MIN env var)
Source: aviationweather.gov METAR API (free, no key required)
"""

import os, math, logging, threading, time, json
from datetime import datetime, timezone
from typing   import Optional, List, Dict
from collections import deque

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("ColdFrontDetector")

FRONT_CHECK_INTERVAL_MIN = int(os.getenv("FRONT_CHECK_INTERVAL_MIN", "10"))
ALERT_PHONE  = os.getenv("ALERT_PHONE", "")
ALERT_EMAIL  = os.getenv("ALERT_EMAIL", "")

# Alert thresholds
WATCH_THRESHOLD   = 2    # pre-frontal signals for WATCH
WARNING_THRESHOLD = 3    # pre-frontal signals for WARNING
URGENT_SIGNALS    = 1    # any frontal passage signal = URGENT


# ══════════════════════════════════════════════════════════════════════════════
#  METAR FETCHER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_metar_history(icao: str, hours: int = 3) -> List[dict]:
    """
    Fetch last N hours of METAR observations for trend analysis.
    Multiple observations allow trend calculation (rising/falling).
    Returns list of parsed METAR dicts, newest first.
    """
    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": icao, "format": "json",
                    "taf": "false", "hours": hours},
            headers={"User-Agent": "Safety1271-ColdFrontDetector/1.0"},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"METAR history fetch failed for {icao}: {e}")
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL DETECTORS
# ══════════════════════════════════════════════════════════════════════════════

def detect_wind_backing(observations: List[dict]) -> dict:
    """
    Wind backing = counterclockwise direction shift.
    Cold front approaches from SW → wind shifts S → SE → E as it gets closer.
    Returns signal strength and direction sequence.
    """
    if len(observations) < 2:
        return {"detected": False, "detail": "Insufficient data"}

    dirs = []
    for obs in observations[:4]:  # last 4 observations
        d = obs.get("wdir")
        s = obs.get("wspd", 0) or 0
        if d is not None and s >= 3:  # ignore calm winds
            dirs.append(d)

    if len(dirs) < 2:
        return {"detected": False, "detail": "Insufficient wind data"}

    # Check for backing: newest direction minus oldest should be negative
    # (counterclockwise), accounting for 360° wrap
    newest = dirs[0]
    oldest = dirs[-1]
    diff = newest - oldest

    # Normalise to -180 to +180
    while diff > 180:  diff -= 360
    while diff < -180: diff += 360

    backing = diff < -15  # at least 15° counterclockwise shift

    if backing:
        dir_seq = " → ".join(str(d) + "°" for d in reversed(dirs))
        return {
            "detected": True,
            "detail": f"Wind backing {dir_seq} ({abs(diff):.0f}° CCW shift)",
            "severity": "HIGH" if abs(diff) > 45 else "MODERATE"
        }
    return {"detected": False, "detail": f"Wind not backing (shift: {diff:+.0f}°)"}


def detect_pressure_fall(observations: List[dict]) -> dict:
    """
    Falling pressure = classic pre-frontal signal.
    Threshold: > 0.02 inHg/hour is notable; > 0.05/hr is rapid and urgent.
    """
    if len(observations) < 2:
        return {"detected": False, "detail": "Insufficient data"}

    alts = []
    for obs in observations[:4]:
        a = obs.get("altim")
        if a is not None:
            alts.append(float(a))

    if len(alts) < 2:
        return {"detected": False, "detail": "No altimeter data"}

    # Pressure change per hour
    # Observations are typically 20-60 minutes apart
    # Use first and last to get overall trend
    change = alts[0] - alts[-1]  # newest minus oldest
    # Estimate hours elapsed (assume ~1 hr between observations on average)
    hours_elapsed = max(0.5, len(alts) * 0.5)
    change_per_hr = change / hours_elapsed

    if change_per_hr < -0.02:
        severity = "HIGH" if change_per_hr < -0.05 else "MODERATE"
        return {
            "detected": True,
            "detail": f"Pressure falling {abs(change_per_hr):.3f} inHg/hr "
                      f"({alts[-1]:.2f} → {alts[0]:.2f} inHg)",
            "severity": severity,
            "change_per_hr": change_per_hr
        }
    return {"detected": False,
            "detail": f"Pressure stable/rising ({change_per_hr:+.3f} inHg/hr)"}


def detect_temp_rising(observations: List[dict]) -> dict:
    """
    Temperature rising ahead of cold front as warm sector air advects in.
    """
    if len(observations) < 2:
        return {"detected": False, "detail": "Insufficient data"}

    temps = [obs.get("temp") for obs in observations[:4]
             if obs.get("temp") is not None]

    if len(temps) < 2:
        return {"detected": False, "detail": "No temperature data"}

    change = temps[0] - temps[-1]  # newest minus oldest

    if change > 1.5:  # rose at least 1.5°C over observation period
        return {
            "detected": True,
            "detail": f"Temperature rising {change:.1f}°C "
                      f"({temps[-1]:.0f}°C → {temps[0]:.0f}°C)",
            "severity": "HIGH" if change > 4 else "MODERATE"
        }
    return {"detected": False, "detail": f"Temperature not rising ({change:+.1f}°C)"}


def detect_dewpoint_rising(observations: List[dict]) -> dict:
    """
    Rising dewpoint = increasing moisture — warm moist air advecting ahead of front.
    """
    if len(observations) < 2:
        return {"detected": False, "detail": "Insufficient data"}

    dewps = [obs.get("dewp") for obs in observations[:4]
             if obs.get("dewp") is not None]

    if len(dewps) < 2:
        return {"detected": False, "detail": "No dewpoint data"}

    change = dewps[0] - dewps[-1]

    if change > 2.0:
        return {
            "detected": True,
            "detail": f"Dewpoint rising {change:.1f}°C — moisture increasing "
                      f"({dewps[-1]:.0f}°C → {dewps[0]:.0f}°C)",
            "severity": "MODERATE"
        }
    return {"detected": False, "detail": f"Dewpoint not rising ({change:+.1f}°C)"}


def detect_high_cloud_advance(observations: List[dict]) -> dict:
    """
    High cirrus appearing is the EARLIEST cold front indicator —
    can precede the surface front by 12-24 hours.
    Look for FEW or SCT layers above 20,000 ft MSL (200 in METAR hundreds).
    """
    if not observations:
        return {"detected": False, "detail": "No data"}

    latest = observations[0]
    sky = latest.get("sky", []) or []

    for layer in (sky if isinstance(sky, list) else []):
        base = layer.get("base")
        cover = layer.get("cover", "")
        if base is not None and base >= 200 and cover in ("FEW", "SCT", "BKN"):
            return {
                "detected": True,
                "detail": f"High cloud at {base*100:,} ft AGL ({cover}) — "
                          f"possible cirrus advance warning",
                "severity": "LOW"
            }
    return {"detected": False, "detail": "No high-altitude cloud detected"}


def detect_frontal_passage(observations: List[dict]) -> List[dict]:
    """
    Frontal passage signals — front is HERE. Any single signal = URGENT.
    Returns list of detected passage signals.
    """
    if not observations:
        return []

    latest    = observations[0]
    signals   = []

    wx_str = (latest.get("wxString") or "").upper()
    cat    = latest.get("flightCategory", "")
    vis    = latest.get("visib") or 0
    temp   = latest.get("temp")
    wdir   = latest.get("wdir")
    wspd   = latest.get("wspd") or 0
    alt    = latest.get("altim")

    # Thunderstorm detected
    if "TS" in wx_str:
        signals.append({
            "signal": "THUNDERSTORM",
            "detail": f"TS in METAR: {wx_str}",
            "severity": "CRITICAL"
        })

    # Sudden wind veer to NW/W with increased speed
    if wdir is not None and wspd >= 10:
        if 270 <= wdir <= 340 or wdir <= 30:  # NW to N sector
            if len(observations) >= 2:
                prev_dir = observations[1].get("wdir")
                if prev_dir is not None:
                    shift = wdir - prev_dir
                    while shift > 180:  shift -= 360
                    while shift < -180: shift += 360
                    if shift > 30:  # clockwise shift
                        signals.append({
                            "signal": "WIND_VEER_NW",
                            "detail": f"Sudden NW wind veer: {prev_dir}° → {wdir}° "
                                      f"at {wspd} kts (frontal passage)",
                            "severity": "URGENT"
                        })

    # IFR or LIFR flight category
    if cat in ("IFR", "LIFR"):
        signals.append({
            "signal": "IFR_CONDITIONS",
            "detail": f"Flight category: {cat} — vis {vis} SM",
            "severity": "URGENT"
        })

    # Sudden pressure rise after fall
    if alt is not None and len(observations) >= 3:
        alts = [o.get("altim") for o in observations[:3] if o.get("altim")]
        if len(alts) == 3:
            if alts[0] > alts[1] > alts[2]:  # newest > middle > oldest = rising
                rise = alts[0] - alts[2]
                if rise > 0.04:
                    signals.append({
                        "signal": "PRESSURE_RISE",
                        "detail": f"Sharp pressure rise {rise:.3f} inHg — "
                                  f"post-frontal cold air arriving",
                        "severity": "HIGH"
                    })

    # Sharp temperature drop
    if temp is not None and len(observations) >= 2:
        prev_temp = observations[1].get("temp")
        if prev_temp is not None:
            drop = prev_temp - temp
            if drop > 3.0:
                signals.append({
                    "signal": "TEMP_DROP",
                    "detail": f"Temperature dropped {drop:.1f}°C "
                              f"({prev_temp:.0f}°C → {temp:.0f}°C) — cold air mass",
                    "severity": "HIGH"
                })

    return signals




# ══════════════════════════════════════════════════════════════════════════════
#  OCCLUDED FRONT DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

def detect_occluded_front(observations: List[dict]) -> dict:
    """
    Occluded front signature differs from a cold front in key ways:
    — No pre-frontal warming (warm air already lifted off surface)
    — Cool temperatures on BOTH sides of the front
    — Complex layered cloud structure (multiple layers at different altitudes)
    — Embedded thunderstorms hidden inside general precipitation
    — Highly variable and rapidly changing ceiling and visibility
    — Pressure trough (lower than surrounding areas) rather than clear fall/rise

    Detection strategy:
    1. Multiple simultaneous cloud layers (complex layering)
    2. Embedded TS within otherwise moderate precipitation
    3. Ceiling rapidly oscillating (not steadily falling as with a cold front)
    4. Persistent moderate precipitation without classic pre-frontal warming
    5. Pressure trough — lowest in current observation vs history, not trending
    6. Cool air mass with no warming signal (distinguishes from cold front approach)
    """
    if not observations:
        return {"detected": False, "detail": "No data", "signals": []}

    latest   = observations[0]
    signals  = []
    detected = False

    wx_str   = (latest.get("wxString") or "").upper()
    sky      = latest.get("sky", []) or []
    temp     = latest.get("temp")
    dewp     = latest.get("dewp")
    alt      = latest.get("altim")
    vis      = latest.get("visib") or 0
    cat      = latest.get("flightCategory", "")

    # Signal 1 — Multiple cloud layers (complex layering typical of occlusion)
    if isinstance(sky, list):
        n_layers = len(sky)
        if n_layers >= 3:
            layer_descs = [f"{l.get('cover','')} {l.get('base','')}ft"
                           for l in sky[:4]]
            signals.append(
                f"Complex multi-layer cloud structure: "
                f"{', '.join(layer_descs)} ({n_layers} layers)"
            )

    # Signal 2 — Embedded thunderstorm within precipitation
    # TSRA or TS alongside RA/RASN/FZRA = embedded TS in general precip
    has_ts   = "TS" in wx_str
    has_rain = any(p in wx_str for p in ["RA", "DZ", "SN", "RASN", "FZRA"])
    if has_ts and has_rain:
        signals.append(
            f"Embedded thunderstorm in precipitation: {wx_str} "
            f"— hidden TS hazard characteristic of occluded fronts"
        )

    # Signal 3 — Variable/oscillating ceiling over recent observations
    if len(observations) >= 3:
        ceilings = []
        for obs in observations[:4]:
            obs_sky = obs.get("sky", []) or []
            for layer in (obs_sky if isinstance(obs_sky, list) else []):
                if layer.get("cover") in ("BKN", "OVC", "VV"):
                    ceilings.append(layer.get("base") or 0)
                    break
        if len(ceilings) >= 3:
            ceiling_range = max(ceilings) - min(ceilings)
            if ceiling_range >= 20:   # 2,000 ft swing in ceiling
                signals.append(
                    f"Rapidly variable ceiling: oscillating "
                    f"{min(ceilings)*100:,}–{max(ceilings)*100:,} ft AGL "
                    f"({ceiling_range*100:,} ft range) — occlusion characteristic"
                )

    # Signal 4 — Persistent moderate precip WITHOUT pre-frontal warming
    # (If temp is not rising but rain/snow is falling — occluded signature)
    has_precip = any(p in wx_str for p in ["RA", "-RA", "+RA", "DZ", "SN"])
    temp_rising = False
    if len(observations) >= 2 and temp is not None:
        prev_temp = observations[1].get("temp")
        if prev_temp is not None:
            temp_rising = temp > prev_temp + 1.0

    if has_precip and not temp_rising and not has_ts:
        signals.append(
            f"Persistent precipitation without pre-frontal warming: {wx_str} "
            f"— cool occluded air mass on both sides of front"
        )

    # Signal 5 — Pressure trough (lowest in recent history, not clearly trending)
    if alt is not None and len(observations) >= 3:
        alts = [o.get("altim") for o in observations[:4] if o.get("altim")]
        if len(alts) >= 3:
            # Trough: current reading is LOWER than both earlier and later
            # i.e. alts[1] > alts[0] < alts[2] (current is the minimum)
            is_trough = (len(alts) >= 3 and
                         alts[0] < alts[1] and alts[0] < alts[2])
            if is_trough:
                signals.append(
                    f"Pressure trough: {alts[2]:.2f} → {alts[0]:.2f} → ... inHg "
                    f"— current reading is local minimum indicating frontal trough"
                )

    # Signal 6 — IFR with no single dominant cause
    # Occluded fronts produce IFR from layered combination rather than one clear cause
    if cat in ("IFR", "LIFR") and has_precip and n_layers if isinstance(sky, list) else False:
        signals.append(
            f"IFR conditions from complex combination: {cat}, vis {vis} SM, "
            f"precipitation, multiple cloud layers — occluded front signature"
        )

    detected = len(signals) >= 2   # need at least 2 signals for occluded diagnosis

    # Severity assessment
    if has_ts and has_rain:
        severity = "HIGH"     # embedded TS is always high risk
    elif len(signals) >= 3:
        severity = "MODERATE"
    elif len(signals) >= 2:
        severity = "LOW"
    else:
        severity = "NONE"

    return {
        "detected":  detected,
        "severity":  severity,
        "signals":   signals,
        "n_signals": len(signals),
        "detail":    (
            f"Occluded front — {len(signals)} signals detected. "
            + " | ".join(signals[:2])
            if detected else
            f"No occluded front signature ({len(signals)} signals, need 2+)"
        ),
        "embedded_ts": has_ts and has_rain,
    }






# ══════════════════════════════════════════════════════════════════════════════
#  THUNDERSTORM STAGE DETECTOR
#  Watches for all three stages of thunderstorm development so the RPIC
#  gets the earliest possible warning — not just when TS is already mature.
# ══════════════════════════════════════════════════════════════════════════════

def detect_thunderstorm_stage(observations: List[dict]) -> dict:
    """
    Detect which stage of thunderstorm development is occurring and
    how close the cell is to entering the 20 NM danger buffer.

    STAGE 1 — CUMULUS (building) — detectable by:
      • Rapidly rising temperature + high dewpoint = instability building
      • Towering cumulus: TCU code in METAR sky condition
      • CB (cumulonimbus) just appearing at high altitude
      • Rapid pressure fall (convective instability)
      • Temp-dewpoint spread closing rapidly (< 5°F and narrowing)
      • Convective SIGMET issued but no TS in local METAR yet
      METAR codes: TCU, CB forming

    STAGE 2 — MATURE (most dangerous) — detectable by:
      • TS code in METAR (thunderstorm at or near station)
      • VCTS (TS in vicinity — 5-10 SM from station)
      • CB in sky condition at any level
      • +TSRA / TSGR (heavy TS rain or hail)
      • Lightning reported (LTG in remarks)
      • Rapid visibility drop + strong gusts + shifting wind
      METAR codes: TS, VCTS, +TSRA, TSGR, LTG

    STAGE 3 — DISSIPATING — detectable by:
      • TS code disappearing from METAR after being present
      • Precipitation continuing but TS code dropped
      • Pressure rising after the fall
      • Wind speed decreasing after gusts
      • Visibility improving slowly
      METAR codes: RA without TS after TS was present, TSRA- (lightening)
    """
    if not observations:
        return {"stage": "NONE", "detail": "No data", "action": ""}

    latest   = observations[0]
    wx       = (latest.get("wxString") or "").upper()
    sky      = latest.get("sky", []) or []
    temp     = latest.get("temp")
    dewp     = latest.get("dewp")
    alt      = latest.get("altim")
    wspd     = latest.get("wspd") or 0
    wgst     = latest.get("wgst") or wspd
    vis      = latest.get("visib") or 10

    # ── Stage 2 detection (mature) — check first as highest priority ──────
    has_ts       = "TS" in wx
    has_vcts     = "VCTS" in wx
    has_cb_wx    = "CB" in wx
    has_heavy    = "+TS" in wx or "TSGR" in wx
    has_lightning= "LTG" in wx

    # CB in sky condition layers
    has_cb_sky = any(str(l.get("cover","")).upper() == "CB" or
                     "CB" in str(l.get("cloudType","")).upper()
                     for l in (sky if isinstance(sky, list) else []))

    if has_ts or has_heavy:
        phase = "STAGE 2 — MATURE"
        severity = "CRITICAL" if has_heavy else "HIGH"
        detail = (
            f"Active thunderstorm: {wx}. "
            + ("HAIL REPORTED. " if "GR" in wx else "")
            + ("Heavy precipitation. " if "+" in wx else "")
            + f"Wind {wspd} kts gusting {wgst} kts. Vis {vis} SM."
        )
        action = "LAND IMMEDIATELY — mature thunderstorm. 20 NM buffer violated."
        return {
            "stage":    phase,
            "severity": severity,
            "detail":   detail,
            "action":   action,
            "in_20nm_auto": True,  # by definition if TS is at local station
            "wx":       wx,
        }

    if has_vcts or has_lightning:
        phase = "STAGE 2 — MATURE (vicinity)"
        detail = (
            f"Thunderstorm in vicinity: {wx}. "
            f"Lightning detected within 5-10 SM. "
            f"Effective 20 NM buffer may be violated."
        )
        return {
            "stage":    phase,
            "severity": "HIGH",
            "detail":   detail,
            "action":   "RTH NOW — TS in vicinity. Do not wait for local TS code.",
            "in_20nm_auto": True,
            "wx":       wx,
        }

    # ── Stage 3 detection (dissipating) ──────────────────────────────────
    # Previous observation had TS, current does not = dissipating
    prev_had_ts = False
    if len(observations) >= 2:
        prev_wx = (observations[1].get("wxString") or "").upper()
        prev_had_ts = "TS" in prev_wx

    has_rain = any(p in wx for p in ["RA","DZ","SN"])
    if prev_had_ts and has_rain and not has_ts:
        # Check pressure rising (post-frontal)
        rising = False
        if alt and len(observations) >= 2:
            prev_alt = observations[1].get("altim")
            if prev_alt and alt > prev_alt:
                rising = True
        return {
            "stage":    "STAGE 3 — DISSIPATING",
            "severity": "MODERATE",
            "detail":   (f"Thunderstorm weakening — TS code dropped, "
                         f"precipitation continuing ({wx}). "
                         + ("Pressure rising — cold air arriving." if rising else "")),
            "action":   ("Continue holding on ground — residual hazards remain. "
                         "Wait 20+ minutes after last lightning before resuming."),
            "in_20nm_auto": False,
            "wx": wx,
        }

    # ── Stage 1 detection (cumulus — pre-storm building) ──────────────────
    stage1_signals = []

    # TCU or CB appearing in sky condition
    for layer in (sky if isinstance(sky, list) else []):
        cover = str(layer.get("cover","")).upper()
        ctype = str(layer.get("cloudType","")).upper()
        base  = layer.get("base")
        if "TCU" in cover or "TCU" in ctype:
            stage1_signals.append(
                f"Towering cumulus (TCU) at {(base or 0)*100:,} ft — "
                f"convective development in progress"
            )
        elif "CB" in cover or "CB" in ctype:
            stage1_signals.append(
                f"Cumulonimbus (CB) at {(base or 0)*100:,} ft — "
                f"thunderstorm cell forming, Stage 1 → Stage 2 transition"
            )

    # Temperature/dewpoint spread closing
    if temp is not None and dewp is not None:
        spread_c = temp - dewp
        spread_f = spread_c * 9/5
        if spread_f <= 5:
            stage1_signals.append(
                f"Temp-dewpoint spread {spread_f:.1f}°F "
                f"(temp {temp:.0f}°C / dewp {dewp:.0f}°C) — "
                f"atmosphere near saturation, convection possible"
            )

        # Rapid spread closing trend
        if len(observations) >= 2:
            prev_temp = observations[1].get("temp")
            prev_dewp = observations[1].get("dewp")
            if prev_temp is not None and prev_dewp is not None:
                prev_spread_f = (prev_temp - prev_dewp) * 9/5
                if prev_spread_f - spread_f >= 3:  # spread closing 3°F+
                    stage1_signals.append(
                        f"Spread closing rapidly: {prev_spread_f:.1f}°F → "
                        f"{spread_f:.1f}°F — instability building"
                    )

    # High temperature with high dewpoint = high convective potential
    if temp is not None and temp >= 25 and dewp is not None and dewp >= 18:
        stage1_signals.append(
            f"High instability: temp {temp:.0f}°C / dewp {dewp:.0f}°C — "
            f"convective potential elevated (afternoon hours most dangerous)"
        )

    # Rapid pressure fall (convective instability signal)
    if len(observations) >= 2 and alt is not None:
        prev_alt = observations[1].get("altim")
        if prev_alt:
            fall_rate = prev_alt - alt
            if fall_rate > 0.03:
                stage1_signals.append(
                    f"Rapid pressure fall {fall_rate:.3f} inHg — "
                    f"convective instability, potential for rapid storm development"
                )

    if stage1_signals:
        return {
            "stage":    "STAGE 1 — CUMULUS (building)",
            "severity": "MODERATE" if len(stage1_signals) >= 2 else "LOW",
            "detail":   f"{len(stage1_signals)} pre-storm signal(s): " +
                        " | ".join(stage1_signals[:2]),
            "signals":  stage1_signals,
            "action":   ("Complete patrol quickly and land — conditions favour "
                         "thunderstorm development within 30-60 min. "
                         "Do not launch new patrols." if len(stage1_signals) >= 2
                         else "Monitor closely — early convective signals present."),
            "in_20nm_auto": False,
            "wx": wx,
        }

    return {
        "stage":    "NONE",
        "severity": "NONE",
        "detail":   "No thunderstorm development signals detected.",
        "action":   "",
        "in_20nm_auto": False,
        "wx": wx,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  THUNDERSTORM 20 NM BUFFER DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

# Known airport positions for distance calculations
# Add more airports as needed for your operational area
AIRPORT_POSITIONS = {
    "KDCA": (38.8521, -77.0377),   # Reagan National
    "KIAD": (38.9531, -77.4565),   # Dulles International
    "KDAA": (38.7150, -77.1808),   # Davison AAF
    "KJYO": (39.0780, -77.5575),   # Leesburg Executive
    "KHEF": (38.7214, -77.5152),   # Manassas Regional
    "KBWI": (39.1754, -76.6682),   # Baltimore Washington
    "KRIC": (37.5052, -77.3197),   # Richmond International
    "KOKV": (39.1433, -78.1456),   # Winchester Regional
    "KFRR": (38.9175, -78.2533),   # Front Royal
    "KORF": (36.9776, -76.0355),   # Norfolk International (for awareness)
}

def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in nautical miles."""
    import math
    R   = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dp  = math.radians(lat2 - lat1)
    dl  = math.radians(lon2 - lon1)
    a   = math.sin(dp/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))




def fetch_convective_sigmets(drone_lat: float, drone_lon: float,
                              buffer_nm: float = 20.0) -> dict:
    """
    Fetch active Convective SIGMETs from aviationweather.gov and check
    whether any polygon is within buffer_nm of the drone position.

    Convective SIGMETs cover thunderstorm cells irrespective of whether
    a METAR station is nearby — this catches storms over mountains, water,
    and open terrain that METAR-based detection would miss.

    Issued by: Aviation Weather Center (AWC)
    Valid: up to 2 hours (special issuances as needed)
    Covers: severe thunderstorms (hail ≥3/4", winds ≥50 kts, tornadoes),
            embedded TS, lines of TS, areas of TS ≥40% coverage.
    """
    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/airsigmet",
            params={"format": "json", "type": "sigmet", "hazard": "conv"},
            headers={"User-Agent": "Safety1271-SIGMETChecker/1.0"},
            timeout=10
        )
        if not resp.ok:
            return {"error": f"SIGMET fetch failed: {resp.status_code}",
                    "in_buffer": False}

        sigmets = resp.json() or []

    except Exception as e:
        return {"error": str(e), "in_buffer": False}

    hits = []
    for sig in sigmets:
        # Each SIGMET has a list of lat/lon points defining the polygon
        coords = sig.get("coords", [])
        if not coords:
            continue

        # Find closest point on the SIGMET polygon to the drone
        min_dist = float("inf")
        for point in coords:
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is None or lon is None:
                continue
            dist = haversine_nm(drone_lat, drone_lon, lat, lon)
            if dist < min_dist:
                min_dist = dist

        if min_dist <= buffer_nm:
            hits.append({
                "sigmet_id":  sig.get("sigmetId", ""),
                "hazard":     sig.get("hazard", "CONVECTIVE"),
                "intensity":  sig.get("intensity", ""),
                "movement":   sig.get("movement", ""),
                "valid_time": sig.get("validTimeTo", ""),
                "dist_nm":    round(min_dist, 1),
                "raw":        sig.get("rawAirSigmet", ""),
                "severity":   ("CRITICAL" if min_dist <= 5  else
                               "HIGH"     if min_dist <= 10 else
                               "MODERATE"),
            })

    hits.sort(key=lambda h: h["dist_nm"])

    if hits:
        closest = hits[0]
        return {
            "in_buffer":     True,
            "hits":          hits,
            "closest_nm":    closest["dist_nm"],
            "closest_id":    closest["sigmet_id"],
            "severity":      closest["severity"],
            "summary": (
                f"⛈️ CONVECTIVE SIGMET within {buffer_nm} NM: "
                f"{closest['sigmet_id']} at {closest['dist_nm']:.0f} NM. "
                f"{closest['raw'][:80]}"
            ),
            "action": (
                "LAND IMMEDIATELY — active Convective SIGMET within 5 NM"
                if closest["dist_nm"] <= 5 else
                "RTH NOW — Convective SIGMET within 10 NM"
                if closest["dist_nm"] <= 10 else
                f"Do not launch — Convective SIGMET within {buffer_nm} NM buffer"
            )
        }

    return {
        "in_buffer":  False,
        "hits":       [],
        "closest_nm": None,
        "summary":    f"No Convective SIGMETs within {buffer_nm} NM.",
        "action":     "Clear."
    }


def check_thunderstorm_20nm_buffer(
        drone_lat: float,
        drone_lon: float,
        monitored_airports: List[str] = None,
        buffer_nm: float = 20.0) -> dict:
    """
    Check all monitoring airports for thunderstorm activity.
    Returns alert if any TS-reporting station is within buffer_nm of drone.

    FAA guidance: avoid thunderstorms by at least 20 NM.
    This is especially critical because:
      — Lightning can strike up to 10 NM from a visible cell
      — Hail can extend 20 NM from the cell
      — Outflow winds and microbursts extend well beyond the visible storm
      — Radar-indicated cells may be 5-10 min old — storm is already moving

    Also checks PIREPs via METAR VC (vicinity) codes — storms reported
    in the vicinity of a station (5-10 SM from the station) count too.
    """
    airports_to_check = monitored_airports or list(AIRPORT_POSITIONS.keys())
    ts_reports   = []
    clear_reports = []

    # Query all airports in one API call
    ids_param = ",".join(a for a in airports_to_check if a in AIRPORT_POSITIONS)

    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": ids_param, "format": "json",
                    "taf": "false", "hours": 1},
            headers={"User-Agent": "Safety1271-TSBuffer/1.0"},
            timeout=10
        )
        if not resp.ok:
            return {"error": f"METAR fetch failed: {resp.status_code}"}

        observations = resp.json() or []

    except Exception as e:
        return {"error": f"METAR fetch exception: {e}"}

    for obs in observations:
        icao  = obs.get("stationId", "")
        wx    = (obs.get("wxString") or "").upper()
        raw   = obs.get("rawOb", "")

        # Airport coordinates
        if icao not in AIRPORT_POSITIONS:
            continue
        apt_lat, apt_lon = AIRPORT_POSITIONS[icao]
        dist_nm = haversine_nm(drone_lat, drone_lon, apt_lat, apt_lon)

        # Check for thunderstorm codes
        has_ts       = "TS" in wx
        has_vcsh     = "VCSH" in wx      # showers in vicinity
        has_vcts     = "VCTS" in wx      # TS in vicinity (5-10 SM from station)
        has_lightning = "LTG" in wx.upper() or "LTNG" in wx.upper()

        # Any TS indicator — including VCTS adds ~10 SM to effective distance
        ts_effective_dist = dist_nm
        if has_vcts:
            ts_effective_dist = dist_nm - 10  # vicinity = up to 10 SM closer

        if has_ts or has_vcts or has_lightning:
            # How far is the storm from the drone?
            threat_nm   = max(0, ts_effective_dist)
            in_buffer   = threat_nm <= buffer_nm
            severity    = ("CRITICAL" if threat_nm <= 5   else
                           "HIGH"     if threat_nm <= 10  else
                           "MODERATE" if threat_nm <= buffer_nm else
                           "LOW")

            ts_reports.append({
                "airport":      icao,
                "dist_to_apt_nm": round(dist_nm, 1),
                "effective_ts_dist_nm": round(threat_nm, 1),
                "in_buffer":    in_buffer,
                "severity":     severity,
                "has_ts":       has_ts,
                "has_vcts":     has_vcts,
                "has_lightning":has_lightning,
                "wx_string":    wx,
                "raw_metar":    raw,
            })
        else:
            clear_reports.append({"airport": icao, "dist_nm": round(dist_nm,1)})

    # Sort by closest effective TS distance
    ts_reports.sort(key=lambda r: r["effective_ts_dist_nm"])

    # Overall determination
    in_buffer_reports = [r for r in ts_reports if r["in_buffer"]]
    closest = ts_reports[0] if ts_reports else None

    # Also check Convective SIGMETs for non-airport storms
    sigmet_result = fetch_convective_sigmets(drone_lat, drone_lon, buffer_nm)
    if sigmet_result.get("in_buffer") and not in_buffer_reports:
        # SIGMET hit but no METAR TS — storm over terrain/water
        return {
            "ts_detected":     True,
            "in_20nm_buffer":  True,
            "should_land_now": True,
            "overall_severity": sigmet_result["severity"],
            "ts_reports":      [],
            "sigmet_result":   sigmet_result,
            "closest_ts_nm":   sigmet_result["closest_nm"],
            "summary":         sigmet_result["summary"],
            "action":          sigmet_result["action"],
        }

    if in_buffer_reports:
        closest_in = in_buffer_reports[0]
        # Merge SIGMET data if also present
        overall_severity = in_buffer_reports[0]["severity"]
        if sigmet_result.get("in_buffer"):
            if ({"NONE":0,"LOW":1,"MODERATE":2,"HIGH":3,"CRITICAL":4}.get(
                    sigmet_result["severity"],0) >
                {"NONE":0,"LOW":1,"MODERATE":2,"HIGH":3,"CRITICAL":4}.get(
                    overall_severity,0)):
                overall_severity = sigmet_result["severity"]
        should_land = closest_in["effective_ts_dist_nm"] <= buffer_nm

        return {
            "ts_detected":        True,
            "in_20nm_buffer":     True,
            "should_land_now":    should_land,
            "overall_severity":   overall_severity,
            "ts_reports":         ts_reports,
            "sigmet_result":      sigmet_result,
            "clear_stations":     len(clear_reports),
            "closest_ts_nm":      closest_in["effective_ts_dist_nm"],
            "closest_airport":    closest_in["airport"],
            "summary": (
                f"⛈️ THUNDERSTORM within {buffer_nm} NM buffer. "
                f"Closest: {closest_in['airport']} "
                f"({closest_in['effective_ts_dist_nm']:.0f} NM). "
                f"LAND IMMEDIATELY."
                if closest_in["effective_ts_dist_nm"] <= 5 else
                f"⛈️ THUNDERSTORM WARNING: {closest_in['airport']} "
                f"{closest_in['effective_ts_dist_nm']:.0f} NM away "
                f"(buffer: {buffer_nm} NM). Do not launch."
            ),
            "action": (
                "LAND IMMEDIATELY — thunderstorm within 5 NM"
                if (closest and closest["effective_ts_dist_nm"] <= 5) else
                "RTH NOW — thunderstorm within 10 NM"
                if (closest and closest["effective_ts_dist_nm"] <= 10) else
                f"Do not launch — thunderstorm within {buffer_nm} NM buffer"
            )
        }

    elif ts_reports:
        # TS exists but outside 20 NM buffer — monitor
        return {
            "ts_detected":      True,
            "in_20nm_buffer":   False,
            "should_land_now":  False,
            "overall_severity": "WATCH",
            "ts_reports":       ts_reports,
            "closest_ts_nm":    closest["effective_ts_dist_nm"],
            "closest_airport":  closest["airport"],
            "summary": (
                f"⚠️ Thunderstorm at {closest['airport']} "
                f"({closest['effective_ts_dist_nm']:.0f} NM) — "
                f"outside {buffer_nm} NM buffer but monitor closely."
            ),
            "action": "Monitor — storm outside buffer. Continue patrol with caution."
        }

    return {
        "ts_detected":    False,
        "in_20nm_buffer": False,
        "should_land_now": False,
        "overall_severity": "NONE",
        "ts_reports":     [],
        "closest_ts_nm":  None,
        "summary":        f"No thunderstorm activity within {buffer_nm} NM.",
        "action":         "Clear for operations."
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN DETECTOR CLASS
# ══════════════════════════════════════════════════════════════════════════════

class ColdFrontDetector:
    """
    Background thread monitoring for cold front approach and passage.
    Integrates with DynamicMissionManager via callback.

    Usage:
        detector = ColdFrontDetector(
            airports         = ["KDCA", "KDAA", "KJYO"],
            on_alert         = lambda level, msg: manager.trigger_immediate_recheck(msg),
            alert_phone      = os.environ["ALERT_PHONE"],
        )
        detector.start()
    """

    def __init__(self,
                 airports: List[str],
                 on_alert = None,
                 alert_phone: str = ALERT_PHONE,
                 alert_email: str = ALERT_EMAIL,
                 check_interval_min: int = FRONT_CHECK_INTERVAL_MIN):

        self.airports        = airports
        self.on_alert        = on_alert      # callback to DynamicMissionManager
        self.alert_phone     = alert_phone
        self.alert_email     = alert_email
        self.interval        = check_interval_min * 60
        self._running        = False
        self._thread         = None
        self._alert_history  = deque(maxlen=20)  # dedup recent alerts
        self._last_level     = "NONE"            # NONE / WATCH / WARNING / URGENT
        self.drone_lat       = None              # updated by patrol agent
        self.drone_lon       = None              # updated by patrol agent

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._detector_loop,
            name="ColdFrontDetector",
            daemon=True
        )
        self._thread.start()
        log.info(f"Cold front detector started — polling {self.airports} "
                 f"every {self.interval//60} min")

    def stop(self):
        self._running = False
        log.info("Cold front detector stopped.")

    def update_drone_position(self, lat: float, lon: float):
        """Call from patrol agent each time drone moves to update TS buffer check."""
        self.drone_lat = lat
        self.drone_lon = lon

    def _issue_ts_buffer_alert(self, ts_result: dict):
        """Send immediate SMS when thunderstorm enters 20 NM buffer."""
        # Dedup: don't re-alert same storm within 5 minutes
        dedup_key = f"TS_{ts_result.get('closest_airport','')}"
        now = time.time()
        if hasattr(self, "_ts_alert_times"):
            last = self._ts_alert_times.get(dedup_key, 0)
            if now - last < 300:
                return
        else:
            self._ts_alert_times = {}
        self._ts_alert_times[dedup_key] = now

        sev = ts_result.get("overall_severity","")
        emoji = "🆘" if sev == "CRITICAL" else "⛈️"

        sms = (
            f"{emoji} THUNDERSTORM {sev} — Safety1271\n"
            f"{ts_result['summary']}\n\n"
            f"ACTION: {ts_result['action']}\n\n"
        )
        for r in ts_result.get("ts_reports",[])[:3]:
            sms += (f"  {r['airport']}: {r['effective_ts_dist_nm']:.0f} NM "
                    f"| WX: {r['wx_string']}\n")

        log.critical(f"TS BUFFER ALERT [{sev}]: {ts_result['summary']}")

        if self.alert_phone:
            try:
                from twilio_client import create_twilio_client, get_twilio_from_phone
                create_twilio_client().messages.create(
                    body=sms, from_=get_twilio_from_phone(),
                    to=self.alert_phone
                )
            except Exception as e:
                log.error(f"TS buffer SMS failed: {e}")

        # Fire RTH callback for CRITICAL/HIGH
        if self.on_alert and sev in ("CRITICAL","HIGH"):
            self.on_alert("URGENT", f"TS buffer: {ts_result['action']}")

    def _detector_loop(self):
        while self._running:
            try:
                self._check_all_airports()
                # Standalone TS 20 NM + SIGMET check every poll
                if self.drone_lat is not None:
                    self._check_ts_buffer_standalone()
            except Exception as e:
                log.error(f"ColdFrontDetector error: {e}")
            time.sleep(self.interval)

    def _check_ts_buffer_standalone(self):
        """
        Run the full 20 NM thunderstorm buffer check independently of
        the cold/occluded front analysis. Combines METAR station check
        and Convective SIGMET polygon check.
        """
        # METAR-based station check
        metar_result = check_thunderstorm_20nm_buffer(
            drone_lat  = self.drone_lat,
            drone_lon  = self.drone_lon,
            buffer_nm  = 20.0
        )
        if metar_result.get("in_20nm_buffer"):
            self._issue_ts_buffer_alert(metar_result)
            return

        # Convective SIGMET check (catches storms not near airports)
        sigmet_result = fetch_convective_sigmets(
            drone_lat = self.drone_lat,
            drone_lon = self.drone_lon,
            buffer_nm = 20.0
        )
        if sigmet_result.get("in_buffer"):
            self._issue_ts_buffer_alert({
                "overall_severity": sigmet_result["severity"],
                "summary":          sigmet_result["summary"],
                "action":           sigmet_result["action"],
                "ts_reports":       [],
                "sigmet_result":    sigmet_result,
                "closest_ts_nm":    sigmet_result["closest_nm"],
            })
        else:
            log.info(f"TS 20 NM buffer clear — "
                     f"METAR: {metar_result.get('summary','')} | "
                     f"SIGMET: {sigmet_result.get('summary','')}")

    def _check_all_airports(self):
        """Check all configured airports and issue alerts on worst condition.
        Also runs thunderstorm 20 NM buffer check independently."""
        worst_level   = "NONE"
        worst_report  = None
        worst_airport = None

        # Thunderstorm 20 NM buffer check — runs on every poll
        if hasattr(self, "drone_lat") and self.drone_lat:
            ts_result = check_thunderstorm_20nm_buffer(
                drone_lat  = self.drone_lat,
                drone_lon  = self.drone_lon,
                buffer_nm  = 20.0
            )
            if ts_result.get("in_20nm_buffer"):
                self._issue_ts_buffer_alert(ts_result)
            elif ts_result.get("ts_detected"):
                log.warning(f"TS WATCH: {ts_result['summary']}")

        for icao in self.airports:
            observations = fetch_metar_history(icao, hours=3)
            if not observations:
                continue

            report = self._analyse(icao, observations)
            level  = report["alert_level"]

            level_rank = {"NONE":0,"WATCH":1,"WARNING":2,"URGENT":3}
            if level_rank.get(level,0) > level_rank.get(worst_level,0):
                worst_level   = level
                worst_report  = report
                worst_airport = icao

        if worst_report and worst_level != "NONE":
            self._issue_alert(worst_level, worst_airport, worst_report)
        else:
            log.info(f"Cold front check: no frontal signals detected at "
                     f"{', '.join(self.airports)}")

    def _analyse(self, icao: str, observations: List[dict]) -> dict:
        """Run all signal detectors against the observation history."""

        # Pre-frontal signal battery
        prefrontal = {
            "wind_backing":       detect_wind_backing(observations),
            "pressure_fall":      detect_pressure_fall(observations),
            "temp_rising":        detect_temp_rising(observations),
            "dewpoint_rising":    detect_dewpoint_rising(observations),
            "high_cloud_advance": detect_high_cloud_advance(observations),
        }

        # Frontal passage signals
        passage_signals = detect_frontal_passage(observations)

        # Count pre-frontal signals
        active_prefrontal = {k: v for k, v in prefrontal.items()
                             if v.get("detected")}
        n_prefrontal = len(active_prefrontal)

        # Determine alert level
        latest = observations[0] if observations else {}
        if passage_signals:
            level = "URGENT"
        elif n_prefrontal >= WARNING_THRESHOLD:
            level = "WARNING"
        elif n_prefrontal >= WATCH_THRESHOLD:
            level = "WATCH"
        else:
            level = "NONE"

        # Thunderstorm stage check
        ts_stage = detect_thunderstorm_stage(observations)
        if ts_stage["stage"] != "NONE":
            ts_sev_rank = {"LOW":1,"MODERATE":2,"HIGH":3,"CRITICAL":4}
            if ts_sev_rank.get(ts_stage["severity"],0) >= 3:
                level = "URGENT"   # HIGH or CRITICAL = URGENT
            elif ts_sev_rank.get(ts_stage["severity"],0) >= 2:
                if level_rank.get(level,0) < level_rank.get("WARNING",0):
                    level = "WARNING"

        # Occluded front check
        occluded = detect_occluded_front(observations)
        if occluded["detected"] and occluded["severity"] in ("HIGH","MODERATE"):
            if level_rank.get(level,0) < level_rank.get("WARNING",0):
                level = "WARNING"   # occluded fronts always at least WARNING

        return {
            "airport":           icao,
            "alert_level":       level,
            "prefrontal_count":  n_prefrontal,
            "active_prefrontal": active_prefrontal,
            "passage_signals":   passage_signals,
            "occluded_front":    occluded,
            "ts_stage":          ts_stage,
            "latest_metar":      latest.get("rawOb", ""),
            "flight_category":   latest.get("flightCategory", ""),
            "visibility_sm":     latest.get("visib", 0),
            "wind":              f"{latest.get('wdir','?')}°@{latest.get('wspd','?')}kt",
            "altimeter":         latest.get("altim"),
            "temp_c":            latest.get("temp"),
            "checked_at":        datetime.now().strftime("%H:%M:%S"),
        }

    def _issue_alert(self, level: str, airport: str, report: dict):
        """Issue alert if level changed or escalated."""
        level_rank = {"NONE":0,"WATCH":1,"WARNING":2,"URGENT":3}

        # Only alert if level is new or escalating
        if level_rank.get(level,0) <= level_rank.get(self._last_level,0):
            log.info(f"Cold front {level} at {airport} — "
                     f"same/lower than last alert ({self._last_level}), suppressing")
            return

        self._last_level = level

        emoji = {"WATCH":"🌬️","WARNING":"⚠️","URGENT":"🆘"}.get(level,"⚠️")
        colour = {"WATCH":"yellow","WARNING":"orange","URGENT":"red"}.get(level,"orange")

        # Build pre-frontal signal list
        pf_lines = []
        for name, sig in report["active_prefrontal"].items():
            label = name.replace("_"," ").title()
            pf_lines.append(f"  • {label}: {sig['detail']}")

        passage_lines = []
        for sig in report["passage_signals"]:
            passage_lines.append(f"  🔴 {sig['signal']}: {sig['detail']}")

        if level == "URGENT":
            action = "LAND IMMEDIATELY — frontal passage in progress."
        elif level == "WARNING":
            action = "Consider RTH now. Front likely within 1-3 hours. Do not launch."
        else:
            action = "Monitor closely. Front possible within 3-6 hours."

        # Include occluded front info if present
        occluded = report.get("occluded_front", {})
        # Thunderstorm stage section for SMS
        ts_stage_data = report.get("ts_stage", {})
        ts_stage_section = ""
        if ts_stage_data.get("stage","NONE") != "NONE":
            ts_stage_section = (
                f"\n\n⛈️ STORM STAGE: {ts_stage_data['stage']}\n"
                f"  {ts_stage_data['detail']}\n"
                f"  Action: {ts_stage_data['action']}"
            )

        occ_section = ""
        if occluded.get("detected"):
            occ_section = (
                f"\n\n⚠️ OCCLUDED FRONT SIGNALS ({occluded['n_signals']}):\n"
                + "\n".join(f"  • {s}" for s in occluded.get("signals",[])[:3])
                + ("\n  🔴 EMBEDDED THUNDERSTORM IN PRECIPITATION" if occluded.get("embedded_ts") else "")
            )

        front_type = "OCCLUDED FRONT" if occluded.get("detected") else "COLD FRONT"
        sms = (
            f"{emoji} {front_type} {level} — Safety1271\n"
            f"Airport: {airport} ({report['flight_category']})\n"
            f"Wind: {report['wind']}\n"
            f"Vis: {report['visibility_sm']} SM\n"
            f"Alt: {report['altimeter']} inHg\n\n"
            f"Signals detected ({report['prefrontal_count']} pre-frontal):\n"
            + "\n".join(pf_lines or ["  • None"])
            + ("\n\nFrontal passage:\n" + "\n".join(passage_lines)
               if passage_lines else "")
            + ts_stage_section
            + occ_section
            + f"\n\nACTION: {action}\n"
            f"METAR: {report['latest_metar']}"
        )

        log.warning(f"COLD FRONT {level} at {airport}: "
                    f"{report['prefrontal_count']} pre-frontal signals, "
                    f"{len(report['passage_signals'])} passage signals")

        # Send SMS
        if self.alert_phone:
            try:
                from twilio_client import create_twilio_client, get_twilio_from_phone
                create_twilio_client().messages.create(
                    body  = sms,
                    from_ = get_twilio_from_phone(),
                    to    = self.alert_phone
                )
                log.info(f"Cold front {level} SMS sent to {self.alert_phone}")
            except Exception as e:
                log.error(f"Cold front SMS failed: {e}")

        # Send email for WARNING and above
        if self.alert_email and level in ("WARNING", "URGENT"):
            try:
                from sendgrid import SendGridAPIClient
                from sendgrid.helpers.mail import Mail, Content
                html = f"""
                <div style="font-family:monospace;max-width:600px">
                  <div style="background:{'#8b0000' if level=='URGENT' else '#cc5500'};
                       padding:12px 20px;color:#fff;font-size:18px;font-weight:700">
                    {emoji} COLD FRONT {level} — {airport}
                  </div>
                  <div style="padding:16px;background:#f9f9f9">
                    <table style="font-size:13px;width:100%;border-collapse:collapse">
                      <tr><td style="padding:4px 8px;font-weight:700">Flight category</td>
                          <td>{report['flight_category']}</td></tr>
                      <tr><td style="padding:4px 8px;font-weight:700">Wind</td>
                          <td>{report['wind']}</td></tr>
                      <tr><td style="padding:4px 8px;font-weight:700">Visibility</td>
                          <td>{report['visibility_sm']} SM</td></tr>
                      <tr><td style="padding:4px 8px;font-weight:700">Altimeter</td>
                          <td>{report['altimeter']} inHg</td></tr>
                      <tr><td style="padding:4px 8px;font-weight:700">Temperature</td>
                          <td>{report['temp_c']}°C</td></tr>
                    </table>
                    <h3 style="font-size:14px;margin:12px 0 6px">Pre-frontal signals</h3>
                    {''.join(f'<div style="margin:4px 0;padding:6px 10px;background:#fff3cd;border-radius:4px;font-size:12px">• {v["detail"]}</div>' for v in report['active_prefrontal'].values())}
                    {'<h3 style="font-size:14px;margin:12px 0 6px;color:#8b0000">Frontal passage signals</h3>' + ''.join(f'<div style="margin:4px 0;padding:6px 10px;background:#fde8e8;border-radius:4px;font-size:12px">🔴 {s["detail"]}</div>' for s in report['passage_signals']) if report['passage_signals'] else ''}
                    <div style="margin:16px 0;padding:12px;background:#{'fde8e8' if level=='URGENT' else 'fff3cd'};
                         border-radius:6px;font-size:13px;font-weight:700">
                      ACTION: {action}
                    </div>
                    <div style="font-size:11px;color:#888;font-family:monospace;
                         background:#f0f0f0;padding:8px;border-radius:4px">
                      {report['latest_metar']}
                    </div>
                    <p style="font-size:11px;color:#888;margin-top:12px">
                      Checked at {report['checked_at']} · Safety1271 Cold Front Detector
                    </p>
                  </div>
                </div>"""
                sg   = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
                mail = Mail(
                    from_email = "weather@safety1271.ai",
                    to_emails  = self.alert_email,
                    subject    = f"[{level}] Cold front approaching — {airport}"
                )
                mail.add_content(Content("text/html", html))
                sg.send(mail)
            except Exception as e:
                log.error(f"Cold front email failed: {e}")

        # Fire callback to DynamicMissionManager
        if self.on_alert:
            try:
                self.on_alert(level, f"Cold front {level}: {action}")
            except Exception as e:
                log.error(f"ColdFrontDetector callback error: {e}")

        # Store in history
        self._alert_history.append({
            "time":    report["checked_at"],
            "level":   level,
            "airport": airport,
            "signals": report["prefrontal_count"],
            "metar":   report["latest_metar"],
        })


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    icao = sys.argv[1] if len(sys.argv) > 1 else "KDCA"
    print(f"\nRunning cold front analysis for {icao}...\n")

    obs = fetch_metar_history(icao, hours=3)
    if not obs:
        print(f"No METAR data available for {icao}")
        sys.exit(1)

    print(f"Observations retrieved: {len(obs)}")
    for o in obs:
        print(f"  {o.get('obsTime','')}  {o.get('rawOb','')[:80]}")

    print("\n── Signal Analysis ─────────────────────────────────────────")
    signals = {
        "Wind backing":       detect_wind_backing(obs),
        "Pressure fall":      detect_pressure_fall(obs),
        "Temp rising":        detect_temp_rising(obs),
        "Dewpoint rising":    detect_dewpoint_rising(obs),
        "High cloud advance": detect_high_cloud_advance(obs),
    }
    for name, result in signals.items():
        status = "✅ DETECTED" if result["detected"] else "  not detected"
        print(f"  {status}  {name}: {result['detail']}")

    passage = detect_frontal_passage(obs)
    if passage:
        print(f"\n── FRONTAL PASSAGE SIGNALS ─────────────────────────────────")
        for sig in passage:
            print(f"  🔴 {sig['signal']}: {sig['detail']}")

    active = sum(1 for v in signals.values() if v["detected"])
    level  = ("URGENT" if passage else
              "WARNING" if active >= WARNING_THRESHOLD else
              "WATCH"   if active >= WATCH_THRESHOLD   else "NONE")
    print(f"\n── Alert level: {level} ({active} pre-frontal signals) ──")
