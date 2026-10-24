//! The GM's JSON exports of a world's entities: `GET /worlds/<id>/export` (app/main.py `world_export`, the whole world with its
//! header) and `GET /worlds/<id>/export/entities/<kind>.json` (app/routers/export.py `export_entities_by_kind`) - the same text
//! `stream_json_array_download` writes, each entity's image embedded as a data URI.

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
        let mut st = db.prepare(sql).ok()?;
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
