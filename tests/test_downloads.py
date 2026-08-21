"""Tests for the "download as .md" features (app/main.py): the Rules page,
a single entity (bundling its visible notes), and a per-kind bulk zip.

All three are gated by two independent axes:
1. Visibility/existence — unchanged, reuses _entity_view_gate /
   _filter_visible_entities exactly as the pages themselves already do.
2. A new per-world toggle (World.players_can_download_rules /
   players_can_download_entities) — off by default, so a player is denied
   (403) until the GM explicitly opts in, even for content they can already
   view on-page. GM callers always succeed regardless of the toggle.
"""
import io
import json
import zipfile

from app.database import SessionLocal
from app.models import Entity, EntityNote, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "character"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _add_note(entity_id, content, visible_to_players):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content, visible_to_players=visible_to_players)
        db.add(n)
        db.commit()
    finally:
        db.close()


# ── Rules download ───────────────────────────────────────────────────────────

def test_rules_download_gm_always_allowed(client, seed):
    _set_world(seed.world_a.id, rules_md="# Custom Rules\n\nSome text.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules/download.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="world-a-rules.md"' in r.headers["content-disposition"]
    assert "# Custom Rules" in r.text


def test_rules_download_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules/download.md")
    assert r.status_code == 403


def test_rules_download_player_allowed_once_gm_enables_it(client, seed):
    _set_world(seed.world_a.id, rules_md="# Opt-In Rules", players_can_download_rules=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules/download.md")
    assert r.status_code == 200
    assert "# Opt-In Rules" in r.text


# ── Single entity download ──────────────────────────────────────────────────

def test_entity_download_gm_gets_body_and_hidden_notes(client, seed):
    eid = _add_entity(seed.world_a.id, name="Vex the Informant", kind="character",
                       summary="A nervous fixer.", body="Knows everyone in the Hollow.",
                       visible_to_players=True)
    _add_note(eid, "Secretly a corp plant.", visible_to_players=False)
    _add_note(eid, "Meets at the noodle stand.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 200
    assert 'attachment; filename="Vex the Informant.md"' in r.headers["content-disposition"]
    assert "# Vex the Informant" in r.text
    assert "A nervous fixer." in r.text
    assert "Knows everyone in the Hollow." in r.text
    assert "Secretly a corp plant." in r.text
    assert "Meets at the noodle stand." in r.text


def test_entity_download_player_denied_by_default(client, seed):
    eid = _add_entity(seed.world_a.id, name="Vex", kind="character", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 403


def test_entity_download_player_allowed_but_hidden_notes_excluded(client, seed):
    eid = _add_entity(seed.world_a.id, name="Vex", kind="character",
                       body="Public info.", visible_to_players=True)
    _add_note(eid, "GM secret note.", visible_to_players=False)
    _add_note(eid, "Player-visible note.", visible_to_players=True)
    _set_world(seed.world_a.id, players_can_download_entities=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 200
    assert "Public info." in r.text
    assert "Player-visible note." in r.text
    assert "GM secret note." not in r.text


def test_entity_download_player_still_blocked_from_hidden_entity_even_with_toggle_on(client, seed):
    """The download-permission toggle is a second, independent axis — it
    never overrides visible_to_players/_entity_view_gate."""
    eid = _add_entity(seed.world_a.id, name="Secret NPC", kind="character",
                       body="Shh.", visible_to_players=False)
    _set_world(seed.world_a.id, players_can_download_entities=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 404


def test_entity_download_filename_sanitizes_special_characters(client, seed):
    eid = _add_entity(seed.world_a.id, name='Dr. "Fixer" O\'Malley/Corp?', kind="character",
                       visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert '"' not in cd.split("filename=")[1][1:-1]  # no stray quotes/slashes inside the filename value
    assert "/" not in cd.split("filename=")[1]


def test_entity_download_cross_world_404s(client, seed):
    eid = _add_entity(seed.world_b.id, name="Other World Guy", kind="character", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)  # player_a is only a member of world_a
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 404


# ── Per-kind bulk zip ────────────────────────────────────────────────────────

def test_kind_download_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download.zip")
    assert r.status_code == 403


def test_kind_download_gm_gets_everything_player_gets_only_visible(client, seed):
    _add_entity(seed.world_a.id, name="Visible Guy", kind="character", visible_to_players=True)
    _add_entity(seed.world_a.id, name="Hidden Guy", kind="character", visible_to_players=False)
    _set_world(seed.world_a.id, players_can_download_entities=True)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any("Visible Guy" in n for n in names)
    assert any("Hidden Guy" in n for n in names)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download.zip")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any("Visible Guy" in n for n in names)
    assert not any("Hidden Guy" in n for n in names)


def test_kind_download_unique_names_for_duplicate_entity_names(client, seed):
    id1 = _add_entity(seed.world_a.id, name="Guard", kind="character", visible_to_players=True)
    id2 = _add_entity(seed.world_a.id, name="Guard", kind="character", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download.zip")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert len(names) == len(set(names))
    assert f"Guard-{id1}.md" in names
    assert f"Guard-{id2}.md" in names


def test_kind_download_empty_kind_returns_valid_empty_zip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/location/download.zip")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == []


# ── "Download Selected" (bulk-action-bar, a specific id list) ──────────────

def test_kind_download_selected_gm_gets_only_the_ids_requested(client, seed):
    id1 = _add_entity(seed.world_a.id, name="Alpha", kind="character", visible_to_players=True)
    id2 = _add_entity(seed.world_a.id, name="Beta", kind="character", visible_to_players=True)
    _add_entity(seed.world_a.id, name="Gamma", kind="character", visible_to_players=True)  # not selected
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/kind/character/download-selected.zip?id={id1}&id={id2}")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any("Alpha" in n for n in names)
    assert any("Beta" in n for n in names)
    assert not any("Gamma" in n for n in names)
    assert len(names) == 2


def test_kind_download_selected_player_denied_by_default(client, seed):
    eid = _add_entity(seed.world_a.id, name="Alpha", kind="character", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/kind/character/download-selected.zip?id={eid}")
    assert r.status_code == 403


def test_kind_download_selected_player_cannot_smuggle_a_hidden_id(client, seed):
    """The download toggle doesn't bypass visible_to_players — even if a
    player puts a GM-only entity's id directly in the query string, it's
    silently dropped rather than included in the zip."""
    visible_id = _add_entity(seed.world_a.id, name="Visible", kind="character", visible_to_players=True)
    hidden_id = _add_entity(seed.world_a.id, name="Hidden", kind="character", visible_to_players=False)
    _set_world(seed.world_a.id, players_can_download_entities=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/kind/character/download-selected.zip?id={visible_id}&id={hidden_id}")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert any("Visible" in n for n in names)
    assert not any("Hidden" in n for n in names)


def test_kind_download_selected_ignores_ids_from_another_world_or_kind(client, seed):
    other_world_id = _add_entity(seed.world_b.id, name="OtherWorld", kind="character", visible_to_players=True)
    other_kind_id = _add_entity(seed.world_a.id, name="OtherKind", kind="location", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/kind/character/download-selected.zip?id={other_world_id}&id={other_kind_id}")
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == []


def test_kind_download_selected_no_ids_returns_valid_empty_zip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download-selected.zip")
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == []


# ── Export & Backup: Rules and Notes bundle ─────────────────────────────────
# Unlike the three routes above, this one lives under /export, which is
# GM-only end-to-end via _is_player_safe (see test_player_safe.py) — so there
# is no per-world toggle here and no player-facing test cases: a player can't
# reach the route at all, and the middleware 403/redirects before the handler
# ever runs.

def test_rules_and_notes_gm_bundles_rules_and_all_notes_unfiltered(client, seed):
    _set_world(seed.world_a.id, rules_md="# Custom Rules\n\nHouse rules here.")
    eid = _add_entity(seed.world_a.id, name="Vex the Informant", kind="character",
                       visible_to_players=True)
    _add_note(eid, "Secretly a corp plant.", visible_to_players=False)
    _add_note(eid, "Meets at the noodle stand.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/rules-and-notes.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="world-a-rules-and-notes.md"' in r.headers["content-disposition"]
    assert "# Custom Rules" in r.text
    assert "### Vex the Informant (Character)" in r.text
    assert "Secretly a corp plant." in r.text  # GM-only export: unfiltered, unlike the entity download
    assert "Meets at the noodle stand." in r.text


def test_rules_and_notes_omits_notes_section_when_no_notes_exist(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/rules-and-notes.md")
    assert r.status_code == 200
    assert "## Notes" not in r.text
