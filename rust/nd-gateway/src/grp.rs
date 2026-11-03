//! Player groups, the table layer, for the gateway: the same rule as app/scope.py. A world has a shared setting and, on top, each player group's own table
//! (sessions, quests, facts, parties, characters, calendar events, combats, boards, handouts, schematics, dice rolls). `group_id` on each of those rows:
//! NULL = shared, a group id = that group's, 0 = hidden from every group.
//!
//! One place decides what a request sees: `install` (called by `layout::build`, which every signed-in page and API goes through) shadows each of those tables with a
//! TEMP VIEW of the same name that shows only the shared rows and the viewer's group's. Every unqualified `FROM quests` in the gateway then reads the limited view
//! without knowing about groups; writes are qualified (`main.quests`), so they reach the real table (a view is not writable), after the handler read the row through
//! the view. `Pooled`'s drop calls `clear`, so a connection never lends one request's views to the next.
use rusqlite::Connection;
use serde_json::{json, Value};

pub const SCOPED: [&str; 11] = ["game_sessions", "quests", "facts", "parties", "player_characters", "calendar_events", "combat_sessions", "invest_boards", "world_handouts", "schematics", "dice_rolls"];
pub const ACTIVE_GROUP_COOKIE: &str = "active_group";
/// a GM's "Playing: <group>" never narrows these: they must see every group's rows
pub const GM_UNSCOPED_PREFIXES: [&str; 10] = ["/groups", "/export", "/api/export", "/api/backups", "/import", "/admin", "/worlds", "/settings", "/tools", "/api/worlds"];

pub fn is_scoped(table: &str) -> bool {
    SCOPED.contains(&table)
}

/// Drop the views / scope row of the last request. False = the connection cannot be reused.
pub fn clear(db: &Connection) -> bool {
    let names: Vec<(String, String)> = match db.prepare_cached("SELECT type, name FROM sqlite_temp_master WHERE type IN ('view','table')") {
        Ok(mut st) => match st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).and_then(|rows| rows.collect::<Result<Vec<_>, _>>()) {
            Ok(v) => v,
            Err(_) => return false,
        },
        Err(_) => return false,
    };
    for (kind, name) in names {
        if db.execute_batch(&format!("DROP {} IF EXISTS temp.\"{}\"", if kind == "view" { "VIEW" } else { "TABLE" }, name.replace('"', ""))).is_err() {
            return false;
        }
    }
    true
}

/// The group a new row of this request joins (a GM who chose "Playing: <group>", or a player in a group) - None = shared. `world`: the world the row belongs to.
pub fn new_row_group(db: &Connection, world: Option<i64>) -> Option<i64> {
    let (gid, wid): (Option<i64>, Option<i64>) = db.query_row("SELECT gid, world_id FROM temp.nd_scope LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    if let (Some(w), Some(sw)) = (world, wid) {
        if w != sw {
            return None;
        }
    }
    gid
}

/// Set up the group limit for this request. Returns the top bar's "Playing: <group>" chooser (GM / assistant of a world with groups) as the template's
/// `request.state.group_switch`, or Null. None = the database could not be prepared (the request goes to Python).
pub fn install(db: &Connection, user_id: i64, is_gm: bool, is_assistant: bool, world_id: Option<i64>, cookie_value: Option<&str>, path: &str) -> Option<Value> {
    if !clear(db) {
        return None;
    }
    let world_id = match world_id {
        Some(w) => w,
        None => return Some(Value::Null),
    };
    let (filtered, gid): (bool, Option<i64>);
    if !is_gm && !is_assistant {
        let g: Option<Option<i64>> = db.query_row("SELECT group_id FROM world_memberships WHERE world_id = ?1 AND user_id = ?2 LIMIT 1", [world_id, user_id], |r| r.get(0)).ok();
        (filtered, gid) = (true, g.flatten());
    } else {
        let wanted = cookie_value.filter(|c| !c.is_empty() && c.bytes().all(|b| b.is_ascii_digit())).and_then(|c| c.parse::<i64>().ok());
        match wanted {
            None => (filtered, gid) = (false, None),
            Some(g) if GM_UNSCOPED_PREFIXES.iter().any(|p| path.starts_with(p)) => (filtered, gid) = (false, Some(g)),
            Some(g) => {
                let ok: Option<i64> = db.query_row("SELECT world_id FROM player_groups WHERE id = ?1", [g], |r| r.get(0)).ok();
                if ok == Some(world_id) {
                    (filtered, gid) = (true, Some(g));
                } else {
                    (filtered, gid) = (false, None);
                }
            }
        }
    }
    let mut script = String::new();
    if filtered {
        let cond = match gid {
            None => "group_id IS NULL".to_string(),
            Some(g) => format!("group_id IS NULL OR group_id = {g}"),
        };
        for t in SCOPED {
            script.push_str(&format!("CREATE TEMP VIEW {t} AS SELECT * FROM main.{t} WHERE {cond};\n"));
        }
    }
    if filtered || gid.is_some() {
        script.push_str(&format!("CREATE TEMP TABLE nd_scope (gid INTEGER, world_id INTEGER, filtered INTEGER);\nINSERT INTO nd_scope VALUES ({}, {world_id}, {});\n",
            gid.map_or("NULL".to_string(), |g| g.to_string()), filtered as i64));
    }
    if !script.is_empty() {
        db.execute_batch(&script).ok()?;
    }
    if !(is_gm || is_assistant) {
        return Some(Value::Null);
    }
    let mut st = db.prepare_cached("SELECT id, name FROM main.player_groups WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let choices: Vec<Value> = st.query_map([world_id], |r| Ok(json!({"id": r.get::<_, i64>(0)?, "name": r.get::<_, String>(1)?}))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(if choices.is_empty() { Value::Null } else { json!({"choices": choices, "active": gid}) })
}

/// groups.TABLE_LABELS
pub fn label(table: &str) -> &'static str {
    match table {
        "game_sessions" => "Sessions",
        "quests" => "Quests",
        "facts" => "Facts",
        "parties" => "Parties",
        "player_characters" => "Characters",
        "calendar_events" => "Calendar events",
        "combat_sessions" => "Combats",
        "invest_boards" => "Boards",
        "world_handouts" => "Handouts",
        "schematics" => "Schematics",
        _ => "Dice rolls",
    }
}

/// Maps are JSON files: a map's optional `group_id` key means what the table rows' does. False = this request must not see that map (it is as good as missing).
pub fn map_visible(db: &Connection, data: &serde_json::Map<String, Value>) -> bool {
    let Ok((gid, filtered)) = db.query_row("SELECT gid, filtered FROM temp.nd_scope LIMIT 1", [], |r| Ok((r.get::<_, Option<i64>>(0)?, r.get::<_, i64>(1)?))) else { return true };
    if filtered == 0 {
        return true;
    }
    match data.get("group_id") {
        None | Some(Value::Null) => true,
        Some(v) => gid.is_some() && v.as_i64() == gid,
    }
}

/// The group a map made now joins
pub fn new_map_group(db: &Connection) -> Option<i64> {
    new_row_group(db, None)
}
