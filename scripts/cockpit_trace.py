#!/usr/bin/env python3
"""Trace what a cockpit with many windows asks the server for (docs/LEFTOVERS_PLAN.md, step 9).

    python scripts/cockpit_trace.py --base http://127.0.0.1:8000 --email you@example.com --password '...' [--world slug] [--seconds 60]
                                    [--windows 1,3,6,10] [--chromium /path/to/chrome]

For each window count N it (1) lays out a cockpit with N windows of different kinds and opens it in one tab, then (2) opens the cockpit
plus N-1 ordinary pages in separate tabs of the same browser (pop-outs), and records every request each scenario makes for --seconds
(after the first load settles). It prints requests per minute per endpoint (numbers in paths folded to {n}) and how many live-stream
connections (/api/live) were opened - the design is ONE per browser, whatever the number of windows or tabs (static/js/nd-live.js elects a
leader tab and relays over BroadcastChannel; frames never connect themselves).

It needs a GM login. The world's saved cockpit layout is read first and put back at the end, even when the run fails; nothing else is
written. Needs Playwright (pip install playwright) and a Chromium."""
import argparse
import collections
import json
import re
import sys
import time

import httpx

# window kinds the cockpit offers (app/routers/cockpit.py PANEL_TYPES); "page" windows show an ordinary page in a frame
KINDS = [("dice", ""), ("party", ""), ("combat", ""), ("notes", ""), ("calendar", ""), ("quests", ""), ("timer", ""), ("tables", ""),
         ("page", "/rules"), ("page", "/facts"), ("audio", ""), ("gallery", "")]
POPOUTS = ["/dice", "/rules", "/combat", "/calendar", "/facts", "/quests", "/tables", "/characters", "/maps", "/sessions"]


def layout(n):
    panels = []
    for i in range(n):
        kind, ref = KINDS[i % len(KINDS)]
        panels.append({"id": f"trace{i}", "type": kind, "ref": ref, "title": f"{kind} {i}", "x": 20 + (i % 4) * 330, "y": 60 + (i // 4) * 260,
                       "w": 320, "h": 250, "z": i + 1, "data": {}})
    return {"panels": panels, "vp": {"w": 1400, "h": 900}}


def fold(url, base):
    path = url.split("?", 1)[0].replace(base, "")
    return re.sub(r"/\d+(?=/|$)", "/{n}", path)


def run(pw_browser, base, cookies, urls, seconds, settle=4.0):
    ctx = pw_browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies(cookies)
    seen, live = collections.Counter(), {"n": 0, "closed": 0}
    started = {"t": None}

    def on_request(req):
        if not req.url.startswith(base) or "/static/" in req.url:
            return
        p = fold(req.url, base)
        if p == "/api/live":
            live["n"] += 1
        if started["t"] is not None:
            seen[p] += 1
    ctx.on("request", on_request)

    def on_done(req):
        if req.url.startswith(base) and fold(req.url, base) == "/api/live":
            live["closed"] += 1
    ctx.on("requestfinished", on_done)
    ctx.on("requestfailed", on_done)
    pages = []
    for u in urls:
        pg = ctx.new_page()
        pg.goto(base + u, wait_until="domcontentloaded")
        pages.append(pg)
    pages[0].bring_to_front()
    time.sleep(settle)
    started["t"] = time.time()
    time.sleep(seconds)
    still_open = live["n"] - live["closed"]
    ctx.close()
    per_min = {k: round(v * 60.0 / seconds, 1) for k, v in seen.most_common()}
    return per_min, live["n"], still_open


def main(a):
    from playwright.sync_api import sync_playwright
    http = httpx.Client(base_url=a.base, timeout=30, follow_redirects=False)
    r = http.post("/login", data={"email": a.email, "password": a.password})
    if r.status_code not in (200, 302, 303) or not http.cookies:
        sys.exit(f"login failed ({r.status_code})")
    if a.world:
        http.cookies.set("active_world", a.world)
    saved = http.get("/api/cockpit/workspace")
    if saved.status_code != 200:
        sys.exit(f"cannot read the cockpit layout ({saved.status_code}) - is this a GM login?")
    original = saved.json().get("workspace")
    host = httpx.URL(a.base).host
    cookies = [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in http.cookies.items()]
    rows = []
    try:
        with sync_playwright() as p:
            kw = {"args": ["--no-sandbox"]}
            if a.chromium:
                kw["executable_path"] = a.chromium
            browser = p.chromium.launch(**kw)
            for n in [int(x) for x in a.windows.split(",")]:
                ws = {"current": layout(n), "presets": (original or {}).get("presets") or {}}
                if http.post("/api/cockpit/workspace", json=ws).status_code != 200:
                    sys.exit("could not save the trace layout")
                for scenario, urls in (("cockpit, %d windows" % n, ["/cockpit"]),
                                       ("cockpit + %d pop-out tabs" % (n - 1), ["/cockpit"] + [POPOUTS[i % len(POPOUTS)] for i in range(n - 1)])):
                    if scenario.endswith("+ 0 pop-out tabs"):
                        continue
                    per_min, live, still_open = run(browser, a.base, cookies, urls, a.seconds)
                    total = round(sum(per_min.values()), 1)
                    rows.append((scenario, live, still_open, total, per_min))
                    print(f"\n{scenario}: {live} live connection(s) opened, {still_open} still open at the end, {total} requests/min after load")
                    for k, v in list(per_min.items())[:12]:
                        print(f"    {v:7.1f}/min  {k}")
            browser.close()
    finally:
        http.post("/api/cockpit/workspace", json=original if isinstance(original, dict) and "current" in original else {"current": {"panels": []}, "presets": {}})
    if a.json:
        print(json.dumps(rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--world", default="")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--windows", default="1,3,6,10")
    ap.add_argument("--chromium", default="")
    ap.add_argument("--json", action="store_true")
    main(ap.parse_args())
