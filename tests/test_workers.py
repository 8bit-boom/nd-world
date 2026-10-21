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
