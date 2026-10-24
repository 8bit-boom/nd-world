//! Small JSON reads that were Python's only (phase 6, docs/RUST_SWITCH_OFF.md), each answered the way its FastAPI route answers -
//! the auth gate in front (crate::auth::gate) has already refused anyone the route's own middleware would.

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Map, Value};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::Config;

/// GET routes served here: Some(answer), or None for Python
pub fn get(cfg: Arc<Config>, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    match uri.path() {
        "/api/cockpit/workspace" => cockpit_workspace(&cfg, uri, headers),
        "/api/calendar/search" => crate::calendar::search_api(&cfg, uri, headers),
        "/calendar/event-jump" => crate::calendar::event_jump(&cfg, uri, headers),
        "/api/me" => me(&cfg, uri, headers),
        "/api/view-prefs" => view_prefs(&cfg, uri, headers),
        "/api/dice/history" => dice_history(&cfg, uri, headers),
        "/api/display/state" => display_state(&cfg, uri, headers),
        path if id_between(path, "/api/entity/", "/preview").is_some() => entity_preview(&cfg, uri, headers, id_between(path, "/api/entity/", "/preview")?),
        path if path.starts_with("/worlds/switch/") => world_switch(&cfg, uri, headers),
        path if session_media_id(path).is_some() => session_media(&cfg, uri, headers, session_media_id(path)?),
        path if pack_world(path).is_some() => content_pack(&cfg, uri, headers, pack_world(path)?),
        "/api/audio-jobs" => audio_job_list(&cfg, uri, headers),
        "/api/entities/picker" => entities_picker(&cfg, uri, headers),
        path if id_between(path, "/api/ai/assist-job/", "").is_some() => assist_job(&cfg, uri, headers, id_between(path, "/api/ai/assist-job/", "")?),
        path if audio_job_id(path).is_some() => audio_job_status(&cfg, uri, headers, audio_job_id(path)?),
        path => party_vitals(&cfg, uri, headers, vitals_id(path)?),
    }
}

pub fn is_get(path: &str) -> bool {
    matches!(path, "/api/audio-jobs" | "/api/entities/picker" | "/api/cockpit/workspace" | "/api/calendar/search" | "/calendar/event-jump" | "/api/me" | "/api/view-prefs" | "/api/dice/history" | "/api/display/state")
        || session_media_id(path).is_some() || id_between(path, "/api/ai/assist-job/", "").is_some() || id_between(path, "/api/entity/", "/preview").is_some()
        || (path.starts_with("/worlds/switch/") && path.len() > "/worlds/switch/".len() && !path["/worlds/switch/".len()..].contains('/')) || vitals_id(path).is_some() || audio_job_id(path).is_some() || pack_world(path).is_some()
}

/// the id of `/api/parties/<id>/vitals` (plain digits, as FastAPI's int reads them here)
fn vitals_id(path: &str) -> Option<i64> {
    let d = path.strip_prefix("/api/parties/")?.strip_suffix("/vitals")?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

/// `party_vitals`: {"party_id", "name", "members": _member_vitals} - the party page's visibility rule
fn party_vitals(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let party = match db.query_row("SELECT * FROM parties WHERE id = ?1", [id], |r| crate::layout::row_map(r)) {
        Ok(m) => m,
        Err(_) => return Some(crate::idpages::refusal(404)),
    };
    if !crate::idpages::row_world_visible(&db, &p, party.get("world_id")?.as_i64()?, "parties")?.0 {
        return Some(crate::idpages::refusal(404));
    }
    let ids = crate::idpages::party_ids(party.get("member_pc_ids_json"))?;
    let pcs: Vec<serde_json::Map<String, Value>> = if ids.is_empty() { vec![] } else {
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE id IN ({list}) ORDER BY id")).ok()?;
        let r = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        r
    };
    let members = crate::edits::member_vitals(&db, &pcs, Some(4))?;
    crate::edits::json_response(&json!({"party_id": id, "name": party.get("name"), "members": members}))
}

/// `cockpit_get_workspace`: the active world's saved workspace, or null (GM only; no world: 404)
fn cockpit_workspace(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return Some(crate::idpages::refusal(403));
    }
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let ws = w.get("cockpit_ws_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty())
        .and_then(|t| serde_json::from_str::<Value>(t).ok()).filter(|v| v.is_object()).unwrap_or(Value::Null);
    let mut r = crate::edits::json_response(&json!({"workspace": ws}))?;
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    Some(r)
}


/// the id of `/api/audio-jobs/<id>` (plain digits, as FastAPI's int reads them here)
fn audio_job_id(path: &str) -> Option<i64> {
    let d = path.strip_prefix("/api/audio-jobs/")?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

/// `api_audio_job_status` (app/routers/audio_jobs.py): one background job of the active world as `_job_to_dict` writes it; a GM or an
/// assistant, who does not see a GM-tier World Summary or a Code Assist job
fn audio_job_status(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return Some(crate::idpages::refusal(403));
    }
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let wid = w.get("id")?.as_i64()?;
    let Ok(j) = db.query_row("SELECT * FROM audio_jobs WHERE id = ?1 AND world_id = ?2", [id, wid], |r| crate::layout::row_map(r)) else {
        return Some(crate::idpages::refusal(404));
    };
    let s = |k: &str| j.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let purpose = s("purpose").unwrap_or_default();
    if !p.is_gm {
        let hidden = if purpose == "world_summary" {
            s("audience").as_deref() != Some("players")
        } else if purpose == "ai_assist" {
            match serde_json::from_str::<Value>(s("assist_params_json").filter(|t| !t.is_empty()).as_deref().unwrap_or("{}")) {
                Ok(Value::Object(o)) => o.get("op") == Some(&json!("code_edit")),
                Ok(_) => return None, // .get() on a list: Python fails its own way
                Err(_) => false,
            }
        } else {
            false
        };
        if hidden {
            return Some(crate::idpages::refusal(404));
        }
    }
    crate::edits::json_response(&job_dict(&j)?)
}


fn pack_world(path: &str) -> Option<i64> {
    let d = path.strip_prefix("/api/worlds/")?.strip_suffix("/content-pack")?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

/// `_sections_from_body`: (leading description, {heading: text}) split on "## Heading" lines; None for whitespace Python's `\s` and
/// Rust's disagree about
fn sections_from_body(body: &str) -> Option<(String, Map<String, Value>)> {
    use nd_render::text::py_strip;
    if body.chars().any(|c| ('\u{1c}'..='\u{1f}').contains(&c)) {
        return None;
    }
    if py_strip(body).is_empty() {
        return Some((String::new(), Map::new()));
    }
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"(?m)^##\s+(.+)$").unwrap());
    let mut pieces: Vec<(Option<String>, String)> = vec![];
    let mut last = 0;
    let mut heading: Option<String> = None;
    for c in re.captures_iter(body) {
        let m = c.get(0)?;
        pieces.push((heading.take(), body[last..m.start()].to_string()));
        heading = Some(c.get(1)?.as_str().to_string());
        last = m.end();
    }
    pieces.push((heading, body[last..].to_string()));
    let pre = py_strip(&pieces[0].1).to_string();
    let mut sections = Map::new();
    for (h, text) in pieces.into_iter().skip(1) {
        let h = py_strip(&h?).to_string();
        if !h.is_empty() {
            sections.insert(h, json!(py_strip(&text)));
        }
    }
    if sections.is_empty() {
        let mut m = Map::new();
        m.insert("Description".into(), json!(py_strip(body)));
        return Some((String::new(), m));
    }
    Some((pre, sections))
}

/// `api_content_pack` (app/routers/importer.py): a world's homebrew races / professions / feats / items in the Android app's JSON shape,
/// for any member of the world
fn content_pack(cfg: &Config, uri: &Uri, headers: &HeaderMap, wid: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let exists = db.query_row("SELECT 1 FROM worlds WHERE id = ?1", [wid], |_| Ok(())).is_ok();
    let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_ok();
    if !exists || !(p.is_gm || member) {
        return Some(crate::idpages::refusal(404));
    }
    let mut st = db.prepare("SELECT * FROM entities WHERE world_id = ?1 AND kind IN ('race', 'profession', 'feat', 'item') ORDER BY name, id").ok()?;
    let ents = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut pack = json!({"races": [], "professions": [], "feats": [], "items": []});
    static RACE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static PROF: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let race = RACE.get_or_init(|| regex::Regex::new(r"^Race Feats/([^/]+)/(.+)$").unwrap());
    let prof = PROF.get_or_init(|| regex::Regex::new(r"^Profession Feats/([^/]+)$").unwrap());
    for e in ents {
        let t = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let (pre, sections) = sections_from_body(&t("body"))?;
        let cf = serde_json::from_str::<Value>(e.get("custom_fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}"));
        let bonuses = match cf {
            Ok(Value::Object(o)) => o.get("bonuses").filter(|b| b.is_object()).cloned().unwrap_or(json!({})),
            _ => json!({}),
        };
        let kind = t("kind");
        let summary = t("summary");
        let mut item = json!({"id": format!("custom_{wid}_{}", e.get("id")?.as_i64()?), "name": e.get("name"), "type": "", "tags": t("tags"),
            "description": if summary.is_empty() { pre } else { summary }, "sections": sections, "filepath": "", "specialAttributes": [],
            "bonuses": bonuses, "image_url": t("image_url"), "isCustom": true});
        let sub = nd_render::text::py_strip(&t("subtype")).to_lowercase();
        let o = item.as_object_mut()?;
        match kind.as_str() {
            "race" | "profession" => {
                let st = t("subtype");
                o.insert("tier".into(), json!(nd_render::text::py_strip(if st.is_empty() { "standard" } else { &st }).to_lowercase()));
            }
            "feat" => {
                let cat = match sub.as_str() {
                    "common feat" => "Common", "origin feat" => "Origin", "profession feat" => "Profession",
                    "profession ability" => "Profession Ability", "psy power" => "Psy Power", "race feat" => "Race", _ => "Common",
                };
                o.insert("category".into(), json!(cat));
                o.insert("rank".into(), json!(""));
                let folder = t("folder");
                if folder.contains('\n') {
                    return None; // `$` before a trailing newline: Python's regex reads it its own way
                }
                if let Some(c) = race.captures(&folder) {
                    o.insert("associatedRace".into(), json!(c.get(1)?.as_str()));
                    o.insert("rank".into(), json!(c.get(2)?.as_str()));
                } else if let Some(c) = prof.captures(&folder) {
                    o.insert("associatedProfession".into(), json!(c.get(1)?.as_str()));
                }
            }
            _ => {
                let cat = match sub.as_str() {
                    "weapon" => "Weapon", "armor" => "Armor", "augment" => "Augment", "bio-augmentation" => "Bio Augment",
                    "drone" => "Drone", "vehicle" => "Vehicle", "husk" => "Husk", _ => "Special",
                };
                o.insert("category".into(), json!(cat));
            }
        }
        let bucket = match kind.as_str() { "race" => "races", "profession" => "professions", "feat" => "feats", _ => "items" };
        pack[bucket].as_array_mut()?.push(item);
    }
    crate::edits::json_response(&pack)
}


/// a plain-digits id between `prefix` and `suffix`, as FastAPI's int reads it here
fn id_between(path: &str, prefix: &str, suffix: &str) -> Option<i64> {
    let d = path.strip_prefix(prefix)?.strip_suffix(suffix)?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

fn session_media_id(path: &str) -> Option<i64> {
    id_between(path, "/api/sessions/", "/media")
}

fn page(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<(rusqlite::Connection, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    Some((db, p))
}

fn rows_of(db: &rusqlite::Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

fn iso_of(v: Option<&Value>) -> Option<String> {
    let raw = v?.get("__dt__").or(v)?.as_str()?;
    Some(nd_render::pyvalue::PyDateTime::parse(raw)?.isoformat())
}

/// `api_me` (app/routers/characters.py): the signed-in person and their worlds with their characters there
fn me(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let user = rows_of(&db, "SELECT id, email, is_gm FROM users WHERE id = ?1", &[&p.user_id])?.into_iter().next()?;
    let worlds = if p.is_gm {
        rows_of(&db, "SELECT * FROM worlds ORDER BY name, id", &[])?
    } else {
        rows_of(&db, "SELECT w.* FROM worlds w JOIN world_memberships m ON m.world_id = w.id WHERE m.user_id = ?1 ORDER BY w.name, m.id", &[&p.user_id])?
    };
    let mut list = vec![];
    for w in worlds {
        let wid = w.get("id")?.as_i64()?;
        let mine: Vec<i64> = rows_of(&db, "SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id", &[&wid, &p.user_id])?
            .iter().filter_map(|r| r.get("id")?.as_i64()).collect();
        let max = match w.get("max_characters_per_player") {
            Some(Value::Number(n)) if n.as_i64().map_or(false, |v| v != 0) => n.as_i64()?.max(1),
            None | Some(Value::Null) | Some(Value::Number(_)) => 1,
            _ => return None,
        };
        list.push(json!({"id": wid, "name": w.get("name"), "slug": w.get("slug"), "character_id": mine.first(), "character_ids": mine, "max_characters": max}));
    }
    crate::edits::json_response(&json!({"user": {"id": user.get("id"), "email": user.get("email"), "is_gm": p.is_gm}, "worlds": list}))
}

/// `view_prefs_get`: {section: cleaned choice} for the signed-in person
fn view_prefs(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let mut out = Map::new();
    for r in rows_of(&db, "SELECT section, prefs_json FROM user_view_prefs WHERE user_id = ?1 ORDER BY id", &[&p.user_id])? {
        let section = r.get("section")?.as_str()?.to_string();
        let raw: Value = match serde_json::from_str(r.get("prefs_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")) {
            Ok(v) => v,
            Err(_) => continue,
        };
        out.insert(section.clone(), nd_render::app::clean(&section, &raw)?);
    }
    crate::edits::json_response(&Value::Object(out))
}

/// `api_dice_history`: the active world's last 50 rolls, newest first
fn dice_history(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    if !crate::pages::section_ok(&p, Some(w), "dice")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let mut rolls = vec![];
    for r in rows_of(&db, "SELECT * FROM dice_rolls WHERE world_id = ?1 ORDER BY created_at DESC, id DESC LIMIT 50", &[&wid])? {
        let breakdown: Value = serde_json::from_str(r.get("breakdown").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")).ok()?;
        rolls.push(json!({"id": r.get("id"), "user_name": r.get("user_name"), "notation": r.get("notation"),
            "label": r.get("label").and_then(|v| v.as_str()).unwrap_or(""), "breakdown": breakdown, "total": r.get("total"),
            "created_at": iso_of(r.get("created_at")).map(|t| format!("{t}Z"))}));
    }
    crate::edits::json_response(&json!({"rolls": rolls}))
}

/// `display_state`: the second screen's stage (shared_state "stage:<world>"), GM only
fn display_state(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return Some(crate::idpages::refusal(403));
    }
    let Some(w) = p.world.as_ref() else { return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, "No world selected")) };
    let wid = w.get("id")?.as_i64()?;
    let raw: Option<String> = db.query_row("SELECT value FROM shared_state WHERE key = ?1", [format!("stage:{wid}")], |r| r.get(0)).ok().flatten();
    let s = raw.and_then(|t| serde_json::from_str::<Value>(&t).ok()).filter(|v| !v.is_null()).unwrap_or(json!({"seq": 0, "current": null, "recent": []}));
    let s = s.as_object()?;
    crate::edits::json_response(&json!({"seq": s.get("seq")?, "current": s.get("current")?, "recent": s.get("recent")?.as_array()?}))
}

/// `media_list`: the GM's session media panel, for a session of the active world the caller may edit Sessions in
fn session_media(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    if !crate::pages::edit_level(&p, w, "sessions")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let owner: Option<i64> = db.query_row("SELECT world_id FROM game_sessions WHERE id = ?1", [id], |r| r.get(0)).ok();
    if owner != Some(wid) {
        return Some(crate::idpages::refusal(404));
    }
    crate::edits::json_response(&crate::edits::sm_panel_db(&db, id)?)
}


/// `entity_preview` (app/main.py): the hover popup's data for an entity the viewer may see (`_entity_view_gate`: every refusal a 404)
fn entity_preview(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(e) = rows_of(&db, "SELECT * FROM entities WHERE id = ?1", &[&id])?.into_iter().next() else { return Some(crate::idpages::refusal(404)) };
    let Some(wid) = e.get("world_id").and_then(|v| v.as_i64()) else { return None };
    let Some(w) = rows_of(&db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else { return Some(crate::idpages::refusal(404)) };
    let t = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    if !p.is_gm {
        let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_ok();
        let s = |k: &str| w.get(k).and_then(|v| v.as_str());
        if !member || nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), &format!("kind_{}", t("kind")))? == "none" {
            return Some(crate::idpages::refusal(404));
        }
        if e.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) == 0
            && db.query_row("SELECT 1 FROM entity_player_access WHERE entity_id = ?1 AND user_id = ?2", [id, p.user_id], |_| Ok(())).is_err() {
            return Some(crate::idpages::refusal(404));
        }
    }
    let strip = |text: &str| nd_perm::gmonly::strip_gm_only(text, Some(p.user_id), nd_render::now_minute());
    let body = if p.is_gm { e.get("body").cloned().unwrap_or(Value::Null) } else {
        match e.get("body") {
            Some(Value::String(b)) => json!(strip(b)?),
            other => other.cloned().unwrap_or(Value::Null), // None / "" stay as they are (strip_gm_only returns falsy text unchanged)
        }
    };
    let body_s = body.as_str().unwrap_or("").to_string();
    let summary = if p.is_gm { e.get("summary").cloned().unwrap_or(Value::Null) } else { json!(strip(&t("summary"))?) };
    let (_, icons, _) = nd_render::app::effective_kind_tables(Some(&w))?;
    let image = e.get("image_url").cloned().unwrap_or(Value::Null);
    let thumb = match image.as_str().filter(|s| !s.is_empty()) {
        Some(u) => json!(nd_render::thumb_url(u)?),
        None => image.clone(),
    };
    let tags: Vec<String> = t("tags").split(',').map(|x| nd_render::text::py_strip(x).to_string()).filter(|x| !x.is_empty()).collect();
    let cf = Some(t("custom_fields_json")).filter(|s| !s.is_empty()).unwrap_or_else(|| "{}".into());
    crate::edits::json_response(&json!({"id": id, "name": e.get("name"), "kind": e.get("kind"), "kind_icon": icons.get(&t("kind")).cloned().unwrap_or(json!("")),
        "subtype": e.get("subtype"), "summary": summary, "image_url": image, "thumb_url": thumb, "tags": tags,
        "body_html": if body_s.is_empty() { String::new() } else { nd_render::md::render_md(&body_s) }, "body": body_s, "custom_fields_json": cf}))
}

/// `world_switch` (app/main.py): remember the world (the active_world cookie) and go back where the person was
fn world_switch(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let slug = percent_encoding::percent_decode_str(uri.path().strip_prefix("/worlds/switch/")?).decode_utf8().ok()?.to_string();
    // a cookie value SimpleCookie would quote: Python's
    if slug.is_empty() || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.') {
        return None;
    }
    let Some(w) = rows_of(&db, "SELECT id FROM worlds WHERE slug = ?1 ORDER BY id LIMIT 1", &[&slug])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let wid = w.get("id")?.as_i64()?;
    if !p.is_gm && db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_err() {
        return Some(crate::idpages::refusal(404));
    }
    let next = crate::pages::query_param(uri, "next");
    let dest = crate::pages::safe_next_url(Some(next.as_deref().unwrap_or("/")));
    let loc = format!("{dest}{}w={slug}", if dest.contains('?') { "&" } else { "?" });
    Response::builder().status(303).header("location", loc)
        .header("set-cookie", format!("active_world={slug}; Max-Age=31536000; Path=/; SameSite=lax"))
        .header("x-nd-served-by", "gateway").body(Body::empty()).ok()
}


/// `_job_to_dict(job)` (app/routers/audio_jobs.py)
fn job_dict(j: &Map<String, Value>) -> Option<Value> {
    let s = |k: &str| j.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let id = j.get("id")?.as_i64()?;
    let purpose = s("purpose").unwrap_or_default();
    let label = match purpose.as_str() {
        "session_recap" => "Session Recap",
        "attachment" => "Voice Attachment",
        "condense" => "Condense",
        "facts_parse" => "Facts",
        "session_log_recap" => "Session Log recap",
        "ai_assist" => "AI Assist",
        "world_summary" => "World Summary",
        other => other,
    };
    let iso = |k: &str| match j.get(k).and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
        Some(d) => json!(d.isoformat()),
        None => Value::Null,
    };
    let flag = |k: &str| j.get(k).and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    let v = |k: &str| j.get(k).cloned().unwrap_or(Value::Null);
    let error = s("error").unwrap_or_default();
    let pending = match serde_json::from_str::<Value>(s("pending_facts_json").filter(|t| !t.is_empty()).as_deref().unwrap_or("[]")) {
        Ok(Value::Array(a)) => Value::Array(a),
        _ => json!([]),
    };
    let out = json!({
        "id": id, "purpose": purpose, "purpose_label": label, "filename": v("filename"), "status": v("status"), "error": v("error"),
        "transcript": v("transcript"), "recap": v("recap"), "model": s("model").unwrap_or_default(),
        "extra_instructions": s("extra_instructions").unwrap_or_default(), "result_json": s("result_json").unwrap_or_default(),
        "attachment_url": v("attachment_url"), "game_session_id": v("game_session_id"),
        "chunk_current": v("chunk_current"), "chunk_total": v("chunk_total"),
        "run_started_at": iso("run_started_at"), "finished_at": iso("finished_at"), "created_at": iso("created_at"), "updated_at": iso("updated_at"),
        "resumed_count": v("resumed_count"),
        "think": j.get("think").and_then(|t| t.as_i64()).map_or(json!(true), |t| json!(t != 0)),
        "fit_context": flag("fit_context"), "strictness": s("condense_strictness").filter(|t| !t.is_empty()).unwrap_or_else(|| "guideline".into()),
        "use_rag": flag("use_rag"), "rag_entity_limit": v("rag_entity_limit"), "rag_notes_limit": v("rag_notes_limit"),
        "resumable": s("status").as_deref() == Some("interrupted"),
        "think_fallback": flag("think_fallback"), "think_rejected": flag("think_rejected"), "think_token_fallback": flag("think_token_fallback"),
        "expanded_thinking": flag("expanded_thinking"),
        "thinking_starved": error.starts_with("[empty response") && error.contains("hidden \"thinking\""),
        "pending_facts": pending,
    });
    Some(out)
}


/// `api_audio_job_list`: the active world's jobs, newest first, filtered and paginated (50 a page); an assistant does not see GM-tier
/// World Summaries or Code Assist jobs
fn audio_job_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    if !crate::pages::section_ok(&p, Some(w), "background_jobs")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let q = |k: &str| crate::pages::query_param(uri, k);
    let int_param = |k: &str| -> Option<Option<i64>> {
        match q(k) {
            None => Some(None),
            Some(v) => { let t = v.trim(); (!t.is_empty() && t.trim_start_matches('-').bytes().all(|b| b.is_ascii_digit())).then(|| t.parse().ok().map(Some))? }
        }
    };
    let page_no = int_param("page")?.unwrap_or(1).max(1);
    let gsid = int_param("game_session_id")?;
    let mut sql = "FROM audio_jobs WHERE world_id = ?1".to_string();
    let mut params: Vec<rusqlite::types::Value> = vec![wid.into()];
    if !p.is_gm {
        sql += " AND (purpose != 'world_summary' OR id IN (SELECT id FROM audio_jobs WHERE purpose = 'world_summary' AND audience = 'players'))";
    }
    if let Some(purpose) = q("purpose").filter(|s| !s.is_empty()) {
        params.push(purpose.into());
        sql += &format!(" AND purpose = ?{}", params.len());
    }
    if let Some(status) = q("status").filter(|s| !s.is_empty()) {
        if status == "running" {
            sql += " AND status IN ('pending', 'transcribing', 'summarizing')";
        } else {
            params.push(status.into());
            sql += &format!(" AND status = ?{}", params.len());
        }
    }
    if let Some(g) = gsid {
        params.push(g.into());
        sql += &format!(" AND game_session_id = ?{}", params.len());
    }
    let total: i64 = db.query_row(&format!("SELECT count(*) {sql}"), rusqlite::params_from_iter(params.iter()), |r| r.get(0)).ok()?;
    let total_pages = ((total + 49) / 50).max(1);
    let page_no = page_no.min(total_pages);
    let mut st = db.prepare(&format!("SELECT * {sql} ORDER BY created_at DESC, id LIMIT 50 OFFSET {}", (page_no - 1) * 50)).ok()?;
    let jobs = st.query_map(rusqlite::params_from_iter(params.iter()), |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut out = vec![];
    for j in jobs {
        if !p.is_gm && j.get("purpose").and_then(|v| v.as_str()) == Some("ai_assist") {
            let raw = j.get("assist_params_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
            match serde_json::from_str::<Value>(raw) {
                Ok(Value::Object(o)) if o.get("op") == Some(&json!("code_edit")) => continue,
                Ok(Value::Object(_)) | Err(_) => {}
                Ok(_) => return None,
            }
        }
        out.push(job_dict(&j)?);
    }
    crate::edits::json_response(&json!({"jobs": out, "page": page_no, "total_pages": total_pages}))
}

/// `api_ai_assist_job_status`: an AI-assist job's status, and its result once done
fn assist_job(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let Some(j) = rows_of(&db, "SELECT * FROM audio_jobs WHERE id = ?1", &[&id])?.into_iter().next() else { return Some(crate::idpages::refusal(404)) };
    if j.get("purpose").and_then(|v| v.as_str()) != Some("ai_assist") || (wid.is_some() && j.get("world_id").and_then(|v| v.as_i64()) != wid) {
        return Some(crate::idpages::refusal(404));
    }
    let status = j.get("status").cloned().unwrap_or(Value::Null);
    let mut out = json!({"id": id, "status": status, "error": j.get("error").and_then(|v| v.as_str()).unwrap_or("")});
    let result = j.get("result_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty());
    if let (Some(r), true) = (result, status == json!("done")) {
        out["result"] = match serde_json::from_str::<Value>(r) {
            Ok(v) => v,
            Err(_) if r.contains("NaN") || r.contains("Infinity") => return None,
            Err(_) => json!({"op": "?", "mode": "text", "text": r}),
        };
    }
    crate::edits::json_response(&out)
}


/// json.dumps(v, separators=(",", ":")) with Python's default ensure_ascii
fn dumps_ascii_compact(v: &Value) -> String {
    let raw = serde_json::to_string(v).unwrap_or_default();
    let mut out = String::with_capacity(raw.len());
    for c in raw.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut buf = [0u16; 2];
            for u in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{:04x}", u));
            }
        }
    }
    out
}

/// `api_entities_picker` (app/main.py): {id, name, kind, folder} of every entity the viewer may see in the active world, with an ETag
fn entities_picker(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use md5::Digest;
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    let wid = w.get("id")?.as_i64()?;
    let exclude = match crate::pages::query_param(uri, "exclude_id") {
        None => None,
        Some(v) if !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()) && v.len() < 16 => Some(v.parse::<i64>().ok()?),
        Some(_) => return None, // FastAPI's 422 / int coercions: Python's
    };
    let vis = if p.is_gm { String::new() } else {
        format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let ex = exclude.map(|e| format!(" AND id != {e}")).unwrap_or_default();
    let mut st = db.prepare(&format!("SELECT id, name, kind, folder FROM entities WHERE world_id = ?1{ex}{vis} ORDER BY kind, folder, name, id")).ok()?;
    let rows: Vec<Value> = st.query_map([wid], |r| Ok(json!({"id": r.get::<_, i64>(0)?, "name": r.get::<_, Option<String>>(1)?,
        "kind": r.get::<_, Option<String>>(2)?, "folder": r.get::<_, Option<String>>(3)?.unwrap_or_default()}))).ok()?.collect::<Result<_, _>>().ok()?;
    let payload = dumps_ascii_compact(&json!({"entities": rows}));
    let etag = format!("\"{:x}\"", md5::Md5::digest(payload.as_bytes()));
    let b = Response::builder().header("etag", &etag).header("cache-control", "private, no-cache").header("x-nd-served-by", "gateway");
    if headers.get("if-none-match").and_then(|v| v.to_str().ok()) == Some(etag.as_str()) {
        return b.status(304).body(Body::empty()).ok();
    }
    b.status(200).header("content-type", "application/json").body(Body::from(payload)).ok()
}


/// POST routes served here (small JSON reads that take a body)
pub fn is_post(path: &str) -> bool {
    matches!(path, "/api/ai/world-context-smart" | "/api/ai/world-context-player" | "/api/ai/unsloth/prefs")
}

pub fn post(cfg: Arc<Config>, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    match uri.path() {
        "/api/ai/world-context-smart" => world_context_smart(&cfg, uri, headers, body),
        "/api/ai/world-context-player" => world_context_player(&cfg, uri, headers, body),
        "/api/ai/unsloth/prefs" => unsloth_prefs(&cfg, uri, headers, body),
        _ => None,
    }
}

/// `unsloth_prefs_set` (app/routers/ai.py, GM-only by the auth gate): the TTS / STT choices and the Studio console URL in ai_models.json
/// beside the database, each written in turn as Python's set_* calls do (so an invalid URL still leaves the fields before it saved)
fn unsloth_prefs(cfg: &Config, uri: &Uri, headers: &HeaderMap, raw: &[u8]) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let b = match serde_json::from_slice::<Value>(raw).ok()? {
        Value::Object(o) if crate::edits::plain_numbers(raw) => o,
        _ => return None, // `body: dict` refuses anything else (a 422): Python's
    };
    // str(x or "").strip()
    let text = |k: &str| -> Option<String> {
        match b.get(k) {
            Some(Value::String(s)) => Some(crate::edits::py_strip_pub(s)),
            Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()),
            Some(v) if !crate::edits::py_truthy_pub(Some(v)) => Some(String::new()),
            _ => None,
        }
    };
    let path = cfg.db_path.parent()?.join("ai_models.json");
    let mut data = match std::fs::read_to_string(&path) {
        Ok(t) => match serde_json::from_str::<Value>(&t) {
            Ok(Value::Object(o)) => o,
            Ok(_) => return None, // a list or the like: Python's TypeError
            Err(_) if t.contains("NaN") || t.contains("Infinity") => return None,
            Err(_) => serde_json::from_value(json!({"custom": [], "hidden": []})).ok()?,
        },
        Err(_) => serde_json::from_value(json!({"custom": [], "hidden": []})).ok()?,
    };
    let mut changed = false;
    for k in ["tts_model", "tts_voice", "tts_instructions", "tts_language", "stt_model"] {
        if b.contains_key(k) {
            data.insert(k.into(), json!(text(k)?));
            changed = true;
        }
    }
    let save = |data: &Map<String, Value>| std::fs::write(&path, nd_render::py_dumps_indent(&Value::Object(data.clone()), 2)).ok();
    if b.contains_key("studio_console_url") {
        let url = match b.get("studio_console_url") {
            Some(Value::String(s)) => crate::edits::py_strip_pub(s),
            Some(v) if !crate::edits::py_truthy_pub(Some(v)) => String::new(),
            _ => return None,
        };
        // normalize_console_url: a bare host[:port][/path] gets http://
        static BARE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        let bare = BARE.get_or_init(|| regex::Regex::new(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?(?:/\S*)?$").unwrap());
        let host = url.split('/').next().unwrap_or("");
        let url = if !url.is_empty() && !url.contains("://") && bare.is_match(&url) && (host.contains('.') || host.contains(':')) { format!("http://{url}") } else { url };
        static OK: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        if !url.is_empty() && !OK.get_or_init(|| regex::Regex::new(r"(?i)^https?://[^\s/]").unwrap()).is_match(&url) {
            if changed {
                save(&data)?;
            }
            return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "Studio console URL must start with http:// or https://"));
        }
        data.insert("studio_console_url".into(), json!(url));
        changed = true;
    }
    if changed {
        save(&data)?;
    }
    crate::edits::json_response(&json!({"ok": true}))
}

/// a pydantic body field: absent = the default, a JSON int as it is; anything pydantic would coerce or refuse (Python's 422) = None
fn body_int(b: &Map<String, Value>, k: &str, default: i64) -> Option<i64> {
    match b.get(k) {
        None => Some(default),
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        _ => None,
    }
}

/// `ai_world_context_player` (app/routers/ai.py): the player-safe Smart Context of the /ai-chat page - the same retrieval through the
/// caller's own visibility (a GM's is unfiltered); a player only when the world lets players ask the AI
fn world_context_player(cfg: &Config, uri: &Uri, headers: &HeaderMap, raw: &[u8]) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    // PlayerContextBody: query a string, the limits whole numbers or null (anything else is pydantic's 422: Python's)
    let b = match serde_json::from_slice::<Value>(raw).ok()? {
        Value::Object(o) if crate::edits::plain_numbers(raw) => o,
        _ => return None,
    };
    let query = match b.get("query") {
        None => String::new(),
        Some(Value::String(q)) => q.clone(),
        _ => return None,
    };
    let opt_int = |k: &str| -> Option<Option<i64>> {
        match b.get(k) {
            None | Some(Value::Null) => Some(None),
            Some(Value::Number(n)) if n.is_i64() => Some(n.as_i64()),
            _ => None,
        }
    };
    let (limit, notes_limit) = (opt_int("limit")?, opt_int("notes_limit")?);
    let world = p.world.clone();
    if !p.is_gm {
        let open = world.as_ref().is_some_and(|w| crate::edits::py_truthy_pub(w.get("players_can_ask_ai")) || crate::edits::py_truthy_pub(w.get("players_can_use_ai_chat")));
        if !open {
            return Some(crate::idpages::refusal(403));
        }
    }
    let Some(w) = world else {
        return crate::edits::json_response(&json!({"context": ""}));
    };
    if crate::edits::py_strip_pub(&query).is_empty() {
        return crate::edits::json_response(&json!({"context": ""}));
    }
    let entity_limit = limit.or_else(|| w.get("ai_chat_rag_entity_limit").and_then(|v| v.as_i64())).unwrap_or(15).clamp(0, 100) as usize;
    let notes_limit = notes_limit.or_else(|| w.get("ai_chat_rag_notes_limit").and_then(|v| v.as_i64())).unwrap_or(3).clamp(0, 100) as usize;
    let player = if p.is_gm { None } else { Some(p.user_id) };
    let (context, non_notes, notes) = crate::retrieval::smart_world_context_as(&db, w.get("id")?.as_i64()?, &query, entity_limit, notes_limit, player)?;
    crate::edits::json_response(&json!({"context": context, "count": non_notes.len(), "notes": notes.len()}))
}

/// `ai_world_context_smart` (app/main.py): the AI Chat's Smart Context for a GM (an assistant's filtered view is Python's)
fn world_context_smart(cfg: &Config, uri: &Uri, headers: &HeaderMap, raw: &[u8]) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let b = match serde_json::from_slice::<Value>(raw).ok()? {
        Value::Object(o) if crate::edits::plain_numbers(raw) => o,
        _ => return None,
    };
    let query = match b.get("query") {
        None => String::new(),
        Some(Value::String(q)) => q.chars().take(4000).collect(),
        _ => return None,
    };
    let limit = body_int(&b, "limit", 25)?.clamp(0, 100) as usize;
    let notes_limit = body_int(&b, "notes_limit", 5)?.clamp(0, 100) as usize;
    let Some(w) = p.world.as_ref() else {
        return crate::edits::json_response(&json!({"context": "", "count": 0, "notes": 0, "entities": []}));
    };
    let (context, non_notes, notes) = crate::retrieval::smart_world_context_full(&db, w.get("id")?.as_i64()?, &query, limit, notes_limit)?;
    let entities: Vec<Value> = non_notes.iter().chain(notes.iter()).map(|e| json!({"id": e.get("id"), "name": e.get("name"), "kind": e.get("kind")})).collect();
    crate::edits::json_response(&json!({"context": context, "count": non_notes.len(), "notes": notes.len(), "entities": entities}))
}
