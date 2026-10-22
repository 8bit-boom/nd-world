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

/// The finished tile pyramid of a map picture (`map_tiles.meta_for` when one exists): Some(meta), or None when there is none yet -
/// Python then queues one to be cut, so the page is Python's
fn tiles_meta(cfg: &Config, slug: &str, image_url: &str) -> Option<Value> {
    let src = match image_url.strip_prefix("/static/") {
        Some(rest) => cfg.static_dir.join(rest),
        None => cfg.db_path.parent()?.join("uploads").join(image_url.strip_prefix("/uploads/")?),
    };
    let size = std::fs::metadata(&src).ok()?.len();
    let mut head = vec![0u8; 65536];
    let mut f = std::fs::File::open(&src).ok()?;
    let mut n = 0;
    loop {
        let k = std::io::Read::read(&mut f, &mut head[n..]).ok()?;
        if k == 0 {
            break;
        }
        n += k;
        if n == head.len() {
            break;
        }
    }
    let mut crc = flate2::Crc::new();
    crc.update(&head[..n]);
    let key = format!("{slug}-{size:x}{:08x}", crc.sum());
    for base in [cfg.static_dir.join("map_tiles"), cfg.db_path.parent()?.join("map_tiles")] {
        let meta = base.join(&key).join("meta.json");
        if meta.is_file() {
            // a meta.json Python cannot read gives no tiles there; here the page is simply Python's
            return serde_json::from_str(&std::fs::read_to_string(meta).ok()?).ok();
        }
    }
    None
}

/// `GET /maps/<slug>` (app/main.py `map_viewer`)
pub fn map_viewer(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let slug = uri.path().strip_prefix("/maps/")?;
    if slug.is_empty() || slug == "new" || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return None;
    }
    let jf = maps_dir(cfg)?.join(format!("{slug}.json"));
    let data: Value = serde_json::from_str(&std::fs::read_to_string(&jf).ok()?).ok()?;
    let Value::Object(data) = data else { return None };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let owner = match data.get("world_id") {
        None => 1,
        Some(v) => v.as_i64()?,
    };
    if owner != wid || !section_ok(&p, Some(&world), "maps")? {
        return None;
    }
    let image_url = map_image(cfg, slug)?;
    let tiles = match &image_url {
        Some(u) => tiles_meta(cfg, slug, u)?,
        None => Value::Null,
    };
    // the map's overlay row: a map seen for the first time gets one from Python
    let overlay = db.query_row("SELECT * FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    // {lowercased name: id} of the entities the viewer may see
    let vis = if p.is_gm { "" } else { " AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))" };
    let mut st = db.prepare(&format!("SELECT name, id FROM entities WHERE world_id = ?1{vis} ORDER BY id")).ok()?;
    let mut names = Map::new();
    let rows: Vec<(String, i64)> = if p.is_gm {
        st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?
    } else {
        st.query_map([wid, p.user_id], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?
    };
    for (n, id) in rows {
        names.insert(n.to_lowercase(), json!(id));
    }
    let mut st = db.prepare("SELECT slug, name FROM schematics WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let schematics: Vec<Value> = st
        .query_map([wid], |r| Ok(json!({"slug": r.get::<_, String>(0)?, "name": r.get::<_, String>(1)?})))
        .ok()?
        .collect::<Result<_, _>>()
        .ok()?;
    let mut st = db.prepare("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let parties = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut payload = vec![];
    for party in &parties {
        payload.push(json!({"id": party.get("id")?, "name": party.get("name")?, "member_count": crate::pages::party_member_count(&db, &p, party, wid)?}));
    }
    let mut pins = vec![];
    let mut by_id = parties.clone();
    by_id.sort_by_key(|x| x.get("id").and_then(|v| v.as_i64()));
    for party in &by_id {
        let raw = party.get("location_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
        let Value::Object(loc) = serde_json::from_str::<Value>(raw).ok()? else { return None };
        if loc.get("kind").and_then(|v| v.as_str()) != Some("map") || loc.get("slug").and_then(|v| v.as_str()) != Some(slug) {
            continue;
        }
        pins.push(json!({"id": party.get("id")?, "name": party.get("name")?, "member_count": crate::pages::party_member_count(&db, &p, party, wid)?,
                         "lat": loc.get("lat").cloned().unwrap_or(Value::Null), "lng": loc.get("lng").cloned().unwrap_or(Value::Null)}));
    }
    let c = &mut p.ctx;
    c.insert("map_data".into(), Value::Object(data));
    c.insert("image_url".into(), json!(image_url.unwrap_or_default()));
    c.insert("slug".into(), json!(slug));
    c.insert("map_tiles".into(), tiles);
    c.insert("overlay".into(), Value::Object(overlay));
    c.insert("ename_map".into(), json!(nd_render::py_dumps(&Value::Object(names))));
    c.insert("is_gm".into(), json!(p.is_gm));
    c.insert("schematics_json".into(), json!(nd_render::py_dumps(&Value::Array(schematics))));
    c.insert("world_parties_json".into(), json!(nd_render::py_dumps(&Value::Array(payload))));
    c.insert("party_pins_json".into(), json!(nd_render::py_dumps(&Value::Array(pins))));
    render_with(pages, "map_viewer.html", p.ctx, headers)
}
