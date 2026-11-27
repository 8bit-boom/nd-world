# Trimming nd-world: no Python, no hybrid

Decided 2026-11-15. The live app already runs the Rust-only image, so none of this changes what runs; it removes what is left behind it. Chosen scope: remove Python and the hybrid
mode, keep the Rust routes and their "decline" branches as they are; keep the tests as gateway-only checks.
The repository source and test suite have now been converted to Rust-only assets and binaries.

## Stage 1 - images, compose, CI publish (done)

- `Dockerfile.gateway` is now `Dockerfile`; the hybrid Dockerfile and `docker/entrypoint.sh` are gone.
- `docker-publish.yml` builds that one image (smoke-tested before it is pushed) and pushes it as `<repo>-gateway` and, for installs still following it, as `<repo>`.
- `truenas-compose.yml`'s `world` service uses the gateway image; `docker-compose.yml` builds the one `Dockerfile`.
- README / CLAUDE.md describe one image.

## Stages 2-3 - freeze the oracle, move assets, delete Python (done, 2026-11-26, commit 8821548a)

Python was removed rather than frozen: `app/*.py`, `requirements.txt`, `import_*.py`, `scripts/gen_rust_data.py`, `scripts/golden.py` and the Python parity tests are gone, and the
templates, maps, game data, races and professions live under `assets/`. `rust/nd-render/data/*` is now the source (edited directly). Checks are Rust only: `cargo test` and
`rust/nd-gateway/tests/`. The gateway-vs-Python parity value of the old `tests/test_gateway_*.py` files is not recorded anywhere; new routes need their own Rust tests (CLAUDE.md rule 4).

## Stage 4 - Rust cleanup (not chosen; revisit later)

Remove `ND_PYTHON_FREE` switches and every `return None -> Python` path, `proxy.rs`, `ND_UPSTREAM`. Larger refactor across ~20 files; the behaviour does not change, so it can wait.
