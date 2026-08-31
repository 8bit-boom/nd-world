# IMPROVEMENT_PLAN.md — nd-world working roadmap

> **For AI coding assistants (ZCode et al.):** this is the living plan file for
> nd-world development. Read [AGENTS.md](AGENTS.md) first — it defines the
> repo's hard conventions (router-per-feature, auth allowlist, docs
> requirements). This file adds the roadmap, the current state, and the
> verified dev/test workflow. Update the status checkboxes as work completes.

## How to work in this repo (verified workflow)

### Run the test suite — Docker-first

The app ships as a Linux container; run tests there too. **Do not run pytest
on Windows** — `os.geteuid` (fixed but still), asyncio-proactor noise, and
SQLite-on-bind-mount failures make host runs unreliable.

```bash
# One-time (and after changing requirements or the production image):
docker build -t nd-world:latest .                     # production image
docker build -f docker/test/Dockerfile -t nd-world-test:latest .   # adds pytest + source

# Run the FULL suite (self-contained; no bind mount — bind mounts from
# Windows hosts break SQLite world.db creation):
docker run --rm nd-world-test:latest

# Iterate on a scoped set: rebuild (COPY layers re-run in seconds), then:
docker run --rm nd-world-test:latest python -m pytest tests/test_dice.py tests/test_backups.py -q
```

Rules (from AGENTS.md): never run two pytest invocations at once against the
same DB_PATH; scope to relevant files while iterating; **full suite must pass
before a change is done**; every new HTTP route needs a row in
`docs/API_REFERENCE.md` — enforced by `tests/test_api_reference_docs.py`.

### Git layout

- `main-origin` — remote branch pinned to the pre-improvements main; revert
  anchor (`git reset --hard main-origin`).
- `improvements/phase-1` — active work branch (pushes to GitHub).
- Production smoke test: `docker compose up -d --build`, then
  `GET /login` → 200, `POST /login` (form-encoded GM creds from `.env`) →
  303 → authenticated `GET /` shows the GM badge.

### Conventions added by this plan's work

- Static assets are cache-busted via `?v={{ asset_v('file') }}` (content
  hash, `app/templating.py`) — never hardcode a version number.
- New player-reachable routes must be added to `_is_player_safe()`
  (`app/main.py`) and covered by `tests/test_player_safe.py` expectations.
- Optional features key off env vars and must no-op cleanly when unset
  (pattern: `app/backups.py`'s `ND_BACKUP_DIR`).

---

## Phase 1 — stability foundation + quick wins (IN PROGRESS)

| Item | Status | Notes |
|---|---|---|
| B1 CI workflow running full pytest suite | ✅ done | `.github/workflows/tests.yml` (ubuntu, py3.12, `pip install -r requirements-dev.txt`, `pytest -q`) |
| B4 Dependabot (pip + github-actions) | ✅ done | `.github/dependabot.yml` |
| P4 Automated content-hash cache-busting | ✅ done | `asset_v()` in `app/templating.py`; base.html + 3 auth templates converted; AGENTS.md updated |
| P1 SQLite audit | ✅ done | WAL + `synchronous=NORMAL` pragma (`app/database.py`); composite index `ix_entities_world_folder` in `_migrate()` |
| B2 API-docs drift test | ✅ done | `tests/test_api_reference_docs.py`; closed **66 pre-existing gaps** + 6 new routes; docs now cover all **453 routes** |
| B3 Backup scheduler | ✅ done | `app/backups.py` (VACUUM INTO snapshots, retention, `ND_BACKUP_DIR`/`_BACKUP_INTERVAL_SECONDS`/`_BACKUP_KEEP`), `app/routers/backups.py` (GM-only API), `tests/test_backups.py` |
| G1 Dice roller + shared roll log | ✅ done | `app/routers/dice.py`, `DiceRoll` model, `/dice` page (nav tab), player-safe routes, `tests/test_dice.py` |
| U1 Command palette (Ctrl-K) | ✅ done | `static/js/command-palette.js` in base.html — nav links + `/api/entities/picker` entities, theme-aware, no build step |
| B7 Windows dev support | ✅ done | `scripts/setup.ps1` mirrors setup.sh; suite runs in Docker (see workflow above); `os.geteuid` skipif fixed |
| Test-in-Docker harness | ✅ done | `docker/test/Dockerfile` (+ its own `.dockerignore`) — pytest layered on the production image |
| Full suite green in container | ✅ done | 2261 passed / 4 skipped / 0 failed; fixes on the way: `DiceRoll` added to `_WORLD_DELETE_MODELS`, closed-loop straggler guard in `job_shutdown.drain()`, deploy files COPY'd into the test image, CRLF-normalized toolbar-JS test, faked AI in the attachment-scoping tests (their hung real-transcribe jobs used to poison the next test's DB) |
| Commit + push `improvements/phase-1` | ✅ done | `.env`/`.venv/` added to `.gitignore` first; `tests/_data/` was already ignored |
| CI green on GitHub | ⬜ | Verify Actions run on the pushed branch |

## Phase 2 — table experience + hardening

| # | Item | Priority | Impact | Effort |
|---|------|----------|--------|--------|
| G2 | Map fog-of-war / GM reveal (design spike first: reveal layers vs dynamic mask) | P0 | H | L |
| G3 | Combat tracker: conditions/status w/ durations, HP from stat-block templates, round history | P1 | H | M |
| U3 | Import preview/diff before `/api/import/execute` | P1 | H | M |
| U4 | Soft delete / undo window for entities + worlds | P1 | H | L |
| B5 | Security pass: CSRF on every mutating form, cookie flags, per-world ID-walk audit | P1 | H | M |
| B6 | Upgrade fixtures: golden JSON exports from older versions imported in tests | P1 | M–H | M |
| P2 | N+1 sweep beyond gallery (entity/character lists, boards) using `test_gallery_n_plus_1.py` pattern | P1 | M | M |
| P3 | Image pipeline: upload-time thumbnails, width/height attrs, lazy-loading | P1 | M | M |
| B8 | Large image uploads (>100 MB) despite the Cloudflare Tunnel 100 MB request-body limit: client-side file splitting, sequential part upload (each chunk under the limit), server-side reassembly + integrity check (hash/size), resume on failure | P1 | M | M–L |
| P8 | Animated image support end-to-end in the image section: animated WebP, AVIF, and GIF upload/serve/store without losing animation, animation plays in gallery/entity detail/lightbox, still first-frame thumbnail for grids | P2 | M | M |
| U2 | First-run onboarding wizard | P1 | H | M |
| G4 | Roll random tables from AI chat / entity notes; nested tables | P1 | M | S–M |

## Phase 3 — polish + ecosystem

| # | Item | Priority | Impact | Effort |
|---|------|----------|--------|--------|
| G5 | Player session screen (initiative + revealed map + party, mobile-first) | P1 | H | L |
| M3 | JSON export schema versioning + deprecation policy | P1 | H | M |
| M2/P5 | Test parallelism: per-worker DB_PATH for pytest-xdist, `slow` marker | P2 | M | M |
| G7 | Content pack versioning + safe re-import | P2 | M | M |
| G6/G8 | Downtime/world-clock; loot-XP session ledger | P2 | M | S–M |
| U5–U8 | Mobile player polish; bulk-action consistency; a11y continuation; empty states | P2 | M | S–M |
| P6/P7 | AI status polling consolidation; FTS5 everywhere | P2 | M | S |
| C3–C5 | If repo goes public: demo world, screenshots, shareable read-only snapshot | P2 | M | S–L |
| C6/M7 | i18n, plugin architecture — **deferred** until demand exists | — | — | XL |

## Standing rules

- Keep the single-tenant, self-hosted philosophy — no rate limiting,
  multi-tenancy, or enterprise abstractions (AGENTS.md).
- SQLite stays the only database; AI stays local (Ollama/SwarmUI/whisper.cpp).
- Effort classes: S contained change · M cross-cutting feature · L subsystem ·
  XL epic (split before starting).
