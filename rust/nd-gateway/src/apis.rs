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
        path if saved_views_section(path).is_some() => saved_views_list(&cfg, uri, headers, saved_views_section(path)?),
        "/api/display/state" => display_state(&cfg, uri, headers),
        "/rules/download.md" => rules_download(&cfg, uri, headers),
        path if id_between(path, "/pages/", "/download").is_some() => pages_download(&cfg, uri, headers, id_between(path, "/pages/", "/download")?),
        "/api/ai/imagegen/jobs" => imagegen_jobs_list(&cfg, uri, headers),
        "/api/ai/imagegen/player/jobs" => player_image_jobs_list(&cfg, uri, headers),
        path if id_between(path, "/api/sessions/", "/live-transcript/check").is_some() => live_transcript_check(&cfg, uri, headers, id_between(path, "/api/sessions/", "/live-transcript/check")?),
        "/api/ai/chat/jobs" => chat_jobs_list(&cfg, uri, headers),
        "/api/quests/board" => quests_board(&cfg, uri, headers),
        "/api/backups" => backups_list(&cfg, uri, headers),
        "/api/ai/sessions" => ai_chat_sessions(&cfg, uri, headers),
        "/api/ai/imagegen/models/downloaded" => imagegen_downloaded(&cfg, uri, headers),
        "/api/ai/imagegen/models/downloaded" | "/api/perf/summary" | "/api/perf/recent" => perf_empty(&cfg, uri, headers),
        "/api/ai/imagegen/tags/sources" | "/api/ai/imagegen/tags/status" | "/api/ai/imagegen/tags" => imagegen_tags_empty(&cfg, uri, headers),
        "/api/system/stats" => system_stats(&cfg, uri, headers),
        "/api/orgs/graph" => orgs_graph(&cfg, uri, headers),
        "/api/ai/context-info" => ai_context_info(&cfg, uri, headers),
        "/api/ai/ping" => ai_ping(&cfg, uri, headers),
        "/api/ai/imagegen/status" | "/api/ai/imagegen/models" | "/api/ai/imagegen/loras" | "/api/ai/imagegen/upscalers" | "/api/ai/imagegen/refiners" | "/api/ai/imagegen/ipadapter-models"
        | "/api/ai/imagegen/progress" | "/api/ai/imagegen/updates" | "/api/ai/imagegen/backends" | "/api/ai/imagegen/samplers-schedulers" => imagegen_unbacked(&cfg, uri, headers),
        "/api/ai/imagegen/starred" => imagegen_starred(&cfg, uri, headers),
        "/api/ai/unsloth/models" | "/api/ai/unsloth/image/status" | "/api/ai/unsloth/image/load-progress" | "/api/ai/unsloth/auto-switch" | "/api/ai/unsloth/status"
        | "/api/ai/unsloth/context-overrides" | "/api/ai/unsloth/audio-models" | "/api/ai/unsloth/version" | "/api/ai/unsloth/probe"
        | "/api/ai/unsloth/hub/download-progress" | "/api/ai/unsloth/gguf-variants" => unsloth_unconfigured(&cfg, uri, headers),
        "/api/ai/recap-instructions" => ai_recap_instructions(&cfg, uri, headers),
        "/api/ai/defaults" => ai_defaults(&cfg, uri, headers),
        "/api/ai/prompt-presets" => ai_prompt_presets(&cfg, uri, headers),
        "/api/media-rename/history" => media_rename_history(&cfg, uri, headers),
        "/api/sessions/ai/audio-jobs" => session_audio_job_list(&cfg, uri, headers),
        "/api/kiy/plays" => kiy_plays(&cfg, uri, headers),
        "/api/media-rename/runs" => media_rename_runs(&cfg, uri, headers),
        "/api/media-rename/summary" => media_rename_summary(&cfg, uri, headers),
        "/api/media-rename/items" => media_rename_items(&cfg, uri, headers),
        "/api/gallery/browse" => gallery_browse(&cfg, uri, headers),
        "/admin/diagnostics/events" => diagnostics_events(&cfg, uri, headers),
        "/api/maps/props" => map_props_list(&cfg, uri, headers),
        "/api/facts/last-parse" => facts_last_parse(&cfg, uri, headers),
        "/tables/export" => tables_export(&cfg, uri, headers),
        "/api/characters/catalog" => characters_catalog(&cfg, uri, headers),
        "/api/characters/templates" => characters_templates(&cfg, uri, headers),
        "/api/combat/recent" => combat_recent(&cfg, uri, headers),
        "/api/audio/clips" => audio_clips(&cfg, uri, headers),
        "/api/world-players" => world_players(&cfg, uri, headers),
        "/api/tables/options" => tables_options(&cfg, uri, headers),
        "/api/video-jobs" => video_jobs_list(&cfg, uri, headers),
        path if trailing_id(path, "/api/ai/sessions/").is_some() => ai_chat_session_get(&cfg, uri, headers, trailing_id(path, "/api/ai/sessions/")?),
        path if trailing_id(path, "/api/ai/chat/jobs/").is_some() => chat_job_status(&cfg, uri, headers, trailing_id(path, "/api/ai/chat/jobs/")?),
        "/api/ai/imagegen/model-templates" => imagegen_model_templates(&cfg, uri, headers),
        path if id_between(path, "/api/ai/imagegen/jobs/", "").is_some() => imagegen_job_status_gm(&cfg, uri, headers, id_between(path, "/api/ai/imagegen/jobs/", "")?),
        path if tpl_export_path(path).is_some() => {
            let (wid, tid) = tpl_export_path(path)?;
            sheet_template_json(&cfg, uri, headers, wid, tid)
        }
        path if export_json_path(path).is_some() => {
            let (wid, what) = export_json_path(path)?;
            if what == "rules.json" { export_rules_json(&cfg, uri, headers, wid) } else { export_pcs_json(&cfg, uri, headers, wid) }
        }
        path if id_between(path, "/api/entity/", "/preview").is_some() => entity_preview(&cfg, uri, headers, id_between(path, "/api/entity/", "/preview")?),
        path if id_between(path, "/entity/", "/download.md").is_some() => entity_download(&cfg, uri, headers, id_between(path, "/entity/", "/download.md")?),
        path if id_between(path, "/api/characters/", "/vitals").is_some() => pc_vitals(&cfg, uri, headers, id_between(path, "/api/characters/", "/vitals")?),
        path if id_between(path, "/api/characters/", "/sync").is_some() => pc_sync_get(&cfg, uri, headers, id_between(path, "/api/characters/", "/sync")?),
        path if id_between(path, "/api/characters/", "/advance").is_some() => pc_advance_get(&cfg, uri, headers, id_between(path, "/api/characters/", "/advance")?),
        path if id_between(path, "/api/characters/", "/ability-cards").is_some() => pc_ability_cards(&cfg, uri, headers, id_between(path, "/api/characters/", "/ability-cards")?),
        path if id_between(path, "/characters/", "/export.json").is_some() => pc_export_json(&cfg, uri, headers, id_between(path, "/characters/", "/export.json")?),
        path if id_between(path, "/characters/", "/export.md").is_some() => pc_export_md(&cfg, uri, headers, id_between(path, "/characters/", "/export.md")?),
        path if id_between(path, "/characters/", "/export.ndc").is_some() => pc_export_ndc(&cfg, uri, headers, id_between(path, "/characters/", "/export.ndc")?),
        path if id_between(path, "/characters/", "/export.foundry.json").is_some() => pc_export_foundry(&cfg, uri, headers, id_between(path, "/characters/", "/export.foundry.json")?),
        path if path.starts_with("/worlds/switch/") => world_switch(&cfg, uri, headers),
        path if session_media_id(path).is_some() => session_media(&cfg, uri, headers, session_media_id(path)?),
        path if pack_world(path).is_some() => content_pack(&cfg, uri, headers, pack_world(path)?),
        "/api/audio-jobs" => audio_job_list(&cfg, uri, headers),
        "/api/entities/picker" => entities_picker(&cfg, uri, headers),
        "/api/cockpit/player-board" => cockpit_player_board(&cfg, uri, headers),
        "/api/hover-preview/config" => hover_preview_config(&cfg, uri, headers),
        path if kind_zip_path(path).is_some() => {
            let (kind, selected) = kind_zip_path(path)?;
            kind_download_zip(&cfg, uri, headers, &kind, selected)
        }
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
    matches!(path, "/api/hover-preview/config" | "/api/audio-jobs" | "/api/entities/picker" | "/api/cockpit/workspace" | "/api/cockpit/player-board" | "/api/calendar/search" | "/calendar/event-jump" | "/api/me" | "/api/view-prefs" | "/api/dice/history" | "/api/display/state" | "/rules/download.md" | "/api/ai/imagegen/jobs" | "/api/ai/imagegen/model-templates")
        || id_between(path, "/pages/", "/download").is_some() || tpl_export_path(path).is_some() || export_json_path(path).is_some()
        || session_media_id(path).is_some() || id_between(path, "/api/ai/assist-job/", "").is_some() || id_between(path, "/api/entity/", "/preview").is_some() || id_between(path, "/entity/", "/download.md").is_some()
        || id_between(path, "/api/ai/imagegen/jobs/", "").is_some()
        || (path.starts_with("/worlds/switch/") && path.len() > "/worlds/switch/".len() && !path["/worlds/switch/".len()..].contains('/')) || vitals_id(path).is_some() || audio_job_id(path).is_some() || pack_world(path).is_some()
        || media_run_id(path).is_some() || session_audio_job_id(path).is_some() || player_image_job_id(path).is_some()
        || kind_zip_path(path).is_some()
        || id_between(path, "/sessions/", "/npc-candidates").is_some() || id_between(path, "/sessions/", "/summary.md").is_some() || id_between(path, "/sessions/", "/transcript.md").is_some()
        || id_between(path, "/api/characters/", "/vitals").is_some() || id_between(path, "/api/characters/", "/sync").is_some() || id_between(path, "/api/characters/", "/advance").is_some() || id_between(path, "/api/characters/", "/ability-cards").is_some() || id_between(path, "/characters/", "/export.json").is_some() || id_between(path, "/characters/", "/export.md").is_some() || id_between(path, "/characters/", "/export.foundry.json").is_some() || id_between(path, "/characters/", "/export.ndc").is_some() || saved_views_section(path).is_some()
        || id_between(path, "/api/sessions/", "/live-transcript/check").is_some() || matches!(path, "/api/ai/imagegen/player/jobs" | "/api/ai/chat/jobs" | "/api/quests/board" | "/api/backups" | "/api/ai/imagegen/status" | "/api/ai/imagegen/models" | "/api/ai/imagegen/loras" | "/api/ai/imagegen/upscalers" | "/api/ai/imagegen/refiners" | "/api/ai/imagegen/ipadapter-models" | "/api/ai/imagegen/progress" | "/api/ai/imagegen/updates" | "/api/ai/imagegen/backends" | "/api/ai/imagegen/samplers-schedulers" | "/api/ai/imagegen/starred" | "/api/ai/imagegen/tags/sources" | "/api/ai/imagegen/tags/status" | "/api/ai/imagegen/tags" | "/api/perf/summary" | "/api/perf/recent" | "/api/system/stats" | "/api/orgs/graph" | "/api/ai/context-info" | "/api/ai/ping" | "/api/ai/sessions" | "/api/ai/unsloth/models" | "/api/ai/unsloth/image/status" | "/api/ai/unsloth/image/load-progress" | "/api/ai/unsloth/auto-switch" | "/api/ai/unsloth/status" | "/api/ai/unsloth/context-overrides" | "/api/ai/unsloth/audio-models" | "/api/ai/unsloth/version" | "/api/ai/unsloth/probe" | "/api/ai/unsloth/hub/download-progress" | "/api/ai/unsloth/gguf-variants" | "/api/ai/recap-instructions" | "/api/ai/defaults" | "/api/ai/prompt-presets" | "/api/media-rename/history" | "/api/sessions/ai/audio-jobs" | "/api/kiy/plays" | "/api/media-rename/runs" | "/api/media-rename/summary" | "/api/media-rename/items" | "/api/gallery/browse" | "/admin/diagnostics/events" | "/api/maps/props" | "/api/facts/last-parse" | "/tables/export" | "/api/characters/catalog" | "/api/characters/templates" | "/api/combat/recent" | "/api/audio/clips" | "/api/world-players" | "/api/tables/options" | "/api/video-jobs") || trailing_id(path, "/api/ai/chat/jobs/").is_some() || trailing_id(path, "/api/ai/sessions/").is_some()
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

/// `_run_view` with `_run_names`: one run as the status routes show it (`with_results`: the proposals and errors too)
fn media_run_view(db: &rusqlite::Connection, r: &Map<String, Value>, with_results: bool) -> Option<Value> {
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
    let mut out = json!({
        "id": r.get("id")?.as_i64()?, "status": r.get("status").cloned().unwrap_or(Value::Null),
        "done": r.get("done").and_then(|v| v.as_i64()).unwrap_or(0),
        "total": r.get("total").and_then(|v| v.as_i64()).unwrap_or(0),
        "error": r.get("error").and_then(|v| v.as_str()).unwrap_or(""),
        "resumed": r.get("resumed_count").and_then(|v| v.as_i64()).unwrap_or(0),
        "proposals": proposals, "errors_count": errors_count,
        "created_at": dt_strftime(&r, "created_at", "%Y-%m-%dT%H:%M:%SZ")?,
        "by": by.as_deref().unwrap_or("someone"),
    });
    if with_results {
        out["results"] = results;
        out["errors"] = errors;
    }
    Some(out)
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
    let out = media_run_view(&db, &r, true)?;
    crate::edits::json_response(&out)
}

/// sessions.py `_job_to_dict`: a session recap / condense job as the sessions page shows it
fn session_job_value(j: &Map<String, Value>) -> Option<Value> {
    let purpose = j.get("purpose").and_then(|v| v.as_str()).unwrap_or_default();
    let s = |k: &str| j.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let v = |k: &str| j.get(k).cloned().unwrap_or(Value::Null);
    Some(json!({
        "id": j.get("id")?.as_i64()?, "purpose": purpose, "filename": v("filename"),
        "status": v("status"), "error": v("error"),
        "transcript": v("transcript"), "recap": v("recap"), "model": s("model").unwrap_or_default(),
        "extra_instructions": s("extra_instructions").unwrap_or_default(),
        "game_session_id": v("game_session_id"),
        "chunk_current": v("chunk_current"), "chunk_total": v("chunk_total"),
        "run_started_at": dt_iso(&j, "run_started_at")?, "finished_at": dt_iso(&j, "finished_at")?,
        "created_at": dt_iso(&j, "created_at")?, "updated_at": dt_iso(&j, "updated_at")?,
    }))
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
    let out = session_job_value(&j)?;
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


/// `/worlds/{wid}/export/{rules|player-characters}.json`
fn export_json_path(path: &str) -> Option<(i64, &'static str)> {
    let rest = path.strip_prefix("/worlds/")?;
    let (wid, tail) = rest.split_once("/export/")?;
    let what = match tail {
        "rules.json" => "rules.json",
        "player-characters.json" => "player-characters.json",
        _ => return None,
    };
    let num = |s: &str| !s.is_empty() && s.len() < 16 && s.bytes().all(|b| b.is_ascii_digit()) && !(s.len() > 1 && s.starts_with('0'));
    if !num(wid) {
        return None;
    }
    Some((wid.parse().ok()?, what))
}

/// `/worlds/{wid}/export/templates/{tid}.json`
fn tpl_export_path(path: &str) -> Option<(i64, i64)> {
    let rest = path.strip_prefix("/worlds/")?;
    let (wid, rest) = rest.split_once("/export/templates/")?;
    let tid = rest.strip_suffix(".json")?;
    let num = |s: &str| !s.is_empty() && s.len() < 16 && s.bytes().all(|b| b.is_ascii_digit()) && !(s.len() > 1 && s.starts_with('0'));
    if !num(wid) || !num(tid) {
        return None;
    }
    Some((wid.parse().ok()?, tid.parse().ok()?))
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

/// the section of `/api/saved-views/<section>` (lower-case letters and underscores, as the access rule reads it)
fn saved_views_section(path: &str) -> Option<&str> {
    let s = path.strip_prefix("/api/saved-views/")?;
    (!s.is_empty() && s.len() <= 32 && s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')).then_some(s)
}

/// `saved_views_list`: the person's named views of one page, by name
fn saved_views_list(cfg: &Config, uri: &Uri, headers: &HeaderMap, section: &str) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if nd_render::app::data().get("view_sections")?.get(section).is_none() {
        return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, "Unknown section"));
    }
    let mut views = vec![];
    for r in rows_of(&db, "SELECT id, name, prefs_json FROM saved_views WHERE user_id = ?1 AND section = ?2 ORDER BY name", &[&p.user_id, &section])? {
        let state: Value = serde_json::from_str(r.get("prefs_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")).ok()?;
        views.push(json!({"id": r.get("id"), "name": r.get("name"), "state": nd_render::app::clean(section, &state)?}));
    }
    crate::edits::json_response(&json!({"views": views}))
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
pub(crate) fn entity_gate(db: &rusqlite::Connection, p: &crate::layout::PageCtx, id: i64) -> Option<Result<(Map<String, Value>, Map<String, Value>), Response<Body>>> {
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
    let text = entity_markdown(&db, &p, &e, &w, id)?;
    let name = e.get("name").and_then(|v| v.as_str()).unwrap_or("entity");
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
    matches!(path, "/api/ai/world-context-smart" | "/api/ai/world-context-player" | "/api/ai/unsloth/prefs" | "/api/system/gpu-power")
}

pub fn post(cfg: Arc<Config>, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    match uri.path() {
        "/api/ai/world-context-smart" => world_context_smart(&cfg, uri, headers, body),
        "/api/ai/world-context-player" => world_context_player(&cfg, uri, headers, body),
        "/api/ai/unsloth/prefs" => unsloth_prefs(&cfg, uri, headers, body),
        "/api/system/gpu-power" => gpu_power(&cfg, uri, headers, body),
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

/// `html.escape(s)` (quotes included)
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

/// `str(v)` for a JSON string / int / plain float; anything else (None, bool, list, exponent forms) is Python's own text
fn py_text(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) if n.is_i64() => Some(n.as_i64()?.to_string()),
        Value::Number(n) => {
            let f = n.as_f64()?;
            if !f.is_finite() || f.abs() >= 1e16 || (f != 0.0 && f.abs() < 1e-4) {
                return None;
            }
            Some(if f == f.trunc() { format!("{f:.1}") } else { format!("{f}") })
        }
        _ => None,
    }
}

/// `x.get(k, default)` as text for the export HTML: a missing key is the default
fn get_text(o: &Map<String, Value>, k: &str, default: &str) -> Option<String> {
    match o.get(k) {
        None => Some(default.to_string()),
        Some(v) => py_text(v),
    }
}

/// `a or b` over two keys, both text
fn first_truthy<'a>(o: &'a Map<String, Value>, keys: &[&str]) -> Option<Option<&'a Value>> {
    for k in keys {
        let v = o.get(*k);
        if crate::edits::py_truthy_pub(v) {
            return Some(v);
        }
    }
    Some(None)
}

/// `_pc_to_foundry_journal(pc)`: one character as a Foundry JournalEntry document
pub(crate) fn pc_foundry_doc(db: &rusqlite::Connection, pc: &Map<String, Value>, id: i64) -> Option<Value> {
    let col = |k: &str| -> Option<String> {
        match pc.get(k) { None | Some(Value::Null) => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None }
    };
    let name = col("name")?;
    let title = if name.is_empty() { "Character".to_string() } else { name.clone() };
    let (backstory, notes) = (col("backstory")?, col("notes")?);
    let tpl = crate::sheets::pc_template(db, pc)?;
    let doc = if let Some(t) = tpl.as_ref().filter(|t| t.custom()) {
        let mut body = vec![format!("<h1>{}</h1>", html_escape(&title))];
        for (section, pairs) in pc_export_sections(db, pc)? {
            body.push(format!("<h2>{}</h2><ul>{}</ul>", html_escape(&section),
                pairs.iter().map(|(l, v)| format!("<li><strong>{}:</strong> {}</li>", html_escape(l), html_escape(v))).collect::<String>()));
        }
        if !backstory.is_empty() {
            body.push(format!("<h2>Background</h2><p>{}</p>", html_escape(&backstory)));
        }
        if !notes.is_empty() {
            body.push(format!("<h2>Notes</h2><p>{}</p>", html_escape(&notes)));
        }
        let slug = match t.row.get("slug") { Some(Value::String(s)) => s.clone(), _ => return None };
        json!({"name": title, "folder": null,
            "pages": [{"name": "Character Sheet", "type": "text", "text": {"format": 1, "content": body.concat()}, "sort": 0}],
            "flags": {"nd-world": {"source": "nd-world", "character_id": id, "system": slug}}})
    } else {
        let d = crate::edits::derived(pc)?;
        let level = pc.get("level").and_then(|v| v.as_i64())?;
        let hp = pc.get("current_hp").and_then(|v| v.as_i64())?;
        let (hp_max, shock_max, shock_cur) = (d.get("hp_max")?.as_i64()?, d.get("shock_max")?.as_i64()?, d.get("shock_current")?.as_i64()?);
        let mut currency = vec![];
        for c in d.get("currency")?.as_array()? {
            let c = c.as_object()?;
            let label = match first_truthy(c, &["abbr", "label"])? {
                Some(Value::String(s)) => s.clone(),
                Some(_) => return None,
                None => match c.get("label") { None => String::new(), Some(Value::String(s)) => s.clone(), _ => return None },
            };
            currency.push(format!("{} {}", get_text(c, "value", "0")?, html_escape(&label)));
        }
        let mut stat_rows = String::new();
        for s in d.get("stats")?.as_array()? {
            let Some(s) = s.as_object() else { continue };
            let label = match first_truthy(s, &["label"])? {
                Some(Value::String(l)) => l.clone(),
                Some(_) => return None,
                None => match s.get("id") { None => String::new(), Some(Value::String(i)) => i.clone(), _ => return None },
            };
            stat_rows.push_str(&format!("<tr><td>{}</td><td>{}</td></tr>", html_escape(&label), html_escape(&get_text(s, "value", "")?)));
        }
        let mut sheet = format!("<h1>{}</h1><p><em>{} {}{}</em></p><p>HP: {hp}/{hp_max}{}</p><p>Currency: {}</p>",
            html_escape(&title), html_escape(&col("race")?), html_escape(&col("char_class")?),
            if level != 0 { format!(" — Level {level}") } else { String::new() },
            if shock_max != 0 { format!("  |  Shock: {shock_cur}/{shock_max}") } else { String::new() },
            if currency.is_empty() { "—".to_string() } else { currency.join(", ") });
        if !stat_rows.is_empty() {
            sheet.push_str(&format!("<table><tbody>{stat_rows}</tbody></table>"));
        }
        if !backstory.is_empty() {
            sheet.push_str(&format!("<h2>Background</h2><p>{}</p>", html_escape(&backstory)));
        }
        if !notes.is_empty() {
            sheet.push_str(&format!("<h2>Notes</h2><p>{}</p>", html_escape(&notes)));
        }
        let equipment = d.get("equipment")?.as_array()?;
        let equipment_html = if !equipment.is_empty() {
            let mut rows = String::new();
            for it in equipment {
                let Some(it) = it.as_object() else { continue };
                rows.push_str(&format!("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                    html_escape(&get_text_str(it, "name")?), get_text(it, "qty", "1")?, get_text(it, "weight", "0")?,
                    if crate::edits::py_truthy_pub(it.get("equipped")) { "yes" } else { "" }, html_escape(&get_text_str(it, "notes")?)));
            }
            format!("<table><thead><tr><th>Item</th><th>Qty</th><th>Wt</th><th>Eq</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table>")
        } else {
            "<p><em>No equipment.</em></p>".to_string()
        };
        let feats = d.get("feats")?.as_array()?;
        let feats_html = if !feats.is_empty() {
            let mut items = String::new();
            for f in feats {
                let Some(f) = f.as_object() else { continue };
                let kind = match first_truthy(f, &["type", "source"])? { Some(Value::String(s)) => Some(s.clone()), Some(_) => return None, None => None };
                let desc = match first_truthy(f, &["notes", "description"])? { Some(Value::String(s)) => Some(s.clone()), Some(_) => return None, None => None };
                items.push_str(&format!("<li><strong>{}</strong>{}{}</li>", html_escape(&get_text_str(f, "name")?),
                    kind.map(|k| format!(" ({})", html_escape(&k))).unwrap_or_default(), desc.map(|k| format!(" — {}", html_escape(&k))).unwrap_or_default()));
            }
            format!("<ul>{items}</ul>")
        } else {
            "<p><em>No feats.</em></p>".to_string()
        };
        json!({"name": title, "folder": null,
            "pages": [{"name": "Character Sheet", "type": "text", "text": {"format": 1, "content": sheet}, "sort": 0},
                      {"name": "Equipment", "type": "text", "text": {"format": 1, "content": equipment_html}, "sort": 100},
                      {"name": "Feats", "type": "text", "text": {"format": 1, "content": feats_html}, "sort": 200}],
            "flags": {"nd-world": {"source": "nd-world", "character_id": id}}})
    };
    Some(doc)
}

/// `GET /characters/<id>/export.foundry.json` (app/routers/characters.py `character_export_foundry`): a Foundry JournalEntry
fn pc_export_foundry(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let doc = pc_foundry_doc(&db, &pc, id)?;
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = nd_render::py_dumps_indent_as(&json!([doc]), 2, false);
    let mut r = crate::edits::json_response_raw(&body, "application/json")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.foundry.json\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `x.get(k, '')` that must be a string (html.escape of anything else raises in Python)
fn get_text_str(o: &Map<String, Value>, k: &str) -> Option<String> {
    match o.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None }
}

/// `int(v)` for a stored stat / currency value: a JSON int, a float (truncated) or plain numeric text; anything else is Python's
fn py_int_value(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) if n.is_i64() => n.as_i64(),
        Value::Number(n) => {
            let f = n.as_f64()?;
            (f.is_finite() && f.abs() < 9e15).then(|| f.trunc() as i64)
        }
        Value::String(s) => nd_render::text::py_strip(s).parse::<i64>().ok().filter(|_| s.is_ascii()),
        _ => None,
    }
}

/// `GET /characters/<id>/export.ndc` (app/routers/characters.py `character_export_ndc`): the character in the Android app's / editor's format
fn pc_export_ndc(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    if !crate::edits::pc_maxima(&db, &pc)?.4 {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "The .ndc format is the Neon & Dragons app's — a character on a custom sheet (Hunt in the Moonlight, Asterion, …) can't be exported to it. Use the JSON, Markdown, PDF or Foundry export instead."));
    }
    let list = |k: &str| -> Option<Vec<Value>> {
        match py_json_strict(pc.get(k).and_then(|v| v.as_str()), "[]")? { Value::Array(a) => Some(a), _ => None }
    };
    let (stats, feats, equipment, currency) = (list("stats_json")?, list("feats_json")?, list("equipment_json")?, list("currency_json")?);
    let d = crate::edits::derived(&pc)?;
    let dint = |k: &str| d.get(k).and_then(|v| v.as_i64());
    let mut stat_val: Vec<(String, i64)> = vec![];
    for s in &stats {
        let s = s.as_object()?;
        let id = match s.get("id")? { Value::String(x) => x.clone(), _ => return None };
        let v = match s.get("value") { None => 0, Some(v) => py_int_value(v)? };
        stat_val.retain(|(k, _)| *k != id);
        stat_val.push((id, v));
    }
    let sv = |k: &str| stat_val.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let mut selected = vec![];
    let mut custom_feats = Map::new();
    for f in &feats {
        let f = f.as_object()?;
        if crate::edits::py_truthy_pub(f.get("id")) {
            selected.push(f.get("id")?.clone());
        } else {
            let name = match f.get("name") { v if !crate::edits::py_truthy_pub(v) => String::new(), Some(Value::String(s)) => s.clone(), _ => return None };
            if !name.is_empty() {
                let note = match first_truthy(f, &["notes", "description"])? { Some(v) => v.clone(), None => json!("") };
                custom_feats.insert(name, note);
            }
        }
    }
    let data = nd_render::app::data();
    let mut category_of: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for it in data.get("catalog_equipment")?.as_array()? {
        category_of.insert(it.get("id")?.as_str()?.to_string(), it.get("category")?.as_str()?.to_string());
    }
    let bucket_names = ["weapons", "armor", "augments", "bio_augments", "drones", "vehicles", "bases", "husks", "inventory"];
    let mut buckets: Vec<(&str, Vec<Value>)> = bucket_names.iter().map(|b| (*b, vec![])).collect();
    let mut custom_equipment = Map::new();
    for e in &equipment {
        let e = e.as_object()?;
        let eid = e.get("id");
        if crate::edits::py_truthy_pub(eid) {
            let Some(Value::String(eid_s)) = eid else { return None };
            let cat = match first_truthy(e, &["category"])? {
                Some(Value::String(c)) => c.clone(),
                Some(_) => return None,
                None => category_of.get(eid_s).cloned().unwrap_or_default(),
            };
            let target = if bucket_names.contains(&cat.as_str()) { cat.as_str() } else { "inventory" };
            buckets.iter_mut().find(|b| b.0 == target)?.1.push(eid?.clone());
        } else {
            let name = match e.get("name") { v if !crate::edits::py_truthy_pub(v) => String::new(), Some(Value::String(s)) => s.clone(), _ => return None };
            if !name.is_empty() {
                let note = match first_truthy(e, &["notes"])? { Some(Value::String(s)) => json!(s), Some(_) => return None, None => json!("") };
                custom_equipment.insert(name, note);
            }
        }
    }
    let bucket = |n: &str| Value::Array(buckets.iter().find(|b| b.0 == n).map(|b| b.1.clone()).unwrap_or_default());
    let mut credits = 0;
    for c in &currency {
        let c = c.as_object()?;
        let abbr = match c.get("abbr") { v if !crate::edits::py_truthy_pub(v) => String::new(), Some(Value::String(s)) if s.is_ascii() => s.to_ascii_uppercase(), _ => return None };
        if abbr == "CR" {
            credits = match c.get("value") { None => 0, Some(v) => py_int_value(v)? };
            break;
        }
    }
    let mut portrait = String::new();
    if let Some(Value::String(url)) = pc.get("portrait_url") {
        if url.starts_with("/uploads/") {
            if !url.is_ascii() || url.contains('\\') {
                return None;
            }
            let leaf = url.trim_end_matches('/').rsplit('/').next().unwrap_or("");
            let path = cfg.db_path.parent()?.join("uploads").join(leaf);
            if path.exists() {
                use base64::Engine;
                portrait = base64::engine::general_purpose::STANDARD.encode(std::fs::read(&path).ok()?);
            }
        }
    }
    let mut notes = Map::new();
    let text_col = |k: &str| -> Option<String> { match pc.get(k) { None | Some(Value::Null) => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None } };
    if !text_col("backstory")?.is_empty() {
        notes.insert("Backstory".into(), json!(text_col("backstory")?));
    }
    if !text_col("notes")?.is_empty() {
        notes.insert("Session Notes".into(), json!(text_col("notes")?));
    }
    let race_id = text_col("race_id")?;
    let (phys, ment) = (dint("phys")?, dint("ment")?);
    let ecto = if race_id == "banshee" { phys + ment } else { 0 };
    let n = |k: &str| crate::edits::col_int(&pc, k);
    let empty_obj = || json!({});
    let empty_arr = || json!([]);
    let mut doc = Map::new();
    macro_rules! put {
        ($($k:literal : $v:expr),* $(,)?) => { $( doc.insert($k.to_string(), json!($v)); )* };
    }
    put!{
        "id": 0, "name": text_col("name")?, "raceId": race_id, "raceName": text_col("race")?, "baseRaceId": "", "baseRaceName": "",
        "professionId": text_col("profession_id")?, "professionName": text_col("char_class")?,
        "strength": sv("str"), "dexterity": sv("dex"), "body": sv("bod"), "perception": sv("per"),
        "willpower": sv("wil"), "intellect": sv("int"), "charisma": sv("cha"), "intuition": sv("itu"),
        "strengthBonus": 0, "dexterityBonus": 0, "bodyBonus": 0, "perceptionBonus": 0,
        "willpowerBonus": 0, "intellectBonus": 0, "charismaBonus": 0, "intuitionBonus": 0,
        "maxHealth": dint("hp_max")?, "currentHealth": n("current_hp"), "temporaryHP": n("temp_hp"), "healthBonus": 0,
        "maxShock": dint("shock_max")?, "currentShock": dint("shock_current")?, "temporaryShock": 0, "shockBonus": 0,
        "cyberAdaptivity": dint("ca_derived")?, "speed": dint("speed_derived")?,
        "physicalPoints": phys, "currentPP": dint("pp_current")?, "ppBonus": 0,
        "mentalPoints": ment, "currentMP": dint("mp_current")?, "mpBonus": 0,
        "speedBonus": 0, "caBonus": 0, "maxEctoplasm": ecto, "currentEctoplasm": ecto,
        "selectedFeats": selected.clone(), "creationFeats": selected, "customFeats": custom_feats,
        "psyPowerSelections": empty_obj(), "jackOfTradeSelections": empty_obj(), "linguistLanguages": empty_arr(), "masterLinguistLanguages": empty_arr(),
        "infectedVirus": "", "jackOfAllTradesSelections": empty_arr(), "statPickerSelections": empty_obj(),
        "equippedWeapons": bucket("weapons"), "customWeapons": empty_obj(), "weaponFeats": empty_obj(), "activeWeapons": bucket("weapons"),
        "equippedArmor": bucket("armor"), "armorFeats": empty_obj(), "activeArmor": bucket("armor"),
        "installedAugments": bucket("augments"), "customAugments": empty_obj(), "augmentFeats": empty_obj(), "activeAugments": bucket("augments"),
        "installedBioAugments": bucket("bio_augments"), "customBioAugments": empty_obj(), "bioAugmentFeats": empty_obj(), "activeBioAugments": bucket("bio_augments"),
        "ownedDrones": bucket("drones"), "droneFeats": empty_obj(), "ownedVehicles": bucket("vehicles"), "vehicleFeats": empty_obj(),
        "inventory": bucket("inventory"), "customEquipment": custom_equipment, "ownedBases": bucket("bases"), "customBases": empty_obj(),
        "ownedHusks": bucket("husks"), "equippedHuskId": "", "huskCurrentHealth": 0, "huskCurrentPP": 0,
        "craftedItems": empty_arr(), "credits": credits,
        "yellowSat": 0, "transcendPts": 0, "transcendencePts": 0, "heartPres": 0, "dragonBlood": "", "dragonbloodedStatGroup": "",
        "fleshGraftPts": 0, "flashGraftsPts": 0, "crimsonBoost1": "", "crimsonBoost2": "", "crimsonPenalty": "", "mentalCond": "", "ahCharges": 0,
        "currentXP": n("xp"), "xpSpent": 0, "majorEdges": n("major_edge_count"), "minorEdges": n("minor_edge_count"),
        "portraitBase64": portrait, "notes": notes,
        "featSpecialAttrValues": empty_obj(), "professionSpecialAttrValues": empty_obj(), "raceSpecialAttrValues": empty_obj(),
    }
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = nd_render::py_dumps_indent_as(&json!([Value::Object(doc)]), 2, false);
    let mut r = crate::edits::json_response_raw(&body, "application/json")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.ndc\"").parse() {
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
pub(crate) fn pc_export_sections(db: &rusqlite::Connection, pc: &Map<String, Value>) -> Option<Vec<(String, Vec<(String, String)>)>> {
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

/// `_entity_to_markdown(db, e, request)` (the entity .md download's text, shared by the kind zips); None for anything
/// Python would render its own way
pub(crate) fn entity_markdown(db: &rusqlite::Connection, p: &crate::layout::PageCtx, e: &Map<String, Value>, w: &Map<String, Value>, id: i64) -> Option<String> {
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
    let (_, icons, _) = nd_render::app::effective_kind_tables(Some(w))?;
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
    let notes = rows_of(db, &format!("SELECT * FROM entity_notes WHERE entity_id = ?1{vis} ORDER BY created_at"), &[&id])?;
    if !notes.is_empty() {
        parts.extend(["## Notes".to_string(), String::new()]);
        for n in &notes {
            let content = strip(n.get("content"))??; // a None note is Python's TypeError
            parts.extend([content, String::new(), "---".to_string(), String::new()]);
        }
    }
    Some(format!("{}\n", parts.join("\n").trim_end()))
}

/// (kind, selected) of `/kind/<kind>/download[-selected].zip`
pub(crate) fn kind_zip_path(path: &str) -> Option<(String, bool)> {
    let rest = path.strip_prefix("/kind/")?;
    let (kind, tail) = rest.split_once('/')?;
    let selected = match tail {
        "download.zip" => false,
        "download-selected.zip" => true,
        _ => return None,
    };
    (!kind.is_empty() && !kind.contains('%')).then(|| (kind.to_string(), selected))
}

/// `kind_download` / `kind_download_selected` (app/main.py): every (or the picked) entity of the kind as one .md file
/// per entity, zipped - the same text the single-entity download writes, the same visibility, the download toggle
pub(crate) fn kind_download_zip(cfg: &Config, uri: &Uri, headers: &HeaderMap, kind: &str, selected: bool) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let w = p.world.clone()?;
    let wid = w.get("id")?.as_i64()?;
    if !p.is_gm && !crate::edits::py_truthy_pub(w.get("players_can_download_entities")) {
        return Some(crate::idpages::refusal(403));
    }
    if !crate::pages::section_ok(&p, Some(&w), &format!("kind_{kind}"))? {
        return Some(crate::idpages::refusal(403));
    }
    // the picked ids: FastAPI's list[int] - every value must be an int (a "-5" is one; anything else is its 422)
    let mut ids: Vec<i64> = vec![];
    if selected {
        for pair in uri.query().unwrap_or("").split('&').filter(|p| !p.is_empty()) {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let dec = |s: &str| percent_encoding::percent_decode_str(&s.replace('+', " ")).decode_utf8_lossy().to_string();
            if dec(k) == "id" {
                let raw = dec(v);
                // FastAPI's int: a plain (optionally signed) int; anything else is its 422
                let t = raw.strip_prefix('-').unwrap_or(&raw);
                if t.is_empty() || t.len() > 15 || !t.bytes().all(|b| b.is_ascii_digit()) || raw.parse::<i64>().is_err() {
                    return None;
                }
                ids.push(raw.parse::<i64>().ok()?);
            }
        }
    }
    let vis = if p.is_gm { String::new() } else {
        " AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))".to_string()
    };
    let sql = if selected {
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        if ids.is_empty() {
            format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' AND id IN () {vis} ORDER BY name")
        } else {
            format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' AND id IN ({list}) {vis} ORDER BY name")
        }
    } else {
        format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = '{kind}' {vis} ORDER BY name")
    };
    let rows: Vec<Map<String, Value>> = if p.is_gm {
        rows_of(&db, &sql, &[&wid])?
    } else {
        rows_of(&db, &sql, &[&wid, &p.user_id])?
    };
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut zf = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        for e in &rows {
            let id = e.get("id")?.as_i64()?;
            let name = e.get("name").and_then(|v| v.as_str()).unwrap_or("entity");
            if !name.is_ascii() {
                return None; // the file name's isalnum() beyond ASCII: Python's
            }
            let fname: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || " -_".contains(*c)).collect();
            let fname = if fname.is_empty() { "entity".to_string() } else { fname };
            let text = entity_markdown(&db, &p, e, &w, id)?;
            zf.start_file(format!("{fname}-{id}.md"), opts).ok()?;
            std::io::Write::write_all(&mut zf, text.as_bytes()).ok()?;
        }
        zf.finish().ok()?;
    }
    let slug = w.get("slug").and_then(|v| v.as_str())?;
    let filename = if selected { format!("{slug}-{kind}-selected.zip") } else { format!("{slug}-{kind}.zip") };
    if !filename.is_ascii() {
        return None;
    }
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(axum::http::header::CONTENT_TYPE, "application/zip")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{filename}\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(buf.into_inner()))
        .ok()
}

/// `GET /rules/download.md` (app/main.py `rules_download`): the world's rules as a .md attachment. A
/// non-GM needs the section readable AND the world's players_can_download_rules flag, and gets the
/// text without the :::gm blocks (the page strips them; the download must not carry them around that).
fn rules_download(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.clone() else { return Some(crate::idpages::refusal(403)) };
    if !p.is_gm {
        if !crate::pages::section_ok(&p, Some(&w), "rules")? {
            return Some(crate::idpages::refusal(403));
        }
        if !crate::edits::py_truthy_pub(w.get("players_can_download_rules")) {
            return Some(crate::idpages::refusal(403));
        }
    }
    let mut text = crate::retrieval::world_rules_markdown(&w);
    if !p.is_gm {
        text = crate::retrieval::strip_gm_directives(&text);
    }
    let fname = match w.get("slug").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => format!("{s}-rules.md"),
        _ => "core-rules.md".to_string(),
    };
    Some(Response::builder().header(axum::http::header::CONTENT_TYPE, "text/markdown; charset=utf-8")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{fname}\""))
        .header("x-nd-served-by", "gateway").body(Body::from(text)).unwrap())
}

/// `GET /pages/{doc_id}/download` (app/routers/pages.py `pages_download`): the page's file with
/// Content-Disposition: attachment — the same file the viewer renders, same visibility gate.
fn pages_download(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.clone() else { return Some(crate::idpages::refusal(404)) };
    if !crate::pages::section_ok(&p, Some(&w), "pages")? {
        return Some(crate::idpages::refusal(404));
    }
    let Some(doc) = rows_of(&db, "SELECT * FROM page_docs WHERE id = ?1", &[&id])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    if doc.get("world_id").and_then(|v| v.as_i64()) != w.get("id").and_then(|v| v.as_i64()) {
        return Some(crate::idpages::refusal(404));
    }
    if !p.is_gm && !crate::edits::py_truthy_pub(doc.get("visible_to_players")) {
        return Some(crate::idpages::refusal(404));
    }
    let file_url = doc.get("file_url").and_then(|v| v.as_str()).unwrap_or("");
    let rest = file_url.strip_prefix("/uploads/")?;
    if rest.is_empty() || rest.split('/').any(|seg| seg == "." || seg == "..") {
        return Some(crate::idpages::refusal(404));
    }
    let root = std::path::Path::new(&cfg.accel_root).join("uploads").canonicalize().ok()?;
    let path = root.join(rest).canonicalize().ok()?;
    if !path.starts_with(&root) || !path.is_file() {
        return Some(crate::idpages::refusal(404));
    }
    let bytes = std::fs::read(&path).ok()?;
    let fname: String = (doc.get("name").and_then(|v| v.as_str()).unwrap_or("page")).chars()
        .filter(|c| c.is_alphanumeric() || " -_".contains(*c)).collect();
    let fname = if fname.is_empty() { "page".to_string() } else { fname };
    let ext = path.extension().and_then(|v| v.to_str()).map(|e| format!(".{e}")).unwrap_or_else(|| ".html".to_string());
    // Starlette's FileResponse: a filename urllib.parse.quote would change (a space, non-ASCII) is
    // written in the RFC 5987 form, a plain one quoted
    let full = format!("{fname}{ext}");
    let enc: String = full.bytes().map(|b| match b {
        b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'.' | b'-' | b'_' | b'~' => format!("{}", b as char),
        _ => format!("%{b:02X}"),
    }).collect();
    let disposition = if enc == full { format!("attachment; filename=\"{full}\"") } else { format!("attachment; filename*=utf-8''{enc}") };
    Some(Response::builder().header(axum::http::header::CONTENT_TYPE, "text/html; charset=utf-8")
        .header(axum::http::header::CONTENT_DISPOSITION, disposition)
        .header("x-nd-served-by", "gateway").body(Body::from(bytes)).unwrap())
}

/// `GET /worlds/{world_id}/export/templates/{template_id}.json` (app/routers/export.py): one sheet
/// template as a json.dumps(indent=2) attachment — name, description, sheet_mode, the parsed fields.
fn sheet_template_json(cfg: &Config, uri: &Uri, headers: &HeaderMap, wid: i64, tid: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let Some(w) = rows_of(&db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let Some(t) = rows_of(&db, "SELECT * FROM sheet_templates WHERE id = ?1 AND world_id = ?2", &[&tid, &wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let fields: Value = serde_json::from_str(t.get("fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]"))
        .unwrap_or_else(|_| json!([]));
    // json.dumps(payload, ensure_ascii=False, indent=2): 2-space indent, ", " / ": " separators
    let payload = json!({
        "name": t.get("name").cloned().unwrap_or(Value::Null),
        "description": t.get("description").cloned().unwrap_or(Value::Null),
        "sheet_mode": t.get("sheet_mode").cloned().unwrap_or(Value::Null),
        "fields": fields,
    });
    let body = nd_render::py_dumps_indent_as(&payload, 2, false); // ensure_ascii=False
    let tid_v = t.get("id").and_then(|v| v.as_i64())?;
    let slug = t.get("slug").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string)
        .unwrap_or_else(|| format!("template-{tid_v}"));
    let wslug = w.get("slug").and_then(|v| v.as_str()).unwrap_or("world");
    Some(Response::builder().header(axum::http::header::CONTENT_TYPE, "application/json")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{wslug}-sheet-template-{slug}.json\""))
        .header("x-nd-served-by", "gateway").body(Body::from(body)).unwrap())
}

/// `_image_job_to_dict` (app/routers/ai.py): result_urls_json json.loads'd with no fallback —
/// unreadable text is Python's own 500 (decline)
fn image_job_dict(j: &Map<String, Value>) -> Option<Value> {
    let urls = match j.get("result_urls_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()) {
        Some(t) => serde_json::from_str::<Value>(t).ok()?,
        None => json!([]),
    };
    let iso = |k: &str| match j.get(k).and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
        Some(d) => json!(d.isoformat()),
        None => Value::Null,
    };
    Some(json!({
        "id": j.get("id").cloned().unwrap_or(Value::Null),
        "prompt": j.get("prompt").cloned().unwrap_or(Value::Null),
        "status": j.get("status").cloned().unwrap_or(Value::Null),
        "error": j.get("error").cloned().unwrap_or(Value::Null),
        "urls": urls,
        "created_at": iso("created_at"),
        "updated_at": iso("updated_at"),
    }))
}

/// `GET /api/ai/imagegen/jobs/{job_id}` (GM's own status; the player one below it)
fn imagegen_job_status_gm(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    let Some(j) = rows_of(&db, "SELECT * FROM image_jobs WHERE id = ?1 AND world_id = ?2", &[&id, &wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    crate::edits::json_response(&image_job_dict(&j)?)
}

/// `GET /api/ai/imagegen/jobs`: the world's newest 20
fn imagegen_jobs_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    let rows = rows_of(&db, "SELECT * FROM image_jobs WHERE world_id = ?1 ORDER BY created_at DESC, id DESC LIMIT 20", &[&wid])?;
    let mut out = vec![];
    for j in &rows {
        out.push(image_job_dict(j)?);
    }
    crate::edits::json_response(&Value::Array(out))
}

fn chat_job_dict(j: &Map<String, Value>) -> Option<Value> {
    let v = |k: &str| j.get(k).cloned().unwrap_or(Value::Null);
    Some(json!({"id": v("id"), "prompt": v("prompt"), "status": v("status"), "error": v("error"), "result": v("result"),
        "model": if crate::edits::py_truthy_pub(j.get("model")) { v("model") } else { json!("") },
        "created_at": dt_iso(j, "created_at")?, "updated_at": dt_iso(j, "updated_at")?}))
}

/// `GET /api/ai/chat/jobs/<id>` (`api_chat_job_status`): the GM's background chat job in the active world
fn chat_job_status(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let Some(j) = rows_of(&db, "SELECT * FROM chat_jobs WHERE id = ?1 AND world_id = ?2", &[&id, &wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    crate::edits::json_response(&chat_job_dict(&j)?)
}

/// `GET /api/ai/chat/jobs`: the world's newest 20
fn chat_jobs_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let mut out = vec![];
    for j in rows_of(&db, "SELECT * FROM chat_jobs WHERE world_id = ?1 ORDER BY created_at DESC, id DESC LIMIT 20", &[&wid])? {
        out.push(chat_job_dict(&j)?);
    }
    crate::edits::json_response(&Value::Array(out))
}

/// `GET /api/ai/imagegen/player/jobs`: a player's own jobs, the GM's view of everyone's with the owner's name
fn player_image_jobs_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let w = p.world.as_ref();
    if !p.is_gm && !w.map(|w| crate::edits::py_truthy_pub(w.get("players_can_use_image_gen"))).unwrap_or(false) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w?.get("id")?.as_i64()?;
    let rows = if p.is_gm {
        rows_of(&db, "SELECT * FROM image_jobs WHERE world_id = ?1 ORDER BY created_at DESC, id DESC LIMIT 50", &[&wid])?
    } else {
        rows_of(&db, "SELECT * FROM image_jobs WHERE world_id = ?1 AND created_by_user_id = ?2 ORDER BY created_at DESC, id DESC LIMIT 50", &[&wid, &p.user_id])?
    };
    let mut out = vec![];
    for j in &rows {
        let mut d = image_job_dict(j)?;
        if p.is_gm {
            let owner = match j.get("created_by_user_id").and_then(|v| v.as_i64()) {
                Some(uid) => match rows_of(&db, "SELECT display_name, email FROM users WHERE id = ?1", &[&uid])?.into_iter().next() {
                    Some(u) => [u.get("display_name"), u.get("email")].into_iter().flatten().find(|v| crate::edits::py_truthy_pub(Some(v))).cloned(),
                    None => None,
                },
                None => None,
            };
            d.as_object_mut()?.insert("owner".into(), owner.unwrap_or_else(|| json!("GM")));
        }
        out.push(d);
    }
    crate::edits::json_response(&Value::Array(out))
}

/// DELETE routes: `/api/ai/chat/jobs/<id>` (a finished job's row) and `/api/ai/imagegen/model-templates/custom/<id>` (one of the world's own
/// templates). Anything else, or a case Python judges otherwise, is Python's.
pub fn delete(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "DELETE", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let path = uri.path();
    if let Some(id) = trailing_id(path, "/api/ai/chat/jobs/") {
        let wid = p.world.as_ref()?.get("id")?.as_i64()?;
        let Some(j) = rows_of(&db, "SELECT status FROM chat_jobs WHERE id = ?1 AND world_id = ?2", &[&id, &wid])?.into_iter().next() else {
            return Some(crate::idpages::refusal(404));
        };
        if matches!(j.get("status").and_then(|v| v.as_str()), Some("pending" | "generating")) {
            return None; // IN_PROGRESS_STATUSES: Python's 400 / the running task
        }
        db.execute("DELETE FROM chat_jobs WHERE id = ?1", [id]).ok()?;
        return crate::edits::json_response(&json!({"ok": true}));
    }
    let id = trailing_id(path, "/api/ai/imagegen/model-templates/custom/")?;
    let Some(row) = rows_of(&db, "SELECT scope, world_id FROM prompt_presets WHERE id = ?1", &[&id])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let world_id = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    if row.get("scope").and_then(|v| v.as_str()) != Some("image_template") || world_id.is_some_and(|w| row.get("world_id").and_then(|v| v.as_i64()) != Some(w)) {
        return Some(crate::idpages::refusal(404));
    }
    db.execute("DELETE FROM prompt_presets WHERE id = ?1", [id]).ok()?;
    crate::edits::json_response(&json!({"deleted": id}))
}

/// What `GET /api/sessions/<id>/live-audio/download` comes to: an answer, or the one saved segment to send as a download
pub enum LiveDownload {
    Answer(Response<Body>),
    File(std::path::PathBuf, String),
    /// several segments to stitch with ffmpeg into the output file, then send under the name
    Concat(Vec<std::path::PathBuf>, std::path::PathBuf, String),
}

/// `_ffmpeg_concat_quote`
fn concat_quote(p: &std::path::Path) -> String {
    format!("file '{}'", p.to_string_lossy().replace('\'', "'\\''"))
}

/// `_concat_live_segments`: ffmpeg's concat demuxer with -c copy to a .part file renamed into place on success; Err = Python's 400 text
pub async fn concat_live_segments(segs: &[std::path::PathBuf], out: &std::path::Path) -> Result<(), String> {
    let dir = out.parent().ok_or("bad path")?;
    tokio::fs::create_dir_all(dir).await.map_err(|e| e.to_string())?;
    let list = dir.join(format!("tmp{:08x}.txt", rand::random::<u32>()));
    let text = segs.iter().map(|p| concat_quote(p)).collect::<Vec<_>>().join("\n") + "\n";
    tokio::fs::write(&list, text).await.map_err(|e| e.to_string())?;
    let stem = out.file_stem().and_then(|s| s.to_str()).unwrap_or("recording");
    let ext = out.extension().and_then(|s| s.to_str()).map(|e| format!(".{e}")).unwrap_or_default();
    let tmp_out = out.with_file_name(format!("{stem}.part{ext}"));
    let run = tokio::process::Command::new("ffmpeg").args(["-y", "-f", "concat", "-safe", "0", "-i"]).arg(&list).args(["-c", "copy"]).arg(&tmp_out)
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::piped()).output().await;
    let result = match run {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err("ffmpeg is not available on the server \u{2014} cannot assemble the recording.".to_string()),
        Err(e) => Err(e.to_string()),
        Ok(o) => {
            let size = tokio::fs::metadata(&tmp_out).await.map(|m| m.len()).unwrap_or(0);
            if !o.status.success() || size == 0 {
                let err = String::from_utf8_lossy(&o.stderr).to_string();
                let chars: Vec<char> = err.chars().collect();
                let tail: String = chars[chars.len().saturating_sub(300)..].iter().collect();
                Err(format!("ffmpeg concat failed (rc={}): {tail}", o.status.code().unwrap_or(-1)))
            } else {
                tokio::fs::rename(&tmp_out, out).await.map_err(|e| e.to_string())
            }
        }
    };
    let _ = tokio::fs::remove_file(&list).await;
    let _ = tokio::fs::remove_file(&tmp_out).await;
    result
}

/// `api_live_audio_download` for the cases that need no ffmpeg: the access decision, "nothing saved" (400) and a single segment (served as it is).
/// Several segments are concatenated by Python; Range / conditional requests are Python's too.
pub fn live_audio_download(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<LiveDownload> {
    use axum::http::header;
    if [header::RANGE, header::IF_NONE_MATCH, header::IF_MODIFIED_SINCE, header::IF_MATCH, header::IF_UNMODIFIED_SINCE, header::IF_RANGE].iter().any(|h| headers.contains_key(h)) {
        return None;
    }
    let sid = id_between(uri.path(), "/api/sessions/", "/live-audio/download")?;
    let (db, p) = page(cfg, uri, headers)?;
    let Some(gs) = rows_of(&db, "SELECT * FROM game_sessions WHERE id = ?1", &[&sid])?.into_iter().next() else {
        return Some(LiveDownload::Answer(crate::idpages::refusal(404)));
    };
    if let Some(r) = crate::edits::session_view_gate(&db, &p, gs.get("world_id")?.as_i64()?)? {
        return Some(LiveDownload::Answer(r));
    }
    let uploads = cfg.db_path.parent()?.join("uploads");
    let mut segs = vec![];
    for rel in crate::edits::live_audio_files(&gs)? {
        if rel.starts_with('/') || rel.contains("..") || rel.contains('\\') || rel.is_empty() {
            return None;
        }
        let path = uploads.join(&rel);
        if path.is_file() {
            segs.push(path);
        }
    }
    match segs.len() {
        0 => Some(LiveDownload::Answer(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No raw audio saved for this session."))),
        1 => {
            let ext = segs[0].extension().and_then(|e| e.to_str()).map(|e| format!(".{e}")).unwrap_or_else(|| ".webm".into());
            Some(LiveDownload::File(segs.remove(0), format!("session-{sid}-recording{ext}")))
        }
        _ => {
            let ext = segs[0].extension().and_then(|e| e.to_str()).map(|e| format!(".{e}")).unwrap_or_else(|| ".webm".into());
            let out = uploads.join("live").join(sid.to_string()).join(format!("recording{ext}"));
            let newest = segs.iter().filter_map(|s| s.metadata().ok()?.modified().ok()).max()?;
            let name = format!("session-{sid}-recording{ext}");
            let fresh = out.metadata().ok().filter(|m| m.is_file() && m.len() > 0 && m.modified().map_or(false, |t| t >= newest)).is_some();
            Some(if fresh { LiveDownload::File(out, name) } else { LiveDownload::Concat(segs, out, name) })
        }
    }
}

/// `GET /api/sessions/<id>/live-transcript/check`: only the "no Studio API key" answer (a GM); a key means a probe call to Studio, which is Python's
fn live_transcript_check(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let exists = db.query_row("SELECT 1 FROM game_sessions WHERE id = ?1", [id], |_| Ok(())).is_ok();
    if !exists {
        return Some(crate::idpages::refusal(404));
    }
    if !p.is_gm {
        return None;
    }
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    if key.is_some() {
        return None;
    }
    crate::edits::json_response(&json!({"ok": false, "backend": "unsloth", "model": crate::stt::stt_model(cfg),
        "message": "Speech-to-text runs on Unsloth Studio, but no Studio API key is set (Settings \u{2192} System)."}))
}

fn world_id_of(p: &crate::layout::PageCtx) -> Option<i64> {
    p.world.as_ref()?.get("id")?.as_i64()
}

/// `GET /api/backups` (app/routers/backups.py): the `world-*.db` snapshots in ND_BACKUP_DIR, newest name first. GM only (anyone else: Python's gate).
/// `created_at` is the file's mtime in the server's local time (TZ).
fn backups_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let dir = std::env::var("ND_BACKUP_DIR").unwrap_or_default();
    if dir.trim().is_empty() {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "Scheduled backups are not configured \u{2014} set ND_BACKUP_DIR"));
    }
    let mut out: Vec<(String, u64, String)> = vec![];
    for entry in std::fs::read_dir(dir.trim()).ok()? {
        let entry = entry.ok()?;
        let name = entry.file_name().into_string().ok()?;
        if !(name.starts_with("world-") && name.ends_with(".db")) || name.len() < 9 {
            continue;
        }
        let meta = entry.metadata().ok()?;
        let t = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?;
        let total_micros = (t.as_nanos() + 500) / 1000;
        let (secs, micros) = ((total_micros / 1_000_000) as i64, (total_micros % 1_000_000) as u32);
        let (y, m, d, hh, mm, ss) = nd_render::pyvalue::local_civil(secs);
        let mut iso = format!("{y:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}");
        if micros != 0 {
            iso.push_str(&format!(".{micros:06}"));
        }
        out.push((name, meta.len(), iso));
    }
    out.sort_by(|a, b| b.0.cmp(&a.0));
    let list: Vec<Value> = out.into_iter().map(|(n, size, at)| json!({"name": n, "size_bytes": size, "created_at": at})).collect();
    crate::edits::json_response(&json!({"backups": list}))
}

/// `GET /admin/diagnostics/events`: events.log from the diagnostics folder (ND_DIAG_DIR, else beside the database) as plain text. GM only.
fn diagnostics_events(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let dir = match std::env::var("ND_DIAG_DIR") {
        Ok(d) if !d.trim().is_empty() => std::path::PathBuf::from(d.trim()),
        _ => cfg.db_path.parent()?.join("diagnostics"),
    };
    if !dir.is_dir() {
        return None; // Python creates it; leave that to it
    }
    let text = match std::fs::read(dir.join("events.log")) {
        Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
        Err(_) => String::new(),
    };
    // str.splitlines() also splits on \x0b \x0c \x1c-\x1e \x85 \u2028 \u2029: anything but \n / \r\n is Python's
    if text.chars().any(|c| matches!(c, '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}')) {
        return None;
    }
    let lines: Vec<&str> = text.split('\n').collect();
    let mut lines = lines;
    if lines.last() == Some(&"") {
        lines.pop();
    }
    let body = if lines.is_empty() { "(no events recorded yet)\n".to_string() } else { format!("{}\n", lines.join("\n")) };
    crate::edits::json_response_raw(&body, "text/plain; charset=utf-8")
}

/// `GET /api/gallery/browse[?album_id=N]` (app/routers/gallery.py): the picker's album tree - sub-albums with cover and counts, and at an
/// album its pictures (named) and breadcrumb. GM only; an album of another world is Python's 404.
fn gallery_browse(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let album_id = match crate::pages::query_param(uri, "album_id") {
        None => 0,
        Some(t) if !t.is_empty() && t.len() < 18 && t.bytes().all(|c| c.is_ascii_digit()) => t.parse::<i64>().ok()?,
        Some(_) => return None,
    };
    let urls_of = |a: &Map<String, Value>| -> Option<Vec<String>> {
        crate::gallery::album_urls(a).as_array()?.iter().map(|u| u.as_str().map(|s| s.to_string())).collect()
    };
    let (children, images, crumbs) = if album_id != 0 {
        let album = rows_of(&db, "SELECT * FROM image_albums WHERE id = ?1", &[&album_id])?.into_iter().next();
        let Some(album) = album.filter(|a| a.get("world_id").and_then(|v| v.as_i64()) == Some(wid)) else { return Some(crate::idpages::refusal(404)) };
        let children = rows_of(&db, "SELECT * FROM image_albums WHERE parent_id = ?1 ORDER BY name", &[&album_id])?;
        let urls = urls_of(&album)?;
        let names = crate::gallery::world_image_names(&db, wid, &urls)?;
        let images: Vec<Value> = urls.iter().map(|u| json!({"url": u, "name": names.get(u)})).collect();
        let mut crumbs = crate::media::breadcrumb(&db, "image_albums", &album)?;
        crumbs.push(Value::Object(album));
        (children, Value::Array(images), crumbs)
    } else {
        (rows_of(&db, "SELECT * FROM image_albums WHERE world_id = ?1 AND parent_id IS NULL ORDER BY name", &[&wid])?, Value::Null, vec![])
    };
    let mut albums = vec![];
    for a in &children {
        let urls = urls_of(a)?;
        let id = a.get("id").and_then(|v| v.as_i64())?;
        let subs: i64 = db.query_row("SELECT COUNT(*) FROM image_albums WHERE parent_id = ?1", [id], |r| r.get(0)).ok()?;
        albums.push(json!({"id": id, "name": a.get("name"), "cover_url": urls.first(), "image_count": urls.len(), "sub_album_count": subs}));
    }
    let crumbs: Vec<Value> = crumbs.iter().map(|b| json!({"id": b.get("id"), "name": b.get("name")})).collect();
    crate::edits::json_response(&json!({"breadcrumb": crumbs, "albums": albums, "images": images}))
}

/// The media-rename routes' gate: the active world (404 without) and the sections the viewer may edit, in `SECTION` order (audio, video, image)
fn media_rename_gate(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Result<(rusqlite::Connection, i64, Vec<&'static str>), Response<Body>>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(Err(crate::idpages::refusal(404))) };
    let wid = w.get("id")?.as_i64()?;
    let mut kinds = vec![];
    for (kind, section) in [("audio", "audio"), ("video", "video"), ("image", "images")] {
        if crate::pages::edit_level(&p, w, section)? {
            kinds.push(kind);
        }
    }
    Some(Ok((db, wid, kinds)))
}

/// `GET /api/media-rename/runs`: the world's 20 newest runs
fn media_rename_runs(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, wid, kinds) = match media_rename_gate(cfg, uri, headers)? {
        Ok(g) => g,
        Err(r) => return Some(r),
    };
    if kinds.is_empty() {
        return Some(crate::idpages::refusal(403));
    }
    let runs = rows_of(&db, "SELECT * FROM media_rename_runs WHERE world_id = ?1 ORDER BY id DESC LIMIT 20", &[&wid])?;
    let mut out = vec![];
    for r in &runs {
        out.push(media_run_view(&db, r, false)?);
    }
    crate::edits::json_response(&json!({"runs": out}))
}

/// `GET /api/media-rename/summary`: {audio|video|image: {total, needs_work}} for the kinds the viewer may edit
fn media_rename_summary(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, wid, kinds) = match media_rename_gate(cfg, uri, headers)? {
        Ok(g) => g,
        Err(r) => return Some(r),
    };
    if kinds.is_empty() {
        return Some(crate::idpages::refusal(403));
    }
    let mut out = Map::new();
    for kind in kinds {
        let (total, needs) = if kind == "image" {
            let usage = crate::media_rename::image_usage(&db, wid)?;
            (usage.len(), usage.iter().filter(|u| crate::media_rename::looks_generic(&u.3) && u.1.is_empty()).count())
        } else {
            let table = if kind == "audio" { "audio_clips" } else { "video_clips" };
            let rows = rows_of(&db, &format!("SELECT name, file_url FROM {table} WHERE world_id = ?1 ORDER BY name, id"), &[&wid])?;
            let needs = rows.iter().filter(|r| crate::media_rename::looks_generic(r.get("name").and_then(|v| v.as_str()).unwrap_or(""))).count();
            (rows.len(), needs)
        };
        out.insert(kind.into(), json!({"total": total, "needs_work": needs}));
    }
    crate::edits::json_response(&Value::Object(out))
}

/// `_album_labels`: [(album id, "Parent / Child")] in table order; None when a name is not text
fn media_album_labels(db: &rusqlite::Connection, table: &str, wid: i64) -> Option<Vec<(i64, String)>> {
    let albums = rows_of(db, &format!("SELECT * FROM {table} WHERE world_id = ?1"), &[&wid])?;
    let by_id: std::collections::HashMap<i64, &Map<String, Value>> = albums.iter().filter_map(|a| Some((a.get("id")?.as_i64()?, a))).collect();
    fn label(a: &Map<String, Value>, by_id: &std::collections::HashMap<i64, &Map<String, Value>>, depth: u32) -> Option<String> {
        let parent = a.get("parent_id").and_then(|v| v.as_i64()).filter(|p| *p != 0 && depth < 20).and_then(|p| by_id.get(&p).copied());
        let own = a.get("name").and_then(|v| v.as_str())?;
        Some(match parent {
            Some(p) => format!("{} / {own}", label(p, by_id, depth + 1)?),
            None => own.to_string(),
        })
    }
    albums.iter().map(|a| Some((a.get("id")?.as_i64()?, label(a, &by_id, 0)?))).collect()
}

/// `GET /api/media-rename/items?kind=&album=&q=`: the library's items to choose from, each with `generic`. Text matching lower-cases in
/// Python: anything past ASCII on either side is Python's.
fn media_rename_items(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    const MAX_LISTED: usize = 1500;
    let (db, wid, kinds) = match media_rename_gate(cfg, uri, headers)? {
        Ok(g) => g,
        Err(r) => return Some(r),
    };
    let kind_q = crate::pages::query_param(uri, "kind");
    let Some(kind) = kind_q.as_deref().filter(|k| kinds.contains(k)) else { return Some(crate::idpages::refusal(403)) };
    let q_raw = crate::pages::query_param(uri, "q").unwrap_or_default();
    let q = nd_render::text::py_strip(&q_raw).to_string();
    if !q.is_ascii() {
        return None;
    }
    let q = q.to_ascii_lowercase();
    let album = match crate::pages::query_param(uri, "album").unwrap_or_default().as_str() {
        "" => 0,
        t => match crate::form::py_int(t) {
            Some(n) => n,
            None if t.bytes().all(|c| c.is_ascii_alphabetic() || c == b'.') => 0, // int() refuses words and floats alike
            None => return None,
        },
    };
    let mut items = vec![];
    let mut generic_flags = vec![];
    let labels;
    if kind == "audio" || kind == "video" {
        let (table, albums) = if kind == "audio" { ("audio_clips", "audio_albums") } else { ("video_clips", "video_albums") };
        labels = media_album_labels(&db, albums, wid)?;
        let rows = if album != 0 {
            rows_of(&db, &format!("SELECT * FROM {table} WHERE world_id = ?1 AND album_id = ?2 ORDER BY name, id LIMIT {}", MAX_LISTED + 1), &[&wid, &album])?
        } else {
            rows_of(&db, &format!("SELECT * FROM {table} WHERE world_id = ?1 ORDER BY name, id LIMIT {}", MAX_LISTED + 1), &[&wid])?
        };
        for row in rows {
            let text = |k: &str| row.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let fname = crate::media_rename::readable_filename(&text("file_url"));
            let name = text("name");
            if !q.is_empty() {
                if !(name.is_ascii() && fname.is_ascii()) {
                    return None;
                }
                if !name.to_ascii_lowercase().contains(&q) && !fname.to_ascii_lowercase().contains(&q) {
                    continue;
                }
            }
            let generic = crate::media_rename::looks_generic(&name);
            let poster = text("poster_url");
            let thumb = if kind == "video" && !poster.is_empty() { json!(nd_render::thumb_url(&poster)?) } else { Value::Null };
            let album_label = row.get("album_id").and_then(|v| v.as_i64()).and_then(|a| labels.iter().find(|l| l.0 == a)).map(|l| l.1.clone()).unwrap_or_default();
            generic_flags.push(generic);
            items.push(json!({
                "kind": kind, "id": row.get("id"), "name": name, "filename": fname, "album": album_label, "generic": generic,
                "has_transcript": !nd_render::text::py_strip(&text("transcript")).is_empty(),
                "visible": crate::edits::py_truthy_pub(row.get("visible_to_players")), "thumb": thumb,
            }));
        }
    } else {
        labels = media_album_labels(&db, "image_albums", wid)?;
        let usage = crate::media_rename::image_usage(&db, wid)?;
        let in_album: Option<Vec<String>> = if album != 0 {
            let chosen = rows_of(&db, "SELECT * FROM image_albums WHERE id = ?1 AND world_id = ?2", &[&album, &wid])?.into_iter().next();
            Some(match chosen {
                Some(a) => crate::gallery::album_urls(&a).as_array()?.iter().filter_map(|u| u.as_str().map(str::to_string)).collect(),
                None => vec![],
            })
        } else {
            None
        };
        let mut sorted: Vec<&crate::media_rename::ImageUse> = usage.iter().collect();
        sorted.sort_by(|a, b| a.0.cmp(&b.0));
        for (url, uses, albums, name) in sorted.into_iter().map(|u| (&u.0, &u.1, &u.2, &u.3)) {
            if in_album.as_ref().is_some_and(|set| !set.contains(url)) {
                continue;
            }
            let fname = crate::media_rename::readable_filename(url);
            if !q.is_empty() {
                if !(name.is_ascii() && fname.is_ascii()) {
                    return None;
                }
                if !name.to_ascii_lowercase().contains(&q) && !fname.to_ascii_lowercase().contains(&q) {
                    continue;
                }
            }
            let generic = crate::media_rename::looks_generic(name) && uses.is_empty();
            generic_flags.push(generic);
            items.push(json!({
                "kind": "image", "url": url, "name": name, "filename": fname, "album": albums.iter().take(3).cloned().collect::<Vec<_>>().join(", "),
                "used_in": uses.iter().take(3).collect::<Vec<_>>(), "thumb": nd_render::thumb_url(url)?, "generic": generic,
            }));
        }
    }
    let mut album_list: Vec<(i64, String)> = labels;
    if album_list.iter().any(|l| !l.1.is_ascii()) {
        return None; // sorted(key=str.lower)
    }
    album_list.sort_by_key(|l| l.1.to_ascii_lowercase());
    let truncated = items.len() > MAX_LISTED;
    items.truncate(MAX_LISTED);
    generic_flags.truncate(MAX_LISTED);
    let albums: Vec<Value> = album_list.into_iter().map(|(i, n)| json!({"id": i, "name": n})).collect();
    crate::edits::json_response(&json!({"kind": kind, "items": items, "albums": albums, "truncated": truncated,
        "generic_count": generic_flags.iter().filter(|g| **g).count()}))
}

/// `GET /api/kiy/plays` (app/routers/lore_extras.py): the saved King in Yellow plays (kiy_plays.json beside the database) without their text. GM only.
fn kiy_plays(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let raw = std::fs::read(cfg.db_path.parent()?.join("kiy_plays.json")).ok().and_then(|b| String::from_utf8(b).ok());
    let plays: Vec<Value> = match raw {
        None => vec![],
        Some(t) => {
            if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
                return None;
            }
            match serde_json::from_str::<Value>(&t) {
                Ok(Value::Array(a)) => a,
                Ok(Value::Object(o)) if o.is_empty() => vec![],
                Ok(_) => return None, // iterating anything else raises in Python
                Err(_) => vec![], // unreadable: Python starts over with an empty library
            }
        }
    };
    let mut out = vec![];
    for play in &plays {
        let o = play.as_object()?;
        out.push(json!({"id": o.get("id")?, "title": o.get("title")?, "ts": o.get("ts").cloned().unwrap_or(json!(0))}));
    }
    crate::edits::json_response(&json!({"plays": out, "count": plays.len()}))
}

/// What Python's `GET /api/live-jobs` holds besides the gateway's own rows, for a gateway with no Python behind it: the world's 20 newest bulk
/// rename runs ("r:" keys), under the same 404 / 403 gate. `None`: the caller is not signed in or something is unusual.
pub fn live_jobs_python_part(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    if !crate::pages::section_ok(&p, Some(w), "background_jobs")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
    let epoch = |r: &Map<String, Value>, key: &str| -> Option<Option<f64>> {
        match r.get(key) {
            None | Some(Value::Null) => Some(None),
            Some(v) => {
                let d = nd_render::pyvalue::PyDateTime::parse(v.get("__dt__").or(Some(v))?.as_str()?)?;
                Some(Some(d.epoch_secs() as f64 + d.us as f64 / 1e6))
            }
        }
    };
    let mut rows = vec![];
    for run in rows_of(&db, "SELECT * FROM media_rename_runs WHERE world_id = ?1 ORDER BY id DESC LIMIT 20", &[&wid])? {
        let status = run.get("status").and_then(|v| v.as_str()).unwrap_or("");
        let in_progress = matches!(status, "pending" | "running");
        let started = epoch(&run, "created_at")?.unwrap_or(now);
        let finished = if in_progress { None } else { epoch(&run, "updated_at")? };
        let shown = if status == "pending" { "running" } else { status };
        let end = finished.unwrap_or(now);
        let num = |k: &str| run.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
        rows.push(json!({
            "key": format!("r:{}", run.get("id")?.as_i64()?), "source": "rename", "label": "Bulk rename", "detail": "", "status": shown,
            "error": run.get("error").and_then(|v| v.as_str()).unwrap_or(""), "started": started, "elapsed": ((end - started) as i64).max(0), "position": 0,
            "progress": format!("{} / {}", num("done"), num("total")), "can_cancel": in_progress,
            "can_restart": matches!(status, "cancelled" | "error" | "interrupted"), "can_remove": !in_progress,
        }));
    }
    crate::edits::json_response(&json!({"jobs": rows}))
}

/// `GET /api/sessions/ai/audio-jobs[?game_session_id=N]` (app/routers/sessions.py): the world's 20 newest recap / condense jobs
fn session_audio_job_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let jobs = match crate::pages::query_param(uri, "game_session_id") {
        None => rows_of(&db, "SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose IN ('session_recap', 'condense') ORDER BY created_at DESC LIMIT 20", &[&wid])?,
        Some(t) if !t.is_empty() && t.len() < 18 && t.bytes().all(|c| c.is_ascii_digit()) => {
            let gs: i64 = t.parse().ok()?;
            rows_of(&db, "SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose IN ('session_recap', 'condense') AND game_session_id = ?2 ORDER BY created_at DESC LIMIT 20", &[&wid, &gs])?
        }
        Some(_) => return None,
    };
    let list: Option<Vec<Value>> = jobs.iter().map(session_job_value).collect();
    crate::edits::json_response(&Value::Array(list?))
}

/// `GET /api/media-rename/history` (`recent_batches`): the latest 10 applied rename batches, newest first - when, by whom, how many, whether
/// undone, up to three examples
fn media_rename_history(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, wid, kinds) = match media_rename_gate(cfg, uri, headers)? {
        Ok(g) => g,
        Err(r) => return Some(r),
    };
    if kinds.is_empty() {
        return Some(crate::idpages::refusal(403));
    }
    crate::edits::json_response(&json!({"batches": media_rename_batches(&db, wid)?}))
}

/// `recent_batches`: the latest 10 applied rename batches of a world, newest first
pub(crate) fn media_rename_batches(db: &rusqlite::Connection, wid: i64) -> Option<Vec<Value>> {
    let logs = rows_of(db, "SELECT * FROM media_rename_log WHERE world_id = ?1 ORDER BY id DESC LIMIT 2000", &[&wid])?;
    // batch id -> (first log, count, all undone, examples), in the order batches first appear
    let mut order: Vec<Value> = vec![];
    let mut batches: Vec<(Value, Map<String, Value>, i64, bool, Vec<Value>)> = vec![];
    for log in logs {
        let id = log.get("batch_id").cloned().unwrap_or(Value::Null);
        let undone = !matches!(log.get("undone_at"), None | Some(Value::Null));
        let at = log.get("created_at").cloned().unwrap_or(Value::Null);
        let idx = match order.iter().position(|o| *o == id) {
            Some(i) => i,
            None => {
                order.push(id.clone());
                let mut first = Map::new();
                first.insert("at".into(), at);
                first.insert("user_id".into(), log.get("user_id").cloned().unwrap_or(Value::Null));
                batches.push((id, first, 0, true, vec![]));
                order.len() - 1
            }
        };
        let b = &mut batches[idx];
        b.2 += 1;
        b.3 = b.3 && undone;
        if b.4.len() < 3 {
            b.4.push(json!({"old": log.get("old_name"), "new": log.get("new_name")}));
        }
    }
    let mut out = vec![];
    for (id, first, count, undone, examples) in batches.into_iter().take(10) {
        let by = match first.get("user_id").and_then(|v| v.as_i64()).filter(|u| *u != 0) {
            Some(uid) => rows_of(db, "SELECT * FROM users WHERE id = ?1", &[&uid])?.into_iter().next().and_then(|u| {
                u.get("display_name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string)
                    .or_else(|| u.get("email")?.as_str()?.split('@').next().map(str::to_string))
            }),
            None => None,
        };
        out.push(json!({"batch_id": id, "at": dt_strftime(&first, "at", "%Y-%m-%dT%H:%M:%SZ")?, "count": count, "undone": undone,
            "examples": examples, "by": by.as_deref().unwrap_or("someone")}));
    }
    Some(out)
}

/// `GET /api/ai/sessions` (app/routers/ai.py): the active world's saved Chat conversations, newest first. GM only (anyone else: Python's 403).
fn ai_chat_sessions(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let rows = rows_of(&db, "SELECT id, title FROM chat_sessions WHERE world_id = ?1 AND surface = 'chat' ORDER BY updated_at DESC", &[&wid])?;
    let list: Vec<Value> = rows.iter().map(|r| json!({"id": r.get("id"), "title": r.get("title")})).collect();
    crate::edits::json_response(&json!({"sessions": list}))
}

/// `GET /api/ai/sessions/<id>`: one saved Chat conversation: `{id, messages}`. GM only; another world's or surface's row is a 404
fn ai_chat_session_get(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let rows = rows_of(&db, "SELECT id, messages_json FROM chat_sessions WHERE id = ?1 AND world_id = ?2 AND surface = 'chat'", &[&id, &wid])?;
    let Some(row) = rows.into_iter().next() else { return Some(crate::idpages::refusal(404)) };
    let messages = py_json_strict(row.get("messages_json").and_then(|v| v.as_str()), "[]")?;
    crate::edits::json_response(&json!({"id": id, "messages": messages}))
}

/// `GET /api/ai/recap-instructions`: the world's standing recap instructions. GM only.
fn ai_recap_instructions(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let Some(w) = p.world.as_ref() else { return Some(crate::idpages::refusal(404)) };
    let text = w.get("recap_instructions").and_then(|v| v.as_str()).unwrap_or("");
    crate::edits::json_response(&json!({"instructions": text}))
}

/// `get_defaults()`: the model chosen for each surface, from ai_models.json beside the database
pub(crate) fn model_defaults(cfg: &Config) -> Option<Value> {
    let data: Value = match std::fs::read(cfg.db_path.parent()?.join("ai_models.json")) {
        Ok(b) => {
            let t = String::from_utf8(b).ok()?;
            if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
                return None;
            }
            serde_json::from_str(&t).unwrap_or_else(|_| json!({"custom": [], "hidden": []})) // unreadable: the empty library
        }
        Err(_) => json!({"custom": [], "hidden": []}),
    };
    let defaults = match data.as_object()?.get("defaults") {
        None => Map::new(),
        Some(Value::Object(d)) => d.clone(),
        Some(_) => return None,
    };
    let mut out = Map::new();
    for surface in ["chat", "ask_ai", "image", "recap", "assist"] {
        out.insert(surface.into(), defaults.get(surface).cloned().unwrap_or(json!("")));
    }
    Some(Value::Object(out))
}

/// `GET /api/ai/defaults`: the model chosen for each surface. GM only.
fn ai_defaults(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    crate::edits::json_response(&model_defaults(cfg)?)
}

/// `GET /api/ai/prompt-presets?scope=chat|image`: the world's quick prompts. A world with no chat prompts yet gets Python's seeded defaults
/// (a write), so that case is Python's.
fn ai_prompt_presets(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let scope = crate::pages::query_param(uri, "scope")?;
    if scope != "chat" && scope != "image" {
        return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "scope must be 'chat' or 'image'"));
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let rows = rows_of(&db, "SELECT * FROM prompt_presets WHERE world_id = ?1 AND scope = ?2 ORDER BY sort_order, id", &[&wid, &scope])?;
    if rows.is_empty() && scope == "chat" {
        return None;
    }
    let text = |r: &Map<String, Value>, k: &str| {
        let v = r.get(k).cloned().unwrap_or(Value::Null);
        if crate::edits::py_truthy_pub(Some(&v)) { v } else { json!("") }
    };
    let list: Vec<Value> = rows.iter().map(|r| json!({"id": r.get("id"), "scope": r.get("scope"), "label": r.get("label"), "icon": text(r, "icon"),
        "text": text(r, "text"), "negative": text(r, "negative")})).collect();
    crate::edits::json_response(&json!({"presets": list}))
}

/// The Unsloth Studio routes that start with `_unsloth_or_400()`: with no Studio API key (Settings or UNSLOTH_API_KEY) the answer is Python's 400.
/// With a key they talk to Studio, which is Python's. GM only.
fn unsloth_unconfigured(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    if matches!(uri.path(), "/api/ai/unsloth/hub/download-progress" | "/api/ai/unsloth/gguf-variants") && crate::pages::query_param(uri, "repo_id").is_none() {
        return None; // the 422 for the missing parameter comes first
    }
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    if key.is_some() {
        return None;
    }
    Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No Unsloth backend configured \u{2014} set UNSLOTH_API_KEY (Settings \u{2192} System)."))
}

/// The image-generation status routes whose answer follows from the configuration alone (app/ai.py `_get_type` / `_get_url`): nothing set up, or Studio
/// as the image backend, where Python has nothing to ask. A SwarmUI / ComfyUI back end to talk to is Python's. GM only.
fn imagegen_unbacked(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    let studio = key.is_some() && std::env::var("UNSLOTH_IMAGE_MODEL").map(|m| !m.is_empty()).unwrap_or(false);
    let (kind, has_url) = if studio {
        ("unsloth".to_string(), true)
    } else {
        let t = std::env::var("IMAGEGEN_TYPE").unwrap_or_default();
        if !t.is_ascii() {
            return None;
        }
        (t.to_ascii_lowercase(), !std::env::var("IMAGEGEN_URL").unwrap_or_default().trim_end_matches('/').is_empty())
    };
    let configured = !kind.is_empty() && has_url;
    let path = uri.path().trim_start_matches("/api/ai/imagegen/");
    let body = match path {
        "status" if !configured => json!({"ok": false, "reason": "not configured"}),
        "models" if !configured => json!({"models": []}),
        "loras" if !configured || studio => json!({"loras": []}),
        "upscalers" if !configured || studio => json!({"upscalers": []}),
        "refiners" if !configured || studio => json!({"refiners": []}),
        "ipadapter-models" if !configured || studio => json!({"models": []}),
        "progress" if kind != "swarmui" => json!({"active": false, "percent": 0.0, "current_percent": 0.0, "preview": ""}),
        "updates" if kind != "swarmui" || !has_url => json!({"updates": {}}),
        "backends" if kind != "swarmui" || !has_url => json!({"backends": [], "resources": {}}),
        "samplers-schedulers" if !has_url => json!({"samplers": SAMPLERS.to_vec(), "schedulers": SCHEDULERS.to_vec()}),
        _ => return None,
    };
    crate::edits::json_response(&body)
}

const SAMPLERS: [&str; 33] = ["euler", "euler_ancestral", "euler_cfg_pp", "euler_ancestral_cfg_pp", "heun", "heunpp2", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm", "ddim", "lcm", "uni_pc", "uni_pc_bh2", "ipndm", "ipndm_v", "deis", "res_multistep", "res_multistep_cfg_pp", "sa_solver", "er_sde", "gradient_estimation", "restart"];
const SCHEDULERS: [&str; 11] = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta", "linear_quadratic", "kl_optimal", "ays", "gits"];

/// `GET /api/ai/imagegen/starred`: every starred picture (across worlds), newest first
fn imagegen_starred(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let mut out = vec![];
    for i in rows_of(&db, "SELECT * FROM starred_images ORDER BY created_at DESC", &[])? {
        let raw = i.get("params_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
        if raw.contains("NaN") || raw.contains("Infinity") || !crate::edits::plain_numbers(raw.as_bytes()) {
            return None;
        }
        let params: Value = serde_json::from_str(raw).ok()?;
        out.push(json!({"id": i.get("id"), "url": i.get("url"), "prompt": i.get("prompt"), "negative": i.get("negative"), "model": i.get("model"),
            "seed": i.get("seed"), "params": params, "created_at": dt_iso(&i, "created_at")?}));
    }
    crate::edits::json_response(&json!({"images": out}))
}

/// `GET /api/ai/context-info`: the chat's context-usage indicator - the configured (or assumed 4,096) window and the auto-sizing ceiling. GM only.
fn ai_context_info(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let configured: Option<i64> = db.query_row("SELECT ollama_num_ctx FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get(0)).ok().flatten();
    let baseline = configured.filter(|n| *n != 0).unwrap_or(4096);
    let max_auto = match std::env::var("MAX_AUTO_NUM_CTX") {
        Ok(v) => nd_render::text::py_strip(&v).parse::<i64>().ok()?.max(8192),
        Err(_) => 32768,
    };
    crate::edits::json_response(&json!({"baseline": baseline, "ceiling": baseline.max(max_auto)}))
}

/// `GET /api/ai/ping`: the SSE smoke test - five dummy tokens and [DONE], no back end touched (Python spaces them 50 ms apart; the bytes are the same)
fn ai_ping(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let mut body = String::new();
    for word in ["SSE", " ", "ping", " ", "OK"] {
        body.push_str(&format!("data: {{\"token\": \"{word}\"}}\n\n"));
    }
    body.push_str("data: [DONE]\n\n");
    Response::builder().status(axum::http::StatusCode::OK).header("content-type", "text/event-stream; charset=utf-8")
        .header("x-accel-buffering", "no").header("cache-control", "no-cache").header("x-nd-served-by", "gateway").body(Body::from(body)).ok()
}

/// `str.lower()` where it is certain: ASCII, and non-ASCII characters that are not upper case (they lower to themselves)
fn lower_safe(s: &str) -> Option<String> {
    if s.chars().any(|c| !c.is_ascii() && c.is_uppercase()) {
        return None;
    }
    Some(s.to_ascii_lowercase())
}

/// Python's `needle in hay` / `hay.find(needle)` over characters: the character index of the first match
fn find_chars(hay: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    if needle.len() > hay.len() {
        return None;
    }
    (0..=hay.len() - needle.len()).find(|&i| hay[i..i + needle.len()] == *needle)
}

/// `GET /api/orgs/graph` (app/routers/boards_generate.py `_build_faction_graph`): the faction relationship graph - organizations laid out in
/// clusters by subtype, an edge for each stored link and for each organization named in another's text, its kind read from the words around
/// the mention. The GM's full graph; what a player or assistant may see needs the visibility filter, which is Python's.
fn orgs_graph(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let wid = world_id_of(&p).unwrap_or(1);
    let orgs = rows_of(&db, "SELECT * FROM entities WHERE world_id = ?1 AND kind IN ('organization') ORDER BY subtype, name", &[&wid])?;
    if orgs.is_empty() {
        return crate::edits::json_response(&json!({"nodes": [], "edges": []}));
    }
    let text = |e: &Map<String, Value>, k: &str| -> Option<String> {
        match e.get(k) {
            None | Some(Value::Null) => Some(String::new()),
            Some(Value::String(s)) => Some(s.clone()),
            _ => None,
        }
    };
    // clusters by subtype (else kind), in the order they first appear
    let mut clusters: Vec<(String, Vec<&Map<String, Value>>)> = vec![];
    for e in &orgs {
        let sub = text(e, "subtype")?;
        let key = lower_safe(if sub.is_empty() { text(e, "kind")? } else { sub }.as_str().to_string().as_str()).or(None)?;
        let key = if key.is_empty() { "other".to_string() } else { key };
        match clusters.iter_mut().find(|c| c.0 == key) {
            Some(c) => c.1.push(e),
            None => clusters.push((key, vec![e])),
        }
    }
    let colors: [(&str, &str); 11] = [("megacorp", "#00f0ff"), ("corporation", "#00f0ff"), ("government", "#3b82f6"), ("syndicate", "#ff2d78"), ("cult", "#a78bfa"),
        ("religion", "#a78bfa"), ("culture", "#22c55e"), ("gang", "#f97316"), ("secret society", "#8b0000"), ("ai entity", "#e879f9"), ("family", "#fbbf24")];
    let (cx, cy, cluster_r) = (1400.0_f64, 900.0_f64, 700.0_f64);
    let mut node_ids: Vec<(i64, String)> = vec![];
    let mut nodes = vec![];
    let n_clusters = clusters.len();
    for (ci, (key, members)) in clusters.iter().enumerate() {
        let angle = (2.0 * std::f64::consts::PI * ci as f64 / n_clusters as f64) - std::f64::consts::PI / 2.0;
        let ccx = cx + cluster_r * angle.cos();
        let ccy = cy + cluster_r * angle.sin();
        let inner_r = ((30 * members.len()).min(180)).max(80) as f64;
        let color = colors.iter().find(|c| c.0 == key).map(|c| c.1).unwrap_or("#6b7280");
        for (ni, e) in members.iter().enumerate() {
            let nangle = 2.0 * std::f64::consts::PI * ni as f64 / members.len().max(1) as f64;
            let nx = ccx + inner_r * nangle.cos();
            let ny = ccy + inner_r * nangle.sin();
            let id = e.get("id")?.as_i64()?;
            let nid = format!("fn-{id}");
            node_ids.push((id, nid.clone()));
            let or_empty = |k: &str| -> Option<Value> { Some(if py_truthy_text(e.get(k)) { e.get(k).cloned()? } else { json!("") }) };
            nodes.push(json!({"id": nid, "type": "faction", "title": e.get("name"), "body": or_empty("summary")?, "color": color, "image_url": or_empty("image_url")?,
                "entity_id": id, "status": "", "tags": or_empty("tags")?, "x": nx.round_ties_even() as i64, "y": ny.round_ties_even() as i64}));
        }
    }
    let node_of = |id: i64| node_ids.iter().find(|n| n.0 == id).map(|n| n.1.clone());
    // stored links between organizations (table order)
    let in_list = node_ids.iter().map(|n| n.0.to_string()).collect::<Vec<_>>().join(",");
    let mut st = db.prepare(&format!("SELECT source_id, target_id FROM entity_links WHERE source_id IN ({in_list}) AND target_id IN ({in_list})")).ok()?;
    let links: Vec<(i64, i64)> = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut edges = vec![];
    let mut seen: std::collections::HashSet<(i64, i64)> = std::collections::HashSet::new();
    for (src, tgt) in links {
        let pair = (src.min(tgt), src.max(tgt));
        if !seen.insert(pair) {
            continue;
        }
        edges.push(json!({"id": format!("fe-{src}-{tgt}"), "from": node_of(src)?, "to": node_of(tgt)?, "label": "", "color": "#6b7280", "style": "solid", "kind": "link", "direction": "fwd"}));
    }
    // organizations named in another's text
    let relation_keywords: [(&str, &[&str]); 5] = [
        ("allies", &["allied", "allies", "alliance", "partner", "coalition", "cooperat", "friend"]),
        ("enemies", &["enemy", "enemies", "hostile", "war", "conflict", "fights", "against", "oppose", "oppos"]),
        ("controls", &["controls", "commands", "dominates", "puppet", "directs", "governs", "runs"]),
        ("subsidiary", &["subsidiary", "branch", "division", "arm of", "owned by", "sub-unit", "founded by"]),
        ("rivals", &["rival", "rivals", "competing", "competition", "compete", "contend", "contest"]),
    ];
    let edge_color = |k: &str| match k { "allies" => "#22c55e", "enemies" => "#ef4444", "controls" => "#00f0ff", "subsidiary" => "#a78bfa", "rivals" => "#f97316", _ => "#6b7280" };
    // {name.lower(): id} - a later organization with the same name takes the id, the key keeps its place
    let mut name_to_id: Vec<(String, i64)> = vec![];
    for e in &orgs {
        let name = lower_safe(&text(e, "name")?)?;
        let id = e.get("id")?.as_i64()?;
        match name_to_id.iter_mut().find(|n| n.0 == name) {
            Some(n) => n.1 = id,
            None => name_to_id.push((name, id)),
        }
    }
    for e in &orgs {
        let id = e.get("id")?.as_i64()?;
        let full = lower_safe(&format!("{} {}", text(e, "body")?, text(e, "summary")?))?;
        let hay: Vec<char> = full.chars().collect();
        for (other_name, other_id) in &name_to_id {
            let needle: Vec<char> = other_name.chars().collect();
            if *other_id == id {
                continue;
            }
            let Some(pos) = find_chars(&hay, &needle) else { continue };
            let pair = (id.min(*other_id), id.max(*other_id));
            if !seen.insert(pair) {
                continue;
            }
            let lo = pos.saturating_sub(120);
            let hi = (pos + needle.len() + 120).min(hay.len());
            let context: String = hay[lo..hi].iter().collect();
            let kind = relation_keywords.iter().find(|(_, kws)| kws.iter().any(|kw| context.contains(kw))).map(|(k, _)| *k).unwrap_or("neutral");
            edges.push(json!({"id": format!("fe-{id}-{other_id}"), "from": node_of(id)?, "to": node_of(*other_id)?,
                "label": if kind != "neutral" { kind } else { "" }, "color": edge_color(kind),
                "style": if kind == "subsidiary" || kind == "neutral" { "dashed" } else { "solid" }, "kind": kind, "direction": "fwd"}));
        }
    }
    crate::edits::json_response(&json!({"nodes": nodes, "edges": edges}))
}

/// Python truthiness of a text column (`x or ""`)
fn py_truthy_text(v: Option<&Value>) -> bool {
    matches!(v, Some(Value::String(s)) if !s.is_empty())
}

/// `GET /api/system/stats`: one live reading of CPU, memory and GPUs for the System Monitor. GM only.
fn system_stats(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let mut r = crate::edits::json_response(&crate::sysstats::snapshot())?;
    r.headers_mut().insert("cache-control", axum::http::HeaderValue::from_static("no-store"));
    Some(r)
}

/// The image prompt tag-completion routes (`/api/ai/imagegen/tags*`) on an install with no tag list downloaded: no `<source>_tags.csv`, no active source, no
/// custom sources - the two built-in sources not downloaded and nothing to complete. A downloaded list (parsed once and cached by Python) is Python's. GM only.
fn imagegen_tags_empty(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let dir = cfg.db_path.parent()?.join("tags");
    if dir.is_dir() {
        for entry in std::fs::read_dir(&dir).ok()? {
            let name = entry.ok()?.file_name().into_string().ok()?;
            if name.ends_with("_tags.csv") || name == "custom_sources.json" {
                return None;
            }
            if name == "active_source.txt" && !std::fs::read_to_string(dir.join(&name)).ok()?.trim().is_empty() {
                return None;
            }
        }
    }
    let body = match uri.path() {
        "/api/ai/imagegen/tags/sources" => json!({"sources": [
            {"id": "danbooru", "label": "Danbooru", "description": "General anime/art tags sorted by usage \u{b7} SDXL optimised \u{b7} ~4 MB", "builtin": true, "downloaded": false, "count": 0, "active": false},
            {"id": "e621", "label": "e621", "description": "Furry / anthro art tags from e621 \u{b7} ~3 MB", "builtin": true, "downloaded": false, "count": 0, "active": false}], "active": ""}),
        "/api/ai/imagegen/tags/status" => json!({"loaded": false, "count": 0, "active": ""}),
        _ => {
            // `q: str = "", limit: int = 25`: a limit that is not a whole number is FastAPI's 422
            if let Some(l) = crate::pages::query_param(uri, "limit") {
                if l.is_empty() || !l.bytes().all(|c| c.is_ascii_digit()) || l.len() > 9 {
                    return None;
                }
            }
            json!({"tags": []})
        }
    };
    crate::edits::json_response(&body)
}

/// `GET /api/perf/summary` and `/api/perf/recent`: the request timings Python keeps in memory. Without Python (`ND_PYTHON_FREE=1`) there are none to show -
/// the page's empty shape; next to Python they are Python's. GM only.
fn perf_empty(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    if !crate::config::python_free() {
        return None;
    }
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let slow = match std::env::var("ND_SLOW_MS") {
        Ok(v) if !v.trim().is_empty() => nd_render::text::py_strip(&v).parse::<f64>().ok()?,
        _ => 800.0,
    };
    let body = if uri.path().ends_with("/summary") { json!({"slow_ms": slow, "routes": []}) } else { json!({"requests": []}) };
    let mut r = crate::edits::json_response(&body)?;
    r.headers_mut().insert("cache-control", axum::http::HeaderValue::from_static("no-store"));
    Some(r)
}

/// `POST /api/system/gpu-power` {index, watts}: set a card's power limit inside its own min..max (GM only; a body that is not JSON / an object is the 400s)
fn gpu_power(cfg: &Config, uri: &Uri, headers: &HeaderMap, raw: &[u8]) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let bad = |m: &str| Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, m));
    if !crate::edits::plain_numbers(raw) {
        return None;
    }
    let body = match serde_json::from_slice::<Value>(raw) {
        Ok(Value::Object(o)) => o,
        Ok(_) => return bad("JSON object required"),
        Err(_) if std::str::from_utf8(raw).is_ok() && !raw.contains(&0) && !raw.windows(3).any(|w| w == b"NaN") && !raw.windows(8).any(|w| w == b"Infinity") => return bad("JSON body required"),
        Err(_) => return None, // json.loads reads UTF-16 / 32 and NaN: Python's
    };
    let (ok, result) = crate::sysstats::set_power_limit(body.get("index").unwrap_or(&Value::Null), body.get("watts").unwrap_or(&Value::Null));
    let mut r = crate::edits::json_response(&result)?;
    if !ok {
        *r.status_mut() = axum::http::StatusCode::BAD_REQUEST;
    }
    Some(r)
}

/// `GET /api/ai/imagegen/models/downloaded`: the model files on the SwarmUI volume. With no folder (or an empty one) the list is empty; a folder with files
/// is walked in the file system's order, which is Python's. GM only.
fn imagegen_downloaded(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return None;
    }
    let dir = std::path::PathBuf::from(std::env::var("SWARMUI_MODELS_DIR").unwrap_or_else(|_| "/data/swarmui-models".into()));
    if dir.is_dir() && std::fs::read_dir(&dir).ok()?.next().is_some() {
        return None;
    }
    crate::edits::json_response(&json!({"models": [], "folder_suggestions": ["", "LoRA", "VAE", "clip", "diffusion_models", "ControlNet", "Upscale", "IPAdapter", "Refiner", "Embedding"]}))
}

/// `GET /api/quests/board`: the active world's top-level active quests for the cockpit (GM; 400 without a world)
fn quests_board(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return Some(crate::idpages::refusal(403));
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    let names: Map<String, Value> = rows_of(&db, "SELECT id, name FROM parties WHERE world_id = ?1", &[&wid])?.into_iter()
        .filter_map(|r| Some((r.get("id")?.as_i64()?.to_string(), r.get("name").cloned().unwrap_or(Value::Null)))).collect();
    let mut out = vec![];
    for q in rows_of(&db, "SELECT * FROM quests WHERE world_id = ?1 AND status = 'active' AND parent_id IS NULL ORDER BY category, title LIMIT 12", &[&wid])? {
        let party = q.get("assigned_party_id").and_then(|v| v.as_i64());
        out.push(json!({"id": q.get("id"), "title": q.get("title"),
            "category": if crate::edits::py_truthy_pub(q.get("category")) { q.get("category").cloned().unwrap_or(Value::Null) } else { json!("main") },
            "summary": if crate::edits::py_truthy_pub(q.get("summary")) { q.get("summary").cloned().unwrap_or(Value::Null) } else { json!("") },
            "party_id": q.get("assigned_party_id"), "party": party.and_then(|id| names.get(&id.to_string()).cloned()).unwrap_or(Value::Null)}));
    }
    crate::edits::json_response(&json!({"quests": out}))
}

/// `GET /api/combat/recent`: the ten newest combats for the cockpit picker (400 without a world)
fn combat_recent(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(wid) = world_id_of(&p) else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    let mut out = vec![];
    for cs in rows_of(&db, "SELECT * FROM combat_sessions WHERE world_id = ?1 AND is_template IS NOT 1 ORDER BY (finished_at IS NOT NULL), created_at DESC, id DESC LIMIT 10", &[&wid])? {
        let round = match cs.get("round_num") { v if crate::edits::py_truthy_pub(v) => v.cloned().unwrap_or(json!(1)), _ => json!(1) };
        out.push(json!({"id": cs.get("id"), "name": cs.get("name"), "round_num": round, "running": cs.get("finished_at").map_or(true, |v| v.is_null())}));
    }
    crate::edits::json_response(&json!({"combats": out}))
}

/// `GET /api/audio/clips`: every clip of the active world by name (a GM or assistant; 404 without a world)
fn audio_clips(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return Some(crate::idpages::refusal(403));
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let mut out = vec![];
    for c in rows_of(&db, "SELECT * FROM audio_clips WHERE world_id = ?1 ORDER BY name", &[&wid])? {
        out.push(json!({"id": c.get("id"), "name": c.get("name"), "description": if crate::edits::py_truthy_pub(c.get("description")) { c.get("description").cloned().unwrap_or(Value::Null) } else { json!("") }, "album_id": c.get("album_id")}));
    }
    crate::edits::json_response(&Value::Array(out))
}

/// `GET /api/world-players`: the non-GM members of the active world (GM; 404 without a world)
fn world_players(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !p.is_gm {
        return Some(crate::idpages::refusal(403));
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let mut out = vec![];
    for u in rows_of(&db, "SELECT users.id, users.display_name, users.is_gm FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 ORDER BY users.display_name", &[&wid])? {
        if !crate::edits::py_truthy_pub(u.get("is_gm")) {
            out.push(json!({"id": u.get("id"), "name": u.get("display_name")}));
        }
    }
    crate::edits::json_response(&Value::Array(out))
}

/// `GET /api/tables/options`: the world's (and the global) roll tables with entry counts (400 without a world)
fn tables_options(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(wid) = world_id_of(&p) else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    let mut out = vec![];
    for t in rows_of(&db, "SELECT * FROM random_tables WHERE world_id = ?1 OR world_id IS NULL ORDER BY name LIMIT 60", &[&wid])? {
        let raw = t.get("entries_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
        let n = match serde_json::from_str::<Value>(raw) {
            Ok(Value::Array(a)) => a.len(),
            Ok(Value::Object(o)) => o.len(),
            Ok(Value::String(s)) => s.chars().count(),
            Ok(_) => return None,
            Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
            Err(_) => 0,
        };
        out.push(json!({"id": t.get("id"), "name": t.get("name"), "entries": n}));
    }
    crate::edits::json_response(&json!({"tables": out}))
}

/// `GET /api/video-jobs`: the world's twenty newest video jobs (a GM or assistant; 404 without a world)
fn video_jobs_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return Some(crate::idpages::refusal(403));
    }
    let Some(wid) = world_id_of(&p) else { return Some(crate::idpages::refusal(404)) };
    let mut out = vec![];
    for j in rows_of(&db, "SELECT * FROM video_jobs WHERE world_id = ?1 ORDER BY id DESC LIMIT 20", &[&wid])? {
        let txt = |k: &str| if crate::edits::py_truthy_pub(j.get(k)) { j.get(k).cloned().unwrap_or(Value::Null) } else { json!("") };
        let mut d = json!({"id": j.get("id"), "prompt": txt("prompt"),
            "status": if crate::edits::py_truthy_pub(j.get("status")) { j.get("status").cloned().unwrap_or(Value::Null) } else { json!("pending") },
            "error": txt("error"), "studio_video_id": txt("studio_video_id"), "clip_id": j.get("clip_id"), "created_at": dt_iso(&j, "created_at")?});
        if let Some(cid) = j.get("clip_id").and_then(|v| v.as_i64()).filter(|c| *c != 0) {
            if let Some(clip) = rows_of(&db, "SELECT file_url, name FROM video_clips WHERE id = ?1", &[&cid])?.into_iter().next() {
                let o = d.as_object_mut()?;
                o.insert("clip_url".into(), clip.get("file_url").cloned().unwrap_or(Value::Null));
                o.insert("clip_name".into(), clip.get("name").cloned().unwrap_or(Value::Null));
            }
        }
        out.push(d);
    }
    crate::edits::json_response(&json!({"jobs": out}))
}

/// `GET /api/characters/catalog`: the creation wizard's catalog, as FastAPI writes `catalog_payload()` (kept as text in app_data.json)
fn characters_catalog(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, _p) = page(cfg, uri, headers)?;
    crate::edits::json_response_raw(nd_render::app::data().get("catalog_payload_json")?.as_str()?, "application/json")
}

/// `GET /api/characters/templates`: the sheet templates the active world can use (its own and the global ones), built-ins first
fn characters_templates(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let rows = match world_id_of(&p) {
        Some(w) => rows_of(&db, "SELECT * FROM sheet_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name", &[&w])?,
        None => rows_of(&db, "SELECT * FROM sheet_templates WHERE world_id IS NULL ORDER BY is_builtin DESC, name", &[])?,
    };
    let mut out = vec![];
    for t in rows {
        let loads = |raw: Option<&Value>, default: &str| -> Option<Value> {
            let text = raw.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(default);
            if text.contains("NaN") || text.contains("Infinity") || !crate::edits::plain_numbers(text.as_bytes()) {
                return None;
            }
            serde_json::from_str(text).ok()
        };
        let fields = loads(t.get("fields_json"), "[]")?;
        // system_spec_public: the template's own stored spec, {} when absent / unreadable / not an object
        let system = match t.get("system_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            None => json!({}),
            Some(raw) => match crate::map_layout::py_loads(raw) { Ok(Value::Object(o)) => Value::Object(o), _ => json!({}) },
        };
        let builtin = crate::edits::py_truthy_pub(t.get("is_builtin"));
        let digest = builtin.then(|| t.get("slug").and_then(|v| v.as_str())).flatten().filter(|s| !s.contains('/') && !s.contains('\\') && !s.contains(".."))
            .and_then(|s| nd_render::app::data()["system_digests"].get(s).and_then(|v| v.as_str())).map_or(false, |d| !d.is_empty());
        let own = !nd_render::text::py_strip(t.get("rules_md").and_then(|v| v.as_str()).unwrap_or("")).is_empty();
        out.push(json!({"id": t.get("id"), "name": t.get("name"), "is_builtin": match t.get("is_builtin") { Some(Value::Null) | None => Value::Null, _ => json!(builtin) }, "sheet_mode": t.get("sheet_mode"), "fields": fields, "system": system, "has_rules": digest || own}));
    }
    crate::edits::json_response(&Value::Array(out))
}

/// `GET /api/maps/props`: the world's prop library (the maps edit tier; 404 without a world)
fn map_props_list(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(w) = p.world.as_ref() else { return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, "No world selected")) };
    if p.user_id <= 0 || !crate::pages::edit_level(&p, w, "maps")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = w.get("id")?.as_i64()?;
    let mut props = vec![];
    for r in rows_of(&db, "SELECT * FROM map_props WHERE world_id = ?1 ORDER BY name, id", &[&wid])? {
        props.push(json!({"id": r.get("id"), "name": r.get("name"), "tags": if crate::edits::py_truthy_pub(r.get("tags")) { r.get("tags").cloned().unwrap_or(Value::Null) } else { json!("") },
            "url": r.get("file_url"), "kind": r.get("kind"), "px_w": r.get("px_w"), "px_h": r.get("px_h"), "cells_w": r.get("cells_w"), "cells_h": r.get("cells_h")}));
    }
    crate::edits::json_response(&json!({"props": props, "limit": 500}))
}

/// `GET /api/facts/last-parse`: the newest finished facts-parse draft that was not yet saved (400 without a world, 404 when there is none)
fn facts_last_parse(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let Some(wid) = world_id_of(&p) else { return Some(crate::ai_more::detail(axum::http::StatusCode::BAD_REQUEST, "No active world")) };
    let Some(job) = rows_of(&db, "SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose = 'facts_parse' AND status = 'done' AND draft_consumed IS NOT 1 ORDER BY created_at DESC LIMIT 1", &[&wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let raw = job.get("result_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    if raw.contains("NaN") || raw.contains("Infinity") || !crate::edits::plain_numbers(raw.as_bytes()) {
        return None;
    }
    let facts: Value = serde_json::from_str(raw).unwrap_or_else(|_| json!([]));
    crate::edits::json_response(&json!({"job_id": job.get("id"), "created_at": dt_iso(&job, "created_at")?, "game_session_id": job.get("game_session_id"), "facts": facts}))
}

/// `GET /tables/export`: the global tables and the world's own as a JSON list
fn tables_export(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let rows = match world_id_of(&p) {
        Some(w) => rows_of(&db, "SELECT * FROM random_tables WHERE world_id IS NULL OR world_id = ?1", &[&w])?,
        None => rows_of(&db, "SELECT * FROM random_tables WHERE world_id IS NULL", &[])?,
    };
    let mut out = vec![];
    for t in rows {
        let raw = t.get("entries_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
        if raw.contains("NaN") || raw.contains("Infinity") || !crate::edits::plain_numbers(raw.as_bytes()) {
            return None;
        }
        let entries: Value = serde_json::from_str(raw).ok()?;
        out.push(json!({"name": t.get("name"), "category": t.get("category"), "description": t.get("description"), "entries": entries}));
    }
    crate::edits::json_response(&Value::Array(out))
}

/// `GET /api/ai/imagegen/model-templates` (app/routers/ai.py): the exported built-ins plus this
/// world's custom PromptPresets (scope='image_template') in the same shape, label-ordered
fn imagegen_model_templates(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, p) = page(cfg, uri, headers)?;
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let mut custom = vec![];
    if let Some(wid) = wid {
        let rows = rows_of(&db, "SELECT * FROM prompt_presets WHERE scope = 'image_template' AND world_id = ?1 ORDER BY label", &[&wid])?;
        for preset in rows {
            let params: Map<String, Value> = match preset.get("params_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()) {
                Some(t) => serde_json::from_str(t).unwrap_or_default(),
                None => Map::new(),
            };
            let get = |k: &str| params.get(k).cloned().unwrap_or(Value::Null);
            let falsy_or_absent = |k: &str| !params.get(k).map(|v| crate::edits::py_truthy_pub(Some(v))).unwrap_or(false);
            custom.push(json!({
                "id": format!("custom-{}", preset.get("id").and_then(|v| v.as_i64()).unwrap_or(0)),
                "label": preset.get("label").cloned().unwrap_or(Value::Null),
                "match": if falsy_or_absent("match") { json!([]) } else { get("match") },
                "exclude": if falsy_or_absent("exclude") { json!([]) } else { get("exclude") },
                "prefix": if falsy_or_absent("prefix") { json!("") } else { get("prefix") },
                "suffix": if falsy_or_absent("suffix") { json!("") } else { get("suffix") },
                "example_prompt": preset.get("text").cloned().unwrap_or(json!("")),
                // preset.negative or "" — the preset's own column, like text
                "negative": if preset.get("negative").map(|v| crate::edits::py_truthy_pub(Some(v))).unwrap_or(false) {
                    preset.get("negative").cloned().unwrap_or(json!(""))
                } else {
                    json!("")
                },
                "steps": get("steps"),
                "cfg": get("cfg"),
                "sampler": if falsy_or_absent("sampler") { json!("") } else { get("sampler") },
                "scheduler": if falsy_or_absent("scheduler") { json!("") } else { get("scheduler") },
                "note": if falsy_or_absent("note") { json!("Your template.") } else { get("note") },
                "guide": if falsy_or_absent("guide") { json!("") } else { get("guide") },
                "custom": true,
            }));
        }
    }
    crate::edits::json_response(&json!({
        "templates": nd_render::app::data()["imagegen_templates"].clone(),
        "custom": custom,
    }))
}

/// `_json_download`: json.dumps(payload, ensure_ascii=False, indent=2) as an attachment
fn json_download(payload: &Value, filename: &str) -> Response<Body> {
    Response::builder().header(axum::http::header::CONTENT_TYPE, "application/json")
        .header(axum::http::header::CONTENT_DISPOSITION, format!("attachment; filename=\"{filename}\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_indent_as(payload, 2, false))).unwrap()
}

/// `GET /worlds/{world_id}/export/rules.json` (app/routers/export.py `export_rules`): the world's
/// rules as {name, rules_md}; a world with no custom rules is Python's 404 with its message.
fn export_rules_json(cfg: &Config, uri: &Uri, headers: &HeaderMap, wid: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let Some(w) = rows_of(&db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let rules_md = w.get("rules_md").and_then(|v| v.as_str()).unwrap_or("");
    if rules_md.trim().is_empty() {
        return Some(crate::ai_more::detail(axum::http::StatusCode::NOT_FOUND, "This world has no custom rules set"));
    }
    let wname = w.get("name").and_then(|v| v.as_str()).unwrap_or("World");
    let wslug = w.get("slug").and_then(|v| v.as_str()).unwrap_or("world");
    Some(json_download(&json!({"name": format!("{wname} Rules"), "rules_md": rules_md}), &format!("{wslug}-rules.json")))
}

/// `GET /worlds/{world_id}/export/player-characters.json`: every pc of the world, name-ordered, as
/// `_pc_to_export_dict` builds them — the scalar columns, then each JSON field json.loads'd (its
/// raw text when loads raises; falsy -> [] or {} for custom_fields).
fn export_pcs_json(cfg: &Config, uri: &Uri, headers: &HeaderMap, wid: i64) -> Option<Response<Body>> {
    use crate::importer::{PC_JSON_ALIASES, PC_SCALAR_FIELDS};
    let db = crate::writes::open(cfg)?;
    let Some(w) = rows_of(&db, "SELECT * FROM worlds WHERE id = ?1", &[&wid])?.into_iter().next() else {
        return Some(crate::idpages::refusal(404));
    };
    let rows = rows_of(&db, "SELECT * FROM player_characters WHERE world_id = ?1 ORDER BY name", &[&wid])?;
    let mut out = vec![];
    for pc in &rows {
        let mut d = serde_json::Map::new();
        d.insert("name".into(), pc.get("name").cloned().unwrap_or(Value::Null));
        for field in PC_SCALAR_FIELDS {
            d.insert(field.to_string(), pc.get(field).cloned().unwrap_or(Value::Null));
        }
        for (alias, json_field) in PC_JSON_ALIASES {
            let raw = pc.get(json_field).and_then(|v| v.as_str()).filter(|t| !t.is_empty());
            let parsed = match raw {
                None => Some(if *json_field != *"custom_fields_json" { json!([]) } else { json!({}) }),
                Some(t) => serde_json::from_str::<Value>(t).ok(),
            };
            match parsed {
                Some(v) => d.insert(alias.to_string(), v),
                None => d.insert(alias.to_string(), pc.get(json_field).cloned().unwrap_or(Value::Null)),
            };
        }
        out.push(Value::Object(d));
    }
    let wslug = w.get("slug").and_then(|v| v.as_str()).unwrap_or("world");
    Some(json_download(&Value::Array(out), &format!("{wslug}-player-characters.json")))
}
