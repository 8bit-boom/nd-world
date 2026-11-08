/* Export & Backup: the "Better training examples" card (docs/TEACHER_EXAMPLES.md). Talks to /export/teacher/{status,start,stop,clear}. */
(function () {
  var card = document.getElementById('teacher-card');
  if (!card) return;
  var $ = function (id) { return document.getElementById(id); };
  var timer = null, kindsBuilt = false;

  function text(el, t) { el.textContent = t; }

  function options(limit) {
    var kinds = [].slice.call(document.querySelectorAll('.teacher-kind')).filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
    return { kinds: kinds, per_source: +$('teacher-per').value, general_percent: +$('teacher-gen').value, judge: $('teacher-judge').checked,
             drop_partly: $('teacher-partly').checked, limit: limit || null };
  }

  function buildKinds(kinds, defaults) {
    if (kindsBuilt) return;
    kindsBuilt = true;
    var box = $('teacher-kinds');
    kinds.forEach(function (k) {
      var l = document.createElement('label');
      l.style.display = 'block';
      var c = document.createElement('input');
      c.type = 'checkbox'; c.className = 'teacher-kind'; c.value = k.id; c.checked = defaults.kinds.indexOf(k.id) >= 0;
      l.appendChild(c); l.appendChild(document.createTextNode(' ' + k.name));
      box.appendChild(l);
    });
  }

  function render(d) {
    $('teacher-unavailable').style.display = 'none';
    $('teacher-body').style.display = '';
    buildKinds(d.kinds, d.defaults);
    var st = d.state || {}, busy = st.status === 'running';
    text($('teacher-model'), d.ai_ready ? 'Model: ' + (st.model || d.model) : 'No AI model is set up yet. Choose one in Settings first.');
    $('teacher-start').disabled = busy || !d.ai_ready;
    $('teacher-trial').disabled = busy || !d.ai_ready;
    $('teacher-clear').disabled = busy;
    $('teacher-stop').style.display = busy ? '' : 'none';
    var r = st.report || {}, line = '';
    if (busy) line = (r.jobs ? ('Piece ' + Math.min((r.jobs_done || 0) + 1, r.jobs) + ' of ' + r.jobs + ' — ') : '') + (st.text || '');
    else if (st.status === 'interrupted') line = 'Interrupted (the server restarted). Press "Make the examples" to continue; finished answers are remembered.';
    else if (st.status === 'error') line = st.text || 'It stopped because of an error.';
    else if (st.status === 'done' || st.status === 'stopped') {
      line = (st.status === 'stopped' ? 'Stopped. ' : 'Finished. ') + 'Made ' + (r.added || 0) + ' new examples. Checked: ' + (r.supported || 0) + ' supported, ' +
        (r.partly || 0) + ' partly (kept, marked), ' + (r.unsupported || 0) + ' unsupported (dropped), ' + (r.unchecked || 0) + ' not checked.' +
        (r.failed_calls ? ' ' + r.failed_calls + ' requests failed' + (r.first_error ? ': ' + r.first_error : '') + '.' : '');
    }
    text($('teacher-progress'), line);
    var c = d.counts || {};
    text($('teacher-counts'), c.total ? (c.kept + ' examples are in the next training bundle (' + c.dropped + ' dropped by the check).') : 'No examples yet.');
    schedule(busy);
  }

  function schedule(busy) {
    if (timer) { clearTimeout(timer); timer = null; }
    if (busy) timer = setTimeout(refresh, 3000);
  }

  function refresh() {
    fetch('/export/teacher/status', { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('unavailable');
      return r.json();
    }).then(render).catch(function () {
      $('teacher-body').style.display = 'none';
      $('teacher-unavailable').style.display = '';
    });
  }

  function post(action, body) {
    return fetch('/export/teacher/' + action, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.detail || ('Error ' + r.status)); return j; }); });
  }

  function act(action, body) {
    post(action, body).then(refresh).catch(function (e) { text($('teacher-progress'), e.message); });
  }

  $('teacher-start').onclick = function () { act('start', options(null)); };
  $('teacher-trial').onclick = function () { act('start', options(5)); };
  $('teacher-stop').onclick = function () { act('stop'); };
  $('teacher-clear').onclick = function () {
    if (confirm('Remove every example made for this world? (They are remembered, so making them again costs no model time.)')) act('clear');
  };
  refresh();
})();
