"""The expensive part of a Universal VTT / Foundry export is the IMAGE, not the walls. This measures it the way the app would do it:
in the browser, SVG -> <img> -> canvas -> PNG -> base64, then writes a real .dd2vtt and checks it with the same reader the tests use.

    node make_sheets.js OUT 5 && python3 export_image_spike.py OUT
"""
import base64
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

out = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))
svg = open(os.path.join(out, "furnished-dungeon.svg"), encoding="utf-8").read()
meta = json.load(open(os.path.join(out, "furnished-dungeon.json")))
W_CELLS, H_CELLS = 44, 30

JS = """
async ({ svg, ppg, w, h }) => {
  const t0 = performance.now();
  const blob = new Blob([svg], { type: 'image/svg+xml' });
  const url = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
  const px = [w * ppg, h * ppg];
  const cv = document.createElement('canvas'); cv.width = px[0]; cv.height = px[1];
  const g = cv.getContext('2d'); g.drawImage(img, 0, 0, px[0], px[1]);
  const t1 = performance.now();
  const png = await new Promise(r => cv.toBlob(r, 'image/png'));
  const t2 = performance.now();
  const buf = new Uint8Array(await png.arrayBuffer());
  let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  const b64 = btoa(bin);
  const t3 = performance.now();
  return { px, drawMs: t1 - t0, pngMs: t2 - t1, b64Ms: t3 - t2, pngBytes: buf.length, b64, ok: true };
}
"""

rows = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
    page = b.new_page(viewport={"width": 800, "height": 600})
    page.set_content("<!doctype html><body></body>")
    for ppg in (30, 50, 70, 100):
        t = time.time()
        try:
            r = page.evaluate(JS, {"svg": svg.replace('width="1760"', f'width="{W_CELLS * ppg}"').replace('height="1200"', f'height="{H_CELLS * ppg}"'), "ppg": ppg, "w": W_CELLS, "h": H_CELLS})
        except Exception as e:
            rows.append({"ppg": ppg, "error": str(e)[:120]}); print(rows[-1]); continue
        # a real file, read back by the tested reader
        uv = subprocess.run(["node", "-e", """
          const U = require(process.argv[1] + '/uvtt.js'), gd = require(process.argv[1] + '/gen-dungeon.js'), gw = require(process.argv[1] + '/gridwalls.js'), F = require(process.argv[1] + '/foundry.js');
          const m = gd.generateDungeon({ w: 44, h: 30, seed: 5 }), segs = gd.wallsOf(m);
          const data = U.fromGrid(m.w, m.h, segs, gw.polylines, { ppg: Number(process.argv[2]) });
          const file = U.build(data, require('fs').readFileSync(0, 'utf8'));
          const back = U.parse(JSON.parse(JSON.stringify(file)));
          const scene = F.toScene(back, { padding: 0 });
          require('fs').writeFileSync(process.argv[3], JSON.stringify(file));
          console.log(JSON.stringify({ valid: U.validate(file), walls: scene.stats.walls, doors: scene.stats.doors, problems: F.validateScene(scene.scene), bytes: JSON.stringify(file).length }));
        """, HERE, str(ppg), os.path.join(out, f"furnished-{ppg}.dd2vtt")], input=r["b64"], capture_output=True, text=True, check=True).stdout
        info = json.loads(uv)
        row = {"ppg": ppg, "px": r["px"], "megapixels": round(r["px"][0] * r["px"][1] / 1e6, 1), "draw_ms": round(r["drawMs"]), "png_ms": round(r["pngMs"]), "base64_ms": round(r["b64Ms"]),
               "png_kb": round(r["pngBytes"] / 1024), "dd2vtt_kb": round(info["bytes"] / 1024), "errors": info["valid"]["errors"], "foundry_problems": info["problems"], "walls": info["walls"], "doors": info["doors"]}
        rows.append(row); print(json.dumps(row), flush=True)
    b.close()
