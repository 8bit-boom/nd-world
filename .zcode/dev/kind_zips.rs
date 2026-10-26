
/// `_entity_to_markdown(db, e, request)` (the entity .md download's text, shared by the kind zips); None for anything
/// Python would render its own way
pub(crate) fn entity_markdown(db: &rusqlite::Connection, p: &crate::layout::PageCtx, e: &Map<String, Value>, w: &Map<String, Value>, id: i64) -> Option<String> {
    let strip = |v: Option<&Value>| -> Option<Option<String>> {
        match v {
            None | Some(Value::Null) => Some(None),
            Some(Value::String(s)) if p.is_gm || s.is_empty() => Some(Some(s.clone())),
            Some(Value::String(s)) => Some(Some(nd_perm::gmonly::strip_gm_only(s, Some(p.user_id), nd_render::now_minute())?)),
            _ => None,
        }
    };
    let name = e.get("name").and_then(|v| v.as_str())?.to_string();
    let kind = e.get("kind").and_then(|v| v.as_str())?.to_string();
    if !name.is_ascii() || !kind.is_ascii() {
        return None; // str.isalnum() / str.capitalize() beyond ASCII: Python's
    }
    let (_, icons, _) = nd_render::app::effective_kind_tables(Some(w))?;
    let icon = icons.get(&kind).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let cap = kind.chars().next().map(|c| c.to_ascii_uppercase().to_string() + &kind[1..].to_ascii_lowercase()).unwrap_or_default();
    let mut label = nd_render::text::py_strip(&format!("{icon} {cap}")).to_string();
    match e.get("subtype") {
        Some(Value::String(s)) if !s.is_empty() => label.push_str(&format!(" \u{2014} {s}")),
        None | Some(Value::Null) | Some(Value::String(_)) => {}
        _ => return None,
    }
    let body = strip(e.get("body"))?.unwrap_or_default();
    let mut parts: Vec<String> = vec![format!("# {name}"), String::new(), format!("*{label}*"), String::new()];
    if let Some(s) = strip(e.get("summary"))?.filter(|s| !s.is_empty()) {
        parts.push(format!("*{s}*"));
        parts.push(String::new());
    }
    parts.extend(["---".to_string(), String::new(), body, String::new()]);
    let vis = if p.is_gm { "" } else { " AND visible_to_players IS 1" };
    let notes = rows_of(db, &format!("SELECT * FROM entity_notes WHERE entity_id = ?1{vis} ORDER BY created_at"), &[&id])?;
    if !notes.is_empty() {
        parts.extend(["## Notes".to_string(), String::new()]);
        for n in &notes {
            let content = strip(n.get("content"))??; // a None note is Python's TypeError
            parts.extend([content, String::new(), "---".to_string(), String::new()]);
        }
    }
    Some(format!("{}\n", parts.join("\n").trim_end()))
}

/// (kind, selected) of `/kind/<kind>/download[-selected].zip`
pub(crate) fn kind_zip_path(path: &str) -> Option<(String, bool)> {
    let rest = path.strip_prefix("/kind/")?;
    let (kind, tail) = rest.split_once('/')?;
    let selected = match tail {
        "download.zip" => false,
        "download-selected.zip" => true,
        _ => return None,
    };
    (!kind.is_empty() && !kind.contains('%')).then(|| (kind.to_string(), selected))
}

/// `kind_download` / `kind_download_selected` (app/main.py): every (or the picked) entity of the kind as one .md file
/// per entity, zipped - the same text the single-entity download writes, the same visibility, the download toggle
pub(crate) fn kind_download_zip(cfg: &Config, uri: &Uri, headers: &HeaderMap, kind: &str, selected: bool) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let w = p.world.clone()?;
    let wid = w.get("id")?.as_i64()?;
    if !p.is_gm && !crate::edits::py_truthy_pub(w.get("players_can_download_entities")) {
        return Some(crate::idpages::refusal(403));
    }
    if !crate::pages::section_ok(&p, Some(&w), &format!("kind_{kind}"))? {
        return Some(crate::idpages::refusal(403));
    }
    // the picked ids: FastAPI's list[int] - every value must be an int (a "-5" is one; anything else is its 422)
    let mut ids: Vec<i64> = vec![];
    if selected {
        for pair in uri.query().unwrap_or("").split('&').filter(|p| !p.is_empty()) {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let dec = |s: &str| percent_encoding::percent_decode_str(&s.replace('+', " ")).decode_utf8_lossy().to_string();
            if dec(k) == "id" {
                let raw = dec(v);
                // FastAPI's int: a plain (optionally signed) int; anything else is its 422
                let t = raw.strip_prefix('-').unwrap_or(&raw);
                if t.is_empty() || t.len() > 15 || !t.bytes().all(|b| b.is_ascii_digit()) || raw.parse::<i64>().is_err() {
                    return None;
                }
                ids.push(raw.parse::<i64>().ok()?);
            }
        }
    }
    let vis = if p.is_gm { String::new() } else {
        " AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))".to_string()
    };
    let sql = if selected {
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        if ids.is_empty() {
            format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' AND id IN () {vis} ORDER BY name")
        } else {
            format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' AND id IN ({list}) {vis} ORDER BY name")
        }
    } else {
        format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' {vis} ORDER BY name")
    };
    let rows: Vec<Map<String, Value>> = if p.is_gm {
        rows_of(&db, &sql, &[&wid])?
    } else {
        rows_of(&db, &sql, &[&wid, &p.user_id])?
    };
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut zf = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        for e in &rows {
            let id = e.get("id")?.as_i64()?;
            let name = e.get("name").and_then(|v| v.as_str()).unwrap_or("entity");
            if !name.is_ascii() {
                return None; // the file name's isalnum() beyond ASCII: Python's
            }
            let fname: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || " -_".contains(*c)).collect();
            let fname = if fname.is_empty() { "entity".to_string() } else { fname };
            let text = entity_markdown(&db, &p, e, &w, id)?;
            zf.start_file(format!("{fname}-{id}.md"), opts).ok()?;
            std::io::Write::write_all(&mut zf, text.as_bytes()).ok()?;
        }
        zf.finish().ok()?;
    }
    let slug = w.get("slug").and_then(|v| v.as_str())?;
    let filename = if selected { format!("{slug}-{kind}-selected.zip") } else { format!("{slug}-{kind}.zip") };
    if !filename.is_ascii() {
        return None;
    }
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(axum::http::header::CONTENT_TYPE, "application/zip")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{filename}\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(buf.into_inner()))
        .ok()
}
