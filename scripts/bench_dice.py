#!/usr/bin/env python3
"""Dice rolls per second, written by Python (direct) and by the gateway (docs/RUST_WRITES.md).
    python scripts/bench_dice.py --direct http://127.0.0.1:8001 --gateway http://127.0.0.1:8000 --email gm@x --password '...' [--seconds 8] [--clients 16]"""
import argparse
import asyncio
import statistics
import time

import httpx


async def run(base, cookies, clients, seconds):
    lat, bad, stop = [], 0, time.perf_counter() + seconds

    async def one():
        nonlocal bad
        async with httpx.AsyncClient(base_url=base, cookies=cookies, timeout=60) as c:
            while time.perf_counter() < stop:
                t = time.perf_counter()
                r = await c.post("/api/dice/roll", json={"notation": "4d6+2"})
                bad += r.status_code != 200
                lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(clients)])
    lat.sort()
    return len(lat) / (time.perf_counter() - t0), statistics.median(lat), lat[int(len(lat) * 0.99) - 1], bad


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--clients", type=int, default=16)
    a = ap.parse_args()
    async with httpx.AsyncClient(base_url=a.direct) as c:
        await c.post("/login", data={"email": a.email, "password": a.password, "next": "/"}, follow_redirects=False)
        cookies = dict(c.cookies)
    print(f"{'':10}{'rolls/s':>9}{'p50 ms':>9}{'p99 ms':>9}{'errors':>8}")
    for label, base in (("python", a.direct), ("gateway", a.gateway)):
        rps, p50, p99, bad = await run(base, cookies, a.clients, a.seconds)
        print(f"{label:10}{rps:9.0f}{p50:9.1f}{p99:9.1f}{bad:8d}")


asyncio.run(main())
