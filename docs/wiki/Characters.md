# Characters

*Applies to: Players (creating/playing their own character) and GM (managing
all characters, linking accounts).*

## Creating a Player Character (N&D wizard)

**🎲 Player Characters → + New Character** opens a guided,
rules-driven wizard implementing the Neon & Dragons Core Rules creation
procedure:

1. **Name** — character/player name, portrait.
2. **Race** — pick from Standard / Advanced / Exceptional tiers.
3. **Profession** — one of the six N&D professions.
4. **Stats** — 20-point allocation across the 8 stats, with a live derived-stats
   preview (HP/Shock/CA/Speed/PP/MP) as you allocate.
5. **Feats** — required Race/Profession/Common picks, Free Feat slot(s),
   auto-granted Edge-rank feats where applicable.
6. **Equipment** — spend a 5000-credit starting budget.

The wizard draws from the same race/profession/feat/equipment catalog used
by the NeonDragonsApp Android app and NeonDragonsEditor desktop tool, so
characters made here carry the same content IDs as those two apps —
export/import between them just works.

## The character sheet

Once created, `/characters/{id}` is the live sheet: stats, HP/Shock/Power
Points/Mana Points tracking with +/- controls that save instantly, XP,
equipment, and feats. A player can edit their own character; the GM can edit
any character and, from the sheet, **link (or unlink) it to a connected
player's account** so the player sees it as "their" character.

## Exporting

Every sheet has:

- **⬇ Export .ndc** — the interchange format shared with NeonDragonsApp
  (Android) and NeonDragonsEditor (desktop). Import it there directly — no
  extra setup on either app's side.
- **Export to Foundry VTT** — a Foundry-compatible actor JSON.

## Custom Character Systems

The N&D wizard above isn't the only option. **🗒 Systems & Templates**
(`/characters/templates`, GM-only) manages **Sheet Templates**, which come
in two modes:

- **Extends the N&D sheet** — every character still gets the full N&D sheet;
  the template adds extra sections on top.
- **Fully custom system** — the N&D sheet isn't used at all; the character
  *is* whatever fields the template defines, on its own page. Fields can be
  simple values or a repeatable **list** (e.g. a variable-length ability
  list, inventory, or session log).

A built-in **Asterion** template (a d10 dice-pool system) ships as a working
example. To create a character with a custom-mode template, open **🗒
Systems & Templates** and click **+ Create Character** on that template's
card — the plain **+ New Character** button always goes to the standard N&D
wizard.

To build your own homebrew system: **+ New Template** → **Fully custom
system** → add fields, using **List** for repeatable groups. Templates can
be scoped to one world or left global.

## Rules content is per-world

The **📖 Rules** page falls back to the bundled N&D core rules until a GM
sets something else on **World Edit → 📖 Edit Rules** (paste Markdown
directly, or import a JSON file shaped `{"rules_md": "..."}`). This is what
makes running Asterion (or your own homebrew system) alongside its own rules
text possible in the same app.
