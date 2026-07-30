# Neon & Dragons — Asterion Conversion

This document converts **Neon & Dragons** (N&D) character creation onto the
**Asterion** engine (see `asterion_rules.md` for the full base rules — dice pools,
combat, Glory, Domain Reclamation, items and crafting, enemies, Reputation,
Followers). Most of that is unmodified. This document covers what's different: how
a Neon & Dragons character is built, **and** the resource layer that character
runs on.

**What stays the same as N&D:** the actual catalog — all 17 races, 6 professions, 5
origins, and 22 major edges, by name and fiction, exactly as they exist in Neon &
Dragons today.

**What changes (abilities):** instead of granting a fixed mechanical bonus, each
pick becomes a *seed* for a player-invented Asterion ability, built from Asterion's
own Tier 1/2/3 property tables (see `asterion_rules.md` → "Ability Construction").
This keeps N&D's identity and flavor completely intact while replacing its fixed
bonuses with Asterion's simpler, more flexible engine.

**What changes (resources):** this conversion also replaces Asterion's Flesh /
Ichor / Spark Shield with N&D's own **Health**, **Shock**, **PP**, and **MP** — see
"Resource System" below. A converted character's resource sheet reads like Neon &
Dragons throughout, not just in its race/profession/background/edge names.

---

## The Conversion Mapping

| N&D source | Asterion slot | Ability Tier | Tendency |
|---|---|---|---|
| **Race** (17, tiered Standard/Advanced/Exceptional) | Origin/Lineage-equivalent | Tier 1 | Mostly Passive — Armor, Resistance, Movement, Senses, Max Health/Max Shock |
| **Profession** (6) | Spark-equivalent | Tier 1 | Mostly Active — Damage, Range, Special Effect |
| **Origin** (5 built-in options, or a player-invented Background) | new 4th slot: **Background** | Tier 1 | Either — usually social/utility |
| **Major Edge** (22) | Deed/Curse-equivalent | Tier 3 | Active, once per session (see below) |

A naming note: Asterion's own Character Sentence already uses "Origin (For Gods)" to
mean *how you became divine*. N&D's "Origin" feats (Ex-Corp, Infamous, etc.) are an
unrelated concept — a background/history flag, not a divinity story. To avoid
confusing the two, this document always calls N&D's Origin-derived ability slot
**Background**, reserving "Origin" for Asterion's own term where it appears.

### Why Major Edges are Tier 3, not Tier 2

`asterion_rules.md`'s own Deed/Curse rule already covers this: *"Active Deed (Once
Per Session): If built as an Active ability, it can only be used once per session.
This severe restriction automatically counts as the Trade-Off needed to build a
starting Tier 3 ability."* Every single Major Edge in N&D is already written as
"once per session" in its own text — so Major Edges get to be full Tier 3 abilities
for free, exactly like a vanilla Asterion Deed. If a player would rather build their
Major Edge as a **Passive Curse** instead (permanent drawback, no per-session limit —
see `asterion_rules.md`'s Deed/Curse Ability section), that's equally valid; several
Edges below suggest a natural Curse framing as an alternative.

### The four starting abilities

Adapted Character Sentence: **"I am a [Race] [Profession], marked by my
[Background], known for my [Major Edge]."**

Four clauses instead of Asterion's three — one invented ability per clause: Race
(Tier 1), Profession (Tier 1), Background (Tier 1), Major Edge (Tier 3, via the
once-per-session rule above). This is a deliberately larger starting budget than
vanilla Asterion's two-Tier-1-plus-one-Tier-2, since it's converting four fixed N&D
picks rather than freely inventing three. `asterion_rules.md` states its own
property tables are "guidelines, not strict restrictions" — GMs should feel free to
rebalance (e.g. requiring a Trade-Off on the Major Edge, or starting Background at
Tier 0/Domain-Expression-only) if this runs too strong for their table.

### The Major Edge constraint

A character's Major Edge choice is constrained to the Edge(s) tied to their chosen
**Race** *or* their chosen **Profession** — exactly how N&D's own character wizard
already scopes Edge selection today. **Consumed by Yellow** is the one race with no
race-Edge (by original N&D design — it "cannot get Edges"); a Consumed by Yellow
character takes their Major Edge from their Profession instead, so no race is ever
left without an option.

---

## Resource System

Neon & Dragons already has its own resource layer — **Health**, **Shock**, **PP**
(Physical Points), and **MP** (Mental Points) — distinct from Asterion's Flesh /
Ichor / Spark Shield (`core_rules.md` → "Derived Attributes" and "Health & Shock").
This conversion uses N&D's own resources instead.

### No Spark Shield

Drop Spark Shield entirely. N&D has no absorption-layer-before-your-body mechanic,
and neither does this conversion — damage and harmful effects go straight to
Health or Shock below; there's no shield buffer to burn through first.

### Health and Shock replace Flesh

Where `asterion_rules.md` uses a single Flesh track, this conversion uses N&D's own
two parallel tracks, matching N&D's own Health & Shock rule:

- **Health** absorbs physical harm — weapons, brute force, toxins, fire, cyberware
  failure. At 0 Health, you are dying and incapacitated (replaces Flesh's
  "Shattered").
- **Shock** absorbs mental/psychic/social harm — fear, intimidation, psychic
  assault, Yellow corruption, overwhelming stress. At 0 Shock, you are unconscious
  (roll d10 each round: 5+ regains 1 Shock and wakes you up, 3 failures = out for an
  hour — N&D's own recovery rule).

Both tracks start at Asterion's base Flesh value (5) and raise the same way Flesh
did — 1 Glory per +1 Max Health or +1 Max Shock. A Passive **Max Health** or **Max
Shock** property (choose one) replaces the old Spark Shield row in the Passive
property table.

An ability's Restoration or Special Effect property targets whichever track
matches its fiction — medical nanites restore Health, a calming psychic pulse
restores Shock. Most abilities only ever touch one track; a handful (Yellow
corruption, cyberpsychosis) can plausibly hit both, at GM's discretion.

### PP and MP replace Ichor

Where `asterion_rules.md` uses a single Ichor pool to pay for every Active ability
and to push dice pools, this conversion splits that pool into N&D's own **PP** and
**MP** — the same split N&D already uses to fuel feats and boost rolls:

- **PP** fuels abilities rooted in a Physical Stat (Strength, Dexterity, Body,
  Perception) — melee, gunplay, toughness, physical senses.
- **MP** fuels abilities rooted in a Mental Stat (Willpower, Intellect, Charisma,
  Intuition) — hacking, persuasion, willpower, psychic effects.

Every Tier cost that used to read "Ichor" now reads **PP** or **MP** — whichever
matches the ability's fiction (Tier 1 Active = 0, Tier 2 = 1, Tier 3 = 3, exactly as
before, just paid from the matching pool). Both pools start at Asterion's base
Ichor value (5) and raise the same way Ichor did — 1 Glory per +1 Max PP or +1 Max
MP.

**Pushing Your Limits** works the same as `asterion_rules.md` describes, spending
PP or MP instead of Ichor depending on whether the roll itself is a physical or
mental effort (matching N&D's own "spend up to 2 PP or MP to boost a roll" — GMs
who want that exact cap can apply it here too, though this conversion doesn't
require it).

Purely social/narrative abilities with no damage, restoration, or Max-track
component (most Tier 1 Special Effect abilities below) simply cost 0 PP or 0 MP,
matching their existing "0 Ichor" cost, and don't otherwise touch Health/Shock.

### Applies to player characters, not the bestiary

This resource swap covers characters built with this conversion document.
`asterion_rules.md`'s NPC/Elite/Boss stat blocks (which budget Flesh/Spark
Shield/Ichor) are unaffected — a GM running N&D-flavored enemies can keep using
those blocks as-is, or convert them to Health/Shock/PP/MP by the same logic,
GM's choice.

---

## Worked Example: a Drow Hacker

> "I am a **Drow** **Hacker**, marked by my being **Infamous**, known for my
> **Veil of the Unseen War**."

- **Race ability — Umbral Sight** (Tier 1 Passive, Senses): 1 Superhuman Sense —
  full darkvision, seeing in total darkness as if it were day.
- **Profession ability — Data Spider** (Tier 1 Active, Special Effect, 0 MP):
  minor narrative effect — your Electronic Buddy digs up one usable lead about a
  target, system, or location once per scene.
- **Background ability — Reputation Precedes You** (Tier 1 Active, Special Effect,
  0 MP): minor narrative effect — your bounty-fueled infamy alone cows or
  impresses a minor NPC once per scene, no roll needed.
- **Major Edge — Veil of the Unseen War** (Tier 3 Active, once per session, 3
  PP): properties — Special Effect (Hard Crowd Control: retroactively reveal
  you'd already sabotaged/infiltrated/escaped the scene, per the Edge's own
  "Shadow Infiltration" / "Slip Through the Net" options), Duration (up to 10
  minutes — the twist's consequences hold for the rest of the scene), Range (100
  feet — the effect can touch anything within the immediate location). GM narrates
  the specific twist together with the player, matching whichever of the Edge's
  four listed narrative effects fits the moment.

This character has 3 Tier 1 abilities (Race, Profession, Background) plus 1 Tier 3
Active ability (Major Edge), matching the budget above.

---

## Ability Sources

### Race Ability

**Methodology:** Read the race's existing N&D description for its single clearest
innate trait, then map it onto one Asterion Passive property (Armor / Resistance /
Movement / Senses / Max Health / Max Shock) at Tier 1. A few races (noted below) have a
canonical N&D bonus that reads stronger than a single Tier 1 property — those are
flagged as reasonable Tier 1→2 exceptions rather than forced into an artificially
weak fit; `asterion_rules.md` explicitly allows bending its own tables when an
ability "perfectly fits the narrative." A handful of races (Devilspawn, High
Elves/Elves, Child of the Black Goat) read more naturally as an innate *Active*
ability (a biological function, per `asterion_rules.md`'s own Origin/Lineage
guidance) than a Passive one — those are marked Active below.

#### Standard tier

| Race | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Below Elves/Drow** | Umbral Sight | Passive T1 (Senses) | Full darkvision — see in total darkness as if it were day. |
| **Deviltouched** | Cinderskin | Passive T1 (Resistance) | Mundane Immunity to ordinary heat/fire (handle flame and hot metal unharmed). *Upgrade note: for N&D's full "half damage from all fire" effect, build as Tier 2 Resistance instead.* |
| **Dwarf** | Miner's Eye | Passive T1 (Senses) | An uncanny knack for spotting hidden doors, caches, and mechanisms. |
| **Fae** | Fluxform Physiology | Passive T1 (Movement) | Minor trait — unnaturally flexible, alien body; can squeeze through tight gaps and subtly reshape your features during a Rest. |
| **High Elves/Elves** | Aura Sight | Active T1 (Special Effect, 0 MP) | Minor narrative effect — as an action, read a person's emotional/social aura for insight into their mood or intent. |
| **Human** | Adaptive Instinct | Active T1 (Special Effect, 0 PP or MP) | Minor narrative effect — once per scene, briefly mimic a minor trick you've seen another race or profession use, at GM's discretion (pay from whichever pool matches the mimicked trick). |
| **Low Elves/Orks** | Keen Scent | Passive T1 (Senses) | Exceptional sense of smell — track by scent, detect what mundane noses can't. |

#### Advanced tier

| Race | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Advent AI** | Network Sense | Passive T1 (Senses) | Perceive and interface with nearby electronic systems — a built-in scanner for tech and networks. |
| **Banshee** | Iron Chassis | Passive T1→2 (Armor) | +1 Armor, representing your robotic shell. *Exception note: a genuine Tier 2 pick at Race-tier, justified by the race's built-in drawbacks — no citizen rights, extra damage from electric sources, can't use Bio Augments.* |
| **Child of the Black Goat** | Voice of the Wild | Active T1 (Special Effect, 0 MP) | Minor narrative effect — telepathically communicate with nearby animals, who default to neutral or friendly toward you. |
| **Crimson Elves/Amalgama** | Sanguine Metabolism | Passive T1 (Resistance) | Mundane Immunity — stims, drugs, and mundane toxins have no effect on you (your body runs on blood, not chemistry). |
| **Devilspawn** | Third Eye | Active T1 (Special Effect, 0 MP) | Minor narrative effect — open your third eye as an action to sense one hidden truth or danger nearby. |
| **Xerm** | Psy-Null Physiology | Passive T1 (Resistance) | Mundane Immunity-tier resistance to mental/Psy-type intrusion — your altered biology is hostile ground for psychic effects. |

#### Exceptional tier

| Race | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Consumed by Yellow** | Hollow Vessel | Passive T1 (Max Health) | +1 Max Health, representing the Yellow's corrupted protection. *Flavor note: carries the race's own built-in hunger drawback — no formal Trade-Off needed, it's already baked into the fiction (must "feed" periodically or take Health damage). This race gets no race-Major-Edge — take your Major Edge from your Profession instead.* |
| **Dragonblooded** | Bloodline Spark | Passive or Active T1 (per bloodline) | Choose a Bloodline (Infernis=Fire, Glaciar=Ice, Voltaris=Storm, Umbracline=Shadow, Nekrith=Death/Decay, Psionis=Mind, Crimson=Blood/Rage) — several map almost directly onto `asterion_rules.md`'s own Archetype Reference Table (Fire/Sun, Ice/Winter, Storm/Sky, Secrets/Night, Death/Fate). Build a Tier 1 property matching your bloodline's theme, e.g. Infernis → Resistance: Mundane Immunity to heat; Glaciar → Resistance: Mundane Immunity to cold. |
| **Eldritch** | Void-Touched Physiology | Passive T1 (Resistance) | Mundane Immunity — no need to breathe, eat, drink, or sleep; survive unharmed in vacuum or extreme environments. |
| **Stitches** | Undying Patchwork | Active T1 (Restoration, 0 PP) | Restore 1 Health, usable only the instant you would otherwise be dying — your body claws itself back together at the last second. |

### Profession Ability

**Methodology:** Every profession's N&D "special resource" mechanic (Credit,
Nanite Charges, Electronic Buddy, Bullet Time, Network Access, Street Cred) already
describes a repeatable, on-demand trick — that maps cleanly onto a Tier 1 Active
ability (0 PP or MP, one property from the Active table, whichever pool matches
the profession's Physical or Mental flavor). Pick the resource's signature,
lowest-cost use as the concrete Tier 1 spec.

| Profession | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Charlatan** | Grease Palms | Active T1 (Special Effect, 0 MP) | Minor narrative effect — talk or bribe your way past one minor obstacle (a locked door, a suspicious guard) without a roll. |
| **Cyberdoc** | Micro-Repair Cloud | Active T1 (Restoration, 0 PP) | Restore 1 Health to an ally at touch range with a burst of nanites. |
| **Hacker** | Data Spider | Active T1 (Special Effect, 0 MP) | Minor narrative effect — your Electronic Buddy digs up one usable lead about a target, system, or location. |
| **Merc** | Snapshot | Active T1 (Base Damage, 0 PP) | 1 Damage, melee/touch range — a fast, professional shot. |
| **Psyonic** | Call to Minds | Active T1 (Special Effect, 0 MP) | Minor narrative effect — briefly reach another psyonic you know mentally to send a short message or image. |
| **Street Fighter** | Trained Strike | Active T1 (Base Damage, 0 PP) | 1 Damage, melee — a precise unarmed or improvised strike. |

### Background Ability

**Methodology:** N&D's Origins are life-history flags, not combat traits — most
translate best into a Tier 1 social/utility Active (matching `asterion_rules.md`'s
"Domain Expression" Tier 2 Stunt flavor: narrative permission, social leverage) or a
minor Passive where the Origin describes a physical condition (Infected, Psycho).

**Players are free to invent their own Background instead of picking one of N&D's
five** — unlike Race and Profession, which stay fixed to N&D's catalog, Background
is the one slot where a homebrew concept is explicitly welcome (a lost memory, a
cult upbringing, a survived disaster — whatever life-history flag fits the
character). Use the same conversion methodology above: state the background in one
sentence, then build one Tier 1 ability (Active or Passive) from Asterion's property
tables that reflects it, the same way each of the five listed Origins was converted
below. The five below remain there as ready-to-use options and as worked examples
for building your own.

| Origin | Ability | Type & Tier | Effect |
|---|---|---|---|
| **Ex-Corp** | Corporate Rolodex | Active T1 (Special Effect, 0 MP) | Minor narrative effect — call in a small corporate favor or contact once per scene (a name, a badge, a door that opens). |
| **Identity unknown** | Augmented Vigilance | Passive T1 (Senses) | A minor augment-granted sense — heightened alertness; you tend to notice when you're being watched or followed. |
| **Infamous** | Reputation Precedes You | Active T1 (Special Effect, 0 MP) | Minor narrative effect — your bounty-fueled infamy alone cows or impresses a minor NPC once per scene, no roll needed. |
| **Infected** | Undying Infection | Passive T1 (Max Health) | +1 Max Health — the necrotic virus keeps you standing. |
| **Psycho** | Chrome Fugue | Passive T1 (Resistance) | Mundane Immunity to Fear/Intimidation while your cyberpsychosis runs hot. *Flavor note: keep N&D's own Noise-dial drawback as-is — the GM may occasionally seize control during a spike, the same narrative cost as a Passive Curse, without needing to formally spend a Trade-Off on it.* |

### Major Edge Ability

**Methodology:** Every Major Edge already lists 3–4 concrete narrative-effect
options in its N&D text — pick one as the ability's core **Special Effect**
(usually Tier 3: Hard Crowd Control / major narrative shift), paired with a
Duration (typically Tier 2: up to 10 minutes, or scene-long) and a Range (typically
Tier 1: 30 ft, or self/touch for transformation-style Edges) to fill out the
required three properties. All are **Active, once per session** (3 PP or 3 MP,
whichever matches the Edge's physical or mental fiction) per the rule above — the
GM adjudicates which specific narrative option applies each time it's used, exactly
as N&D's own Edge text already presents a menu of outcomes. The
other narrative options an Edge lists remain available as GM-adjudicated flavor for
the same core ability, not separate abilities.

**Race-tied (16):**

| Race | Major Edge | Core Effect (from N&D text) |
|---|---|---|
| Advent AI | I Am the System | Total, temporary control over nearby technology, perception, or information flow. |
| Banshee | Wail of the Forgotten Circuit | A scream that causes mass panic, possesses an unsecured machine, or forces a haunting vision. |
| Child of the Black Goat | All-Mother’s Embrace | Nearby animals/plants actively aid you, or you take on a beast aspect. |
| Crimson Elves/Amalgama | Divine Flesh, Profane Will | Reshape your own body (or someone else's) into whatever the moment demands. |
| Devilspawn | Whispers of the Antagonist | Partial possession grants perfect prediction, forbidden knowledge, or an infectious suggestion. |
| Deviltouched | Signed in Suffering | Reveal or force a binding legal/contractual twist that favors you. |
| Dragonblooded | Power of the Bloodline | A bloodline-specific reality-bending effect (see the seven bloodline write-ups in N&D's own text). |
| Below Elves/Drow | Veil of the Unseen War | Retroactively reveal you'd already infiltrated, sabotaged, or escaped the scene. |
| Dwarf | Stonebound Resolve | You become the immovable object — hold a collapsing structure, silence a mob, resist any toxin or mind-effect. |
| Eldritch | Reality Is a Suggestion | Bend space, causality, or physics briefly around yourself. |
| High Elves/Elves | The Moment That Bends | Retroactively "already made the smarter choice," or dominate a scene's social tempo. |
| Fae | Truthless Form | Manifest an impossible alien body part or ability for the scene. |
| Human | We Rise Together | Rally allies from certain collapse, improvise a brilliant solution, or ignite a rebellion. |
| Low Elves/Orks | Feast of Echoes | Consume a fallen creature's flesh to inherit a flash of their memory or skill. |
| Stitches | Patchwork Persistence | Graft on a new limb mid-fight, ignore pain entirely, or reassemble into something monstrous. |
| Xerm | Override the Limit | Your auxiliary heart overdrives — impossible feats of strength, speed, or protection. |

*Consumed by Yellow has no race-Edge (by N&D design) — take your Major Edge from your Profession instead.*

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

## Quick Reference / FAQ

**Why Health/Shock/PP/MP instead of Flesh/Ichor/Spark Shield?** Because Neon &
Dragons already has its own resource layer with that exact name and split (see
`core_rules.md`) — reusing it keeps a converted character's sheet feeling like N&D,
not vanilla Asterion. Mechanically the numbers work the same as Flesh/Ichor at
Tier 1–3, just paid from four pools instead of two, and with no shield layer
absorbing damage first.

**Do I have to pick one of the five listed Origins for my Background?** No — the
five (Ex-Corp, Identity unknown, Infamous, Infected, Psycho) are ready-to-use
options, but you're free to write your own background and build a Tier 1 ability
for it using the same methodology. Race and Profession stay fixed to N&D's catalog;
Background is the one slot meant for a homebrew concept.

**My race and my profession both have a Major Edge tied to them — can I pick
either?** Yes. Pick whichever fits your character concept; you only get one Major
Edge at creation.

**I'm playing Consumed by Yellow — do I ever get a race-Edge?** No, by original
N&D design. Take your Major Edge from your chosen Profession's list instead.

**Can I build my Background or Race ability as a Passive Curse instead of a normal
Tier 1 pick?** Not by default — only the Major Edge slot uses the Deed/Curse
framing. If a specific Background or Race clearly wants a permanent drawback
instead (Psycho's Chrome Fugue is a good candidate), the GM can allow it as a
reasonable exception, same as any other table-level rebalancing.

**Do these abilities grow later, or are they locked at four forever?** They grow
exactly like vanilla Asterion — spend Glory to upgrade an existing ability with a
new property, or invent brand-new abilities entirely unrelated to your original
Race/Profession/Background/Edge, per `asterion_rules.md`'s "Progression and Glory"
section. The four starting abilities are just where a Neon & Dragons character
begins, not a permanent ceiling.

**What about Minor Edges?** N&D's Minor Edge is already pure freeform narrative
perk chosen with the GM, not catalog-backed — it needs no conversion at all. Keep
using it exactly as-is, or fold it into Domain Expression (see
`asterion_rules.md`) if your table wants it formalized.
