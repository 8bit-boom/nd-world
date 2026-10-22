//! The image gallery (app/gallery.py + app/routers/gallery.py): every picture a world's content uses, found the way
//! `discover_world_images` finds it, and the `/images` page built on it. `None` = not sure: the page goes to Python.

use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use regex::Regex;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{render_with, section_ok, Pages};

const INITIAL_BATCH: usize = 200;
const SESSION_MEDIA_MAX: usize = 50;

fn md_images(text: Option<&str>) -> Vec<String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"!\[[^\]]*\]\(([^)\s]+)\)").unwrap());
    match text {
        Some(t) if !t.is_empty() => re.captures_iter(t).map(|c| c[1].to_string()).collect(),
        _ => vec![],
    }
}

/// `image_display_name`: where it is first used, else its file name without the 12-hex upload prefix
pub fn display_name(url: &str, uses: &[Value]) -> Option<String> {
    if let Some(first) = uses.first() {
        return Some(first.get("label")?.as_str()?.to_string());
    }
    let fname = url.rsplit('/').next().unwrap_or(url);
    let b = fname.as_bytes();
    let prefixed = b.len() > 13 && b[..12].iter().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c)) && b[12] == b'-';
    let stripped = if prefixed { &fname[13..] } else { fname };
    Some(if stripped.is_empty() { fname } else { stripped }.to_string())
}

/// `title_overrides`: {url: title} given by hand or by the AI renamer
pub fn title_overrides(db: &Connection, wid: i64) -> Option<Map<String, Value>> {
    let mut st = db.prepare("SELECT url, title FROM media_titles WHERE world_id = ?1 ORDER BY id").ok()?;
    let mut m = Map::new();
    for r in st.query_map([wid], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))).ok()? {
        let (u, t) = r.ok()?;
        m.insert(u?, json!(t));
    }
    Some(m)
}

fn text_cols(db: &Connection, sql: &str, wid: i64, n: usize) -> Option<Vec<Vec<Option<String>>>> {
    let mut st = db.prepare(sql).ok()?;
    let rows = st
        .query_map([wid], |r| (0..n).map(|i| r.get::<_, Option<String>>(i)).collect::<rusqlite::Result<Vec<_>>>())
        .ok()?
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    Some(rows)
}

/// `discover_world_images`: [{url, uses: [{label, href}], name}] sorted by URL
pub fn discover(db: &Connection, wid: i64) -> Option<Vec<Map<String, Value>>> {
    let mut found: Vec<(String, Vec<Value>)> = vec![];
    let mut add = |url: Option<&str>, label: String, href: String| {
        let Some(url) = url.filter(|u| !u.is_empty()) else { return };
        let use_ = json!({"label": label, "href": href});
        match found.iter_mut().find(|(u, _)| u == url) {
            Some(e) => e.1.push(use_),
            None => found.push((url.to_string(), vec![use_])),
        }
    };
    // entities: portrait, body embeds, note embeds - in id order
    let ents = text_cols(db, "SELECT CAST(id AS TEXT), name, image_url, body FROM entities WHERE world_id = ?1 ORDER BY id", wid, 4)?;
    let notes = text_cols(
        db,
        "SELECT CAST(entity_notes.entity_id AS TEXT), entity_notes.content FROM entity_notes JOIN entities ON entities.id = entity_notes.entity_id \
         WHERE entities.world_id = ?1 AND entity_notes.content LIKE '%![%' ORDER BY entity_notes.id",
        wid,
        2,
    )?;
    for e in &ents {
        let (id, name) = (e[0].clone()?, e[1].clone().unwrap_or_else(|| "None".into()));
        let href = format!("/entity/{id}");
        add(e[2].as_deref(), name.clone(), href.clone());
        if e[3].as_deref().map_or(false, |b| b.contains("![")) {
            for u in md_images(e[3].as_deref()) {
                add(Some(&u), name.clone(), href.clone());
            }
        }
        for n in notes.iter().filter(|n| n[0] == e[0]) {
            for u in md_images(n[1].as_deref()) {
                add(Some(&u), format!("{name} (note)"), href.clone());
            }
        }
    }
    let pcs = text_cols(db, "SELECT CAST(id AS TEXT), name, portrait_url, backstory, notes FROM player_characters WHERE world_id = ?1 ORDER BY id", wid, 5)?;
    for pc in &pcs {
        let (id, name) = (pc[0].clone()?, pc[1].clone().unwrap_or_else(|| "None".into()));
        let href = format!("/characters/{id}");
        add(pc[2].as_deref(), name.clone(), href.clone());
        for u in md_images(pc[3].as_deref()) {
            add(Some(&u), format!("{name} (backstory)"), href.clone());
        }
        for u in md_images(pc[4].as_deref()) {
            add(Some(&u), format!("{name} (notes)"), href.clone());
        }
    }
    // session media (session_media.media_items): pictures under /uploads/, deduplicated, at most 50 items of any kind
    let sessions = text_cols(db, "SELECT CAST(id AS TEXT), title, recap_media_json FROM game_sessions WHERE world_id = ?1 ORDER BY id", wid, 3)?;
    for s in &sessions {
        let raw = s[2].clone().filter(|t| !t.is_empty()).unwrap_or_else(|| "[]".into());
        let Ok(Value::Array(items)) = serde_json::from_str::<Value>(&raw) else { continue };
        let mut seen_img: Vec<String> = vec![];
        let mut seen_vid: Vec<i64> = vec![];
        let mut kept = 0usize;
        for it in &items {
            if kept >= SESSION_MEDIA_MAX {
                break;
            }
            let Some(it) = it.as_object() else { continue };
            match it.get("kind").and_then(|k| k.as_str()) {
                Some("image") => {
                    let Some(url) = it.get("url").and_then(|u| u.as_str()) else { continue };
                    if !url.starts_with("/uploads/") || seen_img.iter().any(|x| x == url) {
                        continue;
                    }
                    seen_img.push(url.to_string());
                    kept += 1;
                    add(Some(url), format!("{} (session media)", s[1].clone().unwrap_or_else(|| "None".into())), format!("/sessions/{}", s[0].clone()?));
                }
                Some("video") => {
                    let Some(cid) = it.get("clip_id").filter(|v| v.is_i64() || v.is_u64()).and_then(|v| v.as_i64()) else {
                        if it.get("clip_id").map_or(false, |v| v.is_number() && !v.is_f64()) {
                            return None; // a clip id past i64: Python's
                        }
                        continue;
                    };
                    if seen_vid.contains(&cid) {
                        continue;
                    }
                    seen_vid.push(cid);
                    kept += 1;
                }
                _ => {}
            }
        }
    }
    // pictures a character kept from the table (not their own uploads)
    let pc_name = |id: &str| pcs.iter().find(|p| p[0].as_deref() == Some(id)).map(|p| p[1].clone().unwrap_or_else(|| "None".into()));
    let kept = text_cols(
        db,
        "SELECT CAST(character_id AS TEXT), url FROM character_keepsakes WHERE world_id = ?1 AND kind = 'image' AND own IS 0 ORDER BY id",
        wid,
        2,
    )?;
    for k in &kept {
        let cid = k[0].clone()?;
        let who = pc_name(&cid).unwrap_or_else(|| "A character".into());
        add(k[1].as_deref(), format!("{who} (kept)"), format!("/characters/{cid}"));
    }
    let overrides = title_overrides(db, wid)?;
    let mut out = vec![];
    for (url, uses) in found {
        let name = match overrides.get(&url).filter(|t| t.as_str().map_or(false, |s| !s.is_empty())) {
            Some(t) => t.clone(),
            None => json!(display_name(&url, &uses)?),
        };
        let mut m = Map::new();
        m.insert("url".into(), json!(url));
        m.insert("uses".into(), Value::Array(uses));
        m.insert("name".into(), name);
        out.push(m);
    }
    out.sort_by(|a, b| a["url"].as_str().cmp(&b["url"].as_str()));
    Some(out)
}

/// `_load_urls`: an album's picture list ([] for anything that is not a JSON list)
pub fn album_urls(album: &Map<String, Value>) -> Value {
    let raw = album.get("image_urls_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw) {
        Ok(v @ Value::Array(_)) => v,
        _ => json!([]),
    }
}

/// `GET /images` (app/routers/gallery.py `images_gallery`)
pub fn images_gallery(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let world = p.world.clone()?;
    if !section_ok(&p, Some(&world), "images")? {
        return None;
    }
    let wid = world.get("id")?.as_i64()?;
    let uploads = cfg.db_path.parent()?.join("uploads");
    let mut images = discover(&db, wid)?;
    for img in images.iter_mut() {
        let (size, mtime) = crate::media::file_facts(&uploads, img.get("url").and_then(|u| u.as_str()));
        img.insert("size".into(), json!(size));
        img.insert("mtime".into(), json!(mtime));
    }
    let total = images.len();
    let rest: Vec<Map<String, Value>> = if total > INITIAL_BATCH { images.split_off(INITIAL_BATCH) } else { vec![] };
    let mut remaining = vec![];
    for mut img in rest {
        let url = img.get("url")?.as_str()?.to_string();
        let thumb = nd_render::thumb_url(&url)?;
        img.insert("thumb_url".into(), json!(thumb));
        remaining.push(Value::Object(img));
    }
    let mut st = db.prepare("SELECT * FROM image_albums WHERE world_id = ?1 AND parent_id IS NULL ORDER BY name, id").ok()?;
    let albums = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let (mut urls, mut subs) = (Map::new(), Map::new());
    for a in &albums {
        let id = a.get("id")?.as_i64()?;
        urls.insert(id.to_string(), album_urls(a));
        let n: i64 = db.query_row("SELECT count(*) FROM image_albums WHERE parent_id = ?1", [id], |r| r.get(0)).ok()?;
        subs.insert(id.to_string(), json!(n));
    }
    let c = &mut p.ctx;
    c.insert("images".into(), Value::Array(images.into_iter().map(Value::Object).collect()));
    c.insert("images_total".into(), json!(total));
    c.insert("images_remaining".into(), Value::Array(remaining));
    c.insert("albums".into(), Value::Array(albums.into_iter().map(Value::Object).collect()));
    c.insert("album_urls".into(), json!({"__intkeys__": Value::Object(urls)}));
    c.insert("sub_album_counts".into(), json!({"__intkeys__": Value::Object(subs)}));
    render_with(pages, "gallery_index.html", p.ctx, headers)
}

/// `GET /images/albums/<id>` (app/routers/gallery.py `album_detail`)
pub fn album_detail(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let n = uri.path().strip_prefix("/images/albums/")?;
    if n.is_empty() || n.len() >= 16 || !n.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let aid: i64 = n.parse().ok()?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let mut st = db.prepare("SELECT * FROM image_albums WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let all = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let id_of = |a: &Map<String, Value>| a.get("id").and_then(|v| v.as_i64());
    let parent_of = |a: &Map<String, Value>| a.get("parent_id").and_then(|v| v.as_i64());
    let album = all.iter().find(|a| id_of(a) == Some(aid))?.clone();
    let urls_v = album_urls(&album);
    let urls: Vec<String> = urls_v.as_array()?.iter().map(|u| u.as_str().map(|s| s.to_string())).collect::<Option<_>>()?;
    // world_image_names: a chosen title, else where it is used, else the file name
    let overrides = title_overrides(&db, wid)?;
    let used: Vec<(String, Value)> = discover(&db, wid)?.into_iter().map(|e| (e["url"].as_str().unwrap_or("").to_string(), e["name"].clone())).collect();
    let truthy = |v: &Value| !matches!(v, Value::Null) && v.as_str() != Some("");
    let uploads = cfg.db_path.parent()?.join("uploads");
    let (mut names, mut facts) = (Map::new(), Map::new());
    for u in &urls {
        let name = match overrides.get(u).filter(|v| truthy(v)) {
            Some(t) => t.clone(),
            None => match used.iter().rev().find(|(k, _)| k == u).map(|(_, n)| n).filter(|v| truthy(v)) {
                Some(n) => n.clone(),
                None => json!(display_name(u, &[])?),
            },
        };
        names.insert(u.clone(), name);
        let (size, mtime) = crate::media::file_facts(&uploads, Some(u));
        facts.insert(u.clone(), json!({"size": size, "mtime": mtime}));
    }
    let mut st = db.prepare("SELECT * FROM image_albums WHERE parent_id = ?1 ORDER BY name, id").ok()?;
    let children = st.query_map([aid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    let mut child_urls = Map::new();
    for c in &children {
        child_urls.insert(id_of(c)?.to_string(), album_urls(c));
    }
    // move targets: every other album of the world outside this one's own subtree, with its path
    let mut excluded = vec![aid];
    let mut frontier = vec![aid];
    loop {
        let next: Vec<i64> = all.iter().filter(|a| parent_of(a).map_or(false, |p| frontier.contains(&p))).filter_map(|a| id_of(a)).filter(|i| !excluded.contains(i)).collect();
        if next.is_empty() {
            break;
        }
        excluded.extend(&next);
        frontier = next;
    }
    let path_for = |a: &Map<String, Value>| -> Option<String> {
        let mut names = vec![];
        let mut seen = vec![];
        let mut cur = Some(a);
        while let Some(c) = cur {
            let id = id_of(c)?;
            if seen.contains(&id) {
                break;
            }
            seen.push(id);
            names.push(crate::pages::py_str_value(c.get("name")?)?);
            cur = parent_of(c).filter(|p| *p != 0).and_then(|p| all.iter().find(|x| id_of(x) == Some(p)));
        }
        names.reverse();
        Some(names.join(" / "))
    };
    let mut targets = vec![];
    for a in all.iter().filter(|a| id_of(a).map_or(false, |i| !excluded.contains(&i))) {
        targets.push(json!({"id": id_of(a)?, "path": path_for(a)?}));
    }
    let crumbs = crate::media::breadcrumb(&db, "image_albums", &album)?;
    let c = &mut p.ctx;
    c.insert("album".into(), Value::Object(album));
    c.insert("image_urls".into(), urls_v);
    c.insert("image_names".into(), Value::Object(names));
    c.insert("image_facts".into(), Value::Object(facts));
    c.insert("breadcrumb".into(), Value::Array(crumbs));
    c.insert("child_albums".into(), Value::Array(children.into_iter().map(Value::Object).collect()));
    c.insert("child_album_urls".into(), json!({"__intkeys__": Value::Object(child_urls)}));
    c.insert("move_targets".into(), Value::Array(targets));
    render_with(pages, "gallery_album.html", p.ctx, headers)
}
