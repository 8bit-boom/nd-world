"""The MCP server through the gateway (rust/nd-gateway/src/mcp.rs) against Python's (app/mcp_server.py): the same bytes back for the
protocol's own messages and for every tool the gateway answers, for a GM's token and a player's - and what it leaves to Python (an
unknown tool, arguments the SDK would coerce, a batch, a missing or bad token) still answered the same way."""
import hashlib
import json

import httpx
import pytest

from .test_gateway_golden import DIRECT
from .test_gateway_ai import studio  # noqa: F401  (the fake Unsloth Studio fixture)

pytestmark = pytest.mark.skipif(not DIRECT, reason="needs ND_TEST_BASE_URL (the gateway) and ND_GOLDEN_DIRECT_URL (Python directly)")

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _token(user_id, raw):
    from app.database import SessionLocal
    from app.models import ApiToken
    db = SessionLocal()
    try:
        h = hashlib.sha256(raw.encode()).hexdigest()
        if not db.query(ApiToken).filter(ApiToken.token_hash == h).first():
            db.add(ApiToken(user_id=user_id, token_hash=h, label="mcp parity"))
            db.commit()
    finally:
        db.close()
    return raw


def _both(base, token, body, headers=None):
    """(status, content type, body, served by) from Python and from the gateway"""
    out = []
    for url in (DIRECT, base):
        h = dict(HEADERS if headers is None else headers)
        if token:
            h["Authorization"] = f"Bearer {token}"
        r = httpx.post(url + "/mcp", content=body if isinstance(body, (str, bytes)) else json.dumps(body), headers=h, timeout=60)
        out.append((r.status_code, r.headers.get("content-type"), r.text, r.headers.get("x-nd-served-by")))
    return out


@pytest.fixture
def mcp_world(seed):
    from app.database import SessionLocal
    from app.models import Entity, Fact, GameSession, Quest, RandomTable, World, entity_player_access
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.rules_md = "# Rules\n\nRoll 2d6.\n\n:::gm Secret\nThe dragon is fake.\n:::\n\n:::note Tip\nBring snacks — always.\n:::\n"
        w.section_access_json = json.dumps({"kind_item": {"player": "none", "assistant": "read"}})
        s1 = GameSession(world_id=w.id, session_num=1, title="Into the Dark", session_date="2026-01-02", player_summary="Recap ✓", player_summary_published=True)
        s2 = GameSession(world_id=w.id, session_num=2, title="Ünder", session_date=None, player_summary="  ", player_summary_published=True)
        db.add_all([s1, s2])
        db.commit()
        db.add_all([Fact(world_id=w.id, content="The bridge fell.", visible_to_players=True, game_session_id=s1.id),
                    Fact(world_id=w.id, content="Vane did it — secretly.", visible_to_players=False),
                    Quest(world_id=w.id, title="Find Vane", status="active", category="main", summary="Who?"),
                    Quest(world_id=w.id, title="Hidden quest", status="secret", visible_to_players=False),
                    Quest(world_id=w.id, title="Done deal", status="complete", category="side")])
        bridge = Entity(world_id=w.id, kind="location", name="Old Bridge", summary="A broken bridge", body="Stone.", tags="ruin", visible_to_players=True)
        sword = Entity(world_id=w.id, kind="item", name="Bridge Sword", summary="Sharp", visible_to_players=True)
        hidden = Entity(world_id=w.id, kind="location", name="Bridge Vault", summary="hidden", visible_to_players=False)
        granted = Entity(world_id=w.id, kind="location", name="Bridge Den", summary="granted", visible_to_players=False)
        db.add_all([bridge, sword, hidden, granted])
        db.add_all([RandomTable(world_id=w.id, name="Weather", slug=f"mcp-weather-{w.id}", entries_json=json.dumps([{"label": "Rain"}, {"label": "Sun"}])),
                    RandomTable(world_id=w.id, name="Odd", slug=f"mcp-odd-{w.id}", entries_json='{"a": 1, "b": 2}', category="misc", description="dict")])
        db.commit()
        db.execute(entity_player_access.insert().values(entity_id=granted.id, user_id=seed.player_a.id))
        db.commit()
        yield {"w": w.id, "s1": s1.id, "bridge": bridge.id, "sword": sword.id, "hidden": hidden.id, "granted": granted.id}
    finally:
        db.close()


def test_mcp_protocol_and_read_tools_are_pythons(client, seed, mcp_world):
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")
    player = _token(seed.player_a.id, "mcp-parity-player")
    m, w = mcp_world, mcp_world["w"]

    def rpc(method, params=None, id_=1):
        msg = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            msg["params"] = params
        return msg

    def tool(name, **args):
        return rpc("tools/call", {"name": name, "arguments": args})

    served = [
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}),
        rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {"sampling": {}}, "clientInfo": {"name": "x", "version": "1"}}, id_="abc"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("ping"), rpc("tools/list", id_=99),
        tool("list_worlds"), tool("list_facts", world_id=w), tool("list_facts", world_id=w, game_session_id=m["s1"]), tool("list_facts", world_id=999999),
        tool("list_quests", world_id=w), tool("list_quests", world_id=w, status="complete"), tool("list_quests", world_id=w, status=""),
        tool("search_entities", world_id=w, query="bridge"), tool("search_entities", world_id=w, query="bridge", kind="item"),
        tool("search_entities", world_id=w, query="", kind="location"),
        tool("get_entity", entity_id=m["bridge"]), tool("get_entity", entity_id=m["hidden"]), tool("get_entity", entity_id=m["granted"]),
        tool("get_entity", entity_id=m["sword"]), tool("get_entity", entity_id=999999),
        tool("get_rules", world_id=w), tool("list_sessions", world_id=w), tool("list_tables", world_id=w),
    ]
    for token in (gm, player):
        for body in served:
            a, b = _both(base, token, body)
            assert a[:3] == b[:3], (token, body, a[2][:600], b[2][:600])
            assert b[3] == "gateway", (token, body)
    # left to Python - and still the same answer
    python_s = [rpc("tools/call", {"name": "nope", "arguments": {}}), tool("list_facts", world_id="7"), tool("list_facts", world_id=True),
                tool("list_quests", world_id=w, status="null"), [rpc("ping")], rpc("bogus"), "notjson",
                rpc("tools/list", {"cursor": "x"})]
    for body in python_s:
        a, b = _both(base, gm, body)
        assert a[:3] == b[:3], (body, a[2][:600], b[2][:600])
        assert b[3] != "gateway", body
    for token, headers in ((None, None), ("not-a-token", None), (gm, {"Content-Type": "application/json"}), (gm, {**HEADERS, "mcp-protocol-version": "1990-01-01"})):
        a, b = _both(base, token, rpc("ping"), headers)
        assert a[:3] == b[:3], (token, headers, a, b)
        assert b[3] != "gateway"
    # the world's rules empty: the note
    from app.database import SessionLocal
    from app.models import ApiToken, World
    db = SessionLocal()
    try:
        db.get(World, w).rules_md = "  "
        db.commit()
        assert db.query(ApiToken).filter(ApiToken.label == "mcp parity").first().last_used_at is not None
    finally:
        db.close()
    a, b = _both(base, player, tool("get_rules", world_id=w))
    assert a[:3] == b[:3] and b[3] == "gateway"


def test_mcp_sdk_client_through_the_gateway(client, seed, mcp_world):
    """The real MCP client (the SDK's streamable-HTTP transport) talks to the gateway end to end."""
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")

    async def go():
        async with streamablehttp_client(base + "/mcp", headers={"Authorization": f"Bearer {gm}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                facts = await s.call_tool("list_facts", {"world_id": mcp_world["w"]})
                return names, facts
    names, facts = asyncio.run(go())
    assert "list_facts" in names and "ask_chronicler" in names
    assert not facts.isError
    assert {f["content"] for f in facts.structuredContent["result"]} >= {"The bridge fell.", "Vane did it — secretly."}


def _call(url, token, name, args, id_=1):
    r = httpx.post(url + "/mcp", headers={**HEADERS, "Authorization": f"Bearer {token}"}, timeout=60,
                   content=json.dumps({"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name, "arguments": args}}))
    return r.status_code, r.text, r.headers.get("x-nd-served-by")


def _row(table, id_, drop=("id", "created_at", "updated_at")):
    from sqlalchemy import text
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT * FROM {table} WHERE id = :i"), {"i": id_}).mappings().first()
        return None if r is None else {k: v for k, v in r.items() if k not in drop}
    finally:
        db.close()


def test_mcp_write_tools_are_pythons(client, seed, mcp_world):
    """create / update / delete for facts, entities, notes and quests, get_session and roll_table: each side works on its own copy of the
    same rows, and gives the same answer (ids aside) and leaves the same row behind."""
    from app.database import SessionLocal
    from app.models import AudioClip, Entity, EntityNote, EntityRelation, Fact, GameSession, Quest, RandomTable, World
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")
    player = _token(seed.player_a.id, "mcp-parity-player")
    w = mcp_world["w"]
    db = SessionLocal()
    try:
        db.get(World, w).custom_kinds_json = json.dumps([{"id": "custom_ship", "label": "Ship"}, {"id": "custom_ship", "label": "Ship again"}, {"id": "bad", "label": "x"}])
        a1 = AudioClip(world_id=w, name="Ballad", file_url="/uploads/audio/a.mp3", visible_to_players=True, transcript="La la")
        a2 = AudioClip(world_id=w, name="Secret", file_url="/uploads/audio/b.mp3", visible_to_players=False, description="GM")
        db.add_all([a1, a2])
        db.commit()
        pairs = []
        for _ in range(2):
            f = Fact(world_id=w, content="Old fact", visible_to_players=None)
            e = Entity(world_id=w, kind="location", name="Keep", summary="s", body="b", tags="t", visible_to_players=True)
            noted = Entity(world_id=w, kind="location", name="Noted", visible_to_players=True)
            q = Quest(world_id=w, title="Q", status="active", category="main", summary="", visible_to_players=True)
            gs = GameSession(world_id=w, session_num=7, title="Seven", player_summary="Recap", player_summary_published=True,
                             summary="GM notes", recap_audio_json=json.dumps([a2.id, a1.id, a1.id, True, 999999]))
            t = RandomTable(world_id=w, name="Loot", slug=f"mcp-loot-{_}-{w}", entries_json=json.dumps([{"label": "Gold", "weight": 0}, {"label": "Gem", "weight": 3}]))
            db.add_all([f, e, noted, q, gs, t])
            db.commit()
            db.add_all([EntityNote(entity_id=noted.id, content="n"), EntityRelation(world_id=w, source_id=e.id, target_id=noted.id, relation="near")])
            db.commit()
            pairs.append({"fact": f.id, "entity": e.id, "noted": noted.id, "quest": q.id, "session": gs.id, "table": t.id})
    finally:
        db.close()

    def same(name, args_for, token=gm, table=None, key=None, python=False):
        outs = []
        for i, url in enumerate((DIRECT, base)):
            args = args_for(pairs[i])
            status, body, by = _call(url, token, name, args)
            ids = set(pairs[i].values())
            result = json.loads(body.split("data: ", 1)[1])["result"]
            created = None
            if result["isError"]:
                text = result["content"][0]["text"]
                for v in sorted(ids, reverse=True):
                    text = __import__("re").sub(rf"\b{v}\b", "<id>", text)
                shown = text
            else:
                shown = json.loads(result["content"][0]["text"])
                if table:
                    created = _row(table, shown["id"]) if key is None else _row(table, pairs[i][key])
                    if key is None:
                        shown["id"] = "<new>"
                for k in ("id", "deleted"):
                    if shown.get(k) in ids:
                        shown[k] = "<id>"
            body = (result["isError"], shown)
            if created:
                created = {k: ("<id>" if isinstance(v, int) and k.endswith("_id") and v in ids else v) for k, v in created.items()}
            outs.append((status, body, created, by))
        a, b = outs
        if python:                                       # Python's answer names a timestamp: both refused, by Python
            assert a[1][0] and b[1][0] and b[3] != "gateway", (name, a, b)
            return
        assert a[:3] == b[:3], (name, a[:3], b[:3])
        assert b[3] == "gateway", (name, b[3])

    same("create_fact", lambda p: {"world_id": w, "content": "  A new fact ", "visible_to_players": False, "game_session_id": p["session"]}, table="facts")
    same("create_fact", lambda p: {"world_id": w, "content": "  "})
    same("create_fact", lambda p: {"world_id": w, "content": "x"}, token=player)
    same("create_fact", lambda p: {"world_id": 999999, "content": "x"})
    same("update_fact", lambda p: {"fact_id": p["fact"], "content": " Edited "}, table="facts", key="fact")
    same("update_fact", lambda p: {"fact_id": p["fact"], "content": "  ", "visible_to_players": True}, table="facts", key="fact")
    same("update_fact", lambda p: {"fact_id": 999999})
    same("create_entity", lambda p: {"world_id": w, "kind": "custom_ship", "name": " The Gull ", "summary": " fast ", "tags": " sea ", "visible_to_players": False}, table="entities")
    same("create_entity", lambda p: {"world_id": w, "kind": "dragon", "name": "x"})
    same("create_entity", lambda p: {"world_id": w, "kind": "item", "name": " "})
    same("create_note", lambda p: {"world_id": w, "name": "Lore", "body": "  " + "word " * 40}, table="entities")
    same("create_note", lambda p: {"world_id": w, "name": "Lore 2", "body": "b", "summary": " Sum "}, table="entities")
    same("update_entity", lambda p: {"entity_id": p["entity"], "name": " ", "summary": " New ", "body": "B2", "folder": " Places ", "visible_to_players": False}, table="entities", key="entity")
    same("update_entity", lambda p: {"entity_id": p["entity"], "summary": "New"}, table="entities", key="entity")
    same("update_entity", lambda p: {"entity_id": 999999, "summary": "x"})
    same("create_quest", lambda p: {"world_id": w, "title": " Rescue ", "status": "  ", "category": "side", "body": "Go."}, table="quests")
    same("update_quest", lambda p: {"quest_id": p["quest"], "title": "Q2", "status": " done ", "category": " ", "visible_to_players": False}, table="quests", key="quest")
    same("update_quest", lambda p: {"quest_id": p["quest"]}, table="quests", key="quest")
    same("get_session", lambda p: {"session_id": p["session"]})
    same("get_session", lambda p: {"session_id": p["session"]}, token=player)
    same("get_session", lambda p: {"session_id": 999999})
    same("delete_entity", lambda p: {"entity_id": p["noted"]}, python=True)          # it has a note: the ORM's own handling
    same("delete_entity", lambda p: {"entity_id": p["entity"]})
    same("delete_entity", lambda p: {"entity_id": p["entity"]})
    same("delete_fact", lambda p: {"fact_id": p["fact"]})
    same("delete_fact", lambda p: {"fact_id": p["fact"]})
    same("roll_table", lambda p: {"table_id": 999999})
    same("roll_table", lambda p: {"table_id": p["table"]}, token=player)
    for p in pairs:
        assert _row("entities", p["entity"]) is None and _row("entity_relations", 0) is None
    status, body, by = _call(base, gm, "roll_table", {"table_id": pairs[1]["table"], "times": 25})
    res = json.loads(json.loads(body.split("data: ", 1)[1])["result"]["content"][0]["text"])
    assert by == "gateway" and res["table"] == "Loot" and len(res["results"]) == 10 and set(res["results"]) <= {"Gold", "Gem"}


def _party_scene(seed):
    """Anders (Hunt in the Moonlight, the player's), Vex (native N&D, another player's), Zeus (Asterion, nobody's) in one party, a second
    party with only Zeus, and a custom template character with a deleted template."""
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, SheetTemplate, User, World, WorldMembership
    from app import auth as auth_module
    from .conftest import PLAYER_PASSWORD
    db = SessionLocal()
    try:
        w = seed.world_a.id
        p2 = db.query(User).filter(User.email == "mcp-p2@test.local").first()
        if not p2:
            p2 = User(email="mcp-p2@test.local", password_hash=auth_module.hash_password(PLAYER_PASSWORD), display_name="P2", is_gm=False)
            db.add(p2)
            db.commit()
            db.add(WorldMembership(world_id=w, user_id=p2.id))
        tpl = {t.slug: t.id for t in db.query(SheetTemplate).filter(SheetTemplate.is_builtin.is_(True)).all()}
        anders = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Anders", player_name="Archie", max_hp=0, sheet_template_id=tpl["hunt-in-the-moonlight"],
                                 conditions_json=json.dumps(["Stunned", "Prone", "Marked", "Shaken", "Bleeding", 3]),
                                 custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 2, "stamina_max": 5, "strain": "2", "xpCurrent": 4, "xpLifetime": 10}))
        vex = PlayerCharacter(world_id=w, owner_user_id=p2.id, name="Vex", max_hp=20, current_hp=9, shock_current=2, pp_current=1, mp_current=None, xp=5,
                              stats_json=json.dumps([{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]),
                              backstory="Public. [gmonly]Secret.[/gmonly]")
        zeus = PlayerCharacter(world_id=w, name="Zeus", max_hp=0, sheet_template_id=tpl["asterion"], custom_fields_json=json.dumps({"flesh_current": 0, "flesh_max": 5, "glory": 3}))
        orphan = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Orphan", max_hp=10, current_hp=10, sheet_template_id=987654)
        db.add_all([anders, vex, zeus, orphan])
        db.commit()
        crew = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([anders.id, vex.id, zeus.id, 999999, "x"]), goals="Win. [gmonly]Betray.[/gmonly]",
                     loot_json=json.dumps([{"lid": "aaaaaaaa", "name": "Rope", "qty": 2, "notes": "long", "claimed_by": [anders.id, 424242]}]))
        gods = Party(world_id=w, name="Gods", member_pc_ids_json=json.dumps([zeus.id]))
        db.add_all([crew, gods])
        db.commit()
        return {"w": w, "anders": anders.id, "vex": vex.id, "zeus": zeus.id, "orphan": orphan.id, "crew": crew.id, "gods": gods.id, "p2": p2.id}
    finally:
        db.close()


@pytest.fixture
def mcp_party(seed):
    return _party_scene(seed)


def test_mcp_character_and_party_reads_are_pythons(client, seed, mcp_party):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")
    player = _token(seed.player_a.id, "mcp-parity-player")
    m = mcp_party

    def check(token, name, args):
        a = _call(DIRECT, token, name, args)
        b = _call(base, token, name, args)
        assert a[:2] == b[:2], (name, args, a[1][:800], b[1][:800])
        assert b[2] == "gateway", (name, args)

    for see in (True, False):
        db = SessionLocal()
        try:
            db.get(World, m["w"]).players_see_party = see
            db.commit()
        finally:
            db.close()
        for token in (gm, player):
            check(token, "list_characters", {"world_id": m["w"]})
            for c in ("anders", "vex", "zeus", "orphan"):
                check(token, "get_character", {"character_id": m[c]})
            check(token, "get_character", {"character_id": 999999})
            check(token, "list_parties", {"world_id": m["w"]})
            check(token, "get_party", {"party_id": m["crew"]})
            check(token, "get_party", {"party_id": m["gods"]})
            check(token, "get_party", {"party_id": 999999})


def test_mcp_character_writes_are_pythons(client, seed):
    """adjust_character_resource, set_character_conditions, award_character_xp and rest_party: each side changes its own copy of the
    same characters and party, with the same answer and the same rows left behind."""
    from app.database import SessionLocal
    from app.models import World
    from app import char_extras
    from app.models import PlayerCharacter
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")
    player = _token(seed.player_a.id, "mcp-parity-player")
    scenes = [_party_scene(seed), _party_scene(seed)]
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).players_see_party = True
        for sc in scenes:
            char_extras.set_prefs(db, db.get(PlayerCharacter, sc["vex"]), feat_used={"a": "rest", "b": "session"})
        db.commit()
    finally:
        db.close()
    keys = ("anders", "vex", "zeus", "orphan", "crew", "gods")

    def norm(body, sc):
        names = {sc[k]: f"<{k}>" for k in ("anders", "vex", "zeus", "orphan")}
        parties = {sc[k]: f"<{k}>" for k in ("crew", "gods")}
        result = json.loads(body.split("data: ", 1)[1])["result"]
        text = result["content"][0]["text"]
        if result["isError"]:
            return True, __import__("re").sub(r"(Character|Party) (\d+)", lambda m: f"{m[1]} {(names if m[1] == 'Character' else parties).get(int(m[2]), m[2])}", text)

        def ids(v):
            if isinstance(v, dict):
                return {k: (names.get(x, x) if k == "id" else ids(x)) for k, x in v.items()}
            return [ids(x) for x in v] if isinstance(v, list) else v
        return False, ids(json.loads(text))

    def pcs(sc):
        from sqlalchemy import text
        db = SessionLocal()
        try:
            out = {}
            for k in ("anders", "vex", "zeus", "orphan"):
                r = dict(db.execute(text("SELECT current_hp, shock_current, pp_current, mp_current, xp, conditions_json, custom_fields_json FROM player_characters WHERE id = :i"),
                                    {"i": sc[k]}).mappings().first())
                r["prefs"] = char_extras.prefs(db, sc[k])
                out[k] = r
            return out
        finally:
            db.close()

    def same(token, name, args_for):
        res = []
        for sc, url in zip(scenes, (DIRECT, base)):
            st, body, by = _call(url, token, name, args_for(sc))
            res.append((st, norm(body, sc), pcs(sc), by))
        a, b = res
        assert a[:3] == b[:3], (name, a[1], b[1], a[2], b[2])
        assert b[3] == "gateway", name

    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": " HP ", "delta": -4})
    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": "mp", "value": 99})
    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": "luck", "value": 1})
    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": "hp"})
    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": "hp", "delta": 1, "value": 2})
    same(player, "adjust_character_resource", lambda sc: {"character_id": sc["anders"], "resource": "stamina", "delta": -5})
    same(player, "adjust_character_resource", lambda sc: {"character_id": sc["anders"], "resource": "HEALTH", "value": 50})
    same(player, "adjust_character_resource", lambda sc: {"character_id": sc["anders"], "resource": "nope", "delta": 1})
    same(player, "adjust_character_resource", lambda sc: {"character_id": sc["vex"], "resource": "hp", "delta": 1})
    same(gm, "adjust_character_resource", lambda sc: {"character_id": sc["zeus"], "resource": "flesh", "delta": 2})
    same(gm, "set_character_conditions", lambda sc: {"character_id": sc["anders"], "conditions": ["  stunned ", "Stunned", "Prone", ""]})
    same(player, "set_character_conditions", lambda sc: {"character_id": sc["anders"], "conditions": []})
    same(player, "set_character_conditions", lambda sc: {"character_id": sc["zeus"], "conditions": ["x"]})
    same(gm, "award_character_xp", lambda sc: {"character_id": sc["anders"], "amount": 3})
    same(gm, "award_character_xp", lambda sc: {"character_id": sc["zeus"], "amount": -10})
    same(gm, "award_character_xp", lambda sc: {"character_id": sc["vex"], "amount": -100})
    same(gm, "award_character_xp", lambda sc: {"character_id": sc["vex"], "amount": 100001})
    same(player, "award_character_xp", lambda sc: {"character_id": sc["anders"], "amount": 1})
    same(gm, "rest_party", lambda sc: {"party_id": sc["crew"]})
    same(gm, "rest_party", lambda sc: {"party_id": sc["crew"], "kind": "short"})
    same(gm, "rest_party", lambda sc: {"party_id": sc["gods"], "kind": "nap"})
    same(player, "rest_party", lambda sc: {"party_id": sc["crew"]})
    same(gm, "rest_party", lambda sc: {"party_id": 999999})


def test_mcp_templates_and_chronicler_are_pythons(client, seed, mcp_world, studio):
    """The sheet-template tools (list / get / create / update, cleaned by template_draft) and ask_chronicler (the same prompt reaches the
    backend, the same answer comes back)."""
    from .test_gateway_ai import _reply
    from app.database import SessionLocal
    from app.models import SheetTemplate
    base = str(client.base_url)
    gm = _token(seed.gm.id, "mcp-parity-gm")
    player = _token(seed.player_a.id, "mcp-parity-player")
    w = mcp_world["w"]
    db = SessionLocal()
    try:
        own = [SheetTemplate(world_id=w, name="Moon Ç", slug=f"mcp-moon-{i}-{w}", sheet_mode="custom", description=None, rules_md=" Roll 2d6. ",
                             fields_json=json.dumps([{"id": "hp", "label": "Health (0 = down)", "type": "resource", "default_value": "5/5", "vital": "hp"},
                                                     {"id": "xp", "label": "XP", "type": "number", "xp": True}, {"id": "nm", "label": "Name", "type": "text", "binds": "name"}]),
                             system_json=json.dumps({"conditions": ["Hurt"], "rest": {"long": [["full", "hp"]]}})) for i in range(2)]
        db.add_all(own)
        db.commit()
        tids = [t.id for t in own]
        builtin = db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first().id
    finally:
        db.close()

    def body_of(text):
        r = json.loads(text.split("data: ", 1)[1])["result"]
        return r

    def same(token, name, args_for, new=False):
        res = []
        for i, url in enumerate((DIRECT, base)):
            st, text, by = _call(url, token, name, args_for(i))
            r = body_of(text)
            if not r["isError"]:
                v = json.loads(r["content"][0]["text"])
                if isinstance(v, dict) and v.get("id") in tids + [None] or new:
                    v["id"] = "<id>"
                if isinstance(v, list):
                    v = [x for x in v if x["id"] not in tids or x["id"] == tids[i]]
                    for x in v:
                        if x["id"] == tids[i]:
                            x["id"] = "<id>"
                r = v
            res.append((st, r, by))
        a, b = res
        assert a[:2] == b[:2], (name, a[1], b[1])
        assert b[2] == "gateway", name

    same(gm, "list_sheet_templates", lambda i: {"world_id": w})
    same(player, "list_sheet_templates", lambda i: {"world_id": w})
    same(gm, "get_sheet_template", lambda i: {"template_id": tids[i]})
    same(gm, "get_sheet_template", lambda i: {"template_id": builtin})
    same(gm, "get_sheet_template", lambda i: {"template_id": 999999})
    same(gm, "update_sheet_template", lambda i: {"template_id": builtin, "name": "x"})
    same(gm, "update_sheet_template", lambda i: {"template_id": tids[i], "name": "  Moon   Two ", "description": " <b>d</b> ",
                                                 "system": {"conditions": ["A", "A", 3], "rest": {"short": [["full", "nope"]]}}})
    same(gm, "update_sheet_template", lambda i: {"template_id": tids[i], "name": " "})
    same(gm, "update_sheet_template", lambda i: {"template_id": tids[i], "fields": [{"id": "Grit!", "label": "Grit", "type": "number", "xp": True}], "rules_md": "New"})
    same(gm, "update_sheet_template", lambda i: {"template_id": tids[i], "fields": [{"label": ""}]})
    same(gm, "create_sheet_template", lambda i: {"world_id": w, "name": "Star Cats", "fields": [{"id": "lives", "label": "Lives", "type": "resource", "default_value": "9/9", "vital": "hp"}],
                                                 "system": {"conditions": ["Wet"]}, "rules_md": "Cats."}, new=True)
    same(gm, "create_sheet_template", lambda i: {"world_id": w, "name": "Nothing", "fields": []}, new=True)
    for token in (gm, player):
        outs = []
        for url in (DIRECT, base):
            studio.requests.clear()
            studio.reply = _reply("The bridge fell, long ago.")
            st, text, by = _call(url, token, "ask_chronicler", {"world_id": w, "question": " What happened to the bridge? "})
            outs.append((st, text, [q["body"] for q in studio.requests], by))
        a, b = outs
        assert a[:3] == b[:3], (a, b)
        assert b[3] == "gateway"
    a = _call(DIRECT, gm, "ask_chronicler", {"world_id": w, "question": "  "})
    b = _call(base, gm, "ask_chronicler", {"world_id": w, "question": "  "})
    assert a[:2] == b[:2] and b[2] == "gateway"
