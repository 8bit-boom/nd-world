# Player characters — what is built, and what could come next

The goal: **a player lives on their character page** — everything they need during play is on it or opens over it, and it
is comfortable on a phone held in one hand.

## What the character page does now

| Need | Where |
|---|---|
| See / change HP, Shock, a custom system's Health and other tracks, without scrolling | the **live strip** — sticky over every tab, ± buttons, tap the number for "damage / heal by N" |
| Know whose turn it is | strip pill: **YOUR TURN**, "you're up next", or whose turn / "enemy turn" (a GM-run combatant is never named) |
| See conditions, temp HP, level-up ready, next game night | chips under the strip |
| Read any other page (rules, maps, quests, calendar, session log, entities, party, dice, audio…) | **Pages** tab and every link in the hub: opens **over** the character (`pc-viewer.js`), Back / Esc returns |
| Party, loot claims, XP history, recent sessions | Journey tab |
| Quests, goals, private journal, GM notes to them | Quests / Notes tabs |
| What they know about the world | World tab |
| Schedule, polls | Schedule tab |
| Live table dashboard | Cockpit tab |
| Phone ergonomics | tab bar becomes a **bottom bar** (Sheet · Journey · Quests · Notes · More), safe-area aware, Android Back closes the viewer first, no horizontal scroll |

## Ideas, roughly by value for effort

1. ~~Tap-to-roll from the sheet~~ — **done for N&D stats**: tap a stat, optionally spend 1-2 PP/MP, roll Stat + d10 into the shared log with a label.
   Still open: custom systems' own dice (each rulebook differs), attacks / feats as one-tap rolls.
2. **Initiative and turn actions** — "Roll initiative" button in the strip when an encounter starts; "End my turn" that nudges the GM's tracker.
3. **Rest buttons for the player** — short / long rest from the sheet (the party Rest rules exist per system; expose them for one character).
4. **Inventory that knows weight and money** — encumbrance bar, coin purse with ± per currency, "give to party / to a companion" in one tap.
5. **Level-up wizard** — walks through the new level (HP, feats, stat points) with the rules text beside it, instead of a bare button.
6. **A change log with undo** — every HP / XP / item change timestamped ("GM: −4 HP · Pia: used Stim"); one-tap undo for a mis-tap.
7. **Handout inbox** — things the GM "sent to players" collect on the character so a missed pop-up is never lost.
8. **Ability / spell cards** — feats, spells and attacks as cards with cost, range, effect and a "use" button that spends the resource.
9. **Offline at the table** — cache the sheet (service worker) so a bad connection never blocks reading it; queue HP changes and replay.
10. **Notifications** — "it's your turn", "next game night changed" as a Web Push for installed home-screen users.
11. **Build compare / what-if** — preview a feat or stat change before buying it.
12. **Shareable read-only link** for a co-player or a Discord post (token-scoped, GM can revoke).
13. **Per-character quick links** — pin the three pages this player opens most to the top of Pages.
14. **Companion sheets** — familiars, hirelings and vehicles as small sub-sheets with their own strip.
15. **Voice journal** — dictate a journal entry on the phone (browser speech-to-text), tagged to the session.

## Rules the page keeps

* It only ever shows what the player could already reach elsewhere (section levels, reveals, `[gmonly]` stripping).
* Anything a combat shows to the player is minimal: round, whose turn, never an enemy's name.
* The strip and viewer are inert in `?embed=1` pages (cockpit windows) so nothing nests.
* Every new in-hub link goes through the viewer; every new hub route has an owner check and a row in `API_REFERENCE.md`.
