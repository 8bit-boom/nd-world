//! `GET /account` (app/routers/account.py `account_page` / `_render`): the person's name, password, two-step and API-token panels, and
//! their site layout panel. `_render`'s other answers (a wrong code, new backup codes) reuse it through `render`.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::Pages;

/// `_render(request, db, user, active_world, **extra)` with the status it answers; None when the page context is Python's
pub fn render(pages: &Pages, cfg: &Config, db: &Connection, uri: &Uri, headers: &HeaderMap, extra: Map<String, Value>, status: StatusCode)
    -> Option<Response<Body>> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(db, cfg, "GET", uri, headers, now)?;
    let rows = |sql: &str, arg: &dyn rusqlite::ToSql| -> Option<Vec<Value>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([arg], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r.into_iter().map(Value::Object).collect())
    };
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [p.user_id], |r| crate::layout::row_map(r)).ok()?;
    let tokens = rows("SELECT * FROM api_tokens WHERE user_id = ?1 ORDER BY created_at DESC", &p.user_id)?;
    let stamp = crate::orm::now_stamp();
    let mut st = db.prepare("SELECT * FROM trusted_devices WHERE user_id = ?1 AND expires_at > ?2 ORDER BY created_at DESC").ok()?;
    let devices = st.query_map(rusqlite::params![p.user_id, stamp], |r| crate::layout::row_map(r)).ok()?
        .collect::<Result<Vec<_>, _>>().ok()?;
    for (k, v) in [("name_error", Value::Null), ("password_error", Value::Null), ("new_token", Value::Null), ("totp_error", Value::Null),
                   ("new_backup_codes", Value::Null)] {
        p.ctx.insert(k.into(), v);
    }
    p.ctx.insert("user".into(), Value::Object(user));
    p.ctx.insert("tokens".into(), Value::Array(tokens));
    p.ctx.insert("trusted_devices".into(), json!(devices));
    for (k, v) in extra {
        p.ctx.insert(k, v);
    }
    let html = pages.env.get_template("account.html").ok()?.render(nd_render::pyvalue::to_value(&Value::Object(p.ctx)));
    let html = match html {
        Ok(h) => h,
        Err(e) => {
            eprintln!("nd-gateway: account.html: {e:#}");
            return None;
        }
    };
    let mut r = crate::pages::html(status, html, headers);
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    Some(r)
}

/// `GET /account`
pub fn page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    render(pages, cfg, &db, uri, headers, Map::new(), StatusCode::OK)
}
