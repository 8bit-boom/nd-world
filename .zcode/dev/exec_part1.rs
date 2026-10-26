
/// `POST /api/import/execute` (app/routers/importer.py `import_execute`): the shared front (the world, the import edit
/// section, the outer body, the kind) and, this batch, the `world_rules`, `random_table`, `entity_single` and
/// `entity_bulk` branches; anything else declines mid-way (Python answers, transaction rolled back by the caller).
pub async fn execute(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let out = tokio::task::spawn_blocking(move || execute_sync(&cfg, &uri, &headers, &body)).await.ok().flatten();
    out
}

fn execute_sync(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    use rusqlite::types::Value as Sql;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    if !crate::pages::edit_level(&p, Some(&world), "import")? {
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
        None | Some(Value::Null) | Some(Value::String(String::new())) => detect_kind(&data, &kinds)?.get("kind").and_then(|k| k.as_str())?.to_string(),
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
    let answer = execute_import(&db, &p, wid, &world, &data, &kind, &params, &kinds);
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
fn execute_import(db: &rusqlite::Connection, p: &crate::layout::PageCtx, wid: i64, world: &Map<String, Value>, data: &Value,
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
                        let label = if kind == "entity_bulk" { format!("Entity #{i + 1}: {msg}") } else { msg };
                        return Some(Err(label));
                    }
                    Err(None) => return None,
                }
            }
            Some(Ok(if list.len() == 1 { format!("/entity/{last_id}") } else if kind == "entity_bulk" { "/".to_string() } else { format!("/entity/{last_id}") }))
        }
        _ => None, // the branches that follow in later batches: Python's
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
    let col = |k: &str, or_null: bool| -> Option<Sql> {
        match o.get(k) {
            None | Some(Value::Null) => Some(if or_null { Sql::Null } else { Sql::Text(String::new()) }),
            Some(Value::String(s)) => Some(Sql::Text(s.clone())),
            _ => None,
        }
    };
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
    ]).map_err(|_| None)
}
