
/// `json.loads(text or default)` where the two readers agree; the default on a genuine parse error (Python's
/// `except ValueError`), None when the text Python reads differently (NaN, a whole number past 64 bits) - decline
pub(crate) fn py_json_or(text: Option<&str>, default: &str) -> Option<Value> {
    let t = text.filter(|s| !s.is_empty()).unwrap_or(default);
    if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
        return None;
    }
    Some(serde_json::from_str(t).unwrap_or_else(|_| serde_json::from_str(default).unwrap_or(Value::Null)))
}

/// `_safe_export_filename`'s filter: an ASCII name (Python's Unicode-aware `isalnum` keeps more; a name with any of
/// that stays Python's, file name included)
fn export_name(name: Option<&str>) -> Option<String> {
    let raw = name.filter(|s| !s.is_empty()).unwrap_or("character");
    if !raw.chars().all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_') {
        return None;
    }
    let filtered: String = raw.chars().filter(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_').collect();
    Some(if filtered.is_empty() { "character".to_string() } else { filtered })
}

/// `_can_view_character` (app/routers/characters.py): the GM, the owner, or a member of the character's own world while it
/// shows the party (the character's world, not the active one)
fn can_view_pc(db: &rusqlite::Connection, p: &crate::layout::PageCtx, pc: &Map<String, Value>) -> Option<bool> {
    if p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id) {
        return Some(true);
    }
    let wid = pc.get("world_id")?.as_i64()?;
    let see: Option<i64> = db.query_row("SELECT players_see_party FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok();
    let member: bool = db.query_row("SELECT 1 FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [wid, p.user_id], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
    Some(member && see.unwrap_or(0) != 0 && pc.get("owner_user_id").and_then(|v| v.as_i64()).is_some())
}

/// `GET /api/characters/<id>/vitals` (app/routers/characters.py `character_vitals`): the live sheet numbers with the same
/// effective maxima, for anyone who may view the sheet
fn pc_vitals(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !can_view_pc(&db, &p, &pc)? {
        return Some(crate::idpages::refusal(403));
    }
    let (_, shock_max, pp_max, mp_max, _) = crate::edits::pc_maxima(&db, &pc)?;
    let d = crate::edits::derived(&pc)?;
    let conds = py_json_or(pc.get("conditions_json").and_then(|v| v.as_str()), "[]")?;
    let Some(list) = crate::edits::clean_conditions(&conds) else { return None };
    crate::edits::json_response(&json!({
        "level": pc.get("level"), "xp": pc.get("xp").and_then(|v| v.as_i64()).unwrap_or(0),
        "xp_hi": d.get("xp_hi"), "xp_pct": d.get("xp_pct"),
        "hp": pc.get("current_hp").and_then(|v| v.as_i64()).unwrap_or(0), "max_hp": d.get("hp_max"),
        "temp_hp": pc.get("temp_hp").and_then(|v| v.as_i64()).unwrap_or(0),
        "shock": pc.get("shock_current").and_then(|v| v.as_i64()).unwrap_or(0), "shock_max": shock_max,
        "pp": pc.get("pp_current").and_then(|v| v.as_i64()).unwrap_or(0), "pp_max": pp_max,
        "mp": pc.get("mp_current").and_then(|v| v.as_i64()).unwrap_or(0), "mp_max": mp_max,
        "conditions": list,
    }))
}

/// `GET /api/characters/<id>/sync` (app/routers/characters.py `character_sync_get`): the live-sync dict of a character the
/// caller may manage (the GM or the owner, of any world - Python checks no world here)
fn pc_sync_get(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    crate::edits::json_response(&crate::edits::pc_sync_dict(&pc)?)
}

/// `GET /api/characters/<id>/advance` (app/routers/characters.py `character_advance_options`): what this N&D character could
/// buy with its unspent XP; a custom system's 400 keeps Python's wording
fn pc_advance_get(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    if !crate::edits::pc_maxima(&db, &pc)?.4 {
        return Some(crate::ai_more::detail(StatusCode::BAD_REQUEST,
            "Advancement here follows the Neon & Dragons costs; this system spends XP on its own sheet"));
    }
    let c = crate::edits::Ctx { cfg, upload: None, created: vec![], removals: vec![], db, p, uri, headers, body: b"" };
    crate::edits::json_response(&crate::edits::adv_options_pub(&c, &pc)?)
}

/// `GET /api/characters/<id>/ability-cards` (app/routers/characters.py `character_ability_cards`): the feats as cards with
/// their suggested costs and the pools they are spent from
fn pc_ability_cards(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let (_, _, _, _, native) = crate::edits::pc_maxima(&db, &pc)?;
    let c = crate::edits::Ctx { cfg, upload: None, created: vec![], removals: vec![], db, p, uri, headers, body: b"" };
    let cards = crate::edits::ability_cards_pub(&c, &pc)?;
    crate::edits::json_response(&json!({"cards": cards,
        "pools": {"pp": pc.get("pp_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "mp": pc.get("mp_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "shock": pc.get("shock_current").and_then(|v| v.as_i64()).unwrap_or(0),
                  "hp": pc.get("current_hp").and_then(|v| v.as_i64()).unwrap_or(0)},
        "native": native}))
}

/// `GET /characters/<id>/export.json` (app/routers/characters.py `character_export_json`): the canonical import shape,
/// `json.dumps(payload, ensure_ascii=False, indent=2)` as a download; zero / empty fields omitted as Python omits them
fn pc_export_json(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let mut out = Map::new();
    // the live scalar columns in Python's order: a value Python reads as truthy goes in as its column type (level / xp as numbers)
    for (f, _num) in [("name", false), ("player_name", false), ("race", false), ("char_class", false), ("level", true), ("xp", true), ("backstory", false), ("notes", false)] {
        let v = pc.get(f)?;
        let keep = match v {
            Value::Null => false,
            Value::Number(n) => n.as_i64().map_or(false, |i| i != 0),
            Value::String(t) => !t.is_empty(),
            _ => return None,
        };
        if keep {
            out.insert(f.into(), v.clone());
        }
    }
    // setdefault("name", pc.name or "Character"): appended at the end when the name column is empty
    out.entry("name".to_string()).or_insert_with(|| json!(pc.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("Character")));
    for (f, v) in [("race_id", "race_id"), ("profession_id", "profession_id")] {
        let _ = v;
        if let Some(i) = pc.get(f).and_then(|x| x.as_i64()).filter(|i| *i != 0) {
            out.insert(f.into(), json!(i));
        }
    }
    for f in ["level", "xp", "max_hp", "current_hp"] {
        if let Some(i) = pc.get(f).and_then(|x| x.as_i64()).filter(|i| *i != 0) {
            out.entry(f.to_string()).or_insert(json!(i));
        }
    }
    for f in ["custom_fields_json", "app_extra_json"] {
        let v = py_json_or(pc.get(f).and_then(|x| x.as_str()), "{}")?;
        if v.as_object().map_or(false, |o| !o.is_empty()) {
            out.insert(f.trim_end_matches("_json").into(), v);
        }
    }
    for f in ["stats_json", "skills_json", "currency_json", "equipment_json", "feats_json", "attacks_json", "cyberware_json", "conditions_json"] {
        let v = py_json_or(pc.get(f).and_then(|x| x.as_str()), "[]")?;
        if v.as_array().map_or(false, |a| !a.is_empty()) {
            out.insert(f.trim_end_matches("_json").into(), v);
        }
    }
    if let Some(i) = pc.get("sheet_template_id").and_then(|x| x.as_i64()).filter(|i| *i != 0) {
        out.insert("sheet_template_id".into(), json!(i));
    }
    if let Some(Value::String(u)) = pc.get("portrait_url").filter(|v| !v.as_str().unwrap_or_default().is_empty()) {
        out.insert("portrait_url".into(), json!(u));
    }
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = nd_render::py_dumps_indent_as(&Value::Object(out), 2, false);
    let mut r = crate::edits::json_response_raw(&body, "application/json")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.json\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `_group_by_section`: template fields by their `section`, first-seen order
fn group_by_section(fields: &[Value]) -> Option<Vec<(String, Vec<&Map<String, Value>>)>> {
    let mut out: Vec<(String, Vec<&Map<String, Value>>)> = vec![];
    for f in fields {
        let f = f.as_object()?;
        let sec = match f.get("section") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(Value::String(_)) | Some(Value::Null) | None => "Custom".to_string(),
            _ => return None,
        };
        match out.iter_mut().find(|(t, _)| *t == sec) {
            Some((_, v)) => v.push(f),
            None => out.push((sec, vec![f])),
        }
    }
    Some(out)
}

/// `_pc_field_value_lines`: one (label, value) pair per template field, a "list" field expanding per item, a "resource"
/// reading its own current / max, anything Python's `str()` would spell differently left to Python
fn field_value_lines(field: &Map<String, Value>, cf: &Map<String, Value>) -> Option<Vec<(String, String)>> {
    let py_scalar = |v: &Value| -> Option<String> {
        match v {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) if n.as_i64().is_some() => Some(n.as_i64().unwrap().to_string()),
            _ => None,
        }
    };
    let fid = match field.get("id") {
        Some(Value::String(s)) => s.clone(),
        _ => return None,
    };
    let label = match field.get("label") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(Value::String(_)) | Some(Value::Null) | None => fid.clone(),
        _ => return None,
    };
    if field.get("type").and_then(|v| v.as_str()) == Some("list") {
        let items = match cf.get(&fid) {
            Some(Value::Array(a)) => a.clone(),
            Some(Value::Null) | None => return Some(vec![]),
            _ => return Some(vec![]), // Python: not a list (or empty) -> []
        };
        if items.is_empty() {
            return Some(vec![]);
        }
        let item_fields = match field.get("item_fields") {
            Some(Value::Array(a)) => a.clone(),
            Some(Value::Null) | None => vec![],
            _ => return None,
        };
        let mut out = vec![];
        for (i, item) in items.iter().enumerate() {
            let Some(item) = item.as_object() else { continue };
            let mut parts = vec![];
            for sf in &item_fields {
                let Some(sf) = sf.as_object() else { return None };
                let key = match sf.get("id") {
                    Some(Value::String(s)) => s.clone(),
                    _ => return None,
                };
                let val = match item.get(&key) {
                    Some(v @ Value::String(s)) if !s.is_empty() => py_scalar(v)?,
                    Some(v @ Value::Number(_)) => py_scalar(v)?,
                    _ => continue,
                };
                let lab = match sf.get("label") {
                    Some(Value::String(s)) if !s.is_empty() => s.clone(),
                    _ => key.clone(),
                };
                parts.push(format!("{lab}: {val}"));
            }
            out.push((format!("{label} #{}", i + 1), parts.join("; ")));
        }
        return Some(out);
    }
    if field.get("type").and_then(|v| v.as_str()) == Some("resource") {
        let text = crate::edits::field_value_text(field, cf)?;
        if text.is_empty() {
            return Some(vec![]);
        }
        let short = crate::edits::sheet_short_label_pub(&label)?;
        return Some(vec![(if short.is_empty() { label } else { short }, text)]);
    }
    let value = match cf.get(&fid) {
        Some(v) => v.clone(),
        None => field.get("default_value").cloned().unwrap_or(Value::String(String::new())),
    };
    if value.is_null() || value == json!("") {
        return Some(vec![]);
    }
    Some(vec![(label, py_scalar(&value)?)])
}

/// `_pc_export_sections`: the flat (name, [(label, value)]) summary the .md export renders
fn pc_export_sections(db: &rusqlite::Connection, pc: &Map<String, Value>) -> Option<Vec<(String, Vec<(String, String)>)>> {
    let chosen_tpl = match pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0) {
        Some(tid) => db.query_row("SELECT * FROM sheet_templates WHERE id = ?1", [tid], |r| crate::layout::row_map(r)).ok(),
        None => None,
    };
    let cf = py_json_or(pc.get("custom_fields_json").and_then(|v| v.as_str()), "{}")?;
    let Some(cf) = cf.as_object() else { return None };
    let is_custom = chosen_tpl.as_ref().and_then(|t| t.get("sheet_mode").and_then(|v| v.as_str())) == Some("custom");
    let s = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("");
    let n = |k: &str| pc.get(k).and_then(|v| v.as_i64());
    let mut sections: Vec<(String, Vec<(String, String)>)> = vec![];
    let mut basics = vec![];
    let tpl_name = chosen_tpl.as_ref().and_then(|t| t.get("name")).and_then(|v| v.as_str()).unwrap_or("");
    for (l, v) in [("System", tpl_name.to_string()), ("Player", s("player_name").to_string()), ("Race", s("race").to_string()), ("Class", s("char_class").to_string()),
                   ("Level", n("level").filter(|_| !is_custom).map(|x| x.to_string()).unwrap_or_default()),
                   ("XP", n("xp").filter(|_| !is_custom).map(|x| x.to_string()).unwrap_or_default())] {
        if !v.is_empty() {
            basics.push((l.to_string(), v));
        }
    }
    if !basics.is_empty() {
        sections.push(("Basics".into(), basics));
    }
    let conds = py_json_or(pc.get("conditions_json").and_then(|v| v.as_str()), "[]")?;
    if let Some(list) = crate::edits::clean_conditions(&conds) {
        if !list.is_empty() {
            sections.push(("Conditions".into(), vec![("Active".into(), list.join(", "))]));
        }
    } else {
        return None;
    }
    if !is_custom {
        let d = crate::edits::derived(pc)?;
        let mut resources = vec![("HP".to_string(), format!("{}/{}", n("current_hp").unwrap_or(0), d.get("hp_max")?.as_i64()?))];
        if d.get("shock_max")?.as_i64()? != 0 {
            resources.push(("Shock".into(), format!("{}/{}", d.get("shock_current")?.as_i64()?, d.get("shock_max")?.as_i64()?)));
        }
        if d.get("pp_current")?.as_i64()? != 0 || d.get("mp_current")?.as_i64()? != 0 {
            resources.push(("PP".into(), d.get("pp_current")?.as_i64()?.to_string()));
            resources.push(("MP".into(), d.get("mp_current")?.as_i64()?.to_string()));
        }
        if let Some(ac) = n("armor_class").filter(|x| *x != 0) {
            resources.push(("Armor Class".into(), ac.to_string()));
        }
        if let Some(sp) = n("speed").filter(|x| *x != 0) {
            resources.push(("Speed".into(), sp.to_string()));
        }
        sections.push(("Resources".into(), resources));
        let stat_line = |v: &Value| -> Option<(String, String)> {
            let st = v.as_object()?;
            let label = match st.get("label") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match st.get("id") {
                    Some(Value::String(t)) if !t.is_empty() => t.clone(),
                    Some(Value::String(_)) | Some(Value::Null) | None => String::new(),
                    _ => return None,
                },
            };
            let value = match st.get("value") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Number(n)) => n.as_i64()?.to_string(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            Some((label, value))
        };
        let stats: Option<Vec<(String, String)>> = d.get("stats")?.as_array()?.iter()
            .filter(|v| v.as_object().map_or(false, |o| o.contains_key("id"))).map(stat_line).collect();
        let stats = stats?;
        if !stats.is_empty() {
            sections.push(("Ability Scores".into(), stats));
        }
        let cur_line = |v: &Value| -> Option<(String, String)> {
            let c = v.as_object()?;
            let label = match c.get("label") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match c.get("abbr") {
                    Some(Value::String(t)) if !t.is_empty() => t.clone(),
                    _ => String::new(),
                },
            };
            let value = match c.get("value") {
                Some(Value::Number(n)) => n.as_i64()?.to_string(),
                Some(Value::Null) | None => "0".to_string(),
                _ => return None,
            };
            Some((label, value))
        };
        let currency: Option<Vec<(String, String)>> = d.get("currency")?.as_array()?.iter().map(cur_line).collect();
        let currency = currency?;
        if !currency.is_empty() {
            sections.push(("Currency".into(), currency));
        }
        let mut equipment = vec![];
        for it in d.get("equipment")?.as_array()? {
            let Some(it) = it.as_object() else { continue };
            let qty = match it.get("qty") {
                Some(Value::Number(x)) => x.as_i64().unwrap_or(1),
                Some(Value::Null) | None => 1,
                _ => return None,
            };
            let notes = match it.get("notes") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            let name = match it.get("name") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            equipment.push((name, if notes.is_empty() { format!("x{qty}") } else { format!("x{qty} — {notes}") }));
        }
        if !equipment.is_empty() {
            sections.push(("Equipment".into(), equipment));
        }
        let mut feats = vec![];
        for f in d.get("feats")?.as_array()? {
            let Some(f) = f.as_object() else { continue };
            let name = match f.get("name") {
                Some(Value::String(t)) => t.clone(),
                Some(Value::Null) | None => String::new(),
                _ => return None,
            };
            let notes = match f.get("notes") {
                Some(Value::String(t)) if !t.is_empty() => t.clone(),
                _ => match f.get("description") {
                    Some(Value::String(t)) => t.clone(),
                    Some(Value::Null) | None => String::new(),
                    _ => return None,
                },
            };
            feats.push((name, notes));
        }
        if !feats.is_empty() {
            sections.push(("Feats".into(), feats));
        }
    }
    if let Some(tpl) = &chosen_tpl {
        let fields = py_json_or(tpl.get("fields_json").and_then(|v| v.as_str()), "[]")?;
        let fields = fields.as_array()?;
        for (name, fs) in group_by_section(fields)? {
            let mut pairs = vec![];
            for f in fs {
                pairs.extend(field_value_lines(f, cf)?);
            }
            if !pairs.is_empty() {
                sections.push((name, pairs));
            }
        }
    }
    let mut personality = vec![];
    for (l, v) in [("Personality Traits", s("personality_traits")), ("Ideals", s("ideals")), ("Bonds", s("bonds")), ("Flaws", s("flaws"))] {
        if !v.is_empty() {
            personality.push((l.to_string(), v.to_string()));
        }
    }
    if !personality.is_empty() {
        sections.push(("Personality".into(), personality));
    }
    Some(sections)
}

/// `GET /characters/<id>/export.md` (app/routers/characters.py `character_export_md`): the character as the same markdown file
fn pc_export_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let pc = db.query_row("SELECT * FROM player_characters WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(pc) = pc else { return Some(crate::idpages::refusal(404)) };
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(crate::idpages::refusal(403));
    }
    let s = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("");
    let n = |k: &str| pc.get(k).and_then(|v| v.as_i64());
    let native = crate::edits::pc_maxima(&db, &pc)?.4;
    let mut lines = vec![format!("# {}", if s("name").is_empty() { "Character" } else { s("name") })];
    let mut subtitle = [s("race"), s("char_class")].iter().filter(|b| !b.is_empty()).map(|b| b.to_string()).collect::<Vec<_>>().join(" ");
    if n("level").unwrap_or(0) != 0 && native {
        subtitle = format!("{subtitle} — Level {}", n("level").unwrap_or(0)).trim_matches(|c| c == ' ' || c == '—').to_string();
    }
    if !subtitle.is_empty() {
        lines.push(format!("*{subtitle}*"));
    }
    lines.push(String::new());
    for (name, pairs) in pc_export_sections(&db, &pc)? {
        lines.push(format!("## {name}"));
        for (label, value) in pairs {
            lines.push(format!("- **{label}:** {value}"));
        }
        lines.push(String::new());
    }
    for (heading, key) in [("Backstory", "backstory"), ("Notes", "notes")] {
        let text = s(key);
        if text.is_empty() {
            continue;
        }
        lines.push(format!("## {heading}"));
        lines.push(nd_render::text::py_strip(text));
        lines.push(String::new());
    }
    let fname = export_name(pc.get("name").and_then(|v| v.as_str()))?;
    let body = format!("{}\n", nd_render::text::py_strip(&lines.join("\n")));
    let mut r = crate::edits::json_response_raw(&body, "text/markdown; charset=utf-8")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}.md\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}
