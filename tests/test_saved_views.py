"""Saved named views (docs/LEFTOVERS_PLAN.md, step 15): a person saves the layout of a list page under a name and picks it again from the
layout bar's Views menu. Each person's own rows only; the layout is cleaned like every saved layout; the bar offers the menu when signed in."""
from pathlib import Path

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


def test_save_list_replace_and_delete(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get("/api/saved-views/images").json() == {"views": []}
    r = client.post("/api/saved-views/images", json={"name": "  Prep   night ", "state": {"mode": "masonry", "sort": "-date", "size": 3, "junk": 1}})
    assert r.status_code == 200
    (v,) = r.json()["views"]
    assert v["name"] == "Prep night" and v["state"]["mode"] == "masonry" and v["state"]["sort"] == "-date" and "junk" not in v["state"]
    again = client.post("/api/saved-views/images", json={"name": "Prep night", "state": {"mode": "list"}}).json()["views"]
    assert len(again) == 1 and again[0]["state"]["mode"] == "list", "the same name replaces the view"
    client.post("/api/saved-views/images", json={"name": "Big", "state": {"mode": "nonsense"}})
    views = client.get("/api/saved-views/images").json()["views"]
    assert [x["name"] for x in views] == ["Big", "Prep night"] and views[0]["state"]["mode"] == "grid", "an unknown layout falls back to the default"
    assert client.get("/api/saved-views/audio").json() == {"views": []}, "views belong to one page"
    left = client.delete(f"/api/saved-views/images/{views[0]['id']}").json()["views"]
    assert [x["name"] for x in left] == ["Prep night"]


def test_bad_requests(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get("/api/saved-views/nope").status_code == 404
    assert client.post("/api/saved-views/nope", json={"name": "x", "state": {}}).status_code == 404
    assert client.post("/api/saved-views/images", json={"name": "   ", "state": {}}).status_code == 400
    assert client.post("/api/saved-views/images", content="{not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/saved-views/images", json=[1]).status_code == 400
    assert client.delete("/api/saved-views/images/99999").status_code == 404
    long = client.post("/api/saved-views/images", json={"name": "x" * 90, "state": {}}).json()["views"][0]["name"]
    assert len(long) == 40


def test_at_most_twenty_per_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    for i in range(20):
        assert client.post("/api/saved-views/audio", json={"name": f"v{i}", "state": {}}).status_code == 200
    assert client.post("/api/saved-views/audio", json={"name": "one more", "state": {}}).status_code == 400
    assert client.post("/api/saved-views/audio", json={"name": "v3", "state": {"mode": "pads"}}).status_code == 200, "replacing still works"


def test_each_person_sees_and_deletes_only_their_own(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    vid = client.post("/api/saved-views/images", json={"name": "Mine", "state": {}}).json()["views"][0]["id"]
    client.post("/logout")
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/saved-views/images").json() == {"views": []}
    assert client.delete(f"/api/saved-views/images/{vid}").status_code == 404
    client.post("/logout")
    client.cookies.clear()
    assert client.get("/api/saved-views/images").status_code in (401, 302, 303)


def test_the_layout_bar_offers_the_views_menu():
    js = (ROOT / "static" / "js" / "nd-views.js").read_text()
    assert "/api/saved-views/" in js and "Save this layout as" in js and "nd-vb-views" in js
