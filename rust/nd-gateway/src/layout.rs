//! What every signed-in page's layout (base.html) needs, read from the database the way Python's auth gate, `get_world_ctx` and the
//! templating context processor build it (docs/RUST_PAGES.md). `None` anywhere = not sure: the page goes to Python.
use crate::{session, Config};
use axum::http::{header, HeaderMap, Uri};
use nd_render::app;
use percent_encoding::percent_decode_str;
use rusqlite::{types::ValueRef, Connection, Row};
use serde_json::{json, Map, Value};

/// A row as {column: value} (SQLite's own types; booleans are 0 / 1).
pub fn row_map(r: &Row) -> rusqlite::Result<Map<String, Value>> {
    let mut m = Map::new();
    let stmt = r.as_ref();
    for i in 0..stmt.column_count() {
        let name = stmt.column_name(i)?.to_string();
        let v = match r.get_ref(i)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(n) => json!(n),
            ValueRef::Real(f) => json!(f),
            ValueRef::Text(t) if stmt.columns()[i].decl_type().map_or(false, |d| d.eq_ignore_ascii_case("DATETIME")) => {
                json!({"__dt__": String::from_utf8_lossy(t)})
            }
            ValueRef::Text(t) => Value::String(String::from_utf8_lossy(t).into_owned()),
            ValueRef::Blob(_) => Value::Null,
        };
        m.insert(name, v);
    }
    Ok(m)
}

pub struct PageCtx {
    pub ctx: Map<String, Value>,
    pub world: Option<Map<String, Value>>,
    pub user_id: i64,
    pub is_gm: bool,
    /// "player" / "assistant" in the active world (None for a GM)
    pub role: Option<String>,
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

pub fn build(db: &Connection, cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap, now: u64) -> Option<PageCtx> {
    let secret = cfg.secret_key.as_deref()?;
    let cookie_header = headers.get(header::COOKIE).and_then(|v| v.to_str().ok())?;
    let (sess, signed_at) = session::read_with_time(session::cookie(cookie_header, session::COOKIE_NAME)?, secret, now, session::MAX_AGE_SECONDS)?;
    if now.saturating_sub(signed_at) > session::REFRESH_SECONDS {
        return None;
    }
    let user_id = sess.get("user_id")?.as_i64().filter(|i| *i != 0)?;
    let user = db.query_row("SELECT * FROM users WHERE id = ?1", [user_id], |r| row_map(r)).ok()?;
    let is_gm = truthy(user.get("is_gm"));
    if sess.get("session_version")?.as_i64()? != user.get("session_version")?.as_i64()? {
        return None;
    }
    // the person's site layout rides in the session; a stale or foreign copy is refreshed by Python (it writes the cookie)
    let sl = sess.get("site_layout")?.as_object()?;
    if sl.get("uid")?.as_i64()? != user_id || !sl.get("options")?.is_object() || now as i64 - sl.get("at")?.as_i64()? >= 15 * 60 {
        return None;
    }
    let site = app::clean("site", &json!({"options": sl.get("options"), "pins": sl.get("pins")}))?;
    let site_layout = json!({"options": site["options"], "pins": site["pins"], "signed": true});

    let path = percent_decode_str(uri.path()).decode_utf8().ok()?.to_string();
    let query: Vec<(String, String)> = uri
        .query()
        .unwrap_or("")
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            let d = |s: &str| percent_decode_str(&s.replace('+', " ")).decode_utf8_lossy().to_string();
            (d(k), d(v))
        })
        .collect();
    let mut qmap = Map::new();
    for (k, v) in &query {
        qmap.insert(k.clone(), Value::String(v.clone())); // the last one wins, as in Starlette
    }
    let wanted = qmap.get("w").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string)
        .or_else(|| session::cookie(cookie_header, "active_world").map(str::to_string)).unwrap_or_default();
    if !wanted.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return None;
    }
    let worlds: Vec<(Map<String, Value>, Option<String>)> = if is_gm {
        let mut st = db.prepare("SELECT * FROM worlds ORDER BY id").ok()?;
        let r = st.query_map([], |r| Ok((row_map(r)?, None))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        r
    } else {
        let mut st = db.prepare("SELECT w.*, m.role AS __role FROM world_memberships m JOIN worlds w ON w.id = m.world_id WHERE m.user_id = ?1 ORDER BY w.id").ok()?;
        let r = st
            .query_map([user_id], |r| {
                let mut m = row_map(r)?;
                let role = m.remove("__role").and_then(|v| v.as_str().map(str::to_string));
                Ok((m, role))
            })
            .ok()?
            .collect::<Result<Vec<_>, _>>()
            .ok()?;
        r
    };
    let chosen = worlds.iter().find(|(w, _)| w.get("slug").and_then(|s| s.as_str()) == Some(wanted.as_str())).or_else(|| worlds.first());
    if !is_gm {
        if let Some((w, _)) = chosen {
            if worlds.iter().filter(|(x, _)| x.get("id") == w.get("id")).count() != 1 {
                return None;
            }
        }
    }
    let world = chosen.map(|(w, _)| w.clone());
    let active_role = chosen.and_then(|(_, r)| r.clone());
    let viewer = nd_perm::allow::Viewer { is_gm, active_role: active_role.clone(), active_world_id: world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) };
    if !nd_perm::allow::gate_allows(&viewer, method, &path) {
        return None;
    }
    let is_assistant = matches!(active_role.as_deref(), Some("assistant") | Some("owner"));
    let role = if is_gm { None } else { Some(if is_assistant { "assistant" } else { "player" }.to_string()) };
    if world.as_ref().map_or(false, |w| w.get("hero_style").and_then(|v| v.as_str()) == Some("everywhere")) {
        return None; // _hero.html is not ported yet
    }

    // app settings: the nav flags and the hover-preview settings (database.get_app_settings_flags_cached)
    let s = db.query_row("SELECT * FROM app_settings LIMIT 1", [], |r| row_map(r)).ok()?;
    let num = |k: &str, d: i64| s.get(k).and_then(|v| v.as_i64()).filter(|n| *n != 0).unwrap_or(d);
    let hover = json!({
        "enabled": truthy(s.get("hover_preview_enabled")),
        "delay_ms": num("hover_preview_delay_ms", 5000),
        "hide_delay_ms": s.get("hover_preview_hide_delay_ms").and_then(|v| v.as_i64()).unwrap_or(400),
        "width_px": num("hover_preview_width_px", 340),
        "max_height_px": num("hover_preview_max_height_px", 420),
    });
    let (dl, ky) = (truthy(s.get("dreamlands_enabled")), truthy(s.get("king_in_yellow_enabled")));
    let (menus, ungrouped) = app::resolve_nav(&app::NavCtx { is_gm, role: role.as_deref(), world: world.as_ref(), dreamlands_enabled: dl, king_in_yellow_enabled: ky })?;
    let theme = world
        .as_ref()
        .and_then(|w| w.get("theme_json")?.as_str().map(str::to_string))
        .filter(|s| !s.is_empty())
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or(json!({}));

    // every saved layout of this person, cleaned (templating._saved_view)
    let mut saved = Map::new();
    let mut st = db.prepare("SELECT section, prefs_json FROM user_view_prefs WHERE user_id = ?1").ok()?;
    let rows = st.query_map([user_id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    for (section, raw) in rows {
        let cleaned = match raw.as_deref().map(serde_json::from_str::<Value>) {
            Some(Ok(v)) => app::clean(&section, &v).unwrap_or(json!({})),
            _ => json!({}),
        };
        saved.insert(section, cleaned);
    }

    let user_v = json!({"id": user_id, "is_gm": is_gm, "display_name": user.get("display_name").cloned().unwrap_or(Value::Null)});
    let request = json!({"state": {"user": user_v, "is_assistant": is_assistant}, "url": {"path": path}, "query_params": qmap});
    let mut ctx = Map::new();
    ctx.insert("request".into(), request);
    ctx.insert("world".into(), world.clone().map(Value::Object).unwrap_or(Value::Null));
    ctx.insert("worlds".into(), Value::Array(worlds.iter().map(|(w, _)| Value::Object(w.clone())).collect()));
    ctx.insert("world_theme".into(), theme);
    ctx.insert("nav_menus".into(), Value::Array(menus));
    ctx.insert("nav_ungrouped_items".into(), Value::Array(ungrouped));
    ctx.insert("hover_cfg".into(), hover);
    let (kinds, kind_icons, subtypes) = app::effective_kind_tables(world.as_ref())?;
    ctx.insert("kinds".into(), kinds);
    ctx.insert("kind_icons".into(), kind_icons);
    ctx.insert("subtypes".into(), subtypes);
    ctx.insert("dreamlands_enabled".into(), json!(dl));
    ctx.insert("king_in_yellow_enabled".into(), json!(ky));
    ctx.insert("__nd".into(), json!({"site_layout": site_layout, "can_edit": is_gm || is_assistant, "saved_views": saved}));
    Some(PageCtx { ctx, world, user_id, is_gm, role })
}
