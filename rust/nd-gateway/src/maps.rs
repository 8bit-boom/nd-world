//! Map pages (app/main.py): the image maps stored as JSON files next to the database, and schematic maps in the database.
//! `None` = not sure: the page goes to Python.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{render_with, section_ok, Pages};

fn maps_dir(cfg: &Config) -> Option<PathBuf> {
    Some(cfg.db_path.parent()?.join("maps"))
}

/// `_iter_world_maps`: (slug, data) of the world's map files, by file name. A file Python reads differently (JSON with NaN, a
/// world id that is not a plain integer, a document that is not an object) makes the whole page Python's.
pub fn world_maps(cfg: &Config, wid: i64) -> Option<Vec<(String, Map<String, Value>)>> {
    let dir = maps_dir(cfg)?;
    let Ok(entries) = std::fs::read_dir(&dir) else { return Some(vec![]) };
    let mut files: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().map_or(false, |x| x == "json")).collect();
    files.sort();
    let mut out = vec![];
    for f in files {
        let Some(stem) = f.file_stem().and_then(|s| s.to_str()).map(|s| s.to_string()) else { return None };
        if !f.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&f).ok()?;
        let data: Value = serde_json::from_str(&text).ok()?;
        let Value::Object(data) = data else { return None };
        let owner = match data.get("world_id") {
            None => 1,
            Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
            Some(Value::String(_)) | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => continue,
            _ => return None,
        };
        if owner == wid {
            out.push((stem, data));
        }
    }
    Some(out)
}

/// The picture of a map: bundled (static/maps) first, then uploaded (uploads/maps)
fn map_image(cfg: &Config, slug: &str) -> Option<Option<String>> {
    let uploads = cfg.db_path.parent()?.join("uploads").join("maps");
    let statics = cfg.static_dir.join("maps");
    for ext in [".webp", ".jpg", ".jpeg", ".png", ".avif"] {
        if Path::new(&statics).join(format!("{slug}{ext}")).exists() {
            return Some(Some(format!("/static/maps/{slug}{ext}")));
        }
        if uploads.join(format!("{slug}{ext}")).exists() {
            return Some(Some(format!("/uploads/maps/{slug}{ext}")));
        }
    }
    Some(None)
}

/// `len()` of a JSON value the way Python counts it (None for a type len() refuses)
fn py_len(v: &Value) -> Option<usize> {
    match v {
        Value::Array(a) => Some(a.len()),
        Value::Object(o) => Some(o.len()),
        Value::String(s) => Some(s.chars().count()),
        _ => None,
    }
}

/// `GET /maps` (app/main.py `maps_page`)
pub fn maps_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "maps")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let mut maps = vec![];
    for (slug, data) in world_maps(cfg, wid)? {
        let markers = match data.get("markers") {
            None => 0,
            Some(v) => py_len(v)?,
        };
        maps.push(json!({
            "slug": slug,
            "name": data.get("name").cloned().unwrap_or_else(|| json!(slug)),
            "width": data.get("width").cloned().unwrap_or(json!(0)),
            "height": data.get("height").cloned().unwrap_or(json!(0)),
            "markers": markers,
            "image_url": map_image(cfg, &slug)?,
        }));
    }
    let mut st = db.prepare("SELECT * FROM schematics WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let schematics = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let gallery = if p.is_gm { crate::gallery::all_world_image_urls(&db, wid)? } else { vec![] };
    p.ctx.insert("maps".into(), Value::Array(maps));
    p.ctx.insert("schematics".into(), Value::Array(schematics.into_iter().map(Value::Object).collect()));
    p.ctx.insert("gallery_images".into(), Value::Array(gallery));
    render_with(pages, "maps.html", p.ctx, headers)
}
