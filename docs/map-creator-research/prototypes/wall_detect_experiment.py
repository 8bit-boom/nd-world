"""Can walls be found in a battle-map PICTURE? One real map with ground truth: a Dungeondraft export whose .dd2vtt holds the image AND the true walls.

    python3 -I wall_detect_experiment.py /path/to/sampleMap.dd2vtt      (numpy, scipy, Pillow - research only, not app requirements)

Method: draw the true `line_of_sight` walls into a mask; build a "detected wall" mask from the picture; precision = share of detected pixels within
a quarter square of a true wall, recall = share of true wall pixels within a quarter square of a detection. Two naive detectors: strong edges, and dark ink.
The sample is a stylised wood-floor room whose planks are drawn with the same black ink as its walls - a hard case, and a real one.
"""
import base64
import io
import json
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

d = json.load(open(sys.argv[1]))
img = Image.open(io.BytesIO(base64.b64decode(d["image"]))).convert("RGB")
ppg = d["resolution"]["pixels_per_grid"]
ox, oy = d["resolution"]["map_origin"]["x"], d["resolution"]["map_origin"]["y"]
S = 2                                                         # work at half size
W, H = img.size[0] // S, img.size[1] // S
gray = np.asarray(img.resize((W, H), Image.LANCZOS)).astype(float).mean(axis=2)
cell = ppg // S
tol = 0.25 * cell

gt_img = Image.new("L", (W, H), 0)
dr = ImageDraw.Draw(gt_img)
for line in d["line_of_sight"]:
    dr.line([((p["x"] - ox) * cell, (p["y"] - oy) * cell) for p in line], fill=255, width=1)
gt = np.asarray(gt_img) > 0
dt_gt = ndi.distance_transform_edt(~gt)
print(f"map {W}x{H} px at half size, {int(gt.sum())} true wall pixels, tolerance {tol:.0f} px (a quarter square)")


def score(mask, name):
    dt_det = ndi.distance_transform_edt(~mask)
    prec = float((dt_gt[mask] <= tol).mean()) if mask.any() else 0.0
    rec = float((dt_det[gt] <= tol).mean())
    print(f"  {name:44s} {int(mask.sum()):7d} px   precision {prec * 100:5.1f}%   recall {rec * 100:5.1f}%")


sx, sy = ndi.sobel(gray, axis=1), ndi.sobel(gray, axis=0)
mag = np.hypot(sx, sy)
print("edges (Sobel gradient, keep the strongest N%):")
for pct in (80, 90, 95, 98):
    score(mag > np.percentile(mag, pct), f"strongest {100 - pct}% of the gradient")
print("dark ink (grey level below a threshold):")
for thr in (30, 50, 70):
    score(gray < thr, f"grey < {thr}")
