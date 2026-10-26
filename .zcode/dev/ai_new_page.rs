
/// `GET /characters/ai-new` (app/routers/character_ai.py `pc_ai_page`): the AI character creator page - the world's custom
/// systems, the pre-selected template id (a query value FastAPI would 422 on stays Python's), the player's display name;
/// a player at their character limit redirected as on /characters/new
pub fn characters_ai_new(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone();
    let world = world.as_ref()?;
    // template_id: int = 0 - FastAPI's coercion: a plain int (or a value it 422s on: Python's, forwarded)
    let selected = match crate::pages::query_param(uri, "template_id") {
        Some(t) if !t.is_empty() => {
            if !t.bytes().all(|b| b.is_ascii_digit()) || t.len() > 15 || t.parse::<i64>().is_err() {
                return None;
            }
            t.parse::<i64>().ok()?
        }
        _ => 0,
    };
    let user: Option<Map<String, Value>> = db.query_row("SELECT * FROM users WHERE id = ?1", [p.user_id], |r| crate::layout::row_map(r)).ok();
    if let Some(u) = &user {
        if !truthy_pub(u.get("is_gm")) {
            let wid = world.get("id")?.as_i64()?;
            let mut st = db.prepare("SELECT id FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
            let mine: Vec<i64> = st.query_map([wid, p.user_id], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
            drop(st);
            let limit = character_limit(world);
            if mine.len() as i64 >= limit {
                let to = if mine.len() == 1 { format!("/characters/{}", mine[0]) } else { "/characters".to_string() };
                return Some(crate::edits::redirect(&to)?);
            }
        }
    }
    let wid = world.get("id")?.as_i64()?;
    let mut st = db.prepare("SELECT * FROM sheet_templates WHERE sheet_mode = 'custom' AND (world_id IS NULL OR world_id = ?1) ORDER BY name").ok()?;
    let systems: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let display_name = user.as_ref()
        .and_then(|u| u.get("display_name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()))
        .unwrap_or("").to_string();
    let c = &mut p.ctx;
    c.insert("systems".into(), Value::Array(systems.into_iter().map(Value::Object).collect()));
    c.insert("selected_template_id".into(), json!(selected));
    c.insert("display_name".into(), json!(display_name));
    render_with(pages, "characters/ai_new.html", p.ctx, headers)
}
