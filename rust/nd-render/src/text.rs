//! app/rendering.py's text helpers for lists and cards: `parse_stats`, `body_summary`, `entry_text` - line-by-line ports, checked against
//! Python with random and real bodies (tests/test_rust_layout.py).
use fancy_regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;

macro_rules! re {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| Regex::new($p).expect("pattern"))
    }};
}

fn py_ws(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_ws)
}

/// `str.splitlines()`
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let b: Vec<(usize, char)> = s.char_indices().collect();
    let mut i = 0;
    while i < b.len() {
        let (pos, c) = b[i];
        let brk = matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}');
        if brk {
            out.push(&s[start..pos]);
            let mut next = pos + c.len_utf8();
            if c == '\r' && i + 1 < b.len() && b[i + 1].1 == '\n' {
                next += 1;
                i += 1;
            }
            start = next;
        }
        i += 1;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

fn m(re: &Regex, s: &str) -> bool {
    re.is_match(s).unwrap_or(false)
}

fn decode(t: &str) -> String {
    t.replace("&#39;", "'").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
}

/// `re.sub(r'\s+', ' ', t)` with Python's (Unicode) `\s`
fn collapse_ws(t: &str) -> String {
    let mut out = String::new();
    let mut ws = false;
    for c in t.chars() {
        if py_ws(c) {
            if !ws {
                out.push(' ');
            }
            ws = true;
        } else {
            out.push(c);
            ws = false;
        }
    }
    out
}

pub fn entry_text(body: &str) -> String {
    if body.is_empty() {
        return String::new();
    }
    let mut in_entry = false;
    let mut chunks: Vec<String> = vec![];
    for ln in py_splitlines(body) {
        if m(re!(r"(?i)^##\s+Entry"), ln) {
            in_entry = true;
            continue;
        }
        if in_entry && ln.starts_with("##") {
            break;
        }
        if !in_entry || m(re!(r"^(---|!\[|\s*$)"), ln) || ln.starts_with('|') {
            continue;
        }
        let ln = re!(r"\\\*\\\*").replace_all(ln, "");
        let ln = re!(r"\\-").replace_all(&ln, "-");
        let ln = re!(r"\*{1,2}([^*]+)\*{1,2}").replace_all(&ln, "$1");
        let ln = re!(r"\[([^\]]+)\]\([^)]+\)").replace_all(&ln, "$1");
        let ln = re!(r"^[-*]\s+").replace(&ln, "");
        chunks.push(py_strip(&ln).to_string());
    }
    let text = chunks.into_iter().filter(|c| !c.is_empty()).collect::<Vec<_>>().join(" ");
    decode(py_strip(&collapse_ws(&text)))
}

const STAT_SKIP: &[&str] = &["visibility", "type", "physical stats", "other", "costs", ""];
const STAT_WANT: &[&str] = &["damage", "rarity", "armor", "cost", "special conditions", "effect", "requirement", "requirements", "range", "rounds",
    "strength", "power", "speed", "feats", "capacity", "augment slots", "max health", "max pp"];
const STAT_TABLE_SKIP: &[&str] = &["visibility", "physical stats", "other", "costs"];

/// Python's `str.lower()` for the keys here (ASCII and the common cases; Unicode special casing differs only for rare letters).
fn lower(s: &str) -> String {
    s.to_lowercase()
}

pub fn body_summary(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let mut pairs: Vec<String> = vec![];
    let mut special: Option<String> = None;
    for ln in py_splitlines(text) {
        if ln.starts_with('|') {
            continue;
        }
        let caps = re!(r"^\*\s+\*\*([^*]+)\*\*[:\s]\s*(.+)").captures(ln).ok().flatten()
            .or_else(|| re!(r"^\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.+)").captures(ln).ok().flatten());
        let Some(c) = caps else { continue };
        let key_raw = py_strip(&c[1]).to_string();
        let key = lower(&key_raw);
        let v = re!(r"\\\*\\\*").replace_all(&c[2], "");
        let v = py_strip(py_strip(&v).trim_end_matches('\\'));
        let val = decode(v);
        if val.is_empty() || val.starts_with('{') || STAT_SKIP.contains(&key.as_str()) {
            continue;
        }
        if key == "special conditions" {
            special = Some(val.chars().take(220).collect());
        } else if STAT_WANT.contains(&key.as_str()) && pairs.len() < 4 {
            pairs.push(format!("{key_raw}: {val}"));
        }
    }
    if let Some(s) = special.filter(|s| !s.is_empty()) {
        return s;
    }
    let joined = pairs.join("  ·  ");
    if !joined.is_empty() {
        return joined;
    }
    entry_text(text)
}

fn clean_val(v: &str) -> String {
    let v = re!(r"\\\*\\\*").replace_all(v, "");
    let v = re!(r"\*{1,2}([^*]+)\*{1,2}").replace_all(&v, "$1");
    py_strip(&v).to_string()
}

pub fn parse_stats(body: &str) -> Vec<Value> {
    if body.is_empty() {
        return vec![];
    }
    let lines = py_splitlines(body);
    let mut in_section = false;
    let mut rows = vec![];
    let mut i = 0;
    while i < lines.len() {
        let ln = lines[i];
        i += 1;
        if m(re!(r"(?i)^##\s+(Attributes|Entry|Profile)"), ln) {
            in_section = true;
            continue;
        }
        if in_section && ln.starts_with("##") {
            break;
        }
        if !in_section || m(re!(r"^(---|!\[|\s*$)"), ln) {
            continue;
        }
        let mut kv: Option<(String, String)> = None;
        for rx in [re!(r"^\*\s+\*\*([^*\[]+)(?:\[[^\]]*\])?\*\*[:\s]\s*(.*)"), re!(r"^\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.*)"), re!(r"^\*\*([^*:]+)\*\*[:\s]\s*(.*)")] {
            if let Ok(Some(c)) = rx.captures(ln) {
                let k = py_strip(&c[1]).to_string();
                if !k.is_empty() {
                    kv = Some((k, py_strip(&c[2]).to_string()));
                    break;
                }
            }
        }
        if kv.is_none() {
            if let Ok(Some(c)) = re!(r"^([A-Z][A-Za-z ()]{1,38}):\s*(.*)").captures(py_strip(ln)) {
                let (k, mut v) = (py_strip(&c[1]).to_string(), py_strip(&c[2]).to_string());
                if v.is_empty() {
                    let mut j = i;
                    while j < lines.len() && py_strip(lines[j]).is_empty() {
                        j += 1;
                    }
                    if j < lines.len() && !m(re!(r"^##|^---|^\*\s+\*\*|^\\\*\\\*"), lines[j]) {
                        v = py_strip(lines[j]).to_string();
                        i = j + 1;
                    }
                }
                kv = Some((k, v));
            }
        }
        let Some((key, val)) = kv else { continue };
        if key.is_empty() || val.is_empty() {
            continue;
        }
        let val = clean_val(&val);
        if val.is_empty() || val.starts_with('{') || STAT_TABLE_SKIP.contains(&lower(&key).as_str()) {
            continue;
        }
        rows.push(json!({"key": key, "val": val, "special": lower(&key) == "special conditions"}));
    }
    rows
}
