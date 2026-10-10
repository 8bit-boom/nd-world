"""Alerts on the character page (static/js/nd-notify-core.js, run under Node): which snapshot-to-snapshot changes deserve one."""
import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


def _decide(prev, nxt):
    script = ("const c = require('./static/js/nd-notify-core.js');"
              f"console.log(JSON.stringify(c.decide({json.dumps(prev)}, {json.dumps(nxt)})));")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _snap(turn=None, round_=1, nxt=False, handouts=0, session=None, polls=0):
    return {"me": {"name": "Mira"}, "combat": None if turn is None else {"turn": turn, "round": round_, "next_is_me": nxt},
            "handouts_new": handouts, "next_session": session, "polls_waiting": polls}


def test_the_first_snapshot_never_alerts():
    assert _decide(None, _snap(turn="me")) == []


def test_your_turn_and_up_next_alert_once_per_round():
    assert [a["tag"] for a in _decide(_snap(turn="enemy"), _snap(turn="me"))] == ["turn"]
    assert _decide(_snap(turn="me"), _snap(turn="me")) == []                               # still your turn: no repeat
    assert [a["tag"] for a in _decide(_snap(turn="me", round_=1), _snap(turn="me", round_=2))] == ["turn"]
    assert [a["tag"] for a in _decide(_snap(turn="enemy"), _snap(turn="enemy", nxt=True))] == ["next"]
    assert _decide(_snap(turn="enemy", nxt=True), _snap(turn="enemy", nxt=True)) == []
    assert _decide(_snap(turn="me"), _snap(turn="enemy")) == []                            # your turn ending is not news


def test_handouts_and_game_night_changes():
    assert [a["tag"] for a in _decide(_snap(handouts=0), _snap(handouts=2))] == ["handout"]
    assert _decide(_snap(handouts=2), _snap(handouts=1)) == []
    a = {"title": "Game", "starts_at": "2026-10-20T18:00:00Z"}
    b = {"title": "Game", "starts_at": "2026-10-21T18:00:00Z"}
    assert [x["tag"] for x in _decide(_snap(session=a), _snap(session=b))] == ["session"]
    assert _decide(_snap(session=a), _snap(session=a)) == []
    assert [x["tag"] for x in _decide(_snap(session=a), _snap(session=b, handouts=1))] == ["handout", "session"]
    assert [x["tag"] for x in _decide(_snap(polls=0), _snap(polls=1))] == ["poll"]


def test_the_scripts_parse():
    for f in ("static/js/nd-notify-core.js", "static/js/nd-notify.js"):
        assert subprocess.run([NODE, "--check", f], capture_output=True, text=True).returncode == 0
