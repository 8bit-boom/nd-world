//! The example builders that need no AI model: the world book (text) and the "simple facts" question/answer examples made from the
//! world's structured data. Everything is deterministic: the same world and recipe give the same examples, with stable ids.
use std::collections::{BTreeMap, HashSet};

use sha2::{Digest, Sha256};

use super::chunk::chunk_article;
use super::docs::{articles, humanize, relation_sentence, rule_sections, Loaded};
use super::markup::{clean, secrets};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Keep,
    Drop,
}

#[derive(Debug, Clone)]
pub struct Example {
    pub id: String,
    pub builder: String,
    /// what it came from ("entity:12", "rules:Combat", "kind:location" ...)
    pub source: String,
    pub source_title: String,
    /// the world book: plain text to read (no messages)
    pub text: Option<String>,
    /// a practice conversation: system, user, assistant
    pub messages: Vec<Message>,
    pub status: Status,
    pub edited: bool,
    /// teacher examples: what the checking pass said ("supported", "partly", "unsupported", "unchecked")
    pub check: Option<String>,
    /// teacher examples: the checker's reason
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Recipe {
    pub world_book: bool,
    /// which simple-fact builders run (ids from `BUILDERS`)
    pub builders: Vec<String>,
    /// how many different wordings of each question (1-3)
    pub phrasings: u8,
    /// share of sources whose questions are kept for testing, in percent (0-30)
    pub test_percent: u8,
    pub seed: u64,
    pub max_chunk_tokens: usize,
}

/// (id, plain-language name for the page)
pub const BUILDERS: [(&str, &str); 10] = [
    ("identity", "Who or what is it? (summary and description of every entry)"),
    ("aliases", "Other names"),
    ("relations", "How things are connected"),
    ("fields", "Details such as rank, status, stats"),
    ("lists", "Lists of entries by kind"),
    ("tables", "Random tables"),
    ("history", "Sessions and quests"),
    ("rules", "Rules sections"),
    ("characters", "Player characters"),
    ("secrets", "GM secrets and hidden entries"),
];

impl Default for Recipe {
    fn default() -> Self {
        Recipe { world_book: true, builders: BUILDERS.iter().map(|(id, _)| id.to_string()).collect(), phrasings: 2, test_percent: 10, seed: 1, max_chunk_tokens: 700 }
    }
}

pub(crate) fn hash_hex(parts: &[&str]) -> String {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p.as_bytes());
        h.update([0u8]);
    }
    format!("{:x}", h.finalize())
}

pub(crate) fn hash_num(s: &str) -> usize {
    let d = Sha256::digest(s.as_bytes());
    usize::from_be_bytes([d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]])
}

fn fill(t: &str, vars: &[(&str, &str)]) -> String {
    vars.iter().fold(t.to_string(), |acc, (k, v)| acc.replace(&format!("{{{k}}}"), v))
}

/// Up to `n` of the templates, starting at a position that depends on `key`, so different entries use different wordings.
fn pick<'a>(templates: &[&'a str], key: &str, n: usize) -> Vec<&'a str> {
    let start = hash_num(key);
    (0..n.clamp(1, templates.len())).map(|i| templates[(start + i) % templates.len()]).collect()
}

/// At most `max` characters, cut at a paragraph or sentence end when possible.
pub(crate) fn shorten(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let cut: String = text.chars().take(max).collect();
    let end = cut.rfind("\n\n").or_else(|| cut.rfind(". ").map(|i| i + 1)).unwrap_or(cut.len());
    let kept = cut[..end].trim_end();
    if kept.chars().count() < max / 3 { cut.trim_end().to_string() } else { kept.to_string() }
}

/// The system prompt every practice conversation starts with (the teacher examples use it too).
pub fn system_prompt(world: &str) -> String {
    format!("You are the game master's assistant for the tabletop world \"{world}\". Answer from the world's lore and rules. Text marked \"GM secret\" is not known to the players. If something is not in the lore, say so instead of inventing it.")
}

struct Maker<'a> {
    world: &'a str,
    out: Vec<Example>,
    seen: HashSet<String>,
}

impl Maker<'_> {
    fn system(&self) -> String {
        system_prompt(self.world)
    }

    fn qa(&mut self, builder: &str, source: &str, title: &str, question: String, answer: String) {
        if answer.trim().is_empty() {
            return;
        }
        let id = hash_hex(&[builder, source, &question])[..12].to_string();
        if !self.seen.insert(id.clone()) {
            return;
        }
        let sys = self.system();
        self.out.push(Example {
            id, builder: builder.to_string(), source: source.to_string(), source_title: title.to_string(), text: None,
            messages: vec![
                Message { role: "system".into(), content: sys },
                Message { role: "user".into(), content: question },
                Message { role: "assistant".into(), content: answer.trim().to_string() },
            ],
            status: Status::Keep, edited: false, check: None, note: None,
        });
    }
}

const IDENTITY: [&str; 4] = ["Who or what is {n}?", "Tell me about {n}.", "What do we know about {n}?", "Describe {n}."];
const ALIASES: [&str; 3] = ["What else is {n} called?", "Does {n} go by any other names?", "What are the other names of {n}?"];
const REL_PAIR: [&str; 3] = ["How is {a} connected to {b}?", "What is the relationship between {a} and {b}?", "What links {a} and {b}?"];
const REL_LIST: [&str; 3] = ["What is {n} connected to?", "List the connections of {n}.", "Which other entries is {n} linked to?"];
const FIELD: [&str; 3] = ["What is {n}'s {f}?", "What is the {f} of {n}?", "Tell me the {f} of {n}."];
const KIND_LIST: [&str; 3] = ["Which {k} entries exist in {w}?", "List the {k} entries of {w}.", "What {k} entries does {w} have?"];
const TABLE: [&str; 3] = ["What does the random table \"{n}\" contain?", "List the results of the \"{n}\" table.", "What can the \"{n}\" table produce?"];
const SESSION: [&str; 3] = ["What happened in session {n}?", "Summarize session {n}.", "Give me the recap of session {n} ({t})."];
const QUEST: [&str; 3] = ["What is the quest \"{n}\" about?", "Explain the quest \"{n}\".", "Tell me about the quest \"{n}\"."];
const RULE: [&str; 3] = ["How do the rules handle {n}?", "Explain the rule: {n}.", "What do the rules say about {n}?"];
const CHARACTER: [&str; 3] = ["Who is the player character {n}?", "Tell me about {n}, the player character.", "What is {n}'s background?"];
const SECRETS: [&str; 3] = ["What are the GM secrets about {n}?", "What do the players not know about {n}?", "Which secrets surround {n}?"];

/// The world book (as `text` examples) and the question/answer examples of the chosen builders.
pub fn generate(l: &Loaded, recipe: &Recipe) -> Vec<Example> {
    let mut m = Maker { world: &l.world_name, out: Vec::new(), seen: HashSet::new() };
    let n = recipe.phrasings.clamp(1, 3) as usize;
    let on = |id: &str| recipe.builders.iter().any(|b| b == id);

    if recipe.world_book {
        for d in articles(l) {
            for (i, piece) in chunk_article(&d.title, &d.article, recipe.max_chunk_tokens).into_iter().enumerate() {
                let id = hash_hex(&["world_book", &d.source, &i.to_string(), &piece])[..12].to_string();
                m.out.push(Example { id, builder: "world_book".into(), source: d.source.clone(), source_title: d.title.clone(), text: Some(piece), messages: vec![], status: Status::Keep, edited: false, check: None, note: None });
            }
        }
    }

    for e in &l.entities {
        let src = format!("entity:{}", e.id);
        let (summary, body) = (e.summary(), e.body());
        if on("identity") && (!summary.is_empty() || !body.is_empty()) {
            let mut a = format!("{} is {}.", e.name, e.what());
            if !summary.is_empty() {
                a += &format!(" {summary}");
            }
            if !body.is_empty() {
                a += &format!("\n\n{}", shorten(&body, 1500));
            }
            for q in pick(&IDENTITY, &src, n) {
                m.qa("identity", &src, &e.name, fill(q, &[("n", &e.name)]), a.clone());
            }
        }
        if on("aliases") && !e.aliases.is_empty() {
            let a = format!("{} is also known as {}.", e.name, e.aliases.join(", "));
            for q in pick(&ALIASES, &src, n) {
                m.qa("aliases", &src, &e.name, fill(q, &[("n", &e.name)]), a.clone());
            }
        }
        if on("fields") {
            for (label, value) in &e.custom_fields {
                let a = format!("{}'s {} is {}.", e.name, label.to_lowercase(), value);
                for q in pick(&FIELD, &format!("{src}{label}"), n) {
                    m.qa("fields", &src, &e.name, fill(q, &[("n", &e.name), ("f", &label.to_lowercase())]), a.clone());
                }
            }
        }
        if on("relations") {
            let mine: Vec<_> = l.relations.iter().filter(|r| r.source == e.name).collect();
            for r in &mine {
                let a = relation_sentence(&r.source, &r.relation, &r.target);
                for q in pick(&REL_PAIR, &format!("{src}{}", r.target), n) {
                    m.qa("relations", &src, &e.name, fill(q, &[("a", &r.source), ("b", &r.target)]), a.clone());
                }
            }
            if mine.len() >= 2 {
                let a = mine.iter().map(|r| relation_sentence(&r.source, &r.relation, &r.target)).collect::<Vec<_>>().join("\n");
                for q in pick(&REL_LIST, &src, n) {
                    m.qa("relations", &src, &e.name, fill(q, &[("n", &e.name)]), a.clone());
                }
            }
        }
        if on("secrets") {
            let mut found = secrets(&e.body_raw);
            found.extend(secrets(&e.summary_raw));
            found.extend(secrets(&e.roleplay_raw));
            for (note, visible) in &e.notes {
                if !visible {
                    found.push(clean(note));
                } else {
                    found.extend(secrets(note));
                }
            }
            found.dedup();
            if !found.is_empty() {
                let a = format!("Secrets about {}:\n{}", e.name, found.iter().map(|s| format!("- {s}")).collect::<Vec<_>>().join("\n"));
                for q in pick(&SECRETS, &src, n) {
                    m.qa("secrets", &src, &e.name, fill(q, &[("n", &e.name)]), a.clone());
                }
            }
            if !e.visible {
                let mut a = format!("No. {} is hidden from the players, so they do not know about it yet.", e.name);
                if !summary.is_empty() {
                    a += &format!(" For the GM: {summary}");
                }
                m.qa("secrets", &src, &e.name, format!("Do the players know about {}?", e.name), a);
            }
        }
    }

    if on("lists") {
        let mut by_kind: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for e in &l.entities {
            by_kind.entry(e.kind.as_str()).or_default().push(e.name.as_str());
        }
        for (kind, names) in by_kind.iter().filter(|(_, v)| v.len() >= 2) {
            let shown: Vec<String> = names.iter().take(60).map(|s| format!("- {s}")).collect();
            let more = if names.len() > 60 { format!("\n...and {} more.", names.len() - 60) } else { String::new() };
            let a = format!("{} has {} {} entries:\n{}{more}", l.world_name, names.len(), kind, shown.join("\n"));
            let src = format!("kind:{kind}");
            for q in pick(&KIND_LIST, &src, n) {
                m.qa("lists", &src, &format!("All {kind} entries"), fill(q, &[("k", kind), ("w", &l.world_name)]), a.clone());
            }
        }
    }

    if on("tables") {
        for tb in &l.tables {
            let name = tb["name"].as_str().unwrap_or("").trim();
            let entries: Vec<String> = tb["entries"].as_array().cloned().unwrap_or_default().iter().filter_map(|e| e["label"].as_str().map(|s| format!("- {}", s.trim()))).collect();
            if name.is_empty() || entries.is_empty() {
                continue;
            }
            let src = format!("table:{}", tb["id"]);
            let a = format!("The \"{name}\" table has {} results:\n{}", entries.len(), entries.join("\n"));
            for q in pick(&TABLE, &src, n) {
                m.qa("tables", &src, name, fill(q, &[("n", name)]), a.clone());
            }
        }
    }

    if on("history") {
        for s in &l.sessions {
            let docs = articles(&Loaded { sessions: vec![s.clone()], facts: l.facts.clone(), world_name: l.world_name.clone(), ..Default::default() });
            let Some(d) = docs.first() else { continue };
            let title = s["title"].as_str().unwrap_or("");
            let num = s["session_num"].to_string();
            let body = d.article.split_once('\n').map(|x| x.1.trim().to_string()).unwrap_or_default();
            let src = format!("session:{}", s["id"]);
            for q in pick(&SESSION, &src, n) {
                m.qa("history", &src, &d.title, fill(q, &[("n", &num), ("t", title)]), shorten(&body, 1800));
            }
            if let Some(date) = s["session_date"].as_str().filter(|d| !d.is_empty()) {
                m.qa("history", &src, &d.title, format!("When was session {num} played?"), format!("Session {num} (\"{title}\") was played on {date}."));
            }
        }
        for q in &l.quests {
            let title = q["title"].as_str().unwrap_or("").trim();
            let src = format!("quest:{}", q["id"]);
            let text = [q["summary"].as_str().unwrap_or(""), q["body"].as_str().unwrap_or("")].iter().map(|t| clean(t)).filter(|t| !t.is_empty()).collect::<Vec<_>>().join("\n\n");
            if !title.is_empty() && !text.is_empty() {
                for t in pick(&QUEST, &src, n) {
                    m.qa("history", &src, title, fill(t, &[("n", title)]), shorten(&text, 1500));
                }
            }
            if !title.is_empty() {
                m.qa("history", &src, title, format!("What is the status of the quest \"{title}\"?"),
                     format!("The quest \"{title}\" is {} ({} quest).", q["status"].as_str().unwrap_or("active"), q["category"].as_str().unwrap_or("main")));
            }
        }
    }

    if on("rules") {
        for (heading, body) in rule_sections(&l.rules) {
            let body = clean(&body);
            if body.chars().count() < 40 || heading == "Rules" {
                continue;
            }
            let src = format!("rules:{heading}");
            for t in pick(&RULE, &src, n) {
                m.qa("rules", &src, &heading, fill(t, &[("n", &heading)]), shorten(&body, 1800));
            }
        }
    }

    if on("characters") {
        for c in &l.characters {
            let name = c["name"].as_str().unwrap_or("").trim();
            let src = format!("character:{}", c["id"]);
            let mut a = format!("{name} is a player character");
            let race = c["race"].as_str().unwrap_or("").trim();
            let class = c["char_class"].as_str().unwrap_or("").trim();
            if !race.is_empty() || !class.is_empty() {
                a += &format!(" ({}{}{})", race, if !race.is_empty() && !class.is_empty() { " " } else { "" }, class);
            }
            if let Some(level) = c["level"].as_i64() {
                a += &format!(" of level {level}");
            }
            a.push('.');
            let back = clean(c["backstory"].as_str().unwrap_or(""));
            if !back.is_empty() {
                a += &format!("\n\n{}", shorten(&back, 1500));
            }
            if !name.is_empty() && (a.len() > name.len() + 30 || !back.is_empty()) {
                for t in pick(&CHARACTER, &src, n) {
                    m.qa("characters", &src, name, fill(t, &[("n", name)]), a.clone());
                }
            }
        }
    }
    let _ = humanize; // used by docs; kept public for the other builders
    m.out
}
