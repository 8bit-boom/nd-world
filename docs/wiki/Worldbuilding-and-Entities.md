# Worldbuilding & Entities

*Applies to: GM (authoring) and Players (browsing whatever's been made
visible to them).*

Everything in your world's lore — characters (NPCs), locations,
organizations, creatures, events, items, feats, and notes — is an
**Entity**. This is the core content model nearly everything else in the app
links back to.

## Entity kinds

Eight built-in kinds appear as nav tabs, each with its own icon and a set of
suggested **subtypes** (e.g. Organization → megacorp/gang/cult/religion —
these are just suggestions, not a restriction). A GM can also define
**custom kinds** (e.g. "Vehicles", "Deities") from **World Edit → 🏷 Manage
Kinds** — a custom kind gets its own nav tab, its own home-page count tile,
and shows up as a normal option everywhere a kind is picked, including
import. Custom kinds can be renamed/reordered/deleted freely as long as no
entity currently uses them (delete is blocked otherwise, with a count so you
know what to reassign first).

## Creating and editing an entity

**+ New** (top-right, GM-only) → pick a kind → fill in Name, Summary
(shown in lists/previews), Body (the full write-up, Markdown-formatted), and
optional Tags/Folder for organization. If a **Field Template** applies to
this kind (see below), its fields appear automatically once you pick the
kind, no page reload.

**Folders** let you organize a large kind's list hierarchically (e.g.
Locations → "Neon City" → "Docks District"). The form offers a picker of
existing folders but still accepts free text for a new one. From a folder's
breadcrumb on the list page you can rename/move it, or clear it (its
entities become Unfiled).

## Images

Upload JPG/PNG/GIF/WebP/SVG/AVIF images to any entity — the main portrait/art
plus as many inline images as you want inside the body/notes via the
formatting toolbar's 🖼 button. Uploaded raster images are automatically
re-encoded (AVIF or WebP, configurable in Settings) to save space; click any
image to zoom. Card thumbnails preserve their original aspect ratio instead
of force-cropping.

## Linking entities together

On an entity's detail page, **Link to another entity** creates a
bidirectional relationship (e.g. an NPC ↔ the organization they work for).
Linked entities show up on both sides' detail pages, so you can navigate the
web of connections instead of hunting through lists.

## Notes

Beyond the entity's own body, you can attach any number of discrete
**Notes** — each independently toggleable between hidden and visible to
players, *regardless of the entity's own visibility*. This is the standard
way to reveal one detail about a location to players while keeping
everything else about it (and other notes on it) GM-only.

## Visibility

Every entity has a visibility control with three modes:

- **🌐 Everyone** (default) — all players in the world can see it.
- **🔒 GM only** — hidden from every player (spoilers, unrevealed secrets).
- **👥 Specific players** — hidden from the party except a hand-picked list
  (per-character secrets, faction intel only one PC knows, etc.).

Set this per-entity on its edit form, or in bulk from **Settings → Visibility**:
select any number of entities (filterable by name/kind), pick a mode, and
apply to all of them in one action — much faster than editing dozens
one-by-one after, say, a big lore dump or import.

## Field Templates (structured stat blocks)

**🗒 Field Templates** (top of any entity list page) let you attach
structured data on top of an entity's free-text body — instead of
hand-writing "Status: Alive" into the body, define a Status dropdown once
and reuse it everywhere. A template applies to one specific kind, or "Any
kind." Field types: Text, Number, Text Area, Dropdown (fixed options), and
**List** — a repeatable group of sub-fields, e.g. a Stat Block's "Abilities"
list where each entry has its own Name and Effect.

Two ship built-in: **NPC Details** (Title/Age/Gender/Status) and **Stat
Block** (Attack Pool/Defense Pool/Health/Armor/Speed + a repeatable
Abilities list — usable on Characters, Creatures, or anything needing quick
combat stats).

## Search

**🔍 Search** (top nav) does full-text search across names, tags, summaries,
and body text — results are filtered to what the searching user is allowed
to see.

## Investigation Boards

**📌 Boards** (GM-only) are node-and-edge relationship graphs for plotting
factions or story threads on a visual canvas. Two auto-generators exist:

- **🏛 Generate Faction Board** — builds a radial cluster layout from every
  `organization`-kind entity in the world, drawing edges from explicit
  entity links plus keyword-classified relationships (allies/enemies/
  controls/rivals) it infers from the text.
- **🗺 Dreamlands Map** — a fixed 50-location geographic atlas (bundled
  reference content — see [AI Tools & Optional Extras](AI-Tools-and-Optional-Extras.md)).

## Handouts

**Handouts** turn a single entity into a clean, printable page — useful for
physically handing a player an in-character document, letter, or NPC
portrait at the table. Reach the gallery from an entity's detail page, or
`/handouts` for the full list; **Print** combines several into one page.

## Home page & Quick Links

The world's home page (🏠, or just `/`) shows a live-count stat tile per
entity kind, plus a GM-editable set of **Quick Link** sections. Drag any nav
tab onto the home page to pin it as a link (or use **World Edit → 🏠 Edit
Home Page** for the full editor) — handy for surfacing the handful of pages
your table actually opens every session.
