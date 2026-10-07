"""
Safety1271 — simulated aerial demo footage generator.

Renders stylised top-down "drone" clips of a campus into demo_clips/*.mp4 so the
Command Center can replay the three demo scenarios without a flight.
The clips are simulations (watermarked as such), not real footage; no real people appear.

  python demo_footage.py            # render all clips
  python demo_footage.py open_car_door
"""

import sys, math, random
from pathlib import Path
import cv2
import numpy as np

OUT = Path(__file__).with_name("demo_clips")
W, H = 1600, 900          # world size in pixels (~10 px per metre)
VW, VH = 960, 540         # camera view
FPS = 15

# BGR colours
GRASS, ASPHALT, CONCRETE, ROOF = (62, 118, 66), (74, 74, 78), (176, 178, 180), (146, 148, 156)
RED_SHIRT = (28, 28, 215)


def smooth(a, b, u):
    u = max(0.0, min(1.0, u))
    u = u * u * (3 - 2 * u)
    return a + (b - a) * u


def camera(keys, t):
    """keys: [(time_s, cx, cy)] with smooth interpolation; adds a little hover sway."""
    if t <= keys[0][0]:
        cx, cy = keys[0][1:]
    elif t >= keys[-1][0]:
        cx, cy = keys[-1][1:]
    else:
        for (t0, x0, y0), (t1, x1, y1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0)
                cx, cy = smooth(x0, x1, u), smooth(y0, y1, u)
                break
    cx += 4 * math.sin(t * 0.9) + 2 * math.sin(t * 2.3)
    cy += 3 * math.sin(t * 0.7 + 1)
    cx = min(max(cx, VW / 2), W - VW / 2)
    cy = min(max(cy, VH / 2), H - VH / 2)
    return cx, cy


def pingpong(p0, p1, speed, t, phase=0.0):
    length = math.dist(p0, p1)
    d = (speed * t + phase * length) % (2 * length)
    forward = d <= length
    u = (d if forward else 2 * length - d) / length
    x, y = p0[0] + (p1[0] - p0[0]) * u, p0[1] + (p1[1] - p0[1]) * u
    heading = math.atan2(p1[1] - p0[1], p1[0] - p0[0]) + (0 if forward else math.pi)
    return x, y, heading


def draw_car(img, cx, cy, color, door_open=False):
    w, h = 40, 88
    x0, y0, x1, y1 = int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)
    shade = tuple(int(c * 0.55) for c in color)
    cv2.rectangle(img, (x0 + 7, y0 + 7), (x1 + 7, y1 + 7), (40, 40, 44), -1)   # shadow
    cv2.rectangle(img, (x0, y0 + 4), (x1, y1 - 4), color, -1)
    cv2.rectangle(img, (x0 + 4, y0), (x1 - 4, y1), color, -1)
    cv2.rectangle(img, (x0 + 5, y0 + 16), (x1 - 5, y0 + 30), (60, 52, 46), -1)    # windscreen
    cv2.rectangle(img, (x0 + 5, y1 - 24), (x1 - 5, y1 - 14), (60, 52, 46), -1)    # rear window
    cv2.rectangle(img, (x0 + 6, y0 + 32), (x1 - 6, y1 - 26), shade, -1)           # roof
    if door_open:
        hinge = (x0, int(cy - 4))
        a = math.radians(72)
        tip = (int(hinge[0] - 48 * math.sin(a)), int(hinge[1] + 48 * math.cos(a)))
        cv2.line(img, (hinge[0] - 6, hinge[1] + 6), (tip[0] - 6, tip[1] + 6), (40, 40, 44), 8)
        cv2.line(img, hinge, tip, (205, 205, 210), 8)
        cv2.line(img, hinge, tip, (150, 120, 90), 3)


def draw_person_shadow(mask, x, y, s):
    cv2.ellipse(mask, (int(x + 5 * s), int(y + 5 * s)), (int(15 * s), int(9 * s)), 0, 0, 360, 255, -1)


def draw_person(img, x, y, heading, shirt, s=1.0):
    ang = math.degrees(heading) + 90
    axes = (max(3, int(15 * s)), max(3, int(8.5 * s)))
    cv2.ellipse(img, (int(x), int(y)), axes, ang, 0, 360, shirt, -1)
    cv2.ellipse(img, (int(x), int(y)), axes, ang, 0, 360, tuple(int(c * 0.6) for c in shirt), 1)
    cv2.circle(img, (int(x), int(y)), max(3, int(6.3 * s)), (60, 70, 95), -1)       # head (hair)


def make_base(door_open=False, bag=False, seed=7):
    rng = random.Random(seed)
    img = np.full((H, W, 3), GRASS, np.uint8)
    # grass texture
    noise = np.random.default_rng(seed).normal(0, 5, img.shape).astype(np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    # road + fence along the west edge
    cv2.rectangle(img, (0, 0), (52, H), ASPHALT, -1)
    for y in range(0, H, 50):
        cv2.line(img, (26, y), (26, y + 26), (190, 190, 120), 2)
    cv2.line(img, (70, 0), (70, H), (45, 45, 48), 2)
    for y in range(0, H, 36):
        cv2.circle(img, (70, y), 3, (35, 35, 38), -1)

    # west forecourt, main entrance forecourt, paths
    cv2.rectangle(img, (84, 120), (262, 278), CONCRETE, -1)
    cv2.rectangle(img, (392, 300), (568, 392), CONCRETE, -1)
    cv2.rectangle(img, (84, 278), (262, 300), (150, 160, 150), -1)

    # building with shadow, roof panels and doors
    cv2.rectangle(img, (272, 92), (978, 312), (45, 70, 50), -1)                      # shadow
    cv2.rectangle(img, (262, 80), (960, 300), ROOF, -1)
    cv2.rectangle(img, (262, 80), (960, 300), (110, 112, 120), 3)
    for x in range(262, 960, 58):
        cv2.line(img, (x, 80), (x, 300), (130, 132, 140), 1)
    for (hx, hy) in ((340, 130), (620, 150), (860, 130)):
        cv2.rectangle(img, (hx, hy), (hx + 46, hy + 36), (120, 124, 130), -1)         # HVAC units
        cv2.rectangle(img, (hx + 6, hy + 6), (hx + 40, hy + 30), (96, 98, 104), 2)
    cv2.rectangle(img, (254, 170), (266, 232), (60, 92, 150), -1)                     # west door
    cv2.rectangle(img, (440, 294), (520, 306), (60, 92, 150), -1)                     # main door

    if bag:
        bx, by = 474, 314
        cv2.rectangle(img, (bx + 4, by + 4), (bx + 30, by + 40), (40, 50, 40), -1)    # shadow
        cv2.rectangle(img, (bx, by), (bx + 26, by + 36), (38, 36, 40), -1)            # backpack
        cv2.rectangle(img, (bx + 4, by + 18), (bx + 22, by + 32), (60, 58, 64), -1)
        cv2.line(img, (bx + 3, by), (bx + 3, by + 36), (80, 80, 88), 2)

    # parking lot
    cv2.rectangle(img, (640, 380), (1560, 860), ASPHALT, -1)
    bay_w, xs = 64, [670 + i * 64 for i in range(14)]
    for row_top in (410, 610):
        for x in xs:
            cv2.line(img, (x, row_top), (x, row_top + 110), (200, 200, 200), 2)
        cv2.line(img, (xs[0] + 14 * 64 - 64 + 64, row_top), (xs[0] + 14 * 64, row_top), (200, 200, 200), 2)
    palette = [(200, 200, 205), (40, 40, 44), (150, 60, 40), (60, 70, 160), (190, 190, 190),
               (60, 110, 60), (30, 30, 120), (170, 150, 120), (80, 80, 84)]
    for row_top in (410, 610):
        for i, x in enumerate(xs[:-1]):
            if rng.random() < 0.28:
                continue
            cx, cy = x + bay_w // 2, row_top + 55
            is_suv = (row_top == 610 and i == 5)
            if row_top == 610 and i == 4:
                continue                                  # gap beside the SUV
            draw_car(img, cx, cy, (212, 212, 218) if is_suv else rng.choice(palette),
                     door_open=(door_open and is_suv))
    # trees
    for (tx, ty, r) in [(140, 420, 44), (210, 520, 38), (120, 640, 46), (330, 700, 50), (520, 560, 40),
                        (760, 330, 0), (1100, 120, 52), (1300, 90, 44), (1500, 180, 48), (1560, 330, 40),
                        (420, 800, 46), (200, 820, 40)]:
        if r:
            cv2.circle(img, (tx + 9, ty + 9), r, (38, 70, 42), -1)
            cv2.circle(img, (tx, ty), r, (46, 98, 52), -1)
            cv2.circle(img, (tx - r // 4, ty - r // 4), r // 2, (62, 124, 70), -1)
    return img


def vignette():
    y, x = np.ogrid[:VH, :VW]
    d = ((x - VW / 2) / (VW / 2)) ** 2 + ((y - VH / 2) / (VH / 2)) ** 2
    return (1 - 0.28 * np.clip(d, 0, 1.4))[..., None].astype(np.float32)


VIG = vignette()
RNG = np.random.default_rng(3)


def finish(view, t):
    out = view.astype(np.float32) * VIG
    out += RNG.normal(0, 2.2, out.shape).astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0.5)
    cv2.putText(out, "SIMULATED DEMO FOOTAGE", (14, VH - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
    return out


def render(path, duration, cam_keys, base, actors):
    """actors: list of (fn(t)->(x,y,heading), shirt_colour, scale)."""
    path.parent.mkdir(exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (VW, VH))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open an mp4 writer")
    for n in range(int(duration * FPS)):
        t = n / FPS
        cx, cy = camera(cam_keys, t)
        ox, oy = int(cx - VW / 2), int(cy - VH / 2)
        view = base[oy:oy + VH, ox:ox + VW].copy()
        mask = np.zeros((VH, VW), np.uint8)
        pts = []
        for fn, shirt, s in actors:
            x, y, hd = fn(t)
            pts.append((x - ox, y - oy, hd, shirt, s))
            draw_person_shadow(mask, x - ox, y - oy, s)
        view[mask > 0] = (view[mask > 0] * 0.66).astype(np.uint8)
        for x, y, hd, shirt, s in pts:
            draw_person(view, x, y, hd, shirt, s)
        writer.write(finish(view, t))
    writer.release()


ADULT = [(150, 90, 60), (90, 90, 95), (210, 210, 215), (60, 110, 150), (80, 80, 40)]


def clip_open_car_door():
    base = make_base(door_open=True)
    keys = [(0, 700, 520), (14, 1100, 560), (24, 1180, 600), (30, 1180, 600)]
    actors = [(lambda t: pingpong((300, 340), (330, 420), 16, t), ADULT[1], 1.0),
              (lambda t: pingpong((1330, 560), (1480, 560), 14, t, 0.3), ADULT[3], 1.0)]
    render(OUT / "open_car_door.mp4", 30, keys, base, actors)


def clip_lost_child():
    base = make_base()
    keys = [(0, 960, 520), (12, 1100, 540), (22, 360, 270), (30, 340, 280), (45, 340, 290)]
    actors = [
        # look-alike: child in red holding an adult's hand in the car park aisle
        (lambda t: pingpong((900, 565), (1200, 565), 30, t), ADULT[1], 1.0),
        (lambda t: (lambda p: (p[0] - 18, p[1] + 2, p[2]))(pingpong((900, 565), (1200, 565), 30, t)), RED_SHIRT, 0.75),
        (lambda t: pingpong((1250, 470), (1420, 480), 24, t, 0.5), ADULT[0], 1.0),
        # the lone child on the west verge
        (lambda t: (96 + 5 * math.sin(t * 0.5), 318 + 18 * math.sin(t * 0.3), math.pi), RED_SHIRT, 0.75),
        # adults near the main entrance, far from the child
        (lambda t: pingpong((420, 360), (540, 350), 20, t), ADULT[2], 1.0),
    ]
    render(OUT / "lost_child.mp4", 45, keys, base, actors)


def clip_unattended_bag():
    base = make_base(bag=True)
    keys = [(0, 800, 470), (6, 560, 340), (13, 500, 330), (16, 760, 300), (22, 760, 300),
            (26, 520, 340), (36, 500, 340)]
    actors = [
        (lambda t: pingpong((340, 430), (420, 360), 18, t), ADULT[0], 1.0),
        (lambda t: pingpong((620, 440), (560, 372), 20, t, 0.4), ADULT[3], 1.0),
        (lambda t: pingpong((900, 340), (930, 420), 14, t), ADULT[4], 1.0),
    ]
    render(OUT / "unattended_bag.mp4", 36, keys, base, actors)


CLIPS = {"open_car_door": clip_open_car_door, "lost_child": clip_lost_child, "unattended_bag": clip_unattended_bag}

if __name__ == "__main__":
    names = sys.argv[1:] or list(CLIPS)
    for name in names:
        print(f"rendering {name} ...", flush=True)
        CLIPS[name]()
    print("done:", ", ".join(f"{n}.mp4" for n in names))
