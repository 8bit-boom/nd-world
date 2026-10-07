"""The table may have no internet: no template may load a script or stylesheet from another host.

Leaflet (the image-map viewer) used to come from unpkg; it is vendored under static/vendor/leaflet now."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# a <script src> or a stylesheet <link>; a GM-chosen theme font (preconnect/Google Fonts) is optional and not a script
TAG = re.compile(r"""<script\b[^>]*?src\s*=\s*["']\s*(?:https?:)?//|<link\b(?=[^>]*rel\s*=\s*["']stylesheet)[^>]*?href\s*=\s*["']\s*https?://(?!fonts\.googleapis\.com)""", re.I)


def test_no_template_loads_a_script_or_stylesheet_from_another_host():
    hits = []
    for f in (ROOT / "app" / "templates").rglob("*.html"):
        for m in TAG.finditer(f.read_text(errors="ignore")):
            hits.append(f"{f.relative_to(ROOT)}: {m.group(0)[:100]}")
    assert not hits, hits


def test_the_vendored_leaflet_is_complete():
    d = ROOT / "static" / "vendor" / "leaflet"
    for name in ("leaflet-1.9.4.js", "leaflet-1.9.4.css", "LICENSE", "images/marker-icon.png", "images/layers.png"):
        assert (d / name).is_file(), name
