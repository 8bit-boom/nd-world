//! A world, loaded from a bundle, as readable articles (the "world book") and the structured facts the example builders use.
use std::collections::HashMap;

use serde_json::Value;

use super::markup::clean;
/// The members of a training bundle as `training_members` makes them: (file name, bytes).
pub struct Members<'a>(pub &'a [(&'static str, Vec<u8>)]);

type Result<T> = std::result::Result<T, String>;

impl Members<'_> {
    fn get(&self, name: &str) -> Option<&[u8]> {
        self.0.iter().find(|(n, _)| *n == name).map(|(_, b)| b.as_slice())
    }
    /// One JSON value per line; an absent member is an empty list
    fn records(&self, name: &str) -> Result<Vec<Value>> {
        let Some(b) = self.get(name) else { return Ok(Vec::new()) };
        String::from_utf8_lossy(b).lines().filter(|l| !l.trim().is_empty()).map(|l| serde_json::from_str(l).map_err(|e| format!("{name}: {e}"))).collect()
    }
    fn read_text(&self, name: &str) -> Result<String> {
        Ok(self.get(name).map(|b| String::from_utf8_lossy(b).to_string()).unwrap_or_default())
    }
    fn world(&self) -> Value {
        self.get("world.json").and_then(|b| serde_json::from_slice(b).ok()).unwrap_or(Value::Null)
    }
}

#[derive(Debug, Clone)]
pub struct Entity {
    pub id: i64,
    pub kind: String,
    pub subtype: String,
    pub name: String,
    pub aliases: Vec<String>,
    pub tags: Vec<String>,
    pub summary_raw: String,
    pub body_raw: String,
    pub roleplay_raw: String,
    pub visible: bool,
    pub custom_fields: Vec<(String, String)>,
    /// (text as written, visible to players)
    pub notes: Vec<(String, bool)>,
}

#[derive(Debug, Clone)]
pub struct Relation {
    pub source: String,
    pub target: String,
    pub relation: String,
}

#[derive(Debug, Clone, Default)]
pub struct Loaded {
    pub world_name: String,
    pub entities: Vec<Entity>,
    pub relations: Vec<Relation>,
    pub rules: String,
    pub sheet_rules: Vec<(String, String)>,
    pub sessions: Vec<Value>,
    pub facts: Vec<Value>,
    pub quests: Vec<Value>,
    pub tables: Vec<Value>,
    pub characters: Vec<Value>,
    pub instructions: Vec<Value>,
}

/// One piece of the world book.
#[derive(Debug, Clone)]
pub struct Doc {
    /// stable key of what it came from: "entity:12", "rules:How combat works", "session:3" ...
    pub source: String,
    pub kind: String,
    pub title: String,
    pub article: String,
}

fn s(v: &Value, k: &str) -> String {
    v[k].as_str().unwrap_or("").trim().to_string()
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(t) => !t.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn str_list(v: &Value) -> Vec<String> {
    v.as_array().map(|a| a.iter().filter_map(|x| x.as_str()).map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()).unwrap_or_default()
}

/// {template slug: {field id: (label, item field labels)}}
type Labels = HashMap<String, HashMap<String, (String, HashMap<String, String>)>>;

fn template_labels(world: &Value) -> Labels {
    let mut out = Labels::new();
    for t in world["templates"].as_array().cloned().unwrap_or_default() {
        let mut fields = HashMap::new();
        for f in t["fields"].as_array().cloned().unwrap_or_default() {
            let items: HashMap<String, String> = f["item_fields"].as_array().cloned().unwrap_or_default().iter()
                .map(|i| (s(i, "id"), if s(i, "label").is_empty() { s(i, "id") } else { s(i, "label") })).collect();
            let label = if s(&f, "label").is_empty() { s(&f, "id") } else { s(&f, "label") };
            fields.insert(s(&f, "id"), (label, items));
        }
        out.insert(s(&t, "slug"), fields);
    }
    out
}

fn scalar(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(t) => Some(clean(t)).filter(|t| !t.is_empty()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(if *b { "Yes" } else { "No" }.to_string()),
        _ => None,
    }
}

/// A custom field's value as one line of text (None when empty).
fn format_value(v: &Value, items: Option<&HashMap<String, String>>) -> Option<String> {
    match v {
        Value::Array(a) => {
            let parts: Vec<String> = a.iter().filter_map(|x| match x {
                Value::Object(o) => {
                    let kv: Vec<String> = o.iter().filter_map(|(k, val)| {
                        let t = scalar(val)?;
                        Some(format!("{}: {t}", items.and_then(|m| m.get(k)).cloned().unwrap_or_else(|| k.clone())))
                    }).collect();
                    Some(kv.join(", ")).filter(|t| !t.is_empty())
                }
                other => scalar(other),
            }).collect();
            Some(parts.join("; ")).filter(|t| !t.is_empty())
        }
        Value::Object(o) => {
            let kv: Vec<String> = o.iter().filter_map(|(k, val)| Some(format!("{k}: {}", scalar(val)?))).collect();
            Some(kv.join(", ")).filter(|t| !t.is_empty())
        }
        other => scalar(other),
    }
}

pub fn load(bundle: &Members) -> Result<Loaded> {
    let labels = template_labels(&bundle.world());
    let notes = bundle.records("entity_notes.jsonl")?;
    let mut entities = Vec::new();
    for e in bundle.records("entities.jsonl")? {
        let id = e["id"].as_i64().unwrap_or(0);
        let tpl = e["template"].as_str().and_then(|t| labels.get(t));
        let mut custom = Vec::new();
        if let Some(map) = e["custom_fields"].as_object() {
            for (fid, val) in map {
                let (label, items) = tpl.and_then(|t| t.get(fid)).map(|(l, i)| (l.clone(), Some(i))).unwrap_or((fid.clone(), None));
                if let Some(text) = format_value(val, items) {
                    custom.push((label, text));
                }
            }
        }
        entities.push(Entity {
            id, kind: s(&e, "kind"), subtype: s(&e, "subtype"), name: s(&e, "name"), aliases: str_list(&e["aliases"]), tags: str_list(&e["tags"]),
            summary_raw: s(&e, "summary"), body_raw: s(&e, "body"), roleplay_raw: s(&e, "roleplay_personality"),
            visible: truthy(&e["visible_to_players"]), custom_fields: custom,
            notes: notes.iter().filter(|n| n["entity_id"].as_i64() == Some(id)).map(|n| (s(n, "content"), truthy(&n["visible_to_players"]))).filter(|(c, _)| !c.is_empty()).collect(),
        });
    }
    let relations = bundle.records("relations.jsonl")?.iter().map(|r| Relation { source: s(r, "source_name"), target: s(r, "target_name"), relation: s(r, "relation") }).collect();
    Ok(Loaded {
        world_name: bundle.world()["name"].as_str().unwrap_or("").to_string(),
        entities,
        relations,
        rules: bundle.read_text("rules.md")?,
        sheet_rules: bundle.records("rules_sheets.jsonl")?.iter().map(|r| (s(r, "template"), s(r, "rules_md"))).collect(),
        sessions: bundle.records("sessions.jsonl")?,
        facts: bundle.records("facts.jsonl")?,
        quests: bundle.records("quests.jsonl")?,
        tables: bundle.records("tables.jsonl")?,
        characters: bundle.records("characters.jsonl")?,
        instructions: bundle.records("ai_instructions.jsonl")?,
    })
}

impl Entity {
    pub fn summary(&self) -> String {
        clean(&self.summary_raw)
    }
    pub fn body(&self) -> String {
        clean(&self.body_raw)
    }
    /// "a character (player character)"
    pub fn what(&self) -> String {
        if self.subtype.is_empty() { format!("a {}", self.kind) } else { format!("a {} ({})", self.kind, self.subtype) }
    }
}

/// "located_in" -> "located in"
pub fn humanize(rel: &str) -> String {
    rel.replace('_', " ").trim().to_string()
}

/// "A is located in B" / "A owns B"
pub fn relation_sentence(a: &str, rel: &str, b: &str) -> String {
    let r = humanize(rel);
    if r.is_empty() || r == "related" {
        return format!("{a} is related to {b}.");
    }
    let prepositional = ["of", "in", "to", "by", "with", "at", "for", "from", "on"].iter().any(|p| r.ends_with(&format!(" {p}")));
    if prepositional { format!("{a} is {r} {b}.") } else { format!("{a} {r} {b}.") }
}

/// Sections of a rules text: (heading, body), split at markdown headings of level 1-3. Text before the first heading is "Rules".
pub fn rule_sections(md: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, Vec<String>)> = vec![("Rules".to_string(), Vec::new())];
    let mut fence = false;
    for line in md.lines() {
        if line.trim_start().starts_with("```") {
            fence = !fence;
        }
        let hashes = line.chars().take_while(|c| *c == '#').count();
        if !fence && (1..=3).contains(&hashes) && line[hashes..].starts_with(' ') {
            out.push((line[hashes..].trim().to_string(), Vec::new()));
        } else if let Some(last) = out.last_mut() {
            last.1.push(line.to_string());
        }
    }
    out.into_iter().map(|(h, b)| (h, b.join("\n").trim().to_string())).filter(|(_, b)| !b.is_empty()).collect()
}

fn names_by_id(l: &Loaded) -> HashMap<i64, String> {
    l.entities.iter().map(|e| (e.id, e.name.clone())).collect()
}

/// The world book: one article per entity, rules section, session, quest, table and character.
pub fn articles(l: &Loaded) -> Vec<Doc> {
    let mut docs = Vec::new();
    let names = names_by_id(l);
    for e in &l.entities {
        let mut t = format!("# {}\n\nType: {}{}\n", e.name, e.kind, if e.subtype.is_empty() { String::new() } else { format!(" ({})", e.subtype) });
        if !e.aliases.is_empty() {
            t += &format!("Also known as: {}\n", e.aliases.join(", "));
        }
        if !e.tags.is_empty() {
            t += &format!("Tags: {}\n", e.tags.join(", "));
        }
        if !e.visible {
            t += "(This entry is hidden from the players.)\n";
        }
        let (summary, body) = (e.summary(), e.body());
        if !summary.is_empty() {
            t += &format!("\n{summary}\n");
        }
        if !body.is_empty() {
            t += &format!("\n{body}\n");
        }
        if !e.custom_fields.is_empty() {
            t += "\nDetails:\n";
            for (k, v) in &e.custom_fields {
                t += &format!("- {k}: {v}\n");
            }
        }
        let rels: Vec<String> = l.relations.iter().filter(|r| r.source == e.name).map(|r| relation_sentence(&r.source, &r.relation, &r.target)).collect();
        if !rels.is_empty() {
            t += "\nConnections:\n";
            for r in rels {
                t += &format!("- {r}\n");
            }
        }
        let rp = clean(&e.roleplay_raw);
        if !rp.is_empty() {
            t += &format!("\nHow {} speaks and behaves: {rp}\n", e.name);
        }
        if !e.notes.is_empty() {
            t += "\nNotes:\n";
            for (n, visible) in &e.notes {
                t += &format!("- {}{}\n", if *visible { "" } else { "(note hidden from players) " }, clean(n));
            }
        }
        docs.push(Doc { source: format!("entity:{}", e.id), kind: e.kind.clone(), title: e.name.clone(), article: t.trim().to_string() });
    }
    for (heading, body) in rule_sections(&l.rules) {
        docs.push(Doc { source: format!("rules:{heading}"), kind: "rules".into(), title: heading.clone(), article: format!("# {heading}\n\n{}", clean(&body)) });
    }
    for (tpl, md) in &l.sheet_rules {
        docs.push(Doc { source: format!("sheetrules:{tpl}"), kind: "rules".into(), title: format!("Rules of the {tpl} sheet"), article: format!("# Rules of the {tpl} sheet\n\n{}", clean(md)) });
    }
    for sess in &l.sessions {
        let id = sess["id"].as_i64().unwrap_or(0);
        let mut t = format!("# Session {}: {}\n", sess["session_num"], s(sess, "title"));
        if !s(sess, "session_date").is_empty() {
            t += &format!("Played on {}.\n", s(sess, "session_date"));
        }
        let summary = clean(sess["summary"].as_str().unwrap_or(""));
        if !summary.is_empty() {
            t += &format!("\n{summary}\n");
        }
        let npcs: Vec<String> = sess["npcs"].as_array().cloned().unwrap_or_default().iter().map(|n| s(n, "name")).filter(|n| !n.is_empty()).collect();
        if !npcs.is_empty() {
            t += &format!("\nCharacters met: {}\n", npcs.join(", "));
        }
        let facts: Vec<String> = l.facts.iter().filter(|f| f["game_session_id"].as_i64() == Some(id)).map(|f| format!("- {}{}", if truthy(&f["visible_to_players"]) { "" } else { "(hidden from players) " }, clean(f["content"].as_str().unwrap_or("")))).collect();
        if !facts.is_empty() {
            t += &format!("\nFacts:\n{}\n", facts.join("\n"));
        }
        docs.push(Doc { source: format!("session:{id}"), kind: "session".into(), title: format!("Session {}", sess["session_num"]), article: t.trim().to_string() });
    }
    let loose: Vec<String> = l.facts.iter().filter(|f| !l.sessions.iter().any(|s| s["id"].as_i64() == f["game_session_id"].as_i64())).map(|f| format!("- {}{}", if truthy(&f["visible_to_players"]) { "" } else { "(hidden from players) " }, clean(f["content"].as_str().unwrap_or("")))).collect();
    if !loose.is_empty() {
        docs.push(Doc { source: "facts:loose".into(), kind: "session".into(), title: "Facts".into(), article: format!("# Facts\n\n{}", loose.join("\n")) });
    }
    for q in &l.quests {
        let mut t = format!("# Quest: {}\n\nStatus: {}. Category: {}.\n", s(q, "title"), s(q, "status"), s(q, "category"));
        for k in ["summary", "body"] {
            let c = clean(q[k].as_str().unwrap_or(""));
            if !c.is_empty() {
                t += &format!("\n{c}\n");
            }
        }
        let linked: Vec<String> = q["linked_entities"].as_array().cloned().unwrap_or_default().iter().filter_map(|x| names.get(&x["entity_id"].as_i64()?).cloned()).collect();
        if !linked.is_empty() {
            t += &format!("\nInvolves: {}\n", linked.join(", "));
        }
        docs.push(Doc { source: format!("quest:{}", q["id"]), kind: "quest".into(), title: s(q, "title"), article: t.trim().to_string() });
    }
    for tb in &l.tables {
        let entries: Vec<String> = tb["entries"].as_array().cloned().unwrap_or_default().iter().map(|e| {
            let w = e["weight"].as_f64().filter(|w| *w != 1.0).map(|w| format!(" (weight {w})")).unwrap_or_default();
            format!("- {}{w}", s(e, "label"))
        }).collect();
        let mut t = format!("# Random table: {}\n", s(tb, "name"));
        if !s(tb, "category").is_empty() {
            t += &format!("Category: {}\n", s(tb, "category"));
        }
        if !s(tb, "description").is_empty() {
            t += &format!("\n{}\n", clean(&s(tb, "description")));
        }
        t += &format!("\nEntries:\n{}", entries.join("\n"));
        docs.push(Doc { source: format!("table:{}", tb["id"]), kind: "table".into(), title: s(tb, "name"), article: t.trim().to_string() });
    }
    for c in &l.characters {
        let mut t = format!("# Player character: {}\n\n", s(c, "name"));
        let bits: Vec<String> = [("Race", s(c, "race")), ("Class", s(c, "char_class")), ("Level", c["level"].as_i64().map(|n| n.to_string()).unwrap_or_default())]
            .into_iter().filter(|(_, v)| !v.is_empty()).map(|(k, v)| format!("{k}: {v}")).collect();
        t += &format!("{}\n", bits.join(". "));
        for (k, key) in [("Backstory", "backstory"), ("Notes", "notes")] {
            let v = clean(c[key].as_str().unwrap_or(""));
            if !v.is_empty() {
                t += &format!("\n{k}: {v}\n");
            }
        }
        let eq = str_list(&c["equipment"]);
        if !eq.is_empty() {
            t += &format!("\nEquipment: {}\n", eq.join(", "));
        }
        docs.push(Doc { source: format!("character:{}", c["id"]), kind: "character".into(), title: s(c, "name"), article: t.trim().to_string() });
    }
    docs
}
