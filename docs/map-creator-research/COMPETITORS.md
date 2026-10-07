# Competitors, community and licences — with the evidence graded

Companion to [`../MAP_CREATOR_RESEARCH.md`](../MAP_CREATOR_RESEARCH.md). **How much to trust each line:**

| Grade | Meaning |
|---|---|
| **A** | read from the thing itself in this research: a licence file, a README, a package's own metadata, a type definition, working code |
| **B** | a vendor's or tool-maker's own page as summarised by the search tool (I could not open the page: arkenforge.com, foundryvtt.com, kenney.nl, game-icons.net, dndbeyond.com, itch.io, legendkeeper.com, texttotabletop.com, makemythic.com, docs.owlbear.rodeo and web.archive.org are blocked from the sandbox) |
| **C** | community talk or a competitor's blog post, via search summaries — useful for *what people complain about*, biased and unverifiable here |
| **—** | my inference |

There is **no usage data** about your own table in any of this; §6 lists what is really unknown.

## 1. The four kinds of tool, and where nd-world could stand

| Kind | Examples | What they are good at | Price / licence (grade) |
|---|---|---|---|
| **Map makers** | Dungeondraft, Dungeon Scrawl, Dungeon Alchemist, Inkarnate, Wonderdraft | art: asset packs, textures, lighting baked into an image, UVTT export | Dungeon Alchemist $44.99 on Steam (B); Dungeon Scrawl free in the browser (B); the others not re-checked |
| **Online VTTs** | Foundry VTT, Roll20, Owlbear Rodeo, Fantasy Grounds | live play with walls, doors, vision, fog, tokens; import UVTT | Foundry **v13** schema read from its types (A); Owlbear SDK **MIT** (A) |
| **In-person VTTs** | Arkenforge, Infinite Realms, Digital TableTops, MapTool, Dungeon Revealer, `dndfog` | a map on a TV or projector, 1-inch squares, fog and lights, GM on a notebook | Arkenforge one-time $35–$250, "fully offline", multi-screen, includes a map builder (B); **Dungeon Revealer: open source, self-hosted, ISC (A)** |
| **Generators / AI** | Watabou (city, One Page Dungeon), Donjon, Azgaar (MIT, A), AI image tools (tt-rpg.app, ZapGM), **Familiar** (an AI co-pilot that runs a Foundry game: 230 tools, $6/month, PolyForm Shield — A, from its own README) | instant starting points; AI that acts on a VTT | free web tools (B); Watabou says maps may be used commercially, credit appreciated (B) |

Where nd-world already sits: *prep and lore* (nothing above has it), *a small online VTT* (tokens, combat link, merchants), and *a second screen that can only show pictures*.
**Dungeon Revealer is the closest relative** — self-hosted, in person or remote, fog the players cannot see through, tokens, a dice roll chat, notes and images — and its README states the
secrecy model plainly: "what appears as a shadow to the DM will appear as pure blackness to players" (A). nd-world's differences would be the lore links, the combat/character
link, AI, MCP, and map *creation*.

## 2. What people ask for — and what the tools make hard (grade C unless noted)

- **Time.** Placing every wall segment, floor texture and prop by hand is said to take hours for a detailed multi-room map — a competitor's blog post about Dungeondraft, paraphrased by the search tool (C, vendor-biased).
- **Asset curation.** Keeping dozens of third-party packs tidy "becomes a hobby of its own" (C).
- **Walls and light are tedious** — enough that at least three tools exist only to detect them from a picture: **Auto-Wall** (MIT; Python, OpenCV, scikit-learn, PyQt6; Canny edge detection, colour
  picking, light detection; exports UVTT; "a time-saving tool... manual refinement expected", **A**), a Foundry module that does it in the browser (B), and the Universal Battlemap Importer imports the UVTT
  that Auto-Wall writes (B).
- **Interchange.** Everything exports or imports Universal VTT; Dungeon Alchemist lists Foundry, Roll20, Fantasy Grounds, Above VTT and UVTT (B). The Owlbear "Scene Importer"
  reads UVTT, DD2VTT, Foundry scene JSON and module zips (GPL-3.0 — **cannot be reused in an MIT project**, B).
- **TV tables** (B/C): calibrate so one square is one inch (a ruler against the screen), build maps at 70–140 px per square, put the GM controls on a second device. A TV is a *shared* view — one fog for the
  whole party — which is simpler than per-player fog.
- **AI battlemaps** are already common; the unsolved part is everything after the picture: grid, scale, walls, light. Measured on a real map in this research ([PROTOTYPES.md §7](PROTOTYPES.md)): naive edge or ink detection finds
  every wall (100% recall) but only **26–43% of what it marks is a wall**.

## 3. Performance guidance from the field

From `Codas/foundryvtt-performance-hacks` (a community performance module for Foundry v13; its README holds measurements) **(A)**:

- Foundry draws each wall as two graphics objects: "a scene with a few hundred wall segments can spend 600+ draw calls just on drawing the wall controls". Caching them as one batched sprite layer cut that layer from 25.1 ms CPU to 1.3 ms.
- The **token layer** was the most expensive part of a 31-token scene (15 ms GPU baseline); a combined set of optimisations took the scene from **25 to 74 fps**.
- Lighting at full resolution is costly; 40% (light) / 50% (darkness) resolution looked nearly the same.

Common advice repeated in several places (B/C): fewer, longer wall segments beat many short ones; limit dynamic lights; use "unrestricted"/non-blocking walls for decoration.
What this means for nd-world: the cost is in **how many separate display objects** there are, not in the visibility maths. One SVG `<path>` for all walls, one mask for fog, one layer for lights is the
same lesson. **Realistic maps are small**: a generated 100 × 70-cell dungeon (78 rooms) has 749 wall segments, a 200 × 140 one 2,878 ([PROTOTYPES.md](PROTOTYPES.md)).

## 4. Licences — what may go in this repository

| Thing | Licence | Grade | Consequence |
|---|---|---|---|
| nd-world code | MIT; lore/content CC BY-NC-ND (see `LICENSE`) | A | new code MIT; **new art should be MIT/CC0 too** so the Docker image stays clean |
| Dungeon Revealer, Auto-Wall, Azgaar's generator, FVTT-DD-Import, `dungeondraft-mcp`, Owlbear SDK, pixi.js, konva, `visibility-polygon`, `polygon-clipping`, `rbush` | ISC / MIT | A | can be read, adapted with notice, or vendored |
| `uvtt2fgu` (sample files) | BSD-3-Clause | A | a stripped sample is kept as a test fixture with its notice |
| Owlbear **Scene Importer** | GPL-3.0 | B | read for ideas only; copying code would force the GPL |
| `donjon2uvtt` | no licence file seen | A (absence) | used only to read the *field names* of a file format, no code copied |
| Familiar | PolyForm Shield | A | not reusable, and a competitor-use restriction |
| Forgotten Adventures assets | their commercial licence and fan-content policy do not cover integrating assets or tokens into software products (as summarised by the search tool) | B | **do not bundle** community art packs |
| "assume art is for personal use unless commercial use is stated" (Dungeondraft asset ecosystem) | various | C | the same |
| Kenney (CC0), game-icons.net (CC BY 3.0), 2-Minute Tabletop (CC BY-NC 4.0) | as stated | B — **not verified at the source** | Kenney/game-icons are plausible sources for *icons*; NC is incompatible with a published image |

**This is the argument for the first-party prop set in [PROTOTYPES.md §6](PROTOTYPES.md): 24 props and 9 textures drawn in code, no licence to track, consistent style, extendable. GM uploads stay the GM's
responsibility.**

## 5. Where nd-world has no equivalent and where it would lead (inference)

- **Lore-linked maps** — a room is an entity; the entity page shows its map. No tool above has the lore side. 
- **Prep → run in one place** — GM prepares with lore, runs with the same app on the second screen, players see the same map on phones.
- **AI that plans, code that lays out** — the building and furnishing prototypes turn "a smugglers' den with a hidden back room" into rooms, doors, props and light *without the model drawing anything* (and the result is real walls, editable).
- **MCP** — Familiar shows the market for AI acting on a VTT; an MCP server that reads and edits *your own* maps is within reach (no map tool exists yet).
- **Not a place to compete:** hand-painted art quality (Dungeondraft/Inkarnate), a 3D/AI-generated map engine (Dungeon Alchemist), the Foundry module ecosystem.

## 6. What is still unknown (ask / measure)

1. **How your table actually plays** (TV on the table? remote? Foundry?) — it decides the order of work. 
2. How many *real* maps you have and in what form (Dungeondraft files? pictures? nothing yet?). 
3. Whether players use phones. (A zoom-less map is unusable there today.) 
4. Real-hardware rendering numbers (`prototypes/../render-bench.html` was written for this and has not been run; the sandbox has no GPU).
5. The primary Arkenforge UVTT specification and the Roll20/Foundry import behaviour (see [FORMATS.md §8](FORMATS.md)).
