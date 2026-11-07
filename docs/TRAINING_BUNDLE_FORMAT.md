# nd-world Training Bundle, format version 1 (`.ndtrain`)

`GET /export/training.ndtrain` (Tools → Export & Backup → "Training bundle", GM only, the active world) downloads one world's
text for **ND Lore Forge**, the program that fine-tunes a small model on it (repository `Llm-training`, which keeps a copy of this
file as `docs/BUNDLE_FORMAT.md` and a sample bundle as `tests/fixtures/sample.ndtrain`). A format change bumps `version`.

The bundle is for a **GM-only** model, so it contains GM secrets: `[gmonly]` text, hidden entities and notes, facts marked hidden.
Treat it like a backup. It never contains users, e-mail addresses, passwords, tokens, 2FA data, images, audio, video, maps, dice
history, live/combat state, **character journals and goals** or **GM→player private notes**.

Code: `app/training_bundle.py` (Python, the oracle) and `rust/nd-gateway/src/export.rs` `training_bundle` (the gateway; identical members).

## Zip

A zip of UTF-8 members. `*.json` = one JSON document (`indent=2`, then a final newline); `*.jsonl` = one compact JSON object per
line (`separators=(",", ":")`, `ensure_ascii=False`, `\n` after every line). Members are stored in the order below, with timestamp
`1980-01-01 00:00:00`, mode `0644`, deflate level 6. Records are ordered by `id` ascending unless stated. Optional members are
left out when they would be empty.

| Member | Always | Record |
|---|---|---|
| `manifest.json` | yes | see below |
| `world.json` | yes | `{"id","name","slug","description","templates":[{"id","slug","name","kind","fields":[…]}]}`; templates are the world's own and the global ones; `fields` is the template's field list (ids and labels) |
| `entities.jsonl` | yes (may be empty) | `{"id","kind","subtype","name","aliases":[…],"folder","tags":[…],"summary","body","template":slug\|null,"custom_fields":{field id: value},"roleplay_personality","visible_to_players","group_ids":[…],"rag_priority","updated_at"}` |
| `entity_notes.jsonl` | no | `{"id","entity_id","content","visible_to_players","updated_at"}` for every note pinned to an entity of the world |
| `relations.jsonl` | no | `{"id","source_id","source_name","target_id","target_name","relation"}`: typed relations first (by id), then the plain "related" links (by source, target). `id` is the position, 1…n |
| `rules.md` | no | the world's rules text; the bundled core rules when the world has none (raw Markdown, `:::gm` blocks kept) |
| `rules_sheets.jsonl` | no | `{"id","template","rules_md"}` of the world's character-sheet templates that carry rules text |
| `sessions.jsonl` | no | `{"id","session_num","session_date","title","summary","npcs":[…],"loot":[…]}` |
| `facts.jsonl` | no | `{"id","game_session_id","content","visible_to_players","tags"}` |
| `quests.jsonl` | no | `{"id","title","status","category","summary","body","linked_entities":[…],"parent_id"}` |
| `tables.jsonl` | no | `{"id","name","category","description","entries":[…]}` (the world's own tables) |
| `ai_instructions.jsonl` | no | enabled standing AI instructions: `{"id","title","content","applies_to_players"}` |
| `characters.jsonl` | no | `{"id","name","race","char_class","level","backstory","notes","equipment":[…],"feats":[…]}` — no owner or account fields |

Normalisation (so Python and Rust agree): `aliases` / `tags` are the comma-separated text split, each part stripped, empty parts
dropped; `group_ids`, `npcs`, `loot`, `linked_entities`, `entries`, `equipment`, `feats`, `fields` are the stored JSON when it is a list
(`custom_fields`: a dict), else empty; booleans are the stored flag's truthiness; `updated_at` is the stored text with the first space
turned into `T`. Access tags (`[gmonly]`, `[for=IDs]`, `[reveal=DATE]`) stay raw in every text field: the reader decides what to do.
`custom_fields` keeps field ids; `world.json` templates map id → label.

### manifest.json

```json
{"format": "nd-training-bundle", "version": 1, "created": "2026-11-07T12:00:00Z", "nd_build": "abc1234",
 "world": {"id": 1, "name": "…", "slug": "…", "description": "…"},
 "files": {"world.json": {"sha256": "…", "bytes": 800}, "entities.jsonl": {"sha256": "…", "bytes": 12345, "records": 312}}}
```

`files` lists every other member in order; `records` = lines of a `.jsonl`. `created` is the only value that differs between two
exports of an unchanged world.

## Reader rules

1. Refuse a `format` other than `nd-training-bundle` or a `version` higher than the reader knows.
2. Check every listed member's SHA-256 and size; refuse a member the manifest does not list.
3. Ignore unknown keys; treat a missing optional member as empty.
