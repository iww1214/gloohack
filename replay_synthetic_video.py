"""Publish local synthetic footage to the loopback MediaMTX test server only."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import cv2
import imageio_ffmpeg


DEFAULT_RTMP_URL = "rtmp://127.0.0.1:1935/live/drone"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def watermark(frame):
    height, width = frame.shape[:2]
    cv2.rectangle(frame, (8, 8), (236, 36), (30, 30, 30), thickness=-1)
    cv2.putText(
        frame, "SIMULATED / NO FLIGHT", (16, 28),
        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 190, 255), 2, cv2.LINE_AA,
    )
    return frame


def replay_clip(clip_path: Path, rtmp_url: str = DEFAULT_RTMP_URL, loops: int = 0) -> None:
    parsed = urlparse(rtmp_url)
    if parsed.scheme != "rtmp" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("Synthetic replay only permits RTMP destinations on loopback")
    if loops < 0:
        raise ValueError("loops must be zero (repeat forever) or a positive count")
    if not clip_path.is_file():
        raise FileNotFoundError(clip_path)

    capture = cv2.VideoCapture(str(clip_path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"Could not open synthetic clip: {clip_path}")

    fps = capture.get(cv2.CAP_PROP_FPS)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width <= 0 or height <= 0:
        capture.release()
        raise ValueError("Synthetic clip has invalid video metadata")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temporary_file:
        watermarked_path = Path(temporary_file.name)
    writer = cv2.VideoWriter(
        str(watermarked_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height),
    )
    if not writer.isOpened():
        capture.release()
        writer.release()
        watermarked_path.unlink(missing_ok=True)
        raise RuntimeError("OpenCV could not create the watermarked temporary MP4")

    loops_completed = 0
    frames_published = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(watermark(frame))
            frames_published += 1
        if frames_published == 0:
            raise ValueError("No frames could be decoded from the synthetic clip")
    finally:
        writer.release()
        capture.release()

    loop_count = -1 if loops == 0 else loops - 1
    ffmpeg_command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-stream_loop", str(loop_count), "-re", "-i", str(watermarked_path),
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-f", "flv", rtmp_url,
    ]
    print(f"Publishing SIMULATED footage to {rtmp_url}. Press Ctrl+C to stop.", flush=True)
    try:
        subprocess.run(ffmpeg_command, check=True)
        loops_completed = loops
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"FFmpeg RTMP publisher failed with exit code {error.returncode}") from error
    finally:
        watermarked_path.unlink(missing_ok=True)

    print(json.dumps({
        "source_clip": clip_path.name,
        "rtmp_destination": rtmp_url,
        "loops_completed": loops_completed,
        "frames_published": frames_published,
        "watermark": "SIMULATED / NO FLIGHT",
        "flight_commands_sent": False,
    }, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--rtmp-url", default=DEFAULT_RTMP_URL)
    parser.add_argument("--loops", type=int, default=0, help="Zero repeats until Ctrl+C")
    args = parser.parse_args()
    try:
        replay_clip(args.clip, args.rtmp_url, args.loops)
        return 0
    except KeyboardInterrupt:
        print("\nSynthetic replay stopped.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
