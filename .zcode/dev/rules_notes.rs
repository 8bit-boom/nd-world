
/// `GET /export/rules-and-notes.md` (app/main.py `export_rules_and_notes`): the world's rules plus every note entity and
/// every entity note, hidden ones included - a GM prep export. GM-only; a world-less session is Python's 404.
pub fn rules_and_notes(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None; // the auth gate's refusal: Python's
    }
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let name = world.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let slug = world.get("slug").and_then(|v| v.as_str())?;
    let mut parts: Vec<String> = vec![format!("# {name} — Rules & Notes"), String::new()];
    let anchor_re = regex::Regex::new(r#"(?i)<a\s+name="[^"]*">\s*</a>"#).ok()?;
    let rules = anchor_re.replace_all(&crate::retrieval::world_rules_markdown(&world), "").to_string();
    parts.push(nd_render::text::py_strip(&rules).to_string());
    parts.push(String::new());
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    // the world's Notes: kind "note" entities, full text, hidden ones included
    let note_docs = rows("SELECT * FROM entities WHERE world_id = ?1 AND kind = 'note' ORDER BY name, id")?;
    if !note_docs.is_empty() {
        parts.push("---".into());
        parts.push(String::new());
        parts.push("## Notes".into());
        parts.push(String::new());
        for doc in &note_docs {
            let dname = doc.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let sub = doc.get("subtype").and_then(|v| v.as_str()).filter(|s| !s.is_empty());
            if dname.chars().any(|c| !c.is_ascii()) || sub.map_or(false, |s| s.chars().any(|c| !c.is_ascii())) {
                return None; // beyond the ASCII names this export's plain-text writer promises: Python's
            }
            parts.push(format!("### {dname}{}", sub.map(|s| format!(" [{s}]")).unwrap_or_default()));
            parts.push(String::new());
            let body = doc.get("body").and_then(|v| v.as_str()).filter(|s| !s.is_empty());
            let text = body.or_else(|| doc.get("summary").and_then(|v| v.as_str())).unwrap_or("");
            let text = nd_render::text::py_strip(text);
            if !text.is_empty() {
                parts.push(text.to_string());
                parts.push(String::new());
            }
        }
    }
    // every note pinned to an entity of the world, grouped by entity (kind, name order)
    let notes = rows("SELECT n.*, e.name AS __ename, e.kind AS __ekind FROM entity_notes n JOIN entities e ON n.entity_id = e.id \
                      WHERE e.world_id = ?1 ORDER BY e.kind, e.name, n.created_at")?;
    if !notes.is_empty() {
        parts.push("---".into());
        parts.push(String::new());
        parts.push("## Entity notes".into());
        parts.push(String::new());
        let mut last_entity: Option<i64> = None;
        for note in &notes {
            let eid = note.get("entity_id").and_then(|v| v.as_i64())?;
            if last_entity != Some(eid) {
                let ename = note.get("__ename").and_then(|v| v.as_str()).unwrap_or("");
                let ekind = note.get("__ekind").and_then(|v| v.as_str()).unwrap_or("");
                if ename.chars().any(|c| !c.is_ascii()) || !ekind.bytes().all(|b| b.is_ascii_lowercase()) {
                    return None; // a non-ASCII name or a kind Python's capitalize() would read differently: Python's
                }
                // str.capitalize() of an already-lowercase ASCII kind: first letter up, the rest as they are
                parts.push(format!("### {ename} ({}{})", ekind.chars().next().map(|c| c.to_ascii_uppercase()).unwrap_or_default(),
                                   ekind.get(1..).unwrap_or("")));
                parts.push(String::new());
                last_entity = Some(eid);
            }
            parts.push(note.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string());
            parts.push(String::new());
        }
    }
    let content = format!("{}\n", nd_render::text::py_strip(&parts.join("\n")));
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/markdown; charset=utf-8")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{slug}-rules-and-notes.md\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(content))
        .ok()
}
