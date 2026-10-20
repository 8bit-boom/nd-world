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

FROM python:3.13-slim

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10", "--no-access-log"]
