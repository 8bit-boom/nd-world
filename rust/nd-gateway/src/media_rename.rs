//! The AI media renamer (a port of app/media_rename.py's proposal side and app/media_rename_jobs.py): what is known about each audio
//! clip, video clip and image (names, file names, the tags inside the file, transcripts, where an image is used, optionally the picture)
//! becomes a prompt; the model's names come back cleaned. `POST /api/media-rename/suggest` answers a handful of items at once; `POST
//! /api/media-rename/runs` starts a durable run over hundreds, saved after every chunk, which this process works through (the AI slot
//! taken per chunk), owns while it runs and resumes after a restart. Applying, undoing and the listing routes stay Python's.

use axum::body::Body;
use axum::http::{Response, StatusCode};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

use crate::ai::{backend, py_repr, Backend};
use crate::ai_more::{crash, detail, failed, Fut};
use crate::config::Config;
use crate::layout::PageCtx;

const MAX_NAME: usize = 80;
const TEXT_BATCH: usize = 8;
const PICTURE_MAX_SIDE: u32 = 768;
const LISTEN_MAX_BYTES: u64 = 24 * 1024 * 1024;
const LISTEN_MAX_SECONDS: f64 = 600.0;
const MAX_SUGGEST_ITEMS: usize = 12;
const MAX_RUN_ITEMS: usize = 2000;
const CHUNK: usize = 6;
const CHUNK_WITH_PICTURES: usize = 3;
pub const RUN_INTERRUPTED_NOTE: &str = "Paused by a server restart - it picks up from the last saved chunk.";

const STYLE_PRESETS: [(&str, &str); 5] = [
    ("descriptive", "Plain, descriptive names: say what the thing is or shows, in Title Case."),
    ("short", "Keep names very short: two to four words."),
    ("lore", "Names that read as part of the setting - use the world's own places, people and tone when the clues support it."),
    ("music", "For music use 'Artist - Title' when both are known, otherwise just the title."),
    ("sfx", "For ambience and sound effects name what the sound IS and where it fits, e.g. 'Tavern Crowd Murmur', 'Door Creak'."),
];

const SYSTEM: &str = "You rename media files in a tabletop-RPG game master's library so they can be found again. For each item you get what is known about it: its current name, its file name, tags read from inside the file, lyrics or a transcript excerpt, where it is used, and sometimes a picture.\nPropose ONE better name per item: specific, short (two to six words, at most 60 characters), no file extension, no quotes, no emoji, no leading list number. Name music by its title (from the tags or the lyrics); name ambience and sound effects by what they ARE; name videos by what happens in them; name pictures by what they show - a person, place, creature, scene or handout - and if the picture contains readable text (a sign, a map title, a document heading) use it. If the current name is already good, return it unchanged. Never invent facts that no clue supports: with nothing to go on, return the current name. Write in the language of the clues.\nReply ONLY with JSON: {\"names\":[{\"id\":\"<the number in brackets>\",\"name\":\"<new name>\"}]}";

const GENERIC_WORDS: [&str; 48] = [
    "img", "image", "images", "dsc", "dscn", "dcim", "pxl", "pic", "picture", "photo", "screenshot", "screen shot", "screen capture", "capture",
    "snapshot", "untitled", "new", "copy of", "video", "vid", "movie", "mov", "clip", "audio", "audio clip", "sound", "recording", "record", "voice",
    "voice memo", "memo", "track", "file", "download", "downloads", "whatsapp audio", "whatsapp video", "whatsapp image", "unknown", "temp", "tmp",
    "output", "result", "generated", "render", "new recording", "new video", "new audio",
];

struct Res {
    ext: regex::Regex,
    prefix: regex::Regex,
    generic: regex::Regex,
    hexish: regex::Regex,
    numeric: regex::Regex,
    lead_label: regex::Regex,
    lead_number: regex::Regex,
    fence: regex::Regex,
    ws: regex::Regex,
    seps: regex::Regex,
    path_chars: regex::Regex,
    word: regex::Regex,
}

fn res() -> &'static Res {
    static R: OnceLock<Res> = OnceLock::new();
    R.get_or_init(|| {
        let ext = r"(?:mp3|wav|ogg|oga|opus|flac|m4a|aac|wma|aiff?|mp4|m4v|mov|mkv|webm|avi|wmv|png|jpe?g|gif|webp|avif|bmp|tiff?)";
        let mut words: Vec<&str> = GENERIC_WORDS.to_vec();
        words.sort_by(|a, b| b.chars().count().cmp(&a.chars().count())); // stable, like Python's sorted
        let alt = words.iter().map(|w| regex::escape(w)).collect::<Vec<_>>().join("|");
        Res {
            ext: regex::Regex::new(&format!(r"(?i)\.{ext}$")).unwrap(),
            prefix: regex::Regex::new(r"^[0-9a-f]{12}-").unwrap(),
            generic: regex::Regex::new(&format!(r"(?i)^(?:(?:{alt})[\s_\-.]*)+(?:(?:\(\d+\)|\(copy\)|copy|\d[\d\-_.:\s]*|at\s[\d.:\sapm]+|[0-9a-f]{{6,}})[\s_\-.]*)*$")).unwrap(),
            hexish: regex::Regex::new(r"(?i)^[0-9a-f]{8,}$").unwrap(),
            numeric: regex::Regex::new(r"^[\d\s\-_.:()]+$").unwrap(),
            lead_label: regex::Regex::new(r"(?i)^(?:suggested\s+)?(?:name|title)\s*:\s*").unwrap(),
            lead_number: regex::Regex::new(r"^\d+[.)]\s+").unwrap(),
            fence: regex::Regex::new(r"(?s)^```(?:json)?\s*|\s*```$").unwrap(),
            ws: regex::Regex::new(r"\s+").unwrap(),
            seps: regex::Regex::new(r"[_\-]+").unwrap(),
            path_chars: regex::Regex::new(r#"[/\\<>:|?*"]+"#).unwrap(),
            word: regex::Regex::new(r"\w").unwrap(),
        }
    })
}

fn strip(s: &str) -> String {
    crate::edits::py_strip_pub(s)
}

/// `re.sub(r"\s+", " ", s)`
fn ws_sub(s: &str) -> String {
    res().ws.replace_all(s, " ").into_owned()
}

fn strip_chars(s: &str, set: &str) -> String {
    s.trim_matches(|c| set.contains(c)).to_string()
}

/// `str(x)` of a JSON value (a string as it is)
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `readable_filename`
pub fn readable_filename(url: &str) -> String {
    let r = res();
    let name = url.rsplit('/').next().unwrap_or("");
    let name = r.prefix.replace(name, "").into_owned();
    let name = r.ext.replace(&name, "").into_owned();
    let name = if (name.contains('_') || name.contains('-')) && !name.contains(' ') { r.seps.replace_all(&name, " ").into_owned() } else { name.replace('_', " ") };
    strip(&ws_sub(&name))
}

/// `looks_generic`
pub fn looks_generic(name: &str) -> bool {
    let r = res();
    let n = strip(&ws_sub(name));
    if n.is_empty() {
        return true;
    }
    if r.ext.is_match(&n) || r.hexish.is_match(&n) || r.numeric.is_match(&n) || r.generic.is_match(&n) {
        return true;
    }
    !n.contains(' ') && n.contains('_')
}

/// `clean_suggestion`
pub fn clean_suggestion(text: &str) -> String {
    let r = res();
    let s: String = text.chars().filter(|c| !matches!(*c as u32, 0x00..=0x08 | 0x0b | 0x0c | 0x0e..=0x1f | 0x7f)).collect();
    let s = if strip(&s).is_empty() { String::new() } else { strip(&s).split(['\n', '\r', '\u{85}', '\u{2028}', '\u{2029}']).next().unwrap_or("").to_string() };
    let s = strip(&strip_chars(&strip(&s), "`\"'“”‘’«»"));
    let s = r.lead_label.replace(&s, "").into_owned();
    let s = r.lead_number.replace(&s, "").into_owned();
    let s = r.ext.replace(&strip(&s), "").into_owned();
    let s = s.replace('_', " ");
    let s = r.path_chars.replace_all(&s, " ").into_owned();
    let s = strip_chars(&strip(&ws_sub(&s)), "`'“”‘’");
    let mut s = strip_chars(&s, " .,;:-–—_");
    if !r.word.is_match(&s) {
        return String::new();
    }
    if s.chars().count() > MAX_NAME {
        let cut: String = s.chars().take(MAX_NAME).collect();
        s = match cut.rsplit_once(' ') { Some((a, _)) => a.to_string(), None => cut };
    }
    strip_chars(&s, " .,;:-–—_")
}

/// `style_text`
fn style_text(preset: &str, free: &str) -> String {
    let mut parts = vec![];
    if let Some((_, t)) = STYLE_PRESETS.iter().find(|(k, _)| *k == preset) {
        parts.push(t.to_string());
    }
    let free = strip(&ws_sub(free));
    if !free.is_empty() {
        parts.push(free);
    }
    parts.join(" ").chars().take(400).collect()
}

/// `_loads_loose`
fn loads_loose(raw: &str) -> Option<Value> {
    let text = strip(&res().fence.replace_all(&strip(raw), ""));
    if text.is_empty() {
        return None;
    }
    if let Ok(v) = serde_json::from_str::<Value>(&text) {
        return Some(v);
    }
    for opener in ['{', '['] {
        if let Some(i) = text.find(opener) {
            if let Some(Ok(v)) = serde_json::Deserializer::from_str(&text[i..]).into_iter::<Value>().next() {
                return Some(v);
            }
        }
    }
    None
}

/// `parse_names(raw, ["1".."n"], current)`: (item number, name)
fn parse_names(raw: &str, n: usize, current: &[String]) -> Vec<(usize, String)> {
    let mut data = loads_loose(raw);
    if let Some(Value::Object(o)) = &data {
        if let Some(names) = o.get("names") {
            data = Some(names.clone());
        }
    }
    let pairs: Vec<(Value, Value)> = match data {
        Some(Value::Array(a)) => a.into_iter().filter_map(|e| match e {
            Value::Object(o) if o.contains_key("id") => Some((o["id"].clone(), o.get("name").cloned().unwrap_or(Value::Null))),
            _ => None,
        }).collect(),
        Some(Value::Object(o)) => o.into_iter().map(|(k, v)| (json!(k), v)).collect(),
        _ => vec![],
    };
    let mut out: Vec<(usize, String)> = vec![];
    for (key, value) in pairs {
        let key = py_str(&key);
        let Some(idx) = (1..=n).find(|i| i.to_string() == key) else { continue };
        if !matches!(value, Value::String(_) | Value::Number(_) | Value::Bool(_)) {
            continue;
        }
        let cur = &current[idx - 1];
        let kept = match &value { Value::String(v) if !cur.is_empty() && strip(v) == *cur => Some(cur.clone()), _ => None };
        let name = match kept {
            Some(n) => n,
            None => match clean_suggestion(&py_str(&value)) { c if c.is_empty() => continue, c => c },
        };
        match out.iter_mut().find(|(i, _)| *i == idx) {
            Some(e) => e.1 = name,
            None => out.push((idx, name)),
        }
    }
    out
}

/// Everything known about one item (`gather_signals`)
#[derive(Clone)]
pub struct Sig {
    kind: String,
    id: Option<i64>,
    url: Option<String>,
    name: String,
    filename: String,
    album: String,
    attached: String,
    description: String,
    meta: Map<String, Value>,
    transcript: String,
    listened: bool,
    uses: Vec<String>,
    albums: Vec<String>,
    picture: Option<String>,
    thumb: Option<String>,
    /// an audio clip to listen to first (the `listen` option)
    listen_path: Option<PathBuf>,
}

/// `_clock`
fn clock(seconds: f64) -> String {
    let s = seconds.round_ties_even() as i64;
    if s >= 3600 { format!("{}:{:02}:{:02}", s / 3600, s % 3600 / 60, s % 60) } else { format!("{}:{:02}", s / 60, s % 60) }
}

fn meta_text(meta: &Map<String, Value>, k: &str) -> String {
    meta.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `describe_item`
fn describe_item(sig: &Sig, label: usize) -> String {
    let mut lines = vec![format!("[{label}] {}", sig.kind), format!("current name: {}", if sig.name.is_empty() { "(none)" } else { &sig.name })];
    if !sig.filename.is_empty() { lines.push(format!("file name: {}", sig.filename)); }
    if !sig.album.is_empty() { lines.push(format!("folder: {}", sig.album)); }
    if !sig.albums.is_empty() { lines.push(format!("in albums: {}", sig.albums.iter().take(5).cloned().collect::<Vec<_>>().join(", "))); }
    if !sig.attached.is_empty() { lines.push(format!("attached to: {}", sig.attached)); }
    if !sig.uses.is_empty() { lines.push(format!("used as / in: {}", sig.uses.iter().take(5).cloned().collect::<Vec<_>>().join("; "))); }
    if !sig.description.is_empty() { lines.push(format!("description: {}", take(&sig.description, 300))); }
    let meta = &sig.meta;
    let mut tags: Vec<String> = ["title", "artist", "album", "genre", "year", "description", "comment", "keywords", "software"].iter()
        .filter(|k| !meta_text(meta, k).is_empty()).map(|k| format!("{k}=\"{}\"", meta_text(meta, k))).collect();
    if let Some(d) = meta.get("duration_s").and_then(|v| v.as_f64()).filter(|d| *d != 0.0) {
        tags.push(format!("length={}", clock(d)));
    }
    if let (Some(w), Some(h)) = (meta.get("width").and_then(|v| v.as_i64()), meta.get("height").and_then(|v| v.as_i64())) {
        tags.push(format!("size={w}x{h}"));
    }
    if !tags.is_empty() {
        lines.push(format!("tags: {}", tags.iter().map(|t| if t.chars().count() < 320 { t.clone() } else { format!("{}…\"", take(t, 320)) }).collect::<Vec<_>>().join("; ")));
    }
    if !meta_text(meta, "prompt").is_empty() { lines.push(format!("made from the prompt: {}", take(&meta_text(meta, "prompt"), 500))); }
    if !meta_text(meta, "lyrics").is_empty() { lines.push(format!("lyrics: {}", take(&meta_text(meta, "lyrics"), 700))); }
    if !sig.transcript.is_empty() { lines.push(format!("transcript: {}", take(&sig.transcript, 700))); }
    if sig.picture.is_some() { lines.push("(a picture of it is attached)".into()); }
    lines.join("\n")
}

/// `build_prompt`: the user message for `sigs`
fn build_prompt(sigs: &[Sig], style: &str, world_name: &str, about: &str) -> String {
    let mut all = vec![];
    if !world_name.is_empty() {
        all.push(if about.is_empty() { format!("Setting: {world_name}") } else { format!("Setting: {world_name} - {}", take(about, 300)) });
    }
    if !style.is_empty() {
        all.push(format!("Naming style: {style}"));
    }
    if !all.is_empty() {
        all.push(String::new());
    }
    all.push(sigs.iter().enumerate().map(|(i, s)| describe_item(s, i + 1)).collect::<Vec<_>>().join("\n\n"));
    all.join("\n")
}

/// `_basis`
fn basis(sig: &Sig) -> Vec<&'static str> {
    let has = |k: &str| !meta_text(&sig.meta, k).is_empty();
    let mut out = vec![];
    if ["title", "artist", "album", "genre", "description", "comment", "keywords"].iter().any(|k| has(k)) { out.push("tags"); }
    if has("lyrics") { out.push("lyrics"); }
    if !sig.transcript.is_empty() { out.push(if sig.listened { "listened" } else { "transcript" }); }
    if has("prompt") { out.push("prompt"); }
    if sig.picture.is_some() { out.push("picture"); }
    if !sig.uses.is_empty() || !sig.attached.is_empty() { out.push("usage"); }
    if !sig.filename.is_empty() && !looks_generic(&sig.filename) { out.push("filename"); }
    out
}

/// `_upload_path`
fn upload_path(cfg: &Config, url: &str) -> Option<PathBuf> {
    let rel = url.strip_prefix("/uploads/").filter(|r| !r.is_empty())?;
    let root = cfg.db_path.parent()?.join("uploads").canonicalize().ok()?;
    let path = root.join(rel).canonicalize().ok()?;
    (path.starts_with(&root) && path.is_file()).then_some(path)
}

/// `picture_b64`: the picture as a JPEG of at most 768 px a side, in base64 (the pixels Pillow would send; not its exact bytes)
fn picture_b64(path: Option<&Path>) -> Option<String> {
    use base64::Engine;
    let img = image::ImageReader::open(path?).ok()?.with_guessed_format().ok()?.decode().ok()?;
    let img = image::DynamicImage::ImageRgb8(img.to_rgb8());
    let img = if img.width() > PICTURE_MAX_SIDE || img.height() > PICTURE_MAX_SIDE {
        img.resize(PICTURE_MAX_SIDE, PICTURE_MAX_SIDE, image::imageops::FilterType::CatmullRom)
    } else {
        img
    };
    let mut buf = std::io::Cursor::new(vec![]);
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 82).encode_image(&img).ok()?;
    Some(base64::engine::general_purpose::STANDARD.encode(buf.into_inner()))
}

/// One image of the world: (url, where it is used, its albums, the name it shows)
type ImageUse = (String, Vec<String>, Vec<String>, String);

/// `image_usage`
fn image_usage(db: &rusqlite::Connection, wid: i64) -> Option<Vec<ImageUse>> {
    let mut usage: Vec<ImageUse> = vec![];
    let mut names: Vec<(String, String)> = vec![];
    for entry in crate::gallery::discover(db, wid)? {
        let url = entry.get("url")?.as_str()?.to_string();
        let uses: Vec<String> = entry.get("uses").and_then(|u| u.as_array())
            .map(|a| a.iter().filter_map(|u| u.get("label").and_then(|l| l.as_str()).map(str::to_string)).collect()).unwrap_or_default();
        match usage.iter_mut().find(|u| u.0 == url) {
            Some(u) => u.1 = uses,
            None => usage.push((url.clone(), uses, vec![], String::new())),
        }
        names.retain(|n| n.0 != url);
        names.push((url, entry.get("name").and_then(|n| n.as_str()).unwrap_or("").to_string()));
    }
    let mut st = db.prepare("SELECT name, image_urls_json FROM image_albums WHERE world_id = ?1").ok()?;
    let albums: Vec<(Option<String>, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    for (name, raw) in albums {
        let urls = match serde_json::from_str::<Value>(raw.as_deref().filter(|r| !r.is_empty()).unwrap_or("[]")) { Ok(Value::Array(a)) => a, _ => vec![] };
        for u in urls {
            let Value::String(u) = u else { continue };
            let name = name.clone().unwrap_or_else(|| "None".into());
            match usage.iter_mut().find(|x| x.0 == u) {
                Some(x) => x.2.push(name),
                None => usage.push((u, vec![], vec![name], String::new())),
            }
        }
    }
    let overrides = crate::gallery::title_overrides(db, wid)?;
    for u in usage.iter_mut() {
        let o = overrides.get(&u.0).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string);
        let n = names.iter().find(|n| n.0 == u.0).map(|n| n.1.clone()).filter(|s| !s.is_empty());
        u.3 = match o.or(n) { Some(x) => x, None => crate::gallery::display_name(&u.0, &[])? };
    }
    Some(usage)
}

/// `gather_signals` without the network (a clip's listening is left to `suggest`); None: not an item of this world
fn gather(cfg: &Config, db: &rusqlite::Connection, wid: i64, item: &Map<String, Value>, usage: Option<&Vec<ImageUse>>, pictures: bool, listen: bool) -> Option<Sig> {
    let kind = item.get("kind")?.as_str()?;
    if kind == "audio" || kind == "video" {
        let (table, albums) = if kind == "audio" { ("audio_clips", "audio_albums") } else { ("video_clips", "video_albums") };
        let id = crate::ai::reply_int(item.get("id")?).ok()?;
        let row = db.query_row(&format!("SELECT * FROM {table} WHERE id = ?1 AND world_id = ?2"), rusqlite::params![id, wid], |r| crate::layout::row_map(r)).ok()?;
        let s = |k: &str| row.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let name_of = |sql: &str, id: Option<i64>| id.filter(|i| *i != 0).and_then(|i| db.query_row(sql, [i], |r| r.get::<_, Option<String>>(0)).ok().flatten()).unwrap_or_default();
        let album = name_of(&format!("SELECT name FROM {albums} WHERE id = ?1"), row.get("album_id").and_then(|v| v.as_i64()));
        let entity = name_of("SELECT name FROM entities WHERE id = ?1", row.get("entity_id").and_then(|v| v.as_i64()));
        let path = upload_path(cfg, &s("file_url"));
        let meta = path.as_deref().map(|p| crate::media_meta::read_metadata(p, kind)).unwrap_or_default();
        let transcript = strip(&ws_sub(&s("transcript")));
        let listen_path = if transcript.is_empty() && listen && kind == "audio" { path.clone() } else { None };
        let poster = if kind == "video" { Some(s("poster_url")).filter(|p| !p.is_empty()) } else { None };
        let poster_path = poster.as_deref().and_then(|p| upload_path(cfg, p));
        return Some(Sig {
            kind: kind.into(), id: Some(id), url: None, name: s("name"), filename: readable_filename(&s("file_url")), album, attached: entity,
            description: s("description"), meta, transcript, listened: false, uses: vec![], albums: vec![],
            picture: if pictures { picture_b64(poster_path.as_deref()) } else { None }, thumb: poster.as_deref().and_then(nd_render::thumb_url), listen_path,
        });
    }
    if kind == "image" {
        let url = item.get("url")?.as_str()?.to_string();
        let path = upload_path(cfg, &url)?;
        let (_, uses, albums, name) = usage?.iter().find(|u| u.0 == url)?.clone();
        return Some(Sig {
            kind: "image".into(), id: None, url: Some(url.clone()), name, filename: readable_filename(&url), album: String::new(), attached: String::new(),
            description: String::new(), meta: crate::media_meta::read_metadata(&path, "image"), transcript: String::new(), listened: false, uses, albums,
            picture: if pictures { picture_b64(Some(&path)) } else { None }, thumb: nd_render::thumb_url(&url), listen_path: None,
        });
    }
    None
}

/// The signals for some items, and the errors for the ones that are not media of this world (`suggest`'s first half)
pub struct Gathered {
    sigs: Vec<Sig>,
    errors: Vec<String>,
    world_name: String,
    about: String,
}

fn gather_all(cfg: &Config, db: &rusqlite::Connection, wid: i64, items: &[Value], pictures: bool, listen: bool) -> Option<Gathered> {
    let world = db.query_row("SELECT name, description FROM worlds WHERE id = ?1", [wid], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))).ok()?;
    let needs_usage = items.iter().any(|i| i.get("kind").and_then(|k| k.as_str()) == Some("image"));
    let usage = if needs_usage { Some(image_usage(db, wid)?) } else { None };
    let (mut sigs, mut errors) = (vec![], vec![]);
    for item in items {
        let ok_kind = item.get("kind").and_then(|k| k.as_str()).is_some_and(|k| matches!(k, "audio" | "video" | "image"));
        let Some(it) = item.as_object().filter(|_| ok_kind) else {
            errors.push(format!("Not a media item: {}", take(&py_str(item), 80)));
            continue;
        };
        match gather(cfg, db, wid, it, usage.as_ref(), pictures, listen) {
            Some(s) => sigs.push(s),
            None => {
                let which = match it.get("id") { Some(v) if crate::edits::py_truthy_pub(Some(v)) => v.clone(), _ => it.get("url").cloned().unwrap_or(Value::Null) };
                errors.push(format!("Could not find {} {} in this world.", it["kind"].as_str().unwrap_or(""), py_str(&which)));
            }
        }
    }
    Some(Gathered { sigs, errors, world_name: world.0.unwrap_or_default(), about: strip(&world.1.unwrap_or_default()) })
}

/// `suggest`'s second half: listening, the model calls, the proposals {"results", "errors"}
async fn suggest(b: &Backend, stt_model: &str, mut g: Gathered, style: &str, preset: &str) -> Value {
    for sig in g.sigs.iter_mut() {
        let Some(path) = sig.listen_path.take() else { continue };
        // _listen: modest files only, never stored
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(u64::MAX);
        let dur = sig.meta.get("duration_s").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if size > LISTEN_MAX_BYTES || dur > LISTEN_MAX_SECONDS {
            continue;
        }
        if let Ok(text) = crate::stt::one_file(b, &path, stt_model).await {
            let t = take(&strip(&ws_sub(&crate::stt::collapse_repeats(&text))), 1200);
            sig.listened = !t.is_empty();
            sig.transcript = t;
        }
    }
    let guidance = style_text(preset, style);
    let schema = json!({"type": "object", "properties": {"names": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"},
        "name": {"type": "string"}}, "required": ["id", "name"]}}}, "required": ["names"]});
    let mut groups: Vec<Vec<usize>> = g.sigs.iter().enumerate().filter(|(_, s)| s.picture.is_some()).map(|(i, _)| vec![i]).collect();
    let plain: Vec<usize> = g.sigs.iter().enumerate().filter(|(_, s)| s.picture.is_none()).map(|(i, _)| i).collect();
    groups.extend(plain.chunks(TEXT_BATCH).map(|c| c.to_vec()));
    let mut answers: Vec<String> = vec![String::new(); g.sigs.len()];
    let model = b.model.clone();
    let mut opts = Map::new();
    opts.insert("num_predict".into(), json!(900));
    for group in groups {
        let sigs: Vec<Sig> = group.iter().map(|&i| g.sigs[i].clone()).collect();
        let user = build_prompt(&sigs, &guidance, &g.world_name, &g.about);
        let pics: Vec<&String> = sigs.iter().filter_map(|s| s.picture.as_ref()).collect();
        let content = if pics.is_empty() { json!(user) } else {
            let mut parts = vec![json!({"type": "text", "text": user})];
            parts.extend(pics.iter().map(|p| json!({"type": "image_url", "image_url": {"url": format!("data:{};base64,{p}", if p.starts_with("/9j/") { "image/jpeg" } else { "image/png" })}})));
            Value::Array(parts)
        };
        let raw = crate::ai::generate_chat_format(b, &model, SYSTEM, &[json!({"role": "user", "content": content})], false, &opts, Some(&schema)).await;
        if failed(&raw) {
            g.errors.push(take(&raw, 300));
            continue;
        }
        let current: Vec<String> = sigs.iter().map(|s| s.name.clone()).collect();
        for (n, name) in parse_names(&raw, sigs.len(), &current) {
            answers[group[n - 1]] = name;
        }
    }
    let results: Vec<Value> = g.sigs.iter().zip(answers).map(|(sig, new)| {
        json!({"kind": sig.kind, "id": sig.id, "url": sig.url, "old": sig.name, "new": new, "changed": !new.is_empty() && new != sig.name,
            "basis": basis(sig), "generic": looks_generic(&sig.name), "note": if new.is_empty() { "The model gave no name for this one." } else { "" },
            "thumb": sig.thumb})
    }).collect();
    json!({"results": results, "errors": g.errors})
}

// ── the routes ───────────────────────────────────────────────────────────────

/// The paths `plan` serves
pub fn serves(path: &str) -> bool {
    matches!(path, "/api/media-rename/suggest" | "/api/media-rename/runs")
}

/// `_require_edit`: edit level on the section of every kind touched (anything else is Python's 403)
fn kinds_editable(p: &PageCtx, w: &Map<String, Value>, items: &[Value]) -> bool {
    items.iter().all(|i| {
        let section = match i.as_object().and_then(|o| o.get("kind")).and_then(|k| k.as_str()) {
            Some("audio") => "audio",
            Some("video") => "video",
            Some("image") => "images",
            _ => return false,
        };
        crate::pages::edit_level(p, w, section) == Some(true)
    })
}

/// `POST /api/media-rename/suggest` and `POST /api/media-rename/runs` (None: Python's)
pub fn plan(cfg: Arc<Config>, db: &rusqlite::Connection, p: &PageCtx, path: &str, body: &[u8]) -> Option<Fut> {
    let w = p.world.as_ref()?;
    let wid = w.get("id")?.as_i64()?;
    let answer = |r: Response<Body>| -> Option<Fut> { Some(Box::pin(async move { r })) };
    let data = match serde_json::from_slice::<Value>(body) {
        Ok(Value::Object(o)) => o,
        Ok(_) => return answer(detail(StatusCode::BAD_REQUEST, "Send a JSON object.")),
        Err(_) if body.windows(3).any(|x| x == b"NaN") || body.windows(8).any(|x| x == b"Infinity") => return None,
        Err(_) => return answer(detail(StatusCode::BAD_REQUEST, "Send a JSON body.")),
    };
    let items = match data.get("items") { Some(Value::Array(a)) if !a.is_empty() => a.clone(), _ => return answer(detail(StatusCode::BAD_REQUEST, "Choose at least one item.")) };
    let suggesting = path.ends_with("/suggest");
    if suggesting && items.len() > MAX_SUGGEST_ITEMS {
        return answer(detail(StatusCode::BAD_REQUEST, format!("At most {MAX_SUGGEST_ITEMS} items per request.")));
    }
    if !kinds_editable(p, w, &items) {
        return None;
    }
    let b = backend(db)?;
    let text = |k: &str| match data.get(k) { v if !crate::edits::py_truthy_pub(v) => String::new(), Some(v) => py_str(v), None => String::new() };
    let flag = |k: &str| crate::edits::py_truthy_pub(data.get(k));
    if suggesting {
        let g = gather_all(&cfg, db, wid, &items, flag("pictures"), flag("listen"))?;
        let (style, preset, stt) = (text("style"), text("preset"), crate::stt::stt_model(&cfg));
        return Some(Box::pin(async move { crate::edits::json_response(&suggest(&b, &stt, g, &style, &preset).await).unwrap_or_else(crash) }));
    }
    // a run (create_run): saved, then worked through by this process
    if items.len() > MAX_RUN_ITEMS {
        return answer(detail(StatusCode::BAD_REQUEST, format!("At most {MAX_RUN_ITEMS} items in one run - narrow the selection.")));
    }
    let refs: Vec<Value> = items.iter().map(|i| {
        let kind = i["kind"].clone();
        let key = if kind == json!("image") { "url" } else { "id" };
        json!({"kind": kind, key: i.get(key).cloned().unwrap_or(Value::Null)})
    }).collect();
    let options = json!({"preset": take(&text("preset"), 40), "style": take(&text("style"), 400), "pictures": flag("pictures"), "listen": flag("listen")});
    use rusqlite::types::Value as Sql;
    let id = crate::orm::insert(db, "media_rename_runs", &[("world_id", Sql::Integer(wid)), ("created_by_user_id", Sql::Integer(p.user_id)),
        ("status", Sql::Text("pending".into())), ("options_json", Sql::Text(nd_render::py_dumps(&options))),
        ("items_json", Sql::Text(nd_render::py_dumps(&Value::Array(refs.clone())))), ("results_json", Sql::Text("[]".into())),
        ("errors_json", Sql::Text("[]".into())), ("total", Sql::Integer(refs.len() as i64)), ("done", Sql::Integer(0))])?;
    start_run(cfg, id);
    answer(crate::edits::json_response(&json!({"id": id})).unwrap_or_else(crash))
}

fn set_run(cfg: &Config, id: i64, fields: &[(&str, rusqlite::types::Value)]) {
    if let Some(db) = crate::writes::open(cfg) {
        crate::orm::update(&db, "media_rename_runs", id, fields);
    }
}

/// `_run(run_id)`: chunk after chunk, each under the AI slot, the proposals saved after every one
pub fn start_run(cfg: Arc<Config>, id: i64) {
    use rusqlite::types::Value as Sql;
    crate::jobs::spawn("media_rename_runs", id, async move {
        let blocking = |f: Box<dyn FnOnce() + Send>| tokio::task::spawn_blocking(f);
        let c = cfg.clone();
        let _ = blocking(Box::new(move || set_run(&c, id, &[("status", Sql::Text("running".into())), ("error", Sql::Text(String::new()))]))).await;
        loop {
            let c = cfg.clone();
            let state = tokio::task::spawn_blocking(move || -> Option<(i64, Vec<Value>, Map<String, Value>, usize)> {
                let db = crate::writes::open(&c)?;
                let run = db.query_row("SELECT * FROM media_rename_runs WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok()?;
                let raw = |k: &str, d: &'static str| run.get(k).and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or(d).to_string();
                let items = match serde_json::from_str::<Value>(&raw("items_json", "[]")).ok()? { Value::Array(a) => a, _ => vec![] };
                let options = match serde_json::from_str::<Value>(&raw("options_json", "{}")).ok()? { Value::Object(o) => o, _ => Map::new() };
                Some((run.get("world_id")?.as_i64()?, items, options, run.get("done").and_then(|v| v.as_i64()).unwrap_or(0).max(0) as usize))
            }).await.ok().flatten();
            let Some((wid, items, options, done)) = state else { return crate::jobs::Ended::Final };
            if done >= items.len() {
                break;
            }
            let pictures = crate::edits::py_truthy_pub(options.get("pictures"));
            let chunk: Vec<Value> = items[done..(done + if pictures { CHUNK_WITH_PICTURES } else { CHUNK }).min(items.len())].to_vec();
            let out = {
                let _slot = crate::jobs::slot("media rename").await;
                let (c, ch, o) = (cfg.clone(), chunk.clone(), options.clone());
                let prepared = tokio::task::spawn_blocking(move || -> Option<Option<(Backend, Gathered, String)>> {
                    let db = crate::writes::open(&c)?;
                    if db.query_row("SELECT 1 FROM worlds WHERE id = ?1", [wid], |_| Ok(())).is_err() {
                        return Some(None);
                    }
                    let flag = |k: &str| crate::edits::py_truthy_pub(o.get(k));
                    Some(Some((backend(&db)?, gather_all(&c, &db, wid, &ch, flag("pictures"), flag("listen"))?, crate::stt::stt_model(&c))))
                }).await.ok().flatten();
                match prepared {
                    None => {
                        let c = cfg.clone();
                        let _ = blocking(Box::new(move || set_run(&c, id, &[("status", Sql::Text("error".into())),
                            ("error", Sql::Text("RuntimeError: the run's items could not be read".into()))]))).await;
                        return crate::jobs::Ended::Final;
                    }
                    Some(None) => json!({"results": [], "errors": ["The world no longer exists."]}),
                    Some(Some((b, g, stt))) => {
                        let s = |k: &str| options.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
                        suggest(&b, &stt, g, &s("style"), &s("preset")).await
                    }
                }
            };
            let (c, n) = (cfg.clone(), chunk.len());
            let saved = tokio::task::spawn_blocking(move || -> Option<()> {
                let db = crate::writes::open(&c)?;
                let (results, errors): (Option<String>, Option<String>) = db.query_row("SELECT results_json, errors_json FROM media_rename_runs WHERE id = ?1", [id],
                    |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
                let list = |raw: Option<String>| match serde_json::from_str::<Value>(raw.as_deref().filter(|r| !r.is_empty()).unwrap_or("[]")) { Ok(Value::Array(a)) => a, _ => vec![] };
                let mut results = list(results);
                results.extend(out["results"].as_array().cloned().unwrap_or_default());
                let mut errors = list(errors);
                errors.extend(out["errors"].as_array().cloned().unwrap_or_default());
                errors.truncate(200);
                crate::orm::update(&db, "media_rename_runs", id, &[("results_json", Sql::Text(nd_render::py_dumps(&Value::Array(results)))),
                    ("errors_json", Sql::Text(nd_render::py_dumps(&Value::Array(errors)))), ("done", Sql::Integer((done + n) as i64))])?;
                Some(())
            }).await.ok().flatten();
            if saved.is_none() {
                return crate::jobs::Ended::Final; // the run was deleted
            }
        }
        let c = cfg.clone();
        let _ = blocking(Box::new(move || set_run(&c, id, &[("status", Sql::Text("done".into()))]))).await;
        crate::jobs::Ended::Final
    });
}

/// `POST /api/media-rename/runs/<id>/cancel` for a run this process works on (None: Python's)
pub fn cancel(cfg: &Config, p: &PageCtx, id: i64) -> Option<Response<Body>> {
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let db = crate::writes::open(cfg)?;
    db.query_row("SELECT 1 FROM media_rename_runs WHERE id = ?1 AND world_id = ?2", rusqlite::params![id, wid], |_| Ok(())).ok()?;
    if !crate::jobs::abort("media_rename_runs", id) {
        return None;
    }
    use rusqlite::types::Value as Sql;
    crate::orm::update(&db, "media_rename_runs", id, &[("status", Sql::Text("cancelled".into())), ("error", Sql::Text("Cancelled.".into()))]);
    crate::edits::json_response(&json!({"ok": true}))
}
