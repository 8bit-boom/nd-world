//! The smaller one-shot AI tools the gateway answers itself (the routes app/ai_background.py can run in the background): each `plan`
//! reads the request and the database the way the Python route does, then hands back the rest - the model call and the write - as a
//! future. A refusal Python words itself is answered here; anything else (a 422, a 403, a backend that is not Unsloth Studio) is Python's.

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode};
use serde_json::{json, Map, Value};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use crate::ai::{backend, generate_chat, py_repr, small_form, Backend};
use crate::config::Config;
use crate::layout::PageCtx;

pub type Fut = Pin<Box<dyn Future<Output = Response<Body>> + Send>>;

/// The paths this module serves
pub fn serves(path: &str) -> bool {
    matches!(path, "/boards/generate-mystery" | "/calendar/ai-day" | "/calendar/ai-design")
        || (path.starts_with("/combat/") && path.ends_with("/ai-tactics"))
        || (path.starts_with("/parties/") && path.ends_with("/ai-insights"))
        || path == "/tables/generate-ai"
        || (path.starts_with("/api/sessions/") && path.ends_with("/prep/generate"))
        || path == "/api/kiy/build-model"
        || crate::media_rename::serves(path)
}

/// None: not one of these paths; Some(None): the request is Python's; Some(Some(f)): the gateway's answer
pub fn plan(cfg: &Config, db: &rusqlite::Connection, p: &PageCtx, path: &str, headers: &HeaderMap, body: &[u8]) -> Option<Option<Fut>> {
    if !serves(path) {
        return None;
    }
    let cfg = Arc::new(cfg.clone());
    Some(match path {
        "/boards/generate-mystery" => mystery_plan(cfg, db, p, headers, body),
        _ if path.starts_with("/combat/") => tactics_plan(db, p, path),
        _ if path.starts_with("/parties/") => insights_plan(db, p, path),
        "/tables/generate-ai" => table_plan(cfg, db, p, headers, body),
        _ if path.ends_with("/prep/generate") => prep_plan(db, p, path),
        "/api/kiy/build-model" => kiy_plan(&cfg, db, p),
        _ if crate::media_rename::serves(path) => crate::media_rename::plan(cfg, db, p, path, body),
        "/calendar/ai-day" | "/calendar/ai-design" => calendar_plan(db, p, path.ends_with("/ai-design"), headers, body),
        _ => None,
    })
}

pub(crate) fn detail(code: StatusCode, m: impl Into<String>) -> Response<Body> {
    Response::builder().status(code).header(header::CONTENT_TYPE, "application/json").header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": m.into()})))).unwrap()
}

fn refuse(code: StatusCode, m: impl Into<String>) -> Option<Fut> {
    let r = detail(code, m);
    Some(Box::pin(async move { r }))
}

pub(crate) fn crash() -> Response<Body> {
    Response::builder().status(StatusCode::INTERNAL_SERVER_ERROR).header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header("x-nd-served-by", "gateway").body(Body::from("Internal Server Error")).unwrap()
}

fn redirect(to: &str) -> Response<Body> {
    Response::builder().status(StatusCode::SEE_OTHER).header(header::LOCATION, to).header("x-nd-served-by", "gateway").body(Body::empty()).unwrap()
}

/// `is_failure_sentinel`
pub(crate) fn failed(t: &str) -> bool {
    t.starts_with("[AI ") || t.starts_with("[empty response")
}

/// `str(v)` for a JSON value (a string as it is)
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `x or default` for a JSON value
fn or_default<'a>(v: Option<&'a Value>, d: &'a Value) -> &'a Value {
    match v {
        Some(v) if crate::edits::py_truthy_pub(Some(v)) => v,
        _ => d,
    }
}

fn can_edit_content(p: &PageCtx) -> bool {
    p.is_gm || p.role.as_deref() == Some("assistant")
}

// ── the mystery board (app/routers/boards_generate.py) ──────────────────────

const MYSTERY_NODE_COLORS: [(&str, &str); 7] = [("victim", "#e5e7eb"), ("suspect", "#ef4444"), ("witness", "#22c55e"), ("location", "#3b82f6"),
    ("clue", "#f59e0b"), ("motive", "#a78bfa"), ("other", "#6b7280")];
const MAX_MYSTERY_NODES: usize = 14;

/// `_extract_json_object`: the reply's object, a fenced or trailing-prose or cut-off one too
pub(crate) fn extract_json_object(raw: &str) -> Option<Map<String, Value>> {
    use std::sync::OnceLock;
    static FENCE: OnceLock<regex::Regex> = OnceLock::new();
    let fence = FENCE.get_or_init(|| regex::Regex::new(r"(?s)^```(?:json)?\s*|\s*```$").unwrap());
    let text = crate::edits::py_strip_pub(raw);
    let text = crate::edits::py_strip_pub(&fence.replace_all(&text, ""));
    let chars: Vec<char> = text.chars().collect();
    let start = chars.iter().position(|c| *c == '{')?;
    let text: Vec<char> = chars[start..].to_vec();
    let s: String = text.iter().collect();
    let as_obj = |v: Value| match v { Value::Object(o) => Some(o), _ => None };
    if let Ok(v) = serde_json::from_str::<Value>(&s) {
        return as_obj(v);
    }
    if let Some(Ok(Value::Object(o))) = serde_json::Deserializer::from_str(&s).into_iter::<Value>().next() {
        return Some(o);
    }
    let step = (text.len() / 40).max(1);
    let mut cut = text.len();
    while cut > start {
        let attempt: String = text[..cut.min(text.len())].iter().collect();
        let attempt = attempt.trim_end_matches(|c: char| c.is_whitespace()).trim_end_matches(',').to_string();
        for close in ["]})", "]}"] {
            if let Ok(v) = serde_json::from_str::<Value>(&format!("{attempt}{close}")) {
                return as_obj(v);
            }
        }
        if cut < step {
            break;
        }
        cut -= step;
    }
    None
}

fn mystery_plan(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, headers: &HeaderMap, body: &[u8]) -> Option<Fut> {
    if !can_edit_content(p) {
        return None;
    }
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    let f = small_form(headers, body)?;
    let premise = crate::edits::py_strip_pub(f.get("premise").unwrap_or(""));
    let title = f.get("title").unwrap_or("").to_string();
    let replace = f.get("replace").map(str::to_string);
    if premise.is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "Describe the mystery first");
    }
    if premise.chars().count() > 4000 {
        return refuse(StatusCode::BAD_REQUEST, "Premise too long (over 4k chars)");
    }
    let b = backend(db)?;
    Some(Box::pin(mystery(cfg, b, wid, premise, title, replace)))
}

async fn mystery(cfg: Arc<Config>, b: Backend, wid: i64, premise: String, title: String, replace: Option<String>) -> Response<Body> {
    let system = format!("You are a mystery-writing assistant for a tabletop RPG GM. From the premise, design an investigation board. Reply with ONLY a JSON object, no markdown fences:\n{{\"title\": str, \"nodes\": [{{\"role\": one of victim, suspect, witness, location, clue, motive, other, \"title\": str, \"body\": str (<=240 chars, what the GM knows/suspects about this entry), \"links\": [indices of nodes this one connects to]}}]}}\nRules: {}-{} nodes; exactly one victim (or a crime with no body — then role 'other'); 2-4 suspects with at least one innocent; every clue ties to at least one suspect or location; every node except the victim has at least one link; each link is meaningful (the JSON has no per-link labels — connections speak through the node bodies). Never name real people. Keep titles short.",
        MAX_MYSTERY_NODES / 2, MAX_MYSTERY_NODES);
    let mut opts = Map::new();
    opts.insert("num_predict".into(), json!(2000));
    let model = b.model.clone();
    let raw = generate_chat(&b, &model, &system, &[json!({"role": "user", "content": premise})], false, &opts).await;
    if failed(&raw) {
        return detail(StatusCode::BAD_GATEWAY, raw);
    }
    let unusable = || detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't a usable board — try rephrasing the premise.");
    let Some(data) = extract_json_object(&raw) else { return unusable() };
    let Some(Value::Array(nodes)) = data.get("nodes") else { return unusable() };
    if nodes.len() < 3 {
        return unusable();
    }
    let mut nodes_in: Vec<&Map<String, Value>> = vec![];
    for n in nodes {
        let Value::Object(n) = n else { continue };
        let keep = match n.get("title") {
            v if !crate::edits::py_truthy_pub(v) => false,
            Some(Value::String(t)) => !crate::edits::py_strip_pub(t).is_empty(),
            _ => return crash(), // (n.get("title") or "").strip() on a number: Python's AttributeError
        };
        if keep {
            nodes_in.push(n);
        }
    }
    nodes_in.truncate(MAX_MYSTERY_NODES);
    if nodes_in.len() < 3 {
        return unusable();
    }
    let other = json!("other");
    let mut by_role: Vec<(String, Vec<usize>)> = vec![];
    for (i, n) in nodes_in.iter().enumerate() {
        let role = py_str(or_default(n.get("role"), &other)).to_lowercase();
        match by_role.iter_mut().find(|(r, _)| *r == role) {
            Some((_, m)) => m.push(i),
            None => by_role.push((role, vec![i])),
        }
    }
    let (cx, cy) = (1400.0f64, 900.0f64);
    let pi = std::f64::consts::PI;
    let mut board_nodes = vec![];
    let mut index_to_id: std::collections::BTreeMap<i64, String> = Default::default();
    let n_roles = by_role.len().max(1) as f64;
    for (ri, (role, members)) in by_role.iter().enumerate() {
        let angle = (2.0 * pi * ri as f64 / n_roles) - pi / 2.0;
        let (ccx, ccy) = (cx + 620.0 * angle.cos(), cy + 620.0 * angle.sin());
        let color = MYSTERY_NODE_COLORS.iter().find(|(r, _)| r == role).map_or("#6b7280", |c| c.1);
        for (ni, idx) in members.iter().enumerate() {
            let n = nodes_in[*idx];
            let nangle = 2.0 * pi * ni as f64 / members.len().max(1) as f64;
            let (nx, ny) = (ccx + 200.0 * nangle.cos(), ccy + 200.0 * nangle.sin());
            let nid = format!("my-{idx}");
            index_to_id.insert(*idx as i64, nid.clone());
            let empty = json!("");
            board_nodes.push(json!({"id": nid, "type": "faction", "title": py_str(n.get("title").unwrap_or(&Value::Null)).chars().take(120).collect::<String>(),
                "body": py_str(or_default(n.get("body"), &empty)).chars().take(240).collect::<String>(), "color": color, "image_url": "", "entity_id": null,
                "status": py_str(or_default(n.get("role"), &other)), "tags": "", "x": nx.round_ties_even() as i64, "y": ny.round_ties_even() as i64}));
        }
    }
    let mut seen = std::collections::HashSet::new();
    let mut edges = vec![];
    for (idx, n) in nodes_in.iter().enumerate() {
        let links: Vec<Value> = match n.get("links") {
            v if !crate::edits::py_truthy_pub(v) => vec![],
            Some(Value::Array(a)) => a.clone(),
            Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(),
            Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
            _ => return crash(), // iterating a number: Python's TypeError
        };
        for link in links {
            let Ok(other) = crate::ai::reply_int(&link) else { continue };
            if other == idx as i64 || !index_to_id.contains_key(&other) {
                continue;
            }
            let pair = (other.min(idx as i64), other.max(idx as i64));
            if !seen.insert(pair) {
                continue;
            }
            edges.push(json!({"id": format!("me-{}-{}", pair.0, pair.1), "from": index_to_id[&pair.0], "to": index_to_id[&pair.1], "label": "", "color": "#6b7280",
                "style": "solid", "kind": "link", "direction": "fwd"}));
        }
    }
    let investigation = json!("Investigation");
    let name = {
        let t = crate::edits::py_strip_pub(&title);
        if !t.is_empty() { t } else {
            let d: String = crate::edits::py_strip_pub(&py_str(or_default(data.get("title"), &investigation))).chars().take(120).collect();
            if d.is_empty() { "Investigation".into() } else { d }
        }
    };
    let board_name = if name.to_lowercase().starts_with("case") { name.clone() } else { format!("Case — {name}") };
    let base_slug = {
        static NON: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        let re = NON.get_or_init(|| regex::Regex::new(r"[^a-z0-9]+").unwrap());
        let s: String = re.replace_all(&format!("case-{name}").to_lowercase(), "-").trim_matches('-').chars().take(60).collect();
        if s.is_empty() { "case".to_string() } else { s }
    };
    let description = format!("AI-generated investigation board. Premise: {}", premise.chars().take(300).collect::<String>());
    let nodes_json = nd_render::py_dumps(&json!({"nodes": board_nodes}));
    let edges_json = nd_render::py_dumps(&Value::Array(edges));
    let slug = tokio::task::spawn_blocking(move || upsert_board(&cfg, wid, replace.as_deref(), &board_name, &base_slug, &description, nodes_json, edges_json))
        .await.ok().flatten();
    match slug {
        Some(slug) => redirect(&format!("/boards/{slug}")),
        None => crash(),
    }
}

/// `_upsert_board`: the board `replace` names, else a new one under a free slug
fn upsert_board(cfg: &Config, wid: i64, replace: Option<&str>, name: &str, base_slug: &str, description: &str, nodes: String, edges: String) -> Option<String> {
    use rusqlite::types::Value as Sql;
    let db = crate::writes::open(cfg)?;
    let find = |slug: &str| db.query_row("SELECT id FROM invest_boards WHERE slug = ?1 AND world_id = ?2 ORDER BY id LIMIT 1", rusqlite::params![slug, wid], |r| r.get::<_, i64>(0)).ok();
    if let Some(id) = replace.filter(|r| !r.is_empty()).and_then(|r| find(r)) {
        crate::orm::update(&db, "invest_boards", id, &[("nodes_json", Sql::Text(nodes)), ("edges_json", Sql::Text(edges))])?;
        return db.query_row("SELECT slug FROM invest_boards WHERE id = ?1", [id], |r| r.get(0)).ok();
    }
    let mut slug = base_slug.to_string();
    let mut i = 2;
    while find(&slug).is_some() {
        slug = format!("{base_slug}-{i}");
        i += 1;
    }
    crate::orm::insert(&db, "invest_boards", &[("world_id", Sql::Integer(wid)), ("name", Sql::Text(name.into())), ("slug", Sql::Text(slug.clone())),
        ("description", Sql::Text(description.into())), ("canvas_bg", Sql::Text("dark".into())), ("nodes_json", Sql::Text(nodes)), ("edges_json", Sql::Text(edges))])?;
    Some(slug)
}

// ── the calendar's AI helpers (app/routers/calendar.py) ──────────────────────

const DESIGN_SYSTEM: &str = "You design the in-world calendar for a tabletop RPG setting. Reply ONLY with JSON, no fences, in this shape:\n{\"era_name\": str (what the years are counted from, e.g. \"After the Last Flight\"), \"year_format\": str (how a year is written; must contain {year}; may contain {era}, e.g. \"Year {year}\" or \"{year} AA\"), \"days_per_week\": int (1-12), \"weekday_names\": [str, ...] (exactly days_per_week names), \"months\": [{\"name\": str, \"days\": int (10-60), \"season\": str (optional)}, ...] (6-16 months), \"moons\": [{\"name\": str, \"cycle_days\": int, \"color\": \"#rrggbb\"}, ...] (0-3 moons), \"holidays\": [{\"name\": str, \"month\": str (exactly one of the month names above), \"day\": int, \"notes\": str}, ...] (3-8 recurring yearly holidays)}\nMake the names fit the setting's tone and give the calendar a character of its own - unusual month lengths, week lengths and moons are welcome when they suit the world. Never copy a real-world calendar's month or weekday names unless the setting is our own world.";

const DAY_SYSTEM: &str = "You are a TTRPG session-prep assistant generating one day's atmospheric texture. Reply ONLY with JSON, no fences:\n{\"weather\": str (one sentence), \"omen\": str (one subtle portent, one sentence), \"sights\": [str, str, str] (three short sensory details of the world that day)}\nMatch the setting's tone from the context when given; otherwise stay dark cyberpunk-fantasy. Never name real places or people.";

/// `calendar_ai_day` / `calendar_ai_design`: GM-only, the active world, a text reply read as JSON
fn calendar_plan(db: &rusqlite::Connection, p: &PageCtx, design: bool, headers: &HeaderMap, body: &[u8]) -> Option<Fut> {
    if !p.is_gm {
        return None;
    }
    let w = p.world.as_ref()?;
    let f = small_form(headers, body)?;
    let b = backend(db)?;
    let model = b.model.clone();
    if !design {
        let label = { let l = crate::edits::py_strip_pub(f.get("date_label").unwrap_or("")); if l.is_empty() { "an unspecified day".to_string() } else { l } };
        let context: String = crate::edits::py_strip_pub(f.get("context").unwrap_or("")).chars().take(500).collect();
        let ask = if context.is_empty() { format!("Date: {label}") } else { format!("Date: {label}\nContext: {context}") };
        return Some(Box::pin(async move {
            let mut opts = Map::new();
            opts.insert("num_predict".into(), json!(400));
            let raw = generate_chat(&b, &model, DAY_SYSTEM, &[json!({"role": "user", "content": ask})], false, &opts).await;
            if failed(&raw) {
                return detail(StatusCode::BAD_GATEWAY, raw);
            }
            let data = extract_json_object(&raw).unwrap_or_default();
            let empty = json!("");
            let cut = |v: &Value, n: usize| py_str(v).chars().take(n).collect::<String>();
            let sights_raw: Vec<Value> = match data.get("sights") {
                None => vec![],
                Some(Value::Array(a)) => a.clone(),
                Some(Value::String(s)) => s.chars().map(|c| json!(c.to_string())).collect(),
                Some(Value::Object(o)) => o.keys().map(|k| json!(k)).collect(),
                Some(_) => return crash(), // iterating null or a number: Python's TypeError
            };
            let sights: Vec<String> = sights_raw.iter().filter(|s| !crate::edits::py_strip_pub(&py_str(s)).is_empty()).map(|s| cut(s, 160)).take(3).collect();
            let (weather, omen) = (cut(or_default(data.get("weather"), &empty), 240), cut(or_default(data.get("omen"), &empty), 240));
            if weather.is_empty() && omen.is_empty() && sights.is_empty() {
                return detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't usable — try again.");
            }
            crate::edits::json_response(&json!({"weather": weather, "omen": omen, "sights": sights})).unwrap_or_else(crash)
        }));
    }
    let brief: String = crate::edits::py_strip_pub(f.get("brief").unwrap_or("")).chars().take(1500).collect();
    let name = w.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let about_text = w.get("description").and_then(|v| v.as_str()).unwrap_or("");
    let mut ask = format!("World: {name}");
    if !about_text.is_empty() {
        ask.push_str(&format!("\nAbout the world: {}", crate::edits::py_strip_pub(about_text).chars().take(800).collect::<String>()));
    }
    ask.push_str(&if brief.is_empty() { "\nNo further wishes - surprise me.".to_string() } else { format!("\nWhat the GM wants from the calendar: {brief}") });
    Some(Box::pin(async move {
        let mut opts = Map::new();
        opts.insert("num_predict".into(), json!(2000));
        let raw = generate_chat(&b, &model, DESIGN_SYSTEM, &[json!({"role": "user", "content": ask})], false, &opts).await;
        if failed(&raw) {
            return detail(StatusCode::BAD_GATEWAY, raw);
        }
        let data = extract_json_object(&raw).unwrap_or_default();
        let Some(config) = crate::edits::clean_calendar_config(&data) else { return crash() };
        if !matches!(data.get("months"), Some(Value::Array(a)) if !a.is_empty()) {
            return detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't usable — try again.");
        }
        crate::edits::json_response(&json!({"config": config})).unwrap_or_else(crash)
    }))
}

// ── combat tactics (app/routers/combat.py `combat_ai_tactics`) ─────────────

fn tactics_plan(db: &rusqlite::Connection, p: &PageCtx, path: &str) -> Option<Fut> {
    let id: i64 = path.strip_prefix("/combat/")?.strip_suffix("/ai-tactics")?.parse().ok()?;
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let cs = db.query_row("SELECT * FROM combat_sessions WHERE id = ?1 AND world_id = ?2 ORDER BY id LIMIT 1", rusqlite::params![id, wid], |r| crate::layout::row_map(r)).ok()?;
    let b = backend(db)?;
    let (roster, header, label, fluid) = crate::edits::combat_ai_roster(cs)?;
    if roster.is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "No combatants in this fight yet");
    }
    let turn_rule = if fluid {
        "Initiative is FLUID: after a turn, that combatant chooses who acts next - say which target each NPC should pass the turn to and why."
    } else {
        "Initiative order is numeric; act in that order."
    };
    let system = format!("You are a tactically sharp TTRPG GM assistant running {label}. {turn_rule} From the combat roster and round, suggest how the NPCs/monsters should act THIS round and one dramatic beat to keep the fight exciting. Reply ONLY with JSON, no fences:\n{{\"tactics\": [{{\"who\": str (combatant name), \"move\": str (one concrete action, <=160 chars)}}], \"beat\": str (one dramatic twist/turn of the battle, <=200 chars)}}\nRules: 2-4 tactics, only for NON-player combatants (names come from the roster; PCs are the players' to run); skip anyone marked OUT; use the conditions, armor and resources shown; the beat should escalate, not end the fight unless it's clearly nearly over.");
    let user = format!("{header}{}", roster.join("\n"));
    let model = b.model.clone();
    Some(Box::pin(async move {
        let mut opts = Map::new();
        opts.insert("num_predict".into(), json!(700));
        let raw = generate_chat(&b, &model, &system, &[json!({"role": "user", "content": user})], false, &opts).await;
        if failed(&raw) {
            return detail(StatusCode::BAD_GATEWAY, raw);
        }
        let data = extract_json_object(&raw).unwrap_or_default();
        let items: Vec<Value> = match data.get("tactics") {
            None => vec![],
            Some(Value::Array(a)) => a.clone(),
            Some(Value::String(_)) | Some(Value::Object(_)) => vec![], // strings / keys: never a dict
            Some(_) => return crash(),
        };
        let tactics: Vec<&Map<String, Value>> = items.iter().filter_map(|t| t.as_object())
            .filter(|t| crate::edits::py_truthy_pub(t.get("who")) && crate::edits::py_truthy_pub(t.get("move"))).take(6).collect();
        if tactics.is_empty() && !crate::edits::py_truthy_pub(data.get("beat")) {
            return detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't usable — try again.");
        }
        let empty = json!("");
        let cut = |v: &Value, n: usize| py_str(v).chars().take(n).collect::<String>();
        let tactics: Vec<Value> = tactics.iter().map(|t| json!({"who": cut(&t["who"], 80), "move": cut(&t["move"], 200)})).collect();
        crate::edits::json_response(&json!({"tactics": tactics, "beat": cut(or_default(data.get("beat"), &empty), 300)})).unwrap_or_else(crash)
    }))
}

// ── party insights (app/routers/parties.py `party_ai_insights`) ────────────

/// A stored id list (`json.loads(x or "[]")`) of whole numbers; None for anything else
fn id_list(v: Option<&Value>) -> Option<Vec<i64>> {
    let raw = v.and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => a.iter().map(|x| x.as_i64().filter(|_| !x.is_f64())).collect(),
        _ => None,
    }
}

fn insights_plan(db: &rusqlite::Connection, p: &PageCtx, path: &str) -> Option<Fut> {
    let id: i64 = path.strip_prefix("/parties/")?.strip_suffix("/ai-insights")?.parse().ok()?;
    if !p.is_gm {
        return None;
    }
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let party = db.query_row("SELECT * FROM parties WHERE id = ?1 AND world_id = ?2 ORDER BY id LIMIT 1", rusqlite::params![id, wid], |r| crate::layout::row_map(r)).ok()?;
    let b = backend(db)?;
    let (pc_ids, ent_ids) = (id_list(party.get("member_pc_ids_json"))?, id_list(party.get("member_entity_ids_json"))?);
    let rows = |table: &str, ids: &[i64]| -> Option<Vec<Map<String, Value>>> {
        if ids.is_empty() {
            return Some(vec![]);
        }
        let marks = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let mut st = db.prepare(&format!("SELECT * FROM {table} WHERE id IN ({marks}) ORDER BY id")).ok()?;
        let out: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        Some(out)
    };
    let mut roster = vec![];
    for pc in rows("player_characters", &pc_ids)? {
        roster.push(format!("- {}", crate::edits::pc_digest_line(db, &pc, true, true)?));
    }
    for e in rows("entities", &ent_ids)? {
        let name = e.get("name").and_then(|v| v.as_str())?;
        let kind = e.get("kind").and_then(|v| v.as_str())?;
        roster.push(format!("- {name} (NPC ally, {kind})"));
    }
    if roster.len() < 2 {
        return refuse(StatusCode::BAD_REQUEST, "A party needs at least two members for insights");
    }
    let user = format!("Party: {}\n{}", party.get("name").and_then(|v| v.as_str())?, roster.join("\n"));
    let model = b.model.clone();
    Some(Box::pin(async move {
        let system = "You are a TTRPG party-dynamics assistant. From the roster, propose what makes this party tick. Reply ONLY with JSON, no fences:\n{\"bonds\": [str], \"tensions\": [str], \"hooks\": [{\"who\": str, \"hook\": str}]}\nRules: 2-3 bonds and 1-3 tensions (each one sentence, grounded in who these characters are, never contradicting the roster); exactly one hook per member — a personal side-quest seed in one sentence; keep everything setting-neutral.";
        let mut opts = Map::new();
        opts.insert("num_predict".into(), json!(900));
        let raw = generate_chat(&b, &model, system, &[json!({"role": "user", "content": user})], false, &opts).await;
        if failed(&raw) {
            return detail(StatusCode::BAD_GATEWAY, raw);
        }
        let data = extract_json_object(&raw).unwrap_or_default();
        // `for x in data.get(k, [])`: a list, a string's characters, a dict's keys; None or a number is Python's TypeError
        let items = |k: &str| -> Option<Vec<Value>> {
            match data.get(k) {
                None => Some(vec![]),
                Some(Value::Array(a)) => Some(a.clone()),
                Some(Value::String(s)) => Some(s.chars().map(|c| json!(c.to_string())).collect()),
                Some(Value::Object(o)) => Some(o.keys().map(|k| json!(k)).collect()),
                Some(_) => None,
            }
        };
        let texts = |k: &str| -> Option<Vec<String>> {
            Some(items(k)?.iter().filter(|x| !crate::edits::py_strip_pub(&py_str(x)).is_empty()).map(|x| py_str(x).chars().take(240).collect()).take(4).collect())
        };
        let (Some(bonds), Some(tensions), Some(hooks)) = (texts("bonds"), texts("tensions"), items("hooks")) else { return crash() };
        let hooks: Vec<Value> = hooks.iter().filter_map(|h| h.as_object())
            .filter(|h| crate::edits::py_truthy_pub(h.get("who")) && crate::edits::py_truthy_pub(h.get("hook")))
            .map(|h| json!({"who": py_str(&h["who"]).chars().take(80).collect::<String>(), "hook": py_str(&h["hook"]).chars().take(240).collect::<String>()}))
            .take(8).collect();
        if bonds.is_empty() && tensions.is_empty() && hooks.is_empty() {
            return detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't usable — try again.");
        }
        crate::edits::json_response(&json!({"bonds": bonds, "tensions": tensions, "hooks": hooks})).unwrap_or_else(crash)
    }))
}

// ── random table generator (app/routers/tables.py `table_generate_ai`) ──────

fn table_plan(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, headers: &HeaderMap, body: &[u8]) -> Option<Fut> {
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    if crate::pages::edit_level(p, w, "tables") != Some(true) {
        return None;
    }
    let f = small_form(headers, body)?;
    let description = crate::edits::py_strip_pub(f.get("description").unwrap_or(""));
    let count = match f.get("count") { None => 20, Some(c) => crate::form::py_int(c)? };
    let category = f.get("category").unwrap_or("general").to_string();
    if description.is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "Describe the table first");
    }
    if description.chars().count() > 2000 {
        return refuse(StatusCode::BAD_REQUEST, "Description too long (over 2k chars)");
    }
    let count = (if count == 0 { 20 } else { count }).clamp(4, 30);
    let b = backend(db)?;
    let model = b.model.clone();
    Some(Box::pin(async move {
        let system = format!("You generate random tables for a tabletop RPG GM. Reply with ONLY a JSON object, no markdown fences:\n{{\"name\": str (short table name), \"description\": str (one sentence), \"entries\": [str, ...]}}\nRules: exactly {count} entries; each entry one line, evocative but usable at the table (a name, a sight, a complication — per the description); vary them across the full range the description implies; never name real people; no numbering in the text.");
        let mut opts = Map::new();
        opts.insert("num_predict".into(), json!(1800));
        let raw = generate_chat(&b, &model, &system, &[json!({"role": "user", "content": description})], false, &opts).await;
        if failed(&raw) {
            return detail(StatusCode::BAD_GATEWAY, raw);
        }
        let data = extract_json_object(&raw).unwrap_or_default();
        let entries: Vec<String> = match data.get("entries") {
            Some(Value::Array(a)) => a.iter().map(|e| crate::edits::py_strip_pub(&py_str(e))).filter(|e| !e.is_empty())
                .map(|e| e.chars().take(300).collect()).take(40).collect(),
            _ => vec![],
        };
        if entries.len() < 4 {
            return detail(StatusCode::BAD_GATEWAY, "The AI reply wasn't a usable table — try rephrasing the description.");
        }
        let empty = json!("");
        let name: String = { let n = crate::edits::py_strip_pub(&py_str(or_default(data.get("name"), &empty)));
            if n.is_empty() { description.chars().take(60).collect::<String>() } else { n } }.chars().take(200).collect();
        let category = { let c: String = crate::edits::py_strip_pub(if category.is_empty() { "general" } else { &category }).chars().take(64).collect();
            if c.is_empty() { "general".to_string() } else { c } };
        let desc: String = { let d = crate::edits::py_strip_pub(&py_str(or_default(data.get("description"), &empty)));
            if d.is_empty() { description.clone() } else { d } }.chars().take(500).collect();
        let entries_json = nd_render::py_dumps(&Value::Array(entries.iter().map(|e| json!({"label": e, "weight": 1})).collect()));
        let id = tokio::task::spawn_blocking(move || -> Option<i64> {
            use rusqlite::types::Value as Sql;
            let db = crate::writes::open(&cfg)?;
            let slug = slugify(&db, &name)?;
            crate::orm::insert(&db, "random_tables", &[("world_id", Sql::Integer(wid)), ("name", Sql::Text(name)), ("slug", Sql::Text(slug)),
                ("category", Sql::Text(category)), ("description", Sql::Text(desc)), ("is_builtin", Sql::Integer(0)), ("entries_json", Sql::Text(entries_json))])
        }).await.ok().flatten();
        match id {
            Some(id) => redirect(&format!("/tables/{id}/edit")),
            None => crash(),
        }
    }))
}

/// tables.py `_slugify`: lowercased, non-alphanumerics to "-", 50 characters, unique over every table
fn slugify(db: &rusqlite::Connection, name: &str) -> Option<String> {
    static NON: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = NON.get_or_init(|| regex::Regex::new(r"[^a-z0-9]+").unwrap());
    let base: String = re.replace_all(&name.to_lowercase(), "-").trim_matches('-').chars().take(50).collect();
    let base = if base.is_empty() { "table".to_string() } else { base };
    let taken = |sl: &str| db.query_row("SELECT count(*) FROM random_tables WHERE slug = ?1", [sl], |r| r.get::<_, i64>(0)).ok().map(|n| n > 0);
    let mut slug = base.clone();
    let mut n = 2;
    while taken(&slug)? {
        slug = format!("{base}-{n}");
        n += 1;
    }
    Some(slug)
}

// ── session prep (app/routers/sessions.py `prep_generate`) ──────────────────

fn prep_plan(db: &rusqlite::Connection, p: &PageCtx, path: &str) -> Option<Fut> {
    let id: i64 = path.strip_prefix("/api/sessions/")?.strip_suffix("/prep/generate")?.parse().ok()?;
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    let gs = db.query_row("SELECT * FROM game_sessions WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok()?;
    // another world's session (membership and that world's section rules) stays Python's
    if gs.get("world_id").and_then(|v| v.as_i64()) != Some(wid) || crate::pages::edit_level(p, w, "sessions") != Some(true) {
        return None;
    }
    let text = |v: Option<&Value>| -> Option<String> {
        match v { None | Some(Value::Null) => Some("None".into()), Some(Value::String(s)) => Some(s.clone()), Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()), _ => None }
    };
    let mut parts = vec![];
    if let Some(num) = gs.get("session_num").filter(|v| !v.is_null()) {
        let prior = db.query_row("SELECT * FROM game_sessions WHERE world_id = ?1 AND session_num < ?2 ORDER BY session_num DESC LIMIT 1",
            rusqlite::params![wid, num.as_i64()?], |r| crate::layout::row_map(r)).ok();
        if let Some(prior) = prior {
            if crate::edits::py_truthy_pub(prior.get("summary")) {
                parts.push(format!("Recap of the last session (#{} {}):\n{}", text(prior.get("session_num"))?, text(prior.get("title"))?, text(prior.get("summary"))?));
            }
            let mut st = db.prepare("SELECT content FROM facts WHERE game_session_id = ?1 ORDER BY created_at").ok()?;
            let facts: Vec<Option<String>> = st.query_map([prior.get("id")?.as_i64()?], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
            if !facts.is_empty() {
                parts.push(format!("Facts from the last session:\n{}", facts.iter().map(|f| format!("- {}", f.as_deref().unwrap_or("None"))).collect::<Vec<_>>().join("\n")));
            }
        }
    }
    let mut st = db.prepare("SELECT title, summary FROM quests WHERE world_id = ?1 AND status = 'active' ORDER BY title").ok()?;
    let quests: Vec<(Option<String>, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    if !quests.is_empty() {
        let lines: Vec<String> = quests.iter().map(|(t, s)| {
            let t = t.as_deref().unwrap_or("None");
            match s.as_deref().filter(|s| !s.is_empty()) { Some(s) => format!("- {t}: {s}"), None => format!("- {t}") }
        }).collect();
        parts.push(format!("Open quests:\n{}", lines.join("\n")));
    }
    if let Some(party_id) = gs.get("party_id").and_then(|v| v.as_i64()).filter(|i| *i != 0) {
        let party = db.query_row("SELECT member_pc_ids_json FROM parties WHERE id = ?1", [party_id], |r| r.get::<_, Option<String>>(0)).ok();
        let pc_ids = match party { Some(raw) => id_list(Some(&json!(raw.unwrap_or_default())))?, None => vec![] };
        if !pc_ids.is_empty() {
            let marks = pc_ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
            let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE id IN ({marks}) ORDER BY id")).ok()?;
            let pcs: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
            if !pcs.is_empty() {
                let lines: Option<Vec<String>> = pcs.iter().map(|pc| crate::edits::pc_digest_line(db, pc, false, true).map(|l| format!("- {l}"))).collect();
                parts.push(format!("Party:\n{}", lines?.join("\n")));
            }
        }
    }
    let context = parts.join("\n\n");
    if crate::edits::py_strip_pub(&context).is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "Nothing to generate from yet — add a recap/facts to a prior session, an open quest, or a party first.");
    }
    let b = backend(db)?;
    let model = b.model.clone();
    Some(Box::pin(async move {
        let schema = json!({"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "string"}}}, "required": ["tasks"]});
        let msgs = [json!({"role": "system", "content": crate::ai::recap_text("_SESSION_PREP_SYSTEM")}), json!({"role": "user", "content": context})];
        let reply = match crate::ai::chat_with_schema(&b, &model, &msgs, false, &schema).await {
            Ok(r) => r,
            Err(e) => return detail(StatusCode::BAD_GATEWAY, e),
        };
        let tasks = match serde_json::from_str::<Value>(&reply) {
            Ok(Value::Object(o)) => match o.get("tasks") { Some(Value::Array(a)) => Some(a.clone()), _ => None },
            _ => None,
        };
        let Some(tasks) = tasks else { return detail(StatusCode::BAD_GATEWAY, "Could not generate a prep checklist — try again.") };
        let tasks: Vec<String> = tasks.iter().map(|t| crate::edits::py_strip_pub(&py_str(t))).filter(|t| !t.is_empty()).collect();
        crate::edits::json_response(&json!({"tasks": tasks})).unwrap_or_else(crash)
    }))
}

// ── the King in Yellow model build (app/routers/lore_extras.py `kiy_build_model`) ──

/// Building a model needs a local Ollama daemon (`Client.create`); Studio's client has no such call, so on Unsloth Studio the route always
/// streams Python's error event - after the same model lookup
fn kiy_plan(cfg: &Config, db: &rusqlite::Connection, p: &PageCtx) -> Option<Fut> {
    if !p.is_gm {
        return None;
    }
    let file = cfg.db_path.parent()?.join("kiy_plays.json");
    let plays = std::fs::read_to_string(file).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()).unwrap_or(json!([]));
    if !crate::edits::py_truthy_pub(Some(&plays)) {
        return refuse(StatusCode::BAD_REQUEST, "No training plays saved yet");
    }
    let Value::Array(list) = &plays else { return None };
    if !list.iter().rev().take(6).all(|p| p.get("text").is_some_and(|t| t.is_string())) {
        return None;
    }
    let b = backend(db)?;
    Some(Box::pin(async move {
        let _ = crate::ai::resolve_model(&b, "").await;
        let body = format!("data: {}\n\ndata: [DONE]\n\n", nd_render::py_dumps(&json!({"error": "'UnslothClient' object has no attribute 'create'"})));
        Response::builder().status(StatusCode::OK).header(header::CONTENT_TYPE, "text/event-stream; charset=utf-8").header("x-accel-buffering", "no")
            .header(header::CACHE_CONTROL, "no-cache").header("x-nd-served-by", "gateway").body(Body::from(body)).unwrap()
    }))
}
