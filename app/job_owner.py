"""Which process runs a durable background job (docs/RUST_JOBS.md).

While the Rust gateway runs some job kinds itself, a job row in audio_jobs / chat_jobs / image_jobs / video_jobs / media_rename_runs
may be in progress in the gateway, not here. The gateway says so in shared_state:

    jobowner:<table>:<id>    value = {"owner": "gateway", "until": <epoch seconds>}

renewed while it works on the job and left in place when it stops, so its own next start finds and resumes the rows it owned. This
process's sweeps (the shutdown and boot sweeps that mark in-progress rows "interrupted", and the boot resume) leave such rows alone,
and take one over only when the gateway has not renewed it for ADOPT_AFTER_SECONDS (the gateway was taken out, or is down for good):
the job is then resumed here like any of this process's own.
"""
import json
import os
import time

from sqlalchemy import text

from .database import engine

PREFIX = "jobowner:"
ADOPT_AFTER_SECONDS = float(os.environ.get("JOB_ADOPT_AFTER_SECONDS", "600"))


def gateway_ids(table: str) -> set:
    """Ids of `table`'s jobs the gateway holds (renewed within ADOPT_AFTER_SECONDS)."""
    now = time.time()
    out = set()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT key, value FROM shared_state WHERE key LIKE :p"), {"p": f"{PREFIX}{table}:%"}).fetchall()
    except Exception:
        return out
    for key, value in rows:
        try:
            v = json.loads(value or "{}")
            if v.get("owner") == "gateway" and float(v.get("until") or 0) > now - ADOPT_AFTER_SECONDS:
                out.add(int(key.rsplit(":", 1)[1]))
        except (ValueError, TypeError):
            continue
    return out


def not_gateways(table: str, rows: list) -> list:
    """`rows` (objects with .id) without the ones the gateway holds."""
    held = gateway_ids(table) if rows else set()
    return [r for r in rows if r.id not in held]


def release(table: str, job_id: int) -> None:
    """This process takes job `job_id` over (a hand resume, a re-summarize): the gateway's mark on it goes, so neither the gateway's
    next start nor this process's sweeps treat the row as the gateway's any more."""
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM shared_state WHERE key = :k"), {"k": f"{PREFIX}{table}:{job_id}"})
    except Exception:
        pass
