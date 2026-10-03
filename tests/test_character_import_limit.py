"""How much of an uploaded character sheet the AI creator reads at once — sized from the model's context window — and
what happens to a sheet that does not fit.

The creator used to read a fixed first 12,000 characters of an imported sheet and silently drop the rest. Now:
  * the size read in one go follows the model's context window (a bigger window reads more), and the GM may still set a
    number in Settings -> System to override it (blank = automatic);
  * a sheet longer than that is read in PARTS — each part is condensed into notes by the model, and the character is then
    built from the notes — so nothing is dropped until a sheet is beyond a sane number of parts, and then the player is told."""
import io
import math
import time
from types import SimpleNamespace

import pytest

from app import ai as ai_module
from app.database import SessionLocal, get_app_settings
from app.routers import character_ai as cai

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

AUTO = SimpleNamespace(character_import_max_chars=None)
LATIN = "Line of a very long character sheet with plenty of words in it.\n"
CYRILLIC = "Строка очень длинного листа персонажа с большим количеством слов в ней.\n"


def _sheet(chars, line=LATIN, tail=""):
    body = (line * (chars // len(line) + 1))[: max(0, chars - len(tail))]
    return body + tail


def _budgets(settings=AUTO, source="x" * 40, system="s" * 1000, header="h" * 300, prompt="", use_rag=False, think=True):
    return cai.sheet_budgets(settings, source_text=source, system=system, header=header, prompt=prompt,
                             use_rag=use_rag, think=think)


@pytest.fixture
def ctx(monkeypatch):
    """Pin the model's context window (tokens) for a test."""
    def set_ctx(tokens):
        monkeypatch.setattr(ai_module, "_llm_context_tokens_override", tokens)
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")
    return set_ctx


def _settings_post(client, **extra):
    data = {"ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_keep_alive": "",
            "ollama_use_mmap": ""}
    data.update(extra)
    return client.post("/settings/system", data=data, follow_redirects=False)


def _stored():
    db = SessionLocal()
    try:
        return get_app_settings(db).character_import_max_chars
    finally:
        db.close()


# ── the budget: automatic from the context window, or the GM's own number ───────────────────────

def test_the_one_go_limit_grows_with_the_context_window(ctx):
    sizes = []
    for tokens in (12000, 16384, 24576, 32768):
        ctx(tokens)
        sizes.append(_budgets()[0])
    assert sizes == sorted(sizes) and len(set(sizes)) > 1, sizes


def test_a_tiny_window_still_reads_a_useful_amount_and_a_huge_one_is_capped(ctx):
    ctx(4096)
    assert _budgets()[0] == cai.CHARACTER_IMPORT_AUTO_MIN_CHARS
    ctx(10**7)
    assert _budgets()[0] == cai.CHARACTER_IMPORT_MAX_CHARS


def test_text_that_tokenizes_densely_gets_fewer_characters(ctx):
    ctx(16384)
    latin = _budgets(source=_sheet(5000))[0]
    cyr = _budgets(source=_sheet(5000, CYRILLIC))[0]
    assert cyr < latin


def test_what_rides_along_with_the_sheet_takes_room_from_it(ctx):
    ctx(16384)
    plain = _budgets()[0]
    assert _budgets(header="h" * 8000)[0] < plain
    assert _budgets(prompt="p" * 4000)[0] < plain
    assert _budgets(use_rag=True)[0] < plain
    assert _budgets(system="s" * 6000)[0] < plain


def test_a_part_is_bigger_than_the_one_go_limit_since_it_carries_no_rules(ctx):
    ctx(16384)
    limit, part = _budgets()
    assert part > limit


def test_the_gms_number_overrides_the_automatic_limit_and_is_clamped(ctx):
    ctx(16384)
    assert _budgets(SimpleNamespace(character_import_max_chars=30000))[0] == 30000
    assert _budgets(SimpleNamespace(character_import_max_chars=5))[0] == cai.CHARACTER_IMPORT_MIN_CHARS
    assert _budgets(SimpleNamespace(character_import_max_chars=10**9))[0] == cai.CHARACTER_IMPORT_MAX_CHARS
    assert _budgets(SimpleNamespace(character_import_max_chars="junk"))[0] == _budgets(AUTO)[0]      # junk = automatic
    assert _budgets(SimpleNamespace())[0] == _budgets(AUTO)[0]


# ── cutting a sheet into parts ──────────────────────────────────────────────────────────────────

def test_a_sheet_that_fits_is_read_directly():
    plan = cai.plan_sheet("short sheet", limit=100, part_chars=500)
    assert plan.direct and plan.parts == [] and not plan.truncated


def test_a_longer_sheet_is_cut_into_parts_that_each_fit_and_lose_nothing():
    text = _sheet(5000)
    plan = cai.plan_sheet(text, limit=1000, part_chars=1500)
    assert not plan.direct and not plan.truncated and len(plan.parts) >= 4
    assert all(0 < len(p) <= 1500 for p in plan.parts)
    assert "".join("".join(plan.parts).split()) == "".join(text.split())        # same content, same order (edge whitespace aside)


def test_parts_break_between_paragraphs_when_they_can():
    text = ("A" * 100) + "\n\n" + ("B" * 100) + "\n\n" + ("C" * 100)
    parts = cai.plan_sheet(text, limit=50, part_chars=150).parts
    assert [set(p) - {"\n"} for p in parts] == [{"A"}, {"B"}, {"C"}]


def test_one_enormous_line_is_still_cut_to_fit():
    parts = cai.plan_sheet("Z" * 3000, limit=500, part_chars=1000).parts
    assert len(parts) == 3 and all(len(p) <= 1000 for p in parts)


def test_a_sheet_beyond_the_part_cap_is_cut_and_flagged():
    plan = cai.plan_sheet(_sheet(50000), limit=500, part_chars=1000)
    assert plan.truncated and len(plan.parts) == cai.MAX_SHEET_PARTS
    assert plan.unread_chars > 0


# ── the setting (Settings -> System) ────────────────────────────────────────────────────────────

def test_gm_can_set_it_and_clear_it_back_to_automatic(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert _settings_post(client, character_import_max_chars="30000").status_code == 303
    assert _stored() == 30000
    assert _settings_post(client, character_import_max_chars="").status_code == 303        # blank = automatic
    assert _stored() is None


@pytest.mark.parametrize("bad", ["1999", "60001", "abc", "-5", "12.5"])
def test_out_of_range_or_junk_is_rejected_with_a_clear_message(client, seed, bad):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _settings_post(client, character_import_max_chars=bad)
    assert r.status_code == 400
    assert "sheet" in r.text.lower()
    assert _stored() is None


def test_players_cannot_change_it(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert _settings_post(client, character_import_max_chars="50000").status_code == 403
    assert _stored() is None


def test_the_settings_page_explains_automatic_and_parts(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _settings_post(client, character_import_max_chars="24000")
    page = client.get("/settings?tab=system").text
    assert 'name="character_import_max_chars"' in page and 'value="24000"' in page
    low = page.lower()
    assert "automatic" in low and "context window" in low and "in parts" in low


# ── an import, end to end ───────────────────────────────────────────────────────────────────────

DRAFT = '{"name": "Imported", "race": "Elf", "char_class": "Bard", "level": 3, "stats": {}, "backstory": "x"}'


class Model:
    """A scripted model: notes calls (plain text, think off) and the final draft call (JSON schema), recorded in order."""

    def __init__(self, fail_part=0):
        self.calls = []
        self.progress = []
        self.fail_part = fail_part

    async def __call__(self, messages, **kw):
        job = list(cai._PC_AI_JOBS.values())[-1]
        self.progress.append((job.get("stage"), job.get("part"), job.get("parts")))
        system = kw.get("system", "")
        call = {"text": messages[0]["content"], "system": system, "think": kw.get("think"), "format": kw.get("format")}
        self.calls.append(call)
        if kw.get("format") is not None:
            return DRAFT
        part = len(self.notes_calls())
        if self.fail_part == part:
            return "[AI error: the model fell over]"
        return f"NOTES-FROM-PART-{part} name=Mirabel"

    def notes_calls(self):
        return [c for c in self.calls if c["format"] is None]

    def draft_call(self):
        return [c for c in self.calls if c["format"] is not None][-1]


def _poll(client, job_id, seconds=15):
    end = time.time() + seconds
    data = {"status": "running"}
    while time.time() < end:
        data = client.get(f"/api/characters/ai/{job_id}").json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    return data


def _start(client, seed, monkeypatch, sheet, model, name="sheet.txt", **form):
    monkeypatch.setattr(ai_module, "generate_chat", model)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    data = {"prompt": "", "use_rag": "false"}
    data.update(form)
    r = client.post("/api/characters/ai/start", files={"file": (name, io.BytesIO(sheet.encode()), "text/plain")}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_sheet_that_fits_goes_to_the_model_as_is(client, seed, monkeypatch, ctx):
    ctx(16384)
    model = Model()
    info = _start(client, seed, monkeypatch, _sheet(2500, tail="THE-TAIL"), model)
    assert _poll(client, info["job_id"])["status"] == "done"
    assert len(model.calls) == 1 and "THE-TAIL" in model.calls[0]["text"]
    assert info["source_parts"] == 0 and info["source_truncated"] is False and info["source_chars"] == 2500


def test_a_longer_sheet_is_read_in_parts_and_the_character_is_built_from_the_notes(client, seed, monkeypatch, ctx):
    ctx(8192)                                     # a small window: ~3,000 characters at once, ~20,000 per part
    model = Model()
    sheet = _sheet(50000, tail="THE-TAIL-OF-THE-SHEET")
    info = _start(client, seed, monkeypatch, sheet, model)
    assert info["source_parts"] >= 2 and info["source_truncated"] is False
    assert _poll(client, info["job_id"])["status"] == "done"
    notes = model.notes_calls()
    assert len(notes) == info["source_parts"]
    # every part of the sheet reached the model, in order, and the tail is in the last one
    assert "THE-TAIL-OF-THE-SHEET" in notes[-1]["text"] and all("THE-TAIL" not in n["text"] for n in notes[:-1])
    assert sum(len(n["text"]) for n in notes) >= 50000 - 10 * len(notes)
    # the notes calls are plain text without thinking, and told which part they are
    assert all(n["think"] is False for n in notes)
    assert "PART 1 OF" in notes[0]["system"].upper() and f"PART {len(notes)} OF {len(notes)}" in notes[-1]["system"].upper()
    # the final draft is built from the notes, not from the raw sheet
    draft = model.draft_call()["text"]
    assert all(f"NOTES-FROM-PART-{i}" in draft for i in range(1, len(notes) + 1))
    assert "THE-TAIL-OF-THE-SHEET" not in draft and "Line of a very long character sheet" not in draft
    assert "parts" in draft.lower()


def test_the_page_can_see_which_part_is_being_read(client, seed, monkeypatch, ctx):
    ctx(8192)
    model = Model()
    info = _start(client, seed, monkeypatch, _sheet(50000), model)
    assert _poll(client, info["job_id"])["status"] == "done"
    n = info["source_parts"]
    assert model.progress[:n] == [("reading", i, n) for i in range(1, n + 1)]
    assert model.progress[n] == ("building", n, n)


def test_a_running_job_reports_its_progress_to_the_poll(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    cai._PC_AI_JOBS[9001] = {"status": "running", "started": time.time(), "user_id": seed.player_a.id, "draft": None,
                             "error": "", "stage": "reading", "part": 2, "parts": 3}
    try:
        got = client.get("/api/characters/ai/9001").json()
    finally:
        cai._PC_AI_JOBS.pop(9001, None)
    assert got["status"] == "running" and got["stage"] == "reading" and got["part"] == 2 and got["parts"] == 3


def test_a_part_the_model_fails_on_is_reported_with_its_number(client, seed, monkeypatch, ctx):
    ctx(8192)
    model = Model(fail_part=2)
    info = _start(client, seed, monkeypatch, _sheet(50000), model)
    done = _poll(client, info["job_id"])
    assert done["status"] == "error" and "part 2 of" in done["error"].lower()
    assert not [c for c in model.calls if c["format"] is not None], "must not build a character from missing notes"


def test_a_sheet_beyond_the_part_cap_is_flagged_and_the_model_is_told(client, seed, monkeypatch, ctx):
    ctx(8192)
    model = Model()
    sheet = _sheet(300000)
    info = _start(client, seed, monkeypatch, sheet, model)
    assert info["source_truncated"] is True and info["source_parts"] == cai.MAX_SHEET_PARTS
    assert _poll(client, info["job_id"], seconds=30)["status"] == "done"
    assert "could not be read" in model.draft_call()["text"]


def test_a_bigger_window_reads_the_same_sheet_in_fewer_steps(client, seed, monkeypatch, ctx):
    sheet = _sheet(60000)
    ctx(8192)
    small = _start(client, seed, monkeypatch, sheet, Model())
    ctx(32768)
    big = _start(client, seed, monkeypatch, sheet, Model())
    assert big["source_parts"] < small["source_parts"]
    assert big["source_limit"] > small["source_limit"]


def test_the_gms_number_wins_over_the_automatic_one(client, seed, monkeypatch, ctx):
    ctx(8192)
    login(client, seed.gm.email, GM_PASSWORD)
    _settings_post(client, character_import_max_chars="30000")
    client.cookies.clear()
    model = Model()
    info = _start(client, seed, monkeypatch, _sheet(20000, tail="THE-TAIL"), model)
    assert info["source_limit"] == 30000 and info["source_parts"] == 0
    assert _poll(client, info["job_id"])["status"] == "done"
    assert len(model.calls) == 1 and "THE-TAIL" in model.calls[0]["text"]


def test_a_prompt_only_start_has_nothing_to_read(client, seed, monkeypatch, ctx):
    ctx(16384)
    monkeypatch.setattr(ai_module, "generate_chat", Model())
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={"prompt": "a rogue", "use_rag": "false"})
    assert r.status_code == 200
    got = r.json()
    assert got["source_parts"] == 0 and got["source_truncated"] is False and got["source_chars"] == 0


def test_the_creator_page_tells_about_parts_and_cut_sheets(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/characters/ai-new").text
    for needle in ("source_parts", "source_truncated", "source_unread_chars", "d.part", "d.parts"):
        assert needle in page, needle
