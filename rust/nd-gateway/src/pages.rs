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
    /// anything else the handler adds (None = nothing)
    pub extra: Option<fn(&rusqlite::Connection, &mut serde_json::Map<String, serde_json::Value>) -> Option<()>>,
}

/// app/main.py `androidapp`: the emulator's address from Settings, else ANDROID_EMULATOR_URL, without a trailing slash.
fn android_url(db: &rusqlite::Connection, ctx: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let saved: Option<String> = db.query_row("SELECT android_emulator_url FROM app_settings LIMIT 1", [], |r| r.get(0)).ok()?;
    let env = std::env::var("ANDROID_EMULATOR_URL").unwrap_or_default();
    let env = env.trim_end_matches('/');
    let url = saved.filter(|s| !s.is_empty()).unwrap_or_else(|| env.to_string());
    ctx.insert("android_url".into(), url.trim_end_matches('/').into());
    Some(())
}

pub const SIMPLE: &[Simple] = &[
    Simple { path: "/chronicler", template: "chronicler.html", section: Some("chronicler"), needs_world: false, gm_only: false, lists: &[], extra: None },
    Simple { path: "/background-jobs", template: "background_jobs.html", section: Some("background_jobs"), needs_world: true, gm_only: false, lists: &[], extra: None },
    Simple { path: "/tools/bulk-edit", template: "bulk_edit.html", section: Some("bulk_edit"), needs_world: false, gm_only: false, lists: &[], extra: None },
    Simple { path: "/system", template: "system_monitor.html", section: None, needs_world: false, gm_only: true, lists: &[], extra: None },
    Simple { path: "/boards", template: "boards.html", section: Some("boards"), needs_world: true, gm_only: false,
             lists: &[("boards", "SELECT * FROM invest_boards WHERE world_id = ?1 ORDER BY rowid")], extra: None },
    Simple { path: "/androidapp", template: "androidapp.html", section: Some("androidapp"), needs_world: false, gm_only: false, lists: &[], extra: Some(android_url) },
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
    if let Some(f) = page.extra {
        f(&db, &mut p.ctx)?;
    }
    render_with(pages, page.template, p.ctx, headers)
}

const COL_PRIORITY: &[&str] = &["rank", "rarity", "damage", "armor", "rounds", "strength", "body", "dexterity", "perception", "augment slots", "max health",
    "max pp", "feats", "cost", "difficulty to craft", "effect", "type of feat", "requirement", "requirements", "special conditions"];

/// `GET /kind/{kind}` (app/main.py `list_entities`): one kind's entities, by folder, with the stat columns of the kinds that have them.
pub fn kind_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let kind = percent_encoding::percent_decode_str(uri.path().strip_prefix("/kind/")?).decode_utf8().ok()?.to_string();
    if kind.is_empty() || kind.contains('/') {
        return None;
    }
    let qp = |k: &str| query_param(uri, k);
    let q = qp("q").unwrap_or_default();
    let folder: Option<String> = qp("folder");
    let mut view = qp("view").unwrap_or_default();
    let page: i64 = match qp("page") {
        None => 1,
        Some(p) => p.trim().parse().ok()?, // FastAPI's 422 for anything else is Python's
    };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?; // no world: Python redirects to /worlds
    let wid = world.get("id")?.as_i64()?;
    if !p.is_gm {
        let s = |k: &str| world.get(k).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), &format!("kind_{kind}"))? == "none" {
            return None;
        }
    }
    let rows = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    let world_players = rows(
        "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 ORDER BY users.display_name",
        &[&wid],
    )?;
    // the base query: this kind in this world, what the viewer may see (deps.filter_visible_entities), the search
    let mut base = String::from("FROM entities WHERE entities.kind = ?1 AND entities.world_id = ?2");
    if !p.is_gm {
        base.push_str(" AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_player_access.entity_id FROM entity_player_access WHERE entity_player_access.user_id = ?3))");
    } else {
        base.push_str(" AND ?3 IS NOT NULL");
    }
    if !q.is_empty() {
        base.push_str(" AND (lower(entities.name) LIKE lower(?4) OR lower(entities.tags) LIKE lower(?4) OR lower(entities.summary) LIKE lower(?4) OR lower(entities.body) LIKE lower(?4))");
    } else {
        base.push_str(" AND ?4 IS NOT NULL");
    }
    let like = format!("%{q}%");
    let args: [&dyn rusqlite::ToSql; 4] = [&kind, &wid, &p.user_id, &like];
    let mut folder_counts: Vec<(String, i64)> = vec![];
    {
        let mut st = db.prepare(&format!("SELECT entities.folder, count(entities.id) {base} GROUP BY entities.folder")).ok()?;
        let r = st.query_map(&args[..], |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(), r.get::<_, i64>(1)?))).ok()?;
        for x in r {
            let (f, n) = x.ok()?;
            match folder_counts.iter_mut().find(|(k, _)| *k == f) {
                Some(e) => e.1 = n, // None and "" both count as "": the later row wins, as in the dict
                None => folder_counts.push((f, n)),
            }
        }
    }
    let total_count: i64 = folder_counts.iter().map(|(_, n)| n).sum();
    let mut folders: Vec<String> = folder_counts.iter().filter(|(k, _)| !k.is_empty()).map(|(k, _)| k.clone()).collect();
    folders.sort();
    let mut is_parent = false;
    let mut extra = String::new();
    if let Some(f) = &folder {
        if !f.is_empty() {
            is_parent = folders.iter().any(|x| x.starts_with(&format!("{f}/")));
            extra = if is_parent { " AND (entities.folder = ?5 OR entities.folder LIKE ?6)".into() } else { " AND entities.folder = ?5 AND ?6 IS NOT NULL".into() };
        } else {
            extra = " AND entities.folder IS NULL AND ?5 IS NOT NULL AND ?6 IS NOT NULL".into();
        }
    }
    let fval = folder.clone().unwrap_or_default();
    let flike = format!("{fval}/%");
    let entities = if folder.is_some() {
        let a: [&dyn rusqlite::ToSql; 6] = [&kind, &wid, &p.user_id, &like, &fval, &flike];
        rows(&format!("SELECT entities.* {base}{extra} ORDER BY entities.folder NULLS LAST, entities.name"), &a)?
    } else {
        rows(&format!("SELECT entities.* {base} ORDER BY entities.folder NULLS LAST, entities.name"), &args)?
    };
    let efolder = |e: &Map<String, Value>| e.get("folder").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mut grouped: Vec<(String, Vec<Value>)> = vec![];
    for e in &entities {
        let k = efolder(e);
        match grouped.iter_mut().find(|(g, _)| *g == k) {
            Some(g) => g.1.push(Value::Object(e.clone())),
            None => grouped.push((k, vec![Value::Object(e.clone())])),
        }
    }
    let mut subfolder_groups: Vec<Value> = vec![];
    if is_parent {
        let f = folder.clone().unwrap();
        let prefix = format!("{f}/");
        let mut gmap: Vec<(String, Vec<Value>)> = vec![];
        for e in &entities {
            let ef = efolder(e);
            let key = if ef.is_empty() || ef == f {
                f.clone()
            } else if let Some(rest) = ef.strip_prefix(&prefix) {
                format!("{prefix}{}", rest.split('/').next().unwrap_or(""))
            } else {
                f.clone()
            };
            match gmap.iter_mut().find(|(g, _)| *g == key) {
                Some(g) => g.1.push(Value::Object(e.clone())),
                None => gmap.push((key, vec![Value::Object(e.clone())])),
            }
        }
        // sorted by (is the folder itself, lower-cased name); Python's lower() of these names
        gmap.sort_by(|a, b| ((a.0 == f), a.0.to_lowercase()).cmp(&((b.0 == f), b.0.to_lowercase())));
        subfolder_groups = gmap.into_iter().map(|(k, v)| json!([k, v])).collect();
    }
    let stat_kind = matches!(kind.as_str(), "item" | "feat" | "creature" | "character");
    let char_feat_folder = kind == "feat"
        && folder.as_deref().map_or(false, |f| !f.is_empty() && ["Race Feats", "Common Feats", "Profession Feats"].iter().any(|r| f == *r || f.starts_with(&format!("{r}/"))));
    let mut entity_stats = Map::new();
    let mut table_cols: Vec<String> = vec![];
    if folder.is_some() {
        let mut freq: Vec<(String, i64)> = vec![];
        static RANK: std::sync::OnceLock<fancy_regex::Regex> = std::sync::OnceLock::new();
        let rank_re = RANK.get_or_init(|| fancy_regex::Regex::new(r"(?i)\b(rank\s+(\d+)|edge|origin)\b").unwrap());
        for e in &entities {
            let mut d: Vec<(String, String)> = vec![];
            if stat_kind && !char_feat_folder {
                for r in nd_render::text::parse_stats(e.get("body").and_then(|v| v.as_str()).unwrap_or("")) {
                    let (k, v) = (r["key"].as_str()?.to_string(), r["val"].as_str()?.to_string());
                    match d.iter_mut().find(|(a, _)| *a == k) {
                        Some(x) => x.1 = v,
                        None => d.push((k, v)),
                    }
                }
            }
            let ef = efolder(e);
            if kind == "feat" && !ef.is_empty() {
                if let Ok(Some(m)) = rank_re.captures(&ef) {
                    let seg = m[1].to_lowercase();
                    let v = if seg.starts_with("rank") {
                        seg.split_whitespace().last().unwrap_or("").to_string()
                    } else {
                        let mut c = seg.chars();
                        c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
                    };
                    match d.iter_mut().find(|(a, _)| a == "Rank") {
                        Some(x) => x.1 = v,
                        None => d.push(("Rank".into(), v)),
                    }
                }
            }
            if !d.is_empty() {
                for (k, _) in &d {
                    match freq.iter_mut().find(|(a, _)| a == k) {
                        Some(x) => x.1 += 1,
                        None => freq.push((k.clone(), 1)),
                    }
                }
                let id = e.get("id")?.as_i64()?.to_string();
                entity_stats.insert(id, Value::Object(d.into_iter().map(|(k, v)| (k, Value::String(v))).collect()));
            }
        }
        if stat_kind && !char_feat_folder {
            let prio = |k: &str| COL_PRIORITY.iter().position(|c| *c == k.to_lowercase()).unwrap_or(999) as i64;
            let mut cols = freq.clone();
            cols.sort_by_key(|(k, n)| (prio(k), -n));
            table_cols = cols.into_iter().take(8).map(|(k, _)| k).collect();
        }
    }
    if view.is_empty() {
        view = if folder.is_some() { "table".into() } else { "grid".into() };
    }
    let (mut table_page, mut table_total) = (1i64, 1i64);
    let mut table_page_entities: Vec<Value> = entities.iter().cloned().map(Value::Object).collect();
    if folder.is_some() && !is_parent && view == "table" {
        table_total = std::cmp::max(1, (entities.len() as i64 + 49) / 50);
        table_page = page.max(1).min(table_total);
        let s = ((table_page - 1) * 50) as usize;
        table_page_entities = entities.iter().skip(s).take(50).cloned().map(Value::Object).collect();
    }
    // the worlds list here is _visible_worlds (the same set as the layout's), already in the context
    let c = &mut p.ctx;
    c.insert("kind".into(), kind.clone().into());
    c.insert("entities".into(), Value::Array(entities.iter().cloned().map(Value::Object).collect()));
    c.insert("grouped".into(), Value::Object(grouped.into_iter().map(|(k, v)| (k, Value::Array(v))).collect()));
    c.insert("folders".into(), json!(folders));
    c.insert("active_folder".into(), json!(folder));
    c.insert("folder_counts".into(), Value::Object(folder_counts.into_iter().map(|(k, n)| (k, json!(n))).collect()));
    c.insert("total_count".into(), json!(total_count));
    c.insert("q".into(), json!(q));
    c.insert("view".into(), json!(view));
    c.insert("entity_stats".into(), json!({"__intkeys__": Value::Object(entity_stats)}));
    c.insert("table_cols".into(), json!(table_cols));
    c.insert("is_parent_folder".into(), json!(is_parent));
    c.insert("subfolder_groups".into(), Value::Array(subfolder_groups));
    c.insert("char_feat_folder".into(), json!(char_feat_folder));
    c.insert("world_players".into(), Value::Array(world_players.into_iter().map(Value::Object).collect()));
    c.insert("table_page_entities".into(), Value::Array(table_page_entities));
    c.insert("table_page".into(), json!(table_page));
    c.insert("table_total_pages".into(), json!(table_total));
    render_with(pages, "entities/list.html", p.ctx, headers)
}

/// Python's `str(x)` for a custom-field value from JSON; `None` = not sure (floats, containers).
fn py_str_value(v: &serde_json::Value) -> Option<String> {
    use serde_json::Value;
    Some(match v {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        _ => return None,
    })
}

fn truthy_json(v: Option<&serde_json::Value>) -> bool {
    use serde_json::Value;
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `GET /entity/{id}` (app/main.py `detail`): one entity's page - body with headings and autolinks, notes, custom fields, connections, media.
pub fn entity_detail(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use nd_render::{md, text};
    use serde_json::{json, Map, Value};
    let id: i64 = uri.path().strip_prefix("/entity/")?.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let one = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Option<Map<String, Value>>> {
        use rusqlite::OptionalExtension;
        db.query_row(sql, params, |r| crate::layout::row_map(r)).optional().ok()
    };
    let rows = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    // _entity_view_gate: every refusal is Python's 404
    let entity = one("SELECT * FROM entities WHERE id = ?1", &[&id])??;
    let ewid = entity.get("world_id").and_then(|v| v.as_i64());
    let ent_world = match ewid {
        Some(w) => one("SELECT * FROM worlds WHERE id = ?1", &[&w])??,
        None => return None,
    };
    let ewid = ewid?;
    if !p.is_gm {
        let member: i64 = db.query_row("SELECT count(*) FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [ewid, p.user_id], |r| r.get(0)).ok()?;
        if member == 0 {
            return None;
        }
        let s = |k: &str| ent_world.get(k).and_then(|v| v.as_str());
        let kind = entity.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), &format!("kind_{kind}"))? == "none" {
            return None;
        }
        if !truthy_json(entity.get("visible_to_players")) {
            let shared: i64 = db.query_row("SELECT count(*) FROM entity_player_access WHERE entity_id = ?1 AND user_id = ?2", [id, p.user_id], |r| r.get(0)).ok()?;
            if shared == 0 {
                return None;
            }
        }
    }
    let view_as_player = p.is_gm && query_param(uri, "as").as_deref() == Some("player");
    let is_gm = p.is_gm && !view_as_player;
    let viewer_id: Option<i64> = if view_as_player {
        query_param(uri, "player").filter(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())).and_then(|s| s.parse().ok())
    } else {
        Some(p.user_id)
    };
    let strip = |t: &str| -> Option<String> { nd_perm::gmonly::strip_gm_only(t, viewer_id, nd_render::now_minute()) };
    // the request's own visibility rule (filter_visible_entities) goes by the REAL viewer, not the preview
    let vis = if p.is_gm { "1".to_string() } else { format!("(e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id) };
    let active_wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let (mut audio, mut video) = (vec![], vec![]);
    if let Some(aw) = active_wid {
        let v = if is_gm { "" } else { " AND visible_to_players IS 1" };
        audio = rows(&format!("SELECT * FROM audio_clips WHERE entity_id = ?1 AND world_id = ?2{v} ORDER BY created_at DESC"), &[&id, &aw])?;
        video = rows(&format!("SELECT * FROM video_clips WHERE entity_id = ?1 AND world_id = ?2{v} ORDER BY created_at DESC"), &[&id, &aw])?;
    }
    let backlinks = rows(&format!("SELECT e.* FROM entities e JOIN entity_links l ON l.source_id = e.id WHERE l.target_id = ?1 AND {vis} ORDER BY e.kind, e.name"), &[&id])?;
    let notes_vis = if p.is_gm { "" } else { " AND visible_to_players IS 1" };
    let mut notes = rows(&format!("SELECT * FROM entity_notes WHERE entity_id = ?1{notes_vis} ORDER BY created_at"), &[&id])?;
    // the autolink names: every other entity of the entity's world the viewer may see, names then aliases, then derived variants
    let mut names: Vec<(String, i64)> = vec![];
    {
        let mut st = db.prepare(&format!("SELECT e.id, e.name, e.aliases FROM entities e WHERE e.world_id = ?1 AND e.id != ?2 AND {vis} ORDER BY e.id")).ok()?;
        let r: Vec<(i64, Option<String>, Option<String>)> = st.query_map([ewid, id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        let mut add = |n: String, i: i64| {
            if !names.iter().any(|(k, _)| *k == n) {
                names.push((n, i));
            }
        };
        for (eid, name, aliases) in &r {
            if let Some(n) = name.as_ref().filter(|n| !n.is_empty()) {
                add(n.clone(), *eid);
            }
            for a in aliases.as_deref().unwrap_or("").split(',') {
                let a = text::py_strip(a);
                if !a.is_empty() {
                    add(a.to_string(), *eid);
                }
            }
        }
        for (eid, name, _) in &r {
            for v in text::derive_name_variants(name.as_deref().unwrap_or("")) {
                add(v, *eid);
            }
        }
    }
    for n in notes.iter_mut() {
        let content = n.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let content = if is_gm { content } else { strip(&content)? };
        let html = if truthy_json(n.get("content_is_html")) { content } else { md::render_md(&content) };
        n.insert("content_html".into(), Value::String(text::autolink_entities(&html, &names)?));
    }
    // custom fields of a template-made entity
    let mut custom_sections: Vec<(String, Vec<Value>)> = vec![];
    let mut custom_fields = Map::new();
    if let Some(tid) = entity.get("template_id").and_then(|v| v.as_i64()) {
        let tpl = one("SELECT fields_json FROM entity_templates WHERE id = ?1", &[&tid])?;
        let fields: Vec<Value> = match tpl.and_then(|t| t.get("fields_json").and_then(|v| v.as_str()).map(str::to_string)) {
            Some(s) if !s.is_empty() => serde_json::from_str(&s).ok()?,
            _ => vec![],
        };
        custom_fields = match entity.get("custom_fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            Some(s) => serde_json::from_str::<Value>(s).ok()?.as_object()?.clone(),
            None => Map::new(),
        };
        for f in fields {
            let sec = f.get("section").filter(|s| truthy_json(Some(s))).and_then(|s| s.as_str()).unwrap_or("Custom").to_string();
            match custom_sections.iter_mut().find(|(s, _)| *s == sec) {
                Some(x) => x.1.push(f),
                None => custom_sections.push((sec, vec![f])),
            }
        }
    }
    let body = entity.get("body").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let summary = entity.get("summary").and_then(|v| v.as_str()).map(str::to_string);
    let safe_body = if is_gm { body.clone() } else { strip(&body)? };
    let safe_summary: Value = match &summary {
        None => Value::Null,
        Some(s) => Value::String(if is_gm { s.clone() } else { strip(s)? }),
    };
    let (mut body_sections, mut toc) = (vec![], vec![]);
    if !safe_body.is_empty() {
        let html = text::autolink_entities(&md::render_md(&text::strip_legacy_anchors(&safe_body)), &names)?;
        let (h, t) = text::rules_toc(&html, "123");
        toc = t;
        body_sections = text::split_sections_h123(&h);
    }
    let related = rows(
        &format!("SELECT e.* FROM entities e WHERE e.id IN (SELECT target_id FROM entity_links WHERE source_id = ?1) AND {vis} ORDER BY e.kind, e.name"),
        &[&id],
    )?;
    // retrieval.entity_extra_context
    let mut parts: Vec<String> = vec![];
    let mut field_lines: Vec<String> = vec![];
    for (_, fields) in &custom_sections {
        for f in fields {
            let fid = f.get("id").and_then(|v| v.as_str())?;
            let label = f.get("label").and_then(|v| v.as_str()).unwrap_or("");
            if f.get("type").and_then(|v| v.as_str()) == Some("list") {
                for row in custom_fields.get(fid).and_then(|v| v.as_array()).cloned().unwrap_or_default() {
                    let mut bits = vec![];
                    for sf in f.get("item_fields").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
                        let sid = sf.get("id").and_then(|v| v.as_str())?;
                        if truthy_json(row.get(sid)) {
                            bits.push(format!("{}: {}", sf.get("label").and_then(|v| v.as_str()).unwrap_or(""), py_str_value(&row[sid])?));
                        }
                    }
                    if !bits.is_empty() {
                        field_lines.push(format!("- {label}: {}", bits.join("; ")));
                    }
                }
                continue;
            }
            let Some(v) = custom_fields.get(fid).filter(|v| truthy_json(Some(v))) else { continue };
            let mut val = py_str_value(v)?;
            if !is_gm {
                val = nd_perm::gmonly::strip_gm_only(&val, None, nd_render::now_minute())?;
            }
            let val = text::py_strip(&val);
            if !val.is_empty() {
                field_lines.push(format!("- {label}: {val}"));
            }
        }
    }
    if !field_lines.is_empty() {
        parts.push(format!("Custom fields:\n{}", field_lines.join("\n")));
    }
    let mut note_lines = vec![];
    for n in &notes {
        if !is_gm && !truthy_json(n.get("visible_to_players")) {
            continue;
        }
        if truthy_json(n.get("content_is_html")) {
            return None; // html2text is Python's
        }
        let mut c = n.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !is_gm {
            c = nd_perm::gmonly::strip_gm_only(&c, None, nd_render::now_minute())?;
        }
        let c = text::py_strip(&c);
        if !c.is_empty() {
            note_lines.push(format!("- {c}"));
        }
    }
    if !note_lines.is_empty() {
        parts.push(format!("Notes:\n{}", note_lines.join("\n")));
    }
    let mut rel_names: Vec<String> = related.iter().chain(backlinks.iter()).filter_map(|e| e.get("name").and_then(|v| v.as_str()).map(str::to_string)).collect();
    rel_names.sort();
    rel_names.dedup();
    if !rel_names.is_empty() {
        parts.push(format!("Related entities: {}", rel_names.join(", ")));
    }
    let ai_extra = parts.join("\n\n");
    // ai_instructions.enabled_instructions_text for the active world
    let mut ai_custom = String::new();
    if let Some(aw) = active_wid {
        let pl = if is_gm { "" } else { " AND applies_to_players IS 1" };
        let rs = rows(&format!("SELECT title, content FROM ai_instructions WHERE world_id = ?1 AND enabled IS 1{pl} ORDER BY created_at"), &[&aw])?;
        let ps: Vec<String> = rs
            .iter()
            .filter_map(|r| {
                let c = text::py_strip(r.get("content").and_then(|v| v.as_str()).unwrap_or(""));
                (!c.is_empty()).then(|| format!("## {}\n\n{c}", r.get("title").and_then(|v| v.as_str()).unwrap_or("None")))
            })
            .collect();
        if !ps.is_empty() {
            ai_custom = format!("Additional GM-authored instructions for how you should behave — follow these in addition to everything above, not instead of it:\n\n{}", ps.join("\n\n"));
        }
    }
    let objs = |v: Vec<Map<String, Value>>| Value::Array(v.into_iter().map(Value::Object).collect());
    let c = &mut p.ctx;
    c.insert("entity".into(), Value::Object(entity));
    c.insert("view_as_player".into(), json!(view_as_player));
    c.insert("backlinks".into(), objs(backlinks));
    c.insert("entity_notes".into(), objs(notes));
    c.insert("custom_sections".into(), Value::Array(custom_sections.into_iter().map(|(s, f)| json!([s, f])).collect()));
    c.insert("custom_fields".into(), Value::Object(custom_fields));
    c.insert("body_sections".into(), Value::Array(body_sections));
    c.insert("toc".into(), Value::Array(toc));
    c.insert("safe_body".into(), Value::String(safe_body));
    c.insert("safe_summary".into(), safe_summary);
    c.insert("visible_related".into(), objs(related));
    c.insert("entity_ai_extra".into(), Value::String(ai_extra));
    c.insert("entity_media_audio".into(), objs(audio));
    c.insert("entity_media_video".into(), objs(video));
    c.insert("ai_custom_instructions".into(), Value::String(ai_custom));
    render_with(pages, "entities/detail.html", p.ctx, headers)
}

fn section_ok(p: &crate::layout::PageCtx, world: Option<&serde_json::Map<String, serde_json::Value>>, section: &str) -> Option<bool> {
    if p.is_gm {
        return Some(true);
    }
    let s = |k: &str| world.and_then(|w| w.get(k)).and_then(|v| v.as_str());
    Some(nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), section)? != "none")
}

fn page_param(uri: &Uri) -> Option<i64> {
    match query_param(uri, "page") {
        None => Some(1),
        Some(p) => p.trim().parse().ok(),
    }
}

/// `GET /sessions` (app/routers/sessions.py `sessions_list`)
pub fn sessions_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let page = page_param(uri)?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !section_ok(&p, p.world.as_ref(), "sessions")? {
        return None;
    }
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(1);
    let total: i64 = db.query_row("SELECT count(*) FROM game_sessions WHERE world_id = ?1", [wid], |r| r.get(0)).ok()?;
    let total_pages = std::cmp::max(1, (total + 49) / 50);
    let page = page.max(1).min(total_pages);
    let mut st = db.prepare("SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 50 OFFSET ?2").ok()?;
    let sessions = st.query_map([wid, (page - 1) * 50], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut st = db.prepare("SELECT id, name FROM parties WHERE world_id = ?1").ok()?;
    let mut names = Map::new();
    for r in st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()? {
        let (id, n) = r.ok()?;
        names.insert(id.to_string(), json!(n));
    }
    p.ctx.insert("sessions".into(), Value::Array(sessions.into_iter().map(Value::Object).collect()));
    p.ctx.insert("page".into(), json!(page));
    p.ctx.insert("total_pages".into(), json!(total_pages));
    p.ctx.insert("party_names".into(), json!({"__intkeys__": Value::Object(names)}));
    render_with(pages, "sessions/list.html", p.ctx, headers)
}

/// `GET /quests` (app/routers/quests.py `quests_list`)
pub fn quests_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "quests")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let term = nd_render::text::py_strip(&query_param(uri, "q").unwrap_or_default()).to_string();
    let category = query_param(uri, "category").unwrap_or_default();
    let party = query_param(uri, "party").unwrap_or_default();
    let mut sql = String::from("SELECT * FROM quests WHERE world_id = ?1");
    if !p.is_gm {
        sql.push_str(" AND visible_to_players IS NOT 0");
    }
    sql.push_str(if term.is_empty() { " AND ?2 IS NOT NULL" } else { " AND (lower(title) LIKE lower(?2) OR lower(summary) LIKE lower(?2) OR lower(body) LIKE lower(?2))" });
    sql.push_str(if category.is_empty() { " AND ?3 IS NOT NULL" } else { " AND category = ?3" });
    // str.isdigit(): Unicode digits too; those make int() differ - leave such a request to Python
    if party.chars().any(|c| c.is_numeric() && !c.is_ascii_digit()) {
        return None;
    }
    let party_id: Option<i64> = if !party.is_empty() && party.bytes().all(|b| b.is_ascii_digit()) { Some(party.parse().ok()?) } else { None };
    sql.push_str(if party_id.is_some() { " AND assigned_party_id = ?4" } else { " AND ?4 IS NULL" });
    sql.push_str(" ORDER BY title");
    let like = format!("%{term}%");
    let mut st = db.prepare(&sql).ok()?;
    let quests = st
        .query_map(rusqlite::params![wid, like, category, party_id], |r| crate::layout::row_map(r))
        .ok()?
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let (mut done, mut total): (Vec<(i64, i64)>, Vec<(i64, i64)>) = (vec![], vec![]);
    let bump = |v: &mut Vec<(i64, i64)>, k: i64| match v.iter_mut().find(|(a, _)| *a == k) {
        Some(x) => x.1 += 1,
        None => v.push((k, 1)),
    };
    for q in &quests {
        if let Some(pid) = q.get("parent_id").and_then(|v| v.as_i64()).filter(|x| *x != 0) {
            bump(&mut total, pid);
            if q.get("status").and_then(|v| v.as_str()) == Some("complete") {
                bump(&mut done, pid);
            }
        }
    }
    let statuses = ["active", "complete", "failed", "secret"];
    let mut grouped: Vec<(String, Vec<Value>)> = statuses.iter().map(|s| (s.to_string(), vec![])).collect();
    for q in &quests {
        let s = q.get("status").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("active").to_string();
        match grouped.iter_mut().find(|(g, _)| *g == s) {
            Some(g) => g.1.push(Value::Object(q.clone())),
            None => grouped.push((s, vec![Value::Object(q.clone())])),
        }
    }
    let rows = |sql: &str| -> Option<Vec<Value>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r.into_iter().map(Value::Object).collect())
    };
    let parties = rows("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name")?;
    let sessions = rows("SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 50")?;
    // world_can_edit_section(request, world, "quests")
    let can_create = p.is_gm || {
        let s = |k: &str| world.get(k).and_then(|v| v.as_str());
        nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), "quests")? == "edit"
    };
    let ik = |v: Vec<(i64, i64)>| json!({"__intkeys__": Value::Object(v.into_iter().map(|(k, n)| (k.to_string(), json!(n))).collect::<Map<_, _>>())});
    let c = &mut p.ctx;
    c.insert("grouped".into(), Value::Object(grouped.into_iter().map(|(k, v)| (k, Value::Array(v))).collect()));
    c.insert("statuses".into(), json!(statuses));
    c.insert("can_create".into(), json!(can_create));
    c.insert("q".into(), json!(term));
    c.insert("category".into(), json!(category));
    c.insert("party".into(), json!(party));
    c.insert("parties".into(), Value::Array(parties));
    c.insert("subs_done".into(), ik(done));
    c.insert("subs_total".into(), ik(total));
    c.insert("sessions".into(), Value::Array(sessions));
    c.insert("ai_ready".into(), json!(""));
    render_with(pages, "quests/list.html", p.ctx, headers)
}

fn edit_level(p: &crate::layout::PageCtx, world: &serde_json::Map<String, serde_json::Value>, section: &str) -> Option<bool> {
    if p.is_gm {
        return Some(true);
    }
    let s = |k: &str| world.get(k).and_then(|v| v.as_str());
    Some(nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), section)? == "edit")
}

/// `GET /parties` (app/routers/parties.py `parties_list`)
pub fn parties_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let page = page_param(uri)?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "parties")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let total: i64 = db.query_row("SELECT count(*) FROM parties WHERE world_id = ?1", [wid], |r| r.get(0)).ok()?;
    let total_pages = std::cmp::max(1, (total + 49) / 50);
    let page = page.max(1).min(total_pages);
    let mut st = db.prepare("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name LIMIT 50 OFFSET ?2").ok()?;
    let parties = st.query_map([wid, (page - 1) * 50], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut counts = Map::new();
    for party in &parties {
        let arr = |k: &str| -> Option<Vec<Value>> {
            match party.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                None => Some(vec![]),
                Some(s) => serde_json::from_str::<Value>(s).ok()?.as_array().cloned(),
            }
        };
        let pcs = arr("member_pc_ids_json")?;
        let ents = arr("member_entity_ids_json")?;
        let n = if p.is_gm {
            pcs.len() + ents.len()
        } else {
            let ids: Vec<i64> = ents.iter().filter_map(|v| v.as_i64().or_else(|| v.as_bool().map(|b| b as i64))).collect();
            let pwid = party.get("world_id").and_then(|v| v.as_i64()).unwrap_or(wid);
            let mut seen: Vec<i64> = vec![];
            for id in ids {
                let ok: i64 = db
                    .query_row(
                        "SELECT count(*) FROM entities e WHERE e.id = ?1 AND e.world_id = ?2 AND (e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?3))",
                        [id, pwid, p.user_id],
                        |r| r.get(0),
                    )
                    .ok()?;
                if ok > 0 && !seen.contains(&id) {
                    seen.push(id);
                }
            }
            pcs.len() + seen.len()
        };
        counts.insert(party.get("id")?.as_i64()?.to_string(), json!(n));
    }
    let can_create = edit_level(&p, &world, "parties")? && (p.is_gm || p.role.as_deref() == Some("assistant"));
    let c = &mut p.ctx;
    c.insert("parties".into(), Value::Array(parties.into_iter().map(Value::Object).collect()));
    c.insert("member_counts".into(), json!({"__intkeys__": Value::Object(counts)}));
    c.insert("page".into(), json!(page));
    c.insert("total_pages".into(), json!(total_pages));
    c.insert("can_create".into(), json!(can_create));
    render_with(pages, "parties/list.html", p.ctx, headers)
}

/// `GET /facts` (app/routers/facts.py `facts_list`)
pub fn facts_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !section_ok(&p, p.world.as_ref(), "facts")? {
        return None;
    }
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(1);
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    // the dict {id: session} in the database's own order (no ORDER BY in Python: rowid order)
    let sessions = rows("SELECT * FROM game_sessions WHERE world_id = ?1")?;
    let vis = if p.is_gm { "" } else { " AND visible_to_players IS NOT 0" };
    let facts = rows(&format!("SELECT * FROM facts WHERE world_id = ?1{vis} ORDER BY created_at ASC, id ASC"))?;
    let sid_of = |f: &Map<String, Value>| f.get("game_session_id").and_then(|v| v.as_i64()).filter(|x| *x != 0);
    let known = |sid: i64| sessions.iter().find(|s| s.get("id").and_then(|v| v.as_i64()) == Some(sid));
    let mut by_session: Vec<(i64, Vec<Value>)> = vec![];
    let mut unsessioned = vec![];
    for f in &facts {
        match sid_of(f).filter(|s| known(*s).is_some()) {
            Some(sid) => match by_session.iter_mut().find(|(s, _)| *s == sid) {
                Some(x) => x.1.push(Value::Object(f.clone())),
                None => by_session.push((sid, vec![Value::Object(f.clone())])),
            },
            None => unsessioned.push(Value::Object(f.clone())),
        }
    }
    let label = |s: &Map<String, Value>| -> String {
        let num = s.get("session_num").map(|v| if v.is_null() { "None".to_string() } else { v.to_string() }).unwrap_or_else(|| "None".into());
        format!("#{num} {}", s.get("title").and_then(|v| v.as_str()).unwrap_or("None"))
    };
    let mut groups = vec![];
    if !unsessioned.is_empty() {
        unsessioned.reverse();
        groups.push(json!({"session": null, "label": "🗂 No session", "facts": unsessioned}));
    }
    // sorted by (session_num or 0, id), newest first - stable, so ties keep first-seen order reversed... as sorted(reverse=True) does
    let key = |sid: i64| {
        let s = known(sid).unwrap();
        (s.get("session_num").and_then(|v| v.as_i64()).unwrap_or(0), sid)
    };
    by_session.sort_by(|a, b| key(b.0).cmp(&key(a.0)));
    for (sid, fl) in by_session {
        let s = known(sid)?.clone();
        groups.push(json!({"label": label(&s), "session": Value::Object(s), "facts": fl}));
    }
    let mut tags: Vec<(String, i64)> = vec![];
    for g in &groups {
        for f in g["facts"].as_array()? {
            for t in f.get("tags").and_then(|v| v.as_str()).unwrap_or("").split(',') {
                let t = nd_render::text::py_strip(t);
                if !t.is_empty() {
                    match tags.iter_mut().find(|(k, _)| k == t) {
                        Some(x) => x.1 += 1,
                        None => tags.push((t.to_string(), 1)),
                    }
                }
            }
        }
    }
    tags.sort_by(|a, b| (-a.1, &a.0).cmp(&(-b.1, &b.0)));
    let fact_count: usize = groups.iter().map(|g| g["facts"].as_array().map_or(0, |a| a.len())).sum();
    let session_options: Vec<Value> = sessions.iter().map(|s| json!({"id": s.get("id"), "label": label(s)})).collect();
    let smap: Map<String, Value> = sessions.iter().map(|s| (s.get("id").and_then(|v| v.as_i64()).unwrap_or(0).to_string(), Value::Object(s.clone()))).collect();
    let c = &mut p.ctx;
    c.insert("groups".into(), Value::Array(groups));
    c.insert("sessions".into(), json!({"__intkeys__": Value::Object(smap)}));
    c.insert("session_options".into(), Value::Array(session_options));
    c.insert("fact_count".into(), json!(fact_count));
    c.insert("top_tags".into(), Value::Array(tags.into_iter().map(|(k, n)| json!([k, n])).collect()));
    render_with(pages, "facts/list.html", p.ctx, headers)
}

/// `GET /tables` (app/routers/tables.py `tables_list`)
pub fn tables_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "tables")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let mut st = db.prepare("SELECT * FROM random_tables WHERE world_id IS NULL OR world_id = ?1 ORDER BY category, name").ok()?;
    let tables = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let is_assistant = p.role.as_deref() == Some("assistant");
    let mut grouped: Vec<(String, Vec<Value>)> = vec![];
    for mut t in tables {
        // _can_edit_table_row
        let can = match t.get("world_id").and_then(|v| v.as_i64()) {
            None => p.is_gm || is_assistant,
            Some(tw) => {
                let tworld = db.query_row("SELECT section_access_json, custom_kinds_json FROM worlds WHERE id = ?1", [tw], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))).ok();
                let edit = p.is_gm || match tworld {
                    Some((a, k)) => nd_perm::sections::section_level(false, p.role.as_deref(), a.as_deref(), k.as_deref(), "tables")? == "edit",
                    None => nd_perm::sections::section_level(false, p.role.as_deref(), None, None, "tables")? == "edit",
                };
                edit && (p.is_gm || is_assistant || t.get("created_by_user_id").and_then(|v| v.as_i64()) == Some(p.user_id))
            }
        };
        t.insert("__nd_can_edit".into(), json!(can));
        let cat = t.get("category").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("general").to_string();
        match grouped.iter_mut().find(|(c, _)| *c == cat) {
            Some(g) => g.1.push(Value::Object(t)),
            None => grouped.push((cat, vec![Value::Object(t)])),
        }
    }
    let can_create = edit_level(&p, &world, "tables")?;
    p.ctx.insert("grouped".into(), Value::Object(grouped.into_iter().map(|(k, v)| (k, Value::Array(v))).collect()));
    p.ctx.insert("can_create".into(), json!(can_create));
    render_with(pages, "tables/list.html", p.ctx, headers)
}

/// `GET /combat` (app/routers/combat.py `combat_list`): running fights first, then finished ones, newest first; GM also sees encounter templates
pub fn combat_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let page = page_param(uri)?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !section_ok(&p, p.world.as_ref(), "combat")? {
        return None;
    }
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(1);
    // combat_turns.load: the combatants list, keeping only objects
    let count = |r: &Map<String, Value>| -> usize {
        match r.get("combatants_json").and_then(|v| v.as_str()).map(serde_json::from_str::<Value>) {
            Some(Ok(Value::Array(a))) => a.iter().filter(|c| c.is_object()).count(),
            _ => 0,
        }
    };
    let total: i64 = db.query_row("SELECT count(*) FROM combat_sessions WHERE world_id = ?1 AND is_template IS NOT 1", [wid], |r| r.get(0)).ok()?;
    let total_pages = std::cmp::max(1, (total + 49) / 50);
    let page = page.max(1).min(total_pages);
    let mut st = db
        .prepare("SELECT * FROM combat_sessions WHERE world_id = ?1 AND is_template IS NOT 1 ORDER BY finished_at IS NOT NULL, updated_at DESC LIMIT 50 OFFSET ?2")
        .ok()?;
    let sessions = st.query_map([wid, (page - 1) * 50], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut counts = Map::new();
    for s in &sessions {
        counts.insert(s.get("id")?.as_i64()?.to_string(), json!(count(s)));
    }
    let mut tpl = vec![];
    let mut tcounts = Map::new();
    if p.is_gm {
        let mut st = db.prepare("SELECT * FROM combat_sessions WHERE world_id = ?1 AND is_template IS 1 ORDER BY name").ok()?;
        tpl = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        for t in &tpl {
            tcounts.insert(t.get("id")?.as_i64()?.to_string(), json!(count(t)));
        }
    }
    let mut labels = Map::new();
    for pair in nd_render::app::data().get("combat_rule_labels")?.as_array()? {
        labels.insert(pair.get(0)?.as_str()?.to_string(), pair.get(1)?.clone());
    }
    let is_gm = p.is_gm;
    p.ctx.insert("sessions".into(), Value::Array(sessions.into_iter().map(Value::Object).collect()));
    p.ctx.insert("combatant_counts".into(), json!({"__intkeys__": Value::Object(counts)}));
    p.ctx.insert("page".into(), json!(page));
    p.ctx.insert("total_pages".into(), json!(total_pages));
    p.ctx.insert("is_gm".into(), json!(is_gm));
    p.ctx.insert("enc_templates".into(), Value::Array(tpl.into_iter().map(Value::Object).collect()));
    p.ctx.insert("template_counts".into(), json!({"__intkeys__": Value::Object(tcounts)}));
    p.ctx.insert("rule_labels".into(), Value::Object(labels));
    render_with(pages, "combat/list.html", p.ctx, headers)
}
