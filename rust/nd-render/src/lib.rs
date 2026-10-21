//! MiniJinja over the app's real templates (`app/templates/`): the same files Python renders, so a template change keeps working on both sides.
//! Holds the template globals and filters the Rust side has taught MiniJinja so far (see `docs/RUST_PAGES.md` for what is and is not ported).
use minijinja::{escape_formatter, value::ValueKind, Environment, Error, ErrorKind, State, Value};
use minijinja::{AutoEscape, Output};
pub mod app;
pub mod pyvalue;
pub mod md;
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
    match v {
        serde_json::Value::Null => out.push_str("null"),
        serde_json::Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        serde_json::Value::Number(n) => out.push_str(&n.to_string()),
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
                py_json(x, out);
            }
            out.push(']');
        }
        serde_json::Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json(&serde_json::Value::String((*k).clone()), out);
                out.push_str(": ");
                py_json(&o[*k], out);
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
    env.add_function("view_spec", |section: String| -> Value {
        Value::from_serialize(app::data()["view_specs"].get(&section).cloned().unwrap_or(serde_json::json!({})))
    });
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
