"""Copy / paste for media (static/js/nd-clipboard.js, its pure helpers run under Node): which file fields take what was pasted, the
name a screenshot gets, and which copied text is an address of something already in this world."""
import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


def _call(fn, *args):
    script = ("const c = require('./static/js/nd-clipboard.js');"
              "const a = JSON.parse(process.argv[1]);"
              "if (process.argv[2] === 'pastedName') a[2] = new Date(a[2]);"
              "console.log(JSON.stringify(c[process.argv[2]](...a)));")
    out = subprocess.run([NODE, "-e", script, json.dumps(list(args)), fn], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_accept_matching():
    assert _call("acceptMatches", "image/*", "shot.png", "image/png") is True
    assert _call("acceptMatches", "image/*", "song.mp3", "audio/mpeg") is False
    assert _call("acceptMatches", "audio/*,.txt", "notes.TXT", "text/plain") is True
    assert _call("acceptMatches", ".md,.pdf", "a.pdf", "application/pdf") is True
    assert _call("acceptMatches", ".md,.pdf", "a.png", "image/png") is False
    assert _call("acceptMatches", "", "anything.bin", "") is True                      # no accept = takes anything
    assert _call("acceptMatches", "video/mp4", "c.mp4", "video/mp4") is True
    assert _call("acceptMatches", "video/mp4", "c.webm", "video/webm") is False


def test_a_screenshot_gets_a_real_name_and_a_real_name_is_kept():
    when = "2026-10-11T14:05:30Z"
    assert _call("pastedName", "image.png", "image/png", when, 0, 1) == "Pasted picture 2026-10-11 14-05.png"
    assert _call("pastedName", "image.jpeg", "image/jpeg", when, 1, 3) == "Pasted picture 2026-10-11 14-05 2.jpg"
    assert _call("pastedName", "", "audio/webm;codecs=opus", when, 0, 1) == "Pasted audio 2026-10-11 14-05.webm"
    assert _call("pastedName", "map-of-the-market.png", "image/png", when, 0, 1) == "map-of-the-market.png"
    assert _call("pastedName", "Screenshot (3).png", "image/png", when, 0, 1) == "Screenshot (3).png"   # a real file name, not "screenshot.png"


def test_copied_addresses_of_things_in_this_world():
    o = "https://world.example.com"
    assert _call("addressOf", "/uploads/gallery/a.png", o) == {"path": "/uploads/gallery/a.png", "kind": "image"}
    assert _call("addressOf", o + "/uploads/gallery/a%20b.webp?v=3", o) == {"path": "/uploads/gallery/a b.webp", "kind": "image"}
    assert _call("addressOf", o + "/uploads/audio/song.mp3", o) == {"path": "/uploads/audio/song.mp3", "kind": "audio"}
    assert _call("addressOf", "/uploads/video/cut.mp4", o) == {"path": "/uploads/video/cut.mp4", "kind": "video"}
    assert _call("addressOf", "/uploads/audio/clip.webm", o)["kind"] == "audio"        # .webm is told apart by its folder
    assert _call("addressOf", "/uploads/video/clip.webm", o)["kind"] == "video"
    assert _call("addressOf", "/uploads/video/song.mp3", o) is None                    # wrong folder for the type
    for no in ("https://evil.example.org/uploads/a.png", "hello world", "", "/api/secret.png", "/uploads/gallery/a.exe",
               "/uploads/gallery/a.png and more", "data:image/png;base64,AAAA"):
        assert _call("addressOf", no, o) is None, no
