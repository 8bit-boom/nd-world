"""Post-processing for uploaded portraits/entity images: convert to AVIF."""
from pathlib import Path

from PIL import Image

AVIF_QUALITY = 90

# Only rasterized, non-animated formats get converted. SVG is vector (re-saving
# would rasterize it, losing scalability) and GIF may be animated (Pillow's
# plain .save() would flatten it to a single frame) — both are left as-is.
_CONVERTIBLE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def convert_to_avif(path: Path, quality: int = AVIF_QUALITY) -> Path:
    """Convert an uploaded image in place to AVIF and return the new path.
    Returns the original path unchanged if the format isn't convertible, or
    if the image fails to decode — a corrupt/unsupported upload shouldn't
    break the calling upload flow, it just keeps whatever was saved."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return path
    try:
        img = Image.open(path)
        img.load()
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        dest = path.with_suffix(".avif")
        img.save(dest, format="AVIF", quality=quality)
    except Exception:
        return path
    path.unlink(missing_ok=True)
    return dest
