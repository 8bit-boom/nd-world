//! `GET /races` and `GET /professions` (app/routers/races.py `races_page`, app/routers/professions.py `professions_page`): the world's
//! races / professions by tier, and the bundled catalog (app/races/, app/professions/) a GM can add from.

use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{render_with, section_ok, Pages};

const TIERS: [&str; 3] = ["standard", "advanced", "exceptional"];

/// One bundled entry: what the page reads of `_load_builtin_races` / `_load_builtin_professions` (slug, name, tier, image, summary)
#[derive(Clone)]
struct Builtin {
    slug: String,
    name: String,
    tier: &'static str,
    image_url: String,
    summary: String,
}

/// `str.title()` for an ASCII name
fn title(s: &str) -> String {
    let mut out = String::new();
    let mut prev_letter = false;
    for ch in s.chars() {
        out.push(if prev_letter { ch.to_ascii_lowercase() } else { ch.to_ascii_uppercase() });
        prev_letter = ch.is_ascii_alphabetic();
    }
    out
}

/// The bundled catalog of `kind` ("races" / "professions"), read once as Python's lru_cache does; None when a file holds what this port
/// would read differently (not UTF-8, a line separator other than "\n", a non-ASCII file name), so the page stays Python's
fn builtin(cfg: &Config, kind: &str) -> Option<Vec<Builtin>> {
    static RACES: OnceLock<Option<Vec<Builtin>>> = OnceLock::new();
    static PROFESSIONS: OnceLock<Option<Vec<Builtin>>> = OnceLock::new();
    let cell = if kind == "races" { &RACES } else { &PROFESSIONS };
    cell.get_or_init(|| load(cfg, kind)).clone()
}

fn load(cfg: &Config, kind: &str) -> Option<Vec<Builtin>> {
    let first_img = Regex::new(r"^!\[.*?\]\((.+?)\)").ok()?;
    let any_img = Regex::new(r"!\[([^\]]*)\]\(([^)]+)\)").ok()?;
    let root = cfg.templates_dir.parent()?.join(kind);
    let mut out = vec![];
    for tier in TIERS {
        let dir = root.join(tier);
        if !dir.exists() {
            continue;
        }
        let mut files: Vec<_> = std::fs::read_dir(&dir).ok()?.filter_map(|e| e.ok()).map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "md")).collect();
        files.sort();
        for f in files {
            let stem = f.file_stem()?.to_str()?.to_string();
            if !stem.is_ascii() || stem.starts_with('.') {
                return None;
            }
            let Ok(bytes) = std::fs::read(&f) else { continue };
            let mut raw = String::from_utf8(bytes).ok()?;
            if raw.contains(['\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\u{85}', '\u{2028}', '\u{2029}']) {
                return None;
            }
            let mut image_url = String::new();
            let lines: Vec<&str> = raw.lines().collect();
            if let Some(first) = lines.first() {
                if let Some(c) = first_img.captures(nd_render::text::py_strip(first)) {
                    image_url = format!("/static/{kind}/{tier}/{}", &c[1]);
                    raw = lines[1..].join("\n").trim_start().to_string();
                }
            }
            // re.sub(r'!\[([^\]]*)\]\((?!http)([^)]+)\)', ...): a link that starts with http is left as it is
            let fixed = any_img.replace_all(&raw, |c: &regex::Captures| {
                if c[2].starts_with("http") { c[0].to_string() } else { format!("![{}](/static/{kind}/{tier}/{})", &c[1], &c[2]) }
            }).into_owned();
            let slug = stem.split('_').next()?.to_string();
            let mut name = title(&slug.replace('-', " "));
            if let Some(l) = fixed.lines().find(|l| l.starts_with("# ")) {
                name = nd_render::text::py_strip(&l[2..]).to_string();
            }
            let marker = if kind == "races" { "## Entry" } else { "## " };
            let mut summary = String::new();
            let mut inside = false;
            for l in fixed.lines() {
                let s = nd_render::text::py_strip(l);
                if s.starts_with(marker) {
                    inside = true;
                    continue;
                }
                if inside && !s.is_empty() && !l.starts_with('#') && !l.starts_with("**") {
                    summary = s.chars().take(200).collect();
                    break;
                }
            }
            out.push(Builtin { slug, name, tier, image_url, summary });
        }
    }
    Some(out)
}

pub fn races(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    page(pages, cfg, uri, headers, "races", "race")
}

pub fn professions(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    page(pages, cfg, uri, headers, "professions", "profession")
}

fn page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, kind: &str, entity_kind: &str) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), kind)? {
        return Some(crate::idpages::refusal(403));
    }
    let catalog = builtin(cfg, kind)?;
    let mut world_rows: Vec<Map<String, Value>> = vec![];
    let mut names: Vec<String> = vec![];
    if let Some(wid) = world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) {
        let mut st = db.prepare("SELECT name FROM entities WHERE world_id = ?1 AND kind = ?2").ok()?;
        for n in st.query_map(rusqlite::params![wid, entity_kind], |r| r.get::<_, Option<String>>(0)).ok()? {
            if let Some(n) = n.ok()?.filter(|n| !n.is_empty()) {
                names.push(n);
            }
        }
        let vis = if p.is_gm { " AND ?3 IS NOT NULL" } else {
            " AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?3))"
        };
        let mut st = db.prepare(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = ?2{vis} ORDER BY subtype, name")).ok()?;
        world_rows = st.query_map(rusqlite::params![wid, entity_kind, p.user_id], |r| crate::layout::row_map(r)).ok()?
            .collect::<Result<Vec<_>, _>>().ok()?;
    }
    // group_by_tier: the subtype ignoring case and padding, else "unsorted"; each group by name, lower-cased
    let mut groups: Vec<(&str, Vec<Value>)> = TIERS.iter().map(|t| (*t, vec![])).chain([("unsorted", vec![])]).collect();
    for e in &world_rows {
        let key = match e.get("subtype") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => nd_render::text::py_strip(s).to_lowercase(),
            _ => return None,
        };
        let i = TIERS.iter().position(|t| *t == key).unwrap_or(3);
        groups[i].1.push(Value::Object(e.clone()));
    }
    let lower = |v: &Value| v.get("name").and_then(|n| n.as_str()).unwrap_or("").to_lowercase();
    let mut by_tier = Map::new();
    for (t, mut g) in groups {
        g.sort_by_key(|v| lower(v));
        by_tier.insert(t.into(), Value::Array(g));
    }
    let available: Vec<&Builtin> = catalog.iter().filter(|b| !names.contains(&b.name)).collect();
    let as_json = |b: &Builtin| json!({"slug": b.slug, "name": b.name, "tier": b.tier, "image_url": b.image_url, "summary": b.summary});
    let mut avail_tier = Map::new();
    for t in TIERS {
        avail_tier.insert(t.into(), Value::Array(available.iter().filter(|b| b.tier == t).map(|b| as_json(b)).collect()));
    }
    let singular = if kind == "races" { "world_races" } else { "world_professions" };
    p.ctx.insert(singular.into(), Value::Array(world_rows.into_iter().map(Value::Object).collect()));
    p.ctx.insert("world_by_tier".into(), Value::Object(by_tier));
    p.ctx.insert("available_builtin".into(), Value::Array(available.iter().map(|b| as_json(b)).collect()));
    p.ctx.insert("available_by_tier".into(), Value::Object(avail_tier));
    render_with(pages, &format!("{kind}.html"), p.ctx, headers)
}
