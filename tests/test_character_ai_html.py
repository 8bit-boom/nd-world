"""The player AI character creator can import a sheet saved as .html / .htm.

Plenty of character sheets live as web pages (a "Save page as…" from an online sheet, a fillable HTML sheet, a
printout). The creator already took PDF / MD / TXT / JSON / images. An HTML sheet is read as TEXT — scripts and
styles dropped, headings / lists / tables kept readable, and the VALUES typed into form fields (which are attributes,
not text, so a plain tag-stripper loses exactly the filled-in sheet) pulled out next to their field names."""
import io
import time

import pytest

from app import ai as ai_module
from app.rendering import html_to_sheet_text
from app.routers import character_ai

from .conftest import PLAYER_PASSWORD, login

SHEET = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>Bardoc the Bold — Character Sheet</title>
<style>.hp { color: red } body { font-family: serif }</style>
<script>window.secret = "SCRIPT-CONTENT-MUST-NOT-APPEAR"; function roll() { return 4 }</script>
</head><body>
<h1>Bardoc the Bold</h1>
<p>Race: <b>Half-elf</b> &amp; Class: Bard</p>
<h2>Attributes</h2>
<table>
  <tr><th>STR</th><th>DEX</th><th>CHA</th></tr>
  <tr><td>10</td><td>16</td><td>14</td></tr>
</table>
<ul><li>Rapier</li><li>Lute &mdash; masterwork</li></ul>
<form>
  <label>Player <input type="text" name="player_name" value="Archie"></label>
  <input type="number" id="level" value="3">
  <input type="checkbox" name="inspired" checked> <input type="checkbox" name="exhausted">
  <input type="hidden" name="csrf" value="HIDDEN-TOKEN-MUST-NOT-APPEAR">
  <input type="password" name="pw" value="PASSWORD-MUST-NOT-APPEAR">
  <select name="alignment"><option>Lawful</option><option selected>Chaotic good</option></select>
  <textarea name="backstory">Grew up on the docks.
Sold a song to a duke.</textarea>
  <button type="submit">Save</button>
</form>
<noscript>NOSCRIPT-MUST-NOT-APPEAR</noscript>
<svg><text>SVG-MUST-NOT-APPEAR</text></svg>
</body></html>"""


# ── the text extractor ──────────────────────────────────────────────────────────────────────────

def test_visible_text_is_kept_in_a_readable_shape():
    t = html_to_sheet_text(SHEET)
    assert "Bardoc the Bold" in t and "Half-elf & Class: Bard" in t.replace("\n", " ")
    assert "# Bardoc the Bold" in t and "## Attributes" in t
    assert "- Rapier" in t and "- Lute — masterwork" in t
    # the table keeps its rows so a stat sits next to its label
    assert "STR | DEX | CHA" in t and "10 | 16 | 14" in t


def test_scripts_styles_and_hidden_things_are_dropped():
    t = html_to_sheet_text(SHEET)
    for banned in ("SCRIPT-CONTENT", "function roll", "font-family", "NOSCRIPT", "SVG-MUST", "HIDDEN-TOKEN", "PASSWORD-MUST"):
        assert banned not in t, banned


def test_values_typed_into_form_fields_are_read():
    t = html_to_sheet_text(SHEET)
    assert "player_name: Archie" in t
    assert "level: 3" in t
    assert "alignment: Chaotic good" in t
    assert "backstory: Grew up on the docks.\nSold a song to a duke." in t
    assert "[x] inspired" in t and "[ ] exhausted" in t


def test_survives_broken_markup_and_empty_input():
    assert "Hello" in html_to_sheet_text("<p>Hello <b>world")
    assert html_to_sheet_text("") == ""
    assert html_to_sheet_text("<script>only()</script><style>x{}</style>").strip() == ""


def test_a_huge_page_is_capped_with_a_note():
    t = html_to_sheet_text("<p>" + ("word " * 100_000) + "</p>")
    assert len(t) < 70_000 and t.rstrip().endswith("(truncated)")


# ── the upload path ─────────────────────────────────────────────────────────────────────────────

def _decode(data):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(character_ai._extract_file_text(data, ".html", ""))


@pytest.mark.parametrize("ext", [".html", ".htm", ".HTML", ".Htm"])
def test_extract_handles_html_extensions(ext):
    import asyncio
    text = asyncio.new_event_loop().run_until_complete(character_ai._extract_file_text(SHEET.encode(), ext, ""))
    assert "Bardoc the Bold" in text and "player_name: Archie" in text


def test_non_utf8_pages_are_decoded_by_their_declared_charset():
    page = '<html><head><meta charset="windows-1252"></head><body><p>Café Müller, naïve</p></body></html>'
    assert "Café Müller, naïve" in _decode(page.encode("cp1252"))
    assert "Café" in _decode(("﻿" + "<p>Café</p>").encode("utf-8"))                    # BOM


def test_a_page_with_no_readable_text_gets_an_actionable_error():
    import asyncio
    with pytest.raises(ValueError) as e:
        asyncio.new_event_loop().run_until_complete(
            character_ai._extract_file_text(b"<html><body><script>render()</script></body></html>", ".htm", ""))
    assert "no readable text" in str(e.value).lower() and "PDF" in str(e.value)


def test_unsupported_type_message_now_lists_html():
    import asyncio
    with pytest.raises(ValueError) as e:
        asyncio.new_event_loop().run_until_complete(character_ai._extract_file_text(b"x", ".xyz", ""))
    assert "HTML" in str(e.value)


def _patch_gen(monkeypatch, seen):
    async def fake_generate_chat(messages, **kw):
        seen["user_text"] = messages[0]["content"]
        return '{"name": "Bardoc", "race": "Half-elf", "char_class": "Bard", "level": 3, "stats": {}, "backstory": "x"}'

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")


def _poll(client, job_id, seconds=10):
    end = time.time() + seconds
    data = {"status": "running"}
    while time.time() < end:
        data = client.get(f"/api/characters/ai/{job_id}").json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    return data


@pytest.mark.parametrize("filename", ["bardoc.html", "bardoc.htm"])
def test_an_uploaded_html_sheet_reaches_the_generator_as_text(client, seed, monkeypatch, filename):
    seen = {}
    _patch_gen(monkeypatch, seen)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    files={"file": (filename, io.BytesIO(SHEET.encode()), "text/html")},
                    data={"prompt": "convert this to the world's rules", "use_rag": "false"})
    assert r.status_code == 200, r.text
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    sent = seen["user_text"]
    assert "SOURCE SHEET" in sent and "player_name: Archie" in sent and "10 | 16 | 14" in sent
    assert "SCRIPT-CONTENT" not in sent and "HIDDEN-TOKEN" not in sent


def test_html_alone_is_enough_no_prompt_needed(client, seed, monkeypatch):
    _patch_gen(monkeypatch, {})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", files={"file": ("s.html", io.BytesIO(SHEET.encode()), "text/html")},
                    data={"prompt": "", "use_rag": "false"})
    assert r.status_code == 200, r.text


def test_an_empty_script_only_page_is_a_clean_400(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    files={"file": ("s.html", io.BytesIO(b"<html><script>x()</script></html>"), "text/html")},
                    data={"prompt": ""})
    assert r.status_code == 400 and "readable text" in r.json()["detail"]


def test_the_page_offers_html_in_the_picker_and_the_help(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/characters/ai-new").text
    assert ".html,.htm" in page
    assert "HTML" in page.split("Import a sheet")[1][:200]
