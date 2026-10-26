
/// `POST /worlds/<id>/import` (app/main.py `world_import`): a world JSON export's entities restored - the embedded
/// data-URI image written back under a fresh uploads name, each entity upserted by (name, kind) with EVERY field set
/// outright (a restore reproduces the backup exactly), the redirect counting created and updated.
fn world_import(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let wid = ids[0];
    // the world row itself (any world - this is a GM restore route, no active-world scoping)
    let Some(w) = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", wid) else {
        return Some(crate::idpages::refusal(404));
    };
    let slug = w.get("slug").and_then(|v| v.as_str())?.to_string();
    if !slug.is_ascii() {
        return None; // the redirect's percent-encoding of a non-ASCII slug: Python's
    }
    let part = one_file(c, "file")?;
    if part.filename.is_empty() {
        return None; // FastAPI's 422 for a missing required file: Python's
    }
    // the file's bytes: json.loads decides
    let scratch = c.uploads_scratch()?;
    c.upload.as_ref()?.copy_part(part, &scratch)?;
    let bytes = std::fs::read(&scratch).ok()?;
    let _ = std::fs::remove_file(&scratch);
    if !crate::edits::plain_numbers(&bytes) {
        return None; // a body json.loads reads differently (NaN, ...): Python's
    }
    let data: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "Invalid JSON file")),
    };
    let entities = match data.get("entities") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.clone(),
        _ => return None, // a non-list entities: Python's iteration failure
    };
    let uploads = cfg_uploads(c)?;
    let mut created: i64 = 0;
    let mut updated: i64 = 0;
    for item in &entities {
        let Some(o) = item.as_object() else { continue };
        let name = o.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        let kind = o.get("kind").and_then(|v| v.as_str()).unwrap_or("note").to_string();
        if name.is_empty() {
            continue;
        }
        if !name.is_ascii() || !kind.is_ascii() {
            return None; // str() / the query beyond ASCII: Python's
        }
        // the embedded image: a data URI written back under a fresh uploads name
        let mut image_url = match o.get("image_url") {
            Some(Value::String(s)) => Some(s.clone()),
            _ => None,
        };
        if let Some(Value::String(img)) = o.get("image_data") {
            if img.starts_with("data:image/") {
                let Some((header, b64)) = img.split_once(',') else { return None };
                let ext_part = header.split('/').nth(1)?.split(';').next()?;
                let ext = if ext_part.starts_with('.') { ext_part.to_string() } else { format!(".{ext_part}") };
                let ext = if ALLOWED_UPLOAD_EXTS.contains(&ext.as_str()) { ext } else { ".jpg".to_string() };
                let bytes = {
                    use base64::Engine;
                    base64::engine::general_purpose::STANDARD.decode(b64).ok()?  // a bad payload: Python's 500
                };
                let filename = unique_upload_filename(&name, &ext)?;
                std::fs::write(uploads.join(&filename), &bytes).ok()?;
                c.created.push(uploads.join(&filename));
                image_url = Some(format!("/uploads/{filename}"));
            }
        }
        let template_id = resolve_entity_template(c, wid, o);
        let custom_fields = match o.get("custom_fields_json") {
            Some(v @ Value::Object(_)) => v.clone(),
            _ => json!({}),
        };
        let cf_text = nd_render::py_dumps(&custom_fields);
        let visible = crate::edits::py_truthy_pub(o.get("visible_to_players").or(Some(&json!(true))));
        let col = |k: &str| -> Option<Sql> {
            match o.get(k) {
                None | Some(Value::Null) => Some(Sql::Null),
                Some(Value::String(s)) => Some(Sql::Text(s.clone())),
                _ => None, // a list / dict in a passthrough column: Python dies at commit - declined
            }
        };
        let existing: Option<i64> = db_query_one(&c.db,
            "SELECT id FROM entities WHERE name = ?1 AND kind = ?2 AND world_id = ?3 ORDER BY id LIMIT 1", &[&name, &kind, &wid])?;
        use rusqlite::types::Value as Sql;
        if let Some(id) = existing {
            let subtype = col("subtype")?;
            let folder = col("folder")?;
            let tags = col("tags")?;
            let summary = col("summary")?;
            let body = col("body")?;
            crate::orm::update(&c.db, "entities", id, &[
                ("subtype", subtype), ("folder", folder), ("tags", tags),
                ("summary", summary), ("body", body),
                ("image_url", image_url.clone().map_or(Sql::Null, Sql::Text)),
                ("visible_to_players", Sql::Integer(visible as i64)),
                ("template_id", template_id.map_or(Sql::Null, Sql::Integer)),
                ("custom_fields_json", Sql::Text(cf_text)),
            ])?;
            updated += 1;
        } else {
            let subtype = col("subtype")?;
            let folder = col("folder")?;
            let tags = col("tags")?;
            let summary = col("summary")?;
            let body = col("body")?;
            crate::orm::insert(&c.db, "entities", &[
                ("world_id", Sql::Integer(wid)),
                ("name", Sql::Text(name)),
                ("kind", Sql::Text(kind)),
                ("subtype", subtype), ("folder", folder), ("tags", tags),
                ("summary", summary), ("body", body),
                ("image_url", image_url.map_or(Sql::Null, Sql::Text)),
                ("visible_to_players", Sql::Integer(visible as i64)),
                ("template_id", template_id.map_or(Sql::Null, Sql::Integer)),
                ("custom_fields_json", Sql::Text(cf_text)),
            ])?;
            created += 1;
        }
    }
    redirect(&format!("/export?w={slug}&imported={created}&updated={updated}"))
}

const ALLOWED_UPLOAD_EXTS: [&str; 6] = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"];

fn cfg_uploads(c: &Ctx) -> Option<std::path::PathBuf> {
    Some(c.cfg.db_path.parent()?.join("uploads"))
}

fn db_query_one(db: &rusqlite::Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Option<i64>> {
    let mut st = db.prepare(sql).ok()?;
    st.query_row(params, |r| r.get::<_, i64>(0)).ok().map(Some).or(Some(None))
}

/// `_resolve_entity_template` (importer.py), on a Ctx
fn resolve_entity_template(c: &Ctx, wid: i64, ent: &Map<String, Value>) -> Option<i64> {
    match ent.get("template_id") {
        Some(Value::Number(n)) if n.as_i64().is_some() => return Some(n.as_i64().unwrap()),
        Some(Value::String(s)) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => return s.parse().ok(),
        _ => {}
    }
    if let Some(slug) = ent.get("template_slug").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
        let id: Option<i64> = c.db.query_row("SELECT id FROM entity_templates WHERE slug = ?1", [slug], |r| r.get(0)).ok();
        if let Some(id) = id {
            return Some(id);
        }
    }
    if let Some(nm) = ent.get("template").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
        if nm.is_ascii() {
            let id: Option<i64> = c.db.query_row(
                "SELECT id FROM entity_templates WHERE (world_id IS NULL OR world_id = ?1) AND lower(name) = lower(?2) ORDER BY id LIMIT 1",
                rusqlite::params![wid, nm], |r| r.get(0)).ok();
            if let Some(id) = id {
                return Some(id);
            }
        } else {
            return None; // ilike beyond ASCII: Python's
        }
    }
    None
}
