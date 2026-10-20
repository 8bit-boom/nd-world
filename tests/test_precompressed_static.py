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


def test_bundled_maps_as_avif_keep_old_jpg_and_webp_links_working(client, tmp_path):
    from PIL import Image, ImageChops, ImageStat
    from app import static_thumbs
    import math
    pic = Image.radial_gradient("L").resize((300, 200)).convert("RGB")
    pic.save(tmp_path / "m.jpg", quality=95)
    pic.save(tmp_path / "w.webp", quality=95)
    Image.new("RGB", (40, 40), "blue").save(tmp_path / "keep_thumb.webp")
    n, before, after = static_thumbs.compact_maps(tmp_path)
    assert n == 2 and after < before
    assert (tmp_path / "m.avif").is_file() and (tmp_path / "w.avif").is_file()
    assert not (tmp_path / "m.jpg").exists() and not (tmp_path / "w.webp").exists()
    assert (tmp_path / "keep_thumb.webp").is_file()                     # thumbnails are never converted
    with Image.open(tmp_path / "m.avif") as im:                          # still the same picture
        mse = sum(v * v for v in ImageStat.Stat(ImageChops.difference(im.convert("RGB"), pic)).rms) / 3
        assert im.size == (300, 200) and 10 * math.log10(255 ** 2 / max(mse, 1e-9)) > 35
    assert static_thumbs.compact_maps(tmp_path) == (0, 0, 0)            # run again: nothing left to do
    probe = STATIC / "maps" / "_probe_map.avif"
    try:
        Image.new("RGB", (10, 10), "red").save(probe)
        for old in ("jpg", "webp"):
            r = client.get(f"/static/maps/_probe_map.{old}", follow_redirects=False)
            assert r.status_code == 301 and r.headers["location"] == "/static/maps/_probe_map.avif"
        r = client.get("/static/maps/_probe_map.avif")
        assert r.status_code == 200 and r.headers["content-type"] == "image/avif"
        assert client.get("/static/maps/_nothing_here.jpg", follow_redirects=False).status_code == 404
        assert client.get("/static/maps/_nothing_here.webp", follow_redirects=False).status_code == 404
    finally:
        probe.unlink(missing_ok=True)


def test_an_image_from_before_avif_still_redirects_jpg_links_to_webp(client):
    from PIL import Image
    probe = STATIC / "maps" / "_probe_old.webp"
    try:
        Image.new("RGB", (10, 10), "red").save(probe)
        r = client.get("/static/maps/_probe_old.jpg", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/static/maps/_probe_old.webp"
        assert client.get("/static/maps/_probe_old.webp").status_code == 200
    finally:
        probe.unlink(missing_ok=True)


def test_maps_fall_back_to_webp_when_pillow_cannot_write_avif(tmp_path, monkeypatch):
    from PIL import Image, features
    from app import static_thumbs
    Image.radial_gradient("L").resize((300, 200)).convert("RGB").save(tmp_path / "m.png")
    real = features.check
    monkeypatch.setattr(features, "check", lambda name: False if name == "avif" else real(name))
    n, _, _ = static_thumbs.compact_maps(tmp_path)
    assert n == 1 and (tmp_path / "m.webp").is_file() and not (tmp_path / "m.png").exists()


def test_huge_maps_keep_their_file(tmp_path, monkeypatch):
    """A picture over MAX_MAP_PIXELS is shown as tiles; re-encoding it cost gigabytes of memory in the image build for nothing."""
    from PIL import Image
    from app import static_thumbs
    Image.radial_gradient("L").resize((300, 200)).convert("RGB").save(tmp_path / "big.jpg", quality=95)
    monkeypatch.setattr(static_thumbs, "MAX_MAP_PIXELS", 300 * 200 - 1)
    assert static_thumbs.compact_maps(tmp_path) == (0, 0, 0)
    assert (tmp_path / "big.jpg").is_file() and not (tmp_path / "big.avif").exists()
