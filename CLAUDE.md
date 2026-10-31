# nd-world, branch `rust-fork`: port everything to Rust

This branch exists to move as much of nd-world as possible from Python to Rust (Go only for a standalone helper). `main` stays the product: the
hybrid image (Python + Rust gateway) is what runs the game. Nothing here may make `main`'s behaviour worse.

Read first, in this order: `docs/FORK_STATUS.md` (what is done, what is next), `docs/FORK_RUST_PORT_GUIDE.md` (setup and the working loop),
`docs/RUST_SWITCH_OFF.md` (every port so far), `docs/HYBRID_VS_RUST_ONLY.md`.

## Rules (each was learned from a real failure)
1. **Never answer differently from Python.** The Python app is the oracle. A port answers only what is byte-identical; anything unusual returns `None`
   (the gateway hands it to Python in the hybrid image).
2. **A decline must not take a page down without Python.** In the Python-free image (`ND_PYTHON_FREE=1`) every `return None` is a "Not available here".
   For each port ask: what stored data makes this decline (accented names, rich-text notes, `null` or a dict where a list is expected, big worlds)? Handle it
   in Rust when `config::python_free()` is on; keep the strict answer when Python is behind the gateway.
3. **Every port gets two tests**: a parity test (same request to both servers, same status and body: `tests/test_gateway_golden.py`,
   `tests/test_gateway_ai.py`) and a Python-free test (`_python_free_gateway(seed)`).
4. **Test with real-looking data.** Restore a backup into a COPY and crawl every page of every list (`scripts/python_free_entity_crawl.py`; extend it
   for other lists). Seed data has no entities, maps, combats or sessions, which is where the bugs were. Never touch the live data folder.
5. **Audit by what the pages CALL** (`fetch(` in `static/js`, forms in `app/templates`), not by the route list.
6. **Keep Python working.** Do not delete Python code on this branch; it is the oracle and the fallback.
7. **Run the whole suite before every push** (`bash scripts/gateway_suite.sh 4 all` plus the in-process suite). Push only to `rust-fork`.
8. **Update `docs/FORK_STATUS.md` and `docs/RUST_SWITCH_OFF.md` with each batch**, and report: what was ported, the tests, the commit.

## Environment notes
- Gateway build: `cd rust && cargo build --release -p nd-gateway` (3-4 minutes, several cores, about 10 GB of disk; free space first with
  `cargo clean -p nd-gateway` if needed).
- The Python tests need a venv (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-xdist`); the census numbers in the
  docs were taken on Python 3.13, so a failure on 3.11 may be the version, not the gateway.
- This container may have no Docker and no ffmpeg: crawl with `--binary`, report the git commit, and leave the image checks to the NAS host
  (`docker build -f Dockerfile.gateway -t nd-rs:dev .`).
