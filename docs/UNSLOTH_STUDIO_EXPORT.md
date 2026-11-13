# Export for Unsloth Studio

Export & Backup has a card **Training data for Unsloth Studio**. It downloads the active world's training examples as a ready dataset for the
**Train** page of Unsloth Studio / Unsloth Desktop: `train.jsonl` and `eval.jsonl` in the *chatml* format (one `{"messages": [...]}` per line, the shape
Studio's Format = chatml reads) and a `README.md` with the settings to choose (context length, *Train on Completions* on, language layers only for
models with an image part). GM only; the file contains GM secrets (labelled "GM secret"), so a model trained on it repeats them.

`GET /export/unsloth-studio.zip?w=<world slug>&world_book=1&teacher=1&test=10` (`info=1` returns only the counts as JSON; `worlds_all=1` puts every world of the server in one file).

**Which world?** The one named by `w` (the card puts the world you are in into the address), else the active-world cookie: one world per file, `<slug>-unsloth-studio.zip`. The answer is sent `Cache-Control: private, no-store` and `Vary: Cookie`, because it depends on who asks and which world; a cache keyed on the address alone (a browser, a proxy, a CDN that caches `.zip` files) must never hand one world's file to another. **All worlds:** tick *All worlds in one file* (`worlds_all=1`, file `all-worlds-unsloth-studio.zip`): every example carries its own world's name in its system message, so one model can learn them all and still tell them apart; a world with nothing to export is left out.

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

## Settings for Studio, and limits

The zip also holds **`studio-settings.yaml`**, in the shape Studio's own *Save YAML* writes, so *Configuration → Load YAML* on the Train page takes it. Values are worked
out from the data (`teacher/studio.rs::settings`): context length (the next power of two over the longest kept example, 2048-32768), batch 2 x 4 (1 x 8 above 8192),
1 / 2 / 3 epochs for 20000+ / 3000+ / fewer rows, learning rate 1e-4 for a big dataset and 2e-4 otherwise, warmup about 3 % of the steps, save steps about a tenth of the run
(never every 30 steps), eval 0.1 for a small eval file (0 for none or a big one), packing **off**, train-on-completions **on**, rank 16 / alpha 32, all seven target
modules. The README has the same values as a table with the reasons, the quick start, and advice on choosing the model (an official base such as Qwen3.5 9B, not a community
merge; QLoRA when memory is short).

`max_tokens` (default 8192; 0 keeps everything) leaves out examples longer than that many tokens (estimated at 3.5 characters each), counted in the README and in `info=1`
(`skipped_long`), so a few outliers do not force a 32768 context. `eval_limit` (default 300; 0 = all) caps `eval.jsonl` at an even sample of the held-out questions
(`eval_total` says how many there were). Tested in `tests/test_gateway_ai.py` (the YAML keys are Studio's; the values follow the data).

## Recommended settings for the training card

The settings (and the README's "Recommended for your hardware" section) are worked out for a card: `vram=<GB>` on the address (the card's *Training card* list), else the card
nd-world sees on its own machine (`nvidia-smi` or AMD sysfs, the same reading as Settings > System's "Detected hardware"; on TrueNAS SCALE give the nd-world app the GPU, see
`docs/GPU_SETUP.md`), else an assumed 24 GB. nd-world may run on a different computer than the one that trains (the TrueNAS server vs the PC): pick the training computer's card.

`teacher/hw.rs`: the biggest of Qwen3.8 27B (dense, vision; Unsloth: QLoRA works on 24 GB, 16-bit LoRA needs about 60 GB with its weights alone about 56 GB; the 4-bit `unsloth/Qwen3.8-27B-unsloth-bnb-4bit` is what Studio loads for 4-bit), Qwen3.5 9B, Gemma 4 E4B, Qwen3.5 4B, Gemma 4 E2B, Qwen3.5 2B whose 4-bit QLoRA fits a 4096 context in the usable memory (92 % of the card minus
1 GB), then LoRA (16-bit) when it also fits at the context the data needs, else QLoRA; the context is the data's need cut to what fits. The memory figures are Unsloth's published ones for
each model at 2048 / batch 1 (the same numbers as ND Lore Forge's catalog), scaled for context and batch: **estimates**. A small card (12 GB or less) or a context over 8192 gets one
sequence per step (batch 1 x accumulation 8). `max_tokens=auto` (the default) leaves out examples longer than the card trains, at most 8192; a number overrides it. Volta / Turing
cards (V100, T10, T4, RTX 20) get a note: no bf16, training runs in fp16. The README lists every tier (8 / 12 / 16 / 24 / 32 / 48 / 80 GB) with its model, method and context.

The README ends with an "If something goes wrong" section: the dataset-cache `Operation not permitted` error of Studio in a container on TrueNAS SCALE (set `HF_DATASETS_CACHE=/tmp/hf-datasets`, or fix the dataset's ACL type / mode), out of memory, `nan` loss on cards without bf16, and the Target Format.

## Defaults that follow Unsloth Studio's own

The YAML and README use Studio's per-model defaults (read from its source, `studio/backend/assets/configs/model_defaults`): LoRA rank 16 / alpha 16 for Qwen3.5, rank 8 / alpha 8 for
Gemma 4 (alpha equal to the rank is also what Unsloth's docs recommend), packing **off** for both families, and the vision layers **off** (`finetune_vision_layers: false`, language /
attention / MLP layers on) so a model with an image part still reads pictures. Early stopping (patience 3) is on when there is an `eval.jsonl`. Exact duplicate rows and any message with
an empty text are left out (counted in the README).

## Train in Unsloth Studio from nd-world

The card's **Train in Unsloth Studio now** button sends the data and the same settings straight to the Studio set in Settings (address and API key), through Studio's OpenAI-style fine-tuning API
(`/v1/files`, `/v1/fine_tuning/jobs`; read from Studio's source, not in its public docs, so a Studio that is too old answers 404 and nd-world says to use the zip). Routes (Python-free, GM only):

| Route | |
|---|---|
| `POST /export/unsloth-studio/train?...` | the same query as the zip; uploads `train.jsonl` / `eval.jsonl`, starts the job, answers Studio's job object plus `nd` (model, method, context, rows). `per_world=1`: one job for every world ticked |
| `GET /export/unsloth-studio/job?id=` | the job, its last events and its checkpoint count (the card polls every 5 s) |
| `POST /export/unsloth-studio/job/cancel?id=` | stops the job in Studio |
| `POST /export/unsloth-studio/job/{pause,resume}?id=` | answer 400 "Unsloth cannot pause a training job; cancel it and start again." without calling Studio (Studio always refuses them: "Pausing and resuming fine-tuning jobs is not supported; use cancel."). The card has no Pause / Resume buttons; the routes stay so old links are harmless |

`vram=studio` (the card list's "The card Unsloth Studio reports") reads the graphics card from Studio's `/api/train/hardware`, which is right when Studio runs on another computer than nd-world.
While Studio trains on the same card, Studio unloads its chat models, so AI chat in nd-world stops answering until the job is done.
Not done: asking Studio to export a GGUF afterwards. Studio's export routes need the checkpoint's path on Studio's disk, which the API-key routes most likely hide; export in Studio's own *Export* tab.

QLoRA note: Unsloth's Qwen3.5 page says both that QLoRA is not recommended for any Qwen3.5 model and (in its sample code) that only MoE QLoRA is not recommended and dense is fine. The README tells the owner that QLoRA is fine for the dense sizes it recommends, with a little more quality loss than 16-bit.

## Reasoning share (thinking blocks)

Unsloth's Qwen3.8 page: to keep a model's reasoning ability, mix reasoning-style examples with direct answers and keep at least 75 % reasoning data. The export card's **Reasoning share**
(`reasoning=0|25|50|75|100` on the address, default 0) gives that share of the rows (chosen by a hash of the question, so the same data always gives the same rows) a short thinking block
`<think>\n...\n</think>\n\n` before the answer. The thinking is plain and grounded: what is asked, the start of what the lore says (the answer's own opening), and that the answer stays inside the lore;
three phrasings. It keeps the thinking format alive; it is not new knowledge. A row that already has a `<think>` block is left alone. World-book rows, questions and the AI-model examples all count. With 0
the README warns that direct-answers-only training makes a reasoning model think less. Better reasoning examples (written by a strong model) are a follow-up; the teacher examples could carry them.
