"""
Hail camera: measures hailstones on a dark "hail plate" watched by a Reolink camera.

How it measures
  The plate carries four printed ArUco markers (ids 0-3) at its corners, each MARKER_MM wide.
  Their known positions give a homography, so every frame is warped to a top-down view at a fixed
  scale (PX_PER_MM). Hailstones show up as bright, roundish blobs on the dark plate; each blob's
  size is measured in millimetres. Stones are tracked between frames so each new stone is counted
  once, which gives a count and a stones-per-square-foot rate.

  Works day or night: under the camera's infrared light, ice still reads bright against a matte
  black plate. Use tools/make_marker.py to print the markers.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

ARUCO = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


@dataclass
class PlateConfig:
    width_mm: float = 400
    height_mm: float = 400
    marker_mm: float = 50          # printed side length of each black marker square
    inset_mm: float = 12            # gap from plate edge to marker (white border around each marker)
    px_per_mm: float = 2.5          # resolution of the top-down view
    min_mm: float = 5               # ignore anything smaller (rain splash, debris)
    max_mm: float = 120
    min_circularity: float = 0.45
    min_contrast: float = 45        # how much brighter than the plate a stone must be (0-255)

    def marker_origin(self, mid):
        """Top-left corner (mm) of marker id 0..3 at the plate's TL, TR, BR, BL corners."""
        m, w, h, i = self.marker_mm, self.width_mm, self.height_mm, self.inset_mm
        return {0: (i, i), 1: (w - m - i, i), 2: (w - m - i, h - m - i), 3: (i, h - m - i)}.get(mid)


@dataclass
class Stone:
    x: float      # plate mm
    y: float
    d_mm: float   # longest dimension (enclosing circle)
    eq_mm: float  # equal-area diameter
    first_seen: float
    last_seen: float
    id: int


@dataclass
class FrameResult:
    ok: bool
    ts: float
    markers: int = 0
    stones: list = field(default_factory=list)   # stones visible in this frame
    new: int = 0                                 # stones first seen in this frame
    error: str | None = None
    annotated: np.ndarray | None = None


class HailDetector:
    def __init__(self, cfg: PlateConfig):
        self.cfg = cfg
        self.params = cv2.aruco.DetectorParameters()
        self.params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(ARUCO, self.params)
        self.reset()

    def reset(self):
        """Start a new hail event: forget tracked stones and totals."""
        self.tracks: list[Stone] = []
        self.next_id = 1
        self.total_new = 0
        self.max_mm = 0.0
        self.sizes: list[float] = []     # size of every counted stone (first sighting)
        self.started = None
        self.last_H = None               # last good homography (if markers are briefly hidden by ice/water)

    # ------------------------------------------------------------------ geometry
    def homography(self, gray):
        corners, ids, _ = self.detector.detectMarkers(gray)
        if ids is None:
            return None, 0
        src, dst = [], []
        s, m = self.cfg.px_per_mm, self.cfg.marker_mm
        for c, i in zip(corners, ids.flatten()):
            o = self.cfg.marker_origin(int(i))
            if o is None:
                continue
            ox, oy = o
            src.extend(c.reshape(4, 2))
            dst.extend([((ox) * s, (oy) * s), ((ox + m) * s, (oy) * s),
                        ((ox + m) * s, (oy + m) * s), ((ox) * s, (oy + m) * s)])
        n = len(src) // 4
        if n == 0:
            return None, 0
        H, _ = cv2.findHomography(np.float32(src), np.float32(dst), cv2.RANSAC if n > 1 else 0, 3.0)
        return H, n

    # ------------------------------------------------------------------ detection
    def analyze(self, frame_bgr: np.ndarray, ts: float | None = None, annotate=True) -> FrameResult:
        ts = ts or time.time()
        cfg, s = self.cfg, self.cfg.px_per_mm
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
        H, n = self.homography(gray)
        if H is None:
            if self.last_H is None:
                return FrameResult(False, ts, 0, error="plate markers not visible")
            H = self.last_H
        else:
            self.last_H = H
        W, Hh = int(cfg.width_mm * s), int(cfg.height_mm * s)
        top = cv2.warpPerspective(gray, H, (W, Hh), flags=cv2.INTER_LINEAR, borderValue=0)
        valid = cv2.warpPerspective(np.full_like(gray, 255), H, (W, Hh), flags=cv2.INTER_NEAREST, borderValue=0)

        # mask out the markers (plus a margin) and anything outside the camera's view
        mask = (valid > 0).astype(np.uint8) * 255
        pad = int((cfg.inset_mm + 3) * s)
        for i in range(4):
            ox, oy = cfg.marker_origin(i)
            cv2.rectangle(mask, (int(ox * s) - pad, int(oy * s) - pad),
                          (int((ox + cfg.marker_mm) * s) + pad, int((oy + cfg.marker_mm) * s) + pad), 0, -1)
        mask = cv2.erode(mask, np.ones((int(4 * s), int(4 * s)), np.uint8))

        # stones are bright on a dark plate: threshold against the plate's own brightness
        blur = cv2.GaussianBlur(top, (0, 0), 0.6 * s)
        plate = blur[mask > 0]
        if plate.size == 0:
            return FrameResult(False, ts, n, error="plate not in view")
        base = float(np.median(plate))
        mad = float(np.median(np.abs(plate - base))) + 1
        thr = min(250, base + max(cfg.min_contrast, 6 * mad))
        bw = ((blur > thr) & (mask > 0)).astype(np.uint8) * 255
        k = max(3, int(1.2 * s)) | 1
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))

        # split touching stones with a distance-transform watershed
        stones_px = self._split(bw)

        seen = []
        for cnt in stones_px:
            area = cv2.contourArea(cnt)
            if area <= 0:
                continue
            per = cv2.arcLength(cnt, True) or 1
            circ = 4 * math.pi * area / (per * per)
            (cx, cy), r = cv2.minEnclosingCircle(cnt)
            eq = 2 * math.sqrt(area / math.pi) / s
            d = min(2 * r / s, eq * 1.6)  # longest dimension, capped against odd shapes
            if eq < cfg.min_mm or d > cfg.max_mm or circ < cfg.min_circularity:
                continue
            seen.append((cx / s, cy / s, d, eq, cnt))

        new = self._track(seen, ts)
        res = FrameResult(True, ts, n, stones=[dict(x=x, y=y, d_mm=d, eq_mm=e) for x, y, d, e, _ in seen], new=new)
        if annotate:
            res.annotated = self._draw(top, mask, seen, n, ts)
        return res

    def _split(self, bw):
        if not bw.any():
            return []
        dist = cv2.distanceTransform(bw, cv2.DIST_L2, 5)
        k = max(3, int(4 * self.cfg.px_per_mm)) | 1  # peaks must be ~4 mm apart to count as separate stones
        local_max = dist >= cv2.dilate(dist, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) - 0.5
        peaks = local_max & (dist > 0.5 * self.cfg.min_mm * self.cfg.px_per_mm)
        nlab, markers = cv2.connectedComponents(peaks.astype(np.uint8))
        if nlab > 2:
            # merge peaks that sit inside each other's inscribed circle (ridges of oval stones)
            pts = []
            for lab in range(1, nlab):
                ys, xs = np.nonzero(markers == lab)
                pts.append((xs.mean(), ys.mean(), float(dist[ys, xs].max())))
            parent = list(range(len(pts)))
            find = lambda i: i if parent[i] == i else find(parent[i])
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    if math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) < max(pts[i][2], pts[j][2]):
                        parent[find(j)] = find(i)
            remap = np.zeros(nlab, np.int32)
            for lab in range(1, nlab):
                remap[lab] = find(lab - 1) + 1
            markers = remap[markers]
            nlab = len(set(remap[1:])) + 1
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if nlab <= 2:
            return contours
        out = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            sub = markers[y:y + h, x:x + w]
            ids = np.unique(sub[sub > 0])
            if len(ids) <= 1:
                out.append(c)
                continue
            region = np.zeros((h, w), np.uint8)
            cv2.drawContours(region, [c - [x, y]], -1, 255, -1)
            mk = np.where(region > 0, 0, 0).astype(np.int32)
            for j, lab in enumerate(ids, 1):
                mk[sub == lab] = j + 1
            mk[region == 0] = 1
            color = cv2.cvtColor(region, cv2.COLOR_GRAY2BGR)
            cv2.watershed(color, mk)
            for j in range(2, len(ids) + 2):
                part = ((mk == j) & (region > 0)).astype(np.uint8) * 255
                cs, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                out.extend(cc + [x, y] for cc in cs)
        return out

    def _track(self, seen, ts):
        """Match blobs to stones seen before; unmatched blobs are new stones."""
        if self.started is None and seen:
            self.started = ts
        new, used = 0, set()
        for x, y, d, e, _ in seen:
            best, bd = None, 1e9
            for t in self.tracks:
                if t.id in used:
                    continue
                dist = math.hypot(t.x - x, t.y - y)
                if dist < max(6, 0.6 * max(d, t.d_mm)) and dist < bd:
                    best, bd = t, dist
            if best:
                used.add(best.id)
                best.x, best.y, best.last_seen = x, y, ts
                best.d_mm = max(best.d_mm, d)
            else:
                st = Stone(x, y, d, e, ts, ts, self.next_id)
                self.next_id += 1
                self.tracks.append(st)
                used.add(st.id)
                new += 1
                self.total_new += 1
                self.sizes.append(d)
            self.max_mm = max(self.max_mm, d)
        # stones melt or get washed off: forget tracks unseen for 20 s
        self.tracks = [t for t in self.tracks if ts - t.last_seen < 20]
        return new

    def _draw(self, top, mask, seen, n, ts):
        img = cv2.cvtColor(top, cv2.COLOR_GRAY2BGR)
        img[mask == 0] = (img[mask == 0] * 0.35).astype(np.uint8)
        s = self.cfg.px_per_mm
        for x, y, d, e, cnt in seen:
            cv2.drawContours(img, [cnt], -1, (0, 200, 255), 2)
            cv2.putText(img, f"{d:.0f}mm", (int(x * s + d * s / 2 + 4), int(y * s)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 200, 255), 1, cv2.LINE_AA)
        # 25 mm scale bar
        y0 = img.shape[0] - 14
        cv2.line(img, (14, y0), (14 + int(25 * s), y0), (255, 255, 255), 3)
        cv2.putText(img, "25 mm", (14, y0 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        txt = f"{len(seen)} visible  {self.total_new} counted  max {self.max_mm:.0f} mm  markers {n}/4"
        cv2.putText(img, txt, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        return img

    # ------------------------------------------------------------------ summary
    def summary(self):
        area_ft2 = self.cfg.width_mm * self.cfg.height_mm / 92903.04
        sz = sorted(self.sizes)
        return dict(count=self.total_new, max_mm=round(self.max_mm, 1),
                    median_mm=round(sz[len(sz) // 2], 1) if sz else None,
                    per_ft2=round(self.total_new / area_ft2, 1) if area_ft2 else None,
                    started=self.started, plate_ft2=round(area_ft2, 2),
                    histogram=size_histogram(sz))


HAIL_NAMES = ((114, "softball"), (70, "baseball"), (64, "tennis ball"), (44, "golf ball"), (38, "ping pong ball"),
              (32, "half dollar"), (25, "quarter"), (22, "nickel"), (19, "penny"), (13, "marble"), (6, "pea"))


def hail_label(mm):
    for size, name in HAIL_NAMES:
        if mm >= size:
            return name
    return "small"


def size_histogram(sizes):
    edges = [5, 10, 15, 20, 25, 32, 44, 64, 1000]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        out.append(dict(lo=lo, hi=None if hi == 1000 else hi, n=sum(lo <= x < hi for x in sizes)))
    return out


# ---------------------------------------------------------------------------- Reolink capture

class ReolinkCamera:
    """Grabs JPEG snapshots from a Reolink camera over its local HTTP API (or an RTSP URL)."""

    def __init__(self, http, host=None, user="admin", password="", channel=0, https=False, rtsp=None):
        self.http, self.host, self.user, self.password = http, host, user, password
        self.channel, self.rtsp = channel, rtsp
        self.base = f"{'https' if https else 'http'}://{host}/cgi-bin/api.cgi" if host else None
        self.token, self.token_exp = None, 0
        self._cap = None

    async def _login(self):
        body = [{"cmd": "Login", "param": {"User": {"Version": "0", "userName": self.user, "password": self.password}}}]
        async with self.http.post(f"{self.base}?cmd=Login", json=body, ssl=False) as r:
            res = await r.json(content_type=None)
        tok = (res[0].get("value") or {}).get("Token") if res else None
        if not tok:
            raise RuntimeError(f"Reolink login failed: {(res[0].get('error') or {}).get('detail') if res else r.status}")
        self.token, self.token_exp = tok["name"], time.time() + int(tok.get("leaseTime", 3600)) - 60

    async def snapshot(self) -> np.ndarray:
        if self.rtsp:
            import asyncio
            return await asyncio.get_running_loop().run_in_executor(None, self._rtsp_frame)
        for attempt in range(2):
            if not self.token or time.time() > self.token_exp:
                await self._login()
            params = dict(cmd="Snap", channel=str(self.channel), rs=f"{time.time():.0f}", token=self.token)
            async with self.http.get(self.base, params=params, ssl=False) as r:
                data = await r.read()
                ctype = r.headers.get("Content-Type", "")
            if "image" in ctype or data[:2] == b"\xff\xd8":
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    return img
            self.token = None  # JSON error, usually an expired token: log in again once
        raise RuntimeError(f"Reolink snapshot failed: {data[:160]!r}")

    def _rtsp_frame(self):
        if self._cap is None or not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self.rtsp, cv2.CAP_FFMPEG)
        for _ in range(5):  # drain buffered frames so we get a current one
            self._cap.grab()
        ok, frame = self._cap.retrieve()
        if not ok:
            self._cap.release()
            self._cap = None
            raise RuntimeError("RTSP frame grab failed")
        return frame
