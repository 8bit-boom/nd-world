"""Shared machinery for GM download/export routes whose payload can be large
enough — embedded images, a walked uploads/maps directory — that building it
fully in memory before sending a single byte creates a silent, multi-minute
gap: looks like an infinite spinner in the browser, or gets the connection
killed by a reverse proxy's idle-byte timeout (the same class of bug the
Chronicler/AI Chat streaming fixes exist for). Originated as GET
/admin/backup.zip's own fix ("Full Backup" — confirmed live that time-to-
first-byte matched total request time exactly before this existed); pulled
out here so every other export with the same shape (World Book, World JSON,
per-kind bulk downloads) can use the identical, already-hardened mechanism
instead of a copy of it.

stream_download(build, ...) turns a `build(writer)` function into a ready
StreamingResponse: `build` runs in a background thread and writes real
output bytes/text into `writer` (a plain file-like object — hand it to
zipfile.ZipFile, json.dump, or just call writer.write(...) directly) as it
goes, while the response generator drains those bytes to the client
continuously. A stall-timeout safety valve on both ends keeps a vanished
client or a wedged disk from leaking the producer/consumer thread pair
forever."""
import json
import logging
import queue
import threading

from fastapi.responses import StreamingResponse

_log = logging.getLogger("nd.streaming_export")

# A stalled consumer (client vanished mid-download) or producer (a
# genuinely wedged disk) must not leave the build thread and the response
# generator below blocked on the queue forever.
STALL_TIMEOUT = 120


class Stalled(Exception):
    """Raised by QueueWriter.write() when nothing has drained the queue for
    STALL_TIMEOUT seconds — stream_download's build thread treats this
    exactly like any other build failure (log it, stop; the generator side
    notices the same stall independently and ends the response)."""


class QueueWriter:
    """A write-only file object that hands every chunk it's given straight
    to a queue instead of buffering it. Accepts str or bytes (str is
    UTF-8 encoded) so the same writer works as the target of
    zipfile.ZipFile(writer, ...), json.dump(obj, writer), or a plain
    writer.write(text) call. No seek() — zipfile (the caller that cares)
    detects that and falls back to writing per-entry data descriptors
    instead of pre-computed sizes in local headers, which every standard
    unzip tool (Python's own zipfile included) reads fine for a one-pass,
    streamed-out archive like this."""
    def __init__(self, q: "queue.Queue"):
        self._q = q
        self._pos = 0

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        else:
            data = bytes(data)
        try:
            self._q.put(data, timeout=STALL_TIMEOUT)
        except queue.Full:
            raise Stalled("no room in the download queue — consumer stalled")
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass


def stream_download(build, *, media_type: str, filename: str, maxsize: int = 64) -> StreamingResponse:
    """Runs build(writer) in a background thread, streaming whatever it
    writes to the client as it's produced instead of assembling the whole
    payload in memory first. `build` should only ever write through
    `writer` (directly, or via zipfile.ZipFile(writer, ...)/
    json.dump(obj, writer)) — its return value, if any, is ignored."""
    q: "queue.Queue" = queue.Queue(maxsize=maxsize)
    _DONE = object()

    def _run():
        try:
            build(QueueWriter(q))
        except Exception:
            _log.exception("stream_download: build failed mid-stream (filename=%r)", filename)
        finally:
            # Best-effort — if the queue is still full at this point the
            # consumer is gone anyway (see _gen's matching stall check
            # below), so there's no one left to see _DONE.
            try:
                q.put(_DONE, timeout=5)
            except queue.Full:
                pass

    threading.Thread(target=_run, daemon=True).start()

    def _gen():
        while True:
            try:
                chunk = q.get(timeout=STALL_TIMEOUT)
            except queue.Empty:
                _log.error("stream_download: no progress for %ss (filename=%r) — ending the response", STALL_TIMEOUT, filename)
                break
            if chunk is _DONE:
                break
            yield chunk

    return StreamingResponse(
        _gen(), media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def stream_json_array_download(items, item_to_dict, *, filename: str, wrap: tuple[str, str] | None = None) -> StreamingResponse:
    """Streams `[item_to_dict(x) for x in items]` as a JSON array without
    ever holding the whole array in memory at once — each item is only
    built (and any slow per-item work it does, like reading and
    base64-encoding an entity's image off disk) right before that item's
    own JSON text is written, so bytes start reaching the client
    immediately instead of only after every item has already been
    computed (exactly the risk a "World JSON" export with embedded images
    has for an illustrated world). `wrap`, if given, is (prefix, suffix)
    literal, already-valid JSON text placed before/after the array — e.g.
    to embed it as one field of a larger object rather than exporting a
    bare list."""
    prefix, suffix = wrap or ("", "")

    def build(writer):
        writer.write(prefix + "[")
        first = True
        for item in items:
            text = json.dumps(item_to_dict(item), ensure_ascii=False, indent=2)
            indented = "\n".join("    " + line for line in text.split("\n"))
            writer.write(("" if first else ",") + "\n" + indented)
            first = False
        writer.write(("" if first else "\n") + "]" + suffix)

    return stream_download(build, media_type="application/json", filename=filename)
