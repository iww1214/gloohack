"""
Timed demo reveals for the Command Center replay.

Started by dashboard_server when a demo clip starts. For each scripted event it runs the
real agent (analyse) on the scenario's scene description ahead of time, then publishes the
result to the dashboard at the moment the clip shows it. Alerts are never sent to phone/email.

  python demo_runner.py <scenario_id> <start_epoch_seconds>
"""

import os, sys, json, time, base64, threading
from pathlib import Path

# Demo must never page anyone: blank these before the agent module reads them.
os.environ["ALERT_PHONE"] = ""
os.environ["ALERT_EMAIL"] = ""
os.chdir(Path(__file__).parent)

import safety1271_compound as agent

LEAD_S = 22   # start the model call this long before the reveal so latency is hidden


def wait_until(epoch: float):
    delay = epoch - time.time()
    if delay > 0:
        time.sleep(delay)


def post_narration(text: str):
    import urllib.request
    req = urllib.request.Request(f"{agent.DASHBOARD_URL}/api/narration", data=json.dumps({"text": text, "concern": "none"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=3).close()


def frame_b64(clip: Path, at_s: float) -> str:
    """JPEG (base64) of the clip's frame at the given time, wrapping if the clip is shorter."""
    import cv2
    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15
    count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(at_s * fps) % int(count))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"could not read a frame at {at_s}s from {clip.name}")
    h, w = frame.shape[:2]
    if w > 1280:
        frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
    return base64.standard_b64encode(cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()).decode()


def run_event(start: float, event: dict, entry: dict):
    if event.get("kind") == "story":
        # One look at the whole clip, one account; shown when the footage ends (or when ready, if later)
        import clip_story
        clip = Path(__file__).parent / "demo_clips" / entry["file"]
        directive = agent.get_directive() or event.get("directive", "")
        result = clip_story.tell(clip, directive)
        wait_until(start + event["at"])
        # An account that still contradicts the tracking after retries is not shown as fact
        post_narration(result["story"] if "problems" not in result
                       else "Narration withheld: it could not be reconciled with the tracking measurements.")
        if result.get("directive_match") and directive and "problems" not in result:
            agent.push_dashboard_alert("directive_match", {
                "zone": event.get("zone", "Parking lot"), "confidence": "medium",
                "description": result["story"] + " " + str(result.get("match_evidence", ""))})
        return
    wait_until(start + max(0, event["at"] - LEAD_S))
    if event.get("kind") == "parking":
        # Count free spaces across several frames of the clip, publish the median when the clip reaches that moment
        clip = Path(__file__).parent / "demo_clips" / entry["file"]
        heading = event.get("heading_deg", entry.get("heading_deg", 0))
        samples = []
        for t in (max(0, event["at"] - 2), event["at"], min(event["at"] + 2, 1e9)):
            try:
                r = agent.count_parking(frame_b64(clip, t), heading)
                if r:
                    samples.append(r)
            except Exception:
                pass
        count = None
        if samples:
            import statistics
            def med(key):
                vals = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
                return round(statistics.median(vals)) if vals else None
            count = dict(samples[len(samples) // 2])
            for key in ("empty", "occupied", "total_seen"):
                m = med(key)
                if m is not None:
                    count[key] = m
            count["coverage"] = "part_of_lot"
            count["samples"] = len(samples)
        if count and heading is not None:
            count["orientation"] = "assumed north-up (demo)"
        wait_until(start + event["at"])
        if count:
            agent.push_parking(count)
            where = ", ".join(f"{a['name']} ({a['count']})" for a in (count.get("areas") or [])[:3])
            post_narration(f"In the part of the lot in view the drone counted {count.get('empty')} free spaces and {count.get('occupied')} occupied, averaged over {count.get('samples', 1)} passes. "
                           + (f"Free spaces are in: {where}. " if where else "")
                           + f"Confidence: {count.get('confidence')}. The parking team has the count.")
        return
    if event.get("vision"):
        # Real footage: the model looks at the actual frame; nothing is scripted
        zone = event.get("zone", "Parking lot")
        directive = agent.get_directive() or event.get("directive", "")
        clip = Path(__file__).parent / "demo_clips" / entry["file"]
        # A few frames a moment apart, so motion is judged from change, not guessed from one still
        gap = event.get("frame_gap_s", 1.0)
        count = event.get("frames", 3)
        times = [max(0.0, event["at"] - gap * i) for i in range(count - 1, -1, -1)]
        images = [frame_b64(clip, t) for t in times]
        result = agent.analyse(zone, time.strftime("%H:%M \u2014 live"), b64_image=images,
                               directive=directive, frame_gap_s=gap)
    else:
        scenario = agent.DEMO_SCENARIOS[event["scenario"] - 1]
        # A scenario that carries a search directive uses the operator's live command when one is set.
        directive = (agent.get_directive() or scenario["directive"]) if "directive" in scenario else ""
        result = agent.analyse(scenario["zone"], scenario["time"],
                               text_description=scenario["description"], directive=directive)
    wait_until(start + event["at"])
    tool_name, tool_args, analysis = result
    agent.execute_tool(tool_name, tool_args, analysis)
    agent.push_dashboard_alert(tool_name, tool_args)


def main():
    scenario_id, start = sys.argv[1], float(sys.argv[2])
    manifest = json.loads(Path("demo_manifest.json").read_text(encoding="utf-8"))
    entry = next(s for s in manifest["scenarios"] if s["id"] == scenario_id)
    threads = [threading.Thread(target=run_event, args=(start, ev, entry), daemon=True) for ev in entry["events"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
