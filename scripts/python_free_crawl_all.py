#!/usr/bin/env python3
"""Open EVERY detail page of EVERY list in a copy of your data (entities, characters, sessions, combats, quests, parties, maps, boards, albums, tables,
templates, world pages, kinds) on the Python-free gateway and report what answers 501 ("Not available here"), with the gateway's own DECLINE lines.

    python3 scripts/python_free_crawl_all.py --db /path/to/COPY/world.db --email you@example.org --password '...' [--world-slug neon-dragons]
          [--image ghcr.io/8bit-boom/nd-world-gateway:latest | --binary rust/target/release/nd-gateway] [--port 8991]

--db must be a COPY (restore a backup.zip into an empty folder, or cp the data folder): the gateway opens it read-write. The gateway is started
for you: with --image through Docker (the folder of the copy mounted as /data), or with --binary directly. Needs only the Python standard library.
Prints, per page kind: how many answered 200 / 404 / 501 / other, the first failures (id and name), and every DECLINE line the gateway logged."""
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
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    env = dict(os.environ, DB_PATH=db_path, ND_PYTHON_FREE="1", ND_DECLINE_TRACE="1", GM_EMAIL=a.email, GM_PASSWORD=a.password,
               ND_GATEWAY_LISTEN=f"127.0.0.1:{a.port}", ND_UPSTREAM="127.0.0.1:1", SECRET_KEY="crawl-only-key",
               ND_STATIC_DIR=os.path.join(repo, "static"), ND_TEMPLATES_DIR=os.path.join(repo, "app", "templates"), ND_BUNDLED_MAPS_DIR=os.path.join(repo, "app", "maps"),
               ND_RUST_ROUTES="login,dice_roll,live,pages,writes,ai,mcp,fans,auth", ND_PBKDF2_ITERATIONS=os.environ.get("ND_PBKDF2_ITERATIONS", "1000"))
    if a.image:
        cname = f"nd-crawl-{os.getpid()}"
        cmd = ["docker", "run", "--rm", "--name", cname, "-p", f"127.0.0.1:{a.port}:8000", "-v", f"{data_dir}:/data", "-e", "DB_PATH=/data/" + os.path.basename(db_path),
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
        # (table, key column, label column, [page paths with {k}])
        PLAN = [
            ("entities", "id", "name", ["/entity/{k}", "/entity/{k}/edit", "/entity/{k}/download.md", "/handout/{k}"]),
            ("player_characters", "id", "name", ["/characters/{k}", "/characters/{k}/edit", "/characters/{k}/export.json", "/characters/{k}/export.md",
                                                 "/characters/{k}/export.foundry.json", "/characters/{k}/export.ndc"]),
            ("game_sessions", "id", "title", ["/sessions/{k}", "/session-log/{k}", "/sessions/{k}/summary.md", "/sessions/{k}/transcript.md", "/sessions/{k}/npc-candidates"]),
            ("combat_sessions", "id", "name", ["/combat/{k}", "/display/combat/{k}"]),
            ("quests", "id", "title", ["/quests/{k}"]),
            ("parties", "id", "name", ["/parties/{k}", "/parties/{k}/roster", "/parties/{k}/summary", "/parties/{k}/export.ndc"]),
            ("schematics", "slug", "name", ["/maps/{k}", "/maps/schematic/{k}", "/maps/schematic/{k}/view", "/maps/schematic/{k}/view.json", "/maps/schematic/{k}/rooms.json",
                                            "/maps/schematic/{k}/walls.json", "/maps/schematic/{k}/preview.svg"]),
            ("invest_boards", "slug", "name", ["/boards/{k}", "/boards/{k}/export", "/boards/{k}/notes"]),
            ("image_albums", "id", "name", ["/images/albums/{k}"]),
            ("audio_albums", "id", "name", ["/audio/albums/{k}"]),
            ("video_albums", "id", "name", ["/video/albums/{k}"]),
            ("random_tables", "id", "name", ["/tables/{k}/edit"]),
            ("entity_templates", "id", "name", ["/entity-templates/{k}/edit"]),
            ("sheet_templates", "id", "name", ["/characters/templates/{k}/edit", "/characters/templates/{k}/export.json"]),
            ("worlds", "id", "name", ["/worlds/{k}/edit", "/worlds/{k}/home/edit", "/worlds/{k}/kinds/edit", "/worlds/{k}/rules/edit", "/worlds/{k}/export"]),
        ]
        have = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
        from collections import Counter, defaultdict
        total = Counter()
        failures = defaultdict(list)
        for table, key, label, paths in PLAN:
            if table not in have:
                continue
            cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
            where = " where world_id = ?" if "world_id" in cols else ""
            lab = label if label in cols else key
            rows = con.execute(f"select {key}, {lab} from {table}{where}", (wid,) if where else ()).fetchall()
            for k, name in rows:
                for pat in paths:
                    code = get(pat.format(k=urllib.parse.quote(str(k))))
                    total[(pat, code)] += 1
                    if code not in (200, 302, 303):
                        failures[pat].append((code, k, name))
        kinds = [r[0] for r in con.execute("select distinct kind from entities where world_id = ?", (wid,)) if r[0]]
        for k in kinds:
            code = get(f"/kind/{urllib.parse.quote(k)}")
            total[("/kind/{k}", code)] += 1
            if code != 200:
                failures["/kind/{k}"].append((code, k, k))
        print(f"world {slug!r}")
        pats = []
        for (p, c), n in total.items():
            if p not in pats:
                pats.append(p)
        for p in pats:
            counts = {c: n for (pp, c), n in total.items() if pp == p}
            print(f"  {p:<46} " + "  ".join(f"{c}:{n}" for c, n in sorted(counts.items())))
        for p, items in failures.items():
            print(f"FAILING {p}  ({len(items)})")
            for code, k, name in items[:8]:
                print(f"    {code}  {k}  {str(name)[:50]}")
    finally:
        if a.image:
            subprocess.run(["docker", "stop", cname], capture_output=True)
        proc.terminate()
        time.sleep(1)
    log.flush()
    lines = [l.rstrip() for l in open(log.name, errors="replace") if "DECLINE" in l]
    from collections import Counter as C2
    print("gateway log, where it gave up:")
    for l, n in C2(lines).most_common(25):
        print(f"  {n:5d} x {l}")
    print("full log:", log.name)


main()
