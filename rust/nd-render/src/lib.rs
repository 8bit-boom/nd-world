//! MiniJinja over the app's real templates (`app/templates/`): the same files Python renders, so a template change keeps working on both sides.
//! Holds the template globals and filters the Rust side has taught MiniJinja so far (see `docs/RUST_PAGES.md` for what is and is not ported).
use minijinja::{escape_formatter, value::ValueKind, Environment, Error, ErrorKind, State, Value};
use minijinja::{AutoEscape, Output};
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
        use std::fmt::Write;
        return write!(out, "{}", markupsafe_escape(value.as_str().unwrap_or(""))).map_err(|_| Error::new(ErrorKind::WriteFailure, "write"));
    }
    escape_formatter(out, state, value)
}

/// An environment that loads templates from `templates_dir` on demand; `static_dir` is where `asset_v` looks.
pub fn environment(templates_dir: &Path, static_dir: &Path) -> Environment<'static> {
    let mut env = Environment::new();
    env.set_loader(minijinja::path_loader(templates_dir.to_path_buf()));
    env.set_formatter(formatter);
    env.add_function("asset_v", asset_v_fn(static_dir.to_path_buf()));
    env
}
