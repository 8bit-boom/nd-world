
/// the map_overlay branch: the overlay row's markers/regions extended by the import's, the merged totals capped
fn branch_map_overlay(cfg: &Config, db: &rusqlite::Connection, wid: i64, data: &Value, params: &Map<String, Value>) -> Option<Result<String, String>> {
    use rusqlite::types::Value as Sql;
    let Some(o) = data.as_object() else {
        return Some(Err("Expected an object with \"custom_markers\" and/or \"custom_regions\"".to_string()));
    };
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
