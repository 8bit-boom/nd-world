"""Diff the two live answers for the sync GET (run inside the container)."""
import httpx, json, os

direct = httpx.Client(base_url="http://127.0.0.1:9031")
via = httpx.Client(base_url="http://127.0.0.1:9051")
for c in (direct, via):
    c.get("/login")
    c.post("/login", data={"email": "gm@test.local", "password": "gm-password-123"})
a = direct.get("/api/characters/1/sync").json()
b = via.get("/api/characters/1/sync").json()
for k in sorted(set(a) | set(b)):
    if a.get(k) != b.get(k):
        print("KEY", k)
        print("  PY:", json.dumps(a.get(k))[:200])
        print("  GW:", json.dumps(b.get(k))[:200])
print("py keys:", list(a.keys()) == list(b.keys()))
if list(a.keys()) != list(b.keys()):
    print("py order:", list(a.keys()))
    print("gw order:", list(b.keys()))
