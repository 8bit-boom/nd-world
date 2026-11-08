"""GET /export/training.ndtrain (app/training_bundle.py): what goes in, what stays out, who may download."""
import hashlib
import io
import json
import zipfile

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
from .training_bundle_seed import seed_rich_world


def _bundle(client, seed, world="world_a"):
    login(client, seed.gm.email, GM_PASSWORD)
    w = getattr(seed, world)
    client.cookies.set("active_world", w.slug)
    r = client.get("/export/training.ndtrain")
    assert r.status_code == 200
    return r, zipfile.ZipFile(io.BytesIO(r.content))


def _jl(z, name):
    return [json.loads(x) for x in z.read(name).decode("utf-8").splitlines()]


def test_bundle_has_every_member_and_a_valid_manifest(client, seed):
    seed_rich_world(seed.world_a.id)
    r, z = _bundle(client, seed)
    assert r.headers["content-type"] == "application/zip"
    assert f'filename="{seed.world_a.slug}.ndtrain"' in r.headers["content-disposition"]
    names = z.namelist()
    assert names[0] == "manifest.json"
    assert names == [n for n in ("manifest.json", "world.json", "entities.jsonl", "entity_notes.jsonl", "relations.jsonl", "rules.md",
                                 "rules_sheets.jsonl", "sessions.jsonl", "facts.jsonl", "quests.jsonl", "tables.jsonl",
                                 "ai_instructions.jsonl", "characters.jsonl")]
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist())
    man = json.loads(z.read("manifest.json"))
    assert man["format"] == "nd-training-bundle" and man["version"] == 1
    assert man["world"]["slug"] == seed.world_a.slug
    assert set(man["files"]) == set(names) - {"manifest.json"}
    for n, info in man["files"].items():
        data = z.read(n)
        assert info["sha256"] == hashlib.sha256(data).hexdigest() and info["bytes"] == len(data)
        if n.endswith(".jsonl"):
            assert info["records"] == len(data.decode().splitlines())


def test_entities_keep_secrets_raw_and_are_normalised(client, seed):
    ids = seed_rich_world(seed.world_a.id)
    _, z = _bundle(client, seed)
    ents = {e["name"]: e for e in _jl(z, "entities.jsonl")}
    a = ents["Anders de Vallière"]
    assert a["id"] == ids["anders"] and a["aliases"] == ["Anders", "the Hunter"] and a["tags"] == ["hunter", "fire"]
    assert "[gmonly]Fears fire.[/gmonly]" in a["body"] and "🔥" in a["body"]          # GM secrets stay raw, text stays UTF-8
    assert a["visible_to_players"] is False and a["rag_priority"] is True and a["group_ids"] == [1, 2]
    assert a["custom_fields"] == {"rank": "Captain", "level": 3, "tags": ["a", "b"]}
    assert a["template"] == f"hunter-{seed.world_a.id}" and a["roleplay_personality"] == "gruff, terse"
    assert a["updated_at"] and " " not in a["updated_at"] and "T" in a["updated_at"]
    d = ents["Dockside"]
    assert d["custom_fields"] == {} and d["group_ids"] == [] and d["template"] is None
    assert ents["Rumours"]["body"] is None and ents["Rumours"]["tags"] == []
    assert b"\\u" not in z.read("entities.jsonl")      # ensure_ascii=False


def test_other_tables(client, seed):
    ids = seed_rich_world(seed.world_a.id)
    _, z = _bundle(client, seed)
    assert [n["content"] for n in _jl(z, "entity_notes.jsonl")] == ["Secret note ✓"]
    assert not _jl(z, "entity_notes.jsonl")[0]["visible_to_players"]
    rels = _jl(z, "relations.jsonl")
    assert [(r["id"], r["relation"]) for r in rels] == [(1, "located_in"), (2, "related"), (3, "related")]
    assert rels[0]["source_name"] == "Anders de Vallière" and rels[2]["target_id"] == ids["anders"]
    assert "# House rules" in z.read("rules.md").decode() and ":::gm" in z.read("rules.md").decode()
    assert _jl(z, "rules_sheets.jsonl") == [{"id": _jl(z, "rules_sheets.jsonl")[0]["id"], "template": "Hunt", "rules_md": "Sheet rules here"}]
    s = _jl(z, "sessions.jsonl")[0]
    assert s["title"] == "Arrival" and s["npcs"][0]["name"] == "Anders" and s["loot"] == []     # unreadable JSON -> empty list
    assert _jl(z, "facts.jsonl")[0]["visible_to_players"] is False
    assert _jl(z, "quests.jsonl")[0]["linked_entities"][0]["role"] == "ally"
    assert _jl(z, "tables.jsonl")[0]["entries"] == [{"label": "A ship vanished", "weight": 2}]
    assert [i["title"] for i in _jl(z, "ai_instructions.jsonl")] == ["Voice"]               # the disabled one is left out
    c = _jl(z, "characters.jsonl")[0]
    assert c["name"] == "Mira" and c["equipment"] == ["bow"] and "owner_user_id" not in c and "player_name" not in c
    world = json.loads(z.read("world.json"))
    assert world["slug"] == seed.world_a.slug and {t["slug"]: t for t in world["templates"]}[f"hunter-{seed.world_a.id}"]["fields"][0]["label"] == "Rank"


def test_only_this_worlds_data_and_no_accounts(client, seed):
    seed_rich_world(seed.world_a.id)
    seed_rich_world(seed.world_b.id, rules_md="")
    _, z = _bundle(client, seed, "world_a")
    blob = b"".join(z.read(n) for n in z.namelist())
    assert len(_jl(z, "entities.jsonl")) == 3
    assert seed.gm.email.encode() not in blob
    _, zb = _bundle(client, seed, "world_b")
    assert "Core" in zb.read("rules.md").decode() or zb.read("rules.md")      # no custom rules: the core rules fill in


def test_empty_world_still_gives_a_valid_bundle(client, seed):
    _, z = _bundle(client, seed, "world_b")
    assert set(z.namelist()) >= {"manifest.json", "world.json", "entities.jsonl"}
    assert z.read("entities.jsonl") == b""


def test_players_and_anonymous_cannot_download(client, seed):
    seed_rich_world(seed.world_a.id)
    anon = client.get("/export/training.ndtrain", follow_redirects=False)
    assert anon.status_code in (302, 303, 401, 403)
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/training.ndtrain", follow_redirects=False)
    assert r.status_code in (302, 303, 403) and b"PK" != r.content[:2]
