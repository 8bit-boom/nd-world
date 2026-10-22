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

/// `" ".join(s.split())`: runs of whitespace to one space, none at the ends
pub fn py_split_join(s: &str) -> String {
    s.split(py_ws).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
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

// ── the entity page: autolinks, headings, sections ──────────────────────────

fn all_capitalized(w: &str) -> bool {
    w.chars().next().map_or(false, |f| f.is_ascii_uppercase()) // _ALL_CAPITALIZED_RE = ^[A-Z]
}

/// `rendering.derive_name_variants`
pub fn derive_name_variants(name: &str) -> Vec<String> {
    let name = py_strip(name);
    if name.is_empty() {
        return vec![];
    }
    let mut variants: Vec<String> = vec![];
    let core = py_strip(name.split(',').next().unwrap_or("")).to_string();
    if !core.is_empty() && core != name {
        variants.push(core.clone());
    }
    let words: Vec<&str> = core.split(py_ws).filter(|w| !w.is_empty()).collect();
    if words.len() >= 3 {
        let short = words[words.len() - 2..].join(" ");
        if short != core && !variants.contains(&short) {
            variants.push(short);
        }
    }
    variants.into_iter().filter(|v| v.split(py_ws).filter(|w| !w.is_empty()).all(all_capitalized)).collect()
}

/// `rendering.autolink_entities(html, names)`; `names` in the dict's insertion order. `None` = the names hold something this port does not
/// promise to match the same way (non-ASCII letters, whose case folding Python and Rust may treat differently).
pub fn autolink_entities(html: &str, names: &[(String, i64)]) -> Option<String> {
    if html.is_empty() || names.is_empty() {
        return Some(html.to_string());
    }
    let mut lower_to_id: Vec<(String, i64)> = vec![];
    for (name, id) in names {
        let key = py_strip(name).to_lowercase();
        if key.chars().count() < 3 {
            continue;
        }
        if !key.is_ascii() {
            return None;
        }
        if !lower_to_id.iter().any(|(k, _)| *k == key) {
            lower_to_id.push((key, *id));
        }
    }
    if lower_to_id.is_empty() {
        return Some(html.to_string());
    }
    let mut ordered: Vec<&String> = lower_to_id.iter().map(|(k, _)| k).collect();
    ordered.sort_by(|a, b| b.chars().count().cmp(&a.chars().count())); // stable, longest first (Python's sorted(key=len, reverse=True))
    let alt = ordered.iter().map(|n| fancy_regex::escape(n).into_owned()).collect::<Vec<_>>().join("|");
    let pattern = Regex::new(&format!(r"(?i)(?<![A-Za-z0-9_])({alt})(?![A-Za-z0-9_])")).ok()?;
    let tag_name = re!(r"^</?\s*([a-zA-Z0-9]+)");
    let mut skip_depth: i64 = 0;
    let mut out = String::new();
    // re.split with a capturing group: text, tag, text, tag ...
    let mut last = 0;
    let mut chunks: Vec<&str> = vec![];
    for m in re!(r"(<[^>]+>)").find_iter(html) {
        let m = m.ok()?;
        chunks.push(&html[last..m.start()]);
        chunks.push(m.as_str());
        last = m.end();
    }
    chunks.push(&html[last..]);
    for chunk in chunks {
        if chunk.is_empty() {
            continue;
        }
        if chunk.starts_with('<') {
            out.push_str(chunk);
            if let Ok(Some(t)) = tag_name.captures(chunk) {
                if ["style", "pre", "script", "a", "code"].contains(&t[1].to_lowercase().as_str()) {
                    if chunk.starts_with("</") {
                        skip_depth = (skip_depth - 1).max(0);
                    } else if !chunk.ends_with("/>") {
                        skip_depth += 1;
                    }
                }
            }
            continue;
        }
        if skip_depth > 0 {
            out.push_str(chunk);
            continue;
        }
        let replaced = pattern.replace_all(chunk, |c: &fancy_regex::Captures| {
            let found = &c[0];
            let id = lower_to_id.iter().find(|(k, _)| *k == found.to_lowercase()).map(|(_, i)| *i).unwrap_or(0);
            format!(r#"<a href="/entity/{id}" class="auto-entity-link">{found}</a>"#)
        });
        out.push_str(&replaced);
    }
    Some(out)
}

fn strip_tags_unescape(inner: &str) -> String {
    html_escape::decode_html_entities(&re!(r"<[^>]+>").replace_all(inner, "")).into_owned()
}

/// `main._rules_toc(html, levels)` -> (html with ids, [{level, text, id}])
pub fn rules_toc(html: &str, levels: &str) -> (String, Vec<Value>) {
    let mut toc = vec![];
    let mut seen: Vec<(String, i64)> = vec![];
    let rx = Regex::new(&format!(r"(?s)<h([{levels}])>(.*?)</h\1>")).unwrap();
    let out = rx
        .replace_all(html, |c: &fancy_regex::Captures| {
            let (lvl, inner) = (&c[1], &c[2]);
            let text = strip_tags_unescape(inner);
            let lowered = text.to_lowercase();
            let mut slug = String::new();
            let mut dash = false;
            for ch in lowered.chars() {
                if ch.is_alphanumeric() || ch == '_' {
                    slug.push(ch);
                    dash = false;
                } else if !dash {
                    slug.push('-');
                    dash = true;
                }
            }
            let mut slug = slug.trim_matches('-').to_string();
            if slug.is_empty() {
                slug = "sec".into();
            }
            let count = seen.iter().find(|(s, _)| *s == slug).map(|(_, n)| *n).unwrap_or(0);
            match seen.iter_mut().find(|(s, _)| *s == slug) {
                Some(x) => x.1 += 1,
                None => seen.push((slug.clone(), 1)),
            }
            if count > 0 {
                slug = format!("{slug}-{count}");
            }
            toc.push(json!({"level": lvl.parse::<i64>().unwrap_or(0), "text": text, "id": slug}));
            format!(r#"<h{lvl} id="{slug}">{inner}</h{lvl}>"#)
        })
        .into_owned();
    (out, toc)
}

/// `rules_render.split_rules_sections(html, include_h1=True)`
pub fn split_sections_h123(html: &str) -> Vec<Value> {
    let ms: Vec<_> = re!(r#"(?s)<h([123]) id="([^"]*)">(.*?)</h[123]>"#).captures_iter(html).filter_map(|m| m.ok()).collect();
    if ms.is_empty() {
        return vec![json!({"id": null, "level": 0, "title": "", "html": html})];
    }
    let mut out = vec![];
    let first = ms[0].get(0).unwrap().start();
    let pre = &html[..first];
    if !py_strip(pre).is_empty() {
        out.push(json!({"id": null, "level": 0, "title": "", "html": pre}));
    }
    for (i, m) in ms.iter().enumerate() {
        let start = m.get(0).unwrap().start();
        let end = ms.get(i + 1).map(|n| n.get(0).unwrap().start()).unwrap_or(html.len());
        out.push(json!({"id": &m[2], "level": m[1].parse::<i64>().unwrap_or(0), "title": py_strip(&strip_tags_unescape(&m[3])), "html": &html[start..end]}));
    }
    out
}

/// `_RULES_LEGACY_ANCHOR_RE.sub("", text)`
pub fn strip_legacy_anchors(text: &str) -> String {
    re!(r#"<a\s+name="[^"]*">\s*</a>"#).replace_all(text, "").into_owned()
}

/// `rendering.strip_md`: markdown to one line of plain text (list previews).
pub fn strip_md(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let t = re!(r"!\[[^\]]*\]\([^)]+\)").replace_all(text, "");
    let t = re!(r"\[([^\]]+)\]\([^)]+\)").replace_all(&t, "$1");
    let t = re!(r"(?m)^#+\s+").replace_all(&t, "");
    let t = re!(r"\*{1,2}([^*]+)\*{1,2}").replace_all(&t, "$1");
    let t = re!(r"(?m)^\*\s*").replace_all(&t, "");
    let t = re!(r"(?m)^-{3,}$").replace_all(&t, "");
    py_strip(&collapse_ws(&t)).to_string()
}
