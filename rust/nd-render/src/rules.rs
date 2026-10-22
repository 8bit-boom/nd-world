//! The rules page's markdown "superpowers" (app/rules_render.py): ::: directive blocks, ```statblock fences, the per-world rules.json
//! overlay; and the strict `html.unescape` the page runs over the source. Differential tests: tests/test_rust_layout.py.

use fancy_regex::Regex;
use serde_json::{json, Map, Value};

use crate::md::render_md;
use crate::text::{py_splitlines, py_strip};

macro_rules! re {
    ($s:expr) => {{
        static R: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
        R.get_or_init(|| Regex::new($s).unwrap())
    }};
}

/// Python's `html.escape(s, quote=True)`
pub fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

pub struct Block {
    pub kind: &'static str,
    pub btype: String,
    pub title: String,
    pub text: String,
    pub sentinel: String,
}

/// `extract_blocks`: (skeleton with a sentinel paragraph per block, blocks in document order)
pub fn extract_blocks(md: &str) -> (String, Vec<Block>) {
    let open = re!(r"^:::(\w+)(?:[ \t]+(.*?))?[ \t]*$");
    let close = re!(r"^:::[ \t]*$");
    let stat = re!(r"^ {0,3}```statblock[ \t]*$");
    let fence_close = re!(r"^ {0,3}`{3,}[ \t]*$");
    let fence_open = re!(r"^ {0,3}`{3,}[^`]*[ \t]*$");
    let lines = py_splitlines(md);
    let mut skel: Vec<String> = vec![];
    let mut blocks: Vec<Block> = vec![];
    let mut i = 0;
    let mut in_fence = false;
    while i < lines.len() {
        let line = lines[i];
        if in_fence {
            skel.push(line.to_string());
            if fence_close.is_match(line).unwrap_or(false) {
                in_fence = false;
            }
            i += 1;
            continue;
        }
        if let Ok(Some(m)) = open.captures(line) {
            i += 1;
            let mut inner = vec![];
            while i < lines.len() && !close.is_match(lines[i]).unwrap_or(false) {
                inner.push(lines[i]);
                i += 1;
            }
            i += 1;
            let sentinel = format!("NDRULESBLOCK{}NDRULES", blocks.len());
            blocks.push(Block {
                kind: "callout",
                btype: m[1].to_lowercase(),
                title: py_strip(m.get(2).map_or("", |g| g.as_str())).to_string(),
                text: inner.join("\n"),
                sentinel: sentinel.clone(),
            });
            skel.push(sentinel);
        } else if stat.is_match(line).unwrap_or(false) {
            i += 1;
            let mut inner = vec![];
            while i < lines.len() && !fence_close.is_match(lines[i]).unwrap_or(false) {
                inner.push(lines[i]);
                i += 1;
            }
            i += 1;
            let sentinel = format!("NDRULESBLOCK{}NDRULES", blocks.len());
            blocks.push(Block { kind: "statblock", btype: "statblock".into(), title: String::new(), text: inner.join("\n"), sentinel: sentinel.clone() });
            skel.push(sentinel);
        } else {
            skel.push(line.to_string());
            if fence_open.is_match(line).unwrap_or(false) {
                in_fence = true;
            }
            i += 1;
        }
    }
    let sentinel_line = re!(r"^NDRULESBLOCK\d+NDRULES$");
    let mut out: Vec<String> = vec![];
    for line in skel {
        if sentinel_line.is_match(&line).unwrap_or(false) {
            if out.last().map_or(false, |l| !py_strip(l).is_empty()) {
                out.push(String::new());
            }
            out.push(line);
            out.push(String::new());
        } else {
            out.push(line);
        }
    }
    (out.join("\n"), blocks)
}

fn render_callout(b: &Block, is_gm: bool) -> String {
    if b.btype == "gm" && !is_gm {
        return String::new();
    }
    if b.btype == "collapse" {
        let title = if b.title.is_empty() { "Details" } else { &b.title };
        return format!(
            r#"<details class="nd-collapse"><summary>{}</summary><div class="nd-collapse-body">{}</div></details>"#,
            html_escape(title),
            render_md(&b.text)
        );
    }
    let spec = match b.btype.as_str() {
        "tip" => Some(("💡", "Tip", "tip")),
        "note" => Some(("📝", "Note", "note")),
        "warning" => Some(("⚠️", "Warning", "warning")),
        "danger" => Some(("☠️", "Danger", "danger")),
        "lore" => Some(("📖", "Lore", "lore")),
        "gm" => Some(("🗝", "GM Only", "gm")),
        _ => None,
    };
    let (icon, title, css) = match spec {
        Some((icon, default, css)) => (icon, if b.title.is_empty() { default.to_string() } else { b.title.clone() }, css),
        None => ("📝", if b.title.is_empty() { b.btype.clone() } else { format!("{}: {}", b.btype, b.title) }, "note"),
    };
    format!(
        r#"<div class="nd-callout nd-callout-{css}"><div class="nd-callout-title">{} {}</div><div class="nd-callout-body">{}</div></div>"#,
        html_escape(icon),
        html_escape(&title),
        render_md(&b.text)
    )
}

fn render_statblock(b: &Block) -> String {
    let norm = b.text.replace("\r\n", "\n").replace('\r', "\n");
    let mut stripped: Vec<&str> = py_splitlines(&norm).into_iter().map(py_strip).collect();
    while stripped.first().map_or(false, |l| l.is_empty()) {
        stripped.remove(0);
    }
    while stripped.last().map_or(false, |l| l.is_empty()) {
        stripped.pop();
    }
    let name = stripped.first().copied().unwrap_or("Statblock").to_string();
    let mut body: Vec<String> = vec![];
    let mut free: Vec<&str> = vec![];
    let flush = |free: &mut Vec<&str>, body: &mut Vec<String>| {
        if !free.is_empty() {
            body.push(format!("<p>{}</p>", render_md(&free.join("\n"))));
            free.clear();
        }
    };
    for ln in stripped.iter().skip(1) {
        if ln.is_empty() {
            flush(&mut free, &mut body);
            continue;
        }
        match ln.split_once(':') {
            Some((label, value)) if !py_strip(label).is_empty() => {
                flush(&mut free, &mut body);
                let label = py_strip(py_strip(label).trim_matches('*'));
                body.push(format!(
                    r#"<div class="nd-statblock-row"><span class="nd-statblock-label">{}</span><span class="nd-statblock-value">{}</span></div>"#,
                    html_escape(label),
                    html_escape(py_strip(value))
                ));
            }
            _ => free.push(ln),
        }
    }
    flush(&mut free, &mut body);
    let raw = html_escape(&b.text).replace('\n', "&#10;");
    format!(
        r#"<div class="nd-statblock" data-raw="{raw}"><div class="nd-statblock-name">{}</div><div class="nd-statblock-body">{}</div><button type="button" class="nd-statblock-copy">📋 Copy</button></div>"#,
        html_escape(&name),
        body.join("")
    )
}

/// `restore_blocks`
pub fn restore_blocks(skeleton_html: &str, blocks: &[Block], is_gm: bool) -> String {
    let mut out = skeleton_html.to_string();
    for b in blocks {
        let html = if b.kind == "statblock" { render_statblock(b) } else { render_callout(b, is_gm) };
        let wrapped = format!("<p>{}</p>", b.sentinel);
        out = if out.contains(&wrapped) { out.replace(&wrapped, &html) } else { out.replace(&b.sentinel, &html) };
    }
    out
}

/// `rules_render.split_rules_sections(html)` (h2 / h3 split points)
pub fn split_sections_h23(html: &str) -> Vec<Value> {
    let rx = re!(r#"(?s)<h([23]) id="([^"]*)">(.*?)</h[23]>"#);
    let ms: Vec<_> = rx.captures_iter(html).filter_map(|m| m.ok()).collect();
    if ms.is_empty() {
        return vec![json!({"id": null, "level": 0, "title": "", "html": html})];
    }
    let mut out = vec![];
    let first = ms[0].get(0).unwrap().start();
    if !py_strip(&html[..first]).is_empty() {
        out.push(json!({"id": null, "level": 0, "title": "", "html": &html[..first]}));
    }
    for (i, m) in ms.iter().enumerate() {
        let start = m.get(0).unwrap().start();
        let end = ms.get(i + 1).map(|n| n.get(0).unwrap().start()).unwrap_or(html.len());
        let title = html_escape_decode(&re!(r"<[^>]+>").replace_all(&m[3], ""));
        out.push(json!({"id": &m[2], "level": m[1].parse::<i64>().unwrap_or(0), "title": py_strip(&title), "html": &html[start..end]}));
    }
    out
}

fn html_escape_decode(s: &str) -> String {
    html_escape::decode_html_entities(s).into_owned()
}

fn slugify(text: &str) -> String {
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
    slug.trim_matches('-').to_string()
}

/// `parse_rules_overlay`: Ok(Some(overlay)) / Ok(None) for no overlay or an invalid one (the page ignores both), Err(()) for JSON Python
/// might read and this parser cannot (NaN, Infinity...) - the page is then Python's
pub fn parse_overlay(raw: Option<&str>) -> Result<Option<Value>, ()> {
    let Some(raw) = raw.filter(|r| !py_strip(r).is_empty()) else { return Ok(None) };
    // text this parser rejects may still be JSON to Python (NaN, lone surrogates...): that page is Python's
    let data: Value = serde_json::from_str(raw).map_err(|_| ())?;
    let Value::Object(data) = data else { return Ok(None) };
    let sections_in = match data.get("sections") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(o)) => o.clone(),
        Some(_) => return Ok(None),
    };
    let mut sections = Map::new();
    for (slug, cfg) in sections_in {
        let cfg = match cfg {
            Value::Null => Map::new(),
            Value::Object(o) => o,
            _ => return Ok(None),
        };
        let mut entry = Map::new();
        for key in ["icon", "title"] {
            match cfg.get(key) {
                None | Some(Value::Null) => {}
                Some(v @ Value::String(_)) => {
                    entry.insert(key.into(), v.clone());
                }
                Some(_) => return Ok(None),
            }
        }
        match cfg.get("players_visible") {
            None | Some(Value::Null) => {}
            Some(v @ Value::Bool(_)) => {
                entry.insert("players_visible".into(), v.clone());
            }
            Some(_) => return Ok(None),
        }
        match cfg.get("order") {
            None | Some(Value::Null) => {}
            Some(v @ Value::Number(_)) => {
                entry.insert("order".into(), v.clone());
            }
            Some(_) => return Ok(None),
        }
        sections.insert(slug, Value::Object(entry));
    }
    let tabs_in = match data.get("tabs") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(_) => return Ok(None),
    };
    let mut tabs = vec![];
    let mut seen: Vec<String> = vec![];
    for (idx, tab) in tabs_in.iter().enumerate() {
        let Some(tab) = tab.as_object() else { return Ok(None) };
        let Some(label) = tab.get("label").and_then(|l| l.as_str()).filter(|l| !py_strip(l).is_empty()) else { return Ok(None) };
        let mut tab_id = match tab.get("id").and_then(|v| v.as_str()).filter(|s| !py_strip(s).is_empty()) {
            Some(s) => s.to_string(),
            None => {
                let s = slugify(label);
                if s.is_empty() { format!("tab-{}", idx + 1) } else { s }
            }
        };
        let (base, mut n) = (tab_id.clone(), 2);
        while seen.contains(&tab_id) {
            tab_id = format!("{base}-{n}");
            n += 1;
        }
        seen.push(tab_id.clone());
        let secs = match tab.get("sections") {
            None | Some(Value::Null) => vec![],
            Some(Value::Array(a)) if a.iter().all(|s| s.is_string()) => a.clone(),
            Some(_) => return Ok(None),
        };
        tabs.push(json!({"id": py_strip(&tab_id), "label": py_strip(label), "sections": secs}));
    }
    Ok(Some(json!({"sections": sections, "tabs": tabs})))
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `apply_rules_overlay` -> (flat visible sections, tab panes or None)
pub fn apply_overlay(sections: Vec<Value>, overlay: Option<&Value>, is_gm: bool) -> Option<(Vec<Value>, Option<Vec<Value>>)> {
    let Some(overlay) = overlay else { return Some((sections, None)) };
    let cfg = overlay.get("sections")?.as_object()?;
    let mut kept: Vec<Value> = vec![];
    for section in sections {
        let id = section.get("id")?.as_str().filter(|s| !s.is_empty()).map(|s| s.to_string());
        let conf = id.as_ref().and_then(|i| cfg.get(i)).filter(|c| truthy(Some(c)));
        let mut section = section;
        if let Some(conf) = conf {
            if conf.get("players_visible") == Some(&Value::Bool(false)) && !is_gm {
                continue;
            }
            let icon = conf.get("icon").and_then(|v| v.as_str()).unwrap_or("");
            let title = conf.get("title").and_then(|v| v.as_str());
            let s = section.as_object_mut()?;
            if !icon.is_empty() || title.map_or(false, |t| !t.is_empty()) {
                let label = match (icon.is_empty(), title.filter(|t| !t.is_empty())) {
                    (false, Some(t)) => format!("{icon} {t}"),
                    (_, Some(t)) => t.to_string(),
                    (_, None) => py_strip(&format!("{icon} {}", s.get("title")?.as_str()?)).to_string(),
                };
                let html = s.get("html")?.as_str()?.to_string();
                let rx = re!(r#"(?s)(<h[23] id="[^"]*">)(.*?)(</h[23]>)"#);
                let new_html = match rx.captures(&html).ok()? {
                    Some(c) => {
                        let m = c.get(0)?;
                        format!("{}{}{}{}{}", &html[..m.start()], &c[1], html_escape(&label), &c[3], &html[m.end()..])
                    }
                    None => html.clone(),
                };
                s.insert("title".into(), json!(label));
                s.insert("html".into(), json!(new_html));
            }
            s.insert("_order".into(), conf.get("order").cloned().unwrap_or(Value::Null));
        }
        kept.push(section);
    }
    let has_id = |s: &Value| s.get("id").and_then(|v| v.as_str()).map_or(false, |x| !x.is_empty());
    let order_of = |s: &Value| s.get("_order").filter(|v| !v.is_null()).and_then(|v| v.as_f64());
    let body: Vec<&Value> = kept.iter().filter(|s| has_id(s)).collect();
    let mut ordered: Vec<&Value> = body.iter().copied().filter(|s| order_of(s).is_some()).collect();
    ordered.sort_by(|a, b| order_of(a).partial_cmp(&order_of(b)).unwrap_or(std::cmp::Ordering::Equal));
    let natural: Vec<&Value> = body.iter().copied().filter(|s| order_of(s).is_none()).collect();
    let pre: Vec<&Value> = kept.iter().filter(|s| !has_id(s)).collect();
    let flat: Vec<Value> = pre.iter().chain(ordered.iter()).chain(natural.iter()).map(|v| (*v).clone()).collect();
    let tabs_def = overlay.get("tabs").and_then(|t| t.as_array()).cloned().unwrap_or_default();
    if tabs_def.is_empty() {
        return Some((flat, None));
    }
    let by_id = |sid: &str| kept.iter().find(|s| s.get("id").and_then(|v| v.as_str()) == Some(sid));
    let mut listed: Vec<String> = vec![];
    let mut panes: Vec<Value> = vec![];
    for tab in &tabs_def {
        let mut ids = vec![];
        for sid in tab.get("sections")?.as_array()? {
            let sid = sid.as_str()?;
            // a slug repeated within one tab's own list is kept twice, as Python's comprehension does
            if by_id(sid).map_or(false, has_id) && !listed.iter().any(|l| l == sid) {
                ids.push(sid.to_string());
            }
        }
        listed.extend(ids.iter().cloned());
        let secs: Vec<Value> = ids.iter().filter_map(|sid| by_id(sid).cloned()).collect();
        panes.push(json!({"id": tab.get("id")?, "label": tab.get("label")?, "sections": secs}));
    }
    let leftovers: Vec<Value> = kept.iter().filter(|s| has_id(s) && !listed.iter().any(|l| Some(l.as_str()) == s.get("id").and_then(|v| v.as_str()))).cloned().collect();
    if !leftovers.is_empty() {
        panes.push(json!({"id": "more", "label": "More", "sections": leftovers}));
    }
    panes.retain(|p| p["sections"].as_array().map_or(false, |a| !a.is_empty()));
    if panes.is_empty() || (panes.len() == 1 && panes[0]["id"] == "more") {
        return Some((flat, None));
    }
    if !pre.is_empty() {
        let mut first: Vec<Value> = pre.iter().map(|v| (*v).clone()).collect();
        first.extend(panes[0]["sections"].as_array()?.iter().cloned());
        panes[0]["sections"] = Value::Array(first);
    }
    Some((flat, Some(panes)))
}

/// Python's `html.unescape` for the entities this port is sure of (&amp; &lt; &gt; &quot; &apos; &nbsp; and numeric ones for ordinary
/// characters); None when the text has any other `&...` Python would decode (the page is then Python's)
pub fn strict_unescape(s: &str) -> Option<String> {
    if !s.contains('&') {
        return Some(s.to_string());
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        let tail = &rest[i + 1..];
        // what Python's pattern would try to match after "&"
        let next = tail.chars().next();
        match next {
            None => {
                out.push('&');
                rest = "";
                continue;
            }
            Some(c) if matches!(c, '\t' | '\n' | '\x0c' | ' ' | '<' | '&' | ';') => {
                out.push('&');
                rest = tail;
                continue;
            }
            _ => {}
        }
        let named = [("amp;", '&'), ("lt;", '<'), ("gt;", '>'), ("quot;", '"'), ("apos;", '\''), ("nbsp;", '\u{a0}')];
        if let Some((n, c)) = named.iter().find(|(n, _)| tail.starts_with(n)) {
            out.push(*c);
            rest = &tail[n.len()..];
            continue;
        }
        if let Some(num) = tail.strip_prefix('#') {
            let (digits, radix, skip) = match num.strip_prefix(['x', 'X']) {
                Some(h) => (h.chars().take_while(|c| c.is_ascii_hexdigit()).collect::<String>(), 16, 2),
                None => (num.chars().take_while(|c| c.is_ascii_digit()).collect::<String>(), 10, 1),
            };
            if digits.is_empty() || digits.len() > 7 {
                return None;
            }
            let after = &tail[skip + digits.len()..];
            if !after.starts_with(';') {
                return None;
            }
            let cp = u32::from_str_radix(&digits, radix).ok()?;
            let ok = (0x20..0x7f).contains(&cp) || cp == 0x0a || cp == 0x09 || (0xa0..0xd800).contains(&cp) || (0xe000..0xfdd0).contains(&cp) || (0x1_0000..0x10_fffe).contains(&cp) && cp & 0xfffe != 0xfffe;
            if !ok {
                return None;
            }
            out.push(char::from_u32(cp)?);
            rest = &after[1..];
            continue;
        }
        return None;
    }
    out.push_str(rest);
    Some(out)
}

/// The rules page's content pipeline (main.rules_page): extract blocks, render the skeleton, heading ids, restore the blocks
pub fn render_rules_content(md: &str, is_gm: bool) -> String {
    let (skeleton, blocks) = extract_blocks(md);
    let (content, _) = crate::text::rules_toc(&render_md(&skeleton), "23");
    restore_blocks(&content, &blocks, is_gm)
}
