"""Smoke-test Gloo OAuth and one low-cost completion without touching the drone."""

import argparse
import os
import sys

from dotenv import load_dotenv

import gloo_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="Verify OAuth credentials without making a model request",
    )
    args = parser.parse_args()
    load_dotenv()

    using_api_key = bool(os.getenv("GLOO_API_KEY"))
    if not using_api_key:
        missing = [
            name for name in ("GLOO_CLIENT_ID", "GLOO_CLIENT_SECRET")
            if not os.getenv(name)
        ]
        if missing:
            print(
                "Set GLOO_API_KEY, or the deprecated "
                f"{', '.join(missing)}"
            )
            return 2

    try:
        if using_api_key:
            print("Gloo auth: API key")
        else:
            gloo_client._tm.get_token()
            print("Gloo OAuth: OK")
        print(f"Gloo tradition: {gloo_client.GLOO_TRADITION}")
        if args.token_only:
            return 0

        text = gloo_client.complete(
            system_prompt="Reply with exactly GLOO_OK.",
            user_content="Connectivity smoke test.",
            model="atc_filter",
            use_tradition=False,
            max_tokens=16,
            cache=None,
        )
        print(f"Gloo completion: {text.strip()!r}")
        return 0 if text.strip() else 1
    except Exception as error:
        print(f"Gloo integration failed: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())