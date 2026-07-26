"""Post-processing for uploaded portraits/entity images: convert to AVIF."""
from pathlib import Path

from PIL import Image

AVIF_QUALITY = 90

# Only rasterized formats get converted at all. SVG is vector — re-saving
# would rasterize it, losing scalability — so it's excluded outright. GIF,
# WebP, and PNG (APNG) can each be *animated*; that's checked at runtime via
# Pillow's is_animated rather than guessed from the extension, since WebP and
# PNG are just as often static as GIF is animated. A plain .save() would
# flatten any of them to their first frame, silently losing the animation.
_CONVERTIBLE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def convert_to_avif(path: Path, quality: int = AVIF_QUALITY) -> Path:
    """Convert an uploaded image in place to AVIF and return the new path.
    Returns the original path unchanged if the format isn't convertible, if
    it's an animated image, or if it fails to decode — a corrupt/unsupported/
    animated upload shouldn't break the calling upload flow, it just keeps
    whatever was saved."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return path
    try:
        img = Image.open(path)
        if getattr(img, "is_animated", False):
            return path
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
