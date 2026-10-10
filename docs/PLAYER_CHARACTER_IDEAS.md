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
   Also done for the other three rulebooks (see docs / `app/pool_roll.py`): Chronicles of the Worm (same engine, advantage/disadvantage, boost from the matching pool), Hunt in the Moonlight and Game of Gods / Asterion (d10 success pools: 6+ succeeds, 10 explodes, spend Stamina / Ichor, Strain tally). Still open: attacks / feats as one-tap rolls.
2. ~~Initiative and turn actions~~ — **done: Initiative button in the strip when an encounter starts, End turn when it is yours (`hub/initiative`, `hub/end-turn`).**
3. ~~Rest buttons for the player~~ — **done: Short / Long rest on the sheet, with undo (`hub/rest`).**
4. ~~Inventory that knows weight and money~~ — **done: carry bar against a house-default limit (5 x (STR+BOD), changeable), quantity steppers, coin steppers.**
5. ~~Level-up wizard~~ — **done: Advance - stats and feats at the Player's Guide costs and Rank rules.**
6. ~~A change log with undo~~ — **done: Log tab with who/when and one-tap Undo for numeric changes.**
7. ~~Handout inbox~~ — **done: Handouts tab and a new-handout chip.**
8. ~~Ability / spell cards~~ — **done: Abilities cards with Use / Ready (N&D feats).**
9. ~~Offline at the table~~ — **done: service worker keeps the last sheet readable; HP/Shock changes made offline are queued and replayed in order.**
10. ~~Notifications~~ — **done as in-page alerts (Notification API through the service worker) while the page is open - no Web Push server, so nothing arrives with the app closed.**
11. ~~Build compare / what-if~~ — **done: Advance shows what each stat or feat changes (PP/MP/HP/Shock/Speed, XP left, Rank unlock) before the confirm tap.**
12. ~~Shareable read-only link** for a co-player or a Discord post (token-scoped, GM can revoke).~~ — **done: Share button makes a token link (`/share/<token>`); new link replaces the old, empty box stops sharing; no notes, journal, quests or portrait.**
13. ~~Per-character quick links~~ — **done: pin up to 8 pages; they show under the vitals.**
14. ~~Companion sheets~~ — **done: Allies tab (name, kind, HP, notes) with pills in the strip; not full sub-sheets.**
15. ~~Voice journal~~ — **done: Dictate button on the journal form (browser speech recognition; hidden where the browser has none).**

## Rules the page keeps

* It only ever shows what the player could already reach elsewhere (section levels, reveals, `[gmonly]` stripping).
* Anything a combat shows to the player is minimal: round, whose turn, never an enemy's name.
* The strip and viewer are inert in `?embed=1` pages (cockpit windows) so nothing nests.
* Every new in-hub link goes through the viewer; every new hub route has an owner check and a row in `API_REFERENCE.md`.
