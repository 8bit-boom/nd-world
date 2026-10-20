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


def test_the_image_builds_minifier_keeps_every_script_valid(tmp_path):
    """The Docker build minifies static/ in place (python -m app.precompress --minify). Every minified script must still parse."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed")
    shutil.copytree(STATIC / "js", tmp_path / "js")
    shutil.copy(STATIC / "style.css", tmp_path / "style.css")
    st = precompress.minify_tree(tmp_path)
    assert st["files"] > 50 and st["bytes_out"] < st["bytes_in"] * 0.8
    bad = []
    for f in sorted((tmp_path / "js").rglob("*.js")):
        text = f.read_text(encoding="utf-8")
        target = f.with_suffix(".mjs") if ("import " in text and " from " in text) or text.lstrip().startswith("export ") else f
        if target != f:
            f.rename(target)
        r = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
        if r.returncode:
            bad.append((f.name, r.stderr[-200:]))
    assert not bad, bad
    assert "{" in (tmp_path / "style.css").read_text() and "/*" not in (tmp_path / "style.css").read_text()


def test_bundled_maps_as_webp_keep_old_jpg_links_working(client, tmp_path):
    from PIL import Image
    from app import static_thumbs
    Image.new("RGB", (300, 200), "green").save(tmp_path / "m.jpg", quality=95)
    n, before, after = static_thumbs.webp_maps(tmp_path)
    assert n == 1 and after < before and (tmp_path / "m.webp").is_file() and not (tmp_path / "m.jpg").exists()
    probe = STATIC / "maps" / "_probe_map.webp"
    try:
        Image.new("RGB", (10, 10), "red").save(probe)
        r = client.get("/static/maps/_probe_map.jpg", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/static/maps/_probe_map.webp"
        assert client.get("/static/maps/_nothing_here.jpg", follow_redirects=False).status_code == 404
    finally:
        probe.unlink(missing_ok=True)
