/* Export & Backup: the "Training data for Unsloth Studio" card. GET /export/unsloth-studio.zip (?info=1 for the counts). Python-free image only. */
(function () {
  var card = document.getElementById('studio-card');
  if (!card) return;
  var $ = function (id) { return document.getElementById(id); };

  function checkedWorlds() {
    return [].slice.call(document.querySelectorAll('.studio-world')).filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
  }
  function worldsParam() {
    var w = checkedWorlds();
    return w.length ? '&worlds=' + w.map(encodeURIComponent).join(',') : '';
  }

  function query(extra) {
    // the world is named in the address (as everywhere in nd-world, `w` wins over the cookie), so two worlds never share an address
    return 'w=' + encodeURIComponent(card.getAttribute('data-world') || '') +  worldsParam() + '&world_book=' + ($('studio-book').checked ? 1 : 0) + '&teacher=' + ($('studio-teacher').checked ? 1 : 0) + '&test=' + $('studio-test').value + '&vram=' + $('studio-hw').value + '&max_tokens=' + $('studio-maxtok').value + '&eval_limit=' + $('studio-evallimit').value + (extra || '');
  }

  function refresh() {
    $('studio-download').href = '/export/unsloth-studio.zip?' + query();
    fetch('/export/unsloth-studio.zip?' + query('&info=1'), { credentials: 'same-origin' }).then(function (r) {
      var json = (r.headers.get('content-type') || '').indexOf('json') >= 0;
      if (!json) { throw new Error('not here'); }
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (x) {
      $('studio-unavailable').style.display = 'none';
      $('studio-body').style.display = '';
      if (!x.ok) { $('studio-counts').textContent = (x.d && x.d.detail) || 'Nothing to export yet.'; return; }
      var d = x.d, hwi = d.hardware || {};
      $('studio-hwinfo').textContent = (hwi.label || '') + (hwi.ram_gb ? ' · ' + hwi.ram_gb + ' GB RAM' : '') + ' → ' + (d.model || '') + ', ' + (d.method || '') + ', context ' + d.context + ', about ' + d.est_gb + ' GB' +
        (hwi.source === 'assumed' ? ' (no card was detected here: pick yours above)' : '') + ((hwi.notes || []).length ? ' · ' + hwi.notes.join(' ') : '');
      $('studio-counts').textContent = (d.worlds > 1 ? d.worlds + ' worlds: ' : (d.world ? d.world + ': ' : '')) + d.rows + ' rows for training (' + d.chats + ' questions and answers' + (d.world_book ? ', ' + d.world_book + ' world-book pieces' : '') +
        (d.teacher ? ', ' + d.teacher + ' written by the AI model' : '') + ')' + (d.eval ? ', ' + d.eval + ' kept for evaluation' : '') + '.' +
        (d.skipped_long ? ' ' + d.skipped_long + ' longer examples left out.' : '') + ' Settings: ' + d.model + ' ' + d.method + ', context ' + d.context + ', ' + d.epochs + ' epoch' + (d.epochs > 1 ? 's' : '') + ' (about ' + d.steps + ' steps); the zip has the settings as a YAML file to load.';
    }).catch(function () {
      $('studio-body').style.display = 'none';
      $('studio-unavailable').style.display = '';
    });
  }

  ['studio-book', 'studio-teacher', 'studio-test', 'studio-maxtok', 'studio-evallimit', 'studio-hw'].forEach(function (id) { $(id).addEventListener('change', refresh); });
  [].slice.call(document.querySelectorAll('.studio-world')).forEach(function (c) { c.addEventListener('change', refresh); });
  $('studio-allworlds').addEventListener('click', function (e) { e.preventDefault(); [].slice.call(document.querySelectorAll('.studio-world')).forEach(function (c) { c.checked = true; }); refresh(); });
  $('studio-oneworld').addEventListener('click', function (e) {
    e.preventDefault();
    var me = card.getAttribute('data-world');
    [].slice.call(document.querySelectorAll('.studio-world')).forEach(function (c) { c.checked = c.value === me; });
    refresh();
  });
  refresh();
})();
