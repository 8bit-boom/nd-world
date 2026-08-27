"""Tests for app/templating.py's thumb_url helper — the Jinja global/filter
grid/list templates use to prefer a small preview variant (see
app.imaging.make_thumbnail) over a full-resolution upload, falling back to
the original URL when no thumbnail exists on disk (an svg, a pre-existing
upload from before this feature shipped, or any other unthumbnailable
source).
"""
from PIL import Image

from app.imaging import make_thumbnail
from app.templating import templates, thumb_url


def test_thumb_url_registered_as_both_global_and_filter():
    assert templates.env.globals["thumb_url"] is thumb_url
    assert templates.env.filters["thumb"] is thumb_url


def test_thumb_url_blank_and_none_pass_through():
    assert thumb_url("") == ""
    assert thumb_url(None) is None


def test_thumb_url_leaves_non_upload_urls_untouched():
    assert thumb_url("/static/logo.png") == "/static/logo.png"
    assert thumb_url("https://example.com/x.png") == "https://example.com/x.png"


def test_thumb_url_falls_back_to_original_when_no_thumbnail_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("app.templating._UPLOADS_DIR", tmp_path)
    (tmp_path / "portraits").mkdir()
    (tmp_path / "portraits" / "real.avif").write_bytes(b"not decoded, existence is all that matters here")
    assert thumb_url("/uploads/portraits/real.avif") == "/uploads/portraits/real.avif"


def test_thumb_url_resolves_to_the_thumbnail_when_one_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("app.templating._UPLOADS_DIR", tmp_path)
    portraits = tmp_path / "portraits"
    portraits.mkdir()
    src = portraits / "real.png"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(src, format="PNG")
    thumb = make_thumbnail(src)
    assert thumb.is_file()

    assert thumb_url("/uploads/portraits/real.png") == "/uploads/portraits/real_thumb.webp"


def test_thumb_url_is_a_render_template_helper_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("app.templating._UPLOADS_DIR", tmp_path)
    src = tmp_path / "gen.png"
    Image.new("RGB", (900, 900), (5, 5, 5)).save(src, format="PNG")
    make_thumbnail(src)

    tmpl = templates.env.from_string('<img src="{{ thumb_url(url) }}">')
    assert tmpl.render(url="/uploads/gen.png") == '<img src="/uploads/gen_thumb.webp">'

    tmpl2 = templates.env.from_string('<img src="{{ url|thumb }}">')
    assert tmpl2.render(url="/uploads/gen.png") == '<img src="/uploads/gen_thumb.webp">'
