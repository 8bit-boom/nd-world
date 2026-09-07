"""Tests for the built-in Image Gen model templates
(app/imagegen_templates.py + GET /api/ai/imagegen/model-templates)."""
import pytest

from app import imagegen_templates as tmpl


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
    for regular in ("sdxl", "pony", "anima", "chroma", "sd35", "kandinsky"):
        assert by_id[regular]["cfg"] >= 3.5, regular


def test_lumina_prefix_is_the_llm_system_prompt():
    """Lumina 2's input is an LLM (Gemma 2) — without the system-style
    prefix the model generates badly, so the template's prefix must carry
    it (with the <Prompt Start> marker)."""
    t = {x["id"]: x for x in tmpl.TEMPLATES}["lumina"]
    assert t["prefix"].startswith("You are an assistant designed to generate")
    assert "<Prompt Start>" in t["prefix"]


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
    assert tmpl.suggest_template("chroma_radiance") is None
    assert tmpl.suggest_template("hunyuan_video") is None


@pytest.mark.asyncio
async def test_route_returns_templates(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/imagegen/model-templates")
    assert r.status_code == 200
    data = r.json()
    assert len(data["templates"]) == len(tmpl.TEMPLATES)
    first = data["templates"][0]
    for key in ("id", "label", "match", "example_prompt", "negative", "steps", "cfg", "note"):
        assert key in first
