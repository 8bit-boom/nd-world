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
pub(crate) const PC_SCALAR_FIELDS: [&str; 21] = ["player_name", "race", "race_id", "char_class", "profession_id", "level", "xp", "backstory", "notes",
    "max_hp", "current_hp", "shock_max", "shock_current", "pp_current", "mp_current", "minor_edge", "major_edge", "minor_edge_count",
    "major_edge_count", "sheet_template_id", "portrait_url"];
pub(crate) const PC_JSON_ALIASES: [(&str, &str); 9] = [("stats", "stats_json"), ("skills", "skills_json"), ("currency", "currency_json"),
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

/// `_PC_KEY_LOOKUP`: (normkeyed spelling -> canonical name), once
fn pc_key_lookup() -> &'static Vec<(String, String)> {
    static ONCE: std::sync::OnceLock<Vec<(String, String)>> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        PC_SCALAR_FIELDS.iter().copied().chain(PC_JSON_ALIASES.iter().map(|(a, _)| *a)).chain(PC_JSON_ALIASES.iter().map(|(_, b)| *b))
            .filter_map(|f| normkey(f).map(|nk| (nk, f.to_string()))).collect()
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
    let markers = pc_key_lookup();
    for k in o.keys() {
        let Some(nk) = normkey(k) else { return None };
        if markers.iter().any(|(nk2, _)| nk2 == &nk) {
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

/// `POST /api/import/execute` (app/routers/importer.py `import_execute`): the shared front (the world, the import edit
/// section, the outer body, the kind) and, this batch, the `world_rules`, `random_table`, `entity_single` and
/// `entity_bulk` branches; anything else declines mid-way (Python answers, transaction rolled back by the caller).
pub async fn execute(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let out = tokio::task::spawn_blocking(move || execute_sync(&cfg, &uri, &headers, &body)).await.ok().flatten();
    out
}

fn execute_sync(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    if !crate::pages::edit_level(&p, &world, "import")? {
        return Some(crate::idpages::refusal(403));
    }
    // the outer body must parse for both readers alike (a body json.loads fails on is its own 500)
    if !crate::edits::plain_numbers(body) {
        return None;
    }
    let outer: Value = serde_json::from_slice(body).ok()?;
    let o = outer.as_object()?;
    let raw = o.get("json_text").and_then(|v| v.as_str())?;
    if raw.contains("NaN") || raw.contains("Infinity") || !crate::edits::plain_numbers(raw.as_bytes()) || raw.starts_with('\u{feff}') {
        return None; // the "Not valid JSON: <Python's message>" 400: Python's
    }
    let data: Value = serde_json::from_str(raw).ok()?;
    let (kinds_v, _, _) = nd_render::app::effective_kind_tables(Some(&world))?;
    let kinds: Vec<String> = kinds_v.as_array()?.iter().filter_map(|k| k.as_str().map(str::to_string)).collect();
    let kind = match o.get("kind") {
        None | Some(Value::Null) => detect_kind(&data, &kinds)?.get("kind").and_then(|k| k.as_str())?.to_string(),
        Some(Value::String(t)) => t.clone(),
        _ => return None, // a forced kind that is not text: Python's "Unrecognized kind: {repr}": declined
    };
    let params = match o.get("params") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(m)) => m.clone(),
        _ => return None, // params.get on a non-dict: Python's own AttributeError
    };
    // one transaction, like edits::run: a decline rolls back everything
    db.execute_batch("BEGIN IMMEDIATE").ok()?;
    let answer = execute_import(&db, cfg, &p, wid, &world, &data, &kind, &params, &kinds);
    match answer {
        Some(Ok(redirect)) => {
            if db.execute_batch("COMMIT").is_ok() {
                return crate::edits::json_response(&json!({"ok": true, "redirect": redirect}));
            }
            None
        }
        Some(Err(msg)) => {
            let _ = db.execute_batch("ROLLBACK");
            Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, msg))
        }
        None => {
            let _ = db.execute_batch("ROLLBACK");
            None // a branch this port does not carry (or input it reads differently): Python's
        }
    }
}

/// `execute_import`'s branches this port carries; Some(Err) is Python's own 400 message, None declines
fn execute_import(db: &rusqlite::Connection, cfg: &Config, _p: &crate::layout::PageCtx, wid: i64, _world: &Map<String, Value>, data: &Value,
                  kind: &str, params: &Map<String, Value>, kinds: &[String]) -> Option<Result<String, String>> {
    use rusqlite::types::Value as Sql;
    match kind {
        "world_rules" => {
            let Some(rules) = data.get("rules_md").and_then(|v| v.as_str()) else {
                return Some(Err("Expected {\"rules_md\": \"...\"}".to_string()));
            };
            crate::orm::update(db, "worlds", wid, &[("rules_md", Sql::Text(rules.to_string()))])?;
            Some(Ok("/rules".to_string()))
        }
        "random_table" => {
            let rows: Vec<&Value> = match data {
                Value::Array(a) if !a.is_empty() && a.iter().all(|t| t.is_object()) => a.iter().collect(),
                v if v.is_object() => vec![v],
                _ => return Some(Err("Expected a table object {\"entries\": [...]} or an array of them".to_string())),
            };
            for t in rows {
                let t = t.as_object()?;
                let name = match t.get("name") {
                    None | Some(Value::Null) => "Imported Table".to_string(),
                    Some(Value::String(s)) if s.trim().is_empty() => "Imported Table".to_string(),
                    Some(Value::String(s)) => s.trim().to_string(),
                    Some(_) => return None, // str() of a non-text name: Python's
                };
                if !name.is_ascii() {
                    return None; // the slug's lower() beyond ASCII: Python's
                }
                let str_col = |v: Option<&Value>, default: &str| -> Option<String> {
                    match v {
                        None => Some(default.to_string()),
                        Some(Value::Null) => Some("None".to_string()),  // str(None)
                        Some(Value::String(s)) => Some(s.clone()),
                        Some(Value::Number(n)) if n.as_i64().is_some() => Some(n.as_i64().unwrap().to_string()),
                        Some(Value::Bool(b)) => Some(if *b { "True".into() } else { "False".into() }),
                        _ => None, // str() of a list / dict: Python's repr, declined
                    }
                };
                let category = str_col(t.get("category"), "general")?;
                let description = str_col(t.get("description"), "")?;
                let entries = t.get("entries").cloned().unwrap_or(Value::Array(vec![]));
                if !crate::edits::plain_numbers(nd_render::py_dumps(&entries).as_bytes()) {
                    return None;
                }
                let slug = crate::edits::table_slug_pub(db, &name)?;
                crate::orm::insert(db, "random_tables", &[
                    ("world_id", Sql::Integer(wid)),
                    ("name", Sql::Text(name)),
                    ("slug", Sql::Text(slug)),
                    ("category", Sql::Text(category)),
                    ("description", Sql::Text(description)),
                    ("is_builtin", Sql::Integer(0)),
                    ("entries_json", Sql::Text(nd_render::py_dumps(&entries))),
                ])?;
            }
            Some(Ok("/tables".to_string()))
        }
        "entity_single" | "entity_bulk" => {
            let list: Vec<&Value> = if kind == "entity_bulk" {
                let Value::Array(a) = data else {
                    return Some(Err("Expected a non-empty array of entities ({\"kind\",\"name\",...})".to_string()));
                };
                if a.is_empty() {
                    return Some(Err("Expected a non-empty array of entities ({\"kind\",\"name\",...})".to_string()));
                }
                a.iter().collect()
            } else {
                vec![data]
            };
            let mut last_id = 0i64;
            for (i, ent) in list.iter().enumerate() {
                match create_entity(db, wid, ent, kinds) {
                    Ok(id) => last_id = id,
                    Err(Some(msg)) => {
                        let label = if kind == "entity_bulk" { format!("Entity #{}: {}", i + 1, msg) } else { msg };
                        return Some(Err(label));
                    }
                    Err(None) => return None,
                }
            }
            Some(Ok(if list.len() == 1 { format!("/entity/{last_id}") } else if kind == "entity_bulk" { "/".to_string() } else { format!("/entity/{last_id}") }))
        }
        "map_overlay" => branch_map_overlay(cfg, db, wid, data, params),
        "schematic_elements" => branch_schematic_elements(db, wid, data, params),
        "field_template" => branch_field_template(db, wid, data, params),
        "player_character" | "player_character_bulk" => branch_player_characters(db, wid, data, kind),
        "batch" => None, // the per-item envelope: each item commits on its own; a later batch

        _ => Some(Err(format!("Unrecognized kind: {kind}"))),
    }
}

/// `_create_entity` (+ `_resolve_entity_template`): the row, or Err(Python's ValueError text)
fn create_entity(db: &rusqlite::Connection, wid: i64, ent: &Value, kinds: &[String]) -> Result<i64, Option<String>> {
    use rusqlite::types::Value as Sql;
    let Some(o) = ent.as_object() else {
        return Err(Some(format!("Not a valid entity — needs \"kind\" (one of {}) and a non-empty \"name\"", kinds.join(", "))));
    };
    let kind = o.get("kind").and_then(|k| k.as_str());
    let name = o.get("name").and_then(|n| n.as_str()).unwrap_or("").trim();
    let kind_ok = kind.is_some_and(|k| kinds.iter().any(|x| x == k));
    if !kind_ok || name.is_empty() {
        return Err(Some(format!("Not a valid entity — needs \"kind\" (one of {}) and a non-empty \"name\"", kinds.join(", "))));
    }
    let kind = kind.unwrap();
    // name: the row keeps it verbatim; str() beyond the ASCII this port spells: declined
    if !name.is_ascii() || !kind.is_ascii() {
        return Err(None); // beyond the ASCII this port spells: Python's
    }
    let cf = match o.get("custom_fields_json").or_else(|| o.get("custom_fields")) {
        Some(v @ Value::Object(_)) => v.clone(),
        _ => json!({}),
    };
    // _resolve_entity_template: template_id (int or digit-string) > template_slug > template (name, ilike)
    let template_id: Option<i64> = match o.get("template_id") {
        Some(Value::Number(n)) if n.as_i64().is_some() => Some(n.as_i64().unwrap()),
        Some(Value::String(s)) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => s.parse().ok(),
        _ => {
            let by_slug = |col: &str, val: &str| -> Option<i64> {
                db.query_row(&format!("SELECT id FROM entity_templates WHERE {col} = ?1 ORDER BY id LIMIT 1"), [val], |r| r.get(0)).ok()
            };
            match o.get("template_slug").and_then(|s| s.as_str()) {
                Some(slug) => by_slug("slug", slug),
                None => match o.get("template").and_then(|s| s.as_str()) {
                    // ilike: lower() LIKE lower() - ASCII here
                    Some(nm) if nm.is_ascii() => db.query_row("SELECT id FROM entity_templates WHERE (world_id IS NULL OR world_id = ?1) AND lower(name) = lower(?2) ORDER BY id LIMIT 1",
                        rusqlite::params![wid, nm], |r| r.get(0)).ok(),
                    Some(_) => return Err(None),
                    None => None,
                },
            }
        }
    };
    // the passthrough columns (`ent.get(k) or None`): Python hands ANY truthy value to SQLAlchemy, so a list / dict
    // dies at commit (its 400) - the port declines those instead of coercing
    for k in ["subtype", "folder", "tags", "image_url", "summary", "body"] {
        match o.get(k) {
            None | Some(Value::Null) | Some(Value::String(_)) => {}
            Some(Value::Bool(false)) => {}
            _ => return Err(None),
        }
    }
    let visible = crate::edits::py_truthy_pub(o.get("visible_to_players").or(Some(&json!(true))));
    crate::orm::insert(db, "entities", &[
        ("world_id", Sql::Integer(wid)),
        ("kind", Sql::Text(kind.to_string())),
        ("subtype", o.get("subtype").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("name", Sql::Text(name.to_string())),
        ("folder", o.get("folder").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("tags", o.get("tags").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("image_url", o.get("image_url").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("summary", o.get("summary").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("body", o.get("body").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map_or(Sql::Null, |s| Sql::Text(s.to_string()))),
        ("visible_to_players", Sql::Integer(visible as i64)),
        ("template_id", template_id.map_or(Sql::Null, Sql::Integer)),
        ("custom_fields_json", Sql::Text(nd_render::py_dumps(&cf))),
    ]).ok_or(None)?;
    Ok(last_id_value(db).ok_or(None)?)
}

/// last insert rowid
fn last_id_value(db: &rusqlite::Connection) -> Option<i64> {
    db.query_row("SELECT last_insert_rowid()", [], |r| r.get(0)).ok()
}

/// `_map_world_id` (app/routers/importer.py): the filesystem map's world_id (default 1); None when there is no file or
/// it does not parse
fn map_world_id(cfg: &Config, slug: &str) -> Option<i64> {
    let jf = crate::maps::maps_dir(cfg)?.join(format!("{slug}.json"));
    let text = std::fs::read_to_string(&jf).ok()?;
    let data: Value = serde_json::from_str(&text).ok()?;
    Some(data.get("world_id").and_then(|v| v.as_i64()).unwrap_or(1))
}

/// the importer's slug for a new schematic / template: lowercase, non-alphanumerics to dashes, 50 chars, uniquified
/// -2, -3, ... (fallback "schematic" / "template" as each call site passes it)
fn import_slug(db: &rusqlite::Connection, name: &str, table: &str, fallback: &str) -> Option<String> {
    if !name.is_ascii() {
        return None;
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in name.to_ascii_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    let base: String = slug.trim_matches('-').chars().take(50).collect();
    let base = if base.is_empty() { fallback.to_string() } else { base };
    let mut unique = base.clone();
    let mut i = 2;
    loop {
        let taken: bool = db.query_row(&format!("SELECT 1 FROM {table} WHERE slug = ?1"), [&unique], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
        if !taken {
            return Some(unique);
        }
        unique = format!("{base}-{i}");
        i += 1;
    }
}

/// `_normalize_pc_data` (app/routers/importer.py): the keys folded back to the canonical names, the JSON aliases
/// applied, non-string JSON fields encoded as Python's json.dumps writes them, the N&D starting spread when asked.
/// None for a row this port would read differently (a non-ASCII key, a whole number past 64 bits in an encoded field).
fn normalize_pc_data(row: &Map<String, Value>, nd_defaults: bool) -> Option<Vec<(String, Value)>> {
    let markers = pc_key_lookup();
    let mut out: Vec<(String, Value)> = vec![];
    for (k, v) in row {
        let nk = normkey(k)?;
        let name = markers.iter().find(|(nk2, _)| nk2 == &nk).map(|(_, canon)| canon.clone()).unwrap_or_else(|| k.clone());
        if !out.iter().any(|(k2, _)| k2 == &name) {
            out.push((name, v.clone()));
        }
    }
    for (alias, field) in PC_JSON_ALIASES {
        if !out.iter().any(|(k, _)| k == field) {
            if let Some(i) = out.iter().position(|(k, _)| k == alias) {
                let v = out[i].1.clone();
                out.push((field.to_string(), v));
            }
        }
    }
    for (k, v) in out.iter_mut() {
        if PC_JSON_ALIASES.iter().any(|(_, f)| f == k) && !v.is_string() {
            let text = nd_render::py_dumps(v);
            if !crate::edits::plain_numbers(text.as_bytes()) {
                return None;
            }
            *v = Value::String(text);
        }
    }
    if nd_defaults {
        // Python inserts these as json.dumps STRINGS (the same shape every form posts)
        if !out.iter().any(|(k, _)| k == "stats_json") {
            out.push(("stats_json".to_string(), Value::String(nd_render::py_dumps(&nd_default_stats()))));
        }
        if !out.iter().any(|(k, _)| k == "currency_json") {
            out.push(("currency_json".to_string(), Value::String(nd_render::py_dumps(&nd_default_currency()))));
        }
    }
    Some(out)
}

/// the N&D starting spread (app/constants.py)
pub(crate) fn nd_default_stats() -> Value {
    json!([
        {"id": "str", "label": "Strength", "abbr": "STR", "value": 3},
        {"id": "dex", "label": "Dexterity", "abbr": "DEX", "value": 3},
        {"id": "bod", "label": "Body", "abbr": "BOD", "value": 3},
        {"id": "per", "label": "Perception", "abbr": "PER", "value": 3},
        {"id": "wil", "label": "Willpower", "abbr": "WIL", "value": 2},
        {"id": "int", "label": "Intellect", "abbr": "INT", "value": 2},
        {"id": "cha", "label": "Charisma", "abbr": "CHA", "value": 2},
        {"id": "itu", "label": "Intuition", "abbr": "ITU", "value": 2},
    ])
}

pub(crate) fn nd_default_currency() -> Value {
    json!([{"label": "Creds", "abbr": "CR", "value": 0}, {"label": "Tokens", "abbr": "TK", "value": 0}])
}

/// `_resolve_sheet_template`: id / digit-string > slug > name (ilike), over the global templates and this world's.
/// Some(None) = no ref or no match; None = a ref this port would read differently.
fn resolve_sheet_template(db: &rusqlite::Connection, wid: i64, tpl_ref: Option<&Value>) -> Option<Option<String>> {
    let r = match tpl_ref {
        None | Some(Value::Null) => return Some(None),
        Some(v) => v,
    };
    if let Some(n) = r.as_i64() {
        return Some(Some(n.to_string()));
    }
    if let Some(t) = r.as_str() {
        if !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()) {
            return Some(Some(t.to_string()));
        }
        if !t.is_ascii() {
            return None; // ilike's lower() beyond ASCII: Python's
        }
        let id: Option<i64> = db.query_row(
            "SELECT id FROM sheet_templates WHERE (world_id IS NULL OR world_id = ?1) AND (lower(slug) = lower(?2) OR lower(name) = lower(?2)) ORDER BY id LIMIT 1",
            rusqlite::params![wid, t], |r| r.get(0)).ok();
        return Some(id.map(|i| i.to_string()));
    }
    None // str() of a non-text ref: Python's
}

/// `_upsert_player_character` (app/routers/importer.py): create, or update in place when one with this exact name
/// exists; the sheet-template dance (a custom template's fields ARE the character, otherwise the N&D spread is stamped).
/// Err(Some(msg)) is not used here (Python's path has no message); Err(None) = decline.
fn upsert_player_character(db: &rusqlite::Connection, wid: i64, row: &Map<String, Value>) -> Result<i64, Option<String>> {
    use rusqlite::types::Value as Sql;
    let name = row.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let existing: Option<i64> = db.query_row("SELECT id FROM player_characters WHERE world_id = ?1 AND name = ?2 ORDER BY id LIMIT 1",
        rusqlite::params![wid, name], |r| r.get(0)).ok();
    let mut normalized = normalize_pc_data(row, false).ok_or(None)?;
    let resolved = resolve_sheet_template(db, wid,
        normalized.iter().find(|(k, _)| k == "sheet_template_id").map(|(_, v)| v)).ok_or(None)?;
    if let Some(t) = &resolved {
        normalized.retain(|(k, _)| k != "sheet_template_id");
        normalized.push(("sheet_template_id".to_string(), Value::String(t.clone())));
    }
    let is_custom = match &resolved {
        Some(t) => db.query_row("SELECT sheet_mode FROM sheet_templates WHERE id = ?1", [t], |r| r.get::<_, Option<String>>(0))
            .ok().flatten().as_deref() == Some("custom"),
        None => false,
    };
    if !is_custom {
        // the second normalization with the N&D defaults, the first's sheet_template_id kept (Python's dict | {})
        let mut n2 = normalize_pc_data(row, true).ok_or(None)?;
        if let Some(st) = normalized.iter().find(|(k, _)| k == "sheet_template_id").cloned() {
            n2.retain(|(k, _)| k != "sheet_template_id");
            n2.push(st);
        }
        normalized = n2;
    }
    // the dict becomes a Form (what apply_pc_form reads): str / int / bool as Python's str() spells them, None as ""
    // (the same falsy every reader here applies); a float / list / dict is Python's - declined
    let mut pairs: Vec<(String, String)> = vec![];
    for (k, v) in &normalized {
        let text = match v {
            Value::String(t) => t.clone(),
            Value::Number(n) if n.as_i64().is_some() => n.as_i64().unwrap().to_string(),
            Value::Bool(b) => if *b { "True".into() } else { "False".into() },
            Value::Null => String::new(),
            _ => return Err(None),
        };
        pairs.push((k.clone(), text));
    }
    let form = crate::form::Form(pairs);
    let cur: Map<String, Value> = match existing {
        Some(id) => db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).map_err(|_| None)?,
        None => Map::new(),
    };
    let mut cols = crate::edits::apply_pc_form_pub(db, &form, &cur, false).ok_or(None)?;
    match existing {
        Some(id) => {
            crate::orm::update(db, "player_characters", id, &cols).ok_or(None)?;
            Ok(id)
        }
        None => {
            cols.push(("world_id", Sql::Integer(wid)));
            cols.push(("owner_user_id", Sql::Null));
            crate::orm::insert(db, "player_characters", &cols).ok_or(None)
        }
    }
}

/// the map_overlay branch: the overlay row's markers/regions extended by the import's, the merged totals capped
fn branch_map_overlay(cfg: &Config, db: &rusqlite::Connection, wid: i64, data: &Value, params: &Map<String, Value>) -> Option<Result<String, String>> {
    use rusqlite::types::Value as Sql;
    let Some(o) = data.as_object() else {
        return Some(Err("Expected an object with \"custom_markers\" and/or \"custom_regions\"".to_string()));
    };
    let _ = o;
    let Some(slug) = params.get("map_slug").and_then(|s| s.as_str()).filter(|s| !s.is_empty()) else {
        return Some(Err("No map selected".to_string()));
    };
    if !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Some(Err("Map not found".to_string()));
    }
    if map_world_id(cfg, slug)? != wid {
        return Some(Err("Map not found".to_string()));
    }
    let markers_in = match o.get("custom_markers") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.clone(),
        _ => return Some(Err("custom_markers and custom_regions must be lists".to_string())),
    };
    let regions_in = match o.get("custom_regions") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.clone(),
        _ => return Some(Err("custom_markers and custom_regions must be lists".to_string())),
    };
    let existing: (Value, Value) = match db.query_row("SELECT custom_markers_json, custom_regions_json FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1", [slug],
        |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))).ok() {
        Some((m, r)) => {
            let m = serde_json::from_str(m.as_deref().unwrap_or("[]")).unwrap_or(Value::Array(vec![]));
            let r = serde_json::from_str(r.as_deref().unwrap_or("[]")).unwrap_or(Value::Array(vec![]));
            (m, r)
        }
        None => (json!([]), json!([])),
    };
    let mut markers = existing.0.as_array().cloned().unwrap_or_default();
    let mut regions = existing.1.as_array().cloned().unwrap_or_default();
    markers.extend(markers_in);
    regions.extend(regions_in);
    if markers.len() > 500 || regions.len() > 500 {
        return Some(Err("Too many markers/regions on this map — limit is 500 each".to_string()));
    }
    let mid: Option<i64> = db.query_row("SELECT id FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| r.get(0)).ok();
    let (m_text, r_text) = (nd_render::py_dumps(&Value::Array(markers)), nd_render::py_dumps(&Value::Array(regions)));
    match mid {
        Some(id) => {
            crate::orm::update(db, "map_overlays", id, &[("custom_markers_json", Sql::Text(m_text)), ("custom_regions_json", Sql::Text(r_text))])?;
        }
        None => {
            crate::orm::insert(db, "map_overlays", &[
                ("slug", Sql::Text(slug.to_string())),
                ("custom_markers_json", Sql::Text(m_text)),
                ("custom_regions_json", Sql::Text(r_text)),
            ])?;
        }
    }
    Some(Ok(format!("/maps/{slug}")))
}

/// the schematic_elements branch: a new schematic (`__new__`) or the named one's elements merged by id
fn branch_schematic_elements(db: &rusqlite::Connection, wid: i64, data: &Value, params: &Map<String, Value>) -> Option<Result<String, String>> {
    use rusqlite::types::Value as Sql;
    let elements: Vec<Value> = match data {
        Value::Object(o) => match o.get("elements") {
            Some(Value::Array(a)) => a.clone(),
            _ => return Some(Err("Expected a non-empty array of schematic elements (or {\"elements\": [...]})".to_string())),
        },
        Value::Array(a) => a.clone(),
        _ => return Some(Err("Expected a non-empty array of schematic elements (or {\"elements\": [...]})".to_string())),
    };
    if elements.is_empty() || !elements.iter().all(|e| e.is_object()) {
        return Some(Err("Expected a non-empty array of schematic elements (or {\"elements\": [...]})".to_string()));
    }
    for e in &elements {
        let text = nd_render::py_dumps(e);
        if !crate::edits::plain_numbers(text.as_bytes()) {
            return None; // an element Python's json.dumps would spell differently
        }
    }
    let slug = params.get("schematic_slug").and_then(|s| s.as_str()).unwrap_or("");
    if slug == "__new__" {
        let name = params.get("new_schematic_name").and_then(|s| s.as_str()).unwrap_or("Imported Schematic").trim();
        let name = if name.is_empty() { "Imported Schematic" } else { name };
        let int_of = |k: &str, d: i64| -> Option<Result<i64, String>> {
            match params.get(k) {
                None | Some(Value::Null) => Some(Ok(d)),
                Some(v) => match v {
                    Value::Number(n) if n.as_i64().is_some() => Some(Ok(n.as_i64().unwrap())),
                    Value::String(t) if !t.is_empty() => Some(crate::form::py_int(t).ok_or_else(|| "Canvas width/height must be numbers".to_string())),
                    _ => Some(Err("Canvas width/height must be numbers".to_string())),
                },
            }
        };
        let width = match int_of("new_canvas_width", 2000)? {
            Ok(w) => w,
            Err(m) => return Some(Err(m)),
        };
        let height = match int_of("new_canvas_height", 1500)? {
            Ok(h) => h,
            Err(m) => return Some(Err(m)),
        };
        if !(100..=20000).contains(&width) || !(100..=20000).contains(&height) {
            return Some(Err("Canvas width/height must be between 100 and 20000".to_string()));
        }
        let bg = params.get("new_canvas_bg").and_then(|s| s.as_str()).unwrap_or("dark");
        if !name.is_ascii() || !bg.is_ascii() {
            return None; // the slug's lower() and the row's text: Python's
        }
        let slug = import_slug(db, name, "schematics", "schematic")?;
        crate::orm::insert(db, "schematics", &[
            ("world_id", Sql::Integer(wid)),
            ("name", Sql::Text(name.to_string())),
            ("slug", Sql::Text(slug.clone())),
            ("description", Sql::Null),
            ("is_html", Sql::Integer(0)),
            ("canvas_width", Sql::Integer(width)),
            ("canvas_height", Sql::Integer(height)),
            ("canvas_bg", Sql::Text(bg.to_string())),
            ("elements_json", Sql::Text(nd_render::py_dumps(&Value::Array(elements)))),
        ])?;
        return Some(Ok(format!("/maps/schematic/{slug}")));
    }
    if slug.is_empty() || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Some(Err("Schematic not found".to_string()));
    }
    let s = db.query_row("SELECT * FROM schematics WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok();
    let Some(s) = s else { return Some(Err("Schematic not found".to_string())) };
    if s.get("world_id").and_then(|v| v.as_i64()) != Some(wid) {
        return Some(Err("Schematic not found".to_string()));
    }
    let sid = s.get("id")?.as_i64()?;
    let raw = s.get("elements_json").and_then(|v| v.as_str()).unwrap_or("[]");
    let existing: Vec<Value> = serde_json::from_str(raw).ok()?; // a bad stored json: Python's own 500
    let mut merged = existing;
    for el in &elements {
        let eid = el.get("id");
        match eid {
            Some(id) if !id.is_null() => {
                match merged.iter().position(|x| x.get("id").map_or(false, |x| x == id)) {
                    Some(i) => merged[i] = el.clone(),
                    None => merged.push(el.clone()),
                }
            }
            _ => merged.push(el.clone()),
        }
    }
    crate::orm::update(db, "schematics", sid, &[("elements_json", Sql::Text(nd_render::py_dumps(&Value::Array(merged))))])?;
    Some(Ok(format!("/maps/schematic/{slug}")))
}

/// the field_template branch: an entity template (default) or a full sheet template (clean_system_spec applied)
fn branch_field_template(db: &rusqlite::Connection, wid: i64, data: &Value, params: &Map<String, Value>) -> Option<Result<String, String>> {
    use rusqlite::types::Value as Sql;
    let fields: &Vec<Value> = match data {
        Value::Object(o) => match o.get("fields") {
            Some(Value::Array(a)) => a,
            _ => return Some(Err("Expected an array of field definitions ({\"id\",\"label\",\"type\",...}), or {\"fields\": [...]}".to_string())),
        },
        Value::Array(a) => a,
        _ => return Some(Err("Expected an array of field definitions ({\"id\",\"label\",\"type\",...}), or {\"fields\": [...]}".to_string())),
    };
    if fields.is_empty() || !fields.iter().all(|f| f.as_object().map_or(false, |f| f.contains_key("id") && f.contains_key("label"))) {
        return Some(Err("Expected an array of field definitions ({\"id\",\"label\",\"type\",...}), or {\"fields\": [...]}".to_string()));
    }
    let data_name = data.as_object().and_then(|o| o.get("name")).and_then(|v| v.as_str());
    let data_desc = data.as_object().and_then(|o| o.get("description")).and_then(|v| v.as_str());
    let name = match params.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).or(data_name).filter(|s| !s.is_empty()) {
        Some(n) => n,
        None => "Imported Template",
    };
    let description = params.get("description").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).or(data_desc).unwrap_or("");
    if !name.is_ascii() || !description.is_ascii() {
        return None; // the slug's lower() and the rows' text: Python's
    }
    let template_kind = params.get("template_kind").and_then(|v| v.as_str()).unwrap_or("entity");
    if template_kind == "sheet" {
        let sheet_mode = match params.get("sheet_mode").and_then(|v| v.as_str()) {
            Some(m @ ("nd" | "custom")) => m.to_string(),
            _ => "nd".to_string(),
        };
        let own = data.as_object();
        let (spec, _warn) = crate::template_draft::clean_system_spec(own.and_then(|o| o.get("system")).unwrap_or(&Value::Null), fields);
        let sheet_mode = if params.get("sheet_mode").and_then(|v| v.as_str()).is_none() {
            match own.and_then(|o| o.get("sheet_mode")).and_then(|v| v.as_str()) {
                Some(m @ ("nd" | "custom")) => m.to_string(),
                _ => sheet_mode,
            }
        } else {
            sheet_mode
        };
        let rules_md = own.and_then(|o| o.get("rules_md")).and_then(|v| v.as_str()).unwrap_or("").trim();
        if rules_md.chars().count() > 20000 {
            return None; // the [:20000] cut is a code-point slice: Python's
        }
        let slug = import_slug(db, name, "sheet_templates", "template")?;
        let id = crate::orm::insert(db, "sheet_templates", &[
            ("world_id", Sql::Integer(wid)),
            ("name", Sql::Text(name.to_string())),
            ("slug", Sql::Text(slug)),
            ("description", Sql::Text(description.to_string())),
            ("is_builtin", Sql::Integer(0)),
            ("sheet_mode", Sql::Text(sheet_mode)),
            ("fields_json", Sql::Text(nd_render::py_dumps(&Value::Array(fields.clone())))),
            ("system_json", Sql::Text(nd_render::py_dumps(&spec))),
            ("rules_md", Sql::Text(rules_md.to_string())),
        ])?;
        Some(Ok(format!("/characters/templates/{id}/edit")))
    } else {
        let mut entity_kind = params.get("entity_kind").and_then(|v| v.as_str()).map(str::to_string);
        let (kinds, _, _) = nd_render::app::effective_kind_tables(None)?;
        let known = |k: &str| kinds.as_array().map_or(false, |a| a.iter().any(|x| x.as_str() == Some(k)));
        if entity_kind.as_deref().map_or(true, |k| !known(k)) {
            entity_kind = None;
        }
        let slug = import_slug(db, name, "entity_templates", "template")?;
        let id = crate::orm::insert(db, "entity_templates", &[
            ("world_id", Sql::Integer(wid)),
            ("name", Sql::Text(name.to_string())),
            ("slug", Sql::Text(slug)),
            ("kind", entity_kind.map_or(Sql::Null, Sql::Text)),
            ("description", Sql::Text(description.to_string())),
            ("is_builtin", Sql::Integer(0)),
            ("fields_json", Sql::Text(nd_render::py_dumps(&Value::Array(fields.clone())))),
        ])?;
        Some(Ok(format!("/entity-templates/{id}/edit")))
    }
}

/// the player_character branches: one, or several with "Character #N:" on a nameless row
fn branch_player_characters(db: &rusqlite::Connection, wid: i64, data: &Value, kind: &str) -> Option<Result<String, String>> {
    let rows: Vec<&Map<String, Value>> = if kind == "player_character_bulk" {
        let Value::Array(a) = data else {
            return Some(Err("Expected a non-empty array of player characters ({\"name\",...})".to_string()));
        };
        if a.is_empty() {
            return Some(Err("Expected a non-empty array of player characters ({\"name\",...})".to_string()));
        }
        a.iter().map(|r| r.as_object()).collect::<Option<Vec<_>>>()?
    } else {
        match data.as_object() {
            Some(o) => vec![o],
            None => return Some(Err("Expected an object with at least a \"name\"".to_string())),
        }
    };
    let mut last = 0i64;
    for (i, row) in rows.iter().enumerate() {
        if row.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().is_empty() {
            if kind == "player_character_bulk" {
                return Some(Err(format!("Character #{}: needs at least a \"name\"", i + 1)));
            }
            return Some(Err("Expected an object with at least a \"name\"".to_string()));
        }
        last = upsert_player_character(db, wid, row).ok()?;
    }
    let dest = if rows.len() == 1 { format!("/characters/{last}") } else if kind == "player_character_bulk" { "/characters".to_string() } else { format!("/characters/{last}") };
    Some(Ok(dest))
}

/// One reference to an uploaded picture that `convert_world_images` visits, in Python's order
enum Ref {
    Entity(i64),
    Character(i64),
    SchematicBg(i64),
    SchematicEmbed(usize, usize),
    BoardCard(usize, usize),
    AlbumImage(usize, usize),
}

struct Visit {
    scope: String,
    label: String,
    url: String,
    r: Ref,
}

/// `POST /api/import/convert-images` (importer.py `api_convert_images` / `convert_world_images`): every picture the world
/// references is re-encoded in place to one format and the references follow. A picture this port would not read as Pillow does
/// (animated, GIF, AVIF) makes the whole request Python's, before anything is touched; without Python it is left as it is.
pub async fn convert_images(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    tokio::task::spawn_blocking(move || convert_images_sync(&cfg, &uri, &headers, &body)).await.ok().flatten()
}

fn convert_images_sync(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    use crate::imaging::{convert_plan, convert_to, Convert};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    if !crate::edits::plain_numbers(body) {
        return None;
    }
    let Value::Object(b) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let fmt = match b.get("format") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(_)) | Some(Value::Object(_)) => return None, // unhashable: Python's 500
        _ => String::new(),
    };
    if !["avif", "jpg", "png", "webp"].contains(&fmt.as_str()) {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "format must be one of: avif, jpg, png, webp"));
    }
    let quality = match b.get("quality") {
        None => 90,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => {
            return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "quality must be a number"))
        }
        _ => return None, // floats, strings, booleans: int()'s own reading
    };
    if !(1..=100).contains(&quality) {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "quality must be between 1 and 100"));
    }
    let quality = quality as u8;
    let uploads = cfg.db_path.parent()?.join("uploads");
    let root = uploads.canonicalize().ok();

    // _resolve_upload_path: the file an /uploads/ URL names, or None
    let resolve = |url: &str| -> Option<std::path::PathBuf> {
        let rest = url.strip_prefix("/uploads/")?;
        let root = root.as_ref()?;
        let path = uploads.join(rest).canonicalize().ok()?;
        (path.starts_with(root) && path.is_file()).then_some(path)
    };
    let json_list = |t: Option<String>| -> Option<Vec<Value>> {
        let t = t.unwrap_or_default();
        if t.contains("NaN") || t.contains("Infinity") {
            return None;
        }
        match serde_json::from_str::<Value>(if t.is_empty() { "[]" } else { &t }) {
            Ok(Value::Array(a)) => Some(a),
            _ => Some(vec![]),
        }
    };
    let sel = |sql: &str| -> Option<Vec<Vec<Option<String>>>> {
        let mut st = db.prepare(sql).ok()?;
        let n = st.column_count();
        let rows = st.query_map([wid], |r| (0..n).map(|i| r.get::<_, Option<String>>(i)).collect::<Result<Vec<_>, _>>()).ok()?;
        rows.collect::<Result<Vec<_>, _>>().ok()
    };
    let idnum = |r: &Vec<Option<String>>| -> Option<i64> { r[0].as_ref()?.parse().ok() };

    // phase 1: walk the world, listing each visit in Python's order
    let mut visits: Vec<Visit> = vec![];
    for r in sel("SELECT CAST(id AS TEXT), name, image_url FROM entities WHERE world_id=?1 AND image_url IS NOT NULL ORDER BY id")? {
        visits.push(Visit { scope: "entity".into(), label: r[1].clone().unwrap_or_default(), url: r[2].clone()?, r: Ref::Entity(idnum(&r)?) });
    }
    for r in sel("SELECT CAST(id AS TEXT), name, portrait_url FROM player_characters WHERE world_id=?1 AND portrait_url != '' ORDER BY id")? {
        visits.push(Visit { scope: "character".into(), label: r[1].clone().unwrap_or_default(), url: r[2].clone()?, r: Ref::Character(idnum(&r)?) });
    }
    let schem = sel("SELECT CAST(id AS TEXT), name, image_url, elements_json FROM schematics WHERE world_id=?1 ORDER BY id")?;
    let mut sc_docs: Vec<Vec<Value>> = vec![];
    for (si, r) in schem.iter().enumerate() {
        let name = r[1].clone().unwrap_or_default();
        if let Some(u) = r[2].clone().filter(|u| !u.is_empty()) {
            visits.push(Visit { scope: format!("schematic: {name}"), label: "background".into(), url: u, r: Ref::SchematicBg(idnum(r)?) });
        }
        let els = json_list(r[3].clone())?;
        for (ei, el) in els.iter().enumerate() {
            if let Some(h) = el.get("href").and_then(|h| h.as_str()).filter(|h| h.starts_with("/uploads/schematics/embeds/")) {
                visits.push(Visit { scope: format!("schematic: {name}"), label: "embedded image".into(), url: h.into(), r: Ref::SchematicEmbed(si, ei) });
            }
        }
        sc_docs.push(els);
    }
    let boards = sel("SELECT CAST(id AS TEXT), name, nodes_json FROM invest_boards WHERE world_id=?1 ORDER BY id")?;
    let mut bd_docs: Vec<Vec<Value>> = vec![];
    for (bi, r) in boards.iter().enumerate() {
        let name = r[1].clone().unwrap_or_default();
        let nodes = json_list(r[2].clone())?;
        for (ni, n) in nodes.iter().enumerate() {
            if let Some(img) = n.get("image_url").and_then(|h| h.as_str()).filter(|h| !h.is_empty()) {
                let title = match n.get("title") {
                    Some(Value::String(t)) if !t.is_empty() => t.clone(),
                    Some(Value::String(_)) | None | Some(Value::Null) => "card".into(),
                    _ => return None,
                };
                visits.push(Visit { scope: format!("board: {name}"), label: title, url: img.into(), r: Ref::BoardCard(bi, ni) });
            }
        }
        bd_docs.push(nodes);
    }
    let albums = sel("SELECT CAST(id AS TEXT), name, image_urls_json FROM image_albums WHERE world_id=?1 ORDER BY id")?;
    let mut al_docs: Vec<Vec<Value>> = vec![];
    for (ai, r) in albums.iter().enumerate() {
        let name = r[1].clone().unwrap_or_default();
        let list = json_list(r[2].clone())?;
        for (ui, u) in list.iter().enumerate() {
            if let Some(u) = u.as_str() {
                visits.push(Visit { scope: format!("album: {name}"), label: "image".into(), url: u.into(), r: Ref::AlbumImage(ai, ui) });
            }
        }
        al_docs.push(list);
    }
    // user-uploaded maps: (display name, slug, the picture's path, its extension)
    let mut map_files: Vec<(String, String, std::path::PathBuf, String)> = vec![];
    if let Some(dir) = crate::maps::maps_dir(cfg) {
        if let Ok(rd) = std::fs::read_dir(&dir) {
            let mut files: Vec<_> = rd.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().map_or(false, |x| x == "json")).collect();
            files.sort();
            for f in files {
                let slug = f.file_stem().and_then(|s| s.to_str())?.to_string();
                let Ok(text) = std::fs::read_to_string(&f) else { continue };
                if text.contains("NaN") || text.contains("Infinity") {
                    return None;
                }
                let Ok(Value::Object(d)) = serde_json::from_str::<Value>(&text) else { continue };
                match d.get("world_id") {
                    None => if wid != 1 { continue },
                    Some(Value::Number(n)) if n.is_i64() => if n.as_i64() != Some(wid) { continue },
                    Some(Value::String(_)) | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => continue,
                    _ => return None,
                }
                let name = match d.get("name") { Some(Value::String(s)) => s.clone(), None => slug.clone(), _ => return None };
                for ext in [".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"] {
                    let mp = uploads.join("maps").join(format!("{slug}{ext}"));
                    if mp.is_file() {
                        map_files.push((name.clone(), slug.clone(), mp, ext.to_string()));
                        break;
                    }
                }
            }
        }
    }

    // phase 2: can every file be answered for? (nothing is touched until it can)
    for v in &visits {
        if let Some(path) = resolve(&v.url) {
            if matches!(convert_plan(&path, &fmt), Convert::Decline) {
                return None;
            }
        }
    }
    for (_, _, mp, _) in &map_files {
        if matches!(convert_plan(mp, &fmt), Convert::Decline) {
            return None;
        }
    }

    // phase 3: convert (a picture referenced twice only once)
    let mut cache: std::collections::HashMap<String, Option<String>> = std::collections::HashMap::new();
    let mut retire: Vec<std::path::PathBuf> = vec![];
    let mut results: Vec<Value> = vec![];
    let mut news: Vec<Option<String>> = vec![];
    for v in &visits {
        let new_url = match cache.get(&v.url) {
            Some(n) => n.clone(),
            None => {
                let n = match resolve(&v.url) {
                    Some(path) => match convert_to(&path, &fmt, quality) {
                        Convert::Done(dest) => {
                            retire.push(path);
                            let head = v.url.rsplit_once('/').map_or("", |(h, _)| h);
                            Some(format!("{head}/{}", dest.file_name()?.to_string_lossy()))
                        }
                        _ => None,
                    },
                    None => None,
                };
                cache.insert(v.url.clone(), n.clone());
                n
            }
        };
        results.push(json!({"scope": v.scope, "label": v.label, "old_url": v.url, "new_url": new_url, "status": if new_url.is_some() { "ok" } else { "skipped" }}));
        news.push(new_url);
    }
    for (name, slug, mp, ext) in &map_files {
        let new_url = match convert_to(mp, &fmt, quality) {
            Convert::Done(dest) => {
                retire.push(mp.clone());
                Some(format!("/uploads/maps/{}", dest.file_name()?.to_string_lossy()))
            }
            _ => None,
        };
        results.push(json!({"scope": "map", "label": name, "old_url": format!("/uploads/maps/{slug}{ext}"), "new_url": new_url,
                            "status": if new_url.is_some() { "ok" } else { "skipped" }}));
    }
    for path in retire {
        let _ = std::fs::remove_file(path); // the original goes once its replacement is in place
    }

    // the references
    let stamp = crate::orm::now_stamp();
    db.execute_batch("BEGIN IMMEDIATE").ok()?;
    let write = (|| -> Option<()> {
        let (mut ch_sc, mut ch_bd, mut ch_al) = (vec![false; sc_docs.len()], vec![false; bd_docs.len()], vec![false; al_docs.len()]);
        for (vi, v) in visits.iter().enumerate() {
            let Some(nu) = news[vi].clone() else { continue };
            match &v.r {
                Ref::Entity(id) => { db.execute("UPDATE entities SET image_url=?1, updated_at=?2 WHERE id=?3", rusqlite::params![nu, stamp, id]).ok()?; }
                Ref::Character(id) => { db.execute("UPDATE player_characters SET portrait_url=?1, updated_at=?2 WHERE id=?3", rusqlite::params![nu, stamp, id]).ok()?; }
                Ref::SchematicBg(id) => { db.execute("UPDATE schematics SET image_url=?1, updated_at=?2 WHERE id=?3", rusqlite::params![nu, stamp, id]).ok()?; }
                Ref::SchematicEmbed(d, e) => { sc_docs[*d][*e].as_object_mut()?.insert("href".into(), json!(nu)); ch_sc[*d] = true; }
                Ref::BoardCard(d, n) => { bd_docs[*d][*n].as_object_mut()?.insert("image_url".into(), json!(nu)); ch_bd[*d] = true; }
                Ref::AlbumImage(d, u) => { al_docs[*d][*u] = json!(nu); ch_al[*d] = true; }
            }
        }
        for (i, c) in ch_sc.iter().enumerate() {
            if *c { db.execute("UPDATE schematics SET elements_json=?1, updated_at=?2 WHERE id=?3", rusqlite::params![nd_render::py_dumps(&Value::Array(sc_docs[i].clone())), stamp, idnum(&schem[i])?]).ok()?; }
        }
        for (i, c) in ch_bd.iter().enumerate() {
            if *c { db.execute("UPDATE invest_boards SET nodes_json=?1, updated_at=?2 WHERE id=?3", rusqlite::params![nd_render::py_dumps(&Value::Array(bd_docs[i].clone())), stamp, idnum(&boards[i])?]).ok()?; }
        }
        for (i, c) in ch_al.iter().enumerate() {
            if *c { db.execute("UPDATE image_albums SET image_urls_json=?1 WHERE id=?2", rusqlite::params![nd_render::py_dumps(&Value::Array(al_docs[i].clone())), idnum(&albums[i])?]).ok()?; }
        }
        Some(())
    })();
    if write.is_none() || db.execute_batch("COMMIT").is_err() {
        let _ = db.execute_batch("ROLLBACK");
        return Some(crate::idpages::refusal(500));
    }
    let converted = results.iter().filter(|r| r["status"] == "ok").count();
    crate::edits::json_response(&json!({"converted": converted, "total": results.len(), "results": results}))
}
