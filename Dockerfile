# ffmpeg/ffprobe — used to split a long session recording into chunks before sending each to Unsloth Studio's speech-to-text
# (app.ai._split_audio_into_chunks / _probe_audio_duration), to re-encode a recording over Studio's 25 MiB request limit, to assemble the
# live-recording archive, and for video posters, remuxes and the optional AV1 conversion. Every caller degrades gracefully when they are
# missing, so an image without them still works - just without those extras.
#
# They come as two static binaries from mwader/static-ffmpeg, pinned to one release by its multi-arch digest (docs/LEFTOVERS_PLAN.md,
# step 8): Debian's `ffmpeg` package pulled in ~448 MB of shared libraries (53% of the image), the two binaries are ~280 MB and carry
# every encoder the app uses (libsvtav1, libopus, libmp3lame, libx264). Every ffmpeg command the app runs was checked against this build.
# To update: pick a newer tag, take the digest of its manifest list, re-run those checks.
FROM mwader/static-ffmpeg:8.0.1@sha256:252705ff88532fa338e7065c21792756552f8fe7c212f84bc503d3c340689594 AS ffmpeg

# The Rust gateway (rust/nd-gateway, docs/RUST_GATEWAY.md): a ~2 MB static binary (musl) that answers /static/ and sends uploads, and passes
# everything else to the Python app. Dependencies are compiled in their own layer so a change to the gateway's code does not rebuild them.
FROM rust:1-slim AS gateway
WORKDIR /src
# the bundled SQLite (rusqlite) is C: it needs a compiler that targets musl
RUN apt-get update && apt-get install -y --no-install-recommends gcc musl-tools && rm -rf /var/lib/apt/lists/*
ENV CC_x86_64_unknown_linux_musl=musl-gcc CC_aarch64_unknown_linux_musl=musl-gcc
RUN rustup target add "$(uname -m)-unknown-linux-musl"
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/nd-gateway/Cargo.toml nd-gateway/Cargo.toml
COPY rust/nd-render/Cargo.toml nd-render/Cargo.toml
RUN mkdir -p nd-gateway/src nd-render/src && echo "fn main() {}" > nd-gateway/src/main.rs && touch nd-gateway/src/lib.rs nd-render/src/lib.rs \
    && cargo build --release --locked --target "$(uname -m)-unknown-linux-musl" -p nd-gateway; \
    rm -rf nd-gateway/src nd-render/src
COPY rust/nd-gateway/src nd-gateway/src
COPY rust/nd-render/src nd-render/src
RUN touch nd-gateway/src/main.rs nd-gateway/src/lib.rs nd-render/src/lib.rs \
    && cargo build --release --locked --target "$(uname -m)-unknown-linux-musl" -p nd-gateway \
    && cp "target/$(uname -m)-unknown-linux-musl/release/nd-gateway" /nd-gateway

FROM python:3.13-slim

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=gateway /nd-gateway /usr/local/bin/nd-gateway
COPY docker/entrypoint.sh /usr/local/bin/nd-entrypoint

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
# Build-time efficiency steps (docs/EFFICIENCY_PLAN.md): bundled maps as AVIF, thumbnails, map tile pyramids, minified + pre-compressed
# (.br / .gz, served as-is by app/http_perf.py) static files, and compiled bytecode for a faster first start.
RUN python -m app.static_thumbs --avif-maps && python -m app.map_tiles && python -m app.precompress --minify && python -m compileall -q app

# The commit this image was built from (set by .github/workflows/docker-publish.yml), shown by /health and
# Settings > System so "is the new image actually the one running?" has a one-glance answer. Declared
# after the COPYs so a new commit does not invalidate the dependency layers above.
ARG GIT_SHA=""
ENV ND_BUILD=${GIT_SHA}

VOLUME ["/data"]
ENV DB_PATH=/data/world.db
EXPOSE 8000

# --timeout-graceful-shutdown bounds how long uvicorn waits for open
# connections to drain before it runs the ASGI lifespan shutdown (see
# app/job_shutdown.py) — uvicorn's own default is None, meaning it waits
# INDEFINITELY for open connections first. An in-flight /api/ai/stream SSE
# response left open by an idle browser tab would then hold uvicorn past
# Docker's own stop grace period (see docker-compose.yml's
# stop_grace_period) straight to SIGKILL, and the lifespan shutdown —
# the part that checkpoints in-flight background jobs — would never run
# at all.
# --no-access-log: one line per request (including the polls) is noise and unbounded disk; app/perf_log.py keeps route timings and the
# slow-request log, and errors are still logged.
# docker/entrypoint.sh runs the gateway on 8000 with uvicorn (the command above, with --proxy-headers) on 127.0.0.1:8001 behind it; ND_GATEWAY=0
# runs uvicorn alone on 8000 as before.
CMD ["/usr/local/bin/nd-entrypoint"]
