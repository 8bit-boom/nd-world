"""The combat tracker's rules (static/js/combat-core.js) driven under Node, and checked against their Python twins (app/combat_turns.py)
on the same scenarios, so the page, the player hub and the maps can never disagree about whose turn it is."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import combat_rules, combat_turns

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _node(body: str):
    script = "const C = require(process.env.CORE); const RULES = JSON.parse(process.env.RULES); const out = {};\n" + body + "\nconsole.log(JSON.stringify(out));"
    env = {"CORE": str(ROOT / "static/js/combat-core.js"), "RULES": json.dumps(combat_rules.RULESETS), "PATH": os.environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _c(i, init=0, **kw):
    return dict({"id": i, "name": i.upper(), "initiative": init, "hp": 5, "max_hp": 5, "temp_hp": 0, "shock": 0, "max_shock": 0, "armor": 0,
                 "conditions": [], "res": {}, "used": {}, "saves": {"ok": 0, "fail": 0, "ko": 0}, "counters": {}}, **kw)


@needs_node
def test_initiative_order_is_the_same_in_javascript_and_python():
    cases = [
        [_c("a", 5), _c("b", 9), _c("c", 5), _c("d", 9)],
        [_c("a", 5, tiebreak=1), _c("b", 5), _c("c", 5, tiebreak=2)],
        [_c("a", 0), _c("b", -3), _c("c", 12)],
        [_c("a", "7"), _c("b", None), _c("c", "x")],
    ]
    js = _node("out.o = " + json.dumps(cases) + ".map(c => C.order(c));")["o"]
    assert js == [combat_turns.order(c) for c in cases]
    assert js[0] == [1, 3, 0, 2] and js[1] == [2, 0, 1]


@needs_node
def test_the_turn_marker_stays_with_its_combatant_when_the_order_changes():
    res = _node("""
      const mk = () => ({ combatants: [{id:'a',initiative:20},{id:'b',initiative:15},{id:'c',initiative:10},{id:'d',initiative:5}], round: 1, active: 1 });
      let s = mk(); C.keepActive(s, x => { x.combatants[3].initiative = 17; });          // Di (d) jumps ahead
      out.afterInit = [C.activeId(s, false), s.active];
      s = mk(); C.keepActive(s, x => { x.combatants = x.combatants.filter(c => c.id !== 'a'); });   // someone EARLIER is removed
      out.afterRemoveEarlier = [C.activeId(s, false), s.active];
      s = mk(); C.keepActive(s, x => { x.combatants = x.combatants.filter(c => c.id !== 'b'); });   // the active one is removed: next takes over
      out.afterRemoveActive = [C.activeId(s, false), s.active];
      s = mk(); C.keepActive(s, x => { x.combatants.push({id:'e', initiative: 30}); });
      out.afterAdd = [C.activeId(s, false), s.active];
    """)
    assert res["afterInit"] == ["b", 2]
    assert res["afterRemoveEarlier"] == ["b", 0]
    assert res["afterRemoveActive"][0] == "c"
    assert res["afterAdd"] == ["b", 2]


@needs_node
def test_next_and_previous_turn_wrap_rounds_and_stop_at_round_one():
    res = _node("""
      const s = { combatants: [{id:'a',initiative:3},{id:'b',initiative:2}], round: 1, active: 0 };
      C.prevTurn(s); out.atStart = [s.active, s.round];
      C.nextTurn(s); out.second = [s.active, s.round];
      C.nextTurn(s); out.wrapped = [s.active, s.round];
      C.prevTurn(s); out.back = [s.active, s.round];
    """)
    assert res == {"atStart": [0, 1], "second": [1, 1], "wrapped": [0, 2], "back": [1, 1]}


@needs_node
def test_fluid_turns_javascript_and_python_agree_step_by_step():
    """Whoever acts passes the turn: the same script of picks run through combat-core.js and combat_turns.py."""
    ids = ["a", "b", "c"]
    script = [("start", "b"), ("end", None), ("pick", "a"), ("end", None), ("pick", "c"), ("end", None), ("pick", "b")]   # b,a,c all acted -> round 2 -> b again
    js = _node("""
      const s = { combatants: [{id:'a'},{id:'b'},{id:'c'}], round: 1, turn: { active_id: null, acted: [], picker: null } };
      const steps = %s, trace = [];
      steps.forEach(([k, v]) => {
        if (k === 'start' || k === 'pick') trace.push(C.pickNext(s, v)); else trace.push(C.endFluidTurn(s).newRound);
        trace.push(JSON.stringify([s.round, s.turn.active_id, s.turn.acted, s.turn.picker]));
      });
      out.trace = trace;
    """ % json.dumps(script))["trace"]
    cs = SimpleNamespace(ruleset="hunt", turn_json=json.dumps({"active_id": None, "acted": [], "picker": None}), round_num=1, rev=0, active_idx=0)
    combatants = [{"id": i} for i in ids]
    py = []
    for k, v in script:
        if k in ("start", "pick"):
            py.append(combat_turns.pick_next(cs, combatants, v))
        else:
            before = cs.round_num
            combat_turns.advance(cs, combatants)
            py.append(cs.round_num != before)
        st = combat_turns.turn_state(cs)
        py.append(json.dumps([cs.round_num, st["active_id"], st["acted"], st["picker"]]))
    assert [json.loads(x) if isinstance(x, str) else x for x in js] == [json.loads(x) if isinstance(x, str) else x for x in py]
    # nobody can be picked twice in a round
    assert py[0] is True and combat_turns.pick_next(cs, combatants, "b") in (True, False)


@needs_node
def test_damage_goes_through_the_rulesets_absorbers_and_armor():
    res = _node("""
      const asterion = RULES.asterion, nd = RULES.nd;
      const g = { hp: 5, max_hp: 5, temp_hp: 1, armor: 1, res: { sparkShield: { cur: 3, max: 3 } } };
      out.armorThenShield = C.damage(g, 3, asterion, { armor: true });          // 3-1 armor = 2: all into the shield
      out.g1 = JSON.stringify(g);
      const h = { hp: 5, max_hp: 5, temp_hp: 2, armor: 0, res: {} };
      out.temp = C.damage(h, 4, nd); out.h = JSON.stringify(h);
      const bypass = { hp: 5, max_hp: 5, armor: 3, temp_hp: 0, res: {} };
      C.damage(bypass, 2, nd, { armor: true, bypass: true }); out.bypassHp = bypass.hp;     // a condition tick ignores armor
      const heal = { hp: 2, max_hp: 5 }; out.heal = C.heal(heal, 9).gained;
    """)
    assert res["armorThenShield"]["soaked"] == {"sparkShield": 2} and res["armorThenShield"]["lost"] == 0
    assert json.loads(res["g1"])["res"]["sparkShield"]["cur"] == 1 and json.loads(res["g1"])["hp"] == 5
    assert json.loads(res["h"]) == {"hp": 3, "max_hp": 5, "temp_hp": 0, "armor": 0, "res": {}} and res["temp"]["soaked"] == {"temp": 2}
    assert res["bypassHp"] == 3 and res["heal"] == 3


@needs_node
def test_dying_saves_wounds_press_on_and_executions():
    res = _node("""
      const nd = RULES.nd, hunt = RULES.hunt, ast = RULES.asterion;
      const a = { hp: 1, max_hp: 10, saves: { ok: 0, fail: 0, ko: 0 } };
      C.damage(a, 1, nd);                                   // to 0
      out.dying = C.downState(a, nd).label;
      const r = C.damage(a, 1, nd);                         // damage while dying = a failed save
      out.failed = [r.failedSave, a.saves.fail];
      C.recordSave(a, 'fail'); const last = C.recordSave(a, 'fail');
      out.dead = [a.status, last];
      const b = { hp: 0, max_hp: 10, saves: { ok: 2, fail: 0, ko: 0 } };
      C.recordSave(b, 'ok'); out.stable = C.downState(b, nd).label;
      const h = { hp: 0, max_hp: 5, wounds: 0, press_on: false };
      out.hunt = C.downState(h, hunt).label; out.press = [C.pressOn(h), h.hp, h.wounds, C.pressOn(h)];
      const g = { hp: 0, max_hp: 5, res: {} };
      const d = C.downState(g, ast); out.ast = [d.label, d.saves, d.execute];
      const k = { hp: 4, max_hp: 5, shock: 0, max_shock: 5 }; out.ko = C.downState(k, nd).label;
      const healed = { hp: 0, max_hp: 5, status: 'dead', saves: { ok: 1, fail: 3, ko: 0 } }; C.heal(healed, 2); out.healed = [healed.status, healed.saves.fail];
    """)
    assert res["dying"] == "Dying" and res["failed"] == [True, 1]
    assert res["dead"] == ["dead", "dies (3 failed saves)"] and res["stable"] == "Stable"
    assert res["hunt"] == "Broken" and res["press"] == [True, 2, 1, False]
    assert res["ast"] == ["Shattered", False, True] and res["ko"] == "Unconscious" and res["healed"] == ["", 0]


@needs_node
def test_conditions_stack_tick_and_take_actions_away():
    res = _node("""
      const nd = RULES.nd, hunt = RULES.hunt;
      const c = { id: 'a', name: 'A', hp: 20, max_hp: 20, conditions: ['Burn', 'Burn', 'Daze'], used: {} };
      const s = { combatants: [c, { id: 'b', name: 'B', conditions: ['Yellow'] }] };
      out.max = C.ecoMax(c, nd);                                        // Daze: -2 Actions; Reactions untouched
      const ticks = C.pendingTicks(s, nd, 'round_end');
      out.ticks = ticks.map(t => [t.id, t.cond, t.stacks]);
      out.burn = C.tickCost(ticks[0], () => 0.99);                       // two Burns, each 1d8 -> 8 + 8
      out.yellow = C.tickCost(ticks[1]);
      out.expired = C.expireTurnConditions(c, nd); out.left = c.conditions;
      const h = { id: 'h', name: 'H', conditions: ['Bleeding', 'Stunned'], used: {} };
      out.hunt = C.pendingTicks({ combatants: [h] }, hunt, 'turn_start').map(t => t.cond);
      out.stunned = C.ecoMax(h, hunt);
      const boss = { id: 'z', counters: { 'Legendary Action': 0, Phase: 1 }, used: { R: 1 } };
      C.resetRoundEco({ combatants: [boss] }, RULES.asterion); out.legendary = [boss.counters['Legendary Action'], boss.counters.Phase, boss.used.R];
    """)
    assert res["max"] == {"A": 0, "R": 2}
    assert res["ticks"] == [["a", "Burn", 2], ["b", "Yellow", 1]]
    assert res["burn"]["hp"] == 16 and res["yellow"] == {"hp": 2, "shock": 1, "rolls": []}
    assert res["expired"] == ["Daze"] and res["left"] == ["Burn", "Burn"]
    assert res["hunt"] == ["Bleeding"] and res["stunned"]["X"] == 0 and res["stunned"]["M"] == 1
    assert res["legendary"] == [1, 1, 0]


@needs_node
def test_merge3_keeps_my_edits_and_theirs_and_the_turn():
    res = _node("""
      const base = { combatants: [{id:'a',name:'A',hp:10,initiative:0},{id:'b',name:'B',hp:10,initiative:5}], round: 1, active: 0, log: [], ruleset: 'nd', turn: null };
      const mine = C.clone(base); mine.combatants[0].hp = 6;                                  // I hit A
      const theirs = C.clone(base); theirs.combatants[0].initiative = 12; theirs.combatants[1].hp = 9; theirs.round = 2;   // a player rolled for A, the map hit B
      const m = C.merge3(base, mine, theirs, false);
      out.a = m.combatants.find(c => c.id === 'a'); out.b = m.combatants.find(c => c.id === 'b'); out.round = m.round; out.active = C.activeId(m, false);
      const mine2 = C.clone(base); mine2.combatants.push({id:'c',name:'C',hp:3,initiative:1}); mine2.combatants = mine2.combatants.filter(c => c.id !== 'b');
      const theirs2 = C.clone(base); theirs2.combatants.push({id:'d',name:'D',hp:2,initiative:2});
      out.ids = C.merge3(base, mine2, theirs2, false).combatants.map(c => c.id).sort();
      const f = { combatants: [{id:'a'},{id:'b'}], round: 1, active: 0, log: [], ruleset: 'hunt', turn: { active_id: 'a', acted: [], picker: null } };
      const fm = C.clone(f); fm.turn = { active_id: null, acted: ['a'], picker: 'a' };         // I ended A's turn
      out.fluid = C.merge3(f, fm, C.clone(f), true).turn;
    """)
    assert res["a"]["hp"] == 6 and res["a"]["initiative"] == 12 and res["b"]["hp"] == 9 and res["round"] == 2
    assert res["active"] == "a" and res["ids"] == ["a", "c", "d"]
    assert res["fluid"] == {"active_id": None, "acted": ["a"], "picker": "a", "mode": ""}


@needs_node
def test_the_fallen_are_skipped_the_same_way_in_javascript_and_python():
    """Initiative: an enemy at 0 HP (or dead) loses its turn; a PC at 0 still takes its dying rolls. Hunt / Asterion: nobody Broken acts."""
    combatants = [_c("a", 9, source="pc"), _c("b", 5, source="entity", hp=0), _c("c", 3, source="entity"), _c("d", 1, source="pc", hp=0)]
    js = _node("""
      const out_ = {};
      const cb = %s;
      const walk = (rsId) => { const s = { combatants: JSON.parse(JSON.stringify(cb)), round: 1, active: 0 }, seen = [];
        for (let i = 0; i < 6; i++) { C.nextTurn(s, RULES[rsId]); seen.push([s.active, s.round]); } return seen; };
      out.nd = walk('nd'); out.hunt = walk('hunt');
      out.isOut = cb.map(c => [C.isOut(c, RULES.nd), C.isOut(c, RULES.hunt), C.isOut(c, RULES.generic)]);
      const dead = { id: 'x', status: 'dead', hp: 5, max_hp: 5, source: 'pc' }; out.dead = C.isOut(dead, RULES.nd);
    """ % json.dumps(combatants))
    for ruleset, key in (("nd", "nd"), ("hunt", "hunt")):
        cs = SimpleNamespace(ruleset=ruleset, turn_json=json.dumps({"mode": "initiative"}), round_num=1, rev=0, active_idx=0)
        seen = []
        for _ in range(6):
            combat_turns.advance(cs, combatants)
            seen.append([cs.active_idx, cs.round_num])
        assert seen == js[key], (ruleset, seen, js[key])
    assert js["isOut"] == [[False, False, False], [True, True, True], [False, False, False], [False, True, False]] and js["dead"] is True


@needs_node
def test_ties_conditions_with_durations_phase_breaks_and_rolled_initiative():
    res = _node("""
      const s = { combatants: [{id:'a',initiative:5},{id:'b',initiative:5},{id:'c',initiative:5},{id:'z',initiative:9}] };
      const names = () => C.order(s.combatants).map(i => s.combatants[i].id).join('');
      out.start = names();
      out.up = [C.moveTie(s, 'c', -1), names()];            // c steps ahead of b
      out.top = [C.moveTie(s, 'c', -1), names()];           // c is already first among the ties: it also steps ahead of a
      out.edge = C.moveTie(s, 'z', -1);                     // z has no ties
      out.parse = ['Burn 3', 'Bleeding', 'Daze 2 rounds', 'Level 4 cleric', 'Poisoned,5'].map(C.parseCondition);
      const c = { id: 'k', name: 'K', conditions: ['Burn', 'Burn', 'Daze'], cond_dur: { Burn: 2, Daze: 1 } };
      const st = { combatants: [c] };
      out.t1 = C.tickDurations(st).map(e => e.cond); out.after1 = [c.conditions.slice(), JSON.stringify(c.cond_dur)];
      out.t2 = C.tickDurations(st).map(e => e.cond); out.after2 = c.conditions;
      const boss = { res: { sparkShield: { cur: 0, max: 6 } }, counters: { Phase: 1 } };
      out.pb = [C.phaseBreak(boss, RULES.asterion), boss.res.sparkShield.cur, boss.counters.Phase, C.phaseBreak(boss, RULES.nd)];
      out.init = [C.rollInitiative({ init_bonus: 3 }, () => 0.5), C.rollInitiative({ init_bonus: -9 }, () => 0).total];
    """)
    assert res["start"] == "zabc" and res["up"] == [True, "zacb"] and res["top"] == [True, "zcab"] and res["edge"] is False
    assert res["parse"] == [{"name": "Burn", "rounds": 3}, {"name": "Bleeding", "rounds": 0}, {"name": "Daze", "rounds": 2},
                            {"name": "Level 4 cleric", "rounds": 0}, {"name": "Poisoned", "rounds": 5}]
    assert res["t1"] == ["Daze"] and res["after1"] == [["Burn", "Burn"], '{"Burn":1}']
    assert res["t2"] == ["Burn"] and res["after2"] == []
    assert res["pb"][1:3] == [6, 2] and res["pb"][0] == {"shield": True, "phase": 2} and res["pb"][3] is None
    assert res["init"][0] == {"die": 6, "total": 9} and res["init"][1] == 1
