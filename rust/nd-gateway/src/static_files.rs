//! `/static/` straight from disk, the way app/http_perf.py answers it: the build's pre-compressed `.br` / `.gz` twin when the browser accepts it,
//! old picture links redirected to the AVIF / WebP the build converted them to, a year-long immutable cache. Anything unusual (a missing file,
//! an odd path, a text file the browser wants gzipped and that has no twin) returns `None` and Python answers it, so behaviour only ever differs
//! for the cases this module is sure about.
use crate::{files, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, Method, Response, StatusCode, Uri};
use percent_encoding::percent_decode_str;
use std::path::{Component, Path, PathBuf};

const IMMUTABLE: &str = "public, max-age=31536000, immutable";

fn accepted(headers: &HeaderMap) -> Vec<String> {
    headers
        .get(header::ACCEPT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.split(',').map(|p| p.split(';').next().unwrap_or("").trim().to_ascii_lowercase()).filter(|p| !p.is_empty()).collect())
        .unwrap_or_default()
}

/// The path below `static/` for a request path, if it is plain (no `..`, no NUL, no backslash).
pub fn relative(path: &str) -> Option<PathBuf> {
    let rel = percent_decode_str(path.strip_prefix("/static/")?).decode_utf8().ok()?;
    if rel.is_empty() || rel.contains('\0') || rel.contains('\\') {
        return None;
    }
    let p = PathBuf::from(rel.as_ref());
    if p.components().all(|c| matches!(c, Component::Normal(_))) { Some(p) } else { None }
}

fn text_like(path: &Path) -> bool {
    let m = mime_guess::from_path(path).first_or_octet_stream();
    let essence = m.essence_str();
    m.type_() == "text" || essence.ends_with("json") || essence.ends_with("javascript") || essence.ends_with("xml") || essence.contains("svg")
}

async fn mtime_ns(p: &Path) -> Option<u128> {
    let m = tokio::fs::metadata(p).await.ok()?;
    m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok().map(|d| d.as_nanos())
}

pub async fn serve(cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    if method != Method::GET && method != Method::HEAD {
        return None;
    }
    let rel = relative(uri.path())?;
    let root = tokio::fs::canonicalize(&cfg.static_dir).await.ok()?;
    let src = root.join(&rel);
    let name = src.file_name()?.to_str()?.to_string();

    let exists = tokio::fs::metadata(&src).await.map(|m| m.is_file()).unwrap_or(false);
    if !exists {
        // a bundled map's old .jpg / .png / .webp link: the build stores it as .avif (or .webp)
        let ext = src.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
        if rel.starts_with("maps") && matches!(ext.as_str(), "jpg" | "jpeg" | "png" | "webp") {
            for alt_ext in ["avif", "webp"] {
                let alt = src.with_extension(alt_ext);
                if alt != src && tokio::fs::metadata(&alt).await.map(|m| m.is_file()).unwrap_or(false) {
                    let path = uri.path();
                    let stem = path.rsplit_once('.').map(|(a, _)| a).unwrap_or(path);
                    return Response::builder()
                        .status(StatusCode::MOVED_PERMANENTLY)
                        .header(header::LOCATION, format!("{stem}.{alt_ext}"))
                        .body(Body::empty())
                        .ok();
                }
            }
        }
        return None;
    }
    // the symlink / containment check is done on the resolved path
    let real = tokio::fs::canonicalize(&src).await.ok()?;
    if !real.starts_with(&root) || name.ends_with(".br") || name.ends_with(".gz") {
        return None;
    }
    let media = mime_guess::from_path(&src).first_or_octet_stream().essence_str().to_string();
    let accept = accepted(headers);
    let src_time = mtime_ns(&real).await?;

    for (token, ext) in [("br", ".br"), ("gzip", ".gz")] {
        if !accept.iter().any(|a| a == token) {
            continue;
        }
        let twin = real.with_file_name(format!("{name}{ext}"));
        if mtime_ns(&twin).await.map_or(false, |t| t >= src_time) {
            let extra = files::Extra {
                content_type: Some(media.clone()),
                headers: vec![
                    (header::CONTENT_ENCODING, HeaderValue::from_static(if token == "br" { "br" } else { "gzip" })),
                    (header::VARY, HeaderValue::from_static("Accept-Encoding")),
                    (header::CACHE_CONTROL, HeaderValue::from_static(IMMUTABLE)),
                ],
            };
            if let Some(r) = files::send(method, headers, &twin, extra).await {
                return Some(r);
            }
        }
    }
    // no twin: text is gzipped on the way out, as Python's GZipMiddleware does to a streamed FileResponse (whatever its size; pictures,
    // which it also recompresses for nothing, are sent as they are - the decoded bytes are the same)
    let gzip = text_like(&real) && accept.iter().any(|a| a == "gzip");
    let extra = files::Extra { content_type: None, headers: vec![(header::CACHE_CONTROL, HeaderValue::from_static(IMMUTABLE))] };
    let resp = files::send(method, headers, &real, extra).await?;
    if !gzip || resp.status() != StatusCode::OK {
        return Some(resp);
    }
    let (mut parts, body) = resp.into_parts();
    let bytes = axum::body::to_bytes(body, 64 << 20).await.ok()?;
    let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::new(9));
    std::io::Write::write_all(&mut enc, &bytes).ok()?;
    let out = enc.finish().ok()?;
    parts.headers.remove(header::CONTENT_LENGTH);
    parts.headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
    parts.headers.insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    let body = if *method == Method::HEAD { Body::empty() } else { Body::from(out) };
    Some(Response::from_parts(parts, body))
}
