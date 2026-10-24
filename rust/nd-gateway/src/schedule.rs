//! The session planner (app/routers/schedule.py): `GET /schedule`, `GET /api/schedule/state`, the plan / slot / vote writes, confirm /
//! reopen / cancel / delete, and the two .ics downloads - every answer, refusals included, as the FastAPI routes give it. Input Python
//! would read in ways not reproduced here (a time `datetime.fromisoformat` takes in one of its rarer forms, a number string with `_`,
//! a path id FastAPI would coerce) goes to Python unchanged.
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::Pages;

const CHOICES: [&str; 3] = ["yes", "maybe", "no"];
const MAX_SLOTS_PER_PLAN: usize = 20;
const MAX_PLANS_PER_WORLD: i64 = 200;
const DEFAULT_DURATION_MIN: i64 = 180;
const MICROS_PER_MIN: i64 = 60_000_000;

// ── time ─────────────────────────────────────────────────────────────────────

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        _ => 28,
    }
}

/// (y, m, d, h, mi, s, us) of an epoch in microseconds
fn parts(t: i64) -> (i64, i64, i64, i64, i64, i64, i64) {
    let secs = t.div_euclid(1_000_000);
    let us = t.rem_euclid(1_000_000);
    let (y, m, d) = crate::writes::civil_from_days(secs.div_euclid(86400));
    let rem = secs.rem_euclid(86400);
    (y, m, d, rem / 3600, rem % 3600 / 60, rem % 60, us)
}

/// a stored naive-UTC DateTime ("2026-05-04 18:30:00.000000") as epoch microseconds
pub(crate) fn stored(v: Option<&Value>) -> Option<i64> {
    // a DATETIME column comes out of crate::layout::row_map as {"__dt__": text}
    let v = v?;
    let raw = v.get("__dt__").unwrap_or(v).as_str()?;
    let dt = nd_render::pyvalue::PyDateTime::parse(raw)?;
    Some((days_from_civil(dt.y, dt.mo as i64, dt.d as i64) * 86400 + dt.h as i64 * 3600 + dt.mi as i64 * 60 + dt.s as i64) * 1_000_000 + dt.us as i64)
}

/// how SQLAlchemy stores a naive datetime in SQLite
fn store(t: i64) -> String {
    let (y, m, d, h, mi, s, us) = parts(t);
    format!("{y:04}-{m:02}-{d:02} {h:02}:{mi:02}:{s:02}.{us:06}")
}

/// `_iso`: 2031-05-04T18:30:00Z
fn iso(t: i64) -> String {
    let (y, m, d, h, mi, s, _) = parts(t);
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

pub(crate) fn now_micros() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_micros() as i64).unwrap_or(0)
}

fn digits(s: &str, n: usize) -> Option<i64> {
    (s.len() == n && s.bytes().all(|b| b.is_ascii_digit())).then(|| s.parse().ok()).flatten()
}

/// `_parse_time` for the forms `datetime.fromisoformat` is certain to read the same way here: YYYY-MM-DD, optionally followed by
/// T/space HH:MM[:SS[.fraction]] and Z / ±HH:MM / ±HHMM. Some(Ok(epoch µs, whole minutes)), Some(Err) = Python raises ValueError,
/// None = not sure (Python's call).
fn parse_time(v: Option<&Value>) -> Option<Result<i64, ()>> {
    let Some(Value::String(raw)) = v else { return Some(Err(())) }; // not a string: "a time is required"
    let t = nd_render::text::py_strip(raw);
    if t.is_empty() {
        return Some(Err(()));
    }
    if !t.is_ascii() {
        return None;
    }
    let t = t.replace('Z', "+00:00");
    let (date, rest) = if t.len() > 10 { (&t[..10], Some(&t[10..])) } else { (&t[..], None) };
    let dp: Vec<&str> = date.split('-').collect();
    let [ys, ms, ds] = dp.as_slice() else { return None };
    let (y, m, d) = (digits(ys, 4)?, digits(ms, 2)?, digits(ds, 2)?);
    if !(1..=12).contains(&m) || d < 1 || d > days_in_month(y, m) {
        return Some(Err(()));
    }
    let (mut h, mut mi, mut offset_min) = (0, 0, None::<i64>);
    if let Some(rest) = rest {
        let sep = rest.as_bytes()[0];
        if sep != b'T' && sep != b' ' {
            return None;
        }
        let body = &rest[1..];
        let (time, off) = match body.find(['+', '-']) {
            Some(i) => (&body[..i], Some(&body[i..])),
            None => (body, None),
        };
        let (hms, frac) = time.split_once('.').map_or((time, None), |(a, b)| (a, Some(b)));
        let tp: Vec<&str> = hms.split(':').collect();
        match tp.as_slice() {
            [hh, mm] => {
                h = digits(hh, 2)?;
                mi = digits(mm, 2)?;
                if frac.is_some() {
                    return None;
                }
            }
            [hh, mm, ss] => {
                h = digits(hh, 2)?;
                mi = digits(mm, 2)?;
                let s = digits(ss, 2)?;
                if s > 59 {
                    return Some(Err(()));
                }
                if let Some(f) = frac {
                    if f.is_empty() || f.len() > 6 || !f.bytes().all(|b| b.is_ascii_digit()) {
                        return None;
                    }
                }
            }
            _ => return None,
        }
        if h > 23 || mi > 59 {
            return Some(Err(()));
        }
        if let Some(off) = off {
            let sign = if off.starts_with('-') { -1 } else { 1 };
            let o = &off[1..];
            let (oh, om) = match o.len() {
                5 if o.as_bytes()[2] == b':' => (digits(&o[..2], 2)?, digits(&o[3..], 2)?),
                4 => (digits(&o[..2], 2)?, digits(&o[2..], 2)?),
                _ => return None,
            };
            if oh > 23 || om > 59 {
                return Some(Err(()));
            }
            offset_min = Some(sign * (oh * 60 + om));
        }
    }
    let minutes = days_from_civil(y, m, d) * 1440 + h * 60 + mi - offset_min.unwrap_or(0);
    let t = minutes * MICROS_PER_MIN;
    let year = parts(t).0;
    if !(2000..=2100).contains(&year) {
        return Some(Err(()));
    }
    Some(Ok(t))
}

// ── values the way Python reads them ─────────────────────────────────────────

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `_text(value, limit)`: str(value or "").strip()[:limit]; None = a str() not reproduced here (a list, a float)
fn text(v: Option<&Value>, limit: usize) -> Option<String> {
    if !truthy(v) {
        return Some(String::new());
    }
    let s = crate::pages::py_str_value(v?)?;
    Some(nd_render::text::py_strip(&s).chars().take(limit).collect())
}

/// int(value): Some(Some(n)), Some(None) = TypeError / ValueError, None = not sure
fn py_int(v: Option<&Value>) -> Option<Option<i64>> {
    Some(match v {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => None,
        Some(Value::Bool(b)) => Some(*b as i64),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => Some(i),
            None => {
                let f = n.as_f64()?;
                if !f.is_finite() || f.abs() > 9e15 {
                    return None;
                }
                Some(f.trunc() as i64)
            }
        },
        Some(Value::String(s)) => {
            let t = nd_render::text::py_strip(s);
            if !t.is_ascii() || t.contains('_') {
                return None;
            }
            let body = t.strip_prefix(['+', '-']).unwrap_or(t);
            if body.is_empty() || !body.bytes().all(|b| b.is_ascii_digit()) {
                None
            } else {
                Some(t.parse::<i64>().ok()?)
            }
        }
    })
}

/// `_duration(value, default)`
fn duration(v: Option<&Value>, default: i64) -> Option<i64> {
    Some(match py_int(v)? {
        Some(n) => n.clamp(15, 1440),
        None => default,
    })
}

fn detail(code: StatusCode, m: &str) -> Response<Body> {
    crate::ai_more::detail(code, m)
}

fn ok(v: Value) -> Option<Response<Body>> {
    crate::edits::json_response(&v)
}

/// `_body_raw`: the JSON object, or the 400; None = Python reads the text differently (NaN, a huge whole number)
fn body_obj(raw: &[u8]) -> Option<Result<Map<String, Value>, Response<Body>>> {
    match serde_json::from_slice::<Value>(raw) {
        Ok(Value::Object(o)) => {
            if !crate::edits::plain_numbers(raw) {
                return None;
            }
            Some(Ok(o))
        }
        Ok(_) => Some(Err(detail(StatusCode::BAD_REQUEST, "Send a JSON object."))),
        Err(_) => {
            let s = String::from_utf8_lossy(raw);
            if s.contains("NaN") || s.contains("Infinity") || raw.starts_with(&[0xef, 0xbb, 0xbf]) || raw.contains(&0) {
                return None;
            }
            Some(Err(detail(StatusCode::BAD_REQUEST, "Send a JSON body.")))
        }
    }
}

macro_rules! body {
    ($raw:expr) => {
        match body_obj($raw)? {
            Ok(b) => b,
            Err(r) => return Some(r),
        }
    };
}

// ── rows ─────────────────────────────────────────────────────────────────────

fn row(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Option<Map<String, Value>>> {
    match db.query_row(sql, params, |r| crate::layout::row_map(r)) {
        Ok(m) => Some(Some(m)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Some(None),
        Err(_) => None,
    }
}

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let out = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(out)
}

fn int(m: &Map<String, Value>, k: &str) -> Option<i64> {
    m.get(k).and_then(|v| v.as_i64())
}

fn st(m: &Map<String, Value>, k: &str) -> String {
    m.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn dur(plan: &Map<String, Value>) -> i64 {
    int(plan, "duration_min").filter(|d| *d != 0).unwrap_or(DEFAULT_DURATION_MIN)
}

/// `_name(user)`
fn name(u: Option<&Map<String, Value>>) -> String {
    let Some(u) = u else { return "Someone".into() };
    let dn = st(u, "display_name");
    if !dn.is_empty() {
        return dn;
    }
    let local = st(u, "email").split('@').next().unwrap_or("").to_string();
    if local.is_empty() { "Someone".into() } else { local }
}

struct Who {
    user_id: i64,
    world: Map<String, Value>,
    wid: i64,
    is_manager: bool,
}

fn can_touch(w: &Who, owner: Option<i64>) -> bool {
    w.is_manager || owner == Some(w.user_id)
}

/// `_participants`: the GMs by id, then the world's members (owners, assistants, players; by name)
fn participants(db: &Connection, wid: i64) -> Option<Vec<Value>> {
    let gms = rows(db, "SELECT * FROM users WHERE is_gm IS 1 ORDER BY id", &[])?;
    let mut out: Vec<Value> = gms.iter().map(|g| json!({"id": int(g, "id"), "name": name(Some(g)), "role": "gm"})).collect();
    let mut seen: Vec<i64> = gms.iter().filter_map(|g| int(g, "id")).collect();
    let mut rest: Vec<(i64, String, Value)> = vec![];
    for m in rows(db, "SELECT * FROM world_memberships WHERE world_id = ?1 ORDER BY id", &[&wid])? {
        let Some(uid) = int(&m, "user_id") else { continue };
        let Some(u) = row(db, "SELECT * FROM users WHERE id = ?1", &[&uid])? else { continue };
        if seen.contains(&uid) {
            continue;
        }
        seen.push(uid);
        let role = Some(st(&m, "role")).filter(|r| !r.is_empty()).unwrap_or_else(|| "player".into());
        let rank = match role.as_str() {
            "owner" => 0,
            "assistant" => 1,
            _ => 2,
        };
        let n = name(Some(&u));
        rest.push((rank, n.to_lowercase(), json!({"id": uid, "name": n, "role": role})));
    }
    rest.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    out.extend(rest.into_iter().map(|r| r.2));
    Some(out)
}

/// `build_state`
fn build_state(db: &Connection, w: &Who, me: &Map<String, Value>) -> Option<Value> {
    let now = now_micros();
    let people = participants(db, w.wid)?;
    let ids: Vec<i64> = people.iter().filter_map(|p| p["id"].as_i64()).collect();
    let name_of = |uid: i64| people.iter().find(|p| p["id"].as_i64() == Some(uid)).map(|p| p["name"].as_str().unwrap_or("").to_string());
    let plans = rows(db, "SELECT * FROM session_plans WHERE world_id = ?1 ORDER BY id", &[&w.wid])?;
    let slots = rows(db, "SELECT * FROM session_plan_slots WHERE world_id = ?1 ORDER BY starts_at, id", &[&w.wid])?;
    let votes = rows(db, "SELECT * FROM session_plan_votes WHERE world_id = ?1 ORDER BY id", &[&w.wid])?;
    let who = |uid: Option<i64>| -> Option<Value> {
        let uid = uid.filter(|u| *u != 0)?;
        let n = match name_of(uid).filter(|n| !n.is_empty()) {
            Some(n) => n,
            None => name(row(db, "SELECT * FROM users WHERE id = ?1", &[&uid]).flatten().as_ref()),
        };
        Some(json!({"id": uid, "name": n}))
    };
    // slot id -> [(user id, choice)] in vote order, the latest answer per person
    let mut by_slot: Vec<(i64, Vec<(i64, String)>)> = vec![];
    for v in &votes {
        let (Some(sid), Some(uid)) = (int(v, "slot_id"), int(v, "user_id")) else { continue };
        let choice = st(v, "choice");
        if !ids.contains(&uid) || !CHOICES.contains(&choice.as_str()) {
            continue;
        }
        let e = match by_slot.iter_mut().find(|e| e.0 == sid) {
            Some(e) => e,
            None => {
                by_slot.push((sid, vec![]));
                by_slot.last_mut()?
            }
        };
        match e.1.iter_mut().find(|x| x.0 == uid) {
            Some(x) => x.1 = choice,
            None => e.1.push((uid, choice)),
        }
    }
    let me_id = w.user_id;
    let mut out: Vec<Value> = vec![];
    for p in &plans {
        let pid = int(p, "id")?;
        let mine: Vec<&Map<String, Value>> = slots.iter().filter(|s| int(s, "plan_id") == Some(pid)).collect();
        let status = Some(st(p, "status")).filter(|s| !s.is_empty()).unwrap_or_else(|| "open".into());
        let chosen = if st(p, "status") == "confirmed" { mine.iter().find(|s| int(s, "id").is_some() && int(s, "id") == int(p, "confirmed_slot_id")) } else { None };
        let d = dur(p) * MICROS_PER_MIN;
        let end = match chosen {
            Some(c) => Some(stored(c.get("starts_at"))? + d),
            None => None,
        };
        let mut srows = vec![];
        let mut answered = false;
        for s in &mine {
            let sid = int(s, "id")?;
            let here: Vec<(i64, String)> = by_slot.iter().find(|e| e.0 == sid).map(|e| e.1.clone()).unwrap_or_default();
            answered = answered || here.iter().any(|x| x.0 == me_id);
            let start = stored(s.get("starts_at"))?;
            let mut counts = Map::new();
            for c in CHOICES {
                counts.insert(c.into(), json!(here.iter().filter(|x| x.1 == c).count()));
            }
            let mut vmap = Map::new();
            for (uid, c) in &here {
                vmap.insert(uid.to_string(), json!(c));
            }
            srows.push(json!({
                "id": sid, "starts_at": iso(start), "ends_at": iso(start + d),
                "proposed_by": who(int(s, "proposed_by_user_id")),
                "can_delete": can_touch(w, int(s, "proposed_by_user_id")),
                "counts": counts, "votes": vmap,
                "mine": here.iter().find(|x| x.0 == me_id).map(|x| x.1.clone()),
            }));
        }
        let session_id = p.get("session_id").cloned().unwrap_or(Value::Null);
        let href = match int(p, "session_id").filter(|i| *i != 0) {
            Some(sid) => json!(format!("/sessions/{sid}?w={}", st(&w.world, "slug"))),
            None => Value::Null,
        };
        out.push(json!({
            "id": pid, "title": p.get("title"), "notes": st(p, "notes"), "location": st(p, "location"),
            "duration_min": dur(p), "status": status,
            "created_by": who(int(p, "created_by_user_id")),
            "can_edit": can_touch(w, int(p, "created_by_user_id")),
            "confirmed_slot_id": if chosen.is_some() { p.get("confirmed_slot_id").cloned().unwrap_or(Value::Null) } else { Value::Null },
            "session_id": session_id, "session_href": href,
            "past": end.map_or(false, |e| e < now), "my_answered": answered,
            "slots": srows,
        }));
    }
    let far = "9999".to_string();
    let key = |p: &Value| -> (i64, String, i64) {
        let status = p["status"].as_str().unwrap_or("");
        let rank = match status {
            "open" => 0,
            "confirmed" => 1,
            "cancelled" => 2,
            _ => 3,
        };
        let slots = p["slots"].as_array().cloned().unwrap_or_default();
        let when = if status == "confirmed" {
            slots.iter().find(|s| s["id"] == p["confirmed_slot_id"]).and_then(|s| s["starts_at"].as_str()).map(str::to_string).unwrap_or(far.clone())
        } else {
            slots.first().and_then(|s| s["starts_at"].as_str()).map(str::to_string).unwrap_or(far.clone())
        };
        (rank, when, p["id"].as_i64().unwrap_or(0))
    };
    out.sort_by(|a, b| key(a).cmp(&key(b)));
    Some(json!({
        "me": {"id": me_id, "name": name(Some(me)), "can_manage": w.is_manager},
        "participants": people, "plans": out, "now": iso(now - now.rem_euclid(1_000_000)),
    }))
}

// ── routing ──────────────────────────────────────────────────────────────────

/// a path id FastAPI reads as the same int (plain digits, no leading zero); None = Python's call
fn path_id(s: &str) -> Option<i64> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) || (s.len() > 1 && s.starts_with('0')) || s.len() > 18 {
        return None;
    }
    s.parse().ok()
}

enum Route {
    Page,
    State,
    Create,
    Edit(i64),
    AddSlot(i64),
    DeleteSlot(i64),
    Vote(i64),
    Confirm(i64),
    Reopen(i64),
    Cancel(i64),
    Delete(i64),
    PlanIcs(i64),
    ConfirmedIcs,
}

fn route(method: &Method, path: &str) -> Option<Route> {
    let seg: Vec<&str> = path.trim_start_matches('/').split('/').collect();
    let get = *method == Method::GET;
    Some(match (get, seg.as_slice()) {
        (true, ["schedule"]) => Route::Page,
        (true, ["api", "schedule", "state"]) => Route::State,
        (true, ["schedule", "confirmed.ics"]) => Route::ConfirmedIcs,
        (true, ["schedule", "plans", f]) => {
            // {plan_id:int}.ics: Starlette's int converter ([0-9]+)
            let d = f.strip_suffix(".ics")?;
            if d.is_empty() || !d.bytes().all(|b| b.is_ascii_digit()) || d.len() > 18 {
                return None;
            }
            Route::PlanIcs(d.parse().ok()?)
        }
        (false, ["api", "schedule", "plans"]) => Route::Create,
        (false, ["api", "schedule", "plans", id, act]) => {
            let id = path_id(id)?;
            match *act {
                "edit" => Route::Edit(id),
                "slots" => Route::AddSlot(id),
                "confirm" => Route::Confirm(id),
                "reopen" => Route::Reopen(id),
                "cancel" => Route::Cancel(id),
                "delete" => Route::Delete(id),
                _ => return None,
            }
        }
        (false, ["api", "schedule", "slots", id, act]) => {
            let id = path_id(id)?;
            match *act {
                "delete" => Route::DeleteSlot(id),
                "vote" => Route::Vote(id),
                _ => return None,
            }
        }
        _ => return None,
    })
}

pub fn serves(method: &Method, path: &str) -> bool {
    (path.starts_with("/schedule") || path.starts_with("/api/schedule/")) && (*method == Method::GET || *method == Method::POST) && route(method, path).is_some()
}

/// Some(answer), or None for Python
pub fn handle(pages: &Pages, cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let r = route(method, uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    let Some(world) = p.world.clone() else { return Some(crate::idpages::refusal(404)) };
    let w = Who { user_id: p.user_id, wid: world.get("id")?.as_i64()?, world, is_manager: p.is_gm || p.role.as_deref() == Some("assistant") };
    let me = row(&db, "SELECT * FROM users WHERE id = ?1", &[&p.user_id])??;
    let reads = matches!(r, Route::Page | Route::State | Route::PlanIcs(_) | Route::ConfirmedIcs);
    db.execute_batch(if reads { "BEGIN" } else { "BEGIN IMMEDIATE" }).ok()?;
    let out = match r {
        Route::Page => page(pages, &db, p, &w, &me, headers),
        Route::State => ok(build_state(&db, &w, &me)?),
        Route::PlanIcs(id) => plan_ics(&db, &w, id),
        Route::ConfirmedIcs => confirmed_ics(&db, &w),
        Route::Create => plan_create(&db, &w, body),
        Route::Edit(id) => plan_edit(&db, &w, id, body),
        Route::AddSlot(id) => slot_add(&db, &w, id, body),
        Route::DeleteSlot(id) => slot_delete(&db, &w, id),
        Route::Vote(id) => slot_vote(&db, &w, id, body),
        Route::Confirm(id) => plan_confirm(&db, &w, id, body),
        Route::Reopen(id) => plan_status(&db, &w, id, true),
        Route::Cancel(id) => plan_status(&db, &w, id, false),
        Route::Delete(id) => plan_delete(&db, &w, id),
    };
    match &out {
        Some(resp) if resp.status().is_success() => db.execute_batch("COMMIT").ok()?,
        _ => {
            let _ = db.execute_batch("ROLLBACK");
        }
    }
    out
}

fn page(pages: &Pages, db: &Connection, mut p: PageCtx, w: &Who, me: &Map<String, Value>, headers: &HeaderMap) -> Option<Response<Body>> {
    let state = build_state(db, w, me)?;
    p.ctx.insert("state".into(), state);
    crate::pages::render_with(pages, "schedule/index.html", p.ctx, headers)
}

fn plan_of(db: &Connection, w: &Who, id: i64) -> Option<Option<Map<String, Value>>> {
    row(db, "SELECT * FROM session_plans WHERE id = ?1 AND world_id = ?2", &[&id, &w.wid])
}

fn slot_of(db: &Connection, w: &Who, id: i64) -> Option<Option<Map<String, Value>>> {
    row(db, "SELECT * FROM session_plan_slots WHERE id = ?1 AND world_id = ?2", &[&id, &w.wid])
}

macro_rules! or_404 {
    ($e:expr) => {
        match $e? {
            Some(x) => x,
            None => return Some(crate::idpages::refusal(404)),
        }
    };
}

fn touch(db: &Connection, w: &Who) -> Option<()> {
    crate::orm::touch(db, w.wid)
}

/// `_parse_slots`: the distinct times in order, or the 400
fn parse_slots(raw: Option<&Value>) -> Option<Result<Vec<i64>, Response<Body>>> {
    let list = match raw {
        None | Some(Value::Null) => return Some(Ok(vec![])),
        Some(Value::Array(a)) => a,
        Some(_) => return Some(Err(detail(StatusCode::BAD_REQUEST, "slots must be a list of times."))),
    };
    let mut out = vec![];
    for item in list {
        match parse_time(Some(item))? {
            Ok(t) => {
                if !out.contains(&t) {
                    out.push(t);
                }
            }
            Err(()) => {
                // f"Not a valid time: {item!r}" - repr() of a string or a simple value
                let r = match item {
                    // repr() escapes some non-ASCII characters (unprintable ones) by rules not reproduced here
                    Value::String(s) if s.chars().all(|c| (' '..='~').contains(&c) || matches!(c, '\n' | '\r' | '\t')) => crate::ai::py_repr(item),
                    Value::String(_) => return None,
                    Value::Null => "None".into(),
                    Value::Bool(b) => if *b { "True".into() } else { "False".into() },
                    Value::Number(n) if n.is_i64() => n.to_string(),
                    _ => return None,
                };
                return Some(Err(detail(StatusCode::BAD_REQUEST, &format!("Not a valid time: {r}"))));
            }
        }
    }
    if out.len() > MAX_SLOTS_PER_PLAN {
        return Some(Err(detail(StatusCode::BAD_REQUEST, "At most 20 time slots per plan.")));
    }
    Some(Ok(out))
}

fn plan_create(db: &Connection, w: &Who, raw: &[u8]) -> Option<Response<Body>> {
    let b = body!(raw);
    let title = text(b.get("title"), 160)?;
    if title.is_empty() {
        return Some(detail(StatusCode::BAD_REQUEST, "Give the session a title."));
    }
    let times = match parse_slots(b.get("slots"))? {
        Ok(t) => t,
        Err(r) => return Some(r),
    };
    let n: i64 = db.query_row("SELECT count(*) FROM session_plans WHERE world_id = ?1", [w.wid], |r| r.get(0)).ok()?;
    if n >= MAX_PLANS_PER_WORLD {
        return Some(detail(StatusCode::BAD_REQUEST, "Too many plans - delete some old ones first."));
    }
    let id = crate::orm::insert(db, "session_plans", &[
        ("world_id", Sql::Integer(w.wid)), ("title", Sql::Text(title)), ("notes", Sql::Text(text(b.get("notes"), 1500)?)),
        ("location", Sql::Text(text(b.get("location"), 200)?)), ("duration_min", Sql::Integer(duration(b.get("duration_min"), DEFAULT_DURATION_MIN)?)),
        ("status", Sql::Text("open".into())), ("created_by_user_id", Sql::Integer(w.user_id)),
    ])?;
    for t in times {
        crate::orm::insert(db, "session_plan_slots", &[
            ("plan_id", Sql::Integer(id)), ("world_id", Sql::Integer(w.wid)), ("starts_at", Sql::Text(store(t))), ("proposed_by_user_id", Sql::Integer(w.user_id)),
        ])?;
    }
    touch(db, w)?;
    ok(json!({"id": id}))
}

fn plan_edit(db: &Connection, w: &Who, id: i64, raw: &[u8]) -> Option<Response<Body>> {
    let plan = or_404!(plan_of(db, w, id));
    if !can_touch(w, int(&plan, "created_by_user_id")) {
        return Some(crate::idpages::refusal(403));
    }
    let b = body!(raw);
    let mut set: Vec<(&str, Sql)> = vec![];
    if b.contains_key("title") {
        let title = text(b.get("title"), 160)?;
        if title.is_empty() {
            return Some(detail(StatusCode::BAD_REQUEST, "A plan needs a title."));
        }
        set.push(("title", Sql::Text(title)));
    }
    if b.contains_key("notes") {
        set.push(("notes", Sql::Text(text(b.get("notes"), 1500)?)));
    }
    if b.contains_key("location") {
        set.push(("location", Sql::Text(text(b.get("location"), 200)?)));
    }
    if b.contains_key("duration_min") {
        set.push(("duration_min", Sql::Integer(duration(b.get("duration_min"), dur(&plan))?)));
    }
    crate::orm::update(db, "session_plans", id, &set)?;
    touch(db, w)?;
    ok(json!({"ok": true}))
}

fn slot_add(db: &Connection, w: &Who, id: i64, raw: &[u8]) -> Option<Response<Body>> {
    let plan = or_404!(plan_of(db, w, id));
    if st(&plan, "status") != "open" {
        return Some(detail(StatusCode::BAD_REQUEST, "This plan is no longer open for new times."));
    }
    let b = body!(raw);
    let t = match parse_time(b.get("starts_at"))? {
        Ok(t) => t,
        Err(()) => return Some(detail(StatusCode::BAD_REQUEST, "Not a valid time.")),
    };
    let existing = rows(db, "SELECT * FROM session_plan_slots WHERE plan_id = ?1 ORDER BY id", &[&id])?;
    for s in &existing {
        if stored(s.get("starts_at"))? == t {
            return ok(json!({"id": int(s, "id")}));
        }
    }
    if existing.len() >= MAX_SLOTS_PER_PLAN {
        return Some(detail(StatusCode::BAD_REQUEST, "At most 20 time slots per plan."));
    }
    let sid = crate::orm::insert(db, "session_plan_slots", &[
        ("plan_id", Sql::Integer(id)), ("world_id", Sql::Integer(w.wid)), ("starts_at", Sql::Text(store(t))), ("proposed_by_user_id", Sql::Integer(w.user_id)),
    ])?;
    touch(db, w)?;
    ok(json!({"id": sid}))
}

fn slot_delete(db: &Connection, w: &Who, id: i64) -> Option<Response<Body>> {
    let slot = or_404!(slot_of(db, w, id));
    if !can_touch(w, int(&slot, "proposed_by_user_id")) {
        return Some(crate::idpages::refusal(403));
    }
    if let Some(plan) = row(db, "SELECT * FROM session_plans WHERE id = ?1", &[&int(&slot, "plan_id")])? {
        if st(&plan, "status") == "confirmed" && int(&plan, "confirmed_slot_id") == Some(id) {
            return Some(detail(StatusCode::BAD_REQUEST, "Reopen the plan before removing the chosen time."));
        }
    }
    db.execute("DELETE FROM session_plan_votes WHERE slot_id = ?1", [id]).ok()?;
    db.execute("DELETE FROM session_plan_slots WHERE id = ?1", [id]).ok()?;
    touch(db, w)?;
    ok(json!({"ok": true}))
}

fn slot_vote(db: &Connection, w: &Who, id: i64, raw: &[u8]) -> Option<Response<Body>> {
    let slot = or_404!(slot_of(db, w, id));
    let plan = row(db, "SELECT * FROM session_plans WHERE id = ?1", &[&int(&slot, "plan_id")])?;
    let b = body!(raw);
    let choice = match b.get("choice") {
        Some(Value::String(c)) if CHOICES.contains(&c.as_str()) || c == "clear" => c.clone(),
        _ => return Some(detail(StatusCode::BAD_REQUEST, "choice must be yes, maybe, no or clear.")),
    };
    let Some(plan) = plan.filter(|p| st(p, "status") != "cancelled") else {
        return Some(detail(StatusCode::BAD_REQUEST, "This plan was cancelled."));
    };
    if st(&plan, "status") == "confirmed" && int(&plan, "confirmed_slot_id") != Some(id) {
        return Some(detail(StatusCode::BAD_REQUEST, "A time has been chosen - only that one takes answers now."));
    }
    let mine = row(db, "SELECT * FROM session_plan_votes WHERE slot_id = ?1 AND user_id = ?2 ORDER BY id LIMIT 1", &[&id, &w.user_id])?;
    if choice == "clear" {
        if let Some(v) = mine {
            db.execute("DELETE FROM session_plan_votes WHERE id = ?1", [int(&v, "id")?]).ok()?;
        }
    } else if let Some(v) = mine {
        crate::orm::update(db, "session_plan_votes", int(&v, "id")?, &[("choice", Sql::Text(choice.clone())), ("updated_at", Sql::Text(crate::orm::now_stamp()))])?;
    } else {
        crate::orm::insert(db, "session_plan_votes", &[
            ("plan_id", Sql::Integer(int(&plan, "id")?)), ("slot_id", Sql::Integer(id)), ("world_id", Sql::Integer(w.wid)),
            ("user_id", Sql::Integer(w.user_id)), ("choice", Sql::Text(choice.clone())),
        ])?;
    }
    touch(db, w)?;
    ok(json!({"ok": true, "mine": if choice == "clear" { Value::Null } else { json!(choice) }}))
}

/// `_valid_date`: YYYY-MM-DD that is a real date, else None
fn valid_date(v: Option<&Value>) -> Option<Option<String>> {
    let t = text(v, usize::MAX)?;
    let p: Vec<&str> = t.split('-').collect();
    let [y, m, d] = p.as_slice() else { return Some(None) };
    let (Some(y), Some(m), Some(d)) = (digits(y, 4), digits(m, 2), digits(d, 2)) else { return Some(None) };
    Some(((1..=12).contains(&m) && d >= 1 && d <= days_in_month(y, m) && y >= 1).then_some(t))
}

fn plan_confirm(db: &Connection, w: &Who, id: i64, raw: &[u8]) -> Option<Response<Body>> {
    if !w.is_manager {
        return Some(crate::idpages::refusal(403));
    }
    let plan = or_404!(plan_of(db, w, id));
    let b = body!(raw);
    let Some(slot_id) = py_int(b.get("slot_id"))? else {
        return Some(detail(StatusCode::BAD_REQUEST, "Pick one of the plan's times."));
    };
    match st(&plan, "status").as_str() {
        "open" => {}
        "confirmed" => return Some(detail(StatusCode::BAD_REQUEST, "This plan is not open - reopen it to choose a different time.")),
        _ => return Some(detail(StatusCode::BAD_REQUEST, "This plan was cancelled.")),
    }
    let Some(slot) = row(db, "SELECT * FROM session_plan_slots WHERE id = ?1 AND plan_id = ?2", &[&slot_id, &id])? else {
        return Some(detail(StatusCode::BAD_REQUEST, "That time is not one of this plan's."));
    };
    let mut set = vec![("status", Sql::Text("confirmed".into())), ("confirmed_slot_id", Sql::Integer(slot_id))];
    let mut session_id = plan.get("session_id").cloned().unwrap_or(Value::Null);
    let has_session = match int(&plan, "session_id").filter(|i| *i != 0) {
        Some(sid) => row(db, "SELECT id FROM game_sessions WHERE id = ?1", &[&sid])?.is_some(),
        None => false,
    };
    if truthy(b.get("create_session")) && !has_session {
        let last: Option<Option<i64>> = match db.query_row(
            "SELECT session_num FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 1", [w.wid], |r| r.get::<_, Option<i64>>(0)) {
            Ok(n) => Some(n),
            Err(rusqlite::Error::QueryReturnedNoRows) => None,
            Err(_) => return None,
        };
        let num = match last {
            Some(n) => n.unwrap_or(0) + 1,
            None => 1,
        };
        let date = match valid_date(b.get("session_date"))? {
            Some(d) => d,
            None => {
                let (y, m, d, ..) = parts(stored(slot.get("starts_at"))?);
                format!("{y:04}-{m:02}-{d:02}")
            }
        };
        let gs = crate::orm::insert(db, "game_sessions", &[
            ("world_id", Sql::Integer(w.wid)), ("title", Sql::Text(st(&plan, "title"))), ("session_num", Sql::Integer(num)), ("session_date", Sql::Text(date)),
        ])?;
        set.push(("session_id", Sql::Integer(gs)));
        session_id = json!(gs);
    }
    crate::orm::update(db, "session_plans", id, &set)?;
    touch(db, w)?;
    ok(json!({"ok": true, "session_id": session_id}))
}

/// reopen (`open` = true: managers only, not when already open) or cancel (the creator or a manager)
fn plan_status(db: &Connection, w: &Who, id: i64, open: bool) -> Option<Response<Body>> {
    if open && !w.is_manager {
        return Some(crate::idpages::refusal(403));
    }
    let plan = or_404!(plan_of(db, w, id));
    if open {
        if st(&plan, "status") == "open" {
            return Some(detail(StatusCode::BAD_REQUEST, "This plan is already open."));
        }
    } else if !can_touch(w, int(&plan, "created_by_user_id")) {
        return Some(crate::idpages::refusal(403));
    }
    crate::orm::update(db, "session_plans", id, &[("status", Sql::Text(if open { "open" } else { "cancelled" }.into())), ("confirmed_slot_id", Sql::Null)])?;
    touch(db, w)?;
    ok(json!({"ok": true}))
}

fn plan_delete(db: &Connection, w: &Who, id: i64) -> Option<Response<Body>> {
    let plan = or_404!(plan_of(db, w, id));
    if !can_touch(w, int(&plan, "created_by_user_id")) {
        return Some(crate::idpages::refusal(403));
    }
    db.execute("DELETE FROM session_plan_votes WHERE plan_id = ?1", [id]).ok()?;
    db.execute("DELETE FROM session_plan_slots WHERE plan_id = ?1", [id]).ok()?;
    db.execute("DELETE FROM session_plans WHERE id = ?1", [id]).ok()?;
    touch(db, w)?;
    ok(json!({"ok": true}))
}

// ── .ics (app/ical.py) ───────────────────────────────────────────────────────

fn escape_text(v: &str) -> String {
    v.replace("\r\n", "\n").replace('\r', "\n").replace('\\', "\\\\").replace(';', "\\;").replace(',', "\\,").replace('\n', "\\n")
}

fn fold_line(line: &str) -> String {
    if line.len() <= 75 {
        return line.to_string();
    }
    let (mut parts, mut cur, mut size, mut limit) = (vec![], String::new(), 0, 75);
    for ch in line.chars() {
        let n = ch.len_utf8();
        if size + n > limit {
            parts.push(std::mem::take(&mut cur));
            size = 0;
            limit = 74;
        }
        cur.push(ch);
        size += n;
    }
    parts.push(cur);
    parts.join("\r\n ")
}

fn utc_stamp(t: i64) -> String {
    let (y, m, d, h, mi, s, _) = parts(t);
    format!("{y:04}{m:02}{d:02}T{h:02}{mi:02}{s:02}Z")
}

/// (uid, start, end, summary, description, location)
type Event = (String, i64, i64, String, String, String);

fn event_for(plan: &Map<String, Value>, slot: &Map<String, Value>, w: &Who) -> Option<Event> {
    let start = stored(slot.get("starts_at"))?;
    Some((format!("plan-{}-{}@nd-world", int(plan, "id")?, w.wid), start, start + dur(plan) * MICROS_PER_MIN, st(plan, "title"), st(plan, "notes"), st(plan, "location")))
}

fn build_calendar(events: &[Event], name: &str) -> String {
    let mut lines: Vec<String> = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nd-world//Session schedule//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
        .iter().map(|s| s.to_string()).collect();
    if !name.is_empty() {
        lines.push(format!("X-WR-CALNAME:{}", escape_text(name)));
    }
    let now = now_micros();
    for (uid, start, end, summary, description, location) in events {
        lines.push("BEGIN:VEVENT".into());
        lines.push(format!("UID:{uid}"));
        lines.push(format!("DTSTAMP:{}", utc_stamp(now)));
        lines.push(format!("DTSTART:{}", utc_stamp(*start)));
        lines.push(format!("DTEND:{}", utc_stamp(*end)));
        lines.push(format!("SUMMARY:{}", escape_text(summary)));
        if !description.is_empty() {
            lines.push(format!("DESCRIPTION:{}", escape_text(description)));
        }
        if !location.is_empty() {
            lines.push(format!("LOCATION:{}", escape_text(location)));
        }
        lines.push("STATUS:CONFIRMED".into());
        lines.push("END:VEVENT".into());
    }
    lines.push("END:VCALENDAR".into());
    lines.iter().map(|l| fold_line(l) + "\r\n").collect()
}

fn ics(text: String, filename: &str) -> Option<Response<Body>> {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/calendar; charset=utf-8")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{filename}\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(text))
        .ok()
}

/// the plan's confirmed slot, when it still belongs to the plan
fn confirmed_slot(db: &Connection, plan: &Map<String, Value>) -> Option<Option<Map<String, Value>>> {
    let Some(sid) = int(plan, "confirmed_slot_id").filter(|i| *i != 0) else { return Some(None) };
    Some(row(db, "SELECT * FROM session_plan_slots WHERE id = ?1", &[&sid])?.filter(|s| int(s, "plan_id") == int(plan, "id")))
}

fn plan_ics(db: &Connection, w: &Who, id: i64) -> Option<Response<Body>> {
    let plan = or_404!(plan_of(db, w, id));
    let slot = confirmed_slot(db, &plan)?;
    let Some(slot) = slot.filter(|_| st(&plan, "status") == "confirmed") else {
        return Some(detail(StatusCode::NOT_FOUND, "No time has been chosen for this plan yet."));
    };
    let ev = event_for(&plan, &slot, w)?;
    ics(build_calendar(&[ev], &st(&w.world, "name")), &format!("session-{id}.ics"))
}

fn confirmed_ics(db: &Connection, w: &Who) -> Option<Response<Body>> {
    let cutoff = now_micros() - 86_400_000_000;
    let mut events = vec![];
    for plan in rows(db, "SELECT * FROM session_plans WHERE world_id = ?1 AND status = 'confirmed' ORDER BY id", &[&w.wid])? {
        if let Some(slot) = confirmed_slot(db, &plan)? {
            if stored(slot.get("starts_at"))? >= cutoff {
                events.push(event_for(&plan, &slot, w)?);
            }
        }
    }
    events.sort_by_key(|e| e.1);
    ics(build_calendar(&events, &format!("{} sessions", st(&w.world, "name"))), "sessions.ics")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(s: &str) -> Option<Result<String, ()>> {
        parse_time(Some(&json!(s))).map(|r| r.map(iso))
    }

    #[test]
    fn times() {
        assert_eq!(t("2031-05-04T18:30:45.5+02:00"), Some(Ok("2031-05-04T16:30:00Z".into())));
        assert_eq!(t(" 2031-05-04T18:30Z "), Some(Ok("2031-05-04T18:30:00Z".into())));
        assert_eq!(t("2031-05-04 18:30-0130"), Some(Ok("2031-05-04T20:00:00Z".into())));
        assert_eq!(t("2031-05-04"), Some(Ok("2031-05-04T00:00:00Z".into())));
        assert_eq!(t("2000-01-01T00:30+01:00"), Some(Err(()))); // 1999 in UTC
        assert_eq!(t("2031-02-30T10:00"), Some(Err(())));
        assert_eq!(t("2031-05-04T25:00"), Some(Err(())));
        assert_eq!(t(""), Some(Err(())));
        assert_eq!(parse_time(Some(&json!(5))), Some(Err(())));
        for unsure in ["20310504T1830", "2031-W01", "2031-05-04T18", "2031-05-04X18:30", "2031-05-04T18:30:00+02", "nope"] {
            assert_eq!(t(unsure), None, "{unsure}");
        }
    }

    #[test]
    fn ints_and_text() {
        assert_eq!(py_int(Some(&json!(" 90 "))), Some(Some(90)));
        assert_eq!(py_int(Some(&json!("9.5"))), Some(None));
        assert_eq!(py_int(Some(&json!(90.7))), Some(Some(90)));
        assert_eq!(py_int(Some(&json!(true))), Some(Some(1)));
        assert_eq!(py_int(Some(&json!("9_0"))), None);
        assert_eq!(duration(Some(&json!(5)), 180), Some(15));
        assert_eq!(duration(Some(&json!("x")), 60), Some(60));
        assert_eq!(text(Some(&json!(0)), 10), Some(String::new()));
        assert_eq!(text(Some(&json!(true)), 10), Some("True".into()));
        assert_eq!(text(Some(&json!("  héllo world ")), 4), Some("héll".into()));
    }

    #[test]
    fn ical_text() {
        assert_eq!(escape_text("a;b,c\\d\r\ne"), "a\\;b\\,c\\\\d\\ne");
        let long = "SUMMARY:".to_string() + &"é".repeat(40);
        let f = fold_line(&long);
        assert!(f.split("\r\n").all(|l| l.len() <= 75));
        assert_eq!(f.replace("\r\n ", ""), long);
    }
}
