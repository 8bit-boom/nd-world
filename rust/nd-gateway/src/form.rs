//! Request bodies as FastAPI reads them: `request.form()` for application/x-www-form-urlencoded (Starlette's FormParser) and
//! `json.loads(body)`. Only the plain cases are read here - anything else returns None and the request goes to Python.

use percent_encoding::percent_decode_str;

/// A urlencoded form: (name, value) pairs in order; `get` is Starlette's FormData.get (the last value of a repeated name)
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
