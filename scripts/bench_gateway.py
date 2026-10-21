#!/usr/bin/env python3
"""Requests per second for a few kinds of request, the app directly and through the Rust gateway (docs/RUST_GATEWAY.md).

    python scripts/bench_gateway.py --direct http://127.0.0.1:8001 --gateway http://127.0.0.1:8000 [--email gm@x --password '...'] [--seconds 8]

Each request kind is hammered by --clients concurrent connections for --seconds; prints requests/s and the 50th / 99th percentile latency.
Needs an upload to ask for: pass --upload /uploads/<path> (a picture already on the server); without --email the login page stands in for a page."""
import argparse
import asyncio
import statistics
import time

import httpx


async def hammer(base, path, headers, clients, seconds, cookies):
    lat, errors, stop = [], 0, time.perf_counter() + seconds

    async def one():
        nonlocal errors
        async with httpx.AsyncClient(base_url=base, timeout=60, cookies=cookies) as c:
            while time.perf_counter() < stop:
                t = time.perf_counter()
                try:
                    r = await c.get(path, headers=headers, follow_redirects=False)
                    await r.aread()
                    if r.status_code >= 500:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(clients)])
    took = time.perf_counter() - t0
    lat.sort()
    return {"rps": len(lat) / took, "p50": statistics.median(lat), "p99": lat[int(len(lat) * 0.99) - 1] if len(lat) > 100 else lat[-1], "errors": errors}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--upload", default="")
    ap.add_argument("--static", default="/static/style.css")
    ap.add_argument("--clients", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=8)
    a = ap.parse_args()
    cookies = {}
    if a.email:
        async with httpx.AsyncClient(base_url=a.direct) as c:
            r = await c.post("/login", data={"email": a.email, "password": a.password, "next": "/"}, follow_redirects=False)
            cookies = dict(c.cookies)
    kinds = [("static file (style.css)", a.static, {"Accept-Encoding": "identity"}), ("page", "/" if a.email else "/login", {"Accept": "text/html"})]
    if a.upload:
        kinds.insert(1, ("upload", a.upload, {}))
    print(f"{'':28}{'':>9}{'req/s':>9}{'p50 ms':>9}{'p99 ms':>9}{'errors':>8}")
    for name, path, headers in kinds:
        for label, base in (("direct", a.direct), ("gateway", a.gateway)):
            r = await hammer(base, path, headers, a.clients, a.seconds, cookies)
            print(f"{name:28}{label:>9}{r['rps']:9.0f}{r['p50']:9.1f}{r['p99']:9.1f}{r['errors']:8d}")


asyncio.run(main())
