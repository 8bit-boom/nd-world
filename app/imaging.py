"""Post-processing for uploaded portraits/entity images: convert to AVIF or WebP."""
from pathlib import Path

from PIL import Image

CONVERT_QUALITY = 90

# Only rasterized formats get converted at all. SVG is vector — re-saving
# would rasterize it, losing scalability — so it's excluded outright. GIF,
# WebP, and PNG (APNG) can each be *animated*; that's checked at runtime via
# Pillow's is_animated rather than guessed from the extension, since WebP and
# PNG are just as often static as GIF is animated. A plain .save() would
# flatten any of them to their first frame, silently losing the animation.
_CONVERTIBLE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_PILLOW_FORMAT = {"avif": "AVIF", "webp": "WEBP"}


def _frame_mode(frame: "Image.Image") -> str:
    if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
        return "RGBA"
    return "RGB"


def convert_image(
    path: Path,
    static_format: str = "avif",
    animated_format: str = "avif",
    quality: int = CONVERT_QUALITY,
) -> Path:
    """Convert an uploaded image in place to the configured target format —
    "avif" or "webp", chosen independently for static vs animated sources —
    and return the new path. Returns the original path unchanged if the
    source format isn't convertible at all (svg), if the relevant format
    choice is "none", if the source is already in that exact format, or if
    the image fails to decode — a corrupt/unsupported upload shouldn't break
    the calling upload flow, it just keeps whatever was saved."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return path
    try:
        img = Image.open(path)
        animated = getattr(img, "is_animated", False)
        target = animated_format if animated else static_format
        pillow_format = _PILLOW_FORMAT.get(target)
        if not pillow_format:
            return path
        dest = path.with_suffix("." + target)
        if dest.resolve() == path.resolve():
            return path  # already saved as the target format
        if animated:
            frames, durations = [], []
            for i in range(img.n_frames):
                img.seek(i)
                frame = img.convert(_frame_mode(img))
                frames.append(frame.copy())
                durations.append(img.info.get("duration", 100))
            frames[0].save(
                dest, format=pillow_format, save_all=True, append_images=frames[1:],
                duration=durations, loop=img.info.get("loop", 0), quality=quality,
            )
        else:
            img.load()
            img = img.convert(_frame_mode(img))
            img.save(dest, format=pillow_format, quality=quality)
    except Exception:
        return path
    path.unlink(missing_ok=True)
    return dest
