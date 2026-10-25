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
        path if id_between(path, "/entity/", "/download.md").is_some() => entity_download(&cfg, uri, headers, id_between(path, "/entity/", "/download.md")?),
        path if id_between(path, "/api/characters/", "/vitals").is_some() => pc_vitals(&cfg, uri, headers, id_between(path, "/api/characters/", "/vitals")?),
        path if id_between(path, "/api/characters/", "/sync").is_some() => pc_sync_get(&cfg, uri, headers, id_between(path, "/api/characters/", "/sync")?),
        path if id_between(path, "/api/characters/", "/advance").is_some() => pc_advance_get(&cfg, uri, headers, id_between(path, "/api/characters/", "/advance")?),
        path if id_between(path, "/api/characters/", "/ability-cards").is_some() => pc_ability_cards(&cfg, uri, headers, id_between(path, "/api/characters/", "/ability-cards")?),
        path if id_between(path, "/characters/", "/export.json").is_some() => pc_export_json(&cfg, uri, headers, id_between(path, "/characters/", "/export.json")?),
        path if id_between(path, "/characters/", "/export.md").is_some() => pc_export_md(&cfg, uri, headers, id_between(path, "/characters/", "/export.md")?),
        path if path.starts_with("/worlds/switch/") => world_switch(&cfg, uri, headers),
        path if session_media_id(path).is_some() => session_media(&cfg, uri, headers, session_media_id(path)?),
        path if pack_world(path).is_some() => content_pack(&cfg, uri, headers, pack_world(path)?),
        "/api/audio-jobs" => audio_job_list(&cfg, uri, headers),
        "/api/entities/picker" => entities_picker(&cfg, uri, headers),
        "/api/cockpit/player-board" => cockpit_player_board(&cfg, uri, headers),
        "/api/hover-preview/config" => hover_preview_config(&cfg, uri, headers),
        path if id_between(path, "/sessions/", "/npc-candidates").is_some() => session_npc_candidates(&cfg, uri, headers, id_between(path, "/sessions/", "/npc-candidates")?),
        path if id_between(path, "/sessions/", "/summary.md").is_some() => session_summary_md(&cfg, uri, headers, id_between(path, "/sessions/", "/summary.md")?),
        path if id_between(path, "/sessions/", "/transcript.md").is_some() => session_transcript_md(&cfg, uri, headers, id_between(path, "/sessions/", "/transcript.md")?),
        path if id_between(path, "/api/ai/assist-job/", "").is_some() => assist_job(&cfg, uri, headers, id_between(path, "/api/ai/assist-job/", "")?),
        path if audio_job_id(path).is_some() => audio_job_status(&cfg, uri, headers, audio_job_id(path)?),
        path if media_run_id(path).is_some() => media_rename_run(&cfg, uri, headers, media_run_id(path)?),
        path if session_audio_job_id(path).is_some() => session_audio_job_status(&cfg, uri, headers, session_audio_job_id(path)?),
        path if player_image_job_id(path).is_some() => player_image_job_status(&cfg, uri, headers, player_image_job_id(path)?),
        path => party_vitals(&cfg, uri, headers, vitals_id(path)?),
    }
}

pub fn is_get(path: &str) -> bool {
    matches!(path, "/api/hover-preview/config" | "/api/audio-jobs" | "/api/entities/picker" | "/api/cockpit/workspace" | "/api/cockpit/player-board" | "/api/calendar/search" | "/calendar/event-jump" | "/api/me" | "/api/view-prefs" | "/api/dice/history" | "/api/display/state")
        || session_media_id(path).is_some() || id_between(path, "/api/ai/assist-job/", "").is_some() || id_between(path, "/api/entity/", "/preview").is_some() || id_between(path, "/entity/", "/download.md").is_some()
        || (path.starts_with("/worlds/switch/") && path.len() > "/worlds/switch/".len() && !path["/worlds/switch/".len()..].contains('/')) || vitals_id(path).is_some() || audio_job_id(path).is_some() || pack_world(path).is_some()
        || media_run_id(path).is_some() || session_audio_job_id(path).is_some() || player_image_job_id(path).is_some()
        || id_between(path, "/sessions/", "/npc-candidates").is_some() || id_between(path, "/sessions/", "/summary.md").is_some() || id_between(path, "/sessions/", "/transcript.md").is_some()
        || id_between(path, "/api/characters/", "/vitals").is_some() || id_between(path, "/api/characters/", "/sync").is_some() || id_between(path, "/api/characters/", "/advance").is_some() || id_between(path, "/api/characters/", "/ability-cards").is_some() || id_between(path, "/characters/", "/export.json").is_some() || id_between(path, "/characters/", "/export.md").is_some()
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
    if !crate::idpages::row_world_visible_pub(&db, &p, party.get("world_id")?.as_i64()?, "parties")?.0 {
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

/// a trailing plain-digits path id under `prefix`, as FastAPI's `{run_id: int}` reads it (no leading zeros, fits i64)
fn trailing_id(path: &str, prefix: &str) -> Option<i64> {
    let d = path.strip_prefix(prefix)?;
    (!d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'))).then(|| d.parse().ok())?
}

/// the id of `/api/media-rename/runs/<id>` (the GET status; the /cancel and /delete POSTs are ai.rs's)
fn media_run_id(path: &str) -> Option<i64> {
    trailing_id(path, "/api/media-rename/runs/")
}

/// the id of `/api/sessions/ai/audio-jobs/<id>` (from-clip and complete are POSTs of their own)
fn session_audio_job_id(path: &str) -> Option<i64> {
    trailing_id(path, "/api/sessions/ai/audio-jobs/")
}

/// the id of `/api/ai/imagegen/player/jobs/<id>`
fn player_image_job_id(path: &str) -> Option<i64> {
    trailing_id(path, "/api/ai/imagegen/player/jobs/")
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

/// a stored JSON column as `json.loads(col or "[]")` reads it, with the length Python's `len()` gives a list or a dict
/// (a number or a bool has no len - Python's own 500; a string is measured by its characters, too odd to answer here)
fn stored_collection(r: &Map<String, Value>, key: &str) -> Option<(Value, usize)> {
    let t = r.get(key).and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(t).ok()? {
        Value::Array(a) => Some((Value::Array(a.clone()), a.len())),
        Value::Object(o) => Some((Value::Object(o.clone()), o.len())),
        _ => None,
    }
}

/// a DATETIME column as `.strftime(...)` writes it (None in, null out; unparseable text is a row Python could not load either)
fn dt_strftime(r: &Map<String, Value>, key: &str, fmt: &str) -> Option<Value> {
    match r.get(key) {
        None | Some(Value::Null) => Some(Value::Null),
        Some(v) => {
            let t = v.get("__dt__").or(Some(v)).and_then(|x| x.as_str())?;
            match nd_render::pyvalue::PyDateTime::parse(t) {
                Some(d) => Some(json!(d.strftime(fmt).ok()?)),
                None => None,
            }
        }
    }
}

/// a DATETIME column as `.isoformat()` writes it (None in, null out)
fn dt_iso(r: &Map<String, Value>, key: &str) -> Option<Value> {
    match r.get(key) {
        None | Some(Value::Null) => Some(Value::Null),
        Some(v) => {
            let t = v.get("__dt__").or(Some(v)).and_then(|x| x.as_str())?;
            match nd_render::pyvalue::PyDateTime::parse(t) {
                Some(d) => Some(json!(d.isoformat())),
                None => None,
            }
        }
    }
}

/// `media_rename_run` (app/routers/media_rename.py): one rename run of the active world, its results included - a viewer
/// who may edit none of the audio / video / images sections is 403, a run of another world (or missing) is 404
fn media_rename_run(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let level = |s: &str| crate::pages::edit_level(&p, w, s);
    if !(level("audio")? || level("video")? || level("images")?) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let Ok(r) = db.query_row("SELECT * FROM media_rename_runs WHERE id = ?1 AND world_id = ?2", [id, wid], |row| crate::layout::row_map(row)) else {
        return Some(crate::idpages::refusal(404));
    };
    // _run_names: {user id: display_name or email local part}; no author in the row: "someone"
    let by = match r.get("created_by_user_id").and_then(|v| v.as_i64()) {
        Some(uid) => db.query_row("SELECT * FROM users WHERE id = ?1", [uid], |row| crate::layout::row_map(row)).ok().and_then(|u| {
            u.get("display_name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string)
                .or_else(|| u.get("email")?.as_str()?.split('@').next().map(str::to_string))
        }),
        None => None,
    };
    // _run_view: both columns are parsed in one try; either failing resets both to the unreadable fallback
    let parsed = (|| Some((stored_collection(&r, "results_json")?, stored_collection(&r, "errors_json")?)))();
    let (results, errors, proposals, errors_count) = match parsed {
        Some(((rv, rn), (ev, en))) => (rv, ev, rn, en),
        None => (json!([]), json!(["Stored results were unreadable."]), 0, 1),
    };
    let out = json!({
        "id": r.get("id")?.as_i64()?, "status": r.get("status").cloned().unwrap_or(Value::Null),
        "done": r.get("done").and_then(|v| v.as_i64()).unwrap_or(0),
        "total": r.get("total").and_then(|v| v.as_i64()).unwrap_or(0),
        "error": r.get("error").and_then(|v| v.as_str()).unwrap_or(""),
        "resumed": r.get("resumed_count").and_then(|v| v.as_i64()).unwrap_or(0),
        "proposals": proposals, "errors_count": errors_count,
        "created_at": dt_strftime(&r, "created_at", "%Y-%m-%dT%H:%M:%SZ")?,
        "by": by.as_deref().unwrap_or("someone"),
        "results": results, "errors": errors,
    });
    crate::edits::json_response(&out)
}

/// `api_audio_job_status` (app/routers/sessions.py): a session-recap or condense job of the active world, as sessions.py's
/// own `_job_to_dict` writes it - a smaller dict than audio_jobs.py's (no purpose_label, no resumable flag, ...), and a job
/// of another purpose is the same 404 a missing one is
fn session_audio_job_status(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let wid = w.get("id")?.as_i64()?;
    let Ok(j) = db.query_row("SELECT * FROM audio_jobs WHERE id = ?1 AND world_id = ?2", [id, wid], |r| crate::layout::row_map(r)) else {
        return Some(crate::idpages::refusal(404));
    };
    let purpose = j.get("purpose").and_then(|v| v.as_str()).unwrap_or_default();
    if purpose != "session_recap" && purpose != "condense" {
        return Some(crate::idpages::refusal(404));
    }
    let s = |k: &str| j.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let v = |k: &str| j.get(k).cloned().unwrap_or(Value::Null);
    let out = json!({
        "id": j.get("id")?.as_i64()?, "purpose": purpose, "filename": v("filename"),
        "status": v("status"), "error": v("error"),
        "transcript": v("transcript"), "recap": v("recap"), "model": s("model").unwrap_or_default(),
        "extra_instructions": s("extra_instructions").unwrap_or_default(),
        "game_session_id": v("game_session_id"),
        "chunk_current": v("chunk_current"), "chunk_total": v("chunk_total"),
        "run_started_at": dt_iso(&j, "run_started_at")?, "finished_at": dt_iso(&j, "finished_at")?,
        "created_at": dt_iso(&j, "created_at")?, "updated_at": dt_iso(&j, "updated_at")?,
    });
    crate::edits::json_response(&out)
}

/// `api_imagegen_player_job_status` (app/routers/ai.py): a player's own image job (any of them for a GM). The gate comes
/// first - a GM always may, anyone else needs the world's image-gen toggle (a missing world is 403 there, as Python's
/// `_require_player_image_gen_access` raises before the handler's own 404) - and "wrong world" and "not mine" collapse
/// into the same 404 for a player
fn player_image_job_status(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let w = p.world.as_ref();
    if !p.is_gm && !w.map(|w| crate::edits::py_truthy_pub(w.get("players_can_use_image_gen"))).unwrap_or(false) {
        return Some(crate::idpages::refusal(403));
    }
    let w = w?;
    let wid = w.get("id")?.as_i64()?;
    let Ok(j) = db.query_row("SELECT * FROM image_jobs WHERE id = ?1", [id], |r| crate::layout::row_map(r)) else {
        return Some(crate::idpages::refusal(404));
    };
    if j.get("world_id").and_then(|v| v.as_i64()) != Some(wid) {
        return Some(crate::idpages::refusal(404));
    }
    if !p.is_gm && j.get("created_by_user_id").and_then(|v| v.as_i64()) != Some(p.user_id) {
        return Some(crate::idpages::refusal(404));
    }
    // _image_job_to_dict: result_urls_json is json.loads'd with no fallback - unreadable text is Python's own 500
    let urls = match j.get("result_urls_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()) {
        Some(t) => serde_json::from_str::<Value>(t).ok()?,
        None => json!([]),
    };
    let v = |k: &str| j.get(k).cloned().unwrap_or(Value::Null);
    let out = json!({
        "id": j.get("id")?.as_i64()?, "prompt": v("prompt"), "status": v("status"), "error": v("error"),
        "urls": urls, "created_at": dt_iso(&j, "created_at")?, "updated_at": dt_iso(&j, "updated_at")?,
    });
    crate::edits::json_response(&out)
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

/// `_entity_view_gate`: the entity and its world when the viewer may see it, else Python's 404 (Err); None when Python reads it its own way
fn entity_gate(db: &rusqlite::Connection, p: &crate::layout::PageCtx, id: i64) -> Option<Result<(Map<String, Value>, Map<String, Value>), Response<Body>>> {
    let Some(e) = rows_of(db, "SELECT * FROM entities WHERE id = ?1", &[&id])?.into_iter().next() else { return Some(Err(crate::idpages::refusal(404))) };
    let wid = e.get("world_id").and_then(|v| v.as_i64())?;
    let Some(w) = rows_of(db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else { return Some(Err(crate::idpages::refusal(404))) };
    if !p.is_gm {
        let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_ok();
        let s = |k: &str| w.get(k).and_then(|v| v.as_str());
        if !member || nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), &format!("kind_{kind}"))? == "none" {
            return Some(Err(crate::idpages::refusal(404)));
        }
        if e.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) == 0
            && db.query_row("SELECT 1 FROM entity_player_access WHERE entity_id = ?1 AND user_id = ?2", [id, p.user_id], |_| Ok(())).is_err() {
            return Some(Err(crate::idpages::refusal(404)));
        }
    }
    Some(Ok((e, w)))
}

/// `entity_download` (app/main.py, `_entity_to_markdown`): one entity as a standalone .md - title, kind line, summary, body and the
/// notes the viewer may see, [gmonly] text stripped for a non-GM; a player only when the world allows entity downloads
fn entity_download(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let (e, w) = match entity_gate(&db, &p, id)? {
        Ok(x) => x,
        Err(r) => return Some(r),
    };
    if !p.is_gm && !crate::edits::py_truthy_pub(w.get("players_can_download_entities")) {
        return Some(crate::idpages::refusal(403));
    }
    let strip = |v: Option<&Value>| -> Option<Option<String>> {
        match v {
            None | Some(Value::Null) => Some(None),
            Some(Value::String(s)) if p.is_gm || s.is_empty() => Some(Some(s.clone())),
            Some(Value::String(s)) => Some(Some(nd_perm::gmonly::strip_gm_only(s, Some(p.user_id), nd_render::now_minute())?)),
            _ => None,
        }
    };
    let name = e.get("name").and_then(|v| v.as_str())?.to_string();
    let kind = e.get("kind").and_then(|v| v.as_str())?.to_string();
    if !name.is_ascii() || !kind.is_ascii() {
        return None; // str.isalnum() / str.capitalize() beyond ASCII: Python's
    }
    let (_, icons, _) = nd_render::app::effective_kind_tables(Some(&w))?;
    let icon = icons.get(&kind).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let cap = kind.chars().next().map(|c| c.to_ascii_uppercase().to_string() + &kind[1..].to_ascii_lowercase()).unwrap_or_default();
    let mut label = nd_render::text::py_strip(&format!("{icon} {cap}")).to_string();
    match e.get("subtype") {
        Some(Value::String(s)) if !s.is_empty() => label.push_str(&format!(" \u{2014} {s}")),
        None | Some(Value::Null) | Some(Value::String(_)) => {}
        _ => return None,
    }
    let body = strip(e.get("body"))?.unwrap_or_default();
    let mut parts: Vec<String> = vec![format!("# {name}"), String::new(), format!("*{label}*"), String::new()];
    if let Some(s) = strip(e.get("summary"))?.filter(|s| !s.is_empty()) {
        parts.push(format!("*{s}*"));
        parts.push(String::new());
    }
    parts.extend(["---".to_string(), String::new(), body, String::new()]);
    let vis = if p.is_gm { "" } else { " AND visible_to_players IS 1" };
    let notes = rows_of(&db, &format!("SELECT * FROM entity_notes WHERE entity_id = ?1{vis} ORDER BY created_at"), &[&id])?;
    if !notes.is_empty() {
        parts.extend(["## Notes".to_string(), String::new()]);
        for n in &notes {
            let content = strip(n.get("content"))??; // a None note is Python's TypeError
            parts.extend([content, String::new(), "---".to_string(), String::new()]);
        }
    }
    let text = format!("{}\n", parts.join("\n").trim_end());
    let fname: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || " -_".contains(*c)).collect();
    let fname = if fname.is_empty() { "entity".to_string() } else { fname };
    Some(Response::builder().header(axum::http::header::CONTENT_TYPE, "text/markdown; charset=utf-8")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{fname}.md\""))
        .header("x-nd-served-by", "gateway").body(Body::from(text)).unwrap())
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

/// `_viewer_pcs` (app/routers/cockpit.py): the characters the player cockpit is FOR - the viewer's own, or a GM's `?pc=` view through
/// that character's owner (just the character when it has no owner)
fn viewer_pcs(db: &rusqlite::Connection, p: &crate::layout::PageCtx, wid: i64, as_pc: i64) -> Option<Vec<serde_json::Map<String, Value>>> {
    let mut owner = p.user_id;
    if as_pc != 0 && p.is_gm {
        let t = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [as_pc], |r| crate::layout::row_map(r)).ok();
        if let Some(t) = t {
            if t.get("world_id").and_then(|v| v.as_i64()) == Some(wid) {
                match t.get("owner_user_id").and_then(|v| v.as_i64()) {
                    None | Some(0) => return Some(vec![t]),
                    Some(o) => owner = o,
                }
            }
        }
    }
    let mut st = db.prepare("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY name").ok()?;
    let rows: Vec<serde_json::Map<String, Value>> = st.query_map([wid, owner], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    Some(rows)
}

/// `GET /api/cockpit/player-board` (app/routers/cockpit.py `cockpit_player_board`): the player cockpit's live data - the viewer's
/// own parties with member vitals, the world's player-visible active quests, the viewer's characters with vitals and XP
fn cockpit_player_board(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let wid = w.get("id")?.as_i64()?;
    // as_pc: int(pc) if pc.isdigit() else 0 - plain digits only; Unicode digits are Python's
    let pc_param = crate::pages::query_param(uri, "pc").unwrap_or_default();
    let as_pc = if !pc_param.is_empty() && pc_param.bytes().all(|b| b.is_ascii_digit()) && pc_param.len() < 16 { pc_param.parse::<i64>().unwrap_or(0) } else if pc_param.is_empty() { 0 } else { return None };
    let pcs = viewer_pcs(&db, &p, wid, as_pc)?;
    let name_or = |m: &serde_json::Map<String, Value>| m.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    // _player_parties: the parties one of the viewer's own characters belongs to, member vitals included
    let my_pc_ids: Vec<i64> = pcs.iter().filter_map(|pc| pc.get("id").and_then(|v| v.as_i64())).collect();
    let mut parties = vec![];
    if !my_pc_ids.is_empty() {
        let mut st = db.prepare("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name").ok()?;
        let rows: Vec<serde_json::Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        drop(st);
        for party in rows {
            let ids = crate::edits::member_ids(party.get("member_pc_ids_json"))?;
            if !my_pc_ids.iter().any(|i| ids.contains(i)) {
                continue;
            }
            let members: Vec<serde_json::Map<String, Value>> = if ids.is_empty() { vec![] } else {
                let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
                let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE id IN ({list})")).ok()?;
                let r: Vec<serde_json::Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
                r
            };
            parties.push(json!({"id": party.get("id"), "name": party.get("name"),
                "members": crate::edits::member_vitals(&db, &members, Some(4))?}));
        }
    }
    // _player_quests: active top-level quests the world shows players, party names attached
    let mut st = db.prepare("SELECT * FROM quests WHERE world_id = ?1 AND status = 'active' AND parent_id IS NULL AND visible_to_players = 1 ORDER BY category, title LIMIT 12").ok()?;
    let quests: Vec<serde_json::Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let mut party_names = serde_json::Map::new();
    let mut st = db.prepare("SELECT id, name FROM parties WHERE world_id = ?1").ok()?;
    for r in st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()? {
        let (id, name) = r.ok()?;
        party_names.insert(id.to_string(), json!(name));
    }
    drop(st);
    let truthy = |v: Option<&Value>| matches!(v, Some(Value::String(s)) if !s.is_empty());
    let quests: Vec<Value> = quests.iter().map(|q| {
        let cat = q.get("category").filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!("main"));
        json!({
            "id": q.get("id"), "title": q.get("title"), "category": cat,
            "summary": q.get("summary").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| Value::String(s.to_string())).unwrap_or_else(|| json!("")),
            "party_id": q.get("assigned_party_id"),
            "party": q.get("assigned_party_id").and_then(|v| v.as_i64()).and_then(|i| party_names.get(&i.to_string())).cloned().unwrap_or(Value::Null),
        })
    }).collect();
    // _player_my_pcs: the viewer's characters, vitals + XP; the vitals strip is built in SQL order and zipped onto the name-sorted rows
    let mut sorted: Vec<&serde_json::Map<String, Value>> = pcs.iter().collect();
    sorted.sort_by(|a, b| name_or(a).cmp(&name_or(b)));
    let vitals = crate::edits::member_vitals(&db, &pcs, None)?;
    let my_pcs: Vec<Value> = sorted.iter().zip(vitals.iter()).map(|(pc, v)| {
        let mut o = match v { Value::Object(o) => o.clone(), _ => return None };
        o.insert("xp".into(), pc.get("xp").cloned().unwrap_or(Value::Null));
        Some(Value::Object(o))
    }).collect::<Option<Vec<_>>>()?;
    crate::edits::json_response(&json!({"parties": parties, "quests": quests, "my_pcs": my_pcs}))
}

/// `json.loads(text or default)` where the two readers agree; the default on a genuine parse error (Python's
/// `except ValueError`), None when the text Python reads differently (NaN, a whole number past 64 bits) - decline
pub(crate) fn py_json_or(text: Option<&str>, default: &str) -> Option<Value> {
    let t = text.filter(|s| !s.is_empty()).unwrap_or(default);
    if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
        return None;
    }
    Some(serde_json::from_str(t).unwrap_or_else(|_| serde_json::from_str(default).unwrap_or(Value::Null)))
}

/// `_safe_export_filename`'s filter: an ASCII name (Python's Unicode-aware `isalnum` keeps more; a name with any of
/// that stays Python's, file name included)
fn export_name(name: Option<&str>) -> Option<String> {
    let raw = name.filter(|s| !s.is_empty()).unwrap_or("character");
    if !raw.chars().all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_') {
        return None;
    }
    let filtered: String = raw.chars().filter(|c| c.is_ascii_alphanumeric() || *c == ' ' || *c == '-' || *c == '_').collect();
    Some(if filtered.is_empty() { "character".to_string() } else { filtered })
}

/// `_can_view_character` (app/routers/characters.py): the GM, the owner, or a member of the character's own world while it
/// shows the party (the character's world, not the active one)
fn can_view_pc(db: &rusqlite::Connection, p: &crate::layout::PageCtx, pc: &Map<String, Value>) -> Option<bool> {
    if p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id) {
        return Some(true);
    }
    let wid = pc.get("world_id")?.as_i64()?;
    let see: Option<i64> = db.query_row("SELECT players_see_party FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok();
    let member: bool = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
    Some(member && see.unwrap_or(0) != 0 && pc.get("owner_user_id").and_then(|v| v.as_i64()).is_some())
}

/// `GET /api/characters/<id>/vitals` (app/routers/characters.py `character_vitals`): the live sheet numbers with the same
/// effective maxima, for anyone who may view the sheet
fn pc_vitals(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !can_view_pc(&db, &p, &pc)? {
        return Some(crate::idpages::refusal(403));
    }
    let (_, shock_max, pp_max, mp_max, _) = crate::edits::pc_maxima(&db, &pc)?;
    let d = crate::edits::derived(&pc)?;
    let conds = py_json_or(pc.get("conditions_json").and_then(|v| v.as_str()), "[]")?;
    let Some(list) = crate::edits::clean_conditions(&conds) else { return None };
    crate::edits::json_response(&json!({
        "level": pc.get("level"), "xp": pc.get("xp").and_then(|v| v.as_i64()).unwrap_or(0),
        "xp_hi": d.get("xp_hi"), "xp_pct": d.get("xp_pct"),
        "hp": pc.get("current_hp").and_then(|v| v.as_i64()).unwrap_or(0), "max_hp": d.get("hp_max"),
        "temp_hp": pc.get("temp_hp").and_then(|v| v.as_i64()).unwrap_or(0),
        "shock": pc.get("shock_current").and_then(|v| v.as_i64()).unwrap_or(0), "shock_max": shock_max,
        "pp": pc.get("pp_current").and_then(|v| v.as_i64()).unwrap_or(0), "pp_max": pp_max,
        "mp": pc.get("mp_current").and_then(|v| v.as_i64()).unwrap_or(0), "mp_max": mp_max,
        "conditions": list,
    }))
}

/// `GET /api/characters/<id>/sync` (app/routers/characters.py `character_sync_get`): the live-sync dict of a character the
/// caller may manage (the GM or the owner, of any world - Python checks no world here)
fn pc_sync_get(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    crate::edits::json_response(&crate::edits::pc_sync_dict(&pc)?)
}

/// `GET /api/characters/<id>/advance` (app/routers/characters.py `character_advance_options`): what this N&D character could
/// buy with its unspent XP; a custom system's 400 keeps Python's wording
fn pc_advance_get(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    if !crate::edits::pc_maxima(&db, &pc)?.4 {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST,
            "Advancement here follows the Neon & Dragons costs; this system spends XP on its own sheet"));
    }
    let c = crate::edits::Ctx { cfg, upload: None, created: vec![], removals: vec![], db, p, uri, headers, body: b"" };
    crate::edits::json_response(&crate::edits::adv_options_pub(&c, &pc)?)
}

/// `GET /api/characters/<id>/ability-cards` (app/routers/characters.py `character_ability_cards`): the feats as cards with
/// their suggested costs and the pools they are spent from
fn pc_ability_cards(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let (_, _, _, _, native) = crate::edits::pc_maxima(&db, &pc)?;
    let c = crate::edits::Ctx { cfg, upload: None, created: vec![], removals: vec![], db, p, uri, headers, body: b"" };
    let cards = crate::edits::ability_cards_pub(&c, &pc)?;
    crate::edits::json_response(&json!({"cards": cards,
        "pools": {"pp": pc.get("pp_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "mp": pc.get("mp_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "shock": pc.get("shock_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "hp": pc.get("current_hp").and_then(|v| v.as_i64()).unwrap_or(0)},
        "native": native}))
}

/// `GET /characters/<id>/export.json` (app/routers/characters.py `character_export_json`): the canonical import shape,
/// `json.dumps(payload, ensure_ascii=False, indent=2)` as a download; zero / empty fields omitted as Python omits them
fn pc_export_json(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let mut out = Map::new();
    // the live scalar columns in Python's order: a value Python reads as truthy goes in as its column type (level / xp as numbers)
    for (f, _num) in [("name", false), ("player_name", false), ("race", false), ("char_class", false), ("level", true), ("xp", true), ("backstory", false), ("notes", false)] {
        let v = pc.get(f)?;
        let keep = match v {
            Value::Null => false,
            Value::Number(n) => n.as_i64().map_or(false, |i| i != 0),
            Value::String(t) => !t.is_empty(),
            _ => return None,
        };
        if keep {
            out.insert(f.into(), v.clone());
        }
    }
    // setdefault("name", pc.name or "Character"): appended at the end when the name column is empty
    out.entry("name".to_string()).or_insert_with(|| json!(pc.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("Character")));
    for (f, v) in [("race_id", "race_id"), ("profession_id", "profession_id")] {
        let _ = v;
        if let Some(i) = pc.get(f).and_then(|x| x.as_i64()).filter(|i| *i != 0) {
            out.insert(f.into(), json!(i));
        }
    }
    for f in ["level", "xp", "max_hp", "current_hp"] {
        if let Some(i) = pc.get(f).and_then(|x| x.as_i64()).filter(|i| *i != 0) {
            out.entry(f.to_string()).or_insert(json!(i));
        }
    }
    for f in ["custom_fields_json", "app_extra_json"] {
        let v = py_json_or(pc.get(f).and_then(|x| x.as_str()), "{}")?;
        if v.as_object().map_or(false, |o| !o.is_empty()) {
            out.insert(f.into(), v);
        }
    }
    for f in ["stats_json", "skills_json", "currency_json", "equipment_json", "feats_json", "attacks_json", "cyberware_json", "conditions_json"] {
        let v = py_json_or(pc.get(f).and_then(|x| x.as_str()), "[]")?;
        if v.as_array().map_or(false, |a| !a.is_empty()) {
            out.insert(f.into(), v);
        }
    }
    if let Some(i) = pc.get("sheet_template_id").and_then(|x| x.as_i64()).filter(|i| *i != 0) {
        out.insert("sheet_template_id".into(), json!(i));
    }
    if let Some(Value::String(u)) = pc.get("portrait_url").filter(|v| !v.as_str().unwrap_or_default().is_empty()) {
        out.insert("portrait_url".into(), json!(u));
    }
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = nd_render::py_dumps_indent_as(&Value::Object(out), 2, false);
    let mut r = crate::edits::json_response_raw(&body, "application/json")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.json\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `_group_by_section`: template fields by their `section`, first-seen order
pub(crate) fn group_by_section(fields: &[Value]) -> Option<Vec<(String, Vec<&Map<String, Value>>)>> {
    let mut out: Vec<(String, Vec<&Map<String, Value>>)> = vec![];
    for f in fields {
        let f = f.as_object()?;
        let sec = match f.get("section") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(Value::String(_)) | Some(Value::Null) | None => "Custom".to_string(),
            _ => return None,
        };
        match out.iter_mut().find(|(t, _)| *t == sec) {
            Some((_, v)) => v.push(f),
            None => out.push((sec, vec![f])),
        }
    }
    Some(out)
}

/// `_pc_field_value_lines`: one (label, value) pair per template field, a "list" field expanding per item, a "resource"
/// reading its own current / max, anything Python's `str()` would spell differently left to Python
fn field_value_lines(field: &Map<String, Value>, cf: &Map<String, Value>) -> Option<Vec<(String, String)>> {
    let py_scalar = |v: &Value| -> Option<String> {
        match v {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) if n.as_i64().is_some() => Some(n.as_i64().unwrap().to_string()),
            _ => None,
        }
    };
    let fid = match field.get("id") {
        Some(Value::String(s)) => s.clone(),
        _ => return None,
    };
    let label = match field.get("label") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(Value::String(_)) | Some(Value::Null) | None => fid.clone(),
        _ => return None,
    };
    if field.get("type").and_then(|v| v.as_str()) == Some("list") {
        let items = match cf.get(&fid) {
            Some(Value::Array(a)) => a.clone(),
            Some(Value::Null) | None => return Some(vec![]),
            _ => return Some(vec![]), // Python: not a list (or empty) -> []
        };
        if items.is_empty() {
            return Some(vec![]);
        }
        let item_fields = match field.get("item_fields") {
            Some(Value::Array(a)) => a.clone(),
            Some(Value::Null) | None => vec![],
            _ => return None,
        };
        let mut out = vec![];
        for (i, item) in items.iter().enumerate() {
            let Some(item) = item.as_object() else { continue };
            let mut parts = vec![];
            for sf in &item_fields {
                let Some(sf) = sf.as_object() else { return None };
                let key = match sf.get("id") {
                    Some(Value::String(s)) => s.clone(),
                    _ => return None,
                };
                let val = match item.get(&key) {
                    Some(v @ Value::String(s)) if !s.is_empty() => py_scalar(v)?,
                    Some(v @ Value::Number(_)) => py_scalar(v)?,
                    _ => continue,
                };
                let lab = match sf.get("label") {
                    Some(Value::String(s)) if !s.is_empty() => s.clone(),
                    _ => key.clone(),
                };
                parts.push(format!("{lab}: {val}"));
            }
            out.push((format!("{label} #{}", i + 1), parts.join("; ")));
        }
        return Some(out);
    }
    if field.get("type").and_then(|v| v.as_str()) == Some("resource") {
        let text = crate::edits::field_value_text(field, cf)?;
        if text.is_empty() {
            return Some(vec![]);
        }
        let short = crate::edits::sheet_short_label_pub(&label)?;
        return Some(vec![(if short.is_empty() { label } else { short }, text)]);
    }
    let value = match cf.get(&fid) {
        Some(v) => v.clone(),
        None => field.get("default_value").cloned().unwrap_or(Value::String(String::new())),
    };
    if value.is_null() || value == json!("") {
        return Some(vec![]);
    }
    Some(vec![(label, py_scalar(&value)?)])
}

/// `_pc_export_sections`: the flat (name, [(label, value)]) summary the .md export renders
fn pc_export_sections(db: &rusqlite::Connection, pc: &Map<String, Value>) -> Option<Vec<(String, Vec<(String, String)>)>> {
    let chosen_tpl = match pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0) {
        Some(tid) => db.query_row("SELECT * FROM sheet_templates WHERE id = ?1", [tid], |r| crate::layout::row_map(r)).ok(),
        None => None,
    };
    let cf = py_json_or(pc.get("custom_fields_json").and_then(|v| v.as_str()), "{}")?;
    let Some(cf) = cf.as_object() else { return None };
    let is_custom = chosen_tpl.as_ref().and_then(|t| t.get("sheet_mode").and_then(|v| v.as_str())) == Some("custom");
    let s = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("");
    let n = |k: &str| pc.get(k).and_then(|v| v.as_i64());
    let mut sections: Vec<(String, Vec<(String, String)>)> = vec![];
    let mut basics = vec![];
    let tpl_name = chosen_tpl.as_ref().and_then(|t| t.get("name")).and_then(|v| v.as_str()).unwrap_or("");
    for (l, v) in [("System", tpl_name.to_string()), ("Player", s("player_name").to_string()), ("Race", s("race").to_string()), ("Class", s("char_class").to_string()),
                   ("Level", n("level").filter(|x| *x != 0 && !is_custom).map(|x| x.to_string()).unwrap_or_default()),
                   ("XP", n("xp").filter(|x| *x != 0 && !is_custom).map(|x| x.to_string()).unwrap_or_default())] {
        if !v.is_empty() {
            basics.push((l.to_string(), v));
        }
    }
    if !basics.is_empty() {
        sections.push(("Basics".into(), basics));
    }
    let conds = py_json_or(pc.get("conditions_json").and_then(|v| v.as_str()), "[]")?;
    if let Some(list) = crate::edits::clean_conditions(&conds) {
        if !list.is_empty() {
            sections.push(("Conditions".into(), vec![("Active".into(), list.join(", "))]));
        }
    } else {
        return None;
    }
    if !is_custom {
        let d = crate::edits::derived(pc)?;
        let mut resources = vec![("HP".to_string(), format!("{}/{}", n("current_hp").unwrap_or(0), d.get("hp_max")?.as_i64()?))];
        if d.get("shock_max")?.as_i64()? != 0 {
            resources.push(("Shock".into(), format!("{}/{}", d.get("shock_current")?.as_i64()?, d.get("shock_max")?.as_i64()?)));
        }
        if d.get("pp_current")?.as_i64()? != 0 || d.get("mp_current")?.as_i64()? != 0 {
            resources.push(("PP".into(), d.get("pp_current")?.as_i64()?.to_string()));
            resources.push(("MP".into(), d.get("mp_current")?.as_i64()?.to_string()));
        }
        if let Some(ac) = n("armor_class").filter(|x| *x != 0) {
            resources.push(("Armor Class".into(), ac.to_string()));
        }
        if let Some(sp) = n("speed").filter(|x| *x != 0) {
            resources.push(("Speed".into(), sp.to_string()));
        }
        sections.push(("Resources".into(), resources));
        let stat_line = |v: &Value| -> Option<(String, String)> {
            let st = v.as_object()?;
            let label = match st.get("label") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match st.get("id") {
                    Some(Value::String(t)) if !t.is_empty() => t.clone(),
                    Some(Value::String(_)) | Some(Value::Null) | None => String::new(),
                    _ => return None,
                },
            };
            let value = match st.get("value") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Number(n)) => n.as_i64()?.to_string(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            Some((label, value))
        };
        let stats: Option<Vec<(String, String)>> = d.get("stats")?.as_array()?.iter()
            .filter(|v| v.as_object().map_or(false, |o| o.contains_key("id"))).map(stat_line).collect();
        let stats = stats?;
        if !stats.is_empty() {
            sections.push(("Ability Scores".into(), stats));
        }
        let cur_line = |v: &Value| -> Option<(String, String)> {
            let c = v.as_object()?;
            let label = match c.get("label") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match c.get("abbr") {
                    Some(Value::String(t)) if !t.is_empty() => t.clone(),
                    _ => String::new(),
                },
            };
            let value = match c.get("value") {
                Some(Value::Number(n)) => n.as_i64()?.to_string(),
                Some(Value::Null) | None => "0".to_string(),
                _ => return None,
            };
            Some((label, value))
        };
        let currency: Option<Vec<(String, String)>> = d.get("currency")?.as_array()?.iter().map(cur_line).collect();
        let currency = currency?;
        if !currency.is_empty() {
            sections.push(("Currency".into(), currency));
        }
        let mut equipment = vec![];
        for it in d.get("equipment")?.as_array()? {
            let Some(it) = it.as_object() else { continue };
            let qty = match it.get("qty") {
                Some(Value::Number(x)) => x.as_i64().unwrap_or(1),
                Some(Value::Null) | None => 1,
                _ => return None,
            };
            let notes = match it.get("notes") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            let name = match it.get("name") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            equipment.push((name, if notes.is_empty() { format!("x{qty}") } else { format!("x{qty} — {notes}") }));
        }
        if !equipment.is_empty() {
            sections.push(("Equipment".into(), equipment));
        }
        let mut feats = vec![];
        for f in d.get("feats")?.as_array()? {
            let Some(f) = f.as_object() else { continue };
            let name = match f.get("name") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            let notes = match f.get("notes") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match f.get("description") {
                    Some(Value::String(t)) => t.clone(),
                    Some(Value::Null) | None => String::new(),
                    _ => return None,
                },
            };
            feats.push((name, notes));
        }
        if !feats.is_empty() {
            sections.push(("Feats".into(), feats));
        }
    }
    if let Some(tpl) = &chosen_tpl {
        let fields = py_json_or(tpl.get("fields_json").and_then(|v| v.as_str()), "[]")?;
        let fields = fields.as_array()?;
        for (name, fs) in group_by_section(fields)? {
            let mut pairs = vec![];
            for f in fs {
                pairs.extend(field_value_lines(f, cf)?);
            }
            if !pairs.is_empty() {
                sections.push((name, pairs));
            }
        }
    }
    let mut personality = vec![];
    for (l, v) in [("Personality Traits", s("personality_traits")), ("Ideals", s("ideals")), ("Bonds", s("bonds")), ("Flaws", s("flaws"))] {
        if !v.is_empty() {
            personality.push((l.to_string(), v.to_string()));
        }
    }
    if !personality.is_empty() {
        sections.push(("Personality".into(), personality));
    }
    Some(sections)
}

/// `GET /characters/<id>/export.md` (app/routers/characters.py `character_export_md`): the character as the same markdown file
fn pc_export_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let s = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("");
    let n = |k: &str| pc.get(k).and_then(|v| v.as_i64());
    let native = crate::edits::pc_maxima(&db, &pc)?.4;
    let mut lines = vec![format!("# {}", if s("name").is_empty() { "Character" } else { s("name") })];
    let mut subtitle = [s("race"), s("char_class")].iter().filter(|b| !b.is_empty()).map(|b| b.to_string()).collect::<Vec<_>>().join(" ");
    if n("level").unwrap_or(0) != 0 && native {
        subtitle = format!("{subtitle} — Level {}", n("level").unwrap_or(0)).trim_matches(|c| c == ' ' || c == '—').to_string();
    }
    if !subtitle.is_empty() {
        lines.push(format!("*{subtitle}*"));
    }
    lines.push(String::new());
    for (name, pairs) in pc_export_sections(&db, &pc)? {
        lines.push(format!("## {name}"));
        for (label, value) in pairs {
            lines.push(format!("- **{label}:** {value}"));
        }
        lines.push(String::new());
    }
    for (heading, key) in [("Backstory", "backstory"), ("Notes", "notes")] {
        let text = s(key);
        if text.is_empty() {
            continue;
        }
        lines.push(format!("## {heading}"));
        lines.push(nd_render::text::py_strip(text).to_string());
        lines.push(String::new());
    }
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = format!("{}\n", nd_render::text::py_strip(&lines.join("\n")));
    let mut r = crate::edits::json_response_raw(&body, "text/markdown; charset=utf-8")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.md\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `json.loads(text or default)` where the column parses: None on a parse error too (Python's handler has no `except`
/// there - its 500 is the answer) and on text the two readers disagree on
pub(crate) fn py_json_strict(text: Option<&str>, default: &str) -> Option<Value> {
    match text.filter(|s| !s.is_empty()) {
        Some(t) => {
            if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
                return None;
            }
            serde_json::from_str(t).ok()
        }
        None => serde_json::from_str(default).ok(),
    }
}

/// `_featured_entity_candidates` (app/routers/sessions.py): the "Entities Featured" picker's candidates - the world's
/// entities by the picker's groups (folder, name order), then the real PlayerCharacter sheets as "pc:<id>"
fn featured_entity_candidates(db: &rusqlite::Connection, wid: i64) -> Option<Vec<Value>> {
    const GROUPS: &[(&str, &str, bool)] = &[
        ("character", "\u{1f464} NPCs", false),
        ("character", "\u{1f9d1} Player Characters", true),
        ("creature", "\u{2620} Creatures", false),
        ("location", "\u{1f5fa} Locations", false),
        ("organization", "\u{1f3e2} Organizations", false),
        ("race", "\u{1f9ec} Races", false),
        ("profession", "\u{1f3ad} Professions", false),
        ("note", "\u{1f4c4} Notes", false),
    ];
    let mut candidates = vec![];
    for (kind, label, pcs_only) in GROUPS {
        let filter = if *pcs_only { " AND subtype = 'PC'" } else { " AND (subtype != 'PC' OR subtype IS NULL)" };
        let sql = format!("SELECT id, name, folder FROM entities WHERE world_id = ?1 AND kind = '{kind}'{filter} ORDER BY folder, name");
        let mut st = db.prepare(&sql).ok()?;
        let r = st.query_map([wid], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<String>>(2)?))
        }).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        drop(st);
        for (id, name, folder) in r {
            let name = name.unwrap_or_default();
            let full = match folder.filter(|f| !f.is_empty()) {
                Some(f) => format!("{label}/{f}"),
                None => label.to_string(),
            };
            candidates.push(json!({"id": id, "name": name, "folder": full}));
        }
    }
    let mut st = db.prepare("SELECT id, name FROM player_characters WHERE world_id = ?1 ORDER BY name").ok()?;
    let pcs = st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    drop(st);
    for (id, name) in pcs {
        candidates.push(json!({"id": format!("pc:{id}"), "name": name.unwrap_or_default(), "folder": "\u{1f9d1} Player Characters"}));
    }
    Some(candidates)
}

/// `GET /sessions/<id>/npc-candidates` (app/routers/sessions.py `session_npc_candidates`): the picker's candidates for a
/// session of the active world
fn session_npc_candidates(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let gs = db.query_row("SELECT world_id FROM game_sessions WHERE id = ?1", [id], |r| r.get::<_, i64>(0)).ok();
    if gs.map_or(true, |w| w != wid) {
        return Some(crate::idpages::refusal(404));
    }
    crate::edits::json_response(&json!({"candidates": featured_entity_candidates(&db, wid)?}))
}

/// The Neon & Dragons creation defaults (app/constants.py), as the form templates read them
pub(crate) fn nd_defaults() -> (Value, Value) {
    let stats = json!([
        {"id": "str", "label": "Strength", "abbr": "STR", "value": 3},
        {"id": "dex", "label": "Dexterity", "abbr": "DEX", "value": 3},
        {"id": "bod", "label": "Body", "abbr": "BOD", "value": 3},
        {"id": "per", "label": "Perception", "abbr": "PER", "value": 3},
        {"id": "wil", "label": "Willpower", "abbr": "WIL", "value": 2},
        {"id": "int", "label": "Intellect", "abbr": "INT", "value": 2},
        {"id": "cha", "label": "Charisma", "abbr": "CHA", "value": 2},
        {"id": "itu", "label": "Intuition", "abbr": "ITU", "value": 2},
    ]);
    let currency = json!([{"label": "Creds", "abbr": "CR", "value": 0}, {"label": "Tokens", "abbr": "TK", "value": 0}]);
    (stats, currency)
}

/// `_session_download_filename`: the title sanitized to Python's `isalnum` set (an ASCII title; anything beyond stays
/// Python's, file name included), then "-<suffix>.md"
fn session_md_name(title: Option<&str>, suffix: &str) -> Option<String> {
    let raw = title.filter(|t| !t.is_empty()).unwrap_or("session");
    if !raw.chars().all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_') {
        return None;
    }
    let base: String = raw.chars().filter(|c| c.is_ascii_alphanumeric() || *c == ' ' || *c == '-' || *c == '_').collect();
    Some(format!("{}-{suffix}.md", if base.is_empty() { "session".to_string() } else { base }))
}

/// the shared shape of `session_download_summary` / `session_download_transcript` (app/routers/sessions.py): the row by
/// id alone, read-gated by the row's own world's "sessions" section, the column's text with [gmonly] stripped for a
/// non-GM, as a text/markdown attachment; the no-content refusals in Python's words
fn session_md_download(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64, suffix: &str) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let gs = db.query_row("SELECT * FROM game_sessions WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(gs) = gs else { return Some(crate::idpages::refusal(404)) };
    let wid = gs.get("world_id")?.as_i64()?;
    let (ok, _) = crate::idpages::row_world_visible_pub(&db, &p, wid, "sessions")?;
    if !ok {
        return Some(crate::idpages::refusal(404));
    }
    let col = if suffix == "summary" { "summary" } else { "live_transcript" };
    let empty_msg = if suffix == "summary" { "This session has no summary yet" } else { "This session has no live transcript yet" };
    let text = match gs.get(col) {
        Some(Value::String(t)) if !t.is_empty() => t.clone(),
        _ => return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, empty_msg)),
    };
    let body = if p.is_gm {
        text
    } else {
        nd_perm::gmonly::strip_gm_only(&text, Some(p.user_id), nd_render::now_minute())?
    };
    let fname = session_md_name(gs.get("title").and_then(|v| v.as_str()), suffix)?;
    let mut r = crate::edits::json_response_raw(&body, "text/markdown; charset=utf-8")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `GET /sessions/<id>/summary.md`
fn session_summary_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    session_md_download(cfg, uri, headers, id, "summary")
}

/// `GET /sessions/<id>/transcript.md`
fn session_transcript_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    session_md_download(cfg, uri, headers, id, "transcript")
}

/// `GET /api/hover-preview/config` (app/main.py `hover_preview_config`): the instance-wide hover settings, fetched by
/// base.html on every page load (signed-in; the values match the layout's own hover context)
fn hover_preview_config(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let _p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let s = crate::layout::app_settings_row_pub(&db)?;
    let truthy = |k: &str| crate::edits::py_truthy_pub(s.get(k));
    let num = |k: &str, d: i64| s.get(k).and_then(|v| v.as_i64()).filter(|n| *n != 0).unwrap_or(d);
    crate::edits::json_response(&json!({
        "enabled": truthy("hover_preview_enabled"),
        "delay_ms": num("hover_preview_delay_ms", 5000),
        "hide_delay_ms": s.get("hover_preview_hide_delay_ms").and_then(|v| v.as_i64()).unwrap_or(400),
        "width_px": num("hover_preview_width_px", 340),
        "max_height_px": num("hover_preview_max_height_px", 420),
    }))
}
