# Rust port: status and backlog

**2026-11-02: `rust-fork` was promoted to `main`** (the old hybrid-only `main` is kept as the branch `hybrid-python-main`). This file keeps the checklist. Rules: `CLAUDE.md`. Update this file with every batch (move items between sections, add the commit).

## Measured starting point (main at 3f217ee)
- 90% of the app's parameterless GET routes work with no Python (crawl); about 54% of routes are answered only by the gateway, 37% mixed, 9% Python only
  (docs/RUST_PORT_AUDIT.md).
- Found in real use of the Python-free image, all fixed on main: AI Chat history, the AI status dot, the sheet roller, starring a picture, the model list,
  AI rewrite, entity pages with rich-text notes or accented names.
- The entity-page "Not available here" on the owner's real data was a regex backtrack limit on long non-ASCII text in a big world (fixed `62b7dfa`). Found by the local session's crawl of a restored copy; the owner's `world-pf` copy has logged 0 `DECLINE` lines since.

## Batch 0: find what real data breaks (do this first)
- [x] Restore a real backup into a copy; run the entity crawl; fix every DECLINE line (done by the local session: 330 pages compared with Python, seven gaps fixed, `62b7dfa` `b8ead48` `17f03a3`).
- [x] Extend the crawl to every detail page of every list: `scripts/python_free_crawl_all.py` (entities, characters, sessions, combat, quests, parties, maps,
      boards, albums, tables, templates, world pages, kinds). Still to add: calendar, audio / video library pages, search, rules.
- [x] Log every request the Python-free image cannot answer: `DECLINE <method> <path>` (proxy.rs). Per-step reasons exist for the entity page only
      (`dtrace`, pages.rs); add them to the other page builders when a path shows up in the log.
- [x] Missing row: 404 `{"detail": "Not Found"}` (not 501) without Python for entity, handout, character, session, session-log, combat, quest, party,
      map and board pages (proxy.rs `missing_row_404`).

## Batch 1: still Python-only calls the pages make
- [x] imagegen tag lists: GET sources/status/search and POST `fetch`, `/activate`, `/delete` (`tags.rs`, Python-free only: Python keeps a parsed-list cache)
- [x] `POST /api/ai/unsloth/update`
- [x] King in Yellow `POST /api/kiy/plays`, `DELETE /api/kiy/plays/{id}`
- [x] King in Yellow `POST /api/kiy/generate` (`ai.rs::kiy_generate`, Python-free only: random and web-fed; tested against the Studio stand-in)
- [x] `POST /api/worlds`
- [x] legacy Ollama: `POST /api/ai/pull`, `GET /api/ai/test-chat`: will not be ported (the owner runs Unsloth, not Ollama); in a Python-free install they answer the "Not available here" refusal
- [x] `GET /characters/templates/{id}/export.json` (the other exports were already ported; the all-pages crawl found no further gaps)

## Batch 2: exports and documents
- [x] Character `.pdf` export: `minipdf.rs` (Helvetica, US Letter, wrapped, multi-page); same content and order as reportlab's, bytes differ. Text outside Latin-1/WinAnsi prints as `?`.
- [x] World Foundry export `GET /export/foundry.json` (`export.rs::foundry_world`): accepted pulldown-cmark's HTML (blank lines between blocks differ from markdown2; the test compares with that whitespace collapsed). Every character uses the N&D layout, as Python's call with no session does.
- [ ] FastAPI `/docs`, `/redoc`, `/openapi.json`: leave (not used in production).

## Batch 3: refusals of rare input (about 445 mixed routes)
- [ ] Port a refusal's wording only when `ND_DECLINE_TRACE=1` shows it is hit in real use.

## Batch 4: shrink the hybrid image
- [x] The build-time Python stage of `Dockerfile.gateway` is gone: `nd-assets` (`rust/nd-gateway/src/assets.rs`, a second binary) makes the AVIF maps (4 at a time), thumbnails, tile pyramids, minified CSS and `.gz`/`.br` twins. Differences from the Python stage: JavaScript is compressed but not minified; Rust encoders (equivalent pictures, not identical bytes). Run on a copy of `static/`: 14 maps 40.0 MB to 13.6 MB, CSS 220 KB to 172 KB, 94 files compressed, brace counts equal; 9m41 single-threaded before the 4-way change. **The Docker image itself was not built here (no Docker daemon in this sandbox): the first real check is the `docker-publish` run.**
- [ ] Then, only with a month of clean real use: make the Python-free image the default (the owner runs it as `world-pf` on a data copy since 2026-11-02).

## Found by the all-pages crawl on synthetic data, fixed
- [x] `GET /entity/<id>/download.md` was refused for any entity whose name has an accented letter (Python-free only): now served, with an RFC 5987
      `filename*` (Python writes the name as latin-1 and fails on anything wider).
- [x] `GET /worlds/<id>/home/edit` (the home page editor) was not ported at all: now a page of the gateway, byte-identical to Python.

## Speed plan (2026-11-03; the plan was written from the audit in this chat)
Measured with `scripts/bench_bigworld.py` (700 entities, one request at a time, median ms) and a concurrent load of 8 clients on the static (musl) build, the one the image ships.
| | before | after |
|---|---|---|
| Entity page, 700 entities | 235-252 ms (Python 18) | **6.4 ms** |
| Home / rules / search | 8.3 / 8.3 / 10 ms | 6.3 / 5.7 / 5.5 ms |
| Entity list (445 KB), one at a time | 35 ms | 29 ms |
| 8 concurrent clients, musl, entity page | 118 req/s | **350 req/s** (glibc 398) |
| 8 concurrent clients, musl, entity list | 8 req/s (!) | **95 req/s** (glibc 99) |
| 8 concurrent clients, musl, home | 97 req/s | **377 req/s** (glibc 450) |
- [x] 0 `perf.rs`: request timings (route templates only) for `/api/perf/*` and the `ND_SLOW_MS` log without Python; `scripts/bench_bigworld.py`.
- [x] 1 Entity auto-linker: one Aho-Corasick automaton per distinct list of names (cached), same answer as Python's alternation regex (random-text test against the regex;
      the 700-entity pages are byte-identical to Python's). Names and text with accents or other scripts use the same automaton on a case-folded copy (offset map; tests against the regex, incl. random Unicode). Behind Python (hybrid) a non-ASCII name still declines to Python.
- [x] 2 `writes::open` lends pooled connections (identity-checked against a replaced file, rolled back on return, 16 idle at most) with cached statements. Not done on purpose:
      the live counters stay in SQLite - a poll with a reused connection costs ~0.1 ms, and an in-memory copy would need a commit hook for no visible gain.
- [x] 3 `row_map` reads the column list once per row (was once per column).
- [x] 4 mimalloc as the global allocator. The static musl build used musl's malloc, which serialises on one lock: under load it was 3-12x slower than the same code on glibc.
      Fat LTO + one codegen unit measured only +5% for 2.7x the build time: not adopted.
- [x] 5 Big entity card lists: the first 120 cards, then a "Load more" link (`?limit=N`, `?limit=all`) that nd-load-more.js turns into an in-place append (bulk select and layouts refreshed);
      works without JavaScript as a normal link. Tables keep their own paging. Python and the gateway render it identically (`tests/test_entity_load_more.py`).
- [x] 6 Indexes `entity_links(target_id)` (backlinks) and `entity_player_access(user_id)`; `tests/test_query_plans.py` fails on a full scan of the hot queries.
- [x] 7 AVIF encoder speed 10 on the running server (6 MP photo: 3.9 s against ~12 s at speeds 4-8, 5% bigger); the image build keeps speed 6.
- [x] 8 JavaScript minified in the Rust-only image by esbuild (white space and comments only, in its own build stage): 1.26 MB -> 0.78 MB, `node --check` on every file, the Node-run tests pass
      on the minified copies (two tests that look for source text need the unminified files).
- [ ] 9 Whole-page cache - not needed: pages now take ~6 ms.

## Log
- 2026-11-01  batch 0 part 1: DECLINE log for every refusal, missing rows 404, all-pages crawl script, download.md for accented names, home page
  editor page. Tests: `test_python_free_missing_rows_are_404_and_accented_downloads_work`, `test_home_page_editor_is_pythons`.
- 2026-11-02  batches 1, 2 and 4 done and pushed (`4402925` `44f40f2` `89a55e9` `5256b68`); local session fixed the real-data gaps; `rust-fork` promoted to `main`; the footer, Settings
  and `/health` now show the build and the run mode (hybrid / Rust / Python). Open: batch 3 only as the `DECLINE` log asks; JavaScript is not minified in the Rust-only image.

### Memory under parallel load (measured with `scripts/bench_bigworld.py` data, 16 clients x 40 entity pages, 4 cores)
Heap profile (dhat, `--features dhat-heap`, `ND_DHAT_SECS`): the live heap never passed 26 MB for 320 requests, so the 700 MB seen was memory the allocator kept, not data.
Each blocking thread has its own mimalloc heap that holds the pages it freed, so the memory grew with the number of threads tokio started (up to 512), not with the work.
| change | peak RSS | after 15 s idle |
|---|---|---|
| before | 459 MB (user's run: 774) | 417 MB (631) |
| `MIMALLOC_PURGE_DELAY=0` (now set in both Dockerfiles) | 307 | 115 |
| + page builds and the session check on their own pool (`ND_PAGE_THREADS`, default one per core, at least 4) | 97 | 79 |
| + one auto-link automaton per world (the viewed entity's own names are skipped when linking), not one per entity | 87 | 77 |
The same 640 entity pages also take 2.7 s instead of 3.0-3.5 s. The AI / job / upload threads stay on tokio's own pool, so a long generation cannot hold up a page.
