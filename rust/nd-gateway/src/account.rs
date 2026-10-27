//! `GET /account` (app/routers/account.py `account_page` / `_render`): the person's name, password, two-step and API-token panels, and
//! their site layout panel. `_render`'s other answers (a wrong code, new backup codes) reuse it through `render`.
//! `POST /account/password` too: the gateway keeps its own failed-attempt counter (Python's lives in its memory and
//! moves at the switch, with this one replacing it).

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::Pages;

/// `_MAX_ATTEMPTS` / `_LOCKOUT_SECONDS` (app/routers/account.py): only a wrong CURRENT password counts
const RL_MAX_ATTEMPTS: u32 = 8;
const RL_LOCKOUT_SECS: u64 = 300;

/// `_failed_password_changes`: user_id -> (count, window start)
fn rl_map() -> &'static Mutex<HashMap<i64, (u32, Instant)>> {
    static RL: std::sync::OnceLock<Mutex<HashMap<i64, (u32, Instant)>>> = std::sync::OnceLock::new();
    RL.get_or_init(|| Mutex::new(HashMap::new()))
}

/// `_rl_lock_remaining`
fn rl_lock_remaining(uid: i64) -> u64 {
    let mut m = rl_map().lock().unwrap();
    let Some((count, started)) = m.get(&uid).copied() else { return 0 };
    if count < RL_MAX_ATTEMPTS {
        return 0;
    }
    let elapsed = started.elapsed().as_secs();
    if elapsed >= RL_LOCKOUT_SECS {
        m.remove(&uid);
        return 0;
    }
    RL_LOCKOUT_SECS - elapsed
}

/// `_rl_record_failure`
fn rl_record_failure(uid: i64) {
    let mut m = rl_map().lock().unwrap();
    let (mut count, started) = m.get(&uid).copied().unwrap_or((0, Instant::now()));
    if started.elapsed().as_secs() >= RL_LOCKOUT_SECS {
        count = 0;
    }
    m.insert(uid, (count + 1, if count == 0 { Instant::now() } else { started }));
}

/// `_rl_clear`
fn rl_clear(uid: i64) {
    rl_map().lock().unwrap().remove(&uid);
}

/// `auth.hash_password`: "{salt_hex}${digest_hex}", a fresh random 16-byte salt
pub fn hash_password_pub(password: &str) -> String {
    hash_password(password)
}

fn hash_password(password: &str) -> String {
    use rand::Rng;
    let salt: [u8; 16] = rand::random();
    let digest = crate::auth::pbkdf2_sha256_pub(password.as_bytes(), &salt, crate::auth::iterations_pub());
    format!("{}${}", crate::auth::hex_pub(&salt), crate::auth::hex_pub(&digest))
}

/// `_render(request, db, user, active_world, **extra)` with the status it answers; None when the page context is Python's
pub fn render(pages: &Pages, cfg: &Config, db: &Connection, method: &str, uri: &Uri, headers: &HeaderMap, extra: Map<String, Value>, status: StatusCode)
    -> Option<Response<Body>> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(db, cfg, method, uri, headers, now)?;
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
        if k == "__base_url" {
            // request.base_url (the new token's MCP config)
            if let Some(Value::Object(req)) = p.ctx.get_mut("request") {
                req.insert("base_url".into(), v);
            }
            continue;
        }
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
    render(pages, cfg, &db, "GET", uri, headers, Map::new(), StatusCode::OK)
}

/// `POST /account/password` (app/routers/account.py `account_change_password`): the lockout (the gateway's own
/// counter now), the current-password check, the length and confirm checks, and on success the hash swap, the
/// session_version bump, every trusted device revoked and THIS session re-stamped (auth.rs resign). An error re-renders
/// the account page with Python's message and status; None (an unreadable form etc.) forwards.
pub fn password_change(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, form: &crate::form::Form) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let uid = p.user_id;
    let current = form.get("current_password").unwrap_or("");
    let new = form.get("new_password").unwrap_or("");
    let confirm = form.get("confirm_password").unwrap_or("");
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [uid], |r| crate::layout::row_map(r)).ok()?;
    let stored = user.get("password_hash").and_then(|v| v.as_str()).unwrap_or("").to_string();
    const MIN_LEN: usize = 8; // auth.MIN_PASSWORD_LENGTH
    if rl_lock_remaining(uid) > 0 {
        let locked_for = rl_lock_remaining(uid);
        let mut extra = Map::new();
        extra.insert("status_code".into(), json!(429));
        extra.insert("password_error".into(), json!(format!("Too many failed attempts. Try again in {} minute(s).", locked_for / 60 + 1)));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::TOO_MANY_REQUESTS);
    }
    if !crate::auth::verify_password(current, &stored) {
        rl_record_failure(uid);
        let mut extra = Map::new();
        extra.insert("password_error".into(), json!("Current password is incorrect."));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::BAD_REQUEST);
    }
    if new.chars().count() < MIN_LEN {
        let mut extra = Map::new();
        extra.insert("password_error".into(), json!(format!("New password must be at least {MIN_LEN} characters.")));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::BAD_REQUEST);
    }
    if new != confirm {
        let mut extra = Map::new();
        extra.insert("password_error".into(), json!("New passwords don't match."));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::BAD_REQUEST);
    }
    rl_clear(uid);
    let new_version = user.get("session_version").and_then(|v| v.as_i64()).unwrap_or(1) + 1;
    crate::orm::update(&db, "users", uid, &[
        ("password_hash", rusqlite::types::Value::Text(hash_password(new))),
        ("session_version", rusqlite::types::Value::Integer(new_version)),
    ])?;
    db.execute("DELETE FROM trusted_devices WHERE user_id = ?1", [uid]).ok()?;
    // re-stamp THIS session so the password change doesn't log itself out
    let line = crate::auth::resign(cfg, headers, move |sess| {
        sess.insert("session_version".into(), json!(new_version));
    })?;
    let mut r = crate::edits::redirect("/account?saved=password")?;
    if let Ok(v) = line.parse() {
        r.headers_mut().append(header::SET_COOKIE, v);
    }
    Some(r)
}

/// The async entry: parse the form, run the change, forward when the form itself is unreadable
pub async fn password_change_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let form = crate::form::Form::parse(&body)?;
        let answer = password_change(&pages, &cfg, &uri, &headers, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}

/// `POST /account/2fa/disable` (app/routers/account.py `account_2fa_disable`): the same counter, the password verified,
/// then 2FA off - the secret, the backup codes and every trusted device gone (re-enabling later starts from a clean
/// slate). A redirect when 2FA is not on; None (an unreadable form) forwards.
pub fn twofa_disable(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, form: &crate::form::Form) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let uid = p.user_id;
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [uid], |r| crate::layout::row_map(r)).ok()?;
    let stored = user.get("password_hash").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let current = form.get("current_password").unwrap_or("");
    if crate::edits::py_truthy_pub(user.get("totp_enabled")) {
        if rl_lock_remaining(uid) > 0 {
            let locked_for = rl_lock_remaining(uid);
            let mut extra = Map::new();
            extra.insert("status_code".into(), json!(429));
            extra.insert("totp_error".into(), json!(format!("Too many failed attempts. Try again in {} minute(s).", locked_for / 60 + 1)));
            return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::TOO_MANY_REQUESTS);
        }
        if !crate::auth::verify_password(current, &stored) {
            rl_record_failure(uid);
            let mut extra = Map::new();
            extra.insert("totp_error".into(), json!("Current password is incorrect."));
            return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::BAD_REQUEST);
        }
        rl_clear(uid);
        crate::orm::update(&db, "users", uid, &[
            ("totp_enabled", rusqlite::types::Value::Integer(0)),
            ("totp_secret", rusqlite::types::Value::Null),
            ("totp_backup_codes_json", rusqlite::types::Value::Text("[]".into())),
        ])?;
        db.execute("DELETE FROM trusted_devices WHERE user_id = ?1", [uid]).ok()?;
    }
    Some(crate::edits::redirect("/account?saved=2fa-off")?)
}

/// `POST /account/2fa/backup-codes/regenerate` (app/routers/account.py `account_2fa_regen_backup_codes`): 8 fresh codes,
/// only their sha256 hashes stored, the plaintext shown once on the re-rendered account page
pub fn twofa_regen(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, form: &crate::form::Form) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    let uid = p.user_id;
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [uid], |r| crate::layout::row_map(r)).ok()?;
    if !crate::edits::py_truthy_pub(user.get("totp_enabled")) {
        return Some(crate::edits::redirect("/account")?);
    }
    let stored = user.get("password_hash").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let current = form.get("current_password").unwrap_or("");
    if rl_lock_remaining(uid) > 0 {
        let locked_for = rl_lock_remaining(uid);
        let mut extra = Map::new();
        extra.insert("status_code".into(), json!(429));
        extra.insert("totp_error".into(), json!(format!("Too many failed attempts. Try again in {} minute(s).", locked_for / 60 + 1)));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::TOO_MANY_REQUESTS);
    }
    if !crate::auth::verify_password(current, &stored) {
        rl_record_failure(uid);
        let mut extra = Map::new();
        extra.insert("totp_error".into(), json!("Current password is incorrect."));
        return render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::BAD_REQUEST);
    }
    rl_clear(uid);
    use rand::Rng;
    let mut codes = vec![];
    let mut hashes = vec![];
    for _ in 0..8 {
        let code: String = (0..10).map(|_| format!("{:x}", rand::thread_rng().gen_range(0..16))).collect();
        hashes.push(json!(crate::auth::sha256_hex(&code))); // hash_backup_code: sha256 of the stripped, lowered code (hex is both)
        codes.push(json!(code));
    }
    crate::orm::update(&db, "users", uid, &[("totp_backup_codes_json", rusqlite::types::Value::Text(nd_render::py_dumps(&Value::Array(hashes))))])?;
    let mut extra = Map::new();
    extra.insert("new_backup_codes".into(), Value::Array(codes));
    render(pages, cfg, &db, "POST", uri, headers, extra, StatusCode::OK)
}

/// The async entries (parse the form, run, forward when the form itself is unreadable)
pub async fn twofa_disable_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let form = crate::form::Form::parse(&body)?;
        let answer = twofa_disable(&pages, &cfg, &uri, &headers, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}

pub async fn twofa_regen_srv(cfg: std::sync::Arc<Config>, pages: std::sync::Arc<Pages>, uri: Uri, headers: HeaderMap, body: bytes::Bytes)
    -> Option<Response<Body>> {
    let parsed = tokio::task::spawn_blocking(move || {
        let form = crate::form::Form::parse(&body)?;
        let answer = twofa_regen(&pages, &cfg, &uri, &headers, &form);
        Some((form, answer))
    }).await.ok().flatten();
    match parsed {
        Some((_, Some(resp))) => Some(resp),
        _ => None,
    }
}
