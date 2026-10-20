"""Display-size copies of large uploads (efficiency plan, step 13): the lightbox gets a 2,560 px WebP, the original stays one click away."""
from PIL import Image

from app import imaging

from .conftest import GM_PASSWORD, login


def test_a_large_picture_gets_a_display_copy_and_a_small_one_does_not(tmp_path):
    big, small = tmp_path / "big.png", tmp_path / "small.png"
    Image.new("RGB", (4000, 3000), "blue").save(big)
    Image.new("RGB", (800, 600), "blue").save(small)
    d = imaging.make_display(big)
    assert d == imaging.display_path_for(big) and max(Image.open(d).size) == imaging.DISPLAY_MAX_DIM
    assert imaging.make_display(big) == d                                   # reused
    assert imaging.make_display(small) is None and imaging.make_display(d) is None


def test_the_upload_route_serves_the_copy_only_when_asked(client, seed):
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    (UPLOADS_DIR / "p").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3200, 2000), "red").save(UPLOADS_DIR / "p" / "huge.png")
    orig = client.get("/uploads/p/huge.png")
    shown = client.get("/uploads/p/huge.png?display=1")
    assert orig.headers["content-type"] == "image/png" and shown.headers["content-type"] == "image/webp"
    assert len(shown.content) < len(orig.content) and "private" in shown.headers["cache-control"]


def test_the_lightbox_offers_the_original():
    base = open("app/templates/base.html").read()
    assert 'id="lightbox-full"' in base and "?display=1" in base


def test_the_display_filter_follows_the_lightbox_rule():
    from app.templating import display_url
    assert display_url("/uploads/p/a.JPG") == "/uploads/p/a.JPG?display=1"
    assert display_url("/uploads/entities/x.webp") == "/uploads/entities/x.webp?display=1"
    for keep in ("/uploads/maps/m.jpg", "/uploads/p/a_thumb.webp", "/uploads/p/a_display.webp", "/uploads/p/a.png?v=2",
                 "/static/maps/m.avif", "https://example.com/a.png", "/uploads/p/a.gif", "", None):
        assert display_url(keep) == keep


def test_the_handout_print_page_and_the_filmstrip_use_display_copies():
    """A print of many handouts loaded every original (often 3-10 MB each); the picture prints at most ~15 cm wide, where 2,560 px is
    over 300 dpi. The filmstrip's big preview likewise; its "Full size" and second-screen buttons keep the original."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "item.entity.image_url | display" in (root / "app" / "templates" / "handout.html").read_text()
    js = (root / "static" / "js" / "nd-views.js").read_text()
    assert "f.img.src = displayOf(srcOf(item))" in js
    assert "openLightbox(srcOf(current)" in js and "showImage(srcOf(current)" in js


def test_a_copy_from_the_first_version_is_made_again_sharper(tmp_path):
    """The first display copies were 1,600 px at quality 82 and looked over-compressed on sharp screens: one of those is replaced."""
    import os
    from PIL import ImageDraw
    big = tmp_path / "map.jpg"
    im = Image.merge("RGB", [Image.radial_gradient("L").resize((4000, 3000)), Image.linear_gradient("L").resize((4000, 3000)),
                             Image.effect_noise((4000, 3000), 40)])
    im.save(big, quality=95)
    old = imaging.display_path_for(big)
    small = im.copy(); small.thumbnail((1600, 1600)); small.save(old, "WEBP", quality=82)
    os.utime(old, (big.stat().st_mtime + 5, big.stat().st_mtime + 5))
    d2 = imaging.make_display(big)
    assert d2 == old and max(Image.open(d2).size) == imaging.DISPLAY_MAX_DIM == 2560 and imaging.DISPLAY_QUALITY >= 90


def test_a_copy_that_would_barely_be_smaller_is_not_kept(tmp_path):
    """Noise compresses badly: when the copy would not save DISPLAY_MIN_SAVING of the bytes, the original is shown and no copy is left."""
    import os
    big = tmp_path / "noise.jpg"
    Image.frombytes("RGB", (2700, 2000), os.urandom(2700 * 2000 * 3)).save(big, quality=30)
    assert imaging.make_display(big) is None and not imaging.display_path_for(big).exists()
