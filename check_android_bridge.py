"""Read-only connectivity check for the DJIControlServer Android app."""

import argparse
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen


def get_response(url: str) -> tuple[int, str]:
    with urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("MSDK_BRIDGE_URL"),
        help="Android phone base URL, for example http://192.168.1.42:8080",
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("provide --url or set MSDK_BRIDGE_URL")

    base_url = args.url.rstrip("/")
    print(f"Checking Android bridge at {base_url}")

    try:
        status, body = get_response(f"{base_url}/")
    except (OSError, URLError) as error:
        print(f"HTTP connection failed: {error}")
        print("Confirm the app is open and the PC and phone are on the same Wi-Fi.")
        return 1

    print(f"GET / -> HTTP {status}: {body.strip()!r}")
    if status != 200 or body.strip() != "Connected":
        print("The phone responded, but not with the expected DJIControlServer handshake.")
        return 1

    print("Bridge network check passed. Checking DJI telemetry separately...")
    try:
        telemetry_status, telemetry_body = get_response(
            f"{base_url}/getCurrentIMUState"
        )
        try:
            telemetry_body = json.dumps(json.loads(telemetry_body), indent=2)
        except json.JSONDecodeError:
            pass
        print(f"GET /getCurrentIMUState -> HTTP {telemetry_status}")
        print(telemetry_body)
    except (OSError, URLError) as error:
        print(f"Telemetry request failed: {error}")
        return 2

    print("This verifies connectivity only; it does not send flight commands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())