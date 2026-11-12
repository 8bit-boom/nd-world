# Export for Unsloth Studio

Export & Backup has a card **Training data for Unsloth Studio**. It downloads the active world's training examples as a ready dataset for the
**Train** page of Unsloth Studio / Unsloth Desktop: `train.jsonl` and `eval.jsonl` in the *chatml* format (one `{"messages": [...]}` per line, the shape
Studio's Format = chatml reads) and a `README.md` with the settings to choose (context length, *Train on Completions* on, language layers only for
models with an image part). GM only; the file contains GM secrets (labelled "GM secret"), so a model trained on it repeats them.

`GET /export/unsloth-studio.zip?world_book=1&teacher=1&test=10` (`info=1` returns only the counts as JSON).

What is in it:

- **Questions and answers** from the same deterministic builders as ND Lore Forge's Step 2 (`rust/nd-gateway/src/teacher/builders.rs` is a port of its
  `dataset::builders`): who or what an entry is, other names, relations, details such as rank, lists of entries by kind, random tables, sessions and
  quests, rules sections, player characters, GM secrets. Stable ids, the same world gives the same bytes.
- **The world book** (the long articles): Studio has no plain-text mode, so each piece becomes the answer to "Tell me what the lore says about <title>".
  `world_book=0` leaves it out.
- **Examples the world's AI model wrote** (`docs/TEACHER_EXAMPLES.md`), when there are any and they were not dropped by the checking pass (`teacher=0` leaves them out).
- **`eval.jsonl`**: `test` percent (0-30, default 10) of the question sources, held out as a whole, so the evaluation asks about things the model saw as
  text but never as a question. Never contains the world-book answers.

Python-free image only (no Python counterpart): the hybrid image answers "Not available here" and the card says so. For editing the examples, several
worlds in one model, a test page and stop/continue, use the Training Bundle with ND Lore Forge.
