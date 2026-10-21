//! Pages the gateway builds itself instead of passing them to Python (docs/RUST_PAGES.md). Each one is switched on by name in `ND_RUST_ROUTES`
//! and answers only when it is sure to produce what Python would: anything else (an unexpected method, no usable session key) returns `None`
//! and goes to Python as before.
use crate::{session, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use minijinja::{context, Environment};
use percent_encoding::{percent_decode_str, utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use std::time::{SystemTime, UNIX_EPOCH};

pub struct Pages {
    env: Environment<'static>,
    routes: Vec<String>,
    secret: Option<String>,
}

/// What Starlette keeps unescaped in a redirect's Location (`quote(url, safe=":/%#?=@[]!$&'()*+,;")`).
const LOCATION_SAFE: &AsciiSet = &NON_ALPHANUMERIC.remove(b':').remove(b'/').remove(b'%').remove(b'#').remove(b'?').remove(b'=').remove(b'@').remove(b'[').remove(b']').remove(b'!').remove(b'$').remove(b'&').remove(b'\'').remove(b'(').remove(b')').remove(b'*').remove(b'+').remove(b',').remove(b';').remove(b'-').remove(b'.').remove(b'_').remove(b'~');

/// The last value of a query parameter (what FastAPI hands a handler when it is given twice), decoded like `parse_qsl`.
pub fn query_param(uri: &Uri, name: &str) -> Option<String> {
    let mut found = None;
    for pair in uri.query().unwrap_or("").split('&') {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        let dec = |s: &str| percent_decode_str(&s.replace('+', " ")).decode_utf8_lossy().to_string();
        if dec(k) == name {
            found = Some(dec(v));
        }
    }
    found
}

/// `auth.safe_next_url`: a same-site relative path or "/".
pub fn safe_next_url(candidate: Option<&str>) -> String {
    let c = candidate.unwrap_or("").trim();
    if c.is_empty() || !c.starts_with('/') || c.starts_with("//") || c.starts_with("/\\") {
        "/".to_string()
    } else {
        c.to_string()
    }
}

fn html(status: StatusCode, body: String) -> Response<Body> {
    Response::builder().status(status).header(header::CONTENT_TYPE, "text/html; charset=utf-8").body(Body::from(body)).unwrap()
}

impl Pages {
    pub fn new(cfg: &Config) -> Pages {
        Pages { env: nd_render::environment(&cfg.templates_dir, &cfg.static_dir), routes: cfg.rust_routes.clone(), secret: cfg.secret_key.clone() }
    }

    fn on(&self, name: &str) -> bool {
        self.routes.iter().any(|r| r == name)
    }

    fn session(&self, headers: &HeaderMap) -> Option<serde_json::Value> {
        let secret = self.secret.as_deref()?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()).unwrap_or("");
        Some(session::from_cookie_header(cookie, secret, now).unwrap_or(serde_json::Value::Null))
    }

    pub fn serve(&self, method: &Method, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
        if method != Method::GET {
            return None;
        }
        match uri.path() {
            "/login" if self.on("login") => self.login(uri, headers),
            _ => None,
        }
    }

    /// `GET /login` (app/routers/auth.py `login_form`): a signed-in browser is sent on to `next`, anyone else gets the form.
    /// The POST (rate limit, passwords, 2FA) stays in Python.
    fn login(&self, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
        let session = self.session(headers)?;
        let next = safe_next_url(query_param(uri, "next").as_deref());
        let signed_in = match session.get("user_id") {
            None | Some(serde_json::Value::Null) | Some(serde_json::Value::Bool(false)) => false,
            Some(serde_json::Value::Number(n)) => n.as_f64().map_or(true, |f| f != 0.0),
            Some(serde_json::Value::String(s)) => !s.is_empty(),
            Some(_) => true,
        };
        if signed_in {
            let loc = utf8_percent_encode(&next, LOCATION_SAFE).to_string();
            return Response::builder().status(StatusCode::SEE_OTHER).header(header::LOCATION, loc).body(Body::empty()).ok();
        }
        let page = self.env.get_template("auth/login.html").ok()?.render(context! { next => next, error => Option::<String>::None }).ok()?;
        Some(html(StatusCode::OK, page))
    }
}
