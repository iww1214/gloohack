"""Read-only Gloo vision evaluation for synthetic or licensed local video clips."""

import argparse
import base64
import json
from pathlib import Path

import cv2
import numpy as np

import gloo_client


LABELS = ("CLEAR", "PERSON", "VEHICLE", "SMOKE", "DAMAGE", "UNCERTAIN")
SYNTHETIC_LABELS = ("CLEAR", "PERSON", "VEHICLE", "SMOKE", "DAMAGE")
SYSTEM = (
    "You evaluate test footage for a drone operator. Report only what is visible "
    "in this frame; do not infer intent, authorization, identity, or a missing child. "
    "Return only JSON with label (CLEAR, PERSON, VEHICLE, SMOKE, DAMAGE, or "
    "UNCERTAIN) and a brief evidence string. UNCERTAIN is appropriate if the "
    "image does not support a label. Never request drone commands or alerts."
)


def _synthetic_campus_frame(label: str, frame_index: int, frame_count: int):
    """Render a schematic campus scene using shapes, not captured people or sites."""
    frame = np.full((360, 640, 3), (70, 125, 74), dtype=np.uint8)
    cv2.rectangle(frame, (0, 242), (639, 359), (75, 77, 79), -1)
    cv2.line(frame, (0, 300), (639, 300), (220, 220, 220), 2)
    cv2.rectangle(frame, (58, 74), (500, 214), (183, 185, 180), -1)
    cv2.rectangle(frame, (58, 74), (500, 95), (111, 114, 111), -1)
    for window_x in range(85, 485, 54):
        cv2.rectangle(frame, (window_x, 118), (window_x + 25, 145), (145, 190, 205), -1)
        cv2.rectangle(frame, (window_x, 164), (window_x + 25, 190), (145, 190, 205), -1)
    cv2.rectangle(frame, (520, 98), (603, 209), (128, 133, 127), -1)
    cv2.rectangle(frame, (543, 65), (559, 98), (94, 98, 95), -1)

    if label == "VEHICLE":
        progress = frame_index / max(1, frame_count - 1)
        center_x = int(115 + progress * 390)
        cv2.rectangle(frame, (center_x - 34, 269), (center_x + 34, 314), (30, 70, 205), -1)
        cv2.rectangle(frame, (center_x - 22, 276), (center_x + 22, 295), (178, 211, 220), -1)
        for wheel_x in (center_x - 35, center_x + 29):
            cv2.rectangle(frame, (wheel_x, 275), (wheel_x + 7, 286), (22, 22, 22), -1)
            cv2.rectangle(frame, (wheel_x, 298), (wheel_x + 7, 309), (22, 22, 22), -1)
    elif label == "PERSON":
        person_x = int(300 + 48 * np.sin(frame_index / max(1, frame_count - 1) * np.pi))
        cv2.circle(frame, (person_x, 229), 10, (35, 183, 239), -1)
        cv2.line(frame, (person_x, 239), (person_x, 267), (35, 183, 239), 8)
        cv2.line(frame, (person_x, 246), (person_x - 14, 259), (35, 183, 239), 5)
        cv2.line(frame, (person_x, 246), (person_x + 14, 258), (35, 183, 239), 5)
        cv2.line(frame, (person_x, 265), (person_x - 11, 282), (35, 183, 239), 5)
        cv2.line(frame, (person_x, 265), (person_x + 12, 281), (35, 183, 239), 5)
    elif label == "SMOKE":
        for offset, radius in ((0, 17), (22, 23), (50, 19), (76, 27)):
            center = (550 + offset // 3, 59 - offset)
            cv2.circle(frame, center, radius, (175, 178, 179), -1)
    elif label == "DAMAGE":
        damage = np.array([(327, 75), (363, 74), (354, 101), (379, 114),
                           (350, 132), (367, 154), (338, 145), (322, 170),
                           (309, 139), (284, 128), (309, 111)], dtype=np.int32)
        cv2.fillPoly(frame, [damage], (36, 39, 40))
        cv2.line(frame, (337, 86), (330, 126), (95, 96, 93), 3)
        cv2.line(frame, (330, 126), (348, 151), (95, 96, 93), 3)

    return frame


def generate_synthetic_clips(output_dir: Path, fps: int = 6, duration_s: int = 4) -> list[dict]:
    """Write short schematic campus MP4 clips and a provenance manifest."""
    if fps <= 0 or duration_s <= 0:
        raise ValueError("fps and duration_s must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_count = fps * duration_s
    manifest = []
    for label in SYNTHETIC_LABELS:
        path = output_dir / f"synthetic_{label.lower()}.mp4"
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (640, 360)
        )
        if not writer.isOpened():
            writer.release()
            raise RuntimeError(f"Could not create synthetic video: {path}")
        try:
            for frame_index in range(frame_count):
                writer.write(_synthetic_campus_frame(label, frame_index, frame_count))
        finally:
            writer.release()
        manifest.append({
            "clip": path.name,
            "expected": label,
            "source": "generated synthetic schematic; no camera footage or real people",
            "duration_s": duration_s,
            "fps": fps,
        })
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def classify_frame(frame) -> dict:
    height, width = frame.shape[:2]
    if width > 640:
        frame = cv2.resize(frame, (640, round(height * 640 / width)))
    encoded, image = cv2.imencode(".jpg", frame)
    if not encoded:
        raise ValueError("Could not encode video frame")
    image_url = gloo_client.b64_to_data_url(base64.b64encode(image.tobytes()).decode())
    response = gloo_client._v2_client().chat.completions.create(
        model=gloo_client.MODELS["patrol"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "Classify this single test frame."},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ],
        max_tokens=160,
        temperature=0,
        response_format={"type": "json_object"},
    )
    response_text = response.choices[0].message.content
    if not response_text or not response_text.strip():
        raise ValueError("Gloo Completions V2 returned no vision text")
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        start = response_text.find("{")
        if start < 0:
            raise ValueError(f"Gloo returned non-JSON vision text: {response_text[:160]!r}")
        try:
            result, _ = json.JSONDecoder().raw_decode(response_text[start:])
        except json.JSONDecodeError as error:
            raise ValueError(f"Gloo returned invalid vision JSON: {response_text[:160]!r}") from error
    if not isinstance(result, dict) or result.get("label") not in LABELS:
        raise ValueError("Gloo returned an invalid visual label")
    if not isinstance(result.get("evidence"), str) or not result["evidence"].strip():
        raise ValueError("Gloo did not provide visual evidence")
    return {"label": result["label"], "evidence": result["evidence"]}


def evaluate_clip(path: Path, expected: str, interval_s: float, max_frames: int) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"Could not open clip: {path}")
    frames = []
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            raise ValueError("Video has no valid frame rate")
        frame_step = max(1, round(fps * interval_s))
        index = 0
        while len(frames) < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            if index % frame_step == 0:
                judgment = classify_frame(frame)
                frames.append({"time_s": round(index / fps, 2), **judgment})
            index += 1
    finally:
        capture.release()
    if not frames:
        raise ValueError("No frames decoded")
    labels = [frame["label"] for frame in frames]
    return {
        "clip": path.name, "expected": expected, "observed": labels,
        "passed": expected in labels if expected != "CLEAR" else all(label == "CLEAR" for label in labels),
        "frames": frames, "flight_commands_sent": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", nargs="?", type=Path, help="Local synthetic or licensed video (no automatic downloads)")
    parser.add_argument("--expected", choices=LABELS)
    parser.add_argument("--interval-s", type=float, default=4)
    parser.add_argument("--max-frames", type=int, default=3)
    parser.add_argument("--generate-synthetic", action="store_true", help="Generate privacy-safe schematic campus clips")
    parser.add_argument("--output-dir", type=Path, default=Path("eval_videos/synthetic"))
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--duration-s", type=int, default=4)
    args = parser.parse_args()
    if args.generate_synthetic:
        try:
            print(json.dumps(generate_synthetic_clips(args.output_dir, args.fps, args.duration_s), indent=2))
            return 0
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    if args.clip is None or args.expected is None:
        parser.error("clip and --expected are required unless --generate-synthetic is used")
    if args.interval_s <= 0 or args.max_frames <= 0:
        parser.error("interval-s and max-frames must be positive")
    try:
        result = evaluate_clip(args.clip, args.expected, args.interval_s, args.max_frames)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"passed": False, "error": str(error), "clip": args.clip.name}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())