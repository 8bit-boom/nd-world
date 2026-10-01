"""Phone-width audit for the Player Characters / Parties / character-hub pages.

Logs in as a player or the GM on a running instance, visits each page with an iPhone UA
and touch emulation, and reports what the app's mobile CSS hides: content clipped past the
viewport (html/body use overflow-x:clip, so overflow is invisible), tap targets under 40px,
inputs under 16px (iOS zooms into those) and tiny checkboxes. Saves full-page screenshots.

    python3 scripts/phone_audit.py [width height player|gm [page,page,...]]

Environment: AUDIT_BASE (default http://127.0.0.1:8000), AUDIT_PLAYER / AUDIT_GM as
"email:password", AUDIT_SHOTS (screenshot directory, default ./phone-audit-shots),
AUDIT_MAX (rows printed per category, default 14). The page list assumes the demo data
the audit was written against (character 1 = native N&D sheet, 3 = custom sheet, party 1);
edit PAGES for your own world. File inputs are reported for height only: iOS does not zoom
into them, so their font size is not a real problem.
"""
import json, os, re, sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("AUDIT_BASE", "http://127.0.0.1:8000")
SP = os.environ.get("AUDIT_SHOTS", "phone-audit-shots")
os.makedirs(SP, exist_ok=True)
W, H = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (375, 667)
WHO = sys.argv[3] if len(sys.argv) > 3 else "player"
ONLY = sys.argv[4].split(",") if len(sys.argv) > 4 else None
CREDS = {w: tuple(os.environ.get("AUDIT_" + w.upper(), d).split(":", 1))
         for w, d in (("player", "player@e2e.local:player-e2e-12345"), ("gm", "gm@e2e.local:gm-e2e-12345"))}

PAGES = {
 "player": [("chars-list", "/characters"), ("sheet", "/characters/1"), ("hub-journey", "/characters/1#hub-journey"),
            ("hub-quests", "/characters/1#hub-quests"), ("hub-notes", "/characters/1#hub-notes"),
            ("hub-world", "/characters/1#hub-world"), ("hub-schedule", "/characters/1#hub-schedule"),
            ("custom-sheet", "/characters/3"), ("edit-form", "/characters/1/edit"), ("new-form", "/characters/new"),
            ("parties-list", "/parties"), ("party", "/parties/1"), ("party-summary", "/parties/1/summary"),
            ("party-roster", "/parties/1/roster")],
 "gm": [("gm-party", "/parties/1"), ("gm-chars-list", "/characters"), ("gm-sheet", "/characters/2")],
}

AUDIT_JS = r"""
() => {
  const vw = document.documentElement.clientWidth;
  const out = {vw, scrollW: document.documentElement.scrollWidth, clipped: [], small: [], smallFont: [], tiny: []};
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
  const sel = el => { let s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id; else if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).slice(0,2).join('.'); 
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g,' ').slice(0,28); return s + (t ? ' "' + t + '"' : ''); };
  // does an ancestor scroll horizontally (so overflow is reachable)?
  const scrolls = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if ((o === 'auto' || o === 'scroll') && p.scrollWidth > p.clientWidth) return true; } return false; };
  for (const el of document.querySelectorAll('body *')) {
    if (!vis(el)) continue;
    if (el.closest('nav, .topbar, header.topbar, #nd-bug, .nd-float-btn')) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 && !scrolls(el)) out.clipped.push([sel(el), Math.round(r.left), Math.round(r.right)]);
  }
  for (const el of document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [onclick]')) {
    if (!vis(el) || el.closest('nav, .topbar, header.topbar')) continue;
    if (el.tagName === 'A' && !el.textContent.trim() && !el.querySelector('img,svg')) continue;
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (el.type === 'checkbox' || el.type === 'radio') { if (r.height < 20) out.tiny.push([sel(el), Math.round(r.width), Math.round(r.height)]); continue; }
    // inline text links inside paragraphs are exempt from the height rule
    const inline = el.tagName === 'A' && cs.display === 'inline';
    if (!inline && (r.height < 40 || r.width < 40) && !(el.tagName === 'A' && r.width >= 80 && r.height >= 32)) out.small.push([sel(el), Math.round(r.width), Math.round(r.height)]);
    if (['INPUT','SELECT','TEXTAREA'].includes(el.tagName) && el.type !== 'file' && parseFloat(cs.fontSize) < 16) out.smallFont.push([sel(el), cs.fontSize]);
  }
  return out;
}
"""

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("AUDIT_CHROMIUM") or None)
    ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, is_mobile=True, has_touch=True,
                        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    pg = ctx.new_page(); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
    pg.goto(f"{BASE}/login"); pg.fill('input[name="email"]', CREDS[WHO][0]); pg.fill('input[name="password"]', CREDS[WHO][1])
    pg.click('button[type="submit"]'); pg.wait_for_load_state("networkidle")
    total = 0
    for name, path in PAGES[WHO]:
        if ONLY and name not in ONLY: continue
        errs.clear()
        r = pg.goto(BASE + path); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(500)
        if r is not None and r.status != 200 and "#" not in path:
            print(f"\n### {name} {path}: HTTP {r.status}"); continue
        res = pg.evaluate(AUDIT_JS)
        pg.screenshot(path=f"{SP}/{W}-{WHO}-{name}.png", full_page=True)
        issues = len(res["clipped"]) + len(res["small"]) + len(res["smallFont"]) + len(res["tiny"]) + (1 if res["scrollW"] > res["vw"] + 1 else 0)
        total += issues
        print(f"\n### {name} {path}  vw={res['vw']} scrollW={res['scrollW']}  issues={issues} jserrors={errs}")
        for k in ("clipped", "small", "smallFont", "tiny"):
            seen = set()
            for row in res[k]:
                key = row[0]
                if key in seen: continue
                seen.add(key)
                if len(seen) > int(os.environ.get("AUDIT_MAX","14")): print(f"   ... {len(res[k]) - 14}+ more {k}"); break
                print(f"   {k:9s} {row}")
    print("\nTOTAL ISSUES:", total)
    b.close()
