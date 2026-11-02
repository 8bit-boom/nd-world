# nd-world: the Rust gateway is the product, Python is the reference

`main` is a Rust gateway (`rust/nd-gateway`, `nd-render`, `nd-perm`) in front of a Python FastAPI app. It is published as two images
(see README, "Images and run modes"): hybrid (`nd-world`, Python behind the gateway) and Rust-only (`nd-world-gateway`, `ND_PYTHON_FREE=1`).
The Python app stays: it is the oracle every Rust route is tested against. Do not delete Python code.
History: the Rust-only work happened on `rust-fork` and was promoted to `main` on 2026-11-02; the old hybrid-only `main` is the branch `hybrid-python-main`.

Read first: `docs/FORK_STATUS.md` (done / next), `docs/FORK_RUST_PORT_GUIDE.md` (the working loop), `docs/RUST_SWITCH_OFF.md` (every port),
`docs/HYBRID_VS_RUST_ONLY.md`, and for a session on the owner's PC with Docker `docs/LOCAL_SESSION_HANDOFF.md`.

## Rules for every change
1. **Python is the oracle.** The gateway answers only what is byte-identical to Python's answer and returns `None` for the unusual (next to Python the request
   is handed on). Read the Python handler first.
2. **A decline must not take a page down without Python.** In the Rust-only image (`config::python_free()`) every `return None` is a "Not available here".
   For any new reader ask what stored data would make it decline (accented names, rich-text notes, `null` where a list is expected, thousands of rows) and
   handle that in Rust when `python_free()` is on; keep the strict answer when Python is behind the gateway.
3. **Every port gets two tests**: a parity test (same request to both servers: `tests/test_gateway_golden.py`, `tests/test_gateway_ai.py`) and, when the route
   matters without Python, a Rust-only test (`_python_free_gateway(seed)`).
4. **Nothing hard-coded to a mode**: the footer / Settings / `/health` read `ND_BUILD`, `ND_PYTHON_FREE`, `ND_MODE` (Python `_build_info`, Rust `nd_render::build_info`; keep both identical).
5. **Run the whole suite before every push** (`bash scripts/gateway_suite.sh 4 all` plus `pytest -n 4`). Update `docs/FORK_STATUS.md` and `docs/RUST_SWITCH_OFF.md` with each batch.
6. **Never touch the owner's live data.** Real-data checks use a restored copy; the GM password is typed by the owner in their own shell, never in a prompt, commit or log.

## Practical
- Gateway build: `cd rust && cargo build --release -p nd-gateway --bins` (3-6 minutes, about 10 GB of disk; `cargo clean -p nd-gateway` frees space). It also builds `nd-assets`.
- The image build: `docker build -f Dockerfile.gateway -t nd-rs:dev .` (Rust-only, no Python stage) and `docker build -t nd-hybrid:dev .`.
- Tests that need the gateway skip when `rust/target/release/nd-gateway` is missing, so build first. Python 3.13 matches the CI.
- The Android app and desktop editor live in the sibling repository, not here.
