//! The pipeline (a port of ND Lore Forge's `teacher::gen`): pieces of the world -> candidate examples -> a checking pass -> examples on disk.
//! Every model call is cached under `teacher/<world id>/cache/`, so a stopped run, a repeated run and a resumed run only pay for what is new.
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::chunk::chunk_article;
use super::docs::{articles, load, Doc, Loaded, Members};
use crate::ai::Backend;

pub const KINDS: [(&str, &str); 8] = [
    ("t_deeper", "Deeper questions"), ("t_rules", "Rules practice"), ("t_history", "Campaign history"), ("t_npc", "NPC role-play"),
    ("t_lore", "Lore search"), ("t_missing", "Not in the lore"), ("t_general", "General chat"), ("t_style", "House style"),
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Options {
    pub kinds: Vec<String>,
    pub per_source: u8,
    pub general_percent: u8,
    pub judge: bool,
    pub drop_partly: bool,
    pub limit: Option<usize>,
}

impl Default for Options {
    fn default() -> Self {
        Options { kinds: KINDS.iter().map(|k| k.0.to_string()).collect(), per_source: 2, general_percent: 10, judge: true, drop_partly: false, limit: None }
    }
}

impl Options {
    /// From a request body; a missing key keeps its default, a wrong type is an error
    pub fn from_value(v: &Value) -> Option<Options> {
        let d = Options::default();
        let o = v.as_object()?;
        let kinds = match o.get("kinds") {
            None | Some(Value::Null) => d.kinds,
            Some(Value::Array(a)) => a.iter().map(|k| k.as_str().map(String::from)).collect::<Option<Vec<_>>>()?,
            _ => return None,
        };
        let num = |k: &str, dv: u8, max: u8| -> Option<u8> {
            match o.get(k) { None | Some(Value::Null) => Some(dv), Some(x) => x.as_u64().map(|n| n.min(max as u64) as u8) }
        };
        let flag = |k: &str, dv: bool| -> Option<bool> { match o.get(k) { None | Some(Value::Null) => Some(dv), Some(x) => x.as_bool() } };
        let limit = match o.get("limit") { None | Some(Value::Null) => None, Some(x) => Some(x.as_u64()? as usize) };
        Some(Options { kinds, per_source: num("per_source", d.per_source, 4)?.max(1), general_percent: num("general_percent", d.general_percent, 30)?,
                       judge: flag("judge", d.judge)?, drop_partly: flag("drop_partly", d.drop_partly)?, limit })
    }

    pub fn to_value(&self) -> Value {
        json!({"kinds": self.kinds, "per_source": self.per_source, "general_percent": self.general_percent, "judge": self.judge, "drop_partly": self.drop_partly, "limit": self.limit})
    }
}

/// One example, in the shape ND Lore Forge's draft uses (docs/TEACHER_EXAMPLES.md)
#[derive(Debug, Clone)]
pub struct Example {
    pub id: String,
    pub builder: String,
    pub source: String,
    pub source_title: String,
    pub messages: Vec<Message>,
    /// "keep" or "drop" (the checking pass drops what the lore does not support)
    pub status: String,
    pub edited: bool,
    pub check: Option<String>,
    pub note: Option<String>,
}

impl Example {
    pub fn to_value(&self) -> Value {
        let mut o = Map::new();
        o.insert("id".into(), json!(self.id));
        o.insert("builder".into(), json!(self.builder));
        o.insert("source".into(), json!(self.source));
        o.insert("source_title".into(), json!(self.source_title));
        o.insert("messages".into(), Value::Array(self.messages.iter().map(|m| json!({"role": m.role, "content": m.content})).collect()));
        o.insert("status".into(), json!(self.status));
        o.insert("edited".into(), json!(self.edited));
        if let Some(c) = &self.check { o.insert("check".into(), json!(c)); }
        if let Some(n) = &self.note { o.insert("note".into(), json!(n)); }
        Value::Object(o)
    }

    pub fn from_value(v: &Value) -> Option<Example> {
        let s = |k: &str| v[k].as_str().map(String::from);
        Some(Example {
            id: s("id")?, builder: s("builder")?, source: s("source")?, source_title: s("source_title").unwrap_or_default(),
            messages: v["messages"].as_array()?.iter().map(|m| Some(Message { role: m["role"].as_str()?.to_string(), content: m["content"].as_str()?.to_string() })).collect::<Option<Vec<_>>>()?,
            status: s("status").unwrap_or_else(|| "keep".into()), edited: v["edited"].as_bool().unwrap_or(false), check: s("check"), note: s("note"),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub role: String,
    pub content: String,
}

pub fn hash_hex(parts: &[&str]) -> String {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p.as_bytes());
        h.update([0u8]);
    }
    format!("{:x}", h.finalize())
}

pub fn hash_num(s: &str) -> usize {
    let d = Sha256::digest(s.as_bytes());
    usize::from_be_bytes([d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]])
}

pub fn shorten(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let cut: String = text.chars().take(max).collect();
    let end = cut.rfind("\n\n").or_else(|| cut.rfind(". ").map(|i| i + 1)).unwrap_or(cut.len());
    let kept = cut[..end].trim_end();
    if kept.chars().count() < max / 3 { cut.trim_end().to_string() } else { kept.to_string() }
}

pub fn system_prompt(world: &str) -> String {
    format!("You are the game master's assistant for the tabletop world \"{world}\". Answer from the world's lore and rules. Text marked \"GM secret\" is not known to the players. If something is not in the lore, say so instead of inventing it.")
}

pub fn strip_thinking(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find("<think>") {
        out.push_str(&rest[..i]);
        match rest[i..].find("</think>") {
            Some(j) => rest = &rest[i + j + "</think>".len()..],
            None => return out.trim().to_string(),
        }
    }
    out.push_str(rest);
    match out.find("</think>") { Some(i) => out[i + "</think>".len()..].trim().to_string(), None => out.trim().to_string() }
}

pub fn extract_json(text: &str) -> Option<Value> {
    let t = strip_thinking(text);
    if let Ok(v) = serde_json::from_str::<Value>(t.trim()) {
        return Some(v);
    }
    if let Some(start) = t.find("```") {
        let after = &t[start + 3..];
        let after = after.strip_prefix("json").or_else(|| after.strip_prefix("JSON")).unwrap_or(after);
        if let Some(end) = after.find("```") {
            if let Ok(v) = serde_json::from_str::<Value>(after[..end].trim()) {
                return Some(v);
            }
        }
    }
    for (open, close) in [('{', '}'), ('[', ']')] {
        if let (Some(a), Some(b)) = (t.find(open), t.rfind(close)) {
            if a < b {
                if let Ok(v) = serde_json::from_str::<Value>(&t[a..=b]) {
                    return Some(v);
                }
            }
        }
    }
    None
}

#[derive(Clone)]
struct Job {
    kind: &'static str,
    source: String,
    title: String,
    text: String,
    context: Option<String>,
}

const WRITER: &str = "You write training examples for a small assistant that helps a game master run a tabletop role-playing world. You are careful: you use only what the source text says and never add facts of your own. Answer with JSON only, no commentary.";
const JUDGE: &str = "You are a strict fact-checker. You compare an answer with its source text. Answer with JSON only.";
const SECRET_RULE: &str = "Text written as \"(GM secret: ...)\" is secret from the players: an answer may use it, but must say that it is a GM secret.";
const NOT_IN_LORE: [&str; 4] = [
    "I can't find \"{n}\" anywhere in the lore I have, so I won't make something up. If you want it to exist, tell me what it should be and we can work it out together.",
    "\"{n}\" isn't in this world's lore or rules as far as I know. I'd rather say so than invent it. Do you want to create it?",
    "There is nothing about \"{n}\" in the lore. I can't tell you about it without inventing details; if it is new, give me the idea and I'll help flesh it out.",
    "I don't have anything on \"{n}\". It isn't in the lore I was trained on, so any answer would be invented.",
];
const TOPICS: [&str; 12] = ["designing encounters and difficulty", "naming people, places and items", "pacing a session", "handling a player who dominates the table", "improvising when players leave the plot", "describing scenes vividly", "dice odds and probability", "building a mystery with clues", "rewarding players and loot", "running combat smoothly", "making villains memorable", "recapping the last session"];

fn user_prompt(job: &Job, n: u8, world: &str) -> String {
    let head = format!("World: {world}\n\nSOURCE TEXT:\n{}\n\n", job.text);
    let shape = "{\"items\":[{\"question\":\"...\",\"answer\":\"...\"}]}";
    match job.kind {
        "t_rules" => format!("{head}Write {n} different practice items. Each question describes a concrete game situation (who does what, with which numbers) that these rules decide. Each answer gives the correct ruling according to the source text only, with the steps, in 2-6 sentences. Do not use rules that are not in the source text.\nOutput: {shape}"),
        "t_history" => format!("{head}Write {n} different question/answer pairs a game master might ask about what happened: who took part, what was decided, what followed. Answers are 2-5 sentences and use only the source text. {SECRET_RULE}\nOutput: {shape}"),
        "t_lore" => format!("{head}Write {n} different question/answer pairs a game master might ask that can be answered from this lore. Answers use only the lore above, in 2-5 sentences. {SECRET_RULE}\nOutput: {shape}"),
        "t_npc" => format!("{head}The NPC in this text is {}. Write {n} different short conversations between a player and {} (1-2 turns each). The NPC answers in character, following the speaking style given in the text, and knows only what the NPC would know. The NPC never reveals GM secrets.\nOutput: {{\"items\":[{{\"turns\":[{{\"player\":\"...\",\"npc\":\"...\"}}]}}]}}", job.title, job.title),
        "t_missing" => format!("{head}Write {n} questions a game master might ask about something that sounds like it belongs next to this text but does NOT exist in it: an invented person, place, item or rule with a made-up name. Give the invented name separately.\nOutput: {{\"items\":[{{\"question\":\"...\",\"invented_name\":\"...\"}}]}}"),
        "t_style" => format!("World: {world}\n\nSTANDING INSTRUCTION FROM THE GAME MASTER ({}):\n{}\n\nWrite {n} different questions a game master might ask the assistant, with answers that follow this instruction exactly. Do not invent facts about the world; keep answers general enough to be true in any world, in 2-5 sentences.\nOutput: {shape}", job.title, job.text),
        _ => format!("{head}Write {n} different question/answer pairs a game master might ask about this text. Each question needs understanding or combining details (why, how, what follows from this, how two things compare), not just copying one sentence. Answers are 2-6 sentences and use only the source text. {SECRET_RULE}\nOutput: {shape}"),
    }
}

fn items(v: &Value) -> Vec<Value> {
    v["items"].as_array().cloned().or_else(|| v.as_array().cloned()).unwrap_or_default()
}

fn text_of(v: &Value, k: &str) -> String {
    v[k].as_str().unwrap_or("").trim().to_string()
}

/// nd-world's NPC prompt shape (app/routers/npc_talk.py)
fn npc_system(world: &str, l: &Loaded, name: &str) -> Option<String> {
    let e = l.entities.iter().find(|e| e.name == name)?;
    let rp = super::markup::clean(&e.roleplay_raw);
    let mut s = format!("You are {} ({}) in the tabletop world \"{world}\". Speak as this character to the players.\n", e.name, e.kind);
    if !e.summary().is_empty() {
        s += &format!("Summary: {}\n", e.summary());
    }
    if !e.tags.is_empty() {
        s += &format!("Tags: {}\n", e.tags.join(", "));
    }
    if !e.custom_fields.is_empty() {
        s += "Custom fields:\n";
        for (k, v) in e.custom_fields.iter().take(12) {
            s += &format!("- {k}: {v}\n");
        }
    }
    if !rp.is_empty() {
        s += &format!("\nHow you speak and behave (standing direction from the GM \u{2014} follow it over any general instinct):\n{rp}\n");
    }
    Some(s.trim_end().to_string())
}

fn without_secrets(article: &str) -> String {
    article.split("\n\n").filter(|p| !p.contains("GM secret") && !p.contains("hidden from the players") && !p.contains("hidden from players") && !p.contains("note hidden")).collect::<Vec<_>>().join("\n\n")
}

/// `format_context_from_entities`' shape: "- [kind] name (subtype): summary" with an indented excerpt, for the entity and up to 3 related ones
fn lore_context(l: &Loaded, entity: usize) -> String {
    let e = &l.entities[entity];
    let line = |x: &super::docs::Entity, with_body: bool| {
        let sub = if x.subtype.is_empty() { String::new() } else { format!(" ({})", x.subtype) };
        let mut s = format!("- [{}] {}{}: {}", x.kind, x.name, sub, x.summary());
        let body = x.body();
        if with_body && !body.is_empty() {
            s += &format!("\n  {}", shorten(&body, 600).replace('\n', "\n  "));
        }
        s
    };
    let mut lines = vec![line(e, true)];
    let mut related: Vec<&str> = Vec::new();
    for r in &l.relations {
        let other = if r.source == e.name { &r.target } else if r.target == e.name { &r.source } else { continue };
        if !related.contains(&other.as_str()) {
            related.push(other);
        }
    }
    for name in related.into_iter().take(3) {
        if let Some(x) = l.entities.iter().find(|x| x.name == *name) {
            lines.push(line(x, false));
        }
    }
    lines.join("\n")
}

fn with_context(ctx: &str, system: &str) -> String {
    format!("Relevant world lore and notes (for accuracy only \u{2014} don't invent beyond what's here):\n{ctx}\n\n{system}")
}

fn chat(builder: &str, job: &Job, system: String, turns: Vec<(String, String)>) -> Example {
    let first = turns.first().map(|t| t.0.clone()).unwrap_or_default();
    let mut messages = vec![Message { role: "system".into(), content: system }];
    for (u, a) in turns {
        messages.push(Message { role: "user".into(), content: u });
        messages.push(Message { role: "assistant".into(), content: a });
    }
    Example { id: hash_hex(&[builder, &job.source, &first])[..12].to_string(), builder: builder.to_string(), source: job.source.clone(), source_title: job.title.clone(),
              messages, status: "keep".into(), edited: false, check: None, note: None }
}

fn make_jobs(l: &Loaded, docs: &[Doc], opt: &Options) -> Vec<Job> {
    let on = |k: &str| opt.kinds.iter().any(|x| x == k);
    let mut jobs = Vec::new();
    for d in docs {
        let entity = d.source.strip_prefix("entity:").and_then(|id| id.parse::<i64>().ok()).and_then(|id| l.entities.iter().position(|e| e.id == id));
        if d.article.chars().count() < 200 {
            continue;
        }
        let pieces: Vec<String> = chunk_article(&d.title, &d.article, 900).into_iter().take(3).collect();
        let kind: Option<&'static str> = match d.kind.as_str() {
            "rules" => Some("t_rules").filter(|k| on(k)),
            "session" => Some("t_history").filter(|k| on(k)),
            _ => Some("t_deeper").filter(|k| on(k)),
        };
        if let Some(kind) = kind {
            for piece in &pieces {
                jobs.push(Job { kind, source: d.source.clone(), title: d.title.clone(), text: piece.clone(), context: None });
            }
        }
        if let Some(ei) = entity {
            let e = &l.entities[ei];
            if on("t_lore") {
                let ctx = lore_context(l, ei);
                jobs.push(Job { kind: "t_lore", source: d.source.clone(), title: d.title.clone(), text: ctx.clone(), context: Some(ctx) });
            }
            if on("t_missing") && hash_num(&d.source) % 2 == 0 {
                jobs.push(Job { kind: "t_missing", source: d.source.clone(), title: d.title.clone(), text: shorten(&d.article, 1500), context: Some(lore_context(l, ei)) });
            }
            if on("t_npc") && e.visible && !e.roleplay_raw.trim().is_empty() {
                jobs.push(Job { kind: "t_npc", source: d.source.clone(), title: d.title.clone(), text: without_secrets(&d.article), context: None });
            }
        } else if on("t_missing") && d.kind == "rules" && hash_num(&d.source) % 2 == 0 {
            jobs.push(Job { kind: "t_missing", source: d.source.clone(), title: d.title.clone(), text: shorten(&d.article, 1500), context: None });
        }
    }
    if on("t_style") {
        for ins in &l.instructions {
            let (title, content) = (text_of(ins, "title"), super::markup::clean(ins["content"].as_str().unwrap_or("")));
            if !content.is_empty() {
                jobs.push(Job { kind: "t_style", source: format!("instruction:{}", ins["id"]), title: if title.is_empty() { "Instruction".into() } else { title }, text: content, context: None });
            }
        }
    }
    if let Some(limit) = opt.limit {
        jobs.truncate(limit);
    }
    jobs
}

// ------------------------------------------------------------------------------------------------------------------ files on disk

pub fn world_dir(db_dir: &Path, world_id: i64) -> PathBuf {
    db_dir.join("teacher").join(world_id.to_string())
}

pub fn read_examples(dir: &Path) -> Vec<Example> {
    std::fs::read_to_string(dir.join("examples.jsonl")).map(|t| t.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok().and_then(|v| Example::from_value(&v))).collect()).unwrap_or_default()
}

fn write_atomic(path: &Path, data: &[u8]) {
    if let Some(d) = path.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let tmp = path.with_extension("tmp");
    if std::fs::write(&tmp, data).is_ok() {
        let _ = std::fs::rename(tmp, path);
    }
}

/// Adds new examples; one that exists already (same id) is left alone
pub fn merge(dir: &Path, new: Vec<Example>) {
    if new.is_empty() {
        return;
    }
    let mut all = read_examples(dir);
    let have: HashSet<String> = all.iter().map(|e| e.id.clone()).collect();
    all.extend(new.into_iter().filter(|e| !have.contains(&e.id)));
    let mut out = String::new();
    for e in &all {
        if let Ok(l) = serde_json::to_string(&e.to_value()) {
            out.push_str(&l);
            out.push('\n');
        }
    }
    write_atomic(&dir.join("examples.jsonl"), out.as_bytes());
}

pub fn read_state(dir: &Path) -> Value {
    std::fs::read(dir.join("state.json")).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_else(|| json!({"status": "idle"}))
}

pub fn set_state(dir: &Path, patch: Value) {
    let mut v = read_state(dir);
    if let (Some(o), Some(p)) = (v.as_object_mut(), patch.as_object()) {
        for (k, x) in p {
            o.insert(k.clone(), x.clone());
        }
    }
    write_atomic(&dir.join("state.json"), &serde_json::to_vec(&v).unwrap_or_default());
}

/// The totals the card shows
pub fn counts(dir: &Path) -> Value {
    let ex = read_examples(dir);
    let mut by: Map<String, Value> = Map::new();
    for e in &ex {
        let n = by.get(&e.builder).and_then(|v| v.as_u64()).unwrap_or(0) + 1;
        by.insert(e.builder.clone(), json!(n));
    }
    json!({"total": ex.len(), "kept": ex.iter().filter(|e| e.status == "keep").count(), "dropped": ex.iter().filter(|e| e.status == "drop").count(), "by_kind": by})
}

/// The `teacher_examples.jsonl` member of the Training Bundle: every example, one per line (None when there are none)
pub fn bundle_member(db_dir: &Path, world_id: i64) -> Option<Vec<u8>> {
    let ex = read_examples(&world_dir(db_dir, world_id));
    if ex.is_empty() {
        return None;
    }
    let mut out = Vec::new();
    for e in &ex {
        out.extend(serde_json::to_vec(&e.to_value()).ok()?);
        out.push(b'\n');
    }
    Some(out)
}

pub fn clear(dir: &Path, forget_cache: bool) {
    let _ = std::fs::remove_file(dir.join("examples.jsonl"));
    if forget_cache {
        let _ = std::fs::remove_dir_all(dir.join("cache"));
    }
    set_state(dir, json!({"status": "idle", "text": "", "report": Value::Null}));
}

// ------------------------------------------------------------------------------------------------------------------ the model

/// What the backend's helper returns when it could not answer is text in brackets
fn failed(text: &str) -> bool {
    text.starts_with("[AI ") || text.starts_with("[empty response")
}

/// One cached call (the AI slot is held only while the model answers, so chat and other jobs go on in between)
async fn ask(b: &Backend, model: &str, dir: &Path, system: &str, user: &str, temp: f64, max_tokens: i64) -> Result<String, String> {
    let key = hash_hex(&[model, system, user, &format!("{temp}")]);
    let file = dir.join("cache").join(format!("{key}.txt"));
    if let Ok(t) = std::fs::read_to_string(&file) {
        return Ok(t);
    }
    let mut extra = Map::new();
    extra.insert("temperature".into(), json!(temp));
    extra.insert("num_predict".into(), json!(max_tokens));
    let mut last = String::new();
    for attempt in 0..3 {
        let slot = crate::jobs::slot("teacher examples").await;
        let text = crate::ai::generate_chat(b, model, system, &[json!({"role": "user", "content": user})], false, &extra).await;
        drop(slot);
        if !failed(&text) && !text.trim().is_empty() {
            let text = strip_thinking(&text);
            write_atomic(&file, text.as_bytes());
            return Ok(text);
        }
        last = text;
        tokio::time::sleep(std::time::Duration::from_millis(1500 * (1 << attempt))).await;
    }
    Err(last)
}

#[derive(Default)]
struct Report {
    jobs: usize,
    jobs_done: usize,
    generated: usize,
    added: usize,
    supported: usize,
    partly: usize,
    unsupported: usize,
    unchecked: usize,
    rejected: usize,
    failed_calls: usize,
    first_error: Option<String>,
    stopped: bool,
}

async fn judge(b: &Backend, model: &str, dir: &Path, ex: &Example, source: &str) -> Result<(String, String), String> {
    let q = ex.messages.iter().rev().find(|m| m.role == "user").map(|m| m.content.clone()).unwrap_or_default();
    let a = ex.messages.last().map(|m| m.content.clone()).unwrap_or_default();
    let npc = if ex.builder == "t_npc" { "The answer is spoken in character; judge only the facts it states, not the style.\n" } else { "" };
    let user = format!("SOURCE:\n{source}\n\nQUESTION:\n{q}\n\nANSWER:\n{a}\n\n{npc}Is every factual claim in the ANSWER supported by the SOURCE (or a plain consequence of it)? \"supported\": yes; \"partly\": mostly, but one detail is not in the source; \"unsupported\": it contradicts the source or invents facts. Do not reward answers that sound good but add facts.\nOutput: {{\"verdict\":\"supported\"|\"partly\"|\"unsupported\",\"reason\":\"one short sentence\"}}");
    let raw = ask(b, model, dir, JUDGE, &user, 0.0, 400).await?;
    let v = extract_json(&raw).ok_or("The checker's answer was not readable.")?;
    let verdict = v["verdict"].as_str().unwrap_or("").to_lowercase();
    if !["supported", "partly", "unsupported"].contains(&verdict.as_str()) {
        return Err("The checker's verdict was not one of supported / partly / unsupported.".into());
    }
    Ok((verdict, text_of(&v, "reason")))
}

async fn candidates(b: &Backend, model: &str, dir: &Path, l: &Loaded, corpus: &str, job: &Job, opt: &Options, rep: &mut Report) -> Result<Vec<(Example, Option<String>)>, String> {
    let world = &l.world_name;
    let n = opt.per_source.clamp(1, 4);
    let raw = ask(b, model, dir, WRITER, &user_prompt(job, n, world), 0.7, 3000).await?;
    let Some(v) = extract_json(&raw) else {
        rep.rejected += 1;
        return Ok(vec![]);
    };
    let mut out = Vec::new();
    for it in items(&v).into_iter().take(n as usize + 1) {
        match job.kind {
            "t_npc" => {
                let turns: Vec<(String, String)> = it["turns"].as_array().cloned().unwrap_or_default().iter().map(|x| (text_of(x, "player"), text_of(x, "npc"))).filter(|(a, b)| !a.is_empty() && !b.is_empty()).take(2).collect();
                let Some(sys) = npc_system(world, l, &job.title) else { continue };
                if turns.is_empty() {
                    rep.rejected += 1;
                    continue;
                }
                out.push((chat("t_npc", job, sys, turns), Some(job.text.clone())));
            }
            "t_missing" => {
                let (q, name) = (text_of(&it, "question"), text_of(&it, "invented_name"));
                // the invented thing must really be absent from the whole world
                if q.is_empty() || name.chars().count() < 3 || corpus.contains(&name.to_lowercase()) {
                    rep.rejected += 1;
                    continue;
                }
                let answer = NOT_IN_LORE[hash_num(&format!("{}{q}", job.source)) % NOT_IN_LORE.len()].replace("{n}", &name);
                let base = system_prompt(world);
                let with_ctx = hash_num(&q) % 2 == 0 && job.context.is_some();
                let system = if with_ctx { with_context(job.context.as_deref().unwrap_or(""), &base) } else { base };
                out.push((chat("t_missing", job, system, vec![(q, answer)]), None));
            }
            _ => {
                let (q, a) = (text_of(&it, "question"), text_of(&it, "answer"));
                if q.is_empty() || a.is_empty() || q.chars().count() > 500 || a.chars().count() > 3500 {
                    rep.rejected += 1;
                    continue;
                }
                let base = system_prompt(world);
                let system = if job.kind == "t_lore" { with_context(job.context.as_deref().unwrap_or(""), &base) } else { base };
                let check = if job.kind == "t_style" { None } else if job.kind == "t_lore" { job.context.clone() } else { Some(job.text.clone()) };
                out.push((chat(job.kind, job, system, vec![(q, a)]), check));
            }
        }
    }
    Ok(out)
}

impl Report {
    fn to_value(&self) -> Value {
        json!({"jobs": self.jobs, "jobs_done": self.jobs_done, "generated": self.generated, "added": self.added, "supported": self.supported, "partly": self.partly,
               "unsupported": self.unsupported, "unchecked": self.unchecked, "rejected": self.rejected, "failed_calls": self.failed_calls, "first_error": self.first_error, "stopped": self.stopped})
    }
}

fn publish(dir: &Path, rep: &Report, text: &str, status: &str) {
    set_state(dir, json!({"status": status, "text": text, "report": rep.to_value(), "updated": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)}));
}

/// Runs the whole pipeline for one world. State and results are files under `dir`; `cancel` stops it between two calls.
pub async fn run(dir: PathBuf, members: Vec<(&'static str, Vec<u8>)>, b: Backend, model: String, opt: Options, cancel: Arc<AtomicBool>) {
    let l = match load(&Members(&members)) {
        Ok(l) => l,
        Err(e) => {
            set_state(&dir, json!({"status": "error", "text": format!("The world could not be read: {e}")}));
            return;
        }
    };
    let docs = articles(&l);
    let corpus = docs.iter().map(|d| d.article.to_lowercase()).collect::<Vec<_>>().join("\n");
    let jobs = make_jobs(&l, &docs, &opt);
    let mut rep = Report { jobs: jobs.len(), ..Default::default() };
    set_state(&dir, json!({"status": "running", "model": model, "started": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0), "options": opt.to_value()}));
    let existing: HashSet<String> = read_examples(&dir).into_iter().map(|e| e.id).collect();
    let mut fresh: Vec<Example> = Vec::new();
    let mut consecutive_failures = 0;
    for (i, job) in jobs.iter().enumerate() {
        if cancel.load(Ordering::SeqCst) {
            rep.stopped = true;
            break;
        }
        publish(&dir, &rep, &format!("{}: {}", KINDS.iter().find(|k| k.0 == job.kind).map(|k| k.1).unwrap_or(job.kind), job.title), "running");
        match candidates(&b, &model, &dir, &l, &corpus, job, &opt, &mut rep).await {
            Ok(cands) => {
                consecutive_failures = 0;
                for (mut ex, src) in cands {
                    rep.generated += 1;
                    if existing.contains(&ex.id) {
                        continue;
                    }
                    match (opt.judge, src) {
                        (true, Some(src)) => match judge(&b, &model, &dir, &ex, &src).await {
                            Ok((verdict, reason)) => {
                                match verdict.as_str() { "supported" => rep.supported += 1, "partly" => rep.partly += 1, _ => rep.unsupported += 1 }
                                if verdict == "unsupported" || (verdict == "partly" && opt.drop_partly) {
                                    ex.status = "drop".into();
                                }
                                ex.check = Some(verdict);
                                ex.note = Some(reason).filter(|r| !r.is_empty());
                            }
                            Err(e) => {
                                rep.failed_calls += 1;
                                rep.first_error.get_or_insert(e);
                                rep.unchecked += 1;
                                ex.check = Some("unchecked".into());
                                ex.note = Some("The checker could not check this one.".into());
                            }
                        },
                        _ => {
                            rep.unchecked += 1;
                            ex.check = Some("unchecked".into());
                        }
                    }
                    rep.added += 1;
                    fresh.push(ex);
                }
            }
            Err(e) => {
                rep.failed_calls += 1;
                consecutive_failures += 1;
                rep.first_error.get_or_insert(e.clone());
                if consecutive_failures >= 3 {
                    merge(&dir, std::mem::take(&mut fresh));
                    publish(&dir, &rep, &format!("The model keeps failing, so the run stopped (what was made so far is saved). {e}"), "error");
                    return;
                }
            }
        }
        rep.jobs_done = i + 1;
        if i % 5 == 4 {
            merge(&dir, std::mem::take(&mut fresh));
        }
    }
    // a little general chat, as a share of everything made
    if opt.kinds.iter().any(|k| k == "t_general") && opt.general_percent > 0 && !rep.stopped {
        let all = read_examples(&dir);
        let have = all.iter().filter(|e| e.builder != "t_general").count() + fresh.len();
        let pct = opt.general_percent.min(30) as usize;
        let want = ((have * pct).div_ceil(100 - pct)).min(200);
        let mut made = all.iter().filter(|e| e.builder == "t_general").count();
        let mut round = 0;
        while made < want && round < 60 && !cancel.load(Ordering::SeqCst) {
            let topic = TOPICS[round % TOPICS.len()];
            round += 1;
            let job = Job { kind: "t_general", source: format!("general:{topic}"), title: topic.to_string(), text: String::new(), context: None };
            let prompt = format!("Topic: {topic} (variation {})\n\nWrite 5 different short question/answer pairs of general help for a tabletop role-playing game master on this topic. They must not depend on any particular world, character or rules system. Answers are 2-5 sentences, practical and friendly.\nOutput: {{\"items\":[{{\"question\":\"...\",\"answer\":\"...\"}}]}}", round / TOPICS.len() + 1);
            publish(&dir, &rep, &format!("General chat: {topic}"), "running");
            let Ok(raw) = ask(&b, &model, &dir, WRITER, &prompt, 0.8, 2500).await else { rep.failed_calls += 1; continue };
            let Some(v) = extract_json(&raw) else { rep.rejected += 1; continue };
            for it in items(&v) {
                let (q, a) = (text_of(&it, "question"), text_of(&it, "answer"));
                if q.is_empty() || a.is_empty() || made >= want {
                    continue;
                }
                let mut ex = chat("t_general", &job, system_prompt(&l.world_name), vec![(q, a)]);
                rep.generated += 1;
                if existing.contains(&ex.id) || fresh.iter().any(|f| f.id == ex.id) {
                    continue;
                }
                ex.check = Some("unchecked".into());
                rep.unchecked += 1;
                rep.added += 1;
                made += 1;
                fresh.push(ex);
            }
        }
    }
    merge(&dir, fresh);
    let status = if rep.stopped { "stopped" } else { "done" };
    publish(&dir, &rep, if rep.stopped { "Stopped" } else { "Finished" }, status);
}
