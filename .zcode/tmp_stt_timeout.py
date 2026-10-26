"""STT: longer configurable read timeout + duration-capped pieces."""
# 1) unsloth_extras: STT-specific timeout (default 30 min, env-tunable)
p = 'app/unsloth_extras.py'
src = open(p, encoding='utf-8').read()

def rep(old, new, tag):
    global src
    if new in src:
        print("SKIP", tag)
        return
    assert src.count(old) == 1, tag + " count=" + str(src.count(old))
    src = src.replace(old, new, 1)
    print("OK  ", tag)

rep('''_AUDIO_TIMEOUT = _httpx.Timeout(600.0, connect=10.0)''',
'''_AUDIO_TIMEOUT = _httpx.Timeout(600.0, connect=10.0)
# STT's own read budget: transcription of a long piece on a CPU-only box
# (the GM's TrueNAS has no GPU yet) can far exceed TTS's 10 minutes —
# observed live: a Qwen3-ASR job died at exactly the old 600 s read
# timeout after 10m16s. 30 min default, env-tunable.
_STT_TIMEOUT = _httpx.Timeout(
    float(__import__("os").environ.get("UNSLOTH_STT_TIMEOUT_SECONDS", "1800")),
    connect=10.0)''',
    "stt timeout")

rep('''            resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                                data={"model": model}, headers=_headers(key))''',
'''            resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                                data={"model": model}, headers=_headers(key))''',
    "noop anchor (timeout applied below)")

# find the AsyncClient construction in stt() and switch its timeout
old_stt_client = '''    async with _httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as c:
        resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                            data={"model": model}, headers=_headers(key))'''
new_stt_client = '''    async with _httpx.AsyncClient(timeout=_STT_TIMEOUT, follow_redirects=True) as c:
        resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                            data={"model": model}, headers=_headers(key))'''
assert src.count(old_stt_client) == 1, "stt client anchor count=" + str(src.count(old_stt_client))
src = src.replace(old_stt_client, new_stt_client, 1)
print("OK    stt uses _STT_TIMEOUT")
open(p, 'w', encoding='utf-8', newline='\n').write(src)

# 2) ai.py: duration-capped pieces (each request <= 10 min of audio)
p = 'app/ai.py'
src = open(p, encoding='utf-8').read()
rep('''            mp3 = await _transcode_audio_to_mp3(path, tmpdir)
            if mp3.stat().st_size <= _UNSLOTH_STT_MAX_BYTES:
                parts = [mp3]
            else:
                # A recording long enough that even mono 64k MP3 exceeds the
                # limit: split the MP3 proportionally (every piece then fits
                # by construction — proportional sizing on the MP3's own
                # bitrate).
                chunk_seconds = _plan_unsloth_chunks(mp3.stat().st_size, duration)
                mp3_parts, split_dir = await _split_audio_into_chunks(mp3, chunk_seconds)
                if split_dir:
                    cleanup_dirs.append(split_dir)
                parts = mp3_parts''',
'''            mp3 = await _transcode_audio_to_mp3(path, tmpdir)
            # Piece length is capped by DURATION as well as size: a single
            # request carrying an hour of audio can outrun even a long read
            # timeout on a CPU-only box (observed live: a Qwen3-ASR job
            # died at the old 600 s read timeout). ~10 min per request
            # keeps each call comfortably inside the budget and makes
            # retries cheap (the audio-jobs pipeline re-runs pieces).
            if (mp3.stat().st_size <= _UNSLOTH_STT_MAX_BYTES
                    and duration <= _UNSLOTH_STT_CHUNK_SECONDS):
                parts = [mp3]
            else:
                size_plan = _plan_unsloth_chunks(mp3.stat().st_size, duration)
                chunk_seconds = min(size_plan or _UNSLOTH_STT_CHUNK_SECONDS,
                                     _UNSLOTH_STT_CHUNK_SECONDS)
                mp3_parts, split_dir = await _split_audio_into_chunks(mp3, chunk_seconds)
                if split_dir:
                    cleanup_dirs.append(split_dir)
                parts = mp3_parts''',
    "duration-capped split")

rep('''_UNSLOTH_STT_MAX_BYTES = 23 * 1024 * 1024''',
'''_UNSLOTH_STT_MAX_BYTES = 23 * 1024 * 1024
# Max MINUTES of audio per transcription request — duration cap alongside
# the byte cap (see _transcribe_one_file_unsloth).
_UNSLOTH_STT_CHUNK_SECONDS = 10 * 60''',
    "chunk seconds constant")
open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("ALL APPLIED")
