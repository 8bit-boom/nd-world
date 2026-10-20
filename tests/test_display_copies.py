"""Display-size copies of large uploads (efficiency plan, step 13): the lightbox gets a 1,600 px WebP, the original stays one click away."""
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
