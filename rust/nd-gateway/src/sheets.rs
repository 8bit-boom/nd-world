//! The custom sheet systems' engine (a port of app/sheet_systems.py's core): a template's fields, its system spec (the built-in table
//! overlaid with the template's own `system_json`), the resource tracks with their defaults, the vital (HP) track, and the Rest rules.
//! Numbers are whole: a float in a stored value or a default (whose repr Python would print) makes the caller give the request to Python.

use serde_json::{json, Map, Value};

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

/// A sheet template row and what is read from it
pub struct Tpl {
    pub row: Map<String, Value>,
    pub fields: Vec<Map<String, Value>>,
    pub spec: Map<String, Value>,
}

impl Tpl {
    /// None for stored JSON Python reads its own way
    pub fn load(row: Map<String, Value>) -> Option<Tpl> {
        let fields = match row.get("fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()) {
            None => vec![],
            Some(raw) => match serde_json::from_str::<Value>(raw) {
                Ok(Value::Array(a)) => a.into_iter().filter_map(|f| match f { Value::Object(o) => Some(o), _ => None }).collect(),
                Ok(_) => vec![],
                Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
                Err(_) => vec![],
            },
        };
        // system_spec: the built-in entry (a built-in template, by slug) overlaid with the template's own truthy keys
        let mut spec = Map::new();
        if truthy(row.get("is_builtin")) {
            if let Some(Value::Object(b)) = row.get("slug").and_then(|s| s.as_str()).and_then(|s| nd_render::app::data()["builtin_systems"].get(s)) {
                spec = b.clone();
            }
        }
        if let Some(raw) = row.get("system_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()) {
            match serde_json::from_str::<Value>(raw) {
                Ok(Value::Object(own)) => {
                    for (k, v) in own {
                        if truthy(Some(&v)) {
                            spec.insert(k, v);
                        }
                    }
                }
                Ok(_) => {}
                Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
                Err(_) => {}
            }
        }
        Some(Tpl { row, fields, spec })
    }

    pub fn custom(&self) -> bool {
        self.row.get("sheet_mode").and_then(|v| v.as_str()) == Some("custom")
    }

    fn ids(&self) -> Vec<Value> {
        self.fields.iter().map(|f| f.get("id").cloned().unwrap_or(Value::Null)).collect()
    }

    /// `system_meta`: (hp field id, xp field ids); None for ids Python compares its own way (not text)
    pub fn meta(&self) -> Option<(Option<String>, Vec<String>)> {
        let mut hp: Option<String> = None;
        let mut xp: Vec<String> = vec![];
        for f in &self.fields {
            let ty = f.get("type").and_then(|v| v.as_str());
            if f.get("vital").and_then(|v| v.as_str()) == Some("hp") && ty == Some("resource") && hp.is_none() {
                hp = Some(f.get("id")?.as_str()?.to_string());
            }
            if truthy(f.get("xp")) && ty == Some("number") {
                xp.push(f.get("id")?.as_str()?.to_string());
            }
        }
        let ids = self.ids();
        if !self.spec.is_empty() {
            if hp.is_none() {
                if let Some(h) = self.spec.get("hp").filter(|h| ids.contains(h)) {
                    hp = Some(h.as_str()?.to_string());
                }
            }
            if xp.is_empty() {
                if let Some(Value::Array(list)) = self.spec.get("xp") {
                    for x in list.iter().filter(|x| ids.contains(x)) {
                        xp.push(x.as_str()?.to_string());
                    }
                }
            }
        }
        Some((hp, xp))
    }

    /// `system_meta`'s binds: {field id: column} - a field's own `binds` ("name" / "player_name"), then the built-in table's for ids the
    /// template has
    pub fn binds(&self) -> Option<Vec<(String, String)>> {
        let mut out: Vec<(String, String)> = vec![];
        for f in &self.fields {
            if let Some(col) = f.get("binds").and_then(|v| v.as_str()).filter(|c| *c == "name" || *c == "player_name") {
                let id = f.get("id")?.as_str()?.to_string();
                out.retain(|(k, _)| *k != id);
                out.push((id, col.to_string()));
            }
        }
        let ids = self.ids();
        if let Some(Value::Object(b)) = self.spec.get("binds") {
            for (fid, col) in b {
                if ids.contains(&json!(fid)) && !out.iter().any(|(k, _)| k == fid) {
                    out.push((fid.clone(), col.as_str()?.to_string()));
                }
            }
        }
        Some(out)
    }

    /// `enrich_fields`: the fields with the resolved vital / binds / xp written onto them
    pub fn enrich_fields(&self) -> Option<Vec<Value>> {
        let (hp, xp) = self.meta()?;
        let binds = self.binds()?;
        let mut out = vec![];
        for f in &self.fields {
            let mut f = f.clone();
            let id = f.get("id").cloned().unwrap_or(Value::Null);
            if hp.as_ref().is_some_and(|h| id == json!(h)) {
                f.insert("vital".into(), json!("hp"));
            }
            if let Some((_, col)) = binds.iter().find(|(k, _)| id == json!(k)) {
                f.insert("binds".into(), json!(col));
            }
            if xp.iter().any(|x| id == json!(x)) {
                f.insert("xp".into(), json!(true));
            }
            out.push(Value::Object(f));
        }
        Some(out)
    }

    /// `system_conditions`: the condition chips the sheet offers
    pub fn conditions(&self) -> Value {
        if let Some(c) = self.spec.get("conditions").filter(|c| truthy(Some(c))) {
            return c.clone();
        }
        if !self.custom() {
            return json!(["Burn", "Freeze", "Toxin", "Bleeding", "Blind", "Yellow", "Charm", "Daze", "Stunned"]);
        }
        json!(["Bleeding", "Burning", "Blinded", "Restrained", "Stunned", "Weakened", "Vulnerable"])
    }

    /// `sheet_pages(tpl, section names)`: how the sections are grouped into the Sheet tab's pages, or Null (one long page)
    pub fn sheet_pages(&self, names: &[String]) -> Option<Value> {
        let mut pages: Vec<Value> = vec![];
        let mut section_page: Vec<(String, String)> = vec![];
        let mut cond: Option<Value> = None;
        match self.spec.get("pages").filter(|p| truthy(Some(p))) {
            Some(Value::Array(spec)) => {
                for (idx, page) in spec.iter().enumerate() {
                    let page = page.as_object()?;
                    let pid = match page.get("id") {
                        v if !truthy(v) => json!(format!("p{idx}")),
                        Some(v) => v.clone(),
                        None => unreachable!(),
                    };
                    let wanted = match page.get("sections") {
                        None => vec![],
                        Some(Value::Array(a)) => a.clone(),
                        Some(_) => return None,
                    };
                    let mine: Vec<String> = wanted.iter().filter_map(|n| n.as_str()).filter(|n| names.iter().any(|x| x == n)).map(str::to_string).collect();
                    if mine.is_empty() {
                        continue;
                    }
                    let label = match page.get("label") { v if truthy(v) => v.cloned()?, _ => pid.clone() };
                    pages.push(json!({"id": pid, "label": label, "icon": page.get("icon").cloned().unwrap_or(json!(""))}));
                    for n in mine {
                        section_page.retain(|(k, _)| *k != n);
                        section_page.push((n, pid.as_str()?.to_string()));
                    }
                    if truthy(page.get("conditions")) {
                        cond = Some(pid);
                    }
                }
                let rest: Vec<&String> = names.iter().filter(|n| !section_page.iter().any(|(k, _)| k == *n)).collect();
                if !rest.is_empty() {
                    pages.push(json!({"id": "more", "label": "More", "icon": "➕"}));
                    for n in rest {
                        section_page.push((n.clone(), "more".into()));
                    }
                }
            }
            Some(_) => return None,
            None if names.len() >= 4 => {
                for (i, n) in names.iter().enumerate() {
                    pages.push(json!({"id": format!("s{i}"), "label": n, "icon": ""}));
                    section_page.push((n.clone(), format!("s{i}")));
                }
            }
            None => {}
        }
        if pages.len() < 2 {
            return Some(Value::Null);
        }
        let sp: Map<String, Value> = section_page.into_iter().map(|(k, v)| (k, json!(v))).collect();
        Some(json!({"pages": pages, "section_page": sp, "conditions": cond.unwrap_or_else(|| pages[0]["id"].clone()), "linked": pages.last()?["id"].clone()}))
    }

    /// `_roll_cfg`'s pool mode (`dice_spec`, `pool_dice`): Null when the system has no dice rules
    pub fn pool_roll(&self, pc: &Map<String, Value>, cf: &Map<String, Value>) -> Option<Value> {
        let Some(Value::Object(spec)) = self.spec.get("dice") else { return Some(Value::Null) };
        let pools = match spec.get("pools") {
            Some(Value::Array(a)) if !a.is_empty() => a,
            v if !truthy(v) => return Some(Value::Null),
            _ => return None, // iterating another truthy value: Python's
        };
        let int_of = |v: Option<&Value>, d: i64| -> Option<i64> {
            match v {
                x if !truthy(x) => Some(d),
                Some(Value::Number(n)) => n.as_i64(),
                _ => None,
            }
        };
        let mut out = vec![];
        for p in pools {
            let p = p.as_object()?;
            let pid = p.get("id")?;
            // pool_dice: the first pool with this id, its dice or its override's when the character's field matches
            let pool = pools.iter().filter_map(|q| q.as_object()).find(|q| q.get("id") == Some(pid))?;
            let mut dice = int_of(pool.get("dice"), 1)?;
            if let Some(Value::Object(ov)) = pool.get("override") {
                let field = ov.get("field").and_then(|v| v.as_str());
                let have = match field.and_then(|f| cf.get(f)) {
                    None => String::new(),
                    Some(Value::String(t)) => t.clone(),
                    Some(Value::Number(n)) if n.is_i64() => n.to_string(),
                    Some(_) => return None,
                };
                let want = match ov.get("equals") {
                    None => String::new(),
                    Some(Value::String(t)) => t.clone(),
                    Some(_) => return None,
                };
                if !have.is_ascii() || !want.is_ascii() {
                    return None;
                }
                let head = have.split(" (").next().unwrap_or("");
                if crate::edits::py_strip_pub(head).to_ascii_lowercase() == want.to_ascii_lowercase() {
                    dice = int_of(ov.get("dice"), dice)?;
                }
            }
            out.push(json!({"id": pid, "label": p.get("label")?, "dice": dice.max(1)}));
        }
        let spend = match spec.get("spend") {
            Some(Value::Object(sp)) if truthy(Some(&Value::Object(sp.clone()))) => json!({"field": sp.get("field")?, "label": sp.get("label")?,
                "per": sp.get("per").cloned().unwrap_or(json!(1))}),
            v if !truthy(v) => Value::Null,
            _ => return None,
        };
        Some(json!({"mode": "pool", "pc_id": pc.get("id"), "pc_name": pc.get("name"), "pools": out, "threshold": spec.get("threshold").cloned().unwrap_or(json!(6)),
            "explode": spec.get("explode").cloned().unwrap_or(json!(true)), "spend": spend}))
    }

    /// `resource_tracks(fields, cf, meta)`: every resource field {id, label, current, max} (defaults for unsaved halves), the vital one
    /// first when `vital_first`
    pub fn tracks(&self, cf: &Map<String, Value>, vital_first: bool) -> Option<Vec<Value>> {
        let mut out = vec![];
        for f in &self.fields {
            if f.get("type").and_then(|v| v.as_str()) != Some("resource") || !truthy(f.get("id")) {
                continue;
            }
            let id = f.get("id")?.as_str()?.to_string();
            let dv = match f.get("default_value") {
                v if !truthy(v) => "0/0".to_string(),
                Some(Value::String(t)) => t.clone(),
                _ => return None,
            };
            let mut halves = dv.split('/');
            let dcur = crate::edits::sheet_num_text_pub(halves.next().unwrap_or("")).ok()?.unwrap_or(0);
            let dmax = if dv.contains('/') { crate::edits::sheet_num_text_pub(halves.next().unwrap_or("")).ok()?.unwrap_or(0) } else { 0 };
            let cur = crate::edits::sheet_num_pub(cf.get(&format!("{id}_current"))).ok()?.unwrap_or(dcur);
            let mx = crate::edits::sheet_num_pub(cf.get(&format!("{id}_max"))).ok()?.unwrap_or(dmax);
            let label_src = match f.get("label") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                v if !truthy(v) => id.clone(),
                _ => return None,
            };
            let label = crate::edits::sheet_short_label_pub(&label_src)?;
            out.push(json!({"id": id, "label": label, "current": cur, "max": mx}));
        }
        if vital_first {
            if let Some(hp) = self.meta()?.0 {
                out.sort_by_key(|t| if t["id"] == json!(hp) { 0 } else { 1 });
            }
        }
        Some(out)
    }

    /// `hp_track`: the vital track, or None (Some(None))
    pub fn hp_track(&self, cf: &Map<String, Value>) -> Option<Option<Value>> {
        let Some(hp) = self.meta()?.0 else { return Some(None) };
        Some(self.tracks(cf, false)?.into_iter().find(|t| t["id"] == json!(hp)))
    }

    /// `rest_ops(tpl, kind)`: the Rest rules of this kind (a system with one Rest lists it as "long")
    pub fn rest_ops(&self, kind: &str) -> Vec<Value> {
        let rest = self.spec.get("rest").and_then(|r| r.as_object());
        let pick = rest.and_then(|r| r.get(kind).filter(|v| truthy(Some(v))).or_else(|| r.get("long").filter(|v| truthy(Some(v)))));
        pick.and_then(|v| v.as_array()).cloned().unwrap_or_default()
    }

    /// `rest_touched_keys`: every custom-field key a Rest of this system (either kind) may change
    pub fn rest_touched_keys(&self) -> Vec<String> {
        let mut keys = vec![];
        if let Some(rest) = self.spec.get("rest").and_then(|r| r.as_object()) {
            for ops in rest.values().filter_map(|v| v.as_array()) {
                for op in ops.iter().filter_map(|o| o.as_array()) {
                    let (Some(name), Some(target)) = (op.first().and_then(|v| v.as_str()), op.get(1).and_then(|v| v.as_str())) else { continue };
                    match name {
                        "full" | "add" | "empty" => {
                            for k in [format!("{target}_current"), format!("{target}_max")] {
                                if !keys.contains(&k) {
                                    keys.push(k);
                                }
                            }
                        }
                        "set" => {
                            if !keys.iter().any(|k| k == target) {
                                keys.push(target.to_string());
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        keys
    }

    /// `apply_rest(tpl, cf, kind)`: the custom fields after this system's Rest rules (None for a rule Python reads its own way)
    pub fn apply_rest(&self, cf: &Map<String, Value>, kind: &str) -> Option<Map<String, Value>> {
        let mut cf = cf.clone();
        let tracks = self.tracks(&cf, false)?;
        for op in self.rest_ops(kind) {
            let op = op.as_array()?;
            let name = op.first()?.as_str()?;
            let target = op.get(1)?.as_str()?;
            match name {
                "full" | "add" | "empty" => {
                    let Some(t) = tracks.iter().find(|t| t["id"] == json!(target)) else { continue };
                    let (cur, mx) = (t["current"].as_i64()?, t["max"].as_i64()?);
                    let new = match name {
                        "full" => mx,
                        "add" => mx.min(cur + op.get(2)?.as_i64()?),
                        _ => 0,
                    };
                    cf.insert(format!("{target}_current"), json!(new));
                    cf.insert(format!("{target}_max"), json!(mx));
                }
                "set" => {
                    cf.insert(target.to_string(), op.get(2)?.clone());
                }
                _ => {}
            }
        }
        Some(cf)
    }
}

/// `parse_custom_fields`: the character's custom fields (an object; anything else is {}); None for JSON serde reads differently
pub fn custom_fields(pc: &Map<String, Value>) -> Option<Map<String, Value>> {
    let raw = pc.get("custom_fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(o)) => Some(o),
        Ok(_) => Some(Map::new()),
        Err(_) if raw.contains("NaN") || raw.contains("Infinity") => None,
        Err(_) => Some(Map::new()),
    }
}

/// The character's sheet template, if any (None: unreadable; Some(None): no template)
pub fn pc_template(db: &rusqlite::Connection, pc: &Map<String, Value>) -> Option<Option<Tpl>> {
    let Some(tid) = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0) else { return Some(None) };
    match db.query_row("SELECT * FROM sheet_templates WHERE id = ?1", [tid], |r| crate::layout::row_map(r)) {
        Ok(row) => Some(Some(Tpl::load(row)?)),
        Err(_) => Some(None),
    }
}
