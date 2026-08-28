FROM python:3.12-slim

# ffmpeg/ffprobe — used to split a long session recording into chunks
# before sending each to Whisper (app.ai._split_audio_into_chunks /
# _probe_audio_duration), bounding a whisper.cpp repetition-loop failure
# to one chunk instead of the rest of a multi-hour file. That code
# already degrades gracefully (falls back to transcribing the whole file
# in one call, exactly like before this existed) if either binary is
# missing, so an image built without this layer still works — just
# without the extra resilience.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional: server-side speech enhancement (DeepFilterNet) for session
# recordings. Off by default — torch/torchaudio/deepfilternet add
# hundreds of MB and are irrelevant to everyone who doesn't use the
# feature. Build with `--build-arg INSTALL_DENOISE=true` to include it;
# app.ai.speech_enhancement_available() feature-detects at runtime, and
# the per-World toggle refuses to enable itself if this wasn't installed.
ARG INSTALL_DENOISE=false
COPY requirements-denoise.txt .
RUN if [ "$INSTALL_DENOISE" = "true" ]; then \
        pip install --no-cache-dir -r requirements-denoise.txt; \
    fi

COPY app/ ./app/
COPY static/ ./static/

VOLUME ["/data"]
ENV DB_PATH=/data/world.db
# DeepFilterNet downloads its model to this cache dir on first use;
# pointing it at the /data volume means the download survives container
# recreation instead of re-fetching every time (harmless/unused if
# INSTALL_DENOISE=false).
ENV XDG_CACHE_HOME=/data/.cache

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
