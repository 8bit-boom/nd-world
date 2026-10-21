//! Sending one file: type, validators (ETag / Last-Modified, 304) and byte ranges (206 / 416), streamed from disk.
use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, Response, StatusCode};
use std::path::Path;
use std::time::UNIX_EPOCH;
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio_util::io::ReaderStream;

/// What to send besides the file's own headers (Python's choice for an upload, the build's for a static file).
#[derive(Default)]
pub struct Extra {
    pub headers: Vec<(HeaderName, HeaderValue)>,
    /// Overrides the type guessed from the name.
    pub content_type: Option<String>,
}

pub fn etag(len: u64, mtime_ns: u128) -> String {
    format!("\"{:x}-{:x}\"", mtime_ns, len)
}

/// `bytes=a-b`, `a-`, `-n` (one range; more than one is answered with the whole file, as the standards allow).
pub fn parse_range(h: &str, len: u64) -> Option<Result<(u64, u64), ()>> {
    let spec = h.trim().strip_prefix("bytes=")?;
    if spec.contains(',') {
        return None;
    }
    let (a, b) = spec.split_once('-')?;
    let (start, end) = if a.is_empty() {
        let n: u64 = b.parse().ok()?;
        if n == 0 {
            return Some(Err(()));
        }
        (len.saturating_sub(n), len.checked_sub(1)?)
    } else {
        let s: u64 = a.parse().ok()?;
        let e: u64 = if b.is_empty() { len.saturating_sub(1) } else { b.parse().ok()? };
        (s, e.min(len.saturating_sub(1)))
    };
    if len == 0 || start >= len || start > end {
        return Some(Err(()));
    }
    Some(Ok((start, end)))
}

fn not_modified(req: &HeaderMap, etag: &str, mtime: std::time::SystemTime) -> bool {
    if let Some(inm) = req.get(header::IF_NONE_MATCH).and_then(|v| v.to_str().ok()) {
        return inm.split(',').any(|t| {
            let t = t.trim();
            t == "*" || t.trim_start_matches("W/") == etag
        });
    }
    if let Some(ims) = req.get(header::IF_MODIFIED_SINCE).and_then(|v| v.to_str().ok()).and_then(|v| httpdate::parse_http_date(v).ok()) {
        // HTTP dates have whole-second resolution
        return mtime.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) <= ims.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    }
    false
}

/// Answers a GET / HEAD for the file at `path`. `None` when it cannot be opened (the caller decides what that means).
pub async fn send(method: &Method, req_headers: &HeaderMap, path: &Path, extra: Extra) -> Option<Response<Body>> {
    let meta = tokio::fs::metadata(path).await.ok().filter(|m| m.is_file())?;
    let mut file = tokio::fs::File::open(path).await.ok()?;
    let len = meta.len();
    let mtime = meta.modified().ok()?;
    let mtime_ns = mtime.duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let tag = etag(len, mtime_ns);
    let ctype = extra
        .content_type
        .clone()
        .unwrap_or_else(|| mime_guess::from_path(path).first_or_octet_stream().essence_str().to_string());
    let ctype = if ctype.starts_with("text/") && !ctype.contains("charset") { format!("{ctype}; charset=utf-8") } else { ctype };

    let mut b = Response::builder()
        .header(header::ETAG, &tag)
        .header(header::LAST_MODIFIED, httpdate::fmt_http_date(mtime))
        .header(header::ACCEPT_RANGES, "bytes");
    for (k, v) in &extra.headers {
        b = b.header(k, v);
    }

    if not_modified(req_headers, &tag, mtime) {
        return b.status(StatusCode::NOT_MODIFIED).body(Body::empty()).ok();
    }
    b = b.header(header::CONTENT_TYPE, ctype);

    // a Range is honoured unless If-Range names another version of the file
    let range_ok = req_headers
        .get(header::IF_RANGE)
        .and_then(|v| v.to_str().ok())
        .map(|v| v == tag || httpdate::parse_http_date(v).ok().map_or(false, |d| d >= mtime - std::time::Duration::from_secs(1) && d <= mtime + std::time::Duration::from_secs(1)))
        .unwrap_or(true);
    let range = if range_ok { req_headers.get(header::RANGE).and_then(|v| v.to_str().ok()).and_then(|v| parse_range(v, len)) } else { None };

    let (status, start, count) = match range {
        None => (StatusCode::OK, 0, len),
        Some(Ok((s, e))) => {
            b = b.header(header::CONTENT_RANGE, format!("bytes {s}-{e}/{len}"));
            (StatusCode::PARTIAL_CONTENT, s, e - s + 1)
        }
        Some(Err(())) => {
            return b.status(StatusCode::RANGE_NOT_SATISFIABLE).header(header::CONTENT_RANGE, format!("bytes */{len}")).body(Body::empty()).ok();
        }
    };
    b = b.header(header::CONTENT_LENGTH, count);
    if method == Method::HEAD {
        return b.status(status).body(Body::empty()).ok();
    }
    if start > 0 {
        file.seek(std::io::SeekFrom::Start(start)).await.ok()?;
    }
    let body = Body::from_stream(ReaderStream::with_capacity(file.take(count), 64 * 1024));
    b.status(status).body(body).ok()
}
