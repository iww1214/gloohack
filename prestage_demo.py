"""
Pre-stage demo narrations: run the real agent (with verification) on every story scenario
ahead of showtime and cache the results, so the reveal during the demo is instant.

  python prestage_demo.py [--scenario person_to_vehicle] [--force]

--scenario  stage only one scenario id from demo_manifest.json (default: all story scenarios)
--force     ignore an existing staged result and recompute it

Run this before the demo, after any clip file changes (the cache is keyed on the clip's
content, so an edited clip never serves a stale account). Verified results are reused;
if a run cannot be reconciled with the tracking it is cached as withheld, exactly like live.
"""

import argparse, json, os, sys
from pathlib import Path

os.environ.setdefault("ALERT_PHONE", "")
os.environ.setdefault("ALERT_EMAIL", "")
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

import clip_story

MANIFEST = Path(__file__).with_name("demo_manifest.json")
DEMO_DIR = Path(__file__).parent / "demo_clips"


def story_scenarios() -> list[dict]:
    out = []
    for entry in json.loads(MANIFEST.read_text(encoding="utf-8"))["scenarios"]:
        for event in entry.get("events", []):
            if event.get("kind") == "story" and (DEMO_DIR / entry["file"]).exists():
                out.append({"id": entry["id"], "title": entry.get("title", entry["id"]),
                            "file": entry["file"], "directive": event.get("directive", "")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="only stage this scenario id")
    ap.add_argument("--force", action="store_true", help="recompute even if a staged result exists")
    args = ap.parse_args()

    todo = story_scenarios()
    if args.scenario:
        todo = [s for s in todo if s["id"] == args.scenario]
        if not todo:
            print(f"no story scenario with id {args.scenario!r}")
            return 1

    failures = 0
    for s in todo:
        clip = DEMO_DIR / s["file"]
        if not args.force and clip_story.load_cached(clip, s["directive"]) is not None:
            print(f"[staged]  {s['id']}: already cached, skipping (use --force to redo)")
            continue
        print(f"[working] {s['id']}: {s['file']} — directive {s['directive']!r} ...", flush=True)
        try:
            result = clip_story.tell(clip, s["directive"], use_cache=False)
            clip_story.save_cached(clip, s["directive"], result)
        except Exception as e:
            print(f"[FAILED]  {s['id']}: {e}")
            failures += 1
            continue
        if "problems" in result:
            print(f"[staged]  {s['id']}: WITHHELD at showtime (unverified after {result['attempts']} attempts: {result['problems'][:120]})")
        else:
            print(f"[staged]  {s['id']}: verified in {result['attempts']} attempt(s): {result['story'][:110]}...")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
