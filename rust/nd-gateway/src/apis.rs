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
        path => party_vitals(&cfg, uri, headers, vitals_id(path)?),
    }
}

pub fn is_get(path: &str) -> bool {
    matches!(path, "/api/cockpit/workspace") || vitals_id(path).is_some()
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
