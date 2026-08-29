"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 2.5: bgRefresh (app/templates/background_jobs.html) wipes and rebuilds
the whole job list every 3s while anything is in progress — which used to
destroy whatever a GM had typed into a retry row's model select/extra
instructions/Thinking checkbox, and reset the scroll position of any
expanded result box, on every poll. JS-source assertion test — no browser
automation, matching this repo's established convention for template-JS
regression coverage (see test_live_recording_wake_lock.py)."""
from .conftest import GM_PASSWORD, login


def _get_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 200
    return r.text


def test_form_state_map_exists_and_is_wired_to_the_retry_controls(client, seed):
    page = _get_page(client, seed)
    assert "const _bgFormState = new Map();" in page
    assert "sel.onchange = () => {" in page
    assert "instructionsInput.oninput = () => {" in page
    assert "thinkCheckbox.onchange = () => {" in page
    # Values are read back from the saved state, not just the job's own
    # server-rendered defaults, on every render.
    assert "savedForm && savedForm.model !== undefined" in page
    assert "savedForm && savedForm.instructions !== undefined" in page
    assert "savedForm && savedForm.think !== undefined" in page


def test_scroll_position_is_tracked_and_restored(client, seed):
    page = _get_page(client, seed)
    assert "const bgTrackScroll = " in page
    assert "data-bg-scroll-key" in page or "dataset.bgScrollKey" in page
    assert "box.addEventListener('scroll'" in page
    # Restoration happens in bgRefresh, after the card is attached to the
    # live DOM — not inside bgRenderJob while the card is still detached.
    refresh_body = page.split("async function bgRefresh()", 1)[1][:2000]
    assert "querySelectorAll('[data-bg-scroll-key]')" in refresh_body
    assert "box.scrollTop = saved.scroll" in refresh_body
