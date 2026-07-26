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


def _frame_mode(frame: "Image.Image") -> str:
    if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
        return "RGBA"
    return "RGB"


def convert_to_avif(
    path: Path,
    quality: int = AVIF_QUALITY,
    convert_static: bool = True,
    convert_animated: bool = True,
) -> Path:
    """Convert an uploaded image in place to AVIF and return the new path.
    Returns the original path unchanged if the format isn't convertible, if
    the relevant convert_static/convert_animated flag is off, or if the
    image fails to decode — a corrupt/unsupported upload shouldn't break the
    calling upload flow, it just keeps whatever was saved."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return path
    try:
        img = Image.open(path)
        animated = getattr(img, "is_animated", False)
        if animated:
            if not convert_animated:
                return path
            dest = path.with_suffix(".avif")
            frames, durations = [], []
            for i in range(img.n_frames):
                img.seek(i)
                frame = img.convert(_frame_mode(img))
                frames.append(frame.copy())
                durations.append(img.info.get("duration", 100))
            frames[0].save(
                dest, format="AVIF", save_all=True, append_images=frames[1:],
                duration=durations, loop=img.info.get("loop", 0), quality=quality,
            )
        else:
            if not convert_static:
                return path
            img.load()
            img = img.convert(_frame_mode(img))
            dest = path.with_suffix(".avif")
            img.save(dest, format="AVIF", quality=quality)
    except Exception:
        return path
    path.unlink(missing_ok=True)
    return dest
