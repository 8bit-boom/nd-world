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
    pub env: Environment<'static>,
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

/// A page as Python's GZip middleware sends it: gzipped from 1 KB up when the browser accepts it.
fn html(status: StatusCode, body: String, request_headers: &HeaderMap) -> Response<Body> {
    let wants_gzip = request_headers
        .get(header::ACCEPT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .map_or(false, |v| v.split(',').any(|p| p.split(';').next().unwrap_or("").trim().eq_ignore_ascii_case("gzip")));
    let b = Response::builder().status(status).header(header::CONTENT_TYPE, "text/html; charset=utf-8");
    if wants_gzip && body.len() >= 1024 {
        use std::io::Write;
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::new(6));
        if enc.write_all(body.as_bytes()).is_ok() {
            if let Ok(bytes) = enc.finish() {
                return b.header(header::CONTENT_ENCODING, "gzip").header(header::VARY, "Accept-Encoding").body(Body::from(bytes)).unwrap();
            }
        }
    }
    b.header(header::VARY, "Accept-Encoding").body(Body::from(body)).unwrap()
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
        Some(html(StatusCode::OK, page, headers))
    }
}

/// A page built from the database: the layout context (crate::layout) plus the page's own data, rendered with the app's template.
/// `None` = let Python build it.
pub fn render_with(pages: &Pages, template: &str, ctx: serde_json::Map<String, serde_json::Value>, headers: &HeaderMap) -> Option<Response<Body>> {
    let page = pages.env.get_template(template).ok()?.render(nd_render::pyvalue::to_value(&serde_json::Value::Object(ctx)));
    let page = match page {
        Ok(p) => p,
        Err(e) => {
            eprintln!("nd-gateway: {template}: {e:#}");
            return None;
        }
    };
    let mut r = html(StatusCode::OK, page, headers);
    r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
    Some(r)
}

/// `GET /dice` (app/routers/dice.py `dice_page`).
pub fn dice(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world_id = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    if let Some(w) = &p.world {
        if !p.is_gm {
            let level = nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "dice")?;
            if level == "none" {
                return None; // Python's 403
            }
        }
    } else if !p.is_gm {
        // no world: Python's world_can_view_section(None) decides by the defaults; "dice" is readable, so the page shows its empty state
    }
    let mut rolls = vec![];
    if let Some(wid) = world_id {
        let mut st = db.prepare("SELECT id, user_name, notation, label, breakdown, total FROM dice_rolls WHERE world_id = ?1 ORDER BY created_at DESC, id DESC LIMIT 50").ok()?;
        let rows = st
            .query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?, r.get::<_, Option<String>>(4)?, r.get::<_, Option<i64>>(5)?)))
            .ok()?
            .collect::<Result<Vec<_>, _>>()
            .ok()?;
        for (id, user_name, notation, label, breakdown, total) in rows {
            let b: serde_json::Value = serde_json::from_str(breakdown.as_deref().unwrap_or("[]")).ok()?;
            rolls.push(serde_json::json!({"id": id, "user_name": user_name, "notation": notation, "label": label.unwrap_or_default(), "breakdown": b, "total": total}));
        }
    }
    let q = |k: &str| query_param(uri, k).unwrap_or_default();
    p.ctx.insert("rolls".into(), serde_json::Value::Array(rolls));
    p.ctx.insert("error".into(), q("error").into());
    p.ctx.insert("last_notation".into(), q("n").into());
    render_with(pages, "dice.html", p.ctx, headers)
}

/// The pages whose handler is "the layout, a section check, perhaps one list": app/routers/*.py and app/main.py, one row each.
pub struct Simple {
    pub path: &'static str,
    pub template: &'static str,
    /// world_can_view_section(...) the handler checks (None = no section check)
    pub section: Option<&'static str>,
    /// the handler raises 404 without a world
    pub needs_world: bool,
    pub gm_only: bool,
    /// extra context: (name, SQL with ?1 = world id, one row per item)
    pub lists: &'static [(&'static str, &'static str)],
}

pub const SIMPLE: &[Simple] = &[
    Simple { path: "/chronicler", template: "chronicler.html", section: Some("chronicler"), needs_world: false, gm_only: false, lists: &[] },
    Simple { path: "/background-jobs", template: "background_jobs.html", section: Some("background_jobs"), needs_world: true, gm_only: false, lists: &[] },
    Simple { path: "/tools/bulk-edit", template: "bulk_edit.html", section: Some("bulk_edit"), needs_world: false, gm_only: false, lists: &[] },
    Simple { path: "/system", template: "system_monitor.html", section: None, needs_world: false, gm_only: true, lists: &[] },
    Simple { path: "/boards", template: "boards.html", section: Some("boards"), needs_world: true, gm_only: false,
             lists: &[("boards", "SELECT * FROM invest_boards WHERE world_id = ?1 ORDER BY rowid")] },
];

pub fn simple(pages: &Pages, cfg: &Config, page: &Simple, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if page.gm_only && !p.is_gm {
        return None;
    }
    if page.needs_world && p.world.is_none() {
        return None;
    }
    if let (Some(section), false) = (page.section, p.is_gm) {
        let w = p.world.as_ref();
        let s = |k: &str| w.and_then(|w| w.get(k)).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), section)? == "none" {
            return None;
        }
    }
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(0);
    for (name, sql) in page.lists {
        let mut st = db.prepare(sql).ok()?;
        let rows = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        p.ctx.insert(name.to_string(), serde_json::Value::Array(rows.into_iter().map(serde_json::Value::Object).collect()));
    }
    p.ctx.insert("kinds".into(), nd_render::app::data()["kinds"].clone());
    p.ctx.insert("kind_icons".into(), nd_render::app::data()["kind_icons"].clone());
    render_with(pages, page.template, p.ctx, headers)
}
