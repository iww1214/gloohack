"""
Does the clip narration match what actually happens in the clip?

  python eval_narration.py [--runs 3] [--clip "part of file name"]

Two kinds of check:
  measured - tracking of the coloured target in ground coordinates (no model involved)
  judged   - a model compares each narration with the ground-truth facts in narration_truth.json
A clip passes only if every run passes, so a lucky run does not count. Exit code 1 on any failure.
"""

import argparse, json, math, os, re, sys
from pathlib import Path

os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

import gloo_client
from clip_story import read_frames, tell
from focus import colours_in, focus_sheets, track_summary

TRUTH = Path("narration_truth.json")
REPORT = Path("eval_narration_last.json")

MAKE_MODEL = ("volkswagen", "tesla", "ford", "toyota", "honda", "nissan", "hyundai", "kia",
              "chevrolet", "chevy", "bmw", "mercedes", "audi", "subaru", "jeep", "ram", "gmc", "mazda", "lexus", "porsche")
HEDGES = ("appears to be", "appearing to be", "likely", "looks like", "probably", "possibly", "similar")


def make_model_as_fact(story: str) -> bool:
    """True when a make or model word is used without a hedge word just before it."""
    for name in MAKE_MODEL:
        for match in re.finditer(re.escape(name), story, flags=re.I):
            before = story[max(0, match.start() - 100):match.start()].lower()
            if not any(hedge in before for hedge in HEDGES):
                return True
    return False


JUDGE_SYSTEM = """You check a narration of drone footage against known facts. Judge only what the narration says.
For every statement in MUST give "satisfied" or "missing". For every statement in MUST_NOT give "violated" or "ok".
A MUST_NOT is violated only if the narration says it or clearly implies it. Reply with JSON only:
{"must": [{"verdict": "satisfied|missing", "reason": "..."}], "must_not": [{"verdict": "violated|ok", "reason": "..."}]}
Same order as given."""


def measure(clip: Path, directive: str) -> dict:
    """Independent facts from tracking: does a mover approach a stationary coloured object and then vanish there?"""
    frames, times, length = read_frames(clip, 0.5)
    _sheets, info = focus_sheets(frames, times, directive)
    s = track_summary(info)
    if not s:
        return {"error": "could not separate a stationary object from a mover"}
    return {**s, "footage_s": round(length, 1),
            "approaches": s["mover_first_distance_px"] > 2 * s["mover_nearest_px"],
            "vanishes_at_object": s["mover_last_distance_px"] < 45 and s["mover_last_seen_s"] < s["end_s"] - 0.9 and s["mover_last_seen_inside_image"]}


def judge(narration: str, case: dict) -> dict:
    must, must_not = case["must"], case["must_not"]
    prompt = ("NARRATION:\n" + narration + "\n\nMUST:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(must))
              + "\n\nMUST_NOT:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(must_not)))
    raw = gloo_client.complete(JUDGE_SYSTEM, prompt, model="patrol", max_tokens=900)
    verdict = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    missing = [must[i] for i, v in enumerate(verdict["must"]) if v.get("verdict") != "satisfied"]
    violated = [must_not[i] for i, v in enumerate(verdict["must_not"]) if v.get("verdict") == "violated"]
    return {"passed": not missing and not violated, "missing": missing, "violated": violated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--clip", default="")
    args = parser.parse_args()
    cases = [c for c in json.loads(TRUTH.read_text(encoding="utf-8"))["clips"] if args.clip.lower() in c["file"].lower()]
    report, failed = [], False
    for case in cases:
        clip = Path("demo_clips") / case["file"]
        entry = {"clip": case["file"], "measured": measure(clip, case["directive"]), "runs": []}
        m = entry["measured"]
        measured_ok = "error" in m or (m.get("approaches") is True and m.get("vanishes_at_object") is True)
        print(f"\n{case['file']}\n  measured: {m}\n  tracking check: {'skipped (no colour cue)' if 'error' in m else measured_ok}")
        for n in range(1, args.runs + 1):
            try:
                story = tell(clip, case["directive"])
                result = judge(story["story"], case)
                if make_model_as_fact(story["story"]):
                    result["passed"] = False
                    result["violated"] = result.get("violated", []) + ["A vehicle is named by make or model stated as fact, not as a guess."]
                result["story"] = story["story"]
            except Exception as e:
                result = {"passed": False, "error": f"{type(e).__name__}: {e}"}
            entry["runs"].append(result)
            print(f"  run {n}: {'PASS' if result['passed'] else 'FAIL'}  {result.get('story', result.get('error', ''))}")
            for item in result.get("missing", []):
                print(f"      missing: {item}")
            for item in result.get("violated", []):
                print(f"      violated: {item}")
        entry["passed"] = measured_ok and all(r["passed"] for r in entry["runs"])
        failed |= not entry["passed"]
        report.append(entry)
        print(f"  => {sum(r['passed'] for r in entry['runs'])}/{len(entry['runs'])} narrations passed; clip {'PASSES' if entry['passed'] else 'FAILS'}")
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
