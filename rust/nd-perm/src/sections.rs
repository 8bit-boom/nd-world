//! app/deps.py: the per-world section matrix (`world_section_access`, `world_section_level`, `world_can_edit_row`) and the custom kinds it
//! depends on (`load_custom_kinds`).
use serde_json::Value;

pub const SECTION_PERMISSION_IDS: &[&str] = &[
    "maps", "calendar", "quests", "parties", "tables", "boards", "combat", "races", "professions", "sessions", "facts", "images", "import",
    "bulk_edit", "background_jobs", "export", "dreamlands", "king-in-yellow", "ai", "imagestudio", "editor", "code_assist", "chronicler", "dice",
    "session_log", "rules", "characters", "audio", "video", "pages", "androidapp",
];
pub const KINDS: &[&str] = &["character", "location", "organization", "creature", "event", "item", "feat", "note", "race", "profession"];
const NO_PLAYER_EDIT: &[&str] = &[
    "maps", "boards", "races", "professions", "sessions", "facts", "images", "pages", "audio", "video", "background_jobs", "import", "bulk_edit",
    "combat", "export", "dreamlands", "king-in-yellow", "ai", "imagestudio", "editor", "code_assist", "chronicler", "dice", "session_log", "rules",
    "characters", "androidapp",
];
const NO_ASSISTANT_EDIT: &[&str] = &[
    "combat", "export", "dreamlands", "king-in-yellow", "ai", "imagestudio", "editor", "code_assist", "chronicler", "dice", "session_log", "rules",
    "characters", "androidapp",
];
const PLAYER_READ_DEFAULT: &[&str] = &["maps", "races", "professions", "chronicler", "dice", "session_log", "rules", "characters", "audio", "video", "pages", "androidapp"];
const ASSISTANT_EDIT_DEFAULT: &[&str] = &[
    "maps", "calendar", "quests", "parties", "tables", "boards", "races", "professions", "sessions", "facts", "images", "pages", "audio", "video",
    "background_jobs", "import", "bulk_edit",
];

/// Python's `json.loads`, as far as serde agrees with it. `Err(())` = Python would parse something serde refuses (NaN, Infinity, a lone
/// surrogate): the caller cannot be sure, so it must defer.
fn py_json(raw: Option<&str>) -> Result<Value, ()> {
    let Some(raw) = raw.filter(|s| !s.is_empty()) else { return Ok(Value::Null) };
    match serde_json::from_str(raw) {
        Ok(v) => Ok(v),
        Err(_) if raw.contains("NaN") || raw.contains("Infinity") || raw.contains("\\u") => Err(()),
        Err(_) => Ok(Value::Null), // Python fails too (ValueError) -> treated as {} / []
    }
}

/// Python's `str.strip()` emptiness for a label; `None` when the text has whitespace Rust and Python disagree about.
fn py_blank(s: &str) -> Option<bool> {
    if s.chars().any(|c| ('\u{1c}'..='\u{1f}').contains(&c) || c == '\u{85}') {
        return None;
    }
    Some(s.trim().is_empty())
}

/// `effective_kinds(world)[0]`: the built-in kinds plus the world's valid custom kinds, in stored order.
pub fn effective_kinds(custom_kinds_json: Option<&str>) -> Option<Vec<String>> {
    let mut out: Vec<String> = KINDS.iter().map(|s| s.to_string()).collect();
    if let Value::Array(entries) = py_json(custom_kinds_json).ok()? {
        for e in entries {
            let Value::Object(e) = e else { continue };
            let Some(Value::String(id)) = e.get("id") else { continue };
            if !id.starts_with("custom_") {
                continue;
            }
            let Some(Value::String(label)) = e.get("label") else { continue };
            if py_blank(label)? {
                continue;
            }
            out.push(id.clone());
        }
    }
    Some(out)
}

pub fn default_levels(section_id: &str) -> (&'static str, &'static str) {
    if section_id.starts_with("kind_") {
        return ("read", "edit");
    }
    let player = if PLAYER_READ_DEFAULT.contains(&section_id) { "read" } else { "none" };
    let assistant = if ASSISTANT_EDIT_DEFAULT.contains(&section_id) {
        "edit"
    } else if PLAYER_READ_DEFAULT.contains(&section_id) {
        "read"
    } else {
        "none"
    };
    (player, assistant)
}

pub fn floor(section_id: &str, role: &str, val: &'static str) -> &'static str {
    if val != "edit" {
        return val;
    }
    if (role == "player" && NO_PLAYER_EDIT.contains(&section_id)) || (role == "assistant" && NO_ASSISTANT_EDIT.contains(&section_id)) {
        return "read";
    }
    val
}

/// `world_section_level`: "none" / "read" / "edit" for a viewer (`role` = None for a GM) in a world with these two JSON columns.
pub fn section_level(is_gm: bool, role: Option<&str>, section_access_json: Option<&str>, custom_kinds_json: Option<&str>, section_id: &str) -> Option<&'static str> {
    if is_gm {
        return Some("edit");
    }
    let role = match role {
        Some(r @ ("player" | "assistant")) => r,
        Some(_) => return None,
        None => return Some("none"),
    };
    let (dp, da) = default_levels(section_id);
    let default = if role == "player" { dp } else { da };
    // world_section_access only has entries for the world's known ids; anything else falls back to the default (unfloored)
    let known = SECTION_PERMISSION_IDS.contains(&section_id)
        || section_id.strip_prefix("kind_").map_or(Ok(false), |k| effective_kinds(custom_kinds_json).map(|ks| ks.iter().any(|x| x == k)).ok_or(()))
            .ok()?;
    if !known {
        return Some(default);
    }
    let data = py_json(section_access_json).ok()?;
    let val = match data.get(section_id).filter(|e| e.is_object()).and_then(|e| e.get(role)) {
        Some(Value::String(s)) if s == "none" => "none",
        Some(Value::String(s)) if s == "read" => "read",
        Some(Value::String(s)) if s == "edit" => "edit",
        _ => default,
    };
    Some(floor(section_id, role, val))
}

/// `world_can_edit_row`: edit level for the section, then any row for a GM or assistant, only their own rows for a player.
pub fn can_edit_row(level: &str, is_gm: bool, is_assistant: bool, user_id: Option<i64>, created_by: Option<i64>) -> bool {
    if level != "edit" {
        return false;
    }
    if is_gm || is_assistant {
        return true;
    }
    matches!((user_id, created_by), (Some(u), Some(c)) if u == c)
}
