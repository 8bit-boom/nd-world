// The combat tracker's rules as pure functions over a state - no DOM, no network, so they run under Node (tests/test_combat_core.py) and
// the page (combat/detail.html) only wires them to buttons. The rule tables come from app/combat_rules.py (RULES_JSON in the page).
//
//   state = { combatants: [...], round, active, name, log: [], ruleset, turn: { active_id, acted: [], picker } }
//
// Two ways to run turns, chosen by the ruleset:
//   INITIATIVE  state.active is a POSITION in initiative order (what the server, the player hub and the maps read). Every change goes
//               through keepActive(), which re-finds the active combatant by id afterwards - editing an initiative, adding or removing
//               someone no longer moves the marker onto another creature.
//   FLUID       state.turn says who is acting; after a turn that combatant CHOOSES who acts next (pickNext); once all have acted the round
//               ends and the last actor chooses the next round's first. (Hunt in the Moonlight, Asterion.)
(function (root) {
  "use strict";
  var C = {};

  function init(c) { var n = parseInt(c && c.initiative, 10); return isNaN(n) ? 0 : n; }
  function tie(c) { var n = parseInt(c && c.tiebreak, 10); return isNaN(n) ? 0 : n; }
  function num(v, d) { var n = parseInt(v, 10); return isNaN(n) ? (d || 0) : n; }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function ids(state) { return state.combatants.map(function (c) { return c.id; }); }

  C.rulesOf = function (rules, id) { return (rules && (rules[id] || rules.generic)) || { turns: "initiative", absorb: ["temp"], eco: [], conditions: [], down: {} }; };
  C.isFluid = function (rules, id) { return C.rulesOf(rules, id).turns === "fluid"; };

  // indices in initiative order: highest initiative, then the higher tiebreak, then the order added (app/combat_turns.py order())
  C.order = function (combatants) {
    return combatants.map(function (c, i) { return i; }).sort(function (a, b) {
      return (init(combatants[b]) - init(combatants[a])) || (tie(combatants[b]) - tie(combatants[a])) || (a - b);
    });
  };

  // the order the page lists them: initiative order, or - with no numbers to sort by - the order they were added
  C.displayOrder = function (state, fluid) {
    return fluid ? state.combatants.map(function (c, i) { return i; }) : C.order(state.combatants);
  };

  C.activeId = function (state, fluid) {
    if (fluid) return (state.turn && state.turn.active_id) || null;
    var o = C.order(state.combatants);
    if (!o.length) return null;
    return state.combatants[o[Math.min(Math.max(state.active || 0, 0), o.length - 1)]].id;
  };

  function turnOf(state) {
    if (!state.turn) state.turn = { active_id: null, acted: [], picker: null };
    return state.turn;
  }

  // run `fn` (which changes state.combatants) and keep the turn with whoever had it. Initiative: the position is recomputed from the id;
  // if that combatant was removed the turn passes to whoever now stands in the same place. Fluid: ids that no longer exist are dropped.
  C.keepActive = function (state, fn, fluid) {
    if (fluid) {
      fn(state);
      var have = {}; ids(state).forEach(function (i) { have[i] = true; });
      var t = turnOf(state);
      if (t.active_id && !have[t.active_id]) t.active_id = null;
      if (t.picker && !have[t.picker]) t.picker = null;
      t.acted = t.acted.filter(function (i) { return have[i]; });
      return state;
    }
    var id = C.activeId(state, false), pos = state.active || 0;
    fn(state);
    var o = C.order(state.combatants);
    if (!o.length) { state.active = 0; return state; }
    var at = id == null ? -1 : o.findIndex(function (i) { return state.combatants[i].id === id; });
    state.active = at >= 0 ? at : Math.min(pos, o.length - 1);
    return state;
  };

  // ── action economy ──────────────────────────────────────────────────────────────────────────────────────────────────
  var COUNT = function (c, name) { var n = 0; (c.conditions || []).forEach(function (x) { if (String(x).toLowerCase() === name.toLowerCase()) n += 1; }); return n; };
  C.stacks = COUNT;

  // what a combatant has for its NEXT turn: the ruleset's economy, less what its conditions take (Daze -2 Actions, Stunned nothing)
  C.ecoMax = function (c, rs) {
    var out = {};
    (rs.eco || []).forEach(function (e) { out[e.id] = e.max; });
    (rs.conditions || []).forEach(function (def) {
      var n = def.eco ? COUNT(c, def.name) : 0;
      if (!n) return;
      Object.keys(def.eco).forEach(function (k) { if (k in out) out[k] = Math.max(0, out[k] + def.eco[k] * (def.stack ? n : 1)); });
    });
    return out;
  };

  C.resetTurnEco = function (c, rs) {
    c.used = c.used || {};
    (rs.eco || []).forEach(function (e) { if (e.per === "turn") c.used[e.id] = 0; });
  };
  C.resetRoundEco = function (state, rs) {
    state.combatants.forEach(function (c) {
      c.used = c.used || {};
      (rs.eco || []).forEach(function (e) { if (e.per === "round") c.used[e.id] = 0; });
      (rs.counter_presets || []).forEach(function (p) {          // a Legendary Action comes back every round
        if (p.refresh === "round" && c.counters && p.label in c.counters) c.counters[p.label] = p.max;
      });
    });
  };

  // a combatant's turn ends: conditions that only last "next turn" (Freeze, Daze) end with it; returns the names removed
  C.expireTurnConditions = function (c, rs) {
    var gone = [];
    (rs.conditions || []).forEach(function (def) {
      if (def.expires !== "turn_end") return;
      var keep = [];
      (c.conditions || []).forEach(function (x) { if (String(x).toLowerCase() === def.name.toLowerCase()) gone.push(x); else keep.push(x); });
      c.conditions = keep;
    });
    return gone;
  };

  // ── turns ──────────────────────────────────────────────────────────────────────────────────────────────────────────
  // INITIATIVE: end the current turn - the next combatant, a new round after the last
  C.nextTurn = function (state) {
    var n = state.combatants.length;
    if (!n) { state.round = (state.round || 1) + 1; return state; }
    var nxt = (state.active || 0) + 1;
    if (nxt >= n) { nxt = 0; state.round = (state.round || 1) + 1; }
    state.active = nxt;
    return state;
  };

  // one turn back (a misclick on Next); before the first turn of round 2 it goes to the last turn of round 1, and stops at round 1
  C.prevTurn = function (state) {
    var n = state.combatants.length;
    if (!n) { state.round = Math.max(1, (state.round || 1) - 1); return state; }
    var prv = (state.active || 0) - 1;
    if (prv < 0) {
      if ((state.round || 1) <= 1) return state;
      prv = n - 1; state.round -= 1;
    }
    state.active = prv;
    return state;
  };

  // FLUID: the actor has finished; they now choose who goes next. Returns {newRound}.
  C.endFluidTurn = function (state) {
    var t = turnOf(state), who = t.active_id, newRound = false;
    if (who && t.acted.indexOf(who) < 0) t.acted.push(who);
    if (ids(state).every(function (i) { return t.acted.indexOf(i) >= 0; })) { t.acted = []; state.round = (state.round || 1) + 1; newRound = true; }
    t.picker = who || t.picker; t.active_id = null;
    return { newRound: newRound, who: who };
  };

  // FLUID: the chooser names who goes next - anyone who has not acted this round. False if that is not allowed.
  C.pickNext = function (state, id) {
    var t = turnOf(state);
    if (ids(state).indexOf(id) < 0 || t.acted.indexOf(id) >= 0) return false;
    t.active_id = id; t.picker = null;
    return true;
  };

  // ── damage, healing, down ──────────────────────────────────────────────────────────────────────────────────────────
  function resCur(c, id) { var r = c.res && c.res[id]; return r ? num(r.cur) : 0; }

  // damage soaks through the ruleset's absorbers in order (temporary HP; Asterion's Spark Shield) before it reaches HP. opts.armor: subtract the
  // combatant's Armor first (physical damage); opts.bypass: ignore it (a condition's tick). Returns what happened, for the log.
  C.damage = function (c, amount, rs, opts) {
    opts = opts || {};
    amount = Math.max(0, num(amount));
    if (opts.armor && !opts.bypass) amount = Math.max(0, amount - Math.max(0, num(c.armor)));
    var left = amount, soaked = {}, wasDown = num(c.hp) <= 0 && num(c.max_hp) > 0;
    ((rs && rs.absorb) || ["temp"]).forEach(function (a) {
      if (left <= 0) return;
      if (a === "temp") {
        var t = Math.min(num(c.temp_hp), left); c.temp_hp = num(c.temp_hp) - t; left -= t; if (t) soaked.temp = t;
      } else if (c.res && c.res[a]) {
        var cur = resCur(c, a), s = Math.min(cur, left); c.res[a].cur = cur - s; left -= s; if (s) soaked[a] = s;
      }
    });
    var hp = num(c.hp), lost = Math.min(hp, left);
    c.hp = hp - lost;
    var out = { amount: amount, soaked: soaked, lost: lost, over: left - lost, failedSave: false };
    // Neon & Dragons: damage taken while dying counts as a failed save
    if (wasDown && amount > 0 && rs && rs.down && rs.down.hp && rs.down.hp.saves) {
      c.saves = c.saves || { ok: 0, fail: 0, ko: 0 };
      c.saves.fail = Math.min(3, num(c.saves.fail) + 1); out.failedSave = true;
      if (c.saves.fail >= 3) c.status = "dead";
    }
    return out;
  };

  C.heal = function (c, amount) {
    amount = Math.max(0, num(amount));
    var max = num(c.max_hp), hp = num(c.hp);
    var gained = Math.max(0, Math.min(max - hp, amount));
    c.hp = hp + gained;
    if (gained && c.saves) { c.saves.ok = 0; c.saves.fail = 0; if (c.status === "dead") c.status = ""; }   // back on their feet: the dying count resets
    return { gained: gained };
  };

  // is this combatant down, and in which way: {kind: "hp"|"shock"|"dead", label, text} or null
  C.downState = function (c, rs) {
    if (c.status === "dead") return { kind: "dead", label: "Dead", text: "" };
    var d = (rs && rs.down) || {};
    if (num(c.max_hp) > 0 && num(c.hp) <= 0) {
      var h = d.hp || { state: "Down" };
      var label = h.state;
      if (h.saves && c.saves && num(c.saves.ok) >= 3) label = "Stable";
      return { kind: "hp", label: label, text: h.text || "", saves: !!h.saves, wound: !!h.wound, press_on: !!h.press_on, execute: !!h.execute };
    }
    if (d.shock && num(c.max_shock) > 0 && num(c.shock) <= 0) return { kind: "shock", label: d.shock.state, text: d.shock.text || "", ko: !!d.shock.ko };
    return null;
  };

  // one death save / unconsciousness roll the GM records: "ok" | "fail" | "ko" (a failed wake-up roll). Returns a log line or "".
  C.recordSave = function (c, kind) {
    c.saves = c.saves || { ok: 0, fail: 0, ko: 0 };
    if (kind === "ok") {
      c.saves.ok = Math.min(3, num(c.saves.ok) + 1);
      return c.saves.ok >= 3 ? "stabilizes" : "death save: success " + c.saves.ok + "/3";
    }
    if (kind === "fail") {
      c.saves.fail = Math.min(3, num(c.saves.fail) + 1);
      if (c.saves.fail >= 3) { c.status = "dead"; return "dies (3 failed saves)"; }
      return "death save: failure " + c.saves.fail + "/3";
    }
    if (kind === "ko") {
      c.saves.ko = Math.min(3, num(c.saves.ko) + 1);
      return c.saves.ko >= 3 ? "is out for an hour (3 failed wake-up rolls)" : "failed to wake: " + c.saves.ko + "/3";
    }
    return "";
  };

  // Hunt in the Moonlight: a Wound each time Health reaches 0; "Press On" (once per session) makes it a Major Wound and restores half max Health
  C.pressOn = function (c) {
    if (c.press_on) return false;
    c.press_on = true; c.wounds = num(c.wounds) + 1;
    c.hp = Math.floor(num(c.max_hp) / 2);
    return true;
  };
  C.addWound = function (c) { c.wounds = num(c.wounds) + 1; return c.wounds; };

  // ── conditions that tick ───────────────────────────────────────────────────────────────────────────────────────────
  C.rollDice = function (expr, rng) {
    rng = rng || Math.random;
    var m = /^(\d+)d(\d+)$/.exec(String(expr || ""));
    if (!m) return 0;
    var total = 0;
    for (var i = 0; i < Math.min(+m[1], 50); i++) total += 1 + Math.floor(rng() * +m[2]);
    return total;
  };

  // the conditions on `combatants` (or one) that tick `when` ("round_end" | "turn_start"): [{id, name, cond, stacks, def}]
  C.pendingTicks = function (state, rs, when, onlyId) {
    var out = [];
    state.combatants.forEach(function (c) {
      if (onlyId && c.id !== onlyId) return;
      if (c.status === "dead") return;
      (rs.conditions || []).forEach(function (def) {
        if (!def.tick || def.tick.when !== when) return;
        var n = COUNT(c, def.name);
        if (n) out.push({ id: c.id, name: c.name, cond: def.name, stacks: n, def: def });
      });
    });
    return out;
  };

  // work out what one pending tick costs: {hp, shock, dice: [...], text}; `rng` for tests
  C.tickCost = function (t, rng) {
    var d = t.def.tick, hp = 0, shock = 0, rolls = [];
    for (var i = 0; i < t.stacks; i++) {
      var amt = d.dice ? C.rollDice(d.dice, rng) : num(d.flat);
      if (d.dice) rolls.push(amt);
      if (d.hp) hp += amt;
      if (d.shock) shock += (d.dice ? 0 : 1) * num(d.shock);
    }
    return { hp: hp, shock: shock, rolls: rolls };
  };

  // ── bookkeeping ────────────────────────────────────────────────────────────────────────────────────────────────────
  C.clone = function (state) { return JSON.parse(JSON.stringify(state)); };

  // Three-way merge of a tracker's unsaved edits (`mine`) onto what the server has now (`theirs`), both descended from `base`: per
  // combatant id and per field, what I changed wins and everything else is theirs - so a player's initiative roll or a map's HP push
  // lands alongside the GM's own click instead of one overwriting the other.
  C.merge3 = function (base, mine, theirs, fluid) {
    var out = { combatants: [], round: 1, active: 0, name: theirs.name, log: [], ruleset: theirs.ruleset, turn: null };
    var byId = function (list) { var m = {}; list.forEach(function (c) { m[c.id] = c; }); return m; };
    var b = byId(base.combatants), m = byId(mine.combatants), t = byId(theirs.combatants);
    var seen = {};
    function take(id) {
      if (seen[id]) return; seen[id] = true;
      var bc = b[id], mc = m[id], tc = t[id];
      if (bc && !mc) return;                                   // I removed it
      if (!bc && mc && !tc) { out.combatants.push(mc); return; }   // I added it
      if (bc && mc && !tc) { if (!same(bc, mc)) out.combatants.push(mc); return; }   // they removed it (kept only if I changed it)
      if (!mc) { out.combatants.push(tc); return; }            // they added it
      var merged = {};
      Object.keys(Object.assign({}, tc, mc)).forEach(function (f) {
        merged[f] = !bc || !same(mc[f], bc[f]) ? mc[f] : tc[f];
        if (merged[f] === undefined) delete merged[f];
      });
      out.combatants.push(merged);
    }
    theirs.combatants.forEach(function (c) { take(c.id); });
    mine.combatants.forEach(function (c) { take(c.id); });
    out.round = same(mine.round, base.round) ? theirs.round : mine.round;
    out.ruleset = same(mine.ruleset, base.ruleset) ? theirs.ruleset : mine.ruleset;
    var have = {}; ids(out).forEach(function (i) { have[i] = true; });
    if (fluid) {
      var pick = same(mine.turn, base.turn) ? theirs.turn : mine.turn;
      out.turn = { active_id: pick && have[pick.active_id] ? pick.active_id : null,
                   acted: ((pick && pick.acted) || []).filter(function (i) { return have[i]; }),
                   picker: pick && have[pick.picker] ? pick.picker : null };
    } else {
      out.turn = theirs.turn;
      var wantId = same(C.activeId(mine, false), C.activeId(base, false)) ? C.activeId(theirs, false) : C.activeId(mine, false);
      var o = C.order(out.combatants);
      var at = wantId == null ? -1 : o.findIndex(function (i) { return out.combatants[i].id === wantId; });
      out.active = at >= 0 ? at : Math.min(theirs.active || 0, Math.max(o.length - 1, 0));
    }
    var seenLog = {};
    (theirs.log || []).concat(mine.log || []).forEach(function (e) {
      var k = e.t + "|" + e.text;
      if (!seenLog[k]) { seenLog[k] = true; out.log.push(e); }
    });
    out.log.sort(function (x, y) { return x.t - y.t; });
    out.log = out.log.slice(-300);
    return out;
  };

  if (typeof module !== "undefined" && module.exports) module.exports = C;
  root.ndCombat = C;
})(typeof window !== "undefined" ? window : globalThis);
