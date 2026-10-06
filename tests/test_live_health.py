"""Live recording: telling the GM the truth about what has and has not been captured.

A report: "it recorded only about 3/4" - the END of a session missing. Recording a fake microphone for 200 s captured
99% of the audio, so the loss is not in the basic capture. What it was: pressing Stop showed "Stopped - transcript
saved." at once, while the last chunk had not even been cut and up to the whole backlog (a speech model slower than the
table talks) was still waiting in the browser's memory - leave then and the tail is gone. And when the mic dropped for
good, the only sign was a line of small grey status text.

The decisions are pure functions in static/js/live-health.js (status text, backlog, silence, reconnect pacing, pauses)
and static/js/live-store.js (an IndexedDB copy of every chunk until the server has it), exercised here under Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HEALTH = str(ROOT / "static/js/live-health.js")
STORE = str(ROOT / "static/js/live-store.js")


def _node(body: str, module: str = HEALTH, global_name: str = "ndLiveHealth"):
    script = f"global.window = global; require({json.dumps(module)}); const H = global.{global_name};\n{body}"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ── clock ────────────────────────────────────────────────────────────────────────────────────────────

@needs_node
@pytest.mark.parametrize("sec,expected", [(0, "0:00"), (5, "0:05"), (65, "1:05"), (3599, "59:59"), (3600, "1:00:00"),
                                          (3725, "1:02:05"), (36000, "10:00:00"), (-3, "0:00"), (None, "0:00"), ("x", "0:00")])
def test_clock(sec, expected):
    assert _node(f"console.log(JSON.stringify(H.clock({json.dumps(sec)})))") == expected


# ── the status text ───────────────────────────────────────────────────────────────────────────────────

def _summary(**state):
    base = {"recording": False, "finalizing": False, "recordedSec": 0, "doneSec": 0, "queuedSec": 0, "chunksWaiting": 0,
            "uploading": False, "failedChunks": 0, "failedSec": 0, "speed": None}
    base.update(state)
    return _node(f"console.log(JSON.stringify(H.summarize({json.dumps(base)})))")


@needs_node
def test_recording_with_nothing_behind_is_calm():
    s = _summary(recording=True, recordedSec=754, doneSec=720)
    assert "12:34" in s["text"] and "behind" not in s["text"] and s["level"] == "ok"


@needs_node
def test_recording_says_up_to_where_the_transcript_is_saved():
    """Not just 'up to date': the chunk being recorded now is not in the transcript yet, and the GM should see that."""
    s = _summary(recording=True, recordedSec=754, doneSec=720)
    assert "saved up to 12:00" in s["text"]


@needs_node
def test_recording_before_the_first_chunk_was_sent_says_so():
    s = _summary(recording=True, recordedSec=35, doneSec=0)
    assert "0:35" in s["text"] and "nothing sent to the transcriber yet" in s["text"]


@needs_node
def test_a_short_wait_is_not_announced_as_about_under_a_minute():
    s = _summary(recordedSec=60, doneSec=0, queuedSec=30, chunksWaiting=1, uploading=True, speed=1.0)
    assert "about under" not in s["text"] and "under a minute left" in s["text"]


@needs_node
def test_recording_with_a_backlog_says_how_far_behind():
    s = _summary(recording=True, recordedSec=3600, doneSec=2700, queuedSec=900, chunksWaiting=15, uploading=True)
    assert "1:00:00" in s["text"] and "15:00 behind" in s["text"] and "15 chunks" in s["text"]
    assert s["level"] == "warn" and s["behindSec"] == 900


@needs_node
def test_a_small_lag_is_just_busy_not_a_warning():
    s = _summary(recording=True, recordedSec=300, doneSec=240, queuedSec=60, chunksWaiting=1, uploading=True)
    assert s["level"] == "busy" and "1 chunk" in s["text"] and "1 chunks" not in s["text"]


@needs_node
def test_stop_with_the_last_chunk_not_cut_yet_is_not_saved_yet():
    """The bug: this moment used to read "Stopped - transcript saved."."""
    s = _summary(finalizing=True, recordedSec=3600, doneSec=3600)
    assert "saved" not in s["text"].lower() or "not" in s["text"].lower()
    assert "finishing" in s["text"].lower() and s["level"] == "warn"


@needs_node
def test_stop_with_a_backlog_says_keep_the_page_open_and_how_long():
    s = _summary(recordedSec=5400, doneSec=4000, queuedSec=1400, chunksWaiting=24, uploading=True, speed=0.75)
    assert "still transcribing" in s["text"].lower() and "24 chunks" in s["text"] and "23:20" in s["text"]
    assert "keep this page open" in s["text"].lower() and s["level"] == "warn"
    assert s["etaSec"] == pytest.approx(1400 / 0.75)
    assert "min left" in s["text"]


@needs_node
def test_stop_with_a_backlog_but_no_speed_yet_gives_no_estimate():
    s = _summary(recordedSec=600, doneSec=0, queuedSec=600, chunksWaiting=2, uploading=True, speed=None)
    assert s["etaSec"] is None and "left" not in s["text"]


@needs_node
def test_stopped_and_fully_transcribed_is_the_only_time_it_says_saved():
    s = _summary(recordedSec=3725, doneSec=3725)
    assert "saved" in s["text"].lower() and "1:02:05" in s["text"] and s["level"] == "ok"


# ── offline: recording carries on, nothing is lost, and the status says where the audio is ────────────────

@needs_node
def test_recording_offline_says_where_the_chunks_are_and_that_they_send_themselves():
    s = _summary(recording=True, recordedSec=300, doneSec=240, queuedSec=60, chunksWaiting=1, uploading=True, offline=True)
    assert "offline" in s["text"].lower() and "1 chunk" in s["text"] and "1:00" in s["text"]
    assert "in this browser" in s["text"] and "by themselves" in s["text"] and s["level"] == "warn"
    assert "saved up to" not in s["text"]      # the transcript is NOT current: it must not say so


@needs_node
def test_recording_offline_with_nothing_queued_yet_still_says_so():
    s = _summary(recording=True, recordedSec=30, doneSec=0, offline=True)
    assert "offline" in s["text"].lower() and "recording continues" in s["text"] and s["level"] == "warn"


@needs_node
def test_stopped_offline_is_not_saved_and_says_how_to_get_it_saved():
    s = _summary(recordedSec=900, doneSec=300, queuedSec=600, chunksWaiting=10, uploading=True, offline=True)
    text = s["text"].lower()
    assert "not on the server yet" in text and "10 chunks" in text and "10:00" in s["text"]
    assert "reopen" in text and "keep this page open" in text and s["level"] == "warn"
    assert "transcribed and saved" not in text


@needs_node
def test_offline_but_everything_was_already_saved_just_says_saved():
    s = _summary(recordedSec=900, doneSec=900, offline=True)
    assert "saved" in s["text"].lower() and "offline" not in s["text"].lower() and s["level"] == "ok"


@needs_node
def test_failed_chunks_are_never_called_saved():
    s = _summary(recordedSec=900, doneSec=600, failedChunks=2, failedSec=300)
    assert s["level"] == "bad" and "2 chunks" in s["text"] and "5:00" in s["text"] and "not" in s["text"].lower()
    assert "saved" not in s["text"].lower().replace("not saved", "")


@needs_node
def test_nothing_recorded_yet():
    s = _summary()
    assert s["level"] == "ok" and s["text"] == ""


# ── how fast is the speech model ──────────────────────────────────────────────────────────────────────────

@needs_node
def test_speed_is_audio_seconds_per_second_and_smoothed():
    assert _node("console.log(JSON.stringify(H.updateSpeed(null, 60, 30)))") == pytest.approx(2.0)
    v = _node("console.log(JSON.stringify(H.updateSpeed(2.0, 60, 120)))")           # a slow chunk pulls it down, not to 0.5
    assert 0.5 < v < 2.0
    assert _node("console.log(JSON.stringify(H.updateSpeed(1.5, 0, 10)))") == 1.5      # nonsense sample ignored
    assert _node("console.log(JSON.stringify(H.updateSpeed(1.5, 60, 0)))") == 1.5


@needs_node
def test_eta():
    assert _node("console.log(JSON.stringify(H.etaSec(600, 2)))") == 300
    assert _node("console.log(JSON.stringify(H.etaSec(600, null)))") is None
    assert _node("console.log(JSON.stringify(H.etaSec(0, 2)))") == 0
    assert _node("console.log(JSON.stringify(H.etaSec(600, 0)))") is None


@needs_node
@pytest.mark.parametrize("sec,expected", [(20, "under a minute"), (59, "under a minute"), (60, "1 min"), (1130, "19 min"),
                                          (3600, "1 h"), (5400, "1 h 30 min"), (8100, "2 h 15 min")])
def test_human_duration(sec, expected):
    assert _node(f"console.log(JSON.stringify(H.humanDuration({sec})))") == expected


# ── silence ───────────────────────────────────────────────────────────────────────────────────────────

@needs_node
def test_silence_monitor_flags_a_long_quiet_stretch_once():
    out = _node("""
      const m = H.createSilenceMonitor({thresholdRms: 0.004, silentAfterSec: 120});
      const log = [];
      for (let t = 0; t <= 200000; t += 1000) { const r = m.push(0.0005, t); if (r.justBecameSilent) log.push(['silent', t]); }
      const r2 = m.push(0.05, 201000);
      console.log(JSON.stringify({log, after: r2}));
    """)
    assert out["log"] == [["silent", 120000]]
    assert out["after"]["silent"] is False and out["after"]["justRecovered"] is True


@needs_node
def test_a_little_sound_resets_the_silence_clock():
    out = _node("""
      const m = H.createSilenceMonitor({thresholdRms: 0.004, silentAfterSec: 120});
      let flagged = false;
      for (let t = 0; t <= 300000; t += 1000) { const r = m.push(t % 100000 === 0 ? 0.02 : 0.0004, t); flagged = flagged || r.silent; }
      console.log(JSON.stringify(flagged));
    """)
    assert out is False


@needs_node
def test_silence_monitor_reports_how_long_it_has_been_quiet():
    out = _node("""
      const m = H.createSilenceMonitor({thresholdRms: 0.004, silentAfterSec: 60});
      m.push(0.0001, 0); const r = m.push(0.0001, 90000);
      console.log(JSON.stringify(r));
    """)
    assert out["silent"] is True and out["silentForSec"] == 90


# ── reconnecting a lost mic ────────────────────────────────────────────────────────────────────────────

@needs_node
def test_reconnect_is_quick_at_first_then_patient():
    delays = _node("console.log(JSON.stringify([1,2,3,4,5,50].map(a => H.reconnectDelayMs(a))))")
    assert delays[:3] == [1000, 2000, 3000]
    assert delays[3:] == [15000, 15000, 15000]


@needs_node
def test_total_patience_is_about_half_an_hour():
    total = _node("""
      let t = 0; for (let a = 1; a <= H.RECONNECT_ATTEMPTS; a++) t += H.reconnectDelayMs(a);
      console.log(JSON.stringify(t));
    """)
    assert 25 * 60 * 1000 <= total <= 35 * 60 * 1000


# ── the computer going to sleep ─────────────────────────────────────────────────────────────────────────

@needs_node
def test_a_long_gap_between_heartbeats_is_a_pause():
    assert _node("console.log(JSON.stringify(H.detectPause(1000, 2000)))") == {"paused": False, "seconds": 0}
    p = _node("console.log(JSON.stringify(H.detectPause(1000, 511000)))")
    assert p["paused"] is True and p["seconds"] == 510
    assert _node("console.log(JSON.stringify(H.detectPause(1000, 16000)))")["paused"] is False      # 15 s of lag is not sleep
    assert _node("console.log(JSON.stringify(H.detectPause(1000, 22000)))")["paused"] is True


@needs_node
def test_pause_message_names_the_missing_time():
    msg = _node("console.log(JSON.stringify(H.pauseMessage(510)))")
    assert "8:30" in msg and "not recorded" in msg.lower()


# ── chunk keys ───────────────────────────────────────────────────────────────────────────────────────────

@needs_node
def test_store_keys_are_stable_and_sortable():
    out = _node("""
      const a = H.storeKey(7, 'ab12', 2), b = H.storeKey(7, 'ab12', 10);
      console.log(JSON.stringify({a, b, sorted: [b, a].sort(), parsed: H.parseStoreKey(a)}));
    """)
    assert out["a"] != out["b"] and out["sorted"] == [out["a"], out["b"]], "zero-padded so string order is index order"
    assert out["parsed"] == {"sessionId": 7, "recordingId": "ab12", "index": 2}


# ── the local chunk store ─────────────────────────────────────────────────────────────────────────────────

FAKE_IDB = r"""
// A tiny in-memory IndexedDB: only what live-store.js uses (open / createObjectStore / transaction / put / delete / getAll).
function fakeIndexedDB() {
  const dbs = {};
  function req(run) {
    const r = {};
    setTimeout(() => { try { r.result = run(); r.onsuccess && r.onsuccess({target: r}); } catch (e) { r.error = e; r.onerror && r.onerror({target: r}); } }, 0);
    return r;
  }
  return {
    open(name) {
      const r = {};
      setTimeout(() => {
        const fresh = !dbs[name];
        dbs[name] = dbs[name] || {stores: {}};
        const db = {
          createObjectStore(n, opts) { dbs[name].stores[n] = {key: opts.keyPath, rows: new Map()}; return {}; },
          objectStoreNames: {contains: n => !!dbs[name].stores[n]},
          transaction(n) {
            const st = dbs[name].stores[n];
            const tx = {oncomplete: null, onerror: null, objectStore: () => ({
              put: v => { const q = req(() => { st.rows.set(v[st.key], JSON.parse(JSON.stringify(v, (k, x) => x))); }); setTimeout(() => tx.oncomplete && tx.oncomplete(), 1); return q; },
              delete: k => { const q = req(() => { st.rows.delete(k); }); setTimeout(() => tx.oncomplete && tx.oncomplete(), 1); return q; },
              getAll: () => req(() => [...st.rows.values()]),
            })};
            return tx;
          },
          close() {},
        };
        r.result = db;
        if (fresh) r.onupgradeneeded && r.onupgradeneeded({target: r});
        r.onsuccess && r.onsuccess({target: r});
      }, 0);
      return r;
    },
  };
}
"""


def _store(body: str):
    script = (FAKE_IDB + f"global.window = global; require({json.dumps(STORE)}); const S = global.ndLiveStore;\n"
              "(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_store_keeps_chunks_until_they_are_removed():
    out = _store("""
      const s = S.create(fakeIndexedDB());
      const rec = (i, session) => ({key: 'k' + session + '-' + i, sessionId: session, recordingId: 'r1', index: i, seconds: 60, name: 'recording.webm', mimeType: 'audio/webm', savedAt: 1000 + i, blob: 'BLOB' + i});
      const ok = [await s.put(rec(1, 7)), await s.put(rec(0, 7)), await s.put(rec(0, 8))];
      const mine = await s.list(7);
      await s.remove('k7-0');
      const after = await s.list(7);
      console.log(JSON.stringify({ok, mine: mine.map(r => r.index), after: after.map(r => r.index), other: (await s.list(8)).length, count: await s.count(7)}));
    """)
    assert out == {"ok": [True, True, True], "mine": [0, 1], "after": [1], "other": 1, "count": 1}


@needs_node
def test_store_lists_in_recording_then_segment_order():
    out = _store("""
      const s = S.create(fakeIndexedDB());
      const rec = (rid, i, t) => ({key: rid + ':' + i, sessionId: 3, recordingId: rid, index: i, seconds: 60, name: 'x', mimeType: 'm', savedAt: t, blob: 'b'});
      await s.put(rec('bbb', 0, 5000)); await s.put(rec('aaa', 2, 1000)); await s.put(rec('aaa', 1, 1100)); await s.put(rec('bbb', 1, 5100));
      console.log(JSON.stringify((await s.list(3)).map(r => r.recordingId + r.index)));
    """)
    assert out == ["aaa1", "aaa2", "bbb0", "bbb1"], "earlier recording first (by save time), then by segment"


@needs_node
def test_a_missing_or_broken_indexeddb_never_throws():
    out = _store("""
      const none = S.create(undefined);
      const broken = S.create({open() { throw new Error('private mode'); }});
      console.log(JSON.stringify({
        a: [await none.put({key: 'x', sessionId: 1}), await none.list(1), await none.remove('x'), await none.count(1)],
        b: [await broken.put({key: 'x', sessionId: 1}), await broken.list(1), await broken.remove('x'), await broken.count(1)],
      }));
    """)
    assert out == {"a": [False, [], False, 0], "b": [False, [], False, 0]}


@needs_node
def test_a_failing_open_is_not_retried_forever():
    out = _store("""
      let opens = 0;
      const flaky = {open() { opens++; const r = {}; setTimeout(() => { r.error = new Error('blocked'); r.onerror && r.onerror({target: r}); }, 0); return r; }};
      const s = S.create(flaky);
      await s.put({key: 'a', sessionId: 1}); await s.put({key: 'b', sessionId: 1}); await s.list(1);
      console.log(JSON.stringify(opens));
    """)
    assert out <= 2
