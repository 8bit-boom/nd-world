//! The app's permission rules, ported from Python line by line (docs/RUST_PERMISSIONS.md). Every function here is pure and has a Python
//! original; `tests/test_rust_permissions.py` feeds both the same inputs (every route of the app with many path variants, random section
//! matrices, random tagged texts) and fails on any difference. Where Python's semantics depend on Unicode details that are not worth
//! re-implementing (digits from other scripts, Python's own whitespace set, JSON Python accepts and serde does not), a function answers
//! `None` = "not sure", and the caller must let Python decide.
pub mod allow;
pub mod gmonly;
pub mod sections;

use fancy_regex::Regex;

/// A Python `re` pattern made to behave the same here: Python's `$` (no MULTILINE) also matches before one final newline.
pub(crate) fn py_re(pattern: &str) -> Regex {
    Regex::new(&pattern.replace('$', r"\n?\z")).expect("pattern")
}

/// `re.match(pattern, s)` (anchored at the start by the pattern itself).
pub(crate) fn m(re: &Regex, s: &str) -> bool {
    re.is_match(s).unwrap_or(false)
}
