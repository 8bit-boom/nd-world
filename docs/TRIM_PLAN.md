# Trimming nd-world: no Python, no hybrid

Decided 2026-11-15. The live app already runs the Rust-only image, so none of this changes what runs; it removes what is left behind it. Chosen scope: remove Python and the hybrid
mode, keep the Rust routes and their "decline" branches as they are; keep the tests as gateway-only checks.

## Stage 1 - images, compose, CI publish (done)

- `Dockerfile.gateway` is now `Dockerfile`; the hybrid Dockerfile and `docker/entrypoint.sh` are gone.
- `docker-publish.yml` builds that one image (smoke-tested before it is pushed) and pushes it as `<repo>-gateway` and, for installs still following it, as `<repo>`.
- `truenas-compose.yml`'s `world` service uses the gateway image; `docker-compose.yml` builds the one `Dockerfile`.
- README / CLAUDE.md describe one image.

## What blocks deleting `app/` (measured 2026-11-15)

- `app/` is not only Python: the gateway image copies `app/templates` (the Jinja templates it renders), `app/maps` (bundled maps) and reads data under `app/game_data`,
  `app/core_rules.md`, `app/races`, `app/professions`. These move out first.
- 360 of 403 test files import `app.*`: 257 `app.database`, 253 `app.models` (the tests seed the database through SQLAlchemy), 59 `app.ai`, 22 `app.main`. 145 files need only
  `database` / `models` / `auth`; 258 use other Python modules (they test Python code and go with it).
- The eight `tests/test_gateway_*.py` files (the largest is ~10k lines) get their value from comparing the gateway with Python's answer. Deleting Python first would delete that value.
- `scripts/gen_rust_data.py` generates `rust/nd-render/data/*` (app data, openapi, rules) by importing the Python app.

## Stage 2 - freeze the oracle

Record Python's answer for every request the parity tests make (status, headers that matter, body) into `tests/golden/*.json`, then make each comparison "gateway == recorded". The
recording tool exists from phase 0 (`docs/RUST_GATEWAY.md`, golden-response tool). Done when the suite passes with Python not running.

## Stage 3 - move assets, replace the seeder, delete Python

1. Move `app/templates`, `app/maps`, `app/game_data`, `app/core_rules.md`, `app/races`, `app/professions` to top-level `assets/` (update `ND_TEMPLATES_DIR` etc., the Dockerfile and the tests' paths).
2. Seeding: keep `tests/support/` with the minimal SQLAlchemy models + schema the fixtures need (test tooling only), or seed through the gateway's own routes; decide when stage 2 shows the size.
3. Delete `app/` code, `requirements.txt`, `import_*.py`, the in-process Python tests, the pytest CI job's Python app start; `gen_rust_data.py` becomes "data is the source" (the generated files are
   committed and edited directly).
4. Update CLAUDE.md (drop "Python is the oracle"), the docs index.

## Stage 4 - Rust cleanup (not chosen; revisit later)

Remove `ND_PYTHON_FREE` switches and every `return None -> Python` path, `proxy.rs`, `ND_UPSTREAM`. Larger refactor across ~20 files; the behaviour does not change, so it can wait.
