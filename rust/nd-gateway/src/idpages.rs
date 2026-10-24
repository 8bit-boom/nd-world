//! Pages addressed by a row id in the path (`/tables/<id>/edit`, `/handout/<id>`, ...), each the port of one Python handler. Same rule as
//! every gateway page: `None` = not sure, the request goes to Python unchanged (a missing row, a refusal, anything unusual).

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::{render_with, Pages};

type Handler = fn(&Pages, &Connection, PageCtx, i64, &HeaderMap) -> Option<Response<Body>>;

/// (path before the id, path after the id, handler)
const ROUTES: &[(&str, &str, Handler)] = &[
    ("/tables/", "/edit", table_edit),
    ("/handout/", "", handout),
    ("/entity-templates/", "/edit", entity_template_edit),
    ("/display/combat/", "", display_combat),
    ("/worlds/", "/kinds/edit", kinds_edit),
    ("/worlds/", "/rules/edit", rules_edit),
    ("/pages/", "", page_viewer),
    ("/quests/", "", quest_detail),
    ("/entity/", "/edit", entity_edit),
    ("/sessions/", "", session_detail),
    ("/parties/", "/roster", party_roster),
    ("/parties/", "/summary", party_summary),
    ("/parties/", "", party_detail),
    ("/session-log/", "", session_log_detail),
];

/// The handler for a path like `/tables/12/edit`, and the id (digits only, as FastAPI's int converter reads them here)
pub fn route(path: &str) -> Option<(Handler, i64)> {
    for (pre, post, h) in ROUTES {
        if let Some(rest) = path.strip_prefix(pre).and_then(|r| r.strip_suffix(post)) {
            if !rest.is_empty() && rest.len() < 16 && rest.bytes().all(|b| b.is_ascii_digit()) {
                return Some((*h, rest.parse().ok()?));
            }
        }
    }
    None
}

pub fn serve(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (h, id) = route(uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    h(pages, &db, p, id, headers)
}

fn row(db: &Connection, sql: &str, id: i64) -> Option<Map<String, Value>> {
    db.query_row(sql, [id], |r| crate::layout::row_map(r)).ok()
}

/// json.loads(text or "[]") - None for text Python would fail on
fn json_or_empty_list(v: Option<&Value>) -> Option<Value> {
    let t = v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    serde_json::from_str(t).ok()
}

/// `GET /tables/<id>/edit` (app/routers/tables.py `table_edit_form`)
fn table_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let t = row(db, "SELECT * FROM random_tables WHERE id = ?1", id)?;
    if !crate::pages::can_edit_table_row(db, &p, &t)? {
        return None;
    }
    let entries = json_or_empty_list(t.get("entries_json"))?;
    p.ctx.insert("tbl".into(), Value::Object(t));
    p.ctx.insert("entries".into(), entries);
    render_with(pages, "tables/form.html", p.ctx, headers)
}

/// `GET /handout/<id>` (app/routers/handouts.py `handout_single`): a bare print page
fn handout(pages: &Pages, db: &Connection, p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let e = row(db, "SELECT * FROM entities WHERE id = ?1", id)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    if e.get("world_id")?.as_i64() != Some(wid) {
        return None;
    }
    let body = match e.get("body") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => nd_render::md::render_md(s),
        _ => return None,
    };
    let mut ctx = Map::new();
    ctx.insert("request".into(), p.ctx.get("request")?.clone());
    if let Some(nd) = p.ctx.get("__nd") {
        ctx.insert("__nd".into(), nd.clone());
    }
    ctx.insert("entities".into(), json!([{"entity": Value::Object(e), "body_html": body}]));
    render_with(pages, "handout.html", ctx, headers)
}

/// `GET /entity-templates/<id>/edit` (app/main.py `entity_template_edit_form`)
fn entity_template_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let t = row(db, "SELECT * FROM entity_templates WHERE id = ?1", id)?;
    let fields = json_or_empty_list(t.get("fields_json"))?;
    p.ctx.insert("tpl".into(), Value::Object(t));
    p.ctx.insert("fields".into(), fields);
    render_with(pages, "entity_template_form.html", p.ctx, headers)
}

/// `GET /display/combat/<id>` (app/routers/display.py `display_combat_page`): the fight's turn order on the table's TV, GM only
fn display_combat(pages: &Pages, db: &Connection, p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    if !p.is_gm {
        return None;
    }
    let world = p.world.as_ref()?;
    let cs = row(db, "SELECT world_id, is_template, name FROM combat_sessions WHERE id = ?1", id)?;
    if cs.get("world_id")?.as_i64() != world.get("id")?.as_i64() || cs.get("is_template").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
        return None;
    }
    let title: String = match cs.get("name") {
        Some(Value::String(s)) => nd_render::text::py_strip(s).chars().take(200).collect(),
        _ => String::new(),
    };
    let mut ctx = Map::new();
    for k in ["request", "__nd", "world"] {
        if let Some(v) = p.ctx.get(k) {
            ctx.insert(k.into(), v.clone());
        }
    }
    ctx.insert("combat_id".into(), json!(id));
    ctx.insert("title".into(), json!(title));
    render_with(pages, "display_combat.html", ctx, headers)
}

/// `deps.load_custom_kinds`: the world's valid custom kinds, malformed entries dropped
pub(crate) fn load_custom_kinds(world: &Map<String, Value>) -> Vec<Value> {
    let raw = world.get("custom_kinds_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let Ok(Value::Array(list)) = serde_json::from_str::<Value>(raw) else { return vec![] };
    let mut out = vec![];
    for e in list {
        let Some(e) = e.as_object() else { continue };
        let (Some(id), Some(label)) = (e.get("id").and_then(|v| v.as_str()), e.get("label").and_then(|v| v.as_str())) else { continue };
        if !id.starts_with("custom_") || nd_render::text::py_strip(label).is_empty() {
            continue;
        }
        let truthy = |v: Option<&Value>| v.filter(|v| !matches!(v, Value::Null | Value::Bool(false)) && v.as_str() != Some("") && v.as_f64() != Some(0.0)
            && v.as_array().map_or(true, |a| !a.is_empty()) && v.as_object().map_or(true, |o| !o.is_empty())).cloned();
        let subtypes: Vec<Value> = match e.get("subtypes") {
            Some(Value::Array(a)) => a.iter().filter(|x| x.is_string()).cloned().collect(),
            _ => vec![],
        };
        out.push(json!({"id": id, "label": label, "icon": truthy(e.get("icon")).unwrap_or(json!("🏷")), "subtypes": subtypes,
                        "created_at": truthy(e.get("created_at")).unwrap_or(json!(""))}));
    }
    out
}

/// `GET /worlds/<id>/kinds/edit` (app/routers/kinds_admin.py `kinds_edit_form`)
fn kinds_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let w = row(db, "SELECT * FROM worlds WHERE id = ?1", id)?;
    p.ctx.insert("initial_kinds".into(), Value::Array(load_custom_kinds(&w)));
    p.ctx.insert("edit_world".into(), Value::Object(w));
    p.ctx.insert("max_custom_kinds".into(), json!(25));
    render_with(pages, "kinds_edit.html", p.ctx, headers)
}

/// `GET /worlds/<id>/rules/edit` (app/main.py `world_rules_edit_form`) - for a world with no rules overlay stored; one with an overlay
/// (whose validation message the page shows) is Python's
fn rules_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let w = row(db, "SELECT * FROM worlds WHERE id = ?1", id)?;
    match w.get("rules_json") {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if nd_render::text::py_strip(s).is_empty() => {}
        _ => return None,
    }
    p.ctx.insert("edit_world".into(), Value::Object(w));
    p.ctx.insert("rules_json_error".into(), Value::Null);
    render_with(pages, "rules_edit.html", p.ctx, headers)
}

/// `GET /pages/<id>` (app/routers/pages.py `pages_viewer`): one document, full page; a hidden one only for the GM
fn page_viewer(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let world = p.world.clone()?;
    if !p.is_gm {
        let s = |k: &str| world.get(k).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), "pages")? == "none" {
            return None;
        }
    }
    let doc = row(db, "SELECT * FROM page_docs WHERE id = ?1", id)?;
    if doc.get("world_id")?.as_i64() != world.get("id")?.as_i64() {
        return None;
    }
    let visible = !matches!(doc.get("visible_to_players"), Some(Value::Null) | Some(Value::Number(_)) if doc.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) == 0);
    if !visible && !p.is_gm {
        return None;
    }
    p.ctx.insert("doc".into(), Value::Object(doc));
    render_with(pages, "page_viewer.html", p.ctx, headers)
}

pub(crate) fn rows(db: &Connection, sql: &str, id: i64) -> Option<Vec<Value>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map([id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r.into_iter().map(Value::Object).collect())
}

/// Python's truthiness of a stored boolean column (NULL and 0 are false)
fn flag(v: Option<&Value>) -> bool {
    v.and_then(|v| v.as_i64()).unwrap_or(0) != 0
}

/// `GET /quests/<id>` (app/routers/quests.py `quest_detail`) - for a quest of the viewer's active world (one of another world, which
/// Python checks against the viewer's world access, is Python's)
fn quest_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let world = p.world.clone()?;
    let wid = world.get("id")?.as_i64()?;
    let q = row(db, "SELECT * FROM quests WHERE id = ?1", id)?;
    if q.get("world_id")?.as_i64() != Some(wid) || !crate::pages::section_ok(&p, Some(&world), "quests")? {
        return None;
    }
    if !flag(q.get("visible_to_players")) && !p.is_gm {
        return None;
    }
    let mut linked = vec![];
    if let Some(list) = json_or_empty_list(q.get("linked_entities_json"))?.as_array() {
        for l in list {
            let l = l.as_object()?;
            let eid = l.get("entity_id")?.as_i64()?;
            let role = match l.get("role") {
                None => json!(""),
                Some(v) => v.clone(),
            };
            if let Some(e) = row(db, "SELECT id, name FROM entities WHERE id = ?1", eid) {
                linked.push(json!({"entity": {"id": e.get("id")?, "name": e.get("name")?}, "role": role}));
            }
        }
    } else {
        return None;
    }
    let edit = crate::pages::edit_level(&p, &world, "quests")?;
    let can_edit = edit && (p.is_gm || p.role.as_deref() == Some("assistant") || q.get("created_by_user_id").and_then(|v| v.as_i64()) == Some(p.user_id));
    let d = nd_render::app::data();
    p.ctx.insert("parties".into(), Value::Array(rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name, id", wid)?));
    let mut st = db.prepare("SELECT * FROM quests WHERE world_id = ?1 AND id != ?2 ORDER BY title, id").ok()?;
    let others = st.query_map([wid, id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    p.ctx.insert("quests".into(), Value::Array(others.into_iter().map(Value::Object).collect()));
    p.ctx.insert("entities".into(), Value::Array(rows(db, "SELECT * FROM entities WHERE world_id = ?1 ORDER BY name, id", wid)?));
    p.ctx.insert("quest".into(), Value::Object(q));
    p.ctx.insert("linked_entities".into(), Value::Array(linked));
    p.ctx.insert("statuses".into(), d.get("quest_statuses")?.clone());
    p.ctx.insert("categories".into(), d.get("quest_categories")?.clone());
    p.ctx.insert("can_edit_this".into(), json!(can_edit));
    render_with(pages, "quests/detail.html", p.ctx, headers)
}

/// `_entity_templates_payload`: [{id, name, kind, fields}] of the templates this world may use (built-in first, by name)
pub fn entity_templates_payload(db: &Connection, wid: i64) -> Option<Vec<Value>> {
    let mut out = vec![];
    for t in rows(db, "SELECT * FROM entity_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name, id", wid)? {
        let t = t.as_object()?;
        out.push(json!({"id": t.get("id")?, "name": t.get("name")?, "kind": t.get("kind")?, "fields": json_or_empty_list(t.get("fields_json"))?}));
    }
    Some(out)
}

/// `GET /new?kind=&folder=&name=` (app/main.py `new_form`): the empty entity form, for a viewer with an active world
pub fn new_entity_form(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let q = |k: &str| crate::pages::query_param(uri, k);
    let kind = q("kind").unwrap_or_else(|| "character".into());
    let folder = q("folder").unwrap_or_default();
    let name: String = nd_render::text::py_strip(&q("name").unwrap_or_default()).chars().take(256).collect();
    let players = rows(
        &db,
        "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 \
         ORDER BY users.display_name, world_memberships.id",
        wid,
    )?;
    let mut st = db.prepare("SELECT DISTINCT folder FROM entities WHERE world_id = ?1 AND kind = ?2 AND folder IS NOT NULL").ok()?;
    let mut folders = st
        .query_map(rusqlite::params![wid, kind], |r| r.get::<_, String>(0))
        .ok()?
        .collect::<Result<Vec<_>, _>>()
        .ok()?
        .into_iter()
        .filter(|f| !f.is_empty())
        .collect::<Vec<_>>();
    folders.sort();
    folders.dedup();
    let ctx = &mut p.ctx;
    ctx.insert("entity".into(), Value::Null);
    ctx.insert("kind".into(), json!(kind));
    ctx.insert("world_players".into(), Value::Array(players));
    ctx.insert("allowed_player_ids".into(), json!([]));
    ctx.insert("folder_options".into(), json!(folders));
    ctx.insert("prefill_folder".into(), json!(folder));
    ctx.insert("prefill_name".into(), json!(name));
    ctx.insert("entity_templates".into(), Value::Array(entity_templates_payload(&db, wid)?));
    ctx.insert("custom_fields".into(), json!({}));
    ctx.insert("gallery_images".into(), Value::Array(crate::gallery::all_world_image_urls(&db, wid)?));
    render_with(pages, "entities/form.html", p.ctx, headers)
}

/// `GET /entity/<id>/edit` (app/main.py `edit_form`) - for an entity of the viewer's active world (the auth gate has already let
/// only an editor through)
fn entity_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let e = row(db, "SELECT * FROM entities WHERE id = ?1", id)?;
    if e.get("world_id")?.as_i64() != Some(wid) {
        return None;
    }
    let kind = e.get("kind")?.as_str()?.to_string();
    let players = rows(
        db,
        "SELECT users.* FROM users JOIN world_memberships ON world_memberships.user_id = users.id WHERE world_memberships.world_id = ?1 \
         ORDER BY users.display_name, world_memberships.id",
        wid,
    )?;
    let mut st = db.prepare("SELECT DISTINCT user_id FROM entity_player_access WHERE entity_id = ?1 ORDER BY user_id").ok()?;
    let allowed = st.query_map([id], |r| r.get::<_, i64>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut st = db.prepare("SELECT DISTINCT folder FROM entities WHERE world_id = ?1 AND kind = ?2 AND folder IS NOT NULL").ok()?;
    let mut folders: Vec<String> =
        st.query_map(rusqlite::params![wid, kind], |r| r.get::<_, String>(0)).ok()?.collect::<Result<Vec<_>, _>>().ok()?.into_iter().filter(|f| !f.is_empty()).collect();
    folders.sort();
    folders.dedup();
    let raw = e.get("custom_fields_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
    let custom: Value = serde_json::from_str(raw).ok()?;
    let ctx = &mut p.ctx;
    ctx.insert("entity".into(), Value::Object(e));
    ctx.insert("kind".into(), json!(kind));
    ctx.insert("world_players".into(), Value::Array(players));
    ctx.insert("allowed_player_ids".into(), json!(allowed));
    ctx.insert("folder_options".into(), json!(folders));
    ctx.insert("prefill_folder".into(), json!(""));
    ctx.insert("entity_templates".into(), Value::Array(entity_templates_payload(db, wid)?));
    ctx.insert("custom_fields".into(), custom);
    ctx.insert("gallery_images".into(), Value::Array(crate::gallery::all_world_image_urls(db, wid)?));
    render_with(pages, "entities/form.html", p.ctx, headers)
}

/// `GET /rules` (app/main.py `rules_page`): the world's rules (or the bundled core rules) with ::: blocks, statblocks, heading ids,
/// sections and the world's rules.json overlay
pub fn rules_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    use nd_render::rules;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if let Some(w) = &world {
        if !crate::pages::section_ok(&p, Some(w), "rules")? {
            return None;
        }
    }
    let own = world.as_ref().and_then(|w| w.get("rules_md")).and_then(|v| v.as_str()).filter(|s| !nd_render::text::py_strip(s).is_empty());
    let md = match own {
        Some(s) => s.to_string(),
        None => {
            let path = cfg.templates_dir.parent()?.join("core_rules.md");
            match std::fs::read(&path) {
                Ok(bytes) => String::from_utf8(bytes).ok()?, // invalid UTF-8 ("errors=ignore" in Python): Python's
                Err(_) => String::new(),
            }
        }
    };
    let md = nd_render::text::strip_legacy_anchors(&md);
    let md = nd_render::text::strip_legacy_anchors(&rules::strict_unescape(&md)?);
    let content = if nd_render::text::py_strip(&md).is_empty() {
        "<p>No rules have been added for this world yet.</p>".to_string()
    } else {
        rules::render_rules_content(&md, p.is_gm)
    };
    let mut sections = rules::split_sections_h23(&content);
    let mut tabs = None;
    if let Some(w) = &world {
        let overlay = rules::parse_overlay(w.get("rules_json").and_then(|v| v.as_str())).ok()?;
        let (s, t) = rules::apply_overlay(sections, overlay.as_ref(), p.is_gm)?;
        sections = s;
        tabs = t;
    }
    let toc: Vec<Value> = sections
        .iter()
        .filter(|s| s.get("id").and_then(|v| v.as_str()).map_or(false, |x| !x.is_empty()))
        .map(|s| json!({"level": s["level"], "text": s["title"], "id": s["id"]}))
        .collect();
    let is_custom = own.is_some();
    let c = &mut p.ctx;
    c.insert("toc".into(), Value::Array(toc));
    c.insert("sections".into(), Value::Array(sections));
    c.insert("tabs_mode".into(), json!(tabs.is_some()));
    c.insert("tabs".into(), tabs.map(Value::Array).unwrap_or(Value::Null));
    c.insert("is_custom_rules".into(), json!(is_custom));
    c.insert("can_edit_rules".into(), json!(p.is_gm));
    render_with(pages, "rules.html", p.ctx, headers)
}

/// `GET /sessions/<id>` (app/routers/sessions.py `session_detail`): the session page - its party, linked combats, featured entities and
/// characters, the party's characters, the session's own facts (GM-only ones for a GM only) with their tag cloud, prep, loot, and the
/// gallery picker for a GM
fn session_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(gs) = row(db, "SELECT * FROM game_sessions WHERE id = ?1", id) else { return Some(refusal(404)) };
    let gw = gs.get("world_id")?.as_i64()?;
    // world_row_visible: the session's world must be reachable, and the sessions section open to this role
    if !p.is_gm {
        if db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [gw, p.user_id], |_| Ok(())).is_err() {
            return Some(refusal(404));
        }
        let w = row(db, "SELECT * FROM worlds WHERE id = ?1", gw)?;
        let s = |k: &str| w.get(k).and_then(|v| v.as_str());
        if nd_perm::sections::section_level(false, p.role.as_deref(), s("section_access_json"), s("custom_kinds_json"), "sessions")? == "none" {
            return Some(refusal(404));
        }
    }
    let parties = rows(db, "SELECT * FROM parties WHERE world_id = ?1 ORDER BY name", gw)?;
    let combats = rows(db, "SELECT * FROM combat_sessions WHERE game_session_id = ?1", id)?;
    let npcs = json_or_empty_list(gs.get("npcs_json"))?;
    let list = npcs.as_array()?.clone();
    let kind_of = |n: &Value| n.get("kind").and_then(|k| k.as_str()).unwrap_or("entity").to_string();
    let (mut names, mut selected) = (vec![], vec![]);
    for n in &list {
        let eid = n.get("entity_id")?.as_i64()?;
        if kind_of(n) == "player_character" {
            if let Some(pc) = row(db, "SELECT name FROM player_characters WHERE id = ?1", eid) {
                names.push(pc.get("name").cloned().unwrap_or(Value::Null));
                selected.push(json!(format!("pc:{eid}")));
            }
        } else if n.get("kind").is_none() || kind_of(n) == "entity" {
            if let Some(e) = row(db, "SELECT name FROM entities WHERE id = ?1", eid) {
                names.push(e.get("name").cloned().unwrap_or(Value::Null));
                selected.push(json!(eid));
            }
        }
    }
    let party_pcs = match gs.get("party_id").and_then(|v| v.as_i64()) {
        Some(pid) => match row(db, "SELECT member_pc_ids_json FROM parties WHERE id = ?1", pid) {
            Some(party) => {
                let ids = json_or_empty_list(party.get("member_pc_ids_json"))?;
                let ids: Vec<i64> = ids.as_array()?.iter().map(|v| v.as_i64()).collect::<Option<_>>()?;
                if ids.is_empty() { vec![] } else {
                    let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
                    let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE id IN ({list})")).ok()?;
                    let got: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
                    got.into_iter().map(Value::Object).collect()
                }
            }
            None => vec![],
        },
        None => vec![],
    };
    let vis = if p.is_gm { "" } else { " AND visible_to_players IS NOT 0" };
    let facts = rows(db, &format!("SELECT * FROM facts WHERE game_session_id = ?1{vis} ORDER BY created_at ASC, id ASC"), id)?;
    let mut counts: Vec<(String, i64)> = vec![];
    for f in &facts {
        for t in f.get("tags").and_then(|v| v.as_str()).unwrap_or("").split(',') {
            let t = crate::edits::py_strip_pub(t);
            if t.is_empty() {
                continue;
            }
            match counts.iter_mut().find(|(k, _)| *k == t) {
                Some(c) => c.1 += 1,
                None => counts.push((t, 1)),
            }
        }
    }
    counts.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let gallery = match (wid, p.is_gm) {
        (Some(w), true) => crate::gallery::all_world_image_urls(db, w)?,
        _ => vec![],
    };
    let next = gs.get("session_num").cloned().unwrap_or(Value::Null);
    let prep = json_or_empty_list(gs.get("prep_json"))?;
    let loot = json_or_empty_list(gs.get("loot_json"))?;
    let mut gsv = gs.clone();
    let live = gs.get("live_transcript").and_then(|v| v.as_str()).is_some_and(|t| !t.is_empty());
    gsv.insert("has_live_transcript".into(), json!(live));
    let ctx = &mut p.ctx;
    ctx.insert("gsession".into(), Value::Object(gsv));
    ctx.insert("gallery_images".into(), Value::Array(gallery));
    ctx.insert("parties".into(), Value::Array(parties));
    ctx.insert("next_num".into(), next);
    ctx.insert("linked_combats".into(), Value::Array(combats));
    ctx.insert("npc_names".into(), Value::Array(names));
    ctx.insert("party_pcs".into(), Value::Array(party_pcs));
    ctx.insert("npc_selected_json".into(), Value::Array(selected));
    ctx.insert("prep".into(), prep);
    ctx.insert("loot".into(), loot);
    ctx.insert("npcs".into(), npcs);
    ctx.insert("facts".into(), Value::Array(facts));
    ctx.insert("fact_top_tags".into(), json!(counts.into_iter().map(|(t, n)| json!([t, n])).collect::<Vec<_>>()));
    render_with(pages, "sessions/detail.html", p.ctx, headers)
}

/// FastAPI's answer to `HTTPException(status)` with no detail: {"detail": "<the status phrase>"}
pub fn refusal(status: u16) -> Response<Body> {
    let code = axum::http::StatusCode::from_u16(status).unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    let phrase = code.canonical_reason().unwrap_or("Error");
    Response::builder().status(code).header("content-type", "application/json").header("x-nd-served-by", "gateway")
        .body(Body::from(format!("{{\"detail\":\"{phrase}\"}}"))).unwrap()
}

/// The ids in a party's member list column when it is a list of plain ints (None: Python's `in_()` / `in` would read it its own way)
pub(crate) fn party_ids(v: Option<&Value>) -> Option<Vec<i64>> {
    match json_or_empty_list(v)? {
        Value::Array(a) => a.iter().map(|x| x.as_i64()).collect(),
        _ => None,
    }
}

fn in_list(ids: &[i64]) -> String {
    ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",")
}

/// `world_row_visible(request, db, world_id, section)` for a row of the active world; None for another world's row (Python's)
pub(crate) fn row_world_visible(db: &Connection, p: &PageCtx, world_id: i64, section: &str) -> Option<(bool, Map<String, Value>)> {
    let w = row(db, "SELECT * FROM worlds WHERE id = ?1", world_id);
    if p.is_gm {
        return Some((w.is_some(), w.unwrap_or_default()));
    }
    let active = p.world.as_ref().and_then(|a| a.get("id")).and_then(|v| v.as_i64());
    let Some(w) = w else { return Some((false, Map::new())) };
    let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [world_id, p.user_id], |_| Ok(())).is_ok();
    if !member {
        return Some((false, w));
    }
    if active != Some(world_id) {
        return None; // the viewer's role is read for the active world; another world's row: Python's
    }
    Some((crate::pages::section_ok(p, Some(&w), section)?, w))
}

/// `GET /parties/<id>` (app/routers/parties.py `party_detail`)
fn party_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(party) = row(db, "SELECT * FROM parties WHERE id = ?1", id) else { return Some(refusal(404)) };
    let wid = party.get("world_id")?.as_i64()?;
    let (ok, pw) = row_world_visible(db, &p, wid, "parties")?;
    if !ok {
        return Some(refusal(404));
    }
    let gm = p.is_gm;
    let pc_ids = party_ids(party.get("member_pc_ids_json"))?;
    let raw_entity_ids = party_ids(party.get("member_entity_ids_json"))?;
    let pcs: Vec<Map<String, Value>> = if pc_ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT * FROM player_characters WHERE id IN ({}) AND ?1 IS NOT NULL ORDER BY id", in_list(&pc_ids)), 0)?
            .into_iter().filter_map(|v| v.as_object().cloned()).collect()
    };
    let my_pc_ids: Vec<i64> = if gm { vec![] } else {
        pcs.iter().filter(|pc| pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)).filter_map(|pc| pc.get("id")?.as_i64()).collect()
    };
    // deps.filter_visible_entities for a non-GM viewer
    let vis = if gm { String::new() } else {
        format!(" AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let visible_ids: Vec<i64> = if raw_entity_ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT id FROM entities WHERE id IN ({}) AND world_id = ?1{vis} ORDER BY id", in_list(&raw_entity_ids)), wid)?
            .iter().filter_map(|r| r.get("id")?.as_i64()).collect()
    };
    let entity_ids: Vec<i64> = if gm { raw_entity_ids.clone() } else { raw_entity_ids.iter().copied().filter(|i| visible_ids.contains(i)).collect() };
    let member_entities = if visible_ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT * FROM entities WHERE id IN ({}) AND ?1 IS NOT NULL ORDER BY id", in_list(&visible_ids)), 0)?
    };
    let all_pcs = rows(db, "SELECT * FROM player_characters WHERE world_id = ?1 ORDER BY name, id", wid)?;
    let all_entities = rows(db, &format!(
        "SELECT * FROM entities WHERE world_id = ?1 AND kind IN ('character', 'creature'){vis} ORDER BY name, id"), wid)?;
    let quests = if gm {
        rows(db, "SELECT * FROM quests WHERE assigned_party_id = ?1 ORDER BY id", id)?
    } else if crate::pages::section_ok(&p, Some(&pw), "quests")? {
        rows(db, "SELECT * FROM quests WHERE assigned_party_id = ?1 AND visible_to_players IS NOT 0 ORDER BY id", id)?
    } else {
        vec![]
    };
    let loot = crate::edits::load_loot(&party)?;
    let vitals = crate::edits::member_vitals(db, &pcs, Some(4))?;
    let vital_of = |pid: i64| vitals.iter().find(|v| v["id"].as_i64() == Some(pid));
    let levelup_names: Vec<Value> = pcs.iter()
        .filter(|pc| pc.get("id").and_then(|v| v.as_i64()).and_then(|i| vital_of(i)).map_or(false, |v| v["levelup"] == json!(true)))
        .map(|pc| pc.get("name").cloned().unwrap_or(Value::Null)).collect();
    let mut conds: Vec<(String, i64)> = vec![];
    let mut downs = 0;
    for v in &vitals {
        if v["down"] == json!(true) {
            downs += 1;
        }
        for c in v["conditions"].as_array()? {
            let k = nd_render::text::py_strip(c.as_str()?).to_lowercase();
            match conds.iter_mut().find(|x| x.0 == k) {
                Some(x) => x.1 += 1,
                None => conds.push((k, 1)),
            }
        }
    }
    conds.sort_by(|a, b| b.1.cmp(&a.1));
    let mut summary = conds.iter().map(|(n, c)| if *c > 1 { format!("{n}×{c}") } else { n.clone() }).collect::<Vec<_>>().join(", ");
    if downs > 0 {
        summary = if summary.is_empty() { format!("{downs} DOWN") } else { format!("{summary}, {downs} DOWN") };
    }
    let unclaimed = loot.iter().filter(|it| !it.get("claimed_by").map_or(false, |c| c.as_array().map_or(true, |a| !a.is_empty()))).count();
    let can_sessions = crate::pages::section_ok(&p, Some(&pw), "sessions")?;
    let sessions = if can_sessions { rows(db, "SELECT * FROM game_sessions WHERE party_id = ?1 ORDER BY session_num DESC, id", id)? } else { vec![] };
    let combats = if crate::pages::section_ok(&p, Some(&pw), "combat")? {
        rows(db, "SELECT * FROM combat_sessions WHERE party_id = ?1 ORDER BY created_at DESC, id", id)?
    } else { vec![] };
    let events = if crate::pages::section_ok(&p, Some(&pw), "calendar")? {
        rows(db, "SELECT * FROM calendar_events WHERE party_id = ?1 ORDER BY day DESC, id", id)?
    } else { vec![] };
    // _party_edit_level
    let edit_level = if crate::pages::edit_level(&p, &pw, "parties")? {
        if gm || p.role.as_deref() == Some("assistant") {
            "full"
        } else if pc_ids.is_empty() {
            "none"
        } else {
            let owners = rows(db, &format!("SELECT owner_user_id FROM player_characters WHERE id IN ({}) AND ?1 IS NOT NULL", in_list(&pc_ids)), 0)?;
            if owners.iter().any(|o| o.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) { "member" } else { "none" }
        }
    } else {
        "none"
    };
    let xp = match json_or_empty_list(party.get("xp_json"))? {
        Value::Array(mut a) => {
            a.reverse();
            a
        }
        _ => return None,
    };
    let mut short_rest = false;
    let mut seen_tpl: Vec<i64> = vec![];
    for pc in &pcs {
        let Some(tid) = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0) else { continue };
        if seen_tpl.contains(&tid) {
            continue;
        }
        seen_tpl.push(tid);
        if let Some(t) = row(db, "SELECT * FROM sheet_templates WHERE id = ?1", tid) {
            short_rest |= crate::sheets::Tpl::load(t)?.has_short_rest();
        }
    }
    let mut names = Map::new();
    for pc in &pcs {
        names.insert(pc.get("id")?.as_i64()?.to_string(), pc.get("name").cloned().unwrap_or(Value::Null));
    }
    let mut by_id = Map::new();
    for v in &vitals {
        by_id.insert(v["id"].as_i64()?.to_string(), v.clone());
    }
    let c = &mut p.ctx;
    c.insert("party".into(), Value::Object(party));
    c.insert("member_pcs".into(), Value::Array(pcs.into_iter().map(Value::Object).collect()));
    c.insert("member_entities".into(), Value::Array(member_entities));
    c.insert("all_pcs".into(), Value::Array(all_pcs));
    c.insert("all_entities".into(), Value::Array(all_entities));
    c.insert("assigned_quests".into(), Value::Array(quests));
    c.insert("loot".into(), Value::Array(loot.into_iter().map(Value::Object).collect()));
    c.insert("pc_ids".into(), json!(pc_ids));
    c.insert("entity_ids".into(), json!(entity_ids));
    c.insert("edit_level".into(), json!(edit_level));
    c.insert("member_names".into(), json!({"__intkeys__": names}));
    c.insert("my_pc_ids".into(), json!(my_pc_ids));
    c.insert("member_vitals".into(), Value::Array(vitals));
    c.insert("vitals_by_id".into(), json!({"__intkeys__": by_id}));
    c.insert("xp_ledger".into(), Value::Array(xp));
    c.insert("history_sessions".into(), Value::Array(sessions));
    c.insert("history_combats".into(), Value::Array(combats));
    c.insert("history_events".into(), Value::Array(events));
    c.insert("can_view_sessions".into(), json!(can_sessions));
    c.insert("levelup_names".into(), Value::Array(levelup_names));
    c.insert("condition_summary".into(), json!(summary));
    c.insert("unclaimed_loot".into(), json!(unclaimed));
    c.insert("short_rest_available".into(), json!(short_rest));
    render_with(pages, "parties/detail.html", p.ctx, headers)
}

/// `GET /session-log/<id>` (app/routers/sessions.py `session_log_detail`): a session as the table reads it - the published player
/// summary, the recap pickers seeded from the last recap job for this audience, and the audio / pictures / videos this viewer may see
fn session_log_detail(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(gs) = row(db, "SELECT * FROM game_sessions WHERE id = ?1", id) else { return Some(refusal(404)) };
    let wid = gs.get("world_id")?.as_i64()?;
    // auth.user_can_access_world
    let world_exists = row(db, "SELECT id FROM worlds WHERE id = ?1", wid).is_some();
    let member = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |_| Ok(())).is_ok();
    if !world_exists || !(p.is_gm || member) {
        return Some(refusal(404));
    }
    let gm = p.is_gm;
    let summary = gs.get("player_summary").and_then(|v| v.as_str()).unwrap_or("");
    let published = gs.get("player_summary_published").and_then(|v| v.as_i64()).unwrap_or(0) != 0 && !nd_render::text::py_strip(summary).is_empty();
    let html = if published { nd_render::md::render_md(summary) } else { String::new() };
    let audience = if gm { "gm" } else { "players" };
    let job = db.query_row("SELECT * FROM audio_jobs WHERE purpose = 'session_log_recap' AND game_session_id = ?1 AND audience = ?2 \
        ORDER BY created_at DESC, id DESC LIMIT 1", rusqlite::params![id, audience], |r| crate::layout::row_map(r)).ok();
    let defaults = match &job {
        Some(j) => {
            let int_or = |k: &str, d: i64| j.get(k).and_then(|v| v.as_i64()).unwrap_or(d);
            let txt = |k: &str| j.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            json!({"model": txt("model"), "think": j.get("think").and_then(|v| v.as_i64()).map_or(true, |t| t != 0),
                   "use_rag": j.get("use_rag").and_then(|v| v.as_i64()).unwrap_or(0) != 0,
                   "rag_entity_limit": int_or("rag_entity_limit", 15), "rag_notes_limit": int_or("rag_notes_limit", 5),
                   "extra_instructions": txt("extra_instructions"), "min_tokens": j.get("min_tokens").cloned().unwrap_or(Value::Null),
                   "max_tokens": j.get("max_tokens").cloned().unwrap_or(Value::Null),
                   "strictness": Some(txt("condense_strictness")).filter(|s| !s.is_empty()).unwrap_or_else(|| "guideline".into())})
        }
        None => json!({"model": "", "think": true, "use_rag": false, "rag_entity_limit": 15, "rag_notes_limit": 5, "extra_instructions": "",
                       "min_tokens": null, "max_tokens": null, "strictness": "guideline"}),
    };
    // session_media.viewer_media
    let visible = |r: &Map<String, Value>| gm || r.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    let mut audio = vec![];
    for cid in crate::edits::sm_clip_ids(&gs)? {
        if let Some(c) = row(db, "SELECT * FROM audio_clips WHERE id = ?1", cid).filter(|c| c.get("world_id").and_then(|v| v.as_i64()) == Some(wid)) {
            if visible(&c) {
                audio.push(crate::edits::sm_audio_dict(&c));
            }
        }
    }
    let items = crate::edits::sm_items(&gs)?;
    let mut videos = vec![];
    let mut images = vec![];
    for it in &items {
        match it.get("kind").and_then(|k| k.as_str()) {
            Some("video") => {
                let cid = it.get("clip_id")?.as_i64()?;
                if let Some(c) = row(db, "SELECT * FROM video_clips WHERE id = ?1", cid).filter(|c| c.get("world_id").and_then(|v| v.as_i64()) == Some(wid)) {
                    if visible(&c) {
                        videos.push(crate::edits::sm_video_dict(&c, it.get("uid")?.as_str()?));
                    }
                }
            }
            Some("image") if gm || it.get("visible") == Some(&json!(true)) => {
                images.push(json!({"uid": it.get("uid"), "url": it.get("url"), "caption": it.get("caption"),
                    "visible_to_players": it.get("visible") == Some(&json!(true)), "own": it.get("own") == Some(&json!(true))}));
            }
            _ => {}
        }
    }
    let c = &mut p.ctx;
    c.insert("gsession".into(), Value::Object(gs));
    c.insert("player_recap_published".into(), json!(published));
    c.insert("player_summary_html".into(), json!(html));
    c.insert("recap_defaults".into(), defaults);
    c.insert("recap_audio".into(), Value::Array(audio.clone()));
    c.insert("session_media".into(), json!({"audio": audio, "videos": videos, "images": images}));
    render_with(pages, "sessions/player_detail.html", p.ctx, headers)
}

/// `str(v)` of a stored value as Python prints it (None for a float or a list: not reproduced)
fn py_str(v: Option<&Value>) -> Option<String> {
    Some(match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(t)) => t.clone(),
        Some(Value::Number(n)) if n.is_i64() => n.to_string(),
        Some(Value::Bool(b)) => if *b { "True".into() } else { "False".into() },
        _ => return None,
    })
}

fn cell_text(text: String, down: bool, prefix: &str) -> Value {
    json!({"kind": "text", "text": text, "down": down, "prefix": prefix})
}

fn cell_na() -> Value {
    json!({"kind": "na"})
}

fn cell_chips(chips: Vec<Value>) -> Value {
    json!({"kind": "chips", "chips": chips})
}

/// `GET /parties/<id>/roster` (app/routers/parties.py `party_roster`, `_roster_table`): the members side by side, every row from what
/// each member's own system has
fn party_roster(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(party) = row(db, "SELECT * FROM parties WHERE id = ?1", id) else { return Some(refusal(404)) };
    let wid = party.get("world_id")?.as_i64()?;
    let (ok, pw) = row_world_visible(db, &p, wid, "parties")?;
    if !ok {
        return Some(refusal(404));
    }
    let gm = p.is_gm;
    let active = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let full = gm || (active == Some(wid) && crate::pages::edit_level(&p, &pw, "parties")? && p.role.as_deref() == Some("assistant"));
    if !(full || pw.get("players_see_party").and_then(|v| v.as_i64()).unwrap_or(0) != 0) {
        return Some(refusal(404));
    }
    let ids = crate::edits::member_ids(party.get("member_pc_ids_json"))?;
    let mut pcs: Vec<Map<String, Value>> = if ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT * FROM player_characters WHERE id IN ({}) AND world_id = ?1 ORDER BY id", in_list(&ids)), wid)?
            .into_iter().filter_map(|v| v.as_object().cloned()).collect()
    };
    let companions = if gm {
        let e = crate::edits::member_ids(party.get("member_entity_ids_json"))?;
        if e.is_empty() { vec![] } else { rows(db, &format!("SELECT * FROM entities WHERE id IN ({}) AND ?1 IS NOT NULL ORDER BY id", in_list(&e)), 0)? }
    } else { vec![] };
    // _roster_table
    let name_of = |m: &Map<String, Value>| m.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    pcs.sort_by(|a, b| name_of(a).cmp(&name_of(b)));
    let vitals = crate::edits::member_vitals(db, &pcs, None)?;
    struct Info { pc: Map<String, Value>, v: Value, native: bool, shock: i64, pp: i64, mp: i64, tpl: Option<crate::sheets::Tpl>, cf: Map<String, Value> }
    let mut columns = vec![];
    let mut info: Vec<Info> = vec![];
    for pc in &pcs {
        let pid = pc.get("id")?.as_i64()?;
        let v = vitals.iter().find(|v| v["id"].as_i64() == Some(pid))?.clone();
        let (_, shock, pp, mp, native) = crate::edits::pc_maxima(db, pc)?;
        let tpl = crate::sheets::pc_template(db, pc)?;
        let t = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let sub = if native {
            [format!("Lv {}", py_str(pc.get("level"))?), t("race"), t("char_class"), t("player_name")].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" · ")
        } else {
            let tn = tpl.as_ref().and_then(|x| x.row.get("name")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            [tn, t("player_name")].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" · ")
        };
        columns.push(json!({"id": pid, "name": pc.get("name"), "sub": sub}));
        info.push(Info { pc: pc.clone(), v, native, shock, pp, mp, cf: crate::sheets::custom_fields(pc)?, tpl });
    }
    let mut groups: Vec<(String, Vec<Value>)> = vec![];
    let mut add = |title: &str, label: String, cells: Vec<Value>| {
        let row = json!({"label": label, "cells": cells});
        match groups.iter_mut().find(|g| g.0 == title) {
            Some(g) => g.1.push(row),
            None => groups.push((title.to_string(), vec![row])),
        }
    };
    let any_native = info.iter().any(|i| i.native);
    let truthy_num = |v: &Value| v.as_i64().map_or(false, |n| n != 0);
    let mut labels: Vec<String> = vec![];
    for i in &info {
        if truthy_num(&i.v["max_hp"]) {
            let l = i.v["hp_label"].as_str()?.to_string();
            if !labels.contains(&l) {
                labels.push(l);
            }
        }
    }
    let mixed = labels.len() > 1;
    let mut cells = vec![];
    for i in &info {
        if truthy_num(&i.v["max_hp"]) {
            let mut t = format!("{} / {}", py_str(Some(&i.v["hp"]))?, py_str(Some(&i.v["max_hp"]))?);
            if truthy_num(&i.v["temp_hp"]) {
                t += &format!(" (+{} temp)", py_str(Some(&i.v["temp_hp"]))?);
            }
            cells.push(cell_text(t, i.v["down"] == json!(true), if mixed { i.v["hp_label"].as_str()? } else { "" }));
        } else {
            cells.push(cell_na());
        }
    }
    add("Condition", if mixed { "HP / vital".into() } else { labels.first().cloned().unwrap_or_else(|| "HP".into()) }, cells);
    let col = |pc: &Map<String, Value>, k: &str| pc.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
    if any_native {
        add("Condition", "Shock".into(), info.iter().map(|i| if i.native { cell_text(format!("{} / {}", col(&i.pc, "shock_current"), i.shock), false, "") } else { cell_na() }).collect());
        add("Condition", "PP / MP".into(), info.iter().map(|i| if i.native {
            cell_text(format!("{}/{} · {}/{}", col(&i.pc, "pp_current"), i.pp, col(&i.pc, "mp_current"), i.mp), false, "")
        } else { cell_na() }).collect());
    }
    add("Condition", "Conditions".into(), info.iter().map(|i| cell_chips(i.v["conditions"].as_array().cloned().unwrap_or_default())).collect());
    if any_native {
        let mut derived: Vec<(i64, Map<String, Value>)> = vec![];
        for i in info.iter().filter(|i| i.native) {
            derived.push((i.pc.get("id")?.as_i64()?, crate::edits::derived(&i.pc)?));
        }
        let d_of = |i: &Info| derived.iter().find(|d| Some(d.0) == i.pc.get("id").and_then(|v| v.as_i64())).map(|d| &d.1);
        for (sid, label) in [("str", "STR"), ("dex", "DEX"), ("bod", "BOD"), ("per", "PER"), ("wil", "WIL"), ("int", "INT"), ("cha", "CHA"), ("itu", "ITU")] {
            let mut cells = vec![];
            for i in &info {
                let val = if i.native {
                    // {st["id"]: st.get("value")}: the last stat with this id wins
                    d_of(i)?.get("stats")?.as_array()?.iter().filter_map(|s| s.as_object())
                        .filter(|s| s.get("id").and_then(|v| v.as_str()) == Some(sid)).last().map(|s| s.get("value").cloned().unwrap_or(Value::Null))
                } else { None };
                cells.push(match val {
                    Some(v) if !v.is_null() => cell_text(py_str(Some(&v))?, false, ""),
                    _ => cell_na(),
                });
            }
            add("Attributes", label.into(), cells);
        }
        let mut ca = vec![];
        let mut speed = vec![];
        let mut edges = vec![];
        let mut cyber = vec![];
        for i in &info {
            if !i.native {
                for v in [&mut ca, &mut speed, &mut edges, &mut cyber] {
                    v.push(cell_na());
                }
                continue;
            }
            let d = d_of(i)?;
            ca.push(cell_text(py_str(d.get("ca_derived"))?, false, ""));
            speed.push(cell_text(py_str(d.get("speed_derived"))?, false, ""));
            edges.push(cell_chips(["minor_edge", "major_edge"].iter().filter_map(|k| d.get(*k).filter(|e| e.as_str().map_or(false, |s| !s.is_empty())).cloned()).collect()));
            let mut chips = vec![];
            for c in d.get("cyberware")?.as_array()?.iter().filter_map(|c| c.as_object()) {
                match c.get("name") {
                    None | Some(Value::Null) => {}
                    Some(Value::String(n)) if n.is_empty() => {}
                    Some(v) => chips.push(json!(py_str(Some(v))?)),
                }
            }
            cyber.push(cell_chips(chips));
        }
        add("Attributes", "Cyber Adapt.".into(), ca);
        add("Attributes", "Speed".into(), speed);
        add("Build", "Edges".into(), edges);
        add("Build", "Cyberware".into(), cyber);
    }
    let n = info.len();
    let mut track_rows: Vec<(String, Vec<(usize, Value)>)> = vec![];
    let mut field_rows: Vec<((String, String), Vec<(usize, Value)>)> = vec![];
    for (idx, i) in info.iter().enumerate() {
        if i.native {
            continue;
        }
        for t in i.v["resources"].as_array()? {
            if t["id"] == i.v["hp_id"] {
                continue;
            }
            let label = py_str(Some(&t["label"]))?;
            let c = cell_text(format!("{} / {}", py_str(Some(&t["current"]))?, py_str(Some(&t["max"]))?), false, "");
            match track_rows.iter_mut().find(|r| r.0 == label) {
                Some(r) => { r.1.retain(|x| x.0 != idx); r.1.push((idx, c)) }
                None => track_rows.push((label, vec![(idx, c)])),
            }
        }
        if let Some(t) = &i.tpl {
            for (title, fields) in crate::edits::roster_field_groups(t)? {
                for f in fields {
                    if f.get("type").and_then(|v| v.as_str()) == Some("resource") {
                        continue;
                    }
                    let txt = crate::edits::field_value_text(f, &i.cf)?;
                    let src = match f.get("label") { Some(Value::String(l)) if !l.is_empty() => Some(l.clone()), Some(v) if !v.is_null() && v != &json!("") => return None, _ => f.get("id").and_then(|v| v.as_str()).map(str::to_string) };
                    let short = match src { Some(l) if !l.is_empty() => crate::edits::sheet_short_label_pub(&l)?, _ => String::new() };
                    let key = (title.clone(), short);
                    let c = if txt.is_empty() { cell_na() } else { cell_text(txt, false, "") };
                    match field_rows.iter_mut().find(|r| r.0 == key) {
                        Some(r) => { r.1.retain(|x| x.0 != idx); r.1.push((idx, c)) }
                        None => field_rows.push((key, vec![(idx, c)])),
                    }
                }
            }
        }
    }
    let fill = |cells: &Vec<(usize, Value)>| (0..n).map(|k| cells.iter().find(|x| x.0 == k).map(|x| x.1.clone()).unwrap_or_else(cell_na)).collect::<Vec<_>>();
    for (label, cells) in &track_rows {
        add("Resources", label.clone(), fill(cells));
    }
    for ((title, label), cells) in &field_rows {
        add(title, label.clone(), fill(cells));
    }
    let groups: Vec<Value> = groups.into_iter().map(|(t, rows)| json!({"title": t, "rows": rows})).collect();
    let c = &mut p.ctx;
    c.insert("party".into(), Value::Object(party));
    c.insert("table".into(), json!({"columns": columns, "groups": groups}));
    c.insert("companions".into(), Value::Array(companions));
    c.insert("is_gm".into(), json!(gm));
    render_with(pages, "parties/roster.html", p.ctx, headers)
}

/// `GET /parties/<id>/summary` (app/routers/parties.py `party_summary`): the printable party card
fn party_summary(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let Some(party) = row(db, "SELECT * FROM parties WHERE id = ?1", id) else { return Some(refusal(404)) };
    let wid = party.get("world_id")?.as_i64()?;
    let (ok, pw) = row_world_visible(db, &p, wid, "parties")?;
    if !ok {
        return Some(refusal(404));
    }
    let gm = p.is_gm;
    let pc_ids = party_ids(party.get("member_pc_ids_json"))?;
    let raw_entity_ids = party_ids(party.get("member_entity_ids_json"))?;
    let pcs: Vec<Map<String, Value>> = if pc_ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT * FROM player_characters WHERE id IN ({}) AND ?1 IS NOT NULL ORDER BY id", in_list(&pc_ids)), 0)?
            .into_iter().filter_map(|v| v.as_object().cloned()).collect()
    };
    let vis = if gm { String::new() } else {
        format!(" AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {}))", p.user_id)
    };
    let visible: Vec<i64> = if raw_entity_ids.is_empty() { vec![] } else {
        rows(db, &format!("SELECT id FROM entities WHERE id IN ({}) AND world_id = ?1{vis} ORDER BY id", in_list(&raw_entity_ids)), wid)?
            .iter().filter_map(|r| r.get("id")?.as_i64()).collect()
    };
    let ents = if visible.is_empty() { vec![] } else { rows(db, &format!("SELECT * FROM entities WHERE id IN ({}) AND ?1 IS NOT NULL ORDER BY id", in_list(&visible)), 0)? };
    let quests = if gm {
        rows(db, "SELECT * FROM quests WHERE assigned_party_id = ?1 ORDER BY id", id)?
    } else if crate::pages::section_ok(&p, Some(&pw), "quests")? {
        rows(db, "SELECT * FROM quests WHERE assigned_party_id = ?1 AND visible_to_players IS NOT 0 ORDER BY id", id)?
    } else { vec![] };
    let loot = json_or_empty_list(party.get("loot_json"))?;
    let vitals = crate::edits::member_vitals(db, &pcs, Some(4))?;
    let mut names = Map::new();
    for pc in &pcs {
        names.insert(pc.get("id")?.as_i64()?.to_string(), pc.get("name").cloned().unwrap_or(Value::Null));
    }
    p.ctx.remove("worlds"); // the handler hands the template no worlds list
    let c = &mut p.ctx;
    c.insert("party".into(), Value::Object(party));
    c.insert("member_pcs".into(), Value::Array(pcs.into_iter().map(Value::Object).collect()));
    c.insert("member_entities".into(), Value::Array(ents));
    c.insert("member_vitals".into(), Value::Array(vitals));
    c.insert("loot".into(), loot);
    c.insert("assigned_quests".into(), Value::Array(quests));
    c.insert("member_names".into(), json!({"__intkeys__": names}));
    render_with(pages, "parties/summary.html", p.ctx, headers)
}
