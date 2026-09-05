"""Regression coverage for a real bug class found via a user report ("I
can't delete session"): several GM edit pages wrote their hidden #del-form
(the JS-submit target for a "Delete" confirm-dialog link) INSIDE the page's
main edit/save <form>. A <form> nested inside another <form> is invalid
HTML — every browser's parser silently drops the inner <form> start tag
from the DOM entirely, so document.getElementById('del-form') returned
null and .submit() never fired. The Delete button was a complete no-op in
a real browser even though the underlying POST .../delete route itself
works fine when hit directly — confirmed live with Playwright before
fixing app/templates/sessions/detail.html, then found + fixed the same
copy-pasted pattern in entity_template_form.html, quests/detail.html, and
tables/form.html.

pytest's TestClient never renders/parses HTML the way a browser does, so a
plain status-code/response-text assertion can't catch this — these tests
use conftest.assert_no_nested_forms, which actually walks the tag tree
with html.parser."""
from app.database import SessionLocal
from app.models import EntityTemplate, Quest, RandomTable

from .conftest import GM_PASSWORD, assert_no_nested_forms, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_entity_template_edit_delete_form_is_not_nested(client, seed):
    db = SessionLocal()
    try:
        tpl = EntityTemplate(world_id=seed.world_a.id, name="Stat Block", slug="stat-block-x")
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        tpl_id = tpl.id
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/entity-templates/{tpl_id}/edit")
    assert r.status_code == 200
    assert 'id="del-form"' in r.text
    assert_no_nested_forms(r.text)


def test_quest_detail_delete_form_is_not_nested(client, seed):
    db = SessionLocal()
    try:
        q = Quest(world_id=seed.world_a.id, title="Find the Missing Courier")
        db.add(q)
        db.commit()
        db.refresh(q)
        quest_id = q.id
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/quests/{quest_id}")
    assert r.status_code == 200
    assert 'id="del-form"' in r.text
    assert_no_nested_forms(r.text)


def test_random_table_edit_delete_form_is_not_nested(client, seed):
    db = SessionLocal()
    try:
        tbl = RandomTable(world_id=seed.world_a.id, name="Street Encounters", slug="street-encounters-x")
        db.add(tbl)
        db.commit()
        db.refresh(tbl)
        table_id = tbl.id
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/tables/{table_id}/edit")
    assert r.status_code == 200
    assert 'id="del-form"' in r.text
    assert_no_nested_forms(r.text)
