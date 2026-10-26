"""Diff the two book.zip index.html entries (run inside the container)."""
import httpx, io, zipfile, difflib, os

d = httpx.Client(base_url="http://127.0.0.1:9031")
d.post("/login", data={"email": "gm@test.local", "password": "gm-password-123"})
v = httpx.Client(base_url="http://127.0.0.1:9051")
for k, vv in d.cookies.items():
    v.cookies.set(k, vv)
a, b = d.get("/export/book.zip"), v.get("/export/book.zip")
print("served:", b.headers.get("x-nd-served-by"), a.status_code, b.status_code)
za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
ta = za.read("index.html").decode("utf-8")
tb = zb.read("index.html").decode("utf-8")
for line in list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), "py", "gw", lineterm=""))[:40]:
    print(line)
