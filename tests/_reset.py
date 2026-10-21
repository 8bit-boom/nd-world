"""Everything a test run must forget between two tests, in the process that serves the app (tests/conftest.py calls it in-process; tests/serve.py
exposes it as POST /__test__/reset for a server started separately).

The database is emptied for every test, so ids restart from 1; any process-local cache or counter keyed by an id (a user, a world, a session)
would carry one test's data into the next, and each of the resets below exists for a failure of that kind that was seen."""
import asyncio
import shutil


def reset_process_state() -> None:
    from app import ai as ai_module
    from app import shared_state
    import app.database as database_module
    from app.deps import _llm_cooldowns
    from app.main import UPLOADS_DIR
    from app.rendering import clear_parse_stats_cache
    from app.routers.chronicler import _ask_cache

    shared_state.reset_local()        # live counters / the second screen's stage live in a table; a new table needs a new process-local copy
    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
    shutil.rmtree(database_module.RESTORE_STAGING_DIR, ignore_errors=True)
    # user-id-keyed cooldown (a reused id would give a false 429), the small AI-answer caches, the app-settings snapshot (a fresh id=1 row every
    # test, but the old snapshot would still be "fresh" by the TTL clock) and the parsed-stats cache (keyed by entity id + updated_at)
    _llm_cooldowns.clear()
    _ask_cache.clear()
    database_module._app_settings_flags_cache = None
    clear_parse_stats_cache()
    # the spotlight answer cache is keyed by (user id, world slug); dict.clear directly - the class's clear() also announces a signal
    from app.main import _spotlight_cache
    dict.clear(_spotlight_cache)
    # the failed-login limiters (keyed by address + email / user id) must not lock a test out because earlier tests failed on purpose
    from app.routers import auth as auth_router
    auth_router._failed_logins.clear()
    auth_router._failed_2fa.clear()
    # asyncio semaphores bind to the first event loop that contends them: every test gets fresh, unbound ones
    ai_module.stt_job_semaphore = asyncio.Semaphore(ai_module.STT_JOB_CONCURRENCY)
    ai_module.ollama_job_semaphore = asyncio.Semaphore(ai_module.OLLAMA_JOB_CONCURRENCY)
    ai_module.imagegen_job_semaphore = asyncio.Semaphore(ai_module.IMAGEGEN_JOB_CONCURRENCY)
