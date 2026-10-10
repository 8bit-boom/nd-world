/* The pure half of offline quick edits (static/js/nd-offline.js): what an HP / Shock change does to the numbers on screen, and
 * how a queue of such changes is kept in order. No DOM, no network, so it runs under Node in the tests.
 *
 * Only DELTAS are queued (+1 / -3 ...): they can be replayed in any order and never overwrite what someone else changed in the
 * meantime. Absolute sets are not queued - they fail like any request offline. */
(function (root) {
  'use strict';
  var KINDS = { 'hp-async': 'hp', 'shock': 'shock' };

  /* '/api/characters/12/hp-async' + {action:'delta'} -> {pc: 12, kind: 'hp', delta: n} or null (not queueable) */
  function classify(method, pathname, body) {
    if (String(method).toUpperCase() !== 'POST') return null;
    var m = /^\/api\/characters\/(\d+)\/(hp-async|shock)$/.exec(pathname);
    if (!m || !body || body.action !== 'delta') return null;
    var n = parseInt(body.value, 10);
    return isNaN(n) || n === 0 ? null : { pc: parseInt(m[1], 10), kind: KINDS[m[2]], delta: n };
  }

  /* state: {hp, max_hp, temp_hp, shock, shock_max}; returns the response the server would have sent */
  function apply(state, change) {
    if (change.kind === 'hp') {
      var ceiling = (state.max_hp || 0) + (state.temp_hp || 0);
      state.hp = Math.max(0, Math.min(ceiling, (state.hp || 0) + change.delta));
      return { current_hp: state.hp, max_hp: state.max_hp || 0, temp_hp: state.temp_hp || 0, queued: true };
    }
    state.shock = Math.max(0, Math.min(state.shock_max || 0, (state.shock || 0) + change.delta));
    return { shock_current: state.shock, shock_max: state.shock_max || 0, queued: true };
  }

  /* merge neighbours that touch the same number into one entry, so a long offline session replays as a few requests */
  function enqueue(queue, change) {
    var last = queue[queue.length - 1];
    if (last && last.pc === change.pc && last.kind === change.kind) {
      last.delta += change.delta;
      if (last.delta === 0) queue.pop();
    } else {
      queue.push({ pc: change.pc, kind: change.kind, delta: change.delta });
    }
    return queue;
  }

  function requestFor(entry) {
    return { url: '/api/characters/' + entry.pc + '/' + (entry.kind === 'hp' ? 'hp-async' : 'shock'),
             body: { action: 'delta', value: entry.delta } };
  }

  var api = { classify: classify, apply: apply, enqueue: enqueue, requestFor: requestFor };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ndOfflineCore = api;
})(typeof window !== 'undefined' ? window : this);
