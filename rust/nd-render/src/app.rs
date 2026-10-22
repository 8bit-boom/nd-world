//! The app's data tables (data/app_data.json, written from Python by scripts/gen_rust_data.py) and the small pure functions over them that
//! every page needs: `view_prefs.clean` (a saved layout choice made safe) and `nav_menus.resolve_nav_menus` (the menus a viewer sees).
use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::OnceLock;

pub fn data() -> &'static Value {
    static D: OnceLock<Value> = OnceLock::new();
    D.get_or_init(|| serde_json::from_str(include_str!("../data/app_data.json")).expect("app_data.json"))
}

fn id_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"^[a-z0-9_-]{1,40}$").unwrap())
}

/// Python's `str(x)` for a JSON scalar; `None` for a value whose Python spelling this does not reproduce (floats, containers).
fn py_str(v: &Value) -> Option<String> {
    Some(match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        _ => return None,
    })
}

/// Python's `int(x)`: `Err` = it would raise (the caller uses the default), `None` outer = not sure.
fn py_int(v: &Value) -> Option<Result<i64, ()>> {
    Some(match v {
        Value::Bool(b) => Ok(*b as i64),
        Value::Number(n) if n.is_i64() => Ok(n.as_i64().unwrap()),
        Value::Number(n) => match n.as_f64() {
            Some(f) if f.is_finite() => Ok(f.trunc() as i64),
            _ => Err(()),
        },
        Value::String(s) => {
            if s.contains('_') || s.chars().any(|c| c.is_numeric() && !c.is_ascii_digit()) {
                return None;
            }
            s.trim_matches(|c: char| c.is_ascii_whitespace()).parse::<i64>().map_err(|_| ())
        }
        _ => Err(()),
    })
}

fn contains(list: &Value, v: &Value) -> bool {
    list.as_array().map_or(false, |a| a.iter().any(|x| x == v))
}

fn dedupe_cap(items: impl Iterator<Item = String>, cap: usize) -> Vec<Value> {
    let mut seen: Vec<String> = vec![];
    for s in items {
        if !seen.contains(&s) {
            seen.push(s);
        }
    }
    seen.truncate(cap);
    seen.into_iter().map(Value::String).collect()
}

/// `view_prefs.clean(section, raw)`. `None` = not sure (let Python render the page).
pub fn clean(section: &str, raw: &Value) -> Option<Value> {
    let s = data()["view_sections"].get(section)?;
    let empty = Map::new();
    let raw = raw.as_object().unwrap_or(&empty);
    let get = |k: &str| raw.get(k).cloned().unwrap_or(Value::Null);
    let mode = if contains(&s["modes"], &get("mode")) { get("mode") } else { s["default"].clone() };
    let sizes = &data()["view_sizes"];
    let size = match py_int(&get("size"))? {
        Ok(n) if contains(sizes, &json!(n)) => json!(n),
        _ => data()["view_default_size"].clone(),
    };
    let sort_raw = match get("sort") {
        Value::Null | Value::Bool(false) => String::new(),
        Value::String(x) if x.is_empty() => String::new(),
        Value::Number(n) if n.as_f64() == Some(0.0) => String::new(),
        v => py_str(&v).unwrap_or_default(),
    };
    let (desc, key) = match sort_raw.strip_prefix('-') {
        Some(k) => (true, k.to_string()),
        None => (false, sort_raw.clone()),
    };
    let sort = if contains(&s["sorts"], &json!(key)) { json!(format!("{}{}", if desc { "-" } else { "" }, key)) } else { s["default_sort"].clone() };
    let g = get("group");
    let group = if contains(&s["groups"], &g) {
        g
    } else if g == json!("") {
        json!("")
    } else {
        s.get("default_group").cloned().unwrap_or(json!(""))
    };
    let filters_in: Vec<String> = get("filters").as_array().map(|a| a.iter().filter(|f| contains(&s["filters"], f)).filter_map(|f| f.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let n = filters_in.len();
    let cap = s.get("max_filters").and_then(|v| v.as_u64()).map(|v| v as usize).unwrap_or(n);
    let mut out = Map::new();
    out.insert("mode".into(), mode);
    out.insert("size".into(), size);
    out.insert("sort".into(), sort);
    out.insert("group".into(), group);
    out.insert("filters".into(), Value::Array(dedupe_cap(filters_in.into_iter(), cap)));
    if let Some(lists) = s.get("lists").and_then(|l| l.as_object()) {
        for (name, cap) in lists {
            let given: Vec<String> = get(name).as_array().map(|a| a.iter().filter_map(|x| x.as_str()).filter(|x| id_re().is_match(x)).map(str::to_string).collect()).unwrap_or_default();
            out.insert(name.clone(), Value::Array(dedupe_cap(given.into_iter(), cap.as_u64().unwrap_or(0) as usize)));
        }
    }
    if let Some(opts) = s.get("options").and_then(|o| o.as_object()).filter(|o| !o.is_empty()) {
        let given = get("options");
        let mut o = Map::new();
        for (k, allowed) in opts {
            let v = given.as_object().and_then(|g| g.get(k)).cloned().unwrap_or(Value::Null);
            let sv = py_str(&v)?;
            let pick = if contains(allowed, &json!(sv)) { json!(sv) } else { allowed[0].clone() };
            o.insert(k.clone(), pick);
        }
        out.insert("options".into(), Value::Object(o));
        let pins: Vec<String> = get("pins").as_array().map(|a| a.iter().filter_map(|x| x.as_str()).filter(|x| id_re().is_match(x)).map(str::to_string).collect()).unwrap_or_default();
        let cap = s.get("max_pins").and_then(|v| v.as_u64()).unwrap_or(6) as usize;
        out.insert("pins".into(), Value::Array(dedupe_cap(pins.into_iter(), cap)));
    }
    Some(Value::Object(out))
}

/// What `resolve_nav_menus` needs to know about the viewer and the world.
pub struct NavCtx<'a> {
    pub is_gm: bool,
    /// "player" / "assistant" for a non-GM viewer (None = no role: every section is "none")
    pub role: Option<&'a str>,
    pub world: Option<&'a Map<String, Value>>,
    pub dreamlands_enabled: bool,
    pub king_in_yellow_enabled: bool,
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn wstr<'a>(w: Option<&'a Map<String, Value>>, k: &str) -> Option<&'a str> {
    w.and_then(|w| w.get(k)).and_then(|v| v.as_str())
}

/// Python's `str.capitalize()` for an ASCII kind id (custom kinds carry their own label).
fn capitalize(s: &str) -> String {
    let lower = s.to_lowercase();
    let mut c = lower.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// `build_catalog(world)`: one item per kind (built-in and custom), then the static catalog.
pub fn build_catalog(world: Option<&Map<String, Value>>) -> Option<Vec<Value>> {
    let kinds = nd_perm::sections::effective_kinds(wstr(world, "custom_kinds_json"))?;
    let customs: Vec<Value> = match wstr(world, "custom_kinds_json").and_then(|s| serde_json::from_str::<Value>(s).ok()) {
        Some(Value::Array(a)) => a,
        _ => vec![],
    };
    let icons = &data()["kind_icons"];
    let mut out = vec![];
    for k in kinds {
        let custom = customs.iter().find(|c| c.get("id").and_then(|v| v.as_str()) == Some(k.as_str()) && c.get("label").map_or(false, |l| l.is_string()));
        let label = custom.and_then(|c| c["label"].as_str()).map(str::to_string).unwrap_or_else(|| capitalize(&k));
        let icon = match custom {
            Some(c) => c.get("icon").filter(|i| truthy(Some(i))).cloned().unwrap_or(json!("🏷")),
            None => icons.get(&k).cloned().unwrap_or(json!("🏷")),
        };
        out.push(json!({"id": format!("kind_{k}"), "label": label, "icon": icon, "href": format!("/kind/{k}"), "exact": true, "condition": null,
                        "gm_only": false, "ql_type": "kind", "ql_ref": k, "player_section": format!("kind_{k}")}));
    }
    for item in data()["nav_static_catalog"].as_array()? {
        out.push(item.clone());
    }
    Some(out)
}

/// `resolve_nav_menus` -> (menus, ungrouped). `None` = not sure.
pub fn resolve_nav(c: &NavCtx) -> Option<(Vec<Value>, Vec<Value>)> {
    let catalog = build_catalog(c.world)?;
    let gm_only_hrefs: Vec<&Value> = catalog.iter().filter(|i| truthy(i.get("gm_only"))).filter_map(|i| i.get("href")).collect();
    let access = wstr(c.world, "section_access_json");
    let kinds_json = wstr(c.world, "custom_kinds_json");
    let visible = |item: &Value| -> Option<bool> {
        if c.is_gm && !truthy(item.get("gm_only")) && item.get("href").map_or(false, |h| gm_only_hrefs.contains(&h)) {
            return Some(false);
        }
        let cond = item.get("condition").and_then(|v| v.as_str());
        if cond == Some("dreamlands_enabled") && !c.dreamlands_enabled {
            return Some(false);
        }
        if cond == Some("king_in_yellow_enabled") && !c.king_in_yellow_enabled {
            return Some(false);
        }
        let section = item.get("player_section").and_then(|v| v.as_str()).filter(|s| !s.is_empty());
        if let (Some(section), false) = (section, c.is_gm) {
            let level = nd_perm::sections::section_level(false, c.role, access, kinds_json, section)?;
            if level == "none" {
                return Some(false);
            }
        } else if truthy(item.get("gm_only")) && !c.is_gm {
            return Some(false);
        }
        if let Some(cond) = cond.filter(|x| *x != "dreamlands_enabled" && *x != "king_in_yellow_enabled") {
            if !truthy(c.world.and_then(|w| w.get(cond))) {
                return Some(false);
            }
        }
        Some(true)
    };
    // load_nav_menus: the world's own menus, else the defaults
    let menus_src: Vec<Value> = match wstr(c.world, "nav_menus_json").filter(|s| !s.is_empty()).map(serde_json::from_str::<Value>) {
        Some(Ok(Value::Array(a))) => a,
        Some(Err(_)) if wstr(c.world, "nav_menus_json").map_or(false, |s| s.contains("NaN") || s.contains("Infinity")) => return None,
        _ => data()["nav_default_menus"].as_array()?.clone(),
    };
    let mut claimed: Vec<String> = vec![];
    let mut menus = vec![];
    for m in &menus_src {
        let Some(mo) = m.as_object() else { continue };
        let mut links = vec![];
        let ids: Vec<Value> = match mo.get("item_ids") {
            Some(Value::Array(a)) => a.clone(),
            Some(v) if !truthy(Some(v)) => vec![],
            None => vec![],
            Some(_) => return None, // a string / object here: Python would iterate it; not worth reproducing
        };
        for iid in ids {
            let Some(iid) = iid.as_str() else { return None };
            claimed.push(iid.to_string());
            let Some(item) = catalog.iter().find(|i| i["id"] == json!(iid)) else { continue };
            if visible(item)? {
                links.push(item.clone());
            }
        }
        if !links.is_empty() {
            menus.push(json!({"id": mo.get("id"), "label": mo.get("label"), "icon": mo.get("icon"), "links": links}));
        }
    }
    let mut ungrouped = vec![];
    for item in &catalog {
        if !claimed.iter().any(|c| Some(c.as_str()) == item["id"].as_str()) && visible(item)? {
            ungrouped.push(item.clone());
        }
    }
    Some((menus, ungrouped))
}

/// `deps.effective_kinds(world)` and `effective_subtypes(world)` as the context processor hands them to every template:
/// (kinds, kind_icons, subtypes) with the world's valid custom kinds added. `None` = not sure.
pub fn effective_kind_tables(world: Option<&Map<String, Value>>) -> Option<(Value, Value, Value)> {
    let raw = wstr(world, "custom_kinds_json");
    let kinds = nd_perm::sections::effective_kinds(raw)?;
    let ordered = |table: &str| -> Option<Map<String, Value>> {
        let src = data()[table].as_object()?;
        let mut m = Map::new();
        for k in data()[&format!("{table}_order")].as_array()? {
            let k = k.as_str()?;
            m.insert(k.to_string(), src.get(k)?.clone());
        }
        Some(m)
    };
    let mut icons = ordered("kind_icons")?;
    let mut subtypes = ordered("subtypes")?;
    let customs: Vec<Value> = match raw.filter(|s| !s.is_empty()).map(serde_json::from_str::<Value>) {
        Some(Ok(Value::Array(a))) => a,
        _ => vec![],
    };
    for k in kinds.iter().filter(|k| k.starts_with("custom_")) {
        // load_custom_kinds keeps every valid entry; a later entry with the same id wins in the dicts
        for c in customs.iter().filter(|c| c.get("id").and_then(|v| v.as_str()) == Some(k.as_str())) {
            let label_ok = c.get("label").and_then(|l| l.as_str()).map_or(false, |l| !l.trim().is_empty());
            if !label_ok {
                continue;
            }
            let icon = c.get("icon").filter(|i| truthy(Some(i))).cloned().unwrap_or(json!("🏷"));
            icons.insert(k.clone(), icon);
            if let Some(Value::Array(st)) = c.get("subtypes") {
                let list: Vec<Value> = st.iter().filter(|x| x.is_string()).cloned().collect();
                if !list.is_empty() {
                    subtypes.insert(k.clone(), Value::Array(list));
                }
            }
        }
    }
    Some((json!(kinds), Value::Object(icons), Value::Object(subtypes)))
}
