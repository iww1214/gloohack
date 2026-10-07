"""
Parking team view: a read-only page with the latest empty-space count, for phones on the same network.

Runs separately from the Command Center on purpose. It shares only the parking numbers; none of the
Command Center's controls, camera feed, alerts or location are reachable from here.

  python parking_server.py                 # http://<this-computer>:8093/
  PARKING_ACCESS_CODE=abc123 python parking_server.py    # require ?key=abc123
"""

import os, hmac, socket, json
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

DASHBOARD = os.getenv("DASHBOARD_URL", "http://127.0.0.1:8092").rstrip("/")
PORT = int(os.getenv("PARKING_PORT", "8093"))
ACCESS_CODE = os.getenv("PARKING_ACCESS_CODE", "")
SITE_NAME = os.getenv("COMPOUND_NAME", "Gloo Church & School Campus")

app = FastAPI(title="Parking team view", docs_url=None, redoc_url=None, openapi_url=None)


def _check(request: Request):
    if ACCESS_CODE and not hmac.compare_digest(request.query_params.get("key", ""), ACCESS_CODE):
        raise HTTPException(status_code=403, detail="Access code required")


@app.get("/api/parking")
def parking(request: Request):
    _check(request)
    try:
        with urlopen(f"{DASHBOARD}/api/parking", timeout=3) as r:
            data = json.load(r)
    except Exception:
        return JSONResponse({"latest": None, "history": [], "unavailable": True})
    # Only the numbers the team needs
    keep = ("occupied", "empty", "total_seen", "coverage", "confidence", "empty_areas", "areas", "orientation", "counted_at", "simulated")
    latest = data.get("latest")
    return JSONResponse({
        "latest": {k: latest.get(k) for k in keep} if latest else None,
        "history": [{"empty": h["empty"], "counted_at": h["counted_at"]} for h in data.get("history", [])[:20]],
    })


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parking</title>
<style>
  :root{--bg:#0f1114;--card:#1b1e23;--text:#f2f2ee;--muted:#9a9a92;--green:#1d9e75;--amber:#ef9f27;--red:#e24b4a}
  body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,Segoe UI,Arial,sans-serif;text-align:center;padding:20px}
  h1{font-size:18px;font-weight:600;color:var(--muted);margin:8px 0 18px}
  .card{background:var(--card);border-radius:16px;padding:28px 16px;max-width:520px;margin:0 auto}
  .count{font-size:clamp(96px,28vw,180px);font-weight:700;line-height:1}
  .label{font-size:22px;margin-top:6px}
  .status{display:inline-block;margin-top:16px;padding:6px 16px;border-radius:20px;font-size:18px;font-weight:600}
  .go{background:rgba(29,158,117,.2);color:var(--green)} .mid{background:rgba(239,159,39,.2);color:var(--amber)}
  .stop{background:rgba(226,75,74,.2);color:var(--red)}
  .where{margin-top:16px;font-size:20px} .small{margin-top:14px;font-size:14px;color:var(--muted);line-height:1.5}
  .areas{margin:18px auto 0;max-width:420px;text-align:left;padding:0;list-style:none}
  .areas li{display:flex;justify-content:space-between;gap:12px;padding:10px 4px;border-top:1px solid #2a2e35;font-size:20px}
  .areas li b{color:var(--green);min-width:2ch;text-align:right}
  .areas-title{margin-top:20px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
  .banner{max-width:520px;margin:0 auto 12px;padding:8px;border:1px solid var(--amber);color:var(--amber);border-radius:8px;font-size:14px}
</style></head><body>
<h1 id="site"></h1>
<div id="banner" class="banner" hidden></div>
<div class="card" role="status" aria-live="polite">
  <div id="count" class="count">&ndash;</div>
  <div id="label" class="label">Checking&hellip;</div>
  <div id="status" class="status" hidden></div>
  <div id="where" class="where"></div>
  <div id="areas-title" class="areas-title" hidden>Send drivers to</div>
  <ul id="areas" class="areas"></ul>
  <div id="detail" class="small"></div>
</div>
<div class="small">Counted from the drone camera, so it is an estimate. Please follow the parking team's directions.</div>
<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
document.getElementById('site').textContent = __SITE__;
const $ = id => document.getElementById(id);
const hm = d => new Date(d).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit', hour12: false});
function ago(d) { const m = Math.max(0, Math.round((Date.now() - new Date(d)) / 60000)); return m < 1 ? 'just now' : m + ' min ago'; }
async function tick() {
  try {
    const r = await fetch('/api/parking' + (KEY ? '?key=' + encodeURIComponent(KEY) : ''));
    if (!r.ok) throw new Error(r.status);
    const d = await r.json(), c = d.latest;
    if (!c) {
      $('count').textContent = '\u2013'; $('label').textContent = d.unavailable ? 'Not available right now' : 'No count yet';
      $('status').hidden = true; $('where').textContent = ''; $('detail').textContent = ''; $('banner').hidden = true; return;
    }
    $('count').textContent = c.empty;
    $('label').textContent = c.empty === 1 ? 'space free' : 'spaces free';
    const share = c.total_seen ? c.empty / c.total_seen : 0;
    const s = $('status'); s.hidden = false;
    if (c.empty === 0) { s.textContent = 'Car park full'; s.className = 'status stop'; }
    else if (share < 0.1 || c.empty < 5) { s.textContent = 'Almost full'; s.className = 'status mid'; }
    else if (share < 0.25) { s.textContent = 'Filling up'; s.className = 'status mid'; }
    else { s.textContent = 'Plenty of space'; s.className = 'status go'; }
    $('where').textContent = (c.areas && c.areas.length) ? '' : (c.empty_areas ? 'Look: ' + c.empty_areas : '');
    const list = $('areas'); list.textContent = '';
    (c.areas || []).forEach(a => {
      const li = document.createElement('li');
      const name = document.createElement('span'); name.textContent = a.name;
      const n = document.createElement('b'); n.textContent = a.count;
      li.append(name, n); list.append(li);
    });
    $('areas-title').hidden = !(c.areas && c.areas.length);
    const bits = ['Counted ' + hm(c.counted_at) + ' (' + ago(c.counted_at) + ')'];
    if (c.coverage === 'part_of_lot') bits.push('Only part of the car park was in view; other areas may have more spaces.');
    if (c.confidence === 'low') bits.push('Rough estimate.');
    if (c.orientation && c.orientation.startsWith('assumed')) bits.push('Directions are assumed (demo).');
    if (c.orientation === 'unknown' && c.areas && c.areas.length) bits.push('Compass direction unknown; areas are by position in the picture.');
    if (Date.now() - new Date(c.counted_at) > 20 * 60000) bits.push('This count is out of date.');
    $('detail').textContent = bits.join(' ');
    $('banner').hidden = !c.simulated; $('banner').textContent = 'DEMO: simulated footage, not the real car park';
  } catch (e) { $('label').textContent = 'Not available right now'; }
}
tick(); setInterval(tick, 10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def page(request: Request):
    _check(request)
    return PAGE.replace("__SITE__", json.dumps(SITE_NAME))


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "this-computer"


if __name__ == "__main__":
    suffix = f"/?key={ACCESS_CODE}" if ACCESS_CODE else "/"
    print(f"\n  Parking team view: http://{_lan_ip()}:{PORT}{suffix}")
    if not ACCESS_CODE:
        print("  No access code set: anyone on this network can open it. Set PARKING_ACCESS_CODE to require one.")
    print("  Windows may ask to allow Python through the firewall; allow it for private networks only.\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
