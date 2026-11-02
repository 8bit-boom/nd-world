"""Which build is running? A redeploy that silently reuses an old `:latest` looks exactly like a fix that did
not work, so published images carry the commit they were built from (Dockerfile ARG -> ENV ND_BUILD) and show
it on /health and in Settings > System."""
import os
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


def test_health_reports_the_short_commit(client, monkeypatch):
    monkeypatch.setenv("ND_BUILD", "0f4cfd1a167a394940120c82349e22d49925259d")
    d = client.get("/health").json()
    assert d == {"status": "ok", "build": "0f4cfd1", "mode": "python"}


def test_health_says_dev_outside_a_published_image(client, monkeypatch):
    monkeypatch.delenv("ND_BUILD", raising=False)
    assert client.get("/health").json() == {"status": "ok", "build": "dev", "mode": "python"}
    monkeypatch.setenv("ND_BUILD", "  ")
    assert client.get("/health").json()["build"] == "dev"


def test_settings_system_tab_shows_the_build(client, seed, monkeypatch):
    monkeypatch.setenv("ND_BUILD", "abc1234def")
    login(client, seed.gm.email, GM_PASSWORD)
    html = client.get("/settings?tab=system").text
    assert 'id="app-build"' in html and "<code>abc1234</code>" in html and "not a published image" not in html
    monkeypatch.delenv("ND_BUILD")
    html = client.get("/settings?tab=system").text
    assert "<code>dev</code>" in html and "not a published image" in html


def test_the_publish_workflow_bakes_the_commit_into_the_image():
    dockerfile = (ROOT / "Dockerfile").read_text()
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    assert "ARG GIT_SHA" in dockerfile and "ENV ND_BUILD=${GIT_SHA}" in dockerfile
    assert "GIT_SHA=${{ github.sha }}" in workflow and "build-args:" in workflow
    # after the dependency layers, so a new commit does not rebuild them
    assert dockerfile.index("ARG GIT_SHA") > dockerfile.index("pip install")


def test_the_retired_denoise_tag_follows_latest_so_pinned_deployments_keep_updating():
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    assert ":latest-denoise" in workflow and ":latest\n" in workflow


@pytest.mark.skipif(bool(os.environ.get("ND_TEST_BASE_URL")), reason="sets the environment of the app in this process; the gateway pair has its own (the Python-free golden test covers the gateway)")
def test_footer_and_health_say_which_build_and_mode_is_running(client, seed, monkeypatch):
    """Every page's footer (and /health) names the build and how it runs: Python alone, the hybrid image (ND_MODE=hybrid) or the Python-free one."""
    monkeypatch.setenv("ND_BUILD", "abcdef0123456789")
    monkeypatch.delenv("ND_PYTHON_FREE", raising=False)
    login(client, seed.gm.email, GM_PASSWORD)
    for env, mode, label in ((None, "python", "Python"), ("hybrid", "hybrid", "Hybrid (Rust + Python)"), ("rust", "rust", "Rust (no Python)"), ("junk", "python", "Python")):
        if env is None:
            monkeypatch.delenv("ND_MODE", raising=False)
        else:
            monkeypatch.setenv("ND_MODE", env)
        assert client.get("/health").json()["mode"] == mode
        page = client.get("/settings?tab=system").text
        assert f"build abcdef0 \u00b7 {label}</span>" in page and 'id="nd-build"' in page, (mode, page[-400:])
        assert f"running as <strong>{label}</strong>" in page
    monkeypatch.setenv("ND_MODE", "hybrid")
    monkeypatch.setenv("ND_PYTHON_FREE", "1")
    assert client.get("/health").json()["mode"] == "rust"
