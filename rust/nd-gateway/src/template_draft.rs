//! AI-drafted character-sheet templates (a port of app/template_draft.py and sheet_systems.clean_system_spec): the model's untrusted
//! JSON made into a template that loads and works - valid unique field ids, known types, selects with options, one HP track, system
//! references that point at real fields - with a warning for everything changed or dropped; and a long rulebook read in pieces.

use serde_json::{json, Map, Value};
use std::sync::OnceLock;

use crate::ai::py_repr;

const FIELD_TYPES: [&str; 6] = ["text", "textarea", "number", "select", "resource", "list"];
const ITEM_TYPES: [&str; 4] = ["text", "textarea", "number", "select"];
const DRAFT_FIELDS_CAP: usize = 120;
const DRAFT_ITEM_FIELDS_CAP: usize = 8;
const DRAFT_OPTIONS_CAP: usize = 30;
const ID_CAP: usize = 40;

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn ws_join(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `str(v)` (a string as it is)
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `repr(v)`, None for a missing key
fn rep(v: Option<&Value>) -> String {
    py_repr(v.unwrap_or(&Value::Null))
}

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

/// `_text(v, cap)`: a short single-line string
fn text(v: Option<&Value>, cap: usize) -> String {
    let s = match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => py_repr(&Value::Number(n.clone())),
        _ => return String::new(),
    };
    take(&ws_join(&s.replace(['<', '>'], "")), cap)
}

/// A number as `_num` reads it (int or float)
#[derive(Clone, Copy, PartialEq, PartialOrd)]
enum Num {
    I(i64),
    F(f64),
}

impl Num {
    fn f(self) -> f64 {
        match self { Num::I(i) => i as f64, Num::F(f) => f }
    }
}

/// `_num(v)`
fn num(v: Option<&Value>) -> Option<Num> {
    match v? {
        Value::Null | Value::Bool(_) => None,
        Value::Number(n) => Some(n.as_i64().map(Num::I).unwrap_or_else(|| Num::F(n.as_f64().unwrap_or(0.0)))),
        other => {
            let s = crate::edits::py_strip_pub(&py_str(other));
            if s.is_empty() {
                return None;
            }
            if let Ok(i) = s.trim_start_matches('+').parse::<i64>() {
                if !s.starts_with("+-") && !s.starts_with("++") {
                    return Some(Num::I(i));
                }
            }
            let f: f64 = s.parse().ok()?;
            Some(if f.fract() == 0.0 && f.is_finite() && f.abs() < 1e18 { Num::I(f as i64) } else { Num::F(f) })
        }
    }
}

/// `_fmt(n)`
fn fmt(n: Num) -> String {
    match n {
        Num::I(i) => i.to_string(),
        Num::F(f) if f.fract() == 0.0 => (f as i64).to_string(),
        Num::F(f) => py_repr(&json!(f)),
    }
}

fn clamp(n: Num, lo: f64, hi: f64) -> Num {
    match n {
        Num::I(i) => Num::I(i.clamp(lo as i64, hi as i64)),
        Num::F(f) => Num::F(f.clamp(lo, hi)),
    }
}

fn id_ok() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| regex::Regex::new(r"^[A-Za-z][A-Za-z0-9_]*$").unwrap())
}

/// `_sanitise_id(raw)`
fn sanitise_id(raw: Option<&Value>) -> String {
    let s = raw.and_then(|v| v.as_str()).unwrap_or("");
    if id_ok().is_match(s) && s.chars().count() <= ID_CAP {
        return s.to_string();
    }
    static NON: OnceLock<regex::Regex> = OnceLock::new();
    let s = NON.get_or_init(|| regex::Regex::new(r"[^a-z0-9]+").unwrap()).replace_all(&s.to_lowercase(), "_").into_owned();
    let s = take(s.trim_matches('_'), ID_CAP).trim_matches('_').to_string();
    if s.is_empty() {
        return s;
    }
    if s.chars().next().is_some_and(|c| c.is_alphabetic()) { s } else { take(&format!("f_{s}"), ID_CAP) }
}

/// `_unique(base, taken)`
fn unique(base: &str, taken: &mut Vec<String>) -> String {
    let (mut cand, mut n) = (base.to_string(), 2);
    while taken.contains(&cand) {
        let suffix = format!("_{n}");
        cand = format!("{}{suffix}", take(base, ID_CAP.saturating_sub(suffix.len())));
        n += 1;
    }
    taken.push(cand.clone());
    cand
}

/// `_options(raw)`
fn options(raw: Option<&Value>) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for o in raw.and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        let o = text(Some(&o), 60);
        if !o.is_empty() && !out.contains(&o) {
            out.push(o);
        }
    }
    out.truncate(DRAFT_OPTIONS_CAP);
    out
}

/// `_default_for(ftype, raw, options)`
fn default_for(ftype: &str, raw: Option<&Value>, opts: &[String]) -> String {
    match ftype {
        "resource" => {
            let s = text(raw, 30);
            if s.is_empty() {
                return "0/0".into();
            }
            let (cur, mx) = s.split_once('/').map(|(a, b)| (a.to_string(), Some(b.to_string()))).unwrap_or((s.clone(), None));
            let c = num(Some(&json!(cur)));
            let m = mx.as_ref().and_then(|m| num(Some(&json!(m))));
            let (Some(c), false) = (c, m.is_none() && mx.is_some()) else { return "0/0".into() };
            let m = m.unwrap_or(c);
            let (c, m) = (clamp(c, 0.0, 9999.0), clamp(m, 0.0, 9999.0));
            let low = if c.f() <= m.f() { c } else { m };
            format!("{}/{}", fmt(low), fmt(m))
        }
        "number" => {
            let n = match raw { None | Some(Value::Null) => Some(Num::I(0)), Some(Value::String(s)) if s.is_empty() => Some(Num::I(0)), v => num(v) };
            n.map_or("0".into(), |n| fmt(clamp(n, -99999.0, 99999.0)))
        }
        "select" => {
            let s = text(raw, 60);
            if opts.contains(&s) { s } else { String::new() }
        }
        "textarea" => crate::edits::py_strip_pub(&take(&raw.and_then(|v| v.as_str()).unwrap_or("").replace(['<', '>'], ""), 2000)),
        _ => text(raw, 400),
    }
}

fn clean_item_fields(raw: Option<&Value>, warnings: &mut Vec<String>) -> Vec<Value> {
    let (mut out, mut taken) = (vec![], vec![]);
    for sf in raw.and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        let Value::Object(sf) = sf else { continue };
        if out.len() >= DRAFT_ITEM_FIELDS_CAP {
            continue;
        }
        let label = { let l = text(sf.get("label"), 80); if l.is_empty() { text(sf.get("id"), 80) } else { l } };
        let sid = { let s = sanitise_id(sf.get("id")); if s.is_empty() { sanitise_id(Some(&json!(label))) } else { s } };
        if sid.is_empty() {
            continue;
        }
        let stype = sf.get("type").and_then(|t| t.as_str()).filter(|t| ITEM_TYPES.contains(t)).unwrap_or("text");
        let id = unique(&sid, &mut taken);
        let mut item = json!({"id": id, "label": if label.is_empty() { sid.clone() } else { label }, "type": stype});
        if stype == "select" {
            let opts = options(sf.get("options"));
            if opts.is_empty() { item["type"] = json!("text"); } else { item["options"] = json!(opts); }
        }
        out.push(item);
    }
    if out.is_empty() {
        out = vec![json!({"id": "name", "label": "Name", "type": "text"}), json!({"id": "notes", "label": "Notes", "type": "text"})];
        warnings.push("a list field had no columns; gave it Name and Notes".into());
    }
    out
}

/// `_clean_fields_mapped(raw)`: (fields, id map, warnings)
fn clean_fields(raw: Option<&Value>) -> (Vec<Value>, Vec<(String, String)>, Vec<String>) {
    let (mut warnings, mut fields, mut id_map): (Vec<String>, Vec<Value>, Vec<(String, String)>) = (vec![], vec![], vec![]);
    let (mut taken, mut has_hp, mut binds_used): (Vec<String>, bool, Vec<String>) = (vec![], false, vec![]);
    for f in raw.and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        let Value::Object(f) = f else { continue };
        if fields.len() >= DRAFT_FIELDS_CAP {
            warnings.push(format!("kept the first {DRAFT_FIELDS_CAP} fields"));
            break;
        }
        let mut label = text(f.get("label"), 80);
        let written = f.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let base = { let b = sanitise_id(Some(&json!(written))); if b.is_empty() { sanitise_id(Some(&json!(label))) } else { b } };
        let ty = f.get("type");
        let known = ty.and_then(|t| t.as_str()).is_some_and(|t| FIELD_TYPES.contains(&t) || t == "table");
        if base.is_empty() || !(!label.is_empty() || known) {
            continue;
        }
        if label.is_empty() {
            label = text(Some(&json!(written.replace('_', " "))), 80);
            if label.is_empty() { label = base.clone(); }
        }
        let fid = unique(&base, &mut taken);
        if !written.is_empty() && fid != written {
            warnings.push(format!("renamed the field id {} to {}", rep(Some(&json!(written))), rep(Some(&json!(fid)))));
        } else if written.is_empty() {
            warnings.push(format!("gave the field {} the id {}", rep(Some(&json!(label))), rep(Some(&json!(fid)))));
        }
        for key in [written.clone(), sanitise_id(Some(&json!(written)))] {
            if !key.is_empty() && !id_map.iter().any(|(k, _)| *k == key) {
                id_map.push((key, fid.clone()));
            }
        }
        let mut ftype = match ty.and_then(|t| t.as_str()) { Some("table") => "list", Some(t) if FIELD_TYPES.contains(&t) => t, _ => "" }.to_string();
        if ftype.is_empty() {
            warnings.push(format!("{} had the unknown type {}; made it text", rep(Some(&json!(fid))), rep(ty)));
            ftype = "text".into();
        }
        let section = { let s = text(f.get("section"), 40); if s.is_empty() { "General".to_string() } else { s } };
        let mut out = Map::new();
        out.insert("id".into(), json!(fid));
        out.insert("label".into(), json!(label));
        out.insert("type".into(), json!(ftype));
        out.insert("section".into(), json!(section));
        let mut opts = vec![];
        if ftype == "select" {
            opts = options(f.get("options"));
            if opts.is_empty() {
                warnings.push(format!("{} is a select with no options; made it text", rep(Some(&json!(fid)))));
                ftype = "text".into();
                out.insert("type".into(), json!("text"));
            } else {
                out.insert("options".into(), json!(opts));
            }
        }
        if ftype == "list" {
            let cols = match f.get("item_fields") { v if truthy(v) => v, _ => f.get("columns") };
            out.insert("item_fields".into(), Value::Array(clean_item_fields(cols, &mut warnings)));
        } else {
            out.insert("default_value".into(), json!(default_for(&ftype, f.get("default_value"), &opts)));
            let dv = f.get("default_value");
            let blank = matches!(dv, None | Some(Value::Null)) || dv == Some(&json!(""));
            if ftype == "number" && !blank && num(dv).is_none() {
                warnings.push(format!("{} had a non-numeric default; set it to 0", rep(Some(&json!(fid)))));
            }
        }
        if f.get("vital") == Some(&json!("hp")) && ftype == "resource" && !has_hp {
            out.insert("vital".into(), json!("hp"));
            has_hp = true;
        }
        if f.get("xp") == Some(&json!(true)) && ftype == "number" {
            out.insert("xp".into(), json!(true));
        }
        if let Some(bind) = f.get("binds").and_then(|b| b.as_str()).filter(|b| matches!(*b, "name" | "player_name")) {
            if ftype == "text" && !binds_used.iter().any(|x| x == bind) {
                out.insert("binds".into(), json!(bind));
                binds_used.push(bind.to_string());
            }
        }
        fields.push(Value::Object(out));
    }
    (fields, id_map, warnings)
}

/// `_remap_system(system, id_map)`
fn remap_system(system: Option<&Value>, id_map: &[(String, String)]) -> Value {
    let Some(Value::Object(s)) = system else { return json!({}) };
    let m = |x: &Value| match x {
        Value::String(k) => id_map.iter().find(|(a, _)| a == k).map(|(_, b)| json!(b)).unwrap_or_else(|| x.clone()),
        other => other.clone(),
    };
    let mut s = s.clone();
    if let Some(Value::Object(rest)) = s.get("rest").cloned() {
        let mut out = Map::new();
        for (k, ops) in rest {
            out.insert(k, match ops {
                Value::Array(ops) => Value::Array(ops.into_iter().map(|op| match op {
                    Value::Array(o) if o.len() >= 2 => { let mut n = vec![o[0].clone(), m(&o[1])]; n.extend(o[2..].iter().cloned()); Value::Array(n) }
                    other => other,
                }).collect()),
                other => other,
            });
        }
        s.insert("rest".into(), Value::Object(out));
    }
    if let Some(Value::Array(roster)) = s.get("roster").cloned() {
        s.insert("roster".into(), Value::Array(roster.into_iter().map(|g| match &g {
            Value::Array(a) if a.len() == 2 && a[1].is_array() => json!([a[0].clone(), a[1].as_array().unwrap().iter().map(&m).collect::<Vec<_>>()]),
            _ => g,
        }).collect()));
    }
    if let Some(hp) = s.get("hp").filter(|v| v.is_string()).cloned() {
        s.insert("hp".into(), m(&hp));
    }
    if let Some(Value::Array(xp)) = s.get("xp").cloned() {
        s.insert("xp".into(), Value::Array(xp.iter().map(&m).collect()));
    }
    if let Some(Value::Object(b)) = s.get("binds").cloned() {
        let mut out = Map::new();
        for (k, v) in b {
            let key = m(&json!(k)).as_str().unwrap_or(&k).to_string();
            out.insert(key, v);
        }
        s.insert("binds".into(), Value::Object(out));
    }
    Value::Object(s)
}

fn field_type<'a>(by_id: &'a [Map<String, Value>], id: Option<&Value>) -> Option<&'a str> {
    let id = id?.as_str()?;
    by_id.iter().find(|f| f.get("id").and_then(|v| v.as_str()) == Some(id)).and_then(|f| f.get("type")).and_then(|t| t.as_str())
}

/// `_clean_rest_op`
fn clean_rest_op(op: &Value, by_id: &[Map<String, Value>], warnings: &mut Vec<String>) -> Option<Value> {
    let ops = ["full", "empty", "add", "set"];
    let name = op.as_array().filter(|o| o.len() >= 2).and_then(|o| o[0].as_str()).filter(|n| ops.contains(n));
    let Some(name) = name else {
        warnings.push(take(&format!("ignored a Rest rule that is not [full|empty|add|set, field, …]: {}", py_repr(op)), 160));
        return None;
    };
    let o = op.as_array()?;
    let target = &o[1];
    let Some(f) = target.as_str().and_then(|t| by_id.iter().find(|f| f.get("id").and_then(|v| v.as_str()) == Some(t))) else {
        warnings.push(format!("ignored a Rest rule for the unknown field {}", py_repr(target)));
        return None;
    };
    let ftype = f.get("type").and_then(|t| t.as_str()).unwrap_or("");
    let t = target.as_str().unwrap_or("");
    if name != "set" {
        if ftype != "resource" {
            warnings.push(format!("ignored Rest '{name}' on {}: only resource fields can be refilled", py_repr(target)));
            return None;
        }
        if name != "add" {
            return Some(json!([name, t]));
        }
        let n = if o.len() > 2 { num(o.get(2)) } else { Some(Num::I(1)) };
        return match n {
            Some(Num::I(n)) if n > 0 && n <= 999 => Some(json!([name, t, n])),
            _ => {
                warnings.push(format!("ignored Rest 'add' on {}: the amount must be a whole number", py_repr(target)));
                None
            }
        };
    }
    let v = o.get(2);
    if !matches!(ftype, "number" | "text" | "select") || v.is_none_or(|v| matches!(v, Value::Array(_) | Value::Object(_) | Value::Bool(_) | Value::Null)) {
        warnings.push(format!("ignored Rest 'set' on {}", py_repr(target)));
        return None;
    }
    let v = v?;
    let value = if v.is_i64() || v.is_u64() { v.clone() } else { json!(take(&crate::edits::py_strip_pub(&py_str(v)), 100)) };
    if ftype == "select" {
        let opts: Vec<String> = f.get("options").and_then(|o| o.as_array()).map(|a| a.iter().map(py_str).collect()).unwrap_or_default();
        if !opts.contains(&py_str(&value)) {
            warnings.push(format!("ignored Rest 'set' on {}: {} is not one of its options", py_repr(target), py_repr(&value)));
            return None;
        }
    }
    Some(json!(["set", t, value]))
}

/// `clean_system_spec(raw, fields)`
pub(crate) fn clean_system_spec(raw: &Value, fields: &[Value]) -> (Value, Vec<String>) {
    let mut warnings = vec![];
    let Value::Object(raw) = raw else { return (json!({}), warnings) };
    let by_id: Vec<Map<String, Value>> = fields.iter().filter_map(|f| f.as_object()).filter(|f| f.get("id").and_then(|v| v.as_str()).is_some_and(|i| !i.is_empty())).cloned().collect();
    let mut sections: Vec<String> = vec![];
    for f in &by_id {
        let s = f.get("section").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_else(|| "Custom".into());
        if !sections.contains(&s) {
            sections.push(s);
        }
    }
    let mut spec = Map::new();
    if let Some(Value::Array(conds)) = raw.get("conditions") {
        let mut out: Vec<String> = vec![];
        for c in conds {
            let c = c.as_str().map(|c| take(&ws_join(c), 30)).unwrap_or_default();
            if !c.is_empty() && !out.iter().any(|o| o.to_lowercase() == c.to_lowercase()) {
                out.push(c);
            }
        }
        if out.len() > 24 {
            warnings.push("kept the first 24 conditions".into());
            out.truncate(24);
        }
        if !out.is_empty() {
            spec.insert("conditions".into(), json!(out));
        }
    }
    if let Some(Value::Object(rest)) = raw.get("rest") {
        let mut clean = Map::new();
        for kind in ["short", "long"] {
            let Some(Value::Array(ops)) = rest.get(kind) else { continue };
            let good: Vec<Value> = ops.iter().filter_map(|op| clean_rest_op(op, &by_id, &mut warnings)).take(12).collect();
            if !good.is_empty() {
                clean.insert(kind.into(), Value::Array(good));
            }
        }
        if !clean.is_empty() {
            spec.insert("rest".into(), Value::Object(clean));
        }
    }
    if let Some(Value::Array(pages)) = raw.get("pages") {
        let (mut used, mut out): (Vec<String>, Vec<Value>) = (vec![], vec![]);
        for page in pages.iter().take(20) {
            let Some(Value::Array(secs)) = page.get("sections").filter(|_| page.is_object()) else { continue };
            let mut mine: Vec<String> = vec![];
            for sec in secs {
                let Some(s) = sec.as_str() else { continue };
                if sections.iter().any(|x| x == s) && !used.iter().any(|x| x == s) {
                    mine.push(s.into());
                    used.push(s.into());
                } else {
                    warnings.push(format!("page section {} is unknown or already on another page", py_repr(sec)));
                }
            }
            if mine.is_empty() {
                continue;
            }
            let label = { let l = take(&ws_join(&page.get("label").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default()), 24); if l.is_empty() { take(&mine[0], 24) } else { l } };
            let conditions = truthy(page.get("conditions")) && !out.iter().any(|p| p["conditions"] == json!(true));
            out.push(json!({"id": format!("p{}", out.len()), "label": label, "icon": take(&page.get("icon").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default(), 4),
                "sections": mine, "conditions": conditions}));
        }
        if out.len() >= 2 {
            out.truncate(10);
            spec.insert("pages".into(), Value::Array(out));
        } else if !out.is_empty() {
            warnings.push("a single page is the same as no pages; ignored".into());
        }
    }
    if let Some(Value::Array(roster)) = raw.get("roster") {
        let mut out = vec![];
        for g in roster.iter().take(12) {
            let Some(g) = g.as_array().filter(|g| g.len() == 2 && g[1].is_array()) else { continue };
            let wanted: Vec<&str> = g[1].as_array().unwrap().iter().filter_map(|i| i.as_str()).collect();
            let ids: Vec<&str> = wanted.iter().copied().filter(|i| matches!(field_type(&by_id, Some(&json!(i))), Some("number" | "text" | "select"))).collect();
            if ids.len() != wanted.len() {
                warnings.push("some roster fields were dropped (unknown, or resources, which are always compared)".into());
            }
            if !ids.is_empty() {
                let title = { let t = take(&ws_join(&if truthy(Some(&g[0])) { py_str(&g[0]) } else { "Sheet".into() }), 30); if t.is_empty() { "Sheet".into() } else { t } };
                out.push(json!([title, ids.into_iter().take(12).collect::<Vec<_>>()]));
            }
        }
        if !out.is_empty() {
            out.truncate(6);
            spec.insert("roster".into(), Value::Array(out));
        }
    }
    if let Some(hp) = raw.get("hp").filter(|h| field_type(&by_id, Some(h)) == Some("resource")) {
        spec.insert("hp".into(), hp.clone());
    }
    if let Some(Value::Array(xp)) = raw.get("xp") {
        let ids: Vec<Value> = xp.iter().filter(|i| field_type(&by_id, Some(i)) == Some("number")).cloned().collect();
        if !ids.is_empty() {
            spec.insert("xp".into(), Value::Array(ids));
        }
    }
    if let Some(dice) = clean_dice(raw.get("dice"), &by_id, &mut warnings) {
        spec.insert("dice".into(), dice);
    }
    if let Some(Value::Object(b)) = raw.get("binds") {
        let ok: Map<String, Value> = b.iter().filter(|(k, v)| field_type(&by_id, Some(&json!(k))) == Some("text") && matches!(v.as_str(), Some("name" | "player_name")))
            .map(|(k, v)| (k.clone(), v.clone())).collect();
        if !ok.is_empty() {
            spec.insert("binds".into(), Value::Object(ok));
        }
    }
    (Value::Object(spec), warnings)
}

/// `_clean_dice_spec`
fn clean_dice(raw: Option<&Value>, by_id: &[Map<String, Value>], warnings: &mut Vec<String>) -> Option<Value> {
    let Some(Value::Object(raw)) = raw else { return None };
    let mut pools = vec![];
    let list = match raw.get("pools") { Some(Value::Array(a)) => a.clone(), _ => vec![] };
    for p in list.iter().take(6) {
        let Value::Object(p) = p else { continue };
        let label = take(&ws_join(&p.get("label").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default()), 40);
        let dice = num(p.get("dice"));
        let Some(d) = dice.filter(|d| (1.0..=10.0).contains(&d.f())) else { continue };
        if label.is_empty() {
            continue;
        }
        let mut entry = json!({"id": format!("p{}", pools.len()), "label": label, "dice": d.f().trunc() as i64});
        if let Some(Value::Object(ov)) = p.get("override") {
            if field_type(by_id, ov.get("field")) == Some("select") {
                if let Some(od) = num(ov.get("dice")) {
                    entry["override"] = json!({"field": ov["field"], "equals": take(&py_str(ov.get("equals").unwrap_or(&json!(""))), 40),
                        "dice": (od.f().trunc() as i64).clamp(1, 10)});
                }
            }
        }
        pools.push(entry);
    }
    if pools.is_empty() {
        return None;
    }
    let mut spec = json!({"pools": pools, "threshold": 6, "explode": true});
    if let Some(t) = num(raw.get("threshold")).filter(|t| (2.0..=10.0).contains(&t.f())) {
        spec["threshold"] = json!(t.f().trunc() as i64);
    }
    if raw.get("explode") == Some(&json!(false)) {
        spec["explode"] = json!(false);
    }
    match raw.get("spend") {
        Some(Value::Object(sp)) if field_type(by_id, sp.get("field")) == Some("resource") => {
            let label = take(&ws_join(&sp.get("label").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_else(|| py_str(&sp["field"]))), 30);
            let mut out = json!({"field": sp["field"], "label": label, "per": 1});
            if let Some(per) = num(sp.get("per")).filter(|p| (1.0..=3.0).contains(&p.f())) {
                out["per"] = json!(per.f().trunc() as i64);
            }
            if field_type(by_id, sp.get("tally")) == Some("number") {
                out["tally"] = sp["tally"].clone();
                if let Some(ev) = num(sp.get("every")).filter(|e| e.f() >= 1.0) {
                    if matches!(field_type(by_id, sp.get("into")), Some("select" | "number")) {
                        out["every"] = json!(ev.f().trunc() as i64);
                        out["into"] = sp["into"].clone();
                    }
                }
            }
            spec["spend"] = out;
        }
        None | Some(Value::Null) => {}
        Some(_) => warnings.push("dice: the spend field is not a resource of this template; ignored".into()),
    }
    Some(spec)
}

/// `clean_template_draft(raw)`: (draft or None, warnings)
pub fn clean_template_draft(raw: &Value) -> (Option<Value>, Vec<String>) {
    let Value::Object(raw) = raw else { return (None, vec!["the draft was not a JSON object".into()]) };
    let (fields, id_map, mut warnings) = clean_fields(raw.get("fields"));
    if fields.is_empty() {
        warnings.push("the draft had no usable fields".into());
        return (None, warnings);
    }
    let mut name = text(raw.get("name"), 80);
    if name.is_empty() {
        name = "AI-drafted system".into();
        warnings.push("the draft had no name; called it 'AI-drafted system'".into());
    }
    let (system, sys_warnings) = clean_system_spec(&remap_system(raw.get("system"), &id_map), &fields);
    let rules = raw.get("rules_md").and_then(|r| r.as_str()).map(|r| take(&crate::edits::py_strip_pub(&r.replace('\0', "")), 20000)).unwrap_or_default();
    warnings.extend(sys_warnings);
    (Some(json!({"name": name, "description": text(raw.get("description"), 300), "sheet_mode": "custom", "fields": fields, "system": system, "rules_md": rules})), warnings)
}

// ── reading a long rulebook in pieces ────────────────────────────────────────

fn clen(s: &str) -> usize {
    s.chars().count()
}

fn pack(units: &[String], max_chars: usize, joiner: &str) -> Vec<String> {
    let (mut out, mut cur) = (vec![], String::new());
    for u in units {
        if !cur.is_empty() && clen(&cur) + clen(joiner) + clen(u) <= max_chars {
            cur = format!("{cur}{joiner}{u}");
        } else {
            if !cur.is_empty() {
                out.push(cur);
            }
            cur = u.clone();
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn hard_split(block: &str, max_chars: usize) -> Vec<String> {
    static TOK: OnceLock<regex::Regex> = OnceLock::new();
    let mut pieces = vec![];
    for tok in TOK.get_or_init(|| regex::Regex::new(r"\S+\s*").unwrap()).find_iter(block) {
        let mut t: Vec<char> = tok.as_str().chars().collect();
        while t.len() > max_chars {
            pieces.push(t[..max_chars].iter().collect::<String>());
            t = t[max_chars..].to_vec();
        }
        if !t.is_empty() {
            pieces.push(t.iter().collect());
        }
    }
    pack(&pieces, max_chars, "").into_iter().map(|c| crate::edits::py_strip_pub(&c)).filter(|c| !c.is_empty()).collect()
}

/// `split_rules_text(text, max_chars)`
pub fn split_rules_text(text: &str, max_chars: usize) -> Vec<String> {
    let max_chars = max_chars.max(200);
    let text = text.replace("\r\n", "\n").replace('\0', "");
    static HEAD: OnceLock<regex::Regex> = OnceLock::new();
    let head = HEAD.get_or_init(|| regex::Regex::new(r"^#{1,6}[ \t]").unwrap());
    // split before every line that starts a heading
    let mut cuts = vec![0];
    let mut pos = 0;
    for line in text.split_inclusive('\n') {
        if pos > 0 && head.is_match(line) {
            cuts.push(pos);
        }
        pos += line.len();
    }
    cuts.push(text.len());
    static PARA: OnceLock<regex::Regex> = OnceLock::new();
    let para = PARA.get_or_init(|| regex::Regex::new(r"\n\s*\n").unwrap());
    let mut units = vec![];
    for w in cuts.windows(2) {
        let section = crate::edits::py_strip_pub(&text[w[0]..w[1]]);
        if section.is_empty() {
            continue;
        }
        if clen(&section) <= max_chars {
            units.push(section);
            continue;
        }
        let mut paras = vec![];
        for p in para.split(&section) {
            let p = crate::edits::py_strip_pub(p);
            if p.is_empty() {
                continue;
            }
            if clen(&p) <= max_chars { paras.push(p) } else { paras.extend(hard_split(&p, max_chars)) }
        }
        units.extend(pack(&paras, max_chars, "\n\n"));
    }
    pack(&units, max_chars, "\n\n")
}

const SHEET_WORDS: [&str; 35] = ["character sheet", "character creation", "create a character", "attribute", "ability score", "skill", "hit point",
    "health", "stamina", "resource", "stat", "defense", "defence", "armor", "armour", "hp", "rest", "recover", "heal", "experience", "xp", "level up",
    "advancement", "condition", "status", "class", "origin", "background", "talent", "feat", "perk", "inventory", "equipment", "starting", ""];

/// `pick_chunks(chunks, cap)`
pub fn pick_chunks(chunks: &[String], cap: usize) -> Vec<String> {
    let cap = cap.max(1);
    if chunks.len() <= cap {
        return chunks.to_vec();
    }
    let score = |c: &str| -> usize { let low = c.to_lowercase(); SHEET_WORDS.iter().filter(|w| !w.is_empty()).map(|w| low.matches(w).count().min(6)).sum() };
    let mut idx: Vec<usize> = (0..chunks.len()).collect();
    idx.sort_by_key(|&i| (std::cmp::Reverse(score(&chunks[i])), i));
    let mut best: Vec<usize> = idx.into_iter().take(cap).collect();
    best.sort();
    best.into_iter().map(|i| chunks[i].clone()).collect()
}

pub const NOTES_SYSTEM: &str = "You are reading part of a tabletop RPG rulebook to prepare a character sheet for it. Write short notes (plain text, at most 25 bullet lines) on ONLY what a character sheet needs: the stats/attributes and their ranges, derived values, resources that go up and down (hit points, stamina, mana, stress, luck…) with their maximums, what resting or healing refills, status conditions, how experience or levels work, the choices made at character creation (class, origin, background), and the lists a character carries (skills, talents, gear, spells). Quote the book's own names. Skip lore, monsters and adventures. If this part has nothing of that kind, answer exactly: NOTHING.";
