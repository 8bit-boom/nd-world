"""
Generates worlds/asterion-game-of-gods.json — an nd-world world-import file for the
"Game of Gods" TTRPG (Asterion Unified Rulebook).

Source material: the Asterion Unified Rulebook, the Morvain the Withered character
sheet, and the Ascended Loki, the Hollow Crown boss statblock. The two blank fillable
sheet templates (character sheet v2, NPC stat block) contained no actual character
data, so nothing was transcribed from them.

Usage:
    python3 scripts/generate_asterion_world.py
"""

import json
from pathlib import Path

OUT_PATH = Path(__file__).parent.parent / "worlds" / "asterion-game-of-gods.json"

WORLD = {
    "name": "Asterion",
    "slug": "asterion",
    "description": (
        "The City of Nine Thousand Shrines — a hyper-dense, gladiator-driven "
        "metropolis where Minor Gods and Mythborn fight for territory, glory, and "
        "survival under the rule of the Nameless God of War. Runs on the Game of "
        "Gods system (Asterion Unified Rulebook)."
    ),
    "accent": "#c9a227",
}

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
# Rules — Note entities, chunked along the rulebook's own section headers
# ────────────────────────────────────────────────────────────────────────────

add(
    "Core Mechanics & Combat Flow", "note", "lore", "Rules",
    "core mechanic, dice pool, combat, initiative, opposed rolls, health",
    "Dice pools, turn structure, opposed rolls, and the Spark Shield / Flesh / Ichor resource triad.",
    """
## Core Mechanic

Every action with a chance of failure uses a d10 dice pool.

- **Success:** Any die that rolls a 6 or higher counts as 1 Success.
- **Exploding 10s:** If you roll a 10, it counts as 1 Success, and you roll an extra d10 to add to your pool. If that extra die is a 10, it explodes again.

### Dice Pools

- **Mortals:** Roll 1d10 for everything.
- **Gods and Mythborn:** Roll 2d10 for any standard action such as attacking with a basic weapon, dodging, or lifting a boulder.
- **Domain Pool:** Roll 3d10 if the action directly connects to your Divine Spark, Mythological Lineage, or Epic Deed.
- **Pushing Your Limits:** Before rolling, you may spend 1 Ichor to add +1d10 to your pool. You can do this multiple times if you have the Ichor to spend.

## Combat Flow

Asterion uses a fluid, narrative-driven initiative system.

### Initiative and Turns

- **The Instigator Goes First:** Whoever initiates the violence, springs the ambush, or declares the first aggressive action takes the first turn.
- **Passing the Turn:** After a character finishes their turn, they choose who goes next. They can pass the turn to an ally to set up a combo, or pass it to an enemy if all allies have acted.
- **The Round Ends:** Once every character (player and NPC) has taken a turn, the round ends. The last person to act in the current round chooses who takes the first turn in the next round.

### Anatomy of a Turn

On your turn, you may take one Movement and one Main Action. You may also take any number of Free Actions such as speaking or dropping an item.

- **Movement:** Move up to your standard movement speed, usually 30 feet / 9 meters / 6 hexes.
- **Splitting Movement:** You can break up your movement, move partway, take your main action, then continue moving.

### Main Action Options

1. Basic Attack: Strike with a weapon or fists.
2. Cast an Active Ability: Use a Spark, Lineage, or Deed ability, paying any required Ichor.
3. Defend / Brace: Gain +1d10 to all your Defender pools until the start of your next turn.
4. Interact / Skill Check: Perform a complex action such as lifting a massive pillar or disarming a trap.
5. Dash: Double your movement speed for the turn.
6. Grapple / Throw: Initiate a physical contest.

### Reactions

You get one Reaction per round, which refreshes at the start of a new round.

- **Opportunity Attack:** If an enemy moves out of your melee range, use your Reaction to make a Basic Attack.
- **Ability-Specific Reactions:** Use evasive teleports, counterspells, or defensive buffs.

## Opposed Rolls

Whenever two entities clash, Asterion uses an opposed roll system. Both the Attacker and the Defender roll their d10 dice pools simultaneously.

### The Clash

- **Attacker Pool:** Usually 2d10, or 3d10 if using your divine element or nature.
- **Defender Pool:** Usually 2d10.
- **Pushing Limits:** Either side may spend 1 Ichor to add +1d10 to their pool before rolling.
- Count the number of Successes (6 or higher) for both sides. Remember that rolling a 10 explodes and adds another die.

### Resolution Results

- **Attacker Successes > Defender Successes:** The attack hits.
- **Damage Dealt =** Attacker Successes minus Defender Successes, plus ability base damage.
- **Defender Successes > Attacker Successes:** The attack is completely dodged, parried, or absorbed. No damage is taken.
- **Tie:** Both combatants take 1 damage, and the attacker's action is resolved. This tie damage bypasses Armor and Resistances but is absorbed by Spark Shield as normal. It is not reduced by any mitigation.

### Damage Mitigation

After calculating incoming damage, apply mitigations.

- **Resistances:** If the Defender has Resistance to the damage type, the Attacker automatically loses 1 Success from their total before damage is calculated.
- **Armor:** If the damage is physical, subtract the Defender's Armor value from the final damage taken.

## Health and Resources

Every god and mythborn tracks three vital resources during combat.

### Spark Shield
- **Base Value:** 3
- **Mechanic:** An invisible aura of divine authority. All damage is dealt to the Spark Shield first.
- **Regeneration:** Your Spark Shield regenerates fully at the start of every combat encounter.

### Flesh
- **Base Value:** 5
- **Mechanic:** Your physical body. Once your Spark Shield drops to 0, damage spills over into your Flesh.
- **Shattering and Death:** If Flesh drops to 0, you are Shattered and lose the bout or fight. If an enemy executes you while Shattered, or if you fail Death Saves, your physical form is destroyed.

### Ichor
- **Base Value:** 5
- **Mechanic:** Spent to use Active Abilities or added to a dice pool at a rate of +1d10 per 1 Ichor spent.
""",
)

add(
    "Advanced Tactics & Status Conditions", "note", "lore", "Rules",
    "range, area of effect, grapple, cover, divine resonance, synergy, status conditions",
    "Ranges/AoE, grappling and throwing, cover, Divine Resonance synergy, and the six status conditions.",
    """
## Ranges and Areas of Effect

- **Melee:** Adjacent (0–5 feet / 0–1.5 meters).
- **Close Range:** Within 30 feet / 9 meters.
- **Long Range:** Within 100 feet / 30 meters.
- **Extreme Range:** Line of sight, 300+ feet / 90+ meters.

AoE abilities force all targets in the area to roll a Defender pool against the Attacker's single roll.

## Grappling and Throwing

- **Grappling:** Make an opposed physical roll. If the Attacker wins, the target is Restrained (0 Movement). You can move at half speed while dragging them.
- **Throwing:** If you have an enemy grappled, use your Main Action to throw them. Make a new Opposed Roll. If you win, you throw them 5 feet / 1.5 meters per net success. Collision causes physical damage equal to the net successes.

## Cover and Elevation

- **Half Cover:** Grants the Defender +1d10 to their Defender pool against ranged attacks.
- **Total Cover:** You cannot be targeted by direct ranged attacks.
- **High Ground:** Grants the Attacker +1d10 to their Attacker pool.

## Divine Resonance

As a deity or mythborn, your physical form and divine magic are naturally intertwined. When you describe an action that fluidly combines your Origin or Lineage and your Spark, you achieve Divine Resonance.

- You cannot synergize your Deed or Curse. The mythic weight of your ultimate move or permanent curse is too absolute and reality-warping to be combined with lesser abilities.
- The cost to synergize is 1 additional Ichor on top of the highest cost of the combined abilities.
  - *Example:* Combining an Active Spark (1 Ichor) with a Passive Lineage (0 Ichor): base highest cost is 1, plus +1 synergy tax = **2 Ichor total**.
  - *Example:* Combining an Active Spark (2 Ichor) with an Active Lineage (1 Ichor): **3 Ichor total**.
- Because you are acting in perfect alignment with your mythic self, you roll your full Domain Pool (3d10).
- The GM grants a logical advantage based on your description, such as bypassing Armor or Resistance, inflicting a hard Status Condition, or turning a single-target strike into an Area of Effect.

**Synergy Examples**

- **Active + Active:** A *Storm Serpent* uses an Active Lightning Strike and an Active constricting coil to create a thunder-charged bind that shocks and restrains the target simultaneously. A *Sun-Spear Archer* fires a radiant bolt while their Lineage manifests a blinding burst from their eyes, turning one hit into a flare that damages and blinds.
- **Active + Passive:** A *Fire-Hearted Giant* with a Passive body-heat aura combines it with an Active volcanic stomp to create a lava burst that scorches enemies in a radius and leaves the ground burning. A *River-Born Oracle* whose Passive water-breathing body cools and stabilizes an Active healing mist, making the mist restore more Flesh and soothe fire-based wounds.
- **Passive + Passive:** A *Stone-Skinned Oracle* combines a Passive prophetic trance with a Passive shadow aura to become a silent, unblinking omen of doom—gaining improved stealth in darkness and a defensive ward that makes them harder to hit while stationary.

### Passive Ability Synergy

If you want to synergize two Passive abilities, the total cost is 1 Ichor. Spending this Ichor allows you to flare your combined passives, creating a powerful new environmental effect or defensive state that lasts for the rest of the scene or combat encounter.

*Example:* A god with a *Passive Spark of Fire* (body radiates heat) and a *Passive Stone Origin* (stone skin) spends 1 Ichor to resonate them. For the rest of the combat, their stone skin turns to molten magma—granting bonus Armor and causing any enemy who strikes them in melee to take automatic Fire damage.

## Status Conditions

- **Blinded:** Ranged attacks automatically fail. You lose 2 dice from your Attacker pool for melee strikes.
- **Burning / Bleeding:** Take 1 damage at the start of your turn. This damage bypasses Armor and Resistances, but is absorbed by Spark Shield first. If Spark Shield is at 0, it hits Flesh directly.
- **Restrained:** Movement speed is 0.
- **Stunned:** Cannot take a Main Action or Movement. You roll 1 fewer die in your Defender pool.
- **Weakened:** Roll 1 fewer die in your Attacker pool.
- **Vulnerable:** Your Armor is reduced to 0, and you lose any Resistances.
""",
)

add(
    "Character Creation & Ability Construction", "note", "lore", "Rules",
    "character creation, origin, lineage, spark, epic deed, tiers, trade-off",
    "The narrative-first Character Sentence, the three starting abilities, and the Tier property tables for building Active/Passive abilities.",
    """
## Character Creation

Character creation is narrative-first. You define your character by writing a single sentence, then invent three specific mechanical abilities based on that sentence.

### Character Sentence

> "I am a **[Origin / Lineage]** who wields the Spark of **[Divine Spark]**, known for my **[Epic Deed / Mythic Curse]**."

- **Origin (For Gods):** How you attained your divinity (e.g., *Ascended Mortal, Bearer of a Stolen Mantle, Fallen Star, Forgotten Idol*).
- **Lineage (For Mythborn):** Your mythological species (e.g., *Bronze Minotaur, Ash-born Gorgon, Harpy*).

### The Three Starting Abilities

You invent exactly one ability for each part of your sentence. At character creation, you start with two Tier 1 abilities and one Tier 2 ability. Any of these three abilities can be Active or Passive.

Optional Exception: You may start with one Tier 3 ability instead of a Tier 2, or two Tier 2 abilities instead of one of the Tier 1 picks. To do this, you must apply a permanent Trade-Off to the upgraded ability.

### Ability Sources

**Origin / Lineage Ability** — represents your physiology, divine birthright, or species. Most players build this as a Passive ability (innate Armor, natural Resistances, Movement, Superhuman Senses). It can also be Active if it reflects a biological function or innate species weapon (a Gorgon's petrifying gaze, a Minotaur's charging gore).

**Spark Ability** — represents your primary godly domain and what you actively manipulate in the world. Most players build this as an Active ability (Range, Damage, or Soft Crowd Control). If built as a Passive, it acts as a permanent environmental aura (a God of Shadows being naturally invisible in the dark).

**Deed / Curse Ability** — represents the echo of your greatest mythic triumph or darkest tragedy.
- **Active Deed (Once Per Session):** If built as an Active ability, it can only be used once per session. This severe restriction automatically counts as the Trade-Off needed to build a starting Tier 3 ability. *Multiple Active Deeds:* as you spend Glory, you may invent multiple Active Deed abilities, but you can still only use **one Active Deed per session in total** — choose which is available at the start of each session; the choice is locked until the next session, even on a Short Rest.
- **Passive Curse (One Burden):** If built as a Passive, the once-per-session limit is replaced by a permanent severe narrative drawback. *Multiple Passive Curses:* if you invent multiple, you can only bear the weight of **one at a time** — a Short Rest is required to swap which is active.

## Ability Construction

Because Asterion offers massive freedom in freeform ability creation, the tables below are guidelines, not strict restrictions. GMs and players are encouraged to bend these rules if an ability perfectly fits the narrative, or to balance narrative extremes using the Trade-Off rule.

### Active Abilities

The baseline default is: Base Damage 0, Melee or Touch Range, Single Target, Instant Duration, No Special Effect. You build your ability by selecting enhancements from the table based on the ability's Tier.

- **Tier 1 (Cost: 0 Ichor):** Choose 1 property from the Tier 1 column.
- **Tier 2 (Cost: 1 Ichor):** Choose 1 property from the Tier 2 column and 1 from Tier 1.
- **Tier 3 (Cost: 3 Ichor):** Choose 1 property from Tier 3, 1 from Tier 2, and 1 from Tier 1.

| Property | Tier 1 | Tier 2 | Tier 3 |
| :--- | :--- | :--- | :--- |
| **Base Damage** | 1 Damage | 2 Damage | 4 Damage |
| **Range** | 30 feet (9 m) | 100 feet (30 m) | Up to 1 mile / No Line of Sight |
| **Area of Effect** | 5-foot radius (adjacent targets) | 15-foot radius (small room) | 60-foot radius (city block / arena) |
| **Duration** | Up to 1 minute | Up to 10 minutes | Up to 1 hour / Permanent until dispelled |
| **Special Effect** | Minor narrative effect (push 5 ft, light a candle) | Soft Crowd Control (blind 1 turn, slow, damage over time) | Hard Crowd Control (paralyze in stone, alter physical architecture) |
| **Restoration** | Restore 1 Flesh | Restore 2 Flesh OR 1 Ichor | Restore 4 Flesh OR 2 Ichor |

**Example — Building a Tier 2 Ability (*Hurl Fireball*):** Pick **Tier 2 Range** (100 feet) and **Tier 1 Base Damage** (1 Damage). That fills both slots for a Tier 2 ability. If you also want a 15-foot AoE blast at 100 feet, that is two Tier 2 properties on one Tier 2 ability — apply a Trade-Off to allow it.

### Restorative Abilities — The Infinite Loop Rule

If you build an ability that restores Flesh or Ichor, it must require an external source, specific condition, or Trade-Off. You cannot infinitely heal yourself in an empty room.

*Example (God of Blood):* Restores 2 Flesh (Tier 2 Restoration), but only when dealing damage to a bleeding enemy.

### Passive Abilities

Passive abilities do not cost Ichor and are always active.

- **Tier 1 Passive:** Choose 1 property from the Tier 1 column.
- **Tier 2 Passive:** Choose 1 property from the Tier 2 column and 1 from Tier 1.
- **Tier 3 Passive:** Choose 1 property from Tier 3, 1 from Tier 2, and 1 from Tier 1.

| Property | Tier 1 | Tier 2 | Tier 3 |
| :--- | :--- | :--- | :--- |
| **Spark Shield** | +1 Max Shield | +2 Max Shield | +3 Max Shield |
| **Armor** | *(None)* | +1 Armor | +2 Armor |
| **Resistance** | Mundane Immunity (normal cold, water breathing) | Resistance (−1 Attacker Success) | Greater Resistance (−2 Attacker Successes) |
| **Movement** | Minor trait (swim speed, climb speed) | Special Movement (flight or teleport at walking speed) | Fast Movement (flight or teleport at double speed) |
| **Senses** | 1 Superhuman Sense (darkvision, scent, hearing) | Advanced Sense (see through walls, detect magic aura) | Mythic Sense (read surface thoughts, truesight) |

Spark Shield, Armor, Resistances, Movement, and Senses cannot be bought as raw stats; you must invent or upgrade a Passive Ability to acquire them.

**Resistance Stacking:** Resistances of the same type do not stack. If you acquire two Resistance abilities for the same damage type, treat them as a single Greater Resistance (−2 Attacker Successes). Greater Resistance cannot stack further.

### Trade-Off Rule

If a player wants an ability to do something slightly outside its allocated properties, they can take a Trade-Off:

- **Self-Harm:** The ability costs Flesh AND Ichor (e.g., spending 1 Ichor and losing 1 Flesh to activate).
- **Collateral Damage:** The ability harms allies just as much as enemies.
- **Charge-Up:** Takes one full turn of concentration to cast. Roll 1 fewer die for Defense while charging.
- **Specific Condition:** Only works under strict circumstances (e.g., "only in direct sunlight," "only against bleeding targets").
""",
)

add(
    "Progression, Glory & Domain Reclamation", "note", "lore", "Rules",
    "glory, xp, progression, domain rank, great wonder",
    "How Glory is earned and spent, resting/restoration, Domain Rank 0-3 growth, and Great Wonders.",
    """
## Progression and Glory

Gods level up by gaining Glory, which the GM awards at the end of a session.

- +1 Glory for surviving and advancing the plot.
- +1 Glory for performing a massive, public miracle witnessed by mortals.
- +2 Glory for winning an Apex Bout or defeating a major rival.

### Spending Glory

Glory is this game's only advancement currency. Costs listed as "XP" anywhere in the rulebook mean Glory spent.

- **Cost 3 Glory:** Upgrade an existing ability by adding a new property from its permitted Tiers.
- **Cost Scaling:** If you upgrade an ability so that it has more than one property from its highest Tier, its Ichor cost permanently increases by +1 for each additional highest-tier property.
- **Cost 4 / 7 / 10 Glory:** Invent a brand-new Tier 1 / Tier 2 / Tier 3 ability. *(A Tier 1 base costs 4 Glory; adding a Tier 2 property is +3 Glory = 7 Glory total for a full Tier 2 ability.)*
- **Cost 10 Glory:** Broaden your Domain by adding a new word to your Spark (e.g., Spark of *Fire and Ash*), expanding what you can roll 3d10 for.

### Stat and Resource Upgrades

- **Cost 1 Glory:** Increase Maximum Flesh by +1. Each purchase scales up by +1 Glory.
- **Cost 1 Glory:** Increase Maximum Ichor by +1. Each purchase scales up by +1 Glory.

## Restoring Flesh and Ichor

- **Short Rest:** Fully restores Spark Shield and regenerates 2 Ichor.
- **Long Rest:** Fully restores all Flesh and all Ichor.
- **Ambrosia:** Instantly restores 5 Ichor.
- **Golden Apple:** Instantly restores 10 Flesh.

## Domain Reclamation

Earning the favor of Mount Olymp is only the beginning. High Gods rarely hand out pristine, functioning universes to newly ascended deities. Instead, they grant Dead Domains—shattered realms, decaying pocket dimensions, or ruined heavens left over from the Primordial War.

When you are granted a Domain, it is a dangerous, feral place. You must conquer it, clean out the squatters, and rebuild it piece by piece to truly claim your godhood.

### Entering the Ruin

When you first arrive at your granted Domain, you must define its Original Concept (what this place was before it fell) and its Current Decay (what is wrong with it now).

### Domain Ranks and Growth

- **Rank 0: The Ruin:** Hostile, broken, and full of feral remnants.
- **Rank 1: Sanctuary:** You have secured and cleansed a central safe zone.
- **Rank 2: Realm:** The decay is mostly purged. The ecology obeys you.
- **Rank 3: Cosmos:** Absolute mastery. You have forged a True Heaven.

#### Rank Benefits

- **Rank 1:** The PC gains 1 bonus die on all rolls while inside their own domain.
- **Rank 2:** All rolls inside the domain use 5+ difficulty.
- **Rank 3:** The PC gains another bonus die on all rolls inside their own domain, for a total of 3 bonus dice, and keeps the 5+ difficulty.

#### Upgrading Rank

- It costs 20 Glory to reach Rank 1, 30 Glory for Rank 2, and 40 Glory for Rank 3.
- You must spend the required Glory, complete a narrative milestone in the world, and have at least two Domain Features constructed at the target Rank.

> **Long-Term Investment:** Domain Reclamation is a campaign-length arc. Reaching Rank 3 is a mythic milestone measured in dozens of sessions, not a mid-campaign checkbox.

### Custom Domain Features

Instead of a fixed list of buildings, you invent and repair your own Domain Features.

- **6 Glory:** Invent a Tier 1 Feature.
- **12 Glory:** Invent a Tier 2 Feature.
- **18 Glory:** Invent a Tier 3 Feature.
- **6 Glory:** Upgrade an existing feature to the next Tier by adding a new property from that target Tier.

**Feature Categories:** Yield/Resources (safe rest, souls, followers, Ambrosia, divine artifacts) · Defenses/Wards (alarm wards, anti-scrying, automated guardians, banishment, boss-level guardians) · Travel/Portals (fixed portals, fast travel across Asterion, the Empyrean Gate) · Metaphysical/Law (weather and architecture control, avatar tethering, absolute law).

If you want a Domain Feature to slightly exceed its limits, you can apply a Domain Trade-Off such as Blood Sacrifice, Corrupted Yield, or Unstable.

### Domain Downtime Actions

Between sessions, instead of resting in Asterion, you may spend your time managing your growing realm.

- **Clear the Blight:** Roll your Domain Pool. On a success, uncover a lost relic, raw materials, or a trapped soul.
- **Shape the Land:** Rearrange the geography of your secured zones to prepare for an incoming invasion.
- **Listen to the Void:** Use your Domain's isolation to scry on Asterion. Ask the GM one question about a rival faction's plans.

## Great Wonder

Once a PC's domain reaches Rank 3, they unlock the ability to build a Great Wonder — a major divine project tied to the domain, such as a holy engine, monumental shrine, living fortress, or mythic landmark. It requires both Glory and a completed quest before construction can begin.

- **Unlock:** Only available after the domain reaches Rank 3.
- **Cost:** The PC must spend Glory, plus complete a special quest defined by the GM.
- **Construction:** Takes time, effort, and story commitment.
- **Reward:** Once finished, it grants a permanent special bonus, buff, ability, or mechanic that works even outside the domain.

**Examples:** a *Sky Bastion* that lets the PC and allies ignore the first environmental hazard each scene · a *Blood Forge* that lets the PC reroll one failed upgrade or crafting roll per session · a *River Gate* allowing instant travel between two sacred locations the PC controls · a *World Bell* granting one automatic success on a ritual, command, or domain-linked action once per session · a *Throne of Ash* that restores 1 Ichor after every major victory, even far from the domain.
""",
)

add(
    "Domain Expression", "note", "lore", "Rules",
    "expression, stunt, feat, archetype",
    "The free-flavor Expression / Stunt (1 Ichor) / Feat tier system, so gods don't need a purchased ability for every small thematic flourish.",
    """
Player gods and mythborn should not need a separate ability purchase for every tiny thematic action. Domain Expression is the rule that lets your nature breathe at the table.

## The Three Tiers

| Tier | What it covers | Cost |
| :--- | :--- | :--- |
| **Expression** | Small, obvious, effortless thematic actions that flow naturally from what you are | Free, unlimited |
| **Stunt** | Bigger actions with minor mechanical impact — scene-shaping, not fight-winning | 1 Ichor |
| **Feat** | Signature powers, combat effects, reality-bending moves | Requires a formal ability |

## Tier 1 — Expression (Free)

An Expression is something your god or mythborn does constantly, without thought, because of what they fundamentally ARE — not something they decide to do, but something they can't help being.

**The Three-Question Test.** All three must be YES for an action to be a free Expression:

1. Is this something your god/bloodline does constantly, without conscious effort, because of their fundamental nature?
2. Does it change nothing about the scene's mechanical outcome? (No combat advantage, no bypassing obstacles, no restoring resources.)
3. Would a skilled mortal with the right tools struggle to replicate it?

If all three are YES → **Expression.** Free. No roll unless the GM calls for it. If any are NO → **Stunt** (1 Ichor) or **Feat** (formal ability).

**GM tiebreaker:** When in doubt, call it a Stunt. The 1 Ichor spend keeps it meaningful and prevents abuse.

## Tier 2 — Stunt (1 Ichor)

A Stunt is a deliberate use of your divine nature to reshape a situation — meaningful enough to change the texture of a scene, not powerful enough to win a fight or replace a formal ability.

**What a Stunt CAN do:**
- Grant narrative permission (unlock a mundane lock, communicate across a district, cross a barrier your nature controls)
- Create a minor environmental change (illuminate a space, chill or heat a room, lay a frost patch or a pool of shadow over a 10 sq ft area for 1 scene)
- Create social leverage (your divine presence cows a crowd, a ghost or undead acknowledges your authority, an animal halts in instinctive deference)
- Remove a minor complication (extinguish a fire, dissipate smoke, preserve a corpse, ease a dying creature's pain — removing Burning or Bleeding from a non-combat target)

**What a Stunt CANNOT do:**
- Deal damage → requires a Feat
- Apply a Status Condition to a creature → requires a Feat
- Restore any resource (Ichor, Flesh, Spark Shield) → requires a Restorative ability
- Replace a dice roll when the GM calls for one
- Last longer than 1 scene without a formal sustaining ability

**Stunt Fatigue:** Using the same Stunt effect twice in a single scene costs +1 Ichor (2 total). A third use costs +2 Ichor (3 total). Variety is free; repetition is taxing.

## Tier 3 — Feat (Formal Ability Required)

Anything that deals damage, applies a Status Condition, restores resources, bypasses obstacles in a durable way, or shapes combat outcomes requires a purchased ability. Domain Expression does not cover these — it is not a shortcut around the ability system.

## Archetype Reference Table

| Archetype | Expression (Free) | Stunt (1 Ichor) | Feat Required |
| :--- | :--- | :--- | :--- |
| **Fire / Sun** | Touch ignites kindling; body always radiates warmth; dim glow in darkness | Melt a mundane lock; create a 5-ft flame wall that blocks movement but deals no damage (1 scene); send a smoke signal visible for miles | Fireball; immolation; melt armor mid-combat |
| **War / Strength** | Weapons never rust in your hands; you sense when nearby creatures are armed; your stance projects menace | Snap chains or bars; unbar a reinforced door; your war-shout freezes a brawl in place | Battle-cry AoE; unstoppable charge; weapon enchantment |
| **Secrets / Night** | Whispers carry only to your intended listener; footsteps are always silent; you know when someone lies your name | Locate a hidden compartment by touch (1 min); speak in perfect privacy across a room; confirm whether a spoken name is true | Scrying; memory extraction; magical silence field |
| **Ice / Winter** | Breath is always visible; surfaces you touch briefly frost over; cold never harms you | Chill or freeze food and drink perfectly; create a slick ice patch (10 sq ft, lasts 1 scene); preserve a body indefinitely | Ice storm; freeze a target solid; ice armor formation |
| **Death / Fate** | You see wounds clearly through clothing; sense how long a creature has left; ghosts and undead acknowledge you | Ease a dying creature (remove Burning/Bleeding); keep animals from approaching; read the cause of death from a corpse | Death gaze; soul extraction; raise undead |
| **Storm / Sky** | Hair and cloak move in unfelt wind; small sparks jump between your fingers; birds land on you willingly | Clear fog in a small area; create a sudden downdraft that extinguishes torches; cause distant thunder without lightning | Lightning bolt; hurricane winds; call a storm |

Use this table to calibrate your own character — any archetype not listed follows the same pattern.
""",
)

add(
    "Items, Currency & Crafting", "note", "lore", "Rules",
    "drachma, consumables, equipment, artifacts, crafting",
    "The Drachma economy, carry limits, consumables/equipment/artifact price and effect tables, and the crafting procedure.",
    """
## Currency: The Drachma

The standard unit of exchange in Asterion is the **Drachma**. Gods and mythborn earn it through arena victories, faction contracts, Domain Yield features, and selling salvaged divine materials.

**Carry Limit:** You may carry up to **3 consumable items** at any time. Equipment (weapons, armor) has no carry limit but you can only benefit from one weapon and one armor set at once.

## Consumables

One-use items. Consuming one is a Free Action unless noted otherwise.

| Item | Cost | Effect |
| :--- | :--- | :--- |
| **Ichor Draught** | 15 dr | Restore 2 Ichor immediately. |
| **Ambrosia** | 60 dr | Restore 5 Ichor. Illegal to carry in public — possession risks arrest. |
| **Golden Apple** | 80 dr | Restore 10 Flesh. Illegal to carry in public. |
| **Spark Salve** | 25 dr | Restore your Spark Shield to maximum immediately. |
| **Antidote Wrap** | 15 dr | Remove Burning or Bleeding immediately. |
| **Blindweed Smoke** | 20 dr | Apply Blinded to one adjacent target. No attack roll. Lasts 1 turn. Main Action to use. |
| **Stun Spike** | 35 dr | Apply Stunned to one adjacent target. No attack roll. Lasts 1 turn. Main Action to use. |
| **Restoration Draught** | 50 dr | Restore 4 Flesh OR 3 Ichor (choose on use). |

## Equipment

Persistent gear that grants passive bonuses. Swapping equipment requires a Short Rest.

| Item | Cost | Effect |
| :--- | :--- | :--- |
| **Mundane Weapon** | 5 dr | Standard melee or ranged weapon. No mechanical bonus. |
| **Quality Weapon** | 30 dr | Once per combat, gain +1d10 on one attack roll before you roll. |
| **Mundane Armor** | 10 dr | +1 Armor against physical damage. |
| **Quality Armor** | 35 dr | +1 Armor + Mundane Immunity (one damage type of your choice, e.g., fire, cold). |
| **Divine Weapon** | 150 dr or crafted | Functions as a Tier 1 Active ability built into the weapon. Choose 1 Tier 1 property when obtained. Costs 0 Ichor to activate. |
| **Divine Armor** | 150 dr or crafted | Functions as a Tier 1 Passive ability. Choose 1 Tier 1 property when obtained. |
| **Masterwork Divine Weapon** | Crafted only | Tier 2 Active ability. Choose 1 Tier 2 property + 1 Tier 1 property. Costs 1 Ichor to activate. |
| **Masterwork Divine Armor** | Crafted only | Tier 2 Passive ability. Choose 1 Tier 2 property + 1 Tier 1 property. Always active. |

Divine and Masterwork items are built using the same Tier property tables as character abilities (see *Character Creation & Ability Construction*).

## Artifacts

Rare items with a limited number of uses. Found through play, domain yield, or purchased from very specific black-market contacts. Not available from standard vendors.

| Item | Uses | Effect |
| :--- | :--- | :--- |
| **Charon's Coin** | 1 | When you would be Shattered, instead restore 3 Flesh and remain standing. Trigger is automatic — no action required. |
| **Vial of Olympian Fire** | 1 | Deal 4 unmitigable fire damage to one target in melee. Free Action. |
| **Oracle's Dust** | 1 | Ask the GM one yes/no question about an upcoming scene or threat. They must answer honestly. |
| **Titan Shard** | 1 | Add +3d10 to any single roll. Declare before rolling. |
| **Ichor Phylactery** | 3 | Each charge restores 1 Ichor. Recharges fully after a Long Rest. |
| **Blessed Bandage** | 2 | Remove any one Status Condition immediately. Free Action. |

## Crafting

Crafting lets you produce consumables, equipment, and divine items using materials and downtime. It uses the same Tier framework as abilities — the roll determines the quality ceiling of what you produce.

**Step 1 — Declare the Item:** state what you are making and what Tier you are attempting; it must be something your character could plausibly create given their Spark, Lineage, or domain knowledge.

**Step 2 — Gather Materials:**

| Target Tier | Material Cost |
| :--- | :--- |
| Consumable / Tier 1 item | 15 dr |
| Quality / Tier 2 item | 40 dr |
| Masterwork / Tier 3 item | 100 dr |

Purchase or salvage raw materials before rolling. If you fail the roll, materials are consumed.

**Step 3 — Roll:** Roll your **Domain Pool (3d10)** if the item connects to your Spark or Lineage. Roll your **Base Pool (2d10)** for general items outside your domain.

| Net Successes | Result |
| :--- | :--- |
| **0** | Failure. Materials are lost. Item is not created. |
| **1** | Tier 1 item or basic consumable (Ichor Draught, Antidote Wrap, Mundane weapon upgrade, etc.) |
| **2** | Tier 2 item or enhanced consumable (Restoration Draught, Quality Armor, Divine Weapon, etc.) |
| **3+** | Tier 3 / Masterwork item or maximum-effect consumable (Masterwork Divine Weapon/Armor, full-power artifact equivalent) |

Crafting always takes **one full downtime period** (between sessions, or a full day of in-fiction time).

**Fast Crafting:** craft during a Short Rest (1 hour) instead; roll 1 fewer die.

**Domain Forge:** if your Domain has a Yield/Resources feature at Tier 2+, you may craft once per downtime period without spending Drachma on materials.

**Trade-Off Crafting:** apply one Trade-Off from the Ability Construction section to an item during creation. This lets you achieve one property tier above your roll result. The Trade-Off must be a real limitation built into the item itself: *Self-Harm* (also costs Flesh per activation), *Collateral Damage* (harms adjacent allies), *Specific Condition* (only works in a particular context), or *Unstable* (roll 1d10 on activation — on a 1, it breaks permanently).
""",
)

add(
    "Enemy Creation & Quick Templates", "note", "lore", "Rules",
    "enemy tiers, mortal, mythborn, elite, boss, legendary action",
    "The four enemy tiers (Mortal / Standard / Elite / Boss), fast-combat rules, and the ready-to-run quick templates (also entered as individual Creature entities).",
    """
Enemies exist to create tension and move the story forward, not to mirror player complexity. Keep enemy stat blocks lean — one or two defining traits, not a full ability suite. Every enemy type below is designed so combat stays fast: fodder falls quickly, elites occupy a few rounds, and bosses are session-defining climaxes.

## Enemy Tiers

### Mortal (Fodder)

| Stat | Value |
| :--- | :--- |
| Attack Pool | 1d10 |
| Defense | 1 fixed success (no roll) |
| Spark Shield | None |
| Flesh | 1–2 |
| Ichor | None |

One melee strike, 1 damage. No special abilities. Mortals are dangerous through numbers, not individual power.

**Minion Rule:** If an attacker rolls at least 1 success against a Minion, the Minion is instantly defeated. Skip the damage calculation entirely — Minions do not track Flesh. One hit ends them.

**Group Attack Rule:** When 3 or more mortals or minions attack the same target, combine them into a single roll using 1d10 per attacker (maximum 5d10). One roll, one resolution — no turn-by-turn crawl.

### Mythborn / Minor Threat (Standard)

| Stat | Value |
| :--- | :--- |
| Attack Pool | 2d10 |
| Defense Pool | 2d10 |
| Spark Shield | 2–3 |
| Flesh | 3–5 |
| Ichor | 0–2 |

One or two Tier 1 abilities. Should fall in 2–3 hits from a god-tier player.

### Elite / Named Enemy

| Stat | Value |
| :--- | :--- |
| Attack Pool | 2d10 standard / 3d10 for their specialty |
| Defense Pool | 2d10 |
| Spark Shield | 3–5 |
| Flesh | 6–10 |
| Ichor | 3–5 |

Two to three abilities, up to Tier 2. One defining trait: a passive resistance, a hard CC, or a reaction. Meant to meaningfully occupy one or two players for several rounds.

### Boss / Apex Threat

| Stat | Value |
| :--- | :--- |
| Attack Pool | 3d10 |
| Defense Pool | 3d10 |
| Spark Shield | 5–8 |
| Flesh | 12–20 |
| Ichor | 6–10 |

Three to four abilities, up to Tier 3. A boss has two phases and a Legendary Action.

- **Phase Break:** When Flesh drops below half, the boss's Spark Shield fully refreshes and they immediately unlock a new ability or behavior change.
- **Legendary Action:** Once per round, outside their turn, the boss may take one free reaction (a strike, a CC, or a repositioning move). This is not their normal Reaction — it is in addition to it.

## Keeping Combat Fast

- **Fixed Defense for Low-Tier Enemies:** mortals and minor threats use a fixed defense value (typically 1 automatic success) instead of rolling — the GM never rolls for them.
- **No Ichor Tracking for Fodder:** Mortals and minor threats never track Ichor. Elites and bosses do.
- **Telegraph Boss Moves:** before a boss uses a Tier 3 ability, announce it one turn in advance with a clear narrative tell (the arena cracks, a corona of flame builds, the air goes silent).
- **One Mechanic Per Tier:** resist mirror-image player sheets. A mortal guard needs only a damage value; an elite needs one signature ability and one passive; a boss needs two phases and a Legendary Action — nothing more.

## Enemy Ability Design

- **Mortal attack:** 1 damage, melee, no special effects — no table needed.
- **Standard ability:** Pick one Tier 1 property. That is the whole ability.
- **Elite signature move:** Pick one Tier 2 property. One Ichor cost. Done.
- **Boss ultimate:** A single Tier 3 effect with the narrative telegraph described above.

## Quick Enemy Templates

Ready-to-run versions of each tier — also entered individually under the **Creature** kind (folder *Enemy Templates*) so they can be pulled up directly at the table.

**Mortal Guard** — Attack 1d10 | Defense 1 fixed success | Flesh 2 | No Spark Shield | No Ichor. *Strike:* 1 damage, melee.

**Arena Mythborn** (Standard) — Attack 2d10 | Defense 2d10 | Spark Shield 2 | Flesh 4 | Ichor 2. *Dash Strike:* move and attack in one action, 1 damage melee. *Feral Resilience (Passive):* Mundane weapon resistance (−1 Attacker Success vs. physical).

**Temple Warden** (Elite) — Attack 2d10 / 3d10 divine | Defense 2d10 | Spark Shield 4 | Flesh 8 | Ichor 4. *Divine Smite:* 2 damage, 100 ft, 1 Ichor. *Stone Skin (Passive):* +1 Armor. *Ward Reaction:* once per round, negate 1 incoming Soft CC as a free reaction (0 Ichor).

**Primordial Beast** (Boss) — Attack 3d10 | Defense 3d10 | Spark Shield 6 | Flesh 16 | Ichor 8. *Crush:* 2 damage + Restrained, melee, 1 Ichor. *Rend (Phase 1):* 2 damage, 15-ft AoE, 2 Ichor, telegraphed. *Primal Roar (Phase 2, below 8 Flesh):* Stunned to all within 30 ft, 1 turn, 3 Ichor; Spark Shield refreshes on phase start. *Legendary Action:* once per round, free melee strike against any character who deals damage to it.

## Design Principles

- Keep combat dangerous and decisive.
- Make domains feel powerful and personal.
- Reserve Great Wonders for mythic milestones.
- Let new rewards matter outside the domain so they always feel worth the effort.
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Characters
# ────────────────────────────────────────────────────────────────────────────

add(
    "Morvain the Withered", "character", "PC", "Player Characters",
    "Decay, Plague-Born, Tank, Ascended Mortal, frontline",
    "Frontline tank and attrition fighter wielding the Spark of Decay — an ascended plague survivor other gods view with suspicion.",
    """
*Plague-Born · Divine Spark of Decay · Party Tank*

> "I am an Ascended Mortal who wields the Spark of Decay, known for my Epic Deed: The Withering Garden."

- **Origin:** Ascended Mortal, Plague Survivor of the Primordial War
- **Divine Spark:** Decay
- **Epic Deed:** The Withering Garden
- **Reputation:** Viewed with suspicion and distaste by other gods
- **Role:** Frontline tank, attrition fighter, area denial
- **XP Spent:** 14 XP · 0 remaining

## Character

Morvain was an ordinary man during the Primordial War, one of thousands crammed into a besieged sanctuary when a divine plague — a weapon with no name, unleashed by forces neither side could fully control — swept through the camp. Nearly everyone around him died within days. Morvain did not die. He simply stopped being entirely human.

The disease never left his body. It changed instead, becoming something ancient, patient, and half-alive, threaded permanently into his flesh. When he ascended, it ascended with him. The other gods do not know how to categorize him: not undead, not cursed in the usual sense, simply wrong in a way that unsettles even immortals. Most keep their distance out of instinct rather than reasoned fear.

## Core Statistics

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Standard Pool | 2d10 | General actions, attacks, defenses, contested checks. |
| Domain Pool | 3d10 | Any action tied to Decay, disease, rot, or his plague-marked body. |
| Spark Shield | 4 | Absorbs damage before Flesh; refreshes at the start of each combat. Raised from base 3 by a 3 XP upgrade to Plague-Wracked Body. |
| Flesh | 4 | Reduced from base 5 to 4 by the Chronic Frailty Trade-Off on Plague-Wracked Body (see below). *The source sheet's stat table listed this as 5 — corrected here to match the Trade-Off Summary, which is explicit about the -1 reduction.* |
| Ichor | 5 | Spent on active abilities and to enhance rolls. |
| Movement | 30 ft / 6 hexes | Standard movement; no special movement trait purchased. |

## Origin: Plague-Wracked Body
**Passive · Tier 2** (base Tier 1 + 2 XP upgrades)

The disease that should have killed Morvain instead became part of his divine physiology. His body is no longer fully alive in the way mortal or even most divine bodies are — it is preserved, sustained, and hardened by something between sickness and undeath. As his condition has been endured longer, his body has calcified further, making him increasingly difficult to put down for good.

- **Greater Resistance** (disease, poison, decay damage): any attacker using these damage types automatically loses 2 Successes when calculating damage against him.
- **Armor 1** against all physical damage — his skin has thickened into something closer to old leather than living tissue. (Tier 2 property, 3 XP upgrade.)
- **Spark Shield +1** (total 4): the sickness itself acts as a second skin of divine authority, giving him a larger buffer before wounds reach his weakened Flesh. (Tier 1 property, 3 XP upgrade.)
- He does not need to eat, rarely sleeps, and does not fear mundane infection, rot, or spoiled food and water.

**Trade-Off 1 — Chronic Frailty (Self-Harm variant):** the plague that hardened him against disease also permanently weakened his living tissue. His maximum Flesh is reduced from 5 to 4, always.

**Trade-Off 2 — Vulnerable to Purification:** radiant, holy, or purification-based damage ignores his Armor and Resistance entirely, and he loses an additional Success against such attacks. Cleansing magic treats him as something that fundamentally should not exist.

## Divine Spark: Withering Touch
**Active · Tier 3** (base Tier 2 + 1 XP upgrade) — Cost: 2 Ichor

Morvain reaches out and lets the sickness inside him spill into another body. Flesh discolors, blackens, and sags where his hand or gaze lingers; wounds refuse to close cleanly.

1. Spend 2 Ichor and use a Main Action.
2. Choose one target within 100 feet.
3. Roll 3d10 against the target's Defender pool.
4. On a hit, deal 4 base damage plus net Successes. (Tier 3 Base Damage property, 3 XP upgrade.)
5. The target becomes Bleeding for 1 turn: it takes 1 damage at the start of its next turn, bypassing Armor and Resistance.

**Trade-Off — Self-Harm:** channeling decay through his own dying body costs 1 Flesh in addition to the Ichor cost every time he uses this ability.

## Epic Deed: The Withering Garden
**Active — Once Per Session · Tier 3** — Cost: 3 Ichor

During the Primordial War's aftermath, Morvain planted something that should not have been able to grow: an alien, half-sentient flora born from his own sickness, blooming in ash and ruin where nothing else survived. Other gods call it an abomination. He calls it proof that something can live after everything else has died.

1. Spend 3 Ichor and use a Main Action.
2. Choose a point within 100 feet. A 60-foot radius zone of pale, pulsing, unnatural flora erupts from the ground — large enough to cover a city block or small arena floor.
3. Roll 3d10 as a single Attacker roll; every creature in the area rolls its own Defender pool.
4. Creatures that fail take 4 damage plus net Successes and become Restrained as writhing roots and thorned vines seize their limbs.
5. The garden persists for up to 1 hour or until dispelled by a comparable divine effect. Anyone entering the zone afterward must roll a fresh Defender check against a static 2 Successes or suffer the same effects.
6. This Deed can only be used once per session.

**Trade-Off — Collateral Damage:** the garden cannot distinguish ally from enemy. It harms and Restrains everyone in the area, including Morvain's own allies, unless they immediately retreat from the zone on their turn.

Combined with the built-in once-per-session limitation, this ability carries two layers of cost, reflecting its raw battlefield-altering power.

## Symbiosis of Rot
**Purchased Ability · Passive · Tier 1** — XP Cost: 4

This ability binds Morvain's divine Spark directly to his degrading mortal condition. The sicker his body becomes, the more of the god of Decay is allowed to surface — his power and his suffering rise and fall together.

- Whenever Morvain's current Flesh is at or below half his maximum, he gains +1d10 to any roll made using his Domain Pool (Decay, disease, rot, or his plague-marked body).
- While in this weakened state, his presence alone causes nearby plants to visibly wilt and small vermin to flee — a passive, non-mechanical tell that warns observant enemies and allies alike that he is running low.

**Trade-Off — Specific Condition:** the bonus only functions while his Flesh is at half or below; at full Flesh, this ability provides nothing.

## XP Ledger

| XP | Purchase | Result |
| :--- | :--- | :--- |
| 4 | Invent new Tier 1 ability | Symbiosis of Rot — links his Spark's power directly to his failing Flesh. |
| 3 | Upgrade existing ability | Plague-Wracked Body gains Armor 1 (Tier 2 property). |
| 3 | Upgrade existing ability | Withering Touch gains Base Damage 4 (Tier 3 property). |
| 3 | Upgrade existing ability | Plague-Wracked Body gains Spark Shield +1, raising it from 3 to 4. |
| **13** | **Total spent** | *(sheet header lists 14 XP spent / 0 remaining; ledger rows sum to 13 — one point unaccounted for in the source sheet.)* |

## Trade-Off Summary

| Ability | Trade-Offs Taken | Effect |
| :--- | :--- | :--- |
| Plague-Wracked Body | 2 of 3 | Max Flesh reduced to 4; extra vulnerability to purification/holy damage. |
| Withering Touch | 1 of 3 | Costs 1 Flesh in addition to Ichor on every use. |
| The Withering Garden | 1 of 3 (plus built-in once/session) | Harms allies in the area exactly as it harms enemies. |
| Symbiosis of Rot | 1 of 3 | Only active while Flesh is at half or below. |

**Tank Role:** Spark Shield 4, Greater Resistance to disease/poison/decay, and Armor 1 let Morvain absorb sustained punishment from most Standard and Elite enemies before his Flesh is threatened. His lowered Flesh total and purification vulnerability preserve his thematic fragility even while he anchors the front line.

## Play Notes

Morvain is a war of attrition given a body. Withering Touch chips away at a single dangerous target while bleeding himself toward the threshold where Symbiosis of Rot activates, making his Domain Pool rolls more dangerous the closer he gets to death.

The Withering Garden should be saved for moments where controlling an entire area matters more than protecting allies — a last stand, a chokepoint, or ground he is willing to sacrifice to win. Other gods' distrust of him is a strong roleplay hook: expect NPC deities to refuse alliances, demand he fight away from sacred sites, or blame unrelated blights on his presence.
""",
)

add(
    "Ascended Loki, the Hollow Crown", "character", "villain", "NPCs/Norse Sphere",
    "Chaos, Illusion, Norse Sphere, boss, usurper, deception",
    "Boss statblock — the true form beneath the stolen mask of the Allfather. Secretly rules the Norse Sphere's throne disguised as Odin.",
    """
*Boss statblock — true form beneath the stolen mask of the Allfather*

## Core Profile

**Domains:** Chaos, illusion, deception, and stolen sovereignty.

> "I am the Trickster God who wields the Spark of Chaos and Illusion, known for the Crown I stole from a broken Allfather."

**Temperament:** Sly, restless, and destabilizing — he savors the irony of ruling through a face that isn't his, and delights in every soul who kneels to a lie.

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Rank | Domain-Level Apex Threat (Boss) | Ascended Loki — secretly usurping the throne of the Allfather |
| Attack Pool | 3d10 | 3d10 baseline, Boss-tier |
| Defense Pool | 3d10 | Chaotic reflexes and layered illusion make him hard to pin down |
| Spark Shield | 6 | Regenerates fully at the start of every encounter |
| Flesh | 14 | |
| Ichor | 9 | Fuels illusion, deception, and chaotic magic |
| Armor | 2 | Ornate black-and-silver plate |
| Movement | 30 ft / 6 hexes | Special Movement — short-range blink teleport, see Trickster's Step |

**Boss Trait — The Stolen Throne:** The real Odin's mind was shattered when the Primordials killed Thor, leaving him a hollow, wandering husk. Ascended Loki has taken his place on the throne, wearing a perfect illusory mask of the Allfather. The **Odin, the Allfather** entry is Loki's disguised form — a fully functional persona with its own abilities, used whenever he wishes to rule, deceive, or fight without revealing himself. This sheet represents Loki's true, unmasked self beneath that mask.

## Passive Nature (Always Active)

**The Perfect Mask (Passive):** Loki may assume the Odin persona (see linked Odin entry) as a Free Action at the start of any scene, fully swapping to that stat block's Attack Pool, abilities, and appearance. Switching back to his true form is also a Free Action, but any damage taken in one form carries over as the same character.

**Chaos-Born Cunning (Passive - Resistance, −1 Attacker Success):** Loki has Resistance against all divination, truth-compelling, and mind-reading effects — his nature is built on layered lies even magic struggles to unravel.

**Whispers of a Fractured Court (Passive - Advanced Sense):** Loki always knows when someone nearby is lying, suspicious, or plotting against him, though he does not learn specifics without further effort.

## Active and Reaction Abilities

**Scepter of Discord** *(Active — 1 Ichor, 100 ft)*: Ranged spear-strike, 2 damage. On a hit, the target is Weakened for 1 round as chaotic energy scrambles their coordination.

**Mirror of Malice** *(Active — 2 Ichor)*: Loki conjures an illusory duplicate of himself adjacent to his true position. The duplicate has 1 Flesh, cannot be told apart from Loki without a successful Defender roll against his Domain Pool, and can make one basic attack per round until destroyed.

**Veil of Unmaking** *(Active — 2 Ichor, 15 ft radius)*: All enemies in the radius roll Defense; on a failure, they are Blinded for 1 round and cannot tell Loki apart from any illusory duplicates present until the Blind ends.

**Trickster's Step** *(Reaction)*: Once per round, when Loki would be hit by an attack, he may spend 1 Ichor to instantly swap places with the nearest Mirror of Malice duplicate, causing the attack to strike the illusion instead.

## Epic Deed

**The Mask Comes Off** *(Active Deed — Once per session, 3 Ichor, Tier 3)*: Loki drops every pretense at once. He unleashes a 60-foot burst of raw chaotic energy centered on himself; every enemy in the area rolls Defense, and on a failure takes 4 damage and is Stunned for 1 round as reality itself seems to glitch and double around him. For the rest of the scene, his Attack Pool increases to 4d10, but he can no longer assume the Odin persona until a Long Rest — the mask has been shattered, not merely lowered.

## Boss Mechanics

**Phase Break — The Cracking Illusion:** When Loki's Flesh drops below half (7 or lower), his Spark Shield fully refreshes and the illusion sustaining his composure falters — for the rest of the encounter, his laughter becomes audible even through the Odin mask if he is wearing it, and he gains +1d10 on all Domain Pool rolls as his true chaotic nature surges forward.

**Legendary Action — Which One Is Real?:** Once per round, outside his own turn, if at least one Mirror of Malice duplicate is active, Loki may force the attacking enemy to immediately reroll their attack against a random valid target within range — his duplicates constantly shifting the battlefield's sense of reality.

## Image Generation Prompt

A dark-fantasy digital illustration of the Ascended Loki, God of Chaos and Illusions, standing in full battle regalia inside a shadow-drenched gothic hall lit by flickering violet and crimson flame. He has long hair split between deep wine-purple and stark white, swept back from a sharp, pale, aristocratic face marked by jagged lightning-crack scars radiating from one glowing yellow reptilian eye, his expression a knowing, cruel smile that never quite reaches sincerity. He wears ornate black-and-silver plate armor with sweeping clawed pauldrons and layered draconic vambraces, a heavy dark cloak billowing behind him, and holds a tall golden ornate scepter-spear topped with an elaborate filigree blade that hums faintly with unstable chaotic energy. Faint illusory duplicates of his own face flicker and shimmer just beneath his skin, as though his true form is only barely holding together beneath the mask he wears for the world. Palette of deep violet, bone white, black steel, and molten gold, with his single glowing eye as the brightest point of focus. Cinematic low-angle composition, dramatic directional lighting from unnatural colored flame, richly detailed painterly dark-fantasy concept art conveying regal menace, deception, and barely-restrained madness.

---
*Asterion Statblock · Ascended Loki · Norse Sphere*
""",
)

add(
    "Odin, the Allfather", "character", "NPC", "NPCs/Norse Sphere",
    "Norse Sphere, hidden identity, disguise, GM secret, plot hook",
    "The current ruler of the Norse Sphere's throne — publicly the Allfather, secretly worn as a mask by Ascended Loki.",
    """
## Public Persona

Odin, the Allfather, sits the throne of the Norse Sphere in Asterion — stern, wise, one-eyed, the picture of a battle-worn king who won his crown through hard-fought war. To the city, to mortals, and to most gods, this is simply who rules here.

## GM Secret — The Mask

**Odin is not real.** The true Allfather's mind was shattered when the Primordials killed Thor, leaving him a hollow, wandering husk somewhere outside the throne room. **Ascended Loki, the Hollow Crown** (see that entry) has taken his place, wearing a perfect illusory mask of the Allfather via *The Perfect Mask* (Passive) — a Free Action assumed at the start of any scene. Whenever Loki wishes to rule, deceive, or fight without revealing himself, he assumes this persona.

No separate mechanical stat block exists for "Odin" — when Loki wears this mask, use the Ascended Loki statblock in full (Attack Pool, Spark Shield, Flesh, Ichor, all abilities carry over). Only the appearance, name, and narrated demeanor change; damage taken in one form carries over to the other.

The mask breaks permanently once Loki uses his Epic Deed, **The Mask Comes Off** — after that, he cannot assume the Odin persona again until a Long Rest.

**GM recommendation:** consider toggling "Hide from players" on after import until the party has reason to suspect the throne is a lie — this entry's body is a full spoiler.
""",
)

# ────────────────────────────────────────────────────────────────────────────
# Creatures — quick enemy templates
# ────────────────────────────────────────────────────────────────────────────

add(
    "Mortal Guard", "creature", "Mortal Fodder", "Enemy Templates",
    "mortal, fodder, guard, quick template",
    "Basic mortal guard — dangerous in numbers, not individually. One hit ends them.",
    """
**Tier:** Mortal (Fodder)

| Stat | Value |
| :--- | :--- |
| Attack Pool | 1d10 |
| Defense | 1 fixed success (no roll) |
| Spark Shield | None |
| Flesh | 2 |
| Ichor | None |

- **Strike:** 1 damage, melee.

**Minion Rule:** If an attacker rolls at least 1 Success against a Minion, the Minion is instantly defeated — skip damage calculation, one hit ends them.

**Group Attack Rule:** When 3+ mortals/minions attack the same target, combine them into a single roll using 1d10 per attacker (maximum 5d10). One roll, one resolution.
""",
)

add(
    "Arena Mythborn", "creature", "Standard Mythborn", "Enemy Templates",
    "mythborn, standard, arena, quick template",
    "Standard Mythborn / Minor Threat — should fall in 2-3 hits from a god-tier player.",
    """
**Tier:** Mythborn / Minor Threat (Standard)

| Stat | Value |
| :--- | :--- |
| Attack Pool | 2d10 |
| Defense Pool | 2d10 |
| Spark Shield | 2 |
| Flesh | 4 |
| Ichor | 2 |

- **Dash Strike:** Move and attack in one action. 1 damage, melee.
- **Feral Resilience (Passive):** Mundane weapon resistance (−1 Attacker Success against physical attacks).

Should fall in 2–3 hits from a god-tier player.
""",
)

add(
    "Temple Warden", "creature", "Elite", "Enemy Templates",
    "elite, named enemy, temple, divine, quick template",
    "Elite / Named Enemy — meant to meaningfully occupy one or two players for several rounds.",
    """
**Tier:** Elite / Named Enemy

| Stat | Value |
| :--- | :--- |
| Attack Pool | 2d10 standard / 3d10 divine |
| Defense Pool | 2d10 |
| Spark Shield | 4 |
| Flesh | 8 |
| Ichor | 4 |

- **Divine Smite:** 2 damage, 100 ft range. Costs 1 Ichor.
- **Stone Skin (Passive):** +1 Armor.
- **Ward Reaction:** Once per round, negate 1 incoming Soft CC as a free reaction (costs 0 Ichor).

Meant to meaningfully occupy one or two players for several rounds.
""",
)

add(
    "Primordial Beast", "creature", "Boss / Apex Threat", "Enemy Templates",
    "boss, apex threat, primordial, legendary action, quick template",
    "Boss / Apex Threat — two phases and a Legendary Action, reserved for session-defining climaxes.",
    """
**Tier:** Boss / Apex Threat

| Stat | Value |
| :--- | :--- |
| Attack Pool | 3d10 |
| Defense Pool | 3d10 |
| Spark Shield | 6 |
| Flesh | 16 |
| Ichor | 8 |

- **Crush:** 2 damage + Restrained. Melee. Costs 1 Ichor.
- **Rend (Phase 1):** 2 damage, 15-ft AoE burst around the beast. Costs 2 Ichor. Telegraphed one turn in advance.
- **Primal Roar (Phase 2 — unlocks below 8 Flesh):** Stunned on all characters within 30 ft for 1 turn. Costs 3 Ichor. Spark Shield refreshes when Phase 2 begins.
- **Legendary Action:** Once per round, makes a free melee strike against any character who deals damage to it.

A boss has two phases and a Legendary Action — reserve for session-defining climaxes.
""",
)

# ────────────────────────────────────────────────────────────────────────────

payload = {"world": WORLD, "entities": entities}
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {OUT_PATH} — {len(entities)} entities")
