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
