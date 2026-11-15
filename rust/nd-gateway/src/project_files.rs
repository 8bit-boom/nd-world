//! Text out of the files a GM attaches to an AI chat project (the /ai sidebar's Projects, like Unsloth's project files).
//!
//! The upload is read once and only its text is kept (table `ai_project_files`); the original is never stored. Plain text of any kind (txt, md, csv, json,
//! yaml, logs, source code ...) is taken as it is, HTML is reduced to its readable text, `.docx` to its paragraphs, `.pdf` to its pages' text (the same
//! reader the chat attachments use). Anything else is accepted when it looks like text (no NUL bytes) and refused otherwise.

use std::io::Read;
use std::sync::OnceLock;

use regex::Regex;

/// The largest upload read (bigger: refused before it is read)
pub const MAX_FILE_BYTES: u64 = 5 * 1024 * 1024;
/// Files one project may hold
pub const MAX_FILES: i64 = 40;
/// Characters of text all of a project's files may hold together
pub const MAX_TOTAL_CHARS: i64 = 2_000_000;
/// Characters kept of one file (the rest is cut, with a note at the end of the kept text)
pub const MAX_FILE_CHARS: usize = 400_000;

fn ext_of(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or(name);
    match base.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() => ext.to_lowercase(),
        _ => String::new(),
    }
}

/// The text of one uploaded file, or the reason it cannot be used (shown to the GM as it is)
pub fn extract(name: &str, bytes: &[u8]) -> Result<String, String> {
    let ext = ext_of(name);
    let raw = match ext.as_str() {
        "pdf" => match crate::pdftext::extract(bytes, None) {
            Some(Ok(t)) => t,
            Some(Err(())) => return Err(format!("“{name}” could not be read as a PDF (scanned, protected or damaged?)")),
            None => return Err("PDF files cannot be read by this server".to_string()),
        },
        "docx" => docx_text(bytes).ok_or_else(|| format!("“{name}” could not be read as a Word document"))?,
        "html" | "htm" | "xhtml" => html_to_text(&decode(bytes)),
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "avif" | "bmp" | "ico" | "mp3" | "wav" | "ogg" | "flac" | "m4a" | "mp4" | "mkv" | "webm" | "mov" | "zip" | "7z" | "gz" | "rar" | "exe" => {
            return Err(format!("“{name}” is not a text file (add pictures and audio to the chat itself)"));
        }
        _ => {
            let utf16 = bytes.len() >= 2 && (bytes[..2] == [0xFF, 0xFE] || bytes[..2] == [0xFE, 0xFF]);
            if !utf16 && bytes.iter().take(8192).any(|b| *b == 0) {
                return Err(format!("“{name}” does not look like a text file"));
            }
            decode(bytes)
        }
    };
    let text = tidy(&raw);
    if text.is_empty() {
        return Err(format!("There is no text in “{name}”"));
    }
    if text.chars().count() > MAX_FILE_CHARS {
        let cut: String = text.chars().take(MAX_FILE_CHARS).collect();
        return Ok(format!("{cut}\n[… cut here: the file is longer than {MAX_FILE_CHARS} characters]"));
    }
    Ok(text)
}

/// UTF-8 (a byte order mark dropped, bad bytes replaced); UTF-16 with a byte order mark is read as that
fn decode(bytes: &[u8]) -> String {
    if bytes.len() >= 2 && (bytes[..2] == [0xFF, 0xFE] || bytes[..2] == [0xFE, 0xFF]) {
        let le = bytes[0] == 0xFF;
        let units: Vec<u16> = bytes[2..].chunks_exact(2).map(|p| if le { u16::from_le_bytes([p[0], p[1]]) } else { u16::from_be_bytes([p[0], p[1]]) }).collect();
        return String::from_utf16_lossy(&units);
    }
    let b = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(bytes);
    String::from_utf8_lossy(b).into_owned()
}

/// Newlines as \n, trailing spaces off every line, runs of blank lines folded into one, the ends trimmed
fn tidy(s: &str) -> String {
    let s = s.replace("\r\n", "\n").replace('\r', "\n").replace('\u{0}', "");
    let mut out = String::with_capacity(s.len());
    let mut blank = 0;
    for line in s.split('\n') {
        let line = line.trim_end();
        if line.trim().is_empty() {
            blank += 1;
            if blank > 1 {
                continue;
            }
        } else {
            blank = 0;
        }
        out.push_str(line);
        out.push('\n');
    }
    out.trim().to_string()
}

fn re(cell: &'static OnceLock<Regex>, pat: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pat).expect("project_files regex"))
}

/// The readable text of an HTML page: scripts, styles and comments dropped, block ends as line breaks, list items as "- ", tags removed, entities decoded
pub fn html_to_text(h: &str) -> String {
    static COMMENT: OnceLock<Regex> = OnceLock::new();
    static SCRIPT: OnceLock<Regex> = OnceLock::new();
    static STYLE: OnceLock<Regex> = OnceLock::new();
    static LI: OnceLock<Regex> = OnceLock::new();
    static BREAK: OnceLock<Regex> = OnceLock::new();
    static CELL: OnceLock<Regex> = OnceLock::new();
    static TAG: OnceLock<Regex> = OnceLock::new();
    let t = re(&COMMENT, r"(?s)<!--.*?-->").replace_all(h, "");
    let t = re(&SCRIPT, r"(?is)<script\b.*?</script\s*>").replace_all(&t, "");
    let t = re(&STYLE, r"(?is)<style\b.*?</style\s*>").replace_all(&t, "");
    let t = re(&LI, r"(?i)<\s*li\b[^>]*>").replace_all(&t, "\n- ");
    let t = re(&BREAK, r"(?i)<\s*(?:br|hr|/p|/div|/tr|/h[1-6]|/ul|/ol|/table|/section|/article|/blockquote|/pre|p|div|h[1-6]|tr|table|section|article|blockquote|pre)\b[^>]*>").replace_all(&t, "\n");
    let t = re(&CELL, r"(?i)<\s*/t[dh]\s*>").replace_all(&t, "\t");
    let t = re(&TAG, r"(?s)<[^>]*>").replace_all(&t, "");
    unescape(&t)
}

/// `&amp;`-style and numeric character references
pub fn unescape(s: &str) -> String {
    static ENT: OnceLock<Regex> = OnceLock::new();
    re(&ENT, r"&(#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[a-zA-Z]{2,8});")
        .replace_all(s, |c: &regex::Captures| {
            let e = &c[1];
            let ch = if let Some(h) = e.strip_prefix("#x").or_else(|| e.strip_prefix("#X")) {
                u32::from_str_radix(h, 16).ok().and_then(char::from_u32)
            } else if let Some(d) = e.strip_prefix('#') {
                d.parse::<u32>().ok().and_then(char::from_u32)
            } else {
                match e {
                    "amp" => Some('&'),
                    "lt" => Some('<'),
                    "gt" => Some('>'),
                    "quot" => Some('"'),
                    "apos" => Some('\''),
                    "nbsp" => Some(' '),
                    "mdash" => Some('—'),
                    "ndash" => Some('–'),
                    "hellip" => Some('…'),
                    "lsquo" => Some('‘'),
                    "rsquo" => Some('’'),
                    "ldquo" => Some('“'),
                    "rdquo" => Some('”'),
                    "copy" => Some('©'),
                    "middot" => Some('·'),
                    "bull" => Some('•'),
                    _ => None,
                }
            };
            ch.map(|c| c.to_string()).unwrap_or_else(|| c[0].to_string())
        })
        .into_owned()
}

/// The paragraphs of a `.docx` (word/document.xml): paragraph ends and line breaks as newlines, tabs as tabs, the markup removed
fn docx_text(bytes: &[u8]) -> Option<String> {
    let mut zip = zip::ZipArchive::new(std::io::Cursor::new(bytes)).ok()?;
    let mut xml = String::new();
    zip.by_name("word/document.xml").ok()?.take(40 * 1024 * 1024).read_to_string(&mut xml).ok()?;
    static PARA: OnceLock<Regex> = OnceLock::new();
    static TAB: OnceLock<Regex> = OnceLock::new();
    static TAG: OnceLock<Regex> = OnceLock::new();
    let t = re(&PARA, r"(?i)</w:p>|<w:br\s*/?>|<w:cr\s*/?>").replace_all(&xml, "\n");
    let t = re(&TAB, r"(?i)<w:tab\s*/?>").replace_all(&t, "\t");
    let t = re(&TAG, r"(?s)<[^>]*>").replace_all(&t, "");
    Some(unescape(&t))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_text_of_any_kind_is_kept_as_it_is() {
        assert_eq!(extract("notes.md", b"# Title\r\n\r\n\r\n\r\nbody  \r\n").unwrap(), "# Title\n\nbody");
        assert_eq!(extract("data.csv", "a,b\n1,é\n".as_bytes()).unwrap(), "a,b\n1,é");
        assert_eq!(extract("Makefile", b"all:\n\techo hi\n").unwrap(), "all:\n\techo hi");
        // a byte order mark, and UTF-16 with one
        assert_eq!(extract("a.txt", b"\xEF\xBB\xBFhello").unwrap(), "hello");
        let u16: Vec<u8> = [0xFFu8, 0xFE].into_iter().chain("hi é".encode_utf16().flat_map(|u| u.to_le_bytes())).collect();
        assert_eq!(extract("w.txt", &u16).unwrap(), "hi é");
    }

    #[test]
    fn html_becomes_readable_text() {
        let h = "<html><head><title>T</title><style>p{color:red}</style><script>var a = '<p>no</p>';</script></head>\
                 <body><!-- hidden --><h1>Harbor &amp; Docks</h1><p>First<br>second &lt;line&gt;</p><ul><li>one</li><li>two&nbsp;&#33;</li></ul>\
                 <table><tr><td>a</td><td>b</td></tr></table></body></html>";
        let t = extract("page.html", h.as_bytes()).unwrap();
        assert!(t.contains("Harbor & Docks"), "{t}");
        assert!(t.contains("First\nsecond <line>"), "{t}");
        assert!(t.contains("- one") && t.contains("- two !"), "{t}");
        assert!(!t.contains("color:red") && !t.contains("var a") && !t.contains("hidden") && !t.contains("<p>") && !t.contains("<li>"), "{t}");
        assert!(t.contains("- one\n- two"), "{t}");
        assert!(t.contains("a\tb"), "{t}");
    }

    #[test]
    fn a_word_document_gives_its_paragraphs() {
        use std::io::Write;
        let mut buf = Vec::new();
        {
            let mut z = zip::ZipWriter::new(std::io::Cursor::new(&mut buf));
            z.start_file("word/document.xml", zip::write::SimpleFileOptions::default()).unwrap();
            z.write_all(b"<w:document><w:body><w:p><w:r><w:t>First &amp; one</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r><w:tab/><w:r><w:t>cell</w:t></w:r></w:p></w:body></w:document>").unwrap();
            z.finish().unwrap();
        }
        assert_eq!(extract("brief.docx", &buf).unwrap(), "First & one\nSecond\tcell");
        assert!(extract("broken.docx", b"not a zip").is_err());
    }

    #[test]
    fn what_is_not_text_is_refused_with_a_reason() {
        assert!(extract("pic.png", b"\x89PNG").unwrap_err().contains("not a text file"));
        assert!(extract("blob.bin", b"ab\0cd").unwrap_err().contains("does not look like a text file"));
        assert!(extract("empty.txt", b" \n\n ").unwrap_err().contains("no text"));
    }

    #[test]
    fn a_very_long_file_is_cut_with_a_note() {
        let big = "word ".repeat(MAX_FILE_CHARS);
        let t = extract("big.txt", big.as_bytes()).unwrap();
        assert!(t.ends_with("characters]") && t.chars().count() < MAX_FILE_CHARS + 100);
    }

    #[test]
    fn entities_that_are_not_known_stay_as_written() {
        assert_eq!(unescape("a &unknown; &#x41; &#66; &amp;amp;"), "a &unknown; A B &amp;");
    }
}
