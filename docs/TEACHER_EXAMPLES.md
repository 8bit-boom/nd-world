# Teacher examples: better training data from the world's own AI model

**Export & Backup → 🧪 Better training examples.** For the active world, the AI model chosen in Settings (the same one AI Chat uses) reads the
world and writes extra practice for ND Lore Forge; the next **Download training bundle** then contains it as `teacher_examples.jsonl`
(format: `docs/TRAINING_BUNDLE_FORMAT.md`). ND Lore Forge shows the examples in its Step 2, where the GM keeps, drops or edits them.

**Python-free image only** (`ND_PYTHON_FREE=1`), like the King in Yellow generator: there is no Python counterpart, so the hybrid image answers
"Not available here" and the card says so. Code: `rust/nd-gateway/src/teacher/` (`gen.rs` the pipeline, `routes.rs` the endpoints, `markup.rs` /
`docs.rs` / `chunk.rs` a port of ND Lore Forge's text preparation: GM secrets stay in the text, labelled `(GM secret: ...)`).

## What it makes
`t_deeper` deeper questions, `t_rules` rules practice, `t_history` session history, `t_npc` NPC role-play (nd-world's NPC prompt shape with the
GM's speaking style; **no GM secret is sent to the model for these**, and hidden entries are skipped), `t_lore` lore search (the "Relevant world
lore" block nd-world puts in front of a question), `t_missing` "not in the lore" (the invented name is checked to appear nowhere in the world),
`t_general` general chat (about 10 %) and `t_style` house style (the world's enabled AI instructions).

## The check
A second call per example compares the answer with its source: `unsupported` examples get `status: drop`, `partly` ones are kept and marked (or
dropped with "also drop partly supported"), general chat and house style are `unchecked`. The checker is the same model: it removes a lot, not
everything.

## Running
`POST /export/teacher/start` (options: `kinds`, `per_source` 1-4, `general_percent` 0-30, `judge`, `drop_partly`, `limit`), `POST .../stop`,
`POST .../clear`, `GET /export/teacher/status` (GM only, the active world). One run per world; it runs in the background and takes the shared AI
slot only while a call is being answered, so AI Chat and other jobs go on in between. Every call is cached under `<database folder>/teacher/<world id>/cache/`,
so a stopped, interrupted (server restart) or repeated run only pays for what is new. Three failures in a row stop the run with the model's message; what
was made stays. State and results are plain files next to the database (`state.json`, `examples.jsonl`); they are not rows of the database, and nothing here promises them a place in the Full Backup (they can be made again: the answers are cached, and the training bundle that carries them is the copy that matters).
