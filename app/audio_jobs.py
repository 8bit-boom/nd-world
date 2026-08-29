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
from . import job_shutdown as _job_shutdown
from . import retrieval as _retrieval
from .database import SessionLocal
from .models import AudioJob, Entity, GameSession, PlayerCharacter, World

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
    values the GM originally set.

    `use_rag`/`rag_entity_limit`/`rag_notes_limit` — same RAG opt-in
    create_job's own docstring describes, see _build_rag_context."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="condense", filename="Condense", status="pending",
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            model=model or None, think=think, fit_context=fit_context,
            extra_instructions=extra_instructions.strip() or None,
            min_tokens=min_tokens, max_tokens=max_tokens,
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


def _glossary_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        gm_glossary = (w.whisper_glossary or "").strip() if w else ""
    finally:
        db.close()
    return merge_glossary(gm_glossary, entity_glossary_terms(world_id))[0]


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
    pinned (see _format_pc_line)."""
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
        return "\n".join(part for part in (pc_context, entity_context) if part)
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
        use_rag = bool(job.use_rag)
        rag_entity_limit = job.rag_entity_limit
        rag_notes_limit = job.rag_notes_limit
        existing_transcript = job.transcript or ""
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
        skip_transcribe = bool(existing_transcript) and not (checkpoint and checkpoint.get("phase") == "transcribe")
        if not skip_transcribe:
            _set(status="transcribing", run_started_at=datetime.utcnow(), finished_at=None)
            glossary = _glossary_for_world(world_id) if world_id else ""
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
        if use_rag and world_id and purpose in ("condense", "session_recap"):
            # Query the transcript/text-to-condense itself — there's no
            # separate short "user question" here the way AI Chat's RAG has
            # one, so the input being summarized IS the best signal for what
            # entities/notes are relevant to it (see _build_rag_context's
            # own docstring for the query-length cap this relies on).
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
            if _looks_like_failure(recap):
                _set(status="error", error=recap, finished_at=datetime.utcnow())
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
                _set(status="done", recap=recap, chunk_current=None, chunk_total=None,
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
        if not job.audio_path and not job.transcript:
            raise ValueError("This job's audio is gone and it has no transcript to resume from — please re-upload.")
        checkpoint = _json.loads(job.checkpoint_json) if job.checkpoint_json else None
        phase = (checkpoint or {}).get("phase")
        resuming_summarize = phase == "summarize" or (not phase and bool(job.transcript))
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
            if not job.transcript and (not job.audio_path or not Path(job.audio_path).is_file()):
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
