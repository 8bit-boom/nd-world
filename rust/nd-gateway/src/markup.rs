//! `app/markup_locator.py`: find text selected on a rendered page inside the markdown it came from, and wrap / unwrap / replace /
//! resize there. Offsets are code points, as in Python. Every refusal (Python's `MarkupError`, a 422 with its message) is `Err(())`: the
//! gateway then hands the request to Python, which gives the message. Text whose regex classes (`\w`, `\s`) the two engines might read
//! differently is refused up front (`plain`) for the same reason.
use fancy_regex::Regex as FRegex;
use regex::Regex;
use std::sync::OnceLock;

type R<T> = Result<T, ()>;

const TAG: &str = r"\[/?(?:u|spoiler|gmonly|mark|color|read|for|reveal)(?:=[^\]\n]{0,20})?\]";

fn mask_re() -> &'static FRegex {
    static RE: OnceLock<FRegex> = OnceLock::new();
    RE.get_or_init(|| {
        FRegex::new(&format!(
            r"(?i)(?P<tag>{TAG})|(?P<linkend>\]\([^)\n]*\))|(?P<open>!?\[)|(?P<strong>\*\*|__|~~)|(?P<tick>`+)|(?P<em>(?<![*\w])\*(?=\S)|(?<=\S)\*(?![*\w])|(?<![\w_])_(?=\S)|(?<=\S)_(?![\w_]))"
        ))
        .unwrap()
    })
}

fn line_start_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^[ \t]{0,3}(?:(?:#{1,6}|[-*+]|\d+[.)]|>)[ \t]+)+").unwrap())
}

fn table_rule_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^[ \t]*\|?[ \t:-]+(?:\|[ \t:-]+)+\|?[ \t]*$").unwrap())
}

fn quote(c: char) -> char {
    match c {
        '\u{2018}' | '\u{2019}' => '\'',
        '\u{201c}' | '\u{201d}' => '"',
        '\u{a0}' => ' ',
        '\u{2013}' | '\u{2014}' => '-',
        _ => c,
    }
}

fn zero_width(c: char) -> bool {
    matches!(c, '\u{200b}' | '\u{200c}' | '\u{200d}' | '\u{feff}')
}

/// `str.isspace()`
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// Text both regex engines read alike: ASCII, punctuation and symbols, and the Latin / Greek / Cyrillic letters (no combining marks, no
/// separators Python's `\s` knows and Rust's does not)
pub fn plain(t: &str) -> bool {
    t.chars().all(|c| {
        if c.is_ascii() {
            return !('\x1c'..='\x1f').contains(&c);
        }
        let n = c as u32;
        if (0x300..=0x36f).contains(&n) || (0x1ab0..=0x1aff).contains(&n) || (0x1dc0..=0x1dff).contains(&n) || (0x20d0..=0x20ff).contains(&n)
            || (0xfe20..=0xfe2f).contains(&n)
        {
            return false;
        }
        !c.is_alphanumeric() || (0xc0..=0x24f).contains(&n) || (0x370..=0x3ff).contains(&n) || (0x400..=0x4ff).contains(&n)
    })
}

/// Byte offsets of every character (and the end) - to move between regex matches and code-point offsets
struct Chars<'a> {
    s: &'a str,
    b: Vec<usize>,
}

impl<'a> Chars<'a> {
    fn new(s: &'a str) -> Self {
        let mut b: Vec<usize> = s.char_indices().map(|(i, _)| i).collect();
        b.push(s.len());
        Chars { s, b }
    }
    fn len(&self) -> usize {
        self.b.len() - 1
    }
    fn ci(&self, byte: usize) -> usize {
        self.b.binary_search(&byte).unwrap_or_else(|i| i)
    }
    fn slice(&self, a: usize, z: usize) -> &'a str {
        &self.s[self.b[a]..self.b[z]]
    }
}

fn strip_py(t: &str) -> &str {
    t.trim_matches(py_space)
}

pub fn norm(text: &str) -> String {
    let mut out = String::new();
    let mut gap = false;
    for c in text.chars().filter(|c| !zero_width(*c)).map(quote) {
        if py_space(c) {
            gap = true;
            continue;
        }
        if gap && !out.is_empty() {
            out.push(' ');
        }
        gap = false;
        out.push(c);
    }
    out
}

/// `_project`: the source as the page shows it, and for each of its characters the source offset it came from
fn project(source: &str) -> R<(Vec<char>, Vec<usize>)> {
    let cs = Chars::new(source);
    let mut masked = vec![false; cs.len()];
    let mut mark = |a: usize, z: usize| {
        for m in masked.iter_mut().take(cs.ci(z)).skip(cs.ci(a)) {
            *m = true;
        }
    };
    for m in mask_re().find_iter(source) {
        let m = m.map_err(|_| ())?;
        mark(m.start(), m.end());
    }
    for rx in [line_start_re(), table_rule_re()] {
        for m in rx.find_iter(source) {
            mark(m.start(), m.end());
        }
    }
    let (mut out, mut idx) = (vec![], vec![]);
    let mut pending: Option<usize> = None;
    for (k, ch) in source.chars().enumerate() {
        if masked[k] || ch == '|' {
            continue;
        }
        let ch = quote(ch);
        if zero_width(ch) {
            continue;
        }
        if py_space(ch) {
            if !out.is_empty() && pending.is_none() {
                pending = Some(k);
            }
            continue;
        }
        if let Some(p) = pending.take() {
            out.push(' ');
            idx.push(p);
        }
        out.push(ch);
        idx.push(k);
    }
    Ok((out, idx))
}

fn find_from(hay: &[char], needle: &[char], from: usize) -> Option<usize> {
    if needle.is_empty() || needle.len() > hay.len() {
        return None;
    }
    (from..=hay.len() - needle.len()).find(|&i| hay[i..i + needle.len()] == *needle)
}

fn rstrip(v: &[char]) -> &[char] {
    let mut z = v.len();
    while z > 0 && py_space(v[z - 1]) {
        z -= 1;
    }
    &v[..z]
}

fn lstrip(v: &[char]) -> &[char] {
    let mut a = 0;
    while a < v.len() && py_space(v[a]) {
        a += 1;
    }
    &v[a..]
}

/// `locate`: one (start, end) source range per line the selection covers
pub fn locate(source: &str, selected: &str, before: &str, after: &str) -> R<Vec<(usize, usize)>> {
    if source.is_empty() {
        return Err(());
    }
    let sel: Vec<char> = norm(selected).chars().collect();
    if sel.is_empty() || sel.len() > 4000 {
        return Err(());
    }
    let (text, idx) = project(source)?;
    let mut hits = vec![];
    let mut at = find_from(&text, &sel, 0);
    while let Some(h) = at {
        hits.push(h);
        at = find_from(&text, &sel, h + 1);
    }
    if hits.is_empty() {
        return Err(());
    }
    if hits.len() > 1 {
        let b: Vec<char> = norm(before).chars().collect();
        let a: Vec<char> = norm(after).chars().collect();
        let mut scored: Vec<(usize, usize)> = hits
            .iter()
            .map(|&h| {
                let (x, y) = (rstrip(&text[..h]), rstrip(&b));
                let mut n = 0;
                while n < x.len() && n < y.len() && x[x.len() - 1 - n] == y[y.len() - 1 - n] {
                    n += 1;
                }
                let (x, y) = (lstrip(&text[h + sel.len()..]), lstrip(&a));
                let mut m = 0;
                while m < x.len() && m < y.len() && x[m] == y[m] {
                    m += 1;
                }
                (n + m, h)
            })
            .collect();
        scored.sort_by(|p, q| q.cmp(p));
        if scored[0].0 == 0 || (scored.len() > 1 && scored[0].0 == scored[1].0) {
            return Err(());
        }
        hits = vec![scored[0].1];
    }
    let a = hits[0];
    let src: Vec<char> = source.chars().collect();
    let mut groups: Vec<(usize, usize)> = vec![];
    let mut last_line: Option<usize> = None;
    for k in a..a + sel.len() {
        if text[k] == ' ' {
            continue;
        }
        let at = idx[k];
        let line = src[..at].iter().filter(|c| **c == '\n').count();
        match groups.last_mut() {
            Some(g) if last_line == Some(line) => g.1 = at + 1,
            _ => groups.push((at, at + 1)),
        }
        last_line = Some(line);
    }
    Ok(groups)
}

fn paragraph(src: &[char], s: usize, e: usize) -> (usize, usize) {
    let lo = (1..s.min(src.len())).rev().find(|&i| src[i - 1] == '\n' && src[i] == '\n' && i < s).map(|i| i + 1);
    let hi = (e..src.len().saturating_sub(1)).find(|&i| src[i] == '\n' && src[i + 1] == '\n');
    (lo.unwrap_or(0), hi.unwrap_or(src.len()))
}

enum Rx {
    Plain(&'static Regex),
    Fancy(&'static FRegex),
}

fn paired() -> &'static [Rx; 5] {
    static P: OnceLock<[Rx; 5]> = OnceLock::new();
    P.get_or_init(|| {
        let leak = |p: &str| -> &'static Regex { Box::leak(Box::new(Regex::new(p).unwrap())) };
        [
            Rx::Plain(leak(r"\*\*")),
            Rx::Plain(leak("__")),
            Rx::Plain(leak("~~")),
            Rx::Plain(leak("`")),
            Rx::Fancy(Box::leak(Box::new(FRegex::new(r"(?<!\*)\*(?!\*)").unwrap()))),
        ]
    })
}

/// `rx.finditer(source, lo, hi)` as code-point (start, end) pairs: the text ends at `hi`, the search starts at `lo` (look-behind still sees
/// what is before it)
fn marks_in(cs: &Chars, rx: &Rx, lo: usize, hi: usize) -> R<Vec<(usize, usize)>> {
    let hay = &cs.s[..cs.b[hi]];
    let mut pos = cs.b[lo];
    let mut out = vec![];
    loop {
        let m = match rx {
            Rx::Plain(r) => r.find_at(hay, pos).map(|m| (m.start(), m.end())),
            Rx::Fancy(r) => r.find_from_pos(hay, pos).map_err(|_| ())?.map(|m| (m.start(), m.end())),
        };
        let Some((a, z)) = m else { break };
        out.push((cs.ci(a), cs.ci(z)));
        if z == a {
            return Err(());
        }
        pos = z;
    }
    Ok(out)
}

fn tag_count(seg: &str, pat: &str) -> usize {
    Regex::new(&format!("(?i){pat}")).map(|r| r.find_iter(seg).count()).unwrap_or(0)
}

/// `_widen`: a range cutting through a pair of emphasis marks is widened over its partner
pub fn widen(source: &str, mut s: usize, mut e: usize) -> R<(usize, usize)> {
    let cs = Chars::new(source);
    let src: Vec<char> = source.chars().collect();
    for _ in 0..3 {
        let before = (s, e);
        for rx in paired() {
            let (lo, hi) = paragraph(&src, s, e);
            let marks = marks_in(&cs, rx, lo, hi)?;
            let inside: Vec<usize> = marks.iter().enumerate().filter(|(_, (a, b))| *a >= s && *b <= e).map(|(k, _)| k).collect();
            let (Some(&first), Some(&last)) = (inside.first(), inside.last()) else { continue };
            if first % 2 == 1 {
                s = marks[first - 1].0;
            }
            if last % 2 == 0 {
                if last + 1 >= marks.len() {
                    return Err(());
                }
                e = marks[last + 1].1;
            }
        }
        if (s, e) == before {
            break;
        }
    }
    for rx in paired() {
        let (lo, hi) = paragraph(&src, s, e);
        let n = marks_in(&cs, rx, lo, hi)?.iter().filter(|(a, _)| s <= *a && *a < e).count();
        if n % 2 == 1 {
            return Err(());
        }
    }
    let seg = cs.slice(s, e);
    for tag in ["u", "spoiler", "gmonly", "mark", "read"] {
        if tag_count(seg, &format!(r"\[{tag}(?:=[^\]\n]{{0,20}})?\]")) != tag_count(seg, &format!(r"\[/{tag}\]")) {
            return Err(());
        }
    }
    Ok((s, e))
}

fn inside_re(source: &str, s: usize, open: &str, tag: &str) -> bool {
    let cs = Chars::new(source);
    let Ok(re) = Regex::new(&format!(r"(?is){open}(.*?)\[/{tag}\]")) else { return false };
    let hit = re.find_iter(source).any(|m| cs.ci(m.start()) <= s && s < cs.ci(m.end()));
    hit
}

/// `_inside`: is offset `s` already inside a [tag]...[/tag] block?
pub fn inside(source: &str, s: usize, tag: &str) -> bool {
    inside_re(source, s, &format!(r"\[{tag}\]"), tag)
}

fn splice(src: &[char], s: usize, e: usize, with: &str) -> Vec<char> {
    let mut out: Vec<char> = src[..s].to_vec();
    out.extend(with.chars());
    out.extend_from_slice(&src[e..]);
    out
}

/// `wrap`
pub fn wrap(source: &str, selected: &str, before: &str, after: &str, action: &str) -> R<String> {
    static FOR: OnceLock<Regex> = OnceLock::new();
    static REVEAL: OnceLock<Regex> = OnceLock::new();
    let for_re = FOR.get_or_init(|| Regex::new(r"^for:(\d{1,9}(?:,\d{1,9}){0,19})$").unwrap());
    let reveal_re = REVEAL.get_or_init(|| Regex::new(r"^reveal:(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?)$").unwrap());
    if !action.is_ascii() || action.contains('\n') {
        return Err(()); // Python's `$` before a final newline, Unicode digits: Python's
    }
    let (open, close) = if let Some(color) = action.strip_prefix("color:") {
        let ok = color.len() == 7 && color.starts_with('#') && color[1..].bytes().all(|b| b.is_ascii_hexdigit());
        if !ok {
            return Err(());
        }
        (format!("[color={color}]"), "[/color]".to_string())
    } else if let Some(c) = for_re.captures(action) {
        (format!("[for={}]", &c[1]), "[/for]".to_string())
    } else if let Some(c) = reveal_re.captures(action) {
        nd_perm::gmonly::parse_reveal_at(&c[1]).ok_or(())?;
        (format!("[reveal={}]", &c[1]), "[/reveal]".to_string())
    } else {
        let (o, z) = match action {
            "gmonly" => ("[gmonly]", "[/gmonly]"),
            "spoiler" => ("[spoiler]", "[/spoiler]"),
            "mark" => ("[mark]", "[/mark]"),
            "read" => ("[read]", "[/read]"),
            "underline" => ("[u]", "[/u]"),
            "bold" => ("**", "**"),
            "italic" => ("*", "*"),
            "strike" => ("~~", "~~"),
            _ => return Err(()),
        };
        (o.to_string(), z.to_string())
    };
    let ranges: Vec<(usize, usize)> = locate(source, selected, before, after)?.into_iter().map(|(s, e)| widen(source, s, e)).collect::<R<_>>()?;
    let kind = if action.starts_with("for:") { "for" } else if action.starts_with("reveal:") { "reveal" } else { "" };
    let cs = Chars::new(source);
    if !kind.is_empty() {
        for &(s, e) in &ranges {
            if tag_count(cs.slice(s, e), &format!(r"\[{kind}[=\]]")) > 0 || inside_re(source, s, &format!(r"\[{kind}=[^\]\n]*\]"), kind) {
                return Err(());
            }
        }
    }
    if (action == "gmonly" || action == "spoiler") && ranges.iter().all(|&(s, _)| inside(source, s, action)) {
        return Err(());
    }
    let mut out: Vec<char> = source.chars().collect();
    for &(s, e) in ranges.iter().rev() {
        let mut seg: String = out[s..e].iter().collect();
        if action == "gmonly" || action == "spoiler" {
            seg = Regex::new(&format!(r"(?i)\[/?{action}\]")).map_err(|_| ())?.replace_all(&seg, "").into_owned();
        }
        out = splice(&out, s, e, &format!("{open}{seg}{close}"));
    }
    Ok(out.into_iter().collect())
}

/// `unwrap`: the n-th [tag]...[/tag] block's text without the tags
pub fn unwrap(source: &str, tag: &str, n: i64, snippet: &str) -> R<String> {
    if !["gmonly", "spoiler", "mark", "read", "for", "reveal"].contains(&tag) {
        return Err(());
    }
    let open = if tag == "for" || tag == "reveal" { format!(r"\[{tag}=[^\]\n]*\]") } else { format!(r"\[{tag}\]") };
    let re = Regex::new(&format!(r"(?is){open}(.*?)\[/{tag}\]")).map_err(|_| ())?;
    let blocks: Vec<regex::Captures> = re.captures_iter(source).collect();
    if n < 0 || n as usize >= blocks.len() {
        return Err(());
    }
    let m = &blocks[n as usize];
    let inner = m.get(1).ok_or(())?.as_str();
    if !snippet.is_empty() {
        let want: Vec<char> = norm(snippet).chars().take(30).collect();
        let (shown, _) = project(inner)?;
        if !want.is_empty() && find_from(&shown, &want, 0).is_none() {
            return Err(());
        }
    }
    let all = m.get(0).ok_or(())?;
    Ok(format!("{}{}{}", &source[..all.start()], inner, &source[all.end()..]))
}

/// `replace`: one line's selection swapped for `replacement`
pub fn replace(source: &str, selected: &str, before: &str, after: &str, replacement: &str) -> R<String> {
    let replacement = strip_py(replacement);
    if replacement.is_empty() || replacement.chars().count() > 6000 {
        return Err(());
    }
    let ranges = locate(source, selected, before, after)?;
    if ranges.len() != 1 {
        return Err(());
    }
    let (s, e) = widen(source, ranges[0].0, ranges[0].1)?;
    let src: Vec<char> = source.chars().collect();
    Ok(splice(&src, s, e, replacement).into_iter().collect())
}

/// `resize_image`: the n-th picture sized to `pct` percent
pub fn resize_image(source: &str, n: i64, url: &str, pct: i64) -> R<String> {
    static IMG: OnceLock<Regex> = OnceLock::new();
    let re = IMG.get_or_init(|| Regex::new(r#"!\[([^\]\n]{0,300})\]\(([^)\s]{1,2000})(?:\s+"([^"\n]{0,20})")?\)"#).unwrap());
    let pct = pct.clamp(10, 300);
    let refs: Vec<regex::Captures> = re.captures_iter(source).filter(|c| !matches!(c.get(3).map(|m| m.as_str()), Some("audio") | Some("video"))).collect();
    if n < 0 || n as usize >= refs.len() {
        return Err(());
    }
    let m = &refs[n as usize];
    let found = &m[2];
    if !url.is_empty() && !(url == found || url.ends_with(found) || found.ends_with(url) || url.contains(found)) {
        return Err(());
    }
    let title = if pct == 100 { String::new() } else { format!(r#" "size:{pct}""#) };
    if let Some(t) = m.get(3) {
        if !t.as_str().starts_with("size:") && !t.as_str().is_empty() {
            return Err(());
        }
    }
    let all = m.get(0).ok_or(())?;
    Ok(format!("{}![{}]({found}{title}){}", &source[..all.start()], &m[1], &source[all.end()..]))
}
