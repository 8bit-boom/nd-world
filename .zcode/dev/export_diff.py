"""Diff the two live answers for the character exports (run inside the container)."""
import httpx, json, difflib

direct = httpx.Client(base_url="http://127.0.0.1:9031")
via = httpx.Client(base_url="http://127.0.0.1:9051")
for c in (direct, via):
    c.get("/login")
    c.post("/login", data={"email": "gm@test.local", "password": "gm-password-123"})
r = direct.get("/characters")
ids = []
# find any pc rows through the api
import sqlite3
db = sqlite3.connect("/work/tests/_data/dev/world.db")
rows = db.execute("SELECT id, name FROM player_characters ORDER BY id").fetchall()
db.close()
for pid, name in rows:
    a = direct.get(f"/characters/{pid}/export.json")
    b = via.get(f"/characters/{pid}/export.json")
    mark = "GW" if b.headers.get("x-nd-served-by") == "gateway" else "py"
    if a.content != b.content:
        print(f"--- pc {pid} ({name}) json differs (served {mark})")
        for line in list(difflib.unified_diff(a.text.splitlines(), b.text.splitlines(), "py", "gw", lineterm=""))[:24]:
            print(line)
    else:
        print(f"pc {pid} ({name}) json OK ({mark})")
    a = direct.get(f"/characters/{pid}/export.md")
    b = via.get(f"/characters/{pid}/export.md")
    mark = "GW" if b.headers.get("x-nd-served-by") == "gateway" else "py"
    if a.content != b.content:
        print(f"--- pc {pid} ({name}) md differs (served {mark})")
        for line in list(difflib.unified_diff(a.text.splitlines(), b.text.splitlines(), "py", "gw", lineterm=""))[:24]:
            print(line)
    else:
        print(f"pc {pid} ({name}) md OK ({mark})")
