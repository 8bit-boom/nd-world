"""Pre-compressed static files (performance plan, step 6): app.precompress writes .br / .gz twins, app.http_perf serves them as-is."""
import gzip
import os
from pathlib import Path

from app import precompress

STATIC = Path(__file__).resolve().parent.parent / "static"
PROBE = STATIC / "js" / "_pc_probe.js"
TEXT = ("// probe\n" + "function probe() { return 'hello precompressed world'; }\n" * 80).encode()


def _cleanup():
    for ext in ("", ".gz", ".br"):
        (STATIC / "js" / ("_pc_probe.js" + ext)).unlink(missing_ok=True)


def test_precompress_writes_smaller_twins_and_skips_small_files(tmp_path):
    (tmp_path / "a.js").write_bytes(TEXT)
    (tmp_path / "tiny.js").write_bytes(b"x = 1;\n")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG" + os.urandom(4000))
    stats = precompress.compress_tree(tmp_path)
    assert stats["files"] == 1 and (tmp_path / "a.js.gz").is_file()
    assert (tmp_path / "a.js.gz").stat().st_size < len(TEXT) / 5
    assert not (tmp_path / "tiny.js.gz").exists() and not (tmp_path / "pic.png.gz").exists()
    assert gzip.decompress((tmp_path / "a.js.gz").read_bytes()) == TEXT
    again = precompress.compress_tree(tmp_path)                      # nothing newer than its twin: nothing rewritten
    assert again["gz"] == 0 and again["br"] == 0


def test_a_request_that_accepts_gzip_gets_the_twin_unchanged(client):
    try:
        PROBE.write_bytes(TEXT)
        PROBE.with_name(PROBE.name + ".gz").write_bytes(gzip.compress(TEXT, mtime=0))
        r = client.get("/static/js/_pc_probe.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and r.headers["vary"] == "Accept-Encoding"
        assert "immutable" in r.headers["cache-control"] and "javascript" in r.headers["content-type"]
        assert r.content == TEXT                                      # httpx decoded it: the bytes are the original file's
        plain = client.get("/static/js/_pc_probe.js", headers={"Accept-Encoding": "identity"})
        assert plain.status_code == 200 and plain.content == TEXT and "content-encoding" not in plain.headers
    finally:
        _cleanup()


def test_a_twin_older_than_its_source_is_never_served(client):
    try:
        PROBE.write_bytes(TEXT)
        twin = PROBE.with_name(PROBE.name + ".gz")
        twin.write_bytes(gzip.compress(b"STALE", mtime=0))
        os.utime(twin, (1, 1))                                        # the file was edited after its twin was made
        r = client.get("/static/js/_pc_probe.js", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and r.content == TEXT
    finally:
        _cleanup()


def test_the_twin_lookup_cannot_leave_the_static_folder(client):
    r = client.get("/static/../app/main.py", headers={"Accept-Encoding": "gzip"})
    assert r.status_code in (404, 400) or b"FastAPI" not in r.content
    r = client.get("/static/js/does-not-exist.js", headers={"Accept-Encoding": "gzip, br"})
    assert r.status_code == 404
