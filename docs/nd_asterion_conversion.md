# Neon & Dragons — Asterion Rulebook

A complete, standalone character-creation and play rulebook for running **Neon &
Dragons** (N&D) on the **Asterion** engine (the d10 dice-pool system documented in
full in `asterion_rules.md`). You can build and play a character from this
document alone. It still points to `asterion_rules.md` for GM-facing material
that isn't part of character creation or ordinary play — the full bestiary/NPC
stat blocks, Domain Reclamation, item crafting, and the Reputation/Followers
subsystems.

**What stays the same as N&D:** the actual catalog — all 17 races, 6 professions,
5 origins, and 22 major edges, by name and fiction, exactly as they exist in Neon
& Dragons today, plus N&D's own 8 Stats and its Health/Shock/PP/MP resource
layer.

**What changes:** instead of a fixed feat catalog with fixed mechanical bonuses,
every special thing a race, profession, background, or major edge gives you
becomes a player-invented Asterion ability — Tier 1/2/3, Active or Passive, built
from Asterion's own property tables (inlined below). Nothing from N&D's own
special rules is left out: where a race or profession has more than one named
mechanic in its own text, it gets more than one ability here too.

Readers who already know vanilla Asterion: this rulebook replaces Asterion's
Flesh/Ichor/Spark Shield resource layer with N&D's own Health/Shock/PP/MP (see
"Resources" below), and replaces flat Tier costs paid from one pool with
costs paid from whichever of PP/MP fits the ability. Everything else about the
engine — dice pools, exploding 10s, opposed rolls, the Tier property tables,
Trade-Off, Glory — works exactly as in `asterion_rules.md`.

---

## Stats

Every character has N&D's 8 Stats, split into two categories. There's nothing
to buy or distribute — Stats aren't numbered at all here. They exist to name
things: which pool (PP or MP) an ability draws from, which die a Skill Check
rolls, and which Stat your three Signature Stats point to.

**Physical:** Strength (melee damage, lifting), Dexterity (speed, ranged
accuracy), Body (toughness, endurance), Perception (awareness, detection).

**Mental:** Willpower (mental resistance, discipline), Intellect (knowledge,
hacking, analysis), Charisma (presence, persuasion), Intuition (reflexes,
instinct, initiative).

A race's own "+1 [Stat]" line (from its N&D fiction) is simply a pointer to
that race's Signature Stat below — it's not a number to add anywhere.

---

## Resources

Every character starts with the same four numbers — no Stat math involved:

| Resource | Starting Value | Represents |
|---|---|---|
| **Health** | 5 | Physical harm — weapons, brute force, toxins, fire, cyberware failure. At 0 Health you are dying and incapacitated. |
| **Shock** | 5 | Mental/psychic/social harm — fear, intimidation, psychic assault, Yellow corruption, overwhelming stress. At 0 Shock you are unconscious (roll d10 each round: 5+ regains 1 Shock and wakes you up; 3 failures = out for an hour). |
| **PP** (Physical Points) | 5 | Fuels abilities rooted in a Physical Stat — melee, gunplay, toughness, physical senses. |
| **MP** (Mental Points) | 5 | Fuels abilities rooted in a Mental Stat — hacking, persuasion, willpower, psychic effects. |

This matches Asterion's own Flesh/Ichor baseline (5) and keeps the Ability
Construction Tier table below (1/2/4 damage, 0/1/3 PP-MP cost) calibrated the
way it was designed — a handful of races adjust these starting numbers
(noted in their own entry below); everyone else grows them the normal way,
through Glory.

There is no Spark Shield or other absorption layer — damage and harmful effects
go straight to Health or Shock, whichever matches their fiction. Most abilities
only ever touch one track; a handful (Yellow corruption, cyberpsychosis) can
plausibly hit both, at GM's discretion. Glory can raise any of the four (see
"Progression and Glory" below).

Every Tier cost below is paid from PP or MP — whichever pool matches the
ability's fiction (Tier 1 = 0, Tier 2 = 1, Tier 3 = 3). Purely social/narrative
abilities with no damage, restoration, or Max-track component simply cost 0 PP
or 0 MP and don't otherwise touch Health/Shock.

Characters may use up to **3 stims per Rest** (N&D's own limit), unless a
race's entry says otherwise.

---

## Dice & Play Engine

### Core Mechanic

Every action with a chance of failure uses a d10 dice pool.

- **Success:** any die that rolls 6+ counts as 1 Success.
- **Exploding 10s:** a 10 counts as a Success *and* you roll an extra d10 to add
  to your pool; if that die is also a 10, it explodes again.
- **Pool size:** roll **2d10** for a standard action (attacking, dodging, a
  skill check). Roll the **Domain Pool (3d10)** for an action tied directly to
  one of your Race/Profession/Background/Major Edge abilities.
- **Pushing Your Limits:** before rolling, spend 1 PP or MP to add +1d10 to your
  pool (spend PP for a physical push, MP for a mental one). You can do this
  multiple times if you have the resource to spend.

### Signature Stats

You have three **Signature Stats**, each set a different way:

- **Race** — one Stat, fixed by your race (its own entry below says which).
- **Profession** — one Stat, fixed by your profession (its own entry below).
- **Your choice** — one Stat you pick yourself at creation, any of the 8,
  reflecting your own personal focus rather than your race or profession's.

**When you roll a pool (Opposed Roll or Skill Check) tied to one of your
Signature Stats, you get bonus points equal to how many of your three
Signature Stats match that roll** — 1 point if only one of the three matches,
up to 3 if your Race, Profession, and personal choice all happen to land on
the same Stat. Apply your points *after* rolling, before counting Successes:
add them to the results of your own dice however you like — all onto one die,
or split across several. A die pushed to exactly 10 explodes as normal;
points that would push it past 10 are simply wasted (a die only explodes
once per roll, no matter how far over 10 your points would otherwise take
it). This is a smaller, more controllable nudge than a full extra die —
enough to matter on a near-miss without swinging the whole roll.

The reverse applies too: where a race's own text gives it a flat *penalty* to
a specific Stat (e.g. Xerm's -1 Intellect), subtract 1 from one of your dice
instead, on a roll using that Stat (minimum die value 1).

Where a race or profession's own text already grants a flat roll bonus to one
specific kind of check (e.g. Advent AI's "+1 bonus to hacking rolls,"
Charlatan's "±1 Credit spend for advantage on social rolls"), that bonus is
*this* mechanic — folded into its Signature Stat rather than built as a
separate ability, so nothing double-dips.

### Skill Checks

Not every roll is a fight against another character. For a roll against a
fixed obstacle instead — resist fear, pick a lock, recall a fact, hold your
footing, force a Willpower/Dexterity/whatever check named by an ability below
— roll a **Skill Check** rather than an Opposed Roll:

- **Base pool:** 1d10, for whichever Stat the check names.
- **Signature Stat bonus points**, per the rule above, if that Stat is one of
  your three.
- **+1d10 per PP/MP** spent Pushing Your Limits, as normal.
- The GM sets an opposing **difficulty pool** sized to the task, using N&D's
  own Difficulty Scale names for reference: Simple/Routine → 1d10,
  Challenging/Professional → 2d10, Heroic/Epic → 3d10, Legendary/Impossible →
  4d10+. Count Successes (6+) on each side — you succeed if you match or beat
  the difficulty pool. **Ties favor you, the acting character** (unlike combat
  ties — there's no damage to trade here, so a tie needs a clean outcome).

Anywhere else in this document that names a Stat and a target number (e.g. "a
Willpower check against a Heroic difficulty") is a Skill Check using this
rule — translate a bare N&D-style DC into the nearest difficulty-pool tier
above (Difficulty 8-10 ≈ Challenging/Professional/2d10, 12-15 ≈ Heroic/
Epic/3d10, 18+ ≈ Legendary/Impossible/4d10+) if you ever need to convert one
by hand.

### Combat Basics

- **Turn:** one Movement (30 ft standard) and one Main Action (Basic Attack /
  cast an Active ability, paying its PP or MP / Defend for +1d10 to your
  Defender pool until your next turn / a complex Interact or Skill Check / Dash
  / Grapple). One Reaction per round.
- **Opposed rolls:** Attacker and Defender roll their pools simultaneously and
  count Successes. Attacker > Defender: the attack hits, damage = the
  difference in Successes + the ability's base damage. Defender > Attacker: no
  damage. Tie: both take 1 damage, bypassing Armor/Resistance (there's no
  Spark Shield left to absorb it).
- **Damage mitigation:** a Resistance to the damage type removes 1 Attacker
  Success before damage is calculated; Armor subtracts flat from physical
  damage after that.
- **Ranges:** Melee 0-5 ft, Close 30 ft, Long 100 ft, Extreme 300+ ft
  (line of sight). Half Cover grants the Defender +1d10 against ranged attacks
  only; High Ground grants the Attacker +1d10.
- **Status Conditions:** Blinded (ranged auto-fails, -2 dice melee), Burning/
  Bleeding (1 damage at the start of your turn to Health or Shock as fits the
  source, bypassing Armor/Resistance), Restrained (0 Movement), Stunned (no
  Main Action/Movement, -1 Defender die), Weakened (-1 Attacker die),
  Vulnerable (Armor and Resistances drop to 0). Stacking multiple dice-losing
  conditions can't drop a pool below 1 die.

For deeper combat/GM material (grappling and throws, Divine Resonance ability
synergy, full bestiary stat blocks) see `asterion_rules.md`.

---

## Ability Construction

The tables below are guidelines, not strict restrictions — bend them if an
ability perfectly fits the narrative, or use the Trade-Off rule to balance a
narrative extreme.

### Active Abilities

Baseline: Base Damage 0, Melee/Touch Range, Single Target, Instant Duration, No
Special Effect. Build up from there:

- **Tier 1 (0 PP or MP):** choose 1 Tier 1 property.
- **Tier 2 (1 PP or MP):** choose 1 Tier 2 property and 1 Tier 1 property.
- **Tier 3 (3 PP or MP):** choose 1 Tier 3, 1 Tier 2, and 1 Tier 1 property.

| Property | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| **Base Damage** | 1 Damage | 2 Damage | 4 Damage |
| **Range** | 30 feet | 100 feet | 1 mile / no line of sight |
| **Area of Effect** | 5-ft radius | 15-ft radius | 60-ft radius |
| **Duration** | Up to 1 minute | Up to 10 minutes | Up to 1 hour / until dispelled |
| **Special Effect** | Minor narrative effect | Soft Crowd Control (blind 1 turn, slow, DoT) | Hard Crowd Control (paralyze, alter architecture) |
| **Restoration** | Restore 1 Health or Shock | Restore 2 Health/Shock OR 1 PP/MP | Restore 4 Health/Shock OR 2 PP/MP |

An ability that restores Health, Shock, PP, or MP must require an external
source, specific condition, or Trade-Off — no infinite self-healing in an empty
room.

### Passive Abilities

Passives cost nothing and are always active.

- **Tier 1:** choose 1 Tier 1 property. **Tier 2:** +1 Tier 2 property. **Tier
  3:** +1 Tier 3 property.

| Property | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| **Max Health / Max Shock** (pick one track) | +1 Max | +2 Max | +3 Max |
| **Armor** | *(none)* | +1 Armor | +2 Armor |
| **Resistance** | Mundane Immunity | Resistance (-1 Attacker Success) | Greater Resistance (-2 Attacker Successes) |
| **Movement** | Minor trait (swim/climb speed) | Special Movement (flight/teleport, walking speed) | Fast Movement (flight/teleport, double speed) |
| **Senses** | 1 Superhuman Sense | Advanced Sense (see through walls, detect auras) | Mythic Sense (read surface thoughts, truesight) |

Resistances of the same type don't stack — a second one becomes a single
Greater Resistance instead.

### Trade-Off Rule

To push an ability slightly outside its properties, take a Trade-Off:
**Self-Harm** (costs Health/Shock *and* PP/MP), **Collateral Damage** (hits
allies too), **Charge-Up** (a full turn of concentration, -1 Defender die while
charging), or **Specific Condition** (only works under strict circumstances).

An **Active ability usable only once per session** automatically counts as
having taken the Trade-Off needed to build a starting Tier 3 ability — this is
how Major Edges (below) get to be full Tier 3 for free.

---

## Character Creation

1. **Choose your Race** (17, tiered Standard/Advanced/Exceptional) — sets any
   racial resource adjustment, your Race Signature Stat, and your full Race
   Ability list (§ Race Abilities — one ability per *named* special mechanic
   the race has in N&D, not just one per race).
2. **Choose your Profession** (6) — sets your Profession Signature Stat and
   full Profession Ability list.
3. **Choose your third Signature Stat** — any of the 8, your own free pick
   (see "Signature Stats" above). Combined with your Race and Profession picks
   from steps 1-2, this is what determines which rolls get a bonus.
4. **Choose your Background** — one of N&D's 5 Origins, or invent your own
   (see § Background Abilities) — sets your Background Ability.
5. **Choose your Major Edge** — constrained to the Edge(s) tied to your Race or
   your Profession (see § Major Edge Abilities).
6. **Set your resources:** Health, Shock, PP, and MP all start at 5 — apply
   any race-specific adjustment noted in your race's entry.
7. **Write down your character sheet:** your full ability list from steps 1,
   2, 4, and 5 (the total varies — a race/profession with more named
   mechanics in N&D gives you more abilities, by design), your three
   Signature Stats, and your derived resources.

A naming note: Asterion's own Character Sentence uses "Origin (For Gods)" to
mean *how you became divine* — that's unrelated to N&D's "Origin" feats
(Ex-Corp, Infamous, etc.), which are a background/history flag. This document
always calls N&D's Origin-derived slot **Background** to avoid the collision.

---

## Ability Sources

### The Major Edge constraint

A character's Major Edge choice is constrained to the Edge(s) tied to their
chosen **Race** *or* their chosen **Profession** — exactly how N&D's own
character wizard already scopes Edge selection. **Consumed by Yellow** is the
one race with no race-Edge (by original N&D design — it "cannot get Edges"); it
takes its Major Edge from its Profession instead.

Every Major Edge in N&D is already written as usable "once per session" — so
Major Edges are built as full **Tier 3 Active abilities for free**, per the
Trade-Off rule above. A player may instead build their Major Edge as a
**Passive Curse** (permanent drawback, no per-session limit) if that fits the
character better.

---

### Race Abilities

**Methodology:** read the race's full N&D write-up — its `description` and
every named mechanic in its `sections` — and give it one ability per named
mechanic, built from the Active/Passive property tables above at whatever Tier
best fits. A mechanic that's already a multi-option menu in N&D's own text
(spend a charge, pick one of several effects) becomes one Active ability whose
Special Effect property *is* that menu — the same pattern this document
already uses for Major Edges. A mechanic that's structurally a tracked
gauge/ladder rather than a grantable ability (a resource that rises and falls
with its own threshold table) stays a tracked gauge here too, written up in
plain terms next to the race's Tier abilities rather than forced into them.

#### Standard tier

##### Below Elves/Drow — Signature Stat: Willpower

| Ability | Type & Tier | Effect |
|---|---|---|
| Umbral Sight | Passive T1 (Senses) | Full darkvision — see in total darkness as if it were day. |
| Dual Persona Protocol | Active T2 (Special Effect, 1 PP or MP) | Switch between Surface Mask and Shadow Self once per scene. While in Surface Mask: +1 bonus point on Deception/Bureaucracy/Social Engineering rolls; treated as non-hostile by patrols/drones; can bypass biometric security. While in Shadow Self: +1 bonus point on Stealth/Hacking/Explosives rolls; spend 1 PP to become Undetected for one action, or trigger a Saboteur Action (sabotage a system/implant a virus/disable a camera). |

##### Deviltouched — Signature Stat: Intellect

| Ability | Type & Tier | Effect |
|---|---|---|
| Cinderskin | Passive T1→2 (Resistance) | Mundane Immunity to fire (T1); upgrade to Resistance for N&D's full "half damage from all fire" (T2). |

*Tracked gauge — Law Pool (0-3):* gain +1 (max once/scene) whenever you
critically succeed on a Contract, Negotiation, or Legal roll. Spend it on
Favors Owed: **1** — a minor favor (a lawyer "loses" a file, a free reroll on a
Contract/Bluff check); **2** — a medium favor (restricted-zone access for a
scene, a debt collector redirected); **3** — a major favor (a contract voided
retroactively, a bounty "paused").

##### Dwarf — Signature Stat: Body

| Ability | Type & Tier | Effect |
|---|---|---|
| Miner's Eye | Passive T1 (Senses) | An uncanny knack for spotting hidden doors, caches, and mechanisms. |
| Spirit Reserve | Active T2 (Special Effect menu, 1 PP) | Spend 1 of 3 charges (refresh each session, or on a Rest if you drink) to trigger one: **Liquid Courage** (ignore Fear/Intimidation/Psychic Suppression 1 scene), **Iron Liver, Iron Body** (cleanse a toxin/resist pain 1 hour), **Drunken Precision** (reroll a failed Crafting/Engineering/Jury-Rig check with +1 bonus point), **Flaming Belch** (short-range fire/corrosive cone, counts as a basic ranged attack), **Brewed Bravery** (+1 bonus point on any roll for 1 minute, then a -1 penalty point on Intuition rolls until your next Rest). |

##### Fae — Signature Stat: Intuition

| Ability | Type & Tier | Effect |
|---|---|---|
| Fluxform Physiology | Passive T1 (Movement) | Minor trait — unnaturally flexible body; squeeze through tight gaps, subtly reshape your features during a Rest. |
| Fluxform | Active T3 (once per session, 3 PP or MP) | Spend your session's Fluxform Point to shift into your true form for a scene. Choose **Physical Shift** (Special Effect: Hard CC fear pulse on sight — non-allies make a Willpower Skill Check against a Heroic (3d10) difficulty pool or are Intimidated/flee; Passive for the scene: immune to grapples, squeeze through tight spaces; your attacks count as tentacle strikes that may restrain or disarm, GM's discretion) or **Mental Shift** (Passive for the scene: Greater Resistance to mental effects; Special Effect: once per shift, ask the GM one question about the immediate past or near future, and once per shift automatically confuse or disorient one NPC in a conversation). |

##### High Elves/Elves — Signature Stat: Charisma

| Ability | Type & Tier | Effect |
|---|---|---|
| Aura Sight | Active T1 (Special Effect, 0 MP) | Read a person's emotional/social aura for insight into their mood or intent. |
| Social Stance | Active T1 (Special Effect, 0 MP) | Declare a Stance at the start of a social scene — Charming, Assertive, Deceptive, or Observant. Gain +1d10 on rolls matching that stance's strength, -1d10 on rolls matching its weakness, until you change it (once per scene). |

##### Human — Signature Stat: a second free pick, any Stat (reflecting Humanity's versatility — stacks with your universal "your choice" Signature Stat from Character Creation step 3, so a Human gets two open picks)

| Ability | Type & Tier | Effect |
|---|---|---|
| Versatile | Structural | You gain one additional Tier 1 ability at creation, freely invented — not tied to your Race, Profession, Background, or Major Edge. |
| Adapt to Win | Active T1 (Special Effect, 0 MP) | Once per Rest or Session, temporarily borrow another profession's Tier 1 Profession Ability for the scene. |

##### Low Elves/Orks — Signature Stat: Dexterity

| Ability | Type & Tier | Effect |
|---|---|---|
| Keen Scent | Passive T1 (Senses) | Exceptional sense of smell — track by scent, detect what mundane noses can't. |
| Memory Marks | Passive T1 (narrative trait) | You start with 1 Memory Mark — a permanent trait from a significant ancestral figure (define it with the GM). You may earn more by Ritual Consumption: eating the flesh of an intelligent creature dead less than a day, gaining a fragment of their memory or a temporary trait (GM discretion). |

#### Advanced tier

##### Advent AI — Signature Stat: Intellect

*Override — Advent AI doesn't use the standard Resources rules:* no Health or
PP at baseline (no Physical Stats to speak of at all). Shock starts at 10
instead of 5 (+5 bonus); at 0 Shock, you are **erased**, not just
unconscious.

| Ability | Type & Tier | Effect |
|---|---|---|
| Network Sense | Passive T1 (Senses) | Perceive and interface with nearby electronic systems — a built-in scanner for tech and networks. |
| Husk/Robot Piloting | Active T1 (Special Effect, 0 MP) | Upload into a Husk (a purpose-built robot body with its own Armor/Resistance/Movement traits and its own PP and Health pool, set by the GM, that you cannot upgrade) or an ordinary robot (5 PP, 7 Health, no special traits) to act in the physical world. If the vessel has anti-virus defenses, defeat or disable them first. If your vessel is destroyed, take 1d4 Shock damage. |

##### Banshee — Signature Stat: a free pick, any Stat (Ectoplasm draws on all of them equally, so nothing is fixed — you decide which one your chassis favors)

*Override — a Banshee doesn't use the standard Resources rules:* no separate
Health, Shock, PP, or MP. Instead you have a single **Ectoplasm** pool
starting at 20 (the combined total of what everyone else's four pools add up
to); anything that would change Health, Shock, PP, or MP changes Ectoplasm
instead. You heal only by repair, never by Rest. You have no citizen rights.
You take +4 damage from electric sources. You take a -1 penalty point on
social rolls (a permanent narrative drawback, already baked in — no
Trade-Off needed). You can't use Bio Augments; you're treated as already
having all Alpha Augments.

| Ability | Type & Tier | Effect |
|---|---|---|
| Iron Chassis | Passive T3 (Armor) | +2 Armor, representing your robotic shell — worn armor items grant no further benefit on top of this. |
| Inorganic Body | Passive T1 (Resistance) | Mundane Immunity — toxins and poison have no effect on you. |
| Ghost Form | Active T1 (Special Effect, 0 MP) | Leave your automaton as an intangible ghost (can't interact with the physical world). A psyonic can banish you while in this form. |

##### Child of the Black Goat — Signature Stat: Willpower

| Ability | Type & Tier | Effect |
|---|---|---|
| Voice of the Wild | Active T1 (Special Effect, 0 MP) | Telepathically communicate with nearby animals, who default to neutral or friendly toward you. |
| Ritual Casting | Active T2 (Special Effect, Charge-Up Trade-Off — a full scene to perform, 1 PP or MP) | Perform a powerful non-combat Psy ritual (no damage/debuff effects). |

*Flavor notes:* +1 bonus point on Nature-related rolls and non-combat Psy
Power rolls (damage/debuff Psy Powers don't qualify); -2 penalty points on
Skill Checks to orient in an urban environment. Implanting metal augments
exiles you from Ritual Casting; Bio Augments need a priest/priestess's
blessing.

##### Crimson Elves/Amalgama — Signature Stat: your currently chosen Physical Stat, swappable after each Rest (unlike the universal "your choice" Signature Stat, which is fixed at creation)

*Override — Flesh Grafts are this race's ability budget:* instead of the usual
Race Ability list, you get **1 Tier 2 and 2 Tier 1 abilities**, built freely
from either property table (Active or Passive) and always permanently
visible — you cannot hide a Flesh Graft under clothing or otherwise.

| Ability | Type & Tier | Effect |
|---|---|---|
| Sanguine Metabolism | Passive T1 (Resistance) | Mundane Immunity — stims, drugs, and mundane toxins have no effect on you. |
| Malleable Flesh | Free (no Tier cost, narrative only) | Change your skin color, height, weight, eye color, etc.; takes 1 minute. |

*Override:* choose 1 Physical Stat at creation as your favored one —
swappable after each Rest, and it sets your Signature Stat. You sustain
yourself on blood; stims/drugs don't affect you. Go 48 hours
without feeding and you take 1 Health damage every hour, unable to heal Health
until you feed — a built-in drawback, no formal Trade-Off needed.

##### Devilspawn — Signature Stat: Intuition

*Override:* -2 to your Max Health (scaled down from N&D's own -5, which
assumed a much larger Health pool than this rulebook's flat starting 5).

| Ability | Type & Tier | Effect |
|---|---|---|
| Third Eye | Active T1 (Special Effect, 0 MP) | Open your third eye as an action. While open, roll your Domain Pool (3d10 instead of 2d10) on Psy-flavored rolls — but risk GM Intrusion: the GM may declare you briefly possessed (+1d10 to every pool you roll, but you act under a dangerous compulsion for 1 minute), usually on a critical failure. |

##### Xerm — Signature Stat: Strength (and a -1 penalty point on Intellect rolls)

*Override:* a -1 penalty point on Intellect rolls (noted above; Strength is
already your Signature Stat, so N&D's own "+1 Strength" is covered there
too). You can't use Psy Powers. You can use Xerm (Heavy) weapons without
penalty. Augments cost +20%; you gain +2 Feat Slots for arm augmentation. Your
stim limit is 1 per Rest (not the usual 3).

| Ability | Type & Tier | Effect |
|---|---|---|
| Psy-Null Physiology | Passive T1 (Resistance) | Greater Resistance to mental/Psy-type intrusion — half damage (rounded down) from Psy Powers. |

*Tracked gauge — Heart Pressure (HPres):* starts at 3; **7+ = instant death**
(your Auxiliary Core ruptures). Gain +1 HPres when you take 5+ damage in one
hit, kill an enemy, perform a physical feat beyond human limits, or push a
failed Strength/Body roll. Lose 1 HPres from an uninterrupted hour of rest, a
Biolumina Serum Injection, or succeeding a Willpower Skill Check against a
Heroic (3d10) difficulty pool after combat.

| HPres | State | Effect |
|---|---|---|
| 0-1 | Stable | +1 bonus point to resist Shock damage. |
| 2-3 | Charged | +1 bonus point on Strength/Body rolls; use all four arms without penalty; extra interaction/reload each turn. |
| 4-5 | Overdrive | +2 bonus points on Strength rolls, +1 on Dexterity rolls (split/stacked like Signature Stat points), +10 ft movement; extra Action each turn; lose 1 Health at the end of your turn. |
| 6 | Critical Limit | +3 bonus points on any Physical Stat roll (Strength/Dexterity/Body/Perception), your choice how to split them; act first this round. At end of turn, roll 1d6: 4+ you survive (stay at 6), else you die. |
| 7+ | Rupture | Death — 2d10 damage to anyone within short range, ignoring Armor. |

#### Exceptional tier

##### Consumed by Yellow — Signature Stat: the Signature Stat of whichever race you draw your Race Abilities from (see below)

*Override:* no Shock, and you cannot become unconscious from it. You don't
need to sleep; restore 1 PP and 1 MP every hour instead. Take 1 Max Health
damage for each day you don't consume a sentient being (scaled down from
N&D's own -5/day, which assumed a much larger Health pool than this
rulebook's flat starting 5); at 0 Max Health you become an Avatar of the
Yellow. You take Race Abilities from one other race of
your choice, but never that race's Tier-3-equivalent ability, tracked-gauge
subsystem, or Major Edge — and no race gets a race-Major-Edge here either; take
your Major Edge from your Profession instead.

| Ability | Type & Tier | Effect |
|---|---|---|
| Hollow Vessel | Passive T1 (Max Health) | +1 Max Health, representing the Yellow's corrupted protection. |
| Power of the Yellow | Active (Tier grows as you spend Glory — every 5 Glory spent invents or upgrades one) | A Special Effect ability paid for by temporarily downgrading one of your own Stats (a built-in Self-Harm Trade-Off) instead of PP/MP. Downgraded Stats return to normal after you consume a sentient being. |

*Tracked gauge — Yellow Saturation (0-10):* rises when you use Power of the
Yellow (+1), go a day without consuming (+1), or voluntarily accept the
Yellow's presence (+1, once/session); falls when you consume a sentient being
(-2) or permanently burn 1 point from a Max resource of your choice —
Health, Shock, PP, or MP — which drops Saturation by 5. This is a much
harsher trade than N&D's own "burn a Stat point" (a bigger pool to begin
with), which fits it being the most drastic of the three options.

| Saturation | Name | Effect |
|---|---|---|
| 0-1 | Awakened Host | No bonus. |
| 2-3 | Dripping Core | +1 PP/MP regen per hour; +1 bonus point on Willpower rolls. |
| 4-5 | Yellow-Gifted | Power of the Yellow costs -1 Stat downgrade (min 1). |
| 6-7 | Walking Blight | +1 Action/turn; immune to Fear; -1 penalty point on others' rolls to resist your mental effects. |
| 8-9 | Crowned With Madness | Enemies at long range take a -1 penalty point on their rolls; allies get a +1 bonus point. |
| 10 | Becoming the Yellow | Power of the Yellow costs nothing; +2 bonus points on every roll you make, your choice how to split them; risk of losing control (roll 1d6 each turn: 1-2 in control, 3-4 GM narrates hallucinated motives, 5 GM controls you for 1 turn, 6 you become the Avatar permanently). |

##### Dragonblooded — Signature Stat: one of your chosen bloodline's two Stat Synergy stats (below), your pick

*Override:* you can augment yourself, but with half the
usual feat augment slots (rounded down). Hiding your dragonblooded form takes
10 minutes; while hidden, +1 Armor and no bloodline abilities. In true form:
+2 Armor, unarmed attacks deal 1d6+3 ignoring 1 Armor, bloodline abilities
usable, and you have +5 Armor against extreme temperatures (stacks with normal
Armor). Once per session, your creator may try to compel you: roll d10, 5+ you
resist, else you act under their command for 1 minute.

Choose one Dragon Bloodline — its three abilities are your Race Abilities.
**Blood Surge costs are kept exactly as N&D wrote them** even where a Surge
packs more than one Tier's worth of properties, per Asterion's own "bend these
rules if an ability perfectly fits the narrative" allowance.

| Bloodline | Stat Synergy | Passive | Blood Surge (Active) | Unique Trait |
|---|---|---|---|---|
| **Infernis** (Fire) | Strength + Intellect | Thermal Conduction: immune to fire; melee attackers vs you take 1 Shock. | 2 PP: 1d8 heat damage, short-radius burst, ignores 2 Armor; +2 bonus points on melee rolls for 1 round. | Regenerate 1 Health when you deal fire damage (once/turn). |
| **Glaciar** (Frost) | Willpower + Dexterity | Cryostasis Veins: half damage from cold/EMP; melee attackers must make a Dexterity Skill Check against a Professional (2d10) difficulty pool or be slowed (half movement speed until their turn ends). | 2 PP: freeze target 1 turn (no damage), or 1d8 cold damage if already slowed/wet. | Regain 1 Shock whenever you spend PP on a Blood Surge. |
| **Voltaris** (Storm) | Dexterity + Intuition | Living Conductor: immune to electricity; +1 Shock when hit by electric/EMP (once/turn). | 1 PP: teleport up to short range as a Reaction when attacked. | Successfully dodging grants your next attack +2 damage. |
| **Umbracline** (Shadow) | Dexterity + Intellect | Shadowmeld: +1 bonus point on Stealth rolls and +1 Armor in dim light or darker. | 2 PP: phase through solid objects/shadows (up to 3m) for one turn; attacks from inside shadows ignore Armor. | Killing a target in darkness restores 1 PP and 1 Shock. |
| **Nekrith** (Decay) | Body + Willpower | Entropic Aura: living enemies in medium range take -1 to healing/regen; immune to poison/disease. | 2 PP: touch attack, 1d4 damage, reduces target's Max Health by that amount until healed (once/target). | Regain 1 PP and 1 Health whenever a creature dies near you (medium range). |
| **Psionis** (Mind) | Intellect + Charisma | Neural Field: enemies in short range take a -1 penalty point on Mental Stat rolls against you. | 2 MP: dominate one humanoid for 1 round (they make a Willpower Skill Check against a Heroic (3d10) difficulty pool to resist). | Regain 1 MP whenever you force a target to fail a Willpower roll. |
| **Crimson** (Blood) | Body + Strength | Burning Blood: below 50% Health, +1 bonus point on Strength rolls and +1 Armor. | 2 PP + 2 Health (Self-Harm Trade-Off): +3 bonus points on attack rolls for 2 turns; damage taken reduced by 1. | Kill an enemy in melee, roll d10 — 7+ regain 2 Health. |

##### Eldritch — Signature Stat: a second free pick, any Stat (reflecting your alien nature — stacks with your universal "your choice" Signature Stat from Character Creation step 3, so an Eldritch gets two open picks)

*Override:* -3 penalty points on empathy/social rolls (N&D's own text says
-3, notably harsher than the usual -1/-2 racial penalty — kept at that
severity rather than flattened to a generic -1d10). Can't use augments. Inventing a new
Transcendence ability costs +1 Glory more than normal (Race Feats cost more in
N&D's own text).

| Ability | Type & Tier | Effect |
|---|---|---|
| Void-Touched Physiology | Passive T1 (Resistance) | Mundane Immunity — no need to breathe, eat, drink, or sleep; survive unharmed in vacuum or extreme environments. |
| True Form | Active T1 (Special Effect, 0 PP or MP) | Switch between your mundane alter ego and your true form (an Action). If a non-Eldritch witness sees you shift into true form, they make a Willpower Skill Check against a Professional (2d10) difficulty pool — on failure they panic (lose 1 Action and 1 Reaction for the round), or on a total failure (0 Successes rolled) they flee for the round instead. While in true form, everyone treats you as hostile. |
| Transcendence | 1 Tier 2 + 2 Tier 1 abilities (built freely, usable only in true form) | This race's ability budget — mirrors Crimson Elves' Flesh Grafts. |

##### Stitches — Signature Stat: Body

*Override:* you start with one limb or organ already broken (roll on the
Limbs and Organs table) and choose two mental conditions (PTSD, ADHD,
Paranoia, etc.) at creation. You take +1d4+1 extra Shock damage from all
Shock sources; Shock can go to -10 (regain 1 per 10 minutes, only while
below 0). At 0 Shock or below, roll on the Mental Breakdown table. You
can't use Augments or Bio Augments (some abilities may let you graft parts
from bodies instead).

| Ability | Type & Tier | Effect |
|---|---|---|
| Undying Patchwork | Active T1 (Restoration, 0 PP) | Restore 1 Health, usable only the instant you would otherwise be dying. |
| Grafted Resilience | Passive T1 (Restoration-flavored) | Health can go below 0, down to -10, without killing you — regain 1 Health at the start of each of your turns while below 0. Roll on the Limbs and Organs table for which part is destroyed. |

| Limbs and Organs (d10) | | Mental Breakdown (d6) | |
|---|---|---|---|
| 1 | Right eye | 1-2 | Minor |
| 2 | Left eye | 3-4 | Medium |
| 3 | Right hand | 5-6 | Heavy |
| 4 | Left hand | | |
| 5 | Right leg | | |
| 6 | Left leg | | |
| 7 | Lungs | | |
| 8 | Heart | | |
| 9 | Brain | | |
| 10 | Head | | |

---

### Profession Abilities

**Methodology:** same approach as races — every named mechanic in a
profession's write-up becomes its own ability or tracked gauge, not just its
headline resource.

##### Charlatan — Signature Stat: Charisma

*Tracked gauge — Credit (₡):* refreshes each session to 4 (+2 if Charisma
is one of your Signature Stats), max 10. Replaces N&D's own "Charisma −
1d6" formula, which needed a numeric Charisma this rulebook doesn't have.

| Ability | Type & Tier | Effect |
|---|---|---|
| The Price of Anything | Active (Special Effect menu, gated by Credit, not PP/MP) | Spend Credit for: **2₡ Flash of Wealth** (1 round of hesitation, or -2 to enemies' Initiative in a social encounter), **3₡ Buy Your Way In** (declare a connection/favor/backdoor for narrative control over one obstacle, GM decides limits), or **4₡ Contract of Convenience** (once/session — forge a binding contract; you define the terms, the GM defines the twist). |

*Note:* ₡1 Grease Palms (N&D's own "advantage on a social roll") is already
covered by your Charisma Signature Stat bonus points above — no separate
spend needed.

##### Cyberdoc — Signature Stat: Intellect

*Tracked gauge — Nanite Charges:* start with 2, regain 1 per session.

| Ability | Type & Tier | Effect |
|---|---|---|
| Nanite Swarm | Active (Special Effect menu, 1 Nanite Charge per use, not PP/MP) | **Micro-Repair Cloud** (restore 2 Health to an ally at touch, or remove a minor injury/status), **Aggro-Leech Protocol** (target an enemy's augments: 1d4 damage, they make a Body Skill Check or one cybernetic system is disabled for a round), **Synaptic Boost** (an ally gains +1 Action this turn or +2 Initiative next round), or **Auto-Surgeon** (repair or field-install a basic augment instantly; 1-in-6 malfunction risk). |

##### Hacker — Signature Stat: Intellect

*Tracked gauge — Electronic Buddy Uses:* 1 function per scene, max 2 per Rest.

| Ability | Type & Tier | Effect |
|---|---|---|
| Electronic Buddy | Active (Special Effect menu, 0 MP, gated by Electronic Buddy Uses) | **Data Spider** (ask the GM one question about a target/system/location, get a usable lead), **Handshake Override** (+1d10 to a Hacking roll, or reduce Alert Level by 1 after a breach), **Signal Sniffer** (detect active signals — drones, smart-guns, comms, augment usage — in the environment), or **Auto-Chatter** (+2 bonus points on a Bluff/Deceive/Distract attempt by impersonating a call, message, or alert). |

*Alert Level* is a GM-tracked meter (starts at 0) that rises 1 per failed
hacking roll during an intrusion; the GM escalates consequences as it
climbs — patrols get alerted, countermeasures activate, backup arrives.
Mirrors N&D's own "Net Awareness."

*Flavor notes:* your Buddy has an optional quirk (roll 1d4 or choose one —
cat-obsessed, paranoid, cheerfully morbid, outdated personality). Once per
session, a critical failure while hacking makes your Buddy misfire — the GM
picks one consequence (early alarm, fake alert delivered to the target, or the
Buddy is unusable for the rest of the scene).

##### Merc — Signature Stat: Dexterity

*Tracked gauge — Bullet Time:* usable once per session.

| Ability | Type & Tier | Effect |
|---|---|---|
| Snapshot | Active T1 (Base Damage, 0 PP) | 1 Damage, melee/touch range — a fast, professional shot. |
| Bullet Time | Active T3 (once per session — auto-Trade-Off, no PP/MP cost) | Lasts 1 round. Choose 2 of: **Double Tap** (two full actions), **Focus Fire** (+2 to hit, ignore cover), **Interrupt Fire** (interrupt an enemy's action once to fire or move), **Tactical Surge** (double movement, ignore difficult terrain), **Ricochet Read** (+1 Defense, can't be flanked or surprised). |

##### Psyonic — Signature Stat: Willpower

*Tracked gauge — Network Access:* enter the Psyonic Network once per Rest,
stay up to 10 in-game minutes.

| Ability | Type & Tier | Effect |
|---|---|---|
| Call to Minds | Active T1 (Special Effect, 0 MP) | Briefly reach another psyonic you know mentally to send a short message or image. |
| Psyonic Network | Active T2 (Special Effect menu, 1 MP, gated by Network Access) | While inside: **Locate a Thought** (a Skill Check using your Domain Pool, 3d10, since it's tied to your Psyonic ability — against a Professional-to-Heroic (2d10-3d10) difficulty pool, scaling with how deep or specific the thought is, for a lead on specific knowledge), or **Download Emotion** — **Rage** (+2 damage on your next psychic attack), **Clarity** (auto-succeed your next psychic control check), or **Despair** (target rolls -1d10 on social rolls for 1 scene). |

*Flavor note:* roll 1d6 on entry for a complication — 1 Mind Leech (Willpower
save or 1 Shock), 2 Echo Trace (a hostile psyker can track you later), 3
Corrupted Terrain (comms jumbled), 4 Mental Predator (3 turns to finish your
task before it arrives), 5 nothing, 6 a rogue psion offers guidance or a
bargain.

##### Street Fighter — Signature Stat: Strength

| Ability | Type & Tier | Effect |
|---|---|---|
| Trained Strike | Active T1 (Base Damage, 0 PP) | 1 Damage, melee — a precise unarmed or improvised strike. |

*Tracked gauge — Street Cred (0-100):* declare a target as your Adversary
(Action); gain +1 Street Cred for killing them, +2 for disabling them without
killing.

| Cred | Passive Bonus |
|---|---|
| 10 | +1 bonus point on Agility-based defense rolls. |
| 20 | +1 Armor against the first hit each round. |
| 30 | Always act first in combat (unless Surprised); +1 bonus point on Initiative. |
| 40 | Detect hidden enemies/surveillance in your Zone without a roll, once/scene. |
| 50 | Once per Rest, auto-succeed a roll to resist a Mental Condition. |
| 60 | Ignore movement penalties from difficult/cluttered terrain. |
| 70 | Enemies must beat your Charisma Skill Check with their own Intuition Skill Check to target you with mental/fear effects. |
| 80 | Unarmed strikes deal +1d4 vs enemies with lower Street Cred/Reputation. |
| 90 | A free Movement once per round that doesn't provoke Opportunity Attacks. |
| 100 | Immune to Fear; allies in your Zone gain a +1 bonus point on all rolls while you're conscious. |

---

### Background Abilities

**Methodology:** N&D's Origins are life-history flags, not combat traits —
most translate best into a Tier 1 social/utility Active, or a minor Passive
where the Origin describes a physical condition (Infected, Psycho).

**Players are free to invent their own Background instead of picking one of
N&D's five** — unlike Race and Profession, which stay fixed to N&D's catalog,
Background is the one slot where a homebrew concept is explicitly welcome (a
lost memory, a cult upbringing, a survived disaster — whatever life-history
flag fits the character). State it in one sentence, then build one Tier 1
ability (Active or Passive) that reflects it, the same way each of the five
listed Origins was converted below.

| Origin | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Ex-Corp** | Corporate Rolodex | Active T1 (Special Effect, 0 MP) | Call in a small corporate favor or contact once per scene (a name, a badge, a door that opens). |
| **Identity unknown** | Augmented Vigilance | Passive T1 (Senses) | A minor augment-granted sense — heightened alertness; you tend to notice when you're being watched or followed. |
| **Infamous** | Reputation Precedes You | Active T1 (Special Effect, 0 MP) | Your bounty-fueled infamy alone cows or impresses a minor NPC once per scene, no roll needed. |
| **Infected** | Undying Infection | Passive T1 (Max Health) | +1 Max Health — the necrotic virus keeps you standing. |
| **Psycho** | Chrome Fugue | Passive T1 (Resistance) | Mundane Immunity to Fear/Intimidation while your cyberpsychosis runs hot. Keep N&D's own Noise-dial drawback as-is — the GM may occasionally seize control during a spike, the narrative cost of a Passive Curse, without a formal Trade-Off. |

---

### Major Edge Abilities

**Methodology:** every Major Edge already lists 3-4 concrete narrative-effect
options in its N&D text — pick one as the ability's core **Special Effect**
(usually Tier 3: Hard Crowd Control / major narrative shift), paired with a
Duration (typically Tier 2: up to 10 minutes, or scene-long) and a Range
(typically Tier 1: 30 ft, or self/touch for transformation-style Edges). All
are **Active, once per session** (3 PP or 3 MP, whichever matches the Edge's
physical or mental fiction) — the GM adjudicates which narrative option
applies each time it's used. The other options an Edge lists remain available
as GM-adjudicated flavor for the same ability, not separate abilities.

**Race-tied (16):**

| Race | Major Edge | Core Effect (from N&D text) |
|---|---|---|
| Advent AI | I Am the System | Total, temporary control over nearby technology, perception, or information flow. |
| Banshee | Wail of the Forgotten Circuit | A scream that causes mass panic, possesses an unsecured machine, or forces a haunting vision. |
| Child of the Black Goat | All-Mother’s Embrace | Nearby animals/plants actively aid you, or you take on a beast aspect. |
| Crimson Elves/Amalgama | Divine Flesh, Profane Will | Reshape your own body (or someone else's) into whatever the moment demands. |
| Devilspawn | Whispers of the Antagonist | Partial possession grants perfect prediction, forbidden knowledge, or an infectious suggestion. |
| Deviltouched | Signed in Suffering | Reveal or force a binding legal/contractual twist that favors you. |
| Dragonblooded | Power of the Bloodline | A bloodline-specific reality-bending effect (see the seven bloodline write-ups above). |
| Below Elves/Drow | Veil of the Unseen War | Retroactively reveal you'd already infiltrated, sabotaged, or escaped the scene. |
| Dwarf | Stonebound Resolve | You become the immovable object — hold a collapsing structure, silence a mob, resist any toxin or mind-effect. |
| Eldritch | Reality Is a Suggestion | Bend space, causality, or physics briefly around yourself. |
| High Elves/Elves | The Moment That Bends | Retroactively "already made the smarter choice," or dominate a scene's social tempo. |
| Fae | Truthless Form | Manifest an impossible alien body part or ability for the scene. |
| Human | We Rise Together | Rally allies from certain collapse, improvise a brilliant solution, or ignite a rebellion. |
| Low Elves/Orks | Feast of Echoes | Consume a fallen creature's flesh to inherit a flash of their memory or skill. |
| Stitches | Patchwork Persistence | Graft on a new limb mid-fight, ignore pain entirely, or reassemble into something monstrous. |
| Xerm | Override the Limit | Your auxiliary heart overdrives — impossible feats of strength, speed, or protection. |

*Consumed by Yellow has no race-Edge (by N&D design) — take your Major Edge
from your Profession instead.*

**Profession-tied (6):**

| Profession | Major Edge | Core Effect (from N&D text) |
|---|---|---|
| Charlatan | The Perfect Lie | Instantly sell a total fabrication — a false identity, a bluff, a fake threat — and it holds. |
| Cyberdoc | Field Resurrection | Impossible field medicine — pull an ally back from death, or overclock their body/cyberware. |
| Hacker | Root Access Granted | Total, temporary control over a digital system, network, or set of devices. |
| Merc | Warzone Instinct | Perfect tactical execution — a killzone, a precision demolition, or rallying allies mid-firefight. |
| Psyonic | Unshackled Mind | Full-power telekinesis, pyrokinesis, biomancy, or a psychic curse. |
| Street Fighter | Stillness in Motion | A single flawless, decisive martial feat — impossible movement, a fight-ending strike, or total imperviousness for the scene. |

---

## Worked Example: a Drow Hacker

**Resources:** Health 5, Shock 5, PP 5, MP 5 — the flat starting values;
Drow carries no resource adjustment.

**Signature Stats:** Willpower (Race: Drow), Intellect (Profession: Hacker),
Intellect (your own choice — picked to double down on hacking). Two of the
three land on Intellect, so Intellect rolls get 2 bonus points to split or
stack across dice; Willpower rolls get 1.

> "I am a **Drow** **Hacker**, marked by my being **Infamous**, known for my
> **Veil of the Unseen War**."

- **Race — Umbral Sight** (Passive T1, Senses): full darkvision.
- **Race — Dual Persona Protocol** (Active T2, Special Effect, 1 MP): switch
  Surface Mask/Shadow Self once per scene.
- **Profession — Electronic Buddy** (Active, Special Effect menu, 0 MP, 1 use
  this scene): Data Spider digs up a usable lead about a target once per
  scene.
- **Background — Reputation Precedes You** (Active T1, Special Effect, 0 MP):
  bounty-fueled infamy cows a minor NPC once per scene.
- **Major Edge — Veil of the Unseen War** (Active T3, once per session, 3 PP):
  Special Effect (Hard CC: retroactively reveal you'd already
  sabotaged/infiltrated/escaped the scene), Duration (up to 10 minutes), Range
  (100 feet).

Five abilities total (two from Race, since Drow has two named mechanics; one
each from Profession, Background, and Major Edge) — this is the "uneven
ability count" the full conversion produces by design.

---

## Progression and Glory

Characters gain Glory at the GM's discretion (surviving and advancing the
plot, a public feat witnessed by others, defeating a major rival). Glory is
this rulebook's advancement currency:

- **3 Glory:** upgrade an existing ability with a new property from its
  permitted Tiers. If this gives it more than one property from its highest
  Tier, its PP/MP cost permanently increases by +1 per extra highest-tier
  property.
- **4 / 7 / 10 Glory:** invent a brand-new Tier 1 / Tier 2 / Tier 3 ability.
- **1 Glory (scaling +1 per purchase):** increase Max Health, Max Shock, Max
  PP, or Max MP by +1.

Health/Shock/PP/MP restore on a Short Rest (half PP and MP, all Shock, per
N&D's own Rest rule) and fully on a Long Rest; Health from medical treatment
restores after a Rest.

---

## Quick Reference / FAQ

**Why do some races/professions have more starting abilities than others?**
Because this rulebook converts *every* named special mechanic a race or
profession has in Neon & Dragons, not just one headline trait — a race with
one paragraph of fiction gets one ability, a race with a whole named subsystem
(Dwarf's Spirit Reserve, Dragonblooded's bloodlines) gets one ability per named
piece of it. Nothing from the source material is left out.

**Why Health/Shock/PP/MP instead of vanilla Asterion's Flesh/Ichor/Spark
Shield?** Because Neon & Dragons already has its own resource layer with that
exact name and split — reusing it keeps a character's sheet feeling like N&D,
not like a reskinned copy of Asterion. The Tier math works exactly the same
(same 5-point starting values, same 0/1/3 costs), just paid from four pools
instead of two, with no shield layer absorbing damage first.

**Do I have to pick one of the five listed Origins for my Background?** No —
they're ready-to-use options, but you're free to write your own and build a
Tier 1 ability for it using the same methodology. Race and Profession stay
fixed to N&D's catalog; Background is the one slot meant for a homebrew
concept.

**My race and my profession both have a Major Edge tied to them — can I pick
either?** Yes. Pick whichever fits your character concept; you only get one
Major Edge at creation.

**I'm playing Consumed by Yellow — do I ever get a race-Edge?** No, by
original N&D design. Take your Major Edge from your chosen Profession's list
instead.

**Can I build my Background or Race ability as a Passive Curse instead of a
normal Tier 1 pick?** Not by default — only the Major Edge slot uses the
Deed/Curse framing. If a specific Background or Race clearly wants a permanent
drawback instead (Psycho's Chrome Fugue is a good candidate), the GM can allow
it as a reasonable exception.

**Do these abilities grow later, or am I locked in forever?** They grow via
Glory (see "Progression and Glory" above) — upgrade an existing ability, or
invent brand-new ones entirely unrelated to your original Race/Profession/
Background/Edge. Your starting ability list is just where a Neon & Dragons
character begins, not a permanent ceiling.

**What about Minor Edges?** N&D's Minor Edge is already pure freeform
narrative perk chosen with the GM, not catalog-backed — it needs no
conversion. Keep using it as-is.

**Where do I find the full bestiary, crafting, Domain Reclamation, or
Reputation/Followers rules?** Those are GM-facing systems this rulebook
doesn't duplicate — see `asterion_rules.md` for all of them.
