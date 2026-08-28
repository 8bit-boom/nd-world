"""Tests for the unified Background Jobs page (plan item AI 1.4) — before
this, background_jobs.html hardcoded BG_LIST_URL = '/api/audio-jobs', so
chat jobs and image-gen jobs (app/chat_jobs.py, app/image_jobs.py) were
only visible inside their own small panels on the AI Chat page, never here.
The page renders entirely client-side (see test_audio_jobs.py's own
test_background_jobs_page_offers_resume_for_an_interrupted_job for why —
no server-rendered job list to grep the static HTML for), so these tests
check both halves of the contract: the shipped JS actually references all
three job engines' endpoints, and each engine's list route returns the
shape that JS reads from."""
from app.database import SessionLocal
from app.models import AudioJob, ChatJob, ImageJob

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_page_references_all_three_job_endpoints(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    page_html = client.get("/background-jobs").text
    assert "/api/audio-jobs" in page_html
    assert "/api/ai/chat/jobs" in page_html
    assert "/api/ai/imagegen/jobs" in page_html
    # The composite-key helper — job ids are only unique WITHIN one job
    # table, so a bare job.id would let e.g. AudioJob #5 and ChatJob #5
    # collide in the page's own _bgExpanded/_bgBars state.
    assert "bgKey" in page_html
    assert "job_type" in page_html


def test_chat_job_list_shape_matches_what_the_page_reads(client, seed):
    db = SessionLocal()
    try:
        db.add(ChatJob(world_id=seed.world_a.id, status="done",
                        prompt="What's in the tavern?", result="A grizzled bartender.", model="llama3.1"))
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/api/ai/chat/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    job = jobs[0]
    for field in ("id", "prompt", "status", "error", "result", "model", "created_at"):
        assert field in job


def test_image_job_list_shape_matches_what_the_page_reads(client, seed):
    db = SessionLocal()
    try:
        db.add(ImageJob(world_id=seed.world_a.id, prompt="a neon city street",
                         status="done", result_urls_json='["/uploads/a.png", "/uploads/b.png"]'))
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/api/ai/imagegen/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    job = jobs[0]
    for field in ("id", "prompt", "status", "error", "urls", "created_at"):
        assert field in job
    assert job["urls"] == ["/uploads/a.png", "/uploads/b.png"]


def test_all_three_job_types_reachable_by_gm_in_one_world(client, seed):
    """Not a DOM test (this page renders client-side, see the module
    docstring) — proves the three underlying lists a GM would see merged
    together all actually return data for the same active world at once,
    which is the real precondition for the unified page to show anything
    at all."""
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="a.mp3"),
            ChatJob(world_id=seed.world_a.id, status="done", prompt="hi", result="hello"),
            ImageJob(world_id=seed.world_a.id, prompt="a dragon", status="done", result_urls_json="[]"),
        ])
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    audio = client.get("/api/audio-jobs").json()["jobs"]
    chat = client.get("/api/ai/chat/jobs").json()
    image = client.get("/api/ai/imagegen/jobs").json()
    assert len(audio) == 1
    assert len(chat) == 1
    assert len(image) == 1


def test_background_jobs_page_still_requires_gm(client, seed):
    """Unchanged by this feature — the whole page (and every job-list route
    it fetches from) stays GM-only."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/background-jobs").status_code == 403
    assert client.get("/api/ai/chat/jobs").status_code == 403
