//! Signing in and out (app/routers/auth.py) answered by the gateway (`auth` in ND_RUST_ROUTES): `POST /login` and `POST /api/login`
//! (PBKDF2-SHA256 passwords, the failed-attempt lockout per address and email, the same timing for an unknown email), two-step codes
//! (`GET` / `POST /login/2fa`: TOTP with one step of drift, single-use backup codes, "trust this device" for 30 days), and
//! `GET /logout`. The session cookie is written exactly as app/session_cookie.py writes it (same signing, flags, and contents in the
//! same order), so Python reads the gateway's sessions and the other way round. Redeeming an invite during sign-in stays Python's.

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use hmac::{Hmac, Mac};
use percent_encoding::utf8_percent_encode;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use crate::config::Config;
use crate::pages::{safe_next_url, Pages, LOCATION_SAFE};

const PBKDF2_ITERATIONS: u32 = 600_000;

/// The PBKDF2 work factor: 600,000, or ND_PBKDF2_ITERATIONS - only for the test servers, which lower it as tests/serve.py lowers
/// Python's (the stored hash does not record its own count)
fn iterations() -> u32 {
    static N: OnceLock<u32> = OnceLock::new();
    *N.get_or_init(|| std::env::var("ND_PBKDF2_ITERATIONS").ok().and_then(|v| v.parse().ok()).filter(|n| *n > 0).unwrap_or(PBKDF2_ITERATIONS))
}
const MAX_ATTEMPTS: u32 = 8;
const LOCKOUT_SECONDS: u64 = 300;
const TRUST_DAYS: i64 = 30;
const SESSION_MAX_AGE: u64 = 14 * 24 * 3600;

// ── passwords ────────────────────────────────────────────────────────────────

fn pbkdf2_sha256(password: &[u8], salt: &[u8], iterations: u32) -> [u8; 32] {
    let prf = Hmac::<Sha256>::new_from_slice(password).expect("hmac key");
    let mut u = { let mut m = prf.clone(); m.update(salt); m.update(&1u32.to_be_bytes()); m.finalize().into_bytes() };
    let mut out: [u8; 32] = u.into();
    for _ in 1..iterations {
        let mut m = prf.clone();
        m.update(&u);
        u = m.finalize().into_bytes();
        for (o, b) in out.iter_mut().zip(u.iter()) {
            *o ^= b;
        }
    }
    out
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(t: &str) -> Option<Vec<u8>> {
    if t.len() % 2 != 0 {
        return None;
    }
    (0..t.len()).step_by(2).map(|i| u8::from_str_radix(t.get(i..i + 2)?, 16).ok()).collect()
}

/// `auth.verify_password`: "salt$hexdigest"; anything unreadable is a mismatch
pub fn verify_password(password: &str, stored: &str) -> bool {
    let Some((salt, digest)) = stored.split_once('$') else { return false };
    let Some(salt) = unhex(salt) else { return false };
    let got = hex(&pbkdf2_sha256(password.as_bytes(), &salt, iterations()));
    got.len() == digest.len() && got.bytes().zip(digest.bytes()).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0
}

/// `auth.burn_password_verify`: the same work for an unknown email
fn burn() {
    let _ = pbkdf2_sha256(b"x", &[0u8; 16], iterations());
}

pub fn sha256_hex(t: &str) -> String {
    hex(&Sha256::digest(t.as_bytes()))
}

// ── two-step codes (app/totp.py) ─────────────────────────────────────────────

fn base32_decode(secret: &str) -> Option<Vec<u8>> {
    let mut bits: u64 = 0;
    let mut n = 0;
    let mut out = vec![];
    for c in secret.trim_end_matches('=').chars() {
        let v = match c.to_ascii_uppercase() {
            c @ 'A'..='Z' => c as u64 - 'A' as u64,
            c @ '2'..='7' => c as u64 - '2' as u64 + 26,
            _ => return None,
        };
        bits = (bits << 5) | v;
        n += 5;
        if n >= 8 {
            n -= 8;
            out.push((bits >> n) as u8);
            bits &= (1 << n) - 1;
        }
    }
    Some(out)
}

fn totp_at(key: &[u8], counter: u64) -> String {
    let mut m = Hmac::<sha1::Sha1>::new_from_slice(key).expect("hmac key");
    m.update(&counter.to_be_bytes());
    let h = m.finalize().into_bytes();
    let o = (h[19] & 0x0f) as usize;
    let code = (u32::from_be_bytes([h[o] & 0x7f, h[o + 1], h[o + 2], h[o + 3]])) % 1_000_000;
    format!("{code:06}")
}

/// `totp.verify_code`: this 30 s step or the one either side
pub fn verify_totp(secret: &str, code: &str) -> bool {
    let code = code.trim();
    if secret.is_empty() || code.is_empty() {
        return false;
    }
    let Some(key) = base32_decode(secret) else { return false };
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) / 30;
    [now.saturating_sub(1), now, now + 1].iter().any(|c| totp_at(&key, *c) == code)
}

/// `totp.consume_backup_code`: Some(new list) when the code matched one (it is used up)
fn consume_backup(stored: Option<&str>, code: &str) -> Option<Vec<Value>> {
    if code.is_empty() {
        return None;
    }
    let mut hashes = match serde_json::from_str::<Value>(stored.filter(|s| !s.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a,
        _ => vec![],
    };
    let target = json!(sha256_hex(&code.trim().to_lowercase()));
    let i = hashes.iter().position(|h| *h == target)?;
    hashes.remove(i);
    Some(hashes)
}

// ── the lockouts (per process, as Python's) ──────────────────────────────────

type Counts = Mutex<HashMap<String, (u32, Instant)>>;

fn counts(which: &str) -> &'static Counts {
    static LOGIN: OnceLock<Counts> = OnceLock::new();
    static TWO: OnceLock<Counts> = OnceLock::new();
    if which == "2fa" { TWO.get_or_init(Default::default) } else { LOGIN.get_or_init(Default::default) }
}

fn lock_remaining(which: &str, key: &str) -> u64 {
    let mut m = counts(which).lock().unwrap_or_else(|e| e.into_inner());
    let Some((count, started)) = m.get(key).copied() else { return 0 };
    if count < MAX_ATTEMPTS {
        return 0;
    }
    let el = started.elapsed().as_secs_f64();
    if el >= LOCKOUT_SECONDS as f64 {
        m.remove(key);
        return 0;
    }
    (LOCKOUT_SECONDS as f64 - el) as u64
}

fn record_failure(which: &str, key: &str) {
    let mut m = counts(which).lock().unwrap_or_else(|e| e.into_inner());
    let (mut count, mut started) = m.get(key).copied().unwrap_or((0, Instant::now()));
    if started.elapsed().as_secs() >= LOCKOUT_SECONDS {
        (count, started) = (0, Instant::now());
    }
    m.insert(key.into(), (count + 1, started));
}

fn clear(which: &str, key: &str) {
    counts(which).lock().unwrap_or_else(|e| e.into_inner()).remove(key);
}

fn too_many(secs: u64) -> String {
    format!("Too many failed attempts. Try again in {} minute(s).", secs / 60 + 1)
}

// ── the session cookie (app/session_cookie.py) ───────────────────────────────

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn secure() -> bool {
    std::env::var("COOKIE_SECURE").unwrap_or_default().trim().to_lowercase() == "true"
}

fn flags() -> String {
    format!("httponly; samesite=lax{}", if secure() { "; secure" } else { "" })
}

/// The session the browser sent: (contents, whether there was one)
fn read_session(pages: &Pages, headers: &HeaderMap) -> (Map<String, Value>, bool) {
    let Some(secret) = pages.secret.as_deref() else { return (Map::new(), false) };
    let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()).unwrap_or("");
    match crate::session::from_cookie_header(cookie, secret, now()) {
        Some(Value::Object(m)) => (m, true),
        _ => (Map::new(), false),
    }
}

/// The `Set-Cookie` for this session (None: nothing to send)
fn session_cookie(pages: &Pages, session: &Map<String, Value>, had: bool) -> Option<String> {
    let secret = pages.secret.as_deref()?;
    if session.is_empty() {
        return had.then(|| format!("session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; {}", flags()));
    }
    let text = nd_render::py_dumps(&Value::Object(session.clone()));
    Some(format!("session={}; path=/; Max-Age={SESSION_MAX_AGE}; {}", crate::session::sign(&text, secret, now()), flags()))
}

fn redirect(to: &str) -> Response<Body> {
    Response::builder().status(StatusCode::SEE_OTHER).header(header::LOCATION, utf8_percent_encode(to, LOCATION_SAFE).to_string())
        .header(header::CONTENT_LENGTH, "0").body(Body::empty()).unwrap()
}

fn with_cookie(mut r: Response<Body>, cookie: Option<String>) -> Response<Body> {
    if let Some(c) = cookie {
        if let Ok(v) = c.parse() {
            r.headers_mut().append(header::SET_COOKIE, v);
        }
    }
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    r
}

fn page(pages: &Pages, template: &str, ctx: Value, status: StatusCode, headers: &HeaderMap) -> Option<Response<Body>> {
    let html = pages.env.get_template(template).ok()?.render(nd_render::pyvalue::to_value(&ctx)).ok()?;
    Some(crate::pages::html(status, html, headers))
}

/// `site_layout.remember`: the person's site layout, as the session carries it
fn site_layout(db: &rusqlite::Connection, uid: i64) -> Value {
    let raw: Option<String> = db.query_row("SELECT prefs_json FROM user_view_prefs WHERE user_id = ?1 AND section = 'site' LIMIT 1", [uid], |r| r.get(0)).ok().flatten();
    let cleaned = raw.and_then(|t| serde_json::from_str::<Value>(if t.is_empty() { "{}" } else { &t }).ok())
        .and_then(|v| nd_render::app::clean("site", &v)).or_else(|| nd_render::app::clean("site", &json!({}))).unwrap_or(json!({}));
    json!({"options": cleaned["options"], "pins": cleaned["pins"], "at": now(), "uid": uid})
}

/// Sign in: user_id, the site layout and the session version (`_begin_2fa_or_login` without 2FA)
fn sign_in(db: &rusqlite::Connection, session: &mut Map<String, Value>, user: &Map<String, Value>) {
    let uid = user["id"].as_i64().unwrap_or(0);
    session.insert("user_id".into(), json!(uid));
    session.insert("site_layout".into(), site_layout(db, uid));
    session.insert("session_version".into(), user.get("session_version").cloned().unwrap_or(Value::Null));
}

/// `_is_device_trusted`: a valid "trust this device" cookie for this person (an expired one is removed)
fn trusted(db: &rusqlite::Connection, headers: &HeaderMap, uid: i64) -> bool {
    let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()).unwrap_or("");
    let Some(raw) = crate::session::cookie(cookie, "trusted_device").filter(|r| !r.is_empty()) else { return false };
    let row: Option<(i64, i64, String)> = db.query_row("SELECT id, user_id, expires_at FROM trusted_devices WHERE token_hash = ?1 LIMIT 1", [sha256_hex(raw)],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok();
    let Some((id, owner, expires)) = row else { return false };
    if expires.as_str() < crate::orm::now_stamp().as_str() {
        let _ = db.execute("DELETE FROM trusted_devices WHERE id = ?1", [id]);
        return false;
    }
    owner == uid
}

fn user_by_email(db: &rusqlite::Connection, email: &str) -> Option<Map<String, Value>> {
    db.query_row("SELECT * FROM users WHERE email = ?1 LIMIT 1", [email], |r| crate::layout::row_map(r)).ok()
}

fn ip(peer: Option<SocketAddr>) -> String {
    peer.map_or("?".into(), |p| p.ip().to_string())
}

/// The routes: Some(answer), or None for Python
pub async fn route(pages: Arc<Pages>, cfg: Arc<Config>, method: Method, uri: Uri, headers: HeaderMap, body: bytes::Bytes, peer: Option<SocketAddr>) -> Option<Response<Body>> {
    pages.secret.as_ref()?;
    tokio::task::spawn_blocking(move || {
        let db = crate::writes::open(&cfg)?;
        match (method.as_str(), uri.path()) {
            ("POST", "/login") => login(&pages, &db, &headers, &body, peer),
            ("POST", "/api/login") => api_login(&pages, &db, &headers, &body, peer),
            ("GET", "/logout") => {
                let (_, had) = read_session(&pages, &headers);
                let mut r = with_cookie(redirect("/login"), session_cookie(&pages, &Map::new(), had));
                r.headers_mut().insert("clear-site-data", "\"cache\", \"storage\"".parse().ok()?);
                Some(r)
            }
            ("GET", "/login/2fa") => {
                let (s, _) = read_session(&pages, &headers);
                if crate::edits::py_truthy_pub(s.get("user_id")) {
                    return Some(with_cookie(redirect("/"), None));
                }
                if !crate::edits::py_truthy_pub(s.get("pending_2fa_user_id")) {
                    return Some(with_cookie(redirect("/login"), None));
                }
                Some(with_cookie(page(&pages, "auth/login_2fa.html", json!({"error": null}), StatusCode::OK, &headers)?, None))
            }
            ("POST", "/login/2fa") => login_2fa(&pages, &db, &headers, &body),
            ("GET", "/account/2fa/setup") => twofa_setup(&pages, &cfg, &db, &uri, &headers),
            ("POST", "/account/2fa/setup") => twofa_confirm(&pages, &cfg, &db, &uri, &headers, &body),
            _ => None,
        }
    }).await.ok()?
}

/// `GET /account/2fa/setup` (app/routers/account.py `account_2fa_setup`): the pending secret kept in the session across reloads, its
/// otpauth URI as a QR code (crate::qr, module for module Python's)
fn twofa_setup(pages: &Pages, cfg: &Config, db: &rusqlite::Connection, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let user = signed_in_user(cfg, db, headers)?;
    if crate::edits::py_truthy_pub(user.get("totp_enabled")) {
        return Some(with_cookie(redirect("/account"), None));
    }
    let (mut s, had) = read_session(pages, headers);
    let mut cookie = None;
    let secret = match s.get("pending_totp_secret") {
        Some(Value::String(t)) if !t.is_empty() => t.clone(),
        Some(v) if crate::edits::py_truthy_pub(Some(v)) => return None, // not text: Python's
        _ => {
            // pyotp.random_base32(): 32 characters of A-Z2-7
            const B32: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
            use rand::Rng;
            let mut rng = rand::rngs::OsRng;
            let t: String = (0..32).map(|_| B32[rng.gen_range(0..32)] as char).collect();
            s.insert("pending_totp_secret".into(), json!(t));
            cookie = session_cookie(pages, &s, had);
            t
        }
    };
    let r = twofa_page(pages, cfg, db, uri, headers, user, &secret, Value::Null, StatusCode::OK)?;
    Some(with_cookie(r, cookie))
}

/// The person the (fresh) session names
fn signed_in_user(cfg: &Config, db: &rusqlite::Connection, headers: &HeaderMap) -> Option<Map<String, Value>> {
    let p = crate::layout::build(db, cfg, "GET", &Uri::from_static("/account"), headers, now())?;
    db.query_row("SELECT * FROM users WHERE id = ?1", [p.user_id], |r| crate::layout::row_map(r)).ok()
}

/// account_2fa_setup.html for `secret`, with `error`
fn twofa_page(pages: &Pages, cfg: &Config, db: &rusqlite::Connection, uri: &Uri, headers: &HeaderMap, user: Map<String, Value>, secret: &str,
              error: Value, status: StatusCode) -> Option<Response<Body>> {
    let mut p = crate::layout::build(db, cfg, "GET", uri, headers, now())?;
    let email = user.get("email").and_then(|v| v.as_str())?.to_string();
    // pyotp's provisioning_uri: quote(issuer) ":" quote(name), then urlencode({secret, issuer}) with "+" as %20
    const QUOTE: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC.remove(b'/').remove(b'_').remove(b'.').remove(b'-').remove(b'~');
    const PLUS: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC.remove(b'_').remove(b'.').remove(b'-').remove(b'~');
    let issuer = "N&D World";
    let otp = format!("otpauth://totp/{}:{}?secret={}&issuer={}", utf8_percent_encode(issuer, QUOTE), utf8_percent_encode(&email, QUOTE),
        utf8_percent_encode(secret, PLUS), utf8_percent_encode(issuer, PLUS));
    p.ctx.insert("user".into(), Value::Object(user));
    p.ctx.insert("secret".into(), json!(secret));
    p.ctx.insert("qr_data_uri".into(), json!(crate::qr::data_uri(&otp)?));
    p.ctx.insert("error".into(), error);
    let html = pages.env.get_template("account_2fa_setup.html").ok()?.render(nd_render::pyvalue::to_value(&Value::Object(p.ctx))).ok()?;
    Some(with_cookie(crate::pages::html(status, html, headers), None))
}

/// `POST /account/2fa/setup` (`account_2fa_confirm`): a wrong code shows the setup page again (400); the right one turns two-step on with
/// 8 fresh backup codes (stored as sha256) and shows them on the account page
fn twofa_confirm(pages: &Pages, cfg: &Config, db: &rusqlite::Connection, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let user = signed_in_user(cfg, db, headers)?;
    if crate::edits::py_truthy_pub(user.get("totp_enabled")) {
        return Some(with_cookie(redirect("/account"), None));
    }
    let (mut s, had) = read_session(pages, headers);
    let secret = match s.get("pending_totp_secret") {
        Some(Value::String(t)) if !t.is_empty() => t.clone(),
        Some(v) if crate::edits::py_truthy_pub(Some(v)) => return None,
        _ => return Some(with_cookie(redirect("/account/2fa/setup"), None)),
    };
    let form = crate::ai::small_form(headers, body)?;
    let code = form.get("code").unwrap_or("").to_string();
    if !verify_totp(&secret, &code) {
        return twofa_page(pages, cfg, db, uri, headers, user, &secret, json!("Incorrect code \u{2014} try again."), StatusCode::BAD_REQUEST);
    }
    use rand::RngCore;
    let codes: Vec<String> = (0..8).map(|_| {
        let mut b = [0u8; 5];
        rand::rngs::OsRng.fill_bytes(&mut b);
        b.iter().map(|x| format!("{x:02x}")).collect()
    }).collect();
    let hashes = Value::Array(codes.iter().map(|c| json!(sha256_hex(&c.to_lowercase()))).collect());
    let uid = user.get("id")?.as_i64()?;
    crate::orm::update(db, "users", uid, &[("totp_secret", rusqlite::types::Value::Text(secret)), ("totp_enabled", rusqlite::types::Value::Integer(1)),
        ("totp_backup_codes_json", rusqlite::types::Value::Text(nd_render::py_dumps(&hashes)))])?;
    s.shift_remove("pending_totp_secret");
    let mut extra = Map::new();
    extra.insert("new_backup_codes".into(), json!(codes));
    let r = crate::account::render(pages, cfg, db, &Uri::from_static("/account/2fa/setup"), headers, extra, StatusCode::OK)?;
    Some(with_cookie(r, session_cookie(pages, &s, had)))
}

fn login(pages: &Pages, db: &rusqlite::Connection, headers: &HeaderMap, body: &[u8], peer: Option<SocketAddr>) -> Option<Response<Body>> {
    let form = crate::ai::small_form(headers, body)?;
    let email = form.get("email").unwrap_or("").trim().to_lowercase();
    let password = form.get("password").unwrap_or("").to_string();
    let next = safe_next_url(Some(form.get("next").filter(|n| !n.is_empty()).unwrap_or("/")));
    let key = format!("{}|{email}", ip(peer));
    let locked = lock_remaining("login", &key);
    if locked > 0 {
        return Some(with_cookie(page(pages, "auth/login.html", json!({"next": next, "error": too_many(locked)}), StatusCode::TOO_MANY_REQUESTS, headers)?, None));
    }
    let user = user_by_email(db, &email);
    let ok = match &user {
        Some(u) => verify_password(&password, u.get("password_hash").and_then(|v| v.as_str()).unwrap_or("")),
        None => { burn(); false }
    };
    let Some(user) = user.filter(|_| ok) else {
        record_failure("login", &key);
        return Some(with_cookie(page(pages, "auth/login.html", json!({"next": next, "error": "Incorrect email or password."}), StatusCode::BAD_REQUEST, headers)?, None));
    };
    clear("login", &key);
    let (mut s, had) = read_session(pages, headers);
    let uid = user["id"].as_i64()?;
    if crate::edits::py_truthy_pub(user.get("totp_enabled")) && !trusted(db, headers, uid) {
        s.insert("pending_2fa_user_id".into(), json!(uid));
        s.insert("pending_2fa_next".into(), json!(next));
        return Some(with_cookie(redirect("/login/2fa"), session_cookie(pages, &s, had)));
    }
    sign_in(db, &mut s, &user);
    Some(with_cookie(redirect(&next), session_cookie(pages, &s, had)))
}

/// A two-step code: the TOTP, else a backup code (used up and saved)
fn check_code(db: &rusqlite::Connection, user: &Map<String, Value>, code: &str) -> bool {
    if verify_totp(user.get("totp_secret").and_then(|v| v.as_str()).unwrap_or(""), code) {
        return true;
    }
    match consume_backup(user.get("totp_backup_codes_json").and_then(|v| v.as_str()), code) {
        Some(left) => crate::orm::update(db, "users", user["id"].as_i64().unwrap_or(0), &[("totp_backup_codes_json", rusqlite::types::Value::Text(nd_render::py_dumps(&json!(left))))]).is_some(),
        None => false,
    }
}

fn login_2fa(pages: &Pages, db: &rusqlite::Connection, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let (mut s, had) = read_session(pages, headers);
    if s.get("pending_2fa_invite_code").is_some() {
        return None; // redeeming an invite on the way in: Python's
    }
    let Some(pending) = s.get("pending_2fa_user_id").filter(|v| crate::edits::py_truthy_pub(Some(v))).and_then(|v| v.as_i64()) else {
        return Some(with_cookie(redirect("/login"), None));
    };
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [pending], |r| crate::layout::row_map(r)).ok();
    let Some(user) = user.filter(|u| crate::edits::py_truthy_pub(u.get("totp_enabled"))) else {
        for k in ["pending_2fa_user_id", "pending_2fa_next", "pending_2fa_invite_code"] {
            s.shift_remove(k);
        }
        return Some(with_cookie(redirect("/login"), session_cookie(pages, &s, had)));
    };
    let key = pending.to_string();
    let locked = lock_remaining("2fa", &key);
    if locked > 0 {
        return Some(with_cookie(page(pages, "auth/login_2fa.html", json!({"error": too_many(locked)}), StatusCode::TOO_MANY_REQUESTS, headers)?, None));
    }
    let form = crate::ai::small_form(headers, body)?;
    let code = form.get("code").unwrap_or("").trim().to_string();
    if !check_code(db, &user, &code) {
        record_failure("2fa", &key);
        return Some(with_cookie(page(pages, "auth/login_2fa.html", json!({"error": "Incorrect code. Try again."}), StatusCode::BAD_REQUEST, headers)?, None));
    }
    clear("2fa", &key);
    let next = safe_next_url(Some(s.shift_remove("pending_2fa_next").and_then(|v| v.as_str().map(str::to_string)).as_deref().unwrap_or("/")));
    s.shift_remove("pending_2fa_invite_code");
    s.shift_remove("pending_2fa_user_id");
    sign_in(db, &mut s, &user);
    let mut r = with_cookie(redirect(&next), session_cookie(pages, &s, had));
    if !form.get("trust_device").unwrap_or("").trim().is_empty() {
        let raw: String = {
            use base64::Engine;
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(rand::random::<[u8; 32]>())
        };
        let ua: String = headers.get(header::USER_AGENT).and_then(|v| v.to_str().ok()).unwrap_or("").chars().take(256).collect();
        let expires = crate::orm::stamp_after(TRUST_DAYS * 86400);
        crate::orm::insert(db, "trusted_devices", &[("user_id", rusqlite::types::Value::Integer(pending)), ("token_hash", rusqlite::types::Value::Text(sha256_hex(&raw))),
            ("label", rusqlite::types::Value::Text(ua)), ("expires_at", rusqlite::types::Value::Text(expires))])?;
        let c = format!("trusted_device={raw}; HttpOnly; Max-Age={}; Path=/; SameSite=lax{}", TRUST_DAYS * 86400, if secure() { "; Secure" } else { "" });
        r.headers_mut().append(header::SET_COOKIE, c.parse().ok()?);
    }
    Some(r)
}

fn json_answer(status: StatusCode, v: Value) -> Response<Body> {
    Response::builder().status(status).header(header::CONTENT_TYPE, "application/json").body(Body::from(serde_json::to_string(&v).unwrap_or_default())).unwrap()
}

fn api_login(pages: &Pages, db: &rusqlite::Connection, headers: &HeaderMap, body: &[u8], peer: Option<SocketAddr>) -> Option<Response<Body>> {
    let Ok(Value::Object(b)) = serde_json::from_slice::<Value>(body) else { return None };
    // str() of a non-string, and anything stranger, is Python's to read
    let field = |k: &str| -> Option<String> { match b.get(k) { None => Some(String::new()), Some(Value::String(s)) => Some(s.clone()), _ => None } };
    let email = field("email")?.trim().to_lowercase();
    let password = field("password")?;
    let code = field("totp_code")?.trim().to_string();
    let key = format!("{}|{email}", ip(peer));
    let locked = lock_remaining("login", &key);
    if locked > 0 {
        return Some(with_cookie(json_answer(StatusCode::TOO_MANY_REQUESTS, json!({"ok": false, "detail": too_many(locked)})), None));
    }
    let user = user_by_email(db, &email);
    let ok = match &user {
        Some(u) => verify_password(&password, u.get("password_hash").and_then(|v| v.as_str()).unwrap_or("")),
        None => { burn(); false }
    };
    let Some(user) = user.filter(|_| ok) else {
        record_failure("login", &key);
        return Some(with_cookie(json_answer(StatusCode::UNAUTHORIZED, json!({"ok": false, "detail": "Incorrect email or password."})), None));
    };
    let uid = user["id"].as_i64()?;
    if crate::edits::py_truthy_pub(user.get("totp_enabled")) {
        let k2 = uid.to_string();
        let locked2 = lock_remaining("2fa", &k2);
        if locked2 > 0 {
            return Some(with_cookie(json_answer(StatusCode::TOO_MANY_REQUESTS, json!({"ok": false, "requires_2fa": true, "detail": too_many(locked2)})), None));
        }
        if !check_code(db, &user, &code) {
            record_failure("2fa", &k2);
            let detail = if code.is_empty() { "Two-step code required." } else { "Invalid two-step code." };
            return Some(with_cookie(json_answer(StatusCode::UNAUTHORIZED, json!({"ok": false, "requires_2fa": true, "detail": detail})), None));
        }
        clear("2fa", &k2);
    }
    clear("login", &key);
    let (mut s, had) = read_session(pages, headers);
    sign_in(db, &mut s, &user);
    let is_gm = crate::edits::py_truthy_pub(user.get("is_gm"));
    Some(with_cookie(json_answer(StatusCode::OK, json!({"ok": true, "user": {"id": uid, "email": user["email"], "is_gm": is_gm}})), session_cookie(pages, &s, had)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc_vectors() {
        // RFC 6070 PBKDF2-HMAC-SHA256 (2 iterations), RFC 6238 TOTP (SHA-1, T = 59 s -> 94287082 -> "287082")
        assert_eq!(hex(&pbkdf2_sha256(b"password", b"salt", 2)), "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43");
        assert_eq!(totp_at(b"12345678901234567890", 59 / 30), "287082");
        assert_eq!(base32_decode("gezdgnbvgy3tqojqgezdgnbvgy3tqojq").unwrap(), b"12345678901234567890");
    }
}

/// The session refreshed at the front of the gateway (what app/session_cookie.py and site_layout.for_request do on Python's pages): a
/// session signed more than 6 h ago is re-signed, and a signed-in person's site layout older than 15 min (or another person's) is
/// read again. Returns (the new cookie value, the Set-Cookie line), or None when nothing needs refreshing.
pub fn refresh(cfg: &Config, headers: &HeaderMap) -> Option<(String, String)> {
    let secret = cfg.secret_key.as_deref()?;
    let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok())?;
    let n = now();
    let (sess, signed_at) = crate::session::read_with_time(crate::session::cookie(cookie, crate::session::COOKIE_NAME)?, secret, n, crate::session::MAX_AGE_SECONDS)?;
    let Value::Object(mut s) = sess else { return None };
    if s.is_empty() {
        return None;
    }
    let mut changed = n.saturating_sub(signed_at) > crate::session::REFRESH_SECONDS;
    if let Some(uid) = s.get("user_id").and_then(|v| v.as_i64()).filter(|i| *i != 0) {
        let fresh = s.get("site_layout").and_then(|v| v.as_object()).is_some_and(|sl| {
            sl.get("uid").and_then(|v| v.as_i64()) == Some(uid) && sl.get("options").is_some_and(|o| o.is_object())
                && (n as i64 - sl.get("at").and_then(|v| v.as_i64()).unwrap_or(0)) < 15 * 60
        });
        if !fresh {
            let db = crate::writes::open(cfg)?;
            let v: Option<i64> = db.query_row("SELECT session_version FROM users WHERE id = ?1", [uid], |r| r.get(0)).ok().flatten();
            if v.is_some() && s.get("session_version").and_then(|x| x.as_i64()) == v {
                s.insert("site_layout".into(), site_layout(&db, uid));
                changed = true;
            }
        }
    }
    if !changed {
        return None;
    }
    let value = crate::session::sign(&nd_render::py_dumps(&Value::Object(s)), secret, n);
    let line = format!("session={value}; path=/; Max-Age={SESSION_MAX_AGE}; {}", flags());
    Some((value, line))
}

const PUBLIC_PATHS: [&str; 7] = ["/login", "/login/2fa", "/api/login", "/logout", "/health", "/favicon.ico", "/sw.js"];
const PUBLIC_PREFIXES: [&str; 3] = ["/join/", "/static/", "/share/"];

/// `urllib.parse.quote(path)` (safe "/")
fn quote_path(p: &str) -> String {
    const SAFE: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC.remove(b'/').remove(b'_').remove(b'.').remove(b'-').remove(b'~');
    utf8_percent_encode(p, SAFE).to_string()
}

fn gate_json(status: StatusCode, detail: &str) -> Response<Body> {
    Response::builder().status(status).header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(serde_json::to_string(&json!({"detail": detail})).unwrap_or_default())).unwrap()
}

/// app/main.py `auth_gate`, at the front of the gateway: Some(the refusal) for a request that may not reach any route - not signed in
/// (a 401 for /api/, else to the sign-in page), a person who no longer exists or whose session predates a password change (the
/// session cleared), or a non-GM on a path their role may not use (403) - else None, and the request goes on.
pub fn gate(pages: &Pages, cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let raw = uri.path();
    let path = percent_encoding::percent_decode_str(raw).decode_utf8().ok()?.to_string();
    if PUBLIC_PATHS.contains(&path.as_str()) || PUBLIC_PREFIXES.iter().any(|p| path.starts_with(p)) {
        return None;
    }
    // app/ai_background.py sits outside auth_gate: a POST asking to run in the background gets its task first, and the gate runs inside it
    if method == "POST" && headers.get("x-nd-background").and_then(|v| v.to_str().ok()).is_some_and(|v| v.trim() == "1") {
        return None;
    }
    let api = path.starts_with("/api/");
    let to_login = || with_cookie(redirect(&format!("/login?next={}", quote_path(&path))), None);
    let (session, had) = read_session(pages, headers);
    let Some(uid) = session.get("user_id").filter(|v| crate::edits::py_truthy_pub(Some(v))).cloned() else {
        return Some(if api { with_cookie(gate_json(StatusCode::UNAUTHORIZED, "Login required"), None) } else { to_login() });
    };
    let db = crate::writes::open(cfg)?;
    let user = uid.as_i64().and_then(|u| db.query_row("SELECT id, is_gm, session_version FROM users WHERE id = ?1", [u],
        |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<bool>>(1)?.unwrap_or(false), r.get::<_, Option<i64>>(2)?))).ok());
    let cleared = session_cookie(pages, &Map::new(), had);
    let Some((uid, is_gm, version)) = user else {
        return Some(with_cookie(redirect("/login"), cleared));
    };
    if session.get("session_version").and_then(|v| v.as_i64()) != version || session.get("session_version").is_none() && version.is_some() {
        return Some(if api {
            with_cookie(gate_json(StatusCode::UNAUTHORIZED, "Session expired — please log in again."), cleared)
        } else {
            with_cookie(redirect(&format!("/login?next={}", quote_path(&path))), cleared)
        });
    }
    if is_gm {
        return None;
    }
    // the role in the active world: ?w=<slug> over the active_world cookie, else the first world this person belongs to
    let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()).unwrap_or("");
    let wanted = crate::pages::query_param(uri, "w").filter(|s| !s.is_empty())
        .or_else(|| crate::session::cookie(cookie, "active_world").map(|c| percent_encoding::percent_decode_str(c).decode_utf8_lossy().to_string()));
    let mut st = db.prepare("SELECT m.role, m.world_id, w.slug FROM world_memberships m JOIN worlds w ON w.id = m.world_id WHERE m.user_id = ?1 ORDER BY w.id").ok()?;
    let rows: Vec<(Option<String>, i64, String)> = st.query_map([uid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let chosen = rows.iter().find(|r| Some(&r.2) == wanted.as_ref()).or_else(|| rows.first());
    let viewer = nd_perm::allow::Viewer { is_gm: false, active_role: chosen.and_then(|c| c.0.clone()), active_world_id: chosen.map(|c| c.1) };
    if nd_perm::allow::gate_allows(&viewer, method, &path) {
        return None;
    }
    Some(with_cookie(if api {
        gate_json(StatusCode::FORBIDDEN, "GM access required")
    } else {
        Response::builder().status(StatusCode::FORBIDDEN).header(header::CONTENT_TYPE, "text/html; charset=utf-8").body(Body::from(
            "<body style='background:#0a0a0f;color:#c8d0e0;font-family:monospace;padding:2rem'><h1 style='color:#ff2d78'>403 — GM access required</h1>\
<p><a href='/' style='color:#00f0ff'>&larr; Back</a></p></body>")).unwrap()
    }, None))
}

/// The test servers' `POST /__test__/reset` clears this process's lockouts too, as it clears Python's - only with ND_TEST_HOOKS=1 (a
/// real server must never let a request reset them)
pub fn test_reset() {
    if std::env::var("ND_TEST_HOOKS").ok().as_deref() == Some("1") {
        for which in ["login", "2fa"] {
            counts(which).lock().unwrap_or_else(|e| e.into_inner()).clear();
        }
    }
}
