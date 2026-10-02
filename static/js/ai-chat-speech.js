// AI page → 🎙 Speech tab: a speech-to-text sanity check against Unsloth Studio
// (status, transcribe a file, record from the mic, or run it as a background
// job) plus the world's recap instructions.

async function spLoadStatus() {
  const el = document.getElementById('sp-status');
  if (!el) return;
  el.textContent = '⏳ Checking…';
  try {
    const d = await fetch('/api/ai/debug').then(r => r.json());
    const s = d.stt || {};
    const lines = [
      s.url ? `Studio: ${s.url}` : 'Studio: not configured — add its API key in Settings → System',
      s.url ? `Reachable: ${s.ok ? '✓ yes' : '✗ no' + (s.reason ? ' — ' + s.reason : '')}` : null,
      'Model: set in Settings → System → "Unsloth Studio server"; use ▷ Test STT there to check it works.',
    ].filter(l => l !== null);
    el.textContent = lines.join('\n');
  } catch (e) {
    el.textContent = '✗ Could not check status: ' + e.message;
  }
}

async function riLoadInstructions() {
  const ta = document.getElementById('ri-textarea');
  if (!ta) return;
  try {
    const d = await fetch('/api/ai/recap-instructions').then(r => r.json());
    ta.value = d.instructions || '';
  } catch (e) { /* leave blank — save will still work */ }
}

async function riSaveInstructions() {
  const btn = document.getElementById('ri-save-btn');
  const status = document.getElementById('ri-status');
  const ta = document.getElementById('ri-textarea');
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/recap-instructions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions: ta.value }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = '✓ Saved';
    setTimeout(() => { if (status.textContent === '✓ Saved') status.textContent = ''; }, 2000);
  } catch (e) {
    status.textContent = '✗ Could not save: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function spTranscribeFile(file) {
  const btn = document.getElementById('sp-transcribe-btn');
  const micBtn = document.getElementById('sp-mic-btn');
  const resultBox = document.getElementById('sp-result');
  const resultText = document.getElementById('sp-result-text');
  const bar = ndProgressBar(document.getElementById('sp-progress'));

  btn.disabled = true;
  micBtn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = '⏳ Transcribing…';
  resultBox.style.display = 'block';
  resultText.textContent = '';
  bar.setPercent(0, 'Uploading… 0%');
  try {
    const data = await ndChunkedUpload(file, {
      directUrl: '/api/ai/attachments/upload',
      chunkUrl: '/api/ai/attachments/upload/chunk',
      completeUrl: '/api/ai/attachments/upload/complete',
      onProgress: (e) => ndProgressFromUpload(bar, e, 'Transcribing through Unsloth Studio…'),
    });
    if (data.kind !== 'audio') throw new Error(`Not recognized as audio (got kind=${data.kind})`);
    resultText.textContent = data.text
      ? data.text
      : '(empty transcript — Studio may be unreachable, still loading the speech model, or the clip has no detected speech; check Status above)';
  } catch (e) {
    resultText.textContent = '✗ ' + e.message;
  } finally {
    bar.clear();
    btn.disabled = false;
    micBtn.disabled = false;
    btn.textContent = origLabel;
  }
}

function spTranscribe() {
  const file = document.getElementById('sp-file').files[0];
  if (!file) { alert('Pick an audio file first'); return; }
  spTranscribeFile(file);
}

let _spMicRecorder = null;
function spToggleMic() {
  const micBtn = document.getElementById('sp-mic-btn');
  if (_spMicRecorder && _spMicRecorder.isRecording()) { _spMicRecorder.stop(); return; }
  _spMicRecorder = ndMicRecorder(
    (blob, mimeType) => {
      micBtn.textContent = '🎤 Record';
      spTranscribeFile(new File([blob], ndMicFilename(mimeType), { type: mimeType }));
    },
    (err) => {
      micBtn.textContent = '🎤 Record';
      document.getElementById('sp-result').style.display = 'block';
      document.getElementById('sp-result-text').textContent = '✗ Microphone error: ' + err.message;
    },
  );
  _spMicRecorder.start().then(started => { if (started) micBtn.textContent = '⏹ Stop'; });
}

const spJobs = ndAudioJobs(document.getElementById('sp-jobs-panel'), {
  createUrl: '/api/ai/attachments/audio-jobs',
  chunkUrl: '/api/ai/attachments/audio-jobs/chunk',
  completeUrl: '/api/ai/attachments/audio-jobs/complete',
  listUrl: '/api/ai/attachments/audio-jobs',
  onUse: (job) => {
    document.getElementById('sp-result').style.display = 'block';
    document.getElementById('sp-result-text').textContent = job.transcript
      || '(empty transcript — Studio may be unreachable, still loading the speech model, or the clip has no detected speech; check Status above)';
  },
});

async function spStartBackgroundJob() {
  const file = document.getElementById('sp-file').files[0];
  if (!file) { alert('Pick an audio file first'); return; }
  const btn = document.getElementById('sp-bgjob-btn');
  btn.disabled = true;
  const bar = ndProgressBar(document.getElementById('sp-progress'));
  bar.setPercent(0, 'Uploading… 0%');
  try {
    await spJobs.startJob(file, {}, (e) => ndProgressFromUpload(bar, e, 'Job started — processing in background…'));
  } catch (e) {
    alert('Failed to start background job: ' + e.message);
  } finally {
    bar.clear();
    btn.disabled = false;
  }
}

