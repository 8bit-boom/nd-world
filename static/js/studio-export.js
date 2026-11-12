/* Export & Backup: the "Training data for Unsloth Studio" card. GET /export/unsloth-studio.zip (?info=1 for the counts). Python-free image only. */
(function () {
  var card = document.getElementById('studio-card');
  if (!card) return;
  var $ = function (id) { return document.getElementById(id); };

  function query(extra) {
    return 'world_book=' + ($('studio-book').checked ? 1 : 0) + '&teacher=' + ($('studio-teacher').checked ? 1 : 0) + '&test=' + $('studio-test').value + (extra || '');
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
      var d = x.d;
      $('studio-counts').textContent = d.rows + ' rows for training (' + d.chats + ' questions and answers' + (d.world_book ? ', ' + d.world_book + ' world-book pieces' : '') +
        (d.teacher ? ', ' + d.teacher + ' written by the AI model' : '') + ')' + (d.eval ? ', ' + d.eval + ' kept for evaluation' : '') + '.';
    }).catch(function () {
      $('studio-body').style.display = 'none';
      $('studio-unavailable').style.display = '';
    });
  }

  ['studio-book', 'studio-teacher', 'studio-test'].forEach(function (id) { $(id).addEventListener('change', refresh); });
  refresh();
})();
