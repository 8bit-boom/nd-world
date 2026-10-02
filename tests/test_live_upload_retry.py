"""Live recording: which upload failures are waited out and which park the chunk.

A reverse proxy answers 502/504/52x while nd-world's container restarts (Watchtower swapping the image mid-session)
and `fetch` itself rejects when the connection drops. Those are "try again shortly" exactly like the 503 the server
sends for a down STT backend, and used to burn the 3-attempt ladder in ~9 seconds - shorter than a restart - parking
the chunk behind a manual Retry button. The decision is a small pure function in the page; this runs it in Node."""
import json
import re
import shutil
import subprocess

import pytest

from .conftest import GM_PASSWORD
from .test_live_recording_unsloth_fixes import _login, _session

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _policy_js(client, seed) -> str:
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    page = client.get(f"/sessions/{sid}").text
    m = re.search(r"// BEGIN live-upload-policy\n(.*?)// END live-upload-policy", page, re.S)
    assert m, "the retry policy block is in the page"
    return m.group(1)


def _run(js: str, expr: str):
    out = subprocess.run(["node", "-e", f"{js}\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
@pytest.mark.parametrize("status", [502, 503, 504, 520, 521, 522, 523, 524])
def test_gateway_and_backend_errors_are_waited_out(client, seed, status):
    js = _policy_js(client, seed)
    assert _run(js, f"liveUploadFailureKind({{httpStatus: {status}}})") == "wait"


@needs_node
def test_a_dropped_connection_is_waited_out(client, seed):
    js = _policy_js(client, seed)
    assert _run(js, "liveUploadFailureKind(new TypeError('Failed to fetch'))") == "wait"
    assert _run(js, "liveUploadFailureKind(new TypeError('NetworkError when attempting to fetch resource.'))") == "wait"


@needs_node
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 415, 422])
def test_setup_problems_park_the_chunk_at_once(client, seed, status):
    js = _policy_js(client, seed)
    assert _run(js, f"liveUploadFailureKind({{httpStatus: {status}}})") == "park"


@needs_node
@pytest.mark.parametrize("expr", ["{httpStatus: 500}", "{httpStatus: 501}", "{httpStatus: 507}", "new Error('boom')",
                                  "new SyntaxError('bad json')", "{}", "null", "undefined"])
def test_anything_else_uses_the_three_attempt_ladder(client, seed, expr):
    js = _policy_js(client, seed)
    assert _run(js, f"liveUploadFailureKind({expr})") == "retry"


@needs_node
@pytest.mark.parametrize("n, label", [(0, "0 B"), (512, "512 B"), (2048, "2 KB"), (350000, "342 KB"),
                                      (1048576, "1.0 MB"), (int(3.5 * 1048576), "3.5 MB"), (10 * 1048576, "10 MB"),
                                      (123 * 1048576, "123 MB")])
def test_the_raw_audio_size_is_readable_below_one_megabyte(client, seed, n, label):
    """A few one-minute Opus segments are a few hundred KB: the line used to say "(~0 MB)"."""
    js = _policy_js(client, seed)
    assert _run(js, f"liveFormatBytes({n})") == label


def test_the_ladder_and_the_status_line_use_them(client, seed):
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    page = client.get(f"/sessions/{sid}").text
    body = page.split("async function liveProcessQueue", 1)[1].split("function liveStartSegment", 1)[0]
    assert "liveUploadFailureKind(e)" in body and "kind === 'wait'" in body and "kind === 'park'" in body
    assert "liveFormatBytes(data.total_bytes)" in page and "Math.round(data.total_bytes / (1024 * 1024))" not in page
