"""Post-processing for uploaded portraits/entity images: convert to AVIF,
WebP, PNG, or JPEG, and generate a small preview variant for gallery/list
grids."""
from pathlib import Path
from typing import Optional

from PIL import Image

CONVERT_QUALITY = 90
THUMBNAIL_QUALITY = 74
# Longest edge of a generated preview, in pixels — chosen to look sharp at
# the ~140-260px CSS box every gallery/list grid actually renders these at
# (even on a 2x-DPR display), while landing an order of magnitude smaller in
# bytes than a full SwarmUI/ComfyUI generation (often several MB) or an
# unresized phone-camera upload.
THUMBNAIL_MAX_DIM = 440
THUMBNAIL_SUFFIX = "_thumb.webp"

# Only rasterized formats get converted at all. SVG is vector — re-saving
# would rasterize it, losing scalability — so it's excluded outright. GIF,
# WebP, PNG (APNG), and AVIF can each be *animated*; that's checked at
# runtime via Pillow's is_animated rather than guessed from the extension,
# since WebP/PNG/AVIF are just as often static as GIF is animated. A plain
# .save() would flatten any of them to their first frame, silently losing
# the animation. AVIF is included here (unlike the original avif/webp-only
# upload pipeline, which never produces an avif *source* — uploads are
# validated against ALLOWED_EXTS, which excludes it) so the retroactive bulk
# converter (convert_image_to) can re-encode a world's already-avif images
# into another format too, not just convert one-way into avif.
_CONVERTIBLE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

_PILLOW_FORMAT = {"avif": "AVIF", "webp": "WEBP", "png": "PNG", "jpg": "JPEG", "jpeg": "JPEG"}
# File extension to save each target format under — "jpeg" still lands on
# the conventional ".jpg" extension.
_TARGET_EXT = {"avif": "avif", "webp": "webp", "png": "png", "jpg": "jpg", "jpeg": "jpg"}
# Formats Pillow can save as multi-frame here. JPEG has no animation support
# at all, so an animated source targeting jpg/jpeg is intentionally flattened
# to its first frame instead of erroring.
_ANIMATABLE_FORMATS = {"AVIF", "WEBP", "PNG"}


def _frame_mode(frame: "Image.Image", pillow_format: str) -> str:
    if pillow_format == "JPEG":
        return "RGB"  # JPEG has no alpha channel
    if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
        return "RGBA"
    return "RGB"


def _convert(path: Path, target_format: str, quality: int) -> Optional[Path]:
    """Shared conversion core. Returns the new Path (not yet unlinking the
    source) on success, or None if the source isn't convertible, the target
    format is unrecognized, the source is already saved as that exact
    format, or the image fails to decode."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return None
    pillow_format = _PILLOW_FORMAT.get(target_format)
    ext = _TARGET_EXT.get(target_format)
    if not pillow_format or not ext:
        return None
    dest = path.with_suffix("." + ext)
    if dest.resolve() == path.resolve():
        return None  # already saved as the target format
    try:
        img = Image.open(path)
        animated = getattr(img, "is_animated", False) and pillow_format in _ANIMATABLE_FORMATS
        save_kwargs = {"optimize": True} if pillow_format == "PNG" else {"quality": quality}
        if animated:
            frames, durations = [], []
            for i in range(img.n_frames):
                img.seek(i)
                frame = img.convert(_frame_mode(img, pillow_format))
                frames.append(frame.copy())
                durations.append(img.info.get("duration", 100))
            frames[0].save(
                dest, format=pillow_format, save_all=True, append_images=frames[1:],
                duration=durations, loop=img.info.get("loop", 0), **save_kwargs,
            )
        else:
            img.load()
            img = img.convert(_frame_mode(img, pillow_format))
            img.save(dest, format=pillow_format, **save_kwargs)
    except Exception:
        return None
    return dest


def convert_image(
    path: Path,
    static_format: str = "avif",
    animated_format: str = "avif",
    quality: int = CONVERT_QUALITY,
) -> Path:
    """Convert an uploaded image in place to the configured target format —
    chosen independently for static vs animated sources — and return the new
    path. Returns the original path unchanged if the source format isn't
    convertible at all (svg), if the relevant format choice is "none", if
    the image is already in that exact format, or if the image fails to
    decode — a corrupt/unsupported upload shouldn't break the calling upload
    flow, it just keeps whatever was saved."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return path
    try:
        animated = getattr(Image.open(path), "is_animated", False)
    except Exception:
        return path
    target = animated_format if animated else static_format
    if target not in _PILLOW_FORMAT:
        return path
    dest = _convert(path, target, quality)
    if dest is None:
        return path
    path.unlink(missing_ok=True)
    return dest


def convert_image_to(path: Path, target_format: str, quality: int = CONVERT_QUALITY) -> Optional[Path]:
    """Retroactively convert an already-uploaded image to a specific target
    format ("avif" | "webp" | "png" | "jpg" | "jpeg") and quality, replacing
    the original file on disk. Returns the new Path on success, or None if
    nothing changed (unconvertible source, unknown format, already saved as
    that format, or decode failure) — the caller should leave the existing
    reference untouched in that case."""
    dest = _convert(path, target_format, quality)
    if dest is None:
        return None
    path.unlink(missing_ok=True)
    return dest


def thumbnail_path_for(path: Path) -> Path:
    """The predictable sibling filename make_thumbnail() writes/would write
    for `path` — e.g. .../abc123.avif -> .../abc123_thumb.webp. Pure string
    manipulation, no filesystem access, so callers (including the Jinja
    thumb_url() global in app/main.py) can compute this for any known image
    path without needing a DB column to remember it."""
    return path.with_name(path.stem + THUMBNAIL_SUFFIX)


def make_thumbnail(path: Path, max_dim: int = THUMBNAIL_MAX_DIM, quality: int = THUMBNAIL_QUALITY) -> Optional[Path]:
    """Write a small WebP preview of `path` alongside it (see
    thumbnail_path_for for the exact name) and return its Path, or None if
    the source isn't a raster format this module handles (svg), or it fails
    to decode — callers should treat that as "no thumbnail available, fall
    back to the full image" rather than an error, same graceful-degradation
    convention as convert_image.

    Always WebP regardless of the source's own format/AppSettings choice —
    a preview grid doesn't need to match the source's format, just be small
    and universally displayable, so there's no reason to duplicate the
    avif/webp static_format branching convert_image already does for the
    real asset. Always the first frame only, even for an animated source —
    a moving thumbnail in a dense grid is more distracting than useful, and
    decoding/re-encoding every frame at preview time would multiply the
    encode cost for no benefit a small static frame doesn't already give."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return None
    dest = thumbnail_path_for(path)
    try:
        img = Image.open(path)
        img.load()
        frame = img.convert(_frame_mode(img, "WEBP"))
        frame.thumbnail((max_dim, max_dim), Image.LANCZOS)
        frame.save(dest, format="WEBP", quality=quality)
    except Exception:
        return None
    return dest
