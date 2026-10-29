//! `GET /share/<token>` (app/routers/character_share.py `share_page`): the read-only view of one character, open to anyone holding the token.

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::edits::{col_int, field_value_text, py_truthy_pub, roster_field_groups, stat_values};
use crate::pages::Pages;

const STAT_LABELS: [(&str, &str); 8] = [("str", "Strength"), ("dex", "Dexterity"), ("bod", "Body"), ("per", "Perception"),
    ("wil", "Willpower"), ("int", "Intellect"), ("cha", "Charisma"), ("itu", "Intuition")];

/// `_json_list(raw)`: the stored list, [] for anything else; None for JSON serde reads differently (NaN and the like)
fn json_list(raw: Option<&Value>) -> Option<Vec<Value>> {
    let text = raw.and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Array(a)) => Some(a),
        Ok(_) => Some(vec![]),
        Err(_) if text.contains("NaN") || text.contains("Infinity") => None,
        Err(_) => Some(vec![]),
    }
}

fn truncated(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `str(v)[:n]` for a value that is a string; anything else is Python's own text form
fn text_of(v: &Value, n: usize) -> Option<String> {
    match v {
        Value::String(s) => Some(truncated(s, n)),
        _ => None,
    }
}

/// `public_view(db, pc)`
fn public_view(db: &rusqlite::Connection, pc: &Map<String, Value>) -> Option<Value> {
    let (hp, shock, _, _, native) = crate::edits::pc_maxima(db, pc)?;
    let s = |k: &str| match pc.get(k) { Some(Value::String(t)) => Some(t.clone()), None | Some(Value::Null) => Some(String::new()), _ => None };
    let name = match pc.get("name") { Some(Value::String(t)) => t.clone(), _ => return None };
    let mut line = [s("race")?, s("char_class")?].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" · ");
    let level = match col_int(pc, "level") { 0 => 1, n => n };
    let (mut stats, mut vitals, mut groups) = (vec![], vec![], vec![]);
    if native {
        let vals = stat_values(pc)?;
        for (sid, label) in STAT_LABELS {
            if let Some((_, v)) = vals.iter().find(|(k, _)| k == sid) {
                stats.push(json!({"label": label, "value": v}));
            }
        }
        vitals.push(json!({"label": "HP", "text": format!("{} / {hp}", col_int(pc, "current_hp"))}));
        vitals.push(json!({"label": "Shock", "text": format!("{} / {shock}", col_int(pc, "shock_current"))}));
    } else if let Some(t) = crate::sheets::pc_template(db, pc)?.as_ref() {
        if line.is_empty() {
            line = match t.row.get("name") { Some(Value::String(n)) if !n.is_empty() => n.clone(), None | Some(Value::Null) => "a custom system".into(), Some(Value::String(_)) => "a custom system".into(), _ => return None };
        }
        let cf = crate::sheets::custom_fields(pc)?;
        let label_of = |f: &Map<String, Value>| -> Option<Value> {
            let l = f.get("label");
            let pick = if py_truthy_pub(l) { l } else { f.get("id") };
            match pick { Some(Value::String(_)) => pick.cloned(), _ => None }
        };
        for f in &t.fields {
            if f.get("type").and_then(|v| v.as_str()) == Some("resource") {
                let text = field_value_text(f, &cf)?;
                if !text.is_empty() {
                    vitals.push(json!({"label": label_of(f)?, "text": text}));
                }
            }
        }
        for (title, fields) in roster_field_groups(t)? {
            let mut rows = vec![];
            for f in fields {
                let text = field_value_text(f, &cf)?;
                if !text.is_empty() {
                    rows.push(json!({"label": label_of(f)?, "text": text}));
                }
            }
            if !rows.is_empty() {
                groups.push(json!({"title": title, "rows": rows}));
            }
        }
    }
    let conditions: Vec<Value> = json_list(pc.get("conditions_json"))?.into_iter().take(12).filter_map(|c| c.as_str().map(|s| json!(truncated(s, 40)))).collect();
    let mut feats = vec![];
    for f in json_list(pc.get("feats_json"))?.into_iter().take(80) {
        let Some(o) = f.as_object() else { continue };
        if !py_truthy_pub(o.get("name")) {
            continue;
        }
        let rank = match o.get("rank") { v if !py_truthy_pub(v) => String::new(), Some(v) => text_of(v, 20)?, None => String::new() };
        feats.push(json!({"name": text_of(o.get("name")?, 80)?, "rank": rank}));
    }
    let mut gear = vec![];
    for e in json_list(pc.get("equipment_json"))?.into_iter().take(120) {
        let Some(o) = e.as_object() else { continue };
        if !py_truthy_pub(o.get("name")) {
            continue;
        }
        let qty = match o.get("qty") {
            v if !py_truthy_pub(v) => json!(1),
            Some(Value::Number(n)) if n.is_i64() => Value::Number(n.clone()),
            Some(Value::String(t)) => json!(t),
            _ => return None,
        };
        gear.push(json!({"name": text_of(o.get("name")?, 80)?, "qty": qty, "equipped": py_truthy_pub(o.get("equipped"))}));
    }
    Some(json!({"name": name, "line": line, "level": level, "native": native, "stats": stats, "vitals": vitals, "groups": groups,
        "conditions": conditions, "feats": feats, "gear": gear}))
}

pub fn share_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let token = uri.path().strip_prefix("/share/")?;
    let token = percent_encoding::percent_decode_str(token).decode_utf8().ok()?.to_string();
    if token.is_empty() || token.contains('/') {
        return None;
    }
    let db = crate::writes::open(cfg)?;
    let token64 = truncated(&token, 64);
    let cid: Option<i64> = db.query_row("SELECT character_id FROM character_shares WHERE token = ?1 AND revoked IS NOT 1 LIMIT 1", [&token64], |r| r.get(0)).ok();
    let pc = cid.and_then(|id| db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok());
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    let v = public_view(&db, &pc)?;
    let mut ctx = Map::new();
    ctx.insert("v".into(), v);
    let mut resp = crate::pages::render_with(pages, "characters/share.html", ctx, headers)?;
    let h = resp.headers_mut();
    h.insert("cache-control", "no-store".parse().ok()?);
    h.insert("x-robots-tag", "noindex, nofollow".parse().ok()?);
    h.insert("referrer-policy", "no-referrer".parse().ok()?);
    Some(resp)
}
