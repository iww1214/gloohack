"""
drone_ops.py — Unified Launcher
================================
Runs both agents together:
  1. Pre-flight check (once at startup)
  2. ATC listener (continuous, background)

Usage:
    python drone_ops.py \\
        --lat 38.7773 --lon -77.1868 \\
        --alt 200 \\
        --time "2026-07-08T19:30:00" \\
        --tz "America/New_York" \\
        --name "Innocent Wafula" \\
        --phone "+17035551234" \\
        --email "you@example.com" \\
        --sn "AIR3XXXXXXXXXXX" \\
        --atc-mode stream \\
        --atc-stream "https://www.liveatc.net/play/kdca.pls"
"""

import argparse
import threading
import queue
import sys
import os

# Import both agents
from preflight_agent  import run_preflight_check
from atc_listener_agent import (
    LiveATCCapture, SDRAudioCapture,
    processing_pipeline, AlertDeduplicator,
    ATC_FREQUENCIES, PILOT_PHONE, PILOT_EMAIL
)

def main():
    parser = argparse.ArgumentParser(description="DJI Air 3 Drone Ops Agent")

    # Flight parameters
    parser.add_argument("--lat",    type=float, required=True)
    parser.add_argument("--lon",    type=float, required=True)
    parser.add_argument("--alt",    type=float, default=200)
    parser.add_argument("--time",   type=str,   required=True)
    parser.add_argument("--tz",     type=str,   default="America/New_York")
    parser.add_argument("--name",   type=str,   default="Pilot")
    parser.add_argument("--phone",  type=str,   default=os.environ.get("PILOT_PHONE",""))
    parser.add_argument("--email",  type=str,   default=os.environ.get("PILOT_EMAIL",""))
    parser.add_argument("--sn",     type=str,   required=True, help="Drone serial number")

    # ATC listener options
    parser.add_argument("--atc-mode",   choices=["sdr","stream","off"], default="stream")
    parser.add_argument("--atc-freq",   type=float, default=119.850)
    parser.add_argument("--atc-stream", type=str,
                        default=os.environ.get("LIVEATC_URL",""))

    args = parser.parse_args()

    print("\n" + "="*60)
    print("  DJI Air 3 — Drone Ops Agent")
    print("="*60)

    # Step 1: Run pre-flight check
    print("\n[1/2] Running pre-flight compliance check...")
    verdict = run_preflight_check(
        lat             = args.lat,
        lon             = args.lon,
        altitude_ft     = args.alt,
        flight_datetime = args.time,
        timezone        = args.tz,
        pilot_name      = args.name,
        pilot_phone     = args.phone,
        pilot_email     = args.email,
        drone_sn        = args.sn
    )

    if verdict == "NO-GO":
        print("\n❌ Pre-flight result: NO-GO")
        print("ATC monitoring skipped — resolve NO-GO conditions first.")
        sys.exit(1)

    # Step 2: Start ATC listener in background
    if args.atc_mode == "off":
        print("\n[2/2] ATC monitoring disabled (--atc-mode off)")
        sys.exit(0)

    print(f"\n[2/2] Starting ATC listener ({args.atc_mode.upper()} mode)...")
    print("Alerts will be sent by SMS and email when relevant ATC traffic is detected.")
    print("Press Ctrl+C to stop.\n")

    audio_queue  = queue.Queue(maxsize=10)
    deduplicator = AlertDeduplicator(cooldown_seconds=120)

    if args.atc_mode == "stream":
        if not args.atc_stream:
            print("ERROR: Provide --atc-stream URL or set LIVEATC_URL")
            print("Find streams: https://www.liveatc.net/search/")
            sys.exit(1)
        capture = LiveATCCapture(args.atc_stream, audio_queue)
    else:
        capture = SDRAudioCapture(args.atc_freq * 1e6, audio_queue)

    capture.start()

    try:
        processing_pipeline(audio_queue, deduplicator)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        capture.stop()

if __name__ == "__main__":
    main()
