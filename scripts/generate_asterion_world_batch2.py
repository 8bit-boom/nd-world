"""
Generates scratchpad/batch2_unique.json — the second wave of Asterion/Game of Gods
world-import entities, covering everything NOT delegated to the pantheon-sphere
transcription subagents: unique named cosmic-tier statblocks, the ruler of
Asterion and his mount, the Morrigan PC, the Denizens of Asterion NPC roster,
the generic Arena Bestiary, the three Ring locations, the cosmic map, and the
Roleplay/Reputation/Followers rules supplement.

Source material: Game_of_Gods.zip (Zeus, Loki [Cosmos-Level], Odin, Bast,
Oramis, Nykhemera & Philotechnos, Ice Queen, Nameless God of War + Asterion
the warhorse, morrigan_character_sheet_v4.html, asterion_denizens_stat_blocks.html,
Asterion Arena Bestiary.mht) + Lore.zip (3 Ring descriptions) + the
Cosmic Map and Rules Supplement markdown files.

Usage:
    python3 scripts/generate_asterion_world_batch2.py
"""

import json
from pathlib import Path

OUT_PATH = Path(__file__).parent.parent.parent / "scratchpad_out" / "batch2_unique.json"
# Actually write directly into the scratchpad the session already uses:
OUT_PATH = Path("/tmp/claude-0/-home-user-nd-world/0c9a527a-05ae-5453-9c47-63e6a4e843a7/scratchpad/batch2_unique.json")

entities = []


def add(name, kind, subtype, folder, tags, summary, body):
    entities.append({
        "name": name,
        "kind": kind,
        "subtype": subtype,
        "folder": folder,
        "tags": tags,
        "summary": summary,
        "body": body.strip() + "\n",
        "image_url": None,
        "image_data": None,
    })


# ────────────────────────────────────────────────────────────────────────────
# Rulers of Asterion
# ────────────────────────────────────────────────────────────────────────────

add(
    "The Nameless God of War", "character", "villain", "NPCs/Rulers of Asterion",
    "Nameless God, ruler, Godhand, cosmos-level boss, martial",
    "The Warden of Asterion and bearer of the Godhand — an ascended mortal who rules the city through undeniable physical supremacy.",
    """
*The Warden of Asterion · Bearer of the Godhand · Cosmos-Level Pinnacle Boss*

**Origin:** Ascended Mortal | **Spark:** Absolute Force | **Epic Deed:** The Fist That Pierced Heaven

> "I have no need for a name, nor a pantheon, nor a throne. I am the fist that shattered the sky. If you wish to rule this city, you must first survive my strike."

## Core Identity

The Nameless God was never born divine. He was a mortal ascetic who believed godhood was simply a physical limit waiting to be broken, and during the Primordial War he proved it — walking into the apocalypse itself, punching through cosmic spells, and grinding gods and titans beneath his bare hands until his technique reached its absolute peak: the **Godhand**, a stance and strike so conceptually perfect it can shatter the barrier between the mortal and divine planes. He rules Asterion the same way he earned his power — through undeniable physical supremacy, an absolute meritocracy, and a martial code that never lies, ambushes, or poisons. Every ability below is a technique of the body empowered by the Godhand, not sorcery.

## Boss Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Cosmos-Level Pinnacle Boss | Rank 5 — Ascended Mortal, no true divine domain |
| Dice Pool | 4d10 | All actions — attack, defense, and Domain-tied checks alike |
| Flesh | 50 | A body hardened past any mortal or divine limit |
| Spark Shield | 10 | Not divine authority — pure trained physical resilience |
| Stamina | 10 | Fuels martial techniques, not spells — regenerates 1 automatically at the start of his turn |
| Armor | 3 | Skin and muscle conditioned like living plate |
| Movement | 30 ft / 6 hexes | Closes distance instantly via Vacuum Grasp instead of raw speed |

**Resistances:** Absolute Immunity to Mind-Control and Forced Movement. His body and stance cannot be altered, teleported, petrified, or repositioned by any force except his own will.

**Passive — Aura of the Undefeated King (Intimidation Aura):** Every creature within 60 feet who has not personally landed a hit on the Nameless God this encounter must succeed on a Willpower check at the start of their turn or become Weakened until their next turn, overwhelmed by raw presence rather than fear magic. The moment a creature draws his blood or breaks his Spark Shield even once, they become permanently immune to this aura for the rest of the encounter — he respects those who have proven themselves, and his aura no longer weighs on them.

## Passive Traits

- **Endless Reserve:** regenerates 1 Stamina automatically at the start of each of his turns, on top of any Stamina recovered through other means.
- **The Unyielding Mountain:** cannot be knocked down, pushed, teleported, or physically moved by any force other than his own will. Spells attempting to alter his body — petrification, blood-bending, transmutation — shatter against his aura of pure martial intent.
- **Meritocratic Instinct:** whenever an enemy deals damage to him equal to or greater than his current Armor value, he immediately identifies that enemy's single strongest ability and gains 1 bonus die on his very next roll made specifically to counter or punish it.

## Active Abilities — Techniques of the Godhand

- **Vacuum Grasp** *(Active, 1 Stamina, 100 ft)* — punches the air so hard it creates a vacuum, dragging a target up to 100 feet directly into melee range and dealing 2 physical damage.
- **Thousand-Fist Requiem** *(Active, 2 Stamina, Melee)* — three separate attack rolls at 3d10 each against one adjacent target; each successful strike deals 1 damage, and if all three hit, the target is Stunned until the start of their next turn.
- **Mountain-Splitting Palm** *(Active, 2 Stamina, 15-ft radius)* — every creature in the radius rolls Defense; on a failure they take 2 damage and are knocked Restrained as the terrain cracks and grips their feet for 1 round.
- **Meridian Rupture** *(Active, 2 Stamina, Melee)* — a pressure-point strike that deals no damage now, but the target takes 4 damage bypassing Armor at the start of their next turn unless they spend their entire turn steadying their body.
- **Bone King's Grip** *(Active, 1 Stamina, Melee/Grapple)* — opposed grapple roll; on a win, the target is Restrained and takes 1 damage per round automatically until they escape with a full Main Action.
- **Shattered Aura Step** *(Reaction, 1 Stamina)* — when an enemy declares a ranged or area attack targeting him, he closes the distance before it resolves, moving up to 30 feet and making a basic melee attack; if it hits, the incoming attack automatically fails.
- **The King's Challenge** *(Active, 2 Stamina, 100 ft)* — declares one enemy worthy of his full attention; for 1 round all his attacks against that target gain 1 bonus die, and that target cannot be healed, shielded, or otherwise assisted by allies.

## Epic Deed

**The Godhand: Heaven-Piercing Strike** *(Once Per Session, 3 Stamina, Tier 3)* — his ultimate technique. He steps forward and throws a single, perfect punch. Everything in a 60-foot cone takes massive physical damage (Tier 3 Base Damage) and is subjected to a Hard Crowd Control effect as bodies and stone alike are hurled backward.

*Trade-Off (Boss Trait):* the sheer force of the Godhand shatters the Absolute Law of any Domain it is used in for the rest of the scene, and instantly reduces all targets' Spark Shields to 0.

## GM Notes

- **Everything is martial, nothing is magic.** Describe every technique as a physical feat of impossible bodily perfection — vacuum punches, pressure-point strikes, shockwaves through stone — never as spellcasting.
- **The aura rewards courage, not caution.** It punishes hesitation but stops affecting anyone who has already drawn blood or broken his Spark Shield.
- **His code is absolute.** He will never ambush, poison, or lie about his intentions. The King's Challenge exists specifically so he can honor his one-on-one martial code even inside a group fight.
- **The Godhand should feel earned.** Telegraph Heaven-Piercing Strike clearly one full turn in advance — the arena should visibly tremble, the air should go still, before the final blow falls.

*See also: Asterion, the Augean Warhorse (his divine mount) and the Combined Tactics note for rider+mount synergy abilities.*

---
*Asterion Unified Rulebook · Cosmos-Level Pinnacle Boss · Game of Gods*
""",
)

add(
    "Asterion, the Augean Warhorse", "creature", "Elite", "NPCs/Rulers of Asterion",
    "divine mount, Nameless God, Augean bloodline, elite companion",
    "The Nameless God's ageless, unbreakable divine warhorse — a companion who chooses his rider rather than serving one.",
    """
*Last Horse of the Augean Bloodline · Divine Mount of the Nameless God of War · Domain-Level Elite Mount*

**Bloodline:** Poseidon / Helios — Ageless · Diseaseless · Unbreakable · Companion, not an independent boss.

> "He does not carry me because I command him. He carries me because I never stopped returning." — The Nameless God of War

Note: shares its name with the city of Asterion itself by design — this entity is the Nameless God's personal warhorse, not the city.

## Core Identity

Asterion is one of the last surviving horses of the Augean bloodline, sacred stock said to descend from a gift of Poseidon or the light of Helios, and therefore functionally ageless, diseaseless, and unbreakable by any force short of an equally relentless will. He is not a magical construct or a Primordial abomination — he is an ancient, patient, and genuinely divine animal who chooses his rider rather than serving one. The Nameless God spent a year proving himself worthy through pure endurance, never magic or cruelty, before Asterion simply stopped running from him.

## Mount Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Domain-Level Elite Mount | Companion-tier, not an independent boss |
| Attack Pool | 2d10 (3d10 charging) | Bite, rear-kick, or trample attacks |
| Defense Pool | 3d10 | Ancient reflexes honed over millennia |
| Spark Shield | 6 | Faint residual divine aura from his bloodline |
| Flesh | 16 | Massive, heavy-boned, built to absorb punishment |
| Ichor | 6 | Minimal — power is physical and ancestral, not sorcerous |
| Armor | 2 | Dense hide and old battle-scarring |
| Movement | 40 ft / 8 hexes (60 ft / 12 hexes charging) | Faster and heavier than any mortal horse |

**Resistances:** Immune to aging, disease, poison, and exhaustion. Greater Resistance (2 Attacker Successes) against Mind-Control, Fear, and Forced Movement — his will cannot be bent, only earned.

## Passive Traits

- **Bloodline of the Sea and Sun:** cannot die of old age, sickness, or exhaustion, and automatically stabilizes instead of dying outright unless reduced to 0 Flesh by direct violence. Divine or Primordial weapons are required to permanently harm his bloodline.
- **Unbroken Will:** any effect that would Charm, Dominate, Frighten, or otherwise force his actions requires the caster to win an opposed roll against his full Defense Pool, even if the effect would normally not allow a save. Failed attempts cause him to become openly hostile to the caster for the rest of the scene.
- **Rider's Trust:** only grants his full Attack Pool bonus, movement bonus, and Active Abilities to a rider he has accepted (established through significant roleplayed effort, not a single roll). An unearned rider may use him only as a normal fast mount with no abilities.
- **Ancient Calm:** cannot be surprised or flanked while his rider is mounted.

## Active Abilities

- **Bronze Charge** *(Active, 0 Ichor, Main Action, Rider-Directed)* — moves up to 60 feet in a straight line and makes one melee attack at the end with 3d10 instead of 2d10; on a hit, deal 2 base damage, and if the target is smaller than Asterion they are also knocked Restrained for 1 turn.
- **Sea-Foam Rear** *(Reaction, 1 Ichor)* — when an enemy attacks Asterion or his rider in melee, he rears and strikes with both forehooves; opposed attack, on a hit deal 2 damage and the triggering attack automatically misses.
- **Trample the Unworthy** *(Active, 2 Ichor, Main Action)* — moves through the spaces of up to two Mortal or Standard-tier enemies; each makes a Defense roll or takes 3 physical damage and is Restrained for 1 round (Elite-tier and above only take 1 damage, no Restrain).
- **Ancient Endurance** *(Active, 1 Ichor, Free Action, Once Per Scene)* — ignores the next source of Bleeding, Poison, Weakened, or Stunned entirely (not usable against divine or Primordial-sourced effects).

## GM Notes

Asterion should never be treated as disposable set dressing — his Unbroken Will and Rider's Trust traits mean any PC or NPC hoping to ride him must earn it through sustained roleplay, not a single Persuasion roll. If the party ever wants to challenge the Nameless God directly, consider making Asterion a separate objective: distracting, calming, or even winning over the horse mid-fight could remove the Godhand's Charge synergy entirely, softening the encounter without touching the god's own stats.

*See also: The Nameless God of War (his rider) and the Combined Tactics note for rider+mount synergy abilities.*

---
*Asterion Unified Rulebook · Divine Mount Companion Sheet · Game of Gods*
""",
)

add(
    "Combined Tactics — The Nameless God & Asterion", "note", "lore", "NPCs/Rulers of Asterion",
    "combo abilities, Nameless God, Asterion, rider and steed, synergy",
    "Synergy abilities that exist only while the Nameless God rides an Asterion who has accepted him.",
    """
These techniques exist only while the Nameless God rides an Asterion who has accepted him (see *Rider's Trust* on Asterion's own entry). Using any Combined ability consumes an action from the rider only — Asterion's own action economy is unaffected unless stated.

## The Godhand's Charge
**Combo — Free, Triggered.** When the Nameless God uses Vacuum Grasp or a basic attack immediately after Asterion's Bronze Charge this turn, the attack gains +1 base damage and ignores the target's Armor entirely — the combined momentum of horse and rider turning a strike into an unstoppable collision of muscle, bronze hide, and divine fist.

## Twin Kings' Trample
**Combo — 2 Stamina (shared pool), Main Action.** Asterion performs Trample the Unworthy while the Nameless God simultaneously drives Mountain-Splitting Palm into the same path from horseback. Resolve both effects together against every creature in the trample's line: they take the combined damage of both abilities and must succeed on a single Defense roll to avoid being Restrained rather than rolling separately for each.

## Undefeated Herd
**Combo — Passive, Always On.** While mounted, the Nameless God's Aura of the Undefeated King extends to a 90-foot radius instead of 60 feet, and Asterion becomes immune to the Weakened condition entirely.

## Heaven-Piercing Charge
**Combo Deed — Once Per Session, 3 Stamina, Tier 3.** The Nameless God unleashes The Godhand: Heaven-Piercing Strike while Asterion is mid-Bronze Charge, throwing the punch at full gallop. The cone extends to 90 feet instead of 60, and any target that survives the initial hit is also knocked along the horse's charge line, taking Asterion's Bronze Charge damage a second time as they're dragged behind the momentum. This consumes both combatants' actions for the round and shares its once-per-session cooldown with the Nameless God's own Heaven-Piercing Strike.

## GM Notes

- **Rider's Trust gates the combos.** If a PC or rival ever steals or borrows Asterion, none of the Combined Tactics function until that new rider earns the same trust — this makes taking the horse a real strategic prize rather than a free upgrade.
- **Telegraph the Heaven-Piercing Charge hard.** This is the single most devastating combo in the game — give the table a full round of warning as the ground trembles and Asterion's hooves start glowing before the charge begins.
- **Removing Asterion softens the fight.** Since Undefeated Herd and the Godhand's Charge both require the mount, disabling or separating Asterion mid-combat is a legitimate way for clever players to reduce the encounter's difficulty without touching the god's own stat block.
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Norse Sphere — cosmos-tier named statblocks
# ────────────────────────────────────────────────────────────────────────────

add(
    "Odin, the Allfather", "character", "villain", "NPCs/Norse Sphere",
    "Norse Sphere, hidden identity, disguise, boss, wisdom, war, runic magic",
    "Boss statblock for the Allfather's throne — mechanically, this is the persona Ascended Loki wears while ruling the Norse Sphere in disguise.",
    """
*Boss statblock — Norse Sphere, god of wisdom, war, death, poetry, and magic*

## GM Note — The Mask

Per **Ascended Loki, the Hollow Crown**'s own entry: "The Odin, the Allfather statblock is Loki's disguised form — a fully functional persona with its own abilities, used whenever he wishes to rule, deceive, or fight without revealing himself." The stats below ARE that persona — a complete, independently usable statblock for whenever Loki (or, at your table, a genuine Odin) sits the throne. Damage taken in one form carries over if you're running the "Loki wears the mask" version; run it as a standalone deity if you'd rather Odin simply exist. Consider hiding the "Ascended Loki" cross-reference from players until the twist should land.

## Core Profile

**Domains:** Wisdom, war, death, poetry, and runic magic.

> "I am the Allfather who wields the Spark of Sacrificed Wisdom, known for the Eye I traded at Mimir's Well and the Spear that never misses its mark."

**Temperament:** Cunning, relentless, and knowledge-hungry — he gathers power and foresight at any personal cost, and speaks to mortals and gods alike as pieces on a board only he can fully see.

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Domain-Level Apex Threat (Boss) | Allfather of the Norse Sphere |
| Attack Pool | 3d10 | Boss-tier |
| Defense Pool | 3d10 | Centuries of war and foresight sharpen his guard |
| Spark Shield | 6 | Regenerates fully at the start of every encounter |
| Flesh | 16 | |
| Ichor | 8 | Fuels runic and sacrificial magic |
| Armor | 2 | Runic iron and bronze war-plate |
| Movement | 30 ft / 6 hexes | Sleipnir may be summoned for Fast Movement — GM discretion |

## Passive Nature (Always Active)

- **The Price of the Eye (Mythic Sense):** Odin sacrificed one eye at Mimir's Well for wisdom beyond mortal or divine reach. He automatically knows the general shape of any foe's greatest weakness or greatest fear the first time he studies them in a scene, and cannot be deceived by illusions affecting sight alone.
- **Huginn and Muninn (Advanced Sense):** as a free action, sees and hears through his two ravens, Thought and Memory, no matter how far they've flown, holding both bonds permanently. Actively scouting for a meaningful tactical advantage counts as a Stunt costing 1 Ichor.
- **Ravens of Every Rooftop:** treated as having a standing intelligence network anywhere ravens or crows gather. Once per scene, the GM will honestly answer one question about recent events or movements in that location.
- **Runic Warding (Resistance, −1 Attacker Success):** ancient bindings woven into his armor and spear grant Resistance against all magical damage.

## Active and Reaction Abilities

- **Gungnir's Unerring Throw** *(Active, 1 Ichor, Melee or 100 ft)* — 2 damage; Gungnir never misses its mark once thrown, automatically counting as 1 guaranteed Success before dice are rolled.
- **Rune of Binding** *(Active, 1 Ichor, 30 ft)* — target rolls Defense; on a failure, Restrained for 1 round as runes of iron chains burn into the ground.
- **Storm of the Slain** *(Active, 2 Ichor, 15-ft radius)* — all enemies in the radius roll Defense; on a failure, take 2 damage and are Blinded until their next turn.
- **The Allfather's Foresight** *(Reaction)* — once per round, when an enemy declares an attack against him, spend 1 Ichor to gain +1d10 to his Defender pool for that roll.

## Epic Deed

**Blood on the World Tree** *(Active Deed, Once per session, 3 Ichor, Tier 3)* — Odin cuts himself and hangs his own vitality on the threads of fate. He takes 2 self-inflicted Flesh damage, then unleashes a 60-foot burst of runic force. Every enemy in the area rolls Defense; on a failure, take 4 damage and Stunned for 1 round. Odin regains 3 Ichor immediately as the sacrifice returns knowledge and power to him.

## Boss Mechanics

- **Phase Break — The Second Sacrifice:** when Flesh drops below half (8 or lower), Spark Shield fully refreshes and he unlocks Wode-Fury — for the rest of the encounter, Attack Pool increases to 4d10 and Storm of the Slain no longer costs Ichor.
- **Legendary Action — Watchful Ravens:** once per round, outside his own turn, Huginn or Muninn dive at an enemy who dealt damage to him, forcing that enemy to lose 1 die from their next roll this round. Does not use his normal Reaction.

## Image Generation Prompt

A dark-fantasy digital illustration of Odin, the Allfather, standing atop a wind-battered stone rampart overlooking a besieged battlefield beneath a churning storm sky. He is an imposing, one-eyed elder god with a weathered, battle-scarred face, his missing eye covered by a simple leather patch etched with faint glowing runes, his remaining eye burning with pale silver-blue light that seems to see through time itself. He has a long grey-white beard braided with iron rings and small bones, and wears a heavy dark cloak of raven feathers over layered bronze-and-iron armor inscribed with runic patterns that faintly glow. He grips Gungnir, a long ash-wood spear tipped with a rune-etched iron head that hums with restrained power, planted firmly at his side. Two black ravens, Huginn and Muninn, circle close around his shoulders and head, one perched briefly on the spear, their eyes glowing faintly the same pale blue as his own. Behind him, storm clouds churn with flickers of lightning, and distant fires burn across a ruined battlefield strewn with fallen warriors. Palette of iron-grey, deep storm-blue, bone-white, and faint runic cyan glow, with his single eye as the brightest point of focus in the frame. Cinematic low-angle composition, dramatic overcast lighting, richly detailed painterly dark-fantasy concept art conveying ancient authority, sacrifice, and the cold weight of foreseen doom.

*See also: Ascended Loki, the Hollow Crown (his true form) and Loki, Northern God of Mischief (an alternate, higher-tier independent write-up of Loki not tied to this throne plot — pick whichever suits your table).*

---
*Asterion Statblock · Odin · Norse Sphere*
""",
)

add(
    "Loki, Northern God of Mischief", "character", "villain", "NPCs/Norse Sphere",
    "Norse Sphere, trickster, illusion, cosmos-level boss, independent",
    "An independent Cosmos-Level Trickster statblock for Loki — a separate, higher-tier write-up from the Ascended Loki/Odin throne-usurper storyline.",
    """
*The Laughing God · Shapeshifter · Maker of Useful Lies · Cosmos-Level Trickster, Rank 4*

**Domains:** Mischief, Deception, Change. Independent Boss Statblock.

> "Truth is only a story that has not yet been interrupted."

## Author's Note — Which Loki Is This?

This is a separate, higher-power write-up of Loki from **Ascended Loki, the Hollow Crown** / **Odin, the Allfather** (the throne-usurper storyline) — this version makes no mention of the Hollow Crown, the stolen throne, or Odin at all, and instead pairs him with Oramis, God of Secrets. Treat it as an alternate statline for a bigger, more cosmic Loki encounter, or as an earlier draft superseded by the Hollow Crown concept — the two were provided as separate documents and are presented here without forcing a merge. Pick whichever version fits the story you're telling.

## Core Identity

Loki is the Northern god of mischief, deception, shapeshifting, loopholes, and inconvenient truths. He does not simply tell lies: he makes certainty betray the people who rely on it, turns plans inside out, and makes a battlefield doubt its own rules. He works with Oramis, the God of Secrets, when their interests overlap — Loki creates the lie, the loophole, and the contradiction, while Oramis collects the hidden fact beneath it — but they are not a combat pair and this statblock is designed for Loki alone.

## Boss Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Cosmos-Level Trickster, Rank 4 | Wins by making certainty impossible, not by trading blows |
| Dice Pool | 4d10 | Deception, escape, spellwork, and opportunistic attacks |
| Flesh | 28 | He refuses fair, prolonged exchanges of force |
| Ichor | 55 | Fuels shapeshifting, illusion, curses, and reality tricks |
| Spark Shield | 35 | Layered false identities and divine luck |
| Armor | 1 | Rarely where a blow was aimed |
| Movement | 40 ft / 8 hexes | May move through occupied spaces while disguised |

**Resistances:** Absolute immunity to ordinary lie detection, forced truth, and mundane disguise-breaking. Greater Resistance (2 Attacker Successes) against Charm, Fear, and effects that would bind him to a single form.

## Passive Traits

- **A Face for Every Witness:** at the start of a scene, Loki chooses one false identity. Until an enemy successfully sees through it with an opposed Willpower or Insight test, that enemy cannot target him with an ability requiring a known identity, true name, or clear divine signature.
- **The Joke Is on Certainty:** the first time each round an enemy fails a roll against Loki, he gains 1 Twist (maximum 3). He may spend a Twist to add 1 die to a deception, escape, or illusion roll, or to force a target to reroll one successful die against him.
- **Shapeshifter's Escape:** ignores mundane restraints and can squeeze through any opening large enough for a fox, serpent, or bird. The first Grapple or Restrain imposed on him each scene automatically fails unless it comes from a divine or Primordial source.

## Active Abilities

- **Borrowed Face** *(Active, 1 Ichor, 60 ft)* — perfectly adopts the visible form, voice, and mannerisms of a creature he can see for 1 round. Enemies must pass a Willpower test to distinguish him; on a failure they cannot willingly attack him.
- **Serpent's Interruption** *(Active, 2 Ichor, Reaction)* — when a creature within 60 feet declares an ability, Loki turns into a small serpent, raven, or flash of green fire beside them; opposed Willpower roll or the ability targets a different legal target (or fizzles).
- **Golden Promise** *(Active, 2 Ichor, 30 ft)* — offers a target exactly what they most want to hear; failed Willpower save = Charmed for 1 round, spending their movement toward a place, object, or person Loki names.
- **Misrule** *(Active, 3 Ichor, Once Per Round)* — declares a minor contradiction in a 30-foot radius for one round (up becomes down, shadows provide cover from light, doors lead where they should not); GM chooses a clear mechanical expression.
- **Foxfire Decoy** *(Active, 2 Ichor, 60 ft)* — creates up to three illusory versions of himself (Spark Shield 1 each) in unoccupied spaces; the next three attacks that would hit Loki must target a decoy first if one is legal; a struck decoy bursts, dealing 1 damage to the attacker.

## Epic Deed

**The Last Laugh** *(Once Per Session, 5 Ichor)* — after an enemy makes a major declaration, attack, or bargain, Loki reveals that its premise was false. Rewind that creature's immediately preceding turn; its actions are undone, but all Ichor and once-per-scene resources it spent remain spent. Loki then takes a free Move and one basic action.

## Reaction

**Not Where You Thought** *(1 Ichor)* — when an attack would hit Loki, he swaps places with a willing creature, a Foxfire Decoy, or a Medium-or-smaller unattended object within 30 feet; the original attack resolves against the new target.

## GM Notes

- **Run Loki as clever, not random.** His tricks should exploit a choice, flaw, promise, or assumption the players actually made.
- **He is an independent encounter.** Oramis may be a distant employer, collaborator, or source of intelligence, but grants no mechanical combo abilities to Loki.
- **Make every form useful.** A raven crosses impossible terrain, a serpent sabotages equipment, a fox escapes a grapple, and a borrowed face fractures trust.
- **Telegraph The Last Laugh.** Let Loki encourage a major declaration or bargain before revealing the hidden loophole.

## Image Generation Prompts

**Character Portrait:** A full-body dark-fantasy character illustration of Loki, the Northern god of mischief, deception, shapeshifting, and dangerous loopholes, standing on a rain-slicked broken stone bridge beneath the aurora borealis at night. He is an elegant, lean, athletic Norse god with pale warm-toned skin, sharp high cheekbones, a clever narrow face, long black hair streaked with subtle deep green and tied loosely behind his head, and bright intelligent emerald-green eyes carrying an amused, unreadable expression. He wears layered black and deep forest-green leather armor beneath a long dark green cloak with a fur-lined collar, ornate but practical gold-and-bronze Norse clasps and bracers, and a distinctive small horned circlet rather than a large helmet. One hand is open as if offering a bargain, while the other holds a small flickering ball of green foxfire; around him, three faint ghostly afterimages show different possible disguises of the same god — a raven, a serpent, and a smiling armored warrior — without obscuring his real body. Palette of deep forest green, black, muted gold, cold moonlit blue, and emerald foxfire; cinematic low-angle composition, richly detailed painterly graphic-novel dark-fantasy concept art, no text, no watermark.

**Action Scene — Misrule in the Hall:** A cinematic dark-fantasy action illustration of Loki unleashing Misrule inside a vast ruined Norse longhouse during a storm, balanced lightly on an overturned banquet table, pointing one hand outward as the rules of the room visibly turn wrong: warriors appear doubled in polished shields, gravity pulls loose weapons sideways, fire burns green without warmth, shadows stretch into false doorways. Translucent foxfire duplicates, a raven silhouette, and a serpent-shaped ribbon of green flame coil around him. Palette of black, deep forest green, emerald flame, cold blue stormlight, and tarnished gold; dynamic wide-angle composition, no text, no watermark.

---
*Asterion Unified Rulebook · Independent Divine Boss Statblock · Game of Gods*
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Olympian Sphere — cosmos-tier named statblocks
# ────────────────────────────────────────────────────────────────────────────

add(
    "Zeus, King of Gods", "character", "villain", "NPCs/Olympian Sphere",
    "Olympian Sphere, thunder, sky, law, cosmos-level boss, three forms",
    "Cosmos-Level Pinnacle Boss with three interchangeable divine forms — the Frail Old King, the Thunder Colossus, and the Swift Father of Thunder.",
    """
*God of Thunder, Sky, Law, Oaths, and Divine Kingship · Cosmos-Level Pinnacle Boss, Rank 5 · Three Divine Forms · Thunder & Lightning Mastery*

> "The sky does not ask permission to rule the earth."

## Core Identity

Zeus is the king of the gods: ruler of sky, thunder, law, order, and oaths. Authoritative, proud, and quick to punish challenge, he preserves cosmic order because he believes order is inseparable from his own rule. He found Nykhemera when others saw only a curse, raised her into a divine instrument, and commands her as a complicated father-figure and executioner. His apparent frailty is never weakness; Zeus chooses the shape that makes the most effective argument.

## Boss Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Cosmos-Level Pinnacle Boss, Rank 5 | Olympian king; applies regardless of form |
| Dice Pool | 4d10 | Thunder, physical combat, authority, and divine law |
| Flesh | 60 | Shared across every form |
| Ichor | 100 | Storm authority and god-king power |
| Spark Shield | 60 | The throne's accumulated divine legitimacy |
| Armor | Varies by form | 1 in Old King, 5 in Colossus, 2 in Swift Form |
| Movement | Varies by form | 30 ft, 25 ft, or 70 ft respectively |

**Resistances:** Absolute immunity to mundane lightning and weather effects. Greater Resistance (2 Attacker Successes) against fear, forced movement, forced transformation, and any effect that would compel him to break a sworn divine oath.

## The Three Forms of Zeus

**Form Rules:** Zeus begins in whichever form best serves his purpose. He may change form once per round as a free action, retaining all current Flesh, Ichor, conditions, and cooldowns; only movement, Armor, and form-specific powers change. A transformation arrives with an audible thunderclap and a brief flash of white-gold lightning.

**I. The Frail Old King** *(Armor 1 · Movement 30 ft)* — a bent, white-haired old man in weathered kingly robes, leaning on a lightning-scarred staff. Appears vulnerable, patient, and almost harmless, but his eyes are storm-dark and perfectly alert. Gains +1 die on Insight, Deception, social authority, and oath-based checks. **Beggar's Test:** one creature that dismisses or attacks him must pass a Willpower save or be Marked for Judgment, taking +1 damage from his lightning until scene end.

**II. The Thunder Colossus** *(Armor 5 · Movement 25 ft)* — Zeus expands into an enormous, hyper-muscular god-king, broad as a temple gate, with white hair and beard flowing through crackling stormlight. Melee attacks deal +2 base damage, he cannot be knocked down or moved against his will, and his Thunder powers gain +10 feet of radius.

**III. The Swift Father of Thunder** *(Armor 2 · Movement 70 ft)* — Zeus compresses into a lean, deceptively slim, perfectly defined fighter; lightning traces his muscles like fine veins of gold. Gains +2 dice on Defense rolls, ignores opportunity attacks, and may make one basic melee strike after any 30-foot or greater movement.

## Thunder & Lightning Powers

- **Thunderbolt of the King** *(Active, 2 Ichor, 120 ft)* — throws a white-gold lightning bolt; on a hit, deal 4 Lightning damage ignoring 2 Armor; may arc to one additional enemy within 15 feet, dealing 2 damage on a successful second attack roll.
- **Stormfather's Command** *(Active, 3 Ichor, 60-ft radius)* — calls a sudden storm for 3 rounds: enemies treat it as difficult terrain, ranged attacks suffer -1 die, and Zeus may target any point inside without needing line of sight.
- **Heaven-Splitting Roar** *(Active, 2 Ichor, 30-ft cone)* — creatures in the cone must pass a Physical or Willpower save or take 2 Thunder damage and become Stunned until the start of their next turn; unattended objects, doors, and weak walls shatter.
- **Aegis of the High Sky** *(Reaction, 2 Ichor)* — when Zeus or an ally he can see is hit, reduce the damage by 3 and deal 1 Lightning damage to the attacker; in Thunder Colossus form, the attacker is also pushed 10 feet.
- **Oathbreaker's Judgment** *(Active, 3 Ichor, 60 ft)* — names a spoken oath, broken promise, or betrayal known to the target; failed Willpower save = Bound by lightning for 1 round, taking 2 Lightning damage whenever it knowingly lies or attacks an ally during that time.
- **Meteoric Knuckle** *(Thunder Colossus Only, 2 Ichor, Melee)* — 5 physical-and-lightning damage in a 15-foot impact radius; targets that fail a Defense save are knocked Restrained in cracked earth.
- **Fist That Outruns Lightning** *(Swift Form Only, 2 Ichor, Melee)* — crosses up to 70 feet in a line and throws a single impossibly fast punch with +2 dice; on a hit, deal 4 damage and the target cannot take Reactions until their next turn.

## Epic Deed

**Katakeleuon — Wrath of the Sky King** *(Once Per Session, 6 Ichor, 120-ft radius)* — Zeus calls down a catastrophic convergence of thunderbolts. Every enemy in the radius makes a Defense save or takes massive Lightning damage (Tier 3), bypassing Armor; failed targets are Stunned for 1 round and the terrain becomes storm-charged, dealing 1 Lightning damage to enemies entering or starting a turn there for the rest of the scene.

## GM Notes

- **Use the old man first.** Let players underestimate or socially engage him, then reveal the god-king only when authority is challenged or a promise is broken.
- **Forms are answers, not phases.** Colossus answers immovable frontliners and brute force; Swift Form answers ranged threats and mobile skirmishers; the Old King controls the room through judgment and conversation.
- **Thunder should feel judicial.** Zeus does not throw lightning at random — his bolts punish defiance, oathbreaking, rebellion, or a threat to the order he considers his own.
- **Katakeleuon needs warning.** Show clouds gathering indoors, metal humming, hair rising, and every shadow flashing white before the strike.

## Image Generation Prompts

**Character Design Sheet — Three Forms:** A cinematic dark-fantasy character design sheet showing three full-body incarnations of Zeus, King of the Greek gods, side by side against a storm-dark Mount Olympus backdrop, all unmistakably the same deity through the same severe white hair, sharp blue-white eyes, regal features, and white-gold lightning aura. Left: the Frail Old King, bent and thin in weathered ivory-and-blue robes leaning on a lightning-scarred staff. Center: the Thunder Colossus, hyper-muscular with a scarred bare torso, bronze greaves, white-gold mantle, lightning bursting from clenched fists. Right: the Swift Father of Thunder, lean and athletic in minimal dark-blue and gold battle cloth, lightning afterimages suggesting impossible speed. Palette of electric blue, white-gold, storm charcoal, bronze, and deep royal blue; no text, no watermark.

**Action Scene — Katakeleuon:** A vast cinematic dark-fantasy action illustration of Zeus in Thunder Colossus form unleashing Katakeleuon from the summit of shattered Mount Olympus, both arms raised toward a spiraling black storm, dozens of white-gold thunderbolts descending toward a battlefield far below. Palette of white-gold lightning, electric blue, black storm clouds, antique bronze, and royal blue; no text, no watermark.

---
*Asterion Unified Rulebook · Zeus Three-Form Divine Boss Statblock · Game of Gods*
""",
)

add(
    "Nykhemera & Philotechnos", "character", "villain", "NPCs/Olympian Sphere",
    "Olympian Sphere, murder, arts, executioner, cosmos-level boss, support deity",
    "Zeus's personal executioner and her husband, a god of Arts who frames her killings as art — a combined Boss + Support Deity pair.",
    """
*The Goddess of Murder and the God Who Calls Her Work Art · Nykhemera: Cosmos-Level Divine Boss · Philotechnos: Domain-Level Support Deity*

> "Every ending has a shape, if someone is willing to witness it." — Philotechnos, on his wife's work

# Nykhemera, Goddess of Murder

## Core Identity

Born under a hereditary curse in a small Greek village that expected to kill children like her at birth, Nykhemera survived, and her earliest acts of violence marked her as a monster before she ever understood what that meant. Zeus found her, refused to fear her, and shaped her into a divine executioner who purges Primordials, monsters, and corrupted bloodlines without hesitation — including, on his order, her own village and her own parents. She does not kill for chaos or pleasure in any loud sense; she kills because she believes the act is necessary, sacred, and final, and her husband Philotechnos gave that instinct a name: art.

## Boss Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Cosmos-Level Divine Boss | Zeus's personal executioner |
| Dice Pool | 4d10 | Speed and precision over brute force |
| Flesh | 45 | Lean and athletic, built for endurance in prolonged violence |
| Ichor | 30 | Fuels regeneration, limb growth, and terror effects |
| Spark Shield | 15 | Moderate divine authority; she relies on speed, not defense |
| Armor | 2 | Wet, reflective scaled skin, tougher than it looks |
| Movement | 50 ft / 10 hexes | Unnaturally nimble; can leap, climb, and change direction midair |

**Resistances:** High resistance to Fear and Pain-based effects. Greater Resistance (2 Attacker Successes) to any effect that would deny her movement or bind her limbs — though such effects remain her most dangerous weakness when they do land.

**Signature Weapon — Mercy's Edge:** her personal relic blade, used only for the most significant sanctioned executions. Ignores 2 points of Armor, causes wounds that resist ordinary healing for 1 round, and deals bonus damage against Primordial-tainted creatures.

## Passive Traits

- **Monstrous Regeneration:** heals 3 Flesh automatically at the start of each of her turns; only divine or Primordial-tier anti-regeneration effects can suppress this, and only briefly.
- **Limb Growth:** regrows a lost arm, tail, claw, or eye fully by the start of her next turn. May manifest a temporary extra limb once per encounter to extend reach or grapple range for 1 round.
- **Terrifying Presence:** the first time each enemy sees her attack or take damage in a scene, they must succeed on a Willpower check or become Weakened for 1 round.

## Active Abilities

- **Sixfold Assault** *(Active, 2 Ichor, Melee)* — four separate attack rolls at 3d10 each against one or two adjacent targets (split as desired); each successful strike deals 1 damage, and any target hit by three or more strikes is Restrained until the end of their next turn.
- **Surgical Kill** *(Active, 2 Ichor, Melee)* — a precise strike at a joint, tendon, throat, or eye; on a hit, deal 3 damage bypassing Armor, and the target suffers a called-shot effect of the GM's choice (Slowed, Silenced, Blinded in one eye, or similar) until healed.
- **Tail Lash** *(Active, 1 Ichor, 15 ft)* — on a hit, deal 2 damage and reposition up to 10 feet using the tail as leverage.
- **Predatory Agility** *(Active, 1 Ichor, Reaction or Movement)* — gain +2 dice on any single Defense roll this round, or move up to 30 feet ignoring difficult terrain and opportunity attacks.
- **Maw of Quieting** *(Active, 2 Ichor, Melee)* — on a hit, deal 3 damage; if the target is already below half Flesh, they are also Silenced for 1 round.

## Epic Deed

**Mercy's Edge: The Final Composition** *(Once Per Session, 4 Ichor)* — a single, perfect execution strike. On a hit, deal massive damage (Tier 3 Base Damage) that ignores Armor and Spark Shield entirely; if this reduces the target to 0 Flesh, their death is treated as absolute and cannot be prevented or reversed by ordinary revival magic, only by intervention from a being of comparable or greater rank.

# Philotechnos, God of Arts

## Core Identity

Philotechnos is a minor god of Arts who first found Nykhemera's slaughter fascinating rather than horrifying, seeing pattern, rhythm, and aesthetic force in what others saw only as brutality. His fascination became devotion, and the two married — he does not soften her in any sentimental sense, but he gives her work a frame, a witness, and a language, and she accepts that framing because it is the closest thing to being understood she has ever received. In combat he composes the scene around her rather than fighting the way she fights.

## Support Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Domain-Level Support Deity | Not built for direct combat |
| Dice Pool | 2d10 | Elegant, unhurried, rarely under direct threat |
| Flesh | 18 | A performer's body, not a fighter's |
| Ichor | 25 | Fuels illusion, framing, and aesthetic manipulation |
| Spark Shield | 12 | Enough divine authority to resist casual harm |
| Armor | 0 | Relies entirely on positioning and misdirection |
| Movement | 30 ft / 6 hexes | Moves like a man watching his own performance from just outside it |

**Resistances:** Immune to Fear generated by Nykhemera or any of her abilities. Standard resistance to psychological and illusion-based effects from other sources.

## Passive Traits

- **The Reverent Witness:** whenever Nykhemera reduces an enemy to 0 Flesh while he is present and conscious, he may immediately grant her 1 Ichor.
- **Aesthetic Distance:** enemies find it difficult to target him directly while Nykhemera is also present in combat; any attack roll made against him instead of her requires 1 additional Attacker Success unless he has directly attacked that enemy this scene.

## Active Abilities

- **Frame the Composition** *(Active, 1 Ichor, 60 ft)* — one ally within 60 feet gains 1 bonus die on their next attack roll made this round.
- **Distort the Gallery** *(Active, 2 Ichor, 30-ft radius)* — all enemies in the radius suffer -1 die on their next attack roll.
- **Macabre Sublime** *(Active, 2 Ichor, 30-ft radius)* — allies within the radius who deal damage this round gain a small heal (1 Flesh) whenever they land a killing blow.
- **Living Memorial** *(Active, 1 Ichor, Free Action, Once Per Scene)* — after a significant kill, preserves the moment as a lingering illusion for the rest of the scene; any enemy who enters that space must succeed on a Willpower check or become Weakened for 1 round.
- **The Critic's Eye** *(Reaction, 1 Ichor)* — when an enemy within 60 feet declares an attack against Nykhemera, force that enemy to reroll one die of their attack pool and take the lower result.

# Combined Tactics — Synergy Abilities

These techniques exist only while both are present in the same scene.

- **The Finished Piece** *(Combo, Free, Triggered)* — whenever Philotechnos uses Frame the Composition targeting Nykhemera specifically, her next successful attack this round also inflicts Terrifying Presence on every enemy who witnesses it, even those who've already resisted it once this scene.
- **Gallery of Endings** *(Combo, 2 Ichor shared pool, Main Action)* — Philotechnos uses Distort the Gallery at the same moment Nykhemera unleashes Sixfold Assault; resolve both together, with an extra +1 damage per successful strike against any target hit by both.
- **Mercy's Edge, Witnessed** *(Combo Deed, Once Per Session, 4 Ichor)* — Philotechnos narrates Nykhemera's use of Mercy's Edge: The Final Composition as it happens; if the attack reduces the target to 0 Flesh, every enemy who witnessed it must succeed on a Willpower check or become Weakened for the rest of the encounter, and Nykhemera immediately regenerates to full Flesh.

## GM Notes

- **Nykhemera is a predator, not a brute.** Keep her mobile and surgical — repositioning, exploiting openings, finishing wounded targets rather than trading blows head-on.
- **Philotechnos should never feel like a healbot.** His support is about framing, misdirection, and narrative weight, not raw numbers.
- **Separating them changes the fight.** If Philotechnos is removed, Nykhemera loses her Ichor-regeneration synergy and her combo abilities entirely — isolating him is a legitimate tactical goal.
- **Their marriage should feel genuinely strange, not villainous camp.** He is calm, articulate, and sincere in calling her work art; she is quiet and controlled rather than gleeful. Played straight, the horror should come from how reasonable they both sound.

---
*Asterion Unified Rulebook · Divine Boss & Support Deity Statblock · Game of Gods*
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Egyptian Sphere — Bast
# ────────────────────────────────────────────────────────────────────────────

add(
    "Bast, Bearer of Ra's Light", "character", "ally", "NPCs/Egyptian Sphere",
    "Egyptian Sphere, protection, guardian, three forms, inherited spark",
    "Guardian deity with three interchangeable forms who carries an inherited fragment of the fallen sun-god Ra's authority.",
    """
*Guardian deity statblock — three forms, universal abilities, and passive nature*

## Core Profile

**Domains:** Protection, cats, vigilant guardianship, music, and inherited sunfire.

**Origin (Asterion premise):** Ra fell during the Primordial War, but bestowed the Light of Ra upon Bast before his death — an inherited mantle, not a claim to his throne.

**Temperament:** Affectionate with those under her protection, observant, playful when safe, and merciless toward predators who mistake gentleness for weakness. Her goal is to keep the last honest warmth of Ra's light alive in Asterion — protecting sanctuaries, children, travelers, and small communities from primordial remnants and tyrants alike.

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Domain-Level Guardian Deity | Egyptian Sphere — inheritor of a fallen sun god's mantle |
| Domain Pool | 3d10 | Any action tied to protection, cats, or the Light of Ra |
| Flesh | 10 | Carries over unchanged between all three forms |
| Spark Shield | 3 | Regenerates fully at the start of every encounter |
| Ichor | 6 | Fuels form abilities and the inherited Light of Ra |
| Movement | 30 ft / 6 hexes | Varies by form — see individual form stats |

## Universal Abilities (All Forms)

- **Warmth of the Last Sun** *(Expression, free)* — her presence gives off gentle gold light and warmth; no mechanical benefit.
- **Radiant Ward** *(Active, 1 Ichor)* — conjures a 5-ft wall or 15-ft circle of sunlight for one scene; blocks movement by hostile shadowy or undead creatures but deals no damage.
- **Last Dawn of Ra** *(Active Deed, Once per session, Tier 3)* — unleashes her inheritance at full strength across a 60-foot arena. Hostile corruption and darkness are exposed and burned away, every ally who acts to defend another regains 2 Flesh, and the ground becomes consecrated sanctuary until the scene ends.
- **Nine Lives** *(Passive)* — the first time her Flesh would drop to 0 in a given session, she instead drops to 1 Flesh and remains standing, her body catching itself like a falling cat.

## Passive Nature (Always Active, Any Form)

- **Eyes of the Nine Thousand Cats (Advanced Sense):** as a free action, can see and hear through the eyes and ears of any cat she has touched or blessed, holding up to 3 such bonds at once. Actively spying for a meaningful advantage counts as a Stunt costing 1 Ichor.
- **Voice the Cats Obey:** every ordinary and wild feline within earshot recognizes her authority — they will not attack her, flee from her, or refuse a simple command (watch, follow, hide, distract) so long as it does not require fighting armed opponents.
- **The Spy in Every Alley:** because cats are everywhere in Asterion, she is treated as having a standing informant network in any district with a notable stray or temple-cat population. Once per scene, the GM will honestly answer one question about who has recently passed through or lingered there.

## Three Forms

### Cat Form — The Shrine Shadow

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Attack Pool | 2d10 | Claw strikes and swift ambush attacks |
| Defense Pool | 3d10 | Feline reflexes make her extremely hard to pin down |
| Movement | 40 ft / 8 hexes | Small, low, and fast |
| Armor | 0 | |

A small sacred black cat, nearly silent and easily overlooked, whose gold eyes catch lies and threats before they arrive. **Silent Prowl (Passive):** superhuman hearing and scent; in darkness or crowds she can move without ordinary notice. **Rooftop Gait (Passive, Special Movement):** climb speed and balances on any ledge or wire without a roll. **Sunlit Pawprint** *(Active, 1 Ichor)*: reveal hidden doors, concealed hostile intent, or the safest route through the current scene.

### Human Form — The Lady of the Quiet Hearth

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Attack Pool | 2d10 | Prefers avoiding combat in this form |
| Defense Pool | 2d10 | Standard divine resilience |
| Movement | 30 ft / 6 hexes | Ordinary walking pace |
| Armor | 0 | |

Her diplomatic and devotional shape: a regal guardian who offers calm, counsel, and sanctuary without surrendering authority. **Hearthkeeper's Grace (Passive):** allies who accept her protection gain +1d10 on their next defense or social roll this scene. **Sistrum of Reassurance** *(Active, 1 Ichor)*: end fear-driven panic in a small group and establish a temporary truce among willing parties. **Word of the Guardian** *(Active, 1 Ichor, 30 ft)*: a binding social command — one mortal creature must openly state its true intentions once before it can act against her.

### Werecat Form — The Solar Huntress

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Attack Pool | 3d10 | Claws carry the fire of her inheritance |
| Defense Pool | 3d10 | Built for open battle |
| Movement | 35 ft / 7 hexes | Larger and heavier than her cat form, still swift |
| Armor | 1 | Sun-Forged Hide |

A towering feline guardian built for open battle, with claws that carry the bright, disciplined fire of Ra's legacy. **Claws of Daybreak** *(Active, 1 Ichor)*: melee, 2 fire damage; a successful hit leaves the target Blinded until its next turn. **Guardian's Leap** *(Reaction)*: move up to 30 ft to an ally being attacked and become the target instead. **Sun-Forged Hide (Passive, 1 Armor):** thickened fur and skin grant Armor against physical damage while in this form.

## Form Rules

Changing form is a free action once per turn when Bast is not Restrained or Stunned. Her Spark Shield, Flesh, Ichor, injuries, and ongoing conditions carry across all three forms without interruption. Universal Abilities and Passive Nature traits function identically in every form; form-specific abilities only function while she wears that shape. The Light of Ra abilities — Radiant Ward and Last Dawn of Ra — are Spark and Deed abilities and cannot be combined with other abilities through Divine Resonance.

## Image Generation Prompts

**Cat Form:** A sacred black Egyptian cat goddess, glossy obsidian fur with subtle bronze rosettes, luminous gold eyes and a small sun-disc sigil glowing at the brow. She sits on a sandstone shrine ledge in a moonlit ancient fantasy city, a thin halo of warm solar light around her, protective and watchful mood, detailed cinematic character portrait, vertical 2:3.

**Human Form:** Bast in her human divine form: an Egyptian goddess with warm brown skin, intelligent golden eyes, long black braided hair, elegant white linen and gold jewelry, carrying a ceremonial sistrum. A restrained sun-disc glow rests behind her head, ancient fantasy temple at dusk, protective and regal expression, detailed cinematic full-body character portrait, vertical 2:3.

**Werecat Form:** Bast in her werecat war form: a tall athletic feline humanoid goddess with sleek black fur, gold eyes, leonine ears and clawed hands, wearing layered Egyptian gold-and-linen battle regalia. The inherited Light of Ra blazes as a controlled golden solar aura around her, a ruined temple battlefield at sunrise, fierce guardian stance, detailed cinematic full-body character portrait, vertical 2:3.

---
*Asterion Statblock · Bast · Egyptian Sphere*
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Independent Powers — not tied to a single pantheon sphere
# ────────────────────────────────────────────────────────────────────────────

add(
    "Oramis, God of Secrets", "character", "villain", "NPCs/Independent Powers",
    "exiled god, secrets, cosmos-level, four forms, escalating campaign boss",
    "A four-form, eight-phase Ascendant Boss for The Exiled Archivist — an escalating campaign-length climax encounter, from a wendigo-like hunter to a bodiless Seraph of surveillance.",
    """
*Exiled Archivist · Seeker of The Truth · Cosmos-Level Ascendant Boss*

**Origin:** Exiled God | **Spark:** Secrets & The Truth | **Epic Deed:** The Sovereign's Echo | **4 Forms · 8 Phases** | **Role:** Escalating Campaign Boss

> "I am the Exiled Archivist who walked beyond the war's edge, seeking The Truth to rewrite this fragile stage."

Forms: **I — Antlered Hunger** · **II — Pallid Archivist** · **III — Labyrinthine Witness** · **IV — Seraph of the Panopticon**

## Form I — The Antlered Hunger

A wendigo-like hybrid of ruined god and Primordial carrion: corpse-pale skin stretched across a ribbed torso, black root-antlers, several hungry skull-faces on his shoulders, long clawed limbs, and red-black void sinew showing through torn flesh. The first consequence of treating secrets as food.

| Attack/Defense | Spark Shield | Flesh | Ichor | Armor | Resistances |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 3d10 / 3d10 | 12 | 20 | 10 | 2 | Psychic; −1 Success vs mundane physical |

**Passive — Hunger Aura:** within 30 feet, secrets spoken aloud taste like blood. Characters who deliberately speak a secret within the aura lose 1 die from their next Defender pool against him.

**Phase 1 — Scent of Confession** *(begins at full resources; hunts isolated prey, probes for fears rather than finishing fights)*
- *Antler Hook (Active, 0 Ichor, Melee):* 2 damage gore/claw; on a hit may pull the target 15 ft; 2+ net successes also Restrain.
- *Stolen Scent (Active, 1 Ichor, 100 ft):* opposed Willpower; on a hit, always knows the target's location (ignores cover/invisibility) and gains 1 bonus die attacking it this scene.
- *Carrion Leap (Reaction, 1 Ichor, 60 ft):* when a character reveals information, casts divination, or becomes isolated, leaps through shadow to an adjacent space and makes a basic claw attack; can pass through walls but not end inside one.
- *Feast on the Unspoken (Active, 2 Ichor, 15-ft burst):* all enemies defend; hits take 2 mental damage and Weakened for 1 round; Oramis restores 1 Flesh per target damaged (max 3).

**Phase 2 — The Starving Revelation** *(trigger: 10 Flesh or fewer — Spark Shield refreshes to 12; skull-faces open, hunts less cautiously)*
- *Gore the Memory* (replaces Antler Hook, 1 Ichor, Melee): 3 damage; target names a valuable memory or bond they fear losing and loses one ability logically tied to it until end of scene.
- *Many-Mouthed Chorus* (replaces Stolen Scent, 2 Ichor, 30-ft aura): failed Willpower = Stunned; success still applies Weakened. Cannot affect the same target twice per encounter.
- *Ribcage Gate* (replaces Carrion Leap, 1 Ichor): teleports up to 100 ft through his own torn chest; departure/arrival spaces become difficult terrain for 1 round.
- *Devour the Name (Active, 3 Ichor, Tier 3, Telegraphed):* next turn, attack within 100 ft, 4 conceptual damage bypassing Armor, and suppress the target's Spark/lineage name until end of their next turn.
- *Legendary Action — Skulk Between Heartbeats:* once per round after another creature's turn, move 30 ft without provoking opportunity attacks, or make a basic claw strike against a Weakened target.

## Form II — The Pallid Archivist

The starving body burns away. Oramis resumes a deceptive near-humanoid outline: a featureless pale mask, layered black robes, elongated ink-stained hands, and a body of floating dark geometric shards stitched with decaying yellow light. He no longer hunts flesh; he edits perception.

| Attack/Defense | Spark Shield | Flesh | Ichor | Armor | Resistances |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 3d10 / 3d10 | 20 | 16 | 16 | 1 | Immune: mind-reading, truth-compulsion, scrying, psychic |

**Passive — Aura of the Unspoken:** enemies within 30 feet cannot communicate with sound or telepathy; speech produces only silent breath, agreed hand signs still work.

**Phase 1 — Catalogue of Lies** *(full resources; sows doubt about targeting, distance, memory)*
- *Cognitive Rend (1 Ichor, 100 ft):* 2 mental damage; hit target Weakened until its next turn; target may offer a real secret to avoid damage (Oramis learns it).
- *Footnote Doppelgänger (1 Ichor, 100 ft):* writes a false duplicate; until his next turn, the next direct attack on Oramis hits the duplicate instead and misses; attacker defends Willpower or Blinded until end of next turn.
- *Redacted Step (Reaction, 1 Ichor):* teleport to another visible space within 60 ft before an attack resolves; if this creates total cover, the attack automatically fails.
- *Geometry of the Beyond (2 Ichor, 30-ft zone):* for 1 round, movement costs double, line-of-sight attacks lose 1 success, failed Willpower defenders are relocated anywhere in the zone.

**Phase 2 — The Tattered Script** *(trigger: 8 Flesh or fewer — Spark Shield refreshes to 20; mask cracks)*
- *Erase the Sentence* (replaces Cognitive Rend, 2 Ichor, 100 ft): 3 conceptual damage; hit target loses one active buff, stance, summon, or terrain advantage of Oramis's choosing.
- *False Initiative* (replaces Footnote Doppelgänger, 1 Ichor, 60 ft): opposed Willpower vs a creature that hasn't acted; on a hit its next Main Action delays to round's end, Oramis chooses who acts next.
- *Margin Walk* (replaces Redacted Step, Passive, 0 Ichor): ignores terrain, walls, and opportunity attacks while moving; once per turn may pass through a creature's space, forcing a defense or 1 mental damage.
- *Glimpse of the Truth (3 Ichor, Tier 3, Telegraphed):* next turn, 4 conceptual damage bypassing Spark Shield, target Stunned and Horrified for the scene unless resisted with divine mental fortitude or a ward.
- *Legendary Action — Editorial Correction:* once per round after a creature ends movement, shift it 10 ft in any safe direction, or remove a line of sight it just gained.

## Form III — The Labyrinthine Witness

The mask opens into a vertical void. Oramis expands into a floating, many-limbed archive-engine: a central black aperture surrounded by rotating shelves, ribs of bronze geometry, quill-like limbs, and hundreds of small eyes that open only when nobody looks directly at them.

| Attack/Defense | Spark Shield | Flesh | Ichor | Armor | Resistances |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 3d10 / 3d10 | 28 | 14 | 22 | 3 | Psychic/void −2 Successes; immune to concealment targeting |

**Passive — All Doors Remember:** cannot be flanked, surprised, or hidden from while any door, mirror, written surface, shadow, or reflective pool exists. Destroying/consecrating every anchor suppresses this until the end of next round.

**Phase 1 — The Secret Becomes a Place** *(full resources; maps the arena into private prisons)*
- *Archive Corridor (1 Ichor, 30-ft line):* targets defend or take 2 conceptual damage and are displaced to either end; attacks cannot cross the corridor except through its open ends until next round.
- *Witness Mark (1 Ichor, 100 ft):* marks a target for 2 rounds — always knows its location, gains 1 bonus die attacking it, may use a Legendary Action against it at any range.
- *Borrowed Ability (2 Ichor, 60 ft):* names an ability the target used this fight; Willpower defense, on a hit the target can't use it until end of next turn and Oramis may mimic a weakened version (max 2 damage or one soft status).
- *Shelves Close (2 Ichor, 15-ft radius):* 2 damage on a hit; failed targets Restrained in a book-cage until a physical or Willpower test succeeds.

**Phase 2 — The Witness Reads Back** *(trigger: 7 Flesh or fewer — Spark Shield refreshes to 28; shelves rotate into a vast eye)*
- *Retrospective Wound* (replaces Archive Corridor, 2 Ichor, 100 ft): 3 conceptual damage; hit target cannot restore Flesh or Spark Shield until end of its next turn.
- *Panoptic Chain* (replaces Witness Mark, 2 Ichor, 100 ft): chains link up to two seen targets; if both are hit, either moving voluntarily damages the other for 1 until broken with a physical/Willpower test.
- *Counterfactual Theft* (replaces Borrowed Ability, 3 Ichor, 60 ft): "What would you have done differently?" — on a hit, undoes one prior action's mechanical consequence (damage, movement, condition); does not erase plot-critical reveals.
- *Library of the Unlived (3 Ichor, Tier 3, Telegraphed):* every wall becomes a shelf, a named book appears for each character; next turn, 60-ft library arena for 1 round — failed defenses take 4 damage and Stunned, successes take 2 and are pushed to the edge.
- *Legendary Action — Index the Intruder:* once per round after a marked creature uses an ability, deal it 1 mental damage or learn one of its resistances/immunities/active defenses.

## Form IV — The Seraph of the Panopticon

Oramis abandons flesh, archives, and masks. His final body is a mathematically perfect engine of surveillance and cosmic law: a dark-gold singularity encircled by immense gyroscopic rings covered in lidless eyes, crossed by vast white and scorched-black wings, with flaming scales beneath weighing secrets against souls.

| Attack/Defense | Spark Shield | Flesh | Ichor | Armor | Resistances |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 3d10 / 3d10 | 80 | 0 (bodiless) | 150 | 5 | Immune: mental/psychic/void; immune to non-god/Primordial weapons |

**Passive — Omniscient Gaze:** cannot be flanked, surprised, or hidden from. Before acting against him, a character defends Willpower; on a failure Oramis predicts the action (gains 1 defense die or repositions a ring for cover). A deliberately created blind spot grants exemption while occupied.

**Phase 1 — Weighing the Soul** *(trigger: begins at 150 Ichor; no Flesh, so Phase 2 triggers at 75 Ichor instead — Spark Shield refreshes to 80; judges and redirects rather than annihilates)*
- *Unmaking Stare (2 Ichor, 100 ft):* 3 conceptual damage; if it hits a target whose Spark Shield is already 0, that target also loses access to one core ability for the rest of the fight.
- *Geometry of the Panopticon (2 Ichor, 60-ft zone):* for 1 round, creatures failing a Willpower defense move opposite their intent, ranged attacks curve back unless fired from a blind spot.
- *Scales of Disclosure (2 Ichor, 100 ft):* up to two targets — reveal a meaningful secret or lose 2 Ichor; refusal forces a defense, a hit deals 2 mental damage and applies Vulnerable until next turn.
- *Wing of the Closed Heaven (Reaction, 1 Ichor):* when an enemy would enter melee, cross a barrier, or teleport, a wing folds across the space; fails unless the creature wins an opposed Willpower roll — on failure it's pushed 30 ft and loses remaining movement.

**Phase 2 — The Written End** *(trigger: 75 Ichor or fewer — Spark Shield refreshes to 80; rings align, all wings open)*
- *Rewrite the Script* (replaces Geometry of the Panopticon, 4 Ichor, Global, Telegraphed): declares one bounded rule change for 1 round (e.g. "fire restores instead of harms," "shadows are solid"); cannot instantly kill or negate a party's whole concept.
- *True Name Reversed* (replaces Scales of Disclosure, 3 Ichor, 100 ft): 4 conceptual damage bypassing Armor; hit target loses one Resistance/immunity for the scene; a major sacrifice grants one reroll of the defense.
- *Blind Spot Extinguished* (replaces Wing of the Closed Heaven, 2 Ichor, Arena-wide): for 1 round, no creature benefits from cover or blind spots against Oramis; reliant creatures defend or Blinded until end of next turn.
- *Legendary Action — Eye Opens Where You Stand:* once per round after any creature's turn, open an eye in its space (defend or take 1 conceptual damage, lose Reactions until next round), or rotate a ring to create/remove one 10-ft blind spot.

## Epic Deed

**The Sovereign's Echo** *(8 Ichor, Tier 3, Telegraphed)* — all rings stop, every eye closes, wings part to expose the core. Next turn: every enemy in line of sight defends. Hits take 4 catastrophic damage bypassing Armor/Spark Shield and are marked by the outer dark (a permanent narrative scar); successes take 2 bypassing Armor.

## GM Levers

- **A negotiated ending:** Oramis can be delayed, redirected, or made to reveal a vital truth only by an exchange he regards as genuinely unequal in his favor — a secret that would ruin a god, an anchoring memory, or the truth of why the Nameless God fears a second Godhand.
- **A discoverable weakness:** the party can create blind spots by destroying eye-rings, consecrating mirrors and written surfaces, or stating a truth Oramis does not already know. Reward this with temporary suppression of a passive, not a flat damage bonus.
- **A fair finale:** in Form IV, telegraph every reality rewrite in plain language before it applies — the horror comes from adapting to an explicit impossible rule, not from concealed rules.
- **Form transition:** reducing a form to 0 Flesh (or 0 Ichor for Form IV) does not kill Oramis. Describe its dissolution, allow one short repositioning beat, then introduce the next form at full listed resources.

---
*Asterion Unified Rulebook · Cosmos-Level Ascendant Boss Sequence · Game of Gods*
""",
)

add(
    "The Ice Queen of the Winter Court", "character", "villain", "NPCs/Independent Powers",
    "True Fae, cosmos-level, cold, void, no ichor, King in Yellow",
    "A True Fae exile of the bloodline of Cassilda of Yhtill — commands cold as stillness and domination, with the crushing dark of the Void beneath.",
    """
*True Fae of Yhtill · Heir to a Stolen Fragment of the King in Yellow · Cosmos-Level Threat, Rank 4 · No Ichor — Innate, Unlimited Fae Power · 12 Powers (2 Passive)*

**Domains:** Cold, Stasis, the Cold Void.

> "I am not cruel because I am cold. I am cold because cruelty deserves patience."

## Core Identity

The Ice Queen is not a goddess and carries no Ichor, no divine mantle, and no borrowed domain — she is a True Fae of the bloodline of Cassilda of Yhtill, exiled from the hidden pocket realm of New Yhtill for being too cruel and too uncontrolled even for her own kin. Her power is not elemental magic she learned; it is her nature, inherited from Cassilda's doomed theft of a fragment of the King in Yellow's authority. She commands cold as an expression of stillness and domination, and beneath that ice lies something colder still: the airless, crushing dark of the Void between stars, where nothing moves and nothing is forgiven.

## Boss Profile

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Cosmos-Level Threat, Rank 4 | True Fae exile, not bound to any pantheon |
| Dice Pool | 4d10 | Reality bends slightly around her intent even at rest |
| Flesh | 30 | Her body was never the source of her danger |
| Power Source | Innate — No Ichor Cost | Every power is used freely, limited only by frequency |
| Spark Shield | 45 | The residue of a stolen royal authority, not a divine gift |
| Armor | 2 | Her form is subtly less "real," harder to fully harm |
| Movement | 30 ft / 6 hexes (or instant, see Step Between Frost) | Physical movement is optional, not required |

**Resistances:** Absolute Immunity to Fear, Charm, and Mind-Reading. Greater Resistance (2 Attacker Successes) to any effect that would alter her identity, name, or form against her will.

**On Power Frequency:** because she has no Ichor pool, her abilities are gated by tags — **At-Will** (any number of times), **Once Per Round** (one such power per round), and **Once Per Encounter/Session** (her largest, reality-breaking powers).

## Passive Powers

- **Aura of Absolute Zero:** all creatures within 60 feet have Movement halved and suffer -1 die on any roll made to act first in turn order; always active, costs nothing.
- **Unbroken Sovereignty:** as a True Fae, cannot be Feared, Charmed, mind-read, or emotionally compelled by any means short of a power explicitly built to affect Fae nobility. Deception or intimidation checks against her are rolled at double difficulty.

## Active Powers — Cold

- **Black Frost Lance** *(At-Will, Ranged 60 ft)* — 2 Cold damage and Slow (movement halved) for 1 round on a hit.
- **Stasis Grip** *(At-Will, Melee or 30 ft)* — target fails a Physical save = Restrained for 1 round, taking 1 Cold damage per round while held.
- **Court of Black Ice** *(At-Will, 20-ft radius)* — raises walls, thrones, or spikes of black frost; creates difficult terrain and can wall off up to two 10-foot sections (Flesh 8, Armor 0 to break).
- **Silence the Room** *(Once Per Round, 30-ft radius)* — all sound dies for 1 round; affected creatures cannot speak or use verbal abilities, and lose any surprise/advantage bonus.
- **Frozen Instant** *(Signature Power, Once Per Encounter, 60-ft radius)* — time stops on her stillness. Every creature other than her is Frozen (no actions, reactions, or movement) for exactly 1 full round while she acts freely. Divine or Primordial-tier Willpower creatures may attempt a save at the end of the round to act one turn early once time resumes.

## Active Powers — The Cold Void

- **Void Maw** *(Once Per Round, 20 ft)* — a small tear in reality opens; creatures within 20 feet fail a Physical save or are dragged 10 feet toward it and take 2 Void damage bypassing Armor.
- **Fracture the Name** *(Once Per Round, 30 ft)* — target fails a Willpower save or forgets one core detail of their own identity for the rest of the scene, taking psychic damage bypassing Spark Shield.
- **Edict of Winter** *(Signature Power, Once Per Encounter, 60-ft radius)* — speaks a single sentence of absolute law that becomes true within the radius for 1 round (e.g. "No one may run"); mechanically enforced as a Hard Crowd Control effect against violators.

## Epic Deed

**Event Horizon** *(Once Per Session, 90-ft radius)* — unmakes a point in space into a true, momentary black hole. All creatures in the radius fail a Physical save or are pulled to its center, taking massive Void damage bypassing Armor and Spark Shield entirely; catastrophic failures are held Restrained at the singularity's edge until the hole collapses at the end of her next turn, dealing its damage a second time to anyone still trapped.

## Reactions

- **Step Between Frost** *(Reaction/Active, At-Will)* — teleport up to 60 feet, ignoring all terrain and barriers, usable even as a Reaction to avoid an incoming attack entirely.
- **Glacial Composure** *(Reaction, At-Will)* — when she takes damage, negate any Stagger, Stun, or forced-movement effect from that hit entirely.

## GM Notes

- **She never runs out of power — only patience.** Since she has no Ichor pool, gate her biggest effects (Frozen Instant, Edict of Winter, Event Horizon) strictly by the Once Per Encounter/Session tags rather than resource costs.
- **Cold powers are her mask; Void powers are her truth.** Consider only letting her use Void Maw, Fracture the Name, Edict of Winter, and Event Horizon once her composure is genuinely broken or she decides the mask no longer serves her.
- **Frozen Instant and Event Horizon should feel wrong.** Describe them as violations of natural law, not spells.
- **Names matter.** A PC's spoken promise, vow, or careless nickname used near her can become genuine narrative leverage for Fracture the Name.

---
*Asterion Unified Rulebook · True Fae Cosmos-Level Threat · Game of Gods*
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Player Character — Morrigan
# ────────────────────────────────────────────────────────────────────────────

add(
    "Morrigan", "character", "PC", "Player Characters",
    "War, Fate, Celtic, Mythborn, damage dealer, battlefield controller",
    "Mythborn Celtic war-goddess and omen-bearer, a frontline damage dealer and fate-manipulator who witnessed the Nameless God's rise.",
    """
*Mythborn · Celtic War-Goddess and Omen-Bearer · Divine Spark of War and Fate*

- **Lineage:** Mythborn — daughter of an ordinary god and a primal mythical creature
- **Divine Spark:** War and Fate
- **Epic Deed / Curse:** Threadsight and the Unweaving
- **Reputation:** Feared, magnetic, and impossible to ignore — a living omen of bloodshed and revelation
- **Role:** Frontline damage dealer, battlefield controller, fate-manipulator
- **XP Spent:** 37 XP · 0 remaining

> "War strips away illusion. It shows what gods, monsters, and mortals truly are when death is close enough to taste — and I have always been close enough to taste it."

## Character

Morrigan fought through the ancient war against the Primordials, a cataclysm that shattered the Celtic pantheon and left her carrying a wound that nearly split her in two. She alone saw the truth buried beneath that slaughter: a mortal man carving through gods and Primordials alike, the man who would become the Nameless God of War. That memory never left her, and neither did the scar.

She is loud, obscene, and gloriously alive — a goddess who refuses to dress war up in noble lies. But beneath the bravado is something sharper: an omen-bearer who reads the threads of fate itself, and who has learned, at great personal cost, that she can occasionally pull on one.

## Core Statistics

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Standard Pool | 2d10 | General actions, attacks, defenses, contested checks. |
| Domain Pool | 3d10 | Any action tied to War, Fate, battle-prowess, or reading/altering destiny. |
| Spark Shield | 3 | Absorbs damage before Flesh; refreshes at the start of each combat. |
| Flesh | 5 | Standard maximum — no reduction, no increase purchased. |
| Ichor | 7 | Increased from base 5 by two 1 XP/2 XP stat purchases. |
| Movement | 30 ft / 6 hexes | Standard movement; no special movement trait purchased. |

## Starting Ability 1 — Lineage: Battle-Born Bloodline
**Passive · Tier 1**

Half-divine, half-primal, Morrigan's mixed nature gives her senses no purely mortal or purely divine being possesses. She feels violence coming the way others feel a change in weather.

**Superhuman Sense — Death-Sense:** always instinctively aware of nearby lethal intent, imminent violence, or a creature about to die within 100 feet, even without seeing or hearing it directly. Cannot be caught flat-footed by an ambush originating from a mortal or lesser threat.

*No Trade-Off taken — kept at base Tier 1.*

## Starting Ability 2 — Divine Spark: Reaper's Onslaught
**Active · Tier 2 · Cost: 1 Ichor**

Morrigan closes the distance in an eyeblink and strikes with the full weight of a battlefield's worth of fury behind a single blow.

1. Spend 1 Ichor and use a Main Action.
2. Choose one target within melee range.
3. Roll 3d10 (Domain Pool) against the target's Defender pool.
4. On a hit, deal 2 base damage plus net Successes (Tier 2 Base Damage property).
5. The target is shoved 5 feet by the force of the blow (Tier 1 Special Effect property).

*No Trade-Off taken — kept at base Tier 2.*

## Starting Ability 3 — Epic Deed / Curse: Threadsight and the Unweaving
**Two-Part Ability · Custom Limitation (GM-approved)**

Morrigan does not merely witness fate — she can read it, and, at great and lasting cost, occasionally rewrite it. Two linked parts: **Threadsight** (the reading) and **the Unweaving** (the changing).

### Part One — Threadsight (Reading Fate)
**Active · Tier 1 · Cost: 1 Ichor**

Spend 1 Ichor and use a Main Action. Touch or target one *willing* character within 100 feet (upgraded from touch-range via 3 XP). Roll 1d10 on the Fate Omen Table:

| Roll | Omen | Meaning |
| :--- | :--- | :--- |
| 1–2 | Doom | The target is fated to die soon, unless something changes. |
| 3–4 | Shadow | A hidden danger or betrayal approaches the target. |
| 5–6 | Balance | The threads are tangled and unclear — a vague, ambiguous vision. |
| 7–8 | Fortune | A hidden advantage or opportunity awaits the target soon. |
| 9 | Glory | The target is fated for a moment of great triumph. |
| 10 | Convergence | Two starkly opposed fates are visible at once — reroll once and reveal both results. |

### Part Two — The Unweaving (Changing Fate)
**Active Epic Deed · Tier 3 · Cost: 3 Ichor**

May only target a character whose fate Morrigan has already read with Threadsight. Spend 3 Ichor and use a Main Action. Choose to either **weave** a favorable turn or **unweave** an unfavorable one: grant one guaranteed full success on a single future roll of their choice within the next month, OR impose one guaranteed failure on a single future roll of theirs within the next month.

**Trade-Off — Once Per Character (Custom Limitation):** may only use the Unweaving on any single specific character once, ever — a permanent, narrative-tracked restriction.

**Trade-Off — The Fate-Scar (Self-Harm, extended):** immediately after using the Unweaving, Morrigan permanently loses 1 point of maximum Flesh and 1 point of maximum Ichor for a full week of in-game time; these cannot be restored by Short Rest, Long Rest, Ambrosia, Golden Apples, or any other normal means — only the passage of the full week restores them.

## Purchased Ability — Reaping Storm
**Active · Tier 3 · XP Cost: 10 · Cost: 3 Ichor**

Morrigan unleashes the full scope of the slaughter she witnessed at the war's end — a screaming storm of ravens, blood-omens, and battle-fury.

1. Spend 3 Ichor and use a Main Action.
2. Choose a point within 100 feet. A 60-foot radius storm of ravens and battle-omens erupts.
3. Roll 3d10 as a single Attacker roll; every enemy in the area rolls its own Defender pool.
4. Creatures that fail take 2 damage plus net Successes and must flee directly away from Morrigan for their next turn.

*No additional Trade-Off — fully paid in XP and Ichor.*

## Purchased Ability — Raven Swarm
**Active · Tier 2 · XP Cost: 7 · Cost: 1 Ichor**

A shrieking cloud of ravens bursts from Morrigan's shadow and descends on a distant target. Spend 1 Ichor, Main Action, target within 100 feet, roll 3d10 against Defender pool; on a hit, the target becomes Blinded for 1 turn.

## Purchased Ability — Battle-Sense of the Slain
**Passive · Tier 2 · XP Cost: 7**

Having watched a battlefield's worth of gods die in a single afternoon, Morrigan reads the ebb and flow of violence the way a sailor reads the tide. **Advanced Sense:** senses the general intentions of nearby creatures mid-combat (flee, attack, feint) before they act, GM-adjudicated. **Minor Movement Trait:** treats difficult battlefield terrain (rubble, corpses, broken ground) as normal terrain.

## Purchased Ability — What the Blade Could Not Finish
**Passive · Tier 2 · XP Cost: 7**

When the man who would become the Nameless God of War cut through her in the slaughter that ended the Celtic pantheon, the wound should have killed her. Instead, Morrigan pressed the halves of herself back together and refused to finish dying. That same stubborn will lives in her flesh still — as long as her body remains one connected whole, no wound closes for good.

**Automatic Trigger:** at the start of each of her turns, as long as her body remains physically whole and connected, spend 1 Ichor to restore 2 Flesh automatically (no action required, unless she suppresses it). Functions even from wounds that would kill a lesser being, so long as no piece of her has been fully separated.

**Trade-Off 1 — Specific Condition:** stops entirely the moment any part of her body is fully severed or separated; regeneration does not resume until she spends a full Main Action to physically rejoin the piece.

**Trade-Off 2 — Self-Harm:** each trigger costs 1 Ichor; if she has none remaining, the regeneration simply does not occur that turn.

*This ability carries 2 of the maximum 3 Trade-Offs.*

## XP Ledger

| XP | Purchase | Result |
| :--- | :--- | :--- |
| 10 | Invent new Tier 3 ability | Reaping Storm — signature battlefield-wide AoE damage and fear effect. |
| 7 | Invent new Tier 2 ability | Raven Swarm — ranged Blinded crowd control. |
| 7 | Invent new Tier 2 ability | Battle-Sense of the Slain — passive combat awareness and terrain mastery. |
| 3 | Upgrade existing ability | Threadsight gains Range 100 ft (Tier 2 property), no longer touch-only. |
| 1 | Stat upgrade | Maximum Ichor increased from 5 to 6. |
| 2 | Stat upgrade | Maximum Ichor increased from 6 to 7. |
| 7 | Invent new Tier 2 ability | Blood-Price Renewal — battlefield self-healing drawn from wounded/dying enemies. |
| **37** | **Total spent** | **0 XP remaining** |

*Note: the ledger's last row names the purchase "Blood-Price Renewal" with flavor text about draining wounded/dying enemies, but the ability written up above under that same 7 XP/Tier 2 passive slot is "What the Blade Could Not Finish" (regeneration while whole, not draining others) — transcribed exactly as the source sheet presents both, discrepancy and all.*

## Trade-Off Summary

| Ability | Trade-Offs Taken | Effect |
| :--- | :--- | :--- |
| The Unweaving (Epic Deed) | 2 of 3 | Usable only once per specific character, ever; permanently costs 1 Flesh + 1 Ichor for a full week, unrestorable by normal means. |
| Battle-Born Bloodline | 0 of 3 | Kept at base Tier 1 — no Trade-Off needed. |
| Reaper's Onslaught | 0 of 3 | Kept at base Tier 2 — no Trade-Off needed. |
| Reaping Storm / Raven Swarm / Battle-Sense | 0 of 3 each | Fully funded by XP and Ichor cost alone. |
| What the Blade Could Not Finish | 2 of 3 | Stops entirely if any part of her body is fully severed, until manually rejoined; costs 1 Ichor every time it triggers. |

## Play Notes

In combat, Morrigan opens with Reaper's Onslaught or Raven Swarm depending on range, then escalates to Reaping Storm once enemies cluster together — her kit rewards aggressive positioning and battlefield control rather than caution. Battle-Sense of the Slain lets her call out enemy intentions, making her a natural tactical anchor for the party even when she isn't the one taking hits.

Threadsight should be used liberally in downtime and social scenes — reading a willing ally's or NPC's fate is cheap, safe, and a strong roleplay hook. The Unweaving, by contrast, should be treated as a genuine last resort: the GM and player should track exactly which characters have already had their fate changed, and the week-long unrestorable Flesh/Ichor loss should meaningfully affect her performance in whatever follows.
""",
)

print(f"Batch2 part 1 built: {len(entities)} entities so far")

# ────────────────────────────────────────────────────────────────────────────
# Shared rules note — High God Dossier mechanics (de-duplicated across all
# 11 pantheon-sphere "Gods of Asterion" / "Divine Figures of Asterion" files,
# which each repeat this boilerplate near-verbatim)
# ────────────────────────────────────────────────────────────────────────────

add(
    "High God Dossiers — How to Use", "note", "lore", "Rules",
    "high god, dossier, pantheon sphere, asterion seat, mythic deed",
    "How to run the pantheon-sphere High God dossiers: Domain action, Asterion Seat, and Mythic Deed.",
    """
Every pantheon sphere (Olympian, Egyptian, Norse, Indic, Sino-Japanese, Mesopotamian, Celtic, Slavic, Yoruba, Mesoamerican, and the Syncretic Fringe) has a roster of High God dossiers — see the "Pantheon/&lt;Sphere&gt; Sphere" folders under Characters. This note covers the mechanics shared by all of them, so it isn't repeated on every entry.

## What These Are

High God dossiers are narrative-tier figures, not routine combat opponents. Use them as patrons, faction leaders, rare apex appearances, and mythic confrontations — not as a fight to be resolved in a normal encounter. Each god has a reliable **Asterion Seat** for recurring scene play and a **Mythic Deed** intended to reshape a major confrontation or story beat.

## Domain Action

Use the Domain Pool (3d10) when an action strongly serves the god's listed Domains; otherwise use the standard divine Base Pool (2d10).

## Seat vs. Mythic Deed

- **Asterion Seat:** a recurring, repeatable scene effect. Use it whenever the fiction calls for it.
- **Mythic Deed:** a once-per-session intervention meant to reshape a major conflict or story turn, and should leave visible narrative consequences.

## A Note on Framing

Several of these dossiers are drawn from real-world mythological and religious traditions, adapted freely for this fictional gladiator-city setting. Play them as Asterion's own fictionalized divine politics — a deliberate remix, not a claim about any living faith or devotional practice. The Syncretic Fringe sphere in particular is an explicit Asterion setting construct (a fictional "contact zone" pantheon), not a historical one.
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Norse Sphere — pantheon dossier + bestiary (handled directly, not delegated)
# ────────────────────────────────────────────────────────────────────────────

_norse_gods = [
    ("Odin", "Allfather of the Gallows", "Wisdom, war, death, poetry, magic",
     "Cunning, relentless, and knowledge-hungry; he seeks foreknowledge and power regardless of cost.",
     "Raven Ledger: Ask one question about a visible foe's intent or weakness; the answer is true but may be cryptic.",
     "Price of Prophecy: Once per session, Odin declares a coming disaster. He and his allies gain decisive preparation against it, but Odin must sacrifice an asset, ally, or certainty."),
    ("Thor", "Shield of the Storm Road", "Thunder, storms, strength, protection",
     "Direct, bold, and protective; he crushes threats and defends gods and mortals.",
     "Hammerfall: Deal 2 lightning damage in a 15-ft burst and push targets 10 ft.",
     "Stormbreaker: Once per session, Thor calls a battlefield storm that shatters a major obstacle or enemy formation and grants allies immunity to forced movement for the scene."),
    ("Freyja", "Lady of the Fallen Gold", "Love, fertility, magic, battle-death",
     "Passionate, self-possessed, and formidable; she gathers beauty, magic, and honored dead.",
     "Seidr Thread: Link two creatures in 30 ft; when either suffers damage, the other may move 10 ft or gain +1d10 on its next roll.",
     "Half the Slain: Once per session, Freyja claims the fallen: allies who were Shattered this scene return with 2 Flesh, but owe her a future service."),
    ("Frigg", "The High Seat Veiled", "Motherhood, foresight, sovereignty",
     "Composed, perceptive, and secretive; she secures family continuity and the high house.",
     "Foreseen Step: Once each round, let an ally reroll a failed Defender die after seeing the result.",
     "The Fate Not Spoken: Once per session, prevent one foretold disaster, execution, or betrayal—but a different unresolved cost becomes inevitable."),
    ("Loki", "The Unfastened Knot", "Mischief, chaos, change",
     "Sly, restless, and destabilizing; he breaks limits and exposes weakness through trickery.",
     "Borrowed Shape: Assume the apparent form and voice of a nearby creature until directly challenged.",
     "The Joke Becomes True: Once per session, turn one absurd lie or improvised claim into temporary reality for the scene."),
    ("Tyr", "The One-Handed Oath", "Law, justice, sacrifice, heroic courage",
     "Brave, restrained, and principled; he upholds justice even through loss.",
     "Binding Hand: Mark a willing oath; the signer gains +1d10 while keeping it and becomes Weakened if they knowingly break it.",
     "The Cost of Justice: Once per session, Tyr accepts a permanent loss, wound, or obligation to bind an enemy, end a feud, or enforce a verdict."),
    ("Baldr", "The Unwounded Light", "Light, purity, peace",
     "Radiant, gentle, and idealized; he seeks to preserve peace and innocence.",
     "Gentle Radiance: Allies in 15 ft end Blinded or Weakened; hostile creatures there cannot make opportunity attacks.",
     "Return of the Beloved: Once per session, Baldr brings one lost hope back into the scene—a destroyed refuge, broken pact, or fallen ally returns in a fragile but real form."),
    ("Njord", "Master of the Favorable Wind", "Sea, wind, wealth",
     "Calm, prosperous, and practical; he ensures wealth, safe voyages, and abundance.",
     "Fair Wind: Allies gain swift travel across water, rooftops, or open ground; their next movement ignores difficult terrain.",
     "Harbor of Plenty: Once per session, make a district, voyage, or expedition prosper: supplies arrive, a route opens, and hostile weather is held back for a story arc."),
    ("Freyr", "The Peaceful Harvest", "Fertility, prosperity, peace, harvest",
     "Generous, peace-seeking, and fertile; he brings growth and good seasons.",
     "Green Truce: Plants and grain rise in 15 ft, providing cover; creatures who enter may choose to end a hostile stance until next turn.",
     "Golden Season: Once per session, end scarcity in a community or secure a lasting truce between rival factions willing to accept shared abundance."),
    ("Hel", "Queen Below the Line", "Underworld, the dead",
     "Detached, patient, and unflinching; she rules the dead without illusion.",
     "Cold Welcome: A target at 0 Spark Shield cannot restore Flesh until it leaves Hel's 30-ft presence or receives a sincere act of mercy.",
     "No Door Back: Once per session, seal a deathly boundary: no resurrection, escape, or summoning crosses it until dawn."),
    ("Heimdall", "Watcher at the Last Bridge", "Guardianship, vigilance, thresholds",
     "Alert, dutiful, and uncompromising; he detects threats before they arrive.",
     "Gjallarhorn Warning: Allies cannot be surprised, and Heimdall reveals one concealed entrance, ambush, or infiltrator in the scene.",
     "The Bridge Holds: Once per session, make one bridge, gate, line, or boundary impossible for enemies to cross until allies choose to release it."),
    ("Skadi", "Huntress of the High Cold", "Winter, mountains, hunt",
     "Hard-edged, solitary, and resilient; she keeps the cold wilds respected.",
     "Ice Trail: Mark a quarry; it leaves frost tracks visible to Skadi, and its speed is reduced by 10 ft while on open ground.",
     "White Silence: Once per session, a blizzard seals the scene: enemies are Blinded beyond 15 ft while Skadi and chosen allies move without penalty."),
]

for name, epithet, domains, temperament, seat, deed in _norse_gods:
    add(
        name, "character", "High God", "Pantheon/Norse Sphere",
        f"Norse, high god dossier, {domains.split(',')[0].strip().lower()}",
        f"{epithet} — {domains}.",
        f"""
*{epithet}*

**Domains:** {domains}

{temperament}

## Asterion Seat

{seat}

## Mythic Deed

{deed}
""",
    )

_norse_creatures = [
    ("Fenrir-Blood Wolf", "Elite", "3d10", "4", "10", "5",
     "Oath-Scent: gains +1 die against a target that broke a promise this scene. Moon-Chain Pounce: 2 damage and Restrained.",
     "A silver-grey dire wolf with rune-scars beneath its fur; its handler must carry a chain made of impossible things."),
    ("Draugr Shield-Breaker", "Elite", "3d10", "4", "11", "4",
     "Grave-Cleave: 2 physical damage, ignores 1 Armor. Dead Man's Refusal: first time reduced below half Flesh, immediately makes a free attack.",
     "An undead Norse champion armored in corroded mail, built for hard-fought duels rather than mindless swarms."),
    ("Nidhogg Broodwyrm", "Standard", "2d10", "2", "6", "2",
     "Root-Rot Bite: 1 damage and Burning/poison-like decay. Tunnel Through Stone: move through arena rubble or floor cracks.",
     "A juvenile black serpent-dragon from beneath the world-tree; excellent in pairs or with crumbling terrain."),
    ("Hel-Hound", "Standard", "2d10", "3", "7", "2",
     "Grave Howl: enemies within 15 feet lose 1 die on their next attack. Ashen Bite: 1 damage and cannot be healed until next turn.",
     "A lean black hound with frost smoke leaking from its ribs and pale blue corpse-fire eyes."),
    ("Iron-Tusk Jotunn Boar", "Elite", "3d10", "5", "12", "4",
     "Avalanche Charge: 2 damage, pushes 15 feet, then creates difficult terrain. Iron Hide: Armor 2.",
     "A colossal mountain boar fitted with broken giant-forged plates, used to smash cover and scatter formations."),
    ("Lindworm of the Rune Pits", "Boss", "3d10", "7", "18", "9",
     "Coil and Crush: 2 damage, Restrained. Venom Spit: 60 feet, 2 damage and Poison. Below half Flesh: Rune-Shed refreshes Shield and leaves a damaging shed skin.",
     "A wingless two-legged dragon with a long serpent body, chained under the arena until the gate rises."),
    ("Mare of the Night Road", "Elite", "3d10", "4", "9", "6",
     "Dream Gallop: teleports 30 feet between shadows. Nightmare Breath: 15-foot cone, Weakened on failed Willpower save.",
     "A black spectral horse with a mane of cold blue flame; it runs along walls and across the arena's ceiling."),
    ("Valkyrie-Steed Descendant", "Elite", "3d10", "5", "10", "5",
     "Sky Spear-Dive: 2 damage in a 30-foot line. Wing Ward: reaction, gains +2 dice to one Defense roll.",
     "A massive winged warhorse descended from the mounts of chooser-spirits, proud enough to become a recurring rival."),
    ("Fafnir's Gold-Scale Whelp", "Boss", "3d10", "8", "20", "10",
     "Greed-Fire Breath: 15-foot cone, 2 fire damage and Burning. Hoard Instinct: gains +1 die while within 10 feet of a valuable object. Below half Flesh: Curse of Gold makes nearby metal scorch.",
     "A low, brutal dragon with molten gold scales and a mind sharpened by hoarded wealth."),
    ("Garmr, Gate-Hound of the Deep", "Boss", "3d10", "7", "19", "8",
     "Chainbreaker Bite: 3 damage against Restrained targets. World-End Bark: telegraphed; 30-foot radius Stunned on failure. Legendary Action: move or bite after taking damage.",
     "A colossal blood-red wolf-hound whose black iron chains snap one link at a time as the bout escalates."),
    ("Raven of the Gallows Wind", "Standard Swarm", "2d10", "2", "5", "0",
     "Omen Peck: 1 damage and target cannot use Reactions until next turn. Flock Escape: ignores opportunity attacks.",
     "A murder of oversized black ravens that circles above the pit, distracting champions before a larger beast attacks."),
    ("Frost Jotunn Arena-Brute", "Boss", "3d10", "8", "22", "8",
     "Rime Club: 3 physical/cold damage. Ice Stomp: 15-foot burst, prone or Restrained. Below half Flesh: Blizzard Skin gives cold resistance and obscures sight.",
     "A towering blue-grey giant fitted with arena shackles and a glacier-stone maul; a crowd-favorite brute-force finale."),
]

for name, tier, attack, shield, flesh, ichor, sig, flavor in _norse_creatures:
    add(
        name, "creature", tier, "Bestiary/Norse Sphere",
        f"Norse, {tier.lower()}, arena bestiary",
        flavor,
        f"""
| Stat | Value |
| :--- | :--- |
| Attack Pool | {attack} |
| Spark Shield | {shield} |
| Flesh | {flesh} |
| Ichor | {ichor} |

## Signature Abilities

{sig}

{flavor}
""",
    )

add(
    "Norse Arena Bestiary — Running Notes", "note", "lore", "Bestiary/Norse Sphere",
    "Norse, arena bouts, GM notes, bestiary",
    "Guidance for running Norse arena creatures: terrain, tone, ready bouts, and GM notes.",
    """
This file is for mystical creatures and Mythborn, not gods. They can appear as captured arena beasts, intelligent pit champions, sponsored challengers, or free denizens of Asterion's lower city. Standard threats are quick encounters; Elites pressure one or two champions; Bosses refresh their Spark Shield and intensify when reduced below half Flesh. Telegraph a boss's strongest move one turn before it happens.

**Useful terrain:** floodgates, rune pillars, hanging chains, cold braziers, and collapsing stone.

## Ready Arena Bouts

- **Chains of the End:** Garmr begins chained at the center of the pit; one chain breaks at the end of every round, while Raven of the Gallows Wind swarms harass anyone trying to control the arena.
- **Gold Is a Wound:** Fafnir's Whelp guards a sponsor's treasure. Contestants may take coins for tactical advantages, but metal becomes dangerous when the dragon reaches its second phase.
- **The Frozen Gate:** Frost Jotunn Arena-Brute and two Hel-Hounds enter through an ice-covered gate, steadily freezing mobility routes across the arena.
- **Runebreaker Hunt:** Lindworm of the Rune Pits burrows beneath shifting stone. Champions can collapse tunnels, use brazier fire, or lure it toward the crowd-proof rune barriers.

## GM Notes

- **Creature does not mean mindless.** Fafnir's Whelp bargains over treasure, a Valkyrie-Steed may demand honor, and Draugr remember old grudges.
- **Make northern opponents physical.** Favor ice, iron, chain, oath, storm, hunger, burial, and inevitable doom instead of generic magic.
- **Use the arena.** Floodgates, rune pillars, hanging chains, cold braziers, and collapsing Babylonian stone make every bout distinct.
""",
)

print(f"After Norse + shared rules note: {len(entities)} entities")

# ────────────────────────────────────────────────────────────────────────────
# Denizens of Asterion — 24 named city NPCs (not tied to a pantheon sphere)
# ────────────────────────────────────────────────────────────────────────────

_denizens = [
    dict(name="Therasios", epithet="God of Broken Oaths", category="Minor God / Faded Divinity", tier="Elite",
         domain="Broken Oaths, Contracts, Betrayal", location="Hall of Reprisals",
         disposition="Neutral — transactional, dangerous if oath is broken in his presence",
         nameless="Tolerated; his domain punishes weakness and empty vows",
         quote="Every promise is a debt. I simply collect what you already owe.",
         desc="A lean, scarred god who feeds on shattered promises instead of worship. He runs the Hall of Reprisals, where contracts etched onto obsidian tablets bind soul or memory to their terms.",
         attack="3d10", defense="3d10", shield="3", flesh="7", ichor="4", move="30 ft / 6 hexes",
         abilities="**Obsidian Contract** *(Active, Tier 2, 2 Ichor)* — forces a target who has broken a spoken oath to make a Domain Pool check or suffer a Compelled condition, obeying one narrow command tied to the broken oath's terms for 1 turn.\n\n**Debt Made Flesh** *(Passive, Tier 1)* — whenever an enemy breaks a promise, vow, or explicit deal in combat, Therasios gains 1 temporary Ichor.",
         hooks="A famous champion wants to break a suicidal contract and hires the PCs to out-bargain Therasios.\n\nTherasios has discovered a divine oath the Nameless God swore in the Primordial War—and is quietly hoarding it."),
    dict(name="Heket of the Red Nile", epithet="Faded River-Goddess of Thresholds and Plagues", category="Minor God / Faded Divinity", tier="Standard Mythborn",
         domain="Thresholds, Plagues, Survival", location="Hidden bathhouse in the slums",
         disposition="Neutral — judges supplicants by honesty",
         nameless="Ignored; too diminished to matter politically",
         quote="The water asks one question. Lie to it, and it answers in kind.",
         desc="A diminished echo of an old river-goddess. Her hidden bathhouse's water can cure any wound or seed a slow, incurable illness depending on how honestly a supplicant answers her questions.",
         attack="2d10", defense="2d10", shield="2", flesh="5", ichor="3", move="30 ft / 6 hexes",
         abilities="**Waters of Judgment** *(Active, Tier 1, 1 Ichor)* — target either fully heals all Flesh damage, or contracts a slow Plague condition (1 Flesh loss per day until cured) — Heket alone decides which, based on the target's honesty.\n\n**Threshold Sense** *(Passive, Tier 1)* — always aware when a lie is spoken in her presence.",
         hooks="An epidemic is sweeping a district; only Heket knows whether it is natural, a curse, or a deliberate cull.\n\nHeket offers the PCs a cure in exchange for carrying a sealed amphora into the Colosseum and breaking it at a specific moment."),
    dict(name="Yngvi Ember-Hand", epithet="Forgotten Smith-God", category="Minor God / Faded Divinity", tier="Standard Mythborn",
         domain="Smithing, Second-Life Weapons", location="Forge beneath the Colosseum",
         disposition="Neutral — gruff but fair to champions",
         nameless="Respected; his craft strengthens the arena's warriors",
         quote="Bring me what's broken. I'll tell you if it deserves to live again.",
         desc="A forgotten smith-god from a cold northern pantheon, hands permanently blackened with divine soot. He forges second-life weapons from broken relics of dead gods and Primordials.",
         attack="2d10", defense="2d10", shield="2", flesh="6", ichor="3", move="30 ft / 6 hexes",
         abilities="**Second-Life Forging** *(Passive, Tier 1)* — can reforge any broken or relic weapon into a functional Tier 1 magic weapon once per champion, per three arena seasons survived.\n\n**Ember Grip** *(Active, Tier 1, 1 Ichor)* — melee strike dealing 1 base damage plus net Successes; on hit, the weapon glows and the target suffers -1 die on their next Defender roll.",
         hooks="A rival faction wants the blueprint of a Godhand-resistant shield Yngvi refuses to make.\n\nYngvi has begun hearing hammering from deep below his forge—someone is forging without his leave."),
    dict(name="Sarasvati the Faded", epithet="Shard of the Goddess of Knowledge and Music", category="Minor God / Faded Divinity", tier="Non-Combatant",
         domain="Knowledge, Music, Memory", location="The Echo Archive",
         disposition="Neutral — trades information for stories",
         nameless="Unnoticed; considered harmless",
         quote="Every death sings a note. I have simply learned to listen.",
         desc="A quiet shard of an ancient goddess of knowledge and music, her radiance dimmed to a soft blue nimbus. She curates the Echo Archive, recording every Colosseum death-cry as musical notation.",
         attack="1d10", defense="1d10", shield="1", flesh="3", ichor="2", move="30 ft / 6 hexes",
         abilities="**Echo Recall** *(Active, Tier 1, 1 Ichor)* — can play back the recorded 'notation' of any death she has archived, granting listeners tactical insight (advantage on one related roll) or traumatic Fear at GM's discretion.",
         hooks="Sarasvati offers the PCs the exact battle-song that once drove a Primordial mad, if they bring her a story never told aloud.\n\nA page of notation has gone missing—one that records the Nameless God's own near-death."),
    dict(name="Xochipilli of Ruinous Joy", epithet="Revel-God of the Festival of Last Nights", category="Minor God / Faded Divinity", tier="Elite",
         domain="Revelry, Gambling, Ecstatic Cultism", location="Festival of Last Nights",
         disposition="Chaotic — cheerful but morally indifferent",
         nameless="Tolerated as a release valve for the condemned",
         quote="Smile! You still have days left to gamble away.",
         desc="A minor revel-god from a blood-soaked southern tradition, always smiling, garlanded in knives and flower-petals. His permanent street carnival lets condemned fighters gamble their remaining days for glory, lovers, or a painless death.",
         attack="3d10", defense="2d10", shield="2", flesh="6", ichor="4", move="30 ft / 6 hexes",
         abilities="**Wager of Days** *(Active, Tier 2, 2 Ichor)* — target willingly stakes a resource (days of life, a memory, a vice) on a contested roll against Xochipilli; winner gains a boon, loser suffers the wagered cost.\n\n**Ecstatic Frenzy** *(Active, Tier 1, 1 Ichor)* — grants an ally or self Frenzied (extra die on Attacker rolls, -1 die on Defender rolls) for 1 turn.",
         hooks="The PCs must navigate the Festival to find a single sober witness who saw something important during a riot.\n\nXochipilli offers to rewrite the party's worst memories into triumphant ones—at the cost of making the original tragedies happen to someone else."),
    dict(name="Namtarion", epithet="Herald of the Last Bell", category="Minor God / Faded Divinity", tier="Elite",
         domain="Fate, Death Omens", location="Wanders Asterion unseen",
         disposition="Neutral — dispassionate broker of fated deaths",
         nameless="Avoided; even he does not discuss the Nameless God's fate",
         quote="The bell already knows how you die. I am only the one who tells you.",
         desc="A thin, jackal-faced god carrying a bronze bell that never visibly moves yet is always ringing in your bones. For a price, he tells warriors the exact kind of blow that will one day kill them.",
         attack="2d10", defense="3d10", shield="3", flesh="6", ichor="4", move="30 ft / 6 hexes",
         abilities="**Foretold Blow** *(Active, Tier 2, 1 Ichor)* — reveals the specific type of attack that will eventually kill the target; grants +1 die vs that specific attack type for the rest of the campaign, but -1 die vs all other attack types for 1 encounter (paranoia).\n\n**Bell That Never Stops** *(Passive, Tier 1)* — cannot be surprised or ambushed; always aware of incoming lethal danger to himself.",
         hooks="A beloved NPC has learned that one of the PCs is fated to kill them; Namtarion refuses to clarify.\n\nSomeone has stolen the clapper of Namtarion's bell, disrupting fates across the city."),
    dict(name="Kallisti", epithet="Goddess of the Perfect Cut", category="Minor God / Faded Divinity", tier="Boss / Apex Threat",
         domain="Duels, Perfection, Precision", location="Duelist salons across Asterion",
         disposition="Hostile in formal duels; respectful otherwise",
         nameless="Regarded with interest; her philosophy echoes his own meritocracy",
         quote="One cut. One truth. Everything else is noise.",
         desc="A modern, Asterion-born war-goddess who manifested from the city's obsession with duels. She appears as a masked duelist in pristine white, her sword never stained.",
         attack="4d10", defense="4d10", shield="4", flesh="9", ichor="5", move="30 ft / 6 hexes (Blink Step 15 ft)",
         abilities="**The Perfect Cut** *(Active, Tier 3, 3 Ichor)* — a single telegraphed strike: if it connects, deals lethal damage equal to the target's remaining Flesh (an instant defeat against non-Boss targets); once per encounter, only against a single chosen opponent declared at the start of the duel.\n\n**Flawless Guard** *(Passive, Tier 2)* — automatically negates the first hit against her each combat.\n\n**Severance Vow** *(Legendary Action, Tier 2, Free, once per duel)* — when reduced below half Flesh, may immediately force a reroll of her last failed Defender check.\n\n**Phase Break** — at half Flesh: her mask cracks, revealing raw fury beneath her composure—she gains +1 die on all Attacker rolls but loses Flawless Guard's automatic negation for the rest of the duel.",
         hooks="Kallisti offers to teach a PC her divine perfect cut, but only if they sever one tie they cannot afford to lose.\n\nHer duelists have started executing petty criminals in the streets, claiming each cut is an offering."),
    dict(name="Abraxos", epithet="Oracle of the Third Eye", category="Minor God / Faded Divinity", tier="Non-Combatant",
         domain="Foresight, Defeat", location="Desert cult shrine near the arena",
         disposition="Neutral — unsettling but non-violent",
         nameless="Watched; his visions are politically dangerous",
         quote="I have never once seen you win. Let me show you exactly how you lose.",
         desc="A minor oracle-god with a vertical gemstone embedded in his brow. He sees possible defeats rather than victories—his visions always show how you lose.",
         attack="1d10", defense="2d10", shield="1", flesh="4", ichor="3", move="30 ft / 6 hexes",
         abilities="**Vision of Defeat** *(Active, Tier 1, 1 Ichor)* — grants a target foreknowledge of their most likely defeat, allowing them to reroll one failed Defender check against the specific circumstance shown, once per session.",
         hooks="Abraxos claims he has finally seen a vision in which the Nameless God dies—and refuses to share it.\n\nThe PCs keep stumbling into situations Abraxos supposedly foresaw, suggesting someone is scripting their failures."),
    dict(name="Lady Mawu", epithet="Keeper of Masks", category="Minor God / Faded Divinity", tier="Standard Mythborn",
         domain="Identity, Reinvention", location="Shrine-market of masks",
         disposition="Neutral — protective of those seeking new lives",
         nameless="Ignored; her domain is social, not martial",
         quote="Wear this, and no one will ever look for who you were.",
         desc="A syncretic household goddess, now patron of identity and reinvention in Asterion. Her shrine-market sells masks that subtly alter a wearer's voice, posture, and social luck.",
         attack="1d10", defense="2d10", shield="2", flesh="5", ichor="3", move="30 ft / 6 hexes",
         abilities="**Mask of the New Self** *(Active, Tier 1, 1 Ichor)* — grants a willing wearer an alternate identity: +2 dice on Social Pool checks to pass as someone else for 1 week, at the cost of an unpredictable shift in luck (GM determines minor complication).",
         hooks="A notorious criminal has vanished by becoming someone else with Mawu's blessing; the PCs must decide whether to unmask them.\n\nA mask forged for a god has gone missing and is now in mortal hands."),
    dict(name="Cassia Vorn", epithet="Arena Prodigy", category="Mortal", tier="Elite",
         domain="Bare-handed combat", location="The Colosseum",
         disposition="Hostile only in the arena; otherwise focused and distant",
         nameless="Watched closely; she may provoke a second Godhand",
         quote="No sponsor. No Spark. Just me, and I still haven't lost.",
         desc="A mortal woman from a ruined border-town who has never lost in the Colosseum, fighting bare-handed in imitation of the Nameless God. She refuses divine sponsorship.",
         attack="3d10", defense="3d10", shield="1", flesh="6", ichor="0", move="30 ft / 6 hexes",
         abilities="**Unbroken Streak** *(Passive, Tier 2)* — gains +1 die on all rolls after the first round of any fight she has not yet lost that season.\n\n**Mimic Strike** *(Active, Tier 2, 0 Ichor/Stamina)* — a bare-fisted strike dealing 2 base damage plus net Successes; usable 3 times per encounter before requiring a short rest to recover Stamina.",
         hooks="Cassia asks the PCs to sabotage her next match so she can experience a genuine defeat before facing the Nameless God.\n\nA god offers the party vast power to quietly remove Cassia before she destabilizes the city's hierarchy."),
    dict(name="Master Jinhai", epithet="The Quiet Referee", category="Mortal", tier="Non-Combatant",
         domain="Arbitration", location="The Colosseum",
         disposition="Neutral — universally respected, even by gods",
         nameless="Personally protected; harming him is forbidden",
         quote="The fight is over when I say it is over. Even he agrees.",
         desc="An elderly mortal monk who serves as neutral arbiter of high-profile matches. The Nameless God personally forbids anyone from harming him.",
         attack="1d10", defense="2d10", shield="1", flesh="4", ichor="0", move="30 ft / 6 hexes",
         abilities="**The Final Word** *(Passive, Tier 1)* — any combat Jinhai declares over immediately ends—no further actions may be taken against the loser, enforced by unspoken divine sanction.",
         hooks="Jinhai privately asks the PCs to investigate a series of fixed fights he suspects are rigged by a god.\n\nSomeone attempts to assassinate Jinhai outside the arena, forcing the Nameless God to intervene—or not."),
    dict(name="Lysa of the Shattered Choir", epithet="Death-Song Singer", category="Mythborn", tier="Non-Combatant",
         domain="Memory, Death", location="Performs at executions and arena events",
         disposition="Neutral — melancholy, easily wounded emotionally",
         nameless="Overlooked; useful to Sarasvati and the Festival",
         quote="I don't choose which deaths I remember. They choose me.",
         desc="A mythborn singer whose voice can replay the last minutes of any death she has witnessed. She performs at executions and major arena events, turning them into operas of blood and memory.",
         attack="1d10", defense="1d10", shield="1", flesh="3", ichor="2", move="30 ft / 6 hexes",
         abilities="**Song of the Last Minute** *(Active, Tier 1, 1 Ichor)* — replays a death she has witnessed as a haunting performance; listeners gain tactical insight into that death's cause (advantage on one related roll) or suffer Fear, GM's choice.",
         hooks="Lysa refuses to sing one particular death; the PCs must learn why it broke her.\n\nHearing Lysa's song about a past battle gives the party tactical insight—or traumatic visions."),
    dict(name="Koru", epithet="Scrap-Forge Goblin", category="Mythborn", tier="Standard Mythborn",
         domain="Improvised weaponry", location="Under the Colosseum stands",
         disposition="Neutral — opportunistic but not malicious",
         nameless="Amused protection; cannot be killed within city walls",
         quote="Ugly? Sure. Works? Also sure.",
         desc="A small, sharp-toothed mythborn scavenger who steals broken weapons from under the Colosseum stands and builds crude but surprisingly effective gear for desperate fighters.",
         attack="2d10", defense="2d10", shield="1", flesh="4", ichor="2", move="30 ft / 6 hexes",
         abilities="**Scrap Weapon** *(Passive, Tier 1)* — can craft a crude but functional Tier 1 weapon from scavenged parts in under an hour, usable by anyone regardless of Spark.\n\n**Jury-Rigged Trap** *(Active, Tier 1, 1 Ichor)* — deploys a hidden improvised trap dealing 1 base damage plus net Successes to the first creature that triggers it.",
         hooks="Koru has assembled a weapon from fragments of a Primordial's bone, and every faction wants it.\n\nThe Nameless God is oddly amused by Koru and has quietly ordered that the goblin never be killed within the city walls."),
    dict(name="Sir Damaris", epithet="Fallen Paladin of a Foreign Sun", category="Mortal", tier="Standard Mythborn",
         domain="Protection, Faded Light", location="Escorts pilgrims through Asterion",
         disposition="Lawful, guarded — questioning his old faith",
         nameless="Unremarkable; one of many stranded champions",
         quote="My god is dead. I still remember how to hold a line.",
         desc="Once a champion of a distant lawful god, now stranded after his patron died in the Primordial War. He wears patched, sun-etched plate and hires himself out as a bodyguard for pilgrims.",
         attack="2d10", defense="3d10", shield="3", flesh="6", ichor="1", move="30 ft / 6 hexes",
         abilities="**Last Light Ward** *(Active, Tier 1, 1 Ichor — last remaining)* — grants an ally a shield equal to 2 points, drawn from the last fading fragment of his dead god's light—usable only once per week.\n\n**Shield Wall** *(Passive, Tier 1)* — +1 die to Defender rolls made to protect an adjacent ally.",
         hooks="Damaris asks the PCs to help retrieve his god's last fragment of light, buried beneath the Empyrean Apex.\n\nA cult tries to crown him a new sun-god; he wants the party to stop them without killing anyone."),
    dict(name="Nera the Debt-Scribe", epithet="Ledger-Keeper of the Arena", category="Mortal", tier="Non-Combatant",
         domain="Wagers, Contracts, Blood-Debts", location="Backrooms of the Colosseum",
         disposition="Neutral — transactional and calculating",
         nameless="Unnoticed; a favorite of Therasios",
         quote="I don't fight. I don't need to. My ledger already owns half this city.",
         desc="A mortal accountant who manages wagers, blood-debts, and sponsorship contracts for half the gladiators in the city. She has no combat skill, but her ledgers can ruin minor gods.",
         attack="1d10", defense="1d10", shield="0", flesh="3", ichor="0", move="30 ft / 6 hexes",
         abilities="**The Ledger's Weight** *(Passive, Tier 1)* — can call in a favor or debt from any NPC previously indebted to her, functioning as an automatic social success once per session.",
         hooks="Nera's ledger is stolen, threatening to collapse the arena's entire betting economy.\n\nShe quietly offers to erase one of the party's worst mistakes from the official record—at a steep price."),
    dict(name="Maskless Izel", epithet="The Halo-Seer Child", category="Mythborn", tier="Non-Combatant",
         domain="Spectatorship, Perception", location="The Colosseum stands",
         disposition="Innocent — curious, unaware of her own value",
         nameless="Unnoticed; courted by Xochipilli",
         quote="Your halo just changed color. Did you lie just now?",
         desc="A mythborn child born during a massacre in the Colosseum, carrying a faint divine spark of spectatorship. She can see a fighter's fear, pride, and lies as colored halos around their head.",
         attack="0", defense="1d10", shield="0", flesh="2", ichor="1", move="30 ft / 6 hexes",
         abilities="**Halo Sight** *(Passive, Tier 1)* — can perceive a target's fear, pride, or dishonesty as a visible colored halo, granting the party insight into an NPC's true emotional state or intentions on request.",
         hooks="Izel tells a PC that their halo vanishes whenever they think about a certain memory.\n\nMultiple gods begin courting Izel's favor as an unofficial oracle of the crowd's will."),
    dict(name="Varo the Stitch-Doctor", epithet="Battlefield Surgeon", category="Mortal", tier="Non-Combatant",
         domain="Medicine, Primordial Grafts", location="Clinic outside the arena gates",
         disposition="Neutral — helpful but quietly self-serving",
         nameless="Unremarkable; tolerated for his usefulness",
         quote="I'll fix you. Good as new. Almost.",
         desc="A mortal battlefield surgeon who runs a clinic right outside the arena gates, using mundane medicine, black-market healing magic, and Primordial grafts.",
         attack="0", defense="1d10", shield="0", flesh="3", ichor="1", move="30 ft / 6 hexes",
         abilities="**Primordial Graft** *(Active, Tier 2, 1 Ichor)* — restores a target to full Flesh outside of combat, but implants a hidden minor flaw (GM-determined future complication) unbeknownst to the recipient.",
         hooks="Varo offers to install a Primordial graft in a PC, promising great power and an unspecified small side effect.\n\nSomeone has started murdering his former patients in ways that target their old injuries."),
    dict(name="Selene of the Brass Quill", epithet="Gossip-Writer", category="Mortal", tier="Non-Combatant",
         domain="Journalism, Scandal", location="Prints the Asterion Night Sheet citywide",
         disposition="Neutral — fearless and opportunistic",
         nameless="Unnoticed; occasionally a nuisance to minor gods",
         quote="By tomorrow morning, everyone will know exactly what you did.",
         desc="A journalist and gossip-writer who produces the illegal Asterion Night Sheet, reporting on divine scandals, fixed fights, and backroom deals. Her brass quill can write on any surface.",
         attack="0", defense="1d10", shield="0", flesh="3", ichor="1", move="30 ft / 6 hexes",
         abilities="**Brass Quill** *(Passive, Tier 1)* — can instantly write and publish information on any surface citywide by nightfall, functioning as an automatic rumor-spreading action once per day.",
         hooks="Selene prints a rumor about the PCs that is dangerously close to the truth.\n\nA god demands that the party silence her permanently—or protect her as she publishes something explosive."),
    dict(name="Targos the Chain-Priest", epithet="Executioner of the Underground Pits", category="Mythborn", tier="Elite",
         domain="Illegal arenas, Execution", location="Underground unsanctioned pits",
         disposition="Hostile in his domain; territorial",
         nameless="Watched with suspicion; his pits rival the Colosseum's authority",
         quote="Every scar on my skin is a name. Yours will be next.",
         desc="A hulking mythborn whose skin is inscribed with every match he has ever officiated in the underground, unsanctioned pits. He serves as priest, bookie, and executioner there.",
         attack="3d10", defense="3d10", shield="3", flesh="7", ichor="3", move="30 ft / 6 hexes",
         abilities="**Chain of Judgment** *(Active, Tier 2, 2 Ichor)* — a weighted chain strike dealing 2 base damage plus net Successes; on hit, the target is Restrained for 1 turn.\n\n**Skin of Every Match** *(Passive, Tier 1)* — gains +1 die on Domain Pool checks made while inside any arena or pit he has officiated.",
         hooks="Targos invites the PCs to fight in a forbidden tournament whose prize is an audience with a hidden god.\n\nThe Nameless God orders the PCs to either shut down Targos's pits or prove they serve his philosophy."),
    dict(name="Mira and Jaxon", epithet="Twin Smugglers of Sparks", category="Mortal", tier="Standard Mythborn",
         domain="Smuggling, Divine Artifacts", location="Moves through Asterion's black markets",
         disposition="Neutral — cautious, self-interested",
         nameless="Unnoticed; useful to multiple factions",
         quote="We finish each other's — / — sentences. Unsettling, isn't it?",
         desc="Human twins who specialize in smuggling minor divine artifacts and stolen Sparks into and out of the city. They speak in rehearsed overlaps, finishing each other's sentences in unnerving unison.",
         attack="2d10", defense="2d10", shield="1", flesh="5", ichor="2", move="30 ft / 6 hexes",
         abilities="**Twinned Reflexes** *(Passive, Tier 1)* — when adjacent to each other, both twins gain +1 die on Defender rolls, reacting to danger in perfect unison.\n\n**Smuggler's Feint** *(Active, Tier 1, 1 Ichor)* — one twin creates a distraction, granting the other advantage on their next Attacker or Stealth roll.",
         hooks="The twins ask the party to help move a stolen fragment of the Nameless God's own aura.\n\nOne twin is secretly bargaining with Oramis, the God of Secrets, without telling the other."),
    dict(name="Old Mother Rhysa", epithet="Retired Arena Champion", category="Mortal", tier="Elite",
         domain="Forgotten combat prowess", location="Streets near the Colosseum",
         disposition="Neutral — gentle unless her 'grandchildren' are threatened",
         nameless="Forgotten; unrecognized by current authorities",
         quote="I'm just an old woman feeding strays. Don't make me prove otherwise.",
         desc="An apparently harmless old woman who feeds stray fighters and street kids near the Colosseum. In truth she is a retired arena champion whose Spark never fully faded.",
         attack="3d10", defense="3d10", shield="2", flesh="6", ichor="3", move="30 ft / 6 hexes",
         abilities="**Faded Spark, Sudden Fire** *(Active, Tier 2, 2 Ichor)* — once revealed, moves and strikes with terrifying speed: gains an extra action this turn, usable only when protecting someone she considers family.\n\n**Old Woman's Guise** *(Passive, Tier 1)* — treated as harmless by default; enemies suffer -1 die on Perception checks to notice her as a threat until she acts.",
         hooks="A gang starts shaking down Rhysa's kids; she asks the PCs to teach them a lesson without revealing her own past.\n\nSomeone recognizes Rhysa from an old Echo Archive song and tries to drag her back into the arena."),
    dict(name="The Painted Choir", epithet="Prophetic Street Artists", category="Mythborn", tier="Non-Combatant",
         domain="Prophecy, Mural Art", location="Asterion's walls, city-wide",
         disposition="Neutral — elusive collective, avoids direct confrontation",
         nameless="Watched by the city guard; their murals are politically dangerous",
         quote="We don't paint what will happen. We paint what already has, somewhere else.",
         desc="Not a single NPC, but a loose group of mythborn street artists who paint prophetic murals overnight on Asterion's walls, often foreshadowing key matches, divine deaths, or uprisings.",
         attack="1d10", defense="1d10", shield="0", flesh="3 (per member)", ichor="1 (per member)", move="30 ft / 6 hexes",
         abilities="**Prophetic Mural** *(Passive, Tier 1)* — once per location, can produce a mural depicting a true fragment of a near-future event, interpreted narratively by the GM.",
         hooks="A new mural clearly depicts one of the PCs dying to the Nameless God's fist.\n\nThe city guard begins hunting members of the Choir; the PCs must decide whether to help them escape or exploit their visions."),
]

for d in _denizens:
    add(
        f"{d['name']} — {d['epithet']}", "character", d["category"], "NPCs/Denizens of Asterion",
        f"{d['category']}, {d['tier']}, {d['domain']}",
        d["desc"][:295],
        f"""
> "{d['quote']}"

**Category:** {d['category']} | **Enemy Tier:** {d['tier']} | **Domain/Sphere:** {d['domain']}
**Location:** {d['location']} | **Disposition:** {d['disposition']}
**Relation to Nameless God:** {d['nameless']}

{d['desc']}

| Attack Pool | Defense Pool | Spark Shield | Flesh | Ichor | Movement |
| :--- | :--- | :--- | :--- | :--- | :--- |
| {d['attack']} | {d['defense']} | {d['shield']} | {d['flesh']} | {d['ichor']} | {d['move']} |

## Abilities

{d['abilities']}

## Plot Hooks

{d['hooks']}
""",
    )

print(f"After Denizens: {len(entities)} entities")

# ────────────────────────────────────────────────────────────────────────────
# Arena Bestiary — generic (non-sphere) mythic opponents
# ────────────────────────────────────────────────────────────────────────────

_arena_creatures = [
    ("Bronze Minotaur", "Elite", "2d10 / 3d10 charge", "4", "9", "4",
     "Labyrinth Charge (2 damage; Restrained on hit); Gore Guard (Reaction: counterstrike a melee attacker).",
     "A disciplined horned arena veteran in bronze plates; ideal opening elite."),
    ("Stymphalian Murder-Flock", "Standard Swarm", "2d10", "2", "5", "0",
     "Razor Feather Volley (15 ft, 1 damage, Bleeding); Scatter (cannot be flanked).",
     "Use three to six birds as one swarm unit; harasses ranged fighters."),
    ("Nemean Lion", "Boss", "3d10", "6", "18", "8",
     "Golden Hide (Greater Resistance to mundane physical attacks); King's Pounce (2 damage, Restrained); Roar below half Flesh stuns nearby foes.",
     "A classic apex hunt; require divine, magical, or clever attacks."),
    ("Chimera of the Brazen Gate", "Boss", "3d10", "6", "16", "10",
     "Lion Bite (2 damage); Goat Horn Rush (push 15 ft); Serpent Tail (Poison); Fire Breath is telegraphed, 15-ft cone, 2 damage.",
     "Three threats in one body; change its head used each turn."),
    ("Hydra of the Flooded Sand", "Boss", "3d10", "7", "20", "10",
     "Many Heads (extra bite after each successful defense); Regrow Two (replace a severed head unless fire is used); Venom Surge below half Flesh.",
     "A multi-target endurance bout in a partially flooded arena."),
    ("Gorgon Pit-Queen", "Elite", "3d10 gaze", "4", "10", "6",
     "Petrifying Glance (30 ft, Weakened; second failure Restrained in stone); Serpent Hair (melee 2 damage, Poison).",
     "Use mirrored shields, cover, and line-of-sight play."),
    ("Manticore of Ash and Iron", "Elite", "3d10", "4", "10", "6",
     "Tail-Spike Barrage (100 ft, 2 damage); Pounce and Rend (2 damage); Wingbeat (reposition 30 ft).",
     "A flying skirmisher that forces the crowd to watch the sky."),
    ("Cerberus, Chainbreaker Hound", "Boss", "3d10", "6", "18", "8",
     "Three-Headed Assault (three 1-damage bites); Grave Bark (30 ft, Frightened/Weakened); Hell-Chain Drag (pull 30 ft).",
     "A guardian-beast bout; the arena gates lock when it enters."),
    ("Basilisk of the Black Mosaic", "Elite", "3d10", "4", "9", "5",
     "Death-Eye (30 ft, 2 damage bypassing Armor if target meets gaze); Venom Bite (2 damage, Poison).",
     "A low-profile horror that rewards indirect attacks and reflected sight."),
    ("Pegasus of the Storm Pens", "Elite", "3d10", "5", "11", "6",
     "Thunder Dive (line charge, 2 lightning damage); Skyward Escape (Reaction: fly 30 ft after being targeted).",
     "A noble but furious aerial opponent for champions who need mobility tests."),
    ("Colchis Bull", "Boss", "3d10", "7", "20", "8",
     "Bronze Hide (Armor 3); Furnace Bellow (15 ft cone, Burning); Stampede (straight line, 2 damage and knockdown).",
     "A siege-beast that turns arena pillars into dangerous debris."),
    ("Erymanthian Boar", "Standard", "2d10", "3", "7", "2",
     "Tusk Charge (1 damage and push); Mud-Wallow (creates difficult terrain).",
     "Fast, brutal, and easy to pair with traps or a second beast."),
]

for name, tier, attack, shield, flesh, ichor, sig, flavor in _arena_creatures:
    add(
        name, "creature", tier, "Bestiary/Arena Bestiary",
        f"arena bestiary, {tier.lower()}, generic",
        flavor,
        f"""
| Stat | Value |
| :--- | :--- |
| Attack Pool | {attack} |
| Spark Shield | {shield} |
| Flesh | {flesh} |
| Ichor | {ichor} |

## Signature Abilities

{sig}

{flavor}
""",
    )

add(
    "Arena Bestiary — Running Notes & Quick Bout Cards", "note", "lore", "Bestiary/Arena Bestiary",
    "arena bestiary, quick bouts, GM notes",
    "How to run the generic Arena Bestiary — twelve mythic opponents for the Colosseum, sanctioned bouts, and the Outer Ring's illegal pits.",
    """
Asterion is a gladiator-driven city ruled by the Nameless God of War, with official arenas and brutal illegal pits in the Outer Ring. These opponents use stripped-down enemy stats: Standards fall quickly, Elites occupy one or two champions, and Bosses use a phase break below half Flesh. All Bosses refresh their Spark Shield at that point and unlock or intensify their final listed behavior.

- **Standard:** use one simple signature ability; pair two or more for a crowd-pleasing bout.
- **Elite:** use one defining trait and one signature move; a strong opponent for a single mythborn or small team.
- **Boss:** telegraph their biggest move one turn early with an unmistakable arena tell: cracked sand, a roar, glowing eyes, or a closing gate.
- **Reward ideas:** Drachma purses, arena Glory, a sponsor's favor, divine beast materials, or release from a blood-debt.

## Quick Bout Cards

- **The Bronze Labyrinth:** Bronze Minotaur plus two Stymphalian Murder-Flock swarms; stone walls rise and shift after each round.
- **Fire Against Flood:** Hydra of the Flooded Sand in waist-deep water; scattered braziers are the only reliable way to stop head regeneration.
- **Royal Hunt:** Nemean Lion in a moonlit marble arena. The audience throws down divine weapons only if impressed by courage.
- **Beastmaker's Trial:** Chimera, Colchis Bull, or Cerberus chosen by a sponsor; each victory earns a different faction's attention.
- **Flight of Judgment:** Pegasus of the Storm Pens versus Manticore of Ash and Iron above a collapsing arena floor.

## GM Notes

- Keep beasts distinct: one memorable mechanic is better than a complete player-style character sheet.
- Make the arena interactive: pillars, chains, floodgates, braziers, elevation, crowd wagers, and shifting sand should matter.
- Do not treat every beast as mindless. The Pegasus may be unwilling, Cerberus may obey an ancient command, and the Gorgon Pit-Queen may bargain for freedom.
""",
)

print(f"After Arena Bestiary: {len(entities)} entities")

# ────────────────────────────────────────────────────────────────────────────
# Locations — the three Rings of Asterion + the cosmic map
# ────────────────────────────────────────────────────────────────────────────

add(
    "Asterion — Core Ring", "location", "district", "Locations",
    "core ring, Empyrean Apex, Colosseum, arena, command",
    "The pressure point of Asterion — the Empyrean Apex, the Colosseum, and the machinery of arena politics, military force, and civic control.",
    """
The core ring is the pressure point of Asterion. It surrounds the Empyrean Apex and the Colosseum, and it is where divine power, arena politics, military force, and civic control are most concentrated. If the outer ring is survival and the inner ring is administration, the core ring is command.

## Overview

The core ring is made up of the sacred and strategic places closest to the arena: **Empyrean Apex / Colosseum** (the center of divine combat, spectacle, and public authority), **Arena Approaches** (streets, gates, ramps, plazas feeding into the Colosseum), **Judgment Terraces** (arbitration, sponsorship, official pronouncements), **Victory Courts** (processional areas where champions are rewarded, displayed, claimed), **Godhand Passages** (secure routes for gods, champions, and elite retainers), and **Memorial Steps** (sites where the dead, the Shattered, and the remembered are honored or exploited).

The core ring is where the city decides who matters. It is highly guarded, heavily watched, and full of people who understand that a single public moment can reshape power across Asterion.

## Empyrean Apex / Colosseum

The throne-ring of Asterion and the site of the Colosseum. This is where the Nameless God of War rules in practice, not just in title. The arena is a cathedral of violence, where every match becomes a ritual, every victory becomes a sermon, and every defeat becomes a lesson for the city. Cassia Vorn trains here in the shadow of the arena's mythology, hoping to force the Nameless God to acknowledge her strength.

**Key locations:** The Main Arena Floor (bouts, executions, apex matches) · Champion Stands (reserved platforms for sponsors, gods, honored guests) · The Black Gate (hidden entrance to move fighters in and corpses out) · The Under-Sand Chambers (preparation rooms, blood-wash tunnels, resting cells beneath the arena).

**Common events:** An Apex Bout witnessed by mortals, gods, and political rivals · A public execution or mercy display that changes the mood of the city · A challenge to the Nameless God's order, either through combat or spectacle.

**NPCs:** The Nameless God of War · Cassia Vorn · Master Jinhai · arena heralds, blood-attendants, and champions.

## Arena Approaches

The streets and plazas surrounding the Colosseum, always crowded with food carts, armor fixers, prayer sellers, and betting runners. This is the place where the build-up to violence matters almost as much as violence itself.

**Key locations:** Victory Stair (broad public stair for champion entrances) · Fight Vendors' Row (last-minute gear, luck charms, illegal aid) · The Red Plaza (packed square where crowds gather before major bouts).

**NPCs:** Betting runners · Armor fixers · Praying spectators.

## Judgment Terraces

Where arena outcomes become law. Sponsorships are granted, penalties announced, public decrees made. Therasios's contracts often intersect here, because the core ring loves binding the legal to the violent.

**Key locations:** Edict Balcony (official declarations) · Sponsorship Hall (fighters claimed by patrons) · The Sentence Steps (punishments, pardons, public terms).

**NPCs:** Arena clerks · Sponsor agents · Therasios's oath auditors.

## Victory Courts

Ceremonial spaces that celebrate successful combatants — beautiful in a severe way: banners, bronze, incense, and blood-clean stone.

**Key locations:** Champion Path (procession lane) · Bronze Fountain (symbolic purification fountain) · Crown Dais (raised platform for titles, honors, public pledges).

**NPCs:** Victory attendants · Crowd heralds · Honored champions.

## Godhand Passages

Secure routes reserved for gods, apex champions, and elite retainers — private negotiations, emergency relocation, quiet threats. If a character is taken into a Godhand Passage, something important is happening.

**Key locations:** Silver Corridor (polished tunnel for high-status movement) · Veiled Gate (guarded threshold for sanctioned divine travel) · Retainer Walks (side passages for bodyguards, servants, witnesses).

**NPCs:** Divine retainers · Gate wardens · Silent messengers.

## Memorial Steps

Where the city remembers its dead, if only selectively. Sarasvati's archive often informs what gets remembered here, while Lysa's songs can make the steps feel haunted even when the bodies are long gone.

**Key locations:** Name Wall (carved slabs of fallen champions) · Ash Stairs (funeral processions and ash offerings) · The Last Torch (perpetual flame for the honored dead).

**NPCs:** Memorial keepers · Funeral singers · Families of the fallen.

## Core Ring Connections

The Arena Approaches draw fighters and spectators from the Outer Ring. The Judgment Terraces link the arena to the Hall of Reprisals in the Inner Ring. The Victory Courts influence the Brazen Grid through sponsorship and patronage. The Memorial Steps feed stories to the Echo Archive.

## Adventure Hooks

- A fighter who was supposed to die is publicly declared a champion instead.
- A sponsor wants the PCs to infiltrate the Judgment Terraces and alter a ruling.
- Cassia Vorn needs help reaching the arena floor under heavy divine scrutiny.
- A memorial name is being erased, and the PCs must decide whether to restore it or exploit the void.
- A secret meeting in the Godhand Passages is about to break into assassination.
- The Nameless God notices the PCs during an Apex Bout and decides to test them.

## Tone

The core ring should feel sacred, dangerous, and politically absolute. PCs who operate here are never just walking through a district; they are stepping into the machinery that decides who gets to matter in Asterion.
""",
)

add(
    "Asterion — Inner Ring", "location", "district", "Locations",
    "inner ring, Brazen Grid, Forge Underbelly, Hall of Reprisals, Echo Archive, Threshold Slums",
    "The working heart of Asterion — trade, law, craft, memory, plague, and oath-work in five districts.",
    """
The inner ring is the working heart of Asterion. It sits close enough to the Empyrean Apex to feel the pressure of the arena, but far enough away to hold trade, law, craft, memory, plague, and oath-work.

## Overview

Five major zones: **Brazen Grid** (contracts, markets, wagers, mortal commerce) · **Forge Underbelly** (divine smithing, repairs, weaponcraft) · **Hall of Reprisals** (oath-binding, punishment, contract law) · **Echo Archive** (memory, battle-song, tactical history) · **Threshold Slums** (healing, plague control, survival). Each zone has a different feel, power structure, and kind of visitor. PCs can use the inner ring for diplomacy, black-market shopping, research, sabotage, or to find strange allies before a bout.

## Brazen Grid

The central marketplace of divine Asterion. Stalls trade in obols, votives, favors, relic fragments, sponsorships, and one-time prayers. Nera the Debt-Scribe operates at the edge of the district; the twin smugglers Mira and Jaxon move through the crowds selling stolen Sparks, forged seals, and contraband messages.

**Key locations:** Ledger Row (counting houses, debt-tables, contract kiosks) · Coin Bridges (moneychangers and fence-runners) · Obsidian Market (the darkest, most dangerous part of the district, used for divine trade).

**NPCs:** Nera the Debt-Scribe · Mira and Jaxon · minor lenders and fence-runners.

## Forge Underbelly

Sits beneath the Colosseum itself and runs hot from the constant pressure of divine violence above it. Yngvi Ember-Hand reforges damaged relics into second-life weapons; Koru scavenges fragments from the arena floor and sells them to desperate fighters.

**Key locations:** Smelter Wells (molten pits for divine alloying) · Ash Tunnels (soot, hidden tools, black-market caches) · Second-Life Smithies (small workshops rebuilding broken weapons).

**NPCs:** Yngvi Ember-Hand · Koru · forge apprentices and heat-wrights.

## Hall of Reprisals

Where promises are made dangerous. Therasios, God of Broken Oaths, presides over obsidian tablets that bind contracts, marriages, mercenary deals, duels, and revenge clauses. Anyone who breaks a vow here risks losing memories, years of life, or pieces of their soul.

**Key locations:** Obsidian Tablets (carved contract stones storing living vows) · Oath Vault (a sealed chamber for impossible bargains and forbidden terms) · Shame Steps (a public staircase where oathbreakers are displayed).

**NPCs:** Therasios · Oath scribes · Supplicants and debtors.

## Echo Archive

A subterranean library of battle-songs, death-memories, and tactical records. Sarasvati the Faded curates the archive; Lysa of the Shattered Choir performs here too, turning death into music and music into weaponized memory.

**Key locations:** Notation Vaults (sealed chambers storing death-cries as song) · Choir Stairs (amphitheater-like performance steps) · Death-Score Chamber (recorded deaths replayed for study).

**NPCs:** Sarasvati the Faded · Lysa of the Shattered Choir · archivists and score-keepers.

## Threshold Slums

A district of survival, infection, and low-level miracles. Heket of the Red Nile runs a hidden bathhouse here; Varo the Stitch-Doctor keeps a nearby clinic patching up bodies with black-market healing and Primordial grafts.

**Key locations:** Red Bathhouse (Heket's place of healing and judgment) · Plague Walk (sickness, rumors, miracle cures) · Mend Alley (surgeons, stitchers, body-repair work).

**NPCs:** Heket of the Red Nile · Varo the Stitch-Doctor · patients, pilgrims, infected refugees.

## Inner Ring Connections

The Brazen Grid supplies coin and contracts to the Hall of Reprisals. The Forge Underbelly supplies weapons to the Arena and repair work to the Brazen Grid. The Echo Archive records the deaths the arena creates. The Threshold Slums supply healers, desperate labor, and bodies to all the other districts.

## Adventure Hooks

- A debt ledger disappears from the Brazen Grid, and everyone who signed it begins to receive impossible threats.
- Yngvi Ember-Hand asks the PCs to recover a weapon that fell into a furnace tunnel and woke up.
- Therasios offers to erase a contract, but only if the party brings him a broken oath from the arena.
- Sarasvati wants a story no living witness has ever told, and she is willing to trade rare battle knowledge for it.
- Heket suspects the plague in the slums is part of a deliberate divine cull.
- Varo has grafted a hidden Primordial fragment into a patient, and the patient is starting to change.

## Tone

The inner ring should feel crowded, practical, and morally unstable — the place where gods negotiate, mortals survive, and every useful thing has a hidden price.
""",
)

add(
    "Asterion — Outer Ring", "location", "district", "Locations",
    "outer ring, Festival Quarter, Duelist Quarter, Mask Market, Oracle Sands, Pit Arenas",
    "Where Asterion becomes unstable — eleven zones of spectacle, refugees, smugglers, pilgrims, and fanatics, less protected by direct divine order.",
    """
The outer ring is where Asterion becomes unstable. It is farther from the Colosseum, more crowded with refugees, smugglers, pilgrims, and fanatics, and less protected by direct divine order. The inner ring feeds on it, the arena ignores most of it, and the denizens here survive by improvisation.

## Overview

Eleven notable zones: **Festival Quarter** (spectacle, gambling, ritual joy, street performance) · **Duelist Quarter** (private honor fights, instruction, challenge culture) · **Mask Market** (identity trades, disguise work, social reinvention) · **Oracle Sands** (prophecy, fate-reading, burial rites, grim certainty) · **Stitch Row** (surgery, survival medicine, body repair) · **Pit Arenas** (illegal fighting pits, back-alley brutality) · **Pilgrims' Gate** (entry tolls, waystations, sanctuary for travelers) · **Night Sheet Alley** (gossip, printing, smuggling, rumor warfare) · **Shrine Sprawl** (street shrines, mural cults, neighborhood worship) · **Threshold Slums** (plague, healing, the edge of disappearance) · **Brazen Grid** (commerce, debt, contracts at the outer edge of trade).

The outer ring is the easiest place to lose yourself, but also one of the best places to build influence from nothing. Everyone here wants protection, payment, or proof that you matter.

## Festival Quarter

Never fully sleeps. Xochipilli of Ruinous Joy keeps the streets soaked in music, dice games, masked performances, and final-night celebrations. Izel is often seen in the crowds; the Painted Choir paints prophecies on the walls after midnight.

**Key locations:** Carnival Stalls · Last Nights Parade · Knife Lantern Way. **NPCs:** Xochipilli of Ruinous Joy · Izel · The Painted Choir.

## Duelist Quarter

For those who believe honor can be practiced, not just claimed. Kallisti sponsors the cleanest blades and most precise fighters; Damaris works here when escort jobs or lessons in discipline are needed.

**Key locations:** White Pavilion · Oath-Bettor Steps · Mirror Yard. **NPCs:** Kallisti · Sir Damaris · Duel referees and sparring students.

## Mask Market

Identity treated as a commodity. Lady Mawu runs shrines and stalls where masks can alter voice, posture, and luck. Many fugitives, exiles, and disgraced champions come here to become someone else.

**Key locations:** House of Faces · Shifting Alley · Veil Stalls. **NPCs:** Lady Mawu · Izel · Mask-smiths and reputation brokers.

## Oracle Sands

The outer ring's place of fatal truth. Abraxos reads possible defeats here; Namtarion tells warriors what kind of blow will one day kill them.

**Key locations:** Bone Sun Obelisk · Silt Chapel · Fate Dunes. **NPCs:** Abraxos of the Third Eye · Namtarion · Truth-seekers, mourners, and fatalists.

## Stitch Row

The body-repair district. Varo runs a clinic here; Lysa of the Shattered Choir often passes through, singing fragments of the dead.

**Key locations:** Graft Clinic · Suture Docks · Needle Walk. **NPCs:** Varo the Stitch-Doctor · Lysa of the Shattered Choir · Apprentice medics and graft scalpers.

## Pit Arenas

The illegal answer to the Colosseum. Targos the Chain-Priest officiates here; Koru scavenges for broken weapons and discarded champion gear.

**Key locations:** Sunken Pits · Chained Rings · Blood Steps. **NPCs:** Targos the Chain-Priest · Koru · Pit fighters and handlers.

## Pilgrims' Gate

The district of arrival and departure. Damaris often works here as a protector; Master Jinhai sometimes arbitrates disputes; Old Mother Rhysa watches over the harmless and the helpless.

**Key locations:** Toll Arch · Pilgrim Steps · Sanctuary Courtyard. **NPCs:** Sir Damaris · Master Jinhai · Old Mother Rhysa.

## Night Sheet Alley

Where Asterion learns what it already knows. Selene of the Brass Quill prints the Night Sheet here; Mira and Jaxon use the same lanes to move contraband.

**Key locations:** Brass Press · Ink Warrens · Whisper Steps. **NPCs:** Selene of the Brass Quill · Mira and Jaxon · Readers, couriers, and snitches.

## Shrine Sprawl

A patchwork of neighborhood altars, wall murals, and half-forgotten local gods. The Painted Choir leaves its work here; Rhysa sometimes tends to children and street people.

**Key locations:** Wall of Murals · Choir Steps · Lantern Shrines. **NPCs:** The Painted Choir · Old Mother Rhysa · Neighborhood shrine-keepers.

## Brazen Grid (Outer Edge)

Where commerce still feels divine, even at its lowest level. Nera the Debt-Scribe manages wagers and blood-debts here too; the twin smugglers move relics and illegal goods through crowded alleys.

**Key locations:** Ledger Market · Coin Bridges · Obsidian Exchange. **NPCs:** Nera the Debt-Scribe · Mira and Jaxon · Lenders, brokers, and debt collectors.

## Outer Ring Connections

The Festival Quarter feeds crowds and gossip into Night Sheet Alley. The Duelist Quarter supplies disciplined fighters to the Pit Arenas. The Mask Market supplies disguises to smugglers and fugitives. The Oracle Sands influences politics everywhere because everyone fears prophecy. Stitch Row keeps the whole ring alive through repair and grafts. Pilgrims' Gate brings new people into the city and often decides whether they stay alive.

## Adventure Hooks

- A masked prophet claims one of the PCs has already died three different ways.
- Xochipilli invites the party to a festival where the final prize is a secret.
- Lady Mawu offers a disguise that could save a life, but it may also change it forever.
- Abraxos sees a defeat involving a PC and refuses to say who survives.
- Varo needs the PCs to escort a graft shipment through gang territory.
- Selene's next issue of the Night Sheet threatens to start a riot.

## Tone

The outer ring should feel alive, unstable, and crowded with people who are one bad day away from a miracle or a massacre.
""",
)

add(
    "Cosmic Map Around Mount Olymp", "location", "cosmology", "Locations",
    "cosmology, Mount Olymp, pantheon spheres, Dead Domain Belt",
    "The spatial layout of the surviving pantheon spheres around Mount Olymp, with Asterion straddling the Olympian Sphere and the Dead Domain Belt.",
    """
Mount Olymp stands at the metaphysical center of the surviving divine order: it is the seat whose favor grants ruined realms to ascended gods (see the Domain Reclamation rules), while Asterion remains a major city-domain under that wider structure rather than the cosmic center itself.

## Central Axis

At the center sits **Mount Olymp**, the seat of the High Gods and the authority that grants Dead Domains, ruined heavens, and decaying pocket worlds to newly ascended deities for reclamation. Asterion is not above Olymp; it is a major divine city connected to the larger cosmic order and can link to domains through advanced portal structures such as the Empyrean Gate.

## Inner Circle: Surviving Pantheons

The largest surviving civilizational blocs closest to Mount Olymp, old enough and powerful enough to retain coherent divine identity after the Primordial War.

1. **Olympian Sphere** — Mount Olymp at the center, seat of the High Gods. **Asterion** sits at the edge of the Olympian order, half in the living divine circle and half in the Dead Domain Belt, a brutal shrine-city ruled through divine violence, spectacle, and salvage.
2. **Egyptian Sphere** — solar courts, death-river kingdoms, embalmed star vaults, jackal roads, tomb-realms, judgment halls. Themes: kingship, death, resurrection, law, sacred architecture.
3. **Norse Sphere** — world-tree fragments, frost ruins, mead halls, wolf-haunted battlefields, broken rainbow roads, giant-scarred borderlands. Themes: fate, heroic doom, winter, oaths, apocalyptic survival.
4. **Indic Sphere** — vast layered heavens, lotus oceans, cosmic mountains, reincarnation rivers, demon war-frontiers, cities of philosophy and divine weaponry. Themes: cyclical time, cosmic duty, avatars, enlightenment, celestial war.
5. **Sino-Japanese Eastern Sphere** — jade courts, storm palaces, ancestor provinces, oni borderlands, moon capitals, dragon rivers, fox roads, bureaucratic spirit cities. (Can later be split into separate Chinese and Japanese macro-domains.)
6. **Mesopotamian Sphere** — ziggurat heavens, lion gates, star tablets, flood plains of memory, underworld roads, storm-throne citadels. Themes: kingship, divine decree, astronomy, flood, sacred urbanism.

## Outer Circle: Additional Pantheon Domains

Farther from Olymp — more fragmented, less centralized, or survived the Primordial War in shattered but still recognizable blocs.

7. **Celtic Sphere** — mist isles, war-goddess moors, underhill courts, cauldron sanctuaries, severed-head shrines, twilight crossings.
8. **Slavic Sphere** — forest shrines, thunder peaks, witch-roads, hut-realms on chicken-legged horizons, drowned river kingdoms, winter spirit marches.
9. **Yoruba Sphere** — storm kingdoms, masked ancestral courts, river-goddess provinces, thunder forges, crossroads markets, ecstatic sacred groves.
10. **Mesoamerican Sphere** — sun-pyramid states, obsidian causeways, feathered serpent skies, sacrificial courts, jungle underworlds, calendar-bound war domains.
11. **Greco-Eastern Syncretic Fringe** — mixed territories where old gods survived by merger rather than purity: trade gods, mask gods, minor war gods, household divinities whose worship crossed imperial routes. Fits especially well for cults built around cross-pantheon migration and reinvention among lesser gods, since Asterion already shows syncretic divine migration among its own minor deities.
12. **Dead Domain Belt** — beyond the surviving pantheon spheres lies the graveyard fringe of ruined pocket worlds and broken heavens handed out by Olymp to rising gods. Not stable pantheon homelands — salvage territories, ruins, and future cosmoi waiting to be reclaimed (see the Domain Reclamation rules).

## Suggested Spatial Layout

| Position | Domain Block | Notes |
| :--- | :--- | :--- |
| Center | Mount Olymp | Seat of the High Gods |
| Boundary zone | Asterion | Half city, half ruin, straddling the outer circle and the Dead Domain Belt |
| East | Egyptian Sphere | Death, kingship, sun, tomb cosmology |
| North-east | Norse Sphere | Doom, frost, heroic battle, world-tree remnants |
| South-east | Indic Sphere | Cycles, avatars, cosmic law, demon wars |
| Far east | Sino-Japanese Eastern Sphere | Bureaucratic heaven, storms, dragons, oni, ancestors |
| South | Mesopotamian Sphere | Ziggurats, stars, flood, decrees |
| North-west | Celtic Sphere | Mists, twilight, underhill courts |
| West | Slavic Sphere | Forest, thunder, winter spirits |
| South-west | Yoruba Sphere | Storms, river gods, crossroads, masked ancestors |
| Far west | Mesoamerican Sphere | Sun-war states, sacrifice, calendar empires |
| Outer ring | Dead Domain Belt | Ruined realms granted for reclamation |

## Campaign Use

This map makes Mount Olymp feel like the old surviving center of divine legitimacy, while Asterion becomes a liminal city sitting between orderly divine civilization and the wreckage of dead realms. It gives clear space for future gods, reclaimed Dead Domains, and invasions or pilgrimages between pantheon spheres.
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Rules Supplement — Roleplay Glory, Ambitions, Followers, Reputation
# ────────────────────────────────────────────────────────────────────────────

add(
    "Roleplay & Ambition Glory", "note", "lore", "Rules",
    "glory, roleplay awards, divine ambition, session-end",
    "Session-end roleplay Glory checklist and the Divine Ambition goal system, extending the core Progression rules.",
    """
This supplement extends the Asterion Unified Rulebook's Progression and Glory rules. All Glory awarded here uses the same currency defined in the core rules: "Glory is this game's only advancement currency."

## Session-End Roleplay Awards

At the end of every session, the GM awards bonus Glory on top of any earned through play, using this checklist. Each award is independent and optional.

| Award | Glory | Trigger |
| :--- | :--- | :--- |
| In-Character Presence | 1 Glory | You consistently spoke, acted, and made decisions in character rather than as a player giving instructions. |
| Domain-True Roleplay | 1 Glory | In at least one scene, you made a choice that reflects your Divine Spark, Origin/Lineage, or Character Sentence — even when it was inconvenient or costly. |
| Memorable Moment | 1 Glory | You contributed a scene, line, or decision the GM or table specifically remembers and enjoyed. |
| Table Spotlight | 1 Glory | You gave another player room to shine, set up their success, or pulled focus toward a quieter character. |

**Maximum 4 bonus Glory per session** from this checklist.

## Divine Ambition Goals

At character creation, or any time after with GM approval, every player writes one **Divine Ambition**: a specific, personal goal tied directly to their Divine Spark or Origin/Lineage that they want their god or mythborn to pursue in Asterion.

- **Examples:** A God of Broken Oaths wants to force a specific rival to publicly break a vow; a Storm-Spark god wants to be worshipped during the next Festival of Last Nights; a Fire-Hearted Giant wants to burn down a named enemy's stronghold.
- Ambitions must be concrete enough that the GM can recognize when they succeed or fail. "Become powerful" is too vague. "Claim the Ashen Forge as my shrine" is not.

### Resolving an Ambition

| Result | Glory | Notes |
| :--- | :--- | :--- |
| Ambition Achieved | 8 Glory | Paid once, immediately, in the session it's completed. |
| Ambition Achieved at Great Cost | +2 Glory bonus | The player accepted a serious permanent complication, injury, enemy, or narrative cost to succeed. |
| Ambition Failed Dramatically | 3 Glory | The attempt failed, but created meaningful fallout, drama, or a new plot thread. Failing badly is still rewarded — retreating quietly is not. |

Once an Ambition is resolved (achieved or permanently failed), the player writes a new one before or during the next session. A character may only have **one active Divine Ambition at a time**, keeping their personal arc focused.
""",
)

add(
    "Reputation", "note", "lore", "Rules",
    "reputation, infamy, positive track, negative track",
    "A single tracked score of fame or infamy across Asterion, with a positive track and a negative (Cursed Name) track.",
    """
Reputation is a single tracked score representing how well-known, feared, respected, or reviled your character is across Asterion. Unlike Glory, Reputation can go negative — a character can become infamous rather than merely unknown.

## Positive Reputation Track

| Reputation Rank | Range | Effect |
| :--- | :--- | :--- |
| Unknown | 0–2 | No mechanical benefit. Most NPCs have never heard of you. |
| Recognized | 3–5 | +1d10 on social rolls with factions or individuals who share your Domain's values or enemies. |
| Renowned | 6–8 | As above, and mortals in your Domain's sphere of influence offer minor aid (shelter, tips, discounts) without being asked. |
| Legendary | 9+ | As above, and once per session you may declare that an NPC has already heard of your deeds and reacts accordingly — the GM narrates the specifics. |

## Negative Reputation Track

If your actions repeatedly betray your Character Sentence, target innocents, or make you a public menace, your Reputation can fall below 0 into the negative track.

| Reputation Rank | Range | Effect |
| :--- | :--- | :--- |
| Distrusted | -1 to -2 | -1d10 on social rolls with factions or individuals who oppose your Domain's values or methods. Merchants may refuse service or charge double. |
| Notorious | -3 to -5 | As above, and NPCs who recognize you may flee, alert authorities, or refuse to deal with you at all. Bounty hunters and rival factions may actively seek you out between sessions at the GM's discretion. |
| Infamous | -6 to -8 | As above, and any faction hostile to you gains +1d10 when specifically hunting or ambushing you. The GM may introduce a dedicated rival, bounty, or hit squad tied to your infamy. |
| Cursed Name | -9 or lower | As above, and your name alone can trigger Fear-based Status Conditions or hostile action from strangers who recognize you on sight. Entire factions or Domains may bar you from entry. Removing this rank requires a major narrative act of redemption, not just positive Reputation gains over time. |

Negative Reputation effects stack with any Passive Curse or Trade-Off drawbacks a character already carries — they represent the world's reaction, not a mechanical penalty built into the character sheet.

## Gaining and Losing Reputation

**Gains (+Reputation):** +1 whenever you achieve a Divine Ambition · +1 whenever you win a public arena bout, complete a faction contract, or otherwise perform a witnessed feat that aligns with your Divine Spark · the GM may award +1 for any action that would plausibly spread by word of mouth through Asterion in your favor.

**Losses (-Reputation):** -1 for a public defeat, broken oath, or failure that contradicts your Character Sentence · -1 to -3 for a witnessed act of cruelty, betrayal, or atrocity, at GM discretion based on severity · -2 for breaking a formal contract, truce, or faction agreement in a public way.

**Climbing Out of the Negative Track:** Positive Reputation gains apply normally and slowly pull a negative score back toward 0. However, escaping **Cursed Name** (-9 or lower) additionally requires completing a dedicated narrative arc of redemption approved by the GM — Glory and roleplay alone cannot buy back a ruined name at that depth.

Reputation is shared knowledge at the table. GMs should let Reputation shift visibly and narrate rumors, gossip, and reactions as it changes in either direction.
""",
)

add(
    "Followers", "note", "lore", "Rules",
    "followers, devotee, disciple, champion, recruitment",
    "The Follower subsystem: recruiting, tiering, and using mortal or mythborn followers who travel with your character.",
    """
Followers are mortals, mythborn, or minor spirits who actively serve, worship, or fight for your character. Unlike Domain Features, Followers travel with you and can be brought into scenes outside your Domain.

## Gaining Followers

Followers are recruited three ways:

1. **Domain Yield Feature:** A Yield/Resources Domain Feature built with the "followers" property generates a small pool of Followers automatically each downtime period.
2. **Glory Purchase:** Spend Glory directly to recruit a named Follower, using the table below.
3. **Narrative Recruitment:** The GM may offer a Follower for free as a reward for a major story beat, with no Glory cost, at their discretion.

| Follower Tier | Glory Cost | Capability |
| :--- | :--- | :--- |
| Tier 1 — Devotee | 3 Glory | A mortal with 1d10 pool and no abilities. Can run errands, gather information, or fight as a mob (1 Flesh, no Armor). |
| Tier 2 — Disciple | 7 Glory | A capable mortal or minor mythborn with 2d10 pool and one Tier 1 ability drawn from your Divine Spark. |
| Tier 3 — Champion | 15 Glory | A named, distinct mythborn or minor god with 2d10 pool (3d10 if acting in your Domain) and one Tier 2 ability drawn from your Divine Spark. |

You may have a number of Followers active at once equal to your **Domain Rank + 1** (minimum 1, even at Rank 0). Followers beyond this limit remain at your Domain or shrine, unavailable for the current session.

## Using Followers

- Followers act on your turn as an extension of your Main Action, or may be given a simple standing order ("guard this door," "spy on that senator") to resolve between scenes with a single roll from the GM.
- A Follower who is Shattered in combat is removed from play. Recovering a lost Follower requires spending their full Glory cost again, or a downtime period of recruitment if the GM allows.
- Followers do not gain Glory or grow independently — they remain at the Tier they were recruited or upgraded to until you spend Glory to improve them (same cost table, paying the difference between Tiers).

## Reputation and Followers Interaction

Your current Reputation Rank affects how easily you recruit and how reliably Followers behave:

| Reputation Rank | Recruitment Effect |
| :--- | :--- |
| Unknown / Recognized | You must actively seek out and convince potential Followers. |
| Renowned / Legendary | Followers may approach you unprompted; the GM can introduce a recruitment opportunity as a scene rather than requiring you to search one out. |
| Distrusted / Notorious | Existing Followers require a Loyalty check (GM-set difficulty) after any major public disgrace, or they may abandon you. |
| Infamous / Cursed Name | Only Followers who share or benefit from your infamy will willingly join. Recruiting ordinary Devotees costs double Glory, reflecting the risk of associating with you. |

*Cross-reference with the Character Creation, Domain Reclamation, and Ability Construction rules notes for full context.*
""",
)

print(f"Batch2 FINAL: {len(entities)} entities")

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(json.dumps(entities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {OUT_PATH} — {len(entities)} entities (FINAL)")
