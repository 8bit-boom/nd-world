//! PDF text for the places Python reads it with pypdf (AI attachments, the rulebook a system draft starts from, a note imported from a PDF).
//! lopdf's text extraction is not pypdf's - spacing and line breaks differ on some documents - so this is used only in an image with no Python
//! (ND_PYTHON_FREE), where the alternative is not reading the PDF at all; next to Python these requests stay Python's.

/// All pages (or the first `max_pages`) joined by a blank line, as `"\\n\\n".join(page.extract_text() or "")`.
/// None: not applicable here (Python is available, so it reads the file); Some(Err): the document could not be read.
pub fn extract(data: &[u8], max_pages: Option<usize>) -> Option<Result<String, ()>> {
    if !crate::config::python_free() {
        return None;
    }
    let doc = match lopdf::Document::load_mem(data) {
        Ok(d) => d,
        Err(_) => return Some(Err(())),
    };
    if doc.is_encrypted() {
        return Some(Err(()));
    }
    let numbers: Vec<u32> = doc.get_pages().keys().copied().collect();
    let take = max_pages.unwrap_or(usize::MAX);
    let texts: Vec<String> = numbers.into_iter().take(take).map(|n| doc.extract_text(&[n]).unwrap_or_default()).collect();
    Some(Ok(texts.join("\n\n")))
}
