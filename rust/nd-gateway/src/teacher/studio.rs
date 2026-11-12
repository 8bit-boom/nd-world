//! Export for Unsloth Studio (the Train page of Unsloth Desktop): the world's training examples as plain JSONL in the "chatml" format
//! (`{"messages": [...]}` per line), zipped with a README of the settings to choose. The same builders as ND Lore Forge's Step 2 (`builders.rs` is a
//! port of its `dataset::builders`), plus the examples the world's own AI model wrote (`gen.rs`) when there are any.
//! Python-free image only: there is no Python counterpart (the hybrid image answers "Not available here").
use std::io::Write;

use serde_json::{json, Value};

use super::builders::{generate, system_prompt, Example, Recipe};
use super::docs::{load, Members};
use super::gen;
use super::hw::{self, Hardware};
use super::split::held_out_sources;

pub struct Options {
    /// the long world-book texts become answers to "Tell me what the lore says about <title>" (Studio has no plain-text mode)
    pub world_book: bool,
    /// the examples the world's AI model wrote, when there are any
    pub teacher: bool,
    /// share of the questions kept out of training as `eval.jsonl`, in percent (0-30)
    pub test_percent: u8,
    /// examples longer than this many tokens (estimated) are left out, so the context length can be small; 0 = keep everything
    pub max_tokens: usize,
    /// at most this many rows in `eval.jsonl` (an even sample); 0 = all of them
    pub eval_limit: usize,
    /// the card the settings are for
    pub hw: Hardware,
}

pub struct Built {
    pub train: Vec<String>,
    pub eval: Vec<String>,
    pub world_book: usize,
    pub chats: usize,
    pub teacher: usize,
    /// the longest kept example and the sum over the training rows, in estimated tokens
    pub longest: usize,
    pub total_tokens: usize,
    pub world: String,
    /// how many worlds the data comes from (1 unless "all worlds")
    pub worlds: usize,
    /// examples left out for being longer than `max_tokens`
    pub skipped_long: usize,
    pub max_tokens: usize,
    /// held-out questions before the `eval_limit` was applied
    pub eval_total: usize,
    /// the card the settings are for
    pub hw: Hardware,
}

/// Tokens of a conversation, estimated: about 3.5 characters each, plus a few for every message's wrapper.
fn approx_tokens(messages: &[(String, String)]) -> usize {
    messages.iter().map(|(_, c)| c.chars().count() as f64 / 3.5 + 6.0).sum::<f64>().ceil() as usize
}

fn chatml(messages: &[(String, String)]) -> Option<String> {
    if !messages.iter().any(|(r, _)| r == "assistant") {
        return None;
    }
    let msgs: Vec<Value> = messages.iter().map(|(r, c)| json!({"role": r, "content": c})).collect();
    serde_json::to_string(&json!({"messages": msgs})).ok()
}

/// The title of a world-book piece: its first markdown heading, else the source label.
fn title_of(text: &str, source: &str) -> String {
    text.lines().map(str::trim).find_map(|l| l.strip_prefix("# ")).map(|t| t.trim().to_string()).filter(|t| !t.is_empty())
        .unwrap_or_else(|| source.split(':').nth(1).unwrap_or(source).to_string())
}

fn pairs(e: &Example) -> Vec<(String, String)> {
    e.messages.iter().map(|m| (m.role.clone(), m.content.clone())).collect()
}

pub fn build(members: &[(&'static str, Vec<u8>)], teacher: &[gen::Example], opts: &Options) -> Result<Built, String> {
    let loaded = load(&Members(members))?;
    let recipe = Recipe { world_book: opts.world_book, test_percent: opts.test_percent.min(30), ..Recipe::default() };
    let examples = generate(&loaded, &recipe);
    // a question's source is held out as a whole: the test asks about things the model saw as text but never as a question
    let chat_sources: Vec<String> = examples.iter().filter(|e| e.text.is_none()).map(|e| e.source.clone()).collect();
    let held = held_out_sources(&chat_sources, recipe.seed, recipe.test_percent);
    let world = loaded.world_name.clone();
    let (mut train, mut eval) = (Vec::<String>::new(), Vec::<String>::new());
    let (mut book, mut chats, mut tch, mut longest, mut total, mut skipped) = (0usize, 0usize, 0usize, 0usize, 0usize, 0usize);
    let too_long = |t: usize| opts.max_tokens > 0 && t > opts.max_tokens;
    for e in &examples {
        if let Some(text) = &e.text {
            if !opts.world_book {
                continue;
            }
            let title = title_of(text, &e.source);
            let msgs = vec![("system".to_string(), system_prompt(&world)), ("user".to_string(), format!("Tell me what the lore says about {title}.")), ("assistant".to_string(), text.clone())];
            let t = approx_tokens(&msgs);
            if too_long(t) {
                skipped += 1;
            } else if let Some(l) = chatml(&msgs) {
                longest = longest.max(t);
                total += t;
                train.push(l);
                book += 1;
            }
        } else {
            let msgs = pairs(e);
            let t = approx_tokens(&msgs);
            if too_long(t) {
                skipped += 1;
            } else if let Some(l) = chatml(&msgs) {
                if held.contains(&e.source) {
                    eval.push(l);
                } else {
                    longest = longest.max(t);
                    total += t;
                    train.push(l);
                    chats += 1;
                }
            }
        }
    }
    if opts.teacher {
        for t in teacher.iter().filter(|t| t.status == "keep") {
            let msgs: Vec<(String, String)> = t.messages.iter().map(|m| (m.role.clone(), m.content.clone())).collect();
            let n = approx_tokens(&msgs);
            if too_long(n) {
                skipped += 1;
            } else if let Some(l) = chatml(&msgs) {
                longest = longest.max(n);
                total += n;
                train.push(l);
                tch += 1;
            }
        }
    }
    if train.is_empty() {
        return Err("This world has nothing to export yet.".into());
    }
    let eval_total = eval.len();
    Ok(Built { train, eval, world_book: book, chats, teacher: tch, longest, total_tokens: total, world, worlds: 1, skipped_long: skipped, max_tokens: opts.max_tokens, eval_total, hw: opts.hw.clone() })
}

/// Several worlds in one dataset (every example carries its own world's name in its system message, so one model can hold them all).
pub fn merge(parts: Vec<Built>) -> Option<Built> {
    let names: Vec<String> = parts.iter().map(|p| p.world.clone()).collect();
    let mut it = parts.into_iter();
    let mut all = it.next()?;
    let mut n = 1;
    for p in it {
        all.train.extend(p.train);
        all.eval.extend(p.eval);
        all.world_book += p.world_book;
        all.chats += p.chats;
        all.teacher += p.teacher;
        all.longest = all.longest.max(p.longest);
        all.total_tokens += p.total_tokens;
        all.skipped_long += p.skipped_long;
        all.eval_total += p.eval_total;
        n += 1;
    }
    all.world = match names.len() {
        1 => names[0].clone(),
        k => format!("{} and {}", names[..k - 1].join(", "), names[k - 1]),
    };
    all.worlds = n;
    Some(all)
}

/// The settings to type into Unsloth Studio's Train page (also written as `studio-settings.yaml`, which Studio's "Load YAML" imports), worked out from the data.
pub struct Settings {
    pub max_seq_length: usize,
    pub num_epochs: usize,
    pub learning_rate: f64,
    pub batch_size: usize,
    pub grad_accum: usize,
    pub warmup_steps: usize,
    pub save_steps: usize,
    pub eval_steps: f64,
    pub steps_per_epoch: usize,
    pub total_steps: usize,
    /// what the card trains: the model, the method and the memory it is estimated to take
    pub model: String,
    pub repo: String,
    pub method: &'static str,
    pub est_gb: f64,
    pub hw_label: String,
}

/// The context the data needs: the next power of two over the longest kept example.
fn needed_ctx(b: &Built) -> usize {
    (((b.longest as f64) * 1.1) as usize).next_power_of_two().clamp(2048, 32768)
}

pub fn settings(b: &Built) -> Settings {
    let rows = b.train.len().max(1);
    let plan = hw::plan(b.hw.vram_gb, needed_ctx(b));
    let max_seq_length = plan.ctx;
    // a long context (or a small card) takes one sequence at a time, the same 8 per update
    let (batch_size, grad_accum) = if max_seq_length > 8192 || b.hw.vram_gb <= 12.0 { (1, 8) } else { (2, 4) };
    let steps_per_epoch = rows.div_ceil(batch_size * grad_accum);
    // a big dataset needs fewer passes than a small one
    let num_epochs = if rows >= 20000 { 1 } else if rows >= 3000 { 2 } else { 3 };
    let total_steps = steps_per_epoch * num_epochs;
    let warmup_steps = (total_steps / 30).clamp(5, 100);
    let save_steps = (total_steps / 10).div_ceil(50).max(1) * 50;
    let eval_steps = if !b.eval.is_empty() && b.eval.len() <= 1000 { 0.1 } else { 0.0 };
    Settings { max_seq_length, num_epochs, learning_rate: if rows >= 20000 { 1e-4 } else { 2e-4 }, batch_size, grad_accum, warmup_steps, save_steps: save_steps.min(2000), eval_steps, steps_per_epoch, total_steps,
               model: plan.model.map(|m| m.name.to_string()).unwrap_or_else(|| "(none fits)".to_string()), repo: plan.model.map(|m| m.repo.to_string()).unwrap_or_default(), method: plan.method, est_gb: plan.est_gb, hw_label: b.hw.label.clone() }
}

/// The file Studio's "Load YAML" reads (the same keys Studio's own "Save YAML" writes). Only settings: the model and the dataset are chosen on the page.
pub fn settings_yaml(s: &Settings) -> String {
    let head = format!("# nd-world: recommended for {}\n# {}, {}, context {}, about {:.0} GB of memory (an estimate). Load with Train -> Configuration -> Load YAML.\n", s.hw_label, s.model, s.method, s.max_seq_length, s.est_gb);
    head + &format!("training:\n  max_seq_length: {}\n  num_epochs: {}\n  learning_rate: {}\n  embedding_learning_rate: null\n  batch_size: {}\n  gradient_accumulation_steps: {}\n  warmup_steps: {}\n  max_steps: 0\n  save_steps: {}\n  eval_steps: {}\n  early_stopping_patience: 0\n  weight_decay: 0.001\n  random_seed: 3407\n  packing: false\n  train_on_completions: true\n  gradient_checkpointing: unsloth\n  offload_layers: 0\n  offload_vram_gb: null\n  offload_vram_gb_per_device: {{}}\n  prefetch_depth: 2\n  optim: adamw_8bit\n  lr_scheduler_type: linear\nlora:\n  lora_r: 16\n  lora_alpha: 32\n  lora_dropout: 0\n  target_modules:\n    - q_proj\n    - k_proj\n    - v_proj\n    - o_proj\n    - gate_proj\n    - up_proj\n    - down_proj\n  use_rslora: false\n  use_loftq: false\n  use_dora: false\nlogging:\n  enable_wandb: false\n  wandb_project: llm-finetuning\n  enable_tensorboard: false\n  tensorboard_dir: runs\n  log_frequency: 10\n",
        s.max_seq_length, s.num_epochs, if s.learning_rate < 1.5e-4 { "0.0001" } else { "0.0002" }, s.batch_size, s.grad_accum, s.warmup_steps, s.save_steps, if s.eval_steps > 0.0 { "0.1" } else { "0" })
}

pub fn readme(b: &Built, nd_build: &str) -> String {
    let st = settings(b);
    let lr = if st.learning_rate < 1.5e-4 { "0.0001" } else { "0.0002" };
    format!("# {world}: training data for Unsloth Studio\n\nMade by nd-world ({nd_build}). {rows} rows in `train.jsonl` ({chats} questions and answers{book}{teacher}){eval}.\n{skipped}\n\
## Recommended for your hardware\n\
**{hw_label}**\n\n\
| | |\n|---|---|\n\
| Model | **{rec_model}** (`{rec_repo}`) |\n\
| Method | **{rec_method}**{method_why} |\n\
| Context Length | **{ctx}** |\n\
| Memory (estimate) | about {est_gb:.0} GB of {vram:.0} GB |\n\
{hw_notes}\n\
The same data on other cards (re-download with another card chosen to get its YAML):\n\n\
| Card | Model | Method | Context | Memory (est.) |\n|---|---|---|---|---|\n{tiers}\n\
The memory figures are estimates from Unsloth's published numbers; if Studio runs out of memory, lower the context or the batch size (the YAML uses one sequence at a time on small cards).\n\n\
## Quick start (about two minutes)\n\
1. In Unsloth Studio open **Train**.\n\
2. **Model**: **{rec_model}** (see \"Choosing the model\" below). Method **{rec_method}**. *Train as*: **Language model**.\n\
3. **Dataset → Local**: drop `train.jsonl`. Add `eval.jsonl` as the **Eval dataset** (if present). *Target Format*: **chatml** (`auto` finds it too).\n\
4. **Configuration → Load YAML**: choose `studio-settings.yaml` from this folder. It sets everything in the table below.\n\
5. Press **Start Training**. When it finishes: **Export** → *GGUF* (Ollama, llama.cpp, LM Studio) or *Merged model* (16-bit files).\n\n\
## The settings (what `studio-settings.yaml` contains)\n\
| Setting (Studio) | Value | Why |\n|---|---|---|\n\
| Context Length (`max_seq_length`) | **{ctx}** | the longest example is about {tok} tokens |\n\
| Epochs | **{epochs}** | {rows} rows; about {spe} steps per epoch, {total} steps in all |\n\
| Max Steps | 0 | the epochs decide |\n\
| Learning Rate | **{lr}** | {lr_why} |\n\
| Batch Size × Grad Accum | **{bs} × {ga} = 8** | {bs_why} |\n\
| Warmup Steps | **{warm}** | about 3 % of the steps |\n\
| Save Steps | **{save}** | a checkpoint about every tenth of the run (not every 30 steps: that fills the disk) |\n\
| Eval Steps | **{eval_steps}** | {eval_why} |\n\
| Early Stopping Patience | 0 | off |\n\
| Weight Decay | 0.001 | |\n\
| Seed | 3407 | |\n\
| Optimizer / LR Scheduler | AdamW 8-bit / linear | |\n\
| Rank / Alpha / Dropout | 16 / 32 / 0 | rank 16 is plenty for a world; alpha twice the rank |\n\
| Target Modules | q, k, v, o, gate, up, down | all seven |\n\
| LoRA variant | LoRA | |\n\
| Gradient Checkpointing | Unsloth | least memory |\n\
| Enable packing | **off** | packing glues examples (and worlds) together; leave it off until a first run looks healthy |\n\
| Assistant completions only | **on** | the model learns the answers, not the questions |\n\
| Offload layers | Off | |\n\n\
Train a first, short run to see speed and loss: in the Dataset section set *Train Split End* to about 3000, or use Max Steps 300, then do the full run.\n\n\
## Choosing the model\n\
- Use an official base: **Qwen3.5 9B** (or 4B / 2B), **Gemma 4 E4B / E2B**. They have official GGUF files and, for models with an image part, the vision file, so a model trained here can still read pictures.\n\
- Community merges (for example \"13B ... Heretic ... Thinking\") are riskier: their chat templates expect thinking blocks this data does not have, there is usually no official vision file, and a 13B model at 16-bit leaves almost no memory on a 32 GB card at this context. If you must, use QLoRA.\n\
- If the model has an image part, train the **language layers only** (*Train as: Language model*; leave *Vision Layers* off if Studio shows that checkbox).\n\n\
## If something goes wrong\n\
- **\"Failed to format ChatML dataset ... Operation not permitted ... /workspace/.cache/huggingface/datasets/...\"** (Studio in a container, for example on TrueNAS SCALE): the folder behind `/workspace` refuses the `chmod` / `chown` calls the dataset cache needs (typically a dataset with NFSv4 ACLs and ACL mode *Restricted*). The data is fine. Easiest fix: give the container the environment variable `HF_DATASETS_CACHE=/tmp/hf-datasets` (TrueNAS: Apps, your Unsloth app, Edit, Environment Variables), so the cache lives inside the container, and restart the app. The other fix: on the dataset set *ACL Type* POSIX (or *ACL Mode* Passthrough), make the app's user the owner and apply recursively. To check: in a shell in the container, `touch /workspace/.cache/t && chmod 644 /workspace/.cache/t` fails with \"Operation not permitted\" when this is the cause.\n\
- **Out of memory:** lower *Context Length* one step, keep batch size 1 with accumulation 8, use QLoRA, and free the card first (Ollama keeps models loaded: `ollama stop <model>`).\n\
- **The loss turns into nan** (Volta / Turing cards, which have no bf16): halve the learning rate.\n\
- **Studio does not recognise the dataset:** set *Target Format* to `chatml` instead of Auto.\n\n\
## Notes\n\
- {worlds_note}\n\
- The text of the world book is {book_note}.\n\
- {eval_note}\n\
- Example lengths are estimates (about 3.5 characters per token).\n\
- For more control (reviewing and editing the examples, a test page that scores the trained model against the original), use ND Lore Forge with the Training bundle.\n\
- **GM secrets are in this data.** The trained model will repeat them: keep it away from players.\n",
        world = b.world, nd_build = nd_build, rows = b.train.len(), chats = b.chats,
        book = if b.world_book > 0 { format!(", {} world-book pieces turned into answers", b.world_book) } else { String::new() },
        teacher = if b.teacher > 0 { format!(", {} written and checked by this world's AI model", b.teacher) } else { String::new() },
        eval = if b.eval.is_empty() { String::new() } else { format!(", {} held-out questions in `eval.jsonl`{}", b.eval.len(), if b.eval_total > b.eval.len() { format!(" (an even sample of {})", b.eval_total) } else { String::new() }) },
        skipped = if b.skipped_long > 0 { format!("\n**{} examples longer than {} tokens were left out**, so the context can stay small. (The export card can keep them, at a much larger context.)\n", b.skipped_long, b.max_tokens) } else { String::new() },
        hw_label = b.hw.label, rec_model = st.model, rec_repo = st.repo, rec_method = st.method, est_gb = st.est_gb, vram = b.hw.vram_gb,
        method_why = if st.method == "LoRA" { " (16-bit weights: it fits this card at this context)" } else { " (4-bit weights: the 16-bit method would not fit at this context)" },
        hw_notes = { let n = hw::notes_for(&b.hw.name); if n.is_empty() { String::new() } else { n.iter().map(|x| format!("- {x}\n")).collect::<String>() } },
        tiers = hw::TIERS.iter().map(|(gb, label)| { let p = hw::plan(*gb, needed_ctx(b)); format!("| {} | {} | {} | {} | about {:.0} GB |", label, p.model.map(|m| m.name).unwrap_or("(too small)"), p.method, p.ctx, p.est_gb) }).collect::<Vec<_>>().join("\n"),
        ctx = st.max_seq_length, tok = b.longest, epochs = st.num_epochs, spe = st.steps_per_epoch, total = st.total_steps, lr = lr,
        lr_why = if st.learning_rate < 1.5e-4 { "a long run over a big dataset: the lower rate is steadier" } else { "the usual rate for a small dataset" },
        bs = st.batch_size, ga = st.grad_accum, bs_why = if st.batch_size == 1 { "a long context: one sequence at a time, the same 8 per update" } else { "two sequences at a time, eight per update" },
        warm = st.warmup_steps, save = st.save_steps,
        eval_steps = if st.eval_steps > 0.0 { "0.1" } else { "0" },
        eval_why = if st.eval_steps > 0.0 { "an evaluation every tenth of the run (a small eval file keeps it cheap)" } else { "off: no eval file, or one too big to evaluate often" },
        worlds_note = if b.worlds > 1 { format!("This file holds {} worlds at once. Every example carries the name of its own world in its system message, so one model can learn all of them and still tell them apart.", b.worlds) } else { "This file holds one world. Tick several worlds on the export card to train them in one model.".to_string() },
        book_note = if b.world_book > 0 { "included as answers to \"Tell me what the lore says about <title>\" (Studio has no plain-text mode for this)" } else { "left out (it was switched off for this export)" },
        eval_note = if b.eval.is_empty() { "No questions were held out, so there is no `eval.jsonl`.".to_string() } else { "`eval.jsonl` holds questions the training never saw: add it as the Eval dataset.".to_string() })
}

/// The zip: train.jsonl, eval.jsonl (when there are held-out questions), studio-settings.yaml, README.md. Fixed timestamps and compression, so the same world gives the same bytes.
pub fn zip(b: &Built, nd_build: &str) -> Option<Vec<u8>> {
    let opts = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .compression_level(Some(6))
        .last_modified_time(zip::DateTime::from_date_and_time(1980, 1, 1, 0, 0, 0).ok()?)
        .unix_permissions(0o644);
    let mut w = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    w.start_file("train.jsonl", opts).ok()?;
    w.write_all((b.train.join("\n") + "\n").as_bytes()).ok()?;
    if !b.eval.is_empty() {
        w.start_file("eval.jsonl", opts).ok()?;
        w.write_all((b.eval.join("\n") + "\n").as_bytes()).ok()?;
    }
    w.start_file("studio-settings.yaml", opts).ok()?;
    w.write_all(settings_yaml(&settings(b)).as_bytes()).ok()?;
    w.start_file("README.md", opts).ok()?;
    w.write_all(readme(b, nd_build).as_bytes()).ok()?;
    Some(w.finish().ok()?.into_inner())
}

/// At most `limit` held-out questions, an even sample (every k-th), so the evaluation stays cheap and still covers everything; 0 keeps them all.
pub fn finish(mut b: Built, limit: usize) -> Built {
    if limit > 0 && b.eval.len() > limit {
        let n = b.eval.len();
        b.eval = (0..limit).map(|i| b.eval[i * n / limit].clone()).collect();
    }
    b
}

/// What the page shows before downloading: counts for the chosen options.
pub fn summary(b: &Built) -> Value {
    let st = settings(b);
    json!({"rows": b.train.len(), "chats": b.chats, "world_book": b.world_book, "teacher": b.teacher, "eval": b.eval.len(), "eval_total": b.eval_total, "skipped_long": b.skipped_long,
           "worlds": b.worlds, "world": b.world, "context": st.max_seq_length, "model": st.model, "method": st.method, "est_gb": (st.est_gb * 10.0).round() / 10.0, "hardware": hw::info(&b.hw), "epochs": st.num_epochs, "steps": st.total_steps, "longest_tokens": b.longest})
}
