//! Splitting long articles into pieces that fit a training sequence, each keeping the title.

/// A rough token count (about 4 characters per token for English and Latin-script text; counts characters, not bytes).
pub fn approx_tokens(text: &str) -> usize {
    text.chars().count().div_ceil(4)
}

fn split_hard(text: &str, max_chars: usize) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    chars.chunks(max_chars.max(1)).map(|c| c.iter().collect()).collect()
}

/// Splits `article` (which starts with its own "# Title" line) at paragraph breaks into pieces of at most `max_tokens`; every piece after the
/// first starts with "# {title} (part N)" so it can be read on its own.
pub fn chunk_article(title: &str, article: &str, max_tokens: usize) -> Vec<String> {
    let max_chars = max_tokens.max(50) * 4;
    if article.chars().count() <= max_chars {
        return vec![article.to_string()];
    }
    let mut pieces: Vec<String> = Vec::new();
    let mut current = String::new();
    let flush = |cur: &mut String, pieces: &mut Vec<String>| {
        if !cur.trim().is_empty() {
            pieces.push(cur.trim().to_string());
        }
        cur.clear();
    };
    for para in article.split("\n\n") {
        let parts = if para.chars().count() > max_chars { split_hard(para, max_chars) } else { vec![para.to_string()] };
        for p in parts {
            if !current.is_empty() && current.chars().count() + p.chars().count() + 2 > max_chars {
                flush(&mut current, &mut pieces);
            }
            if !current.is_empty() {
                current.push_str("\n\n");
            }
            current.push_str(&p);
        }
    }
    flush(&mut current, &mut pieces);
    pieces.iter().enumerate().map(|(i, p)| if i == 0 { p.clone() } else { format!("# {title} (part {})\n\n{p}", i + 1) }).collect()
}
