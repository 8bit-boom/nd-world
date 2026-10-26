"""Unsloth STT: transcode-then-split for oversize audio. Exact-match asserts."""
p = 'app/ai.py'
src = open(p, encoding='utf-8').read()

def rep(old, new, tag):
    global src
    if new in src:
        print("SKIP", tag)
        return
    assert src.count(old) == 1, tag + " count=" + str(src.count(old))
    src = src.replace(old, new, 1)
    print("OK  ", tag)

rep('''async def _transcribe_one_file_unsloth(path: Path) -> str:
    """One audio file through Studio's /v1/audio/transcriptions (OpenAI
    multipart dialect — file + model, verified Phase 0.5). The `model`
    name maps to an STT model managed in Studio's own Settings → Voice;
    a missing one 409s with Studio's instructions, surfaced verbatim via
    WhisperError so the existing job pipeline shows it to the GM. Note
    the whisper.cpp-specific knobs (glossary prompt, beam size, denoise
    flag) have no OpenAI-dialect equivalent — Studio's own STT settings
    own those."""
    from . import unsloth_extras as _unsloth_extras
    if not effective_llm_api_key():
        raise WhisperError("STT backend is set to Unsloth but no UNSLOTH_API_KEY is configured (Settings → System).")
    if not path.is_file():
        raise WhisperError(f"Audio file not found: {path.name}")
    try:
        text = await _unsloth_extras.stt(path.read_bytes(), path.name, model=get_stt_model())
    except _unsloth_extras.StudioError as exc:
        raise WhisperError(f"Unsloth Studio STT: {exc}") from exc
    return (text or "").strip()''',
'''async def _transcribe_one_file_unsloth(path: Path) -> str:
    """One audio file through Studio's /v1/audio/transcriptions (OpenAI
    multipart dialect — file + model, verified Phase 0.5). The `model`
    name maps to an STT model managed in Studio's own Settings → Voice;
    a missing one 409s with Studio's instructions, surfaced verbatim via
    WhisperError so the existing job pipeline shows it to the GM. Note
    the whisper.cpp-specific knobs (glossary prompt, beam size, denoise
    flag) have no OpenAI-dialect equivalent — Studio's own STT settings
    own those.

    Studio rejects request bodies over 25 MiB, so oversized files (the
    typical case: a long session recorded as high-bitrate FLAC) are first
    re-encoded to compact mono MP3 — lossless FLAC at music-grade bitrate
    shrinks ~10x for speech — and then, if the MP3 is still over the
    limit, split proportionally with the existing ffmpeg segment
    machinery. All pieces are transcribed in order and joined with
    newlines, so a multi-hour recording transcribes as one transcript."""
    from . import unsloth_extras as _unsloth_extras
    if not effective_llm_api_key():
        raise WhisperError("STT backend is set to Unsloth but no UNSLOTH_API_KEY is configured (Settings → System).")
    if not path.is_file():
        raise WhisperError(f"Audio file not found: {path.name}")

    parts = [path]
    cleanup_dirs = []
    try:
        if path.stat().st_size > _UNSLOTH_STT_MAX_BYTES:
            duration = await _probe_audio_duration(path)
            if not duration or duration <= 0:
                raise WhisperError(
                    f"{path.name} is {(path.stat().st_size + 1048575) // 1048576} MiB — over Unsloth "
                    "Studio's 25 MiB transcription request limit — and its duration "
                    "couldn't be read to split it (ffmpeg/ffprobe missing?). Convert it "
                    "to MP3/OGG first, or switch the STT backend to whisper.cpp.")
            import tempfile
            tmpdir = Path(tempfile.mkdtemp(prefix="nd-stt-"))
            cleanup_dirs.append(tmpdir)
            mp3 = await _transcode_audio_to_mp3(path, tmpdir)
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
                parts = mp3_parts
        texts = []
        for part in parts:
            try:
                text = await _unsloth_extras.stt(part.read_bytes(), part.name, model=get_stt_model())
            except _unsloth_extras.StudioError as exc:
                raise WhisperError(f"Unsloth Studio STT: {exc}") from exc
            texts.append((text or "").strip())
        return chr(10).join(t for t in texts if t)
    finally:
        import shutil
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)


async def _transcode_audio_to_mp3(path: Path, tmpdir: Path) -> Path:
    """Re-encode to 16 kHz mono MP3 at 64 kbps — transcription-grade audio
    at ~8 KB/second, which shrinks a music-grade FLAC roughly 10x. Raises
    WhisperError with the actionable reason if ffmpeg is missing or lacks
    the MP3 encoder."""
    import asyncio
    out = tmpdir / (path.stem + "-nd-stt.mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        _, _ = await proc.communicate()
    except FileNotFoundError:
        raise WhisperError(
            "ffmpeg isn't available in this deployment, so the oversized audio "
            "can't be prepared for Studio's 25 MiB limit — convert it to MP3/OGG "
            "manually, or switch the STT backend to whisper.cpp.")
    if proc.returncode != 0 or not out.is_file():
        raise WhisperError(
            f"Re-encoding {path.name} for Studio's 25 MiB limit failed "
            "(ffmpeg returned an error — it may be built without the MP3 "
            "encoder). Convert it to MP3/OGG manually, or switch the STT "
            "backend to whisper.cpp.")
    return out''',
    "unsloth stt: transcode-then-split")
open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("ALL APPLIED")
