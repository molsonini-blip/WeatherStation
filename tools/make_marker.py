"""
Print the four hail-plate markers.

  python tools/make_marker.py hail_markers.pdf --marker-mm 50

Prints on US Letter at 300 dpi. Print at 100% / "Actual size" (not "Fit to page"), then check the
100 mm ruler on the page with a real ruler. Cut out each marker with its white border, laminate or tape
over it so it survives the weather, and stick them in the plate's corners:
ID 0 top-left, ID 1 top-right, ID 2 bottom-right, ID 3 bottom-left (as seen from the camera),
each with its black square HAILCAM_INSET_MM (12 mm) in from the plate edges.
"""
import argparse, os, sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from hailcam import ARUCO  # noqa: E402

DPI = 300
px = lambda mm: int(round(mm / 25.4 * DPI))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="hail_markers.pdf")
    ap.add_argument("--marker-mm", type=float, default=50)
    a = ap.parse_args()
    W, H = px(215.9), px(279.4)
    page = np.full((H, W), 255, np.uint8)
    m, border = px(a.marker_mm), px(10)
    names = ["0  TOP-LEFT", "1  TOP-RIGHT", "2  BOTTOM-RIGHT", "3  BOTTOM-LEFT"]
    for i in range(4):
        cx = px(20) + (i % 2) * (W // 2)
        cy = px(25) + (i // 2) * px(a.marker_mm + 45)
        cv2.rectangle(page, (cx - border, cy - border), (cx + m + border, cy + m + border), 170, 2)  # cut line
        page[cy:cy + m, cx:cx + m] = cv2.aruco.generateImageMarker(ARUCO, i, m)
        cv2.putText(page, f"ID {names[i]}", (cx, cy + m + border + px(6)), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
    y = H - px(40)
    x0 = px(20)
    cv2.line(page, (x0, y), (x0 + px(100), y), 0, 4)
    for k in range(11):
        cv2.line(page, (x0 + px(10 * k), y - px(4 if k % 5 else 7)), (x0 + px(10 * k), y), 0, 3)
    cv2.putText(page, "100 mm - check with a ruler; print at 100% / Actual size", (x0, y + px(9)),
                cv2.FONT_HERSHEY_SIMPLEX, 1.3, 0, 3)
    cv2.putText(page, f"Hail plate markers ({a.marker_mm:g} mm). Cut on the grey lines, keep the white border.",
                (x0, px(12)), cv2.FONT_HERSHEY_SIMPLEX, 1.3, 0, 3)
    from PIL import Image
    Image.fromarray(page).save(a.out, resolution=DPI) if a.out.endswith(".pdf") else cv2.imwrite(a.out, page)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
