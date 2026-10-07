"""Deterministic, no-flight evals for the Safety1271 Android bridge."""

import argparse
import json
import sys
import time
from urllib.request import urlopen


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://172.20.10.2:8080")
    parser.add_argument("--max-battery", type=int, default=20)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    failures = []

    try:
        handshake = urlopen(f"{base}/", timeout=5).read().decode().strip()
        if handshake != "Connected":
            failures.append(f"handshake={handshake!r}")
    except Exception as error:
        failures.append(f"handshake_error={error}")

    try:
        status = fetch_json(f"{base}/status")
        required = {"status", "sdk", "aircraft_connected", "telemetry_available"}
        missing = required.difference(status)
        if missing:
            failures.append(f"missing_status_fields={sorted(missing)}")
        if status.get("sdk") != "5.18.0":
            failures.append(f"sdk={status.get('sdk')!r}")
        if status.get("aircraft_connected") is not True:
            failures.append("aircraft_not_connected")
        battery = status.get("battery_pct")
        if battery is not None and not 0 <= battery <= 100:
            failures.append(f"battery_pct={battery!r}")
        if battery is not None and battery <= args.max_battery:
            failures.append(f"battery_below_flight_threshold={battery}")
        updated = status.get("telemetry_updated_at_ms")
        if updated is not None and abs(time.time() * 1000 - updated) > 10000:
            failures.append("telemetry_stale")
        if status.get("aircraft_connected") is True:
            for name in ("altitude_m", "heading_deg"):
                if status.get(name) is None:
                    failures.append(f"{name}_missing_while_connected")
        print(json.dumps(status, indent=2))
    except Exception as error:
        failures.append(f"status_error={error}")

    if failures:
        print("BRIDGE_EVAL_FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("BRIDGE_EVAL_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
