#!/usr/bin/env python3
"""Open EVERY entity page of a copy of your data on the Python-free gateway and report the ones that answer "Not available here" (501), with the
line of the gateway's log that says where it gave up.

    python3 scripts/python_free_entity_crawl.py --db /path/to/COPY/world.db --email you@example.org --password '...' [--world-slug neon-dragons]
          [--image ghcr.io/8bit-boom/nd-world-gateway:latest | --binary rust/target/release/nd-gateway] [--port 8991]

--db must be a COPY (restore a backup.zip into an empty folder, or cp the data folder): the gateway opens it read-write. The gateway is started
for you: with --image through Docker (the folder of the copy mounted as /data), or with --binary directly. Needs only the Python standard library.
Prints: how many entity pages answered 200, and for each failing entity its id, kind and name, plus every 'DECLINE' line the gateway logged."""
import argparse, http.cookiejar, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--world-slug", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--binary", default="")
    ap.add_argument("--port", type=int, default=8991)
    a = ap.parse_args()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.abspath(a.db)
    data_dir = os.path.dirname(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    worlds = con.execute("select id, slug, name from worlds").fetchall()
    slug = a.world_slug or (worlds[0][1] if worlds else "")
    wid = next((w[0] for w in worlds if w[1] == slug), None)
    ents = con.execute("select id, kind, name from entities where world_id = ?", (wid,)).fetchall()
    print(f"world {slug!r}: {len(ents)} entities")
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    env = dict(os.environ, DB_PATH=db_path, ND_PYTHON_FREE="1", ND_DECLINE_TRACE="1", GM_EMAIL=a.email, GM_PASSWORD=a.password,
               ND_GATEWAY_LISTEN=f"127.0.0.1:{a.port}", ND_UPSTREAM="127.0.0.1:1", SECRET_KEY="crawl-only-key",
               ND_STATIC_DIR=os.path.join(repo, "static"), ND_TEMPLATES_DIR=os.path.join(repo, "app", "templates"), ND_BUNDLED_MAPS_DIR=os.path.join(repo, "app", "maps"),
               ND_RUST_ROUTES="login,dice_roll,live,pages,writes,ai,mcp,fans,auth", ND_PBKDF2_ITERATIONS=os.environ.get("ND_PBKDF2_ITERATIONS", "1000"))
    if a.image:
        name = f"nd-entity-crawl-{os.getpid()}"
        cmd = ["docker", "run", "--rm", "--name", name, "-p", f"127.0.0.1:{a.port}:8000", "-v", f"{data_dir}:/data", "-e", "DB_PATH=/data/" + os.path.basename(db_path),
               "-e", "ND_PYTHON_FREE=1", "-e", "ND_DECLINE_TRACE=1", "-e", f"GM_EMAIL={a.email}", "-e", f"GM_PASSWORD={a.password}", "-e", "SECRET_KEY=crawl-only-key", a.image]
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    elif a.binary:
        proc = subprocess.Popen([a.binary], env=env, stdout=log, stderr=subprocess.STDOUT)
    else:
        sys.exit("give --image or --binary")
    base = f"http://127.0.0.1:{a.port}"
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *x):
            return None
    op_nr = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

    def get(path):
        try:
            with op_nr.open(base + path, timeout=60) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return -1
    try:
        for _ in range(120):
            if get("/login") == 200:
                break
            time.sleep(0.5)
        else:
            sys.exit("the gateway did not start; see " + log.name)
        data = urllib.parse.urlencode({"email": a.email, "password": a.password, "next": "/"}).encode()
        try:
            op_nr.open(urllib.request.Request(base + "/login", data=data), timeout=60)
        except urllib.error.HTTPError:
            pass
        jar.set_cookie(http.cookiejar.Cookie(0, "active_world", slug, None, False, "127.0.0.1", False, False, "/", True, False, None, False, None, None, {}))
        bad = []
        ok = 0
        for eid, kind, name in ents:
            code = get(f"/entity/{eid}")
            if code == 200:
                ok += 1
            else:
                bad.append((eid, kind, name, code))
        print(f"{ok} of {len(ents)} entity pages answered 200")
        for eid, kind, name, code in bad[:60]:
            print(f"  {code}  /entity/{eid}  [{kind}]  {name}")
        if len(bad) > 60:
            print(f"  ... and {len(bad) - 60} more")
    finally:
        if a.image:
            subprocess.run(["docker", "stop", name], capture_output=True)
        proc.terminate()
        time.sleep(1)
    log.flush()
    lines = [l.rstrip() for l in open(log.name, errors="replace") if "DECLINE" in l]
    from collections import Counter
    print("gateway log, where it gave up:")
    for l, n in Counter(lines).most_common(15):
        print(f"  {n:5d} x {l}")
    print("full log:", log.name)


main()
