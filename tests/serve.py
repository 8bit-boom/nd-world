"""Start a server for the test suite to run against over HTTP (docs/RUST_REWRITE_PLAN.md, phase 0):

    python -m tests.serve --port 8950 &
    ND_TEST_BASE_URL=http://127.0.0.1:8950 python -m pytest tests

It is the app exactly as production runs it, with the same environment the in-process tests use (the test database file, the secret key, the
upload limits) and the same fast PBKDF2 setting conftest applies - passwords made by the tests' seed fixture only verify with the same count. A
Rust gateway in front of the app is tested the same way: run it with `--upstream` pointed at this server and give pytest the gateway's address.
Never use it for anything but tests: its password hashing is deliberately weak and its secret key is public."""
import argparse
import os
from pathlib import Path

DATA = Path(__file__).parent / "_data"
DATA.mkdir(exist_ok=True)
os.environ.update(
    DB_PATH=str(DATA / "world.db"), SECRET_KEY="test-secret-key-not-for-production", MAX_UPLOAD_BYTES="1048576", MAX_NOTE_IMPORT_BYTES="1048576",
    ND_JOB_STOP_GRACE_SECONDS="0", OLLAMA_CONFIG_DIR=str(DATA / "ollama-config"),
)
os.environ.pop("GM_EMAIL", None)
os.environ.pop("GM_PASSWORD", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8950)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    from app import auth
    assert auth._PBKDF2_ITERATIONS == 600_000
    auth._PBKDF2_ITERATIONS = 1000                      # the same override tests/conftest.py applies in-process
    from app import main
    from tests._reset import reset_process_state

    @main._fastapi_app.post("/__test__/reset")
    def _reset():                                      # the in-process fixture's resets, for a server the tests cannot reach into
        reset_process_state()
        return {"ok": True}
    main._PUBLIC_PATHS.add("/__test__/reset")          # no login: it runs before the test has one
    import uvicorn
    uvicorn.run(main.app, host=a.host, port=a.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
