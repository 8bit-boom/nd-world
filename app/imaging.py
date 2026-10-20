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
THUMBNAIL_SUFFIX = "_thumb.webp"            # a WebP thumbnail (the format of every thumbnail made before AVIF ones)
THUMBNAIL_SUFFIXES = ("_thumb.avif", "_thumb.webp")

# ── the format of the copies this module makes (Settings -> "Thumbnails and opened pictures", AppSettings.preview_format) ─────────────────
# "avif" (the default) - measured ~20-25 % smaller than WebP at the same fidelity: thumbnails 178 KB at 31.2 dB PSNR against 232 KB at
# 30.7 dB, opened pictures 6.8 MB at 37.4 dB against 8.5 MB at 37.8 dB (eight bundled maps and race pictures); encoding takes ~2x longer, once
# per picture. "webp" - the widest support. "original" - an opened picture is the uploaded file itself; grids still get thumbnails (WebP),
# since a page of full-size pictures is what made the home page 25 MB. The app sets this at startup and when the setting is saved.
PREVIEW_FORMATS = ("avif", "webp", "original")
_SPEC = {  # format -> (Pillow name, thumbnail suffix, thumbnail quality, display suffix, display quality)
    "avif": ("AVIF", "_thumb.avif", 60, "_display.avif", 80),
    "webp": ("WEBP", "_thumb.webp", 74, "_display.webp", 90),
}
_preview_format = "avif"


def set_preview_format(fmt: str) -> None:
    global _preview_format
    _preview_format = fmt if fmt in PREVIEW_FORMATS else "avif"


def preview_format() -> str:
    return _preview_format


def _copy_spec(for_display: bool = False):
    """(Pillow format, suffix, quality) for a thumbnail or a display copy in the chosen format - WebP when this Pillow cannot write AVIF,
    and for thumbnails when the choice is "original"."""
    fmt = _preview_format
    if fmt == "avif":
        from PIL import features
        if not features.check("avif"):
            fmt = "webp"
    if fmt == "original":
        fmt = "webp"
    pil, tsuf, tq, dsuf, dq = _SPEC[fmt]
    return (pil, dsuf, dq) if for_display else (pil, tsuf, tq)

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
    """The thumbnail of `path`: the one that exists (`<name>_thumb.avif`, else `<name>_thumb.webp`), or, when there is none yet, the name
    make_thumbnail() would write in the chosen format."""
    for suf in THUMBNAIL_SUFFIXES:
        cand = path.with_name(path.stem + suf)
        if cand.is_file():
            return cand
    return path.with_name(path.stem + _copy_spec()[1])


def thumbnail_paths_for(path: Path) -> list:
    """Every thumbnail name `path` can have."""
    return [path.with_name(path.stem + suf) for suf in THUMBNAIL_SUFFIXES]


def derived_paths_for(path: Path) -> list:
    """Every copy this module can make of `path` - thumbnails and display copies in either format - to delete them with it."""
    return thumbnail_paths_for(path) + [path.with_name(path.stem + suf) for suf in ("_display.avif", "_display.webp")]


def make_thumbnail(path: Path, max_dim: int = THUMBNAIL_MAX_DIM, quality: int = THUMBNAIL_QUALITY) -> Optional[Path]:
    """Write a small WebP preview of `path` alongside it (see
    thumbnail_path_for for the exact name) and return its Path, or None if
    the source isn't a raster format this module handles (svg), or it fails
    to decode — callers should treat that as "no thumbnail available, fall
    back to the full image" rather than an error, same graceful-degradation
    convention as convert_image.

    In the format of the "Thumbnails and opened pictures" setting (AVIF by
    default, see preview_format above), whatever the source's own format.
    Always the first frame only, even for an animated source —
    a moving thumbnail in a dense grid is more distracting than useful, and
    decoding/re-encoding every frame at preview time would multiply the
    encode cost for no benefit a small static frame doesn't already give."""
    if path.suffix.lower() not in _CONVERTIBLE_EXTS:
        return None
    pil, suffix, q = _copy_spec()
    dest = path.with_name(path.stem + suffix)
    try:
        img = Image.open(path)
        img.load()
        frame = img.convert(_frame_mode(img, pil))
        frame.thumbnail((max_dim, max_dim), Image.LANCZOS)
        frame.save(dest, format=pil, quality=q if quality == THUMBNAIL_QUALITY else quality)
    except Exception:
        return None
    for other in thumbnail_paths_for(path):        # a thumbnail in the other format is now stale
        if other != dest:
            other.unlink(missing_ok=True)
    return dest


# ── Display-size copies (efficiency plan, step 13) ─────────────────────────────
# What the lightbox shows: a 2,560 px copy of a large upload in the chosen format (AVIF at quality 80 or WebP at quality 90 - see
# preview_format), made on the first request for it (GET /uploads/...?display=1) and kept beside the original. With "original" there is no
# copy. The original is untouched and one click away ("Full size" in the lightbox).
# 2,560 px: the first version (1,600 px WebP at quality 82, decoded with JPEG's fast downscale) looked over-compressed on a sharp
# screen - 32 dB PSNR on a detailed map against 37 dB now. A copy that would not save at least DISPLAY_MIN_SAVING of the original is not
# kept: the original is shown instead.
DISPLAY_MAX_DIM = 2560
DISPLAY_QUALITY = 90                      # WebP; AVIF uses _SPEC's 80 (the same fidelity)
DISPLAY_MIN_SAVING = 0.3
DISPLAY_SUFFIX = "_display.webp"


def display_path_for(path: Path) -> Path:
    """The name of `path`'s display copy in the chosen format."""
    return path.with_name(path.stem + _copy_spec(for_display=True)[1])


def make_display(path: Path, max_dim: int = DISPLAY_MAX_DIM, quality: Optional[int] = None) -> Optional[Path]:
    """The display copy of `path` (made now if missing, older than the original, or made at an older, smaller size), or None when the
    setting is "original", the original is small enough to show as it is, is animated, can't be read, or the copy would barely be smaller -
    callers then serve the original."""
    if _preview_format == "original":
        return None
    if path.suffix.lower() not in _CONVERTIBLE_EXTS or path.suffix.lower() == ".gif" or path.stem.endswith(("_thumb", "_display")):
        return None
    pil, suffix, q = _copy_spec(for_display=True)
    dest = path.with_name(path.stem + suffix)
    try:
        if dest.is_file() and dest.stat().st_mtime_ns >= path.stat().st_mtime_ns:
            with Image.open(dest) as made:
                if max(made.size) >= max_dim:
                    return dest
            # a copy from the first version (1,600 px): made again below at the current size and quality
        with Image.open(path) as img:
            if max(img.size) <= max_dim or getattr(img, "is_animated", False):
                dest.unlink(missing_ok=True)
                return None
            frame = img.convert(_frame_mode(img, pil))
            frame.thumbnail((max_dim, max_dim), Image.LANCZOS)
            frame.save(dest, format=pil, quality=quality or q, **({"method": 4} if pil == "WEBP" else {}))
        if dest.stat().st_size > path.stat().st_size * (1 - DISPLAY_MIN_SAVING):
            dest.unlink(missing_ok=True)
            return None
    except Exception:
        dest.unlink(missing_ok=True)
        return None
    return dest
