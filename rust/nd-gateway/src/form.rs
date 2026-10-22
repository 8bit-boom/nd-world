//! Request bodies as FastAPI reads them: `request.form()` for application/x-www-form-urlencoded (Starlette's FormParser) and
//! `json.loads(body)`. Only the plain cases are read here - anything else returns None and the request goes to Python.

use percent_encoding::percent_decode_str;

/// A urlencoded form: (name, value) pairs in order; `get` is Starlette's FormData.get (the last value of a repeated name)
#[derive(Clone)]
pub struct Form(pub Vec<(String, String)>);

impl Form {
    /// None for a body Starlette might read differently than this does: non-ASCII bytes (decoded as latin-1 there), `;` separators,
    /// percent-escapes that are not UTF-8
    pub fn parse(body: &[u8]) -> Option<Form> {
        if !body.is_ascii() || body.contains(&b';') {
            return None;
        }
        let text = std::str::from_utf8(body).ok()?;
        let mut out = vec![];
        for pair in text.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let dec = |s: &str| -> Option<String> { percent_decode_str(&s.replace('+', " ")).decode_utf8().ok().map(|c| c.into_owned()) };
            out.push((dec(k)?, dec(v)?));
        }
        Some(Form(out))
    }

    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }

    pub fn has(&self, name: &str) -> bool {
        self.0.iter().any(|(k, _)| k == name)
    }
}

/// Python's `int(text)` for a form value, when it is plain decimal digits with an optional minus sign; None for anything Python's int()
/// would read differently or reject (the request is then Python's)
pub fn py_int(s: &str) -> Option<i64> {
    let body = s.strip_prefix('-').unwrap_or(s);
    if body.is_empty() || body.len() > 15 || !body.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// The content type without parameters, lowercased
pub fn content_type(headers: &axum::http::HeaderMap) -> String {
    headers.get(axum::http::header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").split(';').next().unwrap_or("").trim().to_ascii_lowercase()
}

/// A file part of a multipart body: its field name, filename and size (the gateway never stores one - a request with a real file is Python's)
pub struct FilePart {
    pub name: String,
    pub filename: String,
    pub len: usize,
    pub data: Vec<u8>,
}

/// `multipart/form-data` as Starlette's MultiPartParser reads it, for the plain shape browsers send: `--boundary` CRLF parts, each with a
/// `Content-Disposition: form-data; name="..."` (plus `filename="..."` for a file) and optionally a Content-Type. Anything else (other
/// headers, escapes or non-ASCII in headers, a preamble, a non-UTF-8 text value) returns None and the request goes to Python.
pub fn parse_multipart(headers: &axum::http::HeaderMap, body: &[u8]) -> Option<(Form, Vec<FilePart>)> {
    let ct = headers.get(axum::http::header::CONTENT_TYPE)?.to_str().ok()?;
    let mut params = ct.split(';');
    if !params.next()?.trim().eq_ignore_ascii_case("multipart/form-data") {
        return None;
    }
    let mut boundary = None;
    for p in params {
        let (k, v) = p.trim().split_once('=')?;
        if k.trim().eq_ignore_ascii_case("boundary") {
            let v = v.trim();
            let v = v.strip_prefix('"').and_then(|x| x.strip_suffix('"')).unwrap_or(v);
            if v.is_empty() || v.contains(['"', '\\']) {
                return None;
            }
            boundary = Some(v.to_string());
        } else {
            return None;
        }
    }
    let delim = format!("--{}", boundary?).into_bytes();
    let mut rest = body.strip_prefix(delim.as_slice())?.strip_prefix(b"\r\n")?;
    let sep = [b"\r\n".as_slice(), &delim].concat();
    let (mut fields, mut files) = (vec![], vec![]);
    loop {
        let end = find(rest, &sep)?;
        let (part, after) = (&rest[..end], &rest[end + sep.len()..]);
        let head_end = find(part, b"\r\n\r\n")?;
        let (head, data) = (&part[..head_end], &part[head_end + 4..]);
        // header values are UTF-8 to Starlette (`_user_safe_decode`); other bytes are Python's
        let (mut name, mut filename) = (None, None);
        for line in std::str::from_utf8(head).ok()?.split("\r\n") {
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
        let name = name?;
        match filename {
            Some(filename) => files.push(FilePart { name, filename, len: data.len(), data: data.to_vec() }),
            None => fields.push((name, std::str::from_utf8(data).ok()?.to_string())),
        }
        if let Some(tail) = after.strip_prefix(b"--") {
            if !(tail.is_empty() || tail == b"\r\n") {
                return None;
            }
            break;
        }
        rest = after.strip_prefix(b"\r\n")?;
    }
    if fields.len() + files.len() > 1000 {
        return None;
    }
    Some((Form(fields), files))
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

impl Form {
    /// Every value of a repeated name, in order (FormData.getlist)
    pub fn all(&self, name: &str) -> Vec<&str> {
        self.0.iter().filter(|(k, _)| k == name).map(|(_, v)| v.as_str()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{header, HeaderMap, HeaderValue};

    fn mp(body: &str) -> Option<(Form, Vec<FilePart>)> {
        let mut h = HeaderMap::new();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("multipart/form-data; boundary=XB"));
        parse_multipart(&h, body.as_bytes())
    }

    #[test]
    fn multipart_reads_browser_bodies_and_refuses_odd_ones() {
        let (f, files) = mp("--XB\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx\r\ny\r\n--XB\r\nContent-Disposition: form-data; name=\"image_file\"; filename=\"\"\r\nContent-Type: application/octet-stream\r\n\r\n\r\n--XB--\r\n").unwrap();
        assert_eq!(f.get("a"), Some("x\r\ny"));
        assert_eq!((files[0].name.as_str(), files[0].filename.as_str(), files[0].len), ("image_file", "", 0));
        assert!(mp("pre\r\n--XB\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx\r\n--XB--").is_none());
        assert!(mp("--XB\r\nContent-Disposition: form-data; name=\"a\\\"b\"\r\n\r\nx\r\n--XB--").is_none());
        assert!(mp("--XB\r\nX-Other: 1\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx\r\n--XB--").is_none());
        assert!(mp("--XB\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx").is_none());
    }
}
