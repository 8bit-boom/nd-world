# Background jobs in the gateway (phase 5)

Python runs durable background jobs (`app/audio_jobs.py`, `chat_jobs.py`, `image_jobs.py`, `video_jobs.py`, `media_rename_jobs.py`)
as asyncio tasks inside its own process. Moving a job kind to the gateway means two processes run jobs against one database and one
GPU, so two pieces of groundwork come first.

## One AI slot for both processes

`app/ai_queue.py` runs background AI tasks one at a time (one Studio, one GPU). Its line lives in Python's memory, so the gateway
could not see it. Both processes now also take a shared lease before a task starts:

    shared_state  key = ai_slot   value = {"owner": "python:<pid>:<ms>" | "gateway:<pid>:<ms>", "until": <epoch s>, "label": ...}

- A task first waits its turn in its own process's line (first come, first served), then for the lease: taken when it is free, has
  run out, or is already this process's (compare-and-swap on the row's counter, as `shared_state.update_json` does).
- The holder renews it every `AI_SLOT_LEASE_SECONDS / 3` (default 90 s / 3) and empties it when done; a process that dies holding it
  loses it when it runs out. Waiters look again every `AI_SLOT_POLL_SECONDS` (0.5 s).
- Between the two processes the order is not strictly first come, first served (whoever looks first after a release wins); within each
  process it is.
- If the lease cannot be written at all (no table, a locked database), the task runs anyway rather than freezing every AI feature.

Code: `app/ai_queue.py` (`_lease_try`, `_lease_acquire`, `_lease_release`, used by `wait_turn` / `leave`), `rust/nd-gateway/src/jobs.rs`
(`slot`).

## Which process runs a job

A job row the gateway runs is marked in shared_state:

    jobowner:<table>:<id>   value = {"owner": "gateway", "until": <epoch s>}

renewed every 20 s while the job runs (the lease lasts 60 s; the heartbeat runs inside the job's own task, so stopping the job stops it).

- **Ends** (done, error, cancelled): the key is removed.
- **Gateway shutdown** (SIGTERM / SIGINT): every running job is stopped and marked `interrupted` with Python's note, and its key kept.
- **Gateway start**: `jobs::adopt` takes each key it finds. A row that is in progress (the gateway died) or interrupted is resumed as
  `start_resume_job` does: `resumed_count` + 1, at most `MAX_AUTO_RESUMES` (3) times, then `error` with Python's text. A key whose
  row has ended or gone is removed.
- **Python's sweeps** (`mark_stragglers_interrupted`, `sweep_interrupted_jobs`, `resume_interrupted_jobs` in all five engines) skip
  rows the gateway holds (`app/job_owner.py`). They take a row over only when the gateway has not renewed its key for
  `JOB_ADOPT_AFTER_SECONDS` (600 s: the gateway was taken out, or is down for good). The row is then resumed in Python like its own.
- **A hand resume or re-summarize in Python** (`start_resume_job`, `start_resummarize_job`) removes the key first
  (`job_owner.release`): Python runs it from then on, and the gateway's next start leaves it alone.
- **Cancel** (`POST /api/audio-jobs/<id>/cancel`) is answered by the process running the job: the gateway for a job it runs (task
  stopped, row `cancelled` / "Cancelled by GM."), Python otherwise.

## Job kinds the gateway runs

| Job | Route | What stays Python's |
|---|---|---|
| AI Assist, free-text operations (`purpose="ai_assist"`) | `POST /api/ai/assist-job` writes the same `audio_jobs` row (`assist_params_json`, `transcript` = the body, the model or the `assist` default), runs it in the AI slot (status `summarizing`, then one `run_assist` call: the `recap` default when the row has no model, Thinking's widening or the 1,024-token cap, the refused-Thinking retry, `_clean_degenerate_recap`), and ends it `done` with `result_json` = `{op, mode: "text", text, model}`, or `error` (the failure text, or `ValueError: <run_assist's message>`). Polling (`GET /api/ai/assist-job/<id>`) reads the row, so it is the same from either process. | The JSON operations (`suggest`, `table_entries`), an unknown operation (400), the legacy Ollama backend |

| Background chat (`chat_jobs`) | `POST /api/ai/chat/jobs` (GM): the same row - the last user prompt (500 characters), the messages with document attachments folded in, the system text, the model, the options through `_clamp_options` plus the `num_ctx` `_ctx_override_if_needed` fits to the text (stored, not sent: Unsloth fixes the context at load) - then, in the AI slot, `generating` and one `generate_chat` (no Thinking), ending `done` (`result`) or `error`. Cancel: `POST /api/ai/chat/jobs/<id>/cancel`. A restart restarts it from the stored request (chat_jobs' note and cap texts). | Players (403), picture or audio attachments, options given as text |
| Session Log recap (`purpose="session_log_recap"`) | `POST /api/session-log/<id>/recap`: the route's own answers first - the newest fresh `done` row for the same configuration (model, Thinking, lore limits, instruction, length targets, strictness; newer than the session's last fact edit and the world's `recap_content_touch`), the running job, a failure within the last 60 s - then the player cooldown (shared with the Chronicler's, in the gateway) and a new row run here: the facts the audience may see, `summarize_session_from_facts` (the strictness note, the GM instructions, the length addendum, the options, the repeat-penalty retry for a degenerate answer), the one strict-length retry, the Rewards footer, `result_json` = `{recap}` (`{recap: "", empty: true}` without facts). | Lore with player characters among the session's picks (`_format_pc_line` / app/pc_digest.py), a body field Python's parsers would 400 or convert in their own way |
| World summary (`purpose="world_summary"`) | `POST /api/ai/world-summary` (GM or assistant): the same row (the `assist` default model, the caller's audience, lore only for a GM), then the campaign digest built at run time (`_world_summary_state_text`: recent entities, quests, facts, sessions, filtered for the players' audience), lore on it, one `world_summary` run_assist call, `recap` = the text, or `error` ("This world has no content to summarize yet.", a failure). `GET` / `DELETE` stay Python's (they read and delete rows). | A body Pydantic would reject or coerce |
| Condense (`purpose="condense"`) | `POST /api/sessions/ai/condense-job` (the Sessions section's edit level): the same row (Python's body parsers, the too-long check), then lore with the session's pinned entities, `condense_recap` (strictness note, instructions, lore, length addendum, the output options; `condense_call_options` only sizes num_ctx, which Unsloth does not take), the Thinking ladder for a starved answer (think, expanded think, no think - `think` / `think_fallback` / `expanded_thinking` written as Python writes them), the one strict-length retry, and `think_rejected` / `think_token_fallback` from this process's own record of models that refused Thinking. | Lore with player characters among the session's picks, a body Python's parsers would 400 |
| Facts parse (`purpose="facts_parse"`) | `POST /api/facts/parse-job` (the Facts section's edit level): the same row, then `parse_facts_from_recap` - the chunk plan (`_facts_parse_chunk_plan`), the split at paragraphs / lines / sentence ends (`_split_transcript_into_chunks`), one schema-constrained call per chunk (`response_format` = the `_RECAP_FACTS_SCHEMA` JSON schema, lore in the user message, the refused-Thinking retry, `chunk_current` / `chunk_total` as it goes), the facts merged with repeats dropped (`_normalized_fact_key`) and tags joined; `result_json` = the facts, or `error` (the first failure when every chunk failed: "Ollama error …", "AI unavailable: …", "Could not parse facts …"). | Lore with player characters among the session's picks, a body Python's parsers would 400 |
| Live-transcript recap (`purpose="session_recap"`, transcript on the row) | `POST /api/sessions/<id>/ai/summarize-live-transcript-job` (the Sessions section's edit level, for a session of the active world): the same row ("Live Transcript"), then lore, `summarize_transcript` under the Thinking ladder - one call for a transcript that fits the window (`_transcript_chunk_char_budget`: Settings' `llm_context_tokens`, else `LLM_CONTEXT_TOKENS`, capped by `MAX_AUTO_NUM_CTX`), else a summary per part with `chunk_current` / `chunk_total` and a `checkpoint_json` after each part, resumed from it after a restart; a failing or empty part ends it - then the pending facts drafted from the transcript (`_auto_extract_pending_facts`: "[]" on any failure) and `think_rejected` as for Condense. | A session of another world than the active one (the section check reads that world), lore with player characters picked, a recording to transcribe (the audio pipeline is Python's) |
| Session recording recap (`purpose="session_recap"` with a recording) | `POST /api/sessions/ai/audio-jobs/from-clip` (an Audio Library clip: kept afterwards) and `/complete` (the parts `/chunk` saved, reassembled into `uploads/session_audio/_jobs/`: deleted afterwards): the same row, then the transcribe phase (`transcribing`, `run_started_at`) - `rust/nd-gateway/src/stt.rs`: a recording over max(15 min, 1.5 x `STT_CHUNK_SECONDS`) is split by ffmpeg into `STT_CHUNK_SECONDS` pieces (progress, a checkpoint with the transcript so far after each, resumed after a restart when the piece count, length and file size still match); each piece goes to Studio's `/v1/audio/transcriptions` (the `stt_model` of ai_models.json, else "small"), re-encoded to Opus (MP3 once Studio refuses Opus) and split again when over 23 MiB or 15 minutes; runs of 4+ identical lines collapsed; a failing piece ends the job with Python's message and the transcript so far - then the recap as for the live transcript. Cancel removes a recording the row says to delete; an interruption keeps it. | The one-request upload (`POST /api/sessions/ai/audio-jobs`, a multipart body streamed to disk), chat attachments' transcription (`purpose="attachment"`), lore with player characters picked |
| Image generation on Unsloth Studio (`image_jobs`) | `POST /api/ai/imagegen/jobs` (GM, with a Studio key and `UNSLOTH_IMAGE_MODEL` set - `_get_type() == "unsloth"`): the same row (`params_json` = every `imagegen_generate` argument in `_imagegen_params`' order, a blank model as the `image` default), then in the AI slot `generating` and Studio's native `/api/inference/images/generate` (model, prompt, size, steps or 9, batch 1-8, negative prompt, guidance, seed, an init image from uploads as a data URL with its strength); a 409 / "No diffusion model is loaded" loads the chosen model once (`ensure_image_model`: the `.gguf` from Studio's variant list - default, then downloaded, then the best quant - `/api/inference/images/load`, then `/status` every 2 s) and tries again; an older Studio without the native route gets `/v1/images/generations` (b64). Each picture is saved as `uploads/ai-images/<uuid>.png` with its thumbnail; `done` with `result_urls_json`, or `error` with Python's texts. Cancel: `POST /api/ai/imagegen/jobs/<id>/cancel`; a restart restarts it (image_jobs' note and cap texts). | SwarmUI and ComfyUI (not needed: they stay with Python), players' image jobs, the Hugging Face file listing `resolve_gguf_filename` falls back to when Studio names no variants (the model is then loaded without a file name) |
| AI video on Unsloth Studio (`video_jobs`) | `POST /api/video-jobs/start` (GM or assistant, the Video section's edit level): the same row and answer (`{id, job}`), then in the AI slot `generating`, `POST /v1/videos` (prompt, and model / seconds / size when given), the generation id kept on the row (`_extract_video_id`'s shapes) so a restart re-attaches to it instead of paying for it twice, `/v1/videos` polled every 10 s until the entry is done or failed (status words, or a content url; `ND_VIDEO_JOB_MAX_SECONDS`, 30 failed polls in a row), `fetching`, the file saved as `uploads/video/ai-video-<8 hex>.<mp4/webm/mov>` and a hidden Video Library clip (the given name, else the prompt's first 60 characters). Cancel: `POST /api/video-jobs/<id>/cancel` (Studio's own generation keeps going, as in Python); a restart re-attaches (video_jobs' note and cap texts). | A decimal `seconds` (its JSON spelling is Python's), a body field Python's `str()` would write its own way |

Lore (`_build_rag_context`, `rust/nd-gateway/src/retrieval.rs` `build_rag_context`): pinned entities, the FTS5 matches for the input (first 4,000 characters) topped up to the entity limit, the newest notes, the last 20 facts. A GM's lore on the AI Assist job, the Session Log recap, the world summary and Condense is built here. Player characters pinned on a session need `pc_digest`'s line and stay Python's: a request with any is forwarded; one that gains a player character pick between the request and the run is built here without its line (a known difference).

The record of models that refused Thinking (`_model_thinking_failures`, `_prompt_token_thinking_models`) lives in each process, as it always has; the gateway keeps its own, updated by every request it sends (streams and jobs).

Tests: `tests/test_cross_process_jobs.py` (the lease and the sweeps, in Python), `tests/test_gateway_ai.py` (the AI Assist job against
Python's with a fake Studio; the lease and the owner key while a gateway job runs; cancel). The restart was checked by hand: a gateway
stopped with SIGTERM mid-job leaves the row `interrupted` with its key; the next start resumes it (`resumed_count` 1) to `done`.

## The blocking AI routes

Answered by the gateway, inline or as a background task (below), with the same functions its jobs use:
`POST /api/sessions/ai/expand-notes` (`expand_recap_notes`), `/api/sessions/ai/condense-recap` (`condense_recap`, no ladder),
`/api/sessions/ai/summarize-transcript` (`summarize_transcript`, `{transcript, recap, recap_failed}`), `/api/sessions/<id>/ai/summarize-live-transcript`
(`{transcript, recap}`), `/api/sessions/<id>/ai/summarize-from-facts` (`summarize_session_from_facts`), `/api/facts/parse`
(`parse_facts_from_recap`, a 502 with its failure) `/api/facts/folk-tale` (run_assist `folk_tale` over the facts, a GM's lore), `/api/ai/chat` (one `generate_chat`, `{result}`; a GM's - the
route itself checks nothing, the rest stays Python's) `/api/ai/chat/compact` (`condense_chat_history`, the AI-chat access rule, its ceiling
and 502), the four `/api/ai/generate/{entity,npc,location,quest}` (`generate(prompt)`: the default system prompt; a GM's) and
`/api/ai/generate/entity-smart` (the prompt with related lore found the Chronicler's way, filtered for an assistant). Python's
own refusals (no notes, no facts) are answered the same way; its section checks, a session of another world, a recap over the
condense ceiling and every body its parsers would reject stay Python's.

## Background AI requests (`X-ND-Background: 1`)

Python runs an AI route in the background when the request carries `X-ND-Background: 1` (app/ai_background.py): a 202 with a task id,
the route run to completion, its answer polled from `GET /api/ai/tasks/<id>`. The gateway does the same for the routes it answers itself
(`rust/nd-gateway/src/tasks.rs`; `POST /api/ai/assist` and the blocking routes above): the route is planned first (a request the gateway would hand to Python
keeps the header and is started by Python's middleware), then a 202 (`{task_id, status: "running", label}`, `X-ND-Task`, the label from
`X-ND-Task-Label` or the route's own) and the answer worked out in the AI slot (`queued`, then `running`), kept with its status, content
type, location and body for an hour (at most 300; 8 running per user, 48 in all). Its task ids start with `gw`: the gateway answers
`GET` / `DELETE /api/ai/tasks/<gw…>` (the owner or a GM; a running task is cancelled, a finished one forgotten), Python every other id,
and `GET /api/ai/tasks` is Python's list with the gateway's tasks merged in (newest first, 50 at most).

The NPC stream keeps going when its reader leaves, so the turn is saved (Python's `DETACH_PATHS`); the gateway's other streams stop
with their reader, as Python's do.
