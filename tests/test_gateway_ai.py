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
        self.stt_texts = []             # speech-to-text answers in turn (None: a failure)
        self.image_refusals = 0         # "no model loaded" answers to give the image endpoint first
        self.video_reply = {"id": "vid-1"}
        self.video_list = {"data": [{"id": "vid-1", "status": "completed"}]}
        self.image_reply = (200, {"images": [{"url": "/gallery/one.png"}, {"url": "http://studio/gallery/two.png"}]})
        import base64 as _b64
        self.png = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFklEQVR4nGP8z8DwnwEJMDEwMDAwAAA5vAIBl3P8SQAAAABJRU5ErkJggg==")
        import struct as _st
        pcm = b"\x00\x00" * 800
        self.wav = b"RIFF" + _st.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + _st.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + _st.pack("<I", len(pcm)) + pcm
        self.speech = (200, self.wav, "audio/wav")   # the text-to-speech answer: (status, body, content type)
        self.delay = 0                  # seconds before that answer
        self.reply_seq = None           # answers to give in turn first (then `reply`)
        self.reject_thinking = None     # an error message: a 400 for any request asking for Thinking
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/v1/videos"):
                    fake.requests.append({"auth": self.headers.get("Authorization"), "body": {"get": self.path}})
                    if self.path.endswith("/content"):
                        body, ctype = b"\x00\x00\x00\x18ftypmp42fake-video", "video/mp4"
                    else:
                        body, ctype = json.dumps(fake.video_list).encode(), "application/json"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/gallery/"):
                    body = fake.png
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/"):
                    fake.requests.append({"auth": self.headers.get("Authorization"), "body": {"get": self.path}})
                    reply = {"loaded": True} if "images/status" in self.path else {"default_variant": "Q4_K_M", "variants": [
                        {"filename": "image-Q8_0.gguf", "quant": "Q8_0"}, {"filename": "image-Q4_K_M.gguf", "quant": "Q4_K_M"}]}
                    body = json.dumps(reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"data": [{"id": "studio-model", "loaded": True}, {"id": "other"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if self.path.endswith("/v1/audio/transcriptions"):
                    import re as _re
                    raw = self.rfile.read(n)
                    fname = _re.search(rb'filename="([^"]*)"', raw)
                    model = _re.search(rb'name="model"\r\n\r\n([^\r]*)', raw)
                    fake.requests.append({"auth": self.headers.get("Authorization"), "body": {"stt": fname and fname.group(1).decode(),
                                                                                              "model": model and model.group(1).decode()}})
                    text = fake.stt_texts.pop(0) if fake.stt_texts else "Someone speaks."
                    body = json.dumps({"text": text} if text is not None else {"error": {"message": "STT exploded"}}).encode()
                    self.send_response(200 if text is not None else 500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                req = json.loads(self.rfile.read(n))
                fake.requests.append({"auth": self.headers.get("Authorization"), "body": req})
                if self.path == "/v1/audio/speech":
                    status, body, ctype = fake.speech
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/v1/videos":
                    body = json.dumps(fake.video_reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/inference/images/"):
                    if self.path.endswith("/generate") and fake.image_refusals:
                        fake.image_refusals -= 1
                        status, reply = 409, {"detail": "No diffusion model is loaded"}
                    elif self.path.endswith("/generate"):
                        status, reply = fake.image_reply
                    else:
                        status, reply = 200, {"ok": True}
                    body = json.dumps(reply).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
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
                    reply = fake.reply_seq.pop(0) if fake.reply_seq else fake.reply
                    body = json.dumps(reply).encode()
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
    # run_assist's ValueErrors: the same 400 from the gateway
    for body in ({"op": "custom", **entity}, {"op": "code_edit", **entity}, {"op": "expand", "body": "   "}, {"op": "analyze"}):
        got = [(r.status_code, r.json()) for r in (gm.post("/api/ai/assist", json=body), _via(gm, base).post("/api/ai/assist", json=body))]
        assert got[0] == got[1] and got[0][0] == 400, (body, got)
    # what stays Python's: lore retrieval, the JSON operations, an unknown operation, content over the interactive cap
    for body in ({"op": "expand", **entity, "use_rag": True}, {"op": "suggest", **entity}, {"op": "nope"}, {"op": "expand", "body": "x" * 60001}):
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
             ({"op": "summarize", **entity, "model": "other"}, _reply("", reasoning="all thought")),
             ({"op": "improve", **entity, "use_rag": True, "rag_entity_limit": 3, "rag_notes_limit": 0}, _reply("With lore.")),
             ({"op": "code_edit", **entity, "instruction": "rename x", "use_rag": True}, _reply("diff"))]
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
    # what stays Python's: the JSON operations, an unknown operation
    for body in ({"op": "suggest", **entity}, {"op": "nope"}):
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


def _chat_row(job_id):
    from app.database import SessionLocal
    from app.models import ChatJob
    db = SessionLocal()
    try:
        j = db.get(ChatJob, job_id)
        return {k: getattr(j, k) for k in ("prompt", "messages_json", "system", "model", "options_json", "status", "error", "result", "resumed_count")}
    finally:
        db.close()


def test_background_chat_job_is_pythons(client, seed, studio):
    """POST /api/ai/chat/jobs (AI Chat's "process in background"): the same chat_jobs row (the last user prompt, the messages with
    documents folded in, the clamped options with the fitted num_ctx), the same request to the backend, the same done / error row."""
    import time
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    long_doc = {"kind": "document", "url": "/uploads/ai/x.txt", "name": "lore.txt", "text": "Lore line. " * 1500}
    msgs = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Yes?"}, {"role": "user", "content": "Summarise", "attachments": [long_doc]}]
    cases = [({"messages": msgs, "system": "Be brief.", "options": {"temperature": 0.3, "top_k": 9.7}}, _reply("Done it.")),
             ({"messages": [{"role": "user", "content": "Привет " * 3000}], "model": "other"}, _reply("Ответ")),
             ({"messages": [{"role": "user", "content": "x"}], "options": {"num_ctx": 2048}}, _reply("", finish="length"))]
    for i, (body, reply) in enumerate(cases):
        studio.reply = reply
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            r = side.post("/api/ai/chat/jobs", json=body)
            assert r.status_code == 200, (i, r.text)
            job_id = r.json()["job_id"]
            end = time.time() + 20
            while time.time() < end and _chat_row(job_id)["status"] in ("pending", "generating"):
                time.sleep(0.1)
            out.append((_chat_row(job_id), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], json.dumps(out[0][1])[:1500], json.dumps(out[1][1])[:1500])
        assert out[1][2] == "gateway", i
    # a player, a picture attachment, a string option: Python's
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    assert _via(player, base).post("/api/ai/chat/jobs", json={"messages": msgs}).headers.get("x-nd-served-by") != "gateway"
    for body in ({"messages": [{"role": "user", "content": "x", "attachments": [{"kind": "image", "url": "/u/a.png"}]}]},
                 {"messages": msgs, "options": {"temperature": "0.2"}}):
        assert _via(gm, base).post("/api/ai/chat/jobs", json=body).headers.get("x-nd-served-by") != "gateway", body


def test_a_gateway_chat_job_can_be_cancelled(client, seed, studio):
    import time
    base = str(client.base_url)
    gm = _via(_signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug), base)
    studio.reply, studio.delay = _reply("slow"), 3
    try:
        job_id = gm.post("/api/ai/chat/jobs", json={"messages": [{"role": "user", "content": "slow"}]}).json()["job_id"]
        end = time.time() + 5
        while time.time() < end and _chat_row(job_id)["status"] != "generating":
            time.sleep(0.05)
        r = gm.post(f"/api/ai/chat/jobs/{job_id}/cancel")
        assert (r.status_code, r.json(), r.headers.get("x-nd-served-by")) == (200, {"ok": True}, "gateway")
        row = _chat_row(job_id)
        assert (row["status"], row["error"]) == ("cancelled", "Cancelled by GM."), row
    finally:
        studio.delay = 0


def test_session_log_recap_is_pythons(client, seed, studio):
    """POST /api/session-log/<id>/recap: the job row, the request (the facts a GM / a player may see, instructions, length targets),
    the cleaned recap with its Rewards footer, the cached answer afterwards, the strict-length retry and the empty / failed answers."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob, Fact, GameSession, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        gs = GameSession(world_id=w, title="Night at the docks", xp_awarded=300, loot_json=json.dumps([{"name": "Rope", "qty": 2}, {"name": "Lantern"}]))
        empty = GameSession(world_id=w, title="Nothing yet")
        db.add_all([gs, empty])
        db.flush()
        db.add_all([Fact(world_id=w, game_session_id=gs.id, content="The party reached the docks.", visible_to_players=True),
                    Fact(world_id=w, game_session_id=gs.id, content="The harbourmaster is a cultist.", visible_to_players=False),
                    Fact(world_id=w, game_session_id=gs.id, content="They stole a boat.", visible_to_players=True)])
        db.get(World, w).recap_instructions = "  Write in present tense. "
        from app.models import Entity
        harbour = Entity(world_id=w, kind="location", name="Harbour", summary="Where the boats are.", body="# Docks\nThe docks smell of tar.")
        cult = Entity(world_id=w, kind="faction", name="Tide Cult", summary="Worshippers of the deep.", visible_to_players=False)
        note = Entity(world_id=w, kind="note", name="GM note on boats", body="Boats leak.")
        db.add_all([harbour, cult, note])
        db.flush()
        gs.npcs_json = json.dumps([{"entity_id": cult.id, "kind": "faction"}])
        db.commit()
        sid, eid = gs.id, empty.id
    finally:
        db.close()

    def clear(session_id):
        s = SessionLocal()
        try:
            s.query(AudioJob).filter(AudioJob.game_session_id == session_id).delete()
            s.commit()
        finally:
            s.close()

    def run(side, session_id, body):
        r = side.post(f"/api/session-log/{session_id}/recap", json=body)
        first = (r.status_code, r.json(), r.headers.get("x-nd-served-by"))
        end = time.time() + 20
        while r.status_code == 200 and r.json().get("pending") and time.time() < end:
            time.sleep(0.15)
            r = side.post(f"/api/session-log/{session_id}/recap", json=body)
        s = SessionLocal()
        try:
            j = s.query(AudioJob).filter(AudioJob.game_session_id == session_id).order_by(AudioJob.id.desc()).first()
            row = j and {k: getattr(j, k) for k in ("filename", "status", "error", "audience", "model", "think", "extra_instructions", "min_tokens",
                                                     "max_tokens", "condense_strictness", "result_json", "rag_entity_limit", "use_rag")}
        finally:
            s.close()
        return first[0], {k: v for k, v in first[1].items() if k != "job_id"}, r.status_code, r.json(), row, first[2]

    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    long_text = "\\n".join(f"The tide turned {i}." for i in range(30))
    cases = [
        (gm, sid, {"think": False, "extra_instructions": "  Mention the boat. ", "min_tokens": "50", "strictness": "firm"}, [_reply("<think>x</think>The party stole a boat.\\nThe party stole a boat.")]),
        (player, sid, {"model": "ignored", "think": True, "rag_entity_limit": 3}, [_reply("They reached the docks.", reasoning="hmm")]),
        (gm, sid, {"think": False, "strictness": "strict", "max_tokens": 5}, [_reply(long_text), _reply("Short.")]),
        (gm, sid, {"think": False, "model": "other"}, [_reply("", reasoning="all thought")]),
        (gm, eid, {}, [_reply("never")]),
        (gm, sid, {"think": False, "use_rag": True, "rag_entity_limit": "2", "rag_notes_limit": 1}, [_reply("With lore.")]),
    ]
    for i, (d, session_id, body, replies) in enumerate(cases):
        out = []
        for side in (d, _via(d, base)):
            clear(session_id)
            studio.requests.clear()
            seq = list(replies)
            studio.reply_seq = seq
            out.append((run(side, session_id, body), [q["body"] for q in studio.requests]))
        a, b = out
        assert a[0][:5] == b[0][:5], (i, a[0][:5], b[0][:5])
        assert a[1] == b[1], (i, json.dumps(a[1])[:2500], json.dumps(b[1])[:2500])
        assert b[0][5] == "gateway", i
        # asked again: the done row answers, from either server
        again = [side.post(f"/api/session-log/{session_id}/recap", json=body).json() for side in (d, _via(d, base))]
        assert again[0] == again[1], (i, again)
    studio.reply_seq = None


def test_world_summary_job_is_pythons(client, seed, studio):
    """POST /api/ai/world-summary: the same audio_jobs row and run - the campaign digest (entities, quests, facts, sessions), lore for a
    GM who asks for it, one world_summary call - ending with the same `recap` (or error)."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob, Entity, Fact, GameSession, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="character", subtype="smith", name="Borin", summary="Forges blades."),
                    Entity(world_id=w, kind="location", name="Hidden Vault", visible_to_players=False),
                    Quest(world_id=w, title="Find the ring", summary="It was lost.", status=None),
                    Fact(world_id=w, content="The king died.", visible_to_players=True),
                    GameSession(world_id=w, title="Session one", session_num=1, summary="Long " * 100)])
        db.commit()
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [({}, _reply("The campaign so far.")), ({"think": False, "model": "other"}, _reply("Short.")),
             ({"use_rag": True, "rag_entity_limit": 2, "think": False}, _reply("With lore.")), ({"think": False}, _reply("", finish="length"))]
    for i, (body, reply) in enumerate(cases):
        studio.reply = reply
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            r = side.post("/api/ai/world-summary", json=body)
            assert r.status_code == 200, r.text
            job_id = r.json()["job_id"]
            end = time.time() + 20
            s = SessionLocal()
            try:
                while time.time() < end and s.get(AudioJob, job_id).status in ("pending", "summarizing"):
                    s.expire_all()
                    time.sleep(0.1)
                j = s.get(AudioJob, job_id)
                row = {k: getattr(j, k) for k in ("filename", "status", "error", "recap", "model", "think", "use_rag", "rag_entity_limit", "audience")}
            finally:
                s.close()
            out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], json.dumps(out[0][1])[:2500], json.dumps(out[1][1])[:2500])
        assert out[1][2] == "gateway", i


def test_condense_job_is_pythons(client, seed, studio):
    """POST /api/sessions/ai/condense-job: the same row and run - instructions, length targets, lore with the session's pinned entities,
    the Thinking ladder for a starved answer, the strict-length retry, a model that refuses Thinking - ending in the same row."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob, Entity, GameSession
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        inn = Entity(world_id=w, kind="location", name="Prancing Pony", summary="An inn.")
        db.add(inn)
        db.flush()
        gs = GameSession(world_id=w, title="Bree", npcs_json=json.dumps([{"entity_id": inn.id, "kind": "location"}]))
        db.add(gs)
        db.commit()
        sid = gs.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    starved = _reply("", reasoning="thinking only")
    text = "The party met at the inn. They drank. A stranger watched them. " * 5
    cases = [
        ({"recap": text, "think": False, "extra_instructions": " Keep names. ", "max_tokens": 80}, [_reply("Condensed.")], None),
        ({"recap": text, "think": True}, [starved, starved, _reply("Finally.")], None),
        ({"recap": text, "think": False, "strictness": "strict", "min_tokens": 400}, [_reply("Too short."), _reply("Longer now.")], None),
        ({"recap": text, "think": False, "use_rag": True, "game_session_id": str(sid), "rag_entity_limit": 1, "rag_notes_limit": 0}, [_reply("With lore.")], None),
        ({"recap": text, "think": True, "model": "refuser"}, [_reply("Without thinking.")], "nope"),
    ]
    for i, (body, replies, reject) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq, studio.reject_thinking = list(replies), reject
            r = side.post("/api/sessions/ai/condense-job", json=body)
            assert r.status_code == 200, (i, r.text)
            job_id = r.json()["job_id"]
            end = time.time() + 20
            s = SessionLocal()
            try:
                while time.time() < end and s.get(AudioJob, job_id).status in ("pending", "summarizing"):
                    s.expire_all()
                    time.sleep(0.1)
                j = s.get(AudioJob, job_id)
                row = {k: getattr(j, k) for k in ("filename", "status", "error", "recap", "model", "think", "think_fallback", "expanded_thinking",
                                                    "think_rejected", "think_token_fallback", "use_rag", "game_session_id", "fit_context", "min_tokens",
                                                    "max_tokens", "condense_strictness", "extra_instructions", "transcript")}
            finally:
                s.close()
            out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        studio.reject_thinking = None
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], json.dumps(out[0][1])[:2500], json.dumps(out[1][1])[:2500])
        assert out[1][2] == "gateway", i
    studio.reply_seq = None


def test_facts_parse_job_is_pythons(client, seed, studio):
    """POST /api/facts/parse-job: the same row and run - the recap cut into chunks, one schema-constrained call each, the facts merged
    with repeats dropped and tags joined, an unusable chunk skipped, every chunk failing as the job's error."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)

    def facts(*items):
        return _reply(json.dumps({"facts": [{"content": c, "visible_to_players": v, "tags": t} for c, v, t in items]}))
    para = "The party walked to the old mill and argued about the map for a while. " * 60
    long_text = "\n\n".join([para] * 5)
    cases = [
        ({"text": "  went to the tavern, met Elyra  ", "extra_instructions": "Short facts."}, [facts(("They met Elyra.", True, [" elyra ", "", 3]))]),
        ({"text": long_text, "think": True}, [facts(("They argued.", True, ["map"]), ("A secret.", False, [])),
                                              facts(("They argued!", True, []), ("New fact.", 1, ["mill"]))]),
        ({"text": long_text}, [_reply("not json"), facts(("Only the second.", True, []))]),
        ({"text": "x happened"}, [_reply("{\"nope\": 1}")]),
    ]
    for i, (body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/api/facts/parse-job", json=body)
            assert r.status_code == 200, (i, r.text)
            job_id = r.json()["job_id"]
            end = time.time() + 20
            s = SessionLocal()
            try:
                while time.time() < end and s.get(AudioJob, job_id).status in ("pending", "summarizing"):
                    s.expire_all()
                    time.sleep(0.1)
                j = s.get(AudioJob, job_id)
                row = {k: getattr(j, k) for k in ("filename", "status", "error", "result_json", "model", "think", "chunk_current", "chunk_total", "extra_instructions", "transcript")}
            finally:
                s.close()
            out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], json.dumps(out[0][1])[:2500], json.dumps(out[1][1])[:2500])
        assert out[1][2] == "gateway", i
    studio.reply_seq = None


def test_live_transcript_recap_job_is_pythons(client, seed, studio):
    """POST /api/sessions/<id>/ai/summarize-live-transcript-job: the same row and run - one call for a transcript that fits, a summary
    per part (with checkpoints, progress and a failing part) for one that does not, the pending facts drafted from the transcript."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob, GameSession
    base = str(client.base_url)
    w = seed.world_a.id
    short = "GM: You enter the cave. Player: I light a torch. GM: Bats fly out."
    long_t = "\n".join(f"GM: Scene {i}. The party moves deeper into the ruins and finds carving number {i}. Player: We copy it down." for i in range(120))
    db = SessionLocal()
    try:
        a = GameSession(world_id=w, title="Caves", live_transcript=short)
        b = GameSession(world_id=w, title="Ruins", live_transcript=long_t)
        db.add_all([a, b])
        db.commit()
        sa, sb = a.id, b.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    _settings(gm, llm_url=studio.url, llm_model="studio", llm_api_key="sk-test", ollama_temperature="0.4", ollama_num_predict="300",
              ollama_num_ctx="8192", llm_context_tokens="2048")
    facts = _reply(json.dumps({"facts": [{"content": "Bats flew out.", "visible_to_players": True, "tags": ["cave"]}]}))
    parts = lambda n: [_reply(f"Part {k} summary.") for k in range(1, n + 1)]
    cases = [(sa, {"think": False, "extra_instructions": "Be vivid."}, [_reply("A short recap."), facts]),
             (sb, {"think": False}, parts(12) + [facts] * 12),
             (sb, {"think": False}, [_reply("Part one."), _reply("   ")]),
             (sa, {"think": True}, [_reply("", reasoning="x"), _reply("", reasoning="y"), _reply("Recap at last."), facts])]
    try:
        for i, (sid, body, replies) in enumerate(cases):
            out = []
            for side in (gm, _via(gm, base)):
                studio.requests.clear()
                studio.reply_seq = list(replies)
                r = side.post(f"/api/sessions/{sid}/ai/summarize-live-transcript-job", json=body)
                assert r.status_code == 200, (i, r.text)
                job_id = r.json()["job_id"]
                end = time.time() + 30
                s = SessionLocal()
                try:
                    while time.time() < end and s.get(AudioJob, job_id).status in ("pending", "summarizing"):
                        s.expire_all()
                        time.sleep(0.1)
                    j = s.get(AudioJob, job_id)
                    row = {k: getattr(j, k) for k in ("filename", "status", "error", "recap", "pending_facts_json", "think", "think_fallback",
                                                        "expanded_thinking", "chunk_current", "chunk_total", "checkpoint_json")}
                finally:
                    s.close()
                out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
            assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], len(out[0][1]), len(out[1][1]), json.dumps(out[0][1])[:1500], json.dumps(out[1][1])[:1500])
            assert out[1][2] == "gateway", i
    finally:
        studio.reply_seq = None
        _settings(gm, llm_url=studio.url, llm_model="studio", llm_api_key="sk-test", ollama_temperature="0.4", ollama_num_predict="300", ollama_num_ctx="8192")



def _tone(path, seconds):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "8000",
                    "-b:a", "8k", str(path)], check=True)


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_session_recording_jobs_are_pythons(client, seed, studio, tmp_path):
    """POST /api/sessions/ai/audio-jobs/from-clip and /complete: the same row (filename, the recording's path kept or the reassembled
    upload, delete_after), the recording transcribed (a long one split by ffmpeg into pieces, each to Studio's speech-to-text, with
    progress and checkpoints; a failing piece keeps the transcript so far), then the recap - and the same row at the end."""
    import os
    import time
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import AudioClip, AudioJob
    base = str(client.base_url)
    w = seed.world_a.id
    uploads = Path(os.environ["DB_PATH"]).parent / "uploads"
    (uploads / "audio").mkdir(parents=True, exist_ok=True)
    short, long_ = uploads / "audio" / "nd-test-short.mp3", uploads / "audio" / "nd-test-long.mp3"
    _tone(short, 20)
    _tone(long_, 16 * 60 + 30)
    db = SessionLocal()
    try:
        a = AudioClip(world_id=w, name="Table talk", file_url="/uploads/audio/nd-test-short.mp3")
        b = AudioClip(world_id=w, name="", file_url="/uploads/audio/nd-test-long.mp3")
        db.add_all([a, b])
        db.commit()
        ca, cb = a.id, b.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    facts = _reply(json.dumps({"facts": []}))

    def wait(job_id):
        end = time.time() + 60
        s = SessionLocal()
        try:
            while time.time() < end and s.get(AudioJob, job_id).status in ("pending", "transcribing", "summarizing"):
                s.expire_all()
                time.sleep(0.15)
            j = s.get(AudioJob, job_id)
            return {k: getattr(j, k) for k in ("filename", "status", "error", "transcript", "recap", "pending_facts_json", "chunk_current",
                                                "chunk_total", "checkpoint_json", "delete_after", "think", "model", "extra_instructions")}, j.audio_path
        finally:
            s.close()

    cases = [({"clip_id": str(ca), "think": "false", "extra_instructions": " Short. "}, ["Hello there.\nHello there."], [_reply("Recap of the clip."), facts]),
             ({"clip_id": str(cb), "think": "false"}, ["Part one words.", "Part two words."], [_reply("Long recap."), facts]),
             ({"clip_id": str(cb), "think": "false"}, ["Part one words.", None], [])]
    for i, (form, stt, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.stt_texts, studio.reply_seq = list(stt), list(replies)
            r = side.post("/api/sessions/ai/audio-jobs/from-clip", data=form)
            assert r.status_code == 200, (i, r.text)
            row, path = wait(r.json()["job_id"])
            out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by"), Path(path).is_file()))
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], out[0][1], out[1][1])
        assert out[1][2] == "gateway" and out[0][3] and out[1][3], (i, out[1][2:])     # a library clip is never deleted
    # /complete: the parts /chunk saved, reassembled; the reassembled file deleted after the run
    data = short.read_bytes()
    out = []
    for side in (gm, _via(gm, base)):
        studio.requests.clear()
        studio.stt_texts, studio.reply_seq = ["Uploaded words."], [_reply("Uploaded recap."), facts]
        uid = __import__("uuid").uuid4().hex
        for k, piece in enumerate((data[: len(data) // 2], data[len(data) // 2:])):
            assert gm.post("/api/sessions/ai/audio-jobs/chunk", data={"upload_id": uid, "chunk_index": str(k)},
                           files={"file": ("blob", piece, "application/octet-stream")}).status_code == 200
        r = side.post("/api/sessions/ai/audio-jobs/complete", data={"upload_id": uid, "filename": "night.MP3", "total_chunks": "2", "think": "0"})
        assert r.status_code == 200, r.text
        row, path = wait(r.json()["job_id"])
        sent = [{**q["body"], "stt": Path(q["body"]["stt"]).suffix} if "stt" in q["body"] else q["body"] for q in studio.requests]   # a random file name
        out.append((row, sent, r.headers.get("x-nd-served-by"), Path(path).exists()))
    assert out[0][:2] == out[1][:2], (out[0][0], out[1][0])
    assert out[1][2] == "gateway" and not out[0][3] and not out[1][3]
    studio.stt_texts, studio.reply_seq = [], None



def test_image_jobs_on_studio_are_pythons(client, seed, studio):
    """POST /api/ai/imagegen/jobs with an Unsloth Studio image model (UNSLOTH_IMAGE_MODEL): the same image_jobs row (every parameter, the
    `image` default model), the same native request to Studio, the pictures saved under uploads/ai-images, the model loaded once when
    Studio has none (the .gguf picked from Studio's variant list), Studio's error as the job's."""
    import os
    import time
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import ImageJob
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [({"prompt": "A dragon", "negative": "blurry", "width": 768, "height": 512, "steps": 0, "cfg": 4, "seed": 7, "batch_size": 12}, 0, None),
             ({"prompt": "A castle", "model": "some/model-GGUF"}, 1, None),
             ({"prompt": "Broken"}, 0, (500, {"detail": "Studio fell over"}))]
    uploads = Path(os.environ["DB_PATH"]).parent / "uploads"
    for i, (body, refusals, reply) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.image_refusals = refusals
            studio.image_reply = reply or (200, {"images": [{"url": "/gallery/one.png"}, {"url": "http://studio/gallery/two.png"}]})
            r = side.post("/api/ai/imagegen/jobs", json=body)
            assert r.status_code == 200, (i, r.text)
            job_id = r.json()["job_id"]
            end = time.time() + 20
            s = SessionLocal()
            try:
                while time.time() < end and s.get(ImageJob, job_id).status in ("pending", "generating"):
                    s.expire_all()
                    time.sleep(0.1)
                j = s.get(ImageJob, job_id)
                urls = json.loads(j.result_urls_json or "[]")
                row = {"status": j.status, "error": j.error, "params": json.loads(j.params_json), "prompt": j.prompt, "n": len(urls)}
                files = [(uploads / u[len("/uploads/"):]).is_file() for u in urls]
            finally:
                s.close()
            out.append((row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by"), files))
        assert out[0][:2] == out[1][:2], (i, out[0][0], out[1][0], json.dumps(out[0][1]), json.dumps(out[1][1]))
        assert out[1][2] == "gateway" and all(out[0][3]) and all(out[1][3]), (i, out[1][2:], out[0][3])
    studio.image_refusals = 0



def test_video_jobs_are_pythons(client, seed, studio):
    """POST /api/video-jobs/start: the same video_jobs row and answer, the generation started on Studio, its id kept, the list polled
    until the entry is done, the file kept as a hidden Video Library clip - or Studio's failure as the job's error."""
    import time
    from app.database import SessionLocal
    from app.models import VideoClip, VideoJob
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [({"prompt": "  A dragon over the sea  ", "model": "vid/model", "seconds": 4, "size": "640x360", "name": " Sea dragon "},
              {"data": {"id": "vid-1"}}, {"data": [{"id": "vid-1", "status": "succeeded"}]}),
             ({"prompt": "A castle"}, {"id": "vid-1"}, {"data": [{"id": "vid-1", "status": "generation_failed", "error": "out of memory"}]}),
             ({"prompt": "A forest", "seconds": 0}, {"nope": True}, {"data": []})]
    for i, (body, reply, listing) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.video_reply, studio.video_list = reply, listing
            r = side.post("/api/video-jobs/start", json=body)
            assert r.status_code == 200, (i, r.text)
            answer = r.json()
            job_id = answer.pop("id")
            answer["job"].pop("id")
            answer["job"].pop("created_at")
            end = time.time() + 20
            s = SessionLocal()
            try:
                while time.time() < end and s.get(VideoJob, job_id).status in ("pending", "generating", "fetching"):
                    s.expire_all()
                    time.sleep(0.1)
                j = s.get(VideoJob, job_id)
                clip = s.get(VideoClip, j.clip_id) if j.clip_id else None
                row = {"status": j.status, "error": j.error, "vid": j.studio_video_id, "params": j.params_json, "prompt": j.prompt,
                       "clip": clip and (clip.name, clip.description, clip.visible_to_players, clip.file_url.rsplit(".", 1)[-1])}
            finally:
                s.close()
            out.append((answer, row, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:3], out[1][:3])
        assert out[1][3] == "gateway", i


def test_a_background_assist_runs_as_a_task_like_pythons(client, seed, studio):
    """X-ND-Background: 1 on POST /api/ai/assist (app/ai_background.py): a 202 with a task id at once, the task polled from
    GET /api/ai/tasks/<id> until done with the route's own answer, listed in GET /api/ai/tasks, and forgotten by DELETE - the same
    shapes from the gateway's own tasks as from Python's."""
    import time
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    studio.reply = _reply("Better text.")
    body = {"op": "improve", "body": "Some text to improve."}
    seen = []
    for side in (gm, _via(gm, base)):
        r = side.post("/api/ai/assist", json=body, headers={"X-ND-Background": "1", "X-ND-Task-Label": "Polish"})
        assert r.status_code == 202, r.text
        tid = r.json()["task_id"]
        assert r.headers.get("x-nd-task") == tid and r.json()["status"] == "running" and r.json()["label"] == "Polish"
        end = time.time() + 20
        while time.time() < end:
            t = side.get(f"/api/ai/tasks/{tid}").json()
            if t["status"] == "done":
                break
            time.sleep(0.1)
        listed = [x for x in side.get("/api/ai/tasks").json()["tasks"] if x["id"] == tid]
        gone = side.delete(f"/api/ai/tasks/{tid}").json()
        seen.append(({k: t[k] for k in ("label", "status", "http_status", "content_type", "body")}, len(listed), gone,
                     side.get(f"/api/ai/tasks/{tid}").status_code, tid.startswith("gw"), r.headers.get("x-nd-served-by")))
    assert seen[0][:4] == seen[1][:4], seen
    assert not seen[0][4] and seen[1][4] and seen[1][5] == "gateway"
    assert seen[1][0]["body"]["text"] == "Better text."


def test_blocking_recap_and_facts_routes_are_pythons(client, seed, studio):
    """The blocking recap / facts routes (expand notes, condense, summarize a transcript or the live one, a recap from facts, parse facts,
    a folk tale): the same request to the backend and the same answer from the gateway, inline - and a refusal Python makes itself."""
    from app.database import SessionLocal
    from app.models import Fact, GameSession, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        gs = GameSession(world_id=w, title="Ruins", live_transcript="GM: You enter. Player: I look around.")
        db.add(gs)
        db.flush()
        db.add_all([Fact(world_id=w, game_session_id=gs.id, content="They entered the ruins."), Fact(world_id=w, content="Another fact.")])
        db.get(World, w).recap_instructions = "Be terse."
        db.commit()
        sid = gs.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    facts = _reply(json.dumps({"facts": [{"content": "A fact.", "visible_to_players": True, "tags": ["x"]}]}))
    cases = [
        ("/api/sessions/ai/expand-notes", {"notes": " went in, found a key ", "think": False}, [_reply("They went in.")]),
        ("/api/sessions/ai/expand-notes", {"notes": "  "}, []),
        ("/api/sessions/ai/condense-recap", {"recap": "A long recap. " * 20, "think": False, "max_tokens": 60, "extra_instructions": "Short."}, [_reply("Short.")]),
        ("/api/sessions/ai/summarize-transcript", {"transcript": "GM: Hi. Player: Hello.", "think": False}, [_reply("They greeted.")]),
        (f"/api/sessions/{sid}/ai/summarize-live-transcript", {"think": False}, [_reply("They entered.")]),
        (f"/api/sessions/{sid}/ai/summarize-from-facts", {"think": False}, [_reply("The ruins.")]),
        ("/api/facts/parse", {"text": "went to the ruins", "extra_instructions": "Short."}, [facts]),
        ("/api/facts/parse", {"text": "went to the ruins"}, [_reply("not json")]),
        ("/api/facts/folk-tale", {"instruction": "Sing it", "game_session_id": sid}, [_reply("Once upon a time.")]),
        ("/api/facts/folk-tale", {"game_session_id": 999999}, []),
        ("/api/ai/chat", {"messages": [{"role": "user", "content": "Hi"}], "system": "Be kind.", "options": {"temperature": 0.2}}, [_reply("Hello!")]),
        ("/api/ai/chat/compact", {"messages": [{"role": "user", "content": "Who is Borin?"}, {"role": "assistant", "content": " A smith. "}], "think": True},
         [_reply("They talked about Borin.")]),
        ("/api/ai/chat/compact", {"messages": []}, []),
        ("/api/ai/generate/entity", {"name": "Borin", "type": "character", "description": "A smith"}, [_reply("Borin is...")]),
        ("/api/ai/generate/npc", {"name": "Mira", "role": "spy"}, [_reply("Mira...")]),
        ("/api/ai/generate/location", {"name": "Bree", "region": "North", "notes": "rainy"}, [_reply("Bree...")]),
        ("/api/ai/generate/quest", {"title": "The ring"}, [_reply("A hook.")]),
        ("/api/ai/generate/entity-smart", {"name": "Ruins", "kind": "location", "subtype": "ancient", "summary": "old stones"}, [_reply("Lore.")]),
        ("/api/ai/chat/compact", {"messages": [{"role": "user", "content": "x"}]}, [_reply("", finish="length")]),
        ("/api/ai/entity-from-text", {"text": "Borin, a dwarf smith in Bree", "kind": "character"},
         [_reply(json.dumps({"kind": "character", "name": " Borin ", "summary": "A smith.", "tags": ["dwarf"]}))]),
        ("/api/ai/entity-from-text", {"text": "Bree, a rainy town", "kind": "nonsense"},
         [_reply(json.dumps({"kind": "location", "name": "Bree", "description": "Rainy."}))]),
        ("/api/ai/entity-from-text", {"text": "something"}, [_reply("not json")]),
        ("/api/bulk-edit/parse", {"instruction": "rename Skinwalker to Ashwalker"},
         [_reply(json.dumps({"understood": True, "find": " Skinwalker ", "replace": "Ashwalker", "note": 5}))]),
        ("/api/bulk-edit/parse", {"instruction": "rewrite all of it"}, [_reply(json.dumps({"understood": True, "find": ""}))]),
        ("/api/bulk-edit/parse", {"instruction": "x"}, [_reply("[1]")]),
        ("/api/bulk-edit/parse", {"instruction": "  "}, []),
        ("/api/ai/entities-from-text-batch", {"text": "Borin the smith. Bree the town."},
         [_reply(json.dumps({"entities": [{"kind": "character", "name": "Borin"}, {"kind": "location", "name": "Bree"}]}))]),
    ]
    for i, (path, body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(path, json=body)
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, path, out[0][:2], out[1][:2], json.dumps(out[0][2])[:1500], json.dumps(out[1][2])[:1500])
        assert out[1][3] == "gateway", (i, path)
    studio.reply_seq = None


def test_photo_import_drafts_are_pythons(client, seed, studio):
    """/api/ai/character-from-images and /api/ai/entity-from-images: the photos go to the model as image parts (JPEG sniffed, PNG otherwise),
    the draft comes back normalized - and the refusals (too many, a wrong type) are Python's, word for word."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    jpeg, png = b"\xff\xd8\xff\xe0" + b"j" * 40, b"\x89PNG\r\n\x1a\n" + b"p" * 40
    pc = _reply(json.dumps({"name": " Ana ", "race": "Elf", "level": "25", "xp": -4, "notes": "Gear"}))
    cases = [
        ("/api/ai/character-from-images", [("files", ("a.JPG", jpeg)), ("files", ("b.png", png))], {"hint": " Page 1 "}, [pc]),
        ("/api/ai/character-from-images", [("files", ("a.jpg", jpeg))], {}, [_reply(json.dumps({"name": "Bo", "level": "lots"}))]),
        ("/api/ai/entity-from-images", [("files", ("map.webp", png))], {}, [_reply(json.dumps({"kind": "location", "name": "Bree", "tags": "town"}))]),
        ("/api/ai/entity-from-images", [("files", ("map.webp", png))], {"hint": "a town"}, [_reply(json.dumps({"kind": "nope", "name": "Bree"}))]),
        ("/api/ai/entity-from-images", [("files", ("notes.txt", b"hi"))], {}, []),
        ("/api/ai/entity-from-images", [("files", (f"{i}.png", png)) for i in range(7)], {}, []),
    ]
    for i, (path, files, data, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(path, files=files, data=data)
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, path, out[0][:2], out[1][:2])
        assert out[1][3] == "gateway", (i, path)
    studio.reply_seq = None


def test_text_to_speech_is_pythons(client, seed, studio):
    """/api/ai/tts: the same speech request to Studio, the WAV kept or turned into Opus the same way, a GM-only clip saved with Python's
    name and description - and Studio's refusal and the route's own 400s word for word."""
    from app.database import SessionLocal
    from app.models import AudioClip
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [
        ({"text": " Hello there ", "name": " Greeting ", "voice": "tara", "instructions": " gruff ", "language": "en"}, None),
        ({"text": "Hi", "model": "my/tts", "description": "x" * 300}, None),
        ({"text": "Hi", "name": "Raw"}, (200, b"ID3fake-mp3", "audio/mpeg")),
        ({"text": "Hi"}, (400, json.dumps({"error": {"message": "no TTS model loaded"}}).encode(), "application/json")),
        ({"text": "   "}, None),
        ({"text": "x" * 20001}, None),
    ]
    for i, (body, speech) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.speech = speech or (200, studio.wav, "audio/wav")
            r = side.post("/api/ai/tts", json=body)
            data = r.json()
            clip = None
            if r.status_code == 200:
                db = SessionLocal()
                try:
                    c = db.get(AudioClip, data["id"])
                    clip = (c.world_id, c.name, c.description, c.visible_to_players, c.file_url.rsplit(".", 1)[1])
                finally:
                    db.close()
                data = {k: v for k, v in data.items() if k not in ("id", "file_url")}
            out.append((r.status_code, data, clip, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:4] == out[1][:4], (i, out[0][:3], out[1][:3])
        assert out[1][4] == "gateway", i


def test_npc_voice_is_pythons(client, seed, studio):
    """/api/npc-talk/<id>/speak: the voice hint derived from the public card (cleaned the same way) and cached, the speech asked for with
    it, the audio linked into the stored message - a cached hint and existing audio reused, `regenerate` redoing both - and the refusals."""
    from app.database import SessionLocal
    from app.models import ChatSession, Entity, EntityVoiceHint
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        npc = Entity(world_id=w, kind="character", subtype="smith", tags="dwarf", name="Borin", summary="A smith [gmonly]and a spy[/gmonly]",
                     body="Short. " * 300, roleplay_personality="Grumbles.")
        db.add(npc)
        db.commit()
        eid = npc.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    gm_id = seed.gm.id
    msgs = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": " Well met. "}, {"role": "assistant", "content": "x" * 4001}]

    def reset(hint=None, audio=None):
        d = SessionLocal()
        try:
            d.query(EntityVoiceHint).filter(EntityVoiceHint.entity_id == eid).delete()
            d.query(ChatSession).filter(ChatSession.entity_id == eid).delete()
            if hint is not None:
                d.add(EntityVoiceHint(entity_id=eid, world_id=w, instructions=hint, model="m"))
            m = [dict(x) for x in msgs]
            if audio:
                m[1]["audio"] = audio
            d.add(ChatSession(world_id=w, user_id=gm_id, surface="npc", entity_id=eid, title="Borin", messages_json=json.dumps(m)))
            d.commit()
        finally:
            d.close()

    def state():
        d = SessionLocal()
        try:
            h = d.get(EntityVoiceHint, eid)
            row = d.query(ChatSession).filter(ChatSession.entity_id == eid).first()
            return (h and (h.instructions, h.model), row and row.messages_json)
        finally:
            d.close()

    cases = [
        ({"index": 1}, {}, [_reply('Voice: "Gruff, slow, low."\nExtra')]),
        ({"index": 1}, {"hint": "  cached  "}, []),
        ({"index": 1}, {"hint": "cached", "audio": "/uploads/npc-talk/old.opus"}, []),
        ({"index": 1, "regenerate": True}, {"hint": "cached", "audio": "/uploads/npc-talk/old.opus"}, [_reply("a " * 300)]),
        ({"index": 0}, {}, []),
        ({"index": 7}, {}, []),
        ({"index": 2}, {}, []),
    ]
    for i, (body, setup, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            reset(**setup)
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(f"/api/npc-talk/{eid}/speak", json=body)
            out.append((r.status_code, r.json(), state(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:4] == out[1][:4], (i, out[0][:3], out[1][:3])
        assert out[1][4] == "gateway", i
    studio.reply_seq = None


def test_recap_song_is_pythons(client, seed, studio):
    """/api/sessions/<id>/recap-song: the lyrics cleaned for the voice, the style's delivery instruction, the clip saved with Python's name,
    description and lyrics and attached to the recap, the panel answered - and the refusals."""
    from app.database import SessionLocal
    from app.models import AudioClip, GameSession
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        gs = GameSession(world_id=w, title="The Ruins")
        db.add(gs)
        db.commit()
        sid = gs.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    song = "# The Ruin Song\n\n**Verse 1**  \nWe went _down_ [chorus]\n(repeat x2)\n\n\n\nAnd came back `up`"
    cases = [
        {"lyrics": song, "style": "Sea Shanty", "name": " Ruin song ", "voice": "tara", "visible_to_players": True},
        {"lyrics": "La la", "style": "unknown"},
        {"lyrics": 12345},
        {"lyrics": "   "},
        {"lyrics": "[chorus]"},
        {"lyrics": "x" * 20001},
    ]
    for i, body in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            d = SessionLocal()
            try:
                d.get(GameSession, sid).recap_audio_json = "[]"
                d.commit()
            finally:
                d.close()
            studio.requests.clear()
            r = side.post(f"/api/sessions/{sid}/recap-song", json=body)
            data = r.json()
            if r.status_code == 200:
                for k in ("clip",):
                    data[k] = {x: y for x, y in data[k].items() if x not in ("id", "file_url")}
                data["clips"] = [{x: y for x, y in c.items() if x not in ("id", "file_url")} for c in data["clips"]]
                data.pop("library")
            out.append((r.status_code, data, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2])
        assert out[1][3] == "gateway", i


def test_clip_transcription_is_pythons(client, seed, studio):
    """/audio/<id>/transcribe and /video/<id>/transcribe: the clip's file sent to speech-to-text, repeats collapsed, the transcript saved -
    an empty one and Studio's failure refused as Python does, a missing file 404s."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import AudioClip, VideoClip
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    (up / "audio").mkdir(parents=True, exist_ok=True)
    (up / "video").mkdir(parents=True, exist_ok=True)
    (up / "audio" / "talk.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    (up / "video" / "talk.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    db = SessionLocal()
    try:
        a = AudioClip(world_id=w, name="Talk", file_url="/uploads/audio/talk.mp3")
        v = VideoClip(world_id=w, name="Talk", file_url="/uploads/video/talk.mp4")
        gone = AudioClip(world_id=w, name="Gone", file_url="/uploads/audio/missing.mp3")
        db.add_all([a, v, gone])
        db.commit()
        ids = {"audio": a.id, "video": v.id, "gone": gone.id}
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [
        (f"/audio/{ids['audio']}/transcribe", ["Hello\nHello\nHello\nHello\nHello\nBye"], AudioClip, ids["audio"]),
        (f"/video/{ids['video']}/transcribe", ["Spoken words."], VideoClip, ids["video"]),
        (f"/audio/{ids['audio']}/transcribe", ["   "], AudioClip, ids["audio"]),
        (f"/audio/{ids['audio']}/transcribe", [None], AudioClip, ids["audio"]),
        (f"/audio/{ids['gone']}/transcribe", [], AudioClip, ids["gone"]),
    ]
    for i, (path, texts, model, cid) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            d = SessionLocal()
            try:
                d.get(model, cid).transcript = ""
                d.commit()
            finally:
                d.close()
            studio.requests.clear()
            studio.stt_texts = list(texts)
            r = side.post(path)
            d = SessionLocal()
            try:
                saved = d.get(model, cid).transcript
            finally:
                d.close()
            out.append((r.status_code, r.json(), saved, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:4] == out[1][:4], (i, out[0][:3], out[1][:3])
        assert out[1][4] == "gateway", i


def test_mystery_board_is_pythons(client, seed, studio):
    """/boards/generate-mystery: the reply read as Python reads it (fences, trailing prose, a reply cut off mid-list), the board laid out
    by role with the same positions, links and slug - a replaced board updated in place - and the refusals."""
    from app.database import SessionLocal
    from app.models import InvestBoard
    base = str(client.base_url)
    w = seed.world_a.id
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    nodes = [{"role": "Victim", "title": "Lord Ash", "body": "Found dead " * 40, "links": []},
             {"role": "suspect", "title": "Mira", "body": "Spy", "links": [0, "2", 2, 1, 9, "x", 1.0]},
             {"role": "suspect", "title": "Tom", "links": "03"},
             {"role": "clue", "title": " Knife ", "body": 7, "links": {"1": True}},
             {"role": "", "title": "Fog", "links": None},
             {"role": "location", "title": ""},
             "junk"]
    full = json.dumps({"title": "  The Missing Ring  ", "nodes": nodes})
    cut = json.dumps({"title": "Cut", "nodes": nodes[:5]})
    cut = cut[:cut.rindex('{"role": "", "title": "Fog"') + 12]
    cases = [
        ({"premise": " A lord is dead "}, [_reply("```json\n" + full + "\n```\nHope this helps!")]),
        ({"premise": "Cut off", "title": ""}, [_reply("Here: " + cut)]),
        ({"premise": "Named", "title": "Case of the Ring", "replace": "case-the-missing-ring"}, [_reply(full)]),
        ({"premise": "Bad"}, [_reply("no board here")]),
        ({"premise": "Few"}, [_reply(json.dumps({"nodes": nodes[:2]}))]),
        ({"premise": "  "}, []),
        ({"premise": "x" * 4001}, []),
    ]
    for i, (form, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            d = SessionLocal()
            try:
                d.query(InvestBoard).filter(InvestBoard.world_id == w, InvestBoard.slug != "case-the-missing-ring").delete()
                if not d.query(InvestBoard).filter(InvestBoard.slug == "case-the-missing-ring").first():
                    d.add(InvestBoard(world_id=w, name="Old", slug="case-the-missing-ring", nodes_json="{}", edges_json="[]"))
                d.commit()
            finally:
                d.close()
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/boards/generate-mystery", data=form, follow_redirects=False)
            d = SessionLocal()
            try:
                boards = sorted((b.name, b.slug, b.description, b.canvas_bg, b.nodes_json, b.edges_json) for b in d.query(InvestBoard).filter(InvestBoard.world_id == w))
            finally:
                d.close()
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            out.append((r.status_code, r.headers.get("location"), body, boards, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:5] == out[1][:5], (i, out[0][:4], out[1][:4])
        assert out[1][5] == "gateway", i
    studio.reply_seq = None


def test_calendar_ai_helpers_are_pythons(client, seed, studio):
    """/calendar/ai-day and /calendar/ai-design: the same prompt, the reply read and cleaned as Python does (a draft calendar's months,
    weekdays, moons and holidays fitted to the limits), and the 'not usable' refusals."""
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    design = {"era_name": "  After the Fall  ", "year_format": "{year} AF", "days_per_week": "5", "weekday_names": ["Ash", 2, "", None, "Ember", "Extra"],
              "months": [{"name": "Frost", "days": 75, "season": "Winter", "color": "#abc"}, {"name": 12, "days": "20"}, {"days": 0}],
              "moons": [{"name": "Pale", "cycle_days": "31", "color": "#fff"}, {"cycle_days": 0, "offset": 3}],
              "holidays": [{"name": "Thaw", "month": "frost", "day": 99, "notes": "Melt"}, {"name": "Second", "month": 1, "day": 2},
                           {"name": "Nowhere", "month": "Nope"}, {"name": "Listy", "month": [1]}, "junk"]}
    cases = [
        ("/calendar/ai-day", {"date_label": " 3rd of Frost ", "context": "A port town " * 60},
         [_reply(json.dumps({"weather": "Rain " * 60, "omen": 5, "sights": ["A", " ", "B" * 200, "C", "D"]}))]),
        ("/calendar/ai-day", {}, [_reply("Sure! " + json.dumps({"sights": "xy"}) + " enjoy")]),
        ("/calendar/ai-day", {}, [_reply(json.dumps({"weather": ""}))]),
        ("/calendar/ai-design", {"brief": " moons please "}, [_reply("```json\n" + json.dumps(design) + "\n```")]),
        ("/calendar/ai-design", {}, [_reply(json.dumps({"months": "lots", "days_per_week": 99, "weekday_names": "A, B;C\nD"}))]),
        ("/calendar/ai-design", {}, [_reply(json.dumps({"months": [{"name": "A", "days": [1]}], "moons": "x", "year_format": "no year"}))]),
    ]
    for i, (path, form, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(path, data=form)
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, path, out[0][:2], out[1][:2])
        assert out[1][3] == "gateway", (i, path)
    studio.reply_seq = None


def test_combat_tactics_is_pythons(client, seed, studio):
    """/combat/<id>/ai-tactics: the roster described the same way (HP, shock, resources, conditions, who acts, initiative or fluid turns),
    the reply read into at most six tactics and a beat - and the refusals."""
    from app.database import SessionLocal
    from app.models import CombatSession
    base = str(client.base_url)
    w = seed.world_a.id
    fighters = [
        {"id": "a", "name": "Orc", "source": "npc", "hp": 7, "max_hp": 10, "temp_hp": 2, "armor": 3, "initiative": 12,
         "conditions": ["prone", "dazed", "prone"], "res": {"rage": {"label": "Rage", "cur": 1, "max": 3}, "x": {"cur": 2}, "bad": 5}, "notes": "Hates elves " * 20},
        {"id": "b", "name": "Ana", "source": "pc", "pc_id": 1, "hp": 0, "max_hp": 8, "max_shock": 4, "initiative": 15},
        {"id": "c", "name": "Wolf", "hp": 3, "max_hp": 3, "initiative": 9, "status": "dead"},
        {"name": "Nameless", "initiative": 1},
    ]
    db = SessionLocal()
    try:
        fight = CombatSession(world_id=w, name="Ambush", ruleset="generic", round_num=2, active_idx=1, combatants_json=json.dumps(fighters))
        fluid = CombatSession(world_id=w, name="Chase", ruleset="generic", round_num=1, combatants_json=json.dumps(fighters[:2]),
                              turn_json=json.dumps({"mode": "fluid", "active_id": "b", "acted": ["a"]}))
        empty = CombatSession(world_id=w, name="Quiet", ruleset="generic", combatants_json="[]")
        db.add_all([fight, fluid, empty])
        db.commit()
        ids = (fight.id, fluid.id, empty.id)
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    tactics = [{"who": "Orc", "move": "Charge " * 50}, {"who": "", "move": "x"}, "junk"] + [{"who": f"N{i}", "move": "m"} for i in range(8)]
    cases = [
        (ids[0], [_reply(json.dumps({"tactics": tactics, "beat": "The roof caves in. " * 30}))]),
        (ids[1], [_reply("```" + json.dumps({"tactics": "none", "beat": "Smoke"}) + "```")]),
        (ids[0], [_reply(json.dumps({"tactics": []}))]),
        (ids[2], []),
    ]
    for i, (cid, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(f"/combat/{cid}/ai-tactics")
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:2000], json.dumps(out[1][2])[:2000])
        assert out[1][3] == "gateway", i
    studio.reply_seq = None


def test_party_insights_is_pythons(client, seed, studio):
    """/parties/<id>/ai-insights: each member described by its own system (a native sheet's race, class, level and vitals; a custom
    sheet's tracks and compared fields; an NPC ally), the reply read into bonds, tensions and hooks - and the refusals."""
    from app.database import SessionLocal
    from app.models import Entity, Party, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    fields = [{"id": "hp", "label": "Health (0 = Broken)", "type": "resource", "vital": "hp", "default_value": "5/5"},
              {"id": "grit", "label": "Grit", "type": "resource", "default_value": "2/3"},
              {"id": "nerve", "label": "Nerve [roll]", "type": "number", "default_value": "4"},
              {"id": "calling", "label": "Calling", "type": "select"},
              {"id": "xp", "label": "XP", "type": "number", "xp": True},
              {"id": "note", "label": "Note", "type": "text"}]
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Moonlit", slug=f"moonlit-{w}", sheet_mode="custom", fields_json=json.dumps(fields))
        db.add(tpl)
        db.flush()
        native = PlayerCharacter(world_id=w, name="Ana", player_name="Archie", race="Elf", char_class="Rogue", level=3, current_hp=4,
                                 conditions_json=json.dumps(["Stunned", "stunned", "Prone"]), backstory="Raised   by wolves.\n" * 40)
        custom = PlayerCharacter(world_id=w, name="Bo", sheet_template_id=tpl.id, custom_fields_json=json.dumps({"hp_current": 3, "calling": "Hunter", "nerve": 6}))
        ally = Entity(world_id=w, kind="character", name="Mira")
        db.add_all([native, custom, ally])
        db.flush()
        party = Party(world_id=w, name="The Lanterns", member_pc_ids_json=json.dumps([custom.id, native.id]), member_entity_ids_json=json.dumps([ally.id]))
        lonely = Party(world_id=w, name="Alone", member_pc_ids_json=json.dumps([native.id]))
        db.add_all([party, lonely])
        db.commit()
        pid, lone = party.id, lonely.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [
        (pid, [_reply(json.dumps({"bonds": ["Old friends", " ", "x" * 300, 5, "a", "b"], "tensions": "ab",
                                  "hooks": [{"who": "Ana", "hook": "Find the wolves"}, {"who": "", "hook": "x"}, "junk"]}))]),
        (pid, [_reply(json.dumps({"bonds": []}))]),
        (lone, []),
    ]
    for i, (party_id, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(f"/parties/{party_id}/ai-insights")
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:3000], json.dumps(out[1][2])[:3000])
        assert out[1][3] == "gateway", i
    studio.reply_seq = None


def test_random_table_generator_is_pythons(client, seed, studio):
    """/tables/generate-ai: the count clamped into the prompt, the entries kept as Python keeps them, the table saved under the same
    unique slug with Python's fallbacks, the redirect to its editor - and the refusals."""
    from app.database import SessionLocal
    from app.models import RandomTable
    base = str(client.base_url)
    w = seed.world_a.id
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    entries = ["A crow", " ", None, 7, "x" * 400] + [f"Thing {i}" for i in range(45)]
    cases = [
        ({"description": " Street sights in Bree ", "count": "50", "category": " urban "}, [_reply(json.dumps({"name": "Bree Sights!", "description": " Sights ", "entries": entries}))]),
        ({"description": "Ünïcode names", "count": "0", "category": ""}, [_reply(json.dumps({"name": "Café Crème", "entries": entries[:6]}))]),
        ({"description": "No name"}, [_reply("```" + json.dumps({"entries": ["a", "b", "c", "d"]}) + "```")]),
        ({"description": "Too few"}, [_reply(json.dumps({"entries": ["a", "b"]}))]),
        ({"description": "  "}, []),
    ]
    for i, (form, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            d = SessionLocal()
            try:
                d.query(RandomTable).filter(RandomTable.world_id == w, RandomTable.is_builtin == False).delete()  # noqa: E712
                d.commit()
            finally:
                d.close()
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/tables/generate-ai", data=form, follow_redirects=False)
            d = SessionLocal()
            try:
                rows = [(t.name, t.slug, t.category, t.description, t.entries_json, t.created_by_user_id)
                        for t in d.query(RandomTable).filter(RandomTable.world_id == w, RandomTable.is_builtin == False)]  # noqa: E712
            finally:
                d.close()
            loc = (r.headers.get("location") or "").split("/")[1:2]
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else ""
            out.append((r.status_code, loc, body, rows, [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:5] == out[1][:5], (i, out[0][:4], out[1][:4])
        assert out[1][5] == "gateway", i
    studio.reply_seq = None


def test_session_prep_is_pythons(client, seed, studio):
    """/api/sessions/<id>/prep/generate: the context built from the last session's recap and facts, the open quests and the party, the
    checklist read back - an empty context and an unusable reply refused as Python does."""
    from app.database import SessionLocal
    from app.models import Fact, GameSession, Party, PlayerCharacter, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        prior = GameSession(world_id=w, session_num=900, title="Old Road", summary="They walked.")
        db.add(prior)
        db.flush()
        db.add_all([Fact(world_id=w, game_session_id=prior.id, content="A bridge fell."), Fact(world_id=w, game_session_id=prior.id, content="Mira lied.")])
        pc = PlayerCharacter(world_id=w, name="Ana", race="Elf", char_class="Rogue", level=2)
        db.add(pc)
        db.flush()
        party = Party(world_id=w, name="Lanterns", member_pc_ids_json=json.dumps([pc.id]))
        db.add_all([party, Quest(world_id=w, title="Zeta hunt", status="active", summary="Find it"), Quest(world_id=w, title="Alpha", status="active"),
                    Quest(world_id=w, title="Done", status="completed")])
        db.flush()
        nxt = GameSession(world_id=w, session_num=901, title="Next", party_id=party.id)
        db.add(nxt)
        db.commit()
        sid = nxt.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    cases = [
        [_reply(json.dumps({"tasks": [" Prep the bridge ", "", 5, "Stat Mira"]}))],
        [_reply(json.dumps({"tasks": "nope"}))],
        [_reply("not json")],
    ]
    for i, replies in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(f"/api/sessions/{sid}/prep/generate")
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:2000], json.dumps(out[1][2])[:2000])
        assert out[1][3] == "gateway", i
    studio.reply_seq = None


def test_kiy_build_model_is_pythons(client, seed, studio):
    """/api/kiy/build-model needs a local Ollama daemon: on Unsloth Studio both servers look the model up and stream the same error event;
    with no plays saved both refuse."""
    import os
    from pathlib import Path
    base = str(client.base_url)
    plays = Path(os.environ["DB_PATH"]).parent / "kiy_plays.json"
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    try:
        for content in (json.dumps([{"text": "ACT I"}, {"text": "ACT II"}]), "[]"):
            plays.write_text(content)
            out = []
            for side in (gm, _via(gm, base)):
                studio.requests.clear()
                r = side.post("/api/kiy/build-model")
                out.append((r.status_code, r.text, r.headers.get("content-type"), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
            assert out[0][:4] == out[1][:4], (out[0][:3], out[1][:3])
            assert out[1][4] == "gateway"
    finally:
        plays.unlink(missing_ok=True)


def _media_files(up):
    """Real files with tags inside them, made by ffmpeg and Pillow: what the renamer reads its clues from."""
    import subprocess
    from PIL import Image, PngImagePlugin
    (up / "audio").mkdir(parents=True, exist_ok=True)
    (up / "video").mkdir(parents=True, exist_ok=True)
    (up / "images").mkdir(parents=True, exist_ok=True)
    tags = ["-metadata", "title=Ember Waltz", "-metadata", "artist=The Lanterns", "-metadata", "album=Night Songs", "-metadata", "genre=Folk",
            "-metadata", "date=2021-05-01", "-metadata", "comment=Played at the inn"]
    made = {}
    for name, extra in (("aaaaaaaaaaaa-ember_waltz.mp3", ["-id3v2_version", "3"]), ("bbbbbbbbbbbb-rain.flac", []), ("cccccccccccc-chant.ogg", ["-c:a", "libopus"]),
                        ("dddddddddddd-door-creak.wav", []), ("eeeeeeeeeeee-IMG_2041.m4a", [])):
        path = up / "audio" / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", *tags, *extra, str(path)], check=True)
        made[name] = f"/uploads/audio/{name}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", "-metadata", "title=Market Day",
                    "-pix_fmt", "yuv420p", str(up / "video" / "ffffffffffff-market.mp4")], check=True)
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "a red dragon over a ruined keep, night\nNegative prompt: blurry\nSteps: 20")
    info.add_text("Title", "Dragon Keep")
    Image.new("RGB", (900, 600), (200, 30, 30)).save(up / "images" / "gggggggggggg-dragon.png", pnginfo=info)
    exif = Image.Exif()
    exif[0x010E] = "A map of the north"
    exif[0x013B] = "Mira"
    exif[0x9C9B] = "Northern Map".encode("utf-16-le") + b"\x00\x00"
    Image.new("RGB", (64, 48), (10, 120, 40)).save(up / "images" / "hhhhhhhhhhhh-DSC_0042.jpg", exif=exif, comment=b"Scanned handout")
    Image.new("RGB", (30, 20), (0, 0, 200)).save(up / "images" / "iiiiiiiiiiii-portrait.webp")
    return made


def test_media_renamer_suggest_is_pythons(client, seed, studio):
    """/api/media-rename/suggest: the clues read from the files (ID3, FLAC / Opus / Vorbis comments, WAV, MP4 atoms, PNG text, JPEG EXIF
    and comment, ffprobe for the rest), where images are used, the prompt and the batches, the names cleaned - and the refusals."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import AudioAlbum, AudioClip, Entity, ImageAlbum, MediaTitle, VideoClip
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    files = _media_files(up)
    db = SessionLocal()
    try:
        album = AudioAlbum(world_id=w, name="Tavern")
        db.add(album)
        db.flush()
        hero = Entity(world_id=w, kind="character", name="Borin", image_url="/uploads/images/gggggggggggg-dragon.png")
        db.add(hero)
        db.flush()
        clips = [AudioClip(world_id=w, name=n, file_url=u, album_id=album.id if i == 0 else None, entity_id=hero.id if i == 1 else None,
                           transcript="Hello   there" if i == 2 else "", description="An old tune" if i == 0 else "")
                 for i, (n, u) in enumerate(zip(["Track 07", "rain", "", "Door Creak", "IMG_2041"], files.values()))]
        db.add_all(clips)
        vid = VideoClip(world_id=w, name="VID_0001", file_url="/uploads/video/ffffffffffff-market.mp4")
        db.add(vid)
        db.add(ImageAlbum(world_id=w, name="Handouts", image_urls_json=json.dumps(["/uploads/images/hhhhhhhhhhhh-DSC_0042.jpg", "/uploads/images/iiiiiiiiiiii-portrait.webp"])))
        db.add(MediaTitle(world_id=w, url="/uploads/images/iiiiiiiiiiii-portrait.webp", title="Old portrait"))
        db.commit()
        ids = [c.id for c in clips] + [vid.id]
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    audio = [{"kind": "audio", "id": i} for i in ids[:5]]
    images = [{"kind": "image", "url": "/uploads/images/gggggggggggg-dragon.png"}, {"kind": "image", "url": "/uploads/images/hhhhhhhhhhhh-DSC_0042.jpg"},
              {"kind": "image", "url": "/uploads/images/iiiiiiiiiiii-portrait.webp"}]
    reply = _reply(json.dumps({"names": [{"id": "1", "name": " \"Ember Waltz.mp3\" "}, {"id": "2", "name": "rain"}, {"id": 3, "name": "Title: 1. Low Chant"},
                                         {"id": "4", "name": "Door/Creak?"}, {"id": "9", "name": "x"}, {"id": "5", "name": ""}, {"id": "6", "name": "Market Day " * 12}]}))
    cases = [
        ({"items": audio + [{"kind": "video", "id": ids[5]}], "preset": "music", "style": "  keep   it short "}, [reply]),
        ({"items": images + [{"kind": "audio", "id": 999999}, {"kind": "audio", "id": "x"}, {"kind": "image", "url": "/uploads/images/nope.png"}]},
         [_reply("```json\n" + json.dumps({"1": "Red Dragon Keep", "2": "Map of the North", "3": "Old portrait"}) + "\n```")]),
        ({"items": audio[:1]}, [_reply("no json at all")]),
        ({"items": []}, []),
        ({"items": audio * 3}, []),
    ]
    for i, (body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/api/media-rename/suggest", json=body)
            out.append((r.status_code, r.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:4000], json.dumps(out[1][2])[:4000])
        assert out[1][3] == "gateway", i
    r = gm.post("/api/media-rename/suggest", data="not json", headers={"content-type": "application/json"})
    r2 = _via(gm, base).post("/api/media-rename/suggest", data="not json", headers={"content-type": "application/json"})
    assert (r.status_code, r.json()) == (r2.status_code, r2.json())
    studio.reply_seq = None


def test_media_renamer_runs_are_pythons(client, seed, studio):
    """A bulk run started through the gateway is worked through by the gateway - chunk by chunk, saved after each - and ends with the same
    proposals a Python run makes; a running gateway run cancels through the gateway."""
    import os
    import time
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import AudioClip, MediaRenameRun
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    files = _media_files(up)
    db = SessionLocal()
    try:
        clips = [AudioClip(world_id=w, name=f"Track {i:02d}", file_url=u) for i, u in enumerate(list(files.values()) * 2)]
        db.add_all(clips)
        db.commit()
        items = [{"kind": "audio", "id": c.id, "extra": 1} for c in clips] + [{"kind": "audio", "id": 999999}]
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)

    def wait(side, rid):
        for _ in range(200):
            v = side.get(f"/api/media-rename/runs/{rid}").json()
            if v["status"] not in ("pending", "running"):
                return v
            time.sleep(0.05)
        raise AssertionError(v)

    names = [_reply(json.dumps({"names": [{"id": str(n), "name": f"Song {n}"} for n in range(1, 7)]})) for _ in range(3)]
    out = []
    for side in (gm, _via(gm, base)):
        studio.requests.clear()
        studio.reply_seq = list(names)
        r = side.post("/api/media-rename/runs", json={"items": items, "preset": "music", "style": 5, "pictures": 0, "listen": "yes"})
        assert r.status_code == 200, r.text
        v = wait(side, r.json()["id"])
        d = SessionLocal()
        try:
            row = d.get(MediaRenameRun, r.json()["id"])
            stored = (row.options_json, row.items_json, row.total, row.done)
        finally:
            d.close()
        out.append(({k: v[k] for k in ("status", "done", "total", "error", "proposals", "errors_count", "results", "errors")}, stored,
                    [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by")))
    assert out[0][:3] == out[1][:3], (out[0][:2], out[1][:2])
    assert out[1][3] == "gateway"
    # a cancel of the gateway's own run
    studio.reply_seq = None
    studio.reply = names[0]
    studio.delay = 1.5
    try:
        via = _via(gm, base)
        rid = via.post("/api/media-rename/runs", json={"items": items}).json()["id"]
        time.sleep(0.4)
        r = via.post(f"/api/media-rename/runs/{rid}/cancel")
        assert (r.status_code, r.json(), r.headers.get("x-nd-served-by")) == (200, {"ok": True}, "gateway")
        v = wait(via, rid)
        assert (v["status"], v["error"]) == ("cancelled", "Cancelled.")
        assert via.post(f"/api/media-rename/runs/{rid}/cancel").status_code == 400     # no longer running anywhere: Python's answer
    finally:
        studio.delay = 0
        studio.reply = {}


def _poll(side, url, done=lambda v: v.get("status") != "running"):
    import time
    for _ in range(300):
        r = side.get(url)
        if r.status_code != 200 or done(r.json()):
            return r
        time.sleep(0.05)
    raise AssertionError(r.text)


def test_map_marker_drafts_are_pythons(client, seed, studio):
    """/api/maps/<slug>/ai-markers/start and its poll: the map read, the existing labels, the world lore (smart_world_context: the matched
    entities and their excerpts, a top-up, the latest notes, the rules sections, the high-priority entities, the party), the reply cleaned
    into markers - a job the gateway runs, polled from the gateway, listed on the Background Jobs page."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, MapOverlay, PlayerCharacter, World
    base = str(client.base_url)
    w = seed.world_a.id
    maps = Path(os.environ["DB_PATH"]).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    slug = f"harbor-{w}"
    (maps / f"{slug}.json").write_text(json.dumps({"world_id": w, "name": "Harbor District", "width": 1600, "height": 900,
                                                   "markers": [{"name": "Old Lighthouse"}, {"name": ""}, "x"]}))
    db = SessionLocal()
    try:
        db.add(MapOverlay(slug=slug, custom_markers_json=json.dumps([{"label": "Smuggler Dock"}, {"label": ""}])))
        body = "# Harbor\n\nIntro.\n\n## Smuggler tunnels\n\n" + ("The smugglers move crates at night. " * 60) + "\n\n## Taxes\n\nHigh."
        ship = Entity(world_id=w, kind="location", name="Harbor Smuggler Tunnels", summary="Tunnels under the harbor", body=body)
        guide = Entity(world_id=w, kind="note", name="Harbor guide", summary="", body="## Harbor lights\n\nLanterns line the harbor." * 3, rag_priority=True)
        other = Entity(world_id=w, kind="character", name="Mira", summary="A fence")
        db.add_all([ship, guide, other])
        db.flush()
        db.add(EntityNote(entity_id=ship.id, content="The tunnel key is with Mira.", visible_to_players=False))
        db.add(PlayerCharacter(world_id=w, name="Ana", race="Elf", char_class="Rogue", level=2))
        db.get(World, w).rules_md = "# Rules\n\n## Smuggling\n\nA smuggler at the harbor rolls Stealth.\n\n## Combat\n\nRoll d20."
        db.commit()
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    reply = _reply("Here you go: " + json.dumps({"markers": [
        {"label": "Old Lighthouse", "lat": 1, "lng": 1}, {"label": " Rope Walk ", "note": "Ropes " * 80, "lat": 2000, "lng": -5, "color": "#00ff00"},
        {"label": "smuggler dock", "lat": 3, "lng": 3}, {"label": "Fish Market", "lat": "450.555", "lng": 12.345, "color": "red"},
        {"label": "Bad", "lat": "north", "lng": 1}, {"label": "Ana's party crossing", "lat": None, "lng": 0}, "junk"]}) + " Enjoy!")
    cases = [
        ({"prompt": "Where do the smugglers hide in the harbor? Ask the party", "count": 3, "think": False}, [reply]),
        ({"prompt": "harbor lights", "use_rag": False, "count": "x"}, [_reply("no markers")]),
    ]
    for i, (body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post(f"/api/maps/{slug}/ai-markers/start", json=body)
            assert r.status_code == 200, r.text
            job = r.json()["job_id"]
            p = _poll(side, f"/api/maps/{slug}/ai-markers/{job}")
            out.append((p.status_code, p.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by"), p.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2], json.dumps(out[0][2])[:6000], json.dumps(out[1][2])[:6000])
        assert out[1][3:] == ("gateway", "gateway"), i
    jobs = _via(gm, base).get("/api/live-jobs").json()["jobs"]
    assert any(j["key"].startswith("m:gw") and j["label"] == "map markers" and j["status"] in ("done", "error") for j in jobs), jobs
    for side in (gm, _via(gm, base)):
        r = side.post(f"/api/maps/{slug}/ai-markers/start", json={"prompt": "  "})
        assert (r.status_code, r.json()) == (400, {"detail": "Describe what to place first"})
    studio.reply_seq = None


def test_cockpit_find_is_pythons(client, seed, studio):
    """/api/cockpit/ai/find/start and its poll: the focus (free text or an entity), the lore, the candidate board, the results checked
    against the real candidates - and the refusals."""
    from app.database import SessionLocal
    from app.models import Entity
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        a = Entity(world_id=w, kind="character", name="Vex the Fence", summary="Buys stolen relics " * 10, body="Lives by the docks.")
        b = Entity(world_id=w, kind="location", name="Dockside", summary="")
        db.add_all([a, b])
        db.commit()
        ids = (a.id, b.id)
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    reply = _reply("```json\n" + json.dumps({"results": [{"id": ids[1], "reason": "Docks " * 80}, {"id": str(ids[1])}, {"id": 999999}, {"id": "x"}, "junk"]}) + "\n```")
    cases = [({"query": "who buys relics at the docks"}, [reply]), ({"entity_id": ids[0], "query": "allies"}, [reply]), ({"entity_id": ids[0]}, [_reply("none")])]
    for i, (body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/api/cockpit/ai/find/start", json=body)
            assert r.status_code == 200, r.text
            p = _poll(side, f"/api/cockpit/ai/find/{r.json()['job_id']}")
            out.append((p.status_code, p.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by"), p.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2])
        assert out[1][3:] == ("gateway", "gateway"), i
    for side in (gm, _via(gm, base)):
        r = side.post("/api/cockpit/ai/find/start", json={"query": " "})
        assert (r.status_code, r.json()["detail"]) == (400, "Type what to look for first — or seed me from an entity card.")
    studio.reply_seq = None


def test_quest_sync_is_pythons(client, seed, studio):
    """/api/quests/suggest/start and its poll: the session material (summary, transcript tail, facts), the quest board, the lore, the
    suggestions filtered to real quests - and the refusals."""
    from app.database import SessionLocal
    from app.models import Fact, GameSession, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        q1 = Quest(world_id=w, title="Find the relic", status="active", category="main", summary="It is lost " * 30)
        gs = GameSession(world_id=w, session_num=7, title="Ruins", live_transcript="GM: You find the relic. " * 400)
        db.add_all([q1, gs])
        db.flush()
        db.add(Fact(world_id=w, game_session_id=gs.id, content="The relic was found."))
        bare = GameSession(world_id=w, session_num=8, title="Empty")
        db.add(bare)
        db.commit()
        qid, sid, bare_id = q1.id, gs.id, bare.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    reply = _reply(json.dumps({"new_quests": [{"title": " The Black Gate ", "summary": "x" * 600, "category": "weird"}, {"title": ""}, "junk"],
                               "quest_updates": [{"quest_id": qid, "status": "complete", "note": " Found "}, {"quest_id": 999999}, {"quest_id": str(qid)},
                                                 {"quest_id": qid, "status": "odd"}]}))
    cases = [({"session_id": sid, "think": False}, [reply]), ({"text": "They burned the bridge.", "use_rag": False}, [_reply("```\n" + json.dumps({"new_quests": "abc"}) + "\n```")]),
             ({"session_id": bare_id}, [_reply("no json")])]
    for i, (body, replies) in enumerate(cases):
        out = []
        for side in (gm, _via(gm, base)):
            studio.requests.clear()
            studio.reply_seq = list(replies)
            r = side.post("/api/quests/suggest/start", json=body)
            assert r.status_code == 200, r.text
            p = _poll(side, f"/api/quests/suggest/{r.json()['job_id']}")
            out.append((p.status_code, p.json(), [q["body"] for q in studio.requests], r.headers.get("x-nd-served-by"), p.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (i, out[0][:2], out[1][:2])
        assert out[1][3:] == ("gateway", "gateway"), i
    for side in (gm, _via(gm, base)):
        r = side.post("/api/quests/suggest/start", json={})
        assert (r.status_code, r.json()["detail"]) == (400, "Provide session_id or text")
    studio.reply_seq = None
