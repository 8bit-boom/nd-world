"""The Studio Console page (/studio) embeds Studio in an iframe and links "open Studio directly" - both are
opened by the person's BROWSER. nd-world reaches Studio by the Docker service name (UNSLOTH_URL=http://unsloth:8000),
which only resolves inside the Compose network, so the page showed "Server Not Found" for unsloth:8000 while the same
Studio answered at <host>:8000. The browser address is derived from how the page itself was reached; an explicit
Studio Console URL still wins."""
import re

import pytest

from app import ai as _ai

from .conftest import GM_PASSWORD, login


@pytest.mark.parametrize("server_url, page_host, expected", [
    ("http://unsloth:8000", "192.168.1.216", "http://192.168.1.216:8000"),        # the reported case
    ("http://unsloth:8000/", "nas.lan", "http://nas.lan:8000"),
    ("https://unsloth:8443", "192.168.1.216", "https://192.168.1.216:8443"),
    ("http://unsloth", "192.168.1.216", "http://192.168.1.216"),                  # no port: none invented
    ("http://unsloth:8000", "fe80::1", "http://[fe80::1]:8000"),                  # IPv6 page host
    ("http://studio.example.com:8000", "192.168.1.216", "http://studio.example.com:8000"),   # a real name is kept
    ("http://192.168.1.50:8000", "192.168.1.216", "http://192.168.1.50:8000"),   # an IP is kept
    ("http://localhost:8888", "192.168.1.216", "http://localhost:8888"),          # Desktop install: kept as is
    ("http://host.docker.internal:8000", "192.168.1.216", "http://192.168.1.216:8000"),
    ("http://unsloth:8000", "", "http://unsloth:8000"),                           # nothing to derive from
])
def test_the_browser_address_is_derived_from_the_page_host(monkeypatch, server_url, page_host, expected):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "")
    monkeypatch.setattr(_ai, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(_ai, "effective_llm_url", lambda: server_url)
    assert _ai.studio_browser_url(page_host) == expected


def test_an_explicit_console_url_always_wins(monkeypatch):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "https://studio.example.com/")
    monkeypatch.setattr(_ai, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(_ai, "effective_llm_url", lambda: "http://unsloth:8000")
    assert _ai.studio_browser_url("192.168.1.216") == "https://studio.example.com"


def test_no_key_and_no_override_means_not_configured(monkeypatch):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "")
    monkeypatch.setattr(_ai, "effective_llm_api_key", lambda: "")
    assert _ai.studio_browser_url("192.168.1.216") == ""


def test_the_console_page_frames_an_address_the_browser_can_reach(client, seed, monkeypatch):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "")
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://unsloth:8000")
    login(client, seed.gm.email, GM_PASSWORD)
    html = client.get("/studio", headers={"host": "192.168.1.216:8087"}).text
    assert re.search(r'<iframe src="http://192\.168\.1\.216:8000"', html), "same host as nd-world, Studio's port"
    assert 'href="http://192.168.1.216:8000"' in html
    assert 'src="http://unsloth:8000"' not in html and 'href="http://unsloth:8000"' not in html
    # the page says what it did, and how to override it
    assert "http://unsloth:8000" in html and "Studio Console URL" in html


def test_the_console_page_uses_the_override_as_given(client, seed, monkeypatch):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "http://studio.lan:8888")
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://unsloth:8000")
    login(client, seed.gm.email, GM_PASSWORD)
    html = client.get("/studio", headers={"host": "192.168.1.216:8087"}).text
    assert '<iframe src="http://studio.lan:8888"' in html and "192.168.1.216:8000" not in html


@pytest.mark.parametrize("typed, expected", [
    ("192.168.1.216:8000", "http://192.168.1.216:8000"),      # the reported override: no scheme
    ("studio.lan", "http://studio.lan"),
    ("localhost:8888/", "http://localhost:8888/"),
    ("  nas.lan:8000/studio  ", "http://nas.lan:8000/studio"),
    ("https://studio.example.com", "https://studio.example.com"),
    ("", ""),
    # not a plain host: left alone so the settings route can refuse it
    ("javascript:alert(1)", "javascript:alert(1)"),
    ("data:text/html,x", "data:text/html,x"),
    ("//evil.example", "//evil.example"),
    ("ftp://x", "ftp://x"),
    ("studio", "studio"),
])
def test_a_console_url_typed_without_a_scheme_gets_http(typed, expected):
    assert _ai.normalize_console_url(typed) == expected


def test_an_override_saved_without_a_scheme_by_an_older_build_still_frames_correctly(monkeypatch):
    monkeypatch.setattr(_ai, "get_studio_console_url", lambda: "192.168.1.216:8000")
    assert _ai.studio_browser_url("192.168.1.216") == "http://192.168.1.216:8000"


def test_the_prefs_route_saves_a_bare_host_with_http_and_still_refuses_script_urls(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": "192.168.1.216:8000"}).status_code == 200
    assert _ai.get_studio_console_url() == "http://192.168.1.216:8000"
    for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example", "ftp://x", "studio"):
        assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": bad}).status_code == 400, bad
    assert _ai.get_studio_console_url() == "http://192.168.1.216:8000", "a refused value never replaces the saved one"
    assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": ""}).status_code == 200
