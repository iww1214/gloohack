"""
run_safety1271.py
=====================
Unified launcher — starts every Safety1271 agent in the correct order.

Starts:
  1. Pre-flight compliance check (Part 107 — runs once, blocks if NO-GO)
  2. Dynamic Mission Manager  (background thread — NOTAM/weather re-routing)
  3. ATC Listener             (background thread — live radio monitoring)
  4. Patrol Scheduler         (background thread — scheduled takeoffs)
  5. Compound patrol agent    (foreground — video analysis loop)

Usage:
  python run_safety1271.py                  # full live system
  python run_safety1271.py --demo           # demo mode, no drone
  python run_safety1271.py --skip-preflight # skip FAA check (dev only)
  python run_safety1271.py --no-scheduler   # manual takeoff only
  python run_safety1271.py --status         # show system status and exit
"""

import os, sys, time, logging, argparse, threading, signal
from datetime import datetime
try:
    from flight_log import get_patrol_logger
    _HAS_FLIGHT_LOG = True
except ImportError:
    _HAS_FLIGHT_LOG = False
from dotenv import load_dotenv

load_dotenv()

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt = "%H:%M:%S",
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(f"safety1271_{datetime.now().strftime('%Y%m%d')}.log")
    ]
)
log = logging.getLogger("Launcher")

# ── Config ───────────────────────────────────────────────────────────────────
COMPOUND_NAME  = os.getenv("COMPOUND_NAME",    "Church & School Campus")
TEST_MODE      = os.getenv("TEST_MODE","false").lower() == "true"
LIVEATC_URL    = os.getenv("LIVEATC_URL", "")
PILOT_PHONE    = os.getenv("PILOT_PHONE", os.getenv("ALERT_PHONE",""))

# ── Shared shutdown event ────────────────────────────────────────────────────
_shutdown = threading.Event()

def _handle_signal(sig, frame):
    log.info("Shutdown signal received — stopping all agents...")
    _shutdown.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT STARTERS
# ══════════════════════════════════════════════════════════════════════════════

def start_preflight(args) -> bool:
    """
    Run Part 107 pre-flight compliance check.
    Returns True if GO/CONDITIONAL.
    On weather-based NO-GO: starts WeatherHoldMonitor and blocks until
    conditions clear or cutoff time reached.
    """
    if args.skip_preflight or args.demo:
        log.info("Pre-flight check skipped.")
        return True
    try:
        from preflight_agent import (run_preflight_check,
                                      start_weather_hold_monitor)
        from safety1271_mini4pro import PATROL_WAYPOINTS, PATROL_ALT_M

        home     = PATROL_WAYPOINTS[0]
        lat      = home[0]
        lon      = home[1]
        alt_ft   = PATROL_ALT_M * 3.281
        tz       = os.getenv("PATROL_TIMEZONE", "America/New_York")
        p_phone  = PILOT_PHONE
        p_email  = os.getenv("ALERT_EMAIL","")
        drone_sn = os.getenv("DRONE_SN","MINI4PROXXXXXXX")

        log.info("Running Part 107 pre-flight compliance check...")
        verdict = run_preflight_check(
            lat             = lat,
            lon             = lon,
            altitude_ft     = alt_ft,
            flight_datetime = datetime.now().isoformat(),
            timezone        = tz,
            pilot_name      = "Safety1271 RPIC",
            pilot_phone     = p_phone,
            pilot_email     = p_email,
            drone_sn        = drone_sn,
        )
        log.info(f"Pre-flight verdict: {verdict}")

        if verdict in ("GO", "CONDITIONAL"):
            return True

        if verdict == "NO-GO" and not TEST_MODE:
            # Try automatic weather hold — only activates for weather failures
            monitor = start_weather_hold_monitor(
                verdict      = verdict,
                checklist    = [],
                lat          = lat,
                lon          = lon,
                airport_icao = "KDAA",
                pilot_phone  = p_phone,
                pilot_email  = p_email,
                pilot_name   = "Safety1271 RPIC",
                drone_sn     = drone_sn,
                altitude_ft  = alt_ft,
                flight_datetime = datetime.now().isoformat(),
                timezone     = tz,
            )
            if monitor:
                log.info("Weather hold monitor running — waiting for conditions to clear...")
                monitor._thread.join()   # blocks until monitor stops
                return False             # scheduler handles retry
            else:
                log.error("Pre-flight NO-GO (non-weather) — resolve and restart.")
                return False

        return True

    except ImportError:
        log.warning("preflight_agent.py not found — skipping.")
        return True
    except Exception as e:
        log.error(f"Pre-flight error: {e}")
        log.error("Pre-flight failed closed: live launch is blocked.")
        return False


def start_dynamic_manager():
    """Start NOTAM/weather re-routing manager in background."""
    try:
        from dynamic_mission_manager import DynamicMissionManager
        from safety1271_mini4pro  import bridge, PATROL_WAYPOINTS, PATROL_ALT_M
        home    = PATROL_WAYPOINTS[0]
        manager = DynamicMissionManager(
            original_waypoints = PATROL_WAYPOINTS,
            bridge             = bridge,
            patrol_alt_m       = PATROL_ALT_M,
            home_lat           = home[0],
            home_lon           = home[1],
        )
        manager.start()
        log.info("✅ Dynamic mission manager active (NOTAM/weather re-routing)")
        return manager
    except ImportError:
        log.warning("dynamic_mission_manager.py not found — static route only.")
        return None
    except Exception as e:
        log.warning(f"Dynamic manager failed to start: {e}")
        return None


def start_atc_listener(dynamic_manager=None):
    """Start live ATC monitoring in background thread."""
    if not LIVEATC_URL:
        log.info("LIVEATC_URL not set — ATC monitoring disabled.")
        log.info("  Set LIVEATC_URL in .env (https://www.liveatc.net/search/) to enable.")
        return None
    try:
        import queue
        from atc_listener_agent import LiveATCCapture, processing_pipeline, AlertDeduplicator

        atc_queue  = queue.Queue(maxsize=10)
        dedup      = AlertDeduplicator(cooldown_seconds=120)
        capture    = LiveATCCapture(LIVEATC_URL, atc_queue)
        capture.start()

        # Patch pipeline to forward TFR alerts to dynamic manager
        original_send = None
        try:
            import atc_listener_agent as atc_mod
            original_send = atc_mod.send_atc_alert
            def patched_send(alert):
                original_send(alert)
                if alert.get("category") == "TFR" and dynamic_manager:
                    dynamic_manager.trigger_immediate_recheck(
                        f"TFR announced on ATC: {alert.get('summary','')}"
                    )
            atc_mod.send_atc_alert = patched_send
        except Exception:
            pass

        t = threading.Thread(
            target = processing_pipeline,
            args   = (atc_queue, dedup),
            daemon = True,
            name   = "ATCListener"
        )
        t.start()
        log.info(f"✅ ATC listener active — stream: {LIVEATC_URL[:60]}")
        return capture
    except ImportError:
        log.warning("atc_listener_agent.py not found — ATC monitoring disabled.")
        return None
    except Exception as e:
        log.warning(f"ATC listener failed: {e}")
        return None


def start_scheduler(args):
    """Start patrol scheduler in background."""
    if args.no_scheduler or args.demo:
        log.info("Patrol scheduler disabled.")
        return None
    try:
        from patrol_scheduler import main as scheduler_main
        t = threading.Thread(
            target = scheduler_main,
            daemon = True,
            name   = "PatrolScheduler"
        )
        t.start()
        log.info("✅ Patrol scheduler active")
        return t
    except ImportError:
        log.warning("patrol_scheduler.py not found — manual takeoff only.")
        return None
    except Exception as e:
        log.warning(f"Scheduler failed: {e}")
        return None


def run_compound_agent(args):
    """Run the compound patrol agent (foreground)."""
    try:
        from safety1271_compound import run_demo, run_live
        if args.demo:
            run_demo()
        else:
            run_live()
    except ImportError:
        log.error("safety1271_compound.py not found.")
        sys.exit(1)
    except Exception as e:
        log.error(f"Compound agent error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  STATUS REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_status():
    """Print system status and file inventory."""
    files = {
        "preflight_agent.py":         "Part 107 pre-flight check",
        "atc_listener_agent.py":      "Live ATC radio monitoring",
        "dynamic_mission_manager.py": "NOTAM/weather route re-planning",
        "patrol_scheduler.py":        "Scheduled automated takeoff",
        "safety1271_mini4pro.py":  "Mini 4 Pro MSDK drone agent",
        "safety1271_compound.py":  "Compound safety/security vision",
        "safety1271_air3.py":      "Air 3 variant (no MSDK)",
        "drone_ops.py":               "Unified drone + ATC launcher",
    }
    print(f"\n{'═'*60}")
    print(f"  Safety1271 — System Status")
    print(f"  {COMPOUND_NAME}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*60}")
    print(f"\n  Agent files:")
    for fname, desc in files.items():
        exists = "✅" if os.path.exists(fname) else "❌"
        print(f"  {exists} {fname:<36} {desc}")

    print(f"\n  Environment:")
    checks = {
        "ANTHROPIC_API_KEY":    bool(os.getenv("ANTHROPIC_API_KEY")),
        "TWILIO_ACCOUNT_SID":   bool(os.getenv("TWILIO_ACCOUNT_SID")),
        "SENDGRID_API_KEY":     bool(os.getenv("SENDGRID_API_KEY")),
        "FAA_API_KEY":          bool(os.getenv("FAA_API_KEY")),
        "MSDK_BRIDGE_URL":      bool(os.getenv("MSDK_BRIDGE_URL")),
        "LIVEATC_URL":          bool(os.getenv("LIVEATC_URL")),
        "ALERT_PHONE":          bool(os.getenv("ALERT_PHONE")),
        "DRONE_SN":             bool(os.getenv("DRONE_SN")),
        "TEST_MODE":            True,
    }
    for k, v in checks.items():
        val = os.getenv(k, "")
        display = ("set" if v and k != "TEST_MODE" else
                   os.getenv("TEST_MODE","false") if k == "TEST_MODE" else "NOT SET")
        mark = "✅" if (v and k != "TEST_MODE") or (k == "TEST_MODE") else "⚠️ "
        print(f"  {mark} {k:<28} {display}")

    print(f"\n  Quick start:")
    print(f"    python run_safety1271.py --demo          # demo, no drone")
    print(f"    python run_safety1271.py --no-scheduler  # live, manual launch")
    print(f"    python run_safety1271.py                 # full scheduled system")
    print(f"{'═'*60}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  BANNER
# ══════════════════════════════════════════════════════════════════════════════

def print_banner(args):
    mode = "DEMO MODE" if args.demo else ("TEST MODE" if TEST_MODE else "LIVE MODE")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║         Safety1271 — Compound Safety & Security       ║
║         Gloo AI Hackathon 2026                           ║
╠══════════════════════════════════════════════════════════╣
║  Site    : {COMPOUND_NAME:<46}║
║  Mode    : {mode:<46}║
║  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<46}║
╠══════════════════════════════════════════════════════════╣
║  Agents  : Pre-flight · Dynamic re-routing · ATC listen  ║
║            Scheduler · Compound vision · MSDK bridge     ║
╚══════════════════════════════════════════════════════════╝
""")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Safety1271 Unified Launcher")
    parser.add_argument("--demo",          action="store_true",
                        help="Run demo mode — 12 scripted scenarios, no drone needed")
    parser.add_argument("--skip-preflight",action="store_true",
                        help="Skip Part 107 pre-flight check (dev/test only)")
    parser.add_argument("--no-scheduler",  action="store_true",
                        help="Disable patrol scheduler — manual takeoff only")
    parser.add_argument("--no-atc",        action="store_true",
                        help="Disable ATC listener")
    parser.add_argument("--no-dynamic",    action="store_true",
                        help="Disable dynamic NOTAM/weather re-routing")
    parser.add_argument("--status",        action="store_true",
                        help="Show system status and exit")
    parser.add_argument("--generate-kmz",  action="store_true",
                        help="Generate patrol_route.kmz and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.generate_kmz:
        try:
            from safety1271_mini4pro import generate_kmz
            generate_kmz()
        except ImportError:
            log.error("safety1271_mini4pro.py not found")
        return

    print_banner(args)

    # ── Step 1: Pre-flight ────────────────────────────────────────────────
    log.info("Step 1/5 — Pre-flight compliance check")
    if not start_preflight(args):
        sys.exit(1)

    # ── Step 2: Dynamic mission manager ──────────────────────────────────
    dynamic_manager = None
    if not args.no_dynamic and not args.demo:
        log.info("Step 2/5 — Starting dynamic mission manager")
        dynamic_manager = start_dynamic_manager()
    else:
        log.info("Step 2/5 — Dynamic mission manager disabled")

    # ── Step 3: ATC listener ─────────────────────────────────────────────
    atc_capture = None
    if not args.no_atc and not args.demo:
        log.info("Step 3/5 — Starting ATC listener")
        atc_capture = start_atc_listener(dynamic_manager)
    else:
        log.info("Step 3/5 — ATC listener disabled")

    # ── Step 4: Patrol scheduler ─────────────────────────────────────────
    log.info("Step 4/5 — Starting patrol scheduler")
    scheduler_thread = start_scheduler(args)

    # ── Step 5: Compound vision agent (foreground) ────────────────────────
    log.info("Step 5/5 — Starting compound patrol agent")
    log.info("Press Ctrl+C to stop all agents.\n")

    if not args.demo:
        # Brief wait to let background agents settle
        time.sleep(2)

    try:
        run_compound_agent(args)
    except SystemExit:
        pass
    finally:
        log.info("Shutting down all agents...")
        _shutdown.set()
        if atc_capture:
            try: atc_capture.stop()
            except Exception: pass
        if dynamic_manager:
            try: dynamic_manager.stop()
            except Exception: pass
        # ── Post-patrol report ────────────────────────────────────────────
        if not args.demo:
            try:
                from post_patrol_report import generate_report
                log.info("Generating post-patrol report...")
                rpt = generate_report(email=bool(os.getenv("ALERT_EMAIL")))
                log.info(f"Report saved: {rpt}")
            except ImportError:
                log.info("post_patrol_report.py not found — skipping report.")
            except Exception as e:
                log.warning(f"Report generation failed: {e}")
        log.info("Safety1271 stopped cleanly.")


if __name__ == "__main__":
    main()
