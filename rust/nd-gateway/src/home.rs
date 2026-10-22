//! `GET /` (app/main.py `home`): the world's dashboard. Queries mirror the SQL Python's ORM sends (same ORDER BY, no extra tie-breakers),
//! so rows tied on a sort key come back in the same order. `None` = not sure: the page goes to Python.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use rusqlite::{Connection, ToSql};
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::{render_with, Pages};

fn rows(db: &Connection, sql: &str, params: &[&dyn ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
    r
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `with_world`: the link pinned to this world (?w= / &w=)
fn with_world(path: &str, slug: &str) -> String {
    format!("{path}{}w={slug}", if path.contains('?') { "&" } else { "?" })
}

/// `int(ref)` for a stored link target: Ok(Some(n)), Ok(None) when Python's int() raises ValueError/TypeError, Err when Python would read
/// it some way this port does not (signs, spaces, underscores, other scripts' digits, floats, bools)
fn ref_int(v: &Value) -> Result<Option<i64>, ()> {
    match v {
        Value::Number(n) if n.is_i64() => Ok(n.as_i64()),
        Value::String(s) if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) => s.parse().map(Some).map_err(|_| ()),
        Value::String(s) if !s.chars().any(|c| c.is_numeric()) && !s.contains(['+', '-', '_']) => Ok(None),
        Value::Null => Ok(None),
        _ => Err(()),
    }
}

/// `_resolve_home_link_href`: Ok(Some(href)) / Ok(None) for a link whose target is gone; Err = Python's call
fn link_href(db: &Connection, cfg: &Config, wid: i64, kinds: &[Value], link: &Map<String, Value>, is_gm: bool) -> Result<Option<String>, ()> {
    let target = link.get("target_type").and_then(|v| v.as_str());
    let raw = link.get("target_ref").filter(|v| truthy(v)).cloned().unwrap_or(json!(""));
    let in_world = |table: &str, id: i64| -> Result<bool, ()> {
        let w: Option<i64> = db.query_row(&format!("SELECT world_id FROM {table} WHERE id = ?1"), [id], |r| r.get(0)).ok();
        Ok(w == Some(wid))
    };
    let by_id = |table: &str, prefix: &str| -> Result<Option<String>, ()> {
        match ref_int(&raw)? {
            Some(id) if in_world(table, id)? => Ok(Some(format!("{prefix}{id}"))),
            _ => Ok(None),
        }
    };
    match target {
        Some("entity") => by_id("entities", "/entity/"),
        Some("session") => by_id("game_sessions", "/sessions/"),
        Some("quest") => by_id("quests", "/quests/"),
        Some("board") | Some("schematic") | Some("map") | Some("kind") | Some("url") => {
            let Some(r) = raw.as_str() else { return Err(()) };
            match target {
                Some("board") => {
                    let n: i64 = db.query_row("SELECT count(*) FROM invest_boards WHERE slug = ?1 AND world_id = ?2", rusqlite::params![r, wid], |x| x.get(0)).map_err(|_| ())?;
                    Ok((n > 0).then(|| format!("/boards/{r}")))
                }
                Some("schematic") => {
                    let n: i64 = db.query_row("SELECT count(*) FROM schematics WHERE slug = ?1 AND world_id = ?2", rusqlite::params![r, wid], |x| x.get(0)).map_err(|_| ())?;
                    Ok((n > 0).then(|| if is_gm { format!("/maps/schematic/{r}") } else { format!("/maps/schematic/{r}/view") }))
                }
                Some("map") => {
                    let maps = crate::maps::world_maps(cfg, wid).ok_or(())?;
                    Ok(maps.iter().any(|(slug, _)| slug == r).then(|| format!("/maps/{r}")))
                }
                Some("kind") => Ok(kinds.iter().any(|k| k.as_str() == Some(r)).then(|| format!("/kind/{r}"))),
                _ => Ok((r.starts_with("http://") || r.starts_with("https://") || r.starts_with('/')).then(|| r.to_string())),
            }
        }
        _ => Ok(None),
    }
}

fn stored_list(world: &Map<String, Value>, col: &str) -> Vec<Value> {
    let raw = world.get(col).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => a,
        _ => vec![],
    }
}

pub fn home(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p: PageCtx = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let slug = world.get("slug")?.as_str()?.to_string();
    let is_gm = p.is_gm;
    let uid = p.user_id;
    // deps.filter_visible_entities for a non-GM viewer
    let vis = if is_gm { " AND ?2 IS NOT NULL" } else { " AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_player_access.entity_id FROM entity_player_access WHERE entity_player_access.user_id = ?2))" };
    let kinds: Vec<Value> = p.ctx.get("kinds")?.as_array()?.clone();
    let mut counts = Map::new();
    for k in &kinds {
        counts.insert(k.as_str()?.to_string(), json!(0));
    }
    {
        let mut st = db.prepare(&format!("SELECT entities.kind, count(entities.id) FROM entities WHERE entities.world_id = ?1{vis} GROUP BY entities.kind")).ok()?;
        for r in st.query_map(rusqlite::params![wid, uid], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, i64>(1)?))).ok()? {
            let (k, n) = r.ok()?;
            counts.insert(k?, json!(n));
        }
    }
    let recent = rows(&db, &format!("SELECT * FROM entities WHERE entities.world_id = ?1{vis} ORDER BY entities.updated_at DESC LIMIT 8"), &[&wid, &uid])?;
    let mut preview_maps = vec![];
    for (s, d) in crate::maps::world_maps(cfg, wid)?.into_iter().take(6) {
        preview_maps.push(json!({"slug": s, "name": d.get("name").cloned().unwrap_or_else(|| json!(s)), "image_url": crate::maps::map_image(cfg, &s)?}));
    }
    let mut most_linked = vec![];
    for mut e in rows(
        &db,
        &format!(
            "SELECT entities.*, count(entity_links.source_id) AS link_count FROM entities JOIN entity_links ON entity_links.target_id = entities.id \
             WHERE entities.world_id = ?1{vis} GROUP BY entities.id ORDER BY count(entity_links.source_id) DESC LIMIT 6"
        ),
        &[&wid, &uid],
    )? {
        let n = e.remove("link_count")?;
        most_linked.push(json!([Value::Object(e), n]));
    }
    let mut tags: Vec<(String, i64)> = vec![];
    {
        let mut st = db.prepare(&format!("SELECT entities.tags FROM entities WHERE entities.world_id = ?1 AND entities.tags IS NOT NULL{vis}")).ok()?;
        for r in st.query_map(rusqlite::params![wid, uid], |r| r.get::<_, Option<String>>(0)).ok()? {
            for t in r.ok()?.unwrap_or_default().split(',') {
                let t = nd_render::text::py_strip(t);
                if t.is_empty() {
                    continue;
                }
                match tags.iter_mut().find(|(k, _)| k == t) {
                    Some(x) => x.1 += 1,
                    None => tags.push((t.to_string(), 1)),
                }
            }
        }
    }
    tags.sort_by(|a, b| b.1.cmp(&a.1));
    tags.truncate(40);
    let recent_boards = rows(&db, "SELECT * FROM invest_boards WHERE invest_boards.world_id = ?1 ORDER BY invest_boards.updated_at DESC LIMIT 3", &[&wid])?;
    let recent_schematics = rows(&db, "SELECT * FROM schematics WHERE schematics.world_id = ?1 ORDER BY schematics.updated_at DESC LIMIT 3", &[&wid])?;
    let recent_sessions = rows(&db, "SELECT * FROM game_sessions WHERE game_sessions.world_id = ?1 ORDER BY game_sessions.session_num DESC LIMIT 3", &[&wid])?;
    let active_quests = rows(&db, "SELECT * FROM quests WHERE quests.world_id = ?1 AND quests.status = 'active' ORDER BY quests.updated_at DESC LIMIT 3", &[&wid])?;
    // the GM's home sections and pinned tiles, as this viewer may see them
    let shown = |v: &Map<String, Value>| is_gm || v.get("visible_to_players").map_or(true, truthy);
    let mut sections = vec![];
    for sec in stored_list(&world, "home_sections_json") {
        let Some(sec) = sec.as_object() else { continue };
        if !shown(sec) {
            continue;
        }
        let mut links = vec![];
        let raw_links = sec.get("links").filter(|v| truthy(v)).cloned().unwrap_or(json!([]));
        for l in raw_links.as_array()? {
            let Some(l) = l.as_object() else { continue };
            if !shown(l) {
                continue;
            }
            let Some(href) = link_href(&db, cfg, wid, &kinds, l, is_gm).ok()? else { continue };
            links.push(json!({"label": l.get("label").cloned().unwrap_or(json!("")), "icon": l.get("icon").cloned().unwrap_or(json!("")), "href": with_world(&href, &slug)}));
        }
        if !links.is_empty() || is_gm {
            let name = sec.get("name").filter(|v| truthy(v)).cloned().unwrap_or(json!("Untitled"));
            sections.push(json!({"name": name, "links": links}));
        }
    }
    let mut tiles = vec![];
    for t in stored_list(&world, "home_pinned_tiles_json") {
        let Some(t) = t.as_object() else { continue };
        if !shown(t) {
            continue;
        }
        let Some(href) = link_href(&db, cfg, wid, &kinds, t, is_gm).ok()? else { continue };
        let count = if t.get("target_type").and_then(|v| v.as_str()) == Some("kind") {
            match t.get("target_ref") {
                Some(Value::String(r)) => counts.get(r).cloned().unwrap_or(Value::Null),
                None | Some(Value::Null) => Value::Null,
                Some(Value::Number(_)) | Some(Value::Bool(_)) => Value::Null,
                _ => return None, // a list / dict key: Python raises there
            }
        } else {
            Value::Null
        };
        tiles.push(json!({"label": t.get("label").cloned().unwrap_or(json!("")), "icon": t.get("icon").cloned().unwrap_or(json!("")),
                          "href": with_world(&href, &slug), "count": count}));
    }
    let hidden: Vec<Value> = stored_list(&world, "home_hidden_kinds_json").into_iter().filter(|k| k.is_string()).collect();
    let empty = counts.values().map(|v| v.as_i64().unwrap_or(0)).sum::<i64>() == 0;
    let arr = |v: Vec<Map<String, Value>>| Value::Array(v.into_iter().map(Value::Object).collect());
    let c = &mut p.ctx;
    c.insert("counts".into(), Value::Object(counts));
    c.insert("recent".into(), arr(recent));
    c.insert("preview_maps".into(), Value::Array(preview_maps));
    c.insert("most_linked".into(), Value::Array(most_linked));
    c.insert("top_tags".into(), Value::Array(tags.into_iter().map(|(k, n)| json!([k, n])).collect()));
    c.insert("recent_boards".into(), arr(recent_boards));
    c.insert("recent_schematics".into(), arr(recent_schematics));
    c.insert("recent_sessions".into(), arr(recent_sessions));
    c.insert("active_quests".into(), arr(active_quests));
    c.insert("home_sections".into(), Value::Array(sections));
    c.insert("world_is_empty".into(), json!(empty));
    c.insert("pinned_tiles".into(), Value::Array(tiles));
    c.insert("hidden_kinds".into(), Value::Array(hidden));
    render_with(pages, "index.html", p.ctx, headers)
}
