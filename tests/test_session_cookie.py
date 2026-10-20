"""The session cookie is re-sent only when it changed or is old (efficiency plan, step 10) - and a session still works and still ends."""
import time

from app import session_cookie

from .conftest import GM_PASSWORD, login


def test_an_unchanged_session_sends_no_cookie_but_a_change_does(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/spotlight")
    assert r.status_code == 200 and "set-cookie" not in r.headers                # nothing changed: no cookie
    assert client.get("/").status_code == 200                                     # still signed in with the cookie it has
    r = client.post("/api/view-prefs/site", json={"options": {"nav": "rail"}})    # the site layout rides in the session: it changed
    assert r.status_code == 200 and "set-cookie" in r.headers


def test_an_old_cookie_is_refreshed_so_the_expiry_keeps_sliding(client, seed, monkeypatch):
    login(client, seed.gm.email, GM_PASSWORD)
    monkeypatch.setattr(session_cookie, "REFRESH_SECONDS", -1)                    # every cookie counts as old
    r = client.get("/api/spotlight")
    assert "set-cookie" in r.headers and "Max-Age=" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()


def test_logging_out_still_clears_the_cookie(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/logout", follow_redirects=False)
    assert "set-cookie" in r.headers and ("null" in r.headers["set-cookie"] or "1970" in r.headers["set-cookie"])
    assert client.get("/api/spotlight").status_code == 401
