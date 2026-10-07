"""Map-object pictures (app/prop_images.py): raster pictures are re-encoded and trimmed, SVGs are rebuilt from an
allow-list. The hostile samples matter most: an uploaded 'prop' is shown to every player."""
import io

import pytest
from PIL import Image

from app import prop_images as P


def _png(size=(200, 120), box=(40, 30, 160, 90), colour=(200, 40, 40, 255), fmt="PNG"):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), colour), (box[0], box[1]))
    out = io.BytesIO()
    if fmt == "JPEG":
        im = im.convert("RGB")
    im.save(out, fmt)
    return out.getvalue()


# ── raster ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt,ext", [("PNG", ".png"), ("WEBP", ".webp"), ("GIF", ".gif"), ("BMP", ".bmp"), ("TIFF", ".tif"), ("JPEG", ".jpg")])
def test_every_raster_format_becomes_a_webp(fmt, ext):
    r = P.process_prop(_png(fmt=fmt, colour=(10, 200, 10, 255)), ext)
    assert r["ext"] == ".webp"
    im = Image.open(io.BytesIO(r["data"]))
    assert im.format == "WEBP" and (r["width"], r["height"]) == im.size


def test_transparent_margins_are_trimmed():
    r = P.process_prop(_png(), ".png")
    assert (r["width"], r["height"]) == (120, 60)      # exactly the painted rectangle


def test_big_pictures_are_shrunk_and_keep_their_aspect():
    big = Image.new("RGBA", (3000, 1500), (5, 5, 5, 255))
    buf = io.BytesIO(); big.save(buf, "PNG")
    r = P.process_prop(buf.getvalue(), ".png")
    assert max(r["width"], r["height"]) == P.MAX_SIDE and r["width"] == 2 * r["height"]


def test_a_blank_tiny_garbage_or_oversized_picture_is_refused():
    with pytest.raises(P.PropError):
        P.process_raster(_blank())
    with pytest.raises(P.PropError):
        P.process_prop(_png(box=(10, 10, 12, 12)), ".png")                 # 2x2 px after trimming
    with pytest.raises(P.PropError):
        P.process_prop(b"this is not a picture at all", ".png")
    with pytest.raises(P.PropError):
        P.process_prop(b"x" * (P.MAX_RAW_BYTES + 1), ".png")
    with pytest.raises(P.PropError):
        P.process_prop(_png(), ".exe")


def _blank():
    buf = io.BytesIO(); Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(buf, "PNG"); return buf.getvalue()


def test_a_pixel_bomb_is_refused_without_decoding_it():
    # 1x1 header lies are not possible with PNG, so use a real but absurd canvas: 9000x9000 = 81 MP of one colour
    buf = io.BytesIO(); Image.new("RGB", (9000, 9000), (1, 2, 3)).save(buf, "PNG", optimize=False, compress_level=9)
    with pytest.raises(P.PropError):
        P.process_prop(buf.getvalue(), ".png")


def test_an_animated_gif_uses_its_first_frame():
    a = Image.new("RGBA", (60, 60), (255, 0, 0, 255)); b = Image.new("RGBA", (60, 60), (0, 0, 255, 255))
    buf = io.BytesIO(); a.save(buf, "GIF", save_all=True, append_images=[b], duration=100, loop=0)
    r = P.process_prop(buf.getvalue(), ".gif")
    px = Image.open(io.BytesIO(r["data"])).convert("RGB").getpixel((5, 5))
    assert px[0] > 200 and px[2] < 80


# ── SVG ──────────────────────────────────────────────────────────────────────

GOOD = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 100 50" '
        b'inkscape:version="1.2"><defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
        b'<rect x="2" y="2" width="96" height="46" fill="url(#g)" style="stroke:#000; stroke-width:2"/><text x="5" y="20">Bed</text></svg>')


def test_a_plain_svg_survives_and_editor_metadata_is_dropped():
    r = P.process_prop(GOOD, ".svg")
    assert r["ext"] == ".svg" and (r["width"], r["height"]) == (100.0, 50.0)
    out = r["data"].decode()
    assert "<rect" in out and "url(#g)" in out and "inkscape" not in out and "stroke:#000" in out


HOSTILE = {
    "script element": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script></svg>',
    "onload": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)"/>',
    "onclick on a child": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5" onclick="x()"/></svg>',
    "foreignObject": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><foreignObject><div/></foreignObject></svg>',
    "external image": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="http://evil.example/x.png"/></svg>',
    "javascript href": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><a href="javascript:alert(1)"><rect/></a></svg>',
    "href on a shape": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect href="#x" width="1" height="1"/></svg>',
    "use": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><use href="#a"/></svg>',
    "style element": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><style>@import url(http://evil/x.css);</style></svg>',
    "style url": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect style="fill:url(http://evil/x)" width="1" height="1"/></svg>',
    "style behaviour": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect style="behavior:url(x.htc)" width="1" height="1"/></svg>',
    "attribute url": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect fill="url(https://evil/p)" width="1" height="1"/></svg>',
    "data uri": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect fill="data:text/html,x" width="1" height="1"/></svg>',
    "doctype": b'<!DOCTYPE svg [<!ENTITY a "x">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>',
    "entity bomb": b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>&b;</text></svg>',
    "not an svg": b'<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>',
    "wrong namespace": b'<svg xmlns="http://evil.example/ns" viewBox="0 0 10 10"/>',
    "not xml": b'{"json": true}',
    "no size": b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>',
    "markup in text": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>a &lt;b</text></svg>',
    "too many nodes": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">' + b'<g/>' * (P.MAX_SVG_NODES + 5) + b'</svg>',
    "too large": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><!--' + b'x' * P.MAX_SVG_BYTES + b'--></svg>',
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_svgs_are_refused_whole(name):
    with pytest.raises(P.PropError):
        P.process_prop(HOSTILE[name], ".svg")


def test_svg_size_falls_back_to_width_and_height():
    r = P.process_prop(b'<svg xmlns="http://www.w3.org/2000/svg" width="80px" height="40px"><rect width="80" height="40"/></svg>', ".svg")
    assert (r["width"], r["height"]) == (80.0, 40.0)


def test_default_footprint_in_cells():
    assert P.default_cells(200, 100) == (2.0, 1.0)
    assert P.default_cells(10, 10) == (0.25, 0.25)
    w, h = P.default_cells(5000, 2500)
    assert (w, h) == (20.0, 10.0)
    assert P.default_cells(0, 0)[0] >= 0.25
