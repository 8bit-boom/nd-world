"""Tests for app/streaming_export.py — the shared queue/thread machinery
every GM download/export route with a real large-payload risk (embedded
images, a walked uploads/maps directory) uses to stream real bytes to the
client as its payload is built, instead of assembling the whole thing in
memory first (originated as GET /admin/backup.zip's own fix — see
test_admin_backup.py for that route's own content tests, and
streaming_export.py's own module docstring for the "why").
"""
import io
import json
import queue
import time
import zipfile

import pytest

from app.streaming_export import (
    STALL_TIMEOUT, Stalled, QueueWriter, stream_download, stream_json_array_download,
)


async def _collect(resp) -> bytes:
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c)
    return b"".join(chunks)


# ── QueueWriter ──────────────────────────────────────────────────────────

def test_queue_writer_accepts_bytes_and_str():
    q: "queue.Queue" = queue.Queue()
    w = QueueWriter(q)
    w.write(b"bytes-chunk")
    w.write("str-chunk")
    assert q.get_nowait() == b"bytes-chunk"
    assert q.get_nowait() == b"str-chunk"  # str encoded as UTF-8


def test_queue_writer_tell_tracks_total_bytes_written():
    q: "queue.Queue" = queue.Queue()
    w = QueueWriter(q)
    w.write(b"1234")
    w.write("abc")  # 3 bytes
    assert w.tell() == 7


def test_queue_writer_raises_stalled_when_consumer_never_drains(monkeypatch):
    """A producer thread must not block forever if nothing is reading from
    the queue (e.g. the client vanished mid-download) — bounded by
    STALL_TIMEOUT instead of hanging indefinitely."""
    monkeypatch.setattr("app.streaming_export.STALL_TIMEOUT", 0.05)
    q: "queue.Queue" = queue.Queue(maxsize=1)
    w = QueueWriter(q)
    w.write(b"first chunk fills the queue")  # succeeds — queue (maxsize=1) now full
    start = time.monotonic()
    with pytest.raises(Stalled):
        w.write(b"second chunk has nowhere to go")
    assert time.monotonic() - start < 2  # bounded by the (monkeypatched) stall timeout


def test_stall_timeout_is_a_sane_positive_number():
    # Guards against an accidental 0/negative value that would make every
    # real download falsely "stall" immediately.
    assert STALL_TIMEOUT > 10


def test_queue_writer_emits_many_chunks_for_a_large_file(tmp_path):
    """The whole point of the fix: writing a real file through zipfile into
    a QueueWriter must hand off the archive incrementally, not accumulate
    it and hand it off all at once."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (64 * 1024))  # bigger than ZipFile.write()'s 8KB copy buffer
    q: "queue.Queue" = queue.Queue()
    with zipfile.ZipFile(QueueWriter(q), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(big, "big.bin")
    chunks = []
    while not q.empty():
        chunks.append(q.get_nowait())
    assert len(chunks) > 1
    zf2 = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert zf2.testzip() is None
    assert zf2.read("big.bin") == b"x" * (64 * 1024)


# ── stream_download ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_download_delivers_everything_the_build_function_writes():
    def build(writer):
        writer.write(b"hello ")
        writer.write("world")

    resp = stream_download(build, media_type="text/plain", filename="test.txt")
    assert resp.media_type == "text/plain"
    assert resp.headers["content-disposition"] == 'attachment; filename="test.txt"'
    assert await _collect(resp) == b"hello world"


@pytest.mark.asyncio
async def test_stream_download_logs_and_ends_cleanly_when_build_raises(caplog):
    def build(writer):
        writer.write(b"partial")
        raise RuntimeError("boom")

    resp = stream_download(build, media_type="text/plain", filename="test.txt")
    # Whatever was written before the failure still reaches the client —
    # the response ends rather than hanging, even though build() blew up.
    assert await _collect(resp) == b"partial"


# ── stream_json_array_download ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_json_array_download_bare_list_round_trips():
    items = [{"a": 1}, {"a": 2}]
    resp = stream_json_array_download(items, lambda x: x, filename="test.json")
    data = await _collect(resp)
    assert json.loads(data) == items


@pytest.mark.asyncio
async def test_stream_json_array_download_wrapped_round_trips():
    items = [{"a": 1}, {"a": 2}]
    prefix = '{"world": {"name": "Test"}, "entities": '
    resp = stream_json_array_download(items, lambda x: x, filename="test.json", wrap=(prefix, "}"))
    data = await _collect(resp)
    assert json.loads(data) == {"world": {"name": "Test"}, "entities": items}


@pytest.mark.asyncio
async def test_stream_json_array_download_empty_list_round_trips():
    resp = stream_json_array_download([], lambda x: x, filename="empty.json")
    data = await _collect(resp)
    assert json.loads(data) == []


@pytest.mark.asyncio
async def test_stream_json_array_download_only_calls_item_to_dict_once_per_item():
    """Each item's dict is built right before it's written — not all
    upfront — this test just confirms item_to_dict is called exactly once
    per item (the actual "streamed as it goes" property is covered by
    test_queue_writer_emits_many_chunks_for_a_large_file at the writer
    level, since a small test payload here wouldn't itself span multiple
    queue chunks)."""
    calls = []

    def item_to_dict(x):
        calls.append(x)
        return {"value": x}

    resp = stream_json_array_download([1, 2, 3], item_to_dict, filename="test.json")
    data = await _collect(resp)
    assert calls == [1, 2, 3]
    assert json.loads(data) == [{"value": 1}, {"value": 2}, {"value": 3}]
