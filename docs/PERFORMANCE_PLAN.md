# Making nd-world faster - plan

Status: BUILT (section 7 has what was done, what was measured, and what was not done). Written as a plan first; the numbers in section 2 are from before the fixes. It builds on the research note the user supplied (`nd-world_performance_research.md`,
reviewed at `87ca2a9`) and adds my own measurements on `4db7dac`. Known setup: one container (`uvicorn app.main:app`, one process, Python
3.12, SQLite in WAL mode on `/data`), the GM on a laptop (often with the cockpit open), 3-6 players on phones, and AI jobs (Ollama, image
generation, transcription) on the same host.

## 1. How it was measured

- **Data**: a temporary database seeded with 5,000 entities (about 2.4 KB of body text each, 20 % GM-only), 5,000 entity notes, 150
  sessions with recaps, 3,000 facts, one GM and one player. That is a large world, not an extreme one.
- **Single requests**: in-process `TestClient` (no network), median of 5 after a warm-up, with the number of SQL statements counted.
- **CPU breakdown**: `yappi` across all threads (the routes run in worker threads, so plain cProfile sees nothing).
- **Concurrency**: a real `uvicorn` with `httpx` clients on the same machine.
- **Not measured**: the real host (TrueNAS, disks), the network or Cloudflare path, a real phone browser, AI jobs running at the same
  time. Section 6 covers those.

## 2. What I found (most important first)

### A. Under load the server deadlocks on its database connection pool (critical)
16 browsers loading the home page at once gave **2.7 requests/s with a median of 30 s per page**, and the log shows
`QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00`. One page alone takes 50 ms.

Cause: each page render holds **two database connections at the same time**:
- the route's own `get_db` session stays open while the template renders, and
- the template context processor `_kinds_context_processor` (`app/templating.py:49`) and `saved_view` (`app/templating.py:137`) each open
  a **new** `SessionLocal()` during that render (plus `auth_gate`'s own session before it, but that one is closed first).

The SQLAlchemy pool has 15 connections and FastAPI has 40 worker threads. With 8 or more page renders in flight, every thread holds one
connection and waits for a second one, and nothing moves until the 30 s pool timeout. The GM cockpit (several iframe windows, each a full
page) plus a few players refreshing can reach this.

### B. A few pages do work proportional to the size of the world, several times over
| Page (GM) | Time | Size | Why |
|---|---|---|---|
| `/facts` (3,000 facts) | **4.1 s** (9 s when two load at once) | **25 MB** HTML | every fact's inline edit form repeats a `<select>` of all 150 sessions (450,000 `<option>`s), and every fact is rendered through markdown |
| `/sessions/<id>` | 560 ms | 477 KB | `NPC_CANDIDATES` embeds every NPC as JSON (a 319 KB inline script) |
| `/settings` | 350 ms | 1.7 MB | the Visibility tab renders one table row with inputs per entity (5,000 rows) |
| `/maps` | 415 ms (player: 18 ms) | 87 KB | GM-only: `all_world_image_urls` scans every entity for images on each visit for the gallery picker |
| `/api/entities/picker` | 158 ms | 325 KB | loads full `Entity` rows to return 4 fields, the whole world every time (finding 1 in the research note) |
| `/kind/npc` | 54 ms | 202 KB | fine now; grows linearly with the kind |
| home, entity page, search, rules, sessions list | 20-75 ms, 5-17 queries | 60-110 KB | fine |

### C. 267 of the 362 `async def` routes do blocking database work on the event loop
FastAPI runs a plain `def` route in a worker thread, but an `async def` route runs **on the event loop**, so a slow SQLite call or file
write inside one stops every other request, including SSE streams and static files, until it finishes. `auth_gate` (an `async` middleware,
`app/main.py:1081`) also runs its user and membership queries on the loop, for every request. In the test, a cheap API call took 5 ms
alone, 266-418 ms while two `/facts` pages rendered, and static CSS took up to 185 ms while four `/settings` pages rendered.

### D. Where the CPU goes on a normal page
`yappi`, per page type:
- **Home**: SQLite 24 %, SQLAlchemy ORM 23 %, stdlib (gzip, regex) 21 %, app code 16 %, Jinja 4 %.
- **Entity list**: Jinja 32 %, ORM 18 %, SQLite 10 %, markdown/escaping 7 %.
- **`/facts`**: Jinja 41 %, markdown and escaping 27 %.

`markdown2` is pure Python and takes about 5 ms per 9 KB body, and nothing caches it. No single layer dominates, which matters for the
"rewrite it in another language" question (section 4).

### E. Smaller items
- **Static files are gzipped again on every request** (about 10-15 ms of CPU for `style.css` / `cockpit.js`); nothing is
  pre-compressed and there is no Brotli.
- **Every page carries about 35 KB of inline script from `base.html`**, which can't be cached, plus 110 KB of external JS and 152 KB of
  CSS that are cached immutably.
- **Every main window polls `/api/spotlight` every 4 s.** Embedded windows and hidden tabs already skip this, which is good.
- **Uploads are served as `Cache-Control: public, max-age=31536000, immutable`** (`app/main.py:1512`) even though `/uploads/` needs a
  login. Behind Cloudflare, image extensions are edge-cached by default, so a GM-only picture could be served from the edge to anyone who
  has the URL. This is a privacy fix as much as a speed one; the research note flagged it too.
- **SQLite tuning is defaults apart from WAL / `busy_timeout` / `synchronous=NORMAL`.** Page cache is 2 MB, with no `mmap_size` and no
  `temp_store=MEMORY`. Indexes are mostly right. The only extra sort is `ORDER BY kind, folder, name` (a temp B-tree), which is cheap at
  5,000 rows.
- **Tooling**: there is no per-route timing or query count in production, so a slow night can't be diagnosed after the fact.

## 3. The plan (ordered by value / effort)

| # | Change | Fixes | Effort | Expected effect |
|---|---|---|---|---|
| 1 | **Stop the pool deadlock**: the context processor and `saved_view` reuse the request's session (`request.state.db`, set by `get_db`) instead of opening their own; cache per request what `saved_view` reads (one query for all of a viewer's layouts, as `docs/SITE_LAYOUTS_PLAN.md` §5.3 already planned); raise the pool to match the thread count (`pool_size=20, max_overflow=20`, or size it from the threadpool) and lower `pool_timeout` to about 5 s so a fault shows as an error, not a 30 s hang. Add a regression test that renders 30 pages concurrently. | A | 0.5-1 day | 16 concurrent clients: from 2.7 req/s at 30 s down to about 50 ms each (roughly 100-300 req/s) |
| 2 | **`/facts`**: one shared hidden `<select>` (or `<template>`) cloned when a fact is edited, instead of one per fact; paginate or virtualise by session (open groups load their facts on demand); cache each fact's rendered markdown | B | 0.5-1 day | 25 MB to under 300 KB, 4 s to under 100 ms |
| 3 | **The other heavy pages**: `/sessions/<id>` loads NPC candidates from an API when the NPC box opens; `/settings` Visibility loads entity rows per kind / page (or with a search); `/maps` and the forms load the gallery picker's image list on demand (`GET /api/gallery/picker`), plus a per-world cache of `all_world_image_urls` invalidated on writes; the entity picker returns only the 4 columns (`with_entities`, as in the research note) with an `ETag` so the browser re-uses it, later a server-side search for huge worlds | B | 1-2 days | Each of these pages to under 60 ms and under 120 KB |
| 4 | **Get blocking work off the event loop**: turn every `async def` route that doesn't `await` anything into `def` (an AST scan finds them; a test keeps it that way); in the rest, wrap DB and file work in `run_in_threadpool`; make `auth_gate` a pure ASGI middleware whose DB lookup runs in a thread (or caches the user in the signed session for about 60 s, as `site_layout` already does) | C | 2-3 days (mechanical, many files) | A slow page no longer stalls SSE, static files or other users |
| 5 | **Cache what is the same for every request**: rendered markdown (an LRU keyed by a text hash, about 2k entries); nav menus and effective kinds per world (invalidate when the world is edited); `get_world_ctx` per request instead of per call | D | 1 day | 10-30 % off typical pages |
| 6 | **Static delivery**: pre-compress `static/` to `.br` and `.gz` at image build time and serve those (no per-request gzip); move `base.html`'s 35 KB of inline script into a versioned `nd-base.js`, which is cacheable; optionally minify JS/CSS in the Docker build (esbuild, no source change) | E | 1 day | About 30 % less transferred per page view; the CPU of the gzip-again work goes away |
| 7 | **Upload cache headers**: `private, max-age=…` for authenticated uploads (keep `immutable` only for content-addressed names), `no-store` where retention matters; versioned map image URLs | E | 0.5 day | Correct under Cloudflare; no stale or leaked images |
| 8 | **SQLite tuning, measured**: `cache_size=-65536` (64 MB), `mmap_size=268435456`, `temp_store=MEMORY`, `PRAGMA optimize` on close; check with `EXPLAIN QUERY PLAN` on the real database; test `(world_id, kind, folder, name)` only if the picker sort shows up | E | 0.5 day | Small (5-15 %) on reads; matters more on a slow disk |
| 9 | **Runtime upgrades**: Python 3.12 to 3.13 (the Docker base image); `orjson` responses for the large JSON APIs; check that `uvicorn[standard]` really uses `uvloop` / `httptools` in the image | D | 0.5-1 day + a test run | 5-15 % CPU; JSON 2-5x faster to encode |
| 10 | **Measure all the time**: a timing middleware recording route, status, duration, SQL count and SQL time (no bodies, no secrets) into a small ring buffer shown on the existing System Monitor page; a slow-request log above 500 ms; a `scripts/perf_smoke.py` (the seed + timing harness used for this plan) run in CI with budgets per page (e.g. `/facts` under 300 ms for 3,000 facts) | all | 1 day | Regressions caught in CI; a slow evening can be diagnosed |
| 11 | **Browser side, after 1-10**: trace the GM's usual cockpit layout in Chromium (requests, JS heap, long tasks); lazy-mount heavy editors; one shared live connection for all cockpit windows (the parent relays to its iframes) instead of per-window polling or SSE; `fetchpriority="high"` on the one hero image, lazy-load the rest | interaction | 2-3 days | The cockpit stays smooth with 6+ windows |
| 12 | **AI contention**: run the stress matrix from the research note (browsing while chat, image, transcription and model load run); if browsing degrades, cap AI concurrency to leave cores free, use `nice` / cgroup CPU shares for the Ollama and image containers, and make sure no DB transaction stays open across an AI call | contention | 1-2 days | Table night stays responsive while the AI works |

**Suggested order**: 1, then 2 and 3 (the measured disasters), then 10 (so everything after is measured), then 4, 5, 6, 7, 8, 9, and
11 and 12 with real traces from your own machine.

## 4. Rewriting in another language - assessment

What the measurements say: once the algorithmic problems are fixed, a page costs **20-75 ms in Python**, spread across SQLite, the ORM,
Jinja and markdown. The disasters (30 s waits, 4 s pages, 25 MB HTML) come from **what the code does** (holding two connections, O(n×m)
HTML, blocking the event loop), not from Python being slow. A rewrite would carry the same mistakes unless they are fixed anyway.

| Option | What it is | Gain | Cost / risk | Verdict |
|---|---|---|---|---|
| **Stay in Python, fix the hot paths** (section 3) | | 10-1000x on the bad pages, about 2x typical | Weeks | **Do this** |
| Faster Python runtime | 3.13 now; free-threaded 3.14 later (only after measuring, since C extensions must support it) | 5-15 % | Low | Yes, item 9 |
| Native pieces from Python | Rust/C libraries for the hot pure functions: markdown (`markdown2` to a C/Rust CommonMark such as `cmarkgfm` or `pyromark`, about 10-50x, but the output must match today's extras and `safe_mode` escaping exactly, which needs a golden-output test), `orjson` for JSON (`nh3` for sanitising is already Rust) | Markdown-heavy pages 20-40 % faster | Medium: rendering differences | Yes, after the cache in item 5, if markdown is still visible in profiles |
| Hot routes in a sidecar service (Go or Rust) | e.g. a small Go server for SSE / live events and static files in front of the Python app | Only helps with hundreds of live connections | A second service, auth shared by two codebases | Not needed at table scale (under 20 users) |
| Full backend rewrite (Go, Rust, TypeScript/Node, C#) | 68,000 lines of Python, 745 routes, about 6,300 tests, Jinja templates, AI job code, MCP server | Maybe 3-10x on CPU-bound rendering; nothing on AI, disk or network | **12-18 months** solo; a long freeze on features; every security and visibility rule re-implemented (GM-only content, player safety), where a regression leaks secrets to players | **No** |
| Frontend rewrite (React / Svelte SPA) | | Snappier navigation after the first load | Rebuild every page; the server must become a JSON API; SEO / no-JS paths lost; the current payload problems would move into JS bundles | No; fix payloads and lazy-load instead (items 2, 3, 6, 11) |
| Change the database (PostgreSQL) | | Concurrent writers, multiple app hosts | Migration, a second container, backups | Only if item 10 shows persistent write contention; SQLite is not the bottleneck today |
| More processes (`--workers 4`) | | Uses more cores | Process-local caches, job semaphores, SSE fan-out and the site-layout session cache all assume one process | Only after 1-4, and only with a shared job/notification layer |

## 5. Acceptance checks for every step
- The full test suite passes, plus a new performance test for the item: query budget, size budget, or concurrency.
- Before / after numbers from `scripts/perf_smoke.py` on the 5,000-entity seed, put in the commit message.
- GM, player, assistant and owner views still show exactly what they did, with no hidden content (the existing visibility tests, plus a
  diff of the rendered HTML for a player before and after on the seed).

## 6. What still needs measuring on your machine
1. The real host: `docker stats`, how big the database is, disk type (local dataset vs NFS/SMB), and the Cloudflare path (does
   compression survive it? what does `cf-cache-status` show on `/uploads/`?).
2. Timings over the LAN and the public URL with your cookie, using the `curl -w` line from the research note, for home, a big entity list,
   an entity, search, maps, and your usual cockpit layout.
3. A Chromium trace of your usual cockpit layout during a game night.
4. The stress matrix with AI jobs running (research note, "Isolate AI contention").


## 7. What was built, and what it measured

Same machine, same seeded world (5,000 entities, 3,000 facts, 150 sessions), same harness (`scratchpad` copy of `scripts/perf_smoke.py`'s method).
Single requests are the median of 5 in-process runs; the load numbers are a real `uvicorn` with `httpx` clients on the same 4-core machine.

| Page (GM) | Before | After |
|---|---|---|
| `/facts` (3,000 facts) | 4,604 ms, 25 MB | **64 ms, 262 KB** |
| `/sessions/<id>` | 621 ms, 477 KB | **67 ms, 241 KB** (about 200 KB of that is the page's own script) |
| `/maps` | 569 ms | **52 ms** |
| `/settings` | 244 ms, 1.7 MB | **20 ms, 137 KB** |
| `/api/entities/picker` | 167 ms | **50 ms** (and a 304 with an ETag when nothing changed) |
| `/` (home) | 66 ms, 80 KB | 58 ms, 57 KB |
| entity page, lists, search, rules | 30-55 ms | 13-48 ms (10-30 % faster; 20-30 KB smaller) |

| Under load (home page, 16 browsers at once) | Before | After |
|---|---|---|
| Pages per second | 2.7 (and `QueuePool limit ... reached` errors) | **26** |
| Median wait | 30 s (the pool timeout) | **0.6 s** |
| A cheap API call while two `/facts` pages render | 266 ms (worst 418) | **10 ms** (worst 15) |
| Static CSS while four `/settings` pages render | worst 185 ms | worst 18 ms |

### Step by step
1. **Pool deadlock - done.** A request holds one connection (`get_db` parks the session on `request.state.db`; the template context processor, `saved_view` and the site layout read through it; every saved layout of a viewer is read in one query). The pool is 20 + 20 with a 10 s timeout. `tests/test_db_pool_budget.py` fails on the old code (2 connections at once) and passes now.
2. **`/facts` - done.** One shared session list for the edit forms, the first group inline and the other groups fetched from `GET /facts/fragment` (all of them in one request when you search, expand all, or pick Timeline / Table), plus an LRU cache of rendered markdown.
3. **The other heavy pages - done.** The NPC picker list loads on first use (`GET /sessions/{id}/npc-candidates`; saved picks ride along as hidden fields until it does, so saving never drops them), Settings > Visibility rows load when the tab opens (`GET /settings/visibility-rows`), the gallery scan reads plain columns and only text that contains an image, the entity picker reads four columns with an ETag.
4. **Blocking work off the event loop - done in part.** The login/membership lookup on every request now runs in the worker threadpool; 30 route handlers that contained no `await` became plain `def` (run in threads). The other 63 awaitless handlers were left alone on purpose: they start background jobs with `asyncio.create_task`, stream, or call helpers that need the event loop, and a thread has none. 179 handlers that `await request.form()` / `.json()` and then do database work were not touched (splitting them is a larger, riskier change). **New in this step:** the render gate - at most 2 HTML page requests are built at once (`ND_RENDERS`); building a page holds Python's GIL, and more threads only took turns slowly (home page at 16 browsers: 6 pages/s with 8-40 threads, 28 with 2).
5. **Caches - partly.** Rendered markdown is cached; nav menus and kinds were already cached per request or flag-cached, and measured as cheap, so they were not changed.
6. **Static delivery - done.** `python -m app.precompress` (run in the Docker build) writes `.br` / `.gz` twins and `PerfMiddleware` serves them directly (no per-request compression); about 24 KB of inline script moved out of `base.html` into four cacheable files. Not measured end to end on a real phone.
7. **Upload cache headers - done.** `private, max-age=31536000, immutable` for uploads (a shared cache such as Cloudflare must not keep a login-only picture), `private, no-cache` for map images.
8. **SQLite tuning - done, measured neutral.** 16 MB page cache per connection, 256 MB `mmap_size`, in-memory temp storage. Removing them changed nothing measurable on this machine; they matter more on a slow disk and cost memory only as pages are read.
9. **Runtime - done.** Python 3.13 in the Docker image and in CI (the suite passes on 3.13 except two tests that are skipped on 3.11 and fail on 3.13 only because of a pytest plugin mismatch in my scratch environment). `uvloop` and `httptools` were already in use. **Not done:** `orjson` (changing every JSON response is a bigger risk than the gain).
10. **Measure all the time - done.** `app/perf_log.py` (route template, status, milliseconds, SQL count and database time; no URLs, query strings, names or bodies) behind `GET /api/perf/summary` and `/api/perf/recent`, a table on the System Monitor page, a slow-request log line above `ND_SLOW_MS` (800), `tests/test_perf_budgets.py` (size and query budgets on a seeded world) and `scripts/perf_smoke.py` for your own host.
11. **Browser side - measured, nothing to change.** An idle cockpit with one window made 13 requests in 30 s (spotlight every 4 s, dice history every 5 s, both already paused in a hidden tab). I could not trace six or more live windows here, so a real trace of your normal layout is still worth doing.
12. **AI contention - not measurable here.** There is no AI backend in this environment. `scripts/perf_smoke.py` is the tool to run three times on your server (AI idle, chat generating, image generating); what I could do from the code is size the pool for the worker threads and keep a page from holding a second connection, which is done.

### Not done, and why
- The stress matrix with real AI jobs (no AI here), a real-phone or Cloudflare timing run, and a real-host `docker stats` reading.
- `orjson`, `--workers N`, PostgreSQL, a faster markdown library: the measurements did not call for them.
- Splitting the 107 KB inline script of the session page into a cacheable file (it contains template values).
