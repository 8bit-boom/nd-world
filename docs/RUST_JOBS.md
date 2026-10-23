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

renewed every 20 s while the job runs (the lease lasts 60 s).

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
| AI Assist, free-text operations (`purpose="ai_assist"`) | `POST /api/ai/assist-job` writes the same `audio_jobs` row (`assist_params_json`, `transcript` = the body, the model or the `assist` default), runs it in the AI slot (status `summarizing`, then one `run_assist` call: the `recap` default when the row has no model, Thinking's widening or the 1,024-token cap, the refused-Thinking retry, `_clean_degenerate_recap`), and ends it `done` with `result_json` = `{op, mode: "text", text, model}`, or `error` (the failure text, or `ValueError: <run_assist's message>`). Polling (`GET /api/ai/assist-job/<id>`) reads the row, so it is the same from either process. | A GM's lore retrieval (`use_rag`), the JSON operations (`suggest`, `table_entries`), an unknown operation (400), the legacy Ollama backend |

Tests: `tests/test_cross_process_jobs.py` (the lease and the sweeps, in Python), `tests/test_gateway_ai.py` (the AI Assist job against
Python's with a fake Studio; the lease and the owner key while a gateway job runs; cancel). The restart was checked by hand: a gateway
stopped with SIGTERM mid-job leaves the row `interrupted` with its key; the next start resumes it (`resumed_count` 1) to `done`.
