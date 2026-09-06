# Rules page — the `rules.json` overlay

The Rules page renders a world's markdown (`World.rules_md`, edited on the
Rules edit page) with an auto-generated table of contents built from the
`##` / `###` headings. The optional **rules.json overlay** is per-world UI
metadata on top of that markdown: icons, custom section titles, reordering,
per-section player visibility, and tab groups. It never changes the rule
text itself — `/rules/download.md` keeps serving the pure markdown, with or
without an overlay.

## TL;DR — make one in 3 steps

1. Open your Rules page and hover/copy the TOC links: each entry's link is
   `#<section-slug>` — e.g. `## Part II — Races & Optional Systems` becomes
   the slug `part-ii-races-optional-systems` (lowercase, everything
   non-alphanumeric collapsed to `-`). For a `tabs` overlay on a document
   with more than a few chapters, skip this and click **"🪄 Auto-build tabs from headings"**
   on the Rules edit page instead — it reads off every slug for you,
   correctly grouped (see the warning below on why doing this by hand is
   easy to get wrong for a large document).
2. Write a JSON object with a `sections` map (slug → `{icon, title,
   players_visible, order}`) and/or a `tabs` list. Only include the
   sections you actually want to change.
3. Paste it into **Worlds → edit → Rules page → the second textarea
   ("rules.json overlay")**, Save — or upload it as a file via the
   **Import from JSON** box on the same page (an overlay-only file with
   just `{"sections": ..., "tabs": ...}` is accepted; `rules_md` is only
   required if you also want to replace the markdown in the same file).

Invalid JSON or unknown slugs never break the page: an invalid overlay is
rejected at save time (HTTP 400 with the parse error) and an overlay that
fails validation at render time is ignored with a log line. A stored
overlay that doesn't validate shows a warning banner on the edit page.

## Schema

```json
{
  "sections": {
    "<section-slug>": {
      "icon": "🧬",                       // optional: prefixes the section title
      "title": "Races & Optional Systems", // optional: overrides the display title (TOC + heading)
      "players_visible": true,             // optional (default true): false hard-hides
                                           // the section from non-GM viewers, server-side
      "order": 2                           // optional: position within its group (missing = natural order)
    }
  },
  "tabs": [
    { "id": "core", "label": "Core Rules", "sections": ["<slug>", "<slug>"] },
    { "id": "appendix", "label": "Appendices", "sections": ["<slug>"] }
  ]
}
```

- Top level: `sections` (a map keyed by section slug) and `tabs` (a list) —
  both optional; an empty object `{}` is a no-op overlay.
- `tabs[].id` is optional (slugified from the label, duplicates get `-2`,
  `-3`, …); `tabs[].label` is required.
- Every slug may appear in exactly one tab. Sections listed in no tab land
  in a trailing **"More"** tab. With no `tabs` at all, the page renders as
  one continuous flow and `order` does the arranging.

  **⚠️ Tabs have no notion of heading hierarchy — listing a Part's own slug
  does NOT pull its chapters along.** For a Part/Chapter document, listing
  only `"part-iii-equipment-economy"` in a tab renders nothing but that
  Part's own intro paragraph; every `###` chapter nested beneath it (which
  is its OWN separate slug) silently falls through to "More" instead. For a
  document with more than a handful of chapters, use the **"🪄 Auto-build tabs from headings"**
  button on the Rules edit page instead of listing slugs by hand — it groups
  every top-level heading with all of the chapters nested under it into one
  tab each, so nothing gets left behind. Hand-list slugs (as the worked
  example below does) only for a short document, or once you're deliberately
  fine-tuning the auto-built result.
- `players_visible: false` is the data-driven equivalent of a `:::gm`
  directive block: the section is removed from the page server-side for
  non-GM viewers (never CSS-hidden), and the TOC is built from the final
  visible sections so the sidebar can't link to them.

## Worked example

For a rules document with:

```markdown
## Part I — Core Rules
### 1. How to Play — the Core Roll, Health, Resources & Combat
## Part II — Races & Optional Systems
### 15. Optional Races — Blood, Root, Stone & Machine
## Appendix B — Editorial Notes & Source Map
```

the generated slugs are `part-i-core-rules`,
`1-how-to-play-the-core-roll-health-resources-combat`,
`part-ii-races-optional-systems`,
`15-optional-races-blood-root-stone-machine`, and
`appendix-b-editorial-notes-source-map` (lowercase; runs of
non-alphanumeric characters — spaces, `—`, `&`, `.`, leading digits stay,
only the separators collapse). A matching overlay:

```json
{
  "sections": {
    "part-i-core-rules": { "icon": "⚔️", "order": 1 },
    "part-ii-races-optional-systems": { "icon": "🧬", "order": 2 },
    "15-optional-races-blood-root-stone-machine": { "players_visible": true },
    "appendix-b-editorial-notes-source-map": { "players_visible": false }
  },
  "tabs": [
    { "label": "⚔️ Core", "sections": ["part-i-core-rules", "1-how-to-play-the-core-roll-health-resources-combat"] },
    { "label": "🧬 Races", "sections": ["part-ii-races-optional-systems", "15-optional-races-blood-root-stone-machine"] },
    { "label": "🗂 Appendix", "sections": ["appendix-b-editorial-notes-source-map"] }
  ]
}
```

renders two tabs (Core, Races, Appendix), with icons on the part headings
and the editorial appendix hidden from players.

## How to get the section slugs, reliably

- **From the Rules page TOC:** every TOC entry is an in-page link — its
  target is `#<slug>`. Copy the part after the `#`.
- **From the page source:** the rendered headings are
  `<h2 id="<slug>">…</h2>` / `<h3 id="<slug>">…</h3>`.
- **From the JSON import round-trip:** save a `{}` overlay, then open the
  edit page — nothing tells you the slugs there today; the TOC method above
  is the intended one.

## Markdown superpowers (related, but not part of the JSON)

The renderer also supports directive blocks and a statblock fence —
`:::tip|note|warning|danger|lore|collapse|gm [title] … :::` themed callouts
(`:::gm` blocks are removed server-side for non-GM viewers), and
```` ```statblock ```` cards with a copy button. Those are authored in the
markdown itself; see the cheatsheet on the Rules edit page.
