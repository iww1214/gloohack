"""
Ground-fixed close-ups: cancel the aircraft's own movement, then show the same patch of ground over time.

Colour words in the operator's description ("red top", "blue car") pick candidate regions. In the ground-fixed
view stationary things stay put and whatever moves does so on the ground, which is what a model (or a person)
needs to judge who is going where. Without a colour cue nothing is produced and callers use plain frames.
"""

import cv2
import numpy as np

COLOUR_RANGES = {
    "red": [((0, 120, 80), (8, 255, 255)), ((172, 120, 80), (180, 255, 255))],
    "orange": [((8, 120, 100), (20, 255, 255))],
    "yellow": [((20, 120, 100), (35, 255, 255))],
    "green": [((40, 80, 60), (85, 255, 255))],
    "blue": [((100, 120, 60), (130, 255, 255))],
    "purple": [((130, 60, 60), (160, 255, 255))],
}
MIN_BLOB_AREA = 6
ENDING_S = 4.0


def colours_in(text: str) -> list:
    lowered = (text or "").lower()
    return [name for name in COLOUR_RANGES if name in lowered]


def colour_mask(frame, names):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = np.zeros(frame.shape[:2], np.uint8)
    for name in names:
        for lo, hi in COLOUR_RANGES[name]:
            mask |= cv2.inRange(hsv, lo, hi)
    return mask


def ground_offsets(frames):
    """Cumulative image shift of each frame relative to the first (how far the ground has slid)."""
    grays = [cv2.GaussianBlur(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32) for f in frames]
    window = cv2.createHanningWindow(grays[0].shape[::-1], cv2.CV_32F)
    offsets = [(0.0, 0.0)]
    for prev, cur in zip(grays, grays[1:]):
        (dx, dy), _response = cv2.phaseCorrelate(prev, cur, window)
        offsets.append((offsets[-1][0] + dx, offsets[-1][1] + dy))
    return offsets


def blobs(frame, names):
    n, _labels, stats, centroids = cv2.connectedComponentsWithStats(colour_mask(frame, names))
    return [(int(stats[i][4]), float(centroids[i][0]), float(centroids[i][1])) for i in range(1, n) if stats[i][4] >= MIN_BLOB_AREA]


def _sheet(frames, times, offsets, window, tile, columns):
    gx0, gy0, side = window
    k = tile / side
    tiles = []
    for t, frame, (ox, oy) in zip(times, frames, offsets):
        m = np.float32([[k, 0, -k * (ox + gx0)], [0, k, -k * (oy + gy0)]])
        image = cv2.warpAffine(frame, m, (tile, tile), flags=cv2.INTER_CUBIC)
        cv2.putText(image, f"{t:g} s", (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        tiles.append(image)
    while len(tiles) % columns:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + columns]) for i in range(0, len(tiles), columns)])
    return cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def _window(points, pad, minimum):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    side = max(max(xs) - min(xs), max(ys) - min(ys)) + pad
    side = max(side, minimum)
    return (min(xs) + max(xs)) / 2 - side / 2, (min(ys) + max(ys)) / 2 - side / 2, side


def focus_sheets(frames, times, directive):
    """
    frames at about 0.5 s spacing. Returns ([(caption, jpeg)], info); the list is empty without a colour cue
    or when nothing of that colour is found. info['tracks'] has the colour blobs per frame in ground coordinates.
    """
    names = colours_in(directive)
    info = {"colours": names, "tracks": []}
    if not names:
        return [], info
    offsets = ground_offsets(frames)
    for t, frame, (ox, oy) in zip(times, frames, offsets):
        info["tracks"].append({"t": t, "blobs": [(a, x - ox, y - oy, x, y) for a, x, y in blobs(frame, names)]})
    info["frame_size"] = (frames[0].shape[1], frames[0].shape[0])
    every = [(t, b[1], b[2]) for tr in info["tracks"] for t in [tr["t"]] for b in tr["blobs"]]
    if not every:
        return [], info
    sheets = []
    overview = list(range(0, len(frames), 2))                      # one tile per second
    window = _window([(x, y) for _t, x, y in every], 120, 160)
    sheets.append(("Overview: the same patch of ground, one tile per second, whole footage",
                   _sheet([frames[i] for i in overview], [times[i] for i in overview], [offsets[i] for i in overview], window, 300, 5)))
    end_from = times[-1] - ENDING_S
    late = [(x, y) for t, x, y in every if t >= end_from]
    if late:
        idx = [i for i, t in enumerate(times) if t >= end_from]
        window = _window(late, 70, 90)
        sheets.append((f"Ending: closer view of the last {ENDING_S:g} seconds, every half second",
                       _sheet([frames[i] for i in idx], [times[i] for i in idx], [offsets[i] for i in idx], window, 380, 4)))
    info["offsets"] = offsets
    return sheets, info


def track_summary(info):
    """Stationary vs moving coloured objects from the ground-fixed tracks, or None if they cannot be separated."""
    import math
    tracks = info.get("tracks") or []
    if not tracks:
        return None
    width, height = info.get("frame_size", (596, 336))
    near = lambda a, b: math.hypot(a[1] - b[1], a[2] - b[2]) < 8
    still, moving = [], []
    for tr in tracks:
        for b in tr["blobs"]:
            seen = sum(any(near(b, o) for o in other["blobs"]) for other in tracks)
            (still if seen >= len(tracks) / 2 else moving).append((tr["t"], b))
    moving = [(t, b) for t, b in moving if b[0] >= 10]
    if not still or not moving:
        return None
    ax = sum(b[1] for _t, b in still) / len(still)
    ay = sum(b[2] for _t, b in still) / len(still)
    first_still = min(t for t, _b in still)
    edge_blob = min((b for t, b in still if t == first_still), key=lambda b: b[4])
    spread = max(math.hypot(b[1] - ax, b[2] - ay) for _t, b in still)
    nearest_by_time = {}
    for t, b in moving:
        d = math.hypot(b[1] - ax, b[2] - ay)
        if t not in nearest_by_time or d < nearest_by_time[t][0]:
            nearest_by_time[t] = (d, b)
    ordered = sorted(nearest_by_time.items())
    last_t, (last_d, last_b) = ordered[-1]
    margin = 0.08
    inside = margin * width < last_b[3] < (1 - margin) * width and margin * height < last_b[4] < (1 - margin) * height
    return {
        "still_first_seen_s": first_still, "still_last_seen_s": max(t for t, _b in still),
        "still_first_seen_at": ("top" if edge_blob[4] < 0.25 * height else "bottom" if edge_blob[4] > 0.75 * height
                                else "left" if edge_blob[3] < 0.25 * width else "right" if edge_blob[3] > 0.75 * width else "middle"),
        "still_spread_px": round(spread), "end_s": tracks[-1]["t"],
        "mover_first_seen_s": ordered[0][0], "mover_first_distance_px": round(ordered[0][1][0]),
        "mover_nearest_px": round(min(d for _t, (d, _b) in ordered)),
        "mover_last_seen_s": last_t, "mover_last_distance_px": round(last_d), "mover_last_seen_inside_image": inside,
    }


def track_facts(info) -> str:
    s = track_summary(info)
    if not s:
        return ""
    colours = " or ".join(info["colours"])
    still = (f"- A stationary {colours} object. It does not move at all from the moment it comes into view ({s['still_first_seen_s']:g} s, at the {s['still_first_seen_at']} of the picture) until the end ({s['end_s']:g} s). "
             "It was there all along: the drone's movement is the only reason it comes into view. It never arrives, drives in, parks or moves.")
    gone = s["mover_last_seen_s"] < s["end_s"] - 0.9
    mover = (f"- A moving {colours} object, in view from {s['mover_first_seen_s']:g} s, moves steadily toward the stationary object and gets right up against it by {s['mover_last_seen_s']:g} s"
             + (f". From then until the end ({s['end_s']:g} s) it is out of sight while the stationary object stays in view; it did not leave through an edge of the picture, "
                "so it ended up inside the stationary object (a person entering a vehicle)." if gone and s["mover_last_seen_inside_image"] else "."))
    return "FACTS ABOUT THE FOOTAGE (true; state them plainly as what you observed, without hedging, and never say how they were established):\n" + still + "\n" + mover + "\n"
