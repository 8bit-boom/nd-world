#!/usr/bin/env python3
"""Golden responses: do two servers answer the same requests the same way? (docs/RUST_REWRITE_PLAN.md, phases 0-6)

    python scripts/golden.py crawl   --base http://127.0.0.1:8000 --email gm@x --password '...' --out routes.txt [--max 400]
    python scripts/golden.py compare --a http://127.0.0.1:8000 --b http://127.0.0.1:8100 --routes routes.txt --email gm@x --password '...'
    python scripts/golden.py compare ... --role player --email p@x --password '...'     # the same pages as another role

`crawl` signs in, starts at a few pages and follows the links it finds (same site, GET, no logout / delete), so the list comes from the real data:
every entity, session, map, quest ... the account can reach. `compare` asks both servers (they must serve the SAME database, so ids match) for
each path with their own login and reports every difference in status, content type, redirect target and body. Bodies are normalised first:
HTML whitespace and attribute order, JSON key order, and the things that legitimately differ between two requests (timestamps, tokens - see
IGNORE). Exit status 1 when anything differs, so it can gate a phase of the Rust migration; --allow lists paths with a known, accepted difference.

Read-only: only GET requests are sent. Pass `--email` / `--password` for an account of the role being checked (the permission matrix runs this once
per role: GM, player, assistant, and logged out with --anonymous)."""
import argparse
import difflib
import json
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

SKIP_PATH = re.compile(r"^/(logout|worlds/switch/|static/|uploads/|api/live|api/display/stream|api/chronicler|api/ai/|__test__|map-tiles/)|/(delete|download|export|stream)\b|\.(zip|pdf|mp3|mp4|webm|png|jpe?g|webp|avif|gif|svg|ics)(\?|$)")
START = ["/", "/rules", "/sessions", "/facts", "/maps", "/calendar", "/characters", "/quests", "/tables", "/dice", "/gallery", "/images", "/audio",
         "/video", "/search?q=a", "/account", "/settings", "/worlds", "/combat", "/parties", "/boards", "/handouts", "/schedule", "/session-log"]

# Differences that are not differences: a value that changes with every request or with the clock.
IGNORE = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?"), "<time>"),            # ISO timestamps
    (re.compile(r"\b\d+ (seconds?|minutes?|hours?|days?) ago\b"), "<ago>"),
    (re.compile(r'(name="csrf[^"]*" value=")[^"]*'), r"\1<token>"),
    (re.compile(r"\bnonce-[A-Za-z0-9+/=_-]{8,}"), "nonce-<n>"),
    (re.compile(r'"(ts|updated_at|created_at|server_time|now)":\s*"[^"]*"'), r'"\1": "<time>"'),
    (re.compile(r'"(ts|updated_at|created_at|server_time|now|uptime|elapsed_ms|ms)":\s*[0-9.]+'), r'"\1": <n>'),
]


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])


PER_SHAPE = 6        # pages collected per route shape ("/entity/{n}"): enough to cover a kind of page, not every row of the data


def shape(path: str) -> str:
    """The route a path belongs to: ids and the query string folded away (`/entity/3?x=1` -> `/entity/{n}`)."""
    return re.sub(r"\d+", "{n}", path.split("?", 1)[0])


def _plain(path: str) -> str:
    """A discovered link without the `w` (world) parameter - the active world comes from the cookie, the same page otherwise."""
    base, _, query = path.partition("?")
    keep = [q for q in query.split("&") if q and not q.startswith("w=")]
    return base + (("?" + "&".join(keep)) if keep else "")


def links(html: str, base: str) -> list:
    p = _Links()
    try:
        p.feed(html)
    except Exception:
        pass
    out = []
    for h in p.hrefs:
        u = urldefrag(urljoin(base, h))[0]
        pr = urlparse(u)
        if pr.scheme in ("", "http", "https") and not pr.netloc.startswith("http") and (not pr.netloc or pr.netloc == urlparse(base).netloc):
            out.append(pr.path + (("?" + pr.query) if pr.query else ""))
    return out


# ── normalising ──────────────────────────────────────────────────────────────

class _Canon(HTMLParser):
    """HTML as a list of tokens: attributes sorted, whitespace between and inside text collapsed, comments dropped."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._raw = 0

    def handle_starttag(self, tag, attrs):
        self.out.append("<" + tag + "".join(f' {k}="{v}"' for k, v in sorted(attrs, key=lambda kv: kv[0])) + ">")
        if tag in ("script", "style", "pre", "textarea"):
            self._raw += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in ("script", "style", "pre", "textarea"):
            self._raw -= 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "pre", "textarea") and self._raw:
            self._raw -= 1
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        t = data if self._raw else " ".join(data.split())
        if t.strip() or self._raw:
            self.out.append(t)


def _ignore(text: str) -> str:
    for rx, rep in IGNORE:
        text = rx.sub(rep, text)
    return text


def normalise(content_type: str, body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        try:
            return _ignore(json.dumps(json.loads(text), sort_keys=True, indent=1, ensure_ascii=False))
        except ValueError:
            pass
    if ct in ("text/html", "application/xhtml+xml"):
        c = _Canon()
        try:
            c.feed(text)
            c.close()
        except Exception:
            return _ignore(text)
        return _ignore("\n".join(c.out))
    return _ignore(text)


# ── talking to a server ──────────────────────────────────────────────────────

def login(client: httpx.Client, email: str, password: str) -> None:
    r = client.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False)
    if r.status_code not in (302, 303):
        raise SystemExit(f"login failed on {client.base_url} ({r.status_code})")


def snapshot(client: httpx.Client, path: str) -> dict:
    r = client.get(path, follow_redirects=False)
    return {"status": r.status_code, "type": r.headers.get("content-type", "").split(";")[0],
            "location": r.headers.get("location", ""), "body": normalise(r.headers.get("content-type", ""), r.content)}


def crawl(client: httpx.Client, limit: int = 400, per_shape: int = PER_SHAPE) -> list:
    seen, queue, found, count = set(), list(START), [], {}
    while queue and len(found) < limit:
        path = _plain(queue.pop(0))
        if path in seen or SKIP_PATH.search(path):
            continue
        seen.add(path)
        sh = shape(path)
        if count.get(sh, 0) >= per_shape and path not in START:
            continue
        try:
            r = client.get(path, follow_redirects=False)
        except httpx.HTTPError:
            continue
        if r.status_code >= 400:
            continue
        found.append(path)
        count[sh] = count.get(sh, 0) + 1
        if "text/html" in r.headers.get("content-type", "") and r.status_code == 200:
            for link in links(r.text, str(client.base_url) + path):
                link = _plain(link)
                if link not in seen and not SKIP_PATH.search(link):
                    queue.append(link)
    return found


def diff_one(a: dict, b: dict, context: int = 2) -> list:
    out = []
    for k in ("status", "type", "location"):
        if a[k] != b[k]:
            out.append(f"  {k}: {a[k]!r} != {b[k]!r}")
    if a["body"] != b["body"]:
        d = list(difflib.unified_diff(a["body"].splitlines(), b["body"].splitlines(), "A", "B", n=context, lineterm=""))
        out.append("  body differs:")
        out += ["    " + line[:200] for line in d[:24]] + (["    ..."] if len(d) > 24 else [])
    return out


def compare(a: httpx.Client, b: httpx.Client, paths: list, allow=()) -> dict:
    same, differ, allowed = [], {}, []
    for path in paths:
        d = diff_one(snapshot(a, path), snapshot(b, path))
        if not d:
            same.append(path)
        elif any(re.search(p, path) for p in allow):
            allowed.append(path)
        else:
            differ[path] = d
    return {"same": same, "differ": differ, "allowed": allowed}


def _client(base: str, args) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=120)
    if not args.anonymous:
        login(c, args.email, args.password)
    if args.world:
        c.cookies.set("active_world", args.world)
    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("crawl", "compare"):
        sp = sub.add_parser(name)
        sp.add_argument("--email", default="")
        sp.add_argument("--password", default="")
        sp.add_argument("--anonymous", action="store_true", help="do not sign in")
        sp.add_argument("--world", default="", help="active_world cookie (a world's slug)")
    c = sub.choices["crawl"]
    c.add_argument("--base", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--max", type=int, default=400)
    k = sub.choices["compare"]
    k.add_argument("--a", required=True)
    k.add_argument("--b", required=True)
    k.add_argument("--routes", required=True, help="one path per line (from `crawl`)")
    k.add_argument("--allow", action="append", default=[], help="regex of paths whose difference is accepted")
    args = ap.parse_args(argv)
    if args.cmd == "crawl":
        paths = crawl(_client(args.base, args), args.max)
        open(args.out, "w").write("\n".join(paths) + "\n")
        print(f"{len(paths)} pages found -> {args.out}")
        return 0
    paths = [p.strip() for p in open(args.routes) if p.strip() and not p.startswith("#")]
    res = compare(_client(args.a, args), _client(args.b, args), paths, args.allow)
    for path, lines in res["differ"].items():
        print(path)
        print("\n".join(lines))
    print(f"\n{len(res['same'])} identical, {len(res['differ'])} different, {len(res['allowed'])} accepted differences (of {len(paths)})")
    return 1 if res["differ"] else 0


if __name__ == "__main__":
    sys.exit(main())
