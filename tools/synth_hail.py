"""
Make synthetic hail-plate camera frames with known stone sizes, for testing the detector.

  python tools/synth_hail.py out_dir --frames 6 --night

Writes frame_XX.jpg plus truth.json (stone positions and sizes in mm).
"""
import argparse, json, math, os, random, sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from hailcam import ARUCO, PlateConfig  # noqa: E402


def plate_image(cfg, stones, s=4.0, night=False, rain=0):
    W, H = int(cfg.width_mm * s), int(cfg.height_mm * s)
    img = np.full((H, W), 28 if not night else 40, np.uint8)
    img = cv2.add(img, np.random.normal(0, 4, img.shape).astype(np.int16).clip(0, 255).astype(np.uint8))
    m = int(cfg.marker_mm * s)
    for i in range(4):
        ox, oy = cfg.marker_origin(i)
        x, y = int(ox * s), int(oy * s)
        # marker printed on a white card that extends into the inset around it
        q = int(cfg.inset_mm * s * 0.8)
        cv2.rectangle(img, (x - q, y - q), (x + m + q - 1, y + m + q - 1), 255, -1)
        mk = cv2.aruco.generateImageMarker(ARUCO, i, m)
        img[y:y + m, x:x + m] = mk
    for st in stones:
        cx, cy, d = st["x"] * s, st["y"] * s, st["d_mm"] * s
        ax = (d / 2, d / 2 * st.get("squash", 1))
        cv2.ellipse(img, (int(cx), int(cy)), (int(ax[0]), int(ax[1])), st.get("ang", 0), 0, 360, 225, -1, cv2.LINE_AA)
        cv2.ellipse(img, (int(cx - d * .12), int(cy - d * .12)), (int(ax[0] * .35), int(ax[1] * .35)), 0, 0, 360, 250, -1, cv2.LINE_AA)
    for _ in range(rain):  # small splashes and streaks that must be ignored
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        cv2.circle(img, (x, y), random.randint(1, int(1.5 * s)), random.randint(90, 200), -1)
    return img


def camera_view(top, cfg, s=4.0, size=(1920, 1080), tilt=0.28, seed=0):
    rng = np.random.default_rng(seed)
    H, W = top.shape
    cw, ch = size
    # plate occupies the middle of the frame, seen from above at an angle
    cx, cy, pw = cw / 2 + rng.uniform(-80, 80), ch / 2 + rng.uniform(-40, 40), ch * 0.78
    dst = np.float32([[cx - pw / 2 * (1 - tilt), cy - pw * .42], [cx + pw / 2 * (1 - tilt), cy - pw * .42],
                      [cx + pw / 2, cy + pw * .42], [cx - pw / 2, cy + pw * .42]])
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    M = cv2.getPerspectiveTransform(src, dst)
    frame = cv2.warpPerspective(top, M, size, borderValue=110)
    frame = cv2.GaussianBlur(frame, (3, 3), 0)
    frame = cv2.add(frame, rng.normal(0, 3, frame.shape).astype(np.int16).clip(0, 255).astype(np.uint8))
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--night", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)
    np.random.seed(a.seed)
    os.makedirs(a.out, exist_ok=True)
    cfg = PlateConfig()
    stones, truth = [], []
    for f in range(a.frames):
        for _ in range(random.randint(2, 5)):  # new stones land each frame
            d = random.choice([8, 12, 16, 19, 25, 32, 44])
            x = random.uniform(cfg.marker_mm + cfg.inset_mm + 20, cfg.width_mm - cfg.marker_mm - cfg.inset_mm - 20)
            y = random.uniform(20, cfg.height_mm - 20)
            if all(math.hypot(x - o["x"], y - o["y"]) > (d + o["d_mm"]) / 2 + 4 for o in stones):
                st = dict(x=x, y=y, d_mm=d, squash=random.uniform(.8, 1), ang=random.uniform(0, 180), frame=f)
                stones.append(st)
                truth.append(st)
        top = plate_image(cfg, stones, night=a.night, rain=40)
        cv2.imwrite(os.path.join(a.out, f"frame_{f:02d}.jpg"), camera_view(top, cfg, seed=f), [cv2.IMWRITE_JPEG_QUALITY, 88])
    json.dump(truth, open(os.path.join(a.out, "truth.json"), "w"), indent=1)
    print(f"{len(truth)} stones over {a.frames} frames -> {a.out}")


if __name__ == "__main__":
    main()
