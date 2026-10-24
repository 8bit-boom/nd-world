//! Small JSON reads that were Python's only (phase 6, docs/RUST_SWITCH_OFF.md), each answered the way its FastAPI route answers -
//! the auth gate in front (crate::auth::gate) has already refused anyone the route's own middleware would.

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::Config;

/// GET routes served here: Some(answer), or None for Python
pub fn get(cfg: Arc<Config>, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    match uri.path() {
        "/api/cockpit/workspace" => cockpit_workspace(&cfg, uri, headers),
        "/api/calendar/search" => crate::calendar::search_api(&cfg, uri, headers),
        path if audio_job_id(path).is_some() => audio_job_status(&cfg, uri, headers, audio_job_id(path)?),
        path => party_vitals(&cfg, uri, headers, vitals_id(path)?),
    }
}

pub fn is_get(path: &str) -> bool {
    matches!(path, "/api/cockpit/workspace" | "/api/calendar/search") || vitals_id(path).is_some() || audio_job_id(path).is_some()
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
    crate::edits::json_response(&out)
}
