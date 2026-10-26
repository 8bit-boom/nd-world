
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
    let markers = pc_marker_keys();
    let mut out: Vec<(String, Value)> = vec![];
    for (k, v) in row {
        let nk = normkey(k)?;
        let name = markers.iter().find(|m| *m == &nk).cloned().unwrap_or_else(|| k.clone());
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
        if !out.iter().any(|(k, _)| k == "stats_json") {
            out.push(("stats_json".to_string(), nd_default_stats()));
        }
        if !out.iter().any(|(k, _)| k == "currency_json") {
            out.push(("currency_json".to_string(), nd_default_currency()));
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
