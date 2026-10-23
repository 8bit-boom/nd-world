"""Signing in and out through the gateway (rust/nd-gateway/src/auth.rs, `auth` in ND_RUST_ROUTES) against Python's (app/routers/auth.py):
the same redirects, pages and JSON for right and wrong passwords, unknown emails, `next` targets, two-step codes (a TOTP, a backup code
used once, a trusted device), the lockout after 8 failures and logging out - and a session either side writes works on the other."""
import base64
import hashlib
import json

import httpx
import pytest
import pyotp

from app import auth as auth_module
from .conftest import GM_PASSWORD, PLAYER_PASSWORD
from .test_gateway_golden import DIRECT

pytestmark = pytest.mark.skipif(not DIRECT, reason="needs ND_TEST_BASE_URL (the gateway) and ND_GOLDEN_DIRECT_URL (Python directly)")


def _session(c):
    raw = c.cookies.get("session")
    if not raw or raw == "null":
        return None
    data = json.loads(base64.b64decode(raw.split(".")[0] + "=="))
    if isinstance(data.get("site_layout"), dict):
        data["site_layout"].pop("at", None)
    return data


def _post(base, path, data=None, js=None, cookies=None):
    c = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
    for k, v in (cookies or {}).items():
        c.cookies.set(k, v)
    r = c.post(path, data=data) if js is None else c.post(path, json=js)
    return r, c


def _norm(r):
    body = r.text if "html" in r.headers.get("content-type", "") else (r.json() if r.content else None)
    return r.status_code, r.headers.get("location"), body


def test_login_is_pythons(client, seed):
    base = str(client.base_url)
    for form in ({"email": seed.gm.email, "password": GM_PASSWORD, "next": "/entity/1"}, {"email": f"  {seed.gm.email.upper()} ", "password": GM_PASSWORD},
                 {"email": seed.gm.email, "password": GM_PASSWORD, "next": "//evil.example"}, {"email": seed.gm.email, "password": "nope", "next": "/x"},
                 {"email": "nobody@test.local", "password": "x"}, {"email": seed.player_a.email, "password": PLAYER_PASSWORD}):
        a, ca = _post(DIRECT, "/login", data=form)
        b, cb = _post(base, "/login", data=form)
        assert _norm(a) == _norm(b), (form, _norm(a), _norm(b))
        assert _session(ca) == _session(cb), form
        assert b.headers.get("x-nd-served-by") == "gateway"
        if a.status_code == 303:
            # each side's session opens the other's pages
            assert httpx.get(DIRECT + "/", cookies={"session": cb.cookies["session"]}, follow_redirects=False).status_code == 200
            assert httpx.get(base + "/", cookies={"session": ca.cookies["session"]}, follow_redirects=False).status_code == 200
            la = ca.get("/logout"), cb.get("/logout")
            assert [(x.status_code, x.headers["location"], x.headers.get("clear-site-data"), x.headers.get("set-cookie", "").split(";")[0]) for x in la] == \
                [(303, "/login", '"cache", "storage"', "session=null")] * 2
    for js in ({"email": seed.gm.email, "password": GM_PASSWORD}, {"email": seed.gm.email, "password": "bad"}, {"email": 5, "password": "x"}):
        a, ca = _post(DIRECT, "/api/login", js=js)
        b, cb = _post(base, "/api/login", js=js)
        assert _norm(a) == _norm(b), (js, _norm(a), _norm(b))
        assert _session(ca) == _session(cb)


def test_lockout_is_pythons(client, seed):
    base = str(client.base_url)
    for side in (DIRECT, base):
        email = f"lock-{hashlib.md5(side.encode()).hexdigest()[:6]}@test.local"
        codes = [_post(side, "/login", data={"email": email, "password": "x"})[0].status_code for _ in range(9)]
        assert codes == [400] * 8 + [429], (side, codes)
        r = _post(side, "/login", data={"email": email, "password": "x"})[0]
        assert "Too many failed attempts. Try again in 5 minute(s)." in r.text


def test_two_step_is_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import User
    base = str(client.base_url)
    secret = pyotp.random_base32()
    db = SessionLocal()
    try:
        u = User(email="mcp-2fa@test.local", password_hash=auth_module.hash_password("pw-2fa-123"), display_name="Two", is_gm=False,
                 totp_enabled=True, totp_secret=secret,
                 totp_backup_codes_json=json.dumps([hashlib.sha256(c.encode()).hexdigest() for c in ("aaaaa11111", "bbbbb22222", "ccccc33333")]))
        db.add(u)
        db.commit()
    finally:
        db.close()
    login = {"email": "mcp-2fa@test.local", "password": "pw-2fa-123", "next": "/facts"}
    for code, trust in ((pyotp.TOTP(secret).now(), ""), ("000000", ""), ("aaaaa11111", "1")):
        out = []
        for side in (DIRECT, base):
            r, c = _post(side, "/login", data=login)
            first = (_norm(r), _session(c))
            page = c.get("/login/2fa")
            r2 = c.post("/login/2fa", data={"code": code if code != "aaaaa11111" or side == DIRECT else "bbbbb22222", "trust_device": trust})
            out.append((first, page.status_code, page.text, _norm(r2), _session(c), bool(c.cookies.get("trusted_device"))))
        assert out[0] == out[1], (code, out)
    # the trusted device skips the code next time, on either side
    for side in (DIRECT, base):
        r, c = _post(side, "/login", data=login)
        r, c = _post(side, "/login/2fa", data={"code": pyotp.TOTP(secret).now(), "trust_device": "1"}, cookies={"session": c.cookies["session"]})
        r, c = _post(side, "/login", data=login, cookies={"trusted_device": c.cookies["trusted_device"]})
        assert (r.status_code, r.headers["location"]) == (303, "/facts"), side
    for js in ({"email": "mcp-2fa@test.local", "password": "pw-2fa-123"}, {"email": "mcp-2fa@test.local", "password": "pw-2fa-123", "totp_code": "123"}):
        a, b = _post(DIRECT, "/api/login", js=js)[0], _post(base, "/api/login", js=js)[0]
        assert _norm(a) == _norm(b), js


def test_stale_session_is_refreshed_by_the_gateway(client, seed):
    """A session whose site layout is older than 15 minutes (or signed over 6 hours ago) used to send the page to Python, which
    rewrites the cookie; the gateway now refreshes it up front (so its own pages can serve it), the new cookie riding on the answer."""
    import itsdangerous
    from app.main import SECRET_KEY
    base = str(client.base_url)
    r, c = _post(base, "/login", data={"email": seed.gm.email, "password": GM_PASSWORD})
    data = _session(c)
    data["site_layout"]["at"] = 1000
    signer = itsdangerous.TimestampSigner(str(SECRET_KEY))
    stale = signer.sign(base64.b64encode(json.dumps(data).encode())).decode()
    r = httpx.get(base + "/", cookies={"session": stale, "active_world": seed.world_a.slug}, follow_redirects=False)
    assert r.status_code == 200
    new = [h for h in r.headers.get_list("set-cookie") if h.startswith("session=")]
    assert len(new) == 1 and "Max-Age=1209600; httponly; samesite=lax" in new[0]
    fresh = json.loads(base64.b64decode(new[0].split("=", 1)[1].split(";")[0].split(".")[0] + "=="))
    assert fresh["site_layout"]["at"] > 1000 and fresh["user_id"] == data["user_id"]


def test_auth_gate_is_pythons(client, seed):
    """auth_gate's refusals answered by the gateway: not signed in (a page goes to /login?next=..., an /api/ path gets a 401), a player
    on a GM-only page or API (403), a session from before a password change (cleared; 401 or back to sign in) and a deleted user."""
    from app.database import SessionLocal
    from app.models import User
    base = str(client.base_url)
    paths = ["/settings", "/api/cockpit/workspace", "/sessions/1", "/a b/ü?x=1", "/api/nope", "/characters", "/cockpit"]

    def both(cookies):
        out = []
        for side in (DIRECT, base):
            res = []
            for p in paths:
                r = httpx.get(side + p, cookies=cookies, follow_redirects=False)
                sc = [h.split(";")[0] for h in r.headers.get_list("set-cookie")]
                body = r.text if r.status_code in (401, 403) else ""
                res.append((p, r.status_code, r.headers.get("location"), body, sc))
            out.append(res)
        return out

    a, b = both({})
    assert a == b, (a, b)
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        r, c = _post(DIRECT, "/login", data={"email": email, "password": pw})
        a, b = both({"session": c.cookies["session"], "active_world": seed.world_a.slug})
        strip = lambda rows: [x[:4] for x in rows if x[1] in (303, 401, 403)]
        assert strip(a) == strip(b), (email, a, b)
    # a password change elsewhere bumps the version; a deleted person is gone
    r, c = _post(DIRECT, "/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD})
    old = c.cookies["session"]
    db = SessionLocal()
    try:
        u = db.get(User, seed.player_a.id)
        u.session_version = (u.session_version or 0) + 1
        db.commit()
        ghost = User(email="ghost@test.local", password_hash=auth_module.hash_password("pw-ghost-1"), display_name="G")
        db.add(ghost)
        db.commit()
    finally:
        db.close()
    a, b = both({"session": old})
    assert a == b, (a, b)
    r, c = _post(DIRECT, "/login", data={"email": "ghost@test.local", "password": "pw-ghost-1"})
    db = SessionLocal()
    try:
        db.delete(db.query(User).filter(User.email == "ghost@test.local").one())
        db.commit()
    finally:
        db.close()
    a, b = both({"session": c.cookies["session"]})
    assert a == b, (a, b)
