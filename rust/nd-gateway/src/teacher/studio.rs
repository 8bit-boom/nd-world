//! Export for Unsloth Studio (the Train page of Unsloth Desktop): the world's training examples as plain JSONL in the "chatml" format
//! (`{"messages": [...]}` per line), zipped with a README of the settings to choose. The same builders as ND Lore Forge's Step 2 (`builders.rs` is a
//! port of its `dataset::builders`), plus the examples the world's own AI model wrote (`gen.rs`) when there are any.
//! Python-free image only: there is no Python counterpart (the hybrid image answers "Not available here").
use std::io::Write;

use serde_json::{json, Value};

use super::builders::{generate, system_prompt, Example, Recipe};
use super::docs::{load, Members};
use super::gen;
use super::split::held_out_sources;

pub struct Options {
    /// the long world-book texts become answers to "Tell me what the lore says about <title>" (Studio has no plain-text mode)
    pub world_book: bool,
    /// the examples the world's AI model wrote, when there are any
    pub teacher: bool,
    /// share of the questions kept out of training as `eval.jsonl`, in percent (0-30)
    pub test_percent: u8,
}

impl Default for Options {
    fn default() -> Self {
        Options { world_book: true, teacher: true, test_percent: 10 }
    }
}

pub struct Built {
    pub train: Vec<String>,
    pub eval: Vec<String>,
    pub world_book: usize,
    pub chats: usize,
    pub teacher: usize,
    pub longest: usize,
    pub world: String,
    /// how many worlds the data comes from (1 unless "all worlds")
    pub worlds: usize,
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
    let (mut book, mut chats, mut tch, mut longest) = (0usize, 0usize, 0usize, 0usize);
    for e in &examples {
        if let Some(text) = &e.text {
            if !opts.world_book {
                continue;
            }
            let title = title_of(text, &e.source);
            let msgs = vec![("system".to_string(), system_prompt(&world)), ("user".to_string(), format!("Tell me what the lore says about {title}.")), ("assistant".to_string(), text.clone())];
            if let Some(l) = chatml(&msgs) {
                longest = longest.max(l.len());
                train.push(l);
                book += 1;
            }
        } else if let Some(l) = chatml(&pairs(e)) {
            longest = longest.max(l.len());
            if held.contains(&e.source) {
                eval.push(l);
            } else {
                train.push(l);
                chats += 1;
            }
        }
    }
    if opts.teacher {
        for t in teacher.iter().filter(|t| t.status == "keep") {
            let msgs: Vec<(String, String)> = t.messages.iter().map(|m| (m.role.clone(), m.content.clone())).collect();
            if let Some(l) = chatml(&msgs) {
                longest = longest.max(l.len());
                train.push(l);
                tch += 1;
            }
        }
    }
    if train.is_empty() {
        return Err("This world has nothing to export yet.".into());
    }
    Ok(Built { train, eval, world_book: book, chats, teacher: tch, longest, world, worlds: 1 })
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
        n += 1;
    }
    all.world = match names.len() {
        1 => names[0].clone(),
        k => format!("{} and {}", names[..k - 1].join(", "), names[k - 1]),
    };
    all.worlds = n;
    Some(all)
}

pub fn readme(b: &Built, nd_build: &str) -> String {
    let ctx = (((b.longest as f64) / 3.5 * 1.15) as usize).next_power_of_two().clamp(2048, 32768);
    format!("# {world}: training data for Unsloth Studio\n\nMade by nd-world ({nd_build}). {rows} rows in `train.jsonl` ({chats} questions and answers{book}{teacher}){eval}.\n\n\
## In Unsloth Studio (Train)\n\
1. **Model**: choose your model (for example Qwen3.5 9B). Method **LoRA** (or QLoRA when memory is short).\n\
2. **Dataset → Local**: drop `train.jsonl` on the page. **Format**: `chatml` (every line is `{{\"messages\": [...]}}`; `auto` finds it too).\n\
3. **Hyperparameters**: *Context Length* at least **{ctx}** (the longest example is about {tok} tokens). Switch **Train on Completions** ON: the model should learn the answers, not the questions. *Packing* OFF. Rank 16-32, learning rate 2e-4 (lower, 1e-4, for the 9B), 2-3 epochs for a small world.\n\
4. If the model has an image part (Qwen3.5, Gemma 4), train the **language layers only**: leave *Vision Layers* off if Studio shows that checkbox. The vision part then stays as it was and the base model's images keep working.\n\
5. Start training. When it finishes, use **Export** in Studio: *GGUF* for Ollama / llama.cpp, or *Merged model* for 16-bit files.\n\n\
## Notes\n\
- {worlds_note}\n\
- The text of the world book is {book_note}.\n\
- {eval_note}\n\
- For more control (several worlds in one model, reviewing and editing the examples, a test page that scores the trained model against the original), use ND Lore Forge with the Training bundle.\n\
- **GM secrets are in this data.** The trained model will repeat them: keep it away from players.\n",
        world = b.world, nd_build = nd_build, rows = b.train.len(), chats = b.chats,
        book = if b.world_book > 0 { format!(", {} world-book pieces turned into answers", b.world_book) } else { String::new() },
        teacher = if b.teacher > 0 { format!(", {} written and checked by this world's AI model", b.teacher) } else { String::new() },
        eval = if b.eval.is_empty() { String::new() } else { format!(", {} held-out questions in `eval.jsonl`", b.eval.len()) },
        tok = (b.longest as f64 / 3.5) as usize,
        worlds_note = if b.worlds > 1 { format!("This file holds {} worlds at once. Every example carries the name of its own world in its system message, so one model can learn all of them and still tell them apart.", b.worlds) } else { "This file holds one world. Download again in another world, or tick \"All worlds in one file\", to train several.".to_string() },
        book_note = if b.world_book > 0 { "included as answers to \"Tell me what the lore says about <title>\" (Studio has no plain-text mode for this)" } else { "left out (it was switched off for this export)" },
        eval_note = if b.eval.is_empty() { "No questions were held out, so there is no `eval.jsonl`.".to_string() } else { "`eval.jsonl` holds questions the training never saw. If Studio lets you pick an eval split for a local file, use it; otherwise ignore the file.".to_string() })
}

/// The zip: train.jsonl, eval.jsonl (when there are held-out questions), README.md. Fixed timestamps and compression, so the same world gives the same bytes.
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
    w.start_file("README.md", opts).ok()?;
    w.write_all(readme(b, nd_build).as_bytes()).ok()?;
    Some(w.finish().ok()?.into_inner())
}

/// What the page shows before downloading: counts for the chosen options.
pub fn summary(b: &Built) -> Value {
    json!({"rows": b.train.len(), "chats": b.chats, "world_book": b.world_book, "teacher": b.teacher, "eval": b.eval.len(), "worlds": b.worlds, "world": b.world})
}
