"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 2.6: static/js/audio-jobs.js's shared ndAudioJobs panel (embedded on
Sessions/Entities detail pages, distinct from the dedicated Background Jobs
page's own bgResumeJob) previously had "interrupted" in neither its
IN_PROGRESS nor FINISHED status sets — an interrupted job just rendered as
a frozen row with no button and no further polling, even though the exact
same job is resumable from the Background Jobs page via
POST /api/audio-jobs/{id}/resume. JS-source assertion test, reading the
static file directly (there's no route that renders it standalone)."""
from pathlib import Path

_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "audio-jobs.js").read_text()


def test_interrupted_is_in_finished_set():
    assert '"interrupted"' in _JS
    finished_line = next(line for line in _JS.splitlines() if "const FINISHED" in line)
    assert "interrupted" in finished_line


def test_resume_button_rendered_for_interrupted_status():
    assert 'job.status === "interrupted"' in _JS
    assert '"▶ Resume"' in _JS


def test_resume_button_posts_to_the_resume_route():
    assert "async function resumeJob(jobId, btn)" in _JS
    assert "/api/audio-jobs/${jobId}/resume" in _JS
    assert 'method: "POST"' in _JS


def test_resume_refreshes_the_list_so_polling_resumes():
    body = _JS.split("async function resumeJob(jobId, btn)", 1)[1][:800]
    assert "await refreshList();" in body


# ── Delegated click handling (fixes a real "Use this" tap doing nothing) ────
# render() rebuilds panelEl's entire contents (panelEl.innerHTML = "") on
# every poll, and polling continues every pollMs for as long as any job is
# in progress. A handler bound directly to a button (btn.onclick = ...) can
# lose a tap that lands between touchstart and click on a slower/mobile
# device: the element the tap started on gets torn out of the DOM by a
# re-render before the click event ever fires, so the tap silently does
# nothing — reported in practice on a session page with an active Condense
# job keeping the poll loop running. Delegating to panelEl itself (never
# replaced, only its children) fixes this: the click still lands on
# whatever element is under it when the event actually fires.

def test_action_buttons_use_data_attributes_not_inline_onclick():
    assert 'btn.dataset.action = "use"' in _JS
    assert 'resumeBtn.dataset.action = "resume"' in _JS
    assert 'delBtn.dataset.action = "delete"' in _JS
    assert "btn.onclick = () => opts.onUse(job)" not in _JS
    assert "resumeBtn.onclick" not in _JS
    assert "delBtn.onclick" not in _JS


def test_single_delegated_click_listener_registered_once_on_panel_el():
    assert _JS.count('panelEl.addEventListener("click"') == 1
    # Registered outside render() (after its closing brace, alongside the
    # initial refreshList() call), not re-registered on every re-render.
    listener_pos = _JS.index('panelEl.addEventListener("click"')
    render_pos = _JS.index("function render()")
    refresh_list_call_pos = _JS.rindex("refreshList();")
    assert render_pos < listener_pos < refresh_list_call_pos


def test_delegated_listener_dispatches_all_three_actions():
    body = _JS.split('panelEl.addEventListener("click", (e) => {', 1)[1].split("});", 1)[0]
    assert 'closest("button[data-action]")' in body
    assert 'btn.dataset.action === "use"' in body
    assert "opts.onUse(job)" in body
    assert 'btn.dataset.action === "resume"' in body
    assert "resumeJob(jobId, btn)" in body
    assert 'btn.dataset.action === "delete"' in body
    assert "deleteJob(jobId, btn)" in body
    # Looks the job up fresh from the live `jobs` array by id at click time
    # rather than a captured closure — the array itself gets replaced by
    # every refreshList() poll, so a stale captured reference would apply
    # a tap to a job object refreshList() may have already discarded.
    assert "jobs.find((j) => String(j.id) === jobId)" in body
