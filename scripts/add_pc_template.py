"""
Adds a single blank "Player Character Template" entity to
worlds/asterion-game-of-gods.json — a fill-in-the-blank character sheet
players can duplicate in nd-world (New Character -> paste into Notes) when
building a new god or mythborn for the Asterion / Game of Gods campaign.

Mirrors the structure already used by the two filled-in PCs in the world
(Morvain the Withered, Morrigan) so a completed template reads consistently
with them, but leaves every field as an instructional placeholder based on
the Character Creation / Ability Construction / Progression rules notes
already in the world under the "Rules" folder.

Usage:
    python3 scripts/add_pc_template.py
"""

import json
from pathlib import Path

WORLD_PATH = Path(__file__).parent.parent / "worlds" / "asterion-game-of-gods.json"

TEMPLATE_BODY = """
*[Origin/Lineage tagline] · Divine Spark of [Spark] · [Role, e.g. Tank / Skirmisher / Controller]*

> "I am a **[Origin / Lineage]** who wields the Spark of **[Divine Spark]**, known for my **[Epic Deed / Mythic Curse]**."

- **Origin (Gods) or Lineage (Mythborn):** [How you attained your divinity, or your mythological species — e.g. Ascended Mortal, Bearer of a Stolen Mantle, Fallen Star, Forgotten Idol, Bronze Minotaur, Ash-born Gorgon, Harpy]
- **Divine Spark:** [Your primary godly domain — e.g. Decay, Storm, Secrets]
- **Epic Deed / Mythic Curse:** [Name of your signature Deed or permanent Curse]
- **Reputation:** [How other gods and mortals view this character]
- **Role:** [e.g. Tank, Controller, Skirmisher, Damage Dealer]
- **XP Spent:** [total] XP · [remaining] remaining

## Character

[Two short paragraphs: the character's mortal life (if applicable), how they were changed or ascended, and how the world reacts to them now. See the "Character Creation & Ability Construction" Rules note for the Character Sentence format this should build on.]

## Core Statistics

| Statistic | Value | Note |
| :--- | :--- | :--- |
| Standard Pool | 2d10 | General actions, attacks, defenses, contested checks. |
| Domain Pool | 3d10 | Any action tied to your Divine Spark, Lineage, or Epic Deed. |
| Spark Shield | [base 3 + any purchased bonus] | Absorbs damage before Flesh; refreshes at the start of each combat. |
| Flesh | [base 5 + any purchased bonus/Trade-Off reduction] | Physical body health pool. |
| Ichor | [base 5 + any purchased bonus] | Spent on active abilities and to enhance rolls. |
| Movement | 30 ft / 6 hexes | Note any special movement trait purchased. |

## Starting Ability 1 — Origin / Lineage: [Ability Name]
**[Passive or Active] · Tier [1/2/3]** [· Cost: X Ichor, if Active]

[Describe what the ability does, its exact mechanical effect (damage, range, duration, properties chosen from the Tier tables), and how it reflects your physiology, divine birthright, or species.]

*Trade-Off (if any):* [Self-Harm / Collateral Damage / Charge-Up / Specific Condition — or "None taken, kept at base Tier."]

## Starting Ability 2 — Divine Spark: [Ability Name]
**[Passive or Active] · Tier [1/2/3]** [· Cost: X Ichor, if Active]

[Describe what the ability does and its exact mechanical effect. Most Spark abilities are Active with Range/Damage/Soft CC properties; a Passive Spark acts as a permanent environmental aura.]

*Trade-Off (if any):* [as above]

## Starting Ability 3 — Epic Deed / Mythic Curse: [Ability Name]
**[Active Deed (Once Per Session) or Passive Curse (One Burden)] · Tier [1/2/3]** [· Cost: X Ichor, if Active]

[Describe the signature Deed or permanent Curse, its area, duration, and effect. Remember: the once-per-session limit (Active Deed) or permanent narrative burden (Passive Curse) already counts as one Trade-Off toward a starting Tier 3 ability.]

*Trade-Off (if any, beyond the built-in Deed/Curse limitation):* [...]

## Additional Abilities *(purchased with Glory after character creation)*

[Add one block per ability bought with Glory — name, Passive/Active, Tier, XP cost, Ichor cost if Active, full mechanical description, and any Trade-Offs. Delete this placeholder line and the XP Ledger row once real purchases exist.]

## XP Ledger

| XP | Purchase | Result |
| :--- | :--- | :--- |
| [4 / 7 / 10] | Invent new Tier 1 / 2 / 3 ability | [Name and effect] |
| [3] | Upgrade existing ability | [Name and which property was added] |
| [1, scaling] | Stat upgrade | [Max Flesh or Max Ichor +1] |
| **[total]** | **Total spent** | **[remaining] XP remaining** |

## Trade-Off Summary

| Ability | Trade-Offs Taken | Effect |
| :--- | :--- | :--- |
| [Ability name] | [X of 3] | [Summarize combined effect] |

*Reminder: no single ability may exceed 3 stacked Trade-Offs.*

## Play Notes

[How this character plays in combat, when to use signature abilities, and any roleplay hooks tied to their reputation among other gods, Divine Ambition (see the "Roleplay & Ambition Glory" Rules note), and starting Reputation.]
"""


def main():
    data = json.loads(WORLD_PATH.read_text(encoding="utf-8"))
    entities = data["entities"]

    # Idempotent: replace if this script has already been run once.
    entities[:] = [e for e in entities if e["name"] != "TEMPLATE — Blank Player Character Sheet"]

    entities.append({
        "name": "TEMPLATE — Blank Player Character Sheet",
        "kind": "character",
        "subtype": "Template",
        "folder": "Player Characters",
        "tags": "template, blank, character creation, PC",
        "summary": "Fill-in-the-blank character sheet template for new Asterion PCs — duplicate this entity and replace the bracketed placeholders.",
        "body": TEMPLATE_BODY.strip() + "\n",
        "image_url": None,
        "image_data": None,
    })

    WORLD_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {WORLD_PATH} — {len(entities)} entities total")


if __name__ == "__main__":
    main()
