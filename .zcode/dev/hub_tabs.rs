/// `created_at.strftime("%Y-%m-%dT%H:%M:%SZ")`, or null
fn strftime_z(v: Option<&Value>) -> Value {
    match v.and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
        Some(dt) => json!(dt.strftime("%Y-%m-%dT%H:%M:%SZ").ok()?),
        None => Value::Null,
    }
}


/// `hub_handouts` (app/routers/character_hub.py): the pictures the GM showed the table, newest first, `new` past the owner's
/// last look (nothing is marked seen here)
fn handouts_tab(db: &Connection, pc: &Map<String, Value>, is_owner: bool) -> Option<Value> {
    let pid = int(pc, "id")?;
    let wid = int(pc, "world_id")?;
    let seen = crate::edits::pc_prefs(db, pid)?.get("handouts_seen").and_then(|v| v.as_i64()).unwrap_or(0);
    let hs = rows(db, "SELECT * FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 40", &[&wid])?;
    let mut out = vec![];
    for h in &hs {
        let at = match h.get("created_at").and_then(|v| v.get("__dt__").or(Some(v))).and_then(|v| v.as_str()).and_then(nd_render::pyvalue::PyDateTime::parse) {
            Some(dt) => json!(dt.strftime("%Y-%m-%dT%H:%M:%SZ").ok()?),
            None => Value::Null,
        };
        out.push(json!({"id": h.get("id"), "url": h.get("url"),
            "label": h.get("label").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("")),
            "new": int(h, "id").map_or(false, |i| i > seen), "at": at}));
    }
    Some(json!({"can_edit": is_owner, "handouts": out}))
}

/// `hub_places` (app/routers/character_hub.py): every page the world opens to a player, as the navigation bar evaluates it for
/// the PLAYER role, grouped; `pins` are the pages the character pinned (still open to players, in the pinned order)
fn places_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>) -> Option<Value> {
    let place_rows = crate::edits::hub_places(world)?;
    let mut tools = vec![];
    let mut kinds = vec![];
    for (g, r) in &place_rows {
        match *g {
            "kinds" => kinds.push(r.clone()),
            _ => tools.push(r.clone()),
        }
    }
    let mut groups = vec![];
    if !tools.is_empty() {
        groups.push(json!({"label": "Tools & logs", "items": tools}));
    }
    if !kinds.is_empty() {
        groups.push(json!({"label": "The world", "items": kinds}));
    }
    let pid = int(pc, "id")?;
    let pins_raw = crate::edits::pc_prefs(db, pid)?.get("pins").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let by_id: Vec<String> = place_rows.iter().filter_map(|(_, r)| r.get("id").and_then(|v| v.as_str()).map(str::to_string)).collect();
    let pins: Vec<Value> = pins_raw.iter()
        .filter(|i| i.as_str().map_or(false, |s| by_id.iter().any(|b| b == s)))
        .cloned().collect();
    Some(json!({"groups": groups, "pins": pins}))
}

/// `hub_keepsakes` (app/routers/character_hub.py): what the character has kept, newest first, the per-kind personal-file
/// limits, and everything the viewer could still keep (a player's view of the world, even for a GM looking in)
fn keepsakes_tab(db: &Connection, pc: &Map<String, Value>, world: &Map<String, Value>, uid: i64, is_gm: bool, is_owner: bool) -> Option<Value> {
    let pid = int(pc, "id")?;
    let wid = int(pc, "world_id")?;
    let ks = rows(db, "SELECT * FROM character_keepsakes WHERE character_id = ?1 ORDER BY id DESC", &[&pid])?;
    let mut keepsakes = vec![];
    let mut counts: Map<String, Value> = Map::new();
    for kind in ["image", "audio", "video"] {
        counts.insert(kind.into(), json!(0));
    }
    for k in &ks {
        keepsakes.push(json!({"id": k.get("id"), "kind": k.get("kind"),
            "url": k.get("url").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("")),
            "text": k.get("text").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("")),
            "name": k.get("name"),
            "description": k.get("description").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("")),
            "by": k.get("saved_by"), "own": crate::edits::py_truthy_pub(k.get("own")), "at": strftime_z(k.get("created_at"))}));
        if crate::edits::py_truthy_pub(k.get("own")) {
            if let Some(kind) = k.get("kind").and_then(|v| v.as_str()) {
                if let Some(c) = counts.get_mut(kind) {
                    *c = json!(c.as_i64().unwrap_or(0) + 1);
                }
            }
        }
    }
    // personal_files.limit_for: the world's stored limit (clamped 0..HARD_MAX), else the default
    let limit = |kind: &str| -> i64 {
        let (col, dflt) = match kind {
            "image" => ("personal_images_max", 30),
            "audio" => ("personal_audio_limit", 10),
            _ => ("personal_video_max", 5),
        };
        let stored = world.get(col).and_then(|v| v.as_i64()).unwrap_or(-1);
        if stored < 0 { dflt } else { stored.min(500) }
    };
    let mut own_limits = Map::new();
    for kind in ["image", "audio", "video"] {
        own_limits.insert(kind.into(), json!({"used": counts.get(kind).cloned().unwrap_or(json!(0)), "max": limit(kind)}));
    }
    // _saveable: handouts, the owner's private notes from someone else, and the session log's player-visible media
    let handouts: Vec<Value> = rows(db, "SELECT * FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 40", &[&wid])?
        .iter().map(|h| json!({"id": h.get("id"), "kind": "image", "url": h.get("url"),
            "name": h.get("label").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("Handout"))})).collect();
    let mut notes = vec![];
    if is_owner {
        for n in rows(db, "SELECT * FROM private_notes WHERE world_id = ?1 AND player_user_id = ?2 AND author_id != ?2 ORDER BY id DESC LIMIT 30", &[&wid, &uid])? {
            let content = n.get("content").and_then(|v| v.as_str()).unwrap_or("");
            let title = n.get("title").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("Note"));
            let stripped = nd_perm::gmonly::strip_gm_only(content, Some(uid), nd_render::now_minute())?;
            notes.push(json!({"id": n.get("id"), "kind": "text", "name": title,
                "text": stripped.chars().take(600).collect::<String>()}));
        }
    }
    let mut session_items: Vec<Value> = vec![];
    if nd_perm::sections::section_level(false, Some("player"), world.get("section_access_json").and_then(|v| v.as_str()), world.get("custom_kinds_json").and_then(|v| v.as_str()), "session_log")? != "none" {
        for gs in rows(db, "SELECT * FROM game_sessions WHERE world_id = ?1 ORDER BY session_num DESC LIMIT 15", &[&wid])? {
            let m = crate::edits::viewer_media(db, &gs, false)?;
            let label = match gs.get("title").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                Some(t) => format!("#{} {t}", int(gs, "session_num")?),
                None => format!("#{} Session", int(gs, "session_num")?),
            };
            for i in m["images"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "image", "ref": i.get("uid"),
                    "url": i.get("url"), "name": i.get("caption").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(Value::String).unwrap_or_else(|| json!("Picture"))}));
            }
            for v in m["videos"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "video", "ref": v.get("id").map(|x| x.to_string()),
                    "url": v.get("file_url"), "name": v.get("name")}));
            }
            for c in m["audio"].as_array()? {
                session_items.push(json!({"session_id": gs.get("id"), "session": label, "kind": "audio", "ref": c.get("id").map(|x| x.to_string()),
                    "url": c.get("file_url"), "name": c.get("name")}));
            }
        }
    }
    session_items.truncate(60);
    Some(json!({"can_edit": true, "is_gm": is_gm, "is_owner": is_owner, "keepsakes": keepsakes,
        "own_limits": own_limits, "saveable": {"handouts": handouts, "notes": notes, "session": session_items}}))
}
