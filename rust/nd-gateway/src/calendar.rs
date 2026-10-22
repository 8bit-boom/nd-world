//! `GET /calendar` - the month view (app/routers/calendar.py `calendar_view`), with the calendar arithmetic of that router and of
//! app/calendar_config.py ported. The gateway answers only for a calendar whose stored JSON has the shape the settings form,
//! the presets and world creation write (app/calendar_config.py `clean_calendar_config`); anything else - a world with no
//! calendar row yet (Python creates it), hand-edited JSON with odd types, a query parameter Python's int() would read
//! differently, a confirmed session plan to show at the top - goes to Python unchanged.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{edit_level, query_param, render_with, section_ok, Pages};

const MAX_YEAR: i64 = 99_999;
const PICKER_ROWS: i64 = 500;
const DEFAULT_MOON_COLOR: &str = "#cccccc";
const DEFAULT_MOON_CYCLE_DAYS: i64 = 29;
const PHASES: [&str; 8] =
    ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous", "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"];
const DEFAULT_MONTH_NAMES: [&str; 12] =
    ["Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall", "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter", "Yearsend"];

fn default_months() -> Vec<Value> {
    DEFAULT_MONTH_NAMES.iter().map(|n| json!({"name": n, "days": 30})).collect()
}

/// Python's falsiness for a JSON value
fn falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

/// A key that is missing, null or a string: its text ("" for missing/null), or None for any other type
fn opt_str<'a>(o: &'a Map<String, Value>, k: &str) -> Option<&'a str> {
    match o.get(k) {
        None | Some(Value::Null) => Some(""),
        Some(Value::String(s)) => Some(s),
        _ => None,
    }
}

/// A key that is missing, null or an integer (not a bool): the integer or `default`, None for any other type
fn opt_int(o: &Map<String, Value>, k: &str, default: i64) -> Option<i64> {
    match o.get(k) {
        None | Some(Value::Null) => Some(default),
        Some(Value::Number(n)) => n.as_i64(),
        _ => None,
    }
}

fn chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn clean_color(s: &str) -> String {
    let t = nd_render::text::py_strip(s);
    let hex = t.len() > 1 && t.starts_with('#') && t[1..].chars().all(|c| c.is_ascii_hexdigit());
    if hex && (t.len() == 4 || t.len() == 7) { t.to_string() } else { String::new() }
}

/// `_safe_int` over a query parameter: None = hand the request to Python (a value its int() might read some other way)
fn safe_int_param(v: Option<String>, default: i64) -> Option<i64> {
    let Some(s) = v else { return Some(default) };
    let body = s.strip_prefix('-').unwrap_or(&s);
    if !body.is_empty() && body.chars().all(|c| c.is_ascii_digit()) {
        return s.parse::<i64>().ok().filter(|n| n.abs() < 1_000_000_000_000);
    }
    if s.chars().any(|c| c.is_numeric() || c.is_whitespace() || c == '+' || c == '_') { None } else { Some(default) }
}

struct Month {
    name: String,
    days: i64,
}

fn year_len(months: &[Month]) -> i64 {
    months.iter().map(|m| m.days).sum::<i64>().max(1)
}

fn resolve_date(months: &[Month], day: i64) -> (i64, usize, i64) {
    let total = year_len(months);
    let mut remaining = (day - 1).rem_euclid(total);
    let year = (day - 1).div_euclid(total) + 1;
    let mut idx = months.len() - 1;
    for (i, m) in months.iter().enumerate() {
        if remaining < m.days {
            idx = i;
            break;
        }
        remaining -= m.days;
    }
    (year, idx, remaining + 1)
}

fn month_start(months: &[Month], year: i64, idx: usize) -> i64 {
    (year - 1) * year_len(months) + months[..idx].iter().map(|m| m.days).sum::<i64>() + 1
}

fn normalise_month(n_months: usize, year: i64, idx: i64) -> (i64, usize) {
    let n = n_months.max(1) as i64;
    let year = year.clamp(1, MAX_YEAR);
    let absolute = ((year - 1) * n + idx).clamp(0, MAX_YEAR * n - 1);
    (absolute / n + 1, (absolute % n) as usize)
}

fn plural(n: i64, unit: &str) -> String {
    if n == 1 { format!("{n} {unit}") } else { format!("{n} {unit}s") }
}

fn relative_months_label(delta: i64, per_year: i64) -> String {
    if delta == 0 {
        return "this month".into();
    }
    if delta.abs() == 1 && per_year > 1 {
        return if delta > 0 { "next month" } else { "last month" }.into();
    }
    let (years, months) = (delta.abs() / per_year.max(1), delta.abs() % per_year.max(1));
    let mut parts = vec![];
    if years != 0 {
        parts.push(plural(years, "year"));
    }
    if months != 0 {
        parts.push(plural(months, "month"));
    }
    let span = parts.join(", ");
    if delta > 0 { format!("in {span}") } else { format!("{span} ago") }
}

fn month_href(year: i64, idx: usize) -> String {
    format!("/calendar?year={year}&month={idx}")
}

/// `format_year`: the year as the world writes it ("Year 427", "427 Y.S.F.")
fn format_year(cfg: &Map<String, Value>, year: i64) -> Option<String> {
    let fmt = chars(nd_render::text::py_strip(opt_str(cfg, "year_format")?), 40);
    let fmt = if fmt.contains("{year}") { fmt } else { "Year {year}".to_string() };
    let era = nd_render::text::py_strip(opt_str(cfg, "era_name")?);
    let text = fmt.replace("{year}", &year.to_string()).replace("{era}", era);
    Some(text.split_whitespace().collect::<Vec<_>>().join(" "))
}

/// `moon_phase_for_day`
fn moon_phase(m: &Map<String, Value>, day: i64) -> Option<Value> {
    let cycle = match opt_int(m, "cycle_days", 0)? {
        0 => DEFAULT_MOON_CYCLE_DAYS,
        c => c,
    }
    .max(1);
    let offset = opt_int(m, "offset", 0)?;
    let t = (day - 1 - offset).rem_euclid(cycle) as f64 / cycle as f64;
    let illum = (1.0 - (2.0 * std::f64::consts::PI * t).cos()) / 2.0;
    let phase = ((t * 8.0).round_ties_even() as i64).rem_euclid(8) as usize;
    let waxing = t < 0.5;
    let dark = ((1.0 - illum) * 100.0).round_ties_even() as i64;
    let name = opt_str(m, "name")?;
    let color = opt_str(m, "color")?;
    Some(json!({
        "name": if name.is_empty() { "Moon" } else { name },
        "color": if color.is_empty() { DEFAULT_MOON_COLOR } else { color },
        "phase_name": PHASES[phase],
        "illum_pct": (illum * 100.0).round_ties_even() as i64,
        "dark_pct": dark,
        "waxing": waxing,
        "lit_offset_pct": if waxing { dark } else { -dark },
    }))
}

/// `weekday_names_of` for a stored list of names (anything else: None, Python's turn)
fn weekday_names(cfg: &Map<String, Value>, dpw: usize) -> Option<Vec<String>> {
    let raw = cfg.get("weekday_names").filter(|v| !falsy(v));
    let Some(raw) = raw else { return Some(vec![]) };
    let mut names = vec![];
    for x in raw.as_array()? {
        names.push(chars(nd_render::text::py_strip(x.as_str()?), 24));
    }
    if names.iter().all(|n| n.is_empty()) {
        return Some(vec![]);
    }
    names.truncate(dpw);
    names.resize(dpw, String::new());
    Some(names.into_iter().enumerate().map(|(i, n)| if n.is_empty() { format!("Day {}", i + 1) } else { n }).collect())
}

/// `holiday_index`: {(month_idx, day): [holiday, ...]}
fn holidays(cfg: &Map<String, Value>, months: &[Month]) -> Option<Vec<(usize, i64, Value)>> {
    let raw = cfg.get("holidays").filter(|v| !falsy(v));
    let Some(raw) = raw else { return Some(vec![]) };
    let mut out = vec![];
    for h in raw.as_array()?.iter().take(100) {
        let Some(h) = h.as_object() else { continue };
        let name = chars(nd_render::text::py_strip(opt_str(h, "name")?), 60);
        let idx = match h.get("month") {
            None | Some(Value::Null) | Some(Value::Bool(_)) => None,
            Some(Value::Number(n)) => Some(n.as_i64()?).filter(|i| (0..months.len() as i64).contains(i)).map(|i| i as usize),
            Some(Value::String(s)) => {
                if !s.is_ascii() {
                    return None;
                }
                let t = s.trim();
                if t.is_empty() {
                    None
                } else if t.chars().all(|c| c.is_ascii_digit()) {
                    t.parse::<usize>().ok().filter(|i| *i < months.len())
                } else {
                    months.iter().position(|m| nd_render::text::py_strip(&m.name).to_lowercase() == t.to_lowercase())
                }
            }
            _ => return None,
        };
        let (true, Some(idx)) = (!name.is_empty(), idx) else { continue };
        let day = match opt_int(h, "day", 0)? {
            0 => 1,
            d => d,
        }
        .clamp(1, months[idx].days);
        let mut e = Map::new();
        e.insert("name".into(), json!(name));
        e.insert("month".into(), json!(idx));
        e.insert("day".into(), json!(day));
        let notes = chars(nd_render::text::py_strip(opt_str(h, "notes")?), 200);
        if !notes.is_empty() {
            e.insert("notes".into(), json!(notes));
        }
        let color = clean_color(opt_str(h, "color")?);
        if !color.is_empty() {
            e.insert("color".into(), json!(color));
        }
        out.push((idx, day, Value::Object(e)));
    }
    out.sort_by_key(|(m, d, _)| (*m, *d));
    Some(out)
}

pub fn calendar_view(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "calendar")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    // a confirmed session plan is shown at the top of the month: that line is still Python's
    let plans: i64 = db.query_row("SELECT count(*) FROM session_plans WHERE world_id = ?1 AND status = 'confirmed'", [wid], |r| r.get(0)).ok()?;
    if plans > 0 {
        return None;
    }
    let raw: Option<String> = db.query_row("SELECT config_json FROM world_calendars WHERE world_id = ?1 ORDER BY id LIMIT 1", [wid], |r| r.get(0)).ok()?;
    let raw = raw.filter(|s| !s.is_empty()).unwrap_or_else(|| "{}".into());
    let parsed: Value = serde_json::from_str(&raw).ok()?;
    let config = if falsy(&parsed) {
        json!({"era_name": "Year 1", "current_day": 1, "months": default_months(), "days_per_week": 7, "moons": []})
    } else {
        parsed
    };
    let c = config.as_object()?;
    let months_v: Vec<Value> = match c.get("months").filter(|v| !falsy(v)) {
        Some(v) => v.as_array()?.clone(),
        None => default_months(),
    };
    let mut months = vec![];
    for m in &months_v {
        let o = m.as_object()?;
        opt_str(o, "season")?;
        opt_str(o, "color")?;
        months.push(Month { name: o.get("name")?.as_str()?.to_string(), days: o.get("days")?.as_i64()? });
    }
    let max_day = MAX_YEAR * year_len(&months);
    let current_day = opt_int(c, "current_day", 1)?.clamp(1, max_day);
    let (cur_year, cur_idx, cur_dom) = resolve_date(&months, current_day);
    let year_q = safe_int_param(query_param(uri, "year"), cur_year)?;
    let month_q = safe_int_param(query_param(uri, "month"), cur_idx as i64)?;
    let (year, idx) = normalise_month(months.len(), year_q, month_q);
    let month = months_v[idx].as_object()?;
    let start = month_start(&months, year, idx);
    let end = start + months[idx].days - 1;

    let mut st = db
        .prepare(
            "SELECT e.id, e.title, e.notes, e.color, e.entity_id, en.name AS entity_label, e.session_id, gs.id AS gs_id, gs.session_num, \
             gs.title AS gs_title, e.character_id, pc.name AS character_label, e.party_id, pa.name AS party_label, e.created_by_user_id, e.day \
             FROM calendar_events e LEFT JOIN entities en ON en.id = e.entity_id LEFT JOIN game_sessions gs ON gs.id = e.session_id \
             LEFT JOIN player_characters pc ON pc.id = e.character_id LEFT JOIN parties pa ON pa.id = e.party_id \
             WHERE e.world_id = ?1 AND e.day >= ?2 AND e.day <= ?3 ORDER BY e.day, e.id",
        )
        .ok()?;
    let mut events: std::collections::HashMap<i64, Vec<Value>> = Default::default();
    let rows = st.query_map(rusqlite::params![wid, start, end], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    for f in rows {
        let day = f.get("day")?.as_i64()?;
        // Python's joinedload gives None for a link to a row that is gone; the LEFT JOIN does the same
        let session_label = if f.get("gs_id")?.is_null() {
            Value::Null
        } else {
            json!(format!("#{} {}", py_text(f.get("session_num")?)?, py_text(f.get("gs_title")?)?))
        };
        let mut e = Map::new();
        for k in ["id", "title", "notes", "color", "entity_id", "entity_label", "session_id"] {
            e.insert(k.into(), f.get(k)?.clone());
        }
        e.insert("session_label".into(), session_label);
        for k in ["character_id", "character_label", "party_id", "party_label", "created_by_user_id"] {
            e.insert(k.into(), f.get(k)?.clone());
        }
        events.entry(day).or_default().push(Value::Object(e));
    }
    let mut st = db
        .prepare("SELECT id, image_url, label, day FROM calendar_day_icons WHERE world_id = ?1 AND day >= ?2 AND day <= ?3 ORDER BY created_at, id")
        .ok()?;
    let mut icons: std::collections::HashMap<i64, Vec<Value>> = Default::default();
    for f in st.query_map(rusqlite::params![wid, start, end], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()? {
        let v = json!({"id": f.get("id")?, "image_url": f.get("image_url")?, "label": f.get("label")?});
        icons.entry(f.get("day")?.as_i64()?).or_default().push(v);
    }

    let moons: Vec<&Map<String, Value>> = match c.get("moons") {
        Some(Value::Array(a)) => a.iter().map(|m| m.as_object()).collect::<Option<_>>()?,
        _ => vec![],
    };
    let hols = holidays(c, &months)?;
    let mut days = vec![];
    for i in 0..months[idx].days {
        let dn = start + i;
        let hs: Vec<Value> = hols.iter().filter(|(m, d, _)| *m == idx && *d == i + 1).map(|(_, _, h)| h.clone()).collect();
        let ms = moons.iter().map(|m| moon_phase(m, dn)).collect::<Option<Vec<_>>>()?;
        let mut d = Map::new();
        d.insert("day_num".into(), json!(dn));
        d.insert("dom".into(), json!(i + 1));
        d.insert("is_current".into(), json!(dn == current_day));
        d.insert("events".into(), Value::Array(events.remove(&dn).unwrap_or_default()));
        d.insert("icons".into(), Value::Array(icons.remove(&dn).unwrap_or_default()));
        d.insert("holidays".into(), Value::Array(hs));
        d.insert("moons".into(), Value::Array(ms));
        days.push(Value::Object(d));
    }
    let dpw = match c.get("days_per_week") {
        None => 7,
        Some(Value::Null) => 7,
        Some(Value::Number(n)) => match n.as_i64()?.clamp(1, 60) {
            0 => 7,
            x => x,
        },
        _ => return None,
    };
    let lead_pad = (start - 1).rem_euclid(dpw);
    let n = months.len() as i64;
    let first = (year, idx) == (1, 0);
    let last = year == MAX_YEAR && idx as i64 == n - 1;
    let (py, pm) = normalise_month(months.len(), year, idx as i64 - 1);
    let (ny, nm) = normalise_month(months.len(), year, idx as i64 + 1);
    let opt = |cond: bool, s: String| if cond { Value::Null } else { json!(s) };
    let nav = json!({
        "prev_month": opt(first, month_href(py, pm)),
        "next_month": opt(last, month_href(ny, nm)),
        "prev_year": opt(year <= 1, month_href(year - 1, idx)),
        "next_year": opt(year >= MAX_YEAR, month_href(year + 1, idx)),
        "prev_decade": opt(year <= 1, month_href((year - 10).max(1), idx)),
        "next_decade": opt(year >= MAX_YEAR, month_href((year + 10).min(MAX_YEAR), idx)),
        "today": month_href(cur_year, cur_idx),
        "year": format!("/calendar/year?year={year}"),
        "prev_event": format!("/calendar/event-jump?dir=prev&from={start}"),
        "next_event": format!("/calendar/event-jump?dir=next&from={end}"),
    });
    let offset_label = relative_months_label(((year - 1) * n + idx as i64) - ((cur_year - 1) * n + cur_idx as i64), n);
    let none_notice = match query_param(uri, "none").as_deref() {
        Some("next") => json!("No later events on the calendar."),
        Some("prev") => json!("No earlier events on the calendar."),
        _ => Value::Null,
    };
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(rusqlite::params![wid, PICKER_ROWS], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    let named = |sql: &str| -> Option<Vec<Value>> { rows(sql)?.into_iter().map(|r| Some(json!({"id": r.get("id")?, "name": r.get("name")?}))).collect() };
    let entities = named("SELECT id, name FROM entities WHERE world_id = ?1 ORDER BY name, id LIMIT ?2")?;
    let sessions = rows("SELECT id, session_num, title FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC, id LIMIT ?2")?
        .into_iter()
        .map(|r| Some(json!({"id": r.get("id")?, "label": format!("#{} {}", py_text(r.get("session_num")?)?, py_text(r.get("title")?)?)})))
        .collect::<Option<Vec<_>>>()?;
    let characters = named("SELECT id, name FROM player_characters WHERE world_id = ?1 ORDER BY name, id LIMIT ?2")?;
    let parties = named("SELECT id, name FROM parties WHERE world_id = ?1 ORDER BY name, id LIMIT ?2")?;
    let can_add = edit_level(&p, &world, "calendar")?;
    let can_manage = can_add && (p.is_gm || p.role.as_deref() == Some("assistant"));
    let era_name = match c.get("era_name") {
        None => json!("Year"),
        Some(v @ Value::String(_)) => v.clone(),
        _ => return None,
    };
    let date_label = format!("{} {}, {}", months[cur_idx].name, cur_dom, format_year(c, cur_year)?);
    let ins = |k: &str, v: Value, ctx: &mut Map<String, Value>| {
        ctx.insert(k.into(), v);
    };
    let ctx = &mut p.ctx;
    ins("config", config.clone(), ctx);
    ins("months", Value::Array(months_v.clone()), ctx);
    ins("month", Value::Object(month.clone()), ctx);
    ins("month_idx", json!(idx), ctx);
    ins("year", json!(year), ctx);
    ins("days", Value::Array(days), ctx);
    ins("era_name", era_name, ctx);
    ins("days_per_week", json!(dpw), ctx);
    ins("lead_pad", json!((0..lead_pad).collect::<Vec<_>>()), ctx);
    ins("current_day", json!(current_day), ctx);
    ins("cur_year", json!(cur_year), ctx);
    ins("cur_month_idx", json!(cur_idx), ctx);
    ins("cur_dom", json!(cur_dom), ctx);
    ins("prev_month", json!(pm), ctx);
    ins("next_month", json!(nm), ctx);
    ins("nav", nav, ctx);
    ins("offset_label", json!(offset_label), ctx);
    ins("none_notice", none_notice, ctx);
    ins("max_year", json!(MAX_YEAR), ctx);
    ins("month_start", json!(start), ctx);
    ins("weekday_names", json!(weekday_names(c, dpw as usize)?), ctx);
    ins("season", json!(opt_str(month, "season")?), ctx);
    ins("month_color", json!(opt_str(month, "color")?), ctx);
    ins("year_text", json!(format_year(c, year)?), ctx);
    ins("cur_year_text", json!(format_year(c, cur_year)?), ctx);
    ins("today_label", json!(date_label), ctx);
    ins("next_session", Value::Null, ctx);
    ins("entities", Value::Array(entities), ctx);
    ins("sessions", Value::Array(sessions), ctx);
    ins("characters", Value::Array(characters), ctx);
    ins("parties", Value::Array(parties), ctx);
    ins("can_manage", json!(can_manage), ctx);
    ins("can_add_event", json!(can_add), ctx);
    ins("current_user_id", json!(p.user_id), ctx);
    render_with(pages, "calendar/month.html", p.ctx, headers)
}

/// a value as Python's f-string writes it (None for null); None = a type the gateway leaves to Python
fn py_text(v: &Value) -> Option<String> {
    Some(match v {
        Value::Null => "None".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) if n.is_i64() => n.to_string(),
        _ => return None,
    })
}
