
/// `GET /export/book.zip` (app/main.py `world_export_book`): the world as a standalone HTML book zipped with the stylesheet
/// and every illustrated entity's image. GM-only (the auth gate refuses everyone else); a world-less session is Python's
/// redirect to /worlds. The zip's own bytes differ from Python's (its timestamps are the build moment); the parity rule for
/// this route compares the extracted entries.
pub fn book_zip(pages: &crate::pages::Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None; // the auth gate's refusal: Python's
    }
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let uploads = cfg.db_path.parent()?.join("uploads");
    // entities by kind, in the query's kind/name order; the template's own attrs (image_rel, body_html) built here
    let mut st = db.prepare("SELECT * FROM entities WHERE world_id = ?1 ORDER BY kind, name").ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let h1_re = regex::Regex::new(r"(?s)^<h1[^>]*>.*?</h1>\s*").ok()?;
    let mut image_paths: Vec<(String, std::path::PathBuf)> = vec![];
    let mut entities_by_kind = Map::new();
    for e in &rows {
        let mut ent = Map::new();
        for k in ["id", "kind", "name", "subtype", "summary", "tags"] {
            ent.insert(k.into(), e.get(k).cloned().unwrap_or(Value::Null));
        }
        let mut image_rel = Value::Null;
        if let Some(url) = e.get("image_url").and_then(|v| v.as_str()) {
            if url.starts_with("/uploads/") {
                // the basename only, as every image-embedding export does
                let name = std::path::Path::new(url).file_name()?.to_str()?.to_string();
                let path = uploads.join(&name);
                if path.exists() {
                    let zip_path = format!("assets/images/{name}");
                    image_paths.push((zip_path.clone(), path));
                    image_rel = json!(format!("./{zip_path}"));
                }
            }
        }
        ent.insert("image_rel".into(), image_rel);
        let body_html = match e.get("body") {
            Some(Value::String(t)) if !t.is_empty() => {
                let raw = nd_render::md::render_md(t);
                h1_re.replace(&raw, "").to_string()
            }
            _ => String::new(),
        };
        ent.insert("body_html".into(), json!(body_html));
        let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("").to_string();
        match entities_by_kind.get_mut(&kind) {
            Some(Value::Array(a)) => a.push(Value::Object(ent)),
            _ => {
                entities_by_kind.insert(kind, Value::Array(vec![Value::Object(ent)]));
            }
        }
    }
    // boards
    let mut st = db.prepare("SELECT * FROM invest_boards WHERE world_id = ?1").ok()?;
    let boards_rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let mut boards = vec![];
    for b in &boards_rows {
        let nodes = match b.get("nodes_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(serde_json::from_str::<Value>) {
            Some(Ok(v @ Value::Array(_))) => v,
            Some(Ok(Value::Object(o))) => o.get("nodes").cloned().unwrap_or(json!([])),
            Some(Ok(_)) | None => json!([]),
            Some(Err(_)) => return None, // json.loads' own reading: Python's 500
        };
        let edges = match b.get("edges_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(serde_json::from_str::<Value>) {
            Some(Ok(v)) => v,
            None => json!([]),
            Some(Err(_)) => return None,
        };
        boards.push(json!({"name": b.get("name"), "description": b.get("description"), "nodes": nodes, "edges": edges}));
    }
    // maps: the name (else the slug) and the marker count (a dict counts its keys, as Python's len)
    let mut maps = vec![];
    for (slug, d) in crate::maps::world_maps(cfg, wid)? {
        let name = d.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string).unwrap_or(slug);
        let markers = match d.get("markers") {
            None => 0,
            Some(Value::Array(a)) => a.len(),
            Some(Value::Object(o)) => o.len(),
            Some(_) => return None, // len() of a scalar: Python's TypeError
        };
        maps.push(json!({"name": name, "markers": markers}));
    }
    // rules: the world's own, else the bundled core rules, the legacy anchors stripped, rendered
    let md = crate::retrieval::world_rules_markdown(&world);
    let anchor_re = regex::Regex::new(r#"(?i)<a\s+name="[^"]*">\s*</a>"#).ok()?;
    let md = anchor_re.replace_all(&md, "").to_string();
    let rules_html = if md.is_empty() { String::new() } else { nd_render::md::render_md(&md) };
    // the kinds and icons the context processor would hand a page of this world
    let (kinds, kind_icons, _) = nd_render::app::effective_kind_tables(Some(&world))?;
    let export_date = {
        let (y, m, d, _, _) = nd_render::now_minute();
        format!("{y:04}-{m:02}-{d:02}")
    };
    let mut ctx = Map::new();
    ctx.insert("world".into(), Value::Object(world.clone()));
    ctx.insert("worlds".into(), p.ctx.get("worlds").cloned().unwrap_or(json!([])));
    ctx.insert("kinds".into(), kinds);
    ctx.insert("kind_icons".into(), kind_icons);
    ctx.insert("entities_by_kind".into(), Value::Object(entities_by_kind));
    ctx.insert("boards".into(), Value::Array(boards));
    ctx.insert("maps".into(), Value::Array(maps));
    ctx.insert("rules_html".into(), json!(rules_html));
    ctx.insert("export_date".into(), json!(export_date));
    let html = pages.env.get_template("world_export.html").ok()?.render(nd_render::pyvalue::to_value(&Value::Object(ctx))).ok()?;
    // the zip: index.html, the stylesheet, every image
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut zf = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        zf.start_file("index.html", opts).ok()?;
        zf.write_all(html.as_bytes()).ok()?;
        let css = cfg.static_dir.join("style.css");
        if css.exists() {
            zf.start_file("assets/style.css", opts).ok()?;
            zf.write_all(&std::fs::read(&css).ok()?).ok()?;
        }
        for (zpath, disk) in &image_paths {
            zf.start_file(zpath.as_str(), opts).ok()?;
            zf.write_all(&std::fs::read(disk).ok()?).ok()?;
        }
        zf.finish().ok()?;
    }
    let slug = world.get("slug").and_then(|v| v.as_str())?.to_string();
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/zip")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{slug}-worldbook.zip\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(buf.into_inner()))
        .ok()
}
