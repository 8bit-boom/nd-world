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
        R.get_or_init(|| fancy_regex::RegexBuilder::new($p).backtrack_limit(200_000_000).build().expect("pattern"))
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

/// markdown2 reads a line of only `#`s (no text) as a heading whose text is a `#`: `##` is `<h1>#</h1>`, `###` is `<h2>#</h2>`, up to
/// `######` as `<h5>#</h5>`, and a lone `#` is a paragraph. pulldown-cmark makes an empty heading of them.
fn fix_bare_hash_lines<'a>(text: &str, events: Vec<(Event<'a>, std::ops::Range<usize>)>) -> Vec<Event<'a>> {
    use pulldown_cmark::{HeadingLevel as H, Tag, TagEnd};
    let mut out: Vec<Event<'a>> = Vec::with_capacity(events.len());
    let mut i = 0;
    while i < events.len() {
        if let (Event::Start(Tag::Heading { level, .. }), Some((Event::End(TagEnd::Heading(_)), _))) = (&events[i].0, events.get(i + 1)) {
            let src = text.get(events[i].1.clone()).unwrap_or("");
            let line = src.trim_end_matches(['\n', '\r']);
            if !line.is_empty() && line.bytes().all(|b| b == b'#') && line.len() == *level as usize {
                if line.len() == 1 {
                    out.extend([Event::Start(Tag::Paragraph), Event::Text("#".into()), Event::End(TagEnd::Paragraph)]);
                } else {
                    let lvl = [H::H1, H::H1, H::H1, H::H2, H::H3, H::H4, H::H5][line.len()];
                    out.extend([Event::Start(Tag::Heading { level: lvl, id: None, classes: vec![], attrs: vec![] }), Event::Text("#".into()), Event::End(TagEnd::Heading(lvl))]);
                }
                i += 2;
                continue;
            }
        }
        out.push(events[i].0.clone());
        i += 1;
    }
    out
}

/// markdown2 puts the paragraph that follows an empty bullet (`- ` + one blank line) into that bullet; CommonMark ends the bullet and
/// leaves an empty `<li>`. Word/Docs imports are full of them, so join the next line onto the marker line (fenced code is left alone).
fn join_empty_bullets(text: &str) -> std::borrow::Cow<'_, str> {
    let lines: Vec<&str> = text.split('\n').collect();
    let is_empty_bullet = |l: &str| {
        let t = l.trim_start_matches(' ');
        l.len() - t.len() <= 3 && {
            let rest = t.strip_prefix(['-', '*', '+']).or_else(|| {
                let digits = t.bytes().take_while(|b| b.is_ascii_digit()).count();
                (digits > 0 && t[digits..].starts_with('.')).then(|| &t[digits + 1..])
            });
            rest.is_some_and(|r| !r.is_empty() && r.bytes().all(|b| b == b' ' || b == b'\t'))
        }
    };
    let is_marker_line = |l: &str| {
        let t = l.trim_start();
        t.strip_prefix(['-', '*', '+']).is_some_and(|r| r.starts_with([' ', '\t']))
            || (t.bytes().take_while(|b| b.is_ascii_digit()).count() > 0 && t.trim_start_matches(|c: char| c.is_ascii_digit()).starts_with(". "))
    };
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let (mut fence, mut changed, mut i) = (None::<String>, false, 0);
    while i < lines.len() {
        let l = lines[i];
        let t = l.trim_start();
        if let Some(f) = &fence {
            if t.starts_with(f.as_str()) {
                fence = None;
            }
        } else if t.starts_with("```") || t.starts_with("~~~") {
            fence = Some(t[..3].to_string());
        } else if is_empty_bullet(l) && i + 2 < lines.len() && lines[i + 1].trim().is_empty() && !lines[i + 2].trim().is_empty() && !is_marker_line(lines[i + 2]) && !lines[i + 2].starts_with([' ', '\t']) {
            out.push(format!("{}{}", l.trim_end(), format!(" {}", lines[i + 2])));
            changed = true;
            i += 3;
            continue;
        }
        out.push(l.to_string());
        i += 1;
    }
    if changed { std::borrow::Cow::Owned(out.join("\n")) } else { std::borrow::Cow::Borrowed(text) }
}

fn markdown(text: &str) -> String {
    let text = &*join_empty_bullets(text);
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    let events = fix_bare_hash_lines(text, Parser::new_ext(text, opts).into_offset_iter().collect()).into_iter().map(|e| match e {
        Event::Html(h) | Event::InlineHtml(h) => Event::Text(h),
        // markdown2 writes a fenced block as a plain <pre><code> whatever its info string ("```python")
        Event::Start(pulldown_cmark::Tag::CodeBlock(pulldown_cmark::CodeBlockKind::Fenced(_))) => {
            Event::Start(pulldown_cmark::Tag::CodeBlock(pulldown_cmark::CodeBlockKind::Fenced("".into())))
        }
        other => other,
    });
    let mut out = String::new();
    html::push_html(&mut out, events);
    // table cell alignment as markdown2 writes it
    re!(r#"<(t[hd]) style="text-align: (left|right|center)">"#).replace_all(&out, r#"<$1 style="text-align:$2;">"#).into_owned()
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
