# Phase 6: switching Python off - the work list

Python can leave the image only when the gateway answers every request itself. To know how far that is, the gateway can log who
answers each request (`ND_FORWARD_LOG=<file>`: one `GW|PY METHOD path` line per request; `scripts/gateway_suite.sh` turns it on per
shard as `tests/_data/shard<N>/forwarded.log`). The whole suite run through the gateway (Python 3.13, every route switch on) gives the
census below. Ids are folded to `{}`.

| Route patterns seen in the suite | Count | Requests still answered by Python |
|---|---|---|
| Answered by the gateway every time | 133 | 0 |
| Gateway for some cases, Python for the rest (edge cases, refusals, coercions) | 376 | 5,388 |
| Python every time | 555 | 5,877 |

The lists, busiest first, are in `docs/census/python_only.txt` and `docs/census/mixed.txt` (`forwarded / gateway` counts). Regenerate
them after each batch; the phase is done when both are empty and the steps below are done.

## Order of work

1. **Authentication**: `POST /login`, `GET /logout`, two-factor (`/login/2fa`, `/account/2fa/*`), `POST /api/login`, password reset.
   Every other route depends on a session the gateway can issue itself.
2. **The busiest Python-only pages and APIs** from `python_only.txt` (session pages, parties, settings, the schedule, the cockpit
   workspace, import), in batches with parity tests as before.
3. **The mixed routes**: close each route's remaining Python cases (refusals with Python's messages, pydantic's coercions), busiest first.
4. **Startup work Python does**: migrations and the database healing in `app/database.py` (`_migrate`, `_heal_table`, seeding), the
   background loops (the jobs' sweeps, the watchdog, backups), and the 2FA / session secrets.
5. **The image**: Python removed from the Dockerfile; image size and memory measured against today's.

Python-only libraries with no exact Rust twin (reportlab for PDF export, the PDF / HTML sheet readers behind the AI drafters) may stay
in a small sidecar, as the plan allows, if a port is not good enough.
