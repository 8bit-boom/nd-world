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
- [ ] Extend the crawl to every list and detail page: characters, maps, boards, sessions, combat, quests, parties, albums, audio, video, calendar.
- [ ] Put the same decline logging into every page builder (the `dtrace` pattern in `rust/nd-gateway/src/pages.rs`).
- [ ] Missing row: answer 404 (not 501) without Python on every ID route.

## Batch 1: still Python-only calls the pages make
- [ ] imagegen tag lists: `POST /api/ai/imagegen/tags/fetch`, `/activate`, `/delete`
- [ ] `POST /api/ai/unsloth/update`
- [ ] King in Yellow `POST /api/kiy/generate`, `/api/kiy/plays`, `DELETE /api/kiy/plays/{id}`
- [ ] `POST /api/worlds`
- [ ] legacy Ollama: `POST /api/ai/pull`, `GET /api/ai/test-chat` (or delete them)
- [ ] `GET /characters/templates/{id}/export.json` and the other exports, for rows the gateway declines today

## Batch 2: exports and documents
- [ ] Character `.pdf` export (reportlab): a PDF writer in Rust; same content, bytes need not match.
- [ ] World Foundry export: needs markdown2's exact HTML; port its rules into `nd-render` or accept `pulldown-cmark`'s output for this export.
- [ ] FastAPI `/docs`, `/redoc`, `/openapi.json`: leave (not used in production).

## Batch 3: refusals of rare input (about 445 mixed routes)
- [ ] Port a refusal's wording only when `ND_DECLINE_TRACE=1` shows it is hit in real use.

## Batch 4: shrink the hybrid image
- [ ] Move the build-time Python stage of `Dockerfile.gateway` (thumbnails, AVIF maps, tile pyramids, minify) to Rust.
- [ ] Then, only with a month of clean real use: make the Python-free image the default.

## Log
(Add one line per batch: date, what, tests, commit.)
