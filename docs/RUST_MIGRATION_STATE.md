# Process-local state: what a second server has to know about

Part of `docs/RUST_REWRITE_PLAN.md`, phase 0. While a Rust (Axum) server and this Python app serve one site together, anything that lives only
in the memory of the Python process is invisible to the other one. This is the inventory (taken from every module-level dict / cache / task
registry in `app/`), with what was done or what has to be done before the route that uses it moves.

## Shared since phase 0 (SQLite table `shared_state`, `app/shared_state.py`)

| State | Key | Notes |
|---|---|---|
| World change counter (`live.touch` / `live.version`) | `live:<world>` | bumps are written by one background thread (a handler may still hold SQLite's write lock); a reader sees its own process's bumps at once, another process's within ~0.3 s |
| Named signals (`live.signal` / `signal_all`: "spot", "dice", "combat") | `sig:<world>:<name>`, `sigall:<name>` | same |
| Second screen's stage (`routers/display.py`) | `stage:<world>` (JSON) | written with compare-and-swap, no lost updates between processes |
| Session cookie | the cookie itself | signed JSON (Starlette / itsdangerous); stateless, so any process with the secret can read it |
| Logins, memberships, 2FA secrets, trusted devices, saved layouts, jobs' rows | their tables | already database state |

## Stays in Python's memory, and why that is fine for now

| State | Where | Why it can stay / what moves it |
|---|---|---|
| AI job registries: `_MAP_AI_JOBS`, `_AI_BUILD_JOBS`, `_AUTO_TAG_JOBS`, `_PC_AI_JOBS`, `_ANALYSIS_JOBS`, `_COCKPIT_FIND_JOBS`, `_QUEST_SUGGEST_JOBS`, `_TPL_AI_JOBS`, `ai_background._TASKS`, `live_jobs._JOBS`, `unsloth_extras._CHECKS`, `sessions._LIVE_STT_INFLIGHT`, `ai_queue` | `ai.py`, `routers/*` | hold asyncio tasks of running AI work; those routes stay in Python until phase 5 and are proxied until then |
| Per-user AI cooldown `deps._llm_cooldowns`, chronicler answer cache `routers/chronicler._ask_cache`, `ai._model_capabilities_cache`, `ai._status_cache`, `routers/ai._tags_cache` | | AI routes only (phase 5) |
| **Failed-login limiters** `routers/auth._failed_logins`, `_failed_2fa` | `auth.py` | **security-relevant**: with two servers answering `/login`, an attacker would get twice the attempts. Until the limiter is a `shared_state` table (done in phase 3, together with the login routes), `/login`, `/login/2fa` and the other auth POSTs are proxied to Python and never answered by Rust |
| Spotlight answer cache `main._spotlight_cache` (2 s TTL, keyed by user + world) | `main.py` | a change made through the other process is seen within the 2 s TTL (the clearing signal now travels through `shared_state` too); when Rust serves `/api/spotlight` it reads the database directly |
| App-settings flag snapshot `database._app_settings_flags_cache` (5 s TTL) | `database.py` | a Settings change made through the other process shows within 5 s; Settings POSTs stay in Python until phase 3 |
| Render gate and its tuner `http_perf._GATES`, `render_gate` | | per process by design: each server limits and tunes its own page building |
| Pure caches keyed by content or file time: rendered-markdown LRU, `rendering._parse_stats_cache`, `http_perf._MAP_JSON_CACHE`, `main._autolink_map_cache`, `main._MASKED_BG_CACHE`, `system_stats._cache` | | invalidated by what they are keyed on (content, mtime, TTL), never by a write in this process |
| Thumbnail / tiling de-duplication `static_thumbs._queued`, `map_tiles._building`, `vault_sync._SYNC_LOCKS` | | only avoids doing the same work twice inside one process; a second process making the same thumbnail writes the same file |
| Background loops: `db_upkeep`, backups (`ND_BACKUP_*`), diagnostics watchdog, fan control, job resume at start-up | `lifespan` | must run in exactly ONE process: they stay in Python (the Rust server starts none of them) |
| Request timing log `perf_log` | | per process: `/api/perf/summary` shows Python's requests only; the gateway keeps its own counters |

## Rules for moving a route to Rust

1. If the route reads or writes anything in the first table, it already works across processes.
2. If it touches a "stays in Python" item, either the item moves to `shared_state` / a table first, or the route stays in Python.
3. A route that changes data other processes cache by TTL (settings, spotlight) accepts that staleness, or clears the other side's cache through a
   `live.signal` both sides listen to.
4. Nothing in the second table may be started in the Rust process.
