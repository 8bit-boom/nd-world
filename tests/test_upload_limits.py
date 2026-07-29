"""Regression tests for upload hardening: before app/uploads.py existed,
shutil.copyfileobj streamed portrait/entity-image uploads to disk with no size
limit at all, so a single oversized upload could fill the /data volume and
take the whole app down (SQLite writes fail once the disk is full).
"""
import io

from .conftest import PLAYER_PASSWORD, login

# conftest.py sets MAX_UPLOAD_BYTES=1048576 (1 MiB) before any app module is
# imported, so this only needs to be a couple hundred KB over that.
_OVERSIZED_BYTES = 1_048_576 + 200_000


def test_oversized_portrait_upload_rejected(client, seed):
    from app.main import UPLOADS_DIR

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    big_file = io.BytesIO(b"\x00" * _OVERSIZED_BYTES)
    r = client.post(
        "/characters/new",
        data={"name": "Big Portrait Guy"},
        files={"portrait": ("huge.png", big_file, "image/png")},
    )
    assert r.status_code == 413

    portraits_dir = UPLOADS_DIR / "portraits"
    leftover = list(portraits_dir.glob("*")) if portraits_dir.exists() else []
    assert leftover == [], f"oversized upload left partial file(s) behind: {leftover}"


def test_normal_sized_portrait_upload_succeeds(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    small_file = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)
    r = client.post(
        "/characters/new",
        data={"name": "Normal Portrait Guy"},
        files={"portrait": ("small.png", small_file, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
