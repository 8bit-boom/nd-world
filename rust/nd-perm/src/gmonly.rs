//! app/rendering.py `strip_gm_only`: what a non-GM viewer may see of a text with `[gmonly]`, `[for=IDs]` and `[reveal=DATE]` blocks.
//! Returns `None` when the text has digits from other scripts (Python's `\d` and `int()` accept them; this port does not try to): the caller
//! then lets Python strip it.
use fancy_regex::{Captures, Regex};
use std::sync::OnceLock;

macro_rules! re {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| Regex::new($p).expect("pattern"))
    }};
}

fn gm_only() -> &'static Regex {
    re!(r"(?si)\[gmonly\](.*?)\[/gmonly\]")
}
fn for_re() -> &'static Regex {
    re!(r"(?si)\[for=([0-9][0-9,\s]{0,63})\]((?:(?!\[for=).)*?)\[/for\]")
}
fn reveal_re() -> &'static Regex {
    re!(r"(?si)\[reveal=(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?)\]((?:(?!\[reveal=).)*?)\[/reveal\]")
}

/// A UTC minute (year, month, day, hour, minute), comparable.
pub type Minute = (i64, i64, i64, i64, i64);

fn days_in(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        _ => 0,
    }
}

/// `parse_reveal_at` for the ASCII forms the tag allows: YYYY-MM-DD or YYYY-MM-DD[T or space or t]HH:MM.
pub fn parse_reveal_at(v: &str) -> Option<Minute> {
    let b = v.as_bytes();
    let num = |s: &[u8]| -> Option<i64> { std::str::from_utf8(s).ok()?.parse().ok() };
    if b.len() != 10 && b.len() != 16 {
        return None;
    }
    let (y, mo, d) = (num(&b[0..4])?, num(&b[5..7])?, num(&b[8..10])?);
    if y < 1 || !(1..=12).contains(&mo) || d < 1 || d > days_in(y, mo) {
        return None;
    }
    let (h, mi) = if b.len() == 16 {
        if !matches!(b[10], b'T' | b't' | b' ') {
            return None;
        }
        (num(&b[11..13])?, num(&b[14..16])?)
    } else {
        (0, 0)
    };
    if h > 23 || mi > 59 {
        return None;
    }
    Some((y, mo, d, h, mi))
}

/// `strip_gm_only(text, viewer_id, now)` where `now` is the current UTC minute (Python compares to the second; a block revealed "at
/// 10:00" is shown from 10:00:00 on both sides, so the minute is enough).
pub fn strip_gm_only(text: &str, viewer_id: Option<i64>, now: Minute) -> Option<String> {
    if text.chars().any(|c| c.is_numeric() && !c.is_ascii_digit()) {
        return None;
    }
    if text.is_empty() {
        return Some(String::new());
    }
    let mut text = gm_only().replace_all(text, "").into_owned();
    if !text.contains('[') {
        return Some(text);
    }
    for _ in 0..50 {
        let new = for_re()
            .replace_all(&text, |c: &Captures| {
                let ids: Vec<i64> = c[1].split(|ch: char| !ch.is_ascii_digit()).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect();
                if viewer_id.map_or(false, |v| ids.contains(&v)) { c[2].to_string() } else { String::new() }
            })
            .into_owned();
        if new == text {
            break;
        }
        text = new;
    }
    for _ in 0..50 {
        let new = reveal_re()
            .replace_all(&text, |c: &Captures| match parse_reveal_at(&c[1]) {
                Some(at) if at <= now => c[2].to_string(),
                _ => String::new(),
            })
            .into_owned();
        if new == text {
            break;
        }
        text = new;
    }
    let text = re!(r"(?si)\[for=[^\]]*\].*").replace_all(&text, "").into_owned();
    let text = re!(r"(?si)\[reveal=[^\]]*\].*").replace_all(&text, "").into_owned();
    Some(re!(r"(?i)\[/(?:for|reveal)\]").replace_all(&text, "").into_owned())
}
