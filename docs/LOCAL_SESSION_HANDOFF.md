# Local Claude Code session: build, test and continue the Rust port

> Update 2026-11-02: `rust-fork` is now `main` (the old hybrid-only `main` is `hybrid-python-main`); read `main` wherever this file says `rust-fork`. Sections 2 and 3 (the image build and the real-data crawl) were done by the local session; keep them as the checklist for the next change.

For a Claude Code session on your own PC, with Docker and your real data. Read this first, then `docs/FORK_STATUS.md` (the checklist, kept current) and
`docs/FORK_RUST_PORT_GUIDE.md` (the working loop and the rules learned the hard way).

## 0. What exists (state at `5256b68`)
- `main` is the hybrid app you play on (Python + Rust gateway). Do not change it from this work.
- `rust-fork` adds, on top: refusal logging (`DECLINE ...` on stderr), 404 for missing rows, the home page editor, King in Yellow plays/generate,
  `POST /api/worlds`, `POST /api/ai/unsloth/update`, imagegen tag lists (Python-free only), the character `.pdf` export (`minipdf.rs`), the world Foundry export,
  the template export, and **`nd-assets`**, a Rust program that replaced the Python build stage of `Dockerfile.gateway`.
- Python stays as the oracle. Never delete Python code in the fork.
- Ollama (`/api/ai/pull`, `/api/ai/test-chat`) is deliberately NOT ported: the owner uses Unsloth.

## 1. Setup
```
git clone git@github.com:8bit-boom/nd-world.git nd-world-rs && cd nd-world-rs
git checkout rust-fork
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt requirements-dev.txt pytest pytest-xdist
rustup toolchain install stable && rustup target add x86_64-unknown-linux-musl
cd rust && cargo build --release -p nd-gateway --bins && cd ..      # builds nd-gateway and nd-assets (3-5 min)
```
Keep 10 GB free. `cargo clean -p nd-gateway` between big changes if the disk is tight.

## 2. FIRST JOB: build the two images (nothing below has been built by Claude: the sandbox had no Docker daemon)
```
docker build -f Dockerfile.gateway -t nd-rs:dev .      # the Python-free image; the nd-assets step runs inside it
docker build -t nd-hybrid:dev .                        # the reference (Python + gateway)
```
Check, in this order, and fix what fails:
1. `docker build -f Dockerfile.gateway` completes. Things that could break: the cached-dependency layer no longer lists the `nd-assets` binary (it has
   `src/bin/nd-assets.rs`, so the stub step should still work); `brotli` crate download; musl link of `rav1e` (AVIF) - it already links in the gateway.
2. Time of the `RUN /nd-assets ...` layer. It took 9m41 single-threaded on a 4-core sandbox before the 4-way change; expect 3-4 minutes. If it is much slower,
   lower the AVIF effort in `rust/nd-gateway/src/imaging.rs` (`new_with_speed_quality(..., 6, ...)`) for the build only.
3. The image content: `docker run --rm --entrypoint sh nd-rs:dev -c 'ls /app/static/map_tiles; ls /app/static/maps | head; ls /app/static/style.css*'` shows
   a tile folder, `.avif` maps (no `.jpg` originals for the small ones), and `style.css`, `style.css.gz`, `style.css.br`.
4. JavaScript is **not minified** in this image (only compressed). That is a known, accepted difference. If page weight matters, port a safe minifier later.
5. The CSS is minified by `assets::minify_css`: open the site in a browser and look for broken styling (a regression there would be a CSS rule the minifier
   mangled; the unit test only covers a small sample).

## 3. Run the Python-free image on a COPY of your real data
Never mount the live data folder.
```
mkdir -p /tmp/copy && # restore your /admin/backup.zip into /tmp/copy (the folder that holds world.db and uploads/)
docker run --rm -p 8991:8000 -v /tmp/copy:/data -e ND_PYTHON_FREE=1 -e ND_DECLINE_TRACE=1 -e SECRET_KEY=x \
  -e GM_EMAIL=you@example.com -e GM_PASSWORD=pw nd-rs:dev 2> /tmp/decline.log
```
Type the GM password yourself in your own shell; never paste it into a chat.
Then crawl every page of every list and read the log:
```
python3 scripts/python_free_crawl_all.py http://127.0.0.1:8991 you@example.com   # prompts for the password
python3 scripts/python_free_entity_crawl.py ...                                  # the entity pages only
grep -E 'DECLINE|dtrace' /tmp/decline.log | sort | uniq -c | sort -rn | head -40
```
**This is the open bug**: on the owner's real data "Not available here" appears on entity pages in the Python-free image (screenshots showed entities 7014 and 7623;
"Auntie Kessa Morn and other normal names" failed too). Two causes were found and fixed on synthetic data (HTML notes, accented names) but the real cause is unknown.
The `DECLINE` lines name the step that refuses (`entity_detail` prints a line number per `?`). Find what is special about those rows
(`sqlite3 /tmp/copy/world.db "select * from entities where id=7014"` - look at columns that are NULL, JSON, very long, or contain odd text), make the Rust page
handle it when `config::python_free()` is on, add a regression test with that shape of data, rerun the crawl until it is clean.

## 4. Run the tests (Python is the oracle)
The in-process suite: `.venv/bin/python -m pytest -q -n 4`.
The gateway suites compare the gateway against Python on the same database. Start Python and the gateway, then point pytest at both:
```
# Python app (tests/serve.py) on 8951, the gateway on 8952:
ND_ACCEL_TOKEN=tok .venv/bin/python -m tests.serve --port 8951 &
ND_ACCEL_TOKEN=tok DB_PATH=$PWD/tests/_data/world.db ND_PBKDF2_ITERATIONS=1000 ND_TEST_HOOKS=1 \
  ND_RUST_ROUTES=login,dice_roll,live,pages,writes,ai,mcp,fans,auth SECRET_KEY=test-secret-key-not-for-production \
  ND_TEMPLATES_DIR=$PWD/app/templates ND_UPSTREAM=127.0.0.1:8951 ND_GATEWAY_LISTEN=127.0.0.1:8952 ND_STATIC_DIR=$PWD/static \
  ND_ACCEL_ROOT=$PWD/tests/_data rust/target/release/nd-gateway &
ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8951 ND_TEST_BASE_URL=http://127.0.0.1:8952 \
  .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_gateway_golden.py -k "your_test_name"
```
(`scripts/gateway_suite.sh` wraps the full run; use it for the whole thing, about 15-20 minutes, before every push.)
Tests that need the gateway binary skip when `rust/target/release/nd-gateway` is missing, so always build first.

## 5. Rules for every change (from docs/FORK_RUST_PORT_GUIDE.md)
1. Read the Python handler; port it in `rust/nd-gateway/src/`.
2. Answer only what is byte-identical to Python; return `None` for the unusual (next to Python it is simply forwarded).
3. In the Python-free image (`config::python_free()`), do NOT decline: handle the odd data (a decline is a 501 page there).
4. Add a parity test (same request to both servers) and, when the route matters without Python, a `_python_free_gateway(seed)` test.
5. Update `docs/FORK_STATUS.md`, commit, run the whole suite, push to `rust-fork` only.

## 6. Then continue the port (in this order)
1. Fix the entity-page 501 (section 3).
2. Run the crawl on every list page and item page; fix each `DECLINE` that real data hits (batch 3 of FORK_STATUS: only wording that real use reaches).
3. Anything the crawl cannot reach: the calendar, audio/video library pages, search and rules are not crawled yet - extend `scripts/python_free_crawl_all.py`.
4. After a month of clean real use of `nd-rs:dev` next to the hybrid image: decide whether the Python-free image becomes the default
   (`docker-compose.yml` / `truenas-compose.yml` already show how to run it as a second service, `world-pf`).
5. Ideas not started: port `hardware`/`fans` edge cases, a safe JavaScript minifier, remaining "Not available here" routes listed by the crawl.

## 7. Safety
- Do not touch the live data folder; always a restored copy.
- No Docker socket mounted into the app container.
- The GM password is typed by the owner in their own shell; never put it in a prompt, a commit or a log.
- Push to `rust-fork` (or a branch off it), never to `main`, until the owner says so.
