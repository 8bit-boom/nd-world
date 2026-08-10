# Settings, Account & Sharing

*Applies to: GM (Settings, invites/members) and everyone (Account).*

## ⚙ Settings (GM-only, instance-wide)

Three tabs:

- **Options** — image-upload format (AVIF/WebP/none, chosen independently
  for static vs. animated images), entity hover-preview behavior (hover a
  link for N seconds to see a popup card; configurable delay/size), and a
  **UI scale** picker (90–150%, for readability — this one's actually a
  per-browser preference like the theme toggle, not instance-wide; also
  reachable from the nav bar itself without opening Settings at all).
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

**🎯 Tools → 📦 Export & Backup** is the single page for all of this — it
gathers what used to be scattered across the nav bar and the Worlds page
into one place, with a one-line explanation of when to use each:

- **🗄 Full Backup** — a complete zip snapshot: the whole database (every
  world, character, session, combat, quest, party, calendar, table, and
  note — not just lore), plus every upload and map file. **This is the one
  to use for disaster recovery.** See the README's
  [Data & Backups](https://github.com/8bit-boom/nd-world/blob/main/README.md#data--backups)
  section for the full restore procedure and the unattended-backup cron script.
- **📖 World Book** — a readable HTML zip of the active world's lore (every
  entity, board, and map, plus the rules) for sharing with players offline,
  printing, or archiving the setting itself. Doesn't include characters,
  sessions, or anything re-importable — deliberately **not** a backup.
- **📦 World JSON** (single file, or split into separate files for external
  tooling) — the active world's entities as JSON with images embedded,
  re-importable into another nd-world instance via the **Import** box on the
  same page.

**🎯 Tools → 📥 Import** is the separate bulk-content importer — auto-detects
entity/character/table/etc. shape, plus bulk portrait matching by filename
and bulk AVIF/WebP re-encoding of everything already uploaded. Not to be
confused with the World JSON re-import above, which is specifically for
round-tripping nd-world's own export format.
