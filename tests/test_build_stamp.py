"""Which build is running? A redeploy that silently reuses an old `:latest` looks exactly like a fix that did
not work, so published images carry the commit they were built from (Dockerfile ARG -> ENV ND_BUILD) and show
it on /health and in Settings > System."""
from pathlib import Path

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


def test_health_reports_the_short_commit(client, monkeypatch):
    monkeypatch.setenv("ND_BUILD", "0f4cfd1a167a394940120c82349e22d49925259d")
    d = client.get("/health").json()
    assert d == {"status": "ok", "build": "0f4cfd1"}


def test_health_says_dev_outside_a_published_image(client, monkeypatch):
    monkeypatch.delenv("ND_BUILD", raising=False)
    assert client.get("/health").json() == {"status": "ok", "build": "dev"}
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
