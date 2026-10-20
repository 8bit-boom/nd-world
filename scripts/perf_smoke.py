#!/usr/bin/env python3
"""Measure a running nd-world from the outside (performance plan, steps 1 and 12).

    python scripts/perf_smoke.py --base http://127.0.0.1:8000 --email you@example.com --password '...' [--world my-world-slug]

It signs in like a browser, then times (1) each main page a few times - status, median and slowest milliseconds, size - (2) a cheap API call on its
own, (3) the home page with 16 browsers loading it at once, and (4) a cheap call while two heavy pages render. Run it three times to see what the
AI jobs do to browsing: with the AI idle, while a chat is generating, and while an image is being made - the numbers that change are the answer.
It only reads (GET) and prints no page content. Nothing is written to the server and the password is never stored."""
import argparse
import asyncio
import statistics
import sys
import time

import httpx

PAGES = ["/", "/rules", "/sessions", "/facts", "/search?q=a", "/maps", "/calendar", "/cockpit", "/characters", "/tables", "/dice", "/quests", "/account",
         "/api/entities/picker", "/api/spotlight"]


async def timed(c, base, path):
    t = time.perf_counter()
    r = await c.get(base + path)
    return (time.perf_counter() - t) * 1000, r.status_code, len(r.content)


async def main(a):
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as c:
        r = await c.post(a.base + "/login", data={"email": a.email, "password": a.password})
        if r.status_code not in (200, 302, 303):
            sys.exit(f"login failed ({r.status_code})")
        if a.world:
            c.cookies.set("active_world", a.world)
        print(f"{'page':26} {'status':>6} {'median ms':>10} {'worst ms':>9} {'KB':>7}")
        for p in PAGES:
            res = [await timed(c, a.base, p) for _ in range(a.repeat)]
            ms = [x[0] for x in res]
            print(f"{p:26} {res[-1][1]:>6} {statistics.median(ms):>10.0f} {max(ms):>9.0f} {res[-1][2] / 1024:>7.0f}")
        cheap = [(await timed(c, a.base, "/api/view-prefs"))[0] for _ in range(20)]
        print(f"\ncheap API alone: median {statistics.median(cheap):.0f} ms")
        stop = time.perf_counter() + a.seconds
        lat = []

        async def worker():
            while time.perf_counter() < stop:
                lat.append((await timed(c, a.base, "/"))[0])
        await asyncio.gather(*[worker() for _ in range(16)])
        lat.sort()
        print(f"home x16 at once: {len(lat) / a.seconds:.1f} pages/s, median {statistics.median(lat):.0f} ms, 95th {lat[int(len(lat) * .95)]:.0f} ms")
        heavy = asyncio.gather(*[timed(c, a.base, "/facts") for _ in range(2)])
        await asyncio.sleep(0.3)
        during = []
        for _ in range(10):
            during.append((await timed(c, a.base, "/api/view-prefs"))[0])
            await asyncio.sleep(0.1)
        await heavy
        print(f"cheap API while 2 heavy pages render: median {statistics.median(during):.0f} ms, worst {max(during):.0f} ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--world", default="")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--seconds", type=int, default=6)
    asyncio.run(main(ap.parse_args()))
