"""Durable background jobs for audio transcription (+ optional
summarization) — see AudioJob in app/models.py for the full rationale.
Kept in its own module (not routers/sessions.py or routers/ai.py) since
both routers start/poll the identical job engine.
"""
import asyncio
import json as _json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import ai as _ai_module
from . import ai_assist as _ai_assist
from . import job_shutdown as _job_shutdown
from . import retrieval as _retrieval
from .database import SessionLocal
from .models import AudioJob, Entity, Fact, GameSession, PlayerCharacter, Quest, World

_log = logging.getLogger("nd.audio_jobs")

# Must hold a strong reference to every in-flight task — asyncio only keeps
# a weak reference of its own, so a task with nothing else referencing it
# can be garbage-collected mid-run. Keyed by AudioJob.id; each task removes
# its own entry once it finishes (success or failure) via a done-callback.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "transcribing", "summarizing")


def _interrupted_note(chunk_current: Optional[int] = None, chunk_total: Optional[int] = None) -> str:
    """The status/error message for a job paused mid-run by a server
    shutdown — not really an error, just explains what happened and that
    the saved checkpoint is what makes a resume possible. One shared
    wording so every call site that can transition a job to "interrupted"
    (both CancelledError-adjacent handlers and JobInterrupted handling in
    both run functions below, plus the boot/shutdown sweeps) says the same
    thing."""
    if chunk_total and chunk_current:
        return (
            f"Paused by a server restart at part {chunk_current} of {chunk_total} — "
            "the work so far is saved; it will resume automatically, or use ▶ Resume."
        )
    return "Paused by a server restart — the work so far is saved; it will resume automatically, or use ▶ Resume."


def _forget_task(job_id: int, task: asyncio.Task) -> None:
    """Done-callback for a job's background task.

    Identity-checked — only removes the registry entry if it's still THIS
    task — because asyncio schedules done-callbacks via call_soon, so
    there's at least one event-loop turn between a task finishing (after
    it has already written a terminal status) and this callback actually
    running. A resume/resummarize started in that window sees a row
    that's already out of IN_PROGRESS_STATUSES, installs its own new task
    into _running_tasks, and then the OLD task's callback would delete
    that live task's registry entry out from under it: it becomes only
    weakly-referenced (asyncio doesn't hold a strong reference of its
    own — eligible for GC mid-run) and cancel_job() can no longer find it
    to cancel.

    Also reconciles a task that was cancelled before its coroutine body
    ever started running at all: asyncio.Task.cancel() on such a task
    skips the body entirely, so neither _run_job's own `except
    asyncio.CancelledError` nor its cleanup ever executes, leaving the row
    stuck in an in-progress status forever (cancel_job and delete_job both
    refuse a row in IN_PROGRESS_STATUSES). Safe to run unconditionally on
    every cancelled task: when the body DID run far enough to reach its
    own CancelledError handler, that handler has already moved the row out
    of IN_PROGRESS_STATUSES before this callback ever fires, so the checks
    below are then no-ops.

    audio_path/delete_after are read off the row itself, not passed in —
    create_job/start_resume_job persist both (see AudioJob's own
    docstring) specifically so a resume after a restart can find the audio
    again without this module needing to thread them through every call."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job or job.status not in IN_PROGRESS_STATUSES:
            return
        if _job_shutdown.stopping():
            # Cancelled before its body ever ran, during a shutdown drain —
            # keep the audio and any checkpoint already on the row so a
            # resume can pick this job back up, same as _run_job's own
            # CancelledError handling below.
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
        else:
            job.status = "cancelled"
            job.error = "Cancelled by GM."
            if job.delete_after and job.audio_path:
                Path(job.audio_path).unlink(missing_ok=True)
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _looks_like_failure(result: str) -> bool:
    """summarize_transcript() never raises on an Ollama-side failure — it
    returns a failure-sentinel string instead (see _ai_module.
    is_failure_sentinel's own docstring). Delegates there rather than
    keeping a second copy of the prefix check, after the two previously
    drifted apart: this module's own copy only checked one of the two
    sentinel families, which let a failed part-summary get woven into a
    "done" recap on the chunked summarization path — without this check
    entirely, the same bug applies to any Ollama-side failure."""
    return _ai_module.is_failure_sentinel(result)


async def _auto_extract_pending_facts(job_id: int, transcript: str, model: str) -> str:
    """Best-effort: draft Facts from a just-finished session_recap job's
    transcript via the SAME model that summarized it (job.model — a GM's
    per-job model choice should stay consistent end to end, not silently
    fall back to the instance default for this one step), using the exact
    parse_facts_from_recap() the manual /facts "Parse with AI" flow already
    uses. Returns a JSON string ready for AudioJob.pending_facts_json —
    "[]" on ANY failure (model unreachable, malformed output) so a
    fact-extraction hiccup can never turn an otherwise-successful recap job
    into a failed one; this step is a bonus on top of the recap, not a
    dependency of it.

    Deliberately does NOT create any Fact rows itself — see
    AudioJob.pending_facts_json's own docstring for why a human still has
    to review/confirm before anything reaches the facts table."""
    try:
        facts = await _ai_module.parse_facts_from_recap(transcript, model=model)
        return _json.dumps(facts)
    except Exception as exc:
        _log.warning("session_recap job %s: auto fact-extraction failed: %s: %s", job_id, type(exc).__name__, exc)
        return "[]"


def create_job(
    world_id: int, purpose: str, filename: str, audio_path: Path,
    delete_after: bool = True, game_session_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None, attachment_url: str = "",
    model: str = "", extra_instructions: str = "", think: bool = True,
    use_rag: bool = False, rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
) -> int:
    """Create the job row and start its background task immediately —
    returns the job id right away, well before transcription (let alone
    summarization) has even started, so the caller's HTTP response can
    return instantly regardless of how long the actual work takes. The
    background task keeps running in the server process independent of
    this (or any) HTTP connection, so closing the tab that started it
    doesn't stop it.

    `model`, if given, is the Ollama model to use for the summarization
    step (purpose="session_recap"/"condense" only — ignored for
    "attachment", which only transcribes). Blank means "whatever the
    instance default is."

    `extra_instructions`, if given, is a one-off note for THIS run's
    summarization only (purpose="session_recap" only) — combined with the
    world's own persistent World.recap_instructions rather than replacing
    it, see _combined_recap_instructions.

    `think` (purpose="session_recap"/"condense" only) is whether the
    summarize/condense step runs with the model's "thinking" mode on —
    persisted onto the row so a resume uses the same setting; see
    AudioJob.think's own docstring for why NULL (a pre-migration row) and
    the default here both mean "on."

    `use_rag`/`rag_entity_limit`/`rag_notes_limit` (purpose="session_recap"
    only — ignored for "attachment", which has no summarization step to feed
    context into) opt this run into retrieving relevant World entities/notes
    (see _build_rag_context) and prepending them to the summarize system
    prompt for accuracy. Blank limits fall back to _DEFAULT_RAG_ENTITY_LIMIT/
    _DEFAULT_RAG_NOTES_LIMIT at run time, not here — see _run_job.

    audio_path and delete_after are persisted onto the row (not just
    passed to _run_job as arguments) so a resume after a server restart —
    which has none of this call's local variables, only the DB row — can
    find the audio again and know whether it owns the file. See
    AudioJob's own docstring."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose=purpose, filename=filename,
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            attachment_url=attachment_url, status="pending", model=model or None,
            extra_instructions=extra_instructions.strip() or None, think=think,
            use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            audio_path=str(audio_path), delete_after=delete_after,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_condense_job(
    world_id: int, text: str, model: str = "", think: bool = True, fit_context: bool = False,
    extra_instructions: str = "", min_tokens: Optional[int] = None, max_tokens: Optional[int] = None,
    strictness: str = "guideline",
    game_session_id: Optional[int] = None, created_by_user_id: Optional[int] = None,
    use_rag: bool = False, rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
) -> int:
    """Condense `text` (typically a Session page's current Summary field) as
    a durable background job instead of a blocking request — same "survives
    closing the tab" contract create_job's own docstring describes, reusing
    the identical task-registration/checkpoint/interrupt machinery.

    Unlike a real audio job, there is no transcription phase: `text` is
    stored directly into job.transcript at creation (the row's "input"
    field, same slot a Whisper transcript would occupy), audio_path is
    blank, and delete_after is False (nothing to delete — there was never
    an uploaded file). _run_job sees a non-empty job.transcript and no
    audio_path, skips straight past the transcribe branch (see its own
    dispatch docstring), and for purpose="condense" calls condense_recap
    instead of summarize_transcript. The condensed result lands in
    job.recap, same field a session_recap job's summary does — so the
    existing polling/"Use this" UI (ndAudioJobs) needs no purpose-specific
    branch to display it.

    `extra_instructions`/`min_tokens`/`max_tokens` are condense_recap's own
    steering/length-target params (see its docstring) — persisted on the
    row like every other condense setting so a resume/redo uses the same
    values the GM originally set. `strictness` ("guideline"|"firm"|
    "strict") rides along the same way: it decides whether those targets
    are phrased as soft guidance or binding requirements, and — for
    "strict" only — whether _run_job estimates the finished recap's token
    count and auto-retries once when it lands outside the requested range.
    Validated here (not just inside condense_recap) so a bogus value fails
    at job creation — where the route maps it to a clean HTTP 400 — rather
    than surfacing mid-run as a job error the GM can't connect to the
    setting that caused it.

    `use_rag`/`rag_entity_limit`/`rag_notes_limit` — same RAG opt-in
    create_job's own docstring describes, see _build_rag_context."""
    db = SessionLocal()
    try:
        if strictness not in ("guideline", "firm", "strict"):
            raise ValueError(f"strictness must be guideline, firm, or strict, got {strictness!r}")
        job = AudioJob(
            world_id=world_id, purpose="condense", filename="Condense", status="pending",
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            model=model or None, think=think, fit_context=fit_context,
            extra_instructions=extra_instructions.strip() or None,
            min_tokens=min_tokens, max_tokens=max_tokens,
            condense_strictness=strictness,
            use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            transcript=text, audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_text_recap_job(
    world_id: int, text: str, *, model: str = "", think: bool = True,
    extra_instructions: str = "", game_session_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None,
    use_rag: bool = False, rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
) -> int:
    """Summarize `text` (a Live Recording session's accumulated
    GameSession.live_transcript) as a durable background job — sibling of
    create_condense_job above, same "text already in hand, no transcribe
    phase" shape, but seeded as purpose="session_recap" so _run_job's
    session_recap branch (map-reduce chunking, the Part 1 retry ladder, RAG)
    applies instead of condense_recap's single-call path. Motivated by
    POST /api/sessions/{id}/ai/summarize-live-transcript running a full
    summarize_transcript call inline in one HTTP request — exactly the
    reverse-proxy-timeout trap that motivated background jobs for audio
    uploads in the first place, and getting none of session_recap's
    checkpointing/RAG/retry-ladder protection a multi-hour live transcript
    deserves as much as an uploaded recording does.

    `text` is stored directly into job.transcript at creation, same as
    create_condense_job's own `text` param — _run_job's existing "already
    has a transcript, no audio_path" carve-out skips straight to
    summarizing, so this needed no _run_job changes at all."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="session_recap", filename="Live Transcript", status="pending",
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            model=model or None, think=think,
            extra_instructions=extra_instructions.strip() or None,
            use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            transcript=text, audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_facts_parse_job(
    world_id: int, text: str, game_session_id: Optional[int] = None,
    model: str = "", created_by_user_id: Optional[int] = None,
    think: bool = False, use_rag: bool = False,
    rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
    extra_instructions: str = "",
) -> int:
    """Parse `text` (a GM's rough recap, or a whole transcript pasted from a
    job's "📋 Extract facts" hand-off) into draft facts as a durable
    background job — sibling of create_condense_job above, same "text already
    in hand, no transcribe phase" shape, but purpose="facts_parse": _run_job's
    facts_parse branch calls _ai_module.parse_facts_from_recap (the same call
    POST /api/facts/parse makes synchronously) and lands the result as a JSON
    array in job.result_json rather than job.recap — a facts draft is review
    UI data ({content, visible_to_players} dicts), not a displayable recap,
    so it must not sit in a field the jobs UI renders as prose.

    Motivated by exactly the reverse-proxy-timeout trap create_condense_job's
    docstring describes: a long recap against a CPU-local model made the
    synchronous POST /api/facts/parse a routine way to trip Cloudflare's
    ~100s tunnel timeout (HTTP 524) and lose everything. `text` is stored
    into job.transcript (the row's "input" field, same slot condense/text-
    recap jobs use), so "Restore last parse" can also show what was parsed;
    `game_session_id`/`model` persist like every other job setting so the
    draft stays attributable to its session and a consistent model is
    available to anything inspecting the row.

    `think` (parse_facts_from_recap's reasoning mode — the Facts page's
    "Thinking" checkbox, OFF by default since a parse needs clean JSON back)
    and `use_rag`/`rag_entity_limit`/`rag_notes_limit` (RAG-retrieved World
    lore prepended to the parse's user message for name accuracy — see
    _build_rag_context) persist on the same AudioJob columns the
    condense/summarize purposes already use, so the runner reads them back
    unchanged on a resume, same as every other per-job setting here.

    `extra_instructions` is the same Condense-style one-off steering note
    (e.g. "only extract facts about the Thornwood Syndicate") on the same
    generic `extra_instructions` column every other purpose uses — threaded
    into parse_facts_from_recap's own system prompt (see its docstring),
    not min_tokens/max_tokens/strictness: a facts parse's output is a JSON
    array of discrete facts, not length-controllable prose, so a length
    target has no natural meaning here the way it does for Condense/Session
    Log recap.

    Raises ValueError on blank text — checked synchronously up front so the
    route maps it to a clean HTTP 400 rather than a job that errors mid-run
    with a message about empty input the GM can't connect to the button they
    clicked."""
    if not (text or "").strip():
        raise ValueError("No recap text provided")
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="facts_parse", filename="Facts", status="pending",
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            model=model or None,
            think=think, use_rag=use_rag,
            rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            extra_instructions=extra_instructions.strip() or None,
            transcript=text, audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_session_log_recap_job(
    world_id: int, session_id: int, audience: str, created_by_user_id: Optional[int] = None,
    model: str = "", think: bool = True, use_rag: bool = False,
    rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
    extra_instructions: str = "", min_tokens: Optional[int] = None, max_tokens: Optional[int] = None,
    strictness: str = "guideline",
) -> int:
    """Synthesize a Session Log page's recap (one summarize_session_from_facts
    call — see _run_job's session_log_recap branch) as a durable background
    job instead of blocking POST /api/session-log/{id}/recap for minutes —
    sibling of create_facts_parse_job above, motivated by the exact same
    reverse-proxy trap: a think=True summarize against a CPU-local model
    made the synchronous route a routine Cloudflare 524, and the first
    viewer of each cache window paid the whole wait inside one HTTP request.

    `audience` ("gm" or "players") is the whole reason this job exists as a
    keyed, findable row rather than a fire-and-forget task: the recap
    genuinely differs by fact visibility (a GM's includes secrets), so the
    polling route looks jobs up by (game_session_id, audience) to answer
    "is there a fresh one / is one already running" — see AudioJob.audience.
    Idempotent-create contract: the route, not this function, does the
    fresh/dedup checks against existing rows; this creator ALWAYS makes a
    new row, so those checks stay in the one place that knows the request
    context (which user, which cooldown window).

    `model` used to have no parameter at all (the row was always seeded with
    the "recap" surface default): an empty string keeps exactly that
    behavior, while a non-empty value is the Session Log page's own model
    picker's explicit choice and is stored as-is. `think` and
    `use_rag`/`rag_entity_limit`/`rag_notes_limit` persist on the same
    AudioJob columns every other purpose uses — the polling route keys its
    fresh-cache match on them too (a recap generated with a different
    model/think/RAG is a different artifact and must never be served for
    this request), so they have to live on the row, not just the call.

    `extra_instructions`/`min_tokens`/`max_tokens`/`strictness` give Session
    Log the same Condense-style customization (a one-off GM note plus soft/
    firm length targets — see condense_recap's own docstring for the full
    rationale) on the same generic AudioJob columns Condense already uses;
    no new columns were needed. Validated here (not just inside
    summarize_session_from_facts) so a bogus strictness fails at job
    creation, same reasoning as create_condense_job's own validation.
    Threaded into the polling route's fresh-cache match too, for the same
    reason model/think/RAG are: a recap generated under a different length
    target/instruction is a different artifact."""
    if audience not in ("gm", "players"):
        raise ValueError(f"audience must be 'gm' or 'players', got {audience!r}")
    if strictness not in ("guideline", "firm", "strict"):
        raise ValueError(f"strictness must be guideline, firm, or strict, got {strictness!r}")
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="session_log_recap", filename="Session Log recap", status="pending",
            game_session_id=session_id, created_by_user_id=created_by_user_id,
            audience=audience,
            # Empty string falls back to the "recap" surface default — the
            # same selection the original synchronous route made via its
            # _recap_model("") helper (app.routers.sessions can't be
            # imported here — it imports this module — so the one-liner is
            # replicated; if it ever drifts, the model-surface tests catch
            # it). _run_job re-applies the same fallback for NULL/blank, so
            # this seeding is also what the route's cache match compares
            # against.
            model=model or _ai_module.get_defaults().get("recap", ""),
            think=think, use_rag=use_rag,
            rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            extra_instructions=extra_instructions.strip() or None,
            min_tokens=min_tokens, max_tokens=max_tokens, condense_strictness=strictness,
            audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_assist_job(
    world_id: int, *, op: str, surface: str = "assist", content: str = "",
    meta: str = "", instruction: str = "", lang: str = "",
    model: str = "", think: bool = False, use_rag: bool = False,
    rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
    created_by_user_id: Optional[int] = None,
) -> int:
    """Run one AI-assist operation (app/ai_assist.run_assist) as a durable
    background job — sibling of create_condense_job's "text already in
    hand, no transcribe phase" shape, for content too big for the
    interactive POST /api/ai/assist cap (a whole rules document above all)
    or ops a GM would rather not hold a browser request open for.

    Everything the op needs beyond the shared per-job columns lands in
    assist_params_json (op/surface/meta/instruction/lang — see the AudioJob
    column's own docstring); `content` goes to `transcript` (the row's
    "input" slot, same as condense/text-recap jobs) and the result to
    result_json — the run_assist return shape verbatim, so the polling
    route hands the panel the identical payload the interactive route
    would have. Raises ValueError (mapped to a clean 400 by the route) on
    an unknown op, exactly like run_assist itself."""
    if op not in _ai_assist.ALL_OPS:
        raise ValueError(f"Unknown AI assist operation: {op!r}")
    params = {
        "op": op, "surface": surface, "meta": meta,
        "instruction": instruction.strip(), "lang": lang.strip(),
    }
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="ai_assist",
            filename=f"AI assist · {surface}", status="pending",
            created_by_user_id=created_by_user_id,
            model=model or None, think=think,
            extra_instructions=instruction.strip() or None,
            use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            assist_params_json=_json.dumps(params),
            transcript=content or "", audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


def create_world_summary_job(
    world_id: int, *, created_by_user_id: Optional[int] = None,
    model: str = "", think: bool = True, use_rag: bool = False,
    rag_entity_limit: Optional[int] = None, rag_notes_limit: Optional[int] = None,
) -> int:
    """Generate the dashboard's AI world summary as a durable background
    job — the campaign-state digest is assembled at run time (see
    _run_job's world_summary branch: entity roster, open quests, recent
    facts/sessions), which is also why this creator takes no content
    argument. The result lands in `recap` (displayable prose, exactly what
    the session_recap purposes put there) and the polling route caches on
    the newest done row until a Regenerate click POSTs a new job — no
    staleness watermark by design (world state changes constantly; the
    card labels the snapshot with its generation time instead).

    `think` defaults to True (unlike every other purpose here) — the
    dashboard widget's own Thinking checkbox defaults checked, matching how
    it shipped before this parameter existed (world_summary jobs were
    always created with think implicitly True via _run_job's own default
    handling). `use_rag`/rag_*_limit are optional lore-grounding, same
    shape as the ai_assist purpose's own — see _run_job's world_summary
    branch for how the RAG query is built (the assembled state_text itself,
    since a world summary has no separate "content" input to query on)."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="world_summary", filename="World summary", status="pending",
            created_by_user_id=created_by_user_id,
            model=model or (_ai_module.get_defaults().get("assist", "") or None),
            think=think, use_rag=use_rag,
            rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
            audio_path="", delete_after=False,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_id


# Kinds worth hinting Whisper toward — proper nouns a session's spoken
# audio is likely to actually contain. Excludes "note" (free text with no
# guarantee its title is a clean proper noun) and "event"/"item"/"feat"
# (not asked for, and less likely to be spoken names Whisper would
# otherwise autocorrect). Both NPC and PC-subtype characters are included —
# a player's own character's name gets said just as often as any NPC's.
GLOSSARY_ENTITY_KINDS = ("character", "creature", "location", "organization", "race", "profession")
# Cap on how many entity names get merged in — whisper.cpp's own
# initial_prompt shares a real (if not precisely documented) token budget
# with whatever a GM already typed by hand; capping keeps a large world's
# full entity roster from crowding that out entirely rather than silently
# truncating mid-list. Ordered by kind then name for a stable, predictable
# list if a world does have more entities than fit.
GLOSSARY_ENTITY_LIMIT = 50
# GLOSSARY_ENTITY_LIMIT bounds the entity-name COUNT, not their total
# length — whisper.cpp's initial_prompt prompt window is a small, fixed
# token budget in practice, shared with whatever a GM already typed by
# hand (see merge_glossary below), so a long GM-typed glossary plus 50
# potentially-long entity names can still overflow it; whisper silently
# truncates the tail (the entity names, appended last — GM text is never
# trimmed) with no signal anything was dropped. This is a second, byte-
# length cap on top of the count cap. ~600 chars is a conservative
# fraction of that real (undocumented) budget, erring toward dropping a
# few extra names rather than risking the same silent-truncation problem
# this whole mechanism exists to avoid.
_GLOSSARY_ENTITY_CHAR_BUDGET = 600


def entity_glossary_terms(world_id: int, limit: int = GLOSSARY_ENTITY_LIMIT) -> list[str]:
    """Entity names (see GLOSSARY_ENTITY_KINDS) to merge into a world's
    Whisper glossary — see _glossary_for_world, which actually merges
    these into the GM's own typed text. Public (no leading underscore):
    also used by routers/ai.py's GET /whisper/glossary to show a GM how
    many entity names are being added on top of what they typed, since the
    merge happens at transcribe time and isn't visible in the saved
    World.whisper_glossary text itself."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Entity.name)
            .filter(Entity.world_id == world_id, Entity.kind.in_(GLOSSARY_ENTITY_KINDS))
            .order_by(Entity.kind, Entity.name)
            .limit(limit)
            .all()
        )
        return [r[0] for r in rows if r[0]]
    finally:
        db.close()


def merge_glossary(gm_glossary: str, entity_terms: list[str]) -> tuple[str, int, int]:
    """GM-typed terms first (a GM who bothered to type something presumably
    cares about it most, and it's least likely to fall outside whatever
    truncation whisper.cpp's own prompt budget applies), then entity names
    — comma/newline-separated either way, matching whisper_glossary's own
    existing free-text convention. Case-insensitive dedup against the GM's
    own terms so a name that's both hand-typed AND an Entity isn't sent
    twice.

    Entity terms (post-dedup) are appended in order up to
    _GLOSSARY_ENTITY_CHAR_BUDGET total characters — whole-term granularity
    (never truncates mid-name), and GM text is never trimmed. Returns
    (merged_text, included_count, dropped_count) rather than just the
    string: `included`/`dropped` describe entity terms specifically (not
    the GM's own text), and are what GET /api/ai/whisper/glossary surfaces
    so a GM isn't left wondering why a name they know is on the roster
    never gets biased for. Public (no leading underscore): called from
    both _glossary_for_world below and that route, same reasoning
    entity_glossary_terms' own docstring gives."""
    if not entity_terms:
        return gm_glossary, 0, 0
    gm_terms_lower = {t.strip().lower() for t in gm_glossary.replace("\n", ",").split(",") if t.strip()}
    new_terms = [t for t in entity_terms if t.strip().lower() not in gm_terms_lower]
    if not new_terms:
        return gm_glossary, 0, 0
    included = []
    used_chars = 0
    for t in new_terms:
        added = len(t) + 2  # ", " separator
        if used_chars + added > _GLOSSARY_ENTITY_CHAR_BUDGET:
            break
        included.append(t)
        used_chars += added
    dropped = len(new_terms) - len(included)
    if dropped:
        _log.info(
            "merge_glossary: %d entity name(s) dropped past the %d-char budget (%d included)",
            dropped, _GLOSSARY_ENTITY_CHAR_BUDGET, len(included),
        )
    if not included:
        return gm_glossary, 0, dropped
    merged = f"{gm_glossary}, {', '.join(included)}" if gm_glossary else ", ".join(included)
    return merged, len(included), dropped


def _glossary_for_world(world_id: int, game_session_id: int | None = None) -> str:
    """`game_session_id`, if given, feeds names from the GM's "Entities
    Featured" picks for that session (see _session_featured_picks) to the
    FRONT of the entity-name list, ahead of entity_glossary_terms' own
    alphabetical-by-kind list — on a big world, the NPCs actually spoken
    aloud this session could otherwise lose merge_glossary's
    _GLOSSARY_ENTITY_CHAR_BUDGET to alphabetically-earlier entities that
    never came up at all. GM-typed text (World.whisper_glossary) still
    always comes first and is never trimmed — see merge_glossary's own
    docstring; this only affects the ORDER of the entity names appended
    after it, not whether they're included."""
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        gm_glossary = (w.whisper_glossary or "").strip() if w else ""
        featured_names = []
        if game_session_id:
            pinned_entity_ids, pinned_pc_ids = _session_featured_picks(game_session_id)
            if pinned_entity_ids:
                rows = (
                    db.query(Entity.name)
                    .filter(Entity.world_id == world_id, Entity.id.in_(pinned_entity_ids))
                    .order_by(Entity.kind, Entity.name)
                    .all()
                )
                featured_names.extend(r[0] for r in rows if r[0])
            if pinned_pc_ids:
                pc_rows = (
                    db.query(PlayerCharacter.name)
                    .filter(PlayerCharacter.world_id == world_id, PlayerCharacter.id.in_(pinned_pc_ids))
                    .order_by(PlayerCharacter.name)
                    .all()
                )
                featured_names.extend(r[0] for r in pc_rows if r[0])
    finally:
        db.close()
    entity_terms = entity_glossary_terms(world_id)
    if featured_names:
        featured_lower = {n.lower() for n in featured_names}
        entity_terms = featured_names + [t for t in entity_terms if t.lower() not in featured_lower]
    return merge_glossary(gm_glossary, entity_terms)[0]


def _whisper_language_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.whisper_language or "").strip() if w else ""
    finally:
        db.close()


def _denoise_for_world(world_id: int) -> bool:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return bool(w and w.whisper_denoise)
    finally:
        db.close()


def _recap_instructions_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.recap_instructions or "").strip() if w else ""
    finally:
        db.close()


def _format_session_rewards(xp_awarded: int, loot_items: list) -> str:
    """A deterministic — never AI-generated — "Rewards" footer for the
    Facts-based session recap. GameSession.xp_awarded/loot_json are
    GM-entered structured fields the summarize call never sees (only
    Fact.content reaches summarize_session_from_facts), so reporting them
    accurately means computing this separately rather than trusting the
    model to notice, count, or total them up from prose it was never
    given in the first place. Returns "" (nothing to append) when there's
    neither XP nor loot to report, so a session with no rewards yet gets
    no empty/awkward section."""
    lines = []
    if xp_awarded:
        lines.append(f"- **XP awarded:** {xp_awarded}")
    if loot_items:
        item_strs = [f"{it.get('name', '?')} ×{it.get('qty', 1)}" for it in loot_items]
        lines.append(f"- **Loot given:** {', '.join(item_strs)}")
    if not lines:
        return ""
    return "## Rewards\n" + "\n".join(lines)


# Caps for the world-summary digest (purpose="world_summary") — generous
# enough that a real campaign's shape comes through, tight enough that one
# digest can't blow the model's context before the summarize call even
# starts (the caps apply to LINE COUNT; per-line size is bounded by the
# summary fields quoted, not bodies).
_SUMMARY_ENTITY_LIMIT = 120
_SUMMARY_QUEST_LIMIT = 30
_SUMMARY_FACT_LIMIT = 40
_SUMMARY_SESSION_LIMIT = 10


def _world_summary_state_text(world_id: int) -> str:
    """The deterministic campaign-state digest behind the dashboard's world
    summary — assembled fresh at run time (never cached on the row) so a
    Regenerate after play always reflects the world as it stands. One
    line per entity/quest/fact/session, ordered so the newest material
    lands last (recency reads better to a model than alphabetization);
    GM-facing by design (the card is GM/assistant-only), so no visibility
    filtering — secrets included, exactly like the GM's own dashboard."""
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        if not world:
            return ""
        entities = (
            db.query(Entity)
            .filter(Entity.world_id == world_id)
            .order_by(Entity.updated_at.desc())
            .limit(_SUMMARY_ENTITY_LIMIT)
            .all()
        )
        quests = (
            db.query(Quest)
            .filter(Quest.world_id == world_id)
            .order_by(Quest.updated_at.desc())
            .limit(_SUMMARY_QUEST_LIMIT)
            .all()
        )
        facts = (
            db.query(Fact)
            .filter(Fact.world_id == world_id)
            .order_by(Fact.created_at.desc())
            .limit(_SUMMARY_FACT_LIMIT)
            .all()
        )
        sessions = (
            db.query(GameSession)
            .filter(GameSession.world_id == world_id)
            .order_by(GameSession.session_num.desc())
            .limit(_SUMMARY_SESSION_LIMIT)
            .all()
        )
        # A world with no entities/quests/facts/sessions has nothing to
        # summarize — return empty so the run branch reports a clean "no
        # content" error instead of asking the model to riff on a bare
        # world name (which would just invent a campaign).
        if not (entities or quests or facts or sessions):
            return ""
        lines = [f"# {world.name}", world.description or "", ""]
        if entities:
            lines.append(f"## Recent entities ({len(entities)})")
            for e in entities:
                line = f"- [{e.kind}] {e.name}"
                if e.subtype:
                    line += f" ({e.subtype})"
                if e.summary:
                    line += f": {e.summary}"
                lines.append(line)
            lines.append("")
        if quests:
            lines.append(f"## Quests ({len(quests)})")
            for q in quests:
                line = f"- [{q.status or 'active'}] {q.title}"
                if q.summary:
                    line += f": {q.summary}"
                lines.append(line)
            lines.append("")
        if facts:
            lines.append(f"## Recent facts ({len(facts)}, newest first)")
            for f in facts:
                lines.append(f"- {f.content}")
            lines.append("")
        if sessions:
            lines.append(f"## Recent sessions ({len(sessions)})")
            for s in sessions:
                line = f"- #{s.session_num or '?'} {s.title}"
                if s.session_date:
                    line += f" ({s.session_date})"
                if s.summary:
                    line += f": {s.summary[:300]}"
                lines.append(line)
            lines.append("")
        return "\n".join(lines)
    finally:
        db.close()


def _combined_recap_instructions(world_instructions: str, job_instructions: str) -> str:
    """The world's own persistent recap_instructions (a standing GM
    preference, e.g. "always call out combat tactics") always applies;
    job_instructions is a one-off note for this specific run only (e.g.
    "this session was mostly shopping/downtime, keep it short"). Neither
    replaces the other — both get passed to summarize_transcript together,
    world-level guidance first so a longer one-off note can't crowd it out
    of the prompt."""
    parts = [p for p in (world_instructions.strip(), job_instructions.strip()) if p]
    return "\n\n".join(parts)


# Defaults used when a job has use_rag=True but the GM left the entity/notes
# limit fields blank — same ballpark as _SmartCtxBody's own defaults
# (limit=25, notes_limit=5) in app.main, just a bit tighter on entities since
# these land in a system prompt alongside a whole transcript/recap rather
# than a short chat message.
_DEFAULT_RAG_ENTITY_LIMIT = 15
_DEFAULT_RAG_NOTES_LIMIT = 5

# Not GM-configurable (unlike entity_limit/notes_limit above) — same
# hardcoded-cap precedent as app.routers.chronicler's own _CHRONICLER_FACT_
# LIMIT: most-recent-first with no relevance filtering, since a campaign's
# total logged-Fact volume is naturally small enough that simple recency
# already covers what a "previously established" recap needs, without
# adding a third RAG limit for a GM to configure.
_RAG_FACT_LIMIT = 20

# The FTS query app.retrieval.find_relevant_entities builds has one OR-clause
# per unique word over 3 characters in the query text — capped here so
# handing it an entire transcript unclipped can't balloon into a
# query with thousands of clauses; the session's real subject matter/proper
# nouns are already well represented in the first few thousand characters.
_RAG_QUERY_CHAR_BUDGET = 4000


def _session_featured_picks(game_session_id: int) -> tuple[list[int], list[int]]:
    """(entity_ids, player_character_ids) a GM checked in the Sessions
    page's "Entities Featured" picker (app.routers.sessions.
    _featured_entity_candidates), read back off GameSession.npcs_json — a
    [{entity_id, name, kind}] list, saved by the ordinary session_edit form
    POST. These become _build_rag_context's `pinned_entity_ids`/
    `pinned_pc_ids`: a GM-curated, deterministic set that's included
    regardless of keyword search, unlike everything else RAG retrieves —
    see _build_rag_context's own docstring for why that matters most for a
    session whose transcript is in a different language than the World's
    entity names.

    A pre-existing row saved before PlayerCharacter picks existed has no
    "kind" key at all — defaults to "entity", same convention session_
    detail's own read path uses. Reads from the DB, not any in-browser/
    unsaved checkbox state — a GM needs to Save the session before a
    Condense/Summarize run picks up their latest picks."""
    db = SessionLocal()
    try:
        gs = db.get(GameSession, game_session_id)
        if not gs or not gs.npcs_json:
            return [], []
        try:
            picks = _json.loads(gs.npcs_json)
        except (ValueError, TypeError):
            return [], []
        entity_ids, pc_ids = [], []
        for n in picks:
            try:
                eid = n["entity_id"]
            except (KeyError, TypeError):
                continue
            (pc_ids if n.get("kind") == "player_character" else entity_ids).append(eid)
        return entity_ids, pc_ids
    finally:
        db.close()


def _format_pc_line(pc: PlayerCharacter) -> str:
    """One reference line for a pinned PlayerCharacter — same rough shape
    app.retrieval.format_context_from_entities uses for an Entity, but
    PlayerCharacter isn't an Entity (its own id sequence, its own table),
    so it can't just be handed to that function."""
    detail = " ".join(x for x in (pc.char_class, pc.race) if x)
    line = f"- [player character] {pc.name}"
    if detail:
        line += f" ({detail})"
    if pc.background:
        line += f": {pc.background}"
    return line


def _build_rag_context(
    world_id: int, query: str, entity_limit: int, notes_limit: int,
    pinned_entity_ids: Optional[list[int]] = None, pinned_pc_ids: Optional[list[int]] = None,
) -> str:
    """RAG retrieval for a summarize/condense job's system prompt (see
    app.ai._with_world_context, which is what actually prepends the result
    onto the system prompt) — reuses app.retrieval's entity search
    (find_relevant_entities) and its notes-guarantee logic verbatim rather
    than keeping a second copy of either; this is the same retrieval
    /api/ai/world-context-smart (AI Chat's RAG panel) is built on.

    entity_limit/notes_limit <= 0 means "don't retrieve that category at
    all" — mirrors _SmartCtxBody's own notes_limit convention (0 there
    already means "skip the guaranteed-notes fetch").

    Non-note entities get the same "top up toward the limit" treatment as
    notes already had, for a case find_relevant_entities' own keyword
    search can't handle: a `query` in a different language/script than
    the World's entity names (e.g. a Russian session transcript against
    English-named characters/places). find_relevant_entities' FTS/ILIKE
    matching has no literal text overlap to find there — it comes back
    empty even though there ARE relevant entities to reference — while its
    OWN "no query words at all" fallback (returning entities ordered by
    kind, name) never triggers, because a foreign-language query still
    splits into plenty of real "words," just ones that can't match
    anything. Without this top-up, a GM running non-English sessions would
    silently get a RAG context with notes but no characters/places at all
    — exactly the case this feature exists for: the model can usually
    still recognize a phonetic/translated rendering of an established
    name once it has the reference list on hand (see _with_world_context's
    own instruction wording), it just needs to actually be given one.

    `pinned_entity_ids`/`pinned_pc_ids` (see _session_featured_picks) are
    ALWAYS included, listed first, and never count against entity_limit/
    notes_limit — a GM's own deliberate "this session featured these"
    picks are a much stronger signal than keyword search, so they're
    guaranteed rather than competing with it for budget. Everything else
    (keyword search, its top-up, the guaranteed-recent-notes fetch) then
    excludes whatever's already pinned. pinned_pc_ids is a separate id
    space from pinned_entity_ids — PlayerCharacter isn't an Entity, has no
    `kind`/`summary` of its own, and is never subject to the keyword
    search or top-up above, only ever appearing here because it was
    pinned (see _format_pc_line).

    Also includes the world's most recent logged Facts (see
    _RAG_FACT_LIMIT) as their own "Established facts" block — previously
    this function only ever drew on Entities/Notes, leaving everything a
    GM had logged as a discrete Fact (session-to-session continuity: named
    NPCs, promises made, secrets already revealed) invisible to a
    Summarize/Condense call even with RAG turned on. Unfiltered by
    visible_to_players — this context is GM-facing only (it feeds the
    recap-WRITING prompt, never anything shown to players directly), same
    "GM sees everything" reasoning app.routers.chronicler's own fact
    lookup for a GM caller uses."""
    db = SessionLocal()
    try:
        pinned = (
            db.query(Entity)
            .filter(Entity.world_id == world_id, Entity.id.in_(pinned_entity_ids))
            .order_by(Entity.kind, Entity.name)
            .all()
            if pinned_entity_ids else []
        )
        pinned_pcs = (
            db.query(PlayerCharacter)
            .filter(PlayerCharacter.world_id == world_id, PlayerCharacter.id.in_(pinned_pc_ids))
            .order_by(PlayerCharacter.name)
            .all()
            if pinned_pc_ids else []
        )
        seen_ids = {e.id for e in pinned}

        entities = (
            _retrieval.find_relevant_entities(db, world_id, query[:_RAG_QUERY_CHAR_BUDGET], limit=entity_limit)
            if entity_limit > 0 else []
        )
        notes = [e for e in entities if e.kind == "note" and e.id not in seen_ids]
        non_notes = [e for e in entities if e.kind != "note" and e.id not in seen_ids]
        seen_ids |= {e.id for e in non_notes} | {e.id for e in notes}
        if entity_limit > 0 and len(non_notes) < entity_limit:
            topup_q = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind != "note")
            if seen_ids:
                topup_q = topup_q.filter(~Entity.id.in_(seen_ids))
            topup = topup_q.order_by(Entity.kind, Entity.name).limit(entity_limit - len(non_notes)).all()
            non_notes = non_notes + topup
            seen_ids |= {e.id for e in topup}
        if notes_limit > 0:
            note_entities = (
                db.query(Entity)
                .filter(Entity.world_id == world_id, Entity.kind == "note")
                .order_by(Entity.updated_at.desc())
                .limit(notes_limit)
                .all()
            )
            notes = notes + [e for e in note_entities if e.id not in seen_ids]

        pinned_notes = [e for e in pinned if e.kind == "note"]
        pinned_non_notes = [e for e in pinned if e.kind != "note"]
        entity_context = _retrieval.format_context_from_entities(pinned_non_notes + non_notes + pinned_notes + notes)
        pc_context = "\n".join(_format_pc_line(pc) for pc in pinned_pcs)

        facts = (
            db.query(Fact)
            .filter(Fact.world_id == world_id)
            .order_by(Fact.created_at.desc())
            .limit(_RAG_FACT_LIMIT)
            .all()
        )
        fact_context = ""
        if facts:
            fact_lines = "\n".join(f"- {f.content}" for f in reversed(facts))  # oldest-first reads as a timeline
            fact_context = "Established facts from past sessions:\n" + fact_lines

        return "\n".join(part for part in (pc_context, entity_context, fact_context) if part)
    finally:
        db.close()


async def _run_job(job_id: int) -> None:
    """Runs (or resumes) a job's transcribe [+ summarize] work. Everything
    this used to take as function arguments (audio_path, purpose,
    delete_after, model, world_id, extra_instructions) now lives on the
    row instead, read once at the top — create_job and start_resume_job
    both just start this same task, rather than one being a subtly
    different reimplementation of the other. purpose="condense" jobs (see
    create_condense_job) are seeded with job.transcript already holding
    the text to condense and no audio_path at all, so the "already has a
    transcript" carve-out just below skips the transcribe phase
    unconditionally on their very first run too — there is no audio to
    transcribe in the first place.

    If the row already has a transcript (job.transcript non-empty) AND
    there's no still-outstanding transcribe-phase checkpoint, transcription
    is skipped entirely and this picks up straight at summarizing, using
    that saved transcript — true when resuming a job interrupted during or
    after the summarize phase. The transcribe-phase-checkpoint carve-out
    matters because a checkpoint's own mirror (see _checkpoint below)
    ALSO populates job.transcript while transcription is only partway
    through the audio (see app.ai.transcribe_audio's on_checkpoint
    contract) — without it, resuming a job interrupted mid-transcription
    would skip straight to summarizing (or, for purpose="attachment",
    straight to "done") on only a partial transcript. A checkpoint left
    on the row (job.checkpoint_json) is passed through to whichever phase
    it belongs to; app.ai's transcribe_audio/summarize_transcript each
    validate it still matches before trusting it (see their own
    docstrings) and discard/start over silently if not."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            return
        audio_path = Path(job.audio_path) if job.audio_path else None
        purpose = job.purpose
        delete_after = job.delete_after
        # A GM who never picked a model for this specific job falls back to
        # the "recap" surface default (Models tab) rather than straight to
        # the single instance-wide default — same per-surface convention
        # already used for chat/ask_ai/image (see DEFAULT_SURFACES).
        model = job.model or _ai_module.get_defaults().get("recap", "")
        world_id = job.world_id
        game_session_id = job.game_session_id
        extra_instructions = job.extra_instructions or ""
        # NULL (a pre-migration row that never had a "Thinking" checkbox to
        # begin with) is treated as True — see AudioJob.think's docstring.
        think = job.think if job.think is not None else True
        fit_context = bool(job.fit_context)
        min_tokens = job.min_tokens
        max_tokens = job.max_tokens
        # NULL (a pre-migration row, or one created before strictness
        # existed) reads as the "guideline" default — same convention
        # _run_job applies to job.think above.
        strictness = job.condense_strictness or "guideline"
        use_rag = bool(job.use_rag)
        rag_entity_limit = job.rag_entity_limit
        rag_notes_limit = job.rag_notes_limit
        existing_transcript = job.transcript or ""
        # purpose="session_log_recap" only — which fact-visibility filter
        # this job's recap must be built from (see AudioJob.audience). NULL
        # (a pre-migration row, or any other purpose) reads as the player
        # tier, the more restrictive of the two.
        audience = job.audience or "players"
        # purpose="ai_assist" only — the op's parameters (see
        # AudioJob.assist_params_json). Parsed defensively: a malformed
        # blob (hand-edited DB, a truncated write) must not crash the run
        # before the generic error handling can record it.
        try:
            assist_params = _json.loads(job.assist_params_json) if job.assist_params_json else {}
        except Exception:
            _log.warning("audio job %s: malformed assist_params_json — treating as empty", job_id)
            assist_params = {}
        checkpoint = _json.loads(job.checkpoint_json) if job.checkpoint_json else None
    finally:
        db.close()

    def _set(**fields):
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    def _checkpoint(state: dict) -> None:
        fields = {"checkpoint_json": _json.dumps(state)}
        if state.get("phase") == "transcribe":
            # Mirrored into `transcript` (not just checkpoint_json) so the
            # partial is independently useful the same way WhisperError's
            # own partial_transcript already is — the existing "Retry
            # summary"/"Extract facts" actions key off job.transcript, and
            # a GM watching the Background Jobs page sees real progress
            # without waiting for the job to finish. Summarization
            # checkpoints are deliberately NOT mirrored into `recap` — a
            # half-written recap would look like a finished answer instead
            # of a work in progress.
            fields["transcript"] = state.get("text", "")
        _set(**fields)

    progress = {
        "current": (checkpoint or {}).get("chunks_done") or (checkpoint or {}).get("parts_done"),
        "total": (checkpoint or {}).get("chunk_total"),
    }

    def _on_progress(current, total):
        progress["current"] = current
        progress["total"] = total
        _set(chunk_current=current, chunk_total=total)

    keep_audio = False
    try:
        # A transcript on the row means transcription is DONE — except a
        # checkpoint's own mirror (see _checkpoint above) also populates
        # job.transcript while a transcribe-phase resume is still only
        # partially through the audio, so a checkpoint still on
        # "transcribe" always wins regardless of what transcript already
        # holds (same phase-precedence start_resume_job's own dispatch
        # uses, see its docstring — this must never disagree with it, or a
        # resumed job could skip straight to a purpose="attachment" "done"
        # after transcribing only part of the recording).
        # purpose="session_log_recap" has no transcribe phase at all — it's
        # pure summarization (of Fact rows, not of any transcript), so it
        # must skip unconditionally even though its transcript is empty
        # (there IS no input text to seed); without the carve-out the
        # empty-transcript path above would send audio_path=None into
        # transcribe_audio. Same for purpose="ai_assist" (an op whose
        # content can legitimately be empty — table_entries works off meta
        # + instruction alone) and purpose="world_summary" (its input is
        # assembled fresh from the DB at run time, never from `transcript`).
        skip_transcribe = (
            (bool(existing_transcript) or purpose in ("session_log_recap", "ai_assist", "world_summary"))
            and not (checkpoint and checkpoint.get("phase") == "transcribe")
        )
        if not skip_transcribe:
            _set(status="transcribing", run_started_at=datetime.utcnow(), finished_at=None)
            glossary = _glossary_for_world(world_id, game_session_id) if world_id else ""
            language = _whisper_language_for_world(world_id) if world_id else ""
            denoise = _denoise_for_world(world_id) if world_id else False
            transcribe_resume = checkpoint if checkpoint and checkpoint.get("phase") == "transcribe" else None
            try:
                # Held for this whole call (all its internal chunks), not
                # just one HTTP request — see ai.whisper_job_semaphore's own
                # docstring for why: it's what actually stops two jobs'
                # chunks from interleaving on the same Whisper backend.
                async with _ai_module.whisper_job_semaphore:
                    transcript = await _ai_module.transcribe_audio(
                        audio_path, glossary=glossary, language=language, denoise=denoise,
                        on_progress=_on_progress, on_checkpoint=_checkpoint,
                        should_stop=_job_shutdown.stopping, resume=transcribe_resume,
                    )
            except _ai_module.WhisperError as exc:
                fields = {"status": "error", "error": str(exc), "finished_at": datetime.utcnow(), "checkpoint_json": ""}
                if exc.partial_transcript:
                    # At least one chunk transcribed before the failure —
                    # save it so the GM can resummarize from the salvaged
                    # partial (start_resummarize_job only needs
                    # job.transcript) instead of re-uploading and
                    # re-transcribing the whole recording.
                    fields["transcript"] = exc.partial_transcript
                _set(**fields)
                return
            if not transcript:
                _set(status="error", finished_at=datetime.utcnow(), checkpoint_json="", error=(
                    "Whisper transcribed this clip successfully but found no speech in it "
                    "— check the recording actually captured audio."
                ))
                return
            _set(transcript=transcript, chunk_current=None, chunk_total=None, checkpoint_json="")
        else:
            transcript = existing_transcript

        world_context = ""
        if use_rag and world_id and purpose in ("condense", "session_recap", "facts_parse"):
            # Query the transcript/text-to-condense itself — there's no
            # separate short "user question" here the way AI Chat's RAG has
            # one, so the input being summarized IS the best signal for what
            # entities/notes are relevant to it (see _build_rag_context's
            # own docstring for the query-length cap this relies on). For
            # purpose="facts_parse" the "transcript" is the pasted recap
            # text (create_facts_parse_job stores it there), so the same
            # convention carries over unchanged. purpose="ai_assist" and
            # purpose="world_summary" both build their own RAG in their
            # branches below instead of here — ai_assist because a
            # content-less op needs the meta+instruction fallback as its
            # query, world_summary because its query text (the assembled
            # campaign-state digest) doesn't exist yet at this point in the
            # function (see _world_summary_state_text, called further down).
            # pinned_entity_ids/pinned_pc_ids: whatever the GM checked in
            # this session's own "Entities Featured" picker (see _session_
            # featured_picks) — guaranteed inclusion regardless of what the
            # keyword search above finds, not just this run's best-effort
            # query.
            pinned_entity_ids, pinned_pc_ids = _session_featured_picks(game_session_id) if game_session_id else ([], [])
            world_context = _build_rag_context(
                world_id, transcript,
                rag_entity_limit if rag_entity_limit is not None else _DEFAULT_RAG_ENTITY_LIMIT,
                rag_notes_limit if rag_notes_limit is not None else _DEFAULT_RAG_NOTES_LIMIT,
                pinned_entity_ids=pinned_entity_ids, pinned_pc_ids=pinned_pc_ids,
            )

        # Each rung below only ever runs because the previous one hit
        # generate_chat's own thinking-starved sentinel (hidden reasoning
        # burned the whole output budget, no visible answer) — any other
        # failure breaks out of the loop immediately into the normal error
        # handling further down. See
        # docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1 for why this is
        # a climbing ladder rather than a single think=False fallback: a
        # model that ignores think=False and starves anyway must not retry
        # into a budget SMALLER than the attempt that just failed — the
        # bug that motivated this ladder. A think=True job climbs
        # (normal budget) -> (expanded budget, same think) -> (expanded
        # budget, think=False) — the think-flip is saved for last so the
        # "still-thinking" attempts get every chance first. A think=False
        # job climbs (normal) -> (expanded) only — its think value never
        # flips against the GM's explicit choice, but it still gets one
        # expanded retry since a model can starve on think=False too (an
        # unset num_predict is then bounded only by num_ctx, or a model can
        # simply ignore think=False outright — the same failure mode the
        # sentinel exists to catch). All rungs stay inside the existing
        # ollama_job_semaphore span so a retry never interleaves with
        # another job's calls.
        attempt_plans = ([(True, False), (True, True), (False, True)]
                          if think else [(False, False), (False, True)])

        if purpose == "condense":
            # No chunking/checkpoint support — condense_recap is always a
            # single call (that's the whole point of fit_context: size
            # num_ctx to fit the entire input rather than splitting it), so
            # there's no partial-progress state to persist between chunks
            # the way session_recap's map-reduce path has.
            _set(status="summarizing")
            # Same world-level recap_instructions + this job's own one-off
            # extra_instructions combination session_recap uses just below —
            # a GM's standing "always write in French" preference should
            # steer Condense too, not just the initial summarize.
            instructions = _combined_recap_instructions(
                _recap_instructions_for_world(world_id) if world_id else "", extra_instructions,
            )
            for attempt_idx, (attempt_think, attempt_expanded) in enumerate(attempt_plans):
                # See condense_call_options' own docstring: sizes num_ctx to
                # actually fit transcript + instructions + world_context +
                # the requested output length whenever fit_context was
                # explicitly asked for, OR whenever the plain (non-fit) call
                # would otherwise risk silently overflowing the GM's
                # configured/assumed context — the failure mode for the
                # latter isn't a clean error, it's the model responding
                # with garbage. Recomputed per rung since think/expanded differ.
                options = _ai_module.condense_call_options(
                    transcript, extra_instructions=instructions, world_context=world_context,
                    max_tokens=max_tokens, think=attempt_think, force_fit=fit_context,
                    expanded=attempt_expanded,
                )
                # See ai.ollama_job_semaphore's own docstring — held for the
                # whole call, same reasoning as the transcribe semaphore above.
                async with _ai_module.ollama_job_semaphore:
                    recap = await _ai_module.condense_recap(
                        transcript, model=model, options=options, think=attempt_think,
                        extra_instructions=instructions, min_tokens=min_tokens, max_tokens=max_tokens,
                        world_context=world_context, expanded_thinking=attempt_expanded,
                        strictness=strictness,
                    )
                if not _ai_module.is_thinking_starved_sentinel(recap):
                    break
                if attempt_idx == len(attempt_plans) - 1:
                    break  # ladder exhausted — _looks_like_failure below reports it
                next_think, next_expanded = attempt_plans[attempt_idx + 1]
                _log.warning(
                    "condense job %s: thinking starved (think=%s expanded=%s) — climbing to (think=%s expanded=%s)",
                    job_id, attempt_think, attempt_expanded, next_think, next_expanded,
                )
                if next_think != attempt_think:
                    _set(think=False, think_fallback=True, expanded_thinking=next_expanded)
                else:
                    _set(expanded_thinking=next_expanded)
            # Strict mode's out-of-band length check. Everything above steers
            # the model via prompt wording alone — a prompt instruction,
            # however firmly worded, is still just a request, so "strict"
            # closes the loop by MEASURING the finished recap with the same
            # coarse chars-per-token estimator the Background Jobs /
            # session page labels use (_chars_per_token_estimate — keeps
            # this check consistent with the "Transcript: ~N tokens ·
            # Recap: ~M tokens" numbers the GM can already see, so the
            # verdict never disagrees with the displayed sizes) and
            # re-running ONCE when the result lands outside the requested
            # range. The 15% tolerance band exists because those estimates
            # are approximate (±15% easily swallows several hundred real
            # tokens on a long recap) — a 1400-token draft against a 1500
            # minimum is inside the noise, and punishing it with a
            # full-priced re-run buys nothing. Only one retry: a strict
            # re-run doubles the job's AI cost, and past a single
            # correction pass the model has already shown where its length
            # instincts sit — editing the result by hand is cheaper for the
            # GM than an unbounded loop of nudges. A retry that itself
            # fails/starves is discarded in favor of the first recap,
            # which was at least a usable answer. The retry reuses the
            # WINNING rung's options/think/expanded (the loop variables
            # still hold them — the loop only ever breaks on the rung that
            # produced the recap being checked), so the re-run is
            # apples-to-apples with the attempt that just missed the range
            # rather than resetting to the job's base thinking settings.
            if (
                strictness == "strict" and (min_tokens or max_tokens)
                and not _looks_like_failure(recap)
                and not _ai_module.is_thinking_starved_sentinel(recap)
            ):
                est_tokens = -(-len(recap) // _ai_module._chars_per_token_estimate(recap))
                below = min_tokens and est_tokens < min_tokens * 0.85
                above = max_tokens and est_tokens > max_tokens * 1.15
                if below or above:
                    if below:
                        violation_note = (
                            f"Your previous draft was ~{est_tokens} tokens; the requirement is at least "
                            f"~{min_tokens} tokens. Expand it with more specific detail from the recap."
                        )
                    else:
                        violation_note = (
                            f"Your previous draft was ~{est_tokens} tokens; the requirement is at most "
                            f"~{max_tokens} tokens. Trim it to fit."
                        )
                    _log.warning(
                        "condense job %s: strict length check failed (estimated ~%s tokens vs min=%s max=%s) — one strict retry",
                        job_id, est_tokens, min_tokens, max_tokens,
                    )
                    async with _ai_module.ollama_job_semaphore:
                        retry_recap = await _ai_module.condense_recap(
                            transcript, model=model, options=options, think=attempt_think,
                            extra_instructions=_combined_recap_instructions(instructions, violation_note),
                            min_tokens=min_tokens, max_tokens=max_tokens,
                            world_context=world_context, expanded_thinking=attempt_expanded,
                            strictness=strictness,
                        )
                    if not _looks_like_failure(retry_recap) and not _ai_module.is_thinking_starved_sentinel(retry_recap):
                        recap = retry_recap
            if _looks_like_failure(recap):
                _set(status="error", error=recap, finished_at=datetime.utcnow())
            elif think and _ai_module.model_rejected_thinking(model):
                # generate_chat/stream_chat already recovered internally
                # (see app.ai) rather than failing this job — the ladder
                # above never saw it, since it only climbs on
                # is_thinking_starved_sentinel. Label the result so the
                # Retry-summary UI's Thinking checkbox reflects what
                # actually produced the recap, same reasoning as
                # think_fallback just above. Two flavors: a model nobody
                # vouched for really did run with thinking off (flip think,
                # point the GM at the override), while a VOUCHED model that
                # got rejected was served reasoning via the <|think|> prompt
                # token instead (ollama#16936's missing capability tag on
                # hf.co imports) — keep think=True there and flag
                # think_token_fallback so the UI explains it informationally
                # rather than telling the GM to disable the very override
                # that powers the workaround.
                if _ai_module.model_thinks_via_prompt_token(model):
                    _set(status="done", recap=recap, think_rejected=True, think_token_fallback=True,
                         finished_at=datetime.utcnow())
                else:
                    _set(status="done", recap=recap, think=False, think_rejected=True, finished_at=datetime.utcnow())
            else:
                _set(status="done", recap=recap, finished_at=datetime.utcnow())
        elif purpose == "session_recap":
            _set(status="summarizing")
            instructions = _combined_recap_instructions(
                _recap_instructions_for_world(world_id) if world_id else "", extra_instructions,
            )
            summarize_resume = checkpoint if checkpoint and checkpoint.get("phase") == "summarize" else None
            # Tracks the freshest summarize-phase checkpoint across rungs —
            # seeded from a checkpoint already on the row (resuming after a
            # restart), then kept current as this run's own rungs write new
            # ones, so a rung that starves partway through several chunks
            # doesn't force the next rung to redo the ones it already
            # finished. chunk_chars only depends on `think` (never on
            # expanded_thinking — see summarize_transcript's own docstring),
            # so a checkpoint is resumable by any rung whose think value
            # matches the one it was written under.
            latest_checkpoint = {"state": summarize_resume}

            def _summarize_checkpoint(state):
                latest_checkpoint["state"] = state
                _checkpoint(state)

            for attempt_idx, (attempt_think, attempt_expanded) in enumerate(attempt_plans):
                resume_for_attempt = latest_checkpoint["state"] if attempt_think == think else None
                async with _ai_module.ollama_job_semaphore:
                    recap = await _ai_module.summarize_transcript(
                        transcript, model=model, extra_instructions=instructions,
                        on_progress=_on_progress, on_checkpoint=_summarize_checkpoint,
                        should_stop=_job_shutdown.stopping, resume=resume_for_attempt, think=attempt_think,
                        world_context=world_context, expanded_thinking=attempt_expanded,
                    )
                if not _ai_module.is_thinking_starved_sentinel(recap):
                    break
                if attempt_idx == len(attempt_plans) - 1:
                    break  # ladder exhausted — _looks_like_failure below reports it
                next_think, next_expanded = attempt_plans[attempt_idx + 1]
                _log.warning(
                    "session_recap job %s: thinking starved (think=%s expanded=%s) — climbing to (think=%s expanded=%s)",
                    job_id, attempt_think, attempt_expanded, next_think, next_expanded,
                )
                if next_think != attempt_think:
                    _set(think=False, think_fallback=True, expanded_thinking=next_expanded)
                else:
                    _set(expanded_thinking=next_expanded)
            if _looks_like_failure(recap):
                _set(status="error", error=recap, chunk_current=None, chunk_total=None,
                     finished_at=datetime.utcnow(), checkpoint_json="")
            else:
                # Auto-draft Facts from this transcript now that the recap
                # succeeded — see _auto_extract_pending_facts's own
                # docstring for why this is best-effort (never turns a good
                # recap into a failed job) and never auto-commits anything;
                # the GM reviews/confirms on the Background Jobs page.
                pending_facts_json = await _auto_extract_pending_facts(job_id, transcript, model)
                if think and _ai_module.model_rejected_thinking(model):
                    # See the identical check in the condense branch above —
                    # including its two flavors (plain rejection vs the
                    # <|think|> prompt-token workaround for vouched models).
                    if _ai_module.model_thinks_via_prompt_token(model):
                        _set(status="done", recap=recap, think_rejected=True, think_token_fallback=True,
                             chunk_current=None, chunk_total=None, finished_at=datetime.utcnow(), checkpoint_json="",
                             pending_facts_json=pending_facts_json)
                    else:
                        _set(status="done", recap=recap, think=False, think_rejected=True, chunk_current=None,
                             chunk_total=None, finished_at=datetime.utcnow(), checkpoint_json="",
                             pending_facts_json=pending_facts_json)
                else:
                    _set(status="done", recap=recap, chunk_current=None, chunk_total=None,
                         finished_at=datetime.utcnow(), checkpoint_json="", pending_facts_json=pending_facts_json)
        elif purpose == "facts_parse":
            # parse_facts_from_recap chunks a long paste into several
            # schema-constrained calls (the same budget/split machinery
            # summarize_transcript's map-reduce uses — see app.ai), so
            # _on_progress gives the job card real "part X/Y" progress
            # instead of an undifferentiated "summarizing" placeholder for a
            # many-minute parse. Held inside ollama_job_semaphore for the
            # whole run now that it IS a multi-chunk run — same interleaving
            # guard the summarize purposes use. No thinking-retry ladder
            # though: the parse has its own per-chunk <|think|> rejection
            # recovery (see app.ai._parse_facts_chat_call) and its
            # ValueError contract is what lands in job.error below.
            _set(status="summarizing")
            async with _ai_module.ollama_job_semaphore:
                try:
                    # think/world_context: the Facts page's own Thinking checkbox
                    # and RAG opt-in (persisted on the row by
                    # create_facts_parse_job). world_context prepends retrieved
                    # World lore to the parse's user message for name accuracy —
                    # parse_facts_from_recap frames it with the same
                    # _with_world_context wording condense_recap uses.
                    # extra_instructions: the Facts page's own one-off steering
                    # note (same Condense-style field, this purpose's own
                    # column already read into scope above).
                    facts = await _ai_module.parse_facts_from_recap(
                        transcript, model=model, think=think, world_context=world_context,
                        extra_instructions=extra_instructions,
                        on_progress=_on_progress,
                    )
                except ValueError as exc:
                    # parse_facts_from_recap raises ValueError when EVERY
                    # chunk failed (Ollama down, unusable JSON — see its
                    # docstring), already worded for a GM — the same message
                    # the synchronous /api/facts/parse maps to HTTP 502.
                    _set(status="error", error=str(exc), chunk_current=None, chunk_total=None,
                         finished_at=datetime.utcnow(), checkpoint_json="")
                    return
            # An empty list is SUCCESS, not an error: the model understood
            # the text and found no in-character facts in it (out-of-character
            # chatter) — the Facts page's UI explains that to the GM. Chunk
            # progress fields clear on completion the way session_recap's
            # done-branch does, so a finished card never shows a stale
            # "part X/Y".
            _set(status="done", result_json=_json.dumps(facts), chunk_current=None, chunk_total=None,
                 finished_at=datetime.utcnow(), checkpoint_json="")
        elif purpose == "session_log_recap":
            # One summarize_session_from_facts call over this session's Fact
            # rows — the identical call the old synchronous POST
            # /api/session-log/{id}/recap made inline (same model/extra_
            # instructions selection, same default think=True), just not
            # inside one HTTP request anymore. Held inside
            # ollama_job_semaphore like the condense/session_recap branches:
            # a think=True summarize against a CPU-local model runs for
            # minutes, and an unheld one would interleave with (and evict
            # the KV cache of) every other queued summarization for no
            # benefit — there is no ladder/checkpoint machinery to mirror,
            # just the one call. The result lands in result_json (never
            # `recap`) so the jobs UI can't render it as a session_recap-
            # style draft.
            _set(status="summarizing")
            db2 = SessionLocal()
            try:
                gs = db2.get(GameSession, game_session_id) if game_session_id else None
                if gs is None:
                    # The session was deleted between the POST that created
                    # this job and the job actually running — nothing left to
                    # recap. Terminal error, not a crash: a stale pending row
                    # would block every future recap attempt for this
                    # (session, audience) forever, since the polling route
                    # waits on pending rows.
                    _set(status="error", error="This session no longer exists.",
                         finished_at=datetime.utcnow(), checkpoint_json="")
                    return
                q = db2.query(Fact).filter(Fact.game_session_id == game_session_id)
                # Same visibility boundary the synchronous route applied per
                # caller: a GM's recap weaves in every fact, players' only the
                # ones marked visible (NULL counts as visible, matching the
                # route's isnot(False)).
                if audience != "gm":
                    q = q.filter(Fact.visible_to_players.isnot(False))
                facts = q.order_by(Fact.created_at).all()
                # World-level standing preference always applies; `extra_
                # instructions` (the row's own field, same column every
                # other purpose uses) is this recap's one-off GM note — see
                # _combined_recap_instructions' own docstring. Session Log's
                # own "Extra instructions"/min/max-tokens/strictness fields
                # persist on these same generic AudioJob columns, so no new
                # columns were needed to give this purpose the same
                # customization Condense already has.
                instructions = _combined_recap_instructions(
                    _recap_instructions_for_world(gs.world_id), extra_instructions,
                )
                xp_awarded = gs.xp_awarded or 0
                try:
                    loot_items = _json.loads(gs.loot_json or "[]")
                except (TypeError, ValueError):
                    loot_items = []
            finally:
                db2.close()
            if not facts:
                # Mirrors the route's old {"recap": "", "empty": true}
                # no-facts shape — the client distinguishes "genuinely
                # nothing logged yet" from a produced-but-empty recap.
                _set(status="done", result_json=_json.dumps({"recap": "", "empty": True}),
                     finished_at=datetime.utcnow(), checkpoint_json="")
                return
            if use_rag and world_id:
                # Built here rather than in the shared RAG block above
                # because this purpose's transcript is empty by design — the
                # facts ARE the input, and only they (already visibility-
                # filtered per `audience` above) make a useful relevance
                # query. Same "the text being summarized is the query"
                # convention the shared block applies to the transcript
                # (_build_rag_context caps the query length itself), and the
                # same session's "Entities Featured" pins and blank-limit
                # defaults as every other RAG-backed purpose.
                pinned_entity_ids, pinned_pc_ids = _session_featured_picks(game_session_id)
                world_context = _build_rag_context(
                    world_id, "\n".join(f.content for f in facts),
                    rag_entity_limit if rag_entity_limit is not None else _DEFAULT_RAG_ENTITY_LIMIT,
                    rag_notes_limit if rag_notes_limit is not None else _DEFAULT_RAG_NOTES_LIMIT,
                    pinned_entity_ids=pinned_entity_ids, pinned_pc_ids=pinned_pc_ids,
                )
            # think: the Session Log page's own "Thinking" checkbox (column
            # NULL on pre-feature rows already read as True at the top of
            # this function); world_context: RAG-retrieved lore, framed as
            # supplementary reference by summarize_session_from_facts itself.
            # min_tokens/max_tokens/strictness: same Condense-style length-
            # target knobs, now available on Session Log's own settings UI.
            async with _ai_module.ollama_job_semaphore:
                recap = await _ai_module.summarize_session_from_facts(
                    [f.content for f in facts], model=model,
                    extra_instructions=instructions,
                    think=think, world_context=world_context,
                    min_tokens=min_tokens, max_tokens=max_tokens, strictness=strictness,
                )
            # generate_chat never raises on an Ollama-side failure — it
            # returns a failure-sentinel STRING instead, and there's no
            # thinking-ladder here to climb first (this branch is one call).
            # Without this check the sentinel was cached as a DONE recap in
            # result_json and served to every poller until the next fact
            # edit — a cached "[AI unavailable: ...]" is worse than no cache
            # at all, since it looks exactly like a real recap to the route.
            # Mirrors the condense (~above) and session_recap branches'
            # terminal-error handling; the route then never serves it (only
            # done rows are) and its poller surfaces job.error instead.
            if _looks_like_failure(recap) or _ai_module.is_thinking_starved_sentinel(recap):
                _set(status="error", error=recap, finished_at=datetime.utcnow(), checkpoint_json="")
                return
            # Same "strict" auto-retry-once-on-violation the condense branch
            # applies (~above) — see its own comment for the full rationale.
            # There's no thinking-ladder rung to reuse here (this purpose is
            # always a single call), so the retry just re-runs the same call
            # with the violation noted as an extra instruction.
            if (
                strictness == "strict" and (min_tokens or max_tokens)
                and not _looks_like_failure(recap)
                and not _ai_module.is_thinking_starved_sentinel(recap)
            ):
                est_tokens = -(-len(recap) // _ai_module._chars_per_token_estimate(recap))
                below = min_tokens and est_tokens < min_tokens * 0.85
                above = max_tokens and est_tokens > max_tokens * 1.15
                if below or above:
                    if below:
                        violation_note = (
                            f"Your previous draft was ~{est_tokens} tokens; the requirement is at least "
                            f"~{min_tokens} tokens. Expand it with more specific detail from the facts."
                        )
                    else:
                        violation_note = (
                            f"Your previous draft was ~{est_tokens} tokens; the requirement is at most "
                            f"~{max_tokens} tokens. Trim it to fit."
                        )
                    _log.warning(
                        "session_log_recap job %s: strict length check failed (estimated ~%s tokens vs min=%s max=%s) — one strict retry",
                        job_id, est_tokens, min_tokens, max_tokens,
                    )
                    async with _ai_module.ollama_job_semaphore:
                        retry_recap = await _ai_module.summarize_session_from_facts(
                            [f.content for f in facts], model=model,
                            extra_instructions=_combined_recap_instructions(instructions, violation_note),
                            think=think, world_context=world_context,
                            min_tokens=min_tokens, max_tokens=max_tokens, strictness=strictness,
                        )
                    if not _looks_like_failure(retry_recap) and not _ai_module.is_thinking_starved_sentinel(retry_recap):
                        recap = retry_recap
            # Appended after (not woven into) the AI prose — see
            # _format_session_rewards' own docstring for why this has to be
            # computed from the structured fields rather than trusted to the
            # model. Same for both audiences: XP/loot awarded is shared
            # campaign state, not GM-secret, and this recap is often the
            # first place a player actually sees it (the Loot/XP panel
            # itself lives on the GM-only Session page).
            rewards = _format_session_rewards(xp_awarded, loot_items)
            if rewards:
                recap = recap + "\n\n" + rewards
            _set(status="done", result_json=_json.dumps({"recap": recap}),
                 finished_at=datetime.utcnow(), checkpoint_json="")
        elif purpose == "ai_assist":
            # One app.ai_assist.run_assist call with everything the op
            # needs read back off the row (see create_assist_job). The
            # result — the run_assist return shape verbatim — lands in
            # result_json (never `recap`) so the jobs UI can't mistake an
            # assist draft for session-recap prose. Held inside
            # ollama_job_semaphore like every other summarization purpose:
            # a whole-document rules rewrite can run for many minutes.
            _set(status="summarizing")
            op = assist_params.get("op", "")
            rag_query = "\n".join(x for x in (transcript, assist_params.get("meta", ""), assist_params.get("instruction", "")) if x)
            if use_rag and world_id and rag_query.strip():
                world_context = _build_rag_context(
                    world_id, rag_query,
                    rag_entity_limit if rag_entity_limit is not None else _DEFAULT_RAG_ENTITY_LIMIT,
                    rag_notes_limit if rag_notes_limit is not None else _DEFAULT_RAG_NOTES_LIMIT,
                )
            async with _ai_module.ollama_job_semaphore:
                result = await _ai_assist.run_assist(
                    op,
                    content=transcript,
                    meta=assist_params.get("meta", ""),
                    instruction=assist_params.get("instruction", ""),
                    lang=assist_params.get("lang", ""),
                    model=model, think=think, world_context=world_context,
                )
            # Both failure families must land as error rows, never cached
            # as a done result the polling route would happily serve:
            # generate_chat's sentinel strings (free-text ops) and
            # ValueError (structured ops — malformed JSON / AI down).
            if result.get("mode") == "text" and (
                _looks_like_failure(result.get("text", ""))
                or _ai_module.is_thinking_starved_sentinel(result.get("text", ""))
            ):
                _set(status="error", error=result.get("text", ""), finished_at=datetime.utcnow(), checkpoint_json="")
                return
            _set(status="done", result_json=_json.dumps(result),
                 finished_at=datetime.utcnow(), checkpoint_json="")
        elif purpose == "world_summary":
            # Assemble the campaign-state digest fresh at run time (never
            # trust a `transcript` seeded at creation — the world moved
            # since), then one free-text summarize over it. The result is
            # displayable prose, so it goes to `recap` like the session
            # recap purposes; the polling route caches on this done row
            # until a Regenerate click.
            _set(status="summarizing")
            state_text = _world_summary_state_text(world_id)
            if not state_text.strip():
                _set(status="error", error="This world has no content to summarize yet.",
                     finished_at=datetime.utcnow(), checkpoint_json="")
                return
            # Same "query on the input itself" convention as the ai_assist
            # branch above (no separate short question here either) — the
            # already-assembled state_text IS the best signal for which
            # lore excerpts are relevant to it.
            if use_rag and world_id:
                world_context = _build_rag_context(
                    world_id, state_text,
                    rag_entity_limit if rag_entity_limit is not None else _DEFAULT_RAG_ENTITY_LIMIT,
                    rag_notes_limit if rag_notes_limit is not None else _DEFAULT_RAG_NOTES_LIMIT,
                )
            async with _ai_module.ollama_job_semaphore:
                result = await _ai_assist.run_assist(
                    _ai_assist.OP_WORLD_SUMMARY, content=state_text, model=model, think=think,
                    world_context=world_context,
                )
            if _looks_like_failure(result.get("text", "")) or _ai_module.is_thinking_starved_sentinel(result.get("text", "")):
                _set(status="error", error=result.get("text", ""), finished_at=datetime.utcnow(), checkpoint_json="")
                return
            _set(status="done", recap=result.get("text", ""),
                 finished_at=datetime.utcnow(), checkpoint_json="")
        else:
            _set(status="done", finished_at=datetime.utcnow())
    except _job_shutdown.JobInterrupted:
        keep_audio = True
        _set(status="interrupted", error=_interrupted_note(progress["current"], progress["total"]),
             finished_at=datetime.utcnow())
    except asyncio.CancelledError:
        # cancel_job() calls Task.cancel() for a GM-initiated cancel; a
        # server shutdown calls it too (via job_shutdown.drain) when a job
        # doesn't reach a chunk boundary inside the stop grace window —
        # stopping() is what tells the two apart. A process restart with
        # no task to cancel at all (a crash/SIGKILL) goes through
        # sweep_interrupted_jobs/_forget_task instead, not this handler.
        #
        # Everything below is synchronous (no `await`) — cancellation only
        # delivers at an await point, so this handler cannot itself be
        # re-cancelled and does not need asyncio.shield. Keep it that way;
        # adding an `await` here would silently reintroduce that need.
        if _job_shutdown.stopping():
            keep_audio = True
            _set(status="interrupted", error=_interrupted_note(progress["current"], progress["total"]),
                 finished_at=datetime.utcnow())
        else:
            _set(status="cancelled", error="Cancelled by GM.", finished_at=datetime.utcnow())
        raise
    except Exception as exc:
        _log.exception("audio job %s failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=datetime.utcnow(), checkpoint_json="")
    finally:
        if audio_path and delete_after and not keep_audio:
            audio_path.unlink(missing_ok=True)


def cancel_job(job_id: int) -> bool:
    """Cancel an in-flight job's background task. Returns False if the job
    isn't currently running (already finished, or never started in this
    process — e.g. the id is stale/unknown), in which case the caller
    should treat it as a no-op rather than an error."""
    task = _running_tasks.get(job_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def delete_job(job_id: int) -> bool:
    """Permanently remove a finished job's row. Returns False (a no-op, not
    an error) if the job is still in progress — cancel it first — or the id
    is unknown, so the caller can 400/404 accordingly."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job or job.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(job)
        db.commit()
        return True
    finally:
        db.close()


def start_resummarize_job(
    job_id: int, model: str = "", extra_instructions: Optional[str] = None, think: Optional[bool] = None,
) -> AudioJob:
    """Kick off re-running just the summarization step against a job's
    already-saved transcript, optionally with a different model — for when
    the first summary failed (wrong/unpulled model, Ollama unreachable) or
    a GM just wants a second pass, without re-uploading or re-transcribing
    the audio. Returns immediately once the job is marked "summarizing";
    the actual work runs as a tracked background task, same engine as
    create_job/_run_job — a long transcript's map-reduce chunking (see
    summarize_transcript) can take several minutes, and running that
    inline inside the request/response cycle used to make it a routine way
    to trip a reverse proxy's own timeout (a raw 524 from Cloudflare, not
    even an error nd-world itself produced) long before Ollama finished.
    Raises ValueError with a caller-displayable message on any invalid
    state — checked synchronously up front so a bad request still fails
    fast rather than only surfacing after the caller starts polling.

    `extra_instructions`, same convention as `model` just above: blank/None
    keeps whatever the job was created with (or last resummarized with),
    a non-blank value replaces it for this run and is persisted for next
    time too.

    `think`, same "None keeps the prior value" convention: unlike `model`/
    `extra_instructions` there's no separate blank-string sentinel to worry
    about (it's already a real Optional[bool]), so None alone means "keep
    whatever this job was created/last resummarized with."

    Deliberately always a FRESH pass, not a resume of some prior
    interrupted attempt: checkpoint_json is cleared and resumed_count reset
    before starting, even if this job happened to be sitting on a
    checkpoint from an earlier run that got interrupted by a shutdown.
    Reusing that checkpoint here — possibly against a different model or
    different instructions — could silently splice output from two
    different summarization passes together. POST .../resume
    (start_resume_job) is the entry point that continues an interrupted
    run instead of restarting it; this one is the GM's explicit "redo it"
    action."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.purpose != "session_recap":
            raise ValueError("Only session-recap jobs can be re-summarized.")
        if not job.transcript:
            raise ValueError("This job has no transcript yet to summarize.")
        if job.status in IN_PROGRESS_STATUSES:
            raise ValueError("This job is already in progress.")
        chosen_model = model or job.model or ""
        chosen_instructions = (extra_instructions or "").strip() or (job.extra_instructions or "")
        job.status = "summarizing"
        job.error = ""
        job.chunk_current = None
        job.chunk_total = None
        job.model = chosen_model or None
        job.extra_instructions = chosen_instructions or None
        if think is not None:
            job.think = think
        job.checkpoint_json = ""
        job.resumed_count = 0
        job.run_started_at = datetime.utcnow()
        job.finished_at = None
        db.commit()
        db.refresh(job)
        job_snapshot = job
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_snapshot


def start_resume_job(job_id: int, reset_attempts: bool = False) -> AudioJob:
    """Continue a job that was paused by a server restart (status=
    "interrupted") from its saved checkpoint, instead of starting over.
    Mirrors start_resummarize_job's shape and contract: validates
    synchronously up front (raises ValueError with a caller-displayable
    message on any invalid state) and sets the row into its resuming
    phase BEFORE create_task, so no HTTP poll immediately after this call
    ever sees the job flicker back to "interrupted".

    Which phase it resumes into is decided from the checkpoint (or, if
    there isn't one — e.g. the job was interrupted right as transcription
    finished, just before the first summarize checkpoint could be
    written — from whether a transcript already exists): _run_job itself
    is what actually skips straight to summarizing when job.transcript is
    already set, so this only needs to validate the audio is still there
    when it ISN'T.

    purpose="session_log_recap" is a carve-out from all of the above: it
    has neither audio_path nor a transcript by design (see
    create_session_log_recap_job — its "transcript" is this session's Fact
    rows, re-read fresh from the DB by _run_job's session_log_recap branch
    on every run, resume included) and never writes a checkpoint, so it
    has exactly one phase — "summarizing" — with nothing to validate a
    resume point against. Without this carve-out the checks below always
    failed for this purpose (no audio, no transcript — indistinguishable
    from a genuinely lost audio upload), so an interrupted session-log
    recap could never resume; it just re-errored ("please re-upload",
    nonsensical for a job with no upload) every time, forcing a full
    regenerate on the caller's very next request.

    `reset_attempts`, if true, zeroes resumed_count — for a GM manually
    resuming a job that hit job_shutdown.MAX_AUTO_RESUMES and gave up
    automatically; a manual resume is a deliberate human decision, not
    another automatic retry, so it shouldn't inherit the cap that stopped
    further AUTOMATIC attempts. The HTTP route always passes True."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.status != "interrupted":
            raise ValueError("Only a job paused by a server restart can be resumed this way.")
        single_phase = job.purpose in ("session_log_recap", "ai_assist", "world_summary")
        if not single_phase and not job.audio_path and not job.transcript:
            raise ValueError("This job's audio is gone and it has no transcript to resume from — please re-upload.")
        checkpoint = _json.loads(job.checkpoint_json) if job.checkpoint_json else None
        phase = (checkpoint or {}).get("phase")
        resuming_summarize = (
            phase == "summarize" or (not phase and bool(job.transcript))
            or single_phase
        )
        if resuming_summarize:
            job.status = "summarizing"
        else:
            if not job.audio_path or not Path(job.audio_path).is_file():
                raise ValueError("This job's audio file is gone — please re-upload to redo the recording.")
            job.status = "transcribing"
        job.error = ""
        job.chunk_current = ((checkpoint or {}).get("chunks_done") or (checkpoint or {}).get("parts_done")) if checkpoint else None
        job.chunk_total = (checkpoint or {}).get("chunk_total") if checkpoint else None
        job.resumed_count = 0 if reset_attempts else job.resumed_count + 1
        job.run_started_at = datetime.utcnow()
        job.finished_at = None
        db.commit()
        db.refresh(job)
        job_snapshot = job
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_snapshot


def live_tasks() -> list[asyncio.Task]:
    """Every currently-running task this engine owns — app.main's shutdown
    handler passes this (alongside image_jobs'/chat_jobs' own) to
    job_shutdown.drain() so it knows what to wait for/cancel."""
    return list(_running_tasks.values())


def mark_stragglers_interrupted() -> None:
    """Called right after job_shutdown.drain() returns during shutdown:
    belt-and-braces sweep for any row still mid-flight whose task's own
    CancelledError handler (or _forget_task) didn't get to run in time —
    same shape as the boot-time sweep_interrupted_jobs below, just running
    at the other end of the process's life instead."""
    db = SessionLocal()
    try:
        stuck = db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
    finally:
        db.close()


def sweep_interrupted_jobs() -> None:
    """Called once at startup, before resume_interrupted_jobs: any job
    still mid-flight when the process last stopped UNCLEANLY (a crash,
    OOM, or a SIGKILL past the graceful-shutdown window — job_shutdown's
    own drain()/mark_stragglers_interrupted already handle a clean
    shutdown) has no background task to resume it in THIS process —
    asyncio.create_task's state doesn't survive a process restart — so
    it's marked "interrupted" here too, the same status a clean shutdown
    leaves a paused job in, so resume_interrupted_jobs (called right after
    this, same startup hook) treats both cases identically."""
    db = SessionLocal()
    try:
        stuck = db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
    finally:
        db.close()


def resume_interrupted_jobs() -> int:
    """Called once at startup, right after sweep_interrupted_jobs:
    auto-resumes every job left at status="interrupted" up to
    job_shutdown.MAX_AUTO_RESUMES times each — a job past the cap, or with
    no viable resume point (its audio is gone and it never got as far as
    saving a transcript), is marked "error" instead with a message
    explaining why, keeping whatever transcript/recap was already
    salvaged. Auto-resuming (rather than just leaving it for a GM to
    notice and click Resume) is the point of this whole feature: a routine
    `git pull && docker compose up -d --build` shouldn't require a human to
    intervene to get their transcription finished. Returns how many jobs
    were resumed, for the caller to log."""
    db = SessionLocal()
    try:
        job_ids = [j.id for j in db.query(AudioJob).filter(AudioJob.status == "interrupted").all()]
    finally:
        db.close()

    resumed = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            if not job or job.status != "interrupted":
                continue
            if job.resumed_count >= _job_shutdown.MAX_AUTO_RESUMES:
                job.status = "error"
                job.error = (
                    f"Interrupted by a server restart {job.resumed_count} times in a row — resume it by "
                    "hand from Background Jobs once the server is stable."
                )
                job.finished_at = datetime.utcnow()
                db.commit()
                continue
            # purpose="session_log_recap" has no viable "resume point" to
            # check in the first place — it has neither audio nor a
            # transcript by design (see start_resume_job's own docstring)
            # and re-reads this session's Facts fresh from the DB on every
            # run, so it's exempt from the check below the same way
            # start_resume_job exempts it.
            if job.purpose != "session_log_recap" and not job.transcript and (
                not job.audio_path or not Path(job.audio_path).is_file()
            ):
                job.status = "error"
                job.error = "Interrupted by a server restart — please re-upload."
                job.finished_at = datetime.utcnow()
                db.commit()
                continue
        finally:
            db.close()
        try:
            start_resume_job(job_id)
            resumed += 1
        except ValueError as exc:
            _log.warning("could not auto-resume audio job %s: %s", job_id, exc)
    return resumed


# Same path routers/sessions.py's own _session_audio_jobs_dir() computes —
# kept as its own small copy here per this codebase's per-module
# convention, rather than importing across the router boundary for one path.
_SESSION_AUDIO_JOBS_CUTOFF_SECONDS = 24 * 60 * 60


def sweep_orphaned_job_audio() -> None:
    """Called once at startup, after sweep_interrupted_jobs/
    resume_interrupted_jobs: a session-recap job's uploaded audio (up to
    MAX_AUDIO_UPLOAD_BYTES, 1 GB default) is only ever deleted by
    _run_job's own cleanup — a crash, deploy, or Watchtower update mid-job
    used to skip that entirely, leaving the file behind with no DB record
    pointing at it. Now that AudioJob.audio_path is persisted, this builds
    a keep-set from it first: any file still referenced by a job that's in
    progress or interrupted (awaiting a resume) is kept regardless of
    age — deleting it out from under a job still waiting to resume would
    silently turn "resume" into "start over" the next time a GM (or
    auto-resume, just above) tries. Everything else under
    uploads/session_audio/_jobs/ older than the cutoff is still swept, same
    as before. Deliberately does NOT touch uploads/ai_attachments/: those
    jobs run with delete_after=False because the file IS the attachment,
    not working storage to clean up."""
    jobs_dir = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"
    if not jobs_dir.is_dir():
        return

    db = SessionLocal()
    try:
        keep = {
            Path(j.audio_path).name
            for j in db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES + ("interrupted",))).all()
            if j.audio_path
        }
    finally:
        db.close()

    cutoff = time.time() - _SESSION_AUDIO_JOBS_CUTOFF_SECONDS
    for child in jobs_dir.iterdir():
        try:
            if child.name in keep:
                continue
            if child.is_file() and child.stat().st_mtime < cutoff:
                child.unlink()
        except OSError:
            pass
