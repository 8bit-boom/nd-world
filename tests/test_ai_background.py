"""AI work runs in the background, not inside the browser's request (app/ai_background.py).

A request that waits on the model is tied to the open connection: Cloudflare cuts it at ~100 s, a phone that
backgrounds the tab drops it, and a reload loses the answer. So every AI task route can be started with the
`X-ND-Background: 1` header: the server answers 202 at once with a task id, runs the very same route to completion
on its own (the client leaving does not cancel it), and the result is polled from /api/ai/tasks/{id}. Without the
header nothing changes (API users, MCP, tests keep the plain request/response contract).

Covered here: the middleware end to end through the real app, the generic "keep going when the client leaves"
detached stream, and the guard that every browser caller of an AI task route goes through the shared helper."""
import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import ai as ai_module
from app import ai_background

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
BG = {"X-ND-Background": "1"}


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _wait_done(client, task_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/ai/tasks/{task_id}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("task never finished")


@pytest.fixture(autouse=True)
def _fresh_task_registry():
    """User ids restart at 1 with every test database, so tasks left over from an earlier test would look like ours."""
    ai_background._TASKS.clear()
    yield
    ai_background._TASKS.clear()


@pytest.fixture()
def slow_model(monkeypatch):
    """generate_chat that takes a moment and can be made to hang, so 'running' is observable."""
    state = {"calls": 0, "release": None, "cancelled": False, "finished": False}

    async def fake(messages, system="", model="", options=None, think=False, format=None):
        state["calls"] += 1
        try:
            if state["release"] is not None:
                await state["release"].wait()
            else:
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        state["finished"] = True
        return "the model said hi"

    monkeypatch.setattr(ai_module, "generate_chat", fake)
    return state


# ── the plain contract is untouched ─────────────────────────────────────────────────────────────

def test_without_the_header_the_route_still_answers_inline(client, seed, slow_model):
    _gm(client, seed)
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json() == {"result": "the model said hi"}
    assert "x-nd-task" not in r.headers


def test_a_route_that_is_not_an_ai_task_ignores_the_header(client, seed):
    _gm(client, seed)
    r = client.get("/api/ai/presets", headers=BG)
    assert r.status_code == 200 and "x-nd-task" not in r.headers


# ── background mode ─────────────────────────────────────────────────────────────────────────────

def test_header_returns_202_at_once_and_the_poll_returns_the_routes_own_answer(client, seed, slow_model):
    _gm(client, seed)
    t0 = time.time()
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG)
    assert r.status_code == 202
    assert time.time() - t0 < 0.14, "the 202 must not wait for the model"
    body = r.json()
    assert body["status"] == "running" and body["task_id"] == r.headers["x-nd-task"]
    first = client.get(f"/api/ai/tasks/{body['task_id']}").json()
    assert first["status"] == "running" and "elapsed" in first
    done = _wait_done(client, body["task_id"])
    assert done["status"] == "done"
    assert done["http_status"] == 200 and done["content_type"].startswith("application/json")
    assert done["body"] == {"result": "the model said hi"}


def test_an_error_status_and_its_detail_come_through_unchanged(client, seed):
    _gm(client, seed)
    # the route rejects this body itself (422) — the poll must show exactly that, not a generic failure
    r = client.post("/api/ai/chat", json={"nonsense": True}, headers=BG)
    assert r.status_code == 202
    done = _wait_done(client, r.json()["task_id"])
    assert done["status"] == "done" and done["http_status"] == 422
    assert "detail" in done["body"]


def test_a_forbidden_caller_gets_the_routes_403_through_the_task(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/generate/entity", json={"prompt": "x"}, headers=BG)
    assert r.status_code == 202
    done = _wait_done(client, r.json()["task_id"])
    assert done["http_status"] == 403


def test_the_work_finishes_even_if_nobody_ever_polls(client, seed, slow_model):
    _gm(client, seed)
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG)
    assert r.status_code == 202
    deadline = time.time() + 5
    while time.time() < deadline and not slow_model["finished"]:
        time.sleep(0.05)
    assert slow_model["finished"], "the model call was abandoned along with the connection"


def test_cancel_stops_the_running_work(client, seed, slow_model):
    slow_model["release"] = asyncio.Event()           # never released: it hangs until cancelled
    _gm(client, seed)
    tid = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG).json()["task_id"]
    time.sleep(0.2)
    assert client.get(f"/api/ai/tasks/{tid}").json()["status"] == "running"
    assert client.delete(f"/api/ai/tasks/{tid}").status_code == 200
    d = _wait_done(client, tid)
    assert d["status"] == "cancelled"
    deadline = time.time() + 3
    while time.time() < deadline and not slow_model["cancelled"]:
        time.sleep(0.05)
    assert slow_model["cancelled"], "the model call kept running after cancel"


# ── who may see a task ──────────────────────────────────────────────────────────────────────────

def test_a_task_belongs_to_its_starter(client, seed, slow_model):
    _gm(client, seed)
    tid = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG).json()["task_id"]
    _wait_done(client, tid)
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/api/ai/tasks/{tid}").status_code == 404
    assert client.delete(f"/api/ai/tasks/{tid}").status_code == 404


def test_unknown_task_is_404(client, seed):
    _gm(client, seed)
    assert client.get("/api/ai/tasks/does-not-exist").status_code == 404


def test_anonymous_requests_are_not_backgrounded(client, seed):
    r = client.post("/api/ai/chat", json={"messages": []}, headers=BG, follow_redirects=False)
    assert r.status_code in (401, 303) and "x-nd-task" not in r.headers


def test_task_list_shows_only_the_callers_tasks(client, seed, slow_model):
    _gm(client, seed)
    tid = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG).json()["task_id"]
    _wait_done(client, tid)
    mine = client.get("/api/ai/tasks").json()["tasks"]
    assert [t["id"] for t in mine] == [tid]
    assert mine[0]["label"] and mine[0]["status"] == "done"
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/tasks").json()["tasks"] == []


def test_a_player_can_poll_their_own_task(client, seed, monkeypatch):
    # players start tasks on player-reachable routes (NPC voice, Ask AI); the poll route must be player-safe too
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/generate/entity", json={"prompt": "x"}, headers=BG)
    tid = r.json()["task_id"]
    _wait_done(client, tid)
    assert client.get("/api/ai/tasks").status_code == 200


# ── limits ──────────────────────────────────────────────────────────────────────────────────────

def test_a_user_cannot_pile_up_unbounded_tasks(client, seed, slow_model, monkeypatch):
    monkeypatch.setattr(ai_background, "MAX_RUNNING_PER_USER", 2)
    slow_model["release"] = asyncio.Event()
    _gm(client, seed)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    ids = [client.post("/api/ai/chat", json=body, headers=BG) for _ in range(2)]
    assert [r.status_code for r in ids] == [202, 202]
    third = client.post("/api/ai/chat", json=body, headers=BG)
    assert third.status_code == 429
    for r in ids:
        client.delete(f"/api/ai/tasks/{r.json()['task_id']}")


def test_finished_tasks_are_forgotten_after_a_while(client, seed, slow_model, monkeypatch):
    _gm(client, seed)
    tid = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=BG).json()["task_id"]
    _wait_done(client, tid)
    monkeypatch.setattr(ai_background, "RETAIN_SECONDS", -1)
    assert client.get(f"/api/ai/tasks/{tid}").status_code == 404


# ── which routes are AI tasks ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/ai/chat", "/api/ai/chat/compact", "/api/ai/generate/entity", "/api/ai/generate/entity-smart",
    "/api/ai/assist", "/api/ai/tts", "/api/ai/entity-from-text", "/api/ai/entities-from-text-batch",
    "/api/bulk-edit/parse", "/api/facts/parse", "/api/facts/folk-tale", "/boards/generate-mystery",
    "/calendar/ai-day", "/combat/3/ai-tactics", "/parties/2/ai-insights", "/tables/generate-ai",
    "/api/sessions/ai/expand-notes", "/api/sessions/ai/summarize-transcript",
    "/api/sessions/7/ai/summarize-live-transcript", "/api/sessions/7/prep/generate", "/api/sessions/7/recap-song",
    "/api/npc-talk/5/speak", "/audio/4/transcribe", "/video/4/transcribe", "/api/kiy/build-model",
])
def test_these_routes_are_ai_tasks(path):
    assert ai_background.TASK_PATHS.match(path), path


@pytest.mark.parametrize("path", ["/api/ai/presets", "/login", "/api/ai/tasks", "/api/ai/stream", "/entity/3/edit"])
def test_these_are_not(path):
    assert not ai_background.TASK_PATHS.match(path), path


def test_every_task_pattern_is_served_by_a_real_post_route():
    """A pattern that no route serves is a typo that would silently never background anything."""
    from app.main import _fastapi_app
    served = {re.sub(r"\{[^}]+\}", "7", r.path) for r in _fastapi_app.routes if "POST" in (getattr(r, "methods", None) or ())}
    missing = [pat for pat in ai_background.TASK_PATH_PATTERNS if not any(re.fullmatch(pat, path) for path in served)]
    assert not missing, f"no POST route serves: {missing}"


# ── streams that must outlive the browser (detached) ────────────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_detached_stream_runs_to_completion_when_the_client_leaves():
    seen = {"finished": False, "saw_disconnect": False}

    async def inner(scope, receive, send):
        await receive()                                           # the request body
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        # a StreamingResponse watches receive() for a disconnect and aborts on it — the middleware must never deliver one
        waiter = asyncio.ensure_future(receive())
        await asyncio.sleep(0.2)
        if waiter.done():
            seen["saw_disconnect"] = waiter.result().get("type") == "http.disconnect"
        waiter.cancel()
        await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": False})
        seen["finished"] = True

    mw = ai_background.AiBackgroundMiddleware(inner)
    got = []

    async def client_send(msg):
        got.append(msg)
        if len(got) == 2:                                          # the client reads one chunk, then goes away
            raise OSError("client disconnected")

    async def client_receive():
        if not getattr(client_receive, "sent", False):
            client_receive.sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": "/api/npc-talk/5/stream", "headers": [], "query_string": b"",
             "session": {"user_id": 1}}

    async def go():
        await mw(scope, client_receive, client_send)
        await asyncio.sleep(0.4)

    _run(go())
    assert seen["finished"], "the generation was cut off when the client disconnected"
    assert not seen["saw_disconnect"], "the app was told the client had gone"


def test_only_the_npc_stream_is_detached():
    assert ai_background.DETACH_PATHS.match("/api/npc-talk/12/stream")
    assert not ai_background.DETACH_PATHS.match("/api/ai/stream")           # has a Stop button: disconnect must cancel
    assert not ai_background.DETACH_PATHS.match("/api/npc-talk/12/speak")


# ── the browser side: every caller goes through ndAiFetch ───────────────────────────────────────

def _js_sources():
    files = list((ROOT / "app" / "templates").rglob("*.html")) + list((ROOT / "static" / "js").glob("*.js"))
    return [f for f in files if f.name != "nd-ai-task.js"]


_FETCH_RE = re.compile(r"""(?<![\w.])(?P<fn>fetch|ndAiFetch)\(\s*(?P<q>['"`])(?P<url>[^'"`]*)(?P=q)""")
# `'/combat/' + id + '/ai-tactics'` style concatenations are normalised to digits before matching
_CONCAT_RE = re.compile(r"""(['"`])\s*\+\s*[^'"`+]+?\s*\+\s*(['"`])""")


def _normalise(url):
    url = re.sub(r"\$\{[^}]*\}", "7", url)
    return url.split("?")[0]


def test_every_browser_caller_of_an_ai_task_route_uses_the_background_helper():
    offenders = []
    for f in _js_sources():
        text = f.read_text()
        text = re.sub(r"""(['"`])\s*\+\s*[A-Za-z_][\w.\[\]()]*\s*\+\s*(['"`])""", r"7", text)   # '/x/' + id + '/y' -> '/x/7/y'
        text = re.sub(r"""\$\{[^}]*\}""", "7", text)
        for m in _FETCH_RE.finditer(text):
            url = m.group("url")
            if m.group("fn") == "fetch" and ai_background.TASK_PATHS.match(_normalise(url).replace("7'7", "7")):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{f.relative_to(ROOT)}:{line}  fetch({url!r})")
    assert not offenders, "AI task routes called with plain fetch() (use ndAiFetch):\n" + "\n".join(offenders)


# Call sites whose URL is built at run time (`window.location.pathname + '/ai-tactics'`) or passed into a helper:
# the literal scan above cannot see them, so look for the telltale tail on any plain fetch( line.
_DYNAMIC_TAILS = ("/ai-tactics", "/ai-insights", "/ai-day", "/recap-song", "/speak", "/transcribe", "/expand-notes",
                  "/generate-mystery", "/generate-ai", "/prep/generate", "/summarize-transcript", "/folk-tale")


def test_no_plain_fetch_builds_an_ai_task_url_at_run_time():
    offenders = []
    for f in _js_sources():
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for m in re.finditer(r"(?<![\w.])fetch\(", line):
                if any(tail in line[m.end():] for tail in _DYNAMIC_TAILS):
                    offenders.append(f"{f.relative_to(ROOT)}:{n}  {line.strip()[:110]}")
    assert not offenders, "plain fetch() of an AI task route:\n" + "\n".join(offenders)


def test_helper_functions_that_take_a_url_use_the_background_fetch():
    detail = (ROOT / "app" / "templates" / "sessions" / "detail.html").read_text()
    for fn in ("async function aiRunRecap(url, body)", "async function raCall(url, opts)"):
        body = detail[detail.index(fn): detail.index(fn) + 700]
        assert "ndAiFetch(" in body and "await fetch(" not in body, fn
    imp = (ROOT / "app" / "templates" / "import.html").read_text()
    assert "ndAiFetch(endpoint" in imp and "fetch(endpoint" not in imp.replace("ndAiFetch(endpoint", "")


def test_forms_that_post_to_an_ai_task_route_are_run_in_the_background_too():
    """A plain <form> post freezes the page for as long as the model takes (the 'AI Table' / 'AI Investigation
    Board' buttons). `data-nd-ai-form` hands the submit to nd-ai-task.js, which backgrounds it and follows the redirect."""
    offenders = []
    for f in (ROOT / "app" / "templates").rglob("*.html"):
        for m in re.finditer(r"<form\b[^>]*>", f.read_text(), re.S):
            tag = m.group(0)
            act = re.search(r"""action\s*=\s*["']([^"']*)["']""", tag)
            if not act:
                continue
            path = re.search(r"""(/[A-Za-z0-9_\-/]+)""", act.group(1))
            if path and ai_background.TASK_PATHS.match(path.group(1)) and "data-nd-ai-form" not in tag:
                offenders.append(f"{f.relative_to(ROOT)}: {tag[:100]}")
    assert not offenders, "\n".join(offenders)


def test_the_helper_is_loaded_on_every_page_before_page_scripts():
    base = (ROOT / "app" / "templates" / "base.html").read_text()
    assert re.search(r'<script src="/static/js/nd-ai-task\.js\?v=\{\{ asset_v\(\'js/nd-ai-task\.js\'\) \}\}"></script>', base)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

_HARNESS = r"""
global.window = global;
global.document = {hidden: false, addEventListener() {}};
const store = {};
global.localStorage = {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; }};
const script = JSON.parse(process.env.SCENARIO);
const calls = [];
let pollN = 0;
global.setTimeout = (fn, ms) => { return global.__realSetTimeout(fn, 0); };
global.__realSetTimeout = require('timers').setTimeout;
global.fetch = async (url, init) => {
  calls.push({url, method: (init && init.method) || 'GET', headers: Object.fromEntries(new Headers((init && init.headers) || {}))});
  if (url.startsWith('/api/ai/tasks/')) {
    if ((init && init.method) === 'DELETE') return new Response('{}', {status: 200});
    const step = script.polls[Math.min(pollN++, script.polls.length - 1)];
    if (step === 'throw') throw new TypeError('network');
    return new Response(JSON.stringify(step.json), {status: step.status || 200, headers: {'Content-Type': 'application/json'}});
  }
  const s = script.start;
  return new Response(s.body, {status: s.status, headers: Object.assign({'Content-Type': 'application/json'}, s.headers || {})});
};
require(process.env.HELPER);
(async () => {
  const out = {};
  try {
    const progress = [];
    const ctl = new AbortController();
    const init = {method: 'POST', body: '{}', headers: {'Content-Type': 'application/json'}};
    if (script.abortAfterPolls) init.signal = ctl.signal;
    const p = window.ndAiFetch('/api/x', init, {label: 'Test', onProgress: e => { progress.push(e.status); if (script.abortAfterPolls && progress.length >= script.abortAfterPolls) ctl.abort(); }});
    const res = await p;
    out.ok = res.ok; out.status = res.status; out.body = await res.text();
    out.progress = progress;
  } catch (e) { out.error = e.name + ':' + e.message; }
  out.calls = calls; out.pending = store['nd_ai_tasks'] || null;
  console.log(JSON.stringify(out));
})();
"""


def _node(scenario):
    import os
    env = dict(os.environ, SCENARIO=json.dumps(scenario), HELPER=str(ROOT / "static" / "js" / "nd-ai-task.js"))
    res = subprocess.run(["node", "-e", _HARNESS], capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@needs_node
def test_helper_polls_and_hands_back_a_real_response():
    out = _node({
        "start": {"status": 202, "body": '{"task_id":"abc","status":"running"}', "headers": {"X-ND-Task": "abc"}},
        "polls": [{"json": {"status": "running", "elapsed": 1}}, {"json": {"status": "running", "elapsed": 2}},
                  {"json": {"status": "done", "http_status": 200, "content_type": "application/json", "body": {"result": "hi"}}}],
    })
    assert out["ok"] is True and out["status"] == 200 and json.loads(out["body"]) == {"result": "hi"}
    assert out["calls"][0]["headers"]["x-nd-background"] == "1"
    assert out["progress"][:2] == ["running", "running"]
    assert out["pending"] in (None, "[]"), "a finished task must not stay in the pending list"


@needs_node
def test_helper_passes_the_routes_error_status_through():
    out = _node({
        "start": {"status": 202, "body": '{"task_id":"abc"}', "headers": {"X-ND-Task": "abc"}},
        "polls": [{"json": {"status": "done", "http_status": 400, "content_type": "application/json", "body": {"detail": "No notes provided"}}}],
    })
    assert out["ok"] is False and out["status"] == 400 and json.loads(out["body"]) == {"detail": "No notes provided"}


@needs_node
def test_helper_falls_back_to_a_plain_response_when_the_server_did_not_background_it():
    out = _node({"start": {"status": 200, "body": '{"result":"inline"}'}, "polls": [{"json": {}}]})
    assert out["status"] == 200 and json.loads(out["body"]) == {"result": "inline"}
    assert len(out["calls"]) == 1


@needs_node
def test_helper_rides_out_network_blips_while_polling():
    out = _node({
        "start": {"status": 202, "body": '{"task_id":"abc"}', "headers": {"X-ND-Task": "abc"}},
        "polls": ["throw", "throw", {"json": {"status": "done", "http_status": 200, "content_type": "application/json", "body": {"ok": 1}}}],
    })
    assert out["status"] == 200 and json.loads(out["body"]) == {"ok": 1}


@needs_node
def test_helper_explains_a_task_the_server_has_forgotten():
    out = _node({
        "start": {"status": 202, "body": '{"task_id":"abc"}', "headers": {"X-ND-Task": "abc"}},
        "polls": [{"status": 404, "json": {"detail": "Unknown AI task"}}],
    })
    assert out["status"] == 410 and "restart" in json.loads(out["body"])["detail"].lower()


@needs_node
def test_helper_cancels_the_server_task_when_the_caller_aborts():
    out = _node({
        "start": {"status": 202, "body": '{"task_id":"abc"}', "headers": {"X-ND-Task": "abc"}},
        "polls": [{"json": {"status": "running", "elapsed": 1}}],
        "abortAfterPolls": 2,
    })
    assert out["error"].startswith("AbortError")
    assert any(c["method"] == "DELETE" and c["url"] == "/api/ai/tasks/abc" for c in out["calls"])
