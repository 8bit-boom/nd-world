"""Unit tests for app/imaging.py: the AVIF/WebP-only upload-time pipeline
(convert_image) and its PNG/JPEG-aware, retroactive sibling
(convert_image_to) used by the bulk image-format-conversion feature, plus
make_thumbnail/thumbnail_path_for — the small WebP preview variant gallery
and list grids use instead of a full-resolution image (see
app.templating.thumb_url, which resolves one of these if it exists).
"""
import io

import pytest
from PIL import Image

from app.imaging import (
    THUMBNAIL_MAX_DIM, convert_image, convert_image_to, make_thumbnail, thumbnail_path_for,
)


def _write_image(path, fmt="PNG", size=(16, 16), color=(200, 50, 50), animated=False):
    if animated:
        frames = [Image.new("RGB", size, color), Image.new("RGB", size, (50, 200, 50))]
        frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    else:
        Image.new("RGB", size, color).save(path, format=fmt)
    return path


@pytest.mark.parametrize("target,expected_ext,expected_format", [
    ("avif", ".avif", "AVIF"),
    ("webp", ".webp", "WEBP"),
    ("jpg", ".jpg", "JPEG"),
    ("jpeg", ".jpg", "JPEG"),
])
def test_convert_image_to_each_target_format(tmp_path, target, expected_ext, expected_format):
    src = _write_image(tmp_path / "src.png")
    dest = convert_image_to(src, target, quality=80)
    assert dest is not None
    assert dest.suffix == expected_ext
    assert dest.is_file()
    assert not src.exists()
    with Image.open(dest) as img:
        assert img.format == expected_format


def test_convert_image_to_png_target_from_jpeg_source(tmp_path):
    src = _write_image(tmp_path / "src.jpg", fmt="JPEG")
    dest = convert_image_to(src, "png", quality=80)
    assert dest is not None
    assert dest.suffix == ".png"
    assert dest.is_file()
    assert not src.exists()
    with Image.open(dest) as img:
        assert img.format == "PNG"


def test_convert_image_to_unknown_format_returns_none(tmp_path):
    src = _write_image(tmp_path / "src.png")
    assert convert_image_to(src, "bmp", quality=80) is None
    assert src.exists()  # untouched


def test_convert_image_to_svg_source_returns_none(tmp_path):
    src = tmp_path / "src.svg"
    src.write_text("<svg></svg>")
    assert convert_image_to(src, "png", quality=80) is None
    assert src.exists()


def test_convert_image_to_already_target_format_returns_none(tmp_path):
    src = _write_image(tmp_path / "src.png")
    assert convert_image_to(src, "png", quality=80) is None
    assert src.exists()


def test_convert_image_to_corrupt_file_returns_none(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)  # signature only, not decodable
    assert convert_image_to(src, "webp", quality=80) is None
    assert src.exists()


def test_convert_image_to_animated_webp_preserves_frames(tmp_path):
    src = _write_image(tmp_path / "src.gif", fmt="GIF", animated=True)
    dest = convert_image_to(src, "webp", quality=80)
    assert dest is not None
    with Image.open(dest) as img:
        assert getattr(img, "is_animated", False)
        assert img.n_frames == 2


def test_convert_image_to_animated_source_to_jpg_flattens_to_one_frame(tmp_path):
    """JPEG has no animation support at all — an animated source targeting
    jpg must not error, it just keeps the first frame."""
    src = _write_image(tmp_path / "src.gif", fmt="GIF", animated=True)
    dest = convert_image_to(src, "jpg", quality=80)
    assert dest is not None
    with Image.open(dest) as img:
        assert img.format == "JPEG"
        assert not getattr(img, "is_animated", False)


def test_convert_image_to_rgba_png_to_jpg_drops_alpha(tmp_path):
    src = tmp_path / "src.png"
    Image.new("RGBA", (10, 10), (255, 0, 0, 128)).save(src, format="PNG")
    dest = convert_image_to(src, "jpg", quality=80)
    assert dest is not None
    with Image.open(dest) as img:
        assert img.mode == "RGB"


def test_convert_image_to_quality_affects_file_size(tmp_path):
    # Higher quality JPEG output should generally be larger for a photo-like
    # (noisy) source than a heavily-compressed low-quality one.
    src = _write_image(tmp_path / "src.png", size=(64, 64))
    src_low = tmp_path / "low.png"
    src_low.write_bytes(src.read_bytes())
    hi = convert_image_to(src, "jpg", quality=95)
    lo = convert_image_to(src_low, "jpg", quality=10)
    assert hi is not None and lo is not None
    assert hi.stat().st_size >= lo.stat().st_size


def test_convert_image_upload_pipeline_unchanged_for_avif_webp(tmp_path):
    """Backward-compat guard: the original upload-time convert_image() API
    (avif/webp only, static vs animated split) must keep behaving exactly as
    before after the PNG/JPEG refactor."""
    src = _write_image(tmp_path / "src.png")
    dest = convert_image(src, static_format="webp", animated_format="avif", quality=85)
    assert dest.suffix == ".webp"
    assert dest.is_file()
    assert not src.exists()


def test_convert_image_none_format_is_a_noop(tmp_path):
    src = _write_image(tmp_path / "src.png")
    dest = convert_image(src, static_format="none", animated_format="none")
    assert dest == src
    assert src.exists()


# ── make_thumbnail / thumbnail_path_for ─────────────────────────────────────

def test_thumbnail_path_for_is_pure_string_manipulation(tmp_path):
    src = tmp_path / "abc123.avif"
    assert thumbnail_path_for(src) == tmp_path / "abc123_thumb.webp"
    # No filesystem access — works even for a path that doesn't exist yet.
    assert not thumbnail_path_for(src).exists()


def test_make_thumbnail_writes_a_bounded_webp_alongside_the_source(tmp_path):
    src = _write_image(tmp_path / "src.png", size=(2000, 1000))
    thumb = make_thumbnail(src)
    assert thumb == thumbnail_path_for(src)
    assert thumb.is_file()
    assert src.exists()  # unlike convert_image, the source is never removed
    with Image.open(thumb) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= THUMBNAIL_MAX_DIM
        assert img.size[0] / img.size[1] == pytest.approx(2000 / 1000, rel=0.02)  # aspect ratio preserved


def test_make_thumbnail_is_a_noop_for_an_image_already_smaller_than_max_dim(tmp_path):
    src = _write_image(tmp_path / "src.png", size=(50, 30))
    thumb = make_thumbnail(src)
    with Image.open(thumb) as img:
        assert img.size == (50, 30)  # Pillow's thumbnail() never upscales


def test_make_thumbnail_defaults_to_first_frame_only_for_animated_source(tmp_path):
    src = _write_image(tmp_path / "src.gif", fmt="GIF", animated=True)
    thumb = make_thumbnail(src)
    assert thumb is not None
    with Image.open(thumb) as img:
        assert not getattr(img, "is_animated", False)


def test_make_thumbnail_svg_source_returns_none(tmp_path):
    src = tmp_path / "src.svg"
    src.write_text("<svg></svg>")
    assert make_thumbnail(src) is None


def test_make_thumbnail_corrupt_source_returns_none_not_raises(tmp_path):
    src = tmp_path / "bad.png"
    src.write_bytes(b"not an image")
    assert make_thumbnail(src) is None


def test_make_thumbnail_custom_max_dim(tmp_path):
    src = _write_image(tmp_path / "src.png", size=(2000, 2000))
    thumb = make_thumbnail(src, max_dim=100)
    with Image.open(thumb) as img:
        assert img.size == (100, 100)
