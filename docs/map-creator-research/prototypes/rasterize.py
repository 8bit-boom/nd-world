"""Turns the SVGs make_sheets.js wrote into PNGs with the Chromium that ships with the sandbox.   python3 rasterize.py OUT_DIR
Each SVG is shown inside a small HTML page (a bare .svg document makes headless Chromium hang on a full-page screenshot)."""
import os
import sys

from playwright.sync_api import sync_playwright

out = sys.argv[1]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
    for name in sorted(f for f in os.listdir(out) if f.endswith(".svg")):
        svg = open(os.path.join(out, name), encoding="utf-8").read()
        page = b.new_page(viewport={"width": 1800, "height": 1300})
        page.set_content('<!doctype html><body style="margin:0;background:#111">' + svg + "</body>")
        page.wait_for_timeout(300)
        el = page.locator("svg").first
        el.screenshot(path=os.path.join(out, name[:-4] + ".png"), timeout=120000)
        page.close()
        print("wrote", name[:-4] + ".png", flush=True)
    b.close()
