//! The character hub's read tabs (app/routers/character_hub.py): `GET /api/characters/<id>/hub/quests` and `/hub/world`, every answer
//! (refusals included) as the FastAPI routes give it. Seen as the PLAYER who owns the character, also when a GM looks in.
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;

const LIST_CAP: i64 = 25;

/// (tab, character id) of a hub read path FastAPI reads the same way (plain digits, no leading zero)
fn route(path: &str) -> Option<(&str, i64)> {
    let rest = path.strip_prefix("/api/characters/")?;
    let (id, tab) = rest.split_once("/hub/")?;
    if id.is_empty() || id.len() > 15 || !id.bytes().all(|b| b.is_ascii_digit()) || (id.len() > 1 && id.starts_with('0')) {
        return None;
    }
    matches!(tab, "quests" | "world").then(|| Some((tab, id.parse().ok()?)))?
}

pub fn serves(path: &str) -> bool {
    route(path).is_some()
}

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

fn int(m: &Map<String, Value>, k: &str) -> Option<i64> {
    m.get(k).and_then(|v| v.as_i64())
}

fn text(m: &Map<String, Value>, k: &str) -> String {
    m.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

/// `datetime.isoformat()` of a stored DATETIME, or null
fn iso(v: Option<&Value>) -> Value {
    let raw = v.and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str());
    match raw.and_then(nd_render::pyvalue::PyDateTime::parse) {
        Some(dt) => json!(dt.isoformat()),
        None => Value::Null,
    }
}

pub fn get(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (tab, id) = route(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    // _hub_pc: the owner, or a global GM looking in; anyone else: the same 404 as no such character
    let Some(pc) = rows(&db, "SELECT * FROM player_characters WHERE id = ?1", &[&id])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let is_owner = int(&pc, "owner_user_id") == Some(p.user_id);
    if !(is_owner || p.is_gm) {
        return Some(crate::idpages::refusal(404));
    }
    let wid = int(&pc, "world_id")?;
    let Some(world) = rows(&db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    if !p.is_gm && db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_err() {
        return Some(crate::idpages::refusal(404));
    }
    let out = match tab {
        "quests" => quests(&db, &pc, &world)?,
        _ => known_world(&db, &p, uri, &pc, &world, is_owner)?,
    };
    crate::edits::json_response(&out)
}

fn with_world(path: &str, world: &Map<String, Value>) -> String {
    format!("{path}{}w={}", if path.contains('?') { "&" } else { "?" }, text(world, "slug"))
}

/// `_player_level(world, section)`: the PLAYER role's level for a section of this world
fn player_level(world: &Map<String, Value>, section: &str) -> Option<&'static str> {
    let s = |k: &str| world.get(k).and_then(|v| v.as_str());
    nd_perm::sections::section_level(false, Some("player"), s("section_access_json"), s("custom_kinds_json"), section)
}

fn quests(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>) -> Option<Value> {
    let wid = int(world, "id")?;
    let pcid = int(pc, "id")?;
    let level = player_level(world, "quests")?;
    // _party_ids: the world's parties listing this character (parties_for_pc, by name)
    let mut party_ids = vec![];
    for party in rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id", &[&wid])? {
        if crate::edits::member_ids(party.get("member_pc_ids_json"))?.contains(&pcid) {
            party_ids.push(int(&party, "id")?);
        }
    }
    let mut quests: Vec<(bool, bool, String, Value)> = vec![];
    if level != "none" {
        let qs = rows(db, "SELECT * FROM quests WHERE world_id = ?1 AND visible_to_players IS NOT 0 ORDER BY title, id", &[&wid])?;
        let parties = rows(db, "SELECT id, name FROM parties WHERE world_id = ?1", &[&wid])?;
        let (mut total, mut done): (Vec<(i64, i64)>, Vec<(i64, i64)>) = (vec![], vec![]);
        let bump = |v: &mut Vec<(i64, i64)>, k: i64| match v.iter_mut().find(|x| x.0 == k) {
            Some(x) => x.1 += 1,
            None => v.push((k, 1)),
        };
        for q in &qs {
            if let Some(pid) = int(q, "parent_id").filter(|i| *i != 0) {
                bump(&mut total, pid);
                if text(q, "status") == "complete" {
                    bump(&mut done, pid);
                }
            }
        }
        for q in &qs {
            if int(q, "parent_id").map_or(false, |i| i != 0) {
                continue;
            }
            let qid = int(q, "id")?;
            let status = Some(text(q, "status")).filter(|s| !s.is_empty()).unwrap_or_else(|| "active".into());
            let assigned = int(q, "assigned_party_id");
            let mine = assigned.map_or(false, |a| party_ids.contains(&a));
            let party = assigned.and_then(|a| parties.iter().find(|p| int(p, "id") == Some(a))).and_then(|p| p.get("name").cloned()).unwrap_or(Value::Null);
            let get = |v: &Vec<(i64, i64)>| v.iter().find(|x| x.0 == qid).map_or(0, |x| x.1);
            let title = q.get("title").and_then(|v| v.as_str())?.to_string();
            let item = json!({"id": qid, "title": title, "status": status,
                "category": Some(text(q, "category")).filter(|s| !s.is_empty()).unwrap_or_else(|| "main".into()),
                "summary": text(q, "summary"), "party": party, "mine": mine, "subs_done": get(&done), "subs_total": get(&total),
                "href": with_world(&format!("/quests/{qid}"), world)});
            quests.push((status != "active", !mine, title.to_lowercase(), item));
        }
        quests.sort_by(|a, b| (a.0, a.1, &a.2).cmp(&(b.0, b.1, &b.2)));
    }
    let mut goals = rows(db, "SELECT * FROM character_journal_entries WHERE character_id = ?1 AND kind = 'goal' ORDER BY created_at DESC, id", &[&pcid])?;
    goals.sort_by_key(|g| text(g, "status") != "active");
    let goals: Vec<Value> = goals.iter().map(|e| json!({"id": e.get("id"), "kind": e.get("kind"), "title": text(e, "title"), "body": text(e, "body"),
        "status": text(e, "status"), "session_id": e.get("session_id"), "session": null,
        "created_at": iso(e.get("created_at")), "updated_at": iso(e.get("updated_at"))})).collect();
    Some(json!({"section": level, "quests": quests.into_iter().map(|q| q.3).collect::<Vec<_>>(),
        "quests_href": if level != "none" { json!(with_world("/quests", world)) } else { Value::Null },
        "goals": goals, "goal_statuses": ["active", "achieved", "failed", "dropped"]}))
}

fn known_world(db: &Connection, p: &PageCtx, uri: &Uri, pc: &Map<String, Value>, world: &Map<String, Value>, is_owner: bool) -> Option<Value> {
    let wid = int(world, "id")?;
    let viewer = int(pc, "owner_user_id");
    // whose eyes: the owner's (deps.filter_visible_entities - none for a GM owner), or the owning player's for a GM looking in
    let base = if is_owner && p.is_gm {
        String::new()
    } else if let Some(v) = viewer {
        format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {v}))")
    } else {
        " AND visible_to_players IS NOT 0".to_string()
    };
    let shared = viewer.unwrap_or(-1);
    let revealed = rows(db, &format!("SELECT * FROM entities WHERE world_id = ?1{base} AND visible_to_players IS 0 AND id IN \
        (SELECT entity_id FROM entity_player_access WHERE user_id = ?2) ORDER BY updated_at DESC, id LIMIT {LIST_CAP}"), &[&wid, &shared])?;
    let term: String = nd_render::text::py_strip(&crate::pages::query_param(uri, "q").unwrap_or_default()).chars().take(80).collect();
    let (mut results, mut recent) = (vec![], vec![]);
    if !term.is_empty() {
        let like = format!("%{}%", term.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_"));
        let l = |c: &str| format!("lower({c}) LIKE lower(?2) ESCAPE '\\'");
        results = rows(db, &format!("SELECT * FROM entities WHERE world_id = ?1{base} AND ({} OR {} OR {} OR {}) ORDER BY name, id LIMIT {LIST_CAP}",
            l("name"), l("aliases"), l("tags"), l("summary")), &[&wid, &like])?;
    } else {
        recent = rows(db, &format!("SELECT * FROM entities WHERE world_id = ?1{base} AND visible_to_players IS NOT 0 ORDER BY updated_at DESC, id LIMIT 12"), &[&wid])?;
    }
    let out = |list: Vec<Map<String, Value>>| -> Option<Vec<Value>> {
        list.iter().map(|e| {
            let summary = nd_perm::gmonly::strip_gm_only(&text(e, "summary"), None, nd_render::now_minute())?;
            Some(json!({"id": e.get("id"), "name": e.get("name"), "kind": e.get("kind"), "summary": summary,
                "href": with_world(&format!("/entity/{}", int(e, "id")?), world)}))
        }).collect()
    };
    Some(json!({"q": term, "revealed": out(revealed)?, "recent": out(recent)?, "results": out(results)?}))
}
