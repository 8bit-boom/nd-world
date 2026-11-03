//! app/rendering.py's text helpers for lists and cards: `parse_stats`, `body_summary`, `entry_text` - line-by-line ports, checked against
//! Python with random and real bodies (tests/test_rust_layout.py).
use fancy_regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;

macro_rules! re {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| fancy_regex::RegexBuilder::new($p).backtrack_limit(200_000_000).build().expect("pattern"))
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

/// `str.splitlines(keepends=True)`
pub fn py_splitlines_keepends(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let b: Vec<(usize, char)> = s.char_indices().collect();
    let mut i = 0;
    while i < b.len() {
        let (pos, c) = b[i];
        if matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            let mut next = pos + c.len_utf8();
            if c == '\r' && i + 1 < b.len() && b[i + 1].1 == '\n' {
                next += 1;
                i += 1;
            }
            out.push(&s[start..next]);
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

/// `ND_PYTHON_FREE` set (the image with no Python behind it): there is nothing to hand a request to, so a page opens rather than declines
fn python_free_env() -> bool {
    matches!(std::env::var("ND_PYTHON_FREE").unwrap_or_default().trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

/// Plain-text names turned into links: an Aho-Corasick automaton over the lower-cased names, built once per distinct list of names and kept (a page asks for the
/// same list again and again). Same answer as the alternation regex of app/rendering.py: at the leftmost place a name matches (letters in any case, not touching
/// an ASCII letter / digit / underscore on either side) the longest name that fits wins, and the search goes on after it. Text and names with other letters
/// (accents, other scripts) are matched on a copy folded one character at a time (`fold`), the matches mapped back to the original text.
struct Matcher {
    ac: aho_corasick::AhoCorasick,
    ids: Vec<i64>,
}

fn is_word(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// simple case folding: a character with a one-character lower case becomes it, any other stays (the multi-character cases such as "İ" are left as they are)
fn fold_char(c: char) -> char {
    let mut l = c.to_lowercase();
    let lower = match (l.next(), l.next()) {
        (Some(one), None) => one,
        _ => c,
    };
    // the letters that are a lower case of their own but fold together with another (Unicode's simple case folding; `re` and the regex crate treat them as equal)
    match lower {
        '\u{3c2}' => '\u{3c3}',            // final sigma -> sigma
        '\u{17f}' => 's',                  // long s
        '\u{b5}' => '\u{3bc}',             // micro sign -> mu
        '\u{3d0}' => '\u{3b2}',            // beta symbol
        '\u{3d1}' => '\u{3b8}',            // theta symbol
        '\u{3d5}' => '\u{3c6}',            // phi symbol
        '\u{3d6}' => '\u{3c0}',            // pi symbol
        '\u{3f0}' => '\u{3ba}',            // kappa symbol
        '\u{3f1}' => '\u{3c1}',            // rho symbol
        '\u{3f5}' => '\u{3b5}',            // lunate epsilon
        '\u{1e9b}' => '\u{1e61}',          // long s with dot above
        '\u{1fbe}' => '\u{3b9}',           // prosgegrammeni
        other => other,
    }
}

fn fold(s: &str) -> String {
    s.chars().map(fold_char).collect()
}

impl Matcher {
    /// `chunk` (a text node) with every name linked
    fn link(&self, chunk: &str) -> String {
        let bytes = chunk.as_bytes();
        // the text the names are looked for in: the chunk itself when it is ASCII (the automaton ignores ASCII case), else a folded copy and, for every byte offset of it
        // that starts a character, where that character starts in the original
        let (folded, back): (Option<String>, Vec<usize>) = if chunk.is_ascii() {
            (None, vec![])
        } else {
            let mut f = String::with_capacity(chunk.len());
            let mut back = Vec::with_capacity(chunk.len() + 1);
            for (i, c) in chunk.char_indices() {
                let fc = fold_char(c);
                back.resize(f.len(), usize::MAX);
                back.push(i);
                f.push(fc);
            }
            back.resize(f.len(), usize::MAX);
            back.push(chunk.len());
            (Some(f), back)
        };
        let hay: &str = folded.as_deref().unwrap_or(chunk);
        let at_orig = |o: usize| if folded.is_some() { back[o] } else { o };
        // every occurrence of every name, then the same walk the regex makes: by start, the longest first, the first one whose edges are clear, on past it
        let mut found: Vec<(usize, usize, usize)> = self.ac.find_overlapping_iter(hay).map(|m| (at_orig(m.start()), at_orig(m.end()), m.pattern().as_usize())).collect();
        if found.is_empty() {
            return chunk.to_string();
        }
        found.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.cmp(&a.1)));
        let mut out = String::with_capacity(chunk.len() + found.len() * 48);
        let (mut at, mut i) = (0usize, 0usize);
        while i < found.len() {
            let (s, e, pid) = found[i];
            if s < at {
                i += 1;
                continue;
            }
            let clear = (s == 0 || !is_word(bytes[s - 1])) && (e == bytes.len() || !is_word(bytes[e]));
            if clear {
                out.push_str(&chunk[at..s]);
                out.push_str(&format!(r#"<a href="/entity/{}" class="auto-entity-link">{}</a>"#, self.ids[pid], &chunk[s..e]));
                at = e;
            }
            i += 1;
        }
        out.push_str(&chunk[at..]);
        out
    }
}

/// the automata made so far, by the list of names they were made from (a hash of names and ids; a few worlds at most, so the whole table is dropped when it grows)
fn matcher_for(keys: &[(String, i64)]) -> Option<std::sync::Arc<Matcher>> {
    use std::collections::HashMap;
    use std::hash::{Hash, Hasher};
    use std::sync::{Arc, Mutex};
    static CACHE: OnceLock<Mutex<HashMap<u64, Arc<Matcher>>>> = OnceLock::new();
    let mut h = std::collections::hash_map::DefaultHasher::new();
    keys.hash(&mut h);
    let key = h.finish();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some(m) = cache.lock().ok()?.get(&key) {
        return Some(m.clone());
    }
    let folded: Vec<String> = keys.iter().map(|(k, _)| fold(k)).collect();
    let ac = aho_corasick::AhoCorasick::builder().ascii_case_insensitive(true).match_kind(aho_corasick::MatchKind::Standard).build(folded.iter().map(String::as_str)).ok()?;
    let m = Arc::new(Matcher { ac, ids: keys.iter().map(|(_, i)| *i).collect() });
    let mut c = cache.lock().ok()?;
    if c.len() >= 16 {
        c.clear();
    }
    c.insert(key, m.clone());
    Some(m)
}

/// `rendering.autolink_entities(html, names)`; `names` in the dict's insertion order. `None` = the names hold something this port does not
/// promise to match the same way (non-ASCII letters, whose case folding Python and Rust may treat differently) while Python is there to answer.
pub fn autolink_entities(html: &str, names: &[(String, i64)]) -> Option<String> {
    autolink_impl(html, names, python_free_env())
}

/// the same, `unicode_names` saying whether names with non-ASCII letters may be linked here
fn autolink_impl(html: &str, names: &[(String, i64)], unicode_names: bool) -> Option<String> {
    if html.is_empty() || names.is_empty() {
        return Some(html.to_string());
    }
    let mut lower_to_id: Vec<(String, i64)> = vec![];
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for (name, id) in names {
        let key = py_strip(name).to_lowercase();
        if key.chars().count() < 3 {
            continue;
        }
        if !key.is_ascii() && !unicode_names {
            return None; // next to Python: its case folding is the reference; without it the page must open (accented names are ordinary)
        }
        if seen.insert(key.clone()) {
            lower_to_id.push((key, *id));
        }
    }
    if lower_to_id.is_empty() {
        return Some(html.to_string());
    }
    let matcher = matcher_for(&lower_to_id)?;
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
        out.push_str(&matcher.link(chunk));
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

#[cfg(test)]
mod autolink_tests {
    use super::*;

    /// the alternation regex of app/rendering.py, on one text node: the reference the automaton must agree with
    fn reference(chunk: &str, keys: &[(String, i64)]) -> String {
        let mut ordered: Vec<&(String, i64)> = keys.iter().collect();
        ordered.sort_by(|a, b| b.0.chars().count().cmp(&a.0.chars().count()));
        let alt = ordered.iter().map(|(n, _)| fancy_regex::escape(n).into_owned()).collect::<Vec<_>>().join("|");
        let rx = fancy_regex::Regex::new(&format!(r"(?i)(?<![A-Za-z0-9_])({alt})(?![A-Za-z0-9_])")).unwrap();
        rx.replace_all(chunk, |c: &fancy_regex::Captures| {
            let found = &c[0];
            let id = keys.iter().find(|(k, _)| *k == found.to_lowercase()).map(|(_, i)| *i).unwrap();
            format!(r#"<a href="/entity/{id}" class="auto-entity-link">{found}</a>"#)
        })
        .into_owned()
    }

    fn names(list: &[&str]) -> Vec<(String, i64)> {
        list.iter().enumerate().map(|(i, n)| (n.to_string(), i as i64 + 1)).collect()
    }

    #[test]
    fn the_automaton_links_like_the_regex() {
        let n = names(&["Bob", "Bob Smith", "Rusty Anchor", "anchor", "ash court", "Ash", "a_b_c", "x-ray"]);
        let keys: Vec<(String, i64)> = n.iter().map(|(k, i)| (k.to_lowercase(), *i)).collect();
        for text in [
            "Bob went to the Rusty Anchor with Bob Smith.",
            "bobby is not bob; BOB is. Bob's hat, bob_x, xbob, 2bob, bob2, (Bob)",
            "ash court and Ash and ashes and Ash-Court and ASH COURT.",
            "anchor, Anchor, ANCHORS, the Rusty Anchors, Rusty Anchor.",
            "a_b_c and a_b_cd and xa_b_c and X-Ray and x-rays and x-ray",
            "BobBob Bob Bob Smithson Bob Smith's Bob  Smith",
            "",
            "no names here at all",
        ] {
            assert_eq!(autolink_entities(text, &n).unwrap(), reference(text, &keys), "{text}");
        }
    }

    #[test]
    fn many_random_texts_agree() {
        let pool = ["Bob", "Bob Smith", "Ash Court", "Ashen", "ash", "Tide", "Tidewater", "Raven", "Ravenna", "Lantern Street", "lan", "Hali"];
        let n = names(&pool);
        let keys: Vec<(String, i64)> = n.iter().filter(|(k, _)| k.len() >= 3).map(|(k, i)| (k.to_lowercase(), *i)).collect();
        let glue = [" ", " ", ", ", ". ", "-", "_", "x", "7", "'s ", "\n"];
        let mut seed = 12345u64;
        let mut next = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 33) as usize
        };
        for _ in 0..400 {
            let mut text = String::new();
            for _ in 0..(5 + next() % 30) {
                text.push_str(pool[next() % pool.len()]);
                if next() % 3 == 0 {
                    text = text.to_uppercase();
                }
                text.push_str(glue[next() % glue.len()]);
            }
            assert_eq!(autolink_entities(&text, &n).unwrap(), reference(&text, &keys), "{text}");
        }
    }

    #[test]
    fn names_with_accents_and_other_scripts_match_like_the_regex() {
        let n = names(&["Éowyn", "Zoë Quinn", "Ünïcode Straße", "Ashen", "Ørsted", "Tōkyō", "Σωκράτης", "Eowyn"]);
        let keys: Vec<(String, i64)> = n.iter().map(|(k, i)| (k.to_lowercase(), *i)).collect();
        for text in [
            "Éowyn rode; ÉOWYN and éowyn and Eowyn and EOWYN. Éowyns hat. xÉowyn, Éowyn_, 7Éowyn.",
            "Zoë Quinn met zoë quinn and ZOË QUINN near Ørsted and ørsted, ØRSTED.",
            "ÜNÏCODE STRASSE is not Ünïcode Straße but ünïcode straße is.",
            "Tōkyō, TŌKYŌ and tōkyō; Σωκράτης and σωκράτης and ΣΩΚΡΆΤΗΣ.",
            "Kelvin K sign and the ſ long s do not confuse Ashen: ASHEN ashen",
            "<p>Éowyn <code>Éowyn</code> <b>Ørsted</b></p>",
        ] {
            let got = autolink_impl(text, &n, true).unwrap();
            if text.starts_with('<') {
                assert_eq!(got, r#"<p><a href="/entity/1" class="auto-entity-link">Éowyn</a> <code>Éowyn</code> <b><a href="/entity/5" class="auto-entity-link">Ørsted</a></b></p>"#);
            } else {
                assert_eq!(got, reference(text, &keys), "{text}");
            }
        }
        // while Python is behind the gateway a non-ASCII name is still Python's to link
        assert!(autolink_impl("Éowyn", &n, false).is_none());
    }

    #[test]
    fn many_random_unicode_texts_agree() {
        let pool = ["Éowyn", "Éowyn Fëanor", "Ashen", "ashé", "Zoë", "Zoë Quinn", "Ørsted", "Tōkyō", "Hali", "Ünïcode", "Straße", "Σωκράτης"];
        let n = names(&pool);
        let keys: Vec<(String, i64)> = n.iter().filter(|(k, _)| k.chars().count() >= 3).map(|(k, i)| (k.to_lowercase(), *i)).collect();
        let glue = [" ", " ", ", ", ". ", "-", "_", "x", "7", "'s ", "é", "\n"];
        let mut seed = 777u64;
        let mut next = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 33) as usize
        };
        for _ in 0..400 {
            let mut text = String::new();
            for _ in 0..(5 + next() % 30) {
                let w = pool[next() % pool.len()];
                match next() % 4 {
                    0 => text.push_str(&w.to_uppercase()),
                    1 => text.push_str(&w.to_lowercase()),
                    _ => text.push_str(w),
                }
                text.push_str(glue[next() % glue.len()]);
            }
            assert_eq!(autolink_impl(&text, &n, true).unwrap(), reference(&text, &keys), "{text}");
        }
    }

    #[test]
    fn tags_and_links_are_left_alone_and_the_automaton_is_reused() {
        let n = names(&["Bob", "Ash Court"]);
        let html = r#"<p>Bob in <code>Bob</code> and <a href="/x">Bob</a> and <b>Ash Court</b></p><pre>Bob</pre>"#;
        let out = autolink_entities(html, &n).unwrap();
        assert_eq!(out, r#"<p><a href="/entity/1" class="auto-entity-link">Bob</a> in <code>Bob</code> and <a href="/x">Bob</a> and <b><a href="/entity/2" class="auto-entity-link">Ash Court</a></b></p><pre>Bob</pre>"#);
        assert_eq!(autolink_entities(html, &n).unwrap(), out);
    }
}
