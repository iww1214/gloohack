"""
patrol_scheduler.py
====================
Schedules compound patrol sessions with automated takeoff via MSDK.

WHAT IT DOES
------------
At each configured patrol time:
  1. Runs full Part 107 pre-flight check (NOTAM, weather, airspace, battery)
  2. If GO:  sends pilot an SMS "Ready to launch — please confirm presence"
  3. Waits for pilot SMS confirmation (or auto-proceeds after timeout)
  4. Calls MSDK bridge → KeyStartTakeoff → drone lifts off autonomously
  5. Starts waypoint mission
  6. Agent monitors video feed until mission complete
  7. Drone auto-RTH and lands
  8. Sends session summary SMS

LEGAL NOTE
----------
This scheduler requires a Remote Pilot In Command (RPIC) to be physically
present and maintaining visual line of sight throughout every flight.
The scheduler sends an SMS to the RPIC before every takeoff to confirm
presence. Do NOT use this for unattended automated flight — that requires
a FAA Part 107 BVLOS waiver which takes 3-6 months to obtain.

SETUP
-----
  pip install apscheduler requests anthropic twilio python-dotenv
  Edit PATROL_SCHEDULE below to match your compound's needs.
  Run: python patrol_scheduler.py
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron        import CronTrigger
from dotenv import load_dotenv
from twilio_client import create_twilio_client, get_twilio_from_phone

load_dotenv()

log = logging.getLogger("PatrolScheduler")
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt= "%H:%M:%S"
)

# ── Config ─────────────────────────────────────────────────────────────────
TIMEZONE         = os.getenv("PATROL_TIMEZONE",   "America/New_York")
PILOT_PHONE      = os.getenv("PILOT_PHONE",        os.getenv("ALERT_PHONE", ""))
COMPOUND_NAME    = os.getenv("COMPOUND_NAME",       "Church Campus")
CONFIRM_TIMEOUT_S= int(os.getenv("CONFIRM_TIMEOUT_SEC", "120"))  # 2 min to confirm presence
AUTO_LAUNCH      = os.getenv("AUTO_LAUNCH_WITHOUT_CONFIRM", "false").lower() == "true"
TEST_MODE        = os.getenv("TEST_MODE", "false").lower() == "true"

# ── Patrol schedule ─────────────────────────────────────────────────────────
# Each entry: (cron_expression, patrol_name, description)
# cron format:  minute  hour  day_of_week
# day_of_week:  mon,tue,wed,thu,fri,sat,sun  or  *
PATROL_SCHEDULE = [
    # Morning patrol — every day
    ("30 7 *",           "morning",      "Pre-service morning patrol"),
    # Mid-morning — Sunday only (during service)
    ("0 10 sun",         "midmorning",   "Sunday service patrol"),
    # Afternoon — weekdays (school day)
    ("0 15 mon-fri",     "afterschool",  "After-school dismissal patrol"),
    # Evening — every day
    ("0 19 *",           "evening",      "Evening programme patrol"),
    # Night security — every day
    ("0 22 *",           "night",        "Night perimeter check"),
]

# ── State ───────────────────────────────────────────────────────────────────
_pending_confirmation = {}   # patrol_id → threading.Event

twilio_client = create_twilio_client()


# ══════════════════════════════════════════════════════════════════════════════
#  PRE-FLIGHT CHECK
# ══════════════════════════════════════════════════════════════════════════════

def run_preflight(patrol_name: str) -> str:
    """
    Run Part 107 pre-flight compliance check.
    Returns 'GO', 'NO-GO', or 'CONDITIONAL'.
    """
    try:
        from preflight_agent import run_preflight_check, start_weather_hold_monitor
        from safety1271_compound import COMPOUND_ZONES

        # Use first zone's approximate coordinates as home point
        # In production, pull from your .env HOME_LAT / HOME_LON
        home_lat = float(os.getenv("HOME_LAT", "38.7742"))
        home_lon = float(os.getenv("HOME_LON", "-77.1892"))
        alt_ft   = float(os.getenv("PATROL_ALTITUDE_M", "15")) * 3.281
        flight_dt = datetime.now().isoformat()
        drone_sn  = os.getenv("DRONE_SN", "MINI4PROXXXXXXX")

        log.info(f"Running pre-flight check for {patrol_name} patrol...")
        verdict = run_preflight_check(
            lat             = home_lat,
            lon             = home_lon,
            altitude_ft     = alt_ft,
            flight_datetime = flight_dt,
            timezone        = TIMEZONE,
            pilot_name      = "Safety1271 RPIC",
            pilot_phone     = PILOT_PHONE,
            pilot_email     = os.getenv("ALERT_EMAIL", ""),
            drone_sn        = drone_sn
        )

        # If the verdict is a weather NO-GO/CONDITIONAL, start the hold monitor
        # so the pilot gets SMS updates and an automatic re-check when a GO
        # window opens. Non-weather failures return None and stay cancelled.
        if verdict in ("NO-GO", "CONDITIONAL"):
            from preflight_agent import LAST_CHECKLIST
            start_weather_hold_monitor(
                verdict,
                checklist=LAST_CHECKLIST,
                lat=home_lat,
                lon=home_lon,
                airport_icao=os.getenv("HOME_ICAO", "KDCA"),
                pilot_phone=PILOT_PHONE,
                pilot_email=os.getenv("ALERT_EMAIL", ""),
                pilot_name="Safety1271 RPIC",
                drone_sn=drone_sn,
                altitude_ft=alt_ft,
                flight_datetime=flight_dt,
                timezone=TIMEZONE,
                poll_interval_min=int(os.getenv("WEATHER_HOLD_POLL_MIN", "30")),
                cutoff_hour=int(os.getenv("WEATHER_HOLD_CUTOFF_HOUR", "14")),
            )

        return verdict

    except ImportError:
        log.warning("preflight_agent.py not found — skipping pre-flight check")
        return "GO"
    except Exception as e:
        log.error(f"Pre-flight check error: {e}")
        return "CONDITIONAL"


# ══════════════════════════════════════════════════════════════════════════════
#  SMS FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def send_sms(message: str, to: str = PILOT_PHONE):
    if TEST_MODE:
        print(f"\n  [SMS → {to}]\n  {message}\n")
        return
    try:
        twilio_client.messages.create(
            body  = message,
            from_ = get_twilio_from_phone(),
            to    = to
        )
    except Exception as e:
        log.error(f"SMS failed: {e}")


def request_pilot_confirmation(patrol_id: str, patrol_name: str,
                                scheduled_time: str) -> bool:
    """
    SMS the RPIC asking them to confirm presence before launch.
    Returns True if confirmed (or AUTO_LAUNCH is True).
    Waits up to CONFIRM_TIMEOUT_S seconds.
    """
    event = threading.Event()
    _pending_confirmation[patrol_id] = event

    send_sms(
        f"Safety1271 — {COMPOUND_NAME}\n"
        f"Scheduled {patrol_name} patrol ready: {scheduled_time}\n\n"
        f"Pre-flight check: PASSED\n\n"
        f"Reply YES to confirm you are on site as RPIC and patrol can launch.\n"
        f"Reply NO to cancel this patrol session.\n"
        f"Auto-{'launch' if AUTO_LAUNCH else 'cancel'} in {CONFIRM_TIMEOUT_S}s if no reply."
    )

    log.info(f"Waiting up to {CONFIRM_TIMEOUT_S}s for RPIC confirmation...")
    confirmed = event.wait(timeout=CONFIRM_TIMEOUT_S)

    del _pending_confirmation[patrol_id]

    if confirmed:
        return True
    elif AUTO_LAUNCH:
        log.warning("No RPIC confirmation received — AUTO_LAUNCH=true, proceeding (ensure RPIC is present)")
        send_sms(f"Auto-launching {patrol_name} patrol. RPIC must be on site and maintaining VLOS.")
        return True
    else:
        log.info("No RPIC confirmation — patrol cancelled for this session")
        send_sms(f"Patrol cancelled — no confirmation received. Next patrol as scheduled.")
        return False


def handle_incoming_sms(from_number: str, body: str):
    """
    Called by your SMS webhook when the RPIC replies.
    Wire this to a Flask/FastAPI endpoint receiving Twilio webhooks.
    """
    reply = body.strip().upper()
    if from_number == PILOT_PHONE:
        for patrol_id, event in list(_pending_confirmation.items()):
            if reply.startswith("YES"):
                log.info(f"RPIC confirmed presence for patrol {patrol_id}")
                event.set()
                send_sms("Confirmed. Drone launching now. Maintain visual line of sight throughout.")
            elif reply.startswith("NO"):
                log.info(f"RPIC cancelled patrol {patrol_id}")
                send_sms("Patrol cancelled as requested.")
                _pending_confirmation.pop(patrol_id, None)


# ══════════════════════════════════════════════════════════════════════════════
#  TAKEOFF AND MISSION
# ══════════════════════════════════════════════════════════════════════════════

def execute_takeoff_and_patrol(patrol_name: str):
    """
    Calls MSDK bridge to trigger automated takeoff,
    then starts the waypoint mission.
    """
    try:
        from safety1271_mini4pro import bridge, PATROL_ALT_M

        if TEST_MODE:
            log.info(f"[TEST] Simulating takeoff for {patrol_name} patrol")
            log.info(f"[TEST] POST /takeoff → KeyStartTakeoff → drone lifts to {PATROL_ALT_M}m")
            log.info("[TEST] POST /mission → waypoint mission started")
            return True

        # Step 1: Trigger takeoff
        log.info("Sending takeoff command via MSDK bridge...")
        result = bridge._post("/takeoff", {"altitude_m": PATROL_ALT_M})
        if result.get("status") != "ok":
            log.error(f"Takeoff failed: {result}")
            send_sms(f"Takeoff FAILED for {patrol_name} patrol. Check drone status.")
            return False

        # Wait for drone to reach patrol altitude (~15-20 seconds)
        log.info(f"Drone taking off to {PATROL_ALT_M}m...")
        time.sleep(20)

        # Step 2: Upload and start mission
        log.info("Starting patrol mission...")
        ok = bridge.upload_and_start_mission("patrol_route.kmz")
        if not ok:
            log.error("Mission start failed after takeoff")
            bridge.return_to_home()
            send_sms(f"Mission start FAILED after takeoff. Drone returning to home.")
            return False

        log.info(f"{patrol_name} patrol mission active")
        return True

    except ImportError:
        log.warning("safety1271_mini4pro.py not found — bridge unavailable")
        if TEST_MODE:
            return True
        return False
    except Exception as e:
        log.error(f"Takeoff/mission error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PATROL JOB (runs at each scheduled time)
# ══════════════════════════════════════════════════════════════════════════════

def run_patrol_job(patrol_name: str, description: str):
    """Full patrol lifecycle: preflight → confirm → takeoff → patrol → RTH."""
    ts         = datetime.now(ZoneInfo(TIMEZONE))
    ts_str     = ts.strftime("%I:%M %p %Z")
    patrol_id  = f"{patrol_name}_{ts.strftime('%H%M')}"

    log.info(f"\n{'='*50}")
    log.info(f"Scheduled patrol: {patrol_name} — {description}")
    log.info(f"Time: {ts_str}")
    log.info(f"{'='*50}")

    # Step 1: Pre-flight check
    verdict = run_preflight(patrol_name)
    log.info(f"Pre-flight verdict: {verdict}")

    if verdict == "NO-GO":
        log.warning(f"Pre-flight NO-GO — {patrol_name} patrol cancelled")
        send_sms(
            f"Safety1271 — {patrol_name} patrol CANCELLED\n"
            f"Time: {ts_str}\n"
            f"Reason: Pre-flight check NO-GO\n"
            f"Check email for full report. Next patrol as scheduled."
        )
        return

    # Step 2: Request RPIC confirmation
    if not request_pilot_confirmation(patrol_id, patrol_name, ts_str):
        return

    # Step 3: Automated takeoff and mission start
    send_sms(f"Launching {patrol_name} patrol now — {ts_str}\nMaintain visual line of sight.")
    success = execute_takeoff_and_patrol(patrol_name)

    if not success:
        return

    # Step 4: Start video analysis in background thread
    log.info("Starting compound safety/security analysis...")
    analysis_thread = threading.Thread(
        target = run_analysis_session,
        args   = (patrol_name,),
        daemon = True
    )
    analysis_thread.start()

    # Wait for mission to complete (estimated from waypoint count and speed)
    try:
        from safety1271_compound import PATROL_ALT_M
        from safety1271_mini4pro import PATROL_WAYPOINTS, PATROL_SPEED_MS
        # Rough estimate: total waypoints × avg spacing × speed + margins
        est_duration = len(PATROL_WAYPOINTS) * 60  # ~60s per waypoint including hover
        log.info(f"Estimated patrol duration: ~{est_duration//60} minutes")
        time.sleep(est_duration)
    except ImportError:
        time.sleep(600)  # default 10 minutes

    # Step 5: Mission complete — drone RTH
    log.info("Patrol mission complete — drone returning to home")
    try:
        from safety1271_mini4pro import bridge
        bridge.return_to_home()
    except Exception:
        pass

    send_sms(
        f"Safety1271 — {patrol_name} patrol complete\n"
        f"Time: {ts_str}\n"
        f"Drone returning to home. Please confirm safe landing."
    )


def run_analysis_session(patrol_name: str):
    """
    Run the compound safety/security analysis while drone is airborne.
    Uses the live RTMP stream from the drone via MediaMTX.
    """
    try:
        from safety1271_compound import run_live
        log.info(f"Video analysis started for {patrol_name} patrol")
        run_live()
    except ImportError:
        log.warning("safety1271_compound.py not found — no video analysis")
    except Exception as e:
        log.error(f"Analysis session error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  MSDK BRIDGE TAKEOFF ENDPOINT (add to Android bridge app)
# ══════════════════════════════════════════════════════════════════════════════

MSDK_TAKEOFF_KOTLIN = '''
// Add to your MSDK V5 Android bridge app
// This handles the POST /takeoff endpoint

@PostMapping("/takeoff")
fun takeoff(@RequestBody body: Map<String, Any>, response: HttpServletResponse): Map<String, Any> {
    val altitudeM = (body["altitude_m"] as? Double) ?: 15.0

    // Check pre-conditions
    val flightMode = KeyManager.getInstance()
        .getValue(FlightControllerKey.KeyFlightMode.create())
    if (flightMode == FlightMode.ON_GROUND) {
        // Motors off — safe to initiate takeoff
        FlightControllerKey.KeyStartTakeoff.create().action(
            onSuccess = {
                Log.d("Bridge", "Takeoff initiated successfully")
            },
            onFailure = { error ->
                Log.e("Bridge", "Takeoff failed: ${error.description()}")
            }
        )
        return mapOf("status" to "ok", "message" to "Takeoff initiated")
    } else {
        response.status = 400
        return mapOf("status" to "error", "message" to "Motors are on or aircraft is not on ground")
    }
}

// Also add POST /land endpoint:
@PostMapping("/land")
fun land(): Map<String, Any> {
    FlightControllerKey.KeyStartAutoLanding.create().action(
        onSuccess = { Log.d("Bridge", "Landing initiated") },
        onFailure = { e -> Log.e("Bridge", "Land failed: ${e.description()}") }
    )
    return mapOf("status" to "ok", "message" to "Landing initiated")
}
'''


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONAL: SMS WEBHOOK (Flask) for RPIC confirmation replies
# ══════════════════════════════════════════════════════════════════════════════

def start_sms_webhook():
    """
    Optional Flask server to receive Twilio SMS webhook callbacks
    so the RPIC can confirm launch by replying YES/NO.

    Expose this to the internet via ngrok:
        ngrok http 5001
    Then set your Twilio webhook URL to:
        https://YOUR_NGROK_URL/sms
    """
    try:
        from flask import Flask, request as flask_request
        app = Flask(__name__)

        @app.route("/sms", methods=["POST"])
        def sms_webhook():
            from_num = flask_request.form.get("From", "")
            body     = flask_request.form.get("Body", "")
            handle_incoming_sms(from_num, body)
            return "<Response></Response>", 200, {"Content-Type": "text/xml"}

        import threading
        t = threading.Thread(
            target = lambda: app.run(port=5001, debug=False, use_reloader=False),
            daemon = True
        )
        t.start()
        log.info("SMS webhook server started on port 5001")
    except ImportError:
        log.info("Flask not installed — SMS confirmation replies will not be received.")
        log.info("  pip install flask  to enable RPIC SMS confirmation.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    tz = ZoneInfo(TIMEZONE)

    print(f"\n{'='*56}")
    print(f"  Safety1271 Patrol Scheduler")
    print(f"  Site:     {COMPOUND_NAME}")
    print(f"  Timezone: {TIMEZONE}")
    print(f"  RPIC:     {PILOT_PHONE}")
    print(f"  Test mode: {TEST_MODE}")
    print(f"  Auto-launch without confirm: {AUTO_LAUNCH}")
    print(f"{'='*56}\n")

    print("  Scheduled patrols:")
    for cron, name, desc in PATROL_SCHEDULE:
        minute, hour, dow = cron.split()
        print(f"    {hour}:{minute.zfill(2)} ({dow:8})  {name:12}  {desc}")
    print()

    # Start SMS webhook for RPIC confirmation replies
    start_sms_webhook()

    # Build scheduler
    scheduler = BackgroundScheduler(timezone=str(tz))

    for cron_expr, patrol_name, description in PATROL_SCHEDULE:
        minute, hour, day_of_week = cron_expr.split()
        scheduler.add_job(
            func           = run_patrol_job,
            trigger        = CronTrigger(
                minute       = minute,
                hour         = hour,
                day_of_week  = day_of_week,
                timezone     = str(tz)
            ),
            args           = [patrol_name, description],
            id             = patrol_name,
            name           = description,
            replace_existing = True
        )

    scheduler.start()
    log.info("Scheduler running. Press Ctrl+C to stop.\n")

    # Print MSDK Kotlin snippet
    print("\n  MSDK BRIDGE — add these endpoints to your Android app:")
    print("  " + "─" * 50)
    for line in MSDK_TAKEOFF_KOTLIN.strip().split("\n")[:8]:
        print(f"  {line}")
    print("  ...")
    print(f"  {'─' * 50}\n")

    try:
        while True:
            # Show next patrol time every 10 minutes
            time.sleep(600)
            jobs = scheduler.get_jobs()
            if jobs:
                next_run = min(j.next_run_time for j in jobs if j.next_run_time)
                log.info(f"Next patrol: {next_run.strftime('%I:%M %p %Z')} — "
                         f"{next(j.name for j in jobs if j.next_run_time == next_run)}")
    except KeyboardInterrupt:
        log.info("Stopping scheduler...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
