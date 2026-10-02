"""Tests for the speech-to-text pipeline in app/ai.py: chunking a long recording,
checkpoint/resume, partial-transcript salvage on a mid-way failure, repetition-loop
collapsing — plus the guarantee that no whisper.cpp sidecar surface is left.

Unsloth Studio is the only speech-to-text backend; every Studio call is mocked at
`ai._transcribe_one_file`, so these only exercise this app's own orchestration.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import ai as ai_module
from app import unsloth_extras as ux
from app.job_shutdown import JobInterrupted

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


def _chunk_fakes(tmp_path, n=3, texts=None):
    chunk_paths = [tmp_path / f"c{i}.mp3" for i in range(n)]
    for p in chunk_paths:
        p.write_bytes(b"x")
    texts = texts or [f"part {i}" for i in range(n)]

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return chunk_paths, None

    async def fake_transcribe_one(path):
        return texts[chunk_paths.index(path)]

    return chunk_paths, fake_probe, fake_split, fake_transcribe_one


# ── ported pipeline tests ────────────────────────────────────────────────────

def test_collapse_leaves_short_runs_untouched():
    text = "Да.\nНет.\nДа.\nДа.\nОкей."
    assert ai_module._collapse_repeated_transcript_lines(text) == text


def test_collapse_removes_long_repeated_runs():
    text = "\n".join(["Танос."] * 25 + ["Дальше по сюжету."])
    assert ai_module._collapse_repeated_transcript_lines(text) == "Танос.\nДальше по сюжету."


def test_collapse_respects_custom_threshold():
    text = "\n".join(["A"] * 3)
    assert ai_module._collapse_repeated_transcript_lines(text, min_repeat=3) == "A"
    assert ai_module._collapse_repeated_transcript_lines(text, min_repeat=4) == "A\nA\nA"


def test_collapse_handles_multiple_separate_runs():
    text = "\n".join(["X"] * 5 + ["mid line"] + ["Y"] * 6)
    assert ai_module._collapse_repeated_transcript_lines(text) == "X\nmid line\nY"


def test_collapse_empty_string_is_a_noop():
    assert ai_module._collapse_repeated_transcript_lines("") == ""


def test_collapse_no_repeats_returns_input_unchanged():
    text = "line one\nline two\nline three"
    assert ai_module._collapse_repeated_transcript_lines(text) == text


@pytest.mark.asyncio
async def test_probe_audio_duration_parses_successful_output(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"185.42\n", b""

    async def _fake_exec(*a, **kw):
        return _FakeProc()

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _fake_exec)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    assert await ai_module._probe_audio_duration(f) == 185.42


@pytest.mark.asyncio
async def test_probe_audio_duration_none_on_nonzero_returncode(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod

    class _FakeProc:
        returncode = 1
        async def communicate(self):
            return b"", b"ffprobe: no such file"

    async def _fake_exec(*a, **kw):
        return _FakeProc()

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _fake_exec)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    assert await ai_module._probe_audio_duration(f) is None


@pytest.mark.asyncio
async def test_probe_audio_duration_none_when_ffprobe_missing(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod

    async def _raise(*a, **kw):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _raise)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    assert await ai_module._probe_audio_duration(f) is None


@pytest.mark.asyncio
async def test_split_audio_into_chunks_success_path(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod
    from pathlib import Path as _Path

    class _FakeProc:
        def __init__(self, args):
            self._args = args

        returncode = 0

        async def communicate(self):
            # Last arg is the ffmpeg output pattern: <tmpdir>/chunk_%04d<ext>
            pattern = _Path(self._args[-1])
            outdir, suffix = pattern.parent, pattern.suffix
            (outdir / f"chunk_0000{suffix}").write_bytes(b"a")
            (outdir / f"chunk_0001{suffix}").write_bytes(b"b")
            return b"", b""

    async def _fake_exec(*a, **kw):
        return _FakeProc(a)

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _fake_exec)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    chunks, tmpdir = await ai_module._split_audio_into_chunks(f, 600)
    try:
        assert len(chunks) == 2
        assert tmpdir is not None
        assert all(c.parent == tmpdir for c in chunks)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_split_audio_into_chunks_falls_back_on_ffmpeg_error(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod

    class _FakeProc:
        returncode = 1
        async def communicate(self):
            return b"", b"ffmpeg: some error"

    async def _fake_exec(*a, **kw):
        return _FakeProc()

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _fake_exec)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    chunks, tmpdir = await ai_module._split_audio_into_chunks(f, 600)
    assert chunks == [f]
    assert tmpdir is None


@pytest.mark.asyncio
async def test_split_audio_into_chunks_falls_back_when_ffmpeg_missing(tmp_path, monkeypatch):
    import asyncio as _asyncio_mod

    async def _raise(*a, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", _raise)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    chunks, tmpdir = await ai_module._split_audio_into_chunks(f, 600)
    assert chunks == [f]
    assert tmpdir is None


@pytest.mark.asyncio
async def test_transcribe_audio_skips_chunking_for_short_clip(tmp_path, monkeypatch):
    calls = []

    async def fake_probe(path):
        return 60.0  # well under the chunking threshold

    async def fake_transcribe_one(path):
        calls.append(path)
        return "short clip text"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    assert await ai_module.transcribe_audio(f) == "short clip text"
    assert calls == [f]


@pytest.mark.asyncio
async def test_transcribe_audio_chunks_long_clip_and_reports_progress(tmp_path, monkeypatch):
    chunk_paths = [tmp_path / "c1.mp3", tmp_path / "c2.mp3", tmp_path / "c3.mp3"]
    for p in chunk_paths:
        p.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0  # well over the chunking threshold

    async def fake_split(path, chunk_seconds):
        return chunk_paths, None  # None: nothing for the caller to clean up in this test

    async def fake_transcribe_one(path):
        return f"part {chunk_paths.index(path)}"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    progress_calls = []
    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    result = await ai_module.transcribe_audio(f, on_progress=lambda c, t: progress_calls.append((c, t)))
    assert result == "part 0\npart 1\npart 2"
    assert progress_calls == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.asyncio
async def test_transcribe_audio_falls_back_when_split_returns_single_file(tmp_path, monkeypatch):
    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return [path], None  # ffmpeg unavailable/failed — see _split_audio_into_chunks

    async def fake_transcribe_one(path):
        return "whole file text"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    progress_calls = []
    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    result = await ai_module.transcribe_audio(f, on_progress=lambda c, t: progress_calls.append((c, t)))
    assert result == "whole file text"
    assert progress_calls == []  # no chunking actually happened, so no progress signal


@pytest.mark.asyncio
async def test_transcribe_audio_removes_tmpdir_for_a_single_segment_split(tmp_path, monkeypatch):
    """Regression test: _split_audio_into_chunks can return a single chunk
    WITH a real tmpdir (e.g. a clip just over the chunking threshold
    producing exactly one segment) — the tmpdir cleanup used to sit outside
    this branch's early return, leaking a full stream-copy of the
    recording on every such case."""
    real_tmpdir = tmp_path / "split_tmp"
    real_tmpdir.mkdir()
    chunk_path = real_tmpdir / "chunk_0000.mp3"
    chunk_path.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return [chunk_path], real_tmpdir

    async def fake_transcribe_one(path):
        return "whole file text"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    result = await ai_module.transcribe_audio(f)
    assert result == "whole file text"
    assert not real_tmpdir.exists()


@pytest.mark.asyncio
async def test_transcribe_audio_removes_tmpdir_even_when_transcription_fails(tmp_path, monkeypatch):
    real_tmpdir = tmp_path / "split_tmp"
    real_tmpdir.mkdir()
    chunk_path = real_tmpdir / "chunk_0000.mp3"
    chunk_path.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return [chunk_path], real_tmpdir

    async def fake_transcribe_one(path):
        raise ai_module.SttError("studio unreachable")

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    with pytest.raises(ai_module.SttError):
        await ai_module.transcribe_audio(f)
    assert not real_tmpdir.exists()


def test_stt_chunk_seconds_has_a_sane_floor(monkeypatch):
    """An accidental 0/negative STT_CHUNK_SECONDS would otherwise ask
    ffmpeg to produce an enormous number of tiny segments."""
    assert ai_module.STT_CHUNK_SECONDS >= 60.0


def test_stt_chunk_min_duration_always_exceeds_chunk_seconds():
    """A clip just over the chunking threshold should always split into at
    least two real chunks, never a single-segment no-op split."""
    assert ai_module._STT_CHUNK_MIN_DURATION > ai_module.STT_CHUNK_SECONDS


@pytest.mark.asyncio
async def test_transcribe_audio_chunking_applies_repeat_collapsing(tmp_path, monkeypatch):
    chunk_paths = [tmp_path / "c1.mp3", tmp_path / "c2.mp3"]
    for p in chunk_paths:
        p.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return chunk_paths, None

    async def fake_transcribe_one(path):
        if chunk_paths.index(path) == 0:
            return "loop\nloop\nloop\nloop"
        return "normal text"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    assert await ai_module.transcribe_audio(f) == "loop\nnormal text"


@pytest.mark.asyncio
async def test_transcribe_audio_mid_chunk_failure_preserves_prior_chunks_as_partial(tmp_path, monkeypatch):
    """Regression test: a mid-way chunk failure (e.g. the studio container
    restarting 3 hours into a 4-hour session) used to discard every
    already-transcribed chunk along with the error. The raised SttError
    must now carry everything transcribed before the failure as
    partial_transcript, so a caller can save it rather than losing hours
    of completed work."""
    chunk_paths = [tmp_path / f"c{i}.mp3" for i in range(4)]
    for p in chunk_paths:
        p.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return chunk_paths, None

    async def fake_transcribe_one(path):
        idx = chunk_paths.index(path)
        if idx == 2:
            raise ai_module.SttError("studio restarted")
        return f"part {idx}"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    with pytest.raises(ai_module.SttError) as excinfo:
        await ai_module.transcribe_audio(f)

    assert "part 3 of 4" in str(excinfo.value)
    assert "studio restarted" in str(excinfo.value)
    assert excinfo.value.partial_transcript == "part 0\npart 1"


@pytest.mark.asyncio
async def test_transcribe_audio_failure_on_the_first_chunk_has_no_partial(tmp_path, monkeypatch):
    chunk_paths = [tmp_path / f"c{i}.mp3" for i in range(3)]
    for p in chunk_paths:
        p.write_bytes(b"x")

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        return chunk_paths, None

    async def fake_transcribe_one(path):
        raise ai_module.SttError("studio unreachable")

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"x")
    with pytest.raises(ai_module.SttError) as excinfo:
        await ai_module.transcribe_audio(f)
    assert excinfo.value.partial_transcript == ""
    assert str(excinfo.value) == "studio unreachable"  # no wrapping when there's nothing to salvage


@pytest.mark.asyncio
async def test_transcribe_calls_on_checkpoint_after_every_chunk(tmp_path, monkeypatch):
    _chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path)
    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    checkpoints = []
    result = await ai_module.transcribe_audio(f, on_checkpoint=checkpoints.append)

    assert result == "part 0\npart 1\npart 2"
    assert [c["chunks_done"] for c in checkpoints] == [1, 2, 3]
    assert all(c["phase"] == "transcribe" for c in checkpoints)
    assert all(c["chunk_total"] == 3 for c in checkpoints)
    assert all(c["audio_size"] == f.stat().st_size for c in checkpoints)
    assert all(c["chunk_seconds"] == ai_module.STT_CHUNK_SECONDS for c in checkpoints)
    assert checkpoints[0]["text"] == "part 0"
    assert checkpoints[-1]["text"] == "part 0\npart 1\npart 2"


@pytest.mark.asyncio
async def test_transcribe_on_checkpoint_not_called_for_an_unchunked_clip(tmp_path, monkeypatch):
    async def fake_probe(path):
        return 5.0  # short — skips chunking entirely

    async def fake_transcribe_one(path):
        return "whole clip"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "short.mp3"
    f.write_bytes(b"x")
    checkpoints = []
    result = await ai_module.transcribe_audio(f, on_checkpoint=checkpoints.append)
    assert result == "whole clip"
    assert checkpoints == []


@pytest.mark.asyncio
async def test_transcribe_resumes_from_a_matching_checkpoint_and_skips_done_chunks(tmp_path, monkeypatch):
    chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path)
    called_with = []

    async def tracking_transcribe_one(path):
        called_with.append(path)
        return await fake_transcribe_one(path)

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", tracking_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    resume = {
        "phase": "transcribe", "chunks_done": 1, "chunk_total": 3,
        "chunk_seconds": ai_module.STT_CHUNK_SECONDS, "audio_size": f.stat().st_size,
        "text": "part 0",
    }
    result = await ai_module.transcribe_audio(f, resume=resume)
    assert result == "part 0\npart 1\npart 2"
    assert called_with == [chunk_paths[1], chunk_paths[2]]  # chunk 0 was skipped, not re-transcribed


@pytest.mark.asyncio
async def test_transcribe_resumed_result_contains_both_the_prior_and_new_text(tmp_path, monkeypatch):
    _chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path, texts=["irrelevant", "part 1", "part 2"])
    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    resume = {
        "phase": "transcribe", "chunks_done": 1, "chunk_total": 3,
        "chunk_seconds": ai_module.STT_CHUNK_SECONDS, "audio_size": f.stat().st_size,
        "text": "part 0 from before the restart",
    }
    result = await ai_module.transcribe_audio(f, resume=resume)
    assert result == "part 0 from before the restart\npart 1\npart 2"


@pytest.mark.asyncio
async def test_transcribe_discards_a_checkpoint_with_a_different_chunk_total(tmp_path, monkeypatch):
    chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path)
    called_with = []

    async def tracking_transcribe_one(path):
        called_with.append(path)
        return await fake_transcribe_one(path)

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", tracking_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    resume = {
        "phase": "transcribe", "chunks_done": 1, "chunk_total": 99,  # doesn't match this call's 3 chunks
        "chunk_seconds": ai_module.STT_CHUNK_SECONDS, "audio_size": f.stat().st_size,
        "text": "stale",
    }
    result = await ai_module.transcribe_audio(f, resume=resume)
    assert result == "part 0\npart 1\npart 2"  # started over, not spliced onto "stale"
    assert called_with == chunk_paths  # every chunk re-transcribed, none skipped


@pytest.mark.asyncio
async def test_transcribe_discards_a_checkpoint_with_a_different_audio_size(tmp_path, monkeypatch):
    _chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path)
    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    resume = {
        "phase": "transcribe", "chunks_done": 1, "chunk_total": 3,
        "chunk_seconds": ai_module.STT_CHUNK_SECONDS, "audio_size": f.stat().st_size + 1,  # mismatch
        "text": "stale",
    }
    result = await ai_module.transcribe_audio(f, resume=resume)
    assert result == "part 0\npart 1\npart 2"


@pytest.mark.asyncio
async def test_transcribe_raises_job_interrupted_at_the_next_chunk_boundary_when_should_stop(tmp_path, monkeypatch):
    chunk_paths, fake_probe, fake_split, fake_transcribe_one = _chunk_fakes(tmp_path)
    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_transcribe_one)

    calls = {"n": 0}
    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # stop right before the second chunk

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    checkpoints = []
    with pytest.raises(JobInterrupted):
        await ai_module.transcribe_audio(f, should_stop=should_stop, on_checkpoint=checkpoints.append)
    assert len(checkpoints) == 1  # chunk 0's checkpoint was saved before the interrupt


@pytest.mark.asyncio
async def test_transcribe_partial_transcript_on_stt_error_includes_the_resumed_prefix(tmp_path, monkeypatch):
    chunk_paths, fake_probe, fake_split, _fake_transcribe_one = _chunk_fakes(tmp_path)

    async def failing_transcribe_one(path):
        if path == chunk_paths[2]:
            raise ai_module.SttError("boom")
        return "part 1"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(ai_module, "_transcribe_one_file", failing_transcribe_one)

    f = tmp_path / "long.mp3"
    f.write_bytes(b"audio-bytes")
    resume = {
        "phase": "transcribe", "chunks_done": 1, "chunk_total": 3,
        "chunk_seconds": ai_module.STT_CHUNK_SECONDS, "audio_size": f.stat().st_size,
        "text": "part 0 from before the restart",
    }
    with pytest.raises(ai_module.SttError) as exc_info:
        await ai_module.transcribe_audio(f, resume=resume)
    assert "part 0 from before the restart" in exc_info.value.partial_transcript
    assert "part 1" in exc_info.value.partial_transcript


# ── status, debug info, library clips ────────────────────────────────────────

@pytest.mark.asyncio
async def test_stt_status_needs_a_studio_key(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "")
    st = await ai_module.stt_status()
    assert st["ok"] is False and "API key" in st["reason"]


@pytest.mark.asyncio
async def test_stt_status_probes_studio(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(ai_module, "effective_llm_url", lambda: "http://studio:8000")

    async def ok():
        return []

    monkeypatch.setattr(ux, "hub_cached", ok)
    assert (await ai_module.stt_status()) == {"ok": True, "backend": "unsloth", "url": "http://studio:8000"}

    async def down():
        raise ux.StudioError("Unsloth Studio unreachable: ConnectError", 503)

    monkeypatch.setattr(ux, "hub_cached", down)
    st = await ai_module.stt_status()
    assert st["ok"] is False and "unreachable" in st["reason"] and st["url"] == "http://studio:8000"


@pytest.mark.asyncio
async def test_debug_info_reports_stt_not_whisper(monkeypatch):
    async def fake_status():
        return {"ok": True, "backend": "unsloth", "url": "http://studio:8000"}

    class _C:
        async def list(self):
            from types import SimpleNamespace
            return SimpleNamespace(models=[])

    monkeypatch.setattr(ai_module, "stt_status", fake_status)
    monkeypatch.setattr(ai_module, "_client", lambda: _C())
    info = await ai_module.debug_info()
    assert info["stt"]["ok"] is True and "whisper" not in info


@pytest.mark.asyncio
async def test_transcribe_audio_with_subtitles_returns_text_and_no_track(tmp_path, monkeypatch):
    async def fake_one(path):
        return "hello\nworld"

    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_one)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    assert await ai_module.transcribe_audio_with_subtitles(f) == ("hello\nworld", "")


@pytest.mark.asyncio
async def test_transcribe_audio_with_subtitles_propagates_errors(tmp_path, monkeypatch):
    async def fake_one(path):
        raise ai_module.SttError("studio unreachable", retryable=True)

    monkeypatch.setattr(ai_module, "_transcribe_one_file", fake_one)
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x")
    with pytest.raises(ai_module.SttError) as ei:
        await ai_module.transcribe_audio_with_subtitles(f)
    assert ei.value.retryable is True


def test_the_old_chunk_and_concurrency_env_names_still_work():
    code = ("import app.ai as a; print(a.STT_CHUNK_SECONDS, a.STT_JOB_CONCURRENCY)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**__import__("os").environ, "WHISPER_CHUNK_SECONDS": "300", "WHISPER_JOB_CONCURRENCY": "2",
                              "STT_CHUNK_SECONDS": "", "STT_JOB_CONCURRENCY": ""})
    assert out.stdout.split()[-2:] == ["300.0", "2"], out.stderr[-300:]
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**__import__("os").environ, "WHISPER_CHUNK_SECONDS": "300", "STT_CHUNK_SECONDS": "900"})
    assert out.stdout.split()[-2] == "900.0", "the new name wins over the old one"


# ── nothing of the whisper.cpp sidecar is left ───────────────────────────────

def test_the_sidecar_api_is_gone_from_the_ai_module():
    for name in ("WHISPER_URL", "effective_whisper_url", "set_whisper_override", "whisper_status", "WhisperError",
                 "whisper_job_semaphore", "WHISPER_TIMEOUT_SECONDS", "download_whisper_model", "WHISPER_KNOWN_MODELS",
                 "active_whisper_model", "load_whisper_model", "whisper_model_status", "get_stt_backend",
                 "set_stt_backend", "denoise_audio_file", "speech_enhancement_available",
                 "_transcribe_one_file_verbose", "WHISPER_CHUNK_SECONDS"):
        assert not hasattr(ai_module, name), name


def test_the_whisper_routes_are_gone(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    for method, path in (("get", "/api/ai/whisper/model-status"), ("get", "/api/ai/whisper/glossary"),
                         ("get", "/api/ai/whisper/language"), ("get", "/api/ai/whisper/denoise"),
                         ("post", "/api/ai/whisper/pull"), ("post", "/api/ai/whisper/activate")):
        assert getattr(client, method)(path).status_code in (404, 405), path


def test_the_pages_have_no_whisper_controls(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    html = client.get("/settings?tab=system").text
    assert 'name="whisper_url"' not in html and "studio-stt-backend" not in html
    assert 'id="studio-stt-model"' in html, "the Studio STT model stays"
    ai_page = client.get("/ai").text
    assert 'id="speech-panel"' in ai_page and 'id="tab-speech"' in ai_page
    assert "whisper-panel" not in ai_page and "wp-model-list" not in ai_page
    assert 'id="ri-textarea"' in ai_page, "recap instructions moved to the Speech tab with it"


def test_no_compose_service_or_dockerfile_for_whisper_remains():
    import yaml
    for f in ("docker-compose.yml", "truenas-compose.yml", "docker-compose.gpu.yml"):
        text = (ROOT / f).read_text()
        assert "whisper" not in text.lower(), f
        assert "whisper" not in yaml.safe_load(text)["services"], f
    assert not (ROOT / "docker" / "whisper").exists() and not (ROOT / "docker" / "whisper-cuda").exists()
    assert not (ROOT / "requirements-denoise.txt").exists()
    assert "INSTALL_DENOISE" not in (ROOT / "Dockerfile").read_text()
    # No denoise BUILD any more; the only mention allowed is the :latest-denoise alias of :latest
    # (a TrueNAS app pinned to that tag would otherwise freeze on its last whisper-era image).
    hits = [ln.strip() for ln in (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text().splitlines()
            if "denoise" in ln.lower() and not ln.strip().startswith("#")]
    assert len(hits) == 1 and hits[0].endswith(":latest-denoise"), hits
    env = (ROOT / ".env.example").read_text()
    assert "WHISPER_URL" not in env and "WHISPER_MODEL_FILE" not in env and "whisper.cpp" not in env


def test_new_databases_do_not_get_the_whisper_columns(client):
    from app.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        worlds = {r[1] for r in conn.execute(text("PRAGMA table_info(worlds)"))}
        settings = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)"))}
    assert not {"whisper_glossary", "whisper_language", "whisper_denoise"} & worlds
    assert "whisper_url" not in settings
