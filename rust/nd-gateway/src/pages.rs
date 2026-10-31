//! Pages the gateway builds itself instead of passing them to Python (docs/RUST_PAGES.md). Each one is switched on by name in `ND_RUST_ROUTES`
//! and answers only when it is sure to produce what Python would: anything else (an unexpected method, no usable session key) returns `None`
//! and goes to Python as before.
use crate::{session, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use minijinja::{context, Environment};
use serde_json::{json, Map, Value};
use percent_encoding::{percent_decode_str, utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use std::time::{SystemTime, UNIX_EPOCH};

pub struct Pages {
    pub env: Environment<'static>,
    routes: Vec<String>,
    pub(crate) secret: Option<String>,
}

/// What Starlette keeps unescaped in a redirect's Location (`quote(url, safe=":/%#?=@[]!$&'()*+,;")`).
pub(crate) const LOCATION_SAFE: &AsciiSet = &NON_ALPHANUMERIC.remove(b':').remove(b'/').remove(b'%').remove(b'#').remove(b'?').remove(b'=').remove(b'@').remove(b'[').remove(b']').remove(b'!').remove(b'$').remove(b'&').remove(b'\'').remove(b'(').remove(b')').remove(b'*').remove(b'+').remove(b',').remove(b';').remove(b'-').remove(b'.').remove(b'_').remove(b'~');

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
pub(crate) fn html(status: StatusCode, body: String, request_headers: &HeaderMap) -> Response<Body> {
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
        let mut env = nd_render::environment(&cfg.templates_dir, &cfg.static_dir);
        // templating.py's tpl_fields_json: a sheet template's fields with the system metadata resolved onto them, as
        // json.dumps writes them; a template this port would read differently is a render error (the page goes to Python)
        // Jinja2's `e` (markupsafe's escape, on a safe value a no-op, on text `& < > ' "` as &#34; and friends) - MiniJinja's
        // own also escapes `/` and spells the quotes differently, so the pages differ byte for byte
        env.add_filter("e", |v: minijinja::Value| -> minijinja::Value {
            match v.as_str() {
                Some(t) if !v.is_safe() => minijinja::Value::from_safe_string(nd_render::markupsafe_escape(t)),
                _ => v,
            }
        });
        env.add_filter("tpl_fields_json", |tpl: minijinja::Value| -> Result<String, minijinja::Error> {
            let row: serde_json::Value = serde_json::to_value(&tpl)
                .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, format!("tpl_fields_json: {e}")))?;
            let Some(row) = row.as_object().cloned() else {
                return Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, "tpl_fields_json: not a template"));
            };
            match crate::sheets::Tpl::load(row).and_then(|t| t.enrich_fields()) {
                Some(fields) => Ok(nd_render::py_dumps(&serde_json::Value::Array(fields))),
                None => Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, "tpl_fields_json: port declined")),
            }
        });
        Pages { env, routes: cfg.rust_routes.clone(), secret: cfg.secret_key.clone() }
    }

    fn on(&self, name: &str) -> bool {
        self.routes.iter().any(|r| r == name)
    }

    pub(crate) fn session(&self, headers: &HeaderMap) -> Option<serde_json::Value> {
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
    /// the handler also needs edit rights on `section` (world_can_edit_section)
    pub edit: bool,
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
    Simple { path: "/chronicler", template: "chronicler.html", section: Some("chronicler"), needs_world: false, gm_only: false, edit: false, lists: &[], extra: None },
    Simple { path: "/background-jobs", template: "background_jobs.html", section: Some("background_jobs"), needs_world: true, gm_only: false, edit: false, lists: &[], extra: None },
    Simple { path: "/tools/bulk-edit", template: "bulk_edit.html", section: Some("bulk_edit"), needs_world: false, gm_only: false, edit: false, lists: &[], extra: None },
    Simple { path: "/system", template: "system_monitor.html", section: None, needs_world: false, gm_only: true, edit: false, lists: &[], extra: None },
    Simple { path: "/boards", template: "boards.html", section: Some("boards"), needs_world: true, gm_only: false, edit: false,
             lists: &[("boards", "SELECT * FROM invest_boards WHERE world_id = ?1 ORDER BY rowid")], extra: None },
    Simple { path: "/androidapp", template: "androidapp.html", section: Some("androidapp"), needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(android_url) },
    Simple { path: "/professions/new", template: "profession_new.html", section: None, needs_world: true, gm_only: false, edit: false, lists: &[], extra: None },
    Simple { path: "/races/new", template: "race_new.html", section: None, needs_world: true, gm_only: false, edit: false, lists: &[], extra: None },
    Simple { path: "/boards/new", template: "board_new.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(kinds_and_icons) },
    Simple { path: "/characters/templates/new", template: "characters/template_form.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[],
             extra: Some(|_, c| { c.insert("tpl".into(), serde_json::Value::Null); c.insert("fields".into(), serde_json::json!([])); c.insert("system".into(), serde_json::json!({})); Some(()) }) },
    Simple { path: "/maps/schematic/new", template: "schematic_form.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[],
             extra: Some(|_, c| { c.insert("schematic".into(), serde_json::Value::Null); Some(()) }) },
    Simple { path: "/entity-templates/new", template: "entity_template_form.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[],
             extra: Some(|_, c| { c.insert("tpl".into(), serde_json::Value::Null); c.insert("fields".into(), serde_json::json!([])); Some(()) }) },
    Simple { path: "/tables/new", template: "tables/form.html", section: Some("tables"), needs_world: false, gm_only: false, edit: true, lists: &[],
             extra: Some(|_, c| { c.insert("tbl".into(), serde_json::Value::Null); c.insert("entries".into(), serde_json::json!([])); Some(()) }) },
    Simple { path: "/display", template: "display.html", section: None, needs_world: true, gm_only: true, edit: false, lists: &[], extra: None },
    Simple { path: "/handouts", template: "handouts_gallery.html", section: None, needs_world: false, gm_only: false, edit: false,
             lists: &[("entities", "SELECT * FROM entities WHERE world_id = ?1 ORDER BY kind, name, id")], extra: None },
    Simple { path: "/characters/templates/ai-new", template: "characters/template_ai.html", section: None, needs_world: true, gm_only: true, edit: false, lists: &[],
             extra: Some(|_, c| {
                 let has = c.get("world").and_then(|w| w.get("rules_md")).and_then(|v| v.as_str()).map(|t| !nd_render::text::py_strip(t).is_empty()).unwrap_or(false);
                 c.insert("has_world_rules".into(), serde_json::json!(has));
                 Some(())
             }) },
    Simple { path: "/studio", template: "studio_console.html", section: None, needs_world: true, gm_only: true, edit: false, lists: &[], extra: Some(studio_console) },
    Simple { path: "/dreamlands", template: "dreamlands.html", section: Some("dreamlands"), needs_world: false, gm_only: false, edit: false, lists: &[],
             extra: Some(|db, c| feature_flag(db, c, "dreamlands_enabled", "Dreamlands", "\u{1F319}")) },
    Simple { path: "/king-in-yellow", template: "king_in_yellow.html", section: Some("king-in-yellow"), needs_world: false, gm_only: false, edit: false, lists: &[],
             extra: Some(|db, c| feature_flag(db, c, "king_in_yellow_enabled", "King in Yellow", "\u{1F3AD}")) },
    Simple { path: "/sessions/new", template: "sessions/detail.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(session_new) },
    Simple { path: "/quests/new", template: "quests/detail.html", section: Some("quests"), needs_world: true, gm_only: false, edit: true,
             lists: &[("parties", "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id"), ("quests", "SELECT * FROM quests WHERE world_id = ?1 ORDER BY title, id"),
                      ("entities", "SELECT * FROM entities WHERE world_id = ?1 ORDER BY name, id")],
             extra: Some(|_, c| {
                 let d = nd_render::app::data();
                 c.insert("quest".into(), serde_json::Value::Null);
                 c.insert("linked_entities".into(), serde_json::json!([]));
                 c.insert("statuses".into(), d.get("quest_statuses")?.clone());
                 c.insert("categories".into(), d.get("quest_categories")?.clone());
                 c.insert("can_edit_this".into(), serde_json::json!(true));
                 Some(())
             }) },
    Simple { path: "/editor", template: "editor_embed.html", section: Some("editor"), needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(editor) },
    Simple { path: "/entity-templates", template: "entity_templates_list.html", section: None, needs_world: false, gm_only: false, edit: false,
             lists: &[("tpls", "SELECT * FROM entity_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name, id")], extra: Some(entity_templates) },
    Simple { path: "/characters/templates", template: "characters/templates_list.html", section: None, needs_world: false, gm_only: false, edit: false,
             lists: &[("sheet_templates", "SELECT * FROM sheet_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name, id")], extra: None },
    Simple { path: "/worlds", template: "worlds.html", section: None, needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(worlds_page) },
    Simple { path: "/export", template: "export_hub.html", section: Some("export"), needs_world: false, gm_only: false, edit: false, lists: &[], extra: Some(export_hub) },
    Simple { path: "/maps/new", template: "map_form.html", section: Some("maps"), needs_world: true, gm_only: false, edit: true, lists: &[],
             extra: Some(|db, c| { let wid = world_id_of(c)?; c.insert("gallery_images".into(), serde_json::Value::Array(crate::gallery::all_world_image_urls(db, wid)?)); Some(()) }) },
];

/// `GET /export`: a staged restore waiting for confirmation (its manifest shown on the page) is Python's
fn export_hub(_: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let db_path = std::env::var("DB_PATH").unwrap_or_else(|_| "/data/world.db".into());
    let staged = std::path::Path::new(&db_path).parent()?.join("restore_staging").join("manifest.json");
    if staged.exists() {
        return None;
    }
    let name = match c.get("world") {
        Some(serde_json::Value::Object(w)) => w.get("name")?.clone(),
        _ => serde_json::json!("RESTORE"),
    };
    c.insert("staged_restore_manifest".into(), serde_json::Value::Null);
    c.insert("restore_confirm_name".into(), name);
    Some(())
}

fn worlds_page(db: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let current = c.remove("world")?;
    // each card shows `w.entities|length`: the world's entity rows (their ids stand in for them)
    for w in c.get_mut("worlds")?.as_array_mut()? {
        let w = w.as_object_mut()?;
        let id = w.get("id")?.as_i64()?;
        let mut st = db.prepare("SELECT id FROM entities WHERE world_id = ?1").ok()?;
        let ids = st.query_map([id], |r| r.get::<_, i64>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        w.insert("entities".into(), serde_json::json!(ids));
    }
    c.insert("current".into(), current);
    let mut presets = serde_json::Map::new();
    for pair in nd_render::app::data().get("calendar_preset_labels")?.as_array()? {
        presets.insert(pair.get(0)?.as_str()?.to_string(), pair.get(1)?.clone());
    }
    c.insert("calendar_presets".into(), serde_json::Value::Object(presets));
    Some(())
}

/// a feature switch in app_settings: off = Python's "feature disabled" page
/// An optional lore extra: its page when the Settings switch is on, else `feature_disabled.html` (the handler's own answer)
fn feature_flag(db: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>, col: &str, name: &str, icon: &str) -> Option<()> {
    let on: Option<i64> = db.query_row(&format!("SELECT {col} FROM app_settings LIMIT 1"), [], |r| r.get(0)).ok()?;
    if on != Some(1) {
        c.insert("feature_name".into(), serde_json::json!(name));
        c.insert("feature_icon".into(), serde_json::json!(icon));
        c.insert("__template".into(), serde_json::json!("feature_disabled.html"));
    }
    Some(())
}

/// app/main.py `studio_console` with no Studio set up (no Console URL in ai_models.json, no Studio API key): the page with an empty address.
/// Anything configured needs `studio_browser_url`'s host rewriting: Python's.
fn studio_console(db: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let db_path = std::env::var("DB_PATH").unwrap_or_else(|_| "/data/world.db".into());
    let explicit = match std::fs::read(std::path::Path::new(&db_path).parent()?.join("ai_models.json")) {
        Ok(b) => match serde_json::from_slice::<serde_json::Value>(&b) {
            Ok(serde_json::Value::Object(o)) => o.get("studio_console_url").cloned(),
            Ok(_) => return None,
            Err(_) => None,
        },
        Err(_) => None,
    };
    if !matches!(explicit, None | Some(serde_json::Value::Null)) && explicit.as_ref().and_then(|v| v.as_str()) != Some("") {
        return None;
    }
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    if key.is_some() {
        return None;
    }
    c.insert("studio_url".into(), serde_json::json!(""));
    c.insert("studio_derived_from".into(), serde_json::json!(""));
    Some(())
}

fn world_id_of(c: &serde_json::Map<String, serde_json::Value>) -> Option<i64> {
    c.get("world").and_then(|w| w.get("id")).and_then(|v| v.as_i64())
}

fn session_new(db: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let wid = world_id_of(c).unwrap_or(1);
    let mut st = db.prepare("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let parties = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let last: Option<i64> = db.query_row("SELECT session_num FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 1", [wid], |r| r.get(0)).ok().flatten();
    let next = match db.query_row("SELECT count(*) FROM game_sessions WHERE world_id = ?1", [wid], |r| r.get::<_, i64>(0)).ok()? {
        0 => 1,
        _ => last? + 1,
    };
    c.insert("gsession".into(), serde_json::Value::Null);
    c.insert("parties".into(), serde_json::Value::Array(parties.into_iter().map(serde_json::Value::Object).collect()));
    c.insert("next_num".into(), serde_json::json!(next));
    c.insert("linked_combats".into(), serde_json::json!([]));
    Some(())
}

fn editor(db: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let saved: Option<String> = db.query_row("SELECT editor_external_url FROM app_settings LIMIT 1", [], |r| r.get(0)).ok()?;
    let env = std::env::var("EDITOR_EXTERNAL_URL").unwrap_or_default();
    let url = match saved.filter(|s| !s.is_empty()) {
        Some(s) => s,
        None => env.trim_end_matches('/').to_string(),
    };
    c.insert("editor_url".into(), serde_json::json!(url.trim_end_matches('/')));
    kinds_and_icons(db, c)
}

fn entity_templates(_: &rusqlite::Connection, c: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    let mut m = serde_json::Map::new();
    for t in c.get("tpls")?.as_array()? {
        let raw = t.get("fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
        m.insert(t.get("id")?.as_i64()?.to_string(), serde_json::from_str(raw).ok()?);
    }
    c.insert("tpl_fields_map".into(), serde_json::json!({"__intkeys__": serde_json::Value::Object(m)}));
    Some(())
}

/// The handler's KINDS / KIND_ICONS: the context processor's world-aware `kinds` / `kind_icons` (already in the layout context) replace
/// them - Starlette applies context processors after the handler's own context
fn kinds_and_icons(_: &rusqlite::Connection, _: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    Some(())
}

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
    if let (Some(section), true, false) = (page.section, page.edit, p.is_gm) {
        let w = p.world.as_ref();
        let s = |k: &str| w.and_then(|w| w.get(k)).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), section)? != "edit" {
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
    let template = match p.ctx.remove("__template") {
        Some(serde_json::Value::String(t)) => std::borrow::Cow::Owned(t),
        _ => std::borrow::Cow::Borrowed(page.template),
    };
    render_with(pages, &template, p.ctx, headers)
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
pub(crate) fn py_str_value(v: &serde_json::Value) -> Option<String> {
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
    let names = autolink_names(&db, ewid, Some(id), &vis)?;
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
        let html_note = truthy_json(n.get("content_is_html"));
        if html_note && !crate::config::python_free() {
            return None; // html2text is Python's
        }
        let mut c = n.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if html_note {
            // no Python to run html2text: the note's plain text, for the AI panel's context only
            c = crate::sheet_import::html_to_sheet_text(&c);
        }
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

pub(crate) fn section_ok(p: &crate::layout::PageCtx, world: Option<&serde_json::Map<String, serde_json::Value>>, section: &str) -> Option<bool> {
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
    let mut st = db.prepare("SELECT *, coalesce(live_transcript, '') != '' AS has_live_transcript FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 50 OFFSET ?2").ok()?;
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

/// `media_items(gs)` reduced to what the lists need: (images as (visible), video clip ids), the first 50 kept items; None for JSON Python
/// would read its own way
fn session_media_kinds(gs: &serde_json::Map<String, serde_json::Value>) -> Option<(Vec<bool>, Vec<i64>)> {
    use serde_json::Value;
    let raw = gs.get("recap_media_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let list = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => a,
        Ok(_) => vec![],
        Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
        Err(_) => vec![],
    };
    let (mut images, mut videos, mut seen_urls, mut kept) = (vec![], vec![], Vec::<String>::new(), 0usize);
    for it in &list {
        if kept >= 50 {
            break;
        }
        let Some(o) = it.as_object() else { continue };
        match o.get("kind").and_then(|k| k.as_str()) {
            Some("image") => {
                let Some(url) = o.get("url").and_then(|u| u.as_str()).filter(|u| u.starts_with("/uploads/")) else { continue };
                if seen_urls.iter().any(|s| s == url) {
                    continue;
                }
                seen_urls.push(url.to_string());
                images.push(crate::edits::py_truthy_pub(o.get("visible")));
                kept += 1;
            }
            Some("video") => {
                let Some(cid) = o.get("clip_id").filter(|v| v.is_i64()).and_then(|v| v.as_i64()) else { continue };
                if videos.contains(&cid) {
                    continue;
                }
                videos.push(cid);
                kept += 1;
            }
            _ => {}
        }
    }
    Some((images, videos))
}

/// `GET /session-log` (app/routers/sessions.py `session_log_list`): the world's sessions newest first, with their recap marker and
/// whether the viewer has a song / read-aloud or a picture / video to open
pub fn session_log_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "session_log")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(1);
    let mut st = db.prepare("SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC").ok()?;
    let sessions = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let id_of = |s: &Map<String, Value>| s.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
    let mut fact_ids: Vec<i64> = vec![];
    if !sessions.is_empty() {
        let list = sessions.iter().map(|s| id_of(s).to_string()).collect::<Vec<_>>().join(",");
        let mut st = db.prepare(&format!("SELECT DISTINCT game_session_id FROM facts WHERE world_id = ?1 AND game_session_id IN ({list})")).ok()?;
        fact_ids = st.query_map([wid], |r| r.get::<_, Option<i64>>(0)).ok()?.filter_map(|r| r.ok().flatten()).collect();
    }
    let mut markers = Map::new();
    for s in &sessions {
        let published = crate::edits::py_truthy_pub(s.get("player_summary_published"))
            && !nd_render::text::py_strip(s.get("player_summary").and_then(|v| v.as_str()).unwrap_or("")).is_empty();
        let m = if published { json!("published") } else if fact_ids.contains(&id_of(s)) { json!("auto") } else { Value::Null };
        markers.insert(id_of(s).to_string(), m);
    }
    let gm = p.is_gm;
    let visible_ids = |table: &str, ids: &[i64]| -> Option<Vec<i64>> {
        if ids.is_empty() {
            return Some(vec![]);
        }
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let vis = if gm && table == "video_clips" { "" } else { " AND visible_to_players = 1" };
        let mut st = db.prepare(&format!("SELECT id FROM {table} WHERE id IN ({list}){vis}")).ok()?;
        let r = st.query_map([], |r| r.get::<_, i64>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    // songs / read-alouds: a GM any attached clip, a player one that is visible to players
    let clip_lists: Vec<(i64, Vec<i64>)> = sessions.iter().map(|s| Some((id_of(s), crate::edits::sm_clip_ids(s)?))).collect::<Option<_>>()?;
    let audio: Vec<i64> = if gm {
        clip_lists.iter().filter(|(_, c)| !c.is_empty()).map(|(i, _)| *i).collect()
    } else {
        let all: Vec<i64> = clip_lists.iter().flat_map(|(_, c)| c.clone()).collect();
        let audible = visible_ids("audio_clips", &all)?;
        clip_lists.iter().filter(|(_, c)| c.iter().any(|x| audible.contains(x))).map(|(i, _)| *i).collect()
    };
    let mut media: Vec<i64> = vec![];
    let mut videos: Vec<(i64, Vec<i64>)> = vec![];
    for s in &sessions {
        let (images, vids) = session_media_kinds(s)?;
        if images.iter().any(|v| gm || *v) {
            media.push(id_of(s));
        }
        if !vids.is_empty() {
            videos.push((id_of(s), vids));
        }
    }
    let all: Vec<i64> = videos.iter().flat_map(|(_, v)| v.clone()).collect();
    let watchable = visible_ids("video_clips", &all)?;
    for (sid, v) in &videos {
        if v.iter().any(|x| watchable.contains(x)) && !media.contains(sid) {
            media.push(*sid);
        }
    }
    p.ctx.insert("sessions".into(), Value::Array(sessions.into_iter().map(Value::Object).collect()));
    p.ctx.insert("recap_markers".into(), json!({"__intkeys__": Value::Object(markers)}));
    p.ctx.insert("audio_session_ids".into(), json!(audio));
    p.ctx.insert("media_session_ids".into(), json!(media));
    render_with(pages, "sessions/player_list.html", p.ctx, headers)
}

/// `GET /npc-talk` (app/routers/npc_talk.py `npc_talk_page`): the characters, creatures and organizations the viewer may see to talk
/// to, and the one `?entity=` picks; a player only when the world lets players ask the AI
pub fn npc_talk_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let pick: i64 = match query_param(uri, "entity") {
        None => 0,
        Some(v) => v.trim().parse().ok()?, // int() of anything else is pydantic's 422: Python's
    };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        let open = p.world.as_ref().is_some_and(|w| crate::edits::py_truthy_pub(w.get("players_can_ask_ai")) || crate::edits::py_truthy_pub(w.get("players_can_use_ai_chat")));
        if !open {
            return Some(crate::idpages::refusal(403));
        }
    }
    let Some(world) = p.world.clone() else {
        let mut r = Response::builder().status(StatusCode::TEMPORARY_REDIRECT).header(header::LOCATION, "/worlds").body(Body::empty()).ok()?;
        r.headers_mut().insert("x-nd-served-by", axum::http::HeaderValue::from_static("gateway"));
        return Some(r);
    };
    let wid = world.get("id")?.as_i64()?;
    let vis = if p.is_gm { String::new() } else {
        format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let mut st = db.prepare(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind IN ('character', 'creature', 'organization'){vis}")).ok()?;
    let mut rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let order = |k: &str| match k { "character" => 0, "creature" => 1, "organization" => 2, _ => 3 };
    rows.sort_by_key(|e| (order(e.get("kind").and_then(|v| v.as_str()).unwrap_or("")), e.get("name").and_then(|v| v.as_str()).unwrap_or("").to_lowercase()));
    let selected = if pick != 0 { rows.iter().find(|e| e.get("id").and_then(|v| v.as_i64()) == Some(pick)).cloned().map(Value::Object) } else { None };
    p.ctx.insert("npcs".into(), Value::Array(rows.into_iter().map(Value::Object).collect()));
    p.ctx.insert("selected".into(), selected.unwrap_or(Value::Null));
    p.ctx.insert("is_gm".into(), json!(p.is_gm));
    render_with(pages, "npc_talk.html", p.ctx, headers)
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

/// `GET /media-rename[?kind=&album=]` (app/routers/media_rename.py `media_rename_page`): the AI renamer's page for a viewer who may edit
/// audio, video or images. No world, or nothing to edit, is Python's 404 / 403.
pub fn media_rename_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let w = p.world.clone()?;
    let wid = w.get("id")?.as_i64()?;
    let mut editable = vec![];
    for (kind, section) in [("audio", "audio"), ("video", "video"), ("image", "images")] {
        if edit_level(&p, &w, section)? {
            editable.push(kind);
        }
    }
    let first = *editable.first()?;
    let kind = query_param(uri, "kind");
    let album = match query_param(uri, "album").unwrap_or_default().as_str() {
        "" => 0,
        t => match crate::form::py_int(t) {
            Some(n) => n,
            None if t.bytes().all(|c| c.is_ascii_alphabetic() || c == b'.') => 0,
            None => return None,
        },
    };
    let kind = match kind.as_deref() {
        Some(k) if editable.contains(&k) => k.to_string(),
        _ => first.to_string(),
    };
    let key = db.query_row("SELECT llm_api_key FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)).ok().flatten().filter(|k| !k.is_empty())
        .or_else(|| std::env::var("UNSLOTH_API_KEY").ok().filter(|k| !k.is_empty()));
    let presets: Map<String, Value> = crate::media_rename::STYLE_PRESETS.iter().map(|(k, v)| (k.to_string(), json!(v))).collect();
    let mut ctx = p.ctx;
    ctx.insert("start".into(), json!({"kind": kind, "album": album, "kinds": editable}));
    ctx.insert("presets".into(), Value::Object(presets));
    ctx.insert("ai_ready".into(), json!(key.is_some()));
    ctx.insert("history".into(), Value::Array(crate::apis::media_rename_batches(&db, wid)?));
    render_with(pages, "media_rename/index.html", ctx, headers)
}

/// `POST /handouts/print` (app/routers/handouts.py): the chosen entities of the active world as printable handouts, in the order asked (GM only; an
/// id that is not this world's is skipped). A body Python cannot read (its 500) is not ours.
pub fn handouts_print(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "POST", uri, headers, now)?;
    if !p.is_gm {
        return None;
    }
    let Some(wid) = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) else {
        return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, "No active world"));
    };
    if !crate::edits::plain_numbers(body) {
        return None;
    }
    let Value::Object(parsed) = serde_json::from_slice::<Value>(body).ok()? else { return None };
    let ids: Vec<i64> = match parsed.get("ids") {
        None => vec![],
        Some(Value::Array(a)) => a.iter().map(|x| match x {
            Value::Number(n) if n.is_i64() => n.as_i64(),
            Value::String(s) => crate::form::py_int(nd_render::text::py_strip(s)),
            _ => None,
        }).collect::<Option<Vec<_>>>()?,
        Some(_) => return None,
    };
    let mut items = vec![];
    for id in ids {
        let e = db.query_row("SELECT * FROM entities WHERE id = ?1 AND world_id = ?2", [id, wid], |r| crate::layout::row_map(r)).ok();
        if let Some(e) = e {
            let text = match e.get("body") { None | Some(Value::Null) => String::new(), Some(Value::String(s)) => s.clone(), _ => return None };
            let html = if text.is_empty() { String::new() } else { nd_render::md::render_md(&text) };
            items.push(json!({"entity": e, "body_html": html}));
        }
    }
    let mut ctx = p.ctx;
    ctx.insert("entities".into(), Value::Array(items));
    render_with(pages, "handout.html", ctx, headers)
}

pub(crate) fn edit_level(p: &crate::layout::PageCtx, world: &serde_json::Map<String, serde_json::Value>, section: &str) -> Option<bool> {
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
        let n = party_member_count(&db, &p, party, wid)?;
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
    let mut facts = rows(&format!("SELECT * FROM facts WHERE world_id = ?1{vis} ORDER BY created_at ASC, id ASC"))?;
    // GET /facts/fragment?keys=3,7,none: just those groups ("none" = no session, or a session that is gone)
    let fragment = uri.path() == "/facts/fragment";
    if fragment {
        let keys = query_param(uri, "keys").unwrap_or_default();
        let (mut ids, mut none) = (vec![], false);
        for k in keys.split(',') {
            if !k.is_ascii() {
                return None; // str.isdigit / int() over other scripts' digits: Python's call
            }
            let k = k.trim_matches(|c: char| c.is_ascii_whitespace() || ('\x1c'..='\x1f').contains(&c));
            if k == "none" {
                none = true;
            } else if !k.is_empty() && k.bytes().all(|b| b.is_ascii_digit()) {
                ids.push(k.parse::<i64>().ok()?);
            }
        }
        let session_ids: Vec<i64> = sessions.iter().filter_map(|s| s.get("id").and_then(|v| v.as_i64())).collect();
        facts.retain(|f| match f.get("game_session_id") {
            None | Some(Value::Null) => none,
            Some(v) => {
                let sid = v.as_i64();
                sid.map_or(false, |x| ids.contains(&x)) || (none && !sid.map_or(false, |x| session_ids.contains(&x)))
            }
        });
    }
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
    render_with(pages, if fragment { "facts/_fragment.html" } else { "facts/list.html" }, p.ctx, headers)
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
    let mut grouped: Vec<(String, Vec<Value>)> = vec![];
    for mut t in tables {
        let can = can_edit_table_row(&db, &p, &t)?;
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

/// `_can_edit_table_row` (app/routers/tables.py): a global table by GM / assistant, a world's table by its section's edit rights and
/// ownership (a player edits only the tables they made)
pub(crate) fn can_edit_table_row(db: &rusqlite::Connection, p: &crate::layout::PageCtx, t: &serde_json::Map<String, serde_json::Value>) -> Option<bool> {
    let is_assistant = p.role.as_deref() == Some("assistant");
    Some(match t.get("world_id").and_then(|v| v.as_i64()) {
        None => p.is_gm || is_assistant,
        Some(tw) => {
            let tworld = db
                .query_row("SELECT section_access_json, custom_kinds_json FROM worlds WHERE id = ?1", [tw], |r| {
                    Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))
                })
                .ok();
            let edit = p.is_gm
                || match tworld {
                    Some((a, k)) => nd_perm::sections::section_level(false, p.role.as_deref(), a.as_deref(), k.as_deref(), "tables")? == "edit",
                    None => nd_perm::sections::section_level(false, p.role.as_deref(), None, None, "tables")? == "edit",
                };
            edit && (p.is_gm || is_assistant || t.get("created_by_user_id").and_then(|v| v.as_i64()) == Some(p.user_id))
        }
    })
}

/// `_party_visible_member_count`: characters plus companions, a non-GM counting only companions they may see (once each)
pub(crate) fn party_member_count(db: &rusqlite::Connection, p: &crate::layout::PageCtx, party: &serde_json::Map<String, serde_json::Value>, wid: i64) -> Option<usize> {
    use serde_json::Value;
    let arr = |k: &str| -> Option<Vec<Value>> {
        match party.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            None => Some(vec![]),
            Some(s) => serde_json::from_str::<Value>(s).ok()?.as_array().cloned(),
        }
    };
    let pcs = arr("member_pc_ids_json")?;
    let ents = arr("member_entity_ids_json")?;
    if p.is_gm {
        return Some(pcs.len() + ents.len());
    }
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
    Some(pcs.len() + seen.len())
}

/// `GET /cockpit` (app/routers/cockpit.py): the GM's table workspace - the pickers of its Add-panel modal (the world's drawn maps, its
/// file maps, its parties); the panels themselves load in the browser
pub fn cockpit(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?; // no world: Python's 404
    cockpit_pickers(&db, cfg, wid, &mut p.ctx)?;
    let mut st = db.prepare("SELECT id, name FROM parties WHERE world_id = ?1 ORDER BY name").ok()?;
    let mut parties = vec![];
    for r in st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()? {
        let (id, name) = r.ok()?;
        parties.push(json!({"id": id, "name": name}));
    }
    p.ctx.insert("parties_json".into(), Value::Array(parties));
    render_with(pages, "cockpit.html", p.ctx, headers)
}

/// What both cockpits' Add-panel modal picks from: the world's drawn maps (not uploaded HTML ones) and its file maps, plus the caps
fn cockpit_pickers(db: &rusqlite::Connection, cfg: &Config, wid: i64, ctx: &mut serde_json::Map<String, serde_json::Value>) -> Option<()> {
    use serde_json::{json, Value};
    let mut maps = vec![];
    let mut st = db.prepare("SELECT slug, name FROM schematics WHERE world_id = ?1 AND is_html = 0 ORDER BY name").ok()?;
    for r in st.query_map([wid], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))).ok()? {
        let (slug, name) = r.ok()?;
        maps.push(json!({"slug": slug, "name": name}));
    }
    // _world_maps
    let mut world_maps = vec![];
    for (slug, data) in crate::maps::world_maps(cfg, wid)? {
        let name = match data.get("name") {
            Some(Value::String(t)) if !t.is_empty() => t.clone(),
            None | Some(Value::Null) | Some(Value::String(_)) => slug.clone(),
            _ => return None, // str() of another value: Python's
        };
        let markers = data.get("markers").and_then(|m| m.as_array()).map_or(0, |a| a.len());
        world_maps.push(json!({"slug": slug, "name": name.chars().take(120).collect::<String>(), "markers": markers}));
    }
    ctx.insert("max_panels".into(), json!(24));
    ctx.insert("max_presets".into(), json!(12));
    ctx.insert("maps_json".into(), Value::Array(maps));
    ctx.insert("world_maps_json".into(), Value::Array(world_maps));
    Some(())
}

/// The second screen (app/routers/display.py, GM only): `/display`, `/display/map/<slug>` (a live map for the table's TV) and
/// `/display/combat/<id>` (a fight's turn order) - bare pages whose data arrives over the live bus
pub fn display_pages(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !p.is_gm {
        return None; // Python's 403
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let path = uri.path();
    let template = if path == "/display" {
        "display.html"
    } else if let Some(slug) = path.strip_prefix("/display/map/").filter(|s| !s.is_empty() && !s.contains('/')) {
        let sc = db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
        let truthy = |v: Option<&Value>| v.is_some_and(|v| !matches!(v, Value::Null | Value::Bool(false)) && v != &json!(0) && v != &json!(""));
        if sc.get("world_id")?.as_i64()? != wid || (truthy(sc.get("is_html")) && truthy(sc.get("html_file"))) {
            return None;
        }
        p.ctx.insert("schematic".into(), Value::Object(sc));
        "schematic_tv.html"
    } else {
        let id = path.strip_prefix("/display/combat/").filter(|s| !s.is_empty() && s.len() <= 15 && s.bytes().all(|b| b.is_ascii_digit()))?;
        let id: i64 = id.parse().ok()?;
        let (world, name, template): (i64, Option<String>, Option<i64>) =
            db.query_row("SELECT world_id, name, is_template FROM combat_sessions WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?;
        if world != wid || template.unwrap_or(0) != 0 {
            return None;
        }
        let title: String = name.map(|n| nd_render::text::py_strip(&n).chars().take(200).collect()).unwrap_or_default();
        p.ctx.insert("combat_id".into(), json!(id));
        p.ctx.insert("title".into(), json!(title));
        "display_combat.html"
    };
    render_with(pages, template, p.ctx, headers)
}

/// `GET /player-cockpit` (app/routers/cockpit.py): the cockpit in player mode - the viewer's parties with their vitals, their characters
pub fn player_cockpit(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let pc = match query_param(uri, "pc") {
        None => 0,
        Some(t) if t.is_empty() => 0,
        Some(t) if t.len() <= 15 && t.bytes().all(|b| b.is_ascii_digit()) => t.parse::<i64>().ok()?,
        Some(t) if t.is_ascii() => 0, // not digits: Python's `pc.isdigit()` is false
        Some(_) => return None,      // Unicode digits isdigit() takes
    };
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let (parties, my_pcs, focus, view_as) = crate::edits::player_cockpit_data(&db, wid, p.user_id, p.is_gm, pc)?;
    cockpit_pickers(&db, cfg, wid, &mut p.ctx)?;
    p.ctx.insert("parties_json".into(), Value::Array(parties));
    p.ctx.insert("my_pcs_json".into(), Value::Array(my_pcs));
    p.ctx.insert("focus_pc_id".into(), json!(focus));
    p.ctx.insert("view_as".into(), json!(view_as));
    p.ctx.insert("exit_href".into(), json!(match focus { Some(f) => format!("/characters/{f}"), None => "/characters".into() }));
    p.ctx.insert("player_mode".into(), json!(true));
    render_with(pages, "cockpit.html", p.ctx, headers)
}

/// `/maps/schematic/<slug>/view` (the players' live map), its `view.json`, and the TV's `/display/map/<slug>/data.json` - for a map
/// without strict secrecy (that one records what the party has seen: Python's)
pub fn schematic_views(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let path = uri.path();
    let (slug, kind) = if let Some(s) = path.strip_prefix("/display/map/").and_then(|s| s.strip_suffix("/data.json")) {
        (s, "tv")
    } else if let Some(s) = path.strip_prefix("/maps/schematic/").and_then(|s| s.strip_suffix("/view.json")) {
        (s, "json")
    } else {
        (path.strip_prefix("/maps/schematic/")?.strip_suffix("/view")?, "page")
    };
    if slug.is_empty() || slug.contains('/') {
        return None;
    }
    if kind == "tv" && !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let sc = db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    let truthy = |v: Option<&Value>| v.is_some_and(|v| !matches!(v, Value::Null | Value::Bool(false)) && v != &json!(0) && v != &json!(""));
    if sc.get("world_id")?.as_i64()? != wid || (truthy(sc.get("is_html")) && truthy(sc.get("html_file"))) {
        return None;
    }
    let scene = crate::edits::player_scene(&db, &sc, (kind != "tv").then_some((p.user_id, p.is_gm)))?;
    let bg = |sc: &serde_json::Map<String, Value>| {
        match sc.get("canvas_bg").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("dark") {
            "dark" => "#111111", "blueprint" => "#0d1b2a", "grid-light" => "#1a1a2e", "light" => "#f0f0f0", _ => "#111111",
        }
    };
    let dim = |k: &str, d: i64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(d);
    if kind == "tv" {
        let raw = sc.get("grid_config_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
        // json.loads(...) or {} when it is not an object (a NaN serde cannot read: Python's)
        let grid = match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(o)) => Value::Object(o),
            Ok(_) => json!({}),
            Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
            Err(_) => json!({}),
        };
        let image = crate::edits::safe_image(scene.image_url.as_deref().unwrap_or(""));
        let mut out = serde_json::Map::new();
        out.insert("elements".into(), Value::Array(scene.visible));
        out.insert("image_url".into(), if image.is_empty() { Value::Null } else { json!(image) });
        out.insert("canvas".into(), json!({"w": dim("canvas_width", 2000), "h": dim("canvas_height", 1500), "bg": bg(&sc)}));
        out.insert("grid_type".into(), json!(sc.get("grid_type").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("none")));
        out.insert("grid_config".into(), grid);
        out.extend(scene.fog_part);
        return crate::edits::json_response(&Value::Object(out));
    }
    // _party_pins_for(world, "schematic", slug)
    let mut st = db.prepare("SELECT * FROM parties WHERE world_id = ?1 ORDER BY id").ok()?;
    let parties = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut pins = vec![];
    for party in &parties {
        let raw = party.get("location_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
        let Value::Object(loc) = serde_json::from_str::<Value>(raw).ok()? else { return None };
        if loc.get("kind").and_then(|v| v.as_str()) != Some("schematic") || loc.get("slug").and_then(|v| v.as_str()) != Some(slug) {
            continue;
        }
        pins.push(json!({"id": party.get("id")?, "name": party.get("name")?, "member_count": party_member_count(&db, &p, party, wid)?,
                         "x": loc.get("x").cloned().unwrap_or(Value::Null), "y": loc.get("y").cloned().unwrap_or(Value::Null)}));
    }
    if kind == "json" {
        let mut out = serde_json::Map::new();
        out.insert("elements".into(), Value::Array(scene.visible));
        out.insert("image_url".into(), json!(scene.image_url));
        out.insert("party_pins".into(), Value::Array(pins));
        out.insert("own_pc_id".into(), scene.own_pc_id);
        out.insert("own_pc_ids".into(), json!(scene.own_pc_ids));
        out.insert("own_pc_currency".into(), scene.own_pc_currency);
        out.insert("combat_active_combatant_id".into(), scene.active_combatant_id);
        out.insert("combat_round".into(), scene.combat_round);
        out.extend(scene.fog_part);
        return crate::edits::json_response(&Value::Object(out));
    }
    let c = &mut p.ctx;
    c.insert("elements_json".into(), json!(nd_render::py_dumps(&Value::Array(scene.visible))));
    c.insert("canvas_bg_color".into(), json!(bg(&sc)));
    c.insert("party_pins_json".into(), json!(nd_render::py_dumps(&Value::Array(pins))));
    c.insert("grid_type".into(), json!(sc.get("grid_type").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("none")));
    c.insert("grid_config_json".into(), json!(sc.get("grid_config_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")));
    c.insert("own_pc_id".into(), scene.own_pc_id);
    c.insert("own_pc_ids".into(), json!(scene.own_pc_ids));
    c.insert("own_pc_currency_json".into(), json!(nd_render::py_dumps(&scene.own_pc_currency)));
    c.insert("combat_active_combatant_id".into(), scene.active_combatant_id);
    c.insert("combat_round".into(), scene.combat_round);
    c.insert("fog_json".into(), json!(nd_render::py_dumps(&Value::Object(scene.fog_part))));
    c.insert("bg_image_url".into(), json!(scene.image_url));
    c.insert("schematic".into(), Value::Object(sc));
    render_with(pages, "schematic_view.html", p.ctx, headers)
}

/// `GET /maps/schematic/<slug>` - the GM's (or an Assistant's) map editor, for a drawn map of the active world (an uploaded HTML map
/// is served from its file: Python's)
pub fn schematic_editor(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let slug = uri.path().strip_prefix("/maps/schematic/").filter(|s| !s.is_empty() && !s.contains('/'))?;
    if slug == "new" {
        return None;
    }
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if !(p.is_gm || p.role.as_deref() == Some("assistant")) {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let sc = db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    let truthy = |v: Option<&Value>| v.is_some_and(|v| !matches!(v, Value::Null | Value::Bool(false)) && v != &json!(0) && v != &json!(""));
    if sc.get("world_id")?.as_i64()? != wid || truthy(sc.get("is_html")) {
        return None;
    }
    let raw = sc.get("elements_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    let elements: Value = serde_json::from_str(raw).ok()?;
    if nd_render::py_dumps(&elements) != raw && raw != "[]" {
        return None; // json.dumps(json.loads(...)) would respell it: Python's
    }
    let (pcs, ents) = crate::edits::combat_candidates(&db, wid)?;
    let rows = |sql: &str| -> Option<Vec<serde_json::Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let items: Vec<Value> = rows("SELECT * FROM entities WHERE world_id = ?1 AND kind = 'item' ORDER BY name")?.iter().map(|e| json!({
        "id": e.get("id"), "name": e.get("name"), "summary": e.get("summary").and_then(|v| v.as_str()).unwrap_or(""),
        "image_url": e.get("image_url").and_then(|v| v.as_str()).unwrap_or("")})).collect();
    let fights: Vec<Value> = rows("SELECT * FROM combat_sessions WHERE world_id = ?1 AND (is_template IS NULL OR is_template != 1) ORDER BY updated_at DESC")?
        .into_iter().map(Value::Object).collect();
    let (mut linked, mut active, mut round) = (Value::Null, Value::Null, Value::Null);
    if let Some(cs_id) = sc.get("combat_session_id").and_then(|v| v.as_i64()).filter(|i| *i != 0) {
        if let Some((cs, a, r)) = crate::edits::fight_now(&db, cs_id)? {
            (linked, active, round) = (Value::Object(cs), a, r);
        }
    }
    let mut parties_payload = vec![];
    let mut pins = vec![];
    let mut parties = rows("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name")?;
    for party in &parties {
        parties_payload.push(json!({"id": party.get("id")?, "name": party.get("name")?, "member_count": party_member_count(&db, &p, party, wid)?}));
    }
    parties.sort_by_key(|x| x.get("id").and_then(|v| v.as_i64()));
    for party in &parties {
        let raw = party.get("location_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
        let Value::Object(loc) = serde_json::from_str::<Value>(raw).ok()? else { return None };
        if loc.get("kind").and_then(|v| v.as_str()) != Some("schematic") || loc.get("slug").and_then(|v| v.as_str()) != Some(slug) {
            continue;
        }
        pins.push(json!({"id": party.get("id")?, "name": party.get("name")?, "member_count": party_member_count(&db, &p, party, wid)?,
                         "x": loc.get("x").cloned().unwrap_or(Value::Null), "y": loc.get("y").cloned().unwrap_or(Value::Null)}));
    }
    let bg = match sc.get("canvas_bg").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("dark") {
        "blueprint" => "#0d1b2a", "grid-light" => "#1a1a2e", "light" => "#f0f0f0", _ => "#111111",
    };
    let gallery = crate::gallery::all_world_image_urls(&db, wid)?;
    let c = &mut p.ctx;
    c.insert("elements_json".into(), json!(nd_render::py_dumps(&elements)));
    c.insert("canvas_bg_color".into(), json!(bg));
    c.insert("world_parties_json".into(), json!(nd_render::py_dumps(&Value::Array(parties_payload))));
    c.insert("party_pins_json".into(), json!(nd_render::py_dumps(&Value::Array(pins))));
    c.insert("grid_type".into(), json!(sc.get("grid_type").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("none")));
    c.insert("grid_config_json".into(), json!(sc.get("grid_config_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")));
    c.insert("pc_payload_json".into(), json!(nd_render::py_dumps(&Value::Array(pcs))));
    c.insert("entity_payload_json".into(), json!(nd_render::py_dumps(&Value::Array(ents))));
    c.insert("item_payload_json".into(), json!(nd_render::py_dumps(&Value::Array(items))));
    c.insert("combat_sessions".into(), Value::Array(fights));
    c.insert("linked_combat".into(), linked);
    c.insert("combat_active_combatant_id".into(), active);
    c.insert("combat_round".into(), round);
    c.insert("gallery_images".into(), Value::Array(gallery));
    c.insert("schematic".into(), Value::Object(sc));
    render_with(pages, "schematic.html", p.ctx, headers)
}

/// `GET /combat/<id>` for the GM: the tracker (a player's read-only view, an encounter of another world or one without a ruleset - which
/// Python settles and saves on the visit - stay Python's)
pub fn combat_tracker(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let id = uri.path().strip_prefix("/combat/").filter(|s| !s.is_empty() && s.len() <= 15 && s.bytes().all(|b| b.is_ascii_digit()))?;
    let id: i64 = id.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let cs = db.query_row("SELECT * FROM combat_sessions WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok()?;
    if cs.get("world_id")?.as_i64()? != wid {
        return None;
    }
    if !p.is_gm {
        // the read-only view, built from the sanitized state; the Combat section must be open to the viewer's role
        let w = p.world.as_ref()?;
        let level = nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "combat")?;
        if level == "none" || cs.get("is_template").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
            return None;
        }
        let state = crate::edits::combat_player_state(&cs)?;
        let rules = nd_render::app::data().get("combat_player_rules_json")?.as_str()?.to_string();
        p.ctx.insert("state_json".into(), json!(nd_render::py_dumps(&state)));
        p.ctx.insert("rules_json".into(), json!(rules));
        p.ctx.insert("combat".into(), Value::Object(cs));
        return render_with(pages, "combat/player.html", p.ctx, headers);
    }
    if cs.get("ruleset").and_then(|v| v.as_str()).map_or(true, |r| r.is_empty()) {
        return None;
    }
    let state = crate::edits::combat_full_state(&cs)?;
    let (pcs, ents) = crate::edits::combat_candidates(&db, wid)?;
    let mut st = db.prepare("SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC").ok()?;
    let sessions: Vec<serde_json::Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    let linked = match cs.get("game_session_id").and_then(|v| v.as_i64()).filter(|i| *i != 0) {
        Some(g) => db.query_row("SELECT * FROM game_sessions WHERE id = ?1", [g], |r| crate::layout::row_map(r)).ok().map_or(Value::Null, Value::Object),
        None => Value::Null,
    };
    let rules = nd_render::app::data().get("combat_rules_json")?.as_str()?.to_string();
    let c = &mut p.ctx;
    c.insert("combatants_json".into(), json!(nd_render::py_dumps(&state["combatants"])));
    c.insert("state_json".into(), json!(nd_render::py_dumps(&state)));
    c.insert("rules_json".into(), json!(rules));
    c.insert("pc_payload_json".into(), json!(nd_render::py_dumps(&Value::Array(pcs))));
    c.insert("entity_payload_json".into(), json!(nd_render::py_dumps(&Value::Array(ents))));
    c.insert("game_sessions".into(), Value::Array(sessions.into_iter().map(Value::Object).collect()));
    c.insert("linked_session".into(), linked);
    c.insert("combat".into(), Value::Object(cs));
    render_with(pages, "combat/detail.html", p.ctx, headers)
}

/// `GET /api/combat/<id>/state` (the GM's full state, a player's sanitized one) and `/api/display/combat/<id>/state` (always the player
/// view - the table's TV), for an encounter of the active world
pub fn combat_state_json(_pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::Value;
    let path = uri.path();
    let (tv, rest) = match path.strip_prefix("/api/display/combat/") {
        Some(r) => (true, r),
        None => (false, path.strip_prefix("/api/combat/")?),
    };
    let id = rest.strip_suffix("/state").filter(|s| !s.is_empty() && s.len() <= 15 && s.bytes().all(|b| b.is_ascii_digit()))?;
    let id: i64 = id.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    if tv && !p.is_gm {
        return None;
    }
    let w = p.world.as_ref()?;
    let cs = db.query_row("SELECT * FROM combat_sessions WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok()?;
    let template = cs.get("is_template").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    if cs.get("world_id")?.as_i64()? != w.get("id")?.as_i64()? || (template && (tv || !p.is_gm)) {
        return None;
    }
    if !p.is_gm {
        let level = nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "combat")?;
        if level == "none" {
            return None;
        }
    }
    let state: Value = if p.is_gm && !tv { crate::edits::combat_full_state(&cs)? } else { crate::edits::combat_player_state(&cs)? };
    crate::edits::json_response(&state)
}

/// `GET /characters/<id>` for an N&D character without a sheet template, in the viewer's active world (the sheet, the roll sheet and the
/// player hub's Journey tab); a custom sheet system is Python's
pub fn character_sheet(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let id = uri.path().strip_prefix("/characters/").filter(|s| !s.is_empty() && s.len() <= 15 && s.bytes().all(|b| b.is_ascii_digit()))?;
    let id: i64 = id.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok()?;
    if pc.get("world_id")?.as_i64()? != wid {
        return None;
    }
    let ctx = crate::edits::character_sheet_ctx(&db, &p, &pc)?;
    let template = if ctx.contains_key("sections") { "characters/custom_sheet.html" } else { "characters/sheet.html" };
    p.ctx.extend(ctx);
    render_with(pages, template, p.ctx, headers)
}

/// `GET /maps/schematic/<slug>/bg.webp` (`map_strict.mask_image`): the picture of a strict-secrecy map with every square the party has
/// not seen painted the background colour, at most 2,400 px a side - the only form of the picture a player can fetch
pub fn schematic_masked_background(_pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let slug = uri.path().strip_prefix("/maps/schematic/")?.strip_suffix("/bg.webp").filter(|s| !s.is_empty() && !s.contains('/'))?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let w = p.world.as_ref()?;
    let sc = db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    let truthy = |v: Option<&Value>| v.is_some_and(|v| !matches!(v, Value::Null | Value::Bool(false)) && v != &json!(0) && v != &json!(""));
    let image_url = sc.get("image_url").and_then(|v| v.as_str()).filter(|u| !u.is_empty())?.to_string();
    if (truthy(sc.get("is_html")) && truthy(sc.get("html_file"))) || sc.get("world_id")?.as_i64() != w.get("id")?.as_i64() {
        return None;
    }
    if !p.is_gm {
        let level = nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "maps")?;
        if level == "none" {
            return None;
        }
    }
    let view = crate::edits::strict_view(&db, &sc)??;
    // the file under uploads/ (a path that leaves it, or no such file: Python's 404)
    let rel = image_url.split_once("/uploads/").map_or(image_url.as_str(), |(_, r)| r);
    if rel.split('/').any(|seg| seg == ".." || seg.is_empty()) {
        return None;
    }
    let src = cfg.db_path.parent()?.join("uploads").join(rel);
    let img = crate::imaging::readable_or_frame(&src)?.to_rgb8();
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64);
    let (cw, ch) = (dim("canvas_width", 2000.0), dim("canvas_height", 1500.0));
    let scale = (2400.0 / cw.max(ch)).min(1.0);
    let (ow, oh) = (((cw * scale).round_ties_even() as u32).max(1), ((ch * scale).round_ties_even() as u32).max(1));
    let pic = image::imageops::resize(&img, ow, oh, image::imageops::FilterType::Lanczos3);
    // the mask: one pixel per cell, scaled (nearest) to the cell grid's size, cropped to the picture
    let (mw, mh) = (((view.cell * view.cols as f64 * scale).round_ties_even() as u32).max(1), ((view.cell * view.rows as f64 * scale).round_ties_even() as u32).max(1));
    let bg = match sc.get("canvas_bg").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("dark") {
        "blueprint" => [0x0d, 0x1b, 0x2a], "grid-light" => [0x1a, 0x1a, 0x2e], "light" => [0xf0, 0xf0, 0xf0], _ => [0x11, 0x11, 0x11],
    };
    let mut out = image::RgbImage::from_pixel(ow, oh, image::Rgb(bg));
    for y in 0..oh.min(mh) {
        let r = ((y as f64 + 0.5) * view.rows as f64 / mh as f64) as i64;
        for x in 0..ow.min(mw) {
            let c = ((x as f64 + 0.5) * view.cols as f64 / mw as f64) as i64;
            if view.explored.contains(&(r.min(view.rows - 1) * view.cols + c.min(view.cols - 1))) {
                out.put_pixel(x, y, *pic.get_pixel(x, y));
            }
        }
    }
    let data = webp::Encoder::from_rgb(out.as_raw(), ow, oh).encode_simple(false, 88.0).ok()?.to_vec();
    axum::http::Response::builder()
        .header(axum::http::header::CONTENT_TYPE, "image/webp")
        .header(axum::http::header::CACHE_CONTROL, "private, no-cache")
        .header("x-content-type-options", "nosniff")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(data))
        .ok()
}

/// `GET /characters` (app/routers/characters.py `characters_list`): the world's characters a viewer may see, searched and sorted, with
/// the derived N&D numbers, custom-system tracks, party badges, owners (GM view) and level-up flags
pub fn characters_list(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "characters")? {
        return Some(crate::idpages::refusal(403));
    }
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [p.user_id], |r| crate::layout::row_map(r)).ok()?;
    let gm = p.is_gm;
    let term = nd_render::text::py_strip(&query_param(uri, "q").unwrap_or_default()).to_string();
    let sort = query_param(uri, "sort").unwrap_or_else(|| "name".into());
    let rows = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let wid = world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let mut pcs: Vec<Map<String, Value>> = vec![];
    if let Some(wid) = wid {
        let mut sql = "SELECT * FROM player_characters WHERE world_id = ?1".to_string();
        if !gm {
            let see = world.as_ref().and_then(|w| w.get("players_see_party")).map_or(false, |v| v.as_i64().unwrap_or(0) != 0);
            sql += if see { " AND (owner_user_id = ?2 OR owner_user_id IS NOT NULL)" } else { " AND owner_user_id = ?2" };
        } else {
            sql += " AND ?2 IS NOT NULL";
        }
        if !term.is_empty() {
            // ilike: lower() LIKE lower() (ASCII case folding in SQLite either way)
            sql += " AND (lower(name) LIKE lower(?3) OR lower(char_class) LIKE lower(?3) OR lower(race) LIKE lower(?3) OR lower(player_name) LIKE lower(?3))";
        } else {
            sql += " AND ?3 IS NOT NULL";
        }
        sql += if sort == "level" { " ORDER BY level DESC, name, id" } else { " ORDER BY name, id" };
        pcs = rows(&sql, &[&wid, &p.user_id, &format!("%{term}%")])?;
        if sort == "hp" {
            let mut keyed = vec![];
            for pc in pcs {
                let top = crate::edits::pc_maxima(&db, &pc)?.0;
                let cur = pc.get("current_hp").and_then(|v| v.as_i64()).unwrap_or(0);
                let f = if top != 0 { cur as f64 / top as f64 } else { f64::INFINITY };
                keyed.push((f, pc));
            }
            keyed.sort_by(|a, b| {
                let n = |m: &Map<String, Value>| m.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
                a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal).then_with(|| n(&a.1).cmp(&n(&b.1)))
            });
            pcs = keyed.into_iter().map(|k| k.1).collect();
        }
    }
    let tpls = rows("SELECT * FROM sheet_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name, id", &[&wid])?;
    let custom_ids: Vec<i64> = tpls.iter().filter(|t| t.get("sheet_mode").and_then(|v| v.as_str()) == Some("custom")).filter_map(|t| t.get("id")?.as_i64()).collect();
    let mine = match (wid, gm) {
        (Some(w), false) => rows("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id", &[&w, &p.user_id])?,
        _ => vec![],
    };
    let my_ids: Vec<i64> = mine.iter().filter_map(|c| c.get("id")?.as_i64()).collect();
    let limit = match world.as_ref().and_then(|w| w.get("max_characters_per_player")) {
        Some(Value::Number(n)) if n.as_i64().map_or(false, |v| v != 0) => n.as_i64()?.max(1),
        None | Some(Value::Null) | Some(Value::Number(_)) => 1,
        _ => return None,
    };
    let can_create = gm || (mine.len() as i64) < limit;
    let (mut pc_party, mut my_party) = (Map::new(), Value::Null);
    if let Some(w) = wid {
        for party in rows("SELECT * FROM parties WHERE world_id = ?1 ORDER BY id", &[&w])? {
            let ids = crate::edits::member_ids(party.get("member_pc_ids_json"))?;
            for i in &ids {
                pc_party.entry(i.to_string()).or_insert_with(|| Value::Object(party.clone()));
            }
            if my_party.is_null() && my_ids.iter().any(|m| ids.contains(m)) {
                my_party = Value::Object(party.clone());
            }
        }
    }
    let (mut derived, mut tracks, mut levelup, mut owners) = (Map::new(), Map::new(), vec![], Map::new());
    let mut out = vec![];
    for mut pc in pcs {
        let id = pc.get("id")?.as_i64()?;
        match crate::edits::derived(&pc) {
            Some(d) => derived.insert(id.to_string(), Value::Object(d)),
            None if crate::config::python_free() => continue, // one unreadable character must not take the roster down
            None => return None,
        };
        let tid = pc.get("sheet_template_id").and_then(|v| v.as_i64());
        let tpl = tid.and_then(|t| tpls.iter().find(|x| x.get("id").and_then(|v| v.as_i64()) == Some(t)));
        if let (Some(t), true) = (tpl, tid.map_or(false, |t| custom_ids.contains(&t))) {
            let mut tr = crate::sheets::Tpl::load(t.clone())?.tracks(&crate::sheets::custom_fields(&pc)?, true)?;
            tr.truncate(4);
            tracks.insert(id.to_string(), Value::Array(tr));
        }
        // _levelup_ready
        let level = pc.get("level")?.as_i64()?;
        let native = crate::edits::pc_maxima(&db, &pc)?.4;
        if (1..20).contains(&level) && native && pc.get("xp")?.as_i64()? >= crate::edits::xp_threshold(level) {
            levelup.push(id);
        } else if level < 1 && native {
            return None;
        }
        if gm {
            if let Some(o) = pc.get("owner_user_id").and_then(|v| v.as_i64()).filter(|o| *o != 0) {
                if let Ok(u) = db.query_row("SELECT * FROM users WHERE id = ?1", [o], |r| crate::layout::row_map(r)) {
                    let dn = u.get("display_name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| json!(s));
                    owners.insert(o.to_string(), dn.unwrap_or_else(|| u.get("email").cloned().unwrap_or(Value::Null)));
                }
            }
        }
        // the sheet_template relationship (a template of any world: the row the id names)
        let rel = match tid {
            Some(t) => db.query_row("SELECT * FROM sheet_templates WHERE id = ?1", [t], |r| crate::layout::row_map(r)).ok().map(Value::Object).unwrap_or(Value::Null),
            None => Value::Null,
        };
        pc.insert("sheet_template".into(), rel);
        out.push(Value::Object(pc));
    }
    let c = &mut p.ctx;
    c.insert("pcs".into(), Value::Array(out));
    c.insert("derived".into(), json!({"__intkeys__": derived}));
    c.insert("sheet_templates".into(), Value::Array(tpls.into_iter().map(Value::Object).collect()));
    c.insert("custom_tpl_ids".into(), json!(custom_ids));
    c.insert("custom_tracks".into(), json!({"__intkeys__": tracks}));
    c.insert("user".into(), Value::Object(user));
    c.insert("my_character".into(), mine.first().cloned().map(Value::Object).unwrap_or(Value::Null));
    c.insert("my_character_ids".into(), json!(my_ids));
    c.insert("character_limit".into(), json!(limit));
    c.insert("my_character_count".into(), json!(mine.len()));
    c.insert("can_create_character".into(), json!(can_create));
    c.insert("q".into(), json!(term));
    c.insert("sort".into(), json!(sort));
    c.insert("pc_party".into(), json!({"__intkeys__": pc_party}));
    c.insert("owner_names".into(), json!({"__intkeys__": owners}));
    c.insert("my_party".into(), my_party);
    c.insert("levelup_ready".into(), json!(levelup));
    render_with(pages, "characters/list.html", p.ctx, headers)
}

const AI_GROUNDING: &str = "When asked whether something already exists in this world's rules or lore (a trait, an item, a price, a named NPC, a fact), answer only from what the retrieved world lore/Rules text actually says — never invent specific names, numbers, or details to fill a gap. If it isn't covered, say so plainly rather than presenting an invented answer as fact, and offer to help create it instead. When the retrieved text includes a markdown table relevant to the question, that table IS the answer: list its actual named rows verbatim (item names, prices, stats) rather than summarizing them into categories or ranges from surrounding overview prose — an overview paragraph is a summary of the table, not a substitute for it. If a table excerpt says more rows were truncated, list every row you were given and say the list continues in the full Rules text rather than treating the partial list as complete.";
const AI_TABLES: &str = "When the provided lore or Rules text contains a table and the question asks to list, enumerate, or compare its entries, reproduce ALL of its rows in your answer — as a markdown table or a complete list. Never answer with a summary, a few examples, or 'the list includes...'. Mention missing rows only if the provided text itself is visibly cut off mid-table.";

/// `GET /ai` (app/main.py `ai_chat_page`): the GM's AI chat, its system prompt built from the world's entity counts and the
/// GM-authored instructions
pub fn ai_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Map, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "ai")? {
        return Some(crate::idpages::refusal(403));
    }
    let mut counts = Map::new();
    let mut system = format!("You are a creative world-building AI assistant for a Neon & Dragons cyberpunk-fantasy TTRPG setting. Help the Game Master with world-building, lore, NPC backstories, plot hooks, and creative writing. Be vivid and immersive. {AI_GROUNDING} {AI_TABLES}");
    if let Some(w) = &world {
        let wid = w.get("id")?.as_i64()?;
        for k in nd_perm::sections::effective_kinds(w.get("custom_kinds_json").and_then(|v| v.as_str()))? {
            counts.insert(k, json!(0));
        }
        // _kind_counts: deps.filter_visible_entities for a non-GM viewer, one GROUP BY
        let vis = if p.is_gm { " AND ?2 IS NOT NULL" } else {
            " AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))"
        };
        let mut st = db.prepare(&format!("SELECT kind, count(id) FROM entities WHERE world_id = ?1{vis} GROUP BY kind")).ok()?;
        for r in st.query_map(rusqlite::params![wid, p.user_id], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, i64>(1)?))).ok()? {
            let (k, n) = r.ok()?;
            counts.insert(k?, json!(n));
        }
        let listed = counts.iter().filter(|(_, v)| v.as_i64().unwrap_or(0) > 0).map(|(k, v)| format!("{v} {k}s")).collect::<Vec<_>>().join(", ");
        let name = w.get("name").and_then(|v| v.as_str()).unwrap_or("");
        system = format!("You are a creative world-building AI assistant for '{name}', a Neon & Dragons cyberpunk-fantasy TTRPG setting. The world currently contains: {listed}. Help the Game Master with world-building, lore, NPC backstories, plot hooks, and creative writing. Be vivid, immersive, and consistent with the cyberpunk-fantasy tone. Keep responses focused; expand only when asked. {AI_GROUNDING} {AI_TABLES}");
        let extra = crate::retrieval::instructions_text(&db, wid, !p.is_gm)?;
        if !extra.is_empty() {
            system = format!("{system}\n\n{extra}");
        }
    }
    let c = &mut p.ctx;
    c.insert("kinds".into(), json!(nd_perm::sections::KINDS));
    c.insert("kind_icons".into(), nd_render::app::data().get("kind_icons")?.clone());
    c.insert("entity_counts".into(), Value::Object(counts));
    c.insert("world_system".into(), json!(system));
    render_with(pages, "ai_chat.html", p.ctx, headers)
}

/// `GET /import` (app/routers/importer.py `import_page`): the import tools, with the world's schematics, maps and entities as JSON
pub fn import_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "import")? {
        return Some(crate::idpages::refusal(403));
    }
    let wid = world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()).unwrap_or(1);
    let mut st = db.prepare("SELECT slug, name FROM schematics WHERE world_id = ?1 AND is_html = 0 ORDER BY name, id").ok()?;
    let schematics: Vec<Value> = st.query_map([wid], |r| Ok(json!({"slug": r.get::<_, Option<String>>(0)?, "name": r.get::<_, Option<String>>(1)?})))
        .ok()?.collect::<Result<_, _>>().ok()?;
    let maps: Vec<Value> = crate::maps::world_maps(cfg, wid)?.into_iter()
        .map(|(slug, d)| { let name = d.get("name").cloned().unwrap_or_else(|| json!(slug)); json!({"slug": slug, "name": name}) }).collect();
    let mut st = db.prepare("SELECT id, name, kind, image_url FROM entities WHERE world_id = ?1 ORDER BY kind, name, id").ok()?;
    let entities: Vec<Value> = st.query_map([wid], |r| Ok(json!({"id": r.get::<_, i64>(0)?, "name": r.get::<_, Option<String>>(1)?,
        "kind": r.get::<_, Option<String>>(2)?, "has_image": r.get::<_, Option<String>>(3)?.map_or(false, |s| !s.is_empty())})))
        .ok()?.collect::<Result<_, _>>().ok()?;
    let c = &mut p.ctx;
    c.insert("schematics_json".into(), json!(nd_render::py_dumps(&Value::Array(schematics))));
    c.insert("maps_json".into(), json!(nd_render::py_dumps(&Value::Array(maps))));
    c.insert("entities_json".into(), json!(nd_render::py_dumps(&Value::Array(entities))));
    c.insert("bulk_image_max_files".into(), json!(100));
    c.insert("kinds_json".into(), json!(nd_render::py_dumps(&json!(nd_perm::sections::KINDS))));
    c.insert("max_vision_import_images".into(), json!(6));
    render_with(pages, "import.html", p.ctx, headers)
}

/// `GET /ai-chat` (app/main.py `player_ai_chat`): the lean chat page a world opens to its players (always the GM's), with the
/// GM-authored instructions a player may see
pub fn ai_chat_player(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::json;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Some(w) = p.world.clone() else { return Some(crate::idpages::refusal(404)) };
    if !p.is_gm && w.get("players_can_use_ai_chat").and_then(|v| v.as_i64()).unwrap_or(0) == 0 {
        return Some(crate::idpages::refusal(403));
    }
    let text = crate::retrieval::instructions_text(&db, w.get("id")?.as_i64()?, !p.is_gm)?;
    p.ctx.insert("ai_custom_instructions".into(), json!(text));
    render_with(pages, "ai_chat_player.html", p.ctx, headers)
}


/// `_autolink_name_map(db, world, request, exclude)`: (name, entity id) for every entity of the world the viewer may see (`vis`: the
/// visibility condition on `e`), names then aliases, then the derived short forms - a real name always wins over a derived one
pub(crate) fn autolink_names(db: &rusqlite::Connection, wid: i64, exclude: Option<i64>, vis: &str) -> Option<Vec<(String, i64)>> {
    use nd_render::text;
    let mut names: Vec<(String, i64)> = vec![];
    let mut st = db.prepare(&format!("SELECT e.id, e.name, e.aliases FROM entities e WHERE e.world_id = ?1 AND e.id != ?2 AND {vis} ORDER BY e.id")).ok()?;
    let r: Vec<(i64, Option<String>, Option<String>)> = st.query_map([wid, exclude.unwrap_or(-1)], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
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
    Some(names)
}

/// (world id, player id) of `/worlds/<w>/notes/<u>`
pub fn notes_path(path: &str) -> Option<(i64, i64)> {
    let rest = path.strip_prefix("/worlds/")?;
    let (w, u) = rest.split_once("/notes/")?;
    let ok = |d: &str| !d.is_empty() && d.len() < 16 && d.bytes().all(|b| b.is_ascii_digit()) && !(d.len() > 1 && d.starts_with('0'));
    (ok(w) && ok(u)).then(|| Some((w.parse().ok()?, u.parse().ok()?)))?
}

/// `GET /worlds/<w>/notes/<u>` (app/main.py `private_notes_view`): the GM's private notes to one player, newest first, for the GM or
/// that player
pub fn private_notes(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use serde_json::{json, Value};
    let (wid, uid) = notes_path(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let Ok(w) = db.query_row("SELECT * FROM worlds WHERE id = ?1", [wid], |r| crate::layout::row_map(r)) else { return Some(crate::idpages::refusal(404)) };
    let gm = p.is_gm;
    if !gm {
        let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_ok();
        if p.user_id != uid || !member {
            return Some(crate::idpages::refusal(403));
        }
    }
    let Ok(target) = db.query_row("SELECT * FROM users WHERE id = ?1", [uid], |r| crate::layout::row_map(r)) else { return Some(crate::idpages::refusal(404)) };
    let mut st = db.prepare("SELECT * FROM private_notes WHERE world_id = ?1 AND player_user_id = ?2 ORDER BY created_at DESC, id").ok()?;
    let mut notes = st.query_map([wid, uid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let vis = if gm { "1".to_string() } else {
        format!("(e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let names = autolink_names(&db, wid, None, &vis)?;
    let mut toc = vec![];
    for n in notes.iter_mut() {
        let content = n.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let content = if gm { content } else { nd_perm::gmonly::strip_gm_only(&content, None, nd_render::now_minute())? };
        n.insert("content_html".into(), json!(nd_render::text::autolink_entities(&nd_render::md::render_md(&content), &names)?));
        // _note_toc_label
        let title = nd_render::text::py_strip(n.get("title").and_then(|v| v.as_str()).unwrap_or("")).to_string();
        let label = if !title.is_empty() { title } else {
            let raw = n.get("content").and_then(|v| v.as_str()).unwrap_or("");
            let first = nd_render::text::py_splitlines(raw).into_iter().map(nd_render::text::py_strip).find(|l| !l.is_empty()).unwrap_or("").to_string();
            let first = nd_render::text::py_strip(first.trim_start_matches(|c: char| matches!(c, '#' | '>' | '*' | '-') || c.is_whitespace())).to_string();
            if first.is_empty() { "Untitled note".to_string() } else {
                let chars: Vec<char> = first.chars().collect();
                if chars.len() > 60 { chars[..60].iter().collect::<String>() + "…" } else { first }
            }
        };
        toc.push(json!({"id": format!("note-{}", n.get("id")?.as_i64()?), "text": label}));
    }
    let c = &mut p.ctx;
    c.insert("world".into(), Value::Object(w));
    c.insert("target_user".into(), Value::Object(target));
    c.insert("notes".into(), Value::Array(notes.into_iter().map(Value::Object).collect()));
    c.insert("can_manage".into(), json!(gm));
    c.insert("toc".into(), Value::Array(toc));
    render_with(pages, "private_notes.html", p.ctx, headers)
}

/// `_templates_for_world`: the world's sheet templates plus the global ones, built-ins first, then by name
fn sheet_templates_for(db: &rusqlite::Connection, wid: Option<i64>) -> Option<Vec<Map<String, Value>>> {
    let sql = "SELECT * FROM sheet_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name";
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map([wid.unwrap_or(-1)], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

/// `character_limit`: World.max_characters_per_player clamped to at least 1
fn character_limit(world: &Map<String, Value>) -> i64 {
    world.get("max_characters_per_player").and_then(|v| v.as_i64()).map_or(1, |n| n.max(1))
}

/// `GET /characters/new` (app/routers/characters.py `character_new_form`): the creation wizard, or a custom sheet's own
/// form when the chosen template is one; a player at their limit is sent to their (single) character or the list
pub fn characters_new(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    let world = world.as_ref()?;
    // the pre-selected template (?template_id=) is validated by FastAPI before the handler runs, so a value its int()
    // refuses (or one past 64 bits) is a 422 - the gateway leaves it to Python
    let chosen = match crate::pages::query_param(uri, "template_id") {
        Some(t) if !t.is_empty() => {
            if !t.bytes().all(|b| b.is_ascii_digit()) || t.len() > 15 || t.parse::<i64>().is_err() {
                return None;
            }
            // a template id with no row falls back to the N&D default, as Python's does
            match sheet_tpl_by(&db, "id", &t)? {
                Some(t) => Some(t),
                None => sheet_tpl_by(&db, "slug", "nd-default")?,
            }
        }
        _ => sheet_tpl_by(&db, "slug", "nd-default")?,
    };
    if !p.is_gm {
        let mut st = db.prepare("SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
        let mine: Vec<i64> = st.query_map([world.get("id")?.as_i64()?, p.user_id], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
        drop(st);
        let limit = character_limit(world);
        if mine.len() as i64 >= limit {
            let to = if mine.len() == 1 { format!("/characters/{}", mine[0]) } else { "/characters".to_string() };
            return Some(crate::edits::redirect(&to)?);
        }
    }
    if chosen.as_ref().and_then(|t| t.get("sheet_mode").and_then(|v| v.as_str())) == Some("custom") {
        let tpl = chosen?;
        let t = crate::sheets::Tpl::load(tpl.clone())?;
        let tpl_fields = t.enrich_fields()?;
        let grouped: Vec<Value> = crate::apis::group_by_section(&tpl_fields)?
            .into_iter().map(|(name, fields)| json!([name, fields])).collect();
        let names: Vec<String> = grouped.iter().filter_map(|g| g.get(0).and_then(|n| n.as_str()).map(str::to_string)).collect();
        let sheet_pages = t.sheet_pages(&names)?;
        let c = &mut p.ctx;
        c.insert("pc".into(), Value::Null);
        c.insert("can_manage".into(), json!(true));
        c.insert("chosen_template".into(), Value::Object(tpl));
        c.insert("sections".into(), Value::Array(grouped));
        c.insert("sheet_pages".into(), sheet_pages);
        c.insert("tpl_fields".into(), Value::Array(tpl_fields));
        c.insert("custom_fields".into(), json!({}));
        return render_with(pages, "characters/custom_sheet.html", p.ctx, headers);
    }
    let c = &mut p.ctx;
    c.insert("chosen_template".into(), chosen.map(Value::Object).unwrap_or(Value::Null));
    render_with(pages, "characters/wizard.html", p.ctx, headers)
}

/// the sheet template by column ("id" with digits, or "slug")
pub(crate) fn sheet_tpl_by(db: &rusqlite::Connection, col: &str, val: &str) -> Option<Option<Map<String, Value>>> {
    Some(db.query_row(&format!("SELECT * FROM sheet_templates WHERE {col} = ?1"), [val], |r| crate::layout::row_map(r)).ok())
}

/// the shared context of characters/form.html (the edit form; the wizard posts elsewhere)
pub(crate) fn character_form_ctx(db: &rusqlite::Connection, p: &mut crate::layout::PageCtx, pc: &Map<String, Value>, chosen_tpl: Option<Map<String, Value>>) -> Option<Map<String, Value>> {
    let list = |k: &str| -> Option<Value> { crate::apis::py_json_strict(pc.get(k).and_then(|v| v.as_str()), "[]") };
    let custom_fields = crate::apis::py_json_strict(pc.get("custom_fields_json").and_then(|v| v.as_str()), "{}")?;
    if !custom_fields.is_object() {
        return None; // json.loads gives a list: Python's page would render it as one - not ported
    }
    let (nd_stats, nd_currency) = crate::apis::nd_defaults();
    let tpl_fields = match &chosen_tpl {
        Some(t) => crate::sheets::Tpl::load(t.clone())?.enrich_fields()?,
        None => vec![],
    };
    let templates = sheet_templates_for(db, p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()))?;
    let c = &mut p.ctx;
    c.insert("pc".into(), Value::Object(pc.clone()));
    c.insert("nd_default_stats".into(), nd_stats);
    c.insert("nd_default_currency".into(), nd_currency);
    c.insert("stats".into(), list("stats_json")?);
    c.insert("currency".into(), list("currency_json")?);
    c.insert("equipment".into(), list("equipment_json")?);
    c.insert("feats".into(), list("feats_json")?);
    c.insert("cyberware".into(), list("cyberware_json")?);
    c.insert("sheet_templates".into(), Value::Array(templates.into_iter().map(Value::Object).collect()));
    c.insert("chosen_template".into(), chosen_tpl.map(Value::Object).unwrap_or(Value::Null));
    c.insert("tpl_fields".into(), Value::Array(tpl_fields));
    c.insert("custom_fields".into(), custom_fields);
    Some(p.ctx.clone())
}

/// `GET /characters/ai-new` (app/routers/character_ai.py `pc_ai_page`): the AI character creator page - the world's custom
/// systems, the pre-selected template id (a query value FastAPI would 422 on stays Python's), the player's display name;
/// a player at their character limit redirected as on /characters/new
pub fn characters_ai_new(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    let world = world.as_ref()?;
    // template_id: int = 0 - FastAPI's coercion: a plain int (or a value it 422s on: Python's, forwarded)
    let selected = match crate::pages::query_param(uri, "template_id") {
        Some(t) if !t.is_empty() => {
            if !t.bytes().all(|b| b.is_ascii_digit()) || t.len() > 15 || t.parse::<i64>().is_err() {
                return None;
            }
            t.parse::<i64>().ok()?
        }
        _ => 0,
    };
    let user: Option<Map<String, Value>> = db.query_row("SELECT * FROM users WHERE id = ?1", [p.user_id], |r| crate::layout::row_map(r)).ok();
    if let Some(u) = &user {
        if !crate::edits::py_truthy_pub(u.get("is_gm")) {
            let wid = world.get("id")?.as_i64()?;
            let mut st = db.prepare("SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
            let mine: Vec<i64> = st.query_map([wid, p.user_id], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
            drop(st);
            let limit = character_limit(world);
            if mine.len() as i64 >= limit {
                let to = if mine.len() == 1 { format!("/characters/{}", mine[0]) } else { "/characters".to_string() };
                return Some(crate::edits::redirect(&to)?);
            }
        }
    }
    let wid = world.get("id")?.as_i64()?;
    let mut st = db.prepare("SELECT * FROM sheet_templates WHERE sheet_mode = 'custom' AND (world_id IS NULL OR world_id = ?1) ORDER BY name").ok()?;
    let systems: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let display_name = user.as_ref()
        .and_then(|u| u.get("display_name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()))
        .unwrap_or("").to_string();
    let c = &mut p.ctx;
    c.insert("systems".into(), Value::Array(systems.into_iter().map(Value::Object).collect()));
    c.insert("selected_template_id".into(), json!(selected));
    c.insert("display_name".into(), json!(display_name));
    render_with(pages, "characters/ai_new.html", p.ctx, headers)
}

/// `GET /maps/schematic/new` (app/main.py `schematic_new_form`): the empty schematic form (player-safe, no section check)
pub fn schematic_new_form(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    p.ctx.insert("schematic".into(), Value::Null);
    render_with(pages, "schematic_form.html", p.ctx, headers)
}
