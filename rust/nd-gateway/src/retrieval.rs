//! What the Chronicler (and later the other AI surfaces) tells the model about the world: a port of app/retrieval.py's entity search
//! (FTS5 with the same column weights, the visibility filter, secret-only matches dropped), the excerpt picker (markdown sections scored
//! by keyword coverage) and the context lines, and app/ai_instructions.py. Text positions are in characters, as Python counts them.

use regex::Regex;
use rusqlite::Connection;
use serde_json::{Map, Value};
use std::sync::OnceLock;

const MAX_QUERY_WORDS: usize = 32;
const HEADING_COVERAGE_WEIGHT: i64 = 40;
const BODY_COVERAGE_WEIGHT: i64 = 8;
const BODY_HIT_TIEBREAK_CAP: i64 = 3;
const MAX_SCORED_CHARS: usize = 4000;
const SECTION_RELEVANCE_RATIO: f64 = 0.4;
const MAX_GUARANTEED_TABLE_SECTIONS: usize = 3;
const GUARANTEED_TABLE_CHARS: usize = 2000;
const TRUNCATION_MARKER: &str = "\n…[truncated — more entries follow in the full Rules text]";
const EXCERPT_COUNT: usize = 5;
const EXCERPT_CHARS: usize = 1200;
const EXCERPT_TOTAL_BUDGET: usize = 8000;
const ENTITY_NOTE_CHARS: usize = 500;
const ENTITY_NOTES_TOTAL_BUDGET: usize = 3000;

/// Python's `str.isspace()` (Rust's whitespace plus the information separators)
fn py_ws(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

fn py_strip(t: &str) -> &str {
    t.trim_matches(py_ws)
}

fn clen(t: &str) -> usize {
    t.chars().count()
}

/// `t[:n]` in characters
fn chead(t: &str, n: usize) -> &str {
    match t.char_indices().nth(n) {
        Some((b, _)) => &t[..b],
        None => t,
    }
}

/// `str.lower()`; None where Python's lowering differs from Rust's (a dotted capital I)
fn lower(t: &str) -> Option<String> {
    (!t.contains('\u{130}')).then(|| t.to_lowercase())
}

fn stopwords() -> &'static Vec<String> {
    static S: OnceLock<Vec<String>> = OnceLock::new();
    S.get_or_init(|| {
        nd_render::app::data()["retrieval_stopwords"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default()
    })
}

/// `_query_words`: lowercased words of 2+ characters, not stopwords, de-duplicated, at most 32
pub fn query_words(q: &str) -> Option<Vec<String>> {
    static SPLIT: OnceLock<Regex> = OnceLock::new();
    let re = SPLIT.get_or_init(|| Regex::new(r"\W+").unwrap());
    let mut out: Vec<String> = vec![];
    for w in re.split(&lower(q)?) {
        if clen(w) >= 2 && !stopwords().iter().any(|s| s == w) && !out.iter().any(|x| x == w) {
            out.push(w.to_string());
            if out.len() >= MAX_QUERY_WORDS {
                break;
            }
        }
    }
    Some(out)
}

/// `_stem`: a trailing "s" off a word longer than 3
pub fn stem(w: &str) -> String {
    if w.ends_with('s') && clen(w) > 3 { chead(w, clen(w) - 1).to_string() } else { w.to_string() }
}

/// `_keyword_score`: distinct query words matched in the heading (x40) and in the body (x8), plus a capped hit-count tiebreak
fn keyword_score(heading: &str, body: &str, words: &[String]) -> Option<i64> {
    let (h, b) = (lower(heading)?, lower(body)?);
    let (mut hc, mut bc, mut hits) = (0, 0, 0);
    for w in words {
        let re = Regex::new(&format!(r"\b{}s?\b", regex::escape(&stem(w)))).ok()?;
        if re.is_match(&h) {
            hc += 1;
        }
        let n = re.find_iter(&b).count() as i64;
        if n > 0 {
            bc += 1;
            hits += n;
        }
    }
    Some(hc * HEADING_COVERAGE_WEIGHT + bc * BODY_COVERAGE_WEIGHT + hits.min(BODY_HIT_TIEBREAK_CAP))
}

struct Section {
    heading: String,
    body: String,
    start: usize,
    full_end: usize,
}

/// `_rules_sections_full`: the markdown split at every heading, a section reaching to the next heading of its level or shallower (just
/// its own text when that would pass 4,000 characters); positions in characters
fn sections_full(markdown: &str) -> Vec<Section> {
    static HEAD: OnceLock<Regex> = OnceLock::new();
    let re = HEAD.get_or_init(|| Regex::new(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$").unwrap());
    if markdown.is_empty() {
        return vec![];
    }
    let md = markdown.replace("\r\n", "\n").replace('\r', "\n");
    // byte offset -> character offset
    let cpos = |b: usize| md[..b].chars().count();
    let total = clen(&md);
    let matches: Vec<regex::Captures> = re.captures_iter(&md).collect();
    if matches.is_empty() {
        let s = py_strip(&md);
        return if s.is_empty() { vec![] } else { vec![Section { heading: "Rules".into(), body: s.to_string(), start: 0, full_end: total }] };
    }
    let mut out = vec![];
    let first = matches[0].get(0).unwrap().start();
    if first > 0 {
        let intro = py_strip(&md[..first]);
        if !intro.is_empty() {
            out.push(Section { heading: "Introduction".into(), body: intro.to_string(), start: 0, full_end: cpos(first) });
        }
    }
    for (i, m) in matches.iter().enumerate() {
        let level = m[1].len();
        let heading = py_strip(&m[2]).to_string();
        let start_b = m.get(0).unwrap().end();
        let rest = &matches[i + 1..];
        let own_end_b = rest.first().map_or(md.len(), |r| r.get(0).unwrap().start());
        let mut full_end_b = md.len();
        for later in rest {
            if later[1].len() <= level {
                full_end_b = later.get(0).unwrap().start();
                break;
            }
        }
        let (start_c, full_end_c) = (cpos(start_b), cpos(full_end_b));
        let end_b = if full_end_c - start_c <= MAX_SCORED_CHARS { full_end_b } else { own_end_b };
        let body = py_strip(&md[start_b..end_b]);
        if !body.is_empty() {
            out.push(Section { heading, body: body.to_string(), start: start_c, full_end: full_end_c });
        }
    }
    out
}

fn has_markdown_table(t: &str) -> bool {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$").unwrap()).is_match(t)
}

/// `_clip_section`: at most `cap` characters, cut at a line end when that keeps at least half, with the truncation marker
fn clip_section(t: &str, cap: usize) -> String {
    if cap == 0 {
        return String::new();
    }
    if clen(t) <= cap {
        return t.to_string();
    }
    let head = chead(t, cap);
    let cut = head.rfind('\n').map(|b| clen(&head[..b]));
    let piece = match cut {
        Some(c) if c as f64 >= cap as f64 * 0.5 => chead(t, c),
        _ => head,
    };
    format!("{}{}", piece.trim_end_matches(py_ws), TRUNCATION_MARKER)
}

/// `_select_relevant_sections` (no per-section cap: best_matching_excerpt's use)
fn select_sections(full: &[Section], words: &[String], budget: usize, max_sections: usize) -> Option<Vec<(String, String)>> {
    let mut scored: Vec<(i64, &Section)> = vec![];
    for s in full {
        let score = keyword_score(&s.heading, chead(&s.body, MAX_SCORED_CHARS), words)?;
        if score > 0 {
            scored.push((score, s));
        }
    }
    if scored.is_empty() {
        return Some(vec![]);
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0));
    let top = scored[0];
    let top_collapsed = top.1.full_end - top.1.start > MAX_SCORED_CHARS;
    let top_truncated = clen(&top.1.body) > budget;
    let (mut picks, mut shown): (Vec<(String, String)>, Vec<String>) = (vec![], vec![]);
    let mut remaining = budget as i64;
    for (score, s) in &scored {
        if picks.len() >= max_sections || remaining <= 0 {
            break;
        }
        if !picks.is_empty() && (*score as f64) < top.0 as f64 * SECTION_RELEVANCE_RATIO {
            break;
        }
        if shown.iter().any(|x| x.contains(s.body.as_str()) || s.body.contains(x.as_str())) {
            continue;
        }
        let piece = clip_section(&s.body, remaining as usize);
        remaining -= clen(&piece) as i64;
        picks.push((s.heading.clone(), piece.clone()));
        shown.push(piece);
    }
    if top_collapsed || top_truncated {
        let mut guaranteed = 0;
        for (_, s) in &scored {
            if guaranteed >= MAX_GUARANTEED_TABLE_SECTIONS {
                break;
            }
            if !(top.1.start < s.start && s.start < top.1.full_end) || !has_markdown_table(&s.body) {
                continue;
            }
            if shown.iter().any(|x| x.contains(s.body.as_str()) || s.body.contains(x.as_str())) {
                continue;
            }
            let piece = clip_section(&s.body, GUARANTEED_TABLE_CHARS);
            picks.push((s.heading.clone(), piece.clone()));
            shown.push(piece);
            guaranteed += 1;
        }
    }
    Some(picks)
}

/// `best_matching_excerpt(body, query, budget, 3)`
fn best_matching_excerpt(body: &str, query: &str, budget: usize) -> Option<String> {
    let body = py_strip(body);
    if body.is_empty() || clen(body) <= budget {
        return Some(body.to_string());
    }
    let words = query_words(query)?;
    if words.is_empty() {
        return Some(chead(body, budget).to_string());
    }
    let full = sections_full(body);
    if full.len() <= 1 {
        return Some(chead(body, budget).to_string());
    }
    let picks = select_sections(&full, &words, budget, 3)?;
    if picks.is_empty() {
        return Some(chead(body, budget).to_string());
    }
    let parts: Vec<String> = picks.into_iter().map(|(h, p)| {
        if h == "Introduction" { p } else { format!("[{h}]{}{p}", if p.contains('\n') { "\n" } else { " " }) }
    }).collect();
    Some(parts.join("\n\n"))
}

fn text(e: &Map<String, Value>, k: &str) -> Option<String> {
    match e.get(k) {
        Some(Value::String(t)) => Some(t.clone()),
        _ => None,
    }
}

/// `strip_gm_only(text)` for a non-GM (viewer as given)
fn strip(t: &str, viewer: Option<i64>) -> Option<String> {
    nd_perm::gmonly::strip_gm_only(t, viewer, nd_render::now_minute())
}

/// `find_relevant_entities(db, world, query, limit, user)`: entity rows, best first; `viewer` = (user id, is_gm)
pub fn find_relevant_entities(db: &Connection, wid: i64, query: &str, limit: usize, viewer: (i64, bool)) -> Option<Vec<Map<String, Value>>> {
    let (uid, gm) = viewer;
    let visible = if gm { "" } else { " AND (e.visible_to_players IS NOT 0 OR e.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?2))" };
    let rows = |sql: &str, params: Vec<rusqlite::types::Value>| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(rusqlite::params_from_iter(params), |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    use rusqlite::types::Value as V;
    let words = query_words(query)?;
    if words.is_empty() {
        return rows(&format!("SELECT e.* FROM entities e WHERE e.world_id = ?1{visible} ORDER BY e.kind, e.name LIMIT {limit}"), vec![V::Integer(wid), V::Integer(uid)]);
    }
    // find_relevant_entities_fts (an FTS failure - Python's ILIKE fallback - is Python's)
    let term = |w: &str| {
        let esc = w.replace('"', "\"\"");
        let st = stem(w);
        if st == w { format!("\"{esc}\"*") } else { format!("(\"{esc}\"* OR \"{}\"*)", st.replace('"', "\"\"")) }
    };
    let fts = words.iter().map(|w| term(w)).collect::<Vec<_>>().join(" OR ");
    let fetch = if gm { limit } else { (limit * 5).max(20).min(250) };
    let mut st = db.prepare("SELECT entities.id FROM entity_fts JOIN entities ON entities.id = entity_fts.rowid WHERE entity_fts MATCH ?1 AND entities.world_id = ?2 ORDER BY bm25(entity_fts, 10.0, 2.0, 1.0, 5.0, 8.0) LIMIT ?3").ok()?;
    let ids: Vec<i64> = st.query_map(rusqlite::params![fts, wid, fetch as i64], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
    if ids.is_empty() {
        return Some(vec![]);
    }
    let marks: Vec<String> = ids.iter().map(|i| i.to_string()).collect();
    let found = rows(&format!("SELECT e.* FROM entities e WHERE e.id IN ({}){}", marks.join(","), visible.replace("?2", "?1")), if gm { vec![] } else { vec![V::Integer(uid)] })?;
    let mut ordered: Vec<Map<String, Value>> = ids.iter().filter_map(|i| found.iter().find(|e| e.get("id").and_then(|v| v.as_i64()) == Some(*i)).cloned()).collect();
    if !gm {
        // drop_secret_only_matches: an entity whose only match is text this viewer cannot see
        static TAG: OnceLock<Regex> = OnceLock::new();
        let tag = TAG.get_or_init(|| Regex::new(r"(?i)\[(?:gmonly|for=|reveal=)").unwrap());
        let mut keep = vec![];
        for e in ordered {
            let (summary, body) = (text(&e, "summary").unwrap_or_default(), text(&e, "body").unwrap_or_default());
            if !tag.is_match(&format!("{summary}\n{body}")) {
                keep.push(e);
                continue;
            }
            let seen = lower(&[text(&e, "name").unwrap_or_default(), text(&e, "tags").unwrap_or_default(), text(&e, "aliases").unwrap_or_default(),
                strip(&summary, Some(uid))?, strip(&body, Some(uid))?].join(" "))?;
            if words.iter().any(|w| seen.contains(w.as_str()) || seen.contains(stem(w).as_str())) {
                keep.push(e);
            }
        }
        ordered = keep;
    }
    ordered.truncate(limit);
    Some(ordered)
}

/// `format_context_from_entities(entities, strip_gm_only=.., query=.., db=..)`: a line per entity, body excerpts for the first five, notes
pub fn format_context(db: &Connection, entities: &[Map<String, Value>], strip_secret: bool, query: &str) -> Option<String> {
    let mut lines: Vec<String> = vec![];
    let (mut excerpt_total, mut excerpted, mut notes_total) = (0usize, 0usize, 0usize);
    let ids: Vec<String> = entities.iter().filter_map(|e| e.get("id").and_then(|v| v.as_i64())).map(|i| i.to_string()).collect();
    let mut notes: Vec<Map<String, Value>> = vec![];
    if !ids.is_empty() {
        let mut st = db.prepare(&format!("SELECT * FROM entity_notes WHERE entity_id IN ({}) ORDER BY entity_id, created_at", ids.join(","))).ok()?;
        notes = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    }
    for e in entities {
        let mut summary = text(e, "summary");
        if strip_secret {
            summary = match summary { Some(s) if !s.is_empty() => Some(strip(&s, None)?), other => other };
        }
        let mut line = format!("- [{}] {}", text(e, "kind").unwrap_or_else(|| "None".into()), text(e, "name").unwrap_or_else(|| "None".into()));
        if let Some(st) = text(e, "subtype").filter(|t| !t.is_empty()) {
            line += &format!(" ({st})");
        }
        if let Some(s) = summary.filter(|t| !t.is_empty()) {
            line += &format!(": {s}");
        }
        lines.push(line);
        let body = text(e, "body").unwrap_or_default();
        if excerpted < EXCERPT_COUNT && !body.is_empty() && excerpt_total < EXCERPT_TOTAL_BUDGET {
            let body = if strip_secret { strip(&body, None)? } else { body };
            let budget = EXCERPT_CHARS.min(EXCERPT_TOTAL_BUDGET - excerpt_total);
            let excerpt = if query.is_empty() { chead(py_strip(&body), budget).to_string() } else { best_matching_excerpt(&body, query, budget)? };
            if !excerpt.is_empty() {
                lines.push(format!("  {}", excerpt.replace('\n', "\n  ")));
                excerpt_total += clen(&excerpt);
            }
            excerpted += 1;
        }
        let eid = e.get("id").and_then(|v| v.as_i64());
        for n in notes.iter().filter(|n| n.get("entity_id").and_then(|v| v.as_i64()) == eid) {
            if strip_secret && n.get("visible_to_players").and_then(|v| v.as_i64()) != Some(1) {
                continue;
            }
            if notes_total >= ENTITY_NOTES_TOTAL_BUDGET {
                break;
            }
            if n.get("content_is_html").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
                return None; // html_to_markdown: Python's
            }
            let mut content = text(n, "content").unwrap_or_default();
            if strip_secret {
                content = strip(&content, None)?;
            }
            let content = py_strip(&content);
            if content.is_empty() {
                continue;
            }
            let piece = chead(content, ENTITY_NOTE_CHARS.min(ENTITY_NOTES_TOTAL_BUDGET - notes_total));
            lines.push(format!("  [note] {}", piece.replace('\n', "\n  ")));
            notes_total += clen(piece);
        }
    }
    Some(lines.join("\n"))
}

/// `ai_instructions.enabled_instructions_text(world, for_players)`
pub fn instructions_text(db: &Connection, wid: i64, for_players: bool) -> Option<String> {
    let sql = format!("SELECT title, content FROM ai_instructions WHERE world_id = ?1 AND enabled = 1{} ORDER BY created_at",
        if for_players { " AND applies_to_players = 1" } else { "" });
    let mut st = db.prepare(&sql).ok()?;
    let rows: Vec<(Option<String>, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let parts: Vec<String> = rows.into_iter().filter_map(|(t, c)| {
        let c = c.unwrap_or_default();
        let c = py_strip(&c).to_string();
        (!c.is_empty()).then(|| format!("## {}\n\n{c}", t.unwrap_or_else(|| "None".into())))
    }).collect();
    if parts.is_empty() {
        return Some(String::new());
    }
    Some(format!("Additional GM-authored instructions for how you should behave — follow these in addition to everything above, not instead of it:\n\n{}", parts.join("\n\n")))
}
