//! Turning nd-world's markup into clean text for a GM model. Secrets are KEPT but labelled, so the model learns the fact and that the
//! players do not know it:
//!   [gmonly]X[/gmonly]            -> (GM secret: X)
//!   [for=IDS]X[/for]              -> (Secret known only to some players: X)
//!   [reveal=DATE]X[/reveal]       -> (Hidden from players until DATE: X)
//!   [read]X[/read]                -> (Read aloud to the players: X)
//!   [spoiler] [color] [mark] [u]  -> X
//!   :::gm ... ::: (rules)         -> (GM only: ...)
//! The tag grammar follows nd-world's app/rendering.py (case-insensitive, nesting resolved from the inside out; a block that is never
//! closed runs to the end of the text, so a secret is never lost or shown unlabelled). Also: [[wikilinks]], markdown images and links,
//! `<img>` and legacy `<a name>` anchors, and the rules page's `:::` directives.
use regex::Regex;
use std::sync::OnceLock;

fn re(cell: &'static OnceLock<Regex>, pat: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pat).expect("valid pattern"))
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Tag {
    GmOnly,
    For,
    Reveal,
    Read,
    Spoiler,
    Color,
    Mark,
    Underline,
}

const TAGS: [(Tag, &str, &str, bool); 8] = [
    // (tag, opening prefix, closing tag, opening has a parameter and ends with ']')
    (Tag::GmOnly, "[gmonly]", "[/gmonly]", false),
    (Tag::For, "[for=", "[/for]", true),
    (Tag::Reveal, "[reveal=", "[/reveal]", true),
    (Tag::Read, "[read]", "[/read]", false),
    (Tag::Spoiler, "[spoiler]", "[/spoiler]", false),
    (Tag::Color, "[color=", "[/color]", true),
    (Tag::Mark, "[mark", "[/mark]", true), // [mark] or [mark=yellow]
    (Tag::Underline, "[u]", "[/u]", false),
];

fn label(tag: Tag, param: &str, inner: &str) -> String {
    let inner = inner.trim();
    match tag {
        Tag::GmOnly => format!("(GM secret: {inner})"),
        Tag::For => format!("(Secret known only to some players: {inner})"),
        Tag::Reveal => format!("(Hidden from players until {}: {inner})", param.trim().replace('T', " ")),
        Tag::Read => format!("(Read aloud to the players: {inner})"),
        _ => inner.to_string(),
    }
}

/// The opening tag that ends at or before `limit` and starts last: (start, end of the opening tag, parameter)
fn last_open(lower: &str, limit: usize, tag: Tag) -> Option<(usize, usize, (usize, usize))> {
    let (_, prefix, _, has_param) = TAGS.iter().find(|t| t.0 == tag)?;
    let hay = &lower[..limit];
    let mut search_end = hay.len();
    while let Some(start) = hay[..search_end].rfind(prefix) {
        if !has_param {
            return Some((start, start + prefix.len(), (0, 0)));
        }
        // a parameter tag: the opening ends at the next ']'
        if let Some(rel) = hay[start + prefix.len()..].find(']') {
            let end = start + prefix.len() + rel + 1;
            let (ps, pe) = (start + prefix.len(), start + prefix.len() + rel);
            let raw_param = &hay[ps..pe];
            // "[mark" must be followed by ']' or '='; anything else (e.g. "[markdown]") is not the tag
            let range = if tag == Tag::Mark {
                if raw_param.is_empty() { Some((ps, pe)) } else { raw_param.strip_prefix('=').map(|_| (ps + 1, pe)) }
            } else {
                Some((ps, pe))
            };
            if let Some(r) = range {
                return Some((start, end, r));
            }
        }
        if start == 0 {
            break;
        }
        search_end = start;
    }
    None
}

/// Resolves all access and styling tags, innermost first.
pub fn resolve_tags(text: &str) -> String {
    resolve_tags_with(text, &mut Vec::new())
}

fn is_secret(tag: Tag) -> bool {
    matches!(tag, Tag::GmOnly | Tag::For | Tag::Reveal)
}

/// Like `resolve_tags`, and the inside of every secret block is added to `found` (innermost first).
pub fn resolve_tags_with(text: &str, found: &mut Vec<String>) -> String {
    let mut text = text.to_string();
    // each pass removes at least one tag, so this ends; the cap only guards against pathological input
    for _ in 0..100_000 {
        let lower = text.to_ascii_lowercase(); // ASCII-only change: byte offsets stay valid for the original text
        let Some((pos, tag)) = TAGS.iter().filter_map(|(t, _, close, _)| lower.find(close).map(|p| (p, *t))).min_by_key(|(p, _)| *p) else { break };
        let close = TAGS.iter().find(|t| t.0 == tag).map(|t| t.2).unwrap_or("");
        match last_open(&lower, pos, tag) {
            Some((start, open_end, (ps, pe))) => {
                let param = text[ps..pe].to_string();
                let inner = &text[open_end..pos];
                if is_secret(tag) && !inner.trim().is_empty() {
                    found.push(inner.trim().to_string());
                }
                let replaced = label(tag, &param, inner);
                text = format!("{}{}{}", &text[..start], replaced, &text[pos + close.len()..]);
            }
            None => text = format!("{}{}", &text[..pos], &text[pos + close.len()..]), // a closing tag with no opening
        }
    }
    // openings that are never closed: a secret runs to the end of the text (labelled), styling tags just vanish
    for _ in 0..10_000 {
        let lower = text.to_ascii_lowercase();
        let Some((start, tag, open_end, (ps, pe))) = TAGS.iter().filter_map(|(t, _, _, _)| {
            let (s, e, p) = first_open(&lower, *t)?;
            Some((s, *t, e, p))
        }).min_by_key(|(s, ..)| *s) else { break };
        let param = text[ps..pe].to_string();
        let rest = text[open_end..].to_string();
        if is_secret(tag) && !rest.trim().is_empty() {
            found.push(rest.trim().to_string());
        }
        let replaced = label(tag, &param, &rest);
        text = format!("{}{}", &text[..start], replaced);
    }
    text
}

fn first_open(lower: &str, tag: Tag) -> Option<(usize, usize, (usize, usize))> {
    let (_, prefix, _, has_param) = TAGS.iter().find(|t| t.0 == tag)?;
    let mut from = 0;
    while let Some(rel) = lower[from..].find(prefix) {
        let start = from + rel;
        if !has_param {
            return Some((start, start + prefix.len(), (0, 0)));
        }
        if let Some(r) = lower[start + prefix.len()..].find(']') {
            let (ps, pe) = (start + prefix.len(), start + prefix.len() + r);
            let raw = &lower[ps..pe];
            let range = if tag == Tag::Mark { if raw.is_empty() { Some((ps, pe)) } else { raw.strip_prefix('=').map(|_| (ps + 1, pe)) } } else { Some((ps, pe)) };
            if let Some(rg) = range {
                return Some((start, pe + 1, rg));
            }
        }
        from = start + prefix.len();
    }
    None
}

/// The rules page's `:::` directives: `:::gm` blocks become "(GM only: ...)", other blocks keep their title as a bold line and their text.
fn directives(text: &str) -> String {
    static OPEN: OnceLock<Regex> = OnceLock::new();
    let open = re(&OPEN, r"^:::(\w+)(?:[ \t]+(.*?))?[ \t]*$");
    let mut out: Vec<String> = Vec::new();
    let mut stack: Vec<(String, usize)> = Vec::new(); // (type, index in `out` where the block's text starts)
    let mut in_fence = false;
    for line in text.lines() {
        if line.trim_start().starts_with("```") {
            in_fence = !in_fence;
        }
        if in_fence || !line.starts_with(":::") {
            out.push(line.to_string());
            continue;
        }
        if line.trim_end() == ":::" {
            if let Some((kind, from)) = stack.pop() {
                if kind == "gm" {
                    let inner: Vec<String> = out.drain(from..).collect();
                    out.push(format!("(GM only: {})", inner.join("\n").trim()));
                }
            }
            continue;
        }
        if let Some(c) = open.captures(line) {
            let kind = c[1].to_lowercase();
            if kind != "gm" {
                if let Some(title) = c.get(2).map(|m| m.as_str().trim()).filter(|t| !t.is_empty()) {
                    out.push(format!("**{title}**"));
                }
            }
            stack.push((kind, out.len()));
        }
    }
    // blocks never closed run to the end of the text
    while let Some((kind, from)) = stack.pop() {
        if kind == "gm" {
            let inner: Vec<String> = out.drain(from..).collect();
            out.push(format!("(GM only: {})", inner.join("\n").trim()));
        }
    }
    out.join("\n")
}

/// Everything: tags, directives, wikilinks, images, links, legacy anchors; whitespace tidied. Accented text is untouched.
pub fn clean(text: &str) -> String {
    static ANCHOR: OnceLock<Regex> = OnceLock::new();
    static IMG_HTML: OnceLock<Regex> = OnceLock::new();
    static IMG_MD: OnceLock<Regex> = OnceLock::new();
    static WIKI: OnceLock<Regex> = OnceLock::new();
    static LINK: OnceLock<Regex> = OnceLock::new();
    static BLANKS: OnceLock<Regex> = OnceLock::new();
    static TRAIL: OnceLock<Regex> = OnceLock::new();
    let mut t = directives(text);
    t = resolve_tags(&t);
    t = re(&ANCHOR, r#"(?i)<a\s+name="[^"]*">\s*</a>"#).replace_all(&t, "").to_string();
    t = re(&IMG_HTML, r"<img\b[^>]*>").replace_all(&t, "").to_string();
    t = re(&IMG_MD, r"!\[([^\]]*)\]\([^)]*\)").replace_all(&t, "$1").to_string();
    t = re(&WIKI, r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]").replace_all(&t, |c: &regex::Captures| c.get(2).unwrap_or_else(|| c.get(1).expect("group 1")).as_str().trim().to_string()).to_string();
    t = re(&LINK, r"\[([^\]]+)\]\((?:/|https?://|\./|#)[^)]*\)").replace_all(&t, "$1").to_string();
    t = re(&TRAIL, r"[ \t]+\n").replace_all(&t, "\n").to_string();
    t = re(&BLANKS, r"\n{3,}").replace_all(&t, "\n\n").to_string();
    t.trim().to_string()
}

/// The GM-secret blocks of a text (what is inside each [gmonly] / [for] / [reveal] block), cleaned; a secret nested inside another is
/// not listed twice. For "what are the GM secrets about X?" examples.
pub fn secrets(text: &str) -> Vec<String> {
    let mut found = Vec::new();
    resolve_tags_with(text, &mut found);
    let cleaned: Vec<String> = found.iter().map(|f| clean(f)).filter(|f| !f.is_empty()).collect();
    let mut out: Vec<String> = Vec::new();
    for (i, f) in cleaned.iter().enumerate() {
        let inside_other = cleaned.iter().enumerate().any(|(j, g)| j != i && g.len() > f.len() && g.contains(f.as_str()));
        if !inside_other && !out.contains(f) {
            out.push(f.clone());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secrets_are_kept_and_labelled() {
        assert_eq!(clean("Hunts beasts. [gmonly]Fears fire.[/gmonly] Kind."), "Hunts beasts. (GM secret: Fears fire.) Kind.");
        assert_eq!(clean("[GMOnly]x[/GMONLY]"), "(GM secret: x)");
        assert_eq!(clean("[for=1, 2]Only you[/for]"), "(Secret known only to some players: Only you)");
        assert_eq!(clean("[reveal=2026-12-01T20:00]The duke[/reveal]"), "(Hidden from players until 2026-12-01 20:00: The duke)");
        assert_eq!(clean("[read]The door creaks.[/read]"), "(Read aloud to the players: The door creaks.)");
    }

    #[test]
    fn nesting_and_mixed_tags_resolve_from_the_inside() {
        assert_eq!(clean("[gmonly]a [for=3]b[/for] c[/gmonly]"), "(GM secret: a (Secret known only to some players: b) c)");
        assert_eq!(clean("[color=red][u]warm[/u][/color] [mark=yellow]hot[/mark] [mark]x[/mark] [spoiler]twist[/spoiler]"), "warm hot x twist");
        assert_eq!(clean("[gmonly]one[/gmonly] and [gmonly]two[/gmonly]"), "(GM secret: one) and (GM secret: two)");
    }

    #[test]
    fn broken_tags_never_lose_or_unlabel_a_secret() {
        assert_eq!(clean("Public. [gmonly]never closed"), "Public. (GM secret: never closed)");
        assert_eq!(clean("stray [/gmonly] close and [/for]"), "stray  close and");
        assert_eq!(clean("[for=1]open [reveal=2026-01-01]deep"), "(Secret known only to some players: open (Hidden from players until 2026-01-01: deep))");
        assert_eq!(clean("a [markdown] and [u]ok"), "a [markdown] and ok");
    }

    #[test]
    fn accented_and_unicode_text_survives() {
        assert_eq!(clean("[gmonly]Émile fears 🔥 日本語[/gmonly] Vallière"), "(GM secret: Émile fears 🔥 日本語) Vallière");
        assert_eq!(clean("[GMONLY]Ünï[/GMONLY]"), "(GM secret: Ünï)");
    }

    #[test]
    fn links_images_wikilinks_and_anchors() {
        assert_eq!(clean("See [[Anders de Vallière]] and [[Dockside|the docks]]."), "See Anders de Vallière and the docks.");
        assert_eq!(clean("![a map](/uploads/m.png) and <img src=\"x.png\" width=\"10\"> and [Anders](/entity/3) and [site](https://e.com)"), "a map and  and Anders and site");
        assert_eq!(clean("<a name=\"x\"></a>## Keep\n\n\n\nBe kind.  \n"), "## Keep\n\nBe kind.");
    }

    #[test]
    fn rules_directives() {
        let t = ":::tip Read this first\nBe kind.\n:::\n\n:::gm\nThe twist.\nMore.\n:::\nAfter";
        assert_eq!(clean(t), "**Read this first**\nBe kind.\n\n(GM only: The twist.\nMore.)\nAfter");
        assert_eq!(clean(":::gm\nnever closed"), "(GM only: never closed)");
        assert_eq!(clean("```\n:::gm\n```"), "```\n:::gm\n```");
    }

    #[test]
    fn secrets_are_listed_once() {
        let t = "[gmonly]He fears fire.[/gmonly] x [for=2]Ask Mira[/for] [gmonly]outer [for=1]inner[/for][/gmonly]";
        let s = secrets(t);
        assert_eq!(s.len(), 3, "{s:?}");
        assert!(s.contains(&"He fears fire.".to_string()) && s.contains(&"Ask Mira".to_string()));
        assert!(s.iter().any(|x| x.starts_with("outer ") && x.contains("inner")));
    }
}
