"""Existing thumbnails are made again in the chosen format (app/thumb_migrate.py): WebP ones become AVIF (and back after a switch), the old
file goes, a thumbnail whose picture is gone is left alone, and the app starts the job at startup and when the setting is saved."""
from PIL import Image

from app import imaging, thumb_migrate

from .conftest import GM_PASSWORD, login


def _pic(path, size=(900, 600)):
    Image.new("RGB", size, (30, 90, 160)).save(path)
    return path


def _old_webp_thumb(src):
    t = src.with_name(src.stem + "_thumb.webp")
    im = Image.open(src).convert("RGB")
    im.thumbnail((440, 440))
    im.save(t, "WEBP")
    return t


def test_webp_thumbnails_become_avif_and_back(tmp_path):
    (tmp_path / "a").mkdir()
    pics = [_pic(tmp_path / "one.png"), _pic(tmp_path / "a" / "two.jpg"), _pic(tmp_path / "a" / "three.webp")]
    olds = [_old_webp_thumb(p) for p in pics]
    orphan = tmp_path / "gone_thumb.webp"
    orphan.write_bytes(olds[0].read_bytes())
    assert imaging.preview_format() == "avif"
    assert thumb_migrate.regenerate(tmp_path) == {"done": 3, "failed": 0}
    for p, old in zip(pics, olds):
        new = p.with_name(p.stem + "_thumb.avif")
        assert new.is_file() and not old.exists() and Image.open(new).format == "AVIF"
        assert imaging.thumbnail_path_for(p) == new
    assert orphan.exists(), "no picture to remake it from: kept"
    assert thumb_migrate.regenerate(tmp_path) == {"done": 0, "failed": 0}, "nothing left to do"
    try:
        imaging.set_preview_format("webp")
        assert thumb_migrate.regenerate(tmp_path)["done"] == 3
        assert all(p.with_name(p.stem + "_thumb.webp").is_file() and not p.with_name(p.stem + "_thumb.avif").exists() for p in pics)
    finally:
        imaging.set_preview_format("avif")


def test_a_second_start_while_running_does_nothing(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(thumb_migrate, "regenerate", lambda *a, **k: gate.wait(5) or {"done": 0, "failed": 0})
    assert thumb_migrate.start(tmp_path) is True
    assert thumb_migrate.start(tmp_path) is False
    gate.set()
    for _ in range(100):
        if not thumb_migrate._running:
            break
        threading.Event().wait(0.02)
    assert thumb_migrate._running is False


def test_saving_the_setting_starts_the_job(client, seed, monkeypatch):
    calls = []
    monkeypatch.setattr(thumb_migrate, "start", lambda *roots, **k: calls.append(roots) or True)
    login(client, seed.gm.email, GM_PASSWORD)
    try:
        client.post("/settings", data={"preview_format": "webp"}, follow_redirects=False)
        assert calls and any(str(r).endswith("uploads") for r in calls[-1])
    finally:
        imaging.set_preview_format("avif")
