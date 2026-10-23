"""The Chronicler through the gateway (rust/nd-gateway/src/ai.rs) against Python's, with a fake Unsloth Studio backend both talk to:
the same system prompt and request reach the backend, and the same SSE stream comes back."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD
from .test_gateway_golden import DIRECT, _signed_in, _via

pytestmark = pytest.mark.skipif(not DIRECT, reason="needs ND_TEST_BASE_URL (the gateway) and ND_GOLDEN_DIRECT_URL (Python directly)")


class _Fake:
    """A Studio stand-in: /v1/models lists one model; /v1/chat/completions records the request and streams the scripted chunks."""

    def __init__(self):
        self.requests = []
        self.script = []
        self.status = 200
        self.reply = {}                 # the answer to a request that is not streamed
        self.delay = 0                  # seconds before that answer
        self.reject_thinking = None     # an error message: a 400 for any request asking for Thinking
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": "studio-model", "loaded": True}, {"id": "other"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n))
                fake.requests.append({"auth": self.headers.get("Authorization"), "body": req})
                rejected = fake.reject_thinking and (req.get("chat_template_kwargs") or {}).get("enable_thinking")
                if fake.status != 200 or rejected:
                    body = json.dumps({"error": {"message": fake.reject_thinking if rejected else "model exploded"}}).encode()
                    self.send_response(400 if rejected else fake.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if req.get("stream") is False:
                    if fake.delay:
                        __import__("time").sleep(fake.delay)
                    body = json.dumps(fake.reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev in fake.script:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _delta(**kw):
    return {"choices": [{"delta": kw, "finish_reason": None}]}


def _settings(d, **kw):
    form = {"llm_url": "", "llm_model": "", "llm_api_key": "", "ollama_url": "", "ollama_model": ""}
    form.update(kw)
    r = d.post("/settings/system", data=form, follow_redirects=False)
    assert r.status_code in (200, 303), r.status_code


def _ask(side, body):
    r = side.post("/api/chronicler/ask", json=body)
    return r.status_code, r.text, r.headers.get("x-nd-served-by")


@pytest.fixture
def studio(seed):
    fake = _Fake()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    _settings(gm, llm_url=fake.url, llm_model="studio", llm_api_key="sk-test", ollama_temperature="0.4", ollama_num_predict="300", ollama_num_ctx="8192")
    try:
        yield fake
    finally:
        _settings(gm, llm_api_key_clear="1")
        fake.server.shutdown()


def test_chronicler_answers_are_pythons(client, seed, studio):
    from app.database import SessionLocal
    from app.models import AiInstruction, Entity, EntityNote, Fact
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Fact(world_id=w, content="The bridge fell in the third winter.", visible_to_players=True),
                    Fact(world_id=w, content="The duke poisoned the well.", visible_to_players=False),
                    AiInstruction(world_id=w, title="Voice", content="  Speak like a sailor.  ", enabled=True, applies_to_players=True),
                    AiInstruction(world_id=w, title="Secret", content="The killer is Vane.", enabled=True, applies_to_players=False)])
        bridge = Entity(world_id=w, kind="location", subtype="ruin", name="Old Bridge", summary="A broken bridge [gmonly]built by Vane[/gmonly]",
                        body="# History\nThe bridge stood for ages.\n\n## Collapse\nIt fell in the third winter, during the siege of the bridges.\n\n## Rumours\n"
                             + "Ghosts walk it. " * 120, visible_to_players=True)
        vane = Entity(world_id=w, kind="character", name="Vane", summary="A builder", body="[gmonly]He poisoned the bridge well.[/gmonly]", visible_to_players=True)
        hidden = Entity(world_id=w, kind="location", name="Secret Bridge Vault", summary="hidden", visible_to_players=False)
        db.add_all([bridge, vane, hidden])
        db.flush()
        db.add_all([EntityNote(entity_id=bridge.id, content="Note about the bridge.", visible_to_players=True),
                    EntityNote(entity_id=bridge.id, content="GM note: trapdoor.", visible_to_players=False)])
        db.commit()
    finally:
        db.close()
    studio.script = [_delta(reasoning_content="mulling"), _delta(content="<thi"), _delta(content="nk>inner</think>The bridge "),
                     _delta(content="fell in winter."), {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    served = 0
    run = __import__("uuid").uuid4().hex[:6]      # a question of its own: each server keeps answers 30 s
    for email, pw, question in ((seed.gm.email, GM_PASSWORD, f"When did the bridge fall? ({run})"),
                                (seed.player_a.email, PLAYER_PASSWORD, f"Tell me about the bridges and Vane ({run})")):
        d = _signed_in(email, pw, seed.world_a.slug)
        out = []
        for side in (d, _via(d, base)):
            studio.requests.clear()
            status, text, by = _ask(side, {"question": f"  {question} ", "model": "", "think": False})
            out.append((status, text, [r["body"] for r in studio.requests], [r["auth"] for r in studio.requests], by))
        assert out[0][0] == 200, out[0]
        assert out[0][1] == out[1][1], (email, out[0][1], out[1][1])
        assert out[0][2] == out[1][2], (email, json.dumps(out[0][2], indent=1)[:3000], json.dumps(out[1][2], indent=1)[:3000])
        assert out[0][3] == out[1][3] == ["Bearer sk-test"]
        served += out[1][4] == "gateway"
        # asked again at once: the answer from each side's own 30 s cache, no backend call
        again = []
        for side in (d, _via(d, base)):
            studio.requests.clear()
            status, text, by = _ask(side, {"question": question})
            again.append((status, text, len(studio.requests)))
        assert again[0] == again[1], again
    assert served == 2, served


def test_chronicler_backend_errors_and_empty_answers_are_pythons(client, seed, studio):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [(500, []), (200, [_delta(reasoning_content="all thought, no answer")]), (200, [{"choices": [{"delta": {}, "finish_reason": "length"}]}]),
             (200, [_delta(content="partial "), {"error": {"message": "context overflow", "code": 413}}])]
    for i, (status, script) in enumerate(cases):
        studio.status, studio.script = status, script
        run = __import__("uuid").uuid4().hex[:6]
        out = [_ask(side, {"question": f"case {i} question {run}"}) for side in (gm, _via(gm, base))]
        assert out[0][:2] == out[1][:2], (i, out)
        assert out[1][2] == "gateway", out


def test_chronicler_what_stays_pythons(client, seed, studio):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    studio.script = [_delta(content="ok")]
    for body in ({"question": "  "}, {"question": 5}):
        r = _via(gm, base).post("/api/chronicler/ask", json=body)
        assert r.headers.get("x-nd-served-by") != "gateway", body


def test_chronicler_gm_model_and_thinking_are_pythons(client, seed, studio):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    studio.script = [_delta(reasoning_content="hmm"), _delta(content="Answer.")]
    run = __import__("uuid").uuid4().hex[:6]
    for i, extra in enumerate(({"model": "other"}, {"think": True}, {"model": " other ", "think": 1})):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            status, text, by = _ask(side, {"question": f"model case {i} {run}", **extra})
            out.append((status, text, [r["body"] for r in studio.requests], by))
        assert out[0][:3] == out[1][:3], (extra, out)
        assert out[1][3] == "gateway", extra


def _stream(side, body):
    r = side.post("/api/ai/stream", json=body)
    return r.status_code, r.text, r.headers.get("x-nd-served-by")


def _set_world(world_id, **kw):
    from app.database import SessionLocal
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def test_ai_stream_is_pythons(client, seed, studio):
    """POST /api/ai/stream (the AI Chat page and the entity page's Ask AI): the same request reaches the backend and the same SSE comes
    back - a GM's model, surface, preset options (clamped) and Thinking; a player's pinned to the surface default with a capped system text;
    document attachments folded into the message."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    studio.script = [_delta(reasoning_content="hmm"), _delta(content="Hello "), _delta(content="there.")]
    doc = {"kind": "document", "url": "/uploads/ai/x.txt", "name": "notes.txt", "text": "Line one. " * 2000}
    msgs = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Yes?"}, {"role": "user", "content": "Read this", "attachments": [doc]}]
    assert gm.post("/api/ai/defaults", json={"surface": "chat", "model_id": "other"}).status_code == 200
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    try:
        cases = [
            (gm, {"messages": msgs, "system": "Be brief."}),
            (gm, {"messages": msgs, "model": "studio", "think": True}),
            (gm, {"messages": msgs, "surface": "recap", "options": {"temperature": 3, "top_k": 2.7, "num_predict": 99, "seed": True, "bogus": 1,
                                                                   "top_p": None, "num_ctx": 4096, "min_p": [1]}, "think": True}),
            (player, {"messages": msgs, "model": "studio", "surface": "assist", "system": "S" * 60000, "think": True}),
            (player, {"messages": [], "surface": "ask_ai"}),
        ]
        for i, (d, body) in enumerate(cases):
            out = []
            for side in (d, _via(d, base)):
                studio.requests.clear()
                status, text, by = _stream(side, body)
                out.append((status, text, [r["body"] for r in studio.requests], by))
            assert out[0][0] == 200, (i, out[0][:2])
            assert out[0][:3] == out[1][:3], (i, out[0][1], out[1][1], json.dumps(out[0][2])[:2000], json.dumps(out[1][2])[:2000])
            assert out[1][3] == "gateway", i
        # what stays Python's: a refused player, a picture, a string option
        _set_world(seed.world_a.id, players_can_use_ai_chat=False)
        for d, body in ((player, {"messages": msgs}), (gm, {"messages": [{"role": "user", "content": "x", "attachments": [{"kind": "image", "url": "/uploads/a.png"}]}]}),
                        (gm, {"messages": msgs, "options": {"temperature": "0.5"}})):
            assert _stream(_via(d, base), body)[2] != "gateway", body
    finally:
        _set_world(seed.world_a.id, players_can_use_ai_chat=False)
        gm.post("/api/ai/defaults", json={"surface": "chat", "model_id": ""})


def test_npc_talk_stream_is_pythons(client, seed, studio):
    """POST /api/npc-talk/<id>/stream: the same in-character system prompt (the NPC's sheet under the caller's visibility, notes, related
    entities, the GM's standing notes), the same history sent, the same SSE, and the same conversation saved afterwards."""
    from app.database import SessionLocal
    from app.models import ChatSession, Entity, EntityNote
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        friend = Entity(world_id=w, kind="character", name="Mira", visible_to_players=True)
        secret = Entity(world_id=w, kind="location", name="Hidden Lair", visible_to_players=False)
        npc = Entity(world_id=w, kind="character", subtype="smith", name="Borin", summary="A smith [gmonly]and a spy[/gmonly]",
                     body="Borin forges blades.\n[gmonly]He sells them to the enemy.[/gmonly]" + " More." * 600,
                     custom_fields_json=json.dumps({"Age": 52, "Traits": ["gruff", "loyal"], "Mood": "[gmonly]scheming[/gmonly] calm", "Empty": "",
                                                    "Stats": {"STR": 14, "DEX": True}}),
                     roleplay_personality="  Speaks in short sentences. ", visible_to_players=True)
        npc.related = [friend, secret]
        db.add_all([friend, secret, npc])
        db.flush()
        db.add_all([EntityNote(entity_id=npc.id, content="Owes Mira money.", visible_to_players=True),
                    EntityNote(entity_id=npc.id, content="Has a trapdoor [gmonly]to the lair[/gmonly].", visible_to_players=False)])
        db.commit()
        nid = npc.id
    finally:
        db.close()
    _set_world(w, players_can_ask_ai=True)

    def conv(uid):
        s = SessionLocal()
        try:
            row = s.query(ChatSession).filter_by(world_id=w, user_id=uid, surface="npc", entity_id=nid).first()
            return (row.messages_json, row.title) if row else None
        finally:
            s.close()

    def restore(uid, snap):
        s = SessionLocal()
        try:
            s.query(ChatSession).filter_by(world_id=w, user_id=uid, surface="npc", entity_id=nid).delete()
            if snap:
                s.add(ChatSession(world_id=w, user_id=uid, surface="npc", entity_id=nid, title=snap[1], messages_json=snap[0]))
            s.commit()
        finally:
            s.close()

    try:
        for user, pw, extra in ((seed.gm, GM_PASSWORD, {"model": "other", "think": True}), (seed.player_a, PLAYER_PASSWORD, {"model": "other"})):
            d = _signed_in(user.email, pw, seed.world_a.slug)
            for turn, (msg, script) in enumerate((("  Hello, smith. ", [_delta(content="Aye.")]), ("What do you make?", [_delta(content="  Blades.  ")]),
                                                   ("And now?", [_delta(content="   ")]))):
                studio.script = script
                before = conv(user.id)
                out = []
                for side in (d, _via(d, base)):
                    restore(user.id, before)
                    studio.requests.clear()
                    status, text, by = _stream_npc(side, nid, {"message": msg, **extra})
                    out.append((status, text, [r["body"] for r in studio.requests], conv(user.id), by))
                assert out[0][0] == 200, out[0][:2]
                assert out[0][:4] == out[1][:4], (user.email, turn, json.dumps(out[0][2])[:3000], json.dumps(out[1][2])[:3000], out[0][3], out[1][3])
                assert out[1][4] == "gateway", turn
        gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        for body in ({"message": "x", "use_rag": True}, {"message": "   "}, {"message": 5}):
            assert _stream_npc(_via(gm, base), nid, body)[2] != "gateway", body
    finally:
        _set_world(w, players_can_ask_ai=False)


def _stream_npc(side, nid, body):
    r = side.post(f"/api/npc-talk/{nid}/stream", json=body)
    return r.status_code, r.text, r.headers.get("x-nd-served-by")


def test_a_refused_thinking_request_is_retried_as_python_does(client, seed, studio):
    """A 400 to a Thinking request: retried without Thinking (with the <|think|> token for a model known to think), except for a 400
    about something else (the context, a missing model), which is shown as the error."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    studio.script = [_delta(content="Plain answer.")]
    run = __import__("uuid").uuid4().hex[:6]
    for i, (reason, model) in enumerate((("thinking unsupported", "other"), ("thinking unsupported", "unsloth/gemma-4-26B-A4B-it-GGUF"),
                                         ("prompt exceeds the context window", "other"), ("model does not support thinking", "studio"))):
        studio.reject_thinking = reason
        for path, body in (("/api/chronicler/ask", {"question": f"retry {i} {run}", "model": model, "think": True}),
                           ("/api/ai/stream", {"messages": [{"role": "user", "content": "Hi"}], "system": "Sys", "model": model, "think": True})):
            out = []
            for side in (gm, _via(gm, base)):
                studio.requests.clear()
                r = side.post(path, json=body)
                out.append((r.status_code, r.text, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
            assert out[0][:3] == out[1][:3], (reason, model, path, out[0][1], out[1][1], json.dumps(out[0][2])[:2000], json.dumps(out[1][2])[:2000])
            assert out[1][3] == "gateway", (reason, path)
            assert len(out[0][2]) == (1 if "context" in reason else 2), (reason, path, len(out[0][2]))


def _reply(content="", reasoning=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"model": "x", "choices": [{"message": msg, "finish_reason": finish}], "usage": {"completion_tokens": 5}}


def test_ai_assist_free_text_ops_are_pythons(client, seed, studio):
    """POST /api/ai/assist (the editors' Expand / Improve / Translate / ... panel) for a free-text operation: the same prompt and options
    reach the backend, the same JSON comes back - the answer cleaned as `_clean_degenerate_recap` does, a failure as a 502."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    entity = {"kind": "character", "name": " Borin ", "summary": "A smith.", "tags": "npc, smith", "body": "Borin forges blades in the old quarter."}
    cases = [
        ({"op": "expand", **entity}, _reply("<think>plan</think>\n  Borin forges blades.\nBorin forges blades.\n\nHe is old.<|im_end|>\nuser\nWrite C code.  ")),
        ({"op": "improve", **entity, "think": True, "model": "other"}, _reply("Better text.", reasoning="hmm")),
        ({"op": "translate", **entity, "lang": " Spanish ", "instruction": "ignored"}, _reply("Borin forja espadas.")),
        ({"op": "translate", "body": "Hello", "instruction": "French"}, _reply("Bonjour")),
        ({"op": "custom", "summary": "Only a summary", "instruction": "Make it rhyme"}, _reply("A rhyme.")),
        ({"op": "analyze", "name": "Nameless"}, _reply("", reasoning="all thought")),
        ({"op": "summarize", **entity}, _reply("", finish="length")),
        ({"op": "folk_tale", **entity}, None),
    ]
    for i, (body, reply) in enumerate(cases):
        studio.reply, studio.status = reply or {}, (500 if reply is None else 200)
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            r = side.post("/api/ai/assist", json=body)
            out.append((r.status_code, r.text, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:2500], json.dumps(out[1][2])[:2500])
        assert out[1][3] == "gateway", i
    studio.status = 200
    # what stays Python's: lore retrieval, the JSON operations, refusals and Python's 400s
    for body in ({"op": "expand", **entity, "use_rag": True}, {"op": "suggest", **entity}, {"op": "custom", **entity}, {"op": "nope"},
                 {"op": "expand", "body": "   "}, {"op": "expand", "body": "x" * 60001}):
        assert _via(gm, base).post("/api/ai/assist", json=body).headers.get("x-nd-served-by") != "gateway", body
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    assert _via(player, base).post("/api/ai/assist", json={"op": "expand", **entity}).headers.get("x-nd-served-by") != "gateway"


def _job_result(side, job_id, timeout=20):
    import time
    end = time.time() + timeout
    while time.time() < end:
        r = side.get(f"/api/ai/assist-job/{job_id}")
        if r.json().get("status") not in ("pending", "summarizing"):
            return r.json()
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {r.json()}")


def _job_row(job_id):
    from app.database import SessionLocal
    from app.models import AudioJob
    db = SessionLocal()
    try:
        j = db.get(AudioJob, job_id)
        return {k: getattr(j, k) for k in ("purpose", "filename", "status", "error", "model", "think", "extra_instructions", "use_rag",
                                            "rag_entity_limit", "rag_notes_limit", "assist_params_json", "transcript", "audio_path",
                                            "delete_after", "result_json", "checkpoint_json", "resumed_count", "chunk_current", "chunk_total")}
    finally:
        db.close()


def test_ai_assist_job_is_pythons(client, seed, studio):
    """POST /api/ai/assist-job: the gateway writes the same audio_jobs row, runs it (the AI slot, one run_assist call) and leaves the
    row as Python's run leaves it - done with the same result_json, or the same error."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    entity = {"kind": "character", "name": "Borin", "summary": "A smith.", "tags": "npc", "body": "Borin forges blades."}
    cases = [({"op": "improve", **entity, "surface": "entity", "instruction": "  Tighter  ", "think": True}, _reply("Borin forges fine blades.")),
             ({"op": "translate", **entity, "lang": " German ", "rag_entity_limit": 4}, _reply("Borin schmiedet Klingen.")),
             ({"op": "expand", "summary": "only a summary"}, _reply("never asked")),                 # no content: run_assist's ValueError
             ({"op": "custom", **entity}, _reply("never asked")),                                    # no instruction
             ({"op": "summarize", **entity, "model": "other"}, _reply("", reasoning="all thought"))]
    for i, (body, reply) in enumerate(cases):
        studio.reply = reply
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            r = side.post("/api/ai/assist-job", json=body)
            assert r.status_code == 200, (i, r.text)
            job_id = r.json()["job_id"]
            res = _job_result(side, job_id)
            res.pop("id")
            out.append((res, _job_row(job_id), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:2000], json.dumps(out[1][2])[:2000])
        assert out[1][3] == "gateway", i
    # what stays Python's: a GM's lore retrieval, the JSON operations, an unknown operation
    for body in ({"op": "expand", **entity, "use_rag": True}, {"op": "suggest", **entity}, {"op": "nope"}):
        assert _via(gm, base).post("/api/ai/assist-job", json=body).headers.get("x-nd-served-by") != "gateway", body


def test_a_gateway_job_holds_the_ai_slot_and_can_be_cancelled(client, seed, studio):
    import time
    from app import shared_state
    base = str(client.base_url)
    gm = _via(_signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug), base)
    studio.reply, studio.delay = _reply("slow"), 3
    try:
        job_id = gm.post("/api/ai/assist-job", json={"op": "improve", "body": "Slow text."}).json()["job_id"]
        end = time.time() + 5
        while time.time() < end and not (shared_state.get_json("ai_slot") or {}).get("owner", "").startswith("gateway:"):
            time.sleep(0.05)
        lease = shared_state.get_json("ai_slot") or {}
        assert lease.get("owner", "").startswith("gateway:"), lease
        assert (shared_state.get_json(f"jobowner:audio_jobs:{job_id}") or {}).get("owner") == "gateway"
        r = gm.post(f"/api/audio-jobs/{job_id}/cancel")
        assert (r.status_code, r.json(), r.headers.get("x-nd-served-by")) == (200, {"ok": True}, "gateway")
        row = _job_row(job_id)
        assert (row["status"], row["error"]) == ("cancelled", "Cancelled by GM."), row
        end = time.time() + 5
        while time.time() < end and (shared_state.get_json("ai_slot") or {}).get("owner"):
            time.sleep(0.05)
        assert not (shared_state.get_json("ai_slot") or {}).get("owner")           # given back
        assert shared_state.get_json(f"jobowner:audio_jobs:{job_id}") is None      # no longer the gateway's
    finally:
        studio.delay = 0
