"""Turning an uploaded picture of a map object (a bed, a barrel, a wall section) into something safe to put on a map.

A leaf module (no router imports). Two kinds of input:

* Raster pictures (PNG, JPG, WebP, GIF, AVIF, BMP, TIFF): decoded with Pillow, the first frame taken, transparent margins
  trimmed (so an object snaps to the grid by its visible edge), shrunk to at most MAX_SIDE pixels and stored as WebP.
  The original bytes are never served, so a polyglot or a pixel bomb cannot ride along.
* SVG: a vector prop is kept as a vector (it stays sharp when zoomed), but SVG is a document that can carry script, so it
  is rebuilt from an allow-list: only known drawing elements and attributes survive, nothing external is ever referenced,
  and anything unexpected rejects the whole file instead of being "fixed" quietly. It is only ever shown through
  <image>/<img>, which never runs script, and the cleaned file has no script to run if someone opens it directly.
"""
import io
import re
import xml.etree.ElementTree as ET

MAX_RAW_BYTES = 12 * 1024 * 1024        # what one prop upload may weigh
MAX_PIXELS = 40_000_000                 # decoded size guard (Pillow's own bomb check is a warning, not a limit)
MAX_SIDE = 1024                         # stored raster props are shrunk to this
MIN_SIDE = 4
MAX_SVG_BYTES = 512 * 1024
MAX_SVG_NODES = 5000

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff"}
SVG_EXTS = {".svg"}
ALLOWED_EXTS = RASTER_EXTS | SVG_EXTS


class PropError(ValueError):
    """The upload cannot be used; the message is safe to show the uploader."""


# ── raster ───────────────────────────────────────────────────────────────────────────────────────

def _trim(im):
    """Crop transparent margins (alpha <= 8) so the object's visible edge is the picture's edge."""
    alpha = im.getchannel("A")
    box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    if box is None:
        raise PropError("The picture is completely transparent")
    return im.crop(box)


def process_raster(raw: bytes):
    """-> (webp_bytes, width, height). Raises PropError."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if len(raw) > MAX_RAW_BYTES:
        raise PropError("That file is too large for a map object")
    try:
        probe = Image.open(io.BytesIO(raw))
        w0, h0 = probe.size
        if w0 * h0 > MAX_PIXELS:
            raise PropError("That picture has too many pixels")
        probe.seek(0)
        im = ImageOps.exif_transpose(probe)
        im = im.convert("RGBA")
    except PropError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PropError("That file is not a picture this app can read")
    im = _trim(im)
    if min(im.size) < MIN_SIDE:
        raise PropError("The object is too small to use")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=92, method=4)
    return out.getvalue(), im.size[0], im.size[1]


# ── SVG ──────────────────────────────────────────────────────────────────────────────────────────

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"

_ELEMENTS = {
    "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan",
    "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern", "title", "desc",
}
_ATTRS = {
    "id", "class", "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "points",
    "transform", "viewBox", "preserveAspectRatio", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit",
    "opacity", "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "fx", "fy",
    "clip-path", "clip-rule", "mask", "patternUnits", "patternTransform", "patternContentUnits", "maskUnits",
    "clipPathUnits", "font-size", "font-family", "font-weight", "text-anchor", "dominant-baseline", "dx", "dy",
    "href", "style", "version", "display", "visibility", "color",
}
# only a same-document reference may appear in a URL-ish value
_BAD_VALUE = re.compile(r"(javascript:|data:|vbscript:|expression\s*\(|@import|<|>|\\)", re.I)
_URL_REF = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
_STYLE_PROP = re.compile(r"^\s*(fill|fill-opacity|stroke|stroke-width|stroke-opacity|stroke-linecap|stroke-linejoin|"
                         r"stroke-dasharray|opacity|stop-color|stop-opacity|font-size|font-family|font-weight|display|"
                         r"visibility|clip-path|mask)\s*:\s*([^;]*)$", re.I)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _check_value(attr: str, value: str):
    if _BAD_VALUE.search(value):
        raise PropError(f"The SVG contains something that is not allowed ({attr})")
    for m in _URL_REF.finditer(value):
        if not m.group(2).startswith("#"):
            raise PropError("The SVG refers to something outside itself")


def _clean_style(value: str) -> str:
    keep = []
    for decl in value.split(";"):
        if not decl.strip():
            continue
        m = _STYLE_PROP.match(decl)
        if not m:
            raise PropError("The SVG uses styling that is not allowed")
        _check_value("style", m.group(2))
        keep.append(f"{m.group(1).lower()}:{m.group(2).strip()}")
    return ";".join(keep)


def sanitize_svg(raw: bytes) -> bytes:
    """The SVG rebuilt from an allow-list, or PropError. Never returns the input unchanged by accident: every node and
    attribute is copied across only if it is on the list."""
    if len(raw) > MAX_SVG_BYTES:
        raise PropError("That SVG is too large")
    low = raw.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise PropError("That SVG declares entities, which are not allowed")
    try:
        parser = ET.XMLParser()
        root = ET.fromstring(raw, parser=parser)
    except ET.ParseError:
        raise PropError("That SVG is not valid")
    if _local(root.tag) != "svg" or (root.tag.startswith("{") and root.tag.split("}")[0][1:] != _SVG_NS):
        raise PropError("That is not an SVG drawing")

    count = [0]

    def clean(node):
        count[0] += 1
        if count[0] > MAX_SVG_NODES:
            raise PropError("That SVG is too complicated")
        tag = _local(node.tag)
        if tag not in _ELEMENTS:
            raise PropError(f"The SVG contains an element that is not allowed ({tag})")
        out = ET.Element(tag)
        for key, value in node.attrib.items():
            name = _local(key)
            if key.startswith("{") and key.split("}")[0][1:] not in (_XLINK, ""):
                continue                                   # editor metadata (inkscape:, sodipodi:...) is dropped
            if name.startswith("xmlns") or name in ("space",):
                continue
            if name not in _ATTRS:
                if name.startswith(("data-", "aria-")):
                    continue
                raise PropError(f"The SVG contains an attribute that is not allowed ({name})")
            if name == "href":
                raise PropError("The SVG refers to something outside itself")
            elif name == "style":
                value = _clean_style(value)
            else:
                _check_value(name, value)
            out.set(name, value)
        if tag == "svg" and not out.get("viewBox") and not (out.get("width") and out.get("height")):
            raise PropError("The SVG has no size (add a viewBox)")
        if tag in ("text", "tspan", "title", "desc") and node.text:
            if len(node.text) > 500 or "<" in node.text:
                raise PropError("The SVG contains text that is not allowed")
            out.text = node.text
        for child in node:
            if isinstance(child.tag, str):
                out.append(clean(child))
        return out

    cleaned = clean(root)
    cleaned.set("xmlns", _SVG_NS)
    return ET.tostring(cleaned, encoding="utf-8", xml_declaration=True)


def svg_size(clean_svg: bytes):
    """Intrinsic (width, height) of a cleaned SVG from its viewBox, else width/height, else a 100x100 default."""
    root = ET.fromstring(clean_svg)
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        if len(vb) == 4 and float(vb[2]) > 0 and float(vb[3]) > 0:
            return float(vb[2]), float(vb[3])
    except ValueError:
        pass
    def num(v):
        m = re.match(r"^\s*([0-9.]+)", v or "")
        return float(m.group(1)) if m else 0.0
    w, h = num(root.get("width")), num(root.get("height"))
    return (w, h) if w > 0 and h > 0 else (100.0, 100.0)


# ── entry point ──────────────────────────────────────────────────────────────────────────────────

def process_prop(raw: bytes, ext: str):
    """-> dict(data, ext, width, height) ready to store. `ext` is the uploaded name's extension, lower case."""
    ext = (ext or "").lower()
    if ext not in ALLOWED_EXTS:
        raise PropError("Use PNG, JPG, WebP, GIF, AVIF, BMP, TIFF or SVG")
    if ext in SVG_EXTS:
        clean_svg = sanitize_svg(raw)
        w, h = svg_size(clean_svg)
        return {"data": clean_svg, "ext": ".svg", "width": w, "height": h}
    data, w, h = process_raster(raw)
    return {"data": data, "ext": ".webp", "width": w, "height": h}


def default_cells(width: float, height: float, per_cell_px: float = 100.0):
    """A sensible footprint in grid cells for a new prop: its pixel size at ~100 px per square, between a quarter of a
    square and 20 squares on the long side, aspect kept."""
    w, h = max(1.0, float(width)), max(1.0, float(height))
    long_side = min(20.0, max(0.25, max(w, h) / per_cell_px))
    scale = long_side / max(w, h)
    q = lambda v: max(0.25, round(v * 4) / 4)
    return q(w * scale), q(h * scale)
