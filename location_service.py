"""
Operating location service: where is the aircraft, which airport matters there,
and which ATC feed to listen to.

The default, Auto, uses only the aircraft's own live GPS as reported by the phone app bridge,
so it starts every session from where the aircraft physically is. Nothing is stored, and no
internet-based or configured location is ever substituted: with no GPS fix the location is
unknown. The operator can set a location manually (airport code or lat/lon) for the current
session only; it is never saved, so a restart returns to Auto.

Airports come from OurAirports (public domain), cached on disk.
ATC feeds are listed by the operator in atc_feeds.json; nothing is scraped.
"""

import os, csv, json, math, time, logging, threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import airspace_service

load_dotenv()
log = logging.getLogger("Location")

BASE = Path(__file__).parent
BRIDGE_URL = os.getenv("MSDK_BRIDGE_URL", "http://172.20.10.2:8080").rstrip("/")
AIRPORTS_CSV = BASE / "airports_cache.csv"
FEEDS_FILE = BASE / "atc_feeds.json"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
FREQS_CSV = BASE / "airport_frequencies_cache.csv"
FREQS_URL = "https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv"
FREQ_ORDER = ["ATIS", "D-ATIS", "CLD", "CLNC", "GND", "TWR", "CTAF", "UNIC", "APP", "DEP", "A/D", "ARR"]
HEADERS = {"User-Agent": "Safety1271/1.0 (location service)"}

_lock = threading.Lock()
_airports = {"rows": None}
_manual = {"value": None}   # in-memory only; resets to Auto on restart
_geo_cache = {}          # rounded lat/lon -> {"elevation_ft", "timezone"}
_resolve_cache = {"at": 0.0, "value": None}


def distance_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── Airports ────────────────────────────────────────────────────────────────

def _load_airports() -> list:
    with _lock:
        if _airports["rows"] is not None:
            return _airports["rows"]
        if not AIRPORTS_CSV.exists() or time.time() - AIRPORTS_CSV.stat().st_mtime > 30 * 86400:
            try:
                resp = requests.get(AIRPORTS_URL, headers=HEADERS, timeout=60)
                resp.raise_for_status()
                AIRPORTS_CSV.write_bytes(resp.content)
            except Exception as e:
                log.warning("Airport database download failed: %s", e)
        rows = []
        if AIRPORTS_CSV.exists():
            with AIRPORTS_CSV.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r["type"] not in ("large_airport", "medium_airport", "small_airport"):
                        continue
                    icao = (r.get("icao_code") or r.get("gps_code") or r["ident"] or "").upper()
                    if len(icao) != 4 or not r["latitude_deg"]:
                        continue
                    rows.append({
                        "icao": icao, "name": r["name"], "type": r["type"],
                        "lat": float(r["latitude_deg"]), "lon": float(r["longitude_deg"]),
                        "elevation_ft": float(r["elevation_ft"]) if r["elevation_ft"] else None,
                        "city": r.get("municipality", ""), "scheduled": r.get("scheduled_service") == "yes",
                    })
        _airports["rows"] = rows
        return rows


_freqs = {"rows": None}


def frequencies_for(icao: str) -> list:
    """Published radio frequencies for an airport (OurAirports data), most useful first."""
    with _lock:
        if _freqs["rows"] is None:
            if not FREQS_CSV.exists() or time.time() - FREQS_CSV.stat().st_mtime > 30 * 86400:
                try:
                    resp = requests.get(FREQS_URL, headers=HEADERS, timeout=60)
                    resp.raise_for_status()
                    FREQS_CSV.write_bytes(resp.content)
                except Exception as e:
                    log.warning("Frequency database download failed: %s", e)
            rows = {}
            if FREQS_CSV.exists():
                with FREQS_CSV.open(encoding="utf-8", newline="") as f:
                    for r in csv.DictReader(f):
                        try:
                            mhz = float(r["frequency_mhz"])
                        except (KeyError, ValueError):
                            continue
                        rows.setdefault(r["airport_ident"].upper(), []).append(
                            {"type": r["type"].upper(), "description": r["description"], "mhz": mhz})
            _freqs["rows"] = rows
    found = _freqs["rows"].get(icao.upper(), [])
    rank = {t: i for i, t in enumerate(FREQ_ORDER)}
    return sorted(found, key=lambda x: (rank.get(x["type"], 99), x["mhz"]))


def find_airport(icao: str):
    icao = icao.strip().upper()
    if len(icao) == 3 and icao.isalpha():
        icao = "K" + icao          # US three-letter codes
    return next((a for a in _load_airports() if a["icao"] == icao), None)


def airports_near(lat: float, lon: float, max_mi: float) -> list:
    out = []
    for a in _load_airports():
        d = distance_mi(lat, lon, a["lat"], a["lon"])
        if d <= max_mi:
            out.append({**a, "distance_mi": round(d, 1)})
    return sorted(out, key=lambda a: a["distance_mi"])


def busiest_nearby(lat: float, lon: float):
    """Nearest large scheduled-service airport within 40 mi, then medium, then wider; else nearest of any."""
    near = airports_near(lat, lon, 100)
    for kind, radius in (("large_airport", 40), ("medium_airport", 40), ("large_airport", 100), ("medium_airport", 100)):
        match = next((a for a in near if a["type"] == kind and a["scheduled"] and a["distance_mi"] <= radius), None)
        if match:
            return match
    return near[0] if near else None


# ── Position ────────────────────────────────────────────────────────────────

_last_fix = {"value": None}   # newest aircraft fix, kept until the aircraft reports a new one


def _aircraft_fix():
    """Live GPS fix from the bridge; holds the last good one when the aircraft loses GPS."""
    try:
        status = requests.get(f"{BRIDGE_URL}/status", timeout=2).json()
        lat, lon = status.get("latitude_deg"), status.get("longitude_deg")
        if status.get("aircraft_connected") and isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
                and (abs(lat) > 0.001 or abs(lon) > 0.001):
            fix = {"lat": lat, "lon": lon, "at": time.time()}
            _last_fix["value"] = fix
            return fix
    except Exception:
        pass
    return _last_fix["value"]


def set_manual(lat: float, lon: float, label: str):
    """Operator-chosen location. Held in memory only, so every restart goes back to Auto."""
    _manual["value"] = {"lat": lat, "lon": lon, "label": label}
    _resolve_cache["value"] = None


def set_auto():
    _manual["value"] = None
    _resolve_cache["value"] = None


def get_location() -> dict:
    """Manual choice if the operator made one this session, otherwise the aircraft's live GPS."""
    manual = _manual["value"]
    if manual:
        return {**manual, "mode": "manual", "source": "manual", "label": f"Manual: {manual['label']}", "at": None}
    fix = _aircraft_fix()
    if fix:
        return {**fix, "mode": "auto", "source": "aircraft_gps", "label": "Aircraft GPS (auto)"}
    if _last_fix["value"]:
        return {**_last_fix["value"], "mode": "auto", "source": "aircraft_gps_last", "label": "Aircraft GPS (last fix)"}
    return {"lat": None, "lon": None, "mode": "auto", "source": "unknown",
            "label": "Auto: waiting for aircraft GPS", "at": None}


# ── Derived facts ───────────────────────────────────────────────────────────

def _geo_facts(lat: float, lon: float, airport) -> dict:
    key = (round(lat, 2), round(lon, 2))
    if key in _geo_cache:
        return _geo_cache[key]
    elevation, tz = None, None
    try:
        r = requests.get("https://epqs.nationalmap.gov/v1/json", params={
            "x": lon, "y": lat, "wkid": 4326, "units": "Feet", "includeDate": "false"}, headers=HEADERS, timeout=8)
        value = r.json().get("value")
        if isinstance(value, (int, float)) and value > -1000:
            elevation = round(value)
    except Exception:
        pass
    if elevation is None and airport and airport.get("elevation_ft") is not None:
        elevation = round(airport["elevation_ft"])
    try:
        r = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=HEADERS, timeout=8)
        tz = r.json()["properties"]["timeZone"]
    except Exception:
        pass
    facts = {"elevation_ft": elevation, "timezone": tz}
    if elevation is not None or tz:
        _geo_cache[key] = facts
    return facts


def load_feeds() -> dict:
    data = _read_json(FEEDS_FILE) or {}
    return {k.upper(): v for k, v in (data.get("feeds") or {}).items() if isinstance(v, list)}


def select_atc_feed(lat: float, lon: float, primary) -> dict:
    """Primary airport's feed if listed, else the next-best nearby airport that has one."""
    feeds = load_feeds()
    order = ([primary] if primary else []) + [a for a in airports_near(lat, lon, 60)
                                              if a["type"] != "small_airport" and (not primary or a["icao"] != primary["icao"])]
    for ap in order:
        listed = [f for f in feeds.get(ap["icao"], []) if f.get("url") or f.get("clips")]
        if listed:
            return {"airport": ap["icao"], "airport_name": ap["name"], "distance_mi": ap.get("distance_mi"),
                    "feed": listed[0], "is_primary": bool(primary and ap["icao"] == primary["icao"]), "reason": None}
    want = primary["icao"] if primary else "this area"
    return {"airport": primary["icao"] if primary else None, "feed": None, "is_primary": False,
            "reason": f"No LiveATC feed listed for {want}; add one to atc_feeds.json"}


def resolve(force: bool = False) -> dict:
    """Everything derived from the current position; cached briefly."""
    if not force and _resolve_cache["value"] and time.time() - _resolve_cache["at"] < 20:
        return _resolve_cache["value"]
    loc = get_location()
    result = {"location": loc, "airport": None, "elevation_ft": None, "timezone": None, "atc": None,
              "airspace": None,
              "resolved_at": datetime.now(timezone.utc).isoformat()}
    if loc["lat"] is not None:
        primary = busiest_nearby(loc["lat"], loc["lon"])
        if primary:
            primary = {**primary, "distance_mi": round(distance_mi(loc["lat"], loc["lon"], primary["lat"], primary["lon"]), 1)}
        facts = _geo_facts(loc["lat"], loc["lon"], primary)
        if primary:
            primary["frequencies"] = frequencies_for(primary["icao"])
        near = airports_near(loc["lat"], loc["lon"], 25)
        result.update({"airport": primary, **facts, "atc": select_atc_feed(loc["lat"], loc["lon"], primary),
                       "airspace": airspace_service.classify(loc["lat"], loc["lon"], near)})
    _resolve_cache.update({"at": time.time(), "value": result})
    return result
