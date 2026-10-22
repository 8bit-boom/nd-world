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
