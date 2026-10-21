#!/usr/bin/env python3
"""Writes rust/nd-gateway/tests/fixtures/session.json: session cookies made by the app's real signer (Starlette / itsdangerous), with a fixed
clock, so the Rust cookie reader is tested against what Python produces.   python scripts/gen_session_fixture.py [--check]"""
import json
import sys
from base64 import b64encode
from pathlib import Path

from itsdangerous import TimestampSigner

OUT = Path(__file__).resolve().parent.parent / "rust" / "nd-gateway" / "tests" / "fixtures" / "session.json"
SECRET = "fixture-secret-not-used-anywhere"
SIGNED_AT = 1_700_000_000


def make() -> dict:
    cases = []
    for session in ({"user_id": 7, "world_id": 3}, {}, {"user_id": 1, "name": "Zoë \"quoted\" ✓", "list": [1, 2, {"a": None}]}):
        signer = TimestampSigner(SECRET)
        signer.get_timestamp = lambda: SIGNED_AT
        data = signer.sign(b64encode(json.dumps(session).encode("utf-8"))).decode()
        cases.append({"cookie": data, "session": session})
    return {"secret": SECRET, "signed_at": SIGNED_AT, "cases": cases}


if __name__ == "__main__":
    text = json.dumps(make(), indent=1, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text() == text else "fixture is stale: run scripts/gen_session_fixture.py")
    OUT.write_text(text)
    print("wrote", OUT)
