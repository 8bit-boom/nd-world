"""Background picture work sized to the machine (app/workers.py): half the cores for picture jobs (1..6), a second map-tiling worker only
with 16 GB of RAM, both overridable; the thumbnail remake runs that many at once and gives the same result."""
from PIL import Image

from app import thumb_migrate, workers


def test_defaults_follow_the_machine(monkeypatch):
    monkeypatch.delenv("ND_IMAGE_WORKERS", raising=False)
    monkeypatch.delenv("ND_TILE_WORKERS", raising=False)
    assert [workers.image_workers(c) for c in (1, 2, 4, 8, 12, 16, 64)] == [1, 1, 2, 4, 6, 8, 8]
    assert [workers.tile_workers(r) for r in (4, 15.5, 16, 40)] == [1, 1, 2, 2]


def test_the_environment_wins(monkeypatch):
    monkeypatch.setenv("ND_IMAGE_WORKERS", "3")
    monkeypatch.setenv("ND_TILE_WORKERS", "1")
    assert workers.image_workers(16) == 3 and workers.tile_workers(64) == 1
    monkeypatch.setenv("ND_IMAGE_WORKERS", "nonsense")
    assert workers.image_workers(16) == 8


def test_the_remake_with_several_workers(tmp_path):
    for i in range(6):
        p = tmp_path / f"p{i}.png"
        Image.new("RGB", (900, 600), (i * 30, 80, 160)).save(p)
        Image.new("RGB", (440, 293)).save(tmp_path / f"p{i}_thumb.webp")
    assert thumb_migrate.regenerate(tmp_path, workers=3) == {"done": 6, "failed": 0}
    assert sorted(x.name for x in tmp_path.glob("*_thumb.*")) == [f"p{i}_thumb.avif" for i in range(6)]


def test_a_container_cpu_limit_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(workers.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert workers.usable_cpus(str(tmp_path)) == 16                      # no limit: every core it may run on
    (tmp_path / "cpu.max").write_text("400000 100000\n")
    assert workers.usable_cpus(str(tmp_path)) == 4                       # docker `cpus: 4`
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert workers.usable_cpus(str(tmp_path)) == 2                       # 1.5 cores round up
    (tmp_path / "cpu.max").unlink()
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("300000")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000")
    assert workers.usable_cpus(str(tmp_path)) == 3                       # cgroup v1
    assert workers.usable_cpus(str(tmp_path / "nothing")) == 16


def test_settings_override_auto_and_env_overrides_settings(monkeypatch):
    for k in ("ND_IMAGE_WORKERS", "ND_RENDERS", "ND_TILE_WORKERS"):
        monkeypatch.delenv(k, raising=False)
    try:
        workers.apply(image_workers=12, render_slots=3, memory_mb=8 * 1024)
        assert workers.image_workers() == 12 and workers.render_slots() == 3
        assert workers.memory_budget_gb() == 8 and workers.tile_workers() == 1
        assert workers.tiling_fits(40_000_000) and not workers.tiling_fits(500_000_000)
        monkeypatch.setenv("ND_IMAGE_WORKERS", "2")
        assert workers.image_workers() == 2
        workers.apply(memory_mb=32 * 1024)
        assert workers.tile_workers() == 2
    finally:
        workers.apply()
    assert workers.render_slots() == 2


def test_the_pools_follow_the_setting(monkeypatch):
    from app import map_tiles, static_thumbs
    monkeypatch.delenv("ND_IMAGE_WORKERS", raising=False)
    monkeypatch.delenv("ND_TILE_WORKERS", raising=False)
    try:
        workers.apply(image_workers=5, memory_mb=64 * 1024)
        assert static_thumbs._pool._max_workers == 5 and map_tiles._pool._max_workers == 2
        workers.apply(image_workers=1, memory_mb=4 * 1024)
        assert static_thumbs._pool._max_workers == 1 and map_tiles._pool._max_workers == 1
    finally:
        workers.apply()


def test_a_container_memory_limit_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "_ram_gb", lambda: 40.0)
    assert workers.usable_ram_gb(str(tmp_path)) == 40.0
    (tmp_path / "memory.max").write_text("max\n")
    assert workers.usable_ram_gb(str(tmp_path)) == 40.0
    (tmp_path / "memory.max").write_text(str(6 * 2 ** 30))
    assert workers.usable_ram_gb(str(tmp_path)) == 6.0


def test_the_settings_page_and_form(client, seed):
    from app.database import SessionLocal, get_app_settings
    from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    page = client.get("/settings?tab=system").text
    assert 'id="server-resources"' in page and 'name="res_image_workers"' in page and "CPU thread" in page
    try:
        r = client.post("/settings/resources", data={"res_image_workers": "6", "res_render_slots": "99", "res_memory_gb": "12.5"}, follow_redirects=False)
        assert r.status_code == 303
        db = SessionLocal()
        try:
            st = get_app_settings(db)
            assert (st.res_image_workers, st.res_render_slots, st.res_memory_mb) == (6, 16, 12800)
        finally:
            db.close()
        assert workers.image_workers() == 6 and workers.render_slots() == 16
        client.post("/settings/resources", data={"res_image_workers": "", "res_render_slots": "auto", "res_memory_gb": "-3"})
        assert workers.render_slots() == 2 and workers._overrides == {"image_workers": None, "render_slots": None, "memory_mb": None}
    finally:
        workers.apply()
    client.post("/logout"); client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/settings/resources", data={"res_image_workers": "32"}, follow_redirects=False).status_code in (302, 303, 403)
    assert workers.image_workers() != 32
