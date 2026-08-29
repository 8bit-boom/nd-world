"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 4.1: background_jobs.html rendered only page 1 of /api/audio-jobs
(discarding the route's own `page`/`total_pages` fields) and the flat,
server-capped-at-20 /api/ai/chat/jobs and /api/ai/imagegen/jobs lists as if
each were the complete history, with no indication anything was cut off.
This is a JS-source assertion test — no browser automation, matching this
file's established convention for template-JS regression coverage (see
test_live_recording_wake_lock.py)."""
from .conftest import GM_PASSWORD, login


def _get_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 200
    return r.text


def test_pagenote_element_present(client, seed):
    page = _get_page(client, seed)
    assert 'id="bg-jobs-pagenote"' in page


def test_bg_fetch_jobs_reads_total_pages(client, seed):
    page = _get_page(client, seed)
    assert "total_pages" in page
    assert "data.total_pages" in page


def test_notice_flags_audio_pagination_and_chat_image_caps(client, seed):
    page = _get_page(client, seed)
    body = page.split("async function bgRefresh()", 1)[1]
    assert "audio.total_pages > 1" in body
    assert "older ones exist" in body
    assert "chat.list.length >= 20" in body
    assert "image.list.length >= 20" in body
    assert "capped at the latest 20" in body
