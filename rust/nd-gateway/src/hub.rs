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
    matches!(tab, "quests" | "world" | "now" | "schedule").then(|| Some((tab, id.parse().ok()?)))?
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
        "now" => now_strip(&db, &pc, &world, is_owner, p.user_id)?,
        "schedule" => schedule_tab(&db, &pc, &world, p.user_id)?,
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
