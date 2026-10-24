//! `GET /uploads/<path>` (app/main.py `serve_upload`): the access decision and the headers, answered here instead of Python deciding
//! and the gateway only streaming the file. A signed-in viewer (the auth gate's rule for the path); the file inside the uploads folder
//! (a thumbnail asked for in the other format found), never the live-recording archive, a strict map's original picture only for
//! those who edit Maps. The lightbox's display copy (`?display=`), which may have to be made first, stays Python's for now.
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, Response, Uri};
use percent_encoding::percent_decode_str;

use crate::config::Config;

pub struct Decision {
    pub path: PathBuf,
    pub headers: Vec<(HeaderName, HeaderValue)>,
    pub content_type: Option<String>,
}

/// Some(Ok(file to send)), Some(Err(404)), or None for Python
pub fn decide(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Result<Decision, ()>> {
    let raw = uri.path().strip_prefix("/uploads/")?;
    if crate::pages::query_param(uri, "display").is_some_and(|d| !d.is_empty()) && !raw.starts_with("maps/") {
        return None;
    }
    let filepath = percent_decode_str(raw).decode_utf8().ok()?.to_string();
    if filepath.contains('\0') {
        return None;
    }
    // a signed-in viewer (Python's auth gate answers the rest: the redirect to sign in, an expired session)
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let root = std::fs::canonicalize(cfg.db_path.parent()?.join("uploads")).ok()?;
    let joined = root.join(&filepath);
    let mut path = match std::fs::canonicalize(&joined) {
        Ok(p) => p,
        Err(_) => normalise(&joined),
    };
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
    if path.starts_with(&root) && !path.is_file() && (name.ends_with("_thumb.webp") || name.ends_with("_thumb.avif")) {
        let other = path.with_file_name(format!("{}{}", &name[..name.len() - 4], if name.ends_with(".webp") { "avif" } else { "webp" }));
        if other.is_file() {
            path = std::fs::canonicalize(&other).ok()?;
        }
    }
    if !path.starts_with(&root) || !path.is_file() {
        return Some(Err(()));
    }
    let rel = path.strip_prefix(&root).ok()?.to_path_buf();
    let first = rel.components().next().and_then(|c| c.as_os_str().to_str()).unwrap_or("");
    if first == "live" {
        return Some(Err(()));
    }
    if first == "schematics" {
        // _guard_strict_map_picture: the original picture of a strict map goes to those who edit Maps only
        let url = format!("/uploads/{}", rel.to_str()?);
        let mut st = db.prepare("SELECT * FROM schematics WHERE image_url = ?1").ok()?;
        let rows = st.query_map([&url], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        for sc in rows {
            if !crate::edits::map_is_strict(&sc)? {
                continue;
            }
            let wid = sc.get("world_id").and_then(|v| v.as_i64());
            let w = wid.and_then(|w| db.query_row("SELECT * FROM worlds WHERE id = ?1", [w], |r| crate::layout::row_map(r)).ok());
            let Some(w) = w else { return Some(Err(())) };
            // world_can_edit_section is read for the viewer's role in the ACTIVE world; another world's map: Python's
            if !p.is_gm && p.world.as_ref().and_then(|a| a.get("id")).and_then(|v| v.as_i64()) != wid {
                return None;
            }
            if !crate::pages::edit_level(&p, &w, "maps")? {
                return Some(Err(()));
            }
        }
    }
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    let mut out: Vec<(HeaderName, HeaderValue)> = vec![(HeaderName::from_static("x-content-type-options"), HeaderValue::from_static("nosniff"))];
    let fname = path.file_name()?.to_str()?;
    if ext == "svg" {
        out.push((header::CONTENT_DISPOSITION, HeaderValue::from_str(&format!("attachment; filename=\"{fname}\"")).ok()?));
    } else if ext == "html" || ext == "htm" {
        out.push((header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("sandbox allow-scripts allow-popups")));
        out.push((header::X_FRAME_OPTIONS, HeaderValue::from_static("SAMEORIGIN")));
    }
    out.push((header::CACHE_CONTROL, HeaderValue::from_static(if filepath.starts_with("maps/") { "private, no-cache" } else { "private, max-age=31536000, immutable" })));
    let content_type = (ext == "html" || ext == "htm").then(|| "text/html; charset=utf-8".to_string());
    Some(Ok(Decision { path, headers: out, content_type }))
}

/// `..` folded away without touching the disk (the target may not exist)
fn normalise(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

pub async fn send(method: &Method, headers: &HeaderMap, d: Decision) -> Option<Response<Body>> {
    let mut r = crate::files::send(method, headers, &d.path, crate::files::Extra { headers: d.headers, content_type: d.content_type }).await?;
    r.headers_mut().insert("x-nd-served-by", HeaderValue::from_static("gateway"));
    Some(r)
}
