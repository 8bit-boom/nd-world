"""Tests for the Image Gen model templates: the built-ins
(app/imagegen_templates.py), the official-guide-derived content, and the
GM's own custom templates (PromptPreset scope="image_template" + the
/api/ai/imagegen/model-templates* routes)."""
import pytest

from app import imagegen_templates as tmpl
from app.database import SessionLocal
from app.models import PromptPreset

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── Built-in templates ──────────────────────────────────────────────────────

def test_templates_have_unique_ids_and_required_fields():
    ids = [t["id"] for t in tmpl.TEMPLATES]
    assert len(ids) == len(set(ids)), "template ids must be unique"
    for t in tmpl.TEMPLATES:
        assert t["label"], t["id"]
        assert t["match"], f"{t['id']}: match keywords are what powers the suggested marker"
        assert isinstance(t["steps"], int) and 1 <= t["steps"] <= 60
        assert 1.0 <= t["cfg"] <= 20.0
        assert t["example_prompt"], f"{t['id']}: an empty prompt box needs a fill example"
        assert t["note"], f"{t['id']}: the note is what the GM reads after applying"
        assert t["guide"], f"{t['id']}: the guide is the researched prompting how-to"


def test_the_seven_requested_families_are_present():
    ids = {t["id"] for t in tmpl.TEMPLATES}
    assert {"anima", "krea2", "sdxl", "pony", "zimage", "flux1", "flux2"} <= ids


def test_distilled_families_are_cfg1_and_regular_ones_are_not():
    """The whole point of the templates: the CFG-1 distillates (where low
    CFG is CORRECT) must be clearly distinguished from regular
    checkpoints (where it means 'prompt ignored')."""
    by_id = {t["id"]: t for t in tmpl.TEMPLATES}
    for distilled in ("krea2", "zimage", "flux1", "flux2", "hidream", "sdturbo", "boogu"):
        assert by_id[distilled]["cfg"] == 1.0, distilled
    for regular in ("sdxl", "pony", "anima", "chroma", "sd35", "kandinsky", "chroma_radiance"):
        assert by_id[regular]["cfg"] >= 3.5, regular


def test_lumina_prefix_is_the_llm_system_prompt():
    """Lumina 2's input is an LLM (Gemma 2) — without the system-style
    prefix the model generates badly, so the template's prefix must carry
    it (with the <Prompt Start> marker)."""
    t = {x["id"]: x for x in tmpl.TEMPLATES}["lumina"]
    assert t["prefix"].startswith("You are an assistant designed to generate")
    assert "<Prompt Start>" in t["prefix"]


def test_anima_uses_the_official_prefix_and_negative():
    """Anima's HF README specifies its recommended positive prefix and
    negative verbatim — the template must carry those, not guesses."""
    t = {x["id"]: x for x in tmpl.TEMPLATES}["anima"]
    assert t["prefix"].startswith("masterpiece, best quality, score_7, safe")
    for required in ("score_1", "score_2", "score_3", "artist name", "chromatic aberration"):
        assert required in t["negative"], required
    # The @artist rule and the stronger-weighting rule are the two most
    # useful non-obvious facts in the official guide.
    assert "@artist" in t["guide"] or "must have @" in t["guide"].lower()


def test_ideogram_example_is_a_json_caption():
    """Ideogram 4 is trained exclusively on JSON captions — plain prose
    'will not work' (its own docs). The template's example must BE the
    schema so the GM starts from the right shape."""
    import json
    t = {x["id"]: x for x in tmpl.TEMPLATES}["ideogram"]
    caption = json.loads(t["example_prompt"])
    assert "high_level_description" in caption
    assert "style_description" in caption
    assert "compositional_deconstruction" in caption


def test_krea2_guide_carries_style_lora_triggers():
    """The 9 official Krea style LoRAs fire on phrases nothing like their
    file names — the guide is the only place a GM can find them."""
    t = {x["id"]: x for x in tmpl.TEMPLATES}["krea2"]
    for phrase in ("monochrome ink wash style", "vintage tarot style", "purple retro anime style"):
        assert phrase in t["guide"], phrase


def test_suggest_template_matches_by_keyword_substring():
    hit = tmpl.suggest_template("Anima - anima-base-v1.0")
    assert hit and hit["id"] == "anima"
    assert tmpl.suggest_template("krea2_turbo_fp8")["id"] == "krea2"
    assert tmpl.suggest_template("sd_xl_base_1.0.safetensors")["id"] == "sdxl"
    assert tmpl.suggest_template("something-unknown") is None
    assert tmpl.suggest_template("") is None
    # Turbo variants of SPECIFIC families suggest that family, not the
    # generic SD-Turbo/LCM template — and sibling-name collisions stay honest.
    assert tmpl.suggest_template("z_image_turbo_bf16")["id"] == "zimage"
    assert tmpl.suggest_template("boogu_image_turbo")["id"] == "boogu"
    assert tmpl.suggest_template("flux2-dev-turbo")["id"] == "flux2"
    assert tmpl.suggest_template("animagineXL_v31") is None
    assert tmpl.suggest_template("hunyuan_video") is None
    # Radiance is its own (WIP) template, excluded from plain Chroma.
    assert tmpl.suggest_template("chroma1-hd")["id"] == "chroma"
    assert tmpl.suggest_template("chroma_radiance")["id"] == "chroma_radiance"


# ── The route + custom templates ────────────────────────────────────────────

def test_route_returns_templates(client, seed):
    _login_gm_in(client, seed)
    r = client.get("/api/ai/imagegen/model-templates")
    assert r.status_code == 200
    data = r.json()
    assert len(data["templates"]) == len(tmpl.TEMPLATES)
    first = data["templates"][0]
    for key in ("id", "label", "match", "example_prompt", "negative", "steps", "cfg", "note", "guide"):
        assert key in first
    assert data["custom"] == []


def test_custom_template_save_merge_and_delete(client, seed):
    _login_gm_in(client, seed)
    body = {
        "label": "My noir Anima",
        "match": "anima, mymodel",
        "example_prompt": "masterpiece, 1girl, detective, rain",
        "negative": "worst quality",
        "steps": 30, "cfg": 4.5, "sampler": "er_sde", "scheduler": "simple",
        "note": "noir look", "guide": "keep tags lowercase",
    }
    r = client.post("/api/ai/imagegen/model-templates/custom", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["custom"] is True
    assert saved["match"] == ["anima", "mymodel"]
    assert saved["steps"] == 30 and saved["cfg"] == 4.5
    row_id = saved["id"]

    # Merged into the GET list.
    data = client.get("/api/ai/imagegen/model-templates").json()
    assert [c["id"] for c in data["custom"]] == [row_id]
    assert data["custom"][0]["example_prompt"] == body["example_prompt"]

    # Upsert by label: same id, changed fields.
    body2 = {**body, "steps": 12, "cfg": 1.0, "negative": ""}
    r2 = client.post("/api/ai/imagegen/model-templates/custom", json=body2)
    assert r2.status_code == 200
    assert r2.json()["id"] == row_id
    db = SessionLocal()
    try:
        assert db.query(PromptPreset).filter(PromptPreset.scope == "image_template").count() == 1
    finally:
        db.close()

    # Delete → gone from GET.
    numeric = int(row_id.split("-")[1])
    assert client.delete(f"/api/ai/imagegen/model-templates/custom/{numeric}").status_code == 200
    assert client.get("/api/ai/imagegen/model-templates").json()["custom"] == []


def test_custom_templates_are_world_scoped(client, seed):
    _login_gm_in(client, seed)
    client.post("/api/ai/imagegen/model-templates/custom", json={"label": "A-world only"})
    # Same GM, other active world → not listed, and not deletable by id.
    client.cookies.set("active_world", seed.world_b.slug)
    assert client.get("/api/ai/imagegen/model-templates").json()["custom"] == []
    db = SessionLocal()
    try:
        row = db.query(PromptPreset).filter(PromptPreset.scope == "image_template").first()
        row_id = row.id
    finally:
        db.close()
    assert client.delete(f"/api/ai/imagegen/model-templates/custom/{row_id}").status_code == 404


def test_custom_template_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/model-templates/custom", json={"label": "sneaky"})
    assert r.status_code == 403


def test_custom_template_rejects_empty_label(client, seed):
    _login_gm_in(client, seed)
    r = client.post("/api/ai/imagegen/model-templates/custom", json={"label": "   "})
    assert r.status_code == 400
