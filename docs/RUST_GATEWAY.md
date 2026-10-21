# The Rust gateway

Phase 1 of `docs/RUST_REWRITE_PLAN.md`. A small Rust program (`rust/nd-gateway`, Axum + Tokio, a ~2 MB static binary) sits on the public port in
front of the Python app. It answers what needs no Python and passes everything else on; nothing a browser sees changes.

```
browser ──> nd-gateway :8000 ──┬── /static/…        straight from disk (pre-compressed .br / .gz twins, ranges, 304)
                               ├── /uploads/…       Python checks who may have it, the gateway sends the file
                               └── everything else  streamed to the Python app on 127.0.0.1:8001
```

## What it does

| Request | Answered by |
|---|---|
| `GET/HEAD /static/…` with a file | the gateway, with the same rules as `app/http_perf.py`: the build's `.br` / `.gz` twin if the browser accepts it and the twin is not older than the file, `Cache-Control: public, max-age=31536000, immutable`, an old map link (`.jpg`/`.png`/`.webp`) redirected (301) to the `.avif` / `.webp` the build made, ETag / Last-Modified / 304, byte ranges |
| `/static/…` that is missing, odd (`..`, backslash, encoded dots, a symlink out of `static/`), a `.br` / `.gz` itself, or text of 1 KB+ with no twin that the browser wants gzipped | Python, exactly as before |
| `/uploads/…` | Python decides (login, world access, strict-map guard, live-recording ban, display copies, thumbnail fallbacks - all unchanged) and, when the request came through the gateway, answers with `X-Nd-Accel-Path: <file>` and its headers instead of reading the file. The gateway sends the file: ETag, 304, ranges (`206`, `416`), HEAD. No Python thread is held during a big download |
| anything else | Python, streamed in both directions (uploads, server-sent events, downloads are never held in memory) |
| `GET /__gateway/health` | the gateway: `{"gateway":"ok","upstream":true}` |

When Python is not up yet (start-up takes a couple of seconds) the gateway shows a "starting" page that reloads itself (502 + `Retry-After`).

## Safety rules (each has a test in `rust/nd-gateway/tests/gateway.rs`)

- `X-Nd-Accel` / `X-Nd-Accel-Path` from a browser are dropped. Python hands a file over only for a request that carries the container's secret token
  (`ND_ACCEL_TOKEN`, random per start, never leaves the container); the gateway only follows a path that is **inside the data folder**
  (`ND_ACCEL_ROOT`, default the folder of `DB_PATH`), after resolving symlinks. Without a token the header is ignored and Python sends files as before.
- The address Python sees as the client (the failed-login limiter uses it) is the gateway's peer address. A browser cannot choose it by sending
  `X-Forwarded-For`. `X-Forwarded-Proto/-Host/-Port`, `Forwarded` and `X-Real-IP` from a browser are dropped too.
  Behind another reverse proxy that sets them (Cloudflare Tunnel, Traefik, nginx) set `ND_GATEWAY_TRUST_PROXY=1`: the chain is then kept and the
  gateway's peer is appended.
- The login routes are not answered by Rust. They stay in Python with their rate limiter.
- `rust/nd-gateway/src/session.rs` reads the app's session cookie (Starlette / itsdangerous format) and is tested against cookies made by
  Python (`scripts/gen_session_fixture.py`). **Nothing uses it to gate a request yet** - it is there for phase 2, when the gateway starts to
  answer pages itself.

## Running it

The Docker image does it (`docker/entrypoint.sh`): gateway on 8000, `uvicorn --proxy-headers` on `127.0.0.1:8001`. Compose files and the
published port are unchanged.

| Variable | Default | |
|---|---|---|
| `ND_GATEWAY` | `1` | `0` runs Python alone on 8000, exactly as before the gateway (rollback: set it and restart) |
| `ND_GATEWAY_TRUST_PROXY` | `0` | see above |
| `ND_GATEWAY_LISTEN` | `0.0.0.0:8000` | |
| `ND_UPSTREAM`, `ND_STATIC_DIR`, `ND_ACCEL_ROOT` | `127.0.0.1:8001`, `/app/static`, folder of `DB_PATH` | set by the entrypoint |

Without Docker: `cd rust && cargo build --release`, start the app (`uvicorn app.main:app --port 8001 --proxy-headers`) with `ND_ACCEL_TOKEN=<secret>`,
then `ND_ACCEL_TOKEN=<secret> ND_UPSTREAM=127.0.0.1:8001 ND_STATIC_DIR=$PWD/static ND_ACCEL_ROOT=<data folder> rust/target/release/nd-gateway`.

## How it is checked

- `cargo test` (`rust/nd-gateway/tests/gateway.rs`): the gateway against a stand-in for Python - twins, stale twins, ranges, 304, traversal and symlink
  escapes, forged headers, streaming, Python down, hand-over inside and outside the data folder, session cookies.
- `tests/test_accel.py`: Python's side (right token, wrong token, no token, strangers, the live-recording ban).
- CI job `gateway` (`.github/workflows/tests.yml`): the whole Python suite over HTTP **through** the gateway.
- `scripts/golden.py compare --a <direct> --b <through the gateway>`: pages answer the same.
