//! MiniJinja over the app's real templates (`app/templates/`): the same files Python renders, so a template change keeps working on both sides.
//! Holds the template globals and filters the Rust side has taught MiniJinja so far (see `docs/RUST_PAGES.md` for what is and is not ported).
use minijinja::{escape_formatter, value::ValueKind, Environment, Error, ErrorKind, State, Value};
use minijinja::{AutoEscape, Output};
pub mod app;
pub mod difflib;
pub mod pyvalue;
pub mod rules;
pub mod md;
pub mod text;
use sha1::{Digest, Sha1};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// `asset_v('style.css')` (app/templating.py): the first 10 hex characters of the file's SHA-1, "0" when it cannot be read. Computed once per file.
fn asset_v_fn(static_dir: PathBuf) -> impl Fn(&str) -> Result<String, Error> + Send + Sync + 'static {
    let cache: Arc<Mutex<HashMap<String, String>>> = Arc::default();
    move |name: &str| {
        if let Some(v) = cache.lock().map_err(|_| Error::new(ErrorKind::InvalidOperation, "asset cache"))?.get(name) {
            return Ok(v.clone());
        }
        let digest = std::fs::read(static_dir.join(name))
            .map(|b| Sha1::digest(&b).iter().map(|x| format!("{x:02x}")).collect::<String>()[..10].to_string())
            .unwrap_or_else(|_| "0".to_string());
        cache.lock().map_err(|_| Error::new(ErrorKind::InvalidOperation, "asset cache"))?.insert(name.to_string(), digest.clone());
        Ok(digest)
    }
}

/// Jinja2's (markupsafe's) HTML escaping: `& < > ' "` become `&amp; &lt; &gt; &#39; &#34;`. MiniJinja's own also escapes `/` (and spells the quotes
/// differently) - the same meaning in HTML, but a different page for a byte-for-byte comparison with what Python renders.
pub fn markupsafe_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '\'' => out.push_str("&#39;"),
            '"' => out.push_str("&#34;"),
            c => out.push(c),
        }
    }
    out
}

fn formatter(out: &mut Output, state: &State, value: &Value) -> Result<(), Error> {
    if matches!(state.auto_escape(), AutoEscape::Html) && value.kind() == ValueKind::String && !value.is_safe() {
        return write!(out, "{}", markupsafe_escape(value.as_str().unwrap_or(""))).map_err(|_| Error::new(ErrorKind::WriteFailure, "write"));
    }
    escape_formatter(out, state, value)
}

/// The current UTC minute, for [reveal=...] blocks.
pub fn now_minute() -> nd_perm::gmonly::Minute {
    nd_perm_now()
}

fn nd_perm_now() -> nd_perm::gmonly::Minute {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    (y, m, d, rem / 3600, rem % 3600 / 60)
}

/// Jinja2's `truncate(s, length=255, killwords=False, end="...", leeway=5)`.
fn jinja_truncate(s: String, length: Option<usize>, killwords: Option<bool>, end: Option<String>, leeway: Option<usize>) -> String {
    let (length, killwords, end, leeway) = (length.unwrap_or(255), killwords.unwrap_or(false), end.unwrap_or_else(|| "...".into()), leeway.unwrap_or(5));
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= length + leeway {
        return s;
    }
    let cut: String = chars[..length.saturating_sub(end.chars().count())].iter().collect();
    if killwords {
        return cut + &end;
    }
    match cut.rsplit_once(' ') {
        Some((head, _)) => head.to_string() + &end,
        None => cut + &end,
    }
}

static PATHS: std::sync::OnceLock<(PathBuf, PathBuf)> = std::sync::OnceLock::new();

/// Where `thumb` looks: the app's `static/` and the uploads folder (set once by the server at start).
pub fn set_paths(static_dir: PathBuf, uploads_dir: PathBuf) {
    let _ = PATHS.set((static_dir, uploads_dir));
}

fn mtime_ns(p: &Path) -> Option<u128> {
    std::fs::metadata(p).ok()?.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok().map(|d| d.as_nanos())
}

/// `fromjson`: json.loads(s) if s else [] (bad JSON is an error, as in Python: the page goes to Python)
fn fromjson(s: Value) -> Result<Value, Error> {
    if !s.is_true() {
        return Ok(Value::from(Vec::<Value>::new()));
    }
    let Some(t) = s.as_str() else { return Err(Error::new(ErrorKind::InvalidOperation, "fromjson: not text")) };
    let v: serde_json::Value = serde_json::from_str(t).map_err(|_| Error::new(ErrorKind::InvalidOperation, "fromjson: bad JSON"))?;
    Ok(pyvalue::to_value(&v))
}

/// `templating.display_url`: an uploaded picture's 2,560 px display copy (`?display=1`); map pictures and small copies keep their URL
/// (a URL with a line break, where Python's `$` behaves differently, is left to Python: an error here)
pub fn display_url(url: Value) -> Result<Value, Error> {
    let Some(u) = url.as_str() else { return Ok(url) };
    if u.contains('\n') {
        return Err(Error::new(ErrorKind::InvalidOperation, "display: line break in URL"));
    }
    let l = u.to_ascii_lowercase();
    let ok = l.starts_with("/uploads/")
        && !l.starts_with("/uploads/maps/")
        && !u.contains(['?', '#'])
        && [".jpg", ".jpeg", ".png", ".webp", ".avif"].iter().any(|e| l.len() > "/uploads/".len() + e.len() && l.ends_with(e))
        && !["_thumb.webp", "_thumb.avif", "_display.webp", "_display.avif"].iter().any(|e| l.ends_with(e));
    Ok(if ok { Value::from(format!("{u}?display=1")) } else { url })
}

/// `templating.thumb_url`: the picture's small copy when it exists (and, for bundled / map pictures, is fresh), else the URL unchanged.
/// A bundled or map picture without a fresh copy makes Python queue one: that page is left to Python (an error here).
/// `templating.thumb_url` for page builders (None = a picture whose small copy Python would queue: the page is Python's)
pub fn thumb_url(url: &str) -> Option<String> {
    thumb(Some(url.to_string())).ok()?.as_str().map(|s| s.to_string())
}

/// `|thumb`: text or nothing; anything else fails in Python (str methods on it), so it is an error here too and the page is Python's
fn thumb_filter(url: Value) -> Result<Value, Error> {
    if !url.is_true() {
        return Ok(url); // `if not url: return url`
    }
    match url.as_str() {
        Some(s) => thumb(Some(s.to_string())),
        None => Err(Error::new(ErrorKind::InvalidOperation, "thumb: not text")),
    }
}

fn thumb(url: Option<String>) -> Result<Value, Error> {
    let url = url.unwrap_or_default();
    if url.is_empty() {
        return Ok(Value::from(url));
    }
    let Some((static_dir, uploads_dir)) = PATHS.get() else { return Err(Error::new(ErrorKind::InvalidOperation, "thumb paths not set")) };
    let path = url.split('?').next().unwrap_or("");
    let (root, prefix) = if let Some(_) = path.strip_prefix("/uploads/") {
        (uploads_dir, "/uploads/")
    } else if path.starts_with("/static/maps/") || path.starts_with("/static/races/") {
        (static_dir, "/static/")
    } else {
        return Ok(Value::from(url));
    };
    let Ok(root) = std::fs::canonicalize(root) else { return Ok(Value::from(url)) };
    let rel = percent_decode(&path[prefix.len()..]);
    let src = match std::fs::canonicalize(root.join(&rel)) {
        Ok(p) => p,
        Err(_) => root.join(&rel), // Python's resolve() does not need the file to exist
    };
    if !src.starts_with(&root) || rel.split('/').any(|c| c == "..") {
        return Ok(Value::from(url));
    }
    let stem = src.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let existing = ["_thumb.avif", "_thumb.webp"].iter().map(|suf| src.with_file_name(format!("{stem}{suf}"))).find(|p| p.is_file());
    if prefix == "/static/" || path.starts_with("/uploads/maps/") {
        let fresh = match (&existing, mtime_ns(&src)) {
            (Some(t), Some(s)) => mtime_ns(t).map_or(false, |tm| tm >= s),
            _ => false,
        };
        if !fresh {
            if src.is_file() {
                // Python's `static_thumbs.request(src)`: the thumbnail made in the background, the original shown this once. Set by a gateway that has
                // no Python to do it; otherwise the page is Python's
                if let Some(queue) = THUMB_QUEUE.get() {
                    queue(&src);
                    return Ok(Value::from(url));
                }
                return Err(Error::new(ErrorKind::InvalidOperation, "a thumbnail would be queued: Python's job"));
            }
            return Ok(Value::from(url));
        }
        let t = existing.unwrap();
        let rel_t = t.strip_prefix(&root).map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
        return Ok(Value::from(format!("{prefix}{rel_t}?v={}", mtime_ns(&t).unwrap_or(0) / 1_000_000)));
    }
    match existing {
        Some(t) => Ok(Value::from(format!("/uploads/{}", t.strip_prefix(&root).map(|p| p.to_string_lossy().to_string()).unwrap_or_default()))),
        None => Ok(Value::from(url)),
    }
}

/// Who makes a missing bundled / map-picture thumbnail (the gateway, when it runs without Python)
pub static THUMB_QUEUE: std::sync::OnceLock<fn(&std::path::Path)> = std::sync::OnceLock::new();

fn percent_decode(s: &str) -> String {
    // URLs in the database are written by the app unencoded; a "%" is taken literally, as Python's Path does
    s.to_string()
}

fn nd(state: &State, key: &str) -> Value {
    state.lookup("__nd").and_then(|v| v.get_attr(key).ok()).unwrap_or(Value::UNDEFINED)
}

/// `deps.with_world(path, ctx["world"])`: `?w=<slug>` (or `&w=`) appended when the page has a world.
fn wq(state: &State, path: String) -> String {
    let slug = state.lookup("world").filter(|w| w.is_true()).and_then(|w| w.get_attr("slug").ok()).map(|s| s.to_string());
    match slug {
        Some(slug) => format!("{path}{}w={slug}", if path.contains('?') { "&" } else { "?" }),
        None => path,
    }
}

/// Jinja2's `urlencode` for a string: `urllib.parse.quote(s, safe="/")` on the UTF-8 bytes.
fn py_urlencode(v: Value) -> String {
    let s = v.to_string();
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~/".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Python's `json.dumps(x, sort_keys=True)` (ensure_ascii, ", " / ": ") made HTML-safe the way Jinja2's `tojson` does.
pub fn py_json(v: &serde_json::Value, out: &mut String) {
    py_json_impl(v, out, true)
}

/// Python's `repr(float)`: the shortest round-trip digits, fixed notation for exponents -4..15, else "1.5e+16" / "1e-07"
pub fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    let sci = format!("{f:e}"); // shortest round-trip digits: "-1.2345e-7"
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let (neg, mant) = match mant.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mant),
    };
    let digits: String = mant.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if neg { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let point = exp + 1; // digits before the decimal point
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if (point as usize) >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        format!("{sign}{body}")
    } else {
        let m = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits.clone() };
        let es = if exp < 0 { "-" } else { "+" };
        format!("{sign}{m}e{es}{:02}", exp.abs())
    }
}

/// FastAPI's JSONResponse body: `json.dumps(x, ensure_ascii=False, separators=(",", ":"))` - keys in insertion order, only control
/// characters escaped, floats as Python's `repr`
pub fn py_dumps_compact(v: &serde_json::Value) -> String {
    let mut out = String::new();
    compact_impl(v, &mut out);
    out
}

fn compact_impl(v: &serde_json::Value, out: &mut String) {
    match v {
        serde_json::Value::String(s) => {
            out.push('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    '\u{8}' => out.push_str("\\b"),
                    '\u{c}' => out.push_str("\\f"),
                    c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                    c => out.push(c),
                }
            }
            out.push('"');
        }
        serde_json::Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                compact_impl(x, out);
            }
            out.push(']');
        }
        serde_json::Value::Object(o) => {
            out.push('{');
            for (i, (k, x)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                compact_impl(&serde_json::Value::String(k.clone()), out);
                out.push(':');
                compact_impl(x, out);
            }
            out.push('}');
        }
        other => py_json_impl(other, out, false),
    }
}

/// Python's plain `json.dumps(x)`: keys in insertion order, otherwise as `py_json`
/// `json.dumps(v, indent=n)`: one item per line, "," between items and ": " after keys, ASCII only, insertion order
pub fn py_dumps_indent(v: &serde_json::Value, indent: usize) -> String {
    py_dumps_indent_as(v, indent, true)
}

/// `json.dumps(v, indent=n, ensure_ascii=...)`: with `ensure_ascii` false, text beyond ASCII is written as it is (control characters are
/// still escaped)
pub fn py_dumps_indent_as(v: &serde_json::Value, indent: usize, ensure_ascii: bool) -> String {
    fn text(s: &str, ensure_ascii: bool, out: &mut String) {
        if ensure_ascii {
            py_json_impl(&serde_json::Value::String(s.to_string()), out, false);
            return;
        }
        out.push('"');
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
    }
    fn go(v: &serde_json::Value, indent: usize, level: usize, out: &mut String, ea: bool) {
        let pad = |l: usize| " ".repeat(indent * l);
        match v {
            serde_json::Value::Array(a) if !a.is_empty() => {
                out.push_str("[\n");
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(",\n");
                    }
                    out.push_str(&pad(level + 1));
                    go(x, indent, level + 1, out, ea);
                }
                out.push('\n');
                out.push_str(&pad(level));
                out.push(']');
            }
            serde_json::Value::Object(o) if !o.is_empty() => {
                out.push_str("{\n");
                for (i, (k, x)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push_str(",\n");
                    }
                    out.push_str(&pad(level + 1));
                    text(k, ea, out);
                    out.push_str(": ");
                    go(x, indent, level + 1, out, ea);
                }
                out.push('\n');
                out.push_str(&pad(level));
                out.push('}');
            }
            serde_json::Value::String(s) => text(s, ea, out),
            other => py_json_impl(other, out, false),
        }
    }
    let mut out = String::new();
    go(v, indent, 0, &mut out, ensure_ascii);
    out
}

pub fn py_dumps(v: &serde_json::Value) -> String {
    let mut out = String::new();
    py_json_impl(v, &mut out, false);
    out
}

fn py_json_impl(v: &serde_json::Value, out: &mut String, sort_keys: bool) {
    match v {
        serde_json::Value::Null => out.push_str("null"),
        serde_json::Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        serde_json::Value::Number(n) => match n.as_f64().filter(|_| n.is_f64()) {
            Some(f) => out.push_str(&py_float_repr(f)),
            None => out.push_str(&n.to_string()),
        },
        serde_json::Value::String(s) => {
            out.push('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    '\u{8}' => out.push_str("\\b"),
                    '\u{c}' => out.push_str("\\f"),
                    c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                        let mut buf = [0u16; 2];
                        for u in c.encode_utf16(&mut buf) {
                            out.push_str(&format!("\\u{:04x}", u));
                        }
                    }
                    c => out.push(c),
                }
            }
            out.push('"');
        }
        serde_json::Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json_impl(x, out, sort_keys);
            }
            out.push(']');
        }
        serde_json::Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            if sort_keys {
                keys.sort();
            }
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json_impl(&serde_json::Value::String((*k).clone()), out, sort_keys);
                out.push_str(": ");
                py_json_impl(&o[*k], out, sort_keys);
            }
            out.push('}');
        }
    }
}

fn py_tojson(v: Value) -> Result<Value, Error> {
    let j: serde_json::Value = serde_json::to_value(&v).map_err(|e| Error::new(ErrorKind::InvalidOperation, e.to_string()))?;
    let mut s = String::new();
    py_json(&j, &mut s);
    let s = s.replace('<', "\\u003c").replace('>', "\\u003e").replace('&', "\\u0026").replace('\'', "\\u0027");
    Ok(Value::from_safe_string(s))
}

/// An environment that loads templates from `templates_dir` on demand; `static_dir` is where `asset_v` looks.
pub fn environment(templates_dir: &Path, static_dir: &Path) -> Environment<'static> {
    let mut env = Environment::new();
    env.set_loader(minijinja::path_loader(templates_dir.to_path_buf()));
    env.set_formatter(formatter);
    env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);
    env.add_filter("tojson", py_tojson);
    env.add_filter("urlencode", py_urlencode);
    env.add_filter("wq", wq);
    // per-render answers the page builder put in the context under "__nd" (site_layout, can_edit, saved views)
    env.add_function("site_layout", |state: &State, _req: Value| -> Result<Value, Error> { Ok(nd(state, "site_layout")) });
    // markdown: pulldown-cmark plus the app's own tags (md.rs) - the owner chose CommonMark's behaviour over markdown2's (docs/RUST_PAGES.md)
    env.add_filter("md", |text: Option<String>| Value::from_safe_string(md::render_md(text.as_deref().unwrap_or(""))));
    env.add_filter("md_for", |state: &State, text: Option<String>, _req: Value| -> Result<Value, Error> {
        let text = text.unwrap_or_default();
        let gm = state.lookup("request").and_then(|r| r.get_attr("state").ok()?.get_attr("user").ok()?.get_attr("is_gm").ok()).map_or(false, |v| v.is_true());
        if gm {
            return Ok(Value::from_safe_string(md::render_md(&text)));
        }
        let viewer = state.lookup("request").and_then(|r| r.get_attr("state").ok()?.get_attr("user").ok()?.get_attr("id").ok()).and_then(|v| i64::try_from(v).ok());
        let now = nd_perm_now();
        match nd_perm::gmonly::strip_gm_only(&text, viewer, now) {
            Some(t) => Ok(Value::from_safe_string(md::render_md(&t))),
            None => Err(Error::new(ErrorKind::InvalidOperation, "strip_gm_only: not sure, let Python render")),
        }
    });
    env.add_filter("body_summary", |t: Option<String>| text::body_summary(t.as_deref().unwrap_or("")));
    env.add_filter("strip_md", |t: Option<String>| text::strip_md(t.as_deref().unwrap_or("")));
    env.add_filter("entry_text", |t: Option<String>| text::entry_text(t.as_deref().unwrap_or("")));
    env.add_filter("parse_stats", |t: Option<String>| Value::from_serialize(text::parse_stats(t.as_deref().unwrap_or(""))));
    env.add_filter("truncate", jinja_truncate);
    // markupsafe's striptags: comments and tags out, whitespace collapsed, entities decoded
    env.add_filter("striptags", |v: String| -> String {
        static C: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        static T: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        let v = C.get_or_init(|| regex::Regex::new(r"(?s)<!--.*?-->").unwrap()).replace_all(&v, "");
        let v = T.get_or_init(|| regex::Regex::new(r"(?s)<.*?>").unwrap()).replace_all(&v, "");
        let v = v.split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ");
        html_escape::decode_html_entities(&v).into_owned()
    });
    env.add_filter("thumb", thumb_filter);
    env.add_filter("display", display_url);
    env.add_filter("fromjson", fromjson);
    env.add_function("thumb_url", thumb);
    // the random-table list's per-row edit check, worked out by the page builder (rust/nd-gateway/src/pages.rs tables_list)
    env.add_function("can_edit_table", |t: Value| -> bool { t.get_attr("__nd_can_edit").map_or(false, |v| v.is_true()) });
    env.add_function("view_spec", |section: String| -> Value {
        Value::from_serialize(app::data()["view_specs"].get(&section).cloned().unwrap_or(serde_json::json!({})))
    });
    // the account page's layout panel (templating.py: all_view_specs, view_titles, layout_presets - the presets in Python's key order)
    env.add_function("all_view_specs", || -> Value { Value::from_serialize(app::data()["view_specs"].clone()) });
    env.add_function("view_titles", || -> Value { Value::from_serialize(app::data()["view_titles"].clone()) });
    let presets: serde_json::Value = app::data()["view_presets_text"].as_str().and_then(|t| serde_json::from_str(t).ok()).unwrap_or(serde_json::json!({}));
    env.add_global("layout_presets", Value::from_serialize(presets));
    env.add_function("can_edit", |state: &State, _req: Value| -> Result<Value, Error> { Ok(nd(state, "can_edit")) });
    env.add_function("saved_view", |state: &State, _req: Value, section: String| -> Result<Value, Error> {
        let v = nd(state, "saved_views");
        if v.is_none() || v.is_undefined() {
            return Ok(Value::from(()));
        }
        Ok(v.get_attr(&section).ok().filter(|x| !x.is_undefined()).unwrap_or_else(|| Value::from_serialize(serde_json::json!({}))))
    });
    env.add_function("asset_v", asset_v_fn(static_dir.to_path_buf()));
    env
}
