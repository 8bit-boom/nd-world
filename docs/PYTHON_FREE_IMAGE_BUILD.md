# Building and checking the Python-free image (`Dockerfile.gateway`)

For Claude Code (or a person) running **on a machine with Docker**, in a local clone of this repo. The cloud sandbox this was written in
has no Docker daemon, so `Dockerfile.gateway` has never been built there. Everything below is what to do the first time, in order, and what
to report back. Background: `docs/RUST_SWITCH_OFF.md` (what the gateway answers itself) and `docs/RUST_PORT_HANDOFF.md` (what is left).

## 0. What the image is

One process, no Python: the Rust gateway (`nd-gateway`, static musl build) answers every route it has ported itself. A request only Python
could serve gets a **501** with the reason (GET) or a generic **400** (a declined write). Python is used only in a throw-away build stage
(thumbnails, AVIF maps, tile pyramids, minified and pre-compressed static files) whose output is copied in. `ND_PYTHON_FREE=1` is set in the
image: it turns on the schema heal at boot, a random per-process `SECRET_KEY` when none is given, and the Python-free answers for the task /
job lists and perf readings.

Known gaps in this image (they answer 501/400, see the list in `docs/RUST_PORT_HANDOFF.md`): PDF text reading for AI attachments, the
character `.pdf` export, FastAPI's `/docs` and `/openapi.json`, the world Foundry export, anything needing outbound **HTTPS** (Hugging Face
search, King in Yellow web fetches; the gateway has no TLS client, so a Studio/Ollama URL must be `http://`), and the legacy Ollama back
end's status routes (a Unsloth Studio key is served).

## 1. Prerequisites

- Docker with BuildKit (`docker buildx version`). Allow ~6 GB free disk and a few minutes; the Rust build is the slow part (cached after the
  first time through the dependency layer).
- `git`, `curl`. Optional for step 5: Python 3.13 with `pip install -r requirements-dev.txt`, and a Rust toolchain.
- Check out the branch to test, with submodules/LFS if any are used (there are none at the time of writing).

## 2. Build

```bash
git rev-parse --short HEAD                      # the commit under test
docker build -f Dockerfile.gateway -t nd-world-gateway:test --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker images nd-world-gateway:test             # note the SIZE
```

What to expect, stage by stage:
1. `ffmpeg` stage: pulls a pinned static ffmpeg (digest-pinned). Needs network.
2. `gateway` stage: `rust:1-slim` + `musl-tools`, then **two** `cargo build --release --locked` passes (dependencies first, then the sources).
   `--locked` means `rust/Cargo.lock` must be committed and current; if it complains, run `cargo update -p <crate>` locally and commit.
3. `assets` stage: `python:3.13-slim`, installs `requirements.txt`, runs `app.static_thumbs --avif-maps`, `app.map_tiles`, `app.precompress --minify`.
4. final `debian:stable-slim`: ca-certificates, ffmpeg/ffprobe, the gateway binary, and `static/`, `app/templates/`, `app/maps/` from the assets stage.

If a stage fails, the usual causes are: a missing system library for the musl build (add it to the `apt-get install` line of the `gateway`
stage), `requirements.txt` pins that do not install on slim (add the build deps to the `assets` stage), or a template/static path that moved
(update the `COPY --from=assets` lines). Fix, rebuild, and note the change - the Dockerfile has not been exercised before.

## 3. Run it on a throw-away volume

```bash
docker volume create nd-pf-data
docker run -d --name nd-pf -p 8000:8000 -v nd-pf-data:/data \
  -e GM_EMAIL=gm@example.org -e GM_PASSWORD='choose-a-long-password' \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  nd-world-gateway:test
docker logs -f nd-pf        # expect: "booted a fresh database (schema + seed rows) at /data/world.db"
curl -s localhost:8000/__gateway/health
```

- First start creates the schema and seed rows (`startup.rs`) and the GM account from `GM_EMAIL` / `GM_PASSWORD`. Without `SECRET_KEY` a random one
  is made per start and every session ends at the next restart (a warning says so) - set it for anything but a test.
- Seeded bundled maps are copied into `/data/maps` on every start (never overwriting).
- Optional env: `ND_BACKUP_DIR` (+ `ND_BACKUP_INTERVAL_SECONDS`, `ND_BACKUP_KEEP`), `COOKIE_SECURE=true` behind HTTPS, `UNSLOTH_URL` /
  `UNSLOTH_API_KEY` for AI (http only), `MAX_UPLOAD_BYTES`, `ND_SLOW_MS`.
- A TrueNAS/compose service needs only: image, port 8000, a volume on `/data`, the env above. No second container, no `uvicorn`.

## 4. Smoke test (a human minute)

Open `http://localhost:8000`, sign in as the GM, then: create a world, add an entity with a markdown body, upload a picture, open `/maps`
(the bundled maps should list), roll dice at `/dice`, open `/settings`, create an invite and join with it in a private window (sign-up works
without Python), create a character. Anything that shows "Not available here" or "could not be completed" is a route still needing Python -
write down the URL.

## 5. The automated checks

Run these against the **container** (adjust the port) - they are the same scripts used in the cloud sandbox:

```bash
# (a) every parameterless GET route the app has, against the running container: lists the ones that answer 501/502
#     scripts/python_free_crawl.sh boots its OWN gateway from rust/target/release/nd-gateway, so first build that binary locally:
(cd rust && cargo build --release -p nd-gateway) && PYTHON=python bash scripts/python_free_crawl.sh 8990
#     expected today: "~17 of 179 parameterless GET routes need Python" (docs, HTTPS, legacy Ollama, world Foundry export, missing-parameter 422s)

# (b) the test suite through the gateway (needs Python 3.13 + requirements-dev.txt; this is also what CI does)
bash scripts/gateway_suite.sh 4 all          # four shards side by side; or:  scripts/gateway_suite.sh 4 1  (one shard)
```

`tests/test_gateway_golden.py::test_python_free_live_jobs_list_reads_the_rename_runs` starts a second, Python-free gateway from
`rust/target/release/nd-gateway` and checks the 501/400 answers - it skips itself if the binary is not built.

## 6. Compare against the normal image

```bash
docker build -t nd-world-python:test .                       # the normal Dockerfile
docker images | grep nd-world
docker stats --no-stream nd-pf                               # memory of the Python-free container after the smoke test
```

Report: both image sizes, idle and after-smoke-test memory, start-up time (`docker logs` timestamps from start to first `health` OK).

## 7. Upgrading an existing data volume

The gateway heals the schema additively at boot (`heal_schema`: missing tables/columns/indexes/triggers, never destructive). To try the
image on real data **copy first**: `docker run --rm -v <real-volume>:/from -v nd-pf-data:/to debian cp -a /from/. /to/`, start the test
container on the copy, and compare `/api/me`, `/worlds`, an entity page and an uploaded picture with the Python image on the same copy.
Do not point the test image at the production volume until that comparison is clean. Keep a backup (`/admin/backup.zip`) either way.

## 8. What to hand back

A short report: the commit built, build log excerpt for anything that had to change (and the change, committed on a branch), image sizes,
memory, the crawl result (how many 501/502 and which), the smoke-test findings, and whether the two-step/invite/upload/map flows worked.
If a Dockerfile edit was needed, note it in `Dockerfile.gateway`'s header comment too. Push to a branch; do not open a PR unless asked.

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container exits at once, "address in use" | port 8000 taken; use `-p 8080:8000` |
| Login works, then logs you out after a restart | no `SECRET_KEY` set (random per start) |
| `501 ... needs the Python app` on a page you expect | a route not ported yet - note the URL; see `docs/census/python_only.txt` |
| `400 ... could not be completed` on a form | a write the gateway declined (unusual input); Python would give the exact message |
| Pictures/maps missing | `static/` or `app/maps` not copied: check the `COPY --from=assets` lines |
| AI features say unreachable | the Studio/Ollama URL is `https://` (no TLS client) or not reachable from the container network |
| Build: `cargo ... --locked` fails | `Cargo.lock` out of date; regenerate and commit it |
