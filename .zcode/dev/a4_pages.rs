
/// `_templates_for_world`: the world's sheet templates plus the global ones, built-ins first, then by name
fn sheet_templates_for(db: &rusqlite::Connection, wid: Option<i64>) -> Option<Vec<Map<String, Value>>> {
    let sql = "SELECT * FROM sheet_templates WHERE world_id IS NULL OR world_id = ?1 ORDER BY is_builtin DESC, name";
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map([wid.unwrap_or(-1)], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

/// `character_limit`: World.max_characters_per_player clamped to at least 1
fn character_limit(world: &Map<String, Value>) -> i64 {
    world.get("max_characters_per_player").and_then(|v| v.as_i64()).map_or(1, |n| n.max(1))
}

/// `GET /characters/new` (app/routers/characters.py `character_new_form`): the creation wizard, or a custom sheet's own
/// form when the chosen template is one; a player at their limit is sent to their (single) character or the list
pub fn characters_new(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    let world = world.as_ref()?;
    if !p.is_gm {
        let mut st = db.prepare("SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
        let mine: Vec<i64> = st.query_map([world.get("id")?.as_i64()?, p.user_id], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
        drop(st);
        let limit = character_limit(world);
        if mine.len() as i64 >= limit {
            return Some(crate::edits::redirect(if mine.len() == 1 { &format!("/characters/{}", mine[0]) } else { "/characters" })?);
        }
    }
    // the pre-selected template (?template_id=), else the N&D default; a template_id Python's int() would refuse is Python's
    let chosen = match crate::pages::query_param(uri, "template_id") {
        Some(t) if !t.is_empty() => {
            if !t.bytes().all(|b| b.is_ascii_digit()) || t.len() > 15 {
                return None;
            }
            sheet_tpl_by(&db, "id", &t)?
        }
        _ => sheet_tpl_by(&db, "slug", "nd-default")?,
    };
    if chosen.as_ref().and_then(|t| t.get("sheet_mode").and_then(|v| v.as_str())) == Some("custom") {
        let tpl = chosen?;
        let t = crate::sheets::Tpl::load(tpl.clone())?;
        let tpl_fields = t.enrich_fields()?;
        let grouped: Vec<Value> = crate::apis::group_by_section(&tpl_fields)?
            .into_iter().map(|(name, fields)| json!([name, fields])).collect();
        let names: Vec<String> = grouped.iter().filter_map(|g| g.get(0).and_then(|n| n.as_str()).map(str::to_string)).collect();
        let sheet_pages = t.sheet_pages(&names)?;
        let c = &mut p.ctx;
        c.insert("pc".into(), Value::Null);
        c.insert("can_manage".into(), json!(true));
        c.insert("chosen_template".into(), Value::Object(tpl));
        c.insert("sections".into(), Value::Array(grouped));
        c.insert("sheet_pages".into(), sheet_pages);
        c.insert("tpl_fields".into(), Value::Array(tpl_fields));
        c.insert("custom_fields".into(), json!({}));
        return render_with(pages, "characters/custom_sheet.html", p.ctx, headers);
    }
    let c = &mut p.ctx;
    c.insert("chosen_template".into(), chosen.map(Value::Object).unwrap_or(Value::Null));
    render_with(pages, "characters/wizard.html", p.ctx, headers)
}

/// the sheet template by column ("id" with digits, or "slug")
pub(crate) fn sheet_tpl_by(db: &rusqlite::Connection, col: &str, val: &str) -> Option<Option<Map<String, Value>>> {
    Some(db.query_row(&format!("SELECT * FROM sheet_templates WHERE {col} = ?1"), [val], |r| crate::layout::row_map(r)).ok())
}

/// the shared context of characters/form.html (the edit form; the wizard posts elsewhere)
pub(crate) fn character_form_ctx(db: &rusqlite::Connection, p: &mut PageCtx, pc: &Map<String, Value>, chosen_tpl: Option<Map<String, Value>>) -> Option<Map<String, Value>> {
    let list = |k: &str| -> Option<Value> { crate::apis::py_json_strict(pc.get(k).and_then(|v| v.as_str()), "[]") };
    let custom_fields = crate::apis::py_json_strict(pc.get("custom_fields_json").and_then(|v| v.as_str()), "{}")?;
    if !custom_fields.is_object() {
        return None; // json.loads gives a list: Python's page would render it as one - not ported
    }
    let (nd_stats, nd_currency) = crate::apis::nd_defaults();
    let tpl_fields = match &chosen_tpl {
        Some(t) => crate::sheets::Tpl::load(t.clone())?.enrich_fields()?,
        None => vec![],
    };
    let templates = sheet_templates_for(db, p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()))?;
    let c = &mut p.ctx;
    c.insert("pc".into(), Value::Object(pc.clone()));
    c.insert("nd_default_stats".into(), nd_stats);
    c.insert("nd_default_currency".into(), nd_currency);
    c.insert("stats".into(), list("stats_json")?);
    c.insert("currency".into(), list("currency_json")?);
    c.insert("equipment".into(), list("equipment_json")?);
    c.insert("feats".into(), list("feats_json")?);
    c.insert("cyberware".into(), list("cyberware_json")?);
    c.insert("sheet_templates".into(), Value::Array(templates.into_iter().map(Value::Object).collect()));
    c.insert("chosen_template".into(), chosen_tpl.map(Value::Object).unwrap_or(Value::Null));
    c.insert("tpl_fields".into(), Value::Array(tpl_fields));
    c.insert("custom_fields".into(), custom_fields);
    Some(p.ctx.clone())
}
