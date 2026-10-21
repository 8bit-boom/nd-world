//! Reads the app's session cookie (Starlette `SessionMiddleware` over itsdangerous' `TimestampSigner`) so the Rust side and the Python side
//! accept the same sessions. Format of the cookie value: `base64(json) "." base64(timestamp) "." base64url(hmac_sha1(key, "<first>.<second>"))`
//! with `key = sha1("itsdangerous.Signer" + "signer" + secret)`. Only reading is done here; Python still writes the cookie. Tested against cookies
//! produced by Python (`tests/fixtures/session.json`, made by `scripts/gen_session_fixture.py`).
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use hmac::{Hmac, Mac};
use sha1::{Digest, Sha1};

pub const COOKIE_NAME: &str = "session";
pub const MAX_AGE_SECONDS: u64 = 14 * 24 * 3600;

fn key(secret: &str) -> Vec<u8> {
    let mut h = Sha1::new();
    h.update(b"itsdangerous.Signer");
    h.update(b"signer");
    h.update(secret.as_bytes());
    h.finalize().to_vec()
}

/// The session's JSON if `value` is a cookie signed with `secret` no longer than `max_age` seconds before `now` (unix seconds).
pub fn read(value: &str, secret: &str, now: u64, max_age: u64) -> Option<serde_json::Value> {
    let (signed, sig) = value.rsplit_once('.')?;
    let (payload, ts) = signed.rsplit_once('.')?;
    let sig = URL_SAFE_NO_PAD.decode(sig).ok()?;
    let mut mac = Hmac::<Sha1>::new_from_slice(&key(secret)).ok()?;
    mac.update(signed.as_bytes());
    mac.verify_slice(&sig).ok()?; // constant time
    let ts_bytes = URL_SAFE_NO_PAD.decode(ts).ok()?;
    if ts_bytes.is_empty() || ts_bytes.len() > 8 {
        return None;
    }
    let signed_at = ts_bytes.iter().fold(0u64, |a, b| (a << 8) | *b as u64);
    if now.saturating_sub(signed_at) > max_age {
        return None;
    }
    let json = STANDARD.decode(payload).ok()?;
    serde_json::from_slice(&json).ok()
}

/// The session from a `Cookie:` request header.
pub fn from_cookie_header(header: &str, secret: &str, now: u64) -> Option<serde_json::Value> {
    header
        .split(';')
        .filter_map(|c| c.trim().split_once('='))
        .find(|(k, _)| *k == COOKIE_NAME)
        .and_then(|(_, v)| read(v, secret, now, MAX_AGE_SECONDS))
}
