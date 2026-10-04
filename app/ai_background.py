"""AI work runs in the background, not inside the browser's request.

A request that waits on the model is tied to its connection: Cloudflare cuts it at ~100 s, a phone that backgrounds
the tab drops it, a reload loses the answer — and a model with thinking on, or a cold model load, easily takes
minutes. So every AI task route can be started in the background by sending `X-ND-Background: 1`:

  * the server answers 202 at once with a task id (also in `X-ND-Task`);
  * the very same route then runs to completion on its own, through the full app (auth, the caller's own DB session,
    the world cookie) — the client leaving does NOT cancel it;
  * the result is polled from GET /api/ai/tasks/{id} (app/routers/ai_tasks.py) and is exactly what the route would
    have returned inline: status code, content type, body. Errors are the route's own errors, not a generic failure.

static/js/nd-ai-task.js (`ndAiFetch`) is the drop-in `fetch` that does the polling and hands the page a real
`Response`, so a call site only swaps the function name. Without the header nothing changes, so API users, MCP and the
tests keep the plain request/response contract.

Why a middleware rather than per-route job code: the routes already hold the right logic (permissions, prompts,
side effects such as saving the clip or linking the audio to the message). Re-running the request keeps all of it, with
no second copy to drift. The cost is that results live in memory — a server restart drops finished tasks (the browser
helper says so instead of hanging); anything a route saves itself (clips, conversation turns, drafts) is unaffected.

Also here: streams that must outlive the browser (the NPC conversation saves its turn when the reply finishes) are
"detached" — the app is run to completion even if the reader disconnects mid-stream.

Plain ASGI on purpose (no BaseHTTPMiddleware): it must see the raw body once and replay it.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from typing import Optional

from starlette.responses import JSONResponse

from . import ai_queue as _ai_queue

_log = logging.getLogger("nd.ai_background")

BG_HEADER = b"x-nd-background"
LABEL_HEADER = b"x-nd-task-label"
MAX_BODY_BYTES = 64 * 1024 * 1024         # request bodies are buffered once so they can be replayed
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_RUNNING_PER_USER = 8
MAX_RUNNING = 48
RETAIN_SECONDS = 3600                      # finished results stay readable this long
MAX_RETAINED = 300

# One entry per route (no alternations) so a test can prove each is served by a real POST route.
TASK_PATH_PATTERNS = [
    r"/api/ai/chat", r"/api/ai/chat/compact",
    r"/api/ai/generate/entity", r"/api/ai/generate/npc", r"/api/ai/generate/location", r"/api/ai/generate/quest",
    r"/api/ai/generate/entity-smart",
    r"/api/ai/assist", r"/api/ai/tts",
    r"/api/ai/entity-from-text", r"/api/ai/entity-from-images", r"/api/ai/entities-from-text-batch",
    r"/api/ai/character-from-images",
    r"/api/bulk-edit/parse", r"/api/facts/parse", r"/api/facts/folk-tale",
    r"/boards/generate-mystery", r"/calendar/ai-day", r"/calendar/ai-design", r"/api/media-rename/suggest",
    r"/combat/\d+/ai-tactics", r"/parties/\d+/ai-insights", r"/tables/generate-ai",
    r"/api/sessions/ai/expand-notes", r"/api/sessions/ai/condense-recap", r"/api/sessions/ai/summarize-transcript",
    r"/api/sessions/\d+/ai/summarize-live-transcript", r"/api/sessions/\d+/ai/summarize-from-facts",
    r"/api/sessions/\d+/prep/generate", r"/api/sessions/\d+/recap-song",
    r"/api/npc-talk/\d+/speak", r"/audio/\d+/transcribe", r"/video/\d+/transcribe",
    r"/api/kiy/build-model",
]
TASK_PATHS = re.compile("^(?:" + "|".join(TASK_PATH_PATTERNS) + ")$")

# Streams that must run to the end even if the reader leaves. Only where the server itself saves the outcome AND
# there is no Stop button whose disconnect is meant to cancel the model (the AI Chat / Ask AI streams have one).
DETACH_PATHS = re.compile(r"^/api/npc-talk/\d+/stream$")

_LABELS = [
    (r"/api/ai/tts$", "Text to speech"), (r"/speak$", "NPC voice"), (r"/recap-song$", "Recap song"),
    (r"/transcribe$", "Transcription"), (r"/prep/generate$", "Session prep"),
    (r"/ai/summarize|/ai/expand|/ai/condense", "Session summary"), (r"/ai-tactics$", "Combat tactics"),
    (r"/ai-insights$", "Party insights"), (r"/generate-mystery$", "Mystery board"), (r"/ai-day$", "Calendar day"),
    (r"/calendar/ai-design$", "Calendar design"),
    (r"/api/media-rename/suggest$", "Media names"),
    (r"/tables/generate-ai$", "Random table"), (r"/api/ai/generate/|/entity-from|/entities-from", "Entity draft"),
    (r"/api/ai/chat", "AI chat"), (r"/api/ai/assist$", "AI suggestions"), (r"/api/facts/", "Facts"),
    (r"/api/bulk-edit/parse$", "Bulk edit"), (r"/character-from-images$", "Character import"),
]


def label_for(path: str) -> str:
    for pat, text in _LABELS:
        if re.search(pat, path):
            return text
    return path


class Task:
    __slots__ = ("id", "user_id", "path", "label", "status", "started", "finished", "http_status",
                 "content_type", "location", "body", "task", "ticket")

    def __init__(self, user_id, path, label):
        self.id = secrets.token_urlsafe(12)
        self.user_id = user_id
        self.path = path
        self.label = label
        self.status = "running"            # queued | running | done | cancelled
        self.started = time.time()
        self.finished: Optional[float] = None
        self.http_status = 0
        self.content_type = ""
        self.location = ""                 # a redirect's target (the form-post AI routes answer 303)
        self.body = b""
        self.task: Optional[asyncio.Task] = None
        self.ticket = None                 # its place in the one-at-a-time AI queue (app/ai_queue.py)

    def view(self, with_body=False) -> dict:
        end = self.finished or time.time()
        d = {"id": self.id, "label": self.label, "status": self.status, "elapsed": int(end - self.started),
             "started": self.started}
        if self.status == "queued" and self.ticket is not None:
            d["position"] = max(1, _ai_queue.queue.position(self.ticket))    # 1 = next to run
        if self.status == "done":
            d["http_status"] = self.http_status
            d["content_type"] = self.content_type
            if self.location:
                d["location"] = self.location
            if with_body:
                d["body"] = _decode_body(self.body, self.content_type)
        return d


def _decode_body(body: bytes, content_type: str):
    text = body.decode("utf-8", errors="replace")
    if "json" in (content_type or "").lower():
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


_TASKS: dict = {}
_PROCS: set = set()          # strong references to detached-stream tasks


def _sweep() -> None:
    now = time.time()
    for tid in [t.id for t in _TASKS.values() if t.finished and now - t.finished > RETAIN_SECONDS]:
        _TASKS.pop(tid, None)
    if len(_TASKS) > MAX_RETAINED:
        done = sorted((t for t in _TASKS.values() if t.finished), key=lambda t: t.finished)
        for t in done[: len(_TASKS) - MAX_RETAINED]:
            _TASKS.pop(t.id, None)


def get_task(task_id: str, user) -> Optional[Task]:
    """The task if it exists and belongs to `user` (or user is the GM); None otherwise — a stranger's id is just 404."""
    _sweep()
    t = _TASKS.get(task_id)
    if t is None:
        return None
    if user is None or (t.user_id != user.id and not getattr(user, "is_gm", False)):
        return None
    return t


def tasks_for(user_id: int) -> list:
    _sweep()
    return sorted((t for t in _TASKS.values() if t.user_id == user_id), key=lambda t: -t.started)


def cancel(task: Task) -> bool:
    if task.status not in ("queued", "running") or task.task is None:
        return False
    task.task.cancel()
    return True


async def shutdown() -> None:
    """Server stopping: AI tasks are in-memory and not resumable — cancel them rather than leave them half-run."""
    running = [t.task for t in _TASKS.values() if t.status in ("queued", "running") and t.task is not None]
    for t in running:
        t.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)


def _running_count(user_id=None) -> int:
    return sum(1 for t in _TASKS.values() if t.status in ("queued", "running") and (user_id is None or t.user_id == user_id))


# ── plumbing shared by both modes ───────────────────────────────────────────────────────────────

async def _read_body(receive):
    """The whole request body, or None if the client went away first. Raises ValueError when over the cap."""
    chunks, total = [], 0
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return None
        part = msg.get("body", b"")
        total += len(part)
        if total > MAX_BODY_BYTES:
            raise ValueError("too large")
        chunks.append(part)
        if not msg.get("more_body"):
            return b"".join(chunks)


def _inner_scope(scope, body: bytes):
    inner = dict(scope)
    inner["headers"] = [
        (k, v) for k, v in scope["headers"]
        if k not in (BG_HEADER, LABEL_HEADER, b"content-length", b"transfer-encoding")
    ] + [(b"content-length", str(len(body)).encode())]
    inner["state"] = dict(scope.get("state") or {})
    if isinstance(scope.get("session"), dict):
        inner["session"] = dict(scope["session"])
    return inner


def _replay(body: bytes):
    """A receive() that delivers the buffered body once, then NEVER reports a disconnect — the work is meant to outlive
    the browser, and a StreamingResponse aborts the moment it is told the client left."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    return receive


class AiBackgroundMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if TASK_PATHS.match(path) and any(k == BG_HEADER and v.strip() == b"1" for k, v in scope["headers"]):
            user_id = (scope.get("session") or {}).get("user_id")
            if user_id:                                  # logged out: let the normal 401 / redirect happen inline
                return await self._start(scope, receive, send, user_id)
        elif DETACH_PATHS.match(path):
            return await self._detached(scope, receive, send)
        return await self.app(scope, receive, send)

    # ── 202 + run to completion on our own ─────────────────────────────────────────────────────
    async def _start(self, scope, receive, send, user_id):
        _sweep()
        if _running_count(user_id) >= MAX_RUNNING_PER_USER or _running_count() >= MAX_RUNNING:
            return await JSONResponse(
                {"detail": "Too many AI tasks are already running — wait for one to finish (or cancel it)."},
                status_code=429)(scope, receive, send)
        try:
            body = await _read_body(receive)
        except ValueError:
            return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
        if body is None:
            return
        label = next((v.decode("latin-1")[:60] for k, v in scope["headers"] if k == LABEL_HEADER), "") or label_for(scope["path"])
        task = Task(user_id, scope["path"], label)
        _TASKS[task.id] = task
        # Join the one-at-a-time AI queue NOW, so the position is known the moment the task is accepted. The ticket is
        # freed by the done-callback too: a task cancelled before its first step never reaches a `finally`.
        task.ticket = _ai_queue.queue.enter(label, "task")
        task.status = "queued" if _ai_queue.queue.position(task.ticket) > 0 else "running"
        task.task = asyncio.create_task(self._run(task, scope, body))
        task.task.add_done_callback(lambda _t, tk=task.ticket: _ai_queue.queue.leave(tk))
        await JSONResponse({"task_id": task.id, "status": "running", "label": label}, status_code=202,
                           headers={"X-ND-Task": task.id})(scope, receive, send)

    async def _run(self, task: Task, scope, body: bytes):
        status, headers, buf = [0], [], bytearray()

        async def collect(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
                headers[:] = msg.get("headers", [])
            elif msg["type"] == "http.response.body" and len(buf) < MAX_RESULT_BYTES:
                buf.extend(msg.get("body", b""))

        try:
            await _ai_queue.queue.wait_turn(task.ticket)       # one AI task at a time, first come first served
            task.status = "running"
            await self.app(_inner_scope(scope, body), _replay(body), collect)
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.finished = time.time()
            raise
        except Exception as exc:                         # an unhandled error in the route
            _log.exception("background AI task %s (%s) crashed", task.id, task.path)
            status[0], buf = 500, bytearray(json.dumps({"detail": f"Internal error: {type(exc).__name__}"}).encode())
            headers[:] = [(b"content-type", b"application/json")]
        task.http_status = status[0] or 500
        task.content_type = next((v.decode("latin-1") for k, v in headers if k.lower() == b"content-type"), "")
        task.location = next((v.decode("latin-1") for k, v in headers if k.lower() == b"location"), "")
        task.body = bytes(buf)
        task.status = "done"
        task.finished = time.time()

    # ── a stream that keeps going after the reader leaves ──────────────────────────────────────
    async def _detached(self, scope, receive, send):
        try:
            body = await _read_body(receive)
        except ValueError:
            return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
        if body is None:
            return
        q: asyncio.Queue = asyncio.Queue()
        failure = []

        async def to_queue(msg):
            q.put_nowait(msg)

        async def run():
            try:
                await self.app(_inner_scope(scope, body), _replay(body), to_queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure.append(exc)
                _log.exception("detached stream %s failed", scope["path"])
            finally:
                q.put_nowait(None)

        proc = asyncio.create_task(run())
        _PROCS.add(proc)
        proc.add_done_callback(_PROCS.discard)
        forwarding, started = True, False
        while True:
            msg = await q.get()
            if msg is None:
                break
            if forwarding:
                try:
                    await send(msg)
                    started = started or msg["type"] == "http.response.start"
                except Exception:                        # the reader is gone: keep draining so the app can finish
                    forwarding = False
        if failure and not started:
            raise failure[0]
