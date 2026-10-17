"""How each rule system runs a fight (a leaf module: plain data + two tiny helpers; no router, no database).

The combat tracker is system-aware. An encounter has a `ruleset` (CombatSession.ruleset) and the page, the player view and the turn
helpers read what that ruleset does from here; the browser gets the same table as JSON (RULES_JSON in combat/detail.html), so the Python
and the JavaScript never carry two copies of a rule. The four rule books this was written against:

  nd       Neon & Dragons     - initiative = Speed (+ d10); 2 Actions and 2 Reactions per round; Health and Shock; at 0 Health you are
                                dying (3 saves / 3 fails), at 0 Shock unconscious; conditions STACK and tick at the end of the round
  worm     Chronicles of the Worm - the same engine and conditions as Neon & Dragons
  hunt     Hunt in the Moonlight  - FLUID initiative: the instigator acts first, after a turn that player CHOOSES who acts next, the last
                                actor of a round chooses the next round's first; a turn is 1 Movement + 1 Main Action (+ 1 Tool Action) and
                                1 Reaction per round; Health 5, Stamina, Strain; 0 Health = Broken, a permanent Wound, "Press On" once per
                                session; Bleeding / Burning cost 1 damage at the START of the turn
  asterion Game of Gods (Asterion) - fluid initiative like Hunt; Spark Shield (3) soaks damage first, then Flesh (5); 0 Flesh = Shattered
                                (no death saves; an enemy may execute a Shattered character with a Main Action); Ichor (5) is spent on
                                abilities; 1 Movement + 1 Main Action, 1 Reaction per round; Armor never above 3 and never applies to tie
                                damage, Burning or Bleeding; bosses have a Legendary Action each round and a Phase Break below half Flesh
  nd_asterion / worm_asterion - Neon & Dragons / Chronicles of the Worm played on the Asterion engine ("Rulebook Conversion"): fluid
                                initiative, Health + Shock + PP + MP (no Spark Shield), Broken at 0 Health with Wounds and Press On, the
                                engine's conditions plus the setting's own (Yellow, Toxin ... / Shaken)
  generic  anything else - the original tracker: initiative order, HP, optional Shock

Used by: routers/combat.py (page + API), combat_turns.py (turn order), combat_state.py (what is stored), static/js/combat-core.js (turn
rules, mirrored and tested against the same cases in tests/test_combat_core.py).
"""
from typing import Optional

TURNS_INITIATIVE = "initiative"      # numeric initiative, highest first
TURNS_FLUID = "fluid"                # the instigator first, then whoever the last actor picks


def _cond(name, text, stack=False, tick=None, eco=None, expires=None):
    c = {"name": name, "text": text, "stack": stack}
    if tick:
        c["tick"] = tick
    if eco:
        c["eco"] = eco
    if expires:
        c["expires"] = expires
    return c


# what a condition costs when it ticks: when = "round_end" | "turn_start"; dice = a dice expression rolled per stack, flat = a fixed amount per
# stack; hp / shock = what it takes; bypass = ignores armor; shield_first = Spark Shield soaks it before Flesh
_ND_CONDITIONS = [
    _cond("Burn", "1d8 damage at the end of every round until an Action is spent putting it out.", stack=True,
          tick={"when": "round_end", "dice": "1d8", "hp": True}),
    _cond("Freeze", "Lose 1 Action and 1 Reaction next turn.", stack=True, eco={"A": -1, "R": -1}, expires="turn_end"),
    _cond("Toxin", "Roll Body vs the source's Difficulty or take d4 damage that ignores armor.", stack=True,
          tick={"when": "round_end", "dice": "1d4", "hp": True, "bypass": True, "note": "Body vs Difficulty - on a failed roll"}),
    _cond("Bleeding", "Take +1d4 damage whenever you are damaged.", stack=True),
    _cond("Blind", "Fail an action unless a Perception check succeeds."),
    _cond("Yellow", "Lose 2 Health and 1 Shock each round, no save; at 0 you are consumed.", stack=True,
          tick={"when": "round_end", "flat": 2, "hp": True, "shock": 1, "bypass": True}),
    _cond("Charm", "Cannot attack non-hostile targets."),
    _cond("Daze", "Lose 2 Actions next turn.", stack=True, eco={"A": -2}, expires="turn_end"),
    _cond("Stunned", "No Actions or Reactions.", eco={"A": -99, "R": -99}),
]

_FLUID_CONDITIONS_COMMON = [
    _cond("Blinded", "Ranged attacks fail; melee attack pools lose 2d10 (2 dice)."),
    _cond("Restrained", "Movement is 0 until you escape with an opposed roll."),
    _cond("Weakened", "Roll 1 fewer die on attack pools."),
    _cond("Vulnerable", "Armor does not reduce damage and you lose resistances until it ends."),
]

RULESETS = {
    "generic": {
        "label": "Generic", "turns": TURNS_INITIATIVE, "hp_label": "HP", "shock_label": "Shock",
        "absorb": ["temp"], "eco": [], "init_hint": "",
        "down": {"hp": {"state": "Down", "saves": False}},
        "conditions": [
            _cond("Bleeding", "Takes damage over time."), _cond("Burning", "Takes damage over time."), _cond("Blinded", ""),
            _cond("Restrained", ""), _cond("Stunned", ""), _cond("Weakened", ""), _cond("Vulnerable", ""),
            _cond("Prone", ""), _cond("Charmed", ""),
        ],
    },
    "nd": {
        "label": "Neon & Dragons", "turns": TURNS_INITIATIVE, "hp_label": "Health", "shock_label": "Shock",
        "absorb": ["temp"],
        "eco": [{"id": "A", "label": "Actions", "per": "turn", "max": 2}, {"id": "R", "label": "Reactions", "per": "round", "max": 2}],
        "init_hint": "Initiative = Speed (Dexterity + Intuition) + d10; players roll their own from their character page.",
        "down": {"hp": {"state": "Dying", "saves": True,
                        "text": "At the end of each of its rounds: Body vs 10. 3 successes stabilize, 3 failures die; damage while dying is a failure."},
                 "shock": {"state": "Unconscious", "ko": True,
                           "text": "d10 each round: 5+ regains 1 Shock and wakes; 3 failures = out for an hour."}},
        "conditions": _ND_CONDITIONS,
    },
    "worm": {
        "label": "Chronicles of the Worm", "turns": TURNS_INITIATIVE, "hp_label": "Health", "shock_label": "Shock",
        "absorb": ["temp"],
        "eco": [{"id": "A", "label": "Actions", "per": "turn", "max": 2}, {"id": "R", "label": "Reactions", "per": "round", "max": 2}],
        "init_hint": "Initiative = Speed + d10.",
        "down": {"hp": {"state": "Dying", "saves": True,
                        "text": "At the end of each of its rounds: Body vs 10. 3 successes stabilize, 3 failures die; damage while dying is a failure."},
                 "shock": {"state": "Unconscious", "ko": True,
                           "text": "d10 each round: 5+ regains 1 Shock and wakes; 3 failures = out for an hour."}},
        "conditions": _ND_CONDITIONS,
    },
    "hunt": {
        "label": "Hunt in the Moonlight", "turns": TURNS_FLUID, "hp_label": "Health", "shock_label": None,
        "absorb": ["temp"],
        "eco": [{"id": "M", "label": "Movement", "per": "turn", "max": 1}, {"id": "X", "label": "Main Action", "per": "turn", "max": 1},
                {"id": "T", "label": "Tool Action", "per": "turn", "max": 1}, {"id": "R", "label": "Reaction", "per": "round", "max": 1}],
        "init_hint": "Fluid initiative: whoever starts the violence acts first; after a turn that player chooses who acts next; the last actor of a round picks the first of the next.",
        "armor_cap": 3,
        "counter_presets": [{"label": "Beast Actions", "max": 6, "text": "A Boss's fixed 6-point pool per stage, taken between other characters' turns; refreshes only on a stage change."},
                            {"label": "Stage", "max": 3}],
        "down": {"hp": {"state": "Broken", "wound": True, "press_on": True,
                        "text": "Each time Health drops to 0 the Hunter suffers a permanent Wound; once per session they may Press On (a Major Wound, regain half max Health) instead of falling Broken."}},
        "conditions": [
            _cond("Bleeding", "1 damage at the start of your turn until treated.", tick={"when": "turn_start", "flat": 1, "hp": True}),
            _cond("Burning", "1 damage at the start of your turn until it ends.", tick={"when": "turn_start", "flat": 1, "hp": True}),
            _cond("Blinded", "Ranged attacks fail; lose 2d10 from melee attack pools."),
            _cond("Marked", "The next ally to attack you gains +1d10; it clears once used or at the end of the scene."),
            _cond("Prone", "Attacks against you gain +1d10; standing up costs half your Movement."),
            _cond("Restrained", "Movement is 0 until you escape with an opposed roll."),
            _cond("Stunned", "You lose your Main Action and defend at -1d10.", eco={"X": -99}),
            _cond("Weakened", "Roll -1d10 on attack pools."),
            _cond("Vulnerable", "Armor does not reduce damage; you lose relevant resistance."),
        ],
    },
    "asterion": {
        "label": "Game of Gods (Asterion)", "turns": TURNS_FLUID, "hp_label": "Flesh", "shock_label": None,
        "absorb": ["sparkShield", "temp"],
        "eco": [{"id": "M", "label": "Movement", "per": "turn", "max": 1}, {"id": "X", "label": "Main Action", "per": "turn", "max": 1},
                {"id": "R", "label": "Reaction", "per": "round", "max": 1}],
        "init_hint": "Fluid initiative: the instigator goes first; after a turn that player chooses who acts next; the last actor of a round chooses the next round's first.",
        "res_defaults": {"sparkShield": {"label": "Spark Shield", "max": 3}, "ichor": {"label": "Ichor", "max": 5}},
        "shield_id": "sparkShield",
        "armor_cap": 3,
        "counter_presets": [{"label": "Legendary Action", "max": 1, "refresh": "round", "text": "A boss's free reaction each round, outside its turn - in addition to its Reaction."},
                            {"label": "Phase", "max": 2, "text": "Phase Break below half Flesh: the Spark Shield fully refreshes and a new ability unlocks."}],
        "down": {"hp": {"state": "Shattered", "saves": False, "execute": True,
                        "text": "Shattered: falls, cannot act, loses the bout. An enemy may execute it with a Main Action - no roll, no death saves. Wakes after the fight with Flesh 1."}},
        "conditions": [
            _cond("Blinded", "Ranged attacks fail; lose 2 dice from melee attack pools."),
            _cond("Burning", "1 damage at the start of your turn; bypasses Armor and Resistances, Spark Shield soaks it first.",
                  tick={"when": "turn_start", "flat": 1, "hp": True, "bypass": True, "shield_first": True}),
            _cond("Bleeding", "1 damage at the start of your turn; bypasses Armor and Resistances, Spark Shield soaks it first.",
                  tick={"when": "turn_start", "flat": 1, "hp": True, "bypass": True, "shield_first": True}),
            _cond("Restrained", "Movement speed is 0."),
            _cond("Stunned", "No Main Action or Movement; one fewer die in your Defender pool.", eco={"X": -99, "M": -99}),
            _cond("Weakened", "Roll 1 fewer die in your Attacker pool."),
            _cond("Vulnerable", "Armor is 0 and you lose any Resistances."),
        ],
    },
}


_ENGINE_COMMON = [
    _cond("Burning", "1 damage at the start of your turn (to Health, or Shock if that fits the source); bypasses Armor and Resistances.",
          tick={"when": "turn_start", "flat": 1, "hp": True, "bypass": True}),
    _cond("Bleeding", "1 damage at the start of your turn; bypasses Armor and Resistances.",
          tick={"when": "turn_start", "flat": 1, "hp": True, "bypass": True}),
    _cond("Blinded", "Ranged attacks automatically fail; melee attack pools lose 2 dice."),
    _cond("Restrained", "Movement is 0."),
    _cond("Stunned", "No Main Action or Movement; one fewer die when defending.", eco={"X": -99, "M": -99}),
    _cond("Weakened", "One fewer die on attack pools."),
    _cond("Vulnerable", "Armor and Resistances drop to 0."),
    _cond("Prone", "Attacks against you gain +1d10; standing up costs half your Movement."),
    _cond("Marked", "The next ally to attack you gains +1d10."),
]

_HYBRID_DOWN = {
    "hp": {"state": "Broken", "wound": True, "press_on": True,
           "text": "Broken: out of the fight, not automatically dead. Every drop to 0 Health leaves a permanent Wound; once per session Press On (a Major Wound) to regain half max Health instead of falling."},
    "shock": {"state": "Unconscious", "ko": True,
              "text": "d10 each round: 5+ regains 1 Shock and wakes; 3 failures = out for an hour."},
}


def _hybrid(label, extra):
    return {
        "label": label, "turns": TURNS_FLUID, "hp_label": "Health", "shock_label": "Shock", "absorb": ["temp"],
        "eco": [{"id": "M", "label": "Movement", "per": "turn", "max": 1}, {"id": "X", "label": "Main Action", "per": "turn", "max": 1},
                {"id": "R", "label": "Reaction", "per": "round", "max": 1}],
        "init_hint": "Fluid initiative: whoever starts it acts first, each actor then chooses who goes next, the last actor of a round opens the next. Healing from items is capped at 2 Health per scene.",
        "armor_cap": 3,
        "counter_presets": [{"label": "Legendary Action", "max": 1, "refresh": "round", "text": "A boss's free reaction each round, outside its turn - in addition to its Reaction."},
                            {"label": "Phase", "max": 2, "text": "Phase Break below half Flesh/Health: the shield refreshes and a new ability unlocks."}],
        "res_defaults": {"pp": {"label": "PP", "max": 5}, "mp": {"label": "MP", "max": 5}},
        "down": _HYBRID_DOWN,
        "conditions": _ENGINE_COMMON + extra,
    }


RULESETS["nd_asterion"] = _hybrid("Neon & Dragons (Asterion engine)", [
    _cond("Toxin", "1 toxic damage at the start of your turn, ignores Armor, until treated (Body / Professional Skill Check).",
          tick={"when": "turn_start", "flat": 1, "hp": True, "bypass": True}),
    _cond("Yellow", "Lose 1 Health AND 1 Shock at the start of your turn; at 0 in either you are consumed.", stack=True,
          tick={"when": "turn_start", "flat": 1, "hp": True, "shock": 1, "bypass": True}),
    _cond("Slowed", "Movement halved (Freeze; Restrained when severe)."),
    _cond("Charm", "Cannot attack the charmer or its allies; Willpower / Professional to break when harmed by them."),
])
RULESETS["worm_asterion"] = _hybrid("Chronicles of the Worm (Asterion engine)", [
    _cond("Shaken", "-1d10 on all pools for 1 round."),
    _cond("Charm", "Cannot attack the charmer or its allies."),
])

RULESET_IDS = tuple(RULESETS)
# template slug of a built-in sheet system -> ruleset
_BY_TEMPLATE_SLUG = {"hunt-in-the-moonlight": "hunt", "asterion": "asterion", "nd-default": "nd"}


def ruleset_for_slugs(slugs) -> str:
    """The ruleset a set of sheet-template slugs points to (Hunt or Asterion if any character uses it, else Neon & Dragons); "" when
    nothing tells."""
    for s in slugs:
        if _BY_TEMPLATE_SLUG.get(s) in ("hunt", "asterion"):
            return _BY_TEMPLATE_SLUG[s]
    return "nd" if any(s in (None, "", "nd-default") for s in slugs) and slugs else ""


def get(ruleset: Optional[str]) -> dict:
    """The profile for a stored ruleset id; anything unknown is the generic tracker."""
    return RULESETS.get(ruleset or "", RULESETS["generic"])


def is_fluid(ruleset: Optional[str]) -> bool:
    return get(ruleset)["turns"] == TURNS_FLUID


def condition(ruleset: Optional[str], name: str) -> Optional[dict]:
    low = (name or "").casefold()
    return next((c for c in get(ruleset)["conditions"] if c["name"].casefold() == low), None)
