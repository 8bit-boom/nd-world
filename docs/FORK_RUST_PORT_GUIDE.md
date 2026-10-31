# Starting a local fork that ports the rest to Rust (and Go where it truly helps)

For a Claude Code session on your own machine, with Docker, so it can run the containers and your real data. Written from what the port taught
(docs/RUST_SWITCH_OFF.md, docs/RUST_PORT_AUDIT.md, docs/HYBRID_VS_RUST_ONLY.md).

## 1. One-time setup
```
git clone git@github.com:8bit-boom/nd-world.git nd-world-rs && cd nd-world-rs
git checkout -b rust-fork
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-xdist      # the Python app is the ORACLE, keep it working
rustup toolchain install stable && rustup target add x86_64-unknown-linux-musl
docker build -f Dockerfile.gateway -t nd-rs:dev .              # the Python-free image
docker build -t nd-hybrid:dev .                                # the Python + gateway image (the reference)
```
Optional: `git remote rename origin upstream` and push your branch to your own repository, so upstream fixes can be merged with `git merge upstream/main`.

## 2. The working loop (keep it; it is what made the port safe)
1. Pick one route or one page. Read the Python handler (`app/routers/*.py`, `app/main.py`).
2. Write it in Rust in `rust/nd-gateway/src/`. Answer ONLY what is byte-identical to Python; return `None` (hand it to Python) for everything unusual.
3. Add a parity test (`tests/test_gateway_golden.py`, `tests/test_gateway_ai.py`): the same request to both servers, same status, same body. Add a
   Python-free check (`_python_free_gateway(seed)`) that the route works with no Python behind it.
4. `cd rust && cargo build --release -p nd-gateway`, then `bash scripts/gateway_suite.sh 4 all` (or just your test through `ND_TEST_BASE_URL`).
5. Document it in `docs/RUST_SWITCH_OFF.md`, commit, then run the whole suite before pushing.
Docker check after every batch: `docker run --rm -p 8991:8000 -v /tmp/copy:/data -e ND_PYTHON_FREE=1 -e ND_DECLINE_TRACE=1 -e SECRET_KEY=x -e GM_EMAIL=you -e GM_PASSWORD=pw nd-rs:dev`
on a COPY of your data (restore `/admin/backup.zip` into an empty folder), then `python3 scripts/python_free_entity_crawl.py ...`.

## 3. Rules that are not obvious (each was learned the hard way)
- **A decline is invisible next to Python and fatal without it.** Every "return None; // Python's" in a page builder is a 501 in the Python-free image.
  After each batch ask: what data makes this decline? (accented names, a rich-text note, `null` or a dict where a list is expected, 700 entities.)
  Handle it in Rust when `config::python_free()` is on; keep the strict answer when Python is behind the gateway.
- **Test with real-looking data, not seed data.** Restore your own backup into a copy and crawl every page of every list. The test database has no entities,
  maps, combats or sessions; the bugs lived there.
- **Exact text guards bite.** A JSON column posted by a browser has no spaces; a number written `1.0` is not the same token as `1`. Compare after
  normalising, never on the raw string.
- **Audit by what the pages CALL, not by the route list.** Grep `static/js` and `app/templates` for `fetch(` / `action=` and check each address against a
  Python-free gateway. Routes that "exist" by prefix can still miss their GET, POST or DELETE.
- **Keep the oracle.** The Python app and its tests stay until the Rust side has run a real month. Do not delete Python code in the fork.
- **Disk and time.** A full gateway build is 3-4 minutes, the suites 15-20; keep 10 GB free (`cargo clean -p nd-gateway` between big changes).

## 4. Backlog, easiest value first
1. Run the entity crawl and the other list crawls on your real copy; fix every `DECLINE` line (cheapest, biggest effect).
2. The remaining unported calls (docs/RUST_SWITCH_OFF.md, "Still Python-only"): imagegen tag-list download / activate / delete, `POST /api/ai/unsloth/update`,
   King in Yellow generate / plays, `POST /api/worlds`, template export for a missing row (501 instead of 404 without Python).
3. Make "missing row" answer 404 without Python (today 501) for every ID route.
4. Character `.pdf` export (reportlab): a PDF writer in Rust (`printpdf` or `lopdf`); exact bytes are not required, only the same content.
5. World Foundry export: needs markdown2's exact HTML; port markdown2's rules into `nd-render`, or accept `pulldown-cmark`'s output for this one export.
6. The ~445 "mixed" routes whose refusals (bad input) say a generic 400 without Python: port their wording only when `ND_DECLINE_TRACE` shows someone hits one.
7. Then, only if you still want less Python in the HYBRID image: move the build-time Python stage of `Dockerfile.gateway` (thumbnails, tile pyramids, minify) to Rust.

## 5. Where Go would (and would not) help
Rust already has the gateway, the renderer and the permission code, and mixing a second language adds a second toolchain, a second image layer and a
second set of bugs. Use **Go only for a standalone helper** that talks over HTTP or a pipe, where a mature Go library beats the Rust one: for example a
sidecar for the PDF export, or an ffmpeg orchestration tool. For everything that touches the database or renders a page, stay in Rust.

## 6. Prompt to paste into the local Claude Code session
> This is a fork of nd-world. Read docs/RUST_SWITCH_OFF.md, docs/RUST_PORT_AUDIT.md, docs/HYBRID_VS_RUST_ONLY.md and docs/FORK_RUST_PORT_GUIDE.md first.
> Goal: make the Python-free image (`Dockerfile.gateway`, `ND_PYTHON_FREE=1`) serve everything the hybrid image does, mostly in Rust (Go only for a
> standalone helper). Rules: every port gets a parity test against the Python app and a Python-free test; never answer differently from Python; a
> decline must not take a page down when there is no Python; keep Python working as the oracle. Start with section 4, item 1: build `nd-rs:dev`,
> restore my backup (I will give the path) into a copy, run `scripts/python_free_entity_crawl.py`, and fix every DECLINE line. Report after each batch
> with the test results and the image id.
