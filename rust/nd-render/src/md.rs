//! `rendering.render_md`: markdown to safe HTML, then the app's own tags ([color], [mark], [u], [read], [spoiler], [gmonly], [for], [reveal]),
//! media titles and prose code blocks - the post-processing is a line-by-line port. The markdown step itself is pulldown-cmark (CommonMark +
//! tables + strikethrough), not markdown2: the two disagree on some inputs, which tests/test_rust_markdown.py measures and docs/RUST_PAGES.md
//! reports. Raw HTML in the text is escaped, as markdown2's safe_mode="escape" does.
use fancy_regex::{Captures, Regex};
use pulldown_cmark::{html, Event, Options, Parser};
use std::sync::OnceLock;

macro_rules! re {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| Regex::new($p).expect("pattern"))
    }};
}

const COLOR_NAMES: &[&str] = &["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink", "white", "black", "gray", "grey", "magenta",
    "lime", "teal", "gold", "silver", "brown", "crimson", "violet", "indigo", "salmon", "coral"];

fn safe_color(raw: &str) -> Option<String> {
    let raw = raw.trim();
    if re!(r"^#(?:[0-9a-fA-F]{3}){1,2}$").is_match(raw).unwrap_or(false) {
        return Some(raw.to_string());
    }
    let l = raw.to_lowercase();
    COLOR_NAMES.contains(&l.as_str()).then_some(l)
}

fn markdown(text: &str) -> String {
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    let events = Parser::new_ext(text, opts).map(|e| match e {
        Event::Html(h) | Event::InlineHtml(h) => Event::Text(h),
        other => other,
    });
    let mut out = String::new();
    html::push_html(&mut out, events);
    out
}

fn transform_media_tags(html: &str) -> String {
    re!(r"<img\b[^>]*>")
        .replace_all(html, |c: &Captures| {
            let tag = &c[0];
            let attrs: Vec<(String, String)> = re!(r#"(\w+)="([^"]*)""#).captures_iter(tag).filter_map(|m| m.ok()).map(|m| (m[1].to_string(), m[2].to_string())).collect();
            let get = |k: &str| attrs.iter().rev().find(|(a, _)| a == k).map(|(_, v)| v.clone()).unwrap_or_default();
            let (title, src) = (get("title"), get("src"));
            if title == "audio" {
                return format!(r#"<audio controls preload="metadata" src="{src}"></audio>"#);
            }
            if title == "video" {
                return format!(r#"<video controls preload="metadata" src="{src}"></video>"#);
            }
            if let Some(n) = title.strip_prefix("size:").filter(|n| (1..=3).contains(&n.len()) && n.bytes().all(|b| b.is_ascii_digit())) {
                let pct = n.parse::<i64>().unwrap().clamp(10, 300);
                let mut kept: Vec<(String, String)> = vec![];
                for (k, v) in &attrs {
                    if k != "title" {
                        if let Some(e) = kept.iter_mut().find(|(a, _)| a == k) {
                            e.1 = v.clone();
                        } else {
                            kept.push((k.clone(), v.clone()));
                        }
                    }
                }
                if let Some(e) = kept.iter_mut().find(|(a, _)| a == "style") {
                    e.1 = format!("width:{pct}%");
                } else {
                    kept.push(("style".into(), format!("width:{pct}%")));
                }
                return format!("<img {}/>", kept.iter().map(|(k, v)| format!(r#"{k}="{v}""#)).collect::<Vec<_>>().join(" "));
            }
            tag.to_string()
        })
        .into_owned()
}

fn wraps_as_prose(code: &str) -> bool {
    let mut prose = false;
    for line in code.lines() {
        let line = line.trim();
        if re!(r"\S {3,}\S").is_match(line).unwrap_or(false) {
            return false;
        }
        if line.chars().count() >= 80 && line.split_whitespace().count() >= 8 {
            prose = true;
        }
    }
    prose
}

fn unescape(s: &str) -> String {
    html_escape::decode_html_entities(s).into_owned()
}

fn mark_prose_code_blocks(html: &str) -> String {
    re!(r"(?s)<pre([^>]*)>(.*?)</pre>")
        .replace_all(html, |c: &Captures| {
            let (attrs, body) = (c[1].to_string(), &c[2]);
            if !wraps_as_prose(&unescape(&re!(r"<[^>]+>").replace_all(body, ""))) {
                return c[0].to_string();
            }
            let attrs = if attrs.contains("class=\"") { attrs.replacen("class=\"", "class=\"md-wrap ", 1) } else { attrs + " class=\"md-wrap\"" };
            format!("<pre{attrs}>{body}</pre>")
        })
        .into_owned()
}

fn block(inner: &str) -> bool {
    re!(r"(?i)<(?:p|div|ul|ol|li|h[1-6]|table|blockquote|pre|hr)\b").is_match(inner).unwrap_or(false)
}

fn apply_inline_styles(html: &str) -> String {
    let h = re!(r"(?s)\[color=([^\]]{1,20})\](.*?)\[/color\]").replace_all(html, |c: &Captures| match safe_color(&c[1]) {
        Some(col) => format!(r#"<span style="color:{col}">{}</span>"#, &c[2]),
        None => c[2].to_string(),
    });
    let h = re!(r"(?s)\[mark(?:=([^\]]{1,20}))?\](.*?)\[/mark\]").replace_all(&h, |c: &Captures| {
        let style = c.get(1).and_then(|m| safe_color(m.as_str())).map(|col| format!(r#" style="background-color:{col}""#)).unwrap_or_default();
        format!("<mark{style}>{}</mark>", &c[2])
    });
    let h = re!(r"(?s)\[u\](.*?)\[/u\]").replace_all(&h, "<u>$1</u>");
    let h = re!(r"(?si)\[read\](.*?)\[/read\]").replace_all(&h, r#"<span class="read-aloud">$1</span>"#);
    let h = re!(r"(?s)\[spoiler\](.*?)\[/spoiler\]")
        .replace_all(&h, r#"<span class="spoiler-text" data-spoiler tabindex="0" role="button" aria-label="Spoiler — click to reveal">$1</span>"#);
    let h = re!(r"(?si)\[gmonly\](.*?)\[/gmonly\]").replace_all(&h, |c: &Captures| {
        if block(&c[1]) { format!(r#"<div class="gm-only-text" data-gm-only>{}</div>"#, &c[1]) } else { format!(r#"<span class="gm-only-text gm-inline" data-gm-only>{}</span>"#, &c[1]) }
    });
    let h = re!(r"(?si)\[for=([0-9][0-9,\s]{0,63})\]((?:(?!\[for=).)*?)\[/for\]").replace_all(&h, |c: &Captures| {
        let mut ids: Vec<i64> = c[1].split(|ch: char| !ch.is_ascii_digit()).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect();
        ids.sort();
        ids.dedup();
        let ids = ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",");
        let (tag, cls) = if block(&c[2]) { ("div", "for-only-text") } else { ("span", "for-only-text gm-inline") };
        format!(r#"<{tag} class="{cls}" data-for="{ids}">{}</{tag}>"#, &c[2])
    });
    let h = re!(r"(?si)\[reveal=(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?)\]((?:(?!\[reveal=).)*?)\[/reveal\]").replace_all(&h, |c: &Captures| {
        let stamp = nd_perm::gmonly::parse_reveal_at(&c[1]).map(|(y, m, d, h, mi)| format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}")).unwrap_or_default();
        let (tag, cls) = if block(&c[2]) { ("div", "reveal-later-text") } else { ("span", "reveal-later-text gm-inline") };
        format!(r#"<{tag} class="{cls}" data-reveal="{stamp}">{}</{tag}>"#, &c[2])
    });
    h.into_owned()
}

pub fn render_md(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    apply_inline_styles(&mark_prose_code_blocks(&transform_media_tags(&markdown(text))))
}
