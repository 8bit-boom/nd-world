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
    matches!(tab, "quests" | "world" | "now" | "schedule" | "log" | "notes" | "keepsakes" | "places" | "handouts" | "sessions").then(|| Some((tab, id.parse().ok()?)))?
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
    if tab == "notes" && !is_owner {
        return Some(crate::idpages::refusal(404)); // _owned_pc: the owner only
    }
    let out = match tab {
        "quests" => quests(&db, &pc, &world)?,
        "now" => now_strip(&db, &pc, &world, is_owner, p.user_id)?,
        "schedule" => schedule_tab(&db, &pc, &world, p.user_id)?,
        "log" => change_log(&db, &pc, is_owner)?,
        "notes" => notes_tab(&db, &pc, &world, p.user_id)?,
        "sessions" => sessions_tab(&db, &pc, &world)?,
        "keepsakes" => keepsakes_tab(&db, &pc, &world, p.user_id, p.is_gm, is_owner)?,
        "places" => places_tab(&db, &pc, &world)?,
        "handouts" => handouts_tab(&db, &pc, is_owner)?,
        _ => known_world(&db, &p, uri, &pc, &world, is_owner)?,
    };
    crate::edits::json_response(&out)
}

/// `hub_log`: the newest 60 changes, with which the owner can still undo (`char_extras.can_undo`: nothing later, not undone, changed the
/// same thing)
fn change_log(db: &Connection, pc: &Map<String, Value>, is_owner: bool) -> Option<Value> {
    let pid = int(pc, "id")?;
    let entries = rows(db, "SELECT * FROM character_log WHERE character_id = ?1 ORDER BY id DESC LIMIT 60", &[&pid])?;
    let mut out = vec![];
    for e in &entries {
        let undone = crate::edits::py_truthy_pub(e.get("undone"));
        let can_undo = if is_owner && !undone {
            let spec = crate::edits::log_undo_spec(e);
            if spec.is_empty() {
                false
            } else {
                let key = crate::edits::log_undo_key(&spec)?;
                let mut st = db.prepare("SELECT undo_json FROM character_log WHERE character_id = ?1 AND id > ?2 AND undone = 0").ok()?;
                let later: Vec<Option<String>> = st.query_map([pid, int(e, "id")?], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
                let mut clash = false;
                for raw in later {
                    let mut m = Map::new();
                    m.insert("undo_json".into(), raw.map_or(Value::Null, Value::String));
                    let sp = crate::edits::log_undo_spec(&m);
                    if !sp.is_empty() && crate::edits::log_undo_key(&sp)? == key {
                        clash = true;
                        break;
                    }
                }
                !clash
            }
        } else {
            false
        };
        let at = e.get("created_at").and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str())
            .and_then(nd_render::pyvalue::PyDateTime::parse).and_then(|d| d.strftime("%Y-%m-%dT%H:%M:%SZ").ok()).map(Value::String).unwrap_or(Value::Null);
        let txt = |k: &str| e.get(k).cloned().unwrap_or(Value::Null);
        out.push(json!({"id": int(e, "id")?, "actor": txt("actor"), "kind": txt("kind"), "text": txt("text"), "undone": undone, "at": at,
                        "can_undo": can_undo}));
    }
    Some(json!({"can_edit": is_owner, "entries": out}))
}

/// `hub_sessions`: the world's Session Log as the PLAYERS get it - newest 40, what each has to hear or see (names only)
fn sessions_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>) -> Option<Value> {
    if player_level(world, "session_log")? == "none" {
        return Some(json!({"enabled": false, "sessions": []}));
    }
    let wid = int(pc, "world_id")?;
    let rows_ = rows(db, "SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 40", &[&wid])?;
    let mut with_facts: Vec<i64> = vec![];
    if !rows_.is_empty() {
        let list = rows_.iter().filter_map(|r| int(r, "id")).map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let mut st = db.prepare(&format!("SELECT DISTINCT game_session_id FROM facts WHERE world_id = ?1 AND game_session_id IN ({list})")).ok()?;
        with_facts = st.query_map([wid], |r| r.get::<_, Option<i64>>(0)).ok()?.filter_map(|r| r.ok().flatten()).collect();
    }
    let mut out = vec![];
    for gs in &rows_ {
        let gid = int(gs, "id")?;
        let media = crate::edits::viewer_media(db, gs, false)?;
        let published = crate::edits::py_truthy_pub(gs.get("player_summary_published")) && !nd_render::text::py_strip(&text(gs, "player_summary")).is_empty();
        let recap = if published { "published" } else if with_facts.contains(&gid) { "auto" } else { "" };
        let title = match text(gs, "title") { t if t.is_empty() => "Session".to_string(), t => t };
        let names = |k: &str, f: &dyn Fn(&Value) -> Value| -> Option<Vec<Value>> { Some(media.get(k)?.as_array()?.iter().map(f).collect()) };
        out.push(json!({"id": gid, "num": gs.get("session_num"), "title": title, "date": text(gs, "session_date"), "recap": recap,
            "href": with_world(&format!("/session-log/{gid}"), world),
            "audio": names("audio", &|c| c.get("name").cloned().unwrap_or(Value::Null))?, "videos": names("videos", &|v| v.get("name").cloned().unwrap_or(Value::Null))?,
            "images": names("images", &|i| match i.get("caption").and_then(|c| c.as_str()) { Some(c) if !c.is_empty() => json!(c), _ => json!("Picture") })?}));
    }
    Some(json!({"enabled": true, "sessions": out}))
}

/// `_excerpt(text)`: tags out, markdown stripped, 280 characters at most (279 and an ellipsis)
fn excerpt(text: &str) -> String {
    static TAG: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let plain = nd_render::text::strip_md(&TAG.get_or_init(|| regex::Regex::new(r"<[^>]+>").unwrap()).replace_all(text, " "));
    if plain.chars().count() <= 280 {
        return plain;
    }
    let cut: String = plain.chars().take(279).collect();
    format!("{}\u{2026}", cut.trim_end())
}

/// `hub_notes`: the GM's private notes to this player (excerpts and the thread link) and the character's journal
fn notes_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>, user_id: i64) -> Option<Value> {
    let wid = int(world, "id")?;
    let notes = rows(db, "SELECT * FROM private_notes WHERE world_id = ?1 AND player_user_id = ?2 ORDER BY updated_at DESC LIMIT 50", &[&wid, &user_id])?;
    let party = party_ids_of(db, pc)?;
    let sessions = if party.is_empty() {
        vec![]
    } else {
        let list = party.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        rows(db, &format!("SELECT * FROM game_sessions WHERE world_id = ?1 AND party_id IN ({list}) ORDER BY session_num DESC LIMIT 60"), &[&int(pc, "world_id")?])?
    };
    let pid = int(pc, "id")?;
    let entries = rows(db, "SELECT * FROM character_journal_entries WHERE character_id = ?1 AND kind = 'journal' ORDER BY created_at DESC", &[&pid])?;
    let mut gm_notes = vec![];
    for n in &notes {
        gm_notes.push(json!({"id": int(n, "id")?, "title": text(n, "title"), "excerpt": excerpt(&text(n, "content")),
                             "updated_at": crate::edits::py_isoformat_pub(n.get("updated_at"))}));
    }
    let href = if notes.is_empty() { Value::Null } else { json!(with_world(&format!("/worlds/{wid}/notes/{user_id}"), world)) };
    let label = |s: &Map<String, Value>| -> String {
        let num = s.get("session_num").and_then(|v| v.as_i64()).map_or("None".to_string(), |n| n.to_string());
        let title = s.get("title").and_then(|v| v.as_str()).unwrap_or("None");
        format!("#{num} {title}")
    };
    let mut sess = vec![];
    for s in &sessions {
        sess.push(json!({"id": int(s, "id")?, "label": label(s)}));
    }
    Some(json!({"gm_notes": gm_notes, "thread_href": href,
                "journal": entries.iter().map(|e| crate::edits::hub_entry_out(e, &sessions)).collect::<Vec<_>>(), "sessions": sess}))
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


/// `next_confirmed(db, world)`: (plan, slot) of the next confirmed session that has not ended yet
fn next_confirmed(db: &Connection, wid: i64) -> Option<Option<(Map<String, Value>, Map<String, Value>)>> {
    let now = crate::schedule::now_micros();
    let mut best: Option<(Map<String, Value>, Map<String, Value>, i64)> = None;
    for plan in rows(db, "SELECT * FROM session_plans WHERE world_id = ?1 AND status = 'confirmed' ORDER BY id", &[&wid])? {
        let Some(sid) = int(&plan, "confirmed_slot_id").filter(|i| *i != 0) else { continue };
        let Some(slot) = rows(db, "SELECT * FROM session_plan_slots WHERE id = ?1", &[&sid])?.into_iter().next() else { continue };
        if int(&slot, "plan_id") != int(&plan, "id") {
            continue;
        }
        let start = crate::schedule::stored(slot.get("starts_at"))?;
        let dur = int(&plan, "duration_min").filter(|d| *d != 0).unwrap_or(180);
        if start + dur * 60_000_000 < now {
            continue;
        }
        if best.as_ref().map_or(true, |b| start < b.2) {
            best = Some((plan, slot, start));
        }
    }
    Some(best.map(|(p, s, _)| (p, s)))
}

/// `polls_waiting(db, world, user)`: open polls with slots where this person has not answered any
fn polls_waiting(db: &Connection, wid: i64, uid: i64) -> Option<i64> {
    let mut n = 0;
    for plan in rows(db, "SELECT id FROM session_plans WHERE world_id = ?1 AND status = 'open' ORDER BY id", &[&wid])? {
        let pid = int(&plan, "id")?;
        let slots: i64 = db.query_row("SELECT count(*) FROM session_plan_slots WHERE plan_id = ?1", [pid], |r| r.get(0)).ok()?;
        if slots == 0 {
            continue;
        }
        let answered = db.query_row("SELECT 1 FROM session_plan_votes WHERE user_id = ?1 AND slot_id IN (SELECT id FROM session_plan_slots WHERE plan_id = ?2) LIMIT 1",
            [uid, pid], |_| Ok(())).is_ok();
        if !answered {
            n += 1;
        }
    }
    Some(n)
}

/// `hub_now`: the strip over every hub tab - the character's live vitals, the live encounter, the next game night, rests, new handouts,
/// pinned pages, companions and polls waiting
fn now_strip(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>, is_owner: bool, user_id: i64) -> Option<Value> {
    let pid = int(pc, "id")?;
    let wid = int(world, "id")?;
    let v = crate::edits::member_vitals(db, std::slice::from_ref(pc), None)?.into_iter().next()?;
    let (_, shock, _, _, native) = crate::edits::pc_maxima(db, pc)?;
    let conds: Vec<Value> = match serde_json::from_str::<Value>(pc.get("conditions_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a.into_iter().filter(|c| c.is_string()).take(12).collect(),
        Ok(Value::Object(_)) => return None, // iterating a dict: Python's own reading
        _ => vec![],
    };
    let carry = if native {
        let eq = match serde_json::from_str::<Value>(pc.get("equipment_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
            Ok(Value::Array(a)) => a,
            Ok(_) => vec![],
            Err(_) => return None,
        };
        crate::edits::carry_info(db, pc, &eq)?
    } else { Value::Null };
    let resources: Vec<Value> = v["resources"].as_array()?.iter().filter(|r| r["id"] != v["hp_id"]).cloned().collect();
    let me = json!({"id": pid, "name": pc.get("name"), "native": v["native"], "system": v["system"], "hp": v["hp"], "max_hp": v["max_hp"],
        "hp_label": v["hp_label"], "temp_hp": v["temp_hp"], "down": v["down"],
        "shock": if native { json!(pc.get("shock_current").and_then(|x| x.as_i64()).unwrap_or(0)) } else { Value::Null },
        "shock_max": if native { json!(shock) } else { Value::Null }, "conditions": conds, "carry": carry, "hp_id": v["hp_id"],
        "levelup": v["levelup"] == json!(true), "resources": resources});
    let asker = int(pc, "owner_user_id").filter(|o| *o != 0).unwrap_or(user_id);
    let next_session = match next_confirmed(db, wid)? {
        Some((plan, slot)) => {
            let mine = rows(db, "SELECT choice FROM session_plan_votes WHERE slot_id = ?1 AND user_id = ?2 ORDER BY id LIMIT 1", &[&int(&slot, "id")?, &asker])?;
            let start = crate::schedule::stored(slot.get("starts_at"))?;
            let secs = start.div_euclid(1_000_000);
            let (y, m, d) = crate::writes::civil_from_days(secs.div_euclid(86400));
            let r = secs.rem_euclid(86400);
            json!({"title": plan.get("title"), "location": text(&plan, "location"),
                "starts_at": format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", r / 3600, r % 3600 / 60, r % 60),
                "my": mine.first().and_then(|x| x.get("choice").cloned())})
        }
        None => Value::Null,
    };
    let combat = match crate::edits::hub_encounter_db(db, pc)? {
        Some((f, i)) => crate::edits::hub_combat_now(&f, pid, i)?,
        None => Value::Null,
    };
    let prefs = crate::edits::pc_prefs(db, pid)?;
    // _rest_kinds
    let rest_kinds: Vec<&str> = if !is_owner { vec![] } else if native { vec!["long"] } else {
        match crate::sheets::pc_template(db, pc)? {
            Some(t) => ["short", "long"].into_iter().filter(|k| !t.rest_ops(k).is_empty()).collect(),
            None => vec![],
        }
    };
    let handouts_new = if is_owner {
        let seen = match prefs.get("handouts_seen") {
            None | Some(Value::Null) => 0,
            Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
            Some(Value::Bool(b)) => *b as i64,
            _ => return None, // int() of text / a float: Python's
        };
        db.query_row("SELECT count(*) FROM world_handouts WHERE world_id = ?1 AND id > ?2", [wid, seen], |r| r.get::<_, i64>(0)).ok()?
    } else { 0 };
    let places = crate::edits::hub_places(world)?;
    let pins: Vec<Value> = match prefs.get("pins") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.iter().filter_map(|i| places.iter().find(|(_, r)| r.get("id") == Some(i)).map(|(_, r)| r.clone())).collect(),
        Some(_) => return None,
    };
    let companions: Vec<Value> = rows(db, "SELECT * FROM character_companions WHERE character_id = ?1 ORDER BY id", &[&pid])?.iter().map(|c| json!({
        "id": c.get("id"), "name": c.get("name"), "kind": c.get("kind"), "hp": int(c, "hp").unwrap_or(0), "hp_max": int(c, "hp_max").unwrap_or(0),
        "notes": text(c, "notes")})).collect();
    Some(json!({"me": me, "can_edit": is_owner, "combat": combat, "next_session": next_session, "rest_kinds": rest_kinds,
        "handouts_new": handouts_new, "pins": pins, "companions": companions, "polls_waiting": polls_waiting(db, wid, asker)?,
        "schedule_href": with_world("/schedule", world)}))
}


/// the ids of the world's parties that list this character (`_party_ids`)
fn party_ids_of(db: &Connection, pc: &Map<String, Value>) -> Option<Vec<i64>> {
    let (wid, pcid) = (int(pc, "world_id")?, int(pc, "id")?);
    let mut out = vec![];
    for party in rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id", &[&wid])? {
        if crate::edits::member_ids(party.get("member_pc_ids_json"))?.contains(&pcid) {
            out.push(int(&party, "id")?);
        }
    }
    Some(out)
}

/// `date.fromisoformat(text[:10])` for the forms this port is sure of: Some(Some(days since epoch)), Some(None) = ValueError, None = unsure
fn iso_date(text: &str) -> Option<Option<i64>> {
    let t: String = text.chars().take(10).collect();
    let b = t.as_bytes();
    if t.len() == 10 && b[4] == b'-' && b[7] == b'-' && t.bytes().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit()) {
        let (y, m, d): (i64, i64, i64) = (t[..4].parse().ok()?, t[5..7].parse().ok()?, t[8..].parse().ok()?);
        let dim = match m { 1 | 3 | 5 | 7 | 8 | 10 | 12 => 31, 4 | 6 | 9 | 11 => 30, 2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29, 2 => 28, _ => 0 };
        if y < 1 || d < 1 || d > dim {
            return Some(None);
        }
        let yy = if m <= 2 { y - 1 } else { y };
        let era = yy.div_euclid(400);
        let yoe = yy - era * 400;
        let doy = (153 * ((m + 9) % 12) + 2) / 5 + d - 1;
        return Some(Some(era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468));
    }
    // the compact and week-date forms fromisoformat also reads: not reproduced
    if t.contains('W') || (t.len() >= 8 && t[..8].bytes().all(|c| c.is_ascii_digit())) {
        return None;
    }
    Some(None)
}

/// `hub_schedule`: upcoming calendar events pinned to the character or its parties, its parties' next dated sessions, the next confirmed
/// game night and the polls waiting
fn schedule_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>, user_id: i64) -> Option<Value> {
    let wid = int(world, "id")?;
    let pcid = int(pc, "id")?;
    let party_ids = party_ids_of(db, pc)?;
    let level = player_level(world, "calendar")?;
    let (mut events, mut today) = (vec![], Value::Null);
    if level != "none" {
        let (config, label) = crate::calendar::read_only(db, wid)?;
        let current = match config.get("current_day") {
            None => 1,
            Some(Value::Number(n)) => match n.as_i64() { Some(i) => i, None => n.as_f64().filter(|f| f.is_finite())?.trunc() as i64 },
            Some(Value::Bool(b)) => *b as i64,
            Some(Value::String(t)) => {
                // int(text): an underscore or a non-ASCII digit is read by Python's own rules
                let s = nd_render::text::py_strip(t);
                if s.contains('_') || !s.is_ascii() {
                    return None;
                }
                s.parse::<i64>().unwrap_or(1)
            }
            _ => 1,
        };
        today = json!(label(current)?);
        let mut cond = format!("character_id = {pcid}");
        if !party_ids.is_empty() {
            cond = format!("({cond} OR party_id IN ({}))", party_ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","));
        }
        for e in rows(db, &format!("SELECT * FROM calendar_events WHERE world_id = ?1 AND day >= ?2 AND {cond} ORDER BY day, id LIMIT 20"), &[&wid, &current])? {
            let day = int(&e, "day")?;
            events.push(json!({"id": e.get("id"), "title": e.get("title"), "notes": text(&e, "notes"), "color": e.get("color"), "day": day,
                "days_away": day - current, "date": label(day)?}));
        }
    }
    let today_days = (crate::schedule::now_micros() / 1_000_000).div_euclid(86400);
    let mut upcoming: Vec<(i64, Map<String, Value>)> = vec![];
    if !party_ids.is_empty() {
        let list = party_ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        for s in rows(db, &format!("SELECT * FROM game_sessions WHERE world_id = ?1 AND party_id IN ({list}) ORDER BY session_num DESC, id LIMIT 60"), &[&wid])? {
            let Some(when) = iso_date(&text(&s, "session_date"))? else { continue };
            if when >= today_days {
                upcoming.push((when, s));
            }
        }
    }
    upcoming.sort_by_key(|u| u.0);
    let asker = int(pc, "owner_user_id").filter(|o| *o != 0).unwrap_or(user_id);
    let next_session = match next_confirmed(db, wid)? {
        Some((plan, slot)) => {
            let mine = rows(db, "SELECT choice FROM session_plan_votes WHERE slot_id = ?1 AND user_id = ?2 ORDER BY id LIMIT 1", &[&int(&slot, "id")?, &asker])?;
            let secs = crate::schedule::stored(slot.get("starts_at"))?.div_euclid(1_000_000);
            let (y, m, d) = crate::writes::civil_from_days(secs.div_euclid(86400));
            let r = secs.rem_euclid(86400);
            json!({"id": plan.get("id"), "title": plan.get("title"), "location": text(&plan, "location"),
                "starts_at": format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", r / 3600, r % 3600 / 60, r % 60), "duration_min": plan.get("duration_min"),
                "my": mine.first().and_then(|x| x.get("choice").cloned())})
        }
        None => Value::Null,
    };
    let sessions: Vec<Value> = upcoming.iter().take(3).map(|(when, s)| {
        let (y, m, d) = crate::writes::civil_from_days(*when);
        json!({"id": s.get("id"), "title": s.get("title"), "num": s.get("session_num"), "date": format!("{y:04}-{m:02}-{d:02}"),
            "href": with_world(&format!("/sessions/{}", int(s, "id").unwrap_or(0)), world)})
    }).collect();
    Some(json!({"calendar_section": level, "calendar_href": if level != "none" { json!(with_world("/calendar", world)) } else { Value::Null },
        "schedule_href": with_world("/schedule", world), "next_session": next_session, "polls_waiting": polls_waiting(db, wid, asker)?,
        "today": today, "events": events, "sessions": sessions}))
}
/// `created_at.strftime("%Y-%m-%dT%H:%M:%SZ")`, or null
fn strftime_z(v: Option<&Value>) -> Value {
    match v.and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
        Some(dt) => match dt.strftime("%Y-%m-%dT%H:%M:%SZ") { Ok(t) => json!(t), Err(_) => Value::Null },
        None => Value::Null,
    }
}


/// `hub_handouts` (app/routers/character_hub.py): the pictures the GM showed the table, newest first, `new` past the owner's
/// last look (nothing is marked seen here)
fn handouts_tab(db: &Connection, pc: &Map<String, Value>, is_owner: bool) -> Option<Value> {
    let pid = int(pc, "id")?;
    let wid = int(pc, "world_id")?;
    let seen = crate::edits::pc_prefs(db, pid)?.get("handouts_seen").and_then(|v| v.as_i64()).unwrap_or(0);
    let hs = rows(db, "SELECT * FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 40", &[&wid])?;
    let mut out = vec![];
    for h in &hs {
        let at = match h.get("created_at").and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
            Some(dt) => match dt.strftime("%Y-%m-%dT%H:%M:%SZ") { Ok(t) => json!(t), Err(_) => Value::Null },
            None => Value::Null,
        };
        out.push(json!({"id": h.get("id"), "url": h.get("url"),
            "label": h.get("label").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("")),
            "new": int(h, "id").map_or(false, |i| i > seen), "at": at}));
    }
    Some(json!({"can_edit": is_owner, "handouts": out}))
}

/// `hub_places` (app/routers/character_hub.py): every page the world opens to a player, as the navigation bar evaluates it for
/// the PLAYER role, grouped; `pins` are the pages the character pinned (still open to players, in the pinned order)
fn places_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>) -> Option<Value> {
    let place_rows = crate::edits::hub_places(world)?;
    let mut tools = vec![];
    let mut kinds = vec![];
    for (g, r) in &place_rows {
        match *g {
            "kinds" => kinds.push(r.clone()),
            _ => tools.push(r.clone()),
        }
    }
    let mut groups = vec![];
    if !tools.is_empty() {
        groups.push(json!({"label": "Tools & logs", "items": tools}));
    }
    if !kinds.is_empty() {
        groups.push(json!({"label": "The world", "items": kinds}));
    }
    let pid = int(pc, "id")?;
    let pins_raw = crate::edits::pc_prefs(db, pid)?.get("pins").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let by_id: Vec<String> = place_rows.iter().filter_map(|(_, r)| r.get("id").and_then(|v| v.as_str()).map(str::to_string)).collect();
    let pins: Vec<Value> = pins_raw.iter()
        .filter(|i| i.as_str().map_or(false, |s| by_id.iter().any(|b| b == s)))
        .cloned().collect();
    Some(json!({"groups": groups, "pins": pins}))
}

/// `hub_keepsakes` (app/routers/character_hub.py): what the character has kept, newest first, the per-kind personal-file
/// limits, and everything the viewer could still keep (a player's view of the world, even for a GM looking in)
fn keepsakes_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>, uid: i64, is_gm: bool, is_owner: bool) -> Option<Value> {
    let pid = int(pc, "id")?;
    let wid = int(pc, "world_id")?;
    let ks = rows(db, "SELECT * FROM character_keepsakes WHERE character_id = ?1 ORDER BY id DESC", &[&pid])?;
    let mut keepsakes = vec![];
    let mut counts: Map<String, Value> = Map::new();
    for kind in ["image", "audio", "video"] {
        counts.insert(kind.into(), json!(0));
    }
    for k in &ks {
        keepsakes.push(json!({"id": k.get("id"), "kind": k.get("kind"),
            "url": k.get("url").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("")),
            "text": k.get("text").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("")),
            "name": k.get("name"),
            "description": k.get("description").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("")),
            "by": k.get("saved_by"), "own": crate::edits::py_truthy_pub(k.get("own")), "at": strftime_z(k.get("created_at"))}));
        if crate::edits::py_truthy_pub(k.get("own")) {
            if let Some(kind) = k.get("kind").and_then(|v| v.as_str()) {
                if let Some(c) = counts.get_mut(kind) {
                    *c = json!(c.as_i64().unwrap_or(0) + 1);
                }
            }
        }
    }
    // personal_files.limit_for: the world's stored limit (clamped 0..HARD_MAX), else the default
    let limit = |kind: &str| -> i64 {
        let (col, dflt) = match kind {
            "image" => ("personal_images_max", 30),
            "audio" => ("personal_audio_max", 10),
            _ => ("personal_video_max", 5),
        };
        let stored = world.get(col).and_then(|v| v.as_i64()).unwrap_or(-1);
        if stored < 0 { dflt } else { stored.min(500) }
    };
    let mut own_limits = Map::new();
    for kind in ["image", "audio", "video"] {
        own_limits.insert(kind.into(), json!({"used": counts.get(kind).cloned().unwrap_or(json!(0)), "max": limit(kind)}));
    }
    // _saveable: handouts, the owner's private notes from someone else, and the session log's player-visible media
    let handouts: Vec<Value> = rows(db, "SELECT * FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 40", &[&wid])?
        .iter().map(|h| json!({"id": h.get("id"), "kind": "image", "url": h.get("url"),
            "name": h.get("label").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("Handout"))})).collect();
    let mut notes = vec![];
    if is_owner {
        for n in rows(db, "SELECT * FROM private_notes WHERE world_id = ?1 AND player_user_id = ?2 AND author_id != ?2 ORDER BY id DESC LIMIT 30", &[&wid, &uid])? {
            let content = n.get("content").and_then(|v| v.as_str()).unwrap_or("");
            let title = n.get("title").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("Note"));
            let stripped = nd_perm::gmonly::strip_gm_only(content, Some(uid), nd_render::now_minute())?;
            notes.push(json!({"id": n.get("id"), "kind": "text", "name": title,
                "text": stripped.chars().take(600).collect::<String>()}));
        }
    }
    let mut session_items: Vec<Value> = vec![];
    if nd_perm::sections::section_level(false, Some("player"), world.get("section_access_json").and_then(|v| v.as_str()), world.get("custom_kinds_json").and_then(|v| v.as_str()), "session_log")? != "none" {
        for gs in rows(db, "SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 15", &[&wid])? {
            let m = crate::edits::viewer_media(db, &gs, false)?;
            let label = match gs.get("title").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                Some(t) => format!("#{} {t}", int(&gs, "session_num")?),
                None => format!("#{} Session", int(&gs, "session_num")?),
            };
            for i in m["images"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "image", "ref": i.get("uid"),
                    "url": i.get("url"), "name": i.get("caption").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|t| Value::String(t.to_string())).unwrap_or_else(|| json!("Picture"))}));
            }
            for v in m["videos"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "video", "ref": v.get("id").map(|x| x.to_string()),
                    "url": v.get("file_url"), "name": v.get("name")}));
            }
            for c in m["audio"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "audio", "ref": c.get("id").map(|x| x.to_string()),
                    "url": c.get("file_url"), "name": c.get("name")}));
            }
        }
    }
    session_items.truncate(60);
    Some(json!({"can_edit": true, "is_gm": is_gm, "is_owner": is_owner, "keepsakes": keepsakes,
        "own_limits": own_limits, "saveable": {"handouts": handouts, "notes": notes, "session": session_items}}))
}
