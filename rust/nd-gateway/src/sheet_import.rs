//! The sheet an AI character draft can start from (app/routers/character_ai.py `_extract_file_text`, app/rendering.py `html_to_sheet_text` /
//! `decode_html_bytes`): a text / markdown / JSON file as it is, a PDF through lopdf, an HTML character sheet read down to its text and field
//! values, or a photo read by the model. Next to Python only the exact cases (text, markdown, plain JSON, a photo) are answered here; a PDF and an
//! HTML page are read here only without Python (ND_PYTHON_FREE), where Python's reader is not there to do it - lopdf's spacing and this HTML reader's
//! corner cases (a malformed page) can differ from pypdf's and `html.parser`'s.

use std::sync::OnceLock;

use serde_json::{json, Value};

use crate::ai::Backend;

pub const MAX_IMPORT_BYTES: usize = 12 * 1024 * 1024;
const SHEET_TEXT_MAX_CHARS: usize = 200_000;

/// `Path(filename).suffix`, as `_extract_file_text` is given it (lowercased there)
fn suffix(filename: &str) -> String {
    let base = filename.rsplit('/').next().unwrap_or("");
    match base.rfind('.') {
        Some(i) if i > 0 && i < base.len() - 1 => base[i..].to_lowercase(),
        _ => String::new(),
    }
}

/// The text of the uploaded sheet, or Python's message for a file it cannot read (a 400). None: Python's (a case this port does not read the same way)
pub async fn sheet_text(data: &[u8], filename: &str, hint: &str, b: &Backend) -> Option<Result<String, String>> {
    let ext = suffix(filename);
    match ext.as_str() {
        ".md" | ".txt" | ".markdown" => Some(Ok(String::from_utf8_lossy(data).into_owned())),
        ".json" => {
            let text = String::from_utf8_lossy(data).into_owned();
            if !crate::edits::plain_numbers(text.as_bytes()) {
                return None;
            }
            Some(Ok(match serde_json::from_str::<Value>(&text) {
                Ok(v) => nd_render::py_dumps_indent_as(&v, 1, false),
                Err(_) => text,
            }))
        }
        ".pdf" => {
            let pages = match crate::pdftext::extract_pages(data, Some(20))? {
                Ok(p) => p,
                Err(()) => return Some(Err("Reading this PDF failed (PdfReadError) — try exporting it as text, or paste the sheet's contents.".into())),
            };
            let text = crate::edits::py_strip_pub(&pages.join("\n"));
            if text.is_empty() {
                return Some(Err("This PDF has no extractable text (it may be a scan) — upload its page as an IMAGE instead so the AI can read it visually.".into()));
            }
            Some(Ok(text))
        }
        ".html" | ".htm" => {
            if !crate::config::python_free() {
                return None;
            }
            let raw = decode_html_bytes(data)?;
            let text = html_to_sheet_text(&raw);
            if text.trim().is_empty() {
                return Some(Err("This HTML file has no readable text (the page may be built by scripts that did not run when it was saved) — print the finished page to PDF, or take a screenshot and upload that as an image instead.".into()));
            }
            Some(Ok(text))
        }
        ".png" | ".jpg" | ".jpeg" | ".webp" => {
            let draft = match crate::ai::draft_character_from_images(b, &[data.to_vec()], hint).await {
                Ok(d) => d,
                Err(m) => return Some(Err(m)),
            };
            Some(Ok(nd_render::py_dumps_indent_as(&draft, 1, false)))
        }
        ".doc" | ".docx" | ".odt" => Some(Err("Word documents aren't readable in this deployment — export the sheet to PDF or plain text, or paste its contents.".into())),
        other => Some(Err(format!("Unsupported file type {} — use PDF, HTML, MD, TXT, JSON, an image, or paste the text.", crate::ai::py_repr(&json!(other))))),
    }
}

// ── an HTML page's bytes -> text (`decode_html_bytes`) ──

const CP1252_HIGH: [char; 32] = ['\u{20AC}', '\u{FFFD}', '\u{201A}', '\u{0192}', '\u{201E}', '\u{2026}', '\u{2020}', '\u{2021}', '\u{02C6}', '\u{2030}', '\u{0160}', '\u{2039}', '\u{0152}', '\u{FFFD}', '\u{017D}', '\u{FFFD}',
    '\u{FFFD}', '\u{2018}', '\u{2019}', '\u{201C}', '\u{201D}', '\u{2022}', '\u{2013}', '\u{2014}', '\u{02DC}', '\u{2122}', '\u{0161}', '\u{203A}', '\u{0153}', '\u{FFFD}', '\u{017E}', '\u{0178}'];

fn cp1252(data: &[u8]) -> String {
    data.iter().map(|&b| if (0x80..0xA0).contains(&b) { CP1252_HIGH[(b - 0x80) as usize] } else { b as char }).collect()
}

/// None: a declared charset this port does not decode
pub fn decode_html_bytes(data: &[u8]) -> Option<String> {
    if let Some(rest) = data.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return Some(String::from_utf8_lossy(rest).into_owned());
    }
    if data.starts_with(&[0xFF, 0xFE]) || data.starts_with(&[0xFE, 0xFF]) {
        let le = data[0] == 0xFF;
        let units: Vec<u16> = data[2..].chunks_exact(2).map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
        return Some(String::from_utf16_lossy(&units));
    }
    if let Ok(s) = std::str::from_utf8(data) {
        return Some(s.to_string());
    }
    static CS: OnceLock<regex::bytes::Regex> = OnceLock::new();
    let re = CS.get_or_init(|| regex::bytes::Regex::new(r#"(?i)charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)"#).unwrap());
    if let Some(m) = re.captures(&data[..data.len().min(4096)]) {
        let name = String::from_utf8_lossy(&m[1]).to_lowercase().replace('_', "-");
        return match name.as_str() {
            "utf-8" | "utf8" => Some(String::from_utf8_lossy(data).into_owned()),
            "ascii" | "us-ascii" => Some(data.iter().map(|&b| if b < 128 { b as char } else { '\u{FFFD}' }).collect()),
            "iso-8859-1" | "latin1" | "latin-1" | "l1" => Some(data.iter().map(|&b| b as char).collect()),
            "windows-1252" | "cp1252" => Some(cp1252(data)),
            _ => None,
        };
    }
    Some(cp1252(data))
}

// ── an HTML character sheet -> plain text (`html_to_sheet_text`) ──

const SKIP_TAGS: [&str; 10] = ["script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "canvas", "button"];
const BLOCK_TAGS: [&str; 30] = ["p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "form", "fieldset", "legend", "table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl", "dt",
    "dd", "blockquote", "pre", "details", "summary", "figure", "figcaption", "address", "caption"];
const SPACED_TAGS: [&str; 16] = ["p", "table", "ul", "ol", "dl", "form", "fieldset", "blockquote", "pre", "section", "article", "details", "figure", "h1", "h2", "h3"];
const SPACED_TAGS_MORE: [&str; 4] = ["h4", "h5", "h6", "hr"];
const SKIP_INPUTS: [&str; 8] = ["hidden", "password", "file", "button", "submit", "reset", "image", "color"];

fn spaced(tag: &str) -> bool {
    SPACED_TAGS.contains(&tag) || SPACED_TAGS_MORE.contains(&tag)
}

#[derive(Default)]
struct Sheet {
    parts: Vec<String>,
    skip: Vec<String>,
    cell_in_row: bool,
    title: Vec<String>,
    in_title: bool,
    select: Option<(String, Vec<(String, bool)>)>,
    option: Option<(String, bool)>,
    textarea: Option<(String, String)>,
    pre: u32,
}

type Attrs = Vec<(String, String)>;

fn attr<'a>(a: &'a Attrs, k: &str) -> Option<&'a str> {
    a.iter().rev().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
}

impl Sheet {
    fn nl(&mut self) {
        if self.parts.last().is_some_and(|l| !l.ends_with('\n')) {
            self.parts.push("\n".into());
        }
    }

    fn blank(&mut self) {
        if self.parts.is_empty() {
            return;
        }
        let tail: String = self.parts[self.parts.len().saturating_sub(2)..].concat();
        if !tail.ends_with("\n\n") {
            self.parts.push(if tail.ends_with('\n') { "\n".into() } else { "\n\n".into() });
        }
    }

    fn add(&mut self, t: &str) {
        if !t.is_empty() {
            self.parts.push(t.to_string());
        }
    }

    fn field_name(a: &Attrs) -> String {
        for k in ["aria-label", "name", "id", "title", "placeholder"] {
            if let Some(v) = attr(a, k).filter(|v| !v.is_empty()) {
                return crate::edits::py_strip_pub(v);
            }
        }
        String::new()
    }

    fn start(&mut self, tag: &str, a: &Attrs) {
        if !self.skip.is_empty() {
            if SKIP_TAGS.contains(&tag) {
                self.skip.push(tag.to_string());
            }
            return;
        }
        if SKIP_TAGS.contains(&tag) {
            self.skip.push(tag.to_string());
            return;
        }
        match tag {
            "title" => self.in_title = true,
            "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => {
                self.blank();
                let n: usize = tag[1..].parse().unwrap_or(1);
                self.add(&format!("{} ", "#".repeat(n)));
            }
            "li" => {
                self.nl();
                self.add("- ");
            }
            "br" => self.nl(),
            "hr" => {
                self.blank();
                self.add("---");
                self.blank();
            }
            "td" | "th" => {
                if self.cell_in_row {
                    self.add(" | ");
                }
                self.cell_in_row = true;
            }
            "tr" => {
                self.nl();
                self.cell_in_row = false;
            }
            "input" => self.input(a),
            "select" => self.select = Some((Self::field_name(a), vec![])),
            "option" if self.select.is_some() => self.option = Some((String::new(), attr(a, "selected").is_some())),
            "textarea" => self.textarea = Some((Self::field_name(a), String::new())),
            "pre" => self.pre += 1,
            _ => {}
        }
        if spaced(tag) && tag != "hr" && !tag.starts_with('h') {
            self.blank();
        } else if BLOCK_TAGS.contains(&tag) {
            self.nl();
        }
    }

    fn end(&mut self, tag: &str) {
        if !self.skip.is_empty() {
            if self.skip.last().map(String::as_str) == Some(tag) {
                self.skip.pop();
            }
            return;
        }
        match tag {
            "title" => self.in_title = false,
            "option" if self.select.is_some() && self.option.is_some() => {
                let (t, s) = self.option.take().unwrap();
                self.select.as_mut().unwrap().1.push((crate::edits::py_strip_pub(&t), s));
            }
            "select" if self.select.is_some() => {
                let (name, opts) = self.select.take().unwrap();
                let chosen = opts.iter().find(|(_, s)| *s).map(|(t, _)| t.clone()).or_else(|| opts.first().map(|(t, _)| t.clone())).unwrap_or_default();
                if !chosen.is_empty() {
                    self.add(&format!("[{}{chosen}] ", if name.is_empty() { String::new() } else { format!("{name}: ") }));
                }
            }
            "textarea" if self.textarea.is_some() => {
                let (name, text) = self.textarea.take().unwrap();
                let body = text.trim_matches(|c| c == '\r' || c == '\n').trim_end().to_string();
                if !crate::edits::py_strip_pub(&body).is_empty() {
                    self.nl();
                    self.add(&format!("{}{body}", if name.is_empty() { String::new() } else { format!("{name}: ") }));
                    self.nl();
                }
            }
            "pre" => self.pre = self.pre.saturating_sub(1),
            _ => {}
        }
        if matches!(tag, "h1" | "h2" | "h3" | "h4" | "h5" | "h6") || spaced(tag) {
            self.blank();
        } else if BLOCK_TAGS.contains(&tag) || tag == "li" {
            self.nl();
        }
    }

    fn input(&mut self, a: &Attrs) {
        let kind = crate::edits::py_strip_pub(attr(a, "type").filter(|t| !t.is_empty()).unwrap_or("text")).to_lowercase();
        if SKIP_INPUTS.contains(&kind.as_str()) {
            return;
        }
        let name = Self::field_name(a);
        if kind == "checkbox" || kind == "radio" {
            let t = format!("[{}] {name} ", if attr(a, "checked").is_some() { "x" } else { " " });
            self.add(&format!("{} ", t.trim_end()));
            return;
        }
        let value = crate::edits::py_strip_pub(attr(a, "value").unwrap_or(""));
        if !value.is_empty() {
            self.add(&format!("[{}{value}] ", if name.is_empty() { String::new() } else { format!("{name}: ") }));
        }
    }

    fn data(&mut self, data: &str) {
        if !self.skip.is_empty() {
            return;
        }
        if self.in_title {
            self.title.push(data.to_string());
        } else if let Some((_, t)) = self.textarea.as_mut() {
            t.push_str(data);
        } else if let Some((t, _)) = self.option.as_mut() {
            t.push_str(data);
        } else if self.select.is_some() {
        } else if self.pre > 0 {
            self.add(data);
        } else {
            static WS: OnceLock<regex::Regex> = OnceLock::new();
            let text = WS.get_or_init(|| regex::Regex::new(r"\s+").unwrap()).replace_all(data, " ").into_owned();
            if text == " " && self.parts.last().is_none_or(|l| l.ends_with('\n') || l.ends_with(' ')) {
                return;
            }
            self.add(&text);
        }
    }
}

/// `html.parser.HTMLParser(convert_charrefs=True)` as far as a sheet needs it: tags with their attributes, text with its character references
/// decoded, comments / declarations dropped, <script> and <style> bodies skipped raw
fn parse(html: &str, sheet: &mut Sheet) {
    let b = html.as_bytes();
    let (mut i, mut text_start) = (0usize, 0usize);
    let flush = |sheet: &mut Sheet, from: usize, to: usize| {
        if to > from {
            sheet.data(&html_escape::decode_html_entities(&html[from..to]));
        }
    };
    while i < b.len() {
        if b[i] != b'<' {
            i += 1;
            continue;
        }
        let rest = &html[i..];
        if rest.starts_with("<!--") {
            flush(sheet, text_start, i);
            i = rest.find("-->").map_or(b.len(), |e| i + e + 3);
            text_start = i;
        } else if rest.starts_with("<!") || rest.starts_with("<?") {
            flush(sheet, text_start, i);
            i = rest.find('>').map_or(b.len(), |e| i + e + 1);
            text_start = i;
        } else if rest.starts_with("</") && rest[2..].chars().next().is_some_and(|c| c.is_ascii_alphabetic()) {
            flush(sheet, text_start, i);
            let end = rest.find('>').map_or(b.len(), |e| i + e + 1);
            let name: String = rest[2..].chars().take_while(|c| !c.is_whitespace() && *c != '>' && *c != '/').collect::<String>().to_lowercase();
            sheet.end(&name);
            i = end;
            text_start = i;
        } else if rest[1..].chars().next().is_some_and(|c| c.is_ascii_alphabetic()) {
            let Some((name, attrs, self_close, len)) = start_tag(rest) else {
                i += 1;
                continue;
            };
            flush(sheet, text_start, i);
            sheet.start(&name, &attrs);
            let void = ["input", "br", "hr", "img", "meta", "link"];
            if self_close && !void.contains(&name.as_str()) {
                sheet.end(&name);
            }
            i += len;
            if (name == "script" || name == "style") && !self_close {
                // raw text up to the matching close tag
                let lower = html[i..].to_ascii_lowercase();
                let close = format!("</{name}");
                let at = lower.find(&close).map_or(b.len(), |p| i + p);
                i = at;
            }
            text_start = i;
        } else {
            i += 1;
        }
    }
    flush(sheet, text_start, b.len());
}

/// (lowercase name, attributes, ends with "/>", bytes used) for the start tag at the head of `s`; None when it never closes
fn start_tag(s: &str) -> Option<(String, Attrs, bool, usize)> {
    let b = s.as_bytes();
    let mut i = 1;
    while i < b.len() && !b[i].is_ascii_whitespace() && b[i] != b'>' && b[i] != b'/' {
        i += 1;
    }
    let name = s[1..i].to_lowercase();
    let mut attrs: Attrs = vec![];
    loop {
        while i < b.len() && (b[i].is_ascii_whitespace() || b[i] == b'/') && !(b[i] == b'/' && b.get(i + 1) == Some(&b'>')) {
            i += 1;
        }
        if i >= b.len() {
            return None;
        }
        if b[i] == b'>' {
            return Some((name, attrs, false, i + 1));
        }
        if b[i] == b'/' {
            return Some((name, attrs, true, i + 2));
        }
        let ns = i;
        while i < b.len() && !b[i].is_ascii_whitespace() && b[i] != b'=' && b[i] != b'>' && !(b[i] == b'/' && b.get(i + 1) == Some(&b'>')) {
            i += 1;
        }
        let key = s[ns..i].to_lowercase();
        let mut j = i;
        while j < b.len() && b[j].is_ascii_whitespace() {
            j += 1;
        }
        let mut value = String::new();
        if j < b.len() && b[j] == b'=' {
            j += 1;
            while j < b.len() && b[j].is_ascii_whitespace() {
                j += 1;
            }
            if j < b.len() && (b[j] == b'"' || b[j] == b'\'') {
                let q = b[j];
                let vs = j + 1;
                let mut k = vs;
                while k < b.len() && b[k] != q {
                    k += 1;
                }
                if k >= b.len() {
                    return None;
                }
                value = html_escape::decode_html_entities(&s[vs..k]).into_owned();
                j = k + 1;
            } else {
                let vs = j;
                while j < b.len() && !b[j].is_ascii_whitespace() && b[j] != b'>' {
                    j += 1;
                }
                value = html_escape::decode_html_entities(&s[vs..j]).into_owned();
            }
            i = j;
        }
        if !key.is_empty() {
            attrs.push((key, value));
        }
    }
}

/// Python's `str.splitlines()` line boundaries
fn splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let chars: Vec<(usize, char)> = s.char_indices().collect();
    let mut k = 0;
    while k < chars.len() {
        let (i, c) = chars[k];
        let brk = matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}');
        if brk {
            out.push(&s[start..i]);
            let mut next = i + c.len_utf8();
            if c == '\r' && chars.get(k + 1).is_some_and(|(_, n)| *n == '\n') {
                next += 1;
                k += 1;
            }
            start = next;
        }
        k += 1;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `html_to_sheet_text`: scripts / styles / hidden and password fields dropped, headings / lists / table rows kept as lines, the VALUES of form
/// fields written next to their names; capped at 200,000 characters
pub fn html_to_sheet_text(raw: &str) -> String {
    if raw.trim().is_empty() {
        return String::new();
    }
    let mut sheet = Sheet::default();
    parse(raw, &mut sheet);
    let mut text = sheet.parts.concat();
    let title = sheet.title.concat().split_whitespace().collect::<Vec<_>>().join(" ");
    if !title.is_empty() {
        text = format!("Page title: {title}\n{text}");
    }
    let text = splitlines(&text).iter().map(|l| l.trim_end()).collect::<Vec<_>>().join("\n");
    static NL: OnceLock<regex::Regex> = OnceLock::new();
    let text = crate::edits::py_strip_pub(&NL.get_or_init(|| regex::Regex::new(r"\n{3,}").unwrap()).replace_all(&text, "\n\n"));
    if text.chars().count() > SHEET_TEXT_MAX_CHARS {
        let cut: String = text.chars().take(SHEET_TEXT_MAX_CHARS).collect();
        let head = cut.rsplit_once('\n').map_or(cut.as_str(), |(h, _)| h).trim_end().to_string();
        return format!("{head}\n(truncated)");
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `html_to_sheet_text` on pages whose Python answer is recorded in sheet_import_cases.json (scripts/gen_sheet_html_cases.py)
    #[test]
    fn the_html_reader_gives_pythons_text() {
        let cases: Vec<Value> = serde_json::from_str(include_str!("sheet_import_cases.json")).unwrap();
        let mut bad = vec![];
        for (i, c) in cases.iter().enumerate() {
            let (html, want) = (c["html"].as_str().unwrap(), c["text"].as_str().unwrap());
            let got = html_to_sheet_text(html);
            if got != want {
                bad.push(format!("case {i}: {:?}\n  python: {:?}\n  rust:   {:?}", &html.chars().take(80).collect::<String>(), &want.chars().take(200).collect::<String>(), &got.chars().take(200).collect::<String>()));
            }
        }
        assert!(bad.is_empty(), "{}", bad.join("\n"));
    }

    #[test]
    fn declared_charsets_decode() {
        assert_eq!(decode_html_bytes(b"\xEF\xBB\xBFcaf\xC3\xA9").unwrap(), "café");
        assert_eq!(decode_html_bytes(b"<meta charset=iso-8859-1>caf\xE9").unwrap(), "<meta charset=iso-8859-1>café");
        assert_eq!(decode_html_bytes(b"caf\xE9 \x93q\x94").unwrap(), "café \u{201C}q\u{201D}");
        assert!(decode_html_bytes(b"<meta charset=shift_jis>\x93").is_none());
    }
}
