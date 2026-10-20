# Making nd-world lighter - efficiency plan

Status: BUILT - section 5 has what was done, the before / after numbers, and what was left out. It follows `docs/PERFORMANCE_PLAN.md` (built: one DB connection per request, lazy heavy lists, render gate,
pre-compressed static files). That work was about **speed under load**; this plan is about **using less**: bytes over the network (players are on
phones), browser memory, server memory and CPU, database work per request, disk, and startup. The rule for every item: **no feature is removed or
changed for the user** - the same pages, the same content, the same full-resolution images when someone asks for them.

Measured on `86c9780` with the same seeded world as before (5,000 entities, 3,000 facts, 150 sessions; a copy with a 205 KB live transcript per
session for the transcript items), Chromium for page weight, `ps` / `getrusage` for memory, `-X importtime` for startup.

## 1. What I found

### A. The biggest waste is images that are far larger than where they are shown
| Page | Downloaded on first view | Why |
|---|---|---|
| Home (`/`) | **25.8 MB** (laptop), 22.2 MB (phone) | the Maps strip shows the bundled map pictures at full size: `dreamlands_9065934.webp` is 10,368 x 6,912 (9.4 MB file, **273 MB once decoded** in the browser), the others 3,072 x 3,072 JPEGs of about 3.3 MB |
| `/maps` | **22.7 MB** (laptop), 9.4 MB (phone) | same pictures in the map cards |
| Map viewer | the whole picture, every time | Leaflet `imageOverlay` loads one full-size image; a phone has to decode up to 273 MB for the Dreamlands map |
| Races page | up to 2.9 MB per card | `static/races/*` are 1-2 K pixel JPEG/PNG/WebP, shown as 150 px cards |

The cause is one line: the `thumb` filter only makes thumbnails for `/uploads/...`; the bundled pictures under `/static/maps/` and
`/static/races/` (64 MB in the image) never get one. Every other page measured is light: 20-100 KB per view and 15-60 KB on a repeat view.

### B. Every page view makes four or five extra API calls, one of them a database write
Over five page views a GM's browser made 30 API calls; a player's made 4 per page view:
- **`POST /api/view-prefs/site` on every page view (a bug).** `nd-base-ai-status.js` calls `setTheme(current)` at load, and `ndSite.set()` saves
  it: one database write plus a re-signed session cookie per page, for nothing.
- `GET /api/hover-preview/config` (2 queries) - settings that change maybe once a month.
- `POST /api/ai/status` (GM) - cached 15 s on the server, but still a request per page.
- `GET /api/spotlight` every 4 s per open tab (1 query each) - about 900 requests per hour per tab, per person.
- Every response carries a fresh `Set-Cookie` (Starlette re-signs the session cookie whether or not it changed).

### C. List pages and every request read big text columns they never show
- With a 205 KB transcript + 217 KB segment list per session, `/sessions` went from 23 ms to **80 ms**, the home page from 58 to 77 ms and
  `/search` from 26 to 80 ms - the transcripts are loaded and thrown away. `GameSession` has seven big Text columns (`live_transcript`,
  `live_transcript_segments_json`, `summary`, `prep_json`, ...).
- The session page itself embeds the full transcript and the segment list: **651 KB** with a transcript (241 KB without).
- `get_world_ctx` loads **every column of every accessible World** on every request (custom rules markdown and JSON, theme, cockpit workspace,
  home sections, nav menus ...), to use the name and slug of all but one.
- Entity list queries load `body` (the full article) for cards that show the summary.

### D. Server memory and startup
- Idle: **138 MB RSS**, 7 threads, 0.2 % CPU. After 16 browsers at once: 211 MB (it was 546 MB before the render gate).
- Startup: **3.6 s** to the first response (2.4 s of it importing). Modules loaded at start that only a few routes use: the MCP server
  (+11 MB, 230 ms), `pypdf` (+12 MB), `ollama` (+5 MB), `reportlab` (+4 MB); `app.auth` spends ~190 ms at import computing a dummy
  PBKDF2 hash (600,000 rounds) for login timing.
- `init_db` re-checks every table's columns on every boot.
- Worst case SQLite page cache: 16 MB x up to 40 pooled connections; the render gate means far fewer are ever open, but nothing caps it.

### E. Disk and logs
- `uvicorn` writes an access-log line for every request, including the 4-second polls; Docker's default `json-file` log driver keeps them
  forever unless a size limit is set (none is set in `docker-compose.yml` / `truenas-compose.yml`).
- The Docker image carries 64 MB of bundled pictures in the formats they were imported in, plus Debian's full `ffmpeg` (several hundred MB of codec
  libraries; measure with `docker image ls` - no Docker daemon was available here).
- JavaScript is shipped unminified (1.6 MB raw, 0.74 MB gzip, smaller as brotli); `three.js` + `cannon-es` (1 MB raw, ~190 KB brotli) load on
  `/dice` even in the Quick buttons layout, which never draws the 3-D tray.

### F. What is already fine (no change proposed)
Static files are cached for a year and pre-compressed; idle CPU is negligible; the cockpit's timers only tick while running; polling already
pauses in hidden tabs; entity, rules, facts, calendar, cockpit pages are 20-100 KB.

## 2. The plan (ordered by value / effort)

| # | Change | Saves | Effort | Risk to features |
|---|---|---|---|---|
| 1 | **Thumbnails for bundled pictures.** Generate `_thumb.webp` (440 px) for `static/maps/*` and `static/races/*/*` at image build (next to `python -m app.precompress`) and let the `thumb` filter use them for `/static/` URLs too. Cards and the home Maps strip use thumbnails; the viewer and lightbox keep the originals. | Home **25.8 MB -> ~0.5 MB**, `/maps` 22.7 MB -> ~0.5 MB, races page several MB -> ~100 KB; phone memory | 0.5 day | None (a picture without a thumbnail keeps its original, as today) |
| 2 | **Fix the per-page theme write.** Only save when the person changes the theme / scale, not when the page applies the current one. | 1 DB write + 1 request + 1 cookie per page view, for everyone | 1 hour | None |
| 3 | **Tiled map viewer.** Cut each map into a tile pyramid (256-512 px WebP tiles, Leaflet `tileLayer` on the existing `CRS.Simple`) once, at upload / first boot, in the background; the viewer loads only the tiles in view at the current zoom. Full resolution is still there at the deepest zoom; pins, regions and overlays keep their coordinates. Keep the single-image path as a fallback until tiles exist. | First view of a big map: **9.4 MB -> a few hundred KB**; browser memory **273 MB -> tens of MB** on the Dreamlands map | 2 days | Low (same coordinates; fallback kept) |
| 4 | **One live channel instead of polling.** Put the spotlight / now-playing version and the dice-log version into the existing `/api/live` SSE stream (already one connection per browser, already relayed to every tab); drop the 4 s spotlight poll and the 5 s dice poll, and the 2 s poll on the TV combat display. Keep a slow fallback poll (60 s) if the stream is down. | ~900 requests / hour / open tab / person, each with an auth query and a log line | 1 day | Low (fallback poll kept) |
| 5 | **Put the rarely-changing settings into the page.** Render the hover-preview settings and the last known AI status into `base.html` (the server already caches both); refresh the AI status only when the GM opens a menu that needs it, or every few minutes. | 2 requests and 2-3 queries per page view | 0.5 day | None |
| 6 | **Read only the columns a page shows.** `defer()` the big Text columns in list and context queries: `GameSession` transcripts / segments / prep / loot on `/sessions`, the home page, search and the session log; `World`'s rules, theme, cockpit and home JSON in `get_world_ctx` (load them for the active world only, when used); `Entity.body` on cards. Load the session page's transcript and segment list when the transcript panel opens (like the NPC list). | `/sessions` 80 -> ~25 ms and search 80 -> ~30 ms with transcripts; session page **651 -> ~240 KB**; less memory per request | 1-2 days | Low (a deferred column still loads if code touches it; tests catch an N+1) |
| 7 | **Load rarely used libraries when first used.** Import the MCP server, `pypdf`, `ollama`, `qrcode` inside the routes / functions that need them (mount `/mcp` lazily); compute the login-timing dummy hash on the first failed login instead of at import; stamp a schema version so `init_db` skips the column checks when nothing changed. | Idle RSS **138 -> ~105-110 MB**; startup **3.6 -> ~2 s** (faster restarts and deploys) | 1 day | Low (first use of those features pays the import once) |
| 8 | **Quieter logs and bounded log files.** Turn off uvicorn's per-request access log (or filter the polling endpoints) - the new perf log already records route, status and time - and set `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}` in both compose files. | Unbounded disk growth from logs; one write per request | 1 hour | None (errors and the slow-request log stay) |
| 9 | **Smaller JavaScript.** Minify JS/CSS in the Docker build (esbuild, no source change, source maps kept); load `three.js` / `cannon-es` on `/dice` only when the 3-D tray is shown. | ~15-25 % fewer bytes per first visit; less parse work on phones; `/dice` in Quick mode ~200 KB less | 1 day | Low (build step only; tests read the sources) |
| 10 | **No cookie on every response.** A session middleware that sends `Set-Cookie` only when the session changed. | ~300 bytes per response; lets the browser and proxies treat responses as unchanged | 0.5 day | Low |
| 11 | **Cap the database's memory.** Pool 10 + 10 (a request holds one connection and page renders take turns now) and an 8 MB page cache per connection: worst case 160 MB instead of 640 MB. Check with the load test that throughput does not drop. | Worst-case RSS | 1 hour + a load run | Low |
| 12 | **Lighter Docker image.** Convert the bundled JPEG maps / race pictures to high-quality WebP (or keep the originals and only add the thumbnails and tiles); use a static `ffmpeg` build instead of Debian's package; `python -m compileall` at build for a faster cold start. | Image size (likely a few hundred MB, to be measured); faster pulls on the TrueNAS host | 0.5-1 day | Low (WebP at high quality is visually the same; keep originals if wanted) |
| 13 | **Display-size copies of large uploads.** Besides the 440 px thumbnail, write a ~1,600 px copy of big uploads and use it (with `srcset`) where a picture is shown large; the original stays for download and zoom. | Lightbox and handouts on phones: often 5-10x fewer bytes | 1 day | None (original always reachable) |
| 14 | **Database upkeep.** `PRAGMA optimize` on shutdown and daily, `auto_vacuum=INCREMENTAL` with a weekly `incremental_vacuum` (space comes back after big deletes such as a removed world); nothing is pruned. | Disk; slightly faster queries over time | 0.5 day | None |

**Suggested order:** 2 and 8 (an hour each), then 1, 5, 6, 4, 3, 7, 9, 10, 11, 12, 13, 14.

## 3. How each item is checked
- The full test suite, plus a budget test per item: page weight (home under 1 MB with the bundled maps; `/maps` under 1 MB), API calls per page
  view (a page view makes no `POST` and at most one API call), query counts, idle RSS after import, startup time.
- Before / after numbers from the same seeded world and the Chromium weight script, written into the commit message.
- Visual check in Chromium at laptop and phone width for every page whose images or scripts change (cards still show a picture, the map viewer
  still zooms to full detail with pins in place, the 3-D dice still roll).
- Player view diffed against before for the pages that change, so no GM-only content appears and nothing a player saw disappears.

## 4. Not proposed
- Rewriting in another language, a JavaScript framework or a different database: none of the measurements point there (see `PERFORMANCE_PLAN.md`).
- Deleting old jobs, recordings or history to save disk: that would lose data people may want.
- Lower image quality for what people look at: thumbnails, tiles and display copies are additions; the originals stay.


## 5. What was built, and what it measured

Before = `86c9780`, after = this commit; same seeded world, same Chromium and timing scripts.

| | Before | After |
|---|---|---|
| Home page, first view | **25.8 MB** (map strip at full size) | **~0.3 MB** of pictures (thumbnails) |
| `/maps`, first view | 22.7 MB (laptop) / 9.4 MB (phone) | **347 KB / 194 KB** |
| Opening the Dreamlands map | 9.4 MB, 273 MB decoded in the browser | **1.0-1.8 MB of tiles**, only what is in view; full detail kept |
| API calls in 5 page views | GM 30, player 12 (one a database write each page) | **GM 15, player 6**, no write |
| Spotlight / dice log | polled every 4 s / 5 s per tab | announced over the existing live stream; 60 s fallback poll |
| `/sessions` with recorded transcripts | 75 ms | **33 ms** |
| Search with transcripts | 96 ms | **34 ms** |
| Session page with a transcript | 651 KB | **242 KB** (the transcript is fetched after the page is up) |
| JavaScript / CSS shipped (gzip) | 423 KB / 32 KB | **270 KB / 21 KB** (minified in the image build) |
| `/dice` in Quick buttons | three.js + cannon-es loaded | not loaded (only for the 3-D tray) |
| Memory after `import app.main` | 124 MB | **103 MB** |
| Startup to first response | 3.6 s | **2.0 s** |
| After 16 browsers at once | 211 MB, 26 pages/s | **189 MB, 26 pages/s** |
| Logs | a line per request, kept forever | no access log (perf log + slow-request log stay), 3 x 10 MB per container |
| Response headers | `Set-Cookie` on every response | only when the session changed or the cookie is 6 h old |

### Step by step
1. **Thumbnails for bundled pictures - done** (`app/static_thumbs.py`; made in the image build and checked again at startup; replaced map images get a fresh one).
2. **Theme write per page view - fixed.**
3. **Tiled map viewer - done** (`app/map_tiles.py`, `GET /map-tiles/...`). Bundled maps are cut in the image build (cutting the 10k-pixel Dreamlands map needs about 1.1 GB of memory once, so it is not done on the server); a GM's own map uploads are cut in the background on first view, and the single picture is shown until then. Pins and regions keep their coordinates (screenshot diff against the old overlay: the same picture within resampling noise).
4. **Live channel instead of polling - done** for the spotlight / now playing and the dice log. **Not changed:** the TV combat display's 2 s poll (`display_combat.html`) - it is a separate full-screen page without the live script.
5. **Settings in the page - done** (hover-preview settings rendered into `base.html`; AI status remembered for a minute per tab).
6. **Only the columns a page shows - done** for session transcripts (deferred; lists use a computed `has_live_transcript`) and the World's rules / cockpit / home / recap texts. **Not changed:** `Entity.body` on cards - the cards use it for their excerpt when there is no summary, and the gain measured small.
7. **Load-on-first-use - done** for `ollama`, `qrcode`, the MCP server and the login-timing hash. **Not done:** a schema-version stamp for `init_db` - measured at 0.1 s, not worth the risk.
8. **Logs - done** (`--no-access-log`, `logging` limits on every compose service).
9. **Smaller JavaScript - done** (`python -m app.precompress --minify` in the image build, every minified file checked with `node --check` in the tests; the 3-D dice module loads only for the tray layouts).
10. **Cookie only when needed - done** (`app/session_cookie.py`).
11. **Database memory - done** (pool keeps 10 idle, up to 40; 8 MB page cache each; throughput unchanged).
12. **Lighter image - partly.** Bundled maps are stored as **AVIF q80** in the image (`python -m app.static_thumbs --avif-maps`; old `.jpg` and `.webp` links redirect) and bytecode is compiled at build. AVIF replaced the first version's WebP q90 after measuring both on the 15 bundled maps:

    | Bundled maps in the image | WebP q90 (first version) | AVIF q80 (now) |
    |---|---|---|
    | `static/maps` folder | 38.5 MB | **31.2 MB** (originals: 48 MB) |
    | Converted files | 12 maps, 35.0 -> 23.8 MB | 13 maps, 39.2 -> **20.8 MB** |
    | Fidelity against the original (mean / lowest PSNR) | 39.8 / 38.3 dB | **40.5 / 38.4 dB** |
    | Build time / peak memory | 37 s | 39 s / 0.6 GB |
    | Finest-zoom tiles cut from it, against tiles cut from the originals | 35.7 dB | **36.2 dB** (same tile size) |

    A picture over 40 MP keeps its file (`MAX_MAP_PIXELS`): the 72 MP Dreamlands map, already WebP, took 2.5 GB and 29 s to encode as AVIF and came out no smaller, and it is shown as tiles anyway. A file AVIF does not shrink is kept as it is (the City of Hue map). If Pillow cannot write AVIF the build falls back to WebP. **Kept as WebP on purpose:** map tiles, thumbnails and lightbox display copies. For 256 px tiles AVIF at equal quality was no smaller (q65: 9.0 KB at 37.4 dB against WebP q82: 9.1 KB at 38.0 dB), about 7x slower to encode (64 ms against 8.5 ms a tile, which matters for a GM's uploads cut on the server) and about 2x slower to decode in the browser; small thumbnails behave the same way. Replacing Debian's `ffmpeg` with a static build was left here and done later, once CI could measure it (docs/LEFTOVERS_PLAN.md, step 8: a 448 MB layer -> two pinned static binaries, ~168 MB smaller).
    The prebuilt map tiles add about 45 MB to the image.
13. **Display-size copies - done** for the lightbox (`?display=1`, a 2,560 px WebP at quality 90 made on first request - first 1,600 px at quality 82, which looked over-compressed on sharp screens - "Full size" link to the original). Other large-picture places (handout print, gallery album) still use the original.
14. **Database upkeep - done** (`app/db_upkeep.py`: daily `PRAGMA optimize`, on shutdown too; a file more than a quarter free pages is switched once to incremental auto-vacuum, then trimmed daily). Nothing is deleted.

Not measured: real phones, the Cloudflare path, the Docker image size, AI-heavy nights.
