//! An entity page's Media tab, the reading half (app/entity_media.py): the pictures an entity keeps (entities.media_images_json), every picture
//! its page shows, the audio / video clips that belong to it, and the state its routes answer with. `None` = not sure: Python's.

use rusqlite::Connection;
use serde_json::{json, Map, Value};

pub const MAX_PICTURES: usize = 100;
const MAX_CAPTION: usize = 200;
const LIBRARY_SHOWN: i64 = 200;
const PICTURES_SHOWN: usize = 2000;

fn md_images(text: &str) -> Vec<&str> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"!\[[^\]]*\]\(([^)\s]+)\)").unwrap());
    if text.is_empty() { vec![] } else { re.captures_iter(text).map(|c| c.get(1).unwrap().as_str()).collect() }
}

/// `new_uid`: ten hex digits
pub fn new_uid() -> String {
    use rand::Rng;
    format!("{:010x}", rand::thread_rng().gen::<u64>() & 0xff_ffff_ffff)
}

/// `_kept_url`: a file of this app, or a web address
fn kept_url(url: &Value) -> bool {
    match url.as_str() {
        Some(u) => !u.is_empty() && u.chars().count() <= 512 && (u.starts_with("/uploads/") || u.starts_with("/static/") || u.starts_with("http://") || u.starts_with("https://")),
        None => false,
    }
}

/// chars of `s` (Python slices by characters), at most `n`
fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `str(v)`: text as it is, an integer as Python prints it; None for None / "" / false / 0 (what `or` falls through), and `None` outer for a
/// value Python would spell its own way (a float, a list, ...)
fn py_str(v: Option<&Value>) -> Option<Option<String>> {
    Some(match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        _ => return None,
    })
}

/// `load_pictures(stored)`: the stored list, cleaned - junk and repeats dropped, every one given a uid, at most MAX_PICTURES.
/// `None` when a uid or caption is a value Python would turn into text its own way.
pub fn load_pictures(stored: Option<&str>) -> Option<Vec<Map<String, Value>>> {
    let Ok(Value::Array(raw)) = serde_json::from_str::<Value>(stored.filter(|s| !s.is_empty()).unwrap_or("[]")) else { return Some(vec![]) };
    let (mut out, mut seen): (Vec<Map<String, Value>>, Vec<String>) = (vec![], vec![]);
    for it in &raw {
        let Some(it) = it.as_object() else { continue };
        let url = match it.get("url").filter(|u| kept_url(u)) {
            Some(Value::String(u)) => u.clone(),
            _ => continue,
        };
        if seen.contains(&url) {
            continue;
        }
        seen.push(url.clone());
        let mut m = Map::new();
        m.insert("uid".into(), json!(take(&py_str(it.get("uid"))?.unwrap_or_else(new_uid), 32)));
        m.insert("url".into(), json!(url));
        m.insert("caption".into(), json!(take(&py_str(it.get("caption"))?.unwrap_or_default(), MAX_CAPTION)));
        m.insert("visible".into(), json!(it.get("visible").map_or(false, |v| crate::edits::py_truthy_pub(Some(v)))));
        out.push(m);
        if out.len() >= MAX_PICTURES {
            break;
        }
    }
    Some(out)
}

/// `store_pictures`: the entity keeps `items`
pub fn store_pictures(db: &Connection, entity_id: i64, items: &[Map<String, Value>]) -> Option<()> {
    crate::orm::update(db, "entities", entity_id, &[("media_images_json", rusqlite::types::Value::Text(nd_render::py_dumps(&Value::Array(items.iter().map(|i| Value::Object(i.clone())).collect()))))])?;
    Some(())
}

/// `picture_dict`
pub fn picture_dict(item: &Map<String, Value>) -> Value {
    json!({"uid": item["uid"], "url": item["url"], "caption": item["caption"], "visible_to_players": item["visible"]})
}

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare_cached(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

/// `attached_audio` / `attached_video`: the clips of `table` that belong to the entity (newest first); a player only the ones marked visible
pub fn attached(db: &Connection, table: &str, entity_id: i64, wid: i64, gm: bool) -> Option<Vec<Map<String, Value>>> {
    let vis = if gm { "" } else { " AND visible_to_players = 1" };
    rows(db, &format!("SELECT * FROM {table} WHERE entity_id = ?1 AND world_id = ?2{vis} ORDER BY created_at DESC, id DESC"), &[&entity_id, &wid])
}

/// `WHERE_LABEL`: how a picture on the page says where it comes from
fn where_label(place: &str) -> &'static str {
    match place {
        "portrait" => "Portrait",
        "body" => "In the text",
        "note" => "In a note",
        _ => "",
    }
}

/// `page_pictures(entity, body, note_texts, is_gm)`: every picture the viewer's page shows, each once - kept pictures, the portrait, the ones
/// written in the body and in the notes (already stripped for this viewer). `None` for a portrait Python would print its own way.
pub fn page_pictures(entity: &Map<String, Value>, body: &str, note_texts: &[String], gm: bool) -> Option<Vec<Map<String, Value>>> {
    let (mut out, mut seen): (Vec<Map<String, Value>>, Vec<String>) = (vec![], vec![]);
    fn add(out: &mut Vec<Map<String, Value>>, seen: &mut Vec<String>, url: &str, caption: &str, item: Option<(&Value, bool)>, place: &str) {
        if url.is_empty() || seen.iter().any(|u| u == url) {
            return;
        }
        seen.push(url.to_string());
        let mut m = Map::new();
        m.insert("uid".into(), item.map(|(uid, _)| uid.clone()).unwrap_or(json!("")));
        m.insert("url".into(), json!(url));
        m.insert("caption".into(), json!(caption));
        m.insert("where".into(), json!(place));
        m.insert("label".into(), json!(where_label(place)));
        m.insert("kept".into(), json!(item.is_some()));
        m.insert("visible".into(), item.map(|(_, v)| json!(v)).unwrap_or(json!(true)));
        out.push(m);
    }
    for item in load_pictures(entity.get("media_images_json").and_then(|v| v.as_str()))?.iter().filter(|i| gm || i["visible"].as_bool().unwrap_or(false)) {
        add(&mut out, &mut seen, item["url"].as_str().unwrap_or(""), item["caption"].as_str().unwrap_or(""), Some((&item["uid"], item["visible"].as_bool().unwrap_or(false))), "kept");
    }
    match entity.get("image_url") {
        Some(Value::String(u)) => add(&mut out, &mut seen, u, "", None, "portrait"),
        Some(Value::Null) | Some(Value::Bool(false)) | None => {}
        _ => return None,
    }
    for u in md_images(body) {
        add(&mut out, &mut seen, u, "", None, "body");
    }
    for text in note_texts {
        for u in md_images(text) {
            add(&mut out, &mut seen, u, "", None, "note");
        }
    }
    Some(out)
}

fn audio_dict(c: &Map<String, Value>) -> Value {
    json!({"id": c["id"], "name": c["name"], "file_url": c["file_url"], "visible_to_players": crate::edits::py_truthy_pub(Some(&c["visible_to_players"])),
           "transcript": c.get("transcript").and_then(|v| v.as_str()).unwrap_or("")})
}

fn video_dict(c: &Map<String, Value>) -> Value {
    json!({"id": c["id"], "name": c["name"], "file_url": c["file_url"], "poster_url": c.get("poster_url").and_then(|v| v.as_str()).unwrap_or(""),
           "visible_to_players": crate::edits::py_truthy_pub(Some(&c["visible_to_players"]))})
}

/// `media_tab`: what the entity page's Media tab is given
pub fn media_tab(db: &Connection, entity: &Map<String, Value>, body: &str, note_texts: &[String], gm: bool) -> Option<Value> {
    let (id, wid) = (entity["id"].as_i64()?, entity["world_id"].as_i64()?);
    Some(json!({"pictures": page_pictures(entity, body, note_texts, gm)?, "audio": attached(db, "audio_clips", id, wid, gm)?, "video": attached(db, "video_clips", id, wid, gm)?}))
}

/// `_state(db, request, world, entity, libraries)`: what is on the tab (everything for a GM, what players may see for an assistant), and with
/// `libraries` what the picker may add from this world
pub fn state(db: &Connection, entity: &Map<String, Value>, gm: bool, libraries: bool) -> Option<Value> {
    let (id, wid) = (entity["id"].as_i64()?, entity["world_id"].as_i64()?);
    let kept = load_pictures(entity.get("media_images_json").and_then(|v| v.as_str()))?;
    let pictures: Vec<Value> = kept.iter().filter(|i| gm || i["visible"].as_bool().unwrap_or(false)).map(|i| picture_dict(i)).collect();
    let audio = attached(db, "audio_clips", id, wid, gm)?;
    let video = attached(db, "video_clips", id, wid, gm)?;
    let mut out = Map::new();
    out.insert("pictures".into(), Value::Array(pictures));
    out.insert("audio".into(), Value::Array(audio.iter().map(|c| audio_dict(c)).collect()));
    out.insert("video".into(), Value::Array(video.iter().map(|c| video_dict(c)).collect()));
    out.insert("max_pictures".into(), json!(MAX_PICTURES));
    if !libraries {
        return Some(Value::Object(out));
    }
    let vis = if gm { "" } else { " AND visible_to_players = 1" };
    let lib = |table: &str, here: &[Map<String, Value>]| -> Option<Vec<Map<String, Value>>> {
        let rows = rows(db, &format!("SELECT * FROM {table} WHERE world_id = ?1{vis} ORDER BY created_at DESC, id DESC LIMIT {LIBRARY_SHOWN}"), &[&wid])?;
        let ids: Vec<i64> = here.iter().filter_map(|c| c["id"].as_i64()).collect();
        Some(rows.into_iter().filter(|c| !c["id"].as_i64().is_some_and(|i| ids.contains(&i))).collect())
    };
    let here_audio = attached(db, "audio_clips", id, wid, true)?;
    let here_video = attached(db, "video_clips", id, wid, true)?;
    let kept_urls: Vec<&Value> = kept.iter().map(|i| &i["url"]).collect();
    let mut lib_pictures = vec![];
    for e in crate::gallery::all_world_image_urls(db, wid)? {
        if kept_urls.iter().all(|u| **u != e["url"]) {
            lib_pictures.push(json!({"url": e["url"], "name": e["name"]}));
        }
    }
    lib_pictures.truncate(PICTURES_SHOWN);
    let clip = |c: &Map<String, Value>, poster: bool| -> Value {
        let mut m = Map::new();
        m.insert("id".into(), c["id"].clone());
        m.insert("name".into(), c["name"].clone());
        m.insert("visible_to_players".into(), json!(crate::edits::py_truthy_pub(Some(&c["visible_to_players"]))));
        m.insert("file_url".into(), c["file_url"].clone());
        if poster {
            m.insert("poster_url".into(), json!(c.get("poster_url").and_then(|v| v.as_str()).unwrap_or("")));
        }
        m.insert("entity_id".into(), c.get("entity_id").cloned().unwrap_or(Value::Null));
        Value::Object(m)
    };
    out.insert("library".into(), json!({"pictures": lib_pictures,
        "audio": lib("audio_clips", &here_audio)?.iter().map(|c| clip(c, false)).collect::<Vec<_>>(),
        "video": lib("video_clips", &here_video)?.iter().map(|c| clip(c, true)).collect::<Vec<_>>()}));
    Some(Value::Object(out))
}
