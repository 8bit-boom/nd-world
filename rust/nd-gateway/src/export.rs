//! The GM's JSON exports of a world's entities: `GET /worlds/<id>/export` (app/main.py `world_export`, the whole world with its
//! header) and `GET /worlds/<id>/export/entities/<kind>.json` (app/routers/export.py `export_entities_by_kind`) - the same text
//! `stream_json_array_download` writes, each entity's image embedded as a data URI.

use std::io::Write;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, Uri};
use base64::Engine;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;

/// The route: (world id, None for the whole world or Some(kind))
pub fn route(path: &str) -> Option<(i64, Option<String>)> {
    let rest = path.strip_prefix("/worlds/")?;
    let (id, tail) = rest.split_once('/')?;
    if id.is_empty() || id.len() > 15 || !id.bytes().all(|b| b.is_ascii_digit()) || (id.len() > 1 && id.starts_with('0')) {
        return None;
    }
    let id: i64 = id.parse().ok()?;
    if tail == "export" {
        return Some((id, None));
    }
    let kind = tail.strip_prefix("export/entities/")?.strip_suffix(".json")?;
    (!kind.is_empty() && !kind.contains('/')).then(|| (id, Some(kind.to_string())))
}

/// `_entity_to_export_dict(e)`; None for custom fields json.loads would read its own way
fn export_dict(db: &Connection, uploads: &std::path::Path, e: &Map<String, Value>) -> Option<Value> {
    let col = |k: &str| e.get(k).cloned().unwrap_or(Value::Null);
    let slug: Option<String> = match e.get("template_id").and_then(|v| v.as_i64()) {
        Some(t) => db.query_row("SELECT slug FROM entity_templates WHERE id = ?1", [t], |r| r.get::<_, Option<String>>(0)).ok().flatten(),
        None => None,
    };
    let cf_raw = e.get("custom_fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
    let cf: Value = serde_json::from_str(cf_raw).ok()?;
    let image_url = e.get("image_url").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mut image_data = Value::Null;
    if image_url.starts_with("/uploads/") {
        let name = std::path::Path::new(&image_url).file_name()?.to_str()?.to_string();
        let path = uploads.join(&name);
        if path.exists() {
            let ext = path.extension().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
            let bytes = std::fs::read(&path).ok()?;
            image_data = json!(format!("data:image/{ext};base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes)));
        }
    }
    let mut d = Map::new();
    for k in ["kind", "subtype", "name", "folder", "tags", "summary", "body", "image_url"] {
        d.insert(k.into(), col(k));
    }
    d.insert("image_data".into(), image_data);
    d.insert("visible_to_players".into(), json!(crate::edits::py_truthy_pub(e.get("visible_to_players"))));
    d.insert("template".into(), slug.clone().map_or(Value::Null, Value::String));
    d.insert("template_slug".into(), slug.map_or(Value::Null, Value::String));
    d.insert("custom_fields_json".into(), cf);
    Some(Value::Object(d))
}

/// `stream_json_array_download`'s text: prefix "[" each item (indent=2, then 4 more spaces a line) "]" suffix
fn array_text(items: &[Value], prefix: &str, suffix: &str) -> String {
    let mut out = format!("{prefix}[");
    for (i, it) in items.iter().enumerate() {
        let text = nd_render::py_dumps_indent_as(it, 2, false);
        let indented = text.split('\n').map(|l| format!("    {l}")).collect::<Vec<_>>().join("\n");
        out.push_str(if i == 0 { "\n" } else { ",\n" });
        out.push_str(&indented);
    }
    out.push_str(if items.is_empty() { "" } else { "\n" });
    out.push(']');
    out.push_str(suffix);
    out
}

pub fn get(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (wid, kind) = route(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None; // the auth gate's refusal: Python's
    }
    let Ok(w) = db.query_row("SELECT * FROM worlds WHERE id = ?1", [wid], |r| crate::layout::row_map(r)) else {
        return Some(crate::idpages::refusal(404));
    };
    let uploads = cfg.db_path.parent()?.join("uploads");
    let rows = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare_cached(sql).ok()?;
        let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let slug = w.get("slug").and_then(|v| v.as_str())?.to_string();
    let (entities, prefix, suffix, filename) = match &kind {
        None => {
            let header_obj = json!({"world": {"name": w.get("name"), "slug": w.get("slug"), "description": w.get("description"), "accent": w.get("accent")}});
            let header_text = nd_render::py_dumps_indent_as(&header_obj, 2, false);
            let trimmed = header_text.trim_end();
            let prefix = format!("{},\n  \"entities\": ", trimmed[..trimmed.len() - 1].trim_end());
            (rows("SELECT * FROM entities WHERE world_id = ?1", &[&wid])?, prefix, "\n}".to_string(), format!("{slug}-export.json"))
        }
        Some(k) => {
            let known = nd_render::app::data().get("kinds")?.as_array()?.iter().any(|x| x.as_str() == Some(k.as_str()));
            if !known {
                return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, format!("Unknown entity kind: {k}")));
            }
            (rows("SELECT * FROM entities WHERE world_id = ?1 AND kind = ?2 ORDER BY name", &[&wid, k])?, String::new(), String::new(), format!("{slug}-{k}.json"))
        }
    };
    let items: Vec<Value> = entities.iter().map(|e| export_dict(&db, &uploads, e)).collect::<Option<_>>()?;
    let text = array_text(&items, &prefix, &suffix);
    Some(Response::builder().header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{filename}\""))
        .header("x-nd-served-by", "gateway").body(Body::from(text)).ok()?)
}

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
    let mut st = db.prepare_cached("SELECT * FROM entities WHERE world_id = ?1 ORDER BY kind, name").ok()?;
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
    let mut st = db.prepare_cached("SELECT * FROM invest_boards WHERE world_id = ?1").ok()?;
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
    for (slug, d) in crate::maps::world_maps(&db, cfg, wid)? {
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

/// the civil date of a days-since-epoch count (Howard Hinnant's civil_from_days)
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `GET /admin/backup.zip` (app/main.py `admin_backup`): the whole instance - a VACUUM INTO snapshot of the database,
/// a manifest with the table counts, every uploaded file and every map json - zipped. GM-only. The filename and the
/// manifest's created_at are the build moment; the parity rule compares the entries with those normalized.
pub fn admin_backup(pages: &crate::pages::Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?;
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now.as_secs())?;
    if !p.is_gm {
        return None; // the auth gate's refusal: Python's
    }
    // datetime.now(timezone.utc).isoformat(): microseconds when there are any, the +00:00 offset
    let iso = |t: std::time::Duration| {
        let secs = t.as_secs();
        let micros = t.subsec_micros();
        let (y, m, d) = civil_from_days(secs.div_euclid(86400) as i64);
        let rem = secs.rem_euclid(86400);
        if micros == 0 {
            format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
        } else {
            format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{micros:06}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
        }
    };
    let count = |table: &str| -> Option<i64> { db.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0)).ok() };
    let mut manifest = Map::new();
    manifest.insert("app".into(), json!("nd-world"));
    manifest.insert("created_at".into(), json!(iso(now)));
    let mut tables = Map::new();
    for t in ["worlds", "entities", "users", "world_memberships", "invite_codes", "player_characters", "combat_sessions",
              "quests", "parties", "game_sessions", "schematics", "map_overlays", "invest_boards", "private_notes",
              "entity_notes", "entity_templates"] {
        tables.insert(t.into(), json!(count(t)?));
    }
    manifest.insert("tables".into(), Value::Object(tables));
    // the snapshot: VACUUM INTO a scratch file, zipped then removed
    // the snapshot lives NEXT TO the database (not under uploads/: the backup walks that tree, and a snapshot of
    // itself inside itself would both bloat it and differ from Python's walk)
    let snap = cfg.db_path.parent()?.join(format!(".backup-{}-{:x}.db.tmp", std::process::id(), now.as_nanos()));
    {
        let raw = rusqlite::Connection::open(&cfg.db_path).ok()?;
        raw.execute("VACUUM INTO ?1", [snap.to_string_lossy().to_string()]).ok()?;
    }
    let uploads = cfg.db_path.parent()?.join("uploads");
    let maps_dir = crate::maps::maps_dir(cfg)?;
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut zf = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        zf.start_file("world.db", opts).ok()?;
        zf.write_all(&std::fs::read(&snap).ok()?).ok()?;
        zf.start_file("manifest.json", opts).ok()?;
        zf.write_all(nd_render::py_dumps_indent_as(&Value::Object(manifest), 2, true).as_bytes()).ok()?;
        let walk = |zf: &mut zip::ZipWriter<&mut std::io::Cursor<Vec<u8>>>, root: &std::path::Path, prefix: &str| -> Option<()> {
            if !root.exists() {
                return Some(());
            }
            let mut stack = vec![root.to_path_buf()];
            while let Some(dir) = stack.pop() {
                let mut entries: Vec<std::path::PathBuf> = std::fs::read_dir(&dir).ok()?.filter_map(|e| e.ok()).map(|e| e.path()).collect();
                entries.sort();
                for e in entries {
                    if e.is_dir() {
                        stack.push(e);
                    } else if e.is_file() {
                        let rel = e.strip_prefix(root).ok()?;
                        zf.start_file(format!("{prefix}/{}", rel.to_string_lossy()), opts).ok()?;
                        zf.write_all(&std::fs::read(&e).ok()?).ok()?;
                    }
                }
            }
            Some(())
        };
        walk(&mut zf, &uploads, "uploads")?;
        walk(&mut zf, &maps_dir, "maps")?;
        zf.finish().ok()?;
    }
    let _ = std::fs::remove_file(&snap);
    // datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    let (y, m, d) = civil_from_days(now.as_secs().div_euclid(86400) as i64);
    let rem = now.as_secs().rem_euclid(86400);
    let stamp = format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, rem % 3600 / 60, rem % 60);
    let _ = pages;
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/zip")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"nd-world-backup-{stamp}.zip\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(buf.into_inner()))
        .ok()
}

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
        let mut st = db.prepare_cached(sql).ok()?;
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
                if !ekind.is_ascii() {
                    return None; // a kind Python's capitalize() could read differently from ASCII: Python's
                }
                // str.capitalize() of an ASCII kind: first letter up, the rest down (kinds like "side_quest" keep their underscore)
                let mut cap = ekind.to_ascii_lowercase();
                if let Some(f) = cap.get_mut(0..1) {
                    f.make_ascii_uppercase();
                }
                parts.push(format!("### {ename} ({cap})"));
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

/// `GET /export/foundry.json` (app/main.py `export_foundry`): every entity, the rules and every character of the active world as Foundry JournalEntry documents,
/// in one JSON array (`stream_json_array_download`: each document indented by 2, then every line by 4 more). GM only.
pub fn foundry_world(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let slug = world.get("slug").and_then(|v| v.as_str())?;
    if !slug.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return None;
    }
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare_cached(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let text = |m: &Map<String, Value>, k: &str| -> Option<String> {
        match m.get(k) { None | Some(Value::Null) => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None }
    };
    let mut docs: Vec<Value> = vec![];
    let anchor_re = regex::Regex::new(r#"(?i)<a\s+name="[^"]*">\s*</a>"#).ok()?;
    let rules = nd_render::md::render_md(&anchor_re.replace_all(&crate::retrieval::world_rules_markdown(&world), ""));
    docs.push(json!({"name": "Rules", "folder": null,
        "pages": [{"name": "Rules", "type": "text", "text": {"format": 1, "content": rules}, "sort": 0}],
        "flags": {"nd-world": {"source": "nd-world", "kind": "rules"}}}));
    let entities = rows("SELECT * FROM entities WHERE world_id = ?1 ORDER BY kind, name")?;
    let mut notes: std::collections::HashMap<i64, Vec<String>> = std::collections::HashMap::new();
    if !entities.is_empty() {
        let mut st = db.prepare_cached("SELECT n.entity_id, n.content FROM entity_notes n JOIN entities e ON e.id = n.entity_id WHERE e.world_id = ?1 ORDER BY n.entity_id, n.created_at").ok()?;
        let r = st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        for (eid, c) in r {
            notes.entry(eid).or_default().push(c?);
        }
    }
    for e in &entities {
        let id = e.get("id")?.as_i64()?;
        let kind = text(e, "kind")?;
        let subtype = text(e, "subtype")?;
        if !kind.is_ascii() || kind.is_empty() {
            return None; // str.capitalize of a kind beyond ASCII: Python's (the subtype is only interpolated)
        }
        let kind_label = format!("{}{}{}", kind[..1].to_uppercase(), kind[1..].to_lowercase(), if subtype.is_empty() { String::new() } else { format!(" \u{2014} {subtype}") });
        let (summary, body) = (text(e, "summary")?, text(e, "body")?);
        let mut content = String::new();
        if !summary.is_empty() {
            content.push_str(&format!("<p><em>{}</em></p>", crate::apis::html_escape(&summary)));
        }
        content.push_str(&if body.is_empty() { "<p><em>No description.</em></p>".to_string() } else { nd_render::md::render_md(&body) });
        let mut pages = vec![json!({"name": "Overview", "type": "text", "text": {"format": 1, "content": content}, "sort": 0})];
        if let Some(ns) = notes.get(&id) {
            let html: String = ns.iter().map(|n| nd_render::md::render_md(n)).collect();
            pages.push(json!({"name": "Notes", "type": "text", "text": {"format": 1, "content": html}, "sort": 100}));
        }
        docs.push(json!({"name": format!("[{kind_label}] {}", text(e, "name")?), "folder": null, "pages": pages,
            "flags": {"nd-world": {"source": "nd-world", "entity_id": id, "kind": kind}}}));
    }
    for pc in rows("SELECT * FROM player_characters WHERE world_id = ?1 ORDER BY name")? {
        let id = pc.get("id")?.as_i64()?;
        docs.push(crate::apis::pc_foundry_doc_as(&db, &pc, id, false)?);
    }
    let mut out = String::from("[");
    for (i, d) in docs.iter().enumerate() {
        let t = nd_render::py_dumps_indent_as(d, 2, false);
        let indented = t.split('\n').map(|l| format!("    {l}")).collect::<Vec<_>>().join("\n");
        out.push_str(&format!("{}\n{indented}", if i == 0 { "" } else { "," }));
    }
    out.push_str("\n]");
    let mut r = crate::edits::json_response_raw(&out, "application/json")?;
    r.headers_mut().insert("content-disposition", format!("attachment; filename=\"{slug}-foundry.json\"").parse().ok()?);
    Some(r)
}

// ---------------------------------------------------------------------------------------------------------------------------------
// The Training Bundle: `GET /export/training.ndtrain` (app/training_bundle.py is the oracle; docs/TRAINING_BUNDLE_FORMAT.md)

/// `training_bundle._iso`: a stored timestamp (the gateway reads DATETIME columns as {"__dt__": text}) with its first space turned into a T
fn tb_iso(v: Option<&Value>) -> Value {
    let s = match v {
        Some(Value::String(s)) => s.as_str(),
        Some(Value::Object(o)) => match o.get("__dt__").and_then(|x| x.as_str()) {
            Some(s) => s,
            None => return Value::Null,
        },
        _ => return v.cloned().unwrap_or(Value::Null),
    };
    Value::String(s.replacen(' ', "T", 1))
}

/// `training_bundle._loads`: the *_json column parsed; anything unreadable or of the other type is the empty list / object
fn tb_loads(v: Option<&Value>, want_list: bool) -> Value {
    let empty = || if want_list { json!([]) } else { json!({}) };
    let Some(Value::String(raw)) = v else { return empty() };
    if raw.is_empty() {
        return empty();
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(p) if (want_list && p.is_array()) || (!want_list && p.is_object()) => p,
        _ => empty(),
    }
}

/// `training_bundle._csv`: the comma-separated text split, every part stripped, empty parts dropped
fn tb_csv(v: Option<&Value>) -> Value {
    let s = v.and_then(|x| x.as_str()).unwrap_or("");
    Value::Array(s.split(',').map(nd_render::text::py_strip).filter(|p| !p.is_empty()).map(|p| Value::String(p.to_string())).collect())
}

fn tb_bool(v: Option<&Value>) -> Value {
    json!(crate::edits::py_truthy_pub(v))
}

fn tb_line(rec: &Value) -> Option<Vec<u8>> {
    let mut s = serde_json::to_string(rec).ok()?;
    s.push('\n');
    Some(s.into_bytes())
}

/// The members of a world's bundle (without manifest.json), in the order the format lists them; absent = empty
pub fn training_members(db: &Connection, world: &Map<String, Value>) -> Option<Vec<(&'static str, Vec<u8>)>> {
    let wid = world.get("id")?.as_i64()?;
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare_cached(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let pick = |m: &Map<String, Value>, k: &str| m.get(k).cloned().unwrap_or(Value::Null);
    let jsonl = |recs: Vec<Value>| -> Option<Vec<u8>> {
        let mut out = Vec::new();
        for r in &recs {
            out.extend(tb_line(r)?);
        }
        Some(out)
    };
    let mut out: Vec<(&'static str, Vec<u8>)> = Vec::new();

    let templates: Vec<Value> = rows("SELECT id, slug, name, kind, fields_json FROM entity_templates WHERE world_id = ?1 OR world_id IS NULL ORDER BY id")?
        .iter().map(|t| json!({"id": pick(t, "id"), "slug": pick(t, "slug"), "name": pick(t, "name"), "kind": pick(t, "kind"),
                               "fields": tb_loads(t.get("fields_json"), true)})).collect();
    let world_doc = json!({"id": wid, "name": pick(world, "name"), "slug": pick(world, "slug"), "description": pick(world, "description"), "templates": templates});
    out.push(("world.json", format!("{}\n", nd_render::py_dumps_indent_as(&world_doc, 2, false)).into_bytes()));

    let ents: Vec<Value> = rows("SELECT e.id, e.kind, e.subtype, e.name, e.aliases, e.folder, e.tags, e.summary, e.body, t.slug AS template, \
                                 e.custom_fields_json, e.roleplay_personality, e.visible_to_players, e.group_ids_json, e.rag_priority, e.updated_at \
                                 FROM entities e LEFT JOIN entity_templates t ON t.id = e.template_id WHERE e.world_id = ?1 ORDER BY e.id")?
        .iter().map(|e| json!({"id": pick(e, "id"), "kind": pick(e, "kind"), "subtype": pick(e, "subtype"), "name": pick(e, "name"),
            "aliases": tb_csv(e.get("aliases")), "folder": pick(e, "folder"), "tags": tb_csv(e.get("tags")), "summary": pick(e, "summary"),
            "body": pick(e, "body"), "template": pick(e, "template"), "custom_fields": tb_loads(e.get("custom_fields_json"), false),
            "roleplay_personality": pick(e, "roleplay_personality"), "visible_to_players": tb_bool(e.get("visible_to_players")),
            "group_ids": tb_loads(e.get("group_ids_json"), true), "rag_priority": tb_bool(e.get("rag_priority")),
            "updated_at": tb_iso(e.get("updated_at"))})).collect();
    out.push(("entities.jsonl", jsonl(ents)?));

    let notes: Vec<Value> = rows("SELECT n.id, n.entity_id, n.content, n.visible_to_players, n.updated_at FROM entity_notes n \
                                  JOIN entities e ON e.id = n.entity_id WHERE e.world_id = ?1 ORDER BY n.id")?
        .iter().map(|n| json!({"id": pick(n, "id"), "entity_id": pick(n, "entity_id"), "content": pick(n, "content"),
                               "visible_to_players": tb_bool(n.get("visible_to_players")), "updated_at": tb_iso(n.get("updated_at"))})).collect();
    if !notes.is_empty() {
        out.push(("entity_notes.jsonl", jsonl(notes)?));
    }

    let mut rels: Vec<Value> = Vec::new();
    for r in rows("SELECT r.source_id, s.name AS sn, r.target_id, t.name AS tn, r.relation FROM entity_relations r \
                   JOIN entities s ON s.id = r.source_id JOIN entities t ON t.id = r.target_id WHERE r.world_id = ?1 ORDER BY r.id")? {
        let rel = match r.get("relation").and_then(|v| v.as_str()) { Some(s) if !s.is_empty() => s.to_string(), _ => "related".to_string() };
        rels.push(json!({"id": rels.len() + 1, "source_id": pick(&r, "source_id"), "source_name": pick(&r, "sn"), "target_id": pick(&r, "target_id"),
                         "target_name": pick(&r, "tn"), "relation": rel}));
    }
    for r in rows("SELECT l.source_id, s.name AS sn, l.target_id, t.name AS tn FROM entity_links l \
                   JOIN entities s ON s.id = l.source_id JOIN entities t ON t.id = l.target_id \
                   WHERE s.world_id = ?1 AND t.world_id = ?1 ORDER BY l.source_id, l.target_id")? {
        rels.push(json!({"id": rels.len() + 1, "source_id": pick(&r, "source_id"), "source_name": pick(&r, "sn"), "target_id": pick(&r, "target_id"),
                         "target_name": pick(&r, "tn"), "relation": "related"}));
    }
    if !rels.is_empty() {
        out.push(("relations.jsonl", jsonl(rels)?));
    }

    let rules = crate::retrieval::world_rules_markdown(world);
    if !nd_render::text::py_strip(&rules).is_empty() {
        out.push(("rules.md", format!("{}\n", rules.trim_end_matches('\n')).into_bytes()));
    }

    let sheets: Vec<Value> = rows("SELECT id, name, rules_md FROM sheet_templates WHERE world_id = ?1 AND TRIM(COALESCE(rules_md, '')) <> '' ORDER BY id")?
        .iter().map(|s| json!({"id": pick(s, "id"), "template": pick(s, "name"), "rules_md": pick(s, "rules_md")})).collect();
    if !sheets.is_empty() {
        out.push(("rules_sheets.jsonl", jsonl(sheets)?));
    }

    let sessions: Vec<Value> = rows("SELECT id, session_num, session_date, title, summary, npcs_json, loot_json FROM game_sessions WHERE world_id = ?1 ORDER BY id")?
        .iter().map(|s| json!({"id": pick(s, "id"), "session_num": pick(s, "session_num"), "session_date": pick(s, "session_date"), "title": pick(s, "title"),
            "summary": pick(s, "summary"), "npcs": tb_loads(s.get("npcs_json"), true), "loot": tb_loads(s.get("loot_json"), true)})).collect();
    if !sessions.is_empty() {
        out.push(("sessions.jsonl", jsonl(sessions)?));
    }

    let facts: Vec<Value> = rows("SELECT id, game_session_id, content, visible_to_players, tags FROM facts WHERE world_id = ?1 ORDER BY id")?
        .iter().map(|f| json!({"id": pick(f, "id"), "game_session_id": pick(f, "game_session_id"), "content": pick(f, "content"),
                               "visible_to_players": tb_bool(f.get("visible_to_players")), "tags": pick(f, "tags")})).collect();
    if !facts.is_empty() {
        out.push(("facts.jsonl", jsonl(facts)?));
    }

    let quests: Vec<Value> = rows("SELECT id, title, status, category, summary, body, linked_entities_json, parent_id FROM quests WHERE world_id = ?1 ORDER BY id")?
        .iter().map(|q| json!({"id": pick(q, "id"), "title": pick(q, "title"), "status": pick(q, "status"), "category": pick(q, "category"),
            "summary": pick(q, "summary"), "body": pick(q, "body"), "linked_entities": tb_loads(q.get("linked_entities_json"), true),
            "parent_id": pick(q, "parent_id")})).collect();
    if !quests.is_empty() {
        out.push(("quests.jsonl", jsonl(quests)?));
    }

    let tables: Vec<Value> = rows("SELECT id, name, category, description, entries_json FROM random_tables WHERE world_id = ?1 ORDER BY id")?
        .iter().map(|t| json!({"id": pick(t, "id"), "name": pick(t, "name"), "category": pick(t, "category"), "description": pick(t, "description"),
                               "entries": tb_loads(t.get("entries_json"), true)})).collect();
    if !tables.is_empty() {
        out.push(("tables.jsonl", jsonl(tables)?));
    }

    let instr: Vec<Value> = rows("SELECT id, title, content, applies_to_players FROM ai_instructions WHERE world_id = ?1 AND enabled = 1 ORDER BY id")?
        .iter().map(|i| json!({"id": pick(i, "id"), "title": pick(i, "title"), "content": pick(i, "content"),
                               "applies_to_players": tb_bool(i.get("applies_to_players"))})).collect();
    if !instr.is_empty() {
        out.push(("ai_instructions.jsonl", jsonl(instr)?));
    }

    let chars: Vec<Value> = rows("SELECT id, name, race, char_class, level, backstory, notes, equipment_json, feats_json FROM player_characters WHERE world_id = ?1 ORDER BY id")?
        .iter().map(|c| json!({"id": pick(c, "id"), "name": pick(c, "name"), "race": pick(c, "race"), "char_class": pick(c, "char_class"),
            "level": pick(c, "level"), "backstory": pick(c, "backstory"), "notes": pick(c, "notes"),
            "equipment": tb_loads(c.get("equipment_json"), true), "feats": tb_loads(c.get("feats_json"), true)})).collect();
    if !chars.is_empty() {
        out.push(("characters.jsonl", jsonl(chars)?));
    }
    // examples written by this world's own AI model (teacher/mod.rs), when there are any (Python-free image only: nothing writes them elsewhere)
    if let Some(dir) = db.path().and_then(|p| std::path::Path::new(p).parent().map(|d| d.to_path_buf())) {
        if let Some(t) = crate::teacher::gen::bundle_member(&dir, wid) {
            out.push(("teacher_examples.jsonl", t));
        }
    }
    Some(out)
}

/// The finished zip: manifest first (it lists every other member's SHA-256), fixed timestamps, deflate level 6
pub fn training_zip(world: &Map<String, Value>, members: Vec<(&'static str, Vec<u8>)>, build: &str, created: &str) -> Option<Vec<u8>> {
    use sha2::{Digest, Sha256};
    let mut files = Map::new();
    for (name, data) in &members {
        let mut info = Map::new();
        info.insert("sha256".into(), json!(format!("{:x}", Sha256::digest(data))));
        info.insert("bytes".into(), json!(data.len()));
        if name.ends_with(".jsonl") {
            info.insert("records".into(), json!(data.split(|b| *b == b'\n').filter(|l| !l.iter().all(|c| c.is_ascii_whitespace())).count()));
        }
        files.insert((*name).to_string(), Value::Object(info));
    }
    let manifest = json!({"format": "nd-training-bundle", "version": 1, "created": created, "nd_build": build,
        "world": {"id": world.get("id")?, "name": world.get("name")?, "slug": world.get("slug")?, "description": world.get("description").cloned().unwrap_or(Value::Null)},
        "files": files});
    let manifest_bytes = format!("{}\n", nd_render::py_dumps_indent_as(&manifest, 2, false)).into_bytes();
    let opts = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .compression_level(Some(6))
        .last_modified_time(zip::DateTime::from_date_and_time(1980, 1, 1, 0, 0, 0).ok()?)
        .unix_permissions(0o644);
    let mut w = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    w.start_file("manifest.json", opts).ok()?;
    w.write_all(&manifest_bytes).ok()?;
    for (name, data) in &members {
        w.start_file(*name, opts).ok()?;
        w.write_all(data).ok()?;
    }
    Some(w.finish().ok()?.into_inner())
}

/// `GET /export/training.ndtrain` (app/main.py `export_training_bundle`): the active world's Training Bundle. GM only (the auth gate's
/// refusal for anyone else is Python's); a world-less session is Python's 404.
pub fn training_bundle(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let world = p.world.clone()?;
    let slug = world.get("slug").and_then(|v| v.as_str())?.to_string();
    let members = training_members(&db, &world)?;
    let (y, m, d) = civil_from_days((now / 86400) as i64);
    let secs = now % 86400;
    let created = format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", secs / 3600, (secs % 3600) / 60, secs % 60);
    let build = nd_render::build_info();
    let build = build.get_attr("build").ok().and_then(|v| v.as_str().map(str::to_string)).unwrap_or_else(|| "dev".to_string());
    let zip = training_zip(&world, members, &build, &created)?;
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/zip")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{slug}.ndtrain\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(zip))
        .ok()
}
