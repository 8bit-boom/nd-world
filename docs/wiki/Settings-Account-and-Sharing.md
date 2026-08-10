# Settings, Account & Sharing

*Applies to: GM (Settings, invites/members) and everyone (Account).*

## ⚙ Settings (GM-only, instance-wide)

Three tabs:

- **Options** — image-upload format (AVIF/WebP/none, chosen independently
  for static vs. animated images) and entity hover-preview behavior (hover a
  link for N seconds to see a popup card; configurable delay/size).
- **Visibility** — bulk-change `visible_to_players` across many entities at
  once (see [Worldbuilding & Entities](Worldbuilding-and-Entities.md#visibility)).
- **System** — Ollama model/URL, SwarmUI/Android-emulator/Content-Editor
  external URLs (override the environment-variable defaults without a
  container restart), and the **Optional extras** toggles for Dreamlands/
  King in Yellow (see [AI Tools & Optional Extras](AI-Tools-and-Optional-Extras.md#optional-lore-extras)).

These settings are **instance-wide**, not per-world — one `world.db`, one
set of Settings, shared across every world in that deployment.

## 👤 Account (everyone)

Your own profile: display name, password change (changing your password
signs out every other logged-in session for your account), and:

### MCP access tokens (chat with your world from your phone)

nd-world runs an [MCP](https://modelcontextprotocol.io) server so a phone or
desktop Claude conversation can read/write your world data directly —
listing worlds, creating/editing Facts, searching entities, listing quests,
and asking the Chronicler — all through the exact same GM/player rules as
the web app (a player's token can never do a GM-only action).

1. Go to **👤 Account → MCP access tokens → Generate**.
2. Copy the raw token shown **once** — it isn't stored anywhere retrievable
   after this.
3. Paste the shown `mcpServers` config snippet into your MCP client (Claude
   Desktop, Claude Code, etc.), pointed at your deployment's `/mcp` URL with
   that token as a Bearer credential.
4. Revoke a token any time from the same page if it's no longer needed.

## Invites & members (GM-only, per world)

See [Getting Started → Invite your players](Getting-Started.md#invite-your-players)
for the invite-link flow. On the same World Edit page you can also toggle
whether **players can see each other's characters** (a read-only party
roster) for that world.

## Going public

nd-world is private by default — every page requires login. To let players
outside your home network reach it, see the README's
[Going public](https://github.com/8bit-boom/nd-world/blob/main/README.md#accounts-invites--going-public) section for
the recommended Cloudflare Tunnel walkthrough.

## Import / Export / Backups

- **⬇ Export** (per-world) — a self-contained JSON export of that world's
  entities with images embedded as base64. **Import** it back on the same
  page under **Worlds → Manage**.
- **📥 Import** (🎯 Tools) — bulk JSON import (auto-detects entity/character/
  table/etc. shape), bulk portrait matching by filename, and bulk AVIF/WebP
  re-encoding of everything already uploaded.
- **🗄 Backup** (nav bar) — a full-fidelity zip: a consistent snapshot of the
  whole database plus every upload and map file. This is the one to use for
  disaster recovery, not the per-world JSON export (which deliberately
  drops characters, combat, quests, parties, sessions, calendar, tables, and
  schematics). See the README's [Data & Backups](https://github.com/8bit-boom/nd-world/blob/main/README.md#data--backups)
  section for the full restore procedure and the unattended-backup cron
  script.
