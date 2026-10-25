//! The general importer (app/routers/importer.py): the read-only detection first (`POST /api/import/detect`), the
//! executing routes to follow. Same rule as everywhere: `None` = not sure, Python answers.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Map, Value};

use crate::config::Config;

const SCHEMATIC_ELEMENT_TYPES: [&str; 11] = ["rect", "circle", "line", "arrow", "poly", "path", "text", "pin", "image", "measure", "token"];
const FIELD_TYPES: [&str; 7] = ["text", "number", "textarea", "select", "list", "resource", "table"];

/// the canonical PC field names (characters.py's own lists): the scalar columns, the friendlier aliases and the encoded ones
const PC_SCALAR_FIELDS: [&str; 21] = ["player_name", "race", "race_id", "char_class", "profession_id", "level", "xp", "backstory", "notes",
    "max_hp", "current_hp", "shock_max", "shock_current", "pp_current", "mp_current", "minor_edge", "major_edge", "minor_edge_count",
    "major_edge_count", "sheet_template_id", "portrait_url"];
const PC_JSON_ALIASES: [(&str, &str); 9] = [("stats", "stats_json"), ("skills", "skills_json"), ("currency", "currency_json"),
    ("equipment", "equipment_json"), ("feats", "feats_json"), ("attacks", "attacks_json"), ("cyberware", "cyberware_json"),
    ("conditions", "conditions_json"), ("custom_fields", "custom_fields_json")];

/// `_normkey`: the key folded to bare lowercase alphanumerics; None where Python's `str.lower()` of a non-ASCII key
/// could read differently than Rust's
fn normkey(k: &str) -> Option<String> {
    if k.chars().any(|c| !c.is_ascii()) {
        return None;
    }
    Some(k.to_lowercase().chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).collect())
}

/// the canonical PC names as normkeyed markers, once
fn pc_marker_keys() -> &'static Vec<String> {
    static ONCE: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    // _PC_MARKER_KEYS_NORM: the normkeyed spellings (set(_PC_KEY_LOOKUP))
    ONCE.get_or_init(|| {
        PC_SCALAR_FIELDS.iter().copied().chain(PC_JSON_ALIASES.iter().map(|(a, _)| *a)).chain(PC_JSON_ALIASES.iter().map(|(_, b)| *b))
            .filter_map(|f| normkey(f)).collect()
    })
}

fn is_str(v: Option<&Value>) -> bool {
    v.and_then(|v| v.as_str()).is_some()
}

/// `_looks_like_table`
fn looks_like_table(d: &Value) -> bool {
    let Some(o) = d.as_object() else { return false };
    let Some(Value::Array(entries)) = o.get("entries") else { return false };
    entries.iter().all(|e| e.as_object().is_some_and(|e| e.contains_key("label")))
}

/// `_looks_like_fields`
fn looks_like_fields(lst: &Value) -> bool {
    let Some(a) = lst.as_array() else { return false };
    !a.is_empty() && a.iter().all(|f| {
        let Some(f) = f.as_object() else { return false };
        f.contains_key("id") && f.contains_key("label") && f.get("type").and_then(|t| t.as_str()).is_some_and(|t| FIELD_TYPES.contains(&t))
    })
}

/// `_looks_like_entity`
fn looks_like_entity(d: &Value, kinds: &[String]) -> bool {
    let Some(o) = d.as_object() else { return false };
    let kind = o.get("kind").and_then(|k| k.as_str());
    let name = o.get("name").and_then(|n| n.as_str()).unwrap_or("");
    kind.is_some_and(|k| kinds.iter().any(|x| x == k)) && is_str(o.get("name")) && !name.is_empty()
}

/// `_looks_like_pc`: a name, not a recognized entity kind, and one PC-ish key somewhere
fn looks_like_pc(d: &Value, kinds: &[String]) -> Option<bool> {
    let Some(o) = d.as_object() else { return Some(false) };
    let name = o.get("name").and_then(|n| n.as_str()).unwrap_or("");
    if !is_str(o.get("name")) || name.is_empty() {
        return Some(false);
    }
    if o.get("kind").and_then(|k| k.as_str()).is_some_and(|k| kinds.iter().any(|x| x == k)) {
        return Some(false);
    }
    let markers = pc_marker_keys();
    for k in o.keys() {
        if markers.contains(&normkey(k)?) {
            return Some(true);
        }
    }
    Some(false)
}

/// `_resolve_batch_item`: the explicit {"kind","data","params"} envelope, else auto-detection
fn resolve_batch_item<'a>(item: &'a Value, kinds: &[String]) -> Option<(String, &'a Value)> {
    if let Some(o) = item.as_object() {
        if o.contains_key("kind") && o.contains_key("data") {
            return Some((o.get("kind").and_then(|k| k.as_str())?.to_string(), o.get("data")?));
        }
    }
    let kind = detect_kind(item, kinds)?.get("kind").and_then(|k| k.as_str())?.to_string();
    Some((kind, item))
}

/// `detect_kind`: the best-effort sniff. None for input whose reading the two runtimes could disagree on (non-ASCII
/// keys, json text Python parses differently).
pub(crate) fn detect_kind(data: &Value, kinds: &[String]) -> Option<Map<String, Value>> {
    // batch: {"imports": [...]}
    if let Some(o) = data.as_object() {
        if let Some(Value::Array(items)) = o.get("imports") {
            if !items.is_empty() {
                let mut item_kinds: Vec<String> = vec![];
                for it in items {
                    item_kinds.push(resolve_batch_item(it, kinds)?.0);
                }
                // Counter.most_common: by count, ties in first-encounter order
                let mut order: Vec<(String, usize)> = vec![];
                for k in &item_kinds {
                    match order.iter_mut().find(|(n, _)| n == k) {
                        Some((_, c)) => *c += 1,
                        None => order.push((k.clone(), 1)),
                    }
                }
                order.sort_by(|a, b| b.1.cmp(&a.1));
                let breakdown = order.iter().map(|(n, c)| format!("{c} {n}")).collect::<Vec<_>>().join(", ");
                return Some(serde_json::Map::from_iter([
                    ("kind".to_string(), json!("batch")),
                    ("summary".to_string(), json!(format!("{} item(s): {breakdown}", items.len()))),
                    ("count".to_string(), json!(items.len())),
                    ("needs".to_string(), json!([])),
                ]));
            }
        }
    }
    // world rules
    if let Some(o) = data.as_object() {
        if o.get("rules_md").and_then(|v| v.as_str()).is_some() {
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("world_rules")),
                ("summary".to_string(), json!("World rules document")),
                ("count".to_string(), json!(1)),
                ("needs".to_string(), json!([])),
            ]));
        }
        // map overlay
        if o.contains_key("custom_markers") || o.contains_key("custom_regions") {
            let len_of = |v: Option<&Value>| v.and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
            let (m, r) = (len_of(o.get("custom_markers")), len_of(o.get("custom_regions")));
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("map_overlay")),
                ("summary".to_string(), json!(format!("{m} map marker(s), {r} region(s)"))),
                ("count".to_string(), json!(m + r)),
                ("needs".to_string(), json!(["map_slug"])),
            ]));
        }
    }
    // schematic elements: {"elements": [...]}, or a bare list of typed dicts
    let elements: Option<&Vec<Value>> = match data {
        Value::Object(o) => match o.get("elements") {
            Some(Value::Array(a)) => Some(a),
            _ => None,
        },
        Value::Array(a) if !a.is_empty() && a.iter().all(|x| {
            x.as_object().and_then(|x| x.get("type")).and_then(|t| t.as_str()).is_some_and(|t| SCHEMATIC_ELEMENT_TYPES.contains(&t))
        }) => Some(a),
        _ => None,
    };
    if let Some(elements) = elements {
        return Some(serde_json::Map::from_iter([
            ("kind".to_string(), json!("schematic_elements")),
            ("summary".to_string(), json!(format!("{} schematic element(s)", elements.len()))),
            ("count".to_string(), json!(elements.len())),
            ("needs".to_string(), json!(["schematic_slug"])),
        ]));
    }
    // random tables: a list of them, or one
    if let Value::Array(a) = data {
        if !a.is_empty() && a.iter().all(looks_like_table) {
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("random_table")),
                ("summary".to_string(), json!(format!("{} random table(s)", a.len()))),
                ("count".to_string(), json!(a.len())),
                ("needs".to_string(), json!([])),
            ]));
        }
    }
    if looks_like_table(data) {
        let entries = data.get("entries").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
        return Some(serde_json::Map::from_iter([
            ("kind".to_string(), json!("random_table")),
            ("summary".to_string(), json!(format!("1 random table ({entries} entries)"))),
            ("count".to_string(), json!(1)),
            ("needs".to_string(), json!([])),
        ]));
    }
    // field templates: {"fields": [...]} (an optional name), or a bare list of fields
    if let Some(o) = data.as_object() {
        if o.get("fields").is_some_and(|f| looks_like_fields(f)) {
            let n = o.get("fields").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
            let name_part = o.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
                .map(|n| format!(" \u{2014} \"{n}\"")).unwrap_or_default();
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("field_template")),
                ("summary".to_string(), json!(format!("{n} field(s){name_part}"))),
                ("count".to_string(), json!(n)),
                ("needs".to_string(), json!(["template_kind", "name"])),
            ]));
        }
    }
    if looks_like_fields(data) {
        let n = data.as_array().map(|a| a.len()).unwrap_or(0);
        return Some(serde_json::Map::from_iter([
            ("kind".to_string(), json!("field_template")),
            ("summary".to_string(), json!(format!("{n} field(s)"))),
            ("count".to_string(), json!(n)),
            ("needs".to_string(), json!(["template_kind", "name"])),
        ]));
    }
    // entities: a list, or one
    if let Value::Array(a) = data {
        if !a.is_empty() && a.iter().all(|e| looks_like_entity(e, kinds)) {
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("entity_bulk")),
                ("summary".to_string(), json!(format!("{} entities", a.len()))),
                ("count".to_string(), json!(a.len())),
                ("needs".to_string(), json!([])),
            ]));
        }
    }
    if looks_like_entity(data, kinds) {
        let o = data.as_object()?;
        return Some(serde_json::Map::from_iter([
            ("kind".to_string(), json!("entity_single")),
            ("summary".to_string(), json!(format!("1 entity: \"{}\" ({})", o.get("name").and_then(|v| v.as_str()).unwrap_or(""), o.get("kind").and_then(|v| v.as_str()).unwrap_or("")))),
            ("count".to_string(), json!(1)),
            ("needs".to_string(), json!([])),
        ]));
    }
    // player characters: a list, or one
    if let Value::Array(a) = data {
        let mut all = !a.is_empty();
        for e in a {
            if looks_like_pc(e, kinds)? != true {
                all = false;
                break;
            }
        }
        if all {
            return Some(serde_json::Map::from_iter([
                ("kind".to_string(), json!("player_character_bulk")),
                ("summary".to_string(), json!(format!("{} player character(s)", a.len()))),
                ("count".to_string(), json!(a.len())),
                ("needs".to_string(), json!([])),
            ]));
        }
    }
    if looks_like_pc(data, kinds)? == true {
        return Some(serde_json::Map::from_iter([
            ("kind".to_string(), json!("player_character")),
            ("summary".to_string(), json!(format!("1 player character: \"{}\"", data.get("name").and_then(|v| v.as_str()).unwrap_or("")))),
            ("count".to_string(), json!(1)),
            ("needs".to_string(), json!([])),
        ]));
    }
    Some(serde_json::Map::from_iter([
        ("kind".to_string(), json!("unknown")),
        ("summary".to_string(), json!("Couldn't tell what this JSON is meant for — pick manually below.")),
        ("count".to_string(), json!(0)),
        ("needs".to_string(), json!(["forced_kind"])),
    ]))
}

/// `POST /api/import/detect` (app/routers/importer.py `import_detect`): the sniff of the pasted JSON. An inner text
/// json.loads refuses (or reads differently: NaN, lone surrogates, a BOM, out-of-range numbers) is Python's own
/// "invalid" answer, with its own message - declined.
pub async fn detect(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let out = tokio::task::spawn_blocking(move || detect_sync(&cfg, &uri, &headers, &body)).await.ok().flatten();
    match out {
        Some(r) => Some(r),
        None => None,
    }
}

fn detect_sync(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    // the outer body must parse for both readers alike
    if !crate::edits::plain_numbers(body) {
        return None;
    }
    let outer: Value = serde_json::from_slice(body).ok()?;
    let raw = outer.get("json_text").and_then(|v| v.as_str()).unwrap_or("");
    if raw.contains("NaN") || raw.contains("Infinity") || !crate::edits::plain_numbers(raw.as_bytes()) || raw.starts_with('\u{feff}') {
        return None; // json.loads' own reading or its own error text: Python's
    }
    let data: Value = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(_) => return None, // the "Not valid JSON: <Python's message>" summary: Python's
    };
    let (kinds_v, _, _) = nd_render::app::effective_kind_tables(p.world.as_ref())?;
    let kinds: Vec<String> = kinds_v.as_array()?.iter().filter_map(|k| k.as_str().map(str::to_string)).collect();
    let out = detect_kind(&data, &kinds)?;
    crate::edits::json_response(&Value::Object(out))
}
