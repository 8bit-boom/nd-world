#!/usr/bin/env python3
"""Open N live streams (/api/live) at once, directly against Python and through the gateway; report how much memory each server process
grew and how long a change took to reach every stream (docs/RUST_LIVE.md).
    python scripts/bench_live.py --direct URL --gateway URL --email E --password P --py-pid PID --gw-pid PID --db PATH --world ID [--n 200]"""
import argparse
import sqlite3
import threading
import time

import httpx


def rss(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def run(base, cookies, n, bump, pids):
    seen, opened = [], threading.Barrier(n + 1)
    stop = threading.Event()

    def one():
        try:
            with httpx.Client(base_url=base, cookies=cookies, timeout=30) as c, c.stream("GET", "/api/live") as r:
                versions = 0
                first = True
                for chunk in r.iter_text():
                    versions += chunk.count("event: version")
                    if first and versions:
                        first = False
                        opened.wait()
                    if versions >= 2:
                        seen.append(time.monotonic())
                        return
                    if stop.is_set():
                        return
        except Exception:
            pass

    before = {p: rss(p) for p in pids}
    ts = [threading.Thread(target=one, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    opened.wait(timeout=60)
    during = {p: rss(p) for p in pids}
    t0 = time.monotonic()
    bump()
    for t in ts:
        t.join(timeout=10)
    stop.set()
    lat = sorted(s - t0 for s in seen)
    return {p: during[p] - before[p] for p in pids}, len(seen), (lat[len(lat) // 2] if lat else None), (lat[-1] if lat else None)


def main():
    ap = argparse.ArgumentParser()
    for k in ("direct", "gateway", "email", "password", "db"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--py-pid", type=int, required=True)
    ap.add_argument("--gw-pid", type=int, required=True)
    ap.add_argument("--world", type=int, required=True)
    ap.add_argument("--n", type=int, default=200)
    a = ap.parse_args()
    with httpx.Client(base_url=a.direct) as c:
        c.post("/login", data={"email": a.email, "password": a.password, "next": "/"}, follow_redirects=False)
        cookies = dict(c.cookies)

    def bump():
        db = sqlite3.connect(a.db, timeout=30)
        db.execute("INSERT INTO shared_state(key, n) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", (f"live:{a.world}",))
        db.commit()
        db.close()

    for label, base in (("python", a.direct), ("gateway", a.gateway)):
        grew, got, p50, worst = run(base, cookies, a.n, bump, [a.py_pid, a.gw_pid])
        print(f"{label:8} {a.n} streams: python process +{grew[a.py_pid]:.1f} MB, gateway process +{grew[a.gw_pid]:.1f} MB; "
              f"{got}/{a.n} saw the change, median {p50 and p50 * 1000:.0f} ms, last {worst and worst * 1000:.0f} ms")


main()
