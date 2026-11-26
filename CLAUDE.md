# nd-world: Rust only

`main` is a Rust application (`rust/nd-gateway`, `nd-render`, `nd-perm`). **There is one image**:
`ghcr.io/8bit-boom/nd-world:latest`, built from `Dockerfile`. It contains no Python application,
Python runtime, hybrid proxy, or legacy Ollama/SwarmUI/ComfyUI runtime.

Shared templates and content live under `assets/`. Rust-only tests live under `rust/*/tests`
and in Rust crate test modules.

Read first: `docs/FORK_STATUS.md` (done / next), `docs/FORK_RUST_PORT_GUIDE.md` (the working loop), `docs/RUST_SWITCH_OFF.md` (every port),
`docs/HYBRID_VS_RUST_ONLY.md`, and for a session on the owner's PC with Docker `docs/LOCAL_SESSION_HANDOFF.md`.

## Rules for every change
1. Implement application behavior in Rust.
2. Keep shared templates, maps, game data, races and professions under `assets/`.
3. Never add a Python process or hybrid upstream fallback.
4. Add Rust tests for every new route or behavior.
5. Never touch the owner's live data. Real-data checks use a restored copy.

## Practical
- Gateway build: `cd rust && cargo build --release -p nd-gateway --bins` (3-6 minutes, about 10 GB of disk; `cargo clean -p nd-gateway` frees space). It also builds `nd-assets`.
- The image build: `docker build -t nd-rs:dev .` (Rust-only, no Python stage; there is no hybrid image any more).
- Tests that need the gateway skip when `rust/target/release/nd-gateway` is missing, so build first. Python 3.13 matches the CI.
- The Android app and desktop editor live in the sibling repository, not here.
