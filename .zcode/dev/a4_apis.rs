
/// `json.loads(text or default)` where the column parses: None on a parse error too (Python's handler has no `except`
/// there - its 500 is the answer) and on text the two readers disagree on
fn py_json_strict(text: Option<&str>, default: &str) -> Option<Value> {
    match text.filter(|s| !s.is_empty()) {
        Some(t) => {
            if t.contains("NaN") || t.contains("Infinity") || !crate::edits::plain_numbers(t.as_bytes()) {
                return None;
            }
            serde_json::from_str(t).ok()
        }
        None => serde_json::from_str(default).ok(),
    }
}

/// `_featured_entity_candidates` (app/routers/sessions.py): the "Entities Featured" picker's candidates - the world's
/// entities by the picker's groups (folder, name order), then the real PlayerCharacter sheets as "pc:<id>"
fn featured_entity_candidates(db: &rusqlite::Connection, wid: i64) -> Option<Vec<Value>> {
    const GROUPS: &[(&str, &str, bool)] = &[
        ("character", "\u{1f464} NPCs", false),
        ("character", "\u{1f9d1} Player Characters", true),
        ("creature", "\u{2620} Creatures", false),
        ("location", "\u{1f5fa} Locations", false),
        ("organization", "\u{1f3e2} Organizations", false),
        ("race", "\u{1f9ec} Races", false),
        ("profession", "\u{1f3ad} Professions", false),
        ("note", "\u{1f4c4} Notes", false),
    ];
    let mut candidates = vec![];
    for (kind, label, pcs_only) in GROUPS {
        let filter = if *pcs_only { " AND subtype = 'PC'" } else { " AND (subtype != 'PC' OR subtype IS NULL)" };
        let sql = format!("SELECT id, name, folder FROM entities WHERE world_id = ?1 AND kind = '{kind}'{filter} ORDER BY folder, name");
        let mut st = db.prepare(&sql).ok()?;
        let r = st.query_map([wid], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<String>>(2)?))
        }).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        drop(st);
        for (id, name, folder) in r {
            let name = name.unwrap_or_default();
            let full = match folder.filter(|f| !f.is_empty()) {
                Some(f) => format!("{label}/{f}"),
                None => label.to_string(),
            };
            candidates.push(json!({"id": id, "name": name, "folder": full}));
        }
    }
    let mut st = db.prepare("SELECT id, name FROM player_characters WHERE world_id = ?1 ORDER BY name").ok()?;
    let pcs = st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    drop(st);
    for (id, name) in pcs {
        candidates.push(json!({"id": format!("pc:{id}"), "name": name.unwrap_or_default(), "folder": "\u{1f9d1} Player Characters"}));
    }
    Some(candidates)
}

/// `GET /sessions/<id>/npc-candidates` (app/routers/sessions.py `session_npc_candidates`): the picker's candidates for a
/// session of the active world
fn session_npc_candidates(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let gs = db.query_row("SELECT world_id FROM game_sessions WHERE id = ?1", [id], |r| r.get::<_, i64>(0)).ok();
    if gs.map_or(true, |w| w != wid) {
        return Some(crate::idpages::refusal(404));
    }
    crate::edits::json_response(&json!({"candidates": featured_entity_candidates(&db, wid)?}))
}

/// The Neon & Dragons creation defaults (app/constants.py), as the form templates read them
fn nd_defaults() -> (Value, Value) {
    let stats = json!([
        {"id": "str", "label": "Strength", "abbr": "STR", "value": 3},
        {"id": "dex", "label": "Dexterity", "abbr": "DEX", "value": 3},
        {"id": "bod", "label": "Body", "abbr": "BOD", "value": 3},
        {"id": "per", "label": "Perception", "abbr": "PER", "value": 3},
        {"id": "wil", "label": "Willpower", "abbr": "WIL", "value": 2},
        {"id": "int", "label": "Intellect", "abbr": "INT", "value": 2},
        {"id": "cha", "label": "Charisma", "abbr": "CHA", "value": 2},
        {"id": "itu", "label": "Intuition", "abbr": "ITU", "value": 2},
    ]);
    let currency = json!([{"label": "Creds", "abbr": "CR", "value": 0}, {"label": "Tokens", "abbr": "TK", "value": 0}]);
    (stats, currency)
}
