# rust-fork: status and backlog

Branch purpose and rules: `CLAUDE.md`. Update this file with every batch (move items between sections, add the commit).

## Measured starting point (main at 3f217ee)
- 90% of the app's parameterless GET routes work with no Python (crawl); about 54% of routes are answered only by the gateway, 37% mixed, 9% Python only
  (docs/RUST_PORT_AUDIT.md).
- Found in real use of the Python-free image, all fixed on main: AI Chat history, the AI status dot, the sheet roller, starring a picture, the model list,
  AI rewrite, entity pages with rich-text notes or accented names.
- Still open: entity pages answer "Not available here" on the owner's real data in the Python-free image. Cause unknown; the gateway now logs
  `DECLINE GET entity page gave up at pages.rs:N` in that image, and `scripts/python_free_entity_crawl.py` lists every failing entity.

## Batch 0: find what real data breaks (do this first)
- [ ] Restore a real backup into a copy; run the entity crawl; fix every DECLINE line, most frequent first.
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
- [ ] King in Yellow `POST /api/kiy/generate` (SSE: web inspirations + AI stream; random, not byte-testable; next)
- [x] `POST /api/worlds`
- [ ] legacy Ollama: `POST /api/ai/pull`, `GET /api/ai/test-chat` (or delete them)
- [ ] `GET /characters/templates/{id}/export.json` and the other exports, for rows the gateway declines today

## Batch 2: exports and documents
- [x] Character `.pdf` export: `minipdf.rs` (Helvetica, US Letter, wrapped, multi-page); same content and order as reportlab's, bytes differ. Text outside Latin-1/WinAnsi prints as `?`.
- [x] World Foundry export `GET /export/foundry.json` (`export.rs::foundry_world`): accepted pulldown-cmark's HTML (blank lines between blocks differ from markdown2; the test compares with that whitespace collapsed). Every character uses the N&D layout, as Python's call with no session does.
- [ ] FastAPI `/docs`, `/redoc`, `/openapi.json`: leave (not used in production).

## Batch 3: refusals of rare input (about 445 mixed routes)
- [ ] Port a refusal's wording only when `ND_DECLINE_TRACE=1` shows it is hit in real use.

## Batch 4: shrink the hybrid image
- [ ] Move the build-time Python stage of `Dockerfile.gateway` (thumbnails, AVIF maps, tile pyramids, minify) to Rust.
- [ ] Then, only with a month of clean real use: make the Python-free image the default.

## Found by the all-pages crawl on synthetic data, fixed
- [x] `GET /entity/<id>/download.md` was refused for any entity whose name has an accented letter (Python-free only): now served, with an RFC 5987
      `filename*` (Python writes the name as latin-1 and fails on anything wider).
- [x] `GET /worlds/<id>/home/edit` (the home page editor) was not ported at all: now a page of the gateway, byte-identical to Python.

## Log
- 2026-11-01  batch 0 part 1: DECLINE log for every refusal, missing rows 404, all-pages crawl script, download.md for accented names, home page
  editor page. Tests: `test_python_free_missing_rows_are_404_and_accented_downloads_work`, `test_home_page_editor_is_pythons`.
