"""
Live airspace & weather conditions monitor for the Command Center.

Polls free government sources on a timer so the dashboard always shows what the aircraft
is flying in, not just the one-off preflight snapshot:
  - METAR observations (aviationweather.gov) from airports near the site, with 6 h history
  - NWS hourly forecast and active NWS alerts (api.weather.gov)

The assessment is advisory. The preflight verdict remains the go/no-go gate.
"""

import os, math, time, logging, threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import location_service

load_dotenv()

log = logging.getLogger("Conditions")

POLL_S = max(60, int(os.getenv("CONDITIONS_POLL_SEC", "900")))
STATIONS = [s.strip().upper() for s in os.getenv("CONDITIONS_STATIONS", "").split(",") if s.strip()]
SEARCH_DEG = 0.35            # about 25 miles around the site
HISTORY_HOURS = 6
MOVED_MI = 5.0               # refresh early if the operating location moves this far

# Limits mirror dynamic_mission_manager.py (Mini 4 Pro: 38 km/h = 23.6 mph)
WIND_WARN_MPH = 22.0
WIND_LIMIT_MPH = 23.6
VIS_MIN_SM = 3.0
KT_TO_MPH = 1.15078
HPA_TO_INHG = 0.02953
HEADERS = {"User-Agent": "Safety1271/1.0 (conditions monitor)"}

_snapshot = {"updated_at": None, "errors": ["Not fetched yet"]}
_lock = threading.Lock()
_stop = threading.Event()


def _distance_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _vis_sm(value):
    if value is None:
        return None
    try:
        return float(str(value).replace("+", ""))
    except ValueError:
        return None


def _ceiling_ft(clouds):
    bases = [c.get("base") for c in clouds or [] if c.get("cover") in ("BKN", "OVC", "VV") and c.get("base") is not None]
    return min(bases) if bases else None


def parse_metar(m: dict) -> dict:
    wind_kt, gust_kt = m.get("wspd"), m.get("wgst")
    return {
        "obs_time": datetime.fromtimestamp(m["obsTime"], timezone.utc).isoformat() if m.get("obsTime") else None,
        "wind_dir": m.get("wdir"),
        "wind_kt": wind_kt,
        "wind_mph": round(wind_kt * KT_TO_MPH, 1) if wind_kt is not None else None,
        "gust_mph": round(gust_kt * KT_TO_MPH, 1) if gust_kt is not None else None,
        "vis_sm": _vis_sm(m.get("visib")),
        "ceiling_ft": _ceiling_ft(m.get("clouds")),
        "flight_category": m.get("fltCat"),
        "temp_c": m.get("temp"),
        "dewp_c": m.get("dewp"),
        "altimeter_inhg": round(m["altim"] * HPA_TO_INHG, 2) if m.get("altim") is not None else None,
        "wx": m.get("wxString") or "",
        "raw": m.get("rawOb", ""),
    }


def fetch_metars(lat: float, lon: float) -> dict:
    """Return {station_id: {"meta": {...}, "obs": [parsed observations, oldest first]}}."""
    params = {"format": "json", "hours": HISTORY_HOURS}
    if STATIONS:
        params["ids"] = ",".join(STATIONS)
    else:
        params["bbox"] = f"{lat - SEARCH_DEG},{lon - SEARCH_DEG},{lat + SEARCH_DEG},{lon + SEARCH_DEG}"
    resp = requests.get("https://aviationweather.gov/api/data/metar", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    stations = {}
    for m in resp.json():
        sid = m.get("icaoId")
        if not sid or m.get("lat") is None:
            continue
        entry = stations.setdefault(sid, {"meta": {
            "id": sid, "name": m.get("name", sid),
            "distance_mi": round(_distance_mi(lat, lon, m["lat"], m["lon"]), 1)}, "obs": []})
        entry["obs"].append(parse_metar(m))
    for entry in stations.values():
        entry["obs"].sort(key=lambda o: o["obs_time"] or "")
    return stations


def fetch_alerts(lat: float, lon: float) -> list:
    resp = requests.get("https://api.weather.gov/alerts/active", params={"point": f"{lat},{lon}"},
                        headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return [{"event": p.get("event"), "severity": p.get("severity"), "headline": p.get("headline"),
             "expires": p.get("expires")} for p in (f.get("properties", {}) for f in resp.json().get("features", []))]


def _parse_mph(text):
    import re
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    return max(nums) if nums else None


def fetch_forecast(lat: float, lon: float, hours: int = 8) -> list:
    point = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=HEADERS, timeout=15)
    point.raise_for_status()
    hourly = requests.get(point.json()["properties"]["forecastHourly"], headers=HEADERS, timeout=15)
    hourly.raise_for_status()
    return [{
        "start": p.get("startTime"),
        "wind_mph": _parse_mph(p.get("windSpeed")),
        "wind_dir": p.get("windDirection"),
        "precip_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
        "temp_f": p.get("temperature"),
        "summary": p.get("shortForecast"),
    } for p in hourly.json()["properties"]["periods"][:hours]]


def trend_of(history: list) -> str:
    """Compare the newest effective wind to the average of the observations 30+ minutes older."""
    pts = [max(h["wind_mph"] or 0, h["gust_mph"] or 0) for h in history if h["wind_mph"] is not None]
    if len(pts) < 3:
        return "unknown"
    older = pts[:-1][-3:]
    delta = pts[-1] - sum(older) / len(older)
    return "rising" if delta >= 3 else "falling" if delta <= -3 else "steady"


def density_altitude_ft(elevation_ft, altimeter_inhg, temp_c):
    """Standard approximation: pressure altitude corrected for temperature deviation from ISA."""
    if None in (elevation_ft, altimeter_inhg, temp_c):
        return None
    pressure_alt = elevation_ft + (29.92 - altimeter_inhg) * 1000
    isa_c = 15 - 2 * (pressure_alt / 1000)
    return round(pressure_alt + 120 * (temp_c - isa_c))


# Where each advisory comes from. Study guide and supplement page numbers are PDF page numbers.
SG = "Remote Pilot Study Guide"
SUP = "Test Supplement"
CITE = {
    "wind":     f"{SG} pp. 31-32 (wind and terrain effects); limit from aircraft specification",
    "gust":     f"{SG} pp. 31-34 (turbulence and gusts)",
    "vis":      "14 CFR §107.51(c): minimum 3 statute miles (Part 107 text in the knowledge base)",
    "ceiling":  f"14 CFR §107.51(d): stay 500 ft below clouds; {SG} pp. 25-26 (ceiling)",
    "ts":       f"{SG} pp. 33-35 (thunderstorms); microbursts p. 32",
    "precip":   "Aircraft specification (not water-resistant); " + f"{SG} pp. 33-35",
    "fog":      f"{SG} pp. 32-34, 36 (fog, mist and temperature inversions)",
    "icing":    f"{SG} pp. 34-35 (structural icing)",
    "da":       f"{SG} pp. 29-30 (density altitude); {SUP} p. 42 (Figure 8, density altitude chart)",
    "metar":    f"{SG} pp. 23-25; {SUP} p. 46 (Figure 12, METAR)",
    "taf":      f"{SG} pp. 26-27; {SUP} p. 49 (Figure 15, TAF)",
    "nws":      "National Weather Service alert",
    "manual":   "Operating location was set by hand, not by the aircraft",
}


def assess(current: dict, alerts: list, forecast: list, elevation_ft=None) -> dict:
    """Advisory status plus reasons; each reason names its source so the pilot can check it."""
    reasons, level = [], 0     # 0 OK, 1 CAUTION, 2 HOLD

    def raise_to(lvl, text, cite):
        nonlocal level
        level = max(level, lvl)
        reasons.append({"level": ("info", "caution", "hold")[lvl], "text": text, "source": CITE[cite]})

    wind, gust = current.get("wind_mph") or 0, current.get("gust_mph") or 0
    effective = max(wind, gust)
    if effective >= WIND_LIMIT_MPH:
        raise_to(2, f"Wind/gusts {effective:.0f} mph at or above the aircraft limit ({WIND_LIMIT_MPH} mph)", "wind")
    elif effective >= WIND_WARN_MPH:
        raise_to(1, f"Wind/gusts {effective:.0f} mph near the aircraft limit ({WIND_LIMIT_MPH} mph)", "wind")
    if gust and gust - wind >= 10:
        raise_to(1, f"Gusty: {wind:.0f} mph gusting {gust:.0f}; expect turbulence and an unsteady aircraft", "gust")

    vis = current.get("vis_sm")
    if vis is not None and vis < VIS_MIN_SM:
        raise_to(2, f"Visibility {vis} SM is below the {VIS_MIN_SM:.0f} SM minimum", "vis")
    elif vis is not None and vis < 5:
        raise_to(1, f"Visibility {vis} SM is close to the {VIS_MIN_SM:.0f} SM minimum", "vis")
    ceiling = current.get("ceiling_ft")
    if ceiling is not None and ceiling < 1000:
        raise_to(1, f"Ceiling {ceiling} ft; keep the aircraft 500 ft below cloud", "ceiling")

    wx = current.get("wx", "")
    if "TS" in wx:
        raise_to(2, "Thunderstorm reported; expect gusts, wind shear and microbursts", "ts")
    elif any(code in wx for code in ("RA", "SN", "DZ", "GR", "PL")):
        raise_to(2, "Precipitation reported; the aircraft is not water-resistant", "precip")
    if "FG" in wx or "BR" in wx:
        raise_to(1, "Fog or mist reported; visibility can drop quickly", "fog")
    temp, dewp = current.get("temp_c"), current.get("dewp_c")
    if temp is not None and dewp is not None and temp - dewp <= 2.2 and not ("FG" in wx or "BR" in wx):
        raise_to(1, f"Temperature and dew point within {temp - dewp:.1f} °C; fog or low cloud can form", "fog")
    if temp is not None and temp <= 2 and (current.get("wx") or (dewp is not None and temp - dewp <= 2.2)):
        raise_to(1, "Near freezing with moisture; icing risk", "icing")
    if current.get("flight_category") in ("IFR", "LIFR"):
        raise_to(1, f"Flight category {current['flight_category']}", "metar")

    da = density_altitude_ft(elevation_ft, current.get("altimeter_inhg"), temp)
    if da is not None and da >= 5000:
        raise_to(1, f"Density altitude about {da:,} ft (field {elevation_ft:,} ft); expect reduced climb and shorter flight time", "da")

    for a in alerts:
        raise_to(2 if a.get("severity") in ("Severe", "Extreme") else 1, f"NWS alert: {a.get('event')}", "nws")

    upcoming = max((f.get("wind_mph") or 0 for f in forecast[:4]), default=0)
    if upcoming >= WIND_WARN_MPH and effective < WIND_WARN_MPH:
        raise_to(1, f"Forecast wind up to {upcoming} mph within 4 hours", "taf")
    rain = max((f.get("precip_pct") or 0 for f in forecast[:4]), default=0)
    if rain >= 40:
        raise_to(1, f"Rain chance up to {rain}% within 4 hours; the aircraft is not water-resistant", "precip")

    if not reasons:
        reasons.append({"level": "info", "text": "Within limits", "source": CITE["metar"]})
    return {"status": ("OK", "CAUTION", "HOLD")[level], "reasons": reasons, "density_altitude_ft": da}


def refresh() -> dict:
    loc = location_service.get_location()
    if loc["lat"] is None:
        snapshot = {"updated_at": None, "errors": ["Waiting for aircraft GPS"], "location": loc}
        with _lock:
            _snapshot.clear()
            _snapshot.update(snapshot)
        return snapshot
    lat, lon = loc["lat"], loc["lon"]
    errors, stations, alerts, forecast = [], {}, [], []
    for label, fn in (("METAR", fetch_metars), ("NWS alerts", fetch_alerts),
                      ("NWS forecast", fetch_forecast)):
        try:
            result = fn(lat, lon)
            if label == "METAR":
                stations = result
            elif label == "NWS alerts":
                alerts = result
            else:
                forecast = result
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}")
            log.warning("%s fetch failed: %s", label, e)

    nearest = min(stations.values(), key=lambda s: s["meta"]["distance_mi"]) if stations else None
    current = nearest["obs"][-1] if nearest and nearest["obs"] else {}
    history = [{"t": o["obs_time"], "wind_mph": o["wind_mph"], "gust_mph": o["gust_mph"]} for o in (nearest or {"obs": []})["obs"]]
    here = location_service.resolve()
    assessment = assess(current, alerts, forecast, here.get("elevation_ft")) if current else \
                 {"status": "UNKNOWN", "reasons": [{"level": "info", "text": "No observation available", "source": ""}]}
    if loc["mode"] == "manual":
        assessment["reasons"].insert(0, {"level": "caution", "text": "Location set manually; not from aircraft GPS", "source": CITE["manual"]})
    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "poll_seconds": POLL_S,
        "site": {"lat": lat, "lon": lon, "label": loc["label"], "source": loc["source"], "mode": loc["mode"]},
        "station": nearest["meta"] if nearest else None,
        "current": current,
        "history": history,
        "trend": trend_of(history),
        "nearby": sorted(({**s["meta"], "wind_mph": s["obs"][-1]["wind_mph"], "gust_mph": s["obs"][-1]["gust_mph"],
                           "flight_category": s["obs"][-1]["flight_category"]} for s in stations.values() if s["obs"]),
                         key=lambda s: s["distance_mi"])[:5],
        "forecast": forecast,
        "alerts": alerts,
        "assessment": assessment,
        "elevation_ft": here.get("elevation_ft"),
        "airport": here.get("airport"),
        "errors": errors,
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(snapshot)
    return snapshot


def get_snapshot() -> dict:
    with _lock:
        return dict(_snapshot)


def _loop():
    last_at, last_pos = 0.0, None
    while not _stop.is_set():
        loc = location_service.get_location()
        moved = (last_pos is not None and loc["lat"] is not None and
                 _distance_mi(last_pos[0], last_pos[1], loc["lat"], loc["lon"]) >= MOVED_MI)
        no_data = last_pos is None and loc["lat"] is not None
        if moved or no_data or time.time() - last_at >= POLL_S:
            try:
                refresh()
                last_at = time.time()
                last_pos = (loc["lat"], loc["lon"]) if loc["lat"] is not None else None
            except Exception:
                log.exception("conditions refresh failed")
                last_at = time.time()
        _stop.wait(30)


def start():
    threading.Thread(target=_loop, name="conditions-monitor", daemon=True).start()
