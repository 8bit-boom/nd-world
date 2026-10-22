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
