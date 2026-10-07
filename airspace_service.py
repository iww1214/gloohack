"""
Live airspace for a position, from the FAA's public Class Airspace layer (no key).

Advisory only: confirm on a sectional chart or a LAANC app before flight. The position comes
from location_service (live aircraft GPS, else last fix, else the operator's manual setting).
"""

import math
import re
import threading
import time

import requests

FAA_CLASS_AIRSPACE = ("https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
                      "Class_Airspace/FeatureServer/0/query")
HEADERS = {"User-Agent": "Safety1271/1.0 (airspace lookup)"}
CACHE_S = 600

# DC Special Flight Rules Area: 30 NM around the DCA VOR.
SFRA_CENTER = (38.8512, -77.0402)
SFRA_RADIUS_NM = 30.0

NOTE = "Derived from the FAA Class Airspace layer — confirm on a sectional chart or LAANC app before flight."
_RANK = {"B": 0, "C": 1, "D": 2, "E": 3}
_cache = {}
_lock = threading.Lock()


def _nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) * 0.868976


def _fetch(lat, lon):
    key = (round(lat, 3), round(lon, 3))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    resp = requests.get(FAA_CLASS_AIRSPACE, params={
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "returnGeometry": "false", "f": "json",
    }, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError((data["error"] or {}).get("message", "FAA service error"))
    rows = [f["attributes"] for f in data.get("features", [])]
    with _lock:
        _cache[key] = (time.time(), rows)
    return rows


def _clean_name(name):
    return re.sub(r"\s+CLASS\s+[A-E]\d?$", "", name or "", flags=re.I).title()


def lookup(lat, lon):
    """Surface-based controlled airspace at the position: {"ok", "class", "name", "hours", "in_sfra"}."""
    try:
        rows = _fetch(lat, lon)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    surface = [r for r in rows if r.get("TYPE_CODE") == "CLASS" and r.get("CLASS") in _RANK
               and r.get("LOWER_VAL") == 0]
    in_sfra = _nm(lat, lon, *SFRA_CENTER) <= SFRA_RADIUS_NM
    if not surface:
        return {"ok": True, "class": "G", "name": None, "hours": None, "in_sfra": in_sfra}
    top = min(surface, key=lambda r: _RANK[r["CLASS"]])
    return {"ok": True, "class": top["CLASS"], "name": _clean_name(top.get("NAME")),
            "hours": (top.get("WKHR_RMK") or "").strip() or None, "in_sfra": in_sfra}



def classify(lat, lon, airports_nearby=None, now=None):
    """Airspace picture for the dashboard panel."""
    out = {"class": None, "label": None, "authorization": None, "detail": None, "advisory": True, "note": NOTE}
    a = lookup(lat, lon)
    if not a["ok"]:
        out.update({"class": "?", "label": "Airspace unknown — FAA lookup unavailable",
                    "authorization": "Verify on a sectional chart or LAANC app before flight.",
                    "detail": a["error"]})
        return out
    cls = a["class"]
    if cls == "G":
        near = airports_nearby[0] if airports_nearby else None
        label = "Class G — uncontrolled"
        if near:
            label += f" (nearest airport {near['icao']}, {near.get('distance_mi', '?')} mi)"
        auth = "No ATC authorization required below 400 ft AGL."
        detail = "No surface-based controlled airspace here."
    else:
        label = f"Class {cls} — {a['name']}"
        auth = "ATC authorization required before flight (LAANC or FAA DroneZone)."
        detail = f"Surface-based Class {cls} airspace."
        if a["hours"]:
            detail += f" Published hours: {a['hours']}."
    if a["in_sfra"]:
        detail += " Inside the DC SFRA (30 NM of DCA) — SFRA rules apply regardless of class."
        if cls == "G":
            auth = "DC SFRA: authorization required even in uncontrolled airspace within the SFRA."
    out.update({"class": cls, "label": label, "authorization": auth, "detail": detail})
    return out
