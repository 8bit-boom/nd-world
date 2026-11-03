#!/usr/bin/env python3
"""Page times on a BIG world: a scratch database gets N entities (names that link to each other, a few paragraphs each), then the main pages are timed one request
at a time (median / p90 over --rounds) against every server given, and the entities are removed again.

    python scripts/bench_bigworld.py --db tests/_data/world.db --world world-a --email gm@test.local --password '...' \
        --server python=http://127.0.0.1:8951 --server hybrid=http://127.0.0.1:8952 --server rust=http://127.0.0.1:8953 [--entities 700] [--rounds 25]

Only for a scratch / test database (never your live data folder): the rows it adds are marked summary='bench-bulk' and deleted at the end (also on Ctrl+C).
A small world hides the cost of anything that grows with the number of entities (the auto-linker, list pages); this is the baseline the speed plan is measured with."""
import argparse
import datetime
import random
import sqlite3
import statistics
import time

import httpx

WORDS = "ash court harbour lantern moon market tide raven bell thorn salt glass".split()


def fill(db, world, n):
    c = sqlite3.connect(db)
    wid = c.execute("select id from worlds where slug=?", (world,)).fetchone()[0]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rnd = random.Random(7)
    names = [f"Bulk {rnd.choice(WORDS).title()} {i}" for i in range(n)]
    for name in names:
        body = " ".join(f"{rnd.choice(names)} met the {rnd.choice(WORDS)}, and {rnd.choice(names)} did not." for _ in range(30))
        c.execute("insert into entities(world_id,kind,name,body,summary,visible_to_players,created_at,updated_at) values (?,?,?,?,?,1,?,?)",
                  (wid, "character", name, body, "bench-bulk", now, now))
    c.commit()
    first = c.execute("select id from entities where summary='bench-bulk' order by id limit 1").fetchone()[0]
    c.close()
    return first


def clean(db):
    c = sqlite3.connect(db)
    n = c.execute("delete from entities where summary='bench-bulk'").rowcount
    c.commit()
    c.close()
    return n


def time_page(base, path, cookies, rounds):
    with httpx.Client(base_url=base, cookies=cookies, timeout=60, follow_redirects=False) as cl:
        for _ in range(3):
            cl.get(path)
        lat, size = [], 0
        for _ in range(rounds):
            t = time.perf_counter()
            r = cl.get(path)
            lat.append((time.perf_counter() - t) * 1000)
            size = len(r.content)
            if r.status_code != 200:
                return None
    lat.sort()
    return statistics.median(lat), lat[int(len(lat) * 0.9) - 1], size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--world", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--server", action="append", required=True, help="name=url, repeatable")
    ap.add_argument("--entities", type=int, default=700)
    ap.add_argument("--rounds", type=int, default=25)
    a = ap.parse_args()
    servers = [s.split("=", 1) for s in a.server]
    first = fill(a.db, a.world, a.entities)
    try:
        pages = [f"/?w={a.world}", f"/kind/character?w={a.world}", f"/entity/{first}?w={a.world}", f"/entity/{first + a.entities // 2}?w={a.world}", f"/rules?w={a.world}", f"/search?q=bulk&w={a.world}"]
        jars = {}
        for name, url in servers:
            with httpx.Client(base_url=url) as c:
                c.post("/login", data={"email": a.email, "password": a.password, "next": "/"}, follow_redirects=False)
                jars[name] = dict(c.cookies)
        print(f"{a.entities} entities; median / p90 ms over {a.rounds} requests (one at a time)")
        print(f"{'page':44}" + "".join(f"{name:>22}" for name, _ in servers))
        for page in pages:
            row = f"{page.split('?')[0] + (' ' + page.split('=')[-1] if 'entity' in page else ''):44}"
            for name, url in servers:
                r = time_page(url, page, jars[name], a.rounds)
                row += f"{'-' if r is None else f'{r[0]:7.1f} / {r[1]:6.1f} ({r[2] // 1024} KB)':>22}"
            print(row)
    finally:
        print("removed", clean(a.db), "entities")


if __name__ == "__main__":
    main()
