//! Upload requests (phase 3): bodies too big to hold in memory. The body is streamed to a temporary file under the uploads folder,
//! its multipart parts are located in that file (`scan`), and a handler in crate::edits answers from there - copying a file part into
//! place without reading it into memory. A request the gateway does not answer goes to Python with the body re-sent from the file.

use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, HeaderMap, Request, Response};
use futures_util::StreamExt;

use crate::config::Config;
use crate::form::Form;
use crate::proxy::Proxy;

/// The most the gateway streams to disk for one request (bigger: Python's, as before)
const MAX_STREAMED: u64 = 4 * 1024 * 1024 * 1024;
/// Text fields of an upload form are small; a bigger one is Python's (Starlette caps a field at 1 MB)
const MAX_TEXT_FIELD: u64 = 1024 * 1024;

/// A file part of a streamed multipart body: where its bytes are in the body file
pub struct FileRef {
    pub name: String,
    pub filename: String,
    pub offset: u64,
    pub len: u64,
}

/// A streamed upload: its text fields, its file parts and the file holding the body
pub struct Upload {
    pub form: Form,
    pub files: Vec<FileRef>,
    pub path: PathBuf,
}

impl Upload {
    /// Copy a file part to `dest` (created; an existing file is replaced)
    pub fn copy_part(&self, part: &FileRef, dest: &Path) -> Option<()> {
        let mut src = std::fs::File::open(&self.path).ok()?;
        src.seek(SeekFrom::Start(part.offset)).ok()?;
        let mut out = std::fs::File::create(dest).ok()?;
        let copied = std::io::copy(&mut src.take(part.len), &mut out).ok()?;
        out.flush().ok()?;
        (copied == part.len).then_some(())
    }
}

fn boundary(headers: &HeaderMap) -> Option<String> {
    let ct = headers.get(header::CONTENT_TYPE)?.to_str().ok()?;
    let mut params = ct.split(';');
    if !params.next()?.trim().eq_ignore_ascii_case("multipart/form-data") {
        return None;
    }
    let mut b = None;
    for p in params {
        let (k, v) = p.trim().split_once('=')?;
        if !k.trim().eq_ignore_ascii_case("boundary") {
            return None;
        }
        let v = v.trim();
        let v = v.strip_prefix('"').and_then(|x| x.strip_suffix('"')).unwrap_or(v);
        if v.is_empty() || v.contains(['"', '\\']) {
            return None;
        }
        b = Some(v.to_string());
    }
    b
}

/// The offset of the first `needle` at or after `from` in the file (read in 1 MB windows that overlap by the needle's length)
fn find(f: &mut std::fs::File, from: u64, needle: &[u8]) -> Option<u64> {
    let mut pos = from;
    let mut buf = vec![0u8; 1024 * 1024 + needle.len()];
    loop {
        f.seek(SeekFrom::Start(pos)).ok()?;
        let mut n = 0;
        while n < buf.len() {
            let k = f.read(&mut buf[n..]).ok()?;
            if k == 0 {
                break;
            }
            n += k;
        }
        if n < needle.len() {
            return None;
        }
        if let Some(i) = buf[..n].windows(needle.len()).position(|w| w == needle) {
            return Some(pos + i as u64);
        }
        if n < buf.len() {
            return None;
        }
        pos += (n - needle.len() + 1) as u64;
    }
}

fn read_at(f: &mut std::fs::File, at: u64, len: u64) -> Option<Vec<u8>> {
    f.seek(SeekFrom::Start(at)).ok()?;
    let mut v = vec![0u8; len as usize];
    f.read_exact(&mut v).ok()?;
    Some(v)
}

/// Locate the parts of the multipart body in `path` - the same strict shape crate::form::parse_multipart accepts (anything else: None)
pub fn scan(path: &Path, headers: &HeaderMap) -> Option<Upload> {
    let delim = format!("--{}", boundary(headers)?).into_bytes();
    let mut f = std::fs::File::open(path).ok()?;
    let size = f.metadata().ok()?.len();
    let head = read_at(&mut f, 0, (delim.len() as u64 + 2).min(size))?;
    if head != [delim.as_slice(), b"\r\n"].concat() {
        return None;
    }
    let sep = [b"\r\n".as_slice(), &delim].concat();
    let mut at = delim.len() as u64 + 2;
    let (mut fields, mut files) = (vec![], vec![]);
    loop {
        let head_end = find(&mut f, at, b"\r\n\r\n")?;
        if head_end - at > 16 * 1024 {
            return None;
        }
        // header values are UTF-8 to Starlette (`_user_safe_decode`); other bytes are Python's
        let head = read_at(&mut f, at, head_end - at)?;
        let (mut name, mut filename) = (None, None);
        for line in std::str::from_utf8(&head).ok()?.split("\r\n") {
            let (h, v) = line.split_once(':')?;
            if h.eq_ignore_ascii_case("content-type") {
                continue;
            }
            if !h.eq_ignore_ascii_case("content-disposition") {
                return None;
            }
            let mut items = v.split(';');
            if !items.next()?.trim().eq_ignore_ascii_case("form-data") {
                return None;
            }
            for it in items {
                let (k, val) = it.trim().split_once('=')?;
                let val = val.strip_prefix('"')?.strip_suffix('"')?;
                if val.contains(['"', '\\', '%']) {
                    return None;
                }
                match k {
                    "name" => name = Some(val.to_string()),
                    "filename" => filename = Some(val.to_string()),
                    _ => return None,
                }
            }
        }
        let data = head_end + 4;
        let end = find(&mut f, data, &sep)?;
        let name = name?;
        match filename {
            Some(filename) => files.push(FileRef { name, filename, offset: data, len: end - data }),
            None => {
                if end - data > MAX_TEXT_FIELD {
                    return None;
                }
                fields.push((name, String::from_utf8(read_at(&mut f, data, end - data)?).ok()?));
            }
        }
        let after = end + sep.len() as u64;
        let tail = read_at(&mut f, after, (size - after).min(4))?;
        if tail.starts_with(b"--") {
            if !(tail.len() == 2 || tail == b"--\r\n") {
                return None;
            }
            break;
        }
        if !tail.starts_with(b"\r\n") {
            return None;
        }
        at = after + 2;
    }
    if fields.len() + files.len() > 1000 {
        return None;
    }
    Some(Upload { form: Form(fields), files, path: path.to_path_buf() })
}

/// The gateway's scratch folder for streamed bodies: <uploads>/_gateway
fn scratch_dir(cfg: &Config) -> Option<PathBuf> {
    let d = cfg.db_path.parent()?.join("uploads").join("_gateway");
    std::fs::create_dir_all(&d).ok()?;
    Some(d)
}

/// Stream the request to a file under the scratch folder, run `answer` on it, and forward it to Python (body from the file) when the
/// answer is None. `answer` runs on a blocking thread.
pub async fn serve<F>(cfg: Arc<Config>, proxy: &Proxy, req: Request<Body>, peer: Option<std::net::SocketAddr>, answer: F) -> Response<Body>
where
    F: FnOnce(&Config, &axum::http::Method, &axum::http::Uri, &HeaderMap, Option<Upload>) -> Option<Response<Body>> + Send + 'static,
{
    let len = req.headers().get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<u64>().ok());
    let Some(len) = len.filter(|n| *n <= MAX_STREAMED) else { return proxy.forward(req, peer).await };
    let Some(dir) = scratch_dir(&cfg) else { return proxy.forward(req, peer).await };
    let tmp = dir.join(format!("{:032x}.body", rand::random::<u128>()));
    let (parts, body) = req.into_parts();
    // the body to disk
    let mut written = 0u64;
    let ok = async {
        let mut out = tokio::fs::File::create(&tmp).await.ok()?;
        let mut stream = body.into_data_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.ok()?;
            written += chunk.len() as u64;
            if written > len {
                return None;
            }
            tokio::io::AsyncWriteExt::write_all(&mut out, &chunk).await.ok()?;
        }
        tokio::io::AsyncWriteExt::flush(&mut out).await.ok()?;
        Some(())
    }
    .await;
    if ok.is_none() || written != len {
        let _ = tokio::fs::remove_file(&tmp).await;
        return Response::builder().status(400).body(Body::empty()).unwrap();
    }
    let (method, uri, headers) = (parts.method.clone(), parts.uri.clone(), parts.headers.clone());
    let (cfg2, tmp2) = (cfg.clone(), tmp.clone());
    let answered = tokio::task::spawn_blocking(move || {
        let up = scan(&tmp2, &headers);
        answer(&cfg2, &method, &uri, &headers, up)
    })
    .await
    .ok()
    .flatten();
    let resp = match answered {
        Some(r) => r,
        None => match tokio::fs::File::open(&tmp).await {
            Ok(f) => proxy.forward(Request::from_parts(parts, Body::from_stream(tokio_util::io::ReaderStream::new(f))), peer).await,
            Err(_) => Response::builder().status(502).body(Body::empty()).unwrap(),
        },
    };
    let _ = tokio::fs::remove_file(&tmp).await;
    resp
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn scan_finds_fields_and_file_parts_in_the_body_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("b");
        let data: Vec<u8> = (0..3_000_000u32).map(|i| (i % 251) as u8).chain(*b"\r\n--XAnot\r\n--X").collect();
        let mut body = b"--XB\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx y\r\n--XB\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\xc3\x9c.mp3\"\r\n\r\n".to_vec();
        body.extend(&data);
        body.extend(b"\r\n--XB--\r\n");
        std::fs::write(&path, &body).unwrap();
        let mut h = HeaderMap::new();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("multipart/form-data; boundary=XB"));
        let up = scan(&path, &h).unwrap();
        assert_eq!(up.form.get("a"), Some("x y"));
        assert_eq!((up.files[0].name.as_str(), up.files[0].filename.as_str(), up.files[0].len), ("file", "Ü.mp3", data.len() as u64));
        let out = dir.path().join("out");
        up.copy_part(&up.files[0], &out).unwrap();
        assert_eq!(std::fs::read(&out).unwrap(), data);
        std::fs::write(&path, b"--XB\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx").unwrap();
        assert!(scan(&path, &h).is_none());
    }
}
