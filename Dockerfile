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

COPY app/ ./app/
COPY static/ ./static/

VOLUME ["/data"]
ENV DB_PATH=/data/world.db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
