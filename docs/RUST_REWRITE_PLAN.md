# Rust (Axum) rewrite plan

Status: PLAN ONLY - nothing here is built. Written at the user's request after the question "which language would use all of the server's
cores". It describes how nd-world could move from Python (FastAPI) to Rust (Axum) without a big-bang switch, what it would cost, what it
would and would not gain, and where to stop if the gain does not show.

## 1. What a rewrite would and would not change

**Would change**
- Page building on every core: Axum runs requests on all threads; the Python interpreter lock that caps page building today
  (`app/render_gate.py`) goes away. Expect roughly 5-20x more pages per second under load and lower worst-case waits.
- Memory: ~20-40 MB for the web server instead of ~190 MB; start-up in milliseconds instead of ~2 s.
- One static binary in the image: no Python, no pip layer (144 MB today), a smaller image.
- Type checking of every request, query and template value at compile time.

**Would not change**
- The AI: Ollama, Unsloth Studio and SwarmUI already run outside nd-world (C++/CUDA on the GPU). The GPU's 16 GB is still the limit.
- Video and audio: still ffmpeg.
- Pictures: Pillow already encodes in C on 8 threads; Rust would be somewhat faster, not a different league.
- What a table of 5-10 people feels: pages already take 10-40 ms. The rewrite pays off with many simultaneous users, a much smaller
  machine, or as a long-term maintenance choice - not on today's load.

## 2. The size of the job (counted on main)

| | Count |
|---|---|
| Python application code | ~70,000 lines; `main.py` 8,300, `ai.py` 5,700, 53 routers |
| HTTP routes | 757 |
| Jinja templates | 128 (92 use `extends`, 37 `include`, 38 `from ... import` macros) + 20 custom filters / globals |
| Database models | 61 tables (SQLite), hand-written migrations in `database.py` |
| Background job systems | 7 (`audio_jobs`, `chat_jobs`, `image_jobs`, `video_jobs`, `live_jobs`, `media_rename_jobs`, `ai_queue`), with checkpoint / resume |
| Live streams (SSE) | 29 files: live updates, display screen, AI streaming, chronicler |
| Front-end | 24,000 lines of JavaScript + CSS - **kept as it is** |
| Tests | 374 files, ~100,000 lines; 49 talk to the app only over HTTP, 325 also call Python functions directly |

## 3. Strategy: replace it piece by piece, never all at once

A "strangler" migration: the Rust server goes in front and answers the routes it has been taught; everything else it passes to the
Python app behind it. Both use the same SQLite file and the same `uploads/` folder. Each step moves a group of routes and ships on its
own; at every point the site works and can be rolled back by switching a route back to Python.

```
browser ──> Rust (Axum) ──┬── routes already ported: answered here
                          └── everything else: proxied to Python (FastAPI) on 127.0.0.1
             both ──> the same SQLite database, uploads/, static/
```

Rules for the whole migration:
- **One database, one schema.** Python keeps owning migrations until the very end; Rust reads the schema as it is (no ORM rewrite of
  the data model). SQLite in WAL mode handles two processes writing; writes go through short transactions on both sides.
- **The same templates.** Rust renders the existing Jinja files with MiniJinja (Jinja-compatible, by Jinja's author), so pages look the
  same and a template change keeps working on both sides during the migration.
- **The same URLs, cookies and logins.** Session cookies (Starlette / itsdangerous signed JSON) and password hashes (PBKDF2-SHA256) are
  read and written in the same format, so nobody is logged out and both servers accept the same session.
- **Live state moves first.** What lives in Python's memory today (live-update counters, the display screen's stage, spotlight, the
  render gate) has to be shared by both processes: it moves into the database (or a tiny Rust service the Python side calls) before any
  route that uses it is ported.
- **Parity, not redesign.** A route is ported to produce the same response; improvements come after the switch, not during it.

## 4. Building blocks (Python today -> Rust)

| Need | Python today | Rust |
|---|---|---|
| Web framework | FastAPI / Starlette, uvicorn | `axum` + `tokio` + `tower-http` (compression, static files with the existing `.br` / `.gz` twins, timeouts) |
| Database | SQLAlchemy 2 + hand migrations | `sqlx` (SQLite, async, queries checked at compile time) - plain SQL, no ORM |
| Templates | Jinja2 | `minijinja` - same files; the 20 filters / globals (`thumb`, `display`, `asset_v`, markdown, view specs ...) rewritten |
| Markdown | markdown2 + extras | `comrak` (GitHub-flavoured); output differences checked by golden tests |
| HTML sanitising | nh3 (Python bindings to ammonia) | `ammonia` - the same library |
| Sessions / auth | Starlette sessions, PBKDF2, TOTP, trusted devices | `tower-sessions`-style middleware reading the itsdangerous format, `pbkdf2` + `sha2`, `totp-rs`, `qrcode` |
| Uploads | python-multipart, chunked uploads | `axum::extract::Multipart`, the same chunk protocol |
| Live updates | SSE (sse-starlette), in-memory counters | `axum::response::sse`, `tokio::sync::broadcast` |
| Pictures | Pillow (thumbnails, AVIF / WebP, tiles, display copies) | `image` + `fast_image_resize`, `ravif` / libavif, `webp` (libwebp) |
| AI back ends | httpx, ollama client, websockets | `reqwest` (streaming), `tokio-tungstenite` |
| MCP server | `mcp` (FastMCP) | `rmcp` (the official Rust SDK) |
| PDF reading / writing | pypdf / reportlab | `lopdf` + `pdf-extract` / `typst` or `printpdf` |
| HTML -> text | html2text | `html2text` crate |
| ffmpeg | subprocess | `tokio::process` - same commands |
| Background jobs | asyncio tasks + DB rows, checkpoint / resume | `tokio` tasks + the same job tables and resume rules |

## 5. Phases

Effort is for one experienced developer, full time; the ranges are honest - the AI and job code is the uncertain part.

| Phase | What | Exit criteria | Effort |
|---|---|---|---|
| 0. Groundwork (in Python) | Move the in-memory live state (live counters, signals, display stage, spotlight cache) into the database; turn the 325 test files that call Python internals into HTTP-level tests where they check behaviour, so the same suite can run against either server (`ND_TEST_BASE_URL`). Add a golden-response tool: record Python's response for a set of requests, compare another server's. | The suite runs against a live server; nothing lives only in Python's memory | 6-8 weeks |
| 1. Rust gateway | Axum binary in front: static files (with `.br` / `.gz`), `/uploads/` (thumbnail fallback, display copies), cookies / sessions / auth gate, proxy for everything else, the perf log. Docker image runs both processes. | All tests green with Rust in front; no visible change | 4-6 weeks |
| 2. Read-only pages | Templates via MiniJinja and the 20 filters; entity pages and lists, search, rules, maps viewer + tiles, calendar, facts, sessions list, home. | Golden HTML matches (normalised) for every ported page; page throughput measured | 8-12 weeks |
| 3. Edits and uploads | Forms and JSON writes for entities, sessions, facts, quests, parties, characters, galleries, audio / video libraries, chunked uploads, picture processing (thumbnails, AVIF, tiles). | Write routes ported with the same permission checks (the visibility / section-access rules are the riskiest part) | 10-14 weeks |
| 4. Live and table tools | SSE streams, live updates, display screen, cockpit, combat tracker, dice, character hub, schematic maps (walls, fog, tokens, buy / pick-up with the same locking). | Two-browser tests of every live feature; race tests pass | 8-10 weeks |
| 5. AI and jobs | AI chat / streaming, recaps, transcription pipeline (chunking, retries, checkpoint / resume), image and video jobs, media renamer, RAG / retrieval, MCP server, Ollama tuning, hardware detection, fan control. | Every job survives a restart mid-way as today; the AI audit list re-checked | 12-16 weeks |
| 6. Switch-off | Migrations move to Rust (`sqlx` migrations reproducing `database.py`'s healing), Python removed from the image, docs. | One binary; image size and memory measured | 3-4 weeks |

**Total: roughly 12-17 months** for one developer (about 51-70 weeks), or 6-9 months for two. Phases 2-5 can overlap with two people.

## 6. How correctness is kept

- **The existing suite is the contract.** Phase 0 makes it server-agnostic; every phase ends with the full suite green against the
  Rust-fronted server - the same rule as today (full suite before every push).
- **Golden responses.** For each ported route, the Python response and the Rust response to the same requests are compared (HTML
  normalised for whitespace and attribute order, JSON compared as data). A difference is either fixed or written down as intended.
- **Shadow traffic** before switching a busy route: Rust answers in parallel, its answer is compared and thrown away, Python's is sent.
- **Permissions get their own matrix**: every route x role (GM, player, assistant, logged-out) x world section access, generated from
  the route list, run against both servers.
- **Rollback per route**: the gateway's route table decides who answers; switching back is one line and a restart.

## 7. Risks

| Risk | Why | Mitigation |
|---|---|---|
| Takes longer than planned | Big rewrites usually do; the AI / job code holds many hard-won fixes (retries, timeouts, resume) | Phases ship separately; stop after any phase and keep the hybrid |
| Lost fixes | The Python code encodes years of edge cases (comments explain many of them) | Port tests first; read each module's comments as requirements |
| Two servers writing one SQLite file | Lock contention, `database is locked` | WAL, short transactions, a busy timeout on both sides; move hot writers as a group |
| Template / markdown differences | MiniJinja and comrak are close to Jinja2 and markdown2, not identical | Golden tests; small template adjustments made in Python first |
| Fewer contributors know Rust | Changes get slower to make | Keep the code plain (no clever generics), document the patterns |
| Feature freeze | New features during the migration have to be built twice or wait | Build new features in Rust once the gateway exists |
| Python-only libraries | A few have no exact Rust twin (reportlab, some markdown extras) | Keep that part in a small Python sidecar until a replacement is good enough |

## 8. Decision points (where to stop)

1. **After phase 0** (worth doing even without a rewrite: it makes the tests and the live state better). Stop if the measured load
   still never reaches the page limit.
2. **After phase 2**: compare pages per second, memory and worst-case wait under the real load (`scripts/perf_smoke.py`). If the gain
   does not matter at the table, stop - the hybrid (Rust in front for static files, uploads and read-only pages, Python for the rest) is
   a fine final state.
3. **Before phase 5**: the AI code is the most expensive to port and the least likely to get faster (it waits on the GPU). Keeping it in
   a Python service behind the Rust server is a reasonable end point.

## 9. Recommendation

Not now: today's pages are fast, the heavy work already runs outside Python, and the server's limit is the GPU's memory. If the rewrite
is wanted anyway (for the experience, a smaller footprint, or a future with many more users), do **phase 0 first** - it pays for itself
in Python - then **phase 1 and 2** and measure at decision point 2 before committing to the rest.

## 10. Phase 0 - what was done (measured)

Done in Python, valuable on its own:
- **Shared live state.** `app/shared_state.py` (table `shared_state`): the world change counter, named signals and the second screen's stage now
  live in the database. Counters are written by one background thread (never inside a request that may hold SQLite's write lock); a process sees
  its own bumps at once and another process's within ~0.3 s (`REFRESH_EVERY` 0.25 s). The stage uses compare-and-swap. A test starts a second OS
  process to prove it. Inventory of everything else that stays in Python's memory, and why that is fine: `docs/RUST_MIGRATION_STATE.md`.
- **Server-agnostic suite.** `ND_TEST_BASE_URL=http://host:port` makes the `client` fixture an httpx client against a separately started server
  (`python -m tests.serve --port N` starts one on a throw-away data dir). In-process: 6,473 passed, 2 skipped. Over HTTP: 5,985 passed, 485 skipped.
- **Golden-response tool.** `scripts/golden.py` (`crawl` then `compare`, per role, normalised HTML/JSON); the app compared with itself shows zero differences.

Not done, said plainly:
- The plan item "turn the 325 internals-calling test files into HTTP-level tests" was only **partly** done: ~487 tests (monkeypatching app modules,
  in-memory registries, cache-hit counts, the per-process perf log, the 5 s settings snapshot) are listed in `tests/inprocess_only.txt` and skipped
  in external mode. They still run in-process. Rewriting them is not needed to start phase 1, but the Rust side will not be covered by them.
- One golden test (`test_crawl_finds_...`) failed once in a full external run but passes alone; it is listed as in-process-only and the cause (a
  server-side cache across tests) is not pinned down.
- Spotlight cache and settings snapshot can be up to 2 s / 5 s stale across processes (accepted, see the state document). The failed-login limiters
  are still per process: auth POSTs must stay proxied to Python until they are shared.

## 11. Phase 1 - what was done (measured)

Built (details and safety rules: `docs/RUST_GATEWAY.md`):
- `rust/nd-gateway` (Axum, ~800 lines, a 2 MB static musl binary): `/static/` from disk (pre-compressed twins, ranges, 304, old-map redirects),
  `/uploads/` sent by the gateway after Python has done every check (`X-Nd-Accel-Path`, shared secret token, confined to the data folder),
  streaming proxy for everything else, client address set by the gateway, a "starting" page while Python is down, graceful shutdown.
- A session-cookie reader in Rust, tested against cookies signed by Python (the key is `sha1("itsdangerous.Signer" + "signer" + secret)`).
- `docker/entrypoint.sh` (gateway on 8000, Python on 127.0.0.1:8001; `ND_GATEWAY=0` = Python alone as before), multi-stage Dockerfile.
- Tests: 15 Rust tests, 5 Python tests for the hand-over, a golden comparison (pages and uploads through the gateway vs direct: identical), the
  whole Python suite over HTTP through the gateway as a CI job.

Measured on the 4-core test machine, 16 concurrent clients, one Python client program (so the static numbers are partly the client's limit):

| Request | Direct (Python) | Through the gateway |
|---|---|---|
| `/static/style.css` (177 KB) | 258 req/s, p50 57 ms | 517 req/s, p50 24 ms |
| a 2 MB upload | 16 req/s, p50 954 ms | 163 req/s, p50 88 ms |
| a page (home) | 50 req/s, p50 264 ms | 60 req/s, p50 244 ms |

The upload gap is mostly not Rust: Python's GZip middleware compresses every response over 1 KB, including pictures that do not shrink, and the
gateway sends them as they are. Pages are about as fast as before, which is expected: Python still builds them. The gateway starts to pay for
pages in phase 2, when it builds some of them itself.

Not done / not true yet:
- The gateway does not check logins for anything. `/uploads/` is protected by Python, as before. The cookie reader is unused until phase 2.
- No WebSocket proxying (the app has no WebSocket route). No HTTP/2 or TLS (the reverse proxy in front does that today).
- Not measured: the Docker image build and image size (no Docker daemon in the development sandbox; the CI build is the first real run), and
  a real browser session through the gateway. The entrypoint was run outside Docker: start, request, SIGTERM stops both processes.
- Files other than `/uploads/` that the app sends with `FileResponse` (map tiles, exports, audio/video downloads) still go through Python.
- Every upload still costs one short Python request (the access check); only the sending of the file moved to Rust.

## 12. Phase 2 - what was done (measured) and what was not

Done: the foundation and **one page**. Details and the full list of what the other pages need: `docs/RUST_PAGES.md`.
- `rust/nd-render`: MiniJinja over the app's real templates, Jinja2-exact HTML escaping, `asset_v`. All 128 templates compile in MiniJinja.
- The gateway builds `GET /login` itself (`ND_RUST_ROUTES=login`, the default): same page, same redirects as Python for 12 kinds of `next` and for
  a signed-in browser (golden test, byte-normalised HTML); the POST stays Python's. It keeps answering the form while Python is restarting.
- A per-page switch (`ND_RUST_ROUTES`), a cookie writer (`session::sign`, reproduces Python's cookie byte for byte).
- Measured (4 cores, 16 clients): the login page 316 req/s from Python, 971 req/s from the gateway (p50 46 ms -> 14 ms). It is the cheapest page the
  app has (no database), so this says the Rust path works, not what the rest will gain.

NOT done - phase 2's exit criteria are **not met**: of the ~130 pages one is ported. Every signed-in page first needs the layout context (auth
gate with roles, worlds, section access, nav menus, theme, layouts) ported against a permission matrix; that is the next piece of work and the
riskiest. The markdown renderer (`md`, `nh3` equivalent) and the other 19 template functions are also still Python. No page throughput gain for
the pages people actually use yet. Nothing else changed for users.

## 13. Phase 3 - what was done (measured) and what was not

Done: **one write route**, built as the pattern (details: `docs/RUST_WRITES.md`). `POST /api/dice/roll` is written by the gateway when
`dice_roll` is in `ND_RUST_ROUTES` (opt-in, not on by default): it re-checks session, user, world, role and section access, rolls, stores the
row and bumps the live counter in one SQLite transaction. It answers only the success case; every refusal and every unusual request goes to
Python unchanged. Checked against Python for three kinds of person and 14 request bodies, a switched-off section, a stranger, a changed
password, and 40 simultaneous rolls written by both servers. 66 -> 366 rolls/s on the 4-core test machine.

NOT done - phase 3's exit criteria are **not met**: of hundreds of write routes one is ported. Every other write needs the permission layer
the plan calls the riskiest part (route allowlists, per-row ownership, `[gmonly]` stripping) in Rust, and its own refusal-parity test; uploads
need multipart handling and the picture pipeline. The static musl build now links a C library (bundled SQLite), so the image's Rust stage
installs `gcc` / `musl-tools`; the binary grew from 2 MB to 7 MB.

### 13a. Phase 3, continued - the permission layer

`rust/nd-perm` (`docs/RUST_PERMISSIONS.md`): the route allowlists, `auth_gate`'s decision, the section matrix with custom kinds, row ownership and
`[gmonly]` / `[for]` / `[reveal]` stripping, ported and checked differentially against the Python originals (~290,000 allowlist questions over
every route, the real `_auth_gate_rest` for 6 kinds of viewer, random matrices and tagged texts). The dice write uses it. Still phase 3's
exit criteria are not met: the write routes themselves, each with its own handler checks and refusal-parity test, are still Python's.

## 14. Phase 4 - what was done (measured) and what was not

One stream: `GET /api/live` served by the gateway (opt-in, `live` in `ND_RUST_ROUTES`; `docs/RUST_LIVE.md`), the same frames as Python's,
checked side by side. Measured with 200 open streams: no gain in memory or delay over Python (whose streams were already cheap); the benefit
is that browsers stay connected through a Python restart. Phase 4's exit criteria are **not met**: the display, AI and chronicler streams,
cockpit, combat tracker, character hub and schematic maps are still Python's.

Found on the way: `tests/test_unsloth_audit_fixes.py::test_no_studio_api_key_is_committed` read every file under the repository, including
`rust/target/` (5 GB of build output), and took half an hour; it now skips `rust/target`.
