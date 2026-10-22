//! The per-world media libraries (documents, audio, video, images): album trees and counts as app/media_albums.py builds them, and the
//! library pages built on them. `None` = not sure: the page goes to Python.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{render_with, section_ok, Pages};

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
    r
}

fn ids(list: &[Map<String, Value>]) -> Option<Vec<i64>> {
    list.iter().map(|r| r.get("id")?.as_i64()).collect()
}

fn in_list(ids: &[i64]) -> String {
    ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",")
}

/// `media_albums.breadcrumb`: the chain of parent albums, root first (at most 50 hops)
pub fn breadcrumb(db: &Connection, table: &str, album: &Map<String, Value>) -> Option<Vec<Value>> {
    let mut chain = vec![];
    let mut parent = album.get("parent_id").and_then(|v| v.as_i64());
    for _ in 0..50 {
        let Some(pid) = parent.filter(|p| *p != 0) else { break };
        let Some(row) = rows(db, &format!("SELECT * FROM {table} WHERE id = ?1"), &[&pid])?.into_iter().next() else { break };
        parent = row.get("parent_id").and_then(|v| v.as_i64());
        chain.push(Value::Object(row));
    }
    chain.reverse();
    Some(chain)
}

/// {album id: count} for `SELECT key, count(id) ... GROUP BY key` over the given album ids, as Python's int-keyed dict
fn grouped_counts(db: &Connection, table: &str, key: &str, album_ids: &[i64], extra: &str) -> Option<Value> {
    let mut m = Map::new();
    if !album_ids.is_empty() {
        let sql = format!("SELECT {key}, count(id) FROM {table} WHERE {key} IN ({}){extra} GROUP BY {key}", in_list(album_ids));
        let mut st = db.prepare(&sql).ok()?;
        for r in st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))).ok()? {
            let (k, n) = r.ok()?;
            m.insert(k.to_string(), json!(n));
        }
    }
    Some(json!({"__intkeys__": Value::Object(m)}))
}

/// `media_albums.sub_album_counts`
pub fn sub_album_counts(db: &Connection, table: &str, album_ids: &[i64]) -> Option<Value> {
    grouped_counts(db, table, "parent_id", album_ids, "")
}

/// `media_albums.child_counts` (a non-GM counts only what they may open)
pub fn child_counts(db: &Connection, table: &str, album_ids: &[i64], visible_only: bool) -> Option<Value> {
    grouped_counts(db, table, "album_id", album_ids, if visible_only { " AND visible_to_players = 1" } else { "" })
}

/// `GET /pages` and `GET /pages/albums/<id>` (app/routers/pages.py `pages_library` / `pages_album_detail`)
pub fn pages_library(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let album_id: Option<i64> = match uri.path().strip_prefix("/pages/albums/") {
        None => None,
        Some(rest) if !rest.is_empty() && rest.len() < 16 && rest.bytes().all(|b| b.is_ascii_digit()) => Some(rest.parse().ok()?),
        Some(_) => return None,
    };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "pages")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let (album, albums, crumbs) = match album_id {
        None => (Value::Null, rows(&db, "SELECT * FROM page_albums WHERE world_id = ?1 AND parent_id IS NULL ORDER BY name, id", &[&wid])?, vec![]),
        Some(aid) => {
            let a = rows(&db, "SELECT * FROM page_albums WHERE id = ?1", &[&aid])?.into_iter().next()?;
            if a.get("world_id")?.as_i64() != Some(wid) {
                return None;
            }
            let subs = rows(&db, "SELECT * FROM page_albums WHERE parent_id = ?1 ORDER BY name, id", &[&aid])?;
            let crumbs = breadcrumb(&db, "page_albums", &a)?;
            (Value::Object(a), subs, crumbs)
        }
    };
    let vis = if p.is_gm { "" } else { " AND visible_to_players = 1" };
    let docs = match album_id {
        None => rows(&db, &format!("SELECT * FROM page_docs WHERE world_id = ?1 AND album_id IS NULL{vis} ORDER BY name, id"), &[&wid])?,
        Some(aid) => rows(&db, &format!("SELECT * FROM page_docs WHERE world_id = ?1 AND album_id = ?2{vis} ORDER BY name, id"), &[&wid, &aid])?,
    };
    let album_ids = ids(&albums)?;
    let doc_ids = ids(&docs)?;
    let max_bytes: i64 = match std::env::var("MAX_PAGE_UPLOAD_BYTES") {
        Ok(v) => v.trim().parse().ok()?,
        Err(_) => 50 * 1024 * 1024,
    };
    let c = &mut p.ctx;
    c.insert("sub_album_counts".into(), sub_album_counts(&db, "page_albums", &album_ids)?);
    c.insert("doc_counts".into(), child_counts(&db, "page_docs", &album_ids, !p.is_gm)?);
    c.insert("sheet_counts".into(), grouped_counts(&db, "character_sheets", "template_id", &doc_ids, "")?);
    c.insert("docs".into(), Value::Array(docs.into_iter().map(Value::Object).collect()));
    c.insert("album".into(), album);
    c.insert("albums".into(), Value::Array(albums.into_iter().map(Value::Object).collect()));
    c.insert("breadcrumb".into(), Value::Array(crumbs));
    c.insert("max_page_mb".into(), json!(max_bytes.div_euclid(1024 * 1024)));
    render_with(pages, "pages_library.html", p.ctx, headers)
}

/// `gallery._file_facts`: (size, mtime) of an /uploads/ file, zeros when it is not one or is not there
pub fn file_facts(uploads: &std::path::Path, url: Option<&str>) -> (i64, i64) {
    let Some(rest) = url.and_then(|u| u.strip_prefix("/uploads/")) else { return (0, 0) };
    let (Ok(root), Ok(path)) = (std::fs::canonicalize(uploads), std::fs::canonicalize(uploads.join(rest))) else { return (0, 0) };
    if !path.starts_with(&root) {
        return (0, 0);
    }
    match std::fs::metadata(&path) {
        Ok(m) if m.is_file() => {
            let mtime = m.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0, |d| d.as_secs() as i64);
            (m.len() as i64, mtime)
        }
        _ => (0, 0),
    }
}

/// `effective_upload_bytes(settings.<col>, env default) // 1 MiB`
fn upload_mb(db: &Connection, col: &str, env: &str, default: i64) -> Option<i64> {
    let saved: Option<i64> = db.query_row(&format!("SELECT {col} FROM app_settings LIMIT 1"), [], |r| r.get(0)).ok()?;
    let bytes = match saved.filter(|v| *v > 0) {
        Some(mb) => mb * 1024 * 1024,
        None => match std::env::var(env) {
            Ok(v) => v.trim().parse().ok()?,
            Err(_) => default,
        },
    };
    Some(bytes.div_euclid(1024 * 1024))
}

pub struct ClipLibrary {
    pub prefix: &'static str,
    pub section: &'static str,
    pub albums: &'static str,
    pub clips: &'static str,
    pub template: &'static str,
    pub max_key: &'static str,
    pub settings_col: &'static str,
    pub env: &'static str,
    pub default_bytes: i64,
}

pub const AUDIO: ClipLibrary = ClipLibrary { prefix: "/audio", section: "audio", albums: "audio_albums", clips: "audio_clips", template: "audio_library.html",
    max_key: "max_audio_mb", settings_col: "max_audio_mb", env: "MAX_AUDIO_UPLOAD_BYTES", default_bytes: 1024 * 1024 * 1024 };
pub const VIDEO: ClipLibrary = ClipLibrary { prefix: "/video", section: "video", albums: "video_albums", clips: "video_clips", template: "video_library.html",
    max_key: "max_video_mb", settings_col: "max_video_mb", env: "MAX_VIDEO_UPLOAD_BYTES", default_bytes: 2 * 1024 * 1024 * 1024 };

/// `GET /audio`, `/audio/albums/<id>`, `/video`, `/video/albums/<id>` (app/routers/audio.py, video.py)
pub fn clip_library(lib: &ClipLibrary, pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let rest = uri.path().strip_prefix(lib.prefix)?;
    let album_id: Option<i64> = match rest {
        "" => None,
        r => {
            let n = r.strip_prefix("/albums/")?;
            if n.is_empty() || n.len() >= 16 || !n.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            Some(n.parse().ok()?)
        }
    };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), lib.section)? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let (albums_t, clips_t) = (lib.albums, lib.clips);
    let (album, albums, crumbs) = match album_id {
        None => (Value::Null, rows(&db, &format!("SELECT * FROM {albums_t} WHERE world_id = ?1 AND parent_id IS NULL ORDER BY name, id"), &[&wid])?, vec![]),
        Some(aid) => {
            let a = rows(&db, &format!("SELECT * FROM {albums_t} WHERE id = ?1"), &[&aid])?.into_iter().next()?;
            if a.get("world_id")?.as_i64() != Some(wid) {
                return None;
            }
            let subs = rows(&db, &format!("SELECT * FROM {albums_t} WHERE parent_id = ?1 ORDER BY name, id"), &[&aid])?;
            let crumbs = breadcrumb(&db, albums_t, &a)?;
            (Value::Object(a), subs, crumbs)
        }
    };
    let vis = if p.is_gm { "" } else { " AND visible_to_players = 1" };
    let clips = match album_id {
        None => rows(&db, &format!("SELECT * FROM {clips_t} WHERE world_id = ?1 AND album_id IS NULL{vis} ORDER BY name, id"), &[&wid])?,
        Some(aid) => rows(&db, &format!("SELECT * FROM {clips_t} WHERE world_id = ?1 AND album_id = ?2{vis} ORDER BY name, id"), &[&wid, &aid])?,
    };
    let uploads = cfg.db_path.parent()?.join("uploads");
    let mut facts = Map::new();
    for c in &clips {
        let (size, mtime) = file_facts(&uploads, c.get("file_url").and_then(|v| v.as_str()));
        let date = match c.get("created_at") {
            Some(Value::Object(o)) => nd_render::pyvalue::PyDateTime::parse(o.get("__dt__")?.as_str()?)?.epoch_secs(),
            _ => mtime,
        };
        facts.insert(c.get("id")?.as_i64()?.to_string(), json!({"size": size, "date": date}));
    }
    let album_ids = ids(&albums)?;
    if album_id.is_none() {
        // the per-clip "attach to an entity" picker: the world's entities the viewer may see, kind then name
        let vis_e = if p.is_gm {
            String::new()
        } else {
            " AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))".to_string()
        };
        let sql = format!("SELECT * FROM entities WHERE world_id = ?1{vis_e} ORDER BY kind, name, id");
        let ents = if p.is_gm { rows(&db, &sql, &[&wid])? } else { rows(&db, &sql, &[&wid, &p.user_id])? };
        p.ctx.insert("entities".into(), Value::Array(ents.into_iter().map(Value::Object).collect()));
    }
    let c = &mut p.ctx;
    c.insert("sub_album_counts".into(), sub_album_counts(&db, albums_t, &album_ids)?);
    c.insert("clip_counts".into(), child_counts(&db, clips_t, &album_ids, !p.is_gm)?);
    c.insert("clip_facts".into(), json!({"__intkeys__": Value::Object(facts)}));
    c.insert("clips".into(), Value::Array(clips.into_iter().map(Value::Object).collect()));
    c.insert("album".into(), album);
    c.insert("albums".into(), Value::Array(albums.into_iter().map(Value::Object).collect()));
    c.insert("breadcrumb".into(), Value::Array(crumbs));
    c.insert(lib.max_key.into(), json!(upload_mb(&db, lib.settings_col, lib.env, lib.default_bytes)?));
    render_with(pages, lib.template, p.ctx, headers)
}
