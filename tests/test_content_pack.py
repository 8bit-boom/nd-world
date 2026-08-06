"""Tests for GET /api/worlds/{world_id}/content-pack — the counterpart to
NeonDragonsEditor's export: converts a world's race/profession/feat/item
Entity rows back into the JSON shape NeonDragonsApp's bundled
assets/data/*.json files use, so the app can pull homebrew content at
runtime without an APK rebuild.
"""
import json

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world, **kwargs):
    db = SessionLocal()
    try:
        ent = Entity(world_id=world.id, **kwargs)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        return ent
    finally:
        db.close()


def test_content_pack_requires_login(client, seed):
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.status_code == 401


def test_content_pack_404s_for_inaccessible_world(client, seed):
    """Player B is only a member of World B — pulling World A's pack must
    404, not leak that the world exists."""
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.status_code == 404


def test_content_pack_accessible_to_member_player(client, seed):
    """Players, not just the GM, are the intended audience — this is what
    lets the Android app pull content without a GM account."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.status_code == 200


def test_content_pack_gm_can_access_any_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_b.id}/content-pack")
    assert r.status_code == 200


def test_content_pack_empty_world_returns_empty_buckets(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.status_code == 200
    body = r.json()
    assert body == {"races": [], "professions": [], "feats": [], "items": []}


def test_content_pack_excludes_non_content_kinds(client, seed):
    _make_entity(seed.world_a, kind="character", name="Some NPC")
    _make_entity(seed.world_a, kind="location", name="Some Place")
    _make_entity(seed.world_a, kind="note", name="Some Note")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    body = r.json()
    all_names = [i["name"] for bucket in body.values() for i in bucket]
    assert all_names == []


def test_content_pack_race_conversion(client, seed):
    ent = _make_entity(
        seed.world_a, kind="race", subtype="advanced", name="Homebrew Race",
        tags="custom", summary="A player-made race.",
        body="## Attributes\n\nSome flavor text.\n\n## Edges\n\nSome edges.",
        image_url="/uploads/race.avif",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    body = r.json()
    assert len(body["races"]) == 1
    race = body["races"][0]
    assert race["id"] == f"custom_{seed.world_a.id}_{ent.id}"
    assert race["name"] == "Homebrew Race"
    assert race["tier"] == "advanced"
    assert race["tags"] == "custom"
    assert race["description"] == "A player-made race."
    assert race["sections"] == {"Attributes": "Some flavor text.", "Edges": "Some edges."}
    assert race["image_url"] == "/uploads/race.avif"
    assert race["isCustom"] is True


def test_content_pack_race_unknown_tier_defaults_standard(client, seed):
    _make_entity(seed.world_a, kind="race", subtype=None, name="No Tier Race")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.json()["races"][0]["tier"] == "standard"


def test_content_pack_profession_conversion(client, seed):
    _make_entity(seed.world_a, kind="profession", subtype="exceptional", name="Homebrew Profession")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    prof = r.json()["professions"][0]
    assert prof["name"] == "Homebrew Profession"
    assert prof["tier"] == "exceptional"


def test_content_pack_feat_conversion_race_feat_folder(client, seed):
    _make_entity(
        seed.world_a, kind="feat", subtype="race feat", name="Blade Focus",
        folder="Race Feats/Human/Rank 2", summary="Sharp.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    feat = r.json()["feats"][0]
    assert feat["category"] == "Race"
    assert feat["associatedRace"] == "Human"
    assert feat["rank"] == "Rank 2"
    assert "associatedProfession" not in feat


def test_content_pack_feat_conversion_profession_feat_folder(client, seed):
    _make_entity(
        seed.world_a, kind="feat", subtype="profession feat", name="Quick Hack",
        folder="Profession Feats/Hacker",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    feat = r.json()["feats"][0]
    assert feat["category"] == "Profession"
    assert feat["associatedProfession"] == "Hacker"
    assert "associatedRace" not in feat


def test_content_pack_feat_unknown_subtype_defaults_common(client, seed):
    _make_entity(seed.world_a, kind="feat", subtype="something odd", name="Weird Feat")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.json()["feats"][0]["category"] == "Common"


def test_content_pack_item_conversion(client, seed):
    _make_entity(seed.world_a, kind="item", subtype="weapon", name="Monoblade")
    _make_entity(seed.world_a, kind="item", subtype="unknown-subtype", name="Mystery Gadget")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    items = {i["name"]: i for i in r.json()["items"]}
    assert items["Monoblade"]["category"] == "Weapon"
    assert items["Mystery Gadget"]["category"] == "Special"


def test_content_pack_bonuses_pass_through_from_custom_fields(client, seed):
    _make_entity(
        seed.world_a, kind="feat", subtype="race feat", name="Stat Booster",
        custom_fields_json=json.dumps({"bonuses": {"stat_str": "1"}}),
    )
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.json()["feats"][0]["bonuses"] == {"stat_str": "1"}


def test_content_pack_bonuses_defaults_to_empty_dict_not_omitted(client, seed):
    """The "bonuses" key must always be present (even {}), never omitted —
    plain Gson (no Kotlin adapter) leaves a missing key null at runtime
    despite the Kotlin field's non-nullable type and default value."""
    _make_entity(seed.world_a, kind="feat", subtype="race feat", name="No Bonuses")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    assert r.json()["feats"][0]["bonuses"] == {}


def test_content_pack_body_without_headings_becomes_description_section(client, seed):
    _make_entity(seed.world_a, kind="item", subtype="weapon", name="Plain Item", body="Just plain prose, no headings.")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    item = r.json()["items"][0]
    assert item["sections"] == {"Description": "Just plain prose, no headings."}


def test_content_pack_every_item_has_gson_safe_fields(client, seed):
    """Every field the Kotlin Race/Profession/Feat/Equipment classes declare
    must be present in the JSON (even at its empty/default value), never
    omitted — see _entity_to_pack_item's docstring for why a missing key is
    unsafe with this app's plain (non-Kotlin-adapter) Gson setup."""
    _make_entity(seed.world_a, kind="race", name="R")
    _make_entity(seed.world_a, kind="profession", name="P")
    _make_entity(seed.world_a, kind="feat", name="F")
    _make_entity(seed.world_a, kind="item", name="I")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    body = r.json()
    for bucket in body.values():
        for item in bucket:
            for key in ("id", "name", "type", "tags", "description", "sections",
                        "filepath", "specialAttributes", "bonuses", "isCustom"):
                assert key in item, f"{item.get('name')} missing {key!r}"


def test_content_pack_scoped_to_world(client, seed):
    _make_entity(seed.world_a, kind="race", name="World A Race")
    _make_entity(seed.world_b, kind="race", name="World B Race")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/worlds/{seed.world_a.id}/content-pack")
    names = [i["name"] for i in r.json()["races"]]
    assert names == ["World A Race"]
