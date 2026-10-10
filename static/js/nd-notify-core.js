/* Which alerts a change on the character page deserves (static/js/nd-notify.js shows them). Pure: compares two snapshots of the
 * strip's data (GET /api/characters/{id}/hub/now) so it runs under Node in the tests. The first snapshot never alerts - opening the
 * page is not a change. */
(function (root) {
  'use strict';
  function decide(prev, next) {
    var out = [];
    if (!prev || !next) return out;
    var pc = prev.combat, nc = next.combat;
    if (nc && nc.turn === 'me' && !(pc && pc.turn === 'me' && pc.round === nc.round)) {
      out.push({ tag: 'turn', title: '⚔ Your turn!', body: 'Round ' + nc.round + ' — it is ' + (next.me && next.me.name ? next.me.name : 'your character') + '’s turn.', vibrate: [200, 100, 200] });
    } else if (nc && nc.next_is_me && !(pc && pc.next_is_me && pc.round === nc.round)) {
      out.push({ tag: 'next', title: '⚔ You are up next', body: 'Get ready — round ' + nc.round + '.', vibrate: [120] });
    }
    if ((next.handouts_new || 0) > (prev.handouts_new || 0)) {
      out.push({ tag: 'handout', title: '📬 New handout', body: 'Your GM showed the table something — it is kept on your character page.', vibrate: [80] });
    }
    var ps = prev.next_session, ns = next.next_session;
    if (ns && (!ps || ps.starts_at !== ns.starts_at || ps.title !== ns.title)) {
      out.push({ tag: 'session', title: '📅 Game night', body: (ns.title || 'Next session') + ' — ' + ns.starts_at.replace('T', ' ').replace(/:\d\dZ$/, ' UTC'), vibrate: [80] });
    } else if ((next.polls_waiting || 0) > (prev.polls_waiting || 0)) {
      out.push({ tag: 'poll', title: '📅 A new session poll', body: 'Your GM is asking when you can play.', vibrate: [80] });
    }
    return out;
  }
  var api = { decide: decide };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ndNotifyCore = api;
})(typeof window !== 'undefined' ? window : this);
