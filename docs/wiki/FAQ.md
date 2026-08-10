# FAQ

*Deployment/Docker issues (blank iframe, AI unreachable, port conflicts,
etc.) are covered in the README's [Troubleshooting](https://github.com/8bit-boom/nd-world/blob/main/README.md#troubleshooting)
section, not here — this page is about using the app once it's running.*

### Can players sign up on their own?

No. There's no public signup — every account beyond the GM is created by
redeeming a GM-issued invite link. See
[Getting Started → Invite your players](Getting-Started.md#invite-your-players).

### I hid an entity from players — why can they still see it linked from something else?

They can't see the hidden entity's own page or list entry, but if another
*visible* entity's body text mentions it by name in prose, that's just text
— nd-world doesn't scan body content for spoilers. Keep spoiler-sensitive
names out of visible entities' write-ups, or use a
[Note](Worldbuilding-and-Entities.md#notes) (independently hideable) instead
of putting the reveal in the main body.

### What's the difference between hiding an entity and hiding a Note on it?

An entity's own visibility controls whether the *whole thing* (and its
existence) is visible. A Note's visibility is independent — you can show an
entity to players while keeping one or more Notes on it GM-only, which is
the standard way to reveal *part* of something (a location, an NPC) while
keeping the rest secret.

### Why doesn't my player see the Dreamlands / King in Yellow nav links?

Both are optional and off by default — a GM has to turn them on from
**⚙ Settings → System → Optional extras** first. See
[AI Tools & Optional Extras](AI-Tools-and-Optional-Extras.md#optional-lore-extras).
(They're GM-only tools either way, so a player won't see them regardless.)

### My AI features say "unavailable" — is something broken?

AI chat (Ollama) and AI image generation (SwarmUI/ComfyUI) are both
optional backends. If you haven't enabled/configured one, the app just
shows that feature as unavailable rather than erroring — everything else
works fine without them. See the README's [AI Setup](https://github.com/8bit-boom/nd-world/blob/main/README.md#ai-setup).

### Can I run more than one TTRPG system in the same world?

Yes — see [Characters → Custom Character Systems](Characters.md#custom-character-systems).
A Sheet Template can fully replace the N&D character sheet with your own
fields, and the per-world **Rules** page can hold that system's own rules
text instead of (or alongside) the N&D core rules.

### How do I add a whole new category of lore, like "Vehicles" or "Deities"?

**World Edit → 🏷 Manage Kinds** — see
[Worldbuilding & Entities → Entity kinds](Worldbuilding-and-Entities.md#entity-kinds).
A custom kind behaves exactly like a built-in one: nav tab, home stat tile,
entity-form option, importable.

### What actually gets backed up, and how do I restore?

See [Settings, Account & Sharing → Import / Export / Backups](Settings-Account-and-Sharing.md#import--export--backups)
— short version: use **🗄 Backup** (the full zip), not the per-world JSON
**⬇ Export** button, if you want a real disaster-recovery copy.

### Is there an API I can build against?

Yes — every HTTP route and the MCP server's tools are catalogued in
[docs/API_REFERENCE.md](https://github.com/8bit-boom/nd-world/blob/main/docs/API_REFERENCE.md). For phone/chat access without
writing any code, see
[MCP access tokens](Settings-Account-and-Sharing.md#mcp-access-tokens-chat-with-your-world-from-your-phone) instead.
