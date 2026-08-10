# Sessions, Facts & AI Recaps

*Applies to: GM (full access) and Players (📓 Session Log, 📜 Chronicler).*

nd-world has two related-but-distinct ways to track "what happened":
**Sessions** (one free-text recap per game session, GM-only) and **Facts**
(a discrete, per-item log with its own visibility flag). Both can be
AI-assisted using your configured local Ollama model — nothing here calls
an external/hosted AI service.

## Sessions (GM)

**🎯 Tools → 📓 Sessions → + New Session** — track prep checklist items, XP
awarded, loot transferred, and a free-text recap (Summary). Because the
Summary is one blob, it can (and often does) contain GM-only secrets — it is
**never** shown to players directly (see Session Log below).

### AI recap assist

On a session's detail page, three buttons above the Summary field:

- **Expand notes** — turns terse bullet notes ("went to the tavern, met
  Elyra") into a written narrative paragraph.
- **Condense recap** — tightens an existing recap into something shorter.
- **Summarize from Facts** — weaves this session's logged Facts (all of
  them, GM sees secrets too) into a recap.

Each shows a preview with **Replace / Append / Discard** before touching
your actual Summary text — nothing is written until you confirm.

## Facts (GM)

**🎯 Tools → 🗒 Facts** — a running log of discrete things that happened
("The party found a hidden data core in the vault", "Elyra is secretly
working for the cult"), each with its own `visible_to_players` flag,
independent of any entity or session's own visibility. This is the
finer-grained alternative to the Sessions' one-blob Summary, and it's what
powers both the Chronicler and the player-facing Session Log below.

**Recap → Facts (AI parser):** paste a rough recap into the panel on the
Facts page and the local model splits it into a reviewable list of draft
facts (content + a suggested visible/hidden flag) — nothing is saved until
you review, edit, and click **Confirm & Save**.

## 📜 Chronicler (everyone)

A chat assistant that answers questions using only the Facts (and matching
entities) the asking user is actually allowed to see — the filtering happens
**before** anything reaches the model, not as an instruction the model could
ignore. A player asking "what happened with Elyra?" never gets the secret
fact fed into their answer, even indirectly; a GM asking the same question
sees everything. Reach it from **📜 Chronicler** in the nav — available to
every logged-in user.

## 📓 Session Log (players)

The player-facing view of session history — **📓 Session Log** in the nav.
It **never** shows the GM's raw Session Summary. Instead, opening a session
here triggers an AI recap synthesized fresh from only the Facts marked
visible to players for that session. If no visible facts are logged yet, it
just says so rather than showing anything. GMs opening the same page get a
recap built from every fact, secret or not.

## Why two systems?

Sessions/Summary is the quick, one-blob GM prep log you'd keep during play.
Facts is the structured, visibility-aware record that everything
player-facing (Session Log, Chronicler, and MCP tools if you use those —
see [Settings, Account & Sharing](Settings-Account-and-Sharing.md#mcp-access-tokens-chat-with-your-world-from-your-phone))
is actually built on. You don't have to use Facts at all — Sessions works
fine on its own for GM-only note-taking — but the moment you want players to
see *anything* about session history, log it as a Fact (or use the "Summarize
from Facts" AI button, or the recap parser) instead of relying on Summary.
