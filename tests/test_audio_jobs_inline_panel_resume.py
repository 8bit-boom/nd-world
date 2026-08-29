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
