# Leftovers plan - what the earlier plans did not do

Status: PLAN ONLY, nothing here is built. It collects every item that `docs/PERFORMANCE_PLAN.md` (section 7), `docs/EFFICIENCY_PLAN.md`
(section 5) and the layout plans (`docs/VIEW_LAYOUTS_PLAN.md`, `docs/SITE_LAYOUTS_PLAN.md`) left as "not done", "partly" or "later", says
what each would take, and gives a recommendation. Each item lists **why it was left**, **how to do it**, **how to check it**, **effort** and
**risk**. Numbers are from the measurements those plans already made unless the item says otherwise.

The rules from the earlier plans still apply: no loss of function, the full test suite before every push, measure before and after, and an
item whose measurement shows no gain is dropped rather than built.

## 0. Summary

| # | Item | Recommendation | Effort | Risk |
|---|---|---|---|---|
| 1 | Measure the real host: Docker image size, `docker stats`, a phone, the Cloudflare path | **Do first** - several items below wait on it | 0.5 day (mostly yours) | None |
| 2 | TV combat display: 2 s poll -> live signal | **Do** | 0.5 day | Low |
| 3 | Session page: 108 KB inline script -> cacheable file | **Do** | 1 day | Medium (28 template values to move) |
| 4 | Async handlers doing database work on the event loop (181 routes) | **Do, in batches** | 2-3 days | Medium |
| 5 | Handouts gallery and album filmstrip still load original pictures | **Do** | 0.5 day | Low |
| 6 | Python 3.13 everywhere in CI + the pytest plugin mismatch | **Do** | 0.25 day | Low |
| 7 | Runtime tiling memory for huge uploads | **Do if** GMs upload maps over ~40 MP | 1-1.5 days | Low-medium |
| 8 | Lighter `ffmpeg` in the image | **Decide after #1** | 0.5-1 day | Medium (supply chain) |
| 9 | Cockpit with many windows: a real trace | **Do** (script it) | 0.5 day | None |
| 10 | AI contention stress matrix | **Do on the real host** | 0.5 day (yours) | None |
| 11 | `Entity.body` excerpt on cards | **Only if #1 / the perf log shows it** | 0.5 day | Low |
| 12 | Nav menu / world caching | **Only if the perf log shows it** | 0.5 day | Medium (stale menus) |
| 13 | `init_db` schema stamp | **Don't** (0.1 s for a risk of a missed migration) | 0.5 day | Medium |
| 14 | `orjson` | **Don't**, unless the perf log shows JSON encoding in the top routes | 0.5-1 day | Medium |
| 15 | Layout leftovers: audio table, video theater, image compare 2-up, justified rows, saved named views | **Do when wanted** (features, not speed) | 3-4 days total | Low |

Suggested order: 1 -> 6 -> 2 -> 5 -> 3 -> 9 -> 4 (batches) -> 7 / 8 / 11 / 12 as #1 and the perf log decide -> 15 whenever.

## 1. Measure the real host (the inputs the other items need)

**Why it was left:** this environment has no Docker daemon, no phone, no Cloudflare tunnel and no AI backend.

**How:**
- *Image size:* add a step to `.github/workflows/docker-publish.yml` after the build that prints `docker image inspect -f '{{.Size}}'` and
  `docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}'` into the job summary. That gives the size of each layer (the `ffmpeg` apt
  layer, the pip layer, `static/`, the prebuilt map tiles) on every build, so #8 is decided on numbers and later growth is visible.
- *`docker stats`:* on the TrueNAS host, `docker stats --no-stream` idle, during a session night (cockpit + 4-6 players), and while the AI
  writes a recap. Note RSS and CPU of the app container.
- *Phone and Cloudflare:* open home, `/maps`, a map, a session page and the cockpit on a real phone over the Cloudflare URL with remote
  debugging (Chrome `chrome://inspect`, Safari Web Inspector); write down bytes, requests and time to interactive per page. Repeat on the LAN URL.
- Record everything in a new section of `docs/EFFICIENCY_PLAN.md` so items 7, 8, 11, 12 and 14 can be decided.

**Check:** the numbers exist. **Risk:** none.

## 2. TV combat display: from a 2 s poll to a live signal

**Why it was left:** `app/templates/display_combat.html` is a separate full-screen page inside the second screen; it does not load
`static/js/nd-live.js`, so it polls `/api/display/combat/{id}/state` every 2 s (1,800 requests an hour per TV, each a database read).

**How:**
1. In `app/routers/combat.py`, after every change that alters the player view of an encounter (turn, round, HP, conditions, add / remove a
   combatant, end), call `live.signal(world_id, "combat")` (the signal mechanism `app/live.py` already has for spotlight and dice).
2. Load `nd-live.js` in `display_combat.html` (the page is signed in as the GM, so `/api/live` already accepts it; `nd-live.js` shares one
   EventSource between all tabs of the browser through `BroadcastChannel`, so this adds no connection when the cockpit is open in the same
   browser). The page already re-fetches on the `nd-live` window event; add a listener for `nd-signal` with `name === "combat"`.
3. Slow the fallback poll to 30 s (it stays as the safety net for a lost stream).

**Check:** a test that a combat change emits the signal (pattern of `tests/test_live_signals.py`); a test that the template loads `nd-live.js`
and polls at 30 s; by hand, a turn change shows on the TV within a second. **Effort:** 0.5 day. **Risk:** low (the poll stays as fallback).

## 3. Session page: the 108 KB inline script as a cacheable file

**Why it was left:** `app/templates/sessions/detail.html` lines 550-2694 are one inline `<script>` of 108 KB that is sent, parsed and not
cached on every view of a GM session page; it contains 28 Jinja expressions (`{{ ... }}` / `{% ... %}`), so it cannot simply be moved.

**How:**
1. List the 28 expressions. Values (`{{ gs.id }}`, URLs, flags, `tojson` data) go into one
   `<script type="application/json" id="nd-session-data">{{ session_data | tojson }}</script>` block built in the route.
2. `{% if %}` blocks that include or leave out code become `if (DATA.flag)` checks in the script (behaviour stays the same; the code for a
   hidden feature is downloaded but not run - and is cached).
3. Move the script to `static/js/session-detail.js`, load it with `asset_v` like the other session scripts (it is then minified, compressed
   and cached by the image build like every other file).
4. Tests that read the inline script from the template switch to reading the file (the `base_js()` helper in `tests/conftest.py` is the
   pattern used when `base.html`'s scripts moved).

**Check:** the existing session-page tests; `node --check` on the file (the minifier test does this already); a byte count of the page before
and after (expect about 650 KB -> 540 KB raw on a page with a transcript, and the script fetched once per deploy); click through recap,
facts, NPC picker, loot, XP, combats, live recording by hand. **Effort:** 1 day. **Risk:** medium - a missed template value breaks a
feature, so do it expression by expression with the tests running.

## 4. Async route handlers that do database work on the event loop

**Why it was left:** 181 `async def` routes take a database session (count over `app/main.py` and `app/routers/*.py`). Most `await
request.form()` or `request.json()` and then run synchronous SQLAlchemy queries inside the event loop, so while one of them runs every other
request - including SSE streams and the cheap API calls - waits. The performance plan converted the 71 handlers with no `await` and left
these as the larger change.

**How:** one mechanical pattern, router by router:
1. Add two async dependencies in `app/deps.py`: `async def form_data(request) -> FormData: return await request.form()` and the same for JSON.
   FastAPI resolves async dependencies on the event loop and then runs a plain `def` handler in its worker threads.
2. Rewrite `async def h(request, db=Depends(get_db)): form = await request.form(); ...` as
   `def h(request, form=Depends(form_data), db=Depends(get_db)): ...`.
3. Leave alone (and list in a comment) handlers that stream, start `asyncio.create_task`, await AI calls, or read the body in chunks
   (uploads) - those need the event loop. For those, move only the database part into a helper called with `await run_in_threadpool(...)`.
4. Batches by router (combat, sessions, calendar, ...), the full suite after each batch, one commit per batch.

**Check:** `tests/test_db_pool_budget.py` still passes (a request still holds one connection); a new load check in `scripts/perf_smoke.py`:
the latency of `/api/live` heartbeats and a cheap API call while 8 clients post forms in a loop - expect the worst case to fall from "the
length of the slowest handler" to a few milliseconds; the perf log's route timings unchanged or better. **Effort:** 2-3 days.
**Risk:** medium - a handler that uses something tied to the event loop breaks in a thread; the batches and the suite catch that.

## 5. Display copies for handouts and gallery albums

**Why it was left:** step 13 of the efficiency plan only covered the lightbox. Two places still load original uploads (often 3-10 MB from a
phone camera or an AI image): `app/templates/handouts_gallery.html` shows **every** handout's original picture in its grid
(`<img src="{{ item.entity.image_url }}">`), and the gallery album's filmstrip layout (`static/js/nd-views.js`) shows the original in its
big preview.

**How:** the handouts grid uses the `thumb_url` filter (the 440 px thumbnail, as entity cards already do); the album filmstrip's big picture
uses the `?display=1` copy (`app/imaging.make_display`, 1,600 px WebP made on first request). The single handout page (`handout.html`) is a
print page and keeps the original (A4 at 300 dpi wants about 2,500 px, more than the display copy), as does every "Full size" link.

**Check:** template tests that the grid uses thumbnails and the filmstrip uses `?display=1`; bytes of a handouts gallery with 10 phone
photos before and after (expect tens of MB -> under 1 MB). **Effort:** 0.5 day. **Risk:** low (a picture without a thumbnail keeps its
original, as everywhere else).

## 6. Python 3.13 everywhere in CI, and the pytest plugin mismatch

**Why it was left:** the image and `tests.yml` run Python 3.13, but `.github/workflows/docker-publish.yml` still runs the suite on 3.11 before
building the 3.13 image, and two tests failed in a local 3.13 environment that lacked a pytest plugin (they are skipped on 3.11).

**How:** set `python-version: "3.13"` in `docker-publish.yml` and install `requirements-dev.txt` there (same pins as `tests.yml`); find the two
tests (run the suite in a clean 3.13 venv made from `requirements-dev.txt`), and add the missing plugin to `requirements-dev.txt` or make
their skip condition say what they need.

**Check:** both workflows green on 3.13; the two tests run (not skipped) in CI. **Effort:** 0.25 day. **Risk:** low.

## 7. Runtime tiling memory for huge uploaded maps

**Why it was left:** bundled maps are cut in the image build. A GM's upload is cut on the server in the background (one at a time), and
Pillow decodes the whole picture first: about 1.1 GB for a 72 MP map, 4 bytes per pixel more for a bigger one.

**How (two options, prefer A):**
- **A. `pyvips`** (libvips streams the picture in strips; tiling a 72 MP map takes tens of MB). Optional dependency: `apt install
  libvips42` + `pyvips` in the image; `app/map_tiles.build` uses it when importable and falls back to the Pillow path otherwise. Same
  tile names, sizes and WebP quality, so the viewer does not change.
- **B. Pillow only:** for JPEG uploads use `Image.draft()` to decode at 1/2-1/8 for the lower zoom levels and cut the full level from
  horizontal strips (`Image.crop` on a lazily loaded JPEG still decodes everything above the strip, so this only helps the lower levels);
  pictures over a pixel cap (say 60 MP) get no deepest level and keep the single-picture fallback for full detail.

**Check:** `tests/test_map_tiles.py` passes with both paths (same tile grid); peak memory measured with `resource.getrusage` while tiling a
generated 80 MP picture: target under 300 MB. **Effort:** 1-1.5 days. **Risk:** low-medium (new system library). **Do only if** #1 shows
GMs uploading maps this large or the host is short of memory.

## 8. A lighter `ffmpeg`

**Why it was left:** Debian's `ffmpeg` package pulls in a long list of libraries (likely the biggest layer of the image), but a downloaded
static binary is a supply-chain risk, and there was no Docker daemon to measure the gain.

**How:** first the layer sizes from #1. Then, if the `ffmpeg` layer is large (over ~150 MB):
- **A.** A multi-stage build: a builder stage compiles a minimal `ffmpeg` (`--disable-everything` plus the demuxers, decoders and encoders
  the app uses: audio only - mp3, m4a/aac, ogg/opus, wav, webm; `ffprobe`) from a pinned, checksum-verified source tarball, and the final
  stage copies the two binaries. Smallest result, longest build (cache the stage).
- **B.** A pinned static build (for example the johnvansickle.com release) verified against a SHA-256 written in the Dockerfile. Simple, but
  trusts one more publisher.
- Either way: the audio tests that call `ffmpeg` (`tests/test_stt_pipeline.py`, `tests/test_transcript_chunking.py`,
  `tests/test_stt_upload_format.py`) and a by-hand run of a long recording (split, re-encode over 25 MiB, live archive).

**Effort:** 0.5-1 day. **Risk:** medium (a missing codec only shows on a real recording) - so **decide after #1**; if the layer is under
~150 MB, leave Debian's package.

## 9. Cockpit with many windows: a real trace

**Why it was left:** an idle cockpit with one window was measured (13 requests in 30 s, since cut to the live stream); six or more live
windows were not traced.

**How:** `scripts/cockpit_trace.py` (Playwright, the pre-installed Chromium): sign in as the GM, open the cockpit plus N pop-out windows
(N = 1, 3, 6, 10) in one browser context, record every request and every open EventSource for 60 s, print requests per endpoint per minute
and the number of `/api/live` connections. Expect one live connection for the whole browser (`nd-live.js` elects one tab and relays over
`BroadcastChannel`); more would also matter on plain HTTP/1.1 (the LAN URL), where a browser allows only 6 connections per host.

**Check:** the script's table in `docs/EFFICIENCY_PLAN.md`; anything polling faster than once a minute without a reason becomes a follow-up.
**Effort:** 0.5 day. **Risk:** none.

## 10. AI contention stress matrix

**Why it was left:** no AI backend here.

**How:** on the real host run `scripts/perf_smoke.py` three times - AI idle, a chat answer generating, an image generating - and once more
during a recap of a long transcript. Compare page and API medians and the worst case. If pages slow down a lot while the AI runs, the
levers are `ND_RENDERS` (render slots), the worker thread count, and running the AI client calls with lower priority; decide from the
numbers. **Effort:** 0.5 day (yours). **Risk:** none.

## 11. `Entity.body` on the card lists

**Why it was left:** entity cards use the body for their excerpt when there is no summary, so the column cannot simply be deferred, and the
measured gain on the seeded world was small.

**How:** a `column_property(func.substr(Entity.body, 1, 600))` called `body_head` on `Entity`, `defer(Entity.body)` on the list queries, and
the card / hover excerpt read `body_head`. **Check:** perf budget test on a world with long bodies (generate 200 entities of 20 KB);
expect the list query's bytes to drop ~30x. **Do only if** a real world has long bodies and #1 / the perf log shows list pages are slow.
**Effort:** 0.5 day. **Risk:** low.

## 12. Nav menu and world caching

**Why it was left:** measured as cheap. The template context processor (`app/templating.py`) does a World lookup, `effective_kinds`,
`effective_subtypes` and `resolve_nav_menus` on every page.

**How (if wanted):** an LRU keyed by `(world.id, a nav version counter, role, the two content flags)` for the resolved menus; bump the counter
from the nav-menu admin routes, world settings saves and kind edits. **Check:** SQL count per page in the perf log drops by the menu
queries; tests that an admin change shows on the next page. **Do only if** the perf log shows the context processor in the top costs.
**Effort:** 0.5 day. **Risk:** medium (a stale menu after an edit if one write path forgets the bump).

## 13. `init_db` schema stamp - recommend not doing

Measured at 0.1 s of startup. A stamp (`PRAGMA user_version` set to a hash of the models and the migration list, `_migrate()` skipped
when it matches) saves that 0.1 s but adds a way to skip a needed migration (a hand-edited database, a restored backup). Not worth it
unless startup time starts to matter (it is 2.0 s now).

## 14. `orjson` - recommend not doing for now

Swapping the default response class changes details of every JSON answer (non-string keys rejected, `NaN` handling, float formatting) for a
gain nobody has measured here. **If** the perf log shows JSON encoding in the top routes (large `/api/...` answers), use `ORJSONResponse`
for those routes only, each with a test comparing the parsed output with today's.

## 15. Layout leftovers (features, not speed)

From `docs/VIEW_LAYOUTS_PLAN.md`; its status table is partly out of date (the entity web, facts and maps layouts were built later through
`docs/SITE_LAYOUTS_PLAN.md`) and should be brought up to date in the same change.

| Layout | What it is | How | Effort |
|---|---|---|---|
| Audio **table** | sortable columns: name, length, size, tags, used in, visibility | new mode in `app/view_prefs.py` (`"audio"`), render in `static/js/audio-views.js` using the shared `nd-views.js` sort / group | 0.5 day |
| Video **theater** | one big player, the list as a strip under it | new mode for `"video"`; the player is the existing `<video>` element moved into a stage | 0.5 day |
| Images **compare 2-up** | two pictures side by side (pick two, swap, sync zoom) | a mode in the images layouts; selection by checkbox, a two-pane viewer | 0.5-1 day |
| Images **justified rows** | rows of equal height that fill the width, Flickr-style | needs each picture's width and height: two nullable columns on the image table filled at upload, a background backfill for old pictures; the layout falls back to grid while any are missing | 1 day |
| **Saved named views** | save the current layout + sort + group + filter under a name, pick it from a menu | a `saved_views` table (user, page key, name, JSON), routes to list / save / delete, a menu in the shared layout bar | 1 day |

**Check:** the existing `tests/test_nd_views.py` (Node) pattern for the pure decisions, a template test per page, and `docs/API_REFERENCE.md`
rows for any new route. **Risk:** low.
