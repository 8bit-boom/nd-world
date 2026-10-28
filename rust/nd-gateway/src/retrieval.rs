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
    select_sections_capped(full, words, budget, max_sections, None)
}

/// `_select_relevant_sections(..., per_section_cap)`
fn select_sections_capped(full: &[Section], words: &[String], budget: usize, max_sections: usize, per_section_cap: Option<usize>) -> Option<Vec<(String, String)>> {
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
    let top_truncated = clen(&top.1.body) > per_section_cap.map_or(budget, |c| c.min(budget));
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
        let cap = per_section_cap.map_or(remaining as usize, |c| c.min(remaining as usize));
        let piece = clip_section(&s.body, cap);
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
pub fn strip(t: &str, viewer: Option<i64>) -> Option<String> {
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
        return rows(&format!("SELECT e.* FROM entities e WHERE e.world_id = ?1{visible} ORDER BY e.kind, e.name LIMIT {limit}"),
                    if gm { vec![V::Integer(wid)] } else { vec![V::Integer(wid), V::Integer(uid)] });
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
    format_context_eligible(db, entities, strip_secret, query, None)
}

/// `format_context_from_entities(..., excerpt_ids=...)`: only the entities in `excerpt_ids` get an excerpt and their notes
pub fn format_context_eligible(db: &Connection, entities: &[Map<String, Value>], strip_secret: bool, query: &str,
                               excerpt_ids: Option<&std::collections::HashSet<i64>>) -> Option<String> {
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
        let eid = e.get("id").and_then(|v| v.as_i64());
        let eligible = excerpt_ids.map_or(true, |ids| eid.is_some_and(|i| ids.contains(&i)));
        if eligible && excerpted < EXCERPT_COUNT && !body.is_empty() && excerpt_total < EXCERPT_TOTAL_BUDGET {
            let body = if strip_secret { strip(&body, None)? } else { body };
            let budget = EXCERPT_CHARS.min(EXCERPT_TOTAL_BUDGET - excerpt_total);
            let excerpt = if query.is_empty() { chead(py_strip(&body), budget).to_string() } else { best_matching_excerpt(&body, query, budget)? };
            if !excerpt.is_empty() {
                lines.push(format!("  {}", excerpt.replace('\n', "\n  ")));
                excerpt_total += clen(&excerpt);
            }
            excerpted += 1;
        }
        for n in notes.iter().filter(|n| eligible && n.get("entity_id").and_then(|v| v.as_i64()) == eid) {
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

/// `audio_jobs._build_rag_context(world, query, entity_limit, notes_limit, pinned_entity_ids)`: the lore a background job's system
/// prompt carries (GM view, nothing stripped): pinned entities, the entities the query finds topped up to the limit, the latest notes,
/// and the last 20 facts oldest first. Pinned player characters (`_format_pc_line`, app/pc_digest.py) are not here: a caller with
/// any leaves the job to Python.
pub fn build_rag_context(db: &Connection, wid: i64, query: &str, entity_limit: i64, notes_limit: i64, pinned: &[i64]) -> Option<String> {
    let rows = |sql: &str, params: Vec<rusqlite::types::Value>| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(rusqlite::params_from_iter(params), |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
        r
    };
    use rusqlite::types::Value as V;
    let id_of = |e: &Map<String, Value>| e.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
    let is_note = |e: &Map<String, Value>| e.get("kind").and_then(|v| v.as_str()) == Some("note");
    let pinned_rows = if pinned.is_empty() { vec![] } else {
        let marks = pinned.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        rows(&format!("SELECT * FROM entities WHERE world_id = ?1 AND id IN ({marks}) ORDER BY kind, name"), vec![V::Integer(wid)])?
    };
    let mut seen: std::collections::HashSet<i64> = pinned_rows.iter().map(id_of).collect();
    let q: String = query.chars().take(4000).collect();
    let found = if entity_limit > 0 { find_relevant_entities(db, wid, &q, entity_limit as usize, (0, true))? } else { vec![] };
    let mut notes: Vec<Map<String, Value>> = found.iter().filter(|e| is_note(e) && !seen.contains(&id_of(e))).cloned().collect();
    let mut non_notes: Vec<Map<String, Value>> = found.iter().filter(|e| !is_note(e) && !seen.contains(&id_of(e))).cloned().collect();
    seen.extend(non_notes.iter().map(id_of));
    seen.extend(notes.iter().map(id_of));
    if entity_limit > 0 && (non_notes.len() as i64) < entity_limit {
        let not_in = if seen.is_empty() { String::new() } else {
            format!(" AND id NOT IN ({})", seen.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","))
        };
        let topup = rows(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind != 'note'{not_in} ORDER BY kind, name LIMIT {}", entity_limit - non_notes.len() as i64),
            vec![V::Integer(wid)])?;
        seen.extend(topup.iter().map(id_of));
        non_notes.extend(topup);
    }
    if notes_limit > 0 {
        let latest = rows(&format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = 'note' ORDER BY updated_at DESC LIMIT {notes_limit}"), vec![V::Integer(wid)])?;
        notes.extend(latest.into_iter().filter(|e| !seen.contains(&id_of(e))));
    }
    let mut all: Vec<Map<String, Value>> = pinned_rows.iter().filter(|e| !is_note(e)).cloned().collect();
    all.extend(non_notes);
    all.extend(pinned_rows.iter().filter(|e| is_note(e)).cloned());
    all.extend(notes);
    let entity_context = format_context(db, &all, false, &q)?;
    let mut st = db.prepare("SELECT content FROM facts WHERE world_id = ?1 ORDER BY created_at DESC LIMIT 20").ok()?;
    let mut facts: Vec<String> = st.query_map([wid], |r| r.get::<_, Option<String>>(0)).ok()?.filter_map(Result::ok).map(|f| f.unwrap_or_else(|| "None".into())).collect();
    facts.reverse();
    let fact_context = if facts.is_empty() { String::new() } else {
        format!("Established facts from past sessions:\n{}", facts.iter().map(|f| format!("- {f}")).collect::<Vec<_>>().join("\n"))
    };
    Some([entity_context, fact_context].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n"))
}

/// `_session_featured_picks(session)`: (entity ids, player character ids) the session's NPC picks pin
pub fn session_picks(db: &Connection, sid: i64) -> (Vec<i64>, Vec<i64>) {
    let raw: Option<String> = db.query_row("SELECT npcs_json FROM game_sessions WHERE id = ?1", [sid], |r| r.get(0)).ok().flatten();
    let Some(Value::Array(picks)) = raw.filter(|r| !r.is_empty()).and_then(|r| serde_json::from_str::<Value>(&r).ok()) else { return (vec![], vec![]) };
    let (mut ents, mut pcs) = (vec![], vec![]);
    for n in picks {
        let Some(eid) = n.get("entity_id").and_then(|v| v.as_i64()) else { continue };
        if n.get("kind").and_then(|k| k.as_str()) == Some("player_character") { pcs.push(eid) } else { ents.push(eid) }
    }
    (ents, pcs)
}

const RULES_EXCERPT_CHARS: usize = 4000;
const RULES_EXCERPT_TOTAL_BUDGET: usize = 8000;
pub const RULES_SECTION_LIMIT: usize = 2;
const PRIORITY_ENTITY_LIMIT: usize = 3;
const PRIORITY_EXCERPT_CHARS: usize = 1200;
const PRIORITY_EXCERPT_TOTAL_BUDGET: i64 = 2400;
const CHARACTERS_CONTEXT_LIMIT: usize = 10;

/// `world_rules_markdown(world)`: the world's own rules, else the bundled core rules
pub fn world_rules_markdown(world: &Map<String, Value>) -> String {
    match world.get("rules_md").and_then(|v| v.as_str()) {
        Some(r) if !py_strip(r).is_empty() => r.to_string(),
        _ => nd_render::app::data()["core_rules_md"].as_str().unwrap_or("").to_string(),
    }
}

/// `rules_render.strip_gm_directives`: every :::gm block dropped, the other blocks' own text kept
pub(crate) fn strip_gm_directives(md: &str) -> String {
    let (mut skeleton, blocks) = nd_render::rules::extract_blocks(md);
    for b in &blocks {
        let repl = if b.kind == "callout" && b.btype == "gm" { "" } else { b.text.as_str() };
        skeleton = skeleton.replace(&b.sentinel, repl);
    }
    skeleton
}

/// `rules_context(..., is_gm)`: for a non-GM the :::gm blocks and [gmonly] text are taken out first
fn rules_context_as(world: &Map<String, Value>, query: &str, limit: usize, is_gm: bool) -> Option<String> {
    if limit == 0 {
        return Some(String::new());
    }
    let words = query_words(query)?;
    if words.is_empty() {
        return Some(String::new());
    }
    let md = world_rules_markdown(world);
    if md.is_empty() {
        return Some(String::new());
    }
    let md = if is_gm { md } else { strip(&strip_gm_directives(&md), None)? };
    let picks = select_sections_capped(&sections_full(&md), &words, RULES_EXCERPT_TOTAL_BUDGET, limit, Some(RULES_EXCERPT_CHARS))?;
    Some(picks.into_iter().map(|(h, e)| format!("- [Rules] {h}:{}{e}", if e.contains('\n') { "\n" } else { " " })).collect::<Vec<_>>().join("\n\n"))
}

/// `priority_entities_context(db, world, query, exclude_ids=...)` for a GM: the "High priority for AI" entities the query matches
fn priority_entities_context_as(db: &Connection, wid: i64, query: &str, exclude: &std::collections::HashSet<i64>, player: Option<i64>) -> Option<String> {
    let words = query_words(query)?;
    if words.is_empty() {
        return Some(String::new());
    }
    let visible = match player {
        Some(uid) => format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {uid}))"),
        None => String::new(),
    };
    let mut st = db.prepare(&format!("SELECT * FROM entities WHERE world_id = ?1 AND rag_priority = 1{visible}")).ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    let mut scored: Vec<(i64, Map<String, Value>, String, String)> = vec![];
    for e in rows {
        if e.get("id").and_then(|v| v.as_i64()).is_some_and(|i| exclude.contains(&i)) {
            continue;
        }
        let (mut summary, mut body) = (text(&e, "summary").unwrap_or_default(), text(&e, "body").unwrap_or_default());
        if player.is_some() {
            summary = strip(&summary, None)?;
            body = strip(&body, None)?;
        }
        let score = keyword_score(&text(&e, "name").unwrap_or_else(|| "None".into()), &format!("{summary}\n{body}"), &words)?;
        if score > 0 {
            scored.push((score, e, summary, body));
        }
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0));
    let mut lines = vec![];
    let mut budget = PRIORITY_EXCERPT_TOTAL_BUDGET;
    for (_, e, summary, body) in scored.into_iter().take(PRIORITY_ENTITY_LIMIT) {
        if budget <= 0 {
            break;
        }
        let mut header = format!("- [{}] {}", text(&e, "kind").unwrap_or_else(|| "None".into()), text(&e, "name").unwrap_or_else(|| "None".into()));
        if !summary.is_empty() {
            header.push_str(&format!(": {summary}"));
        }
        lines.push(header);
        if !body.is_empty() {
            let excerpt = best_matching_excerpt(&body, query, PRIORITY_EXCERPT_CHARS.min(budget as usize))?;
            if !excerpt.is_empty() {
                lines.push(format!("  {}", excerpt.replace('\n', "\n  ")));
                budget -= clen(&excerpt) as i64;
            }
        }
    }
    Some(lines.join("\n"))
}

/// `characters_context(db, world, query, user=None)`: the player characters the question names (in detail), or all of them when it asks
/// about the party
fn characters_context_as(db: &Connection, world: &Map<String, Value>, query: &str, player: Option<i64>) -> Option<String> {
    let wid = world.get("id")?.as_i64()?;
    let q = lower(query)?;
    if py_strip(&q).is_empty() {
        return Some(String::new());
    }
    static TOK: OnceLock<Regex> = OnceLock::new();
    let tok = TOK.get_or_init(|| Regex::new(r"[\w'’-]+").unwrap());
    let tokens: Vec<String> = tok.find_iter(&q).map(|m| m.as_str().to_string()).filter(|t| !stopwords().contains(t)).collect();
    // a player sees the owned characters when the party is shared with players, else only their own
    let who = match player {
        None => String::new(),
        Some(_) if crate::edits::py_truthy_pub(world.get("players_see_party")) => " AND owner_user_id IS NOT NULL".into(),
        Some(uid) => format!(" AND owner_user_id = {uid}"),
    };
    let mut st = db.prepare(&format!("SELECT * FROM player_characters WHERE world_id = ?1{who} ORDER BY name")).ok()?;
    let pcs: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    if pcs.is_empty() {
        return Some(String::new());
    }
    let named = |pc: &Map<String, Value>| -> Option<bool> {
        for label in [text(pc, "name").unwrap_or_default(), text(pc, "player_name").unwrap_or_default()] {
            let label = lower(py_strip(&label))?;
            if label.chars().count() >= 3 && q.contains(&label) {
                return Some(true);
            }
            if tok.find_iter(&label).any(|p| p.as_str().chars().count() >= 3 && tokens.iter().any(|t| t == p.as_str())) {
                return Some(true);
            }
        }
        Some(false)
    };
    const PARTY: [&str; 14] = ["party", "characters", "character", "pcs", "pc", "players", "heroes", "hunters", "adventurers", "everyone", "crew", "group", "team", "companions"];
    let asked_party = tokens.iter().any(|t| PARTY.contains(&t.as_str()));
    let mut chosen = vec![];
    for pc in &pcs {
        if asked_party || named(pc)? {
            chosen.push(pc);
        }
    }
    if chosen.is_empty() {
        return Some(String::new());
    }
    let mut lines = vec![];
    for pc in chosen.into_iter().take(CHARACTERS_CONTEXT_LIMIT) {
        lines.push(format!("- {}", crate::edits::pc_digest_line(db, pc, named(pc)?, player.is_none())?));
    }
    Some(format!("Player characters (live state):\n{}", lines.join("\n")))
}

/// `\b<stem>s?\b` found in `text`
fn names_word(text: &str, w: &str) -> bool {
    Regex::new(&format!(r"\b{}s?\b", regex::escape(&stem(w)))).map(|r| r.is_match(text)).unwrap_or(false)
}

/// `_has_matching_heading`
fn has_matching_heading(body: &str, words: &[String]) -> Option<bool> {
    if words.is_empty() || body.is_empty() || clen(body) <= EXCERPT_CHARS {
        return Some(false);
    }
    static HEAD: OnceLock<Regex> = OnceLock::new();
    let re = HEAD.get_or_init(|| Regex::new(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$").unwrap());
    for m in re.captures_iter(body) {
        let heading = lower(&m[2])?;
        if words.iter().all(|w| names_word(&heading, w)) {
            return Some(true);
        }
    }
    Some(false)
}

/// `smart_world_context(db, world, query, entity_limit, notes_limit)` for a GM caller (user=None): the matched entities, a top-up, the
/// latest notes, and leading them the matching rules sections, priority entities and player characters. None for a world with an
/// Obsidian vault (its vector / graph layer is Python's) or anything read differently.
pub fn smart_world_context(db: &Connection, wid: i64, query: &str, entity_limit: usize, notes_limit: usize) -> Option<String> {
    Some(smart_world_context_full(db, wid, query, entity_limit, notes_limit)?.0)
}

/// The same, with the entity rows behind it: (context, non-notes, notes) as Python's returns them
pub fn smart_world_context_full(db: &Connection, wid: i64, query: &str, entity_limit: usize, notes_limit: usize)
    -> Option<(String, Vec<Map<String, Value>>, Vec<Map<String, Value>>)> {
    smart_world_context_as(db, wid, query, entity_limit, notes_limit, None)
}

/// `smart_world_context(..., user=<a player>)` when `player` is Some(their id): every query through `_visibility_filter`, [gmonly] and
/// :::gm text stripped, the characters they may see, and no vault layer (Python leaves it out for a player too)
pub fn smart_world_context_as(db: &Connection, wid: i64, query: &str, entity_limit: usize, notes_limit: usize, player: Option<i64>)
    -> Option<(String, Vec<Map<String, Value>>, Vec<Map<String, Value>>)> {
    let world: Map<String, Value> = db.query_row("SELECT * FROM worlds WHERE id = ?1", [wid], |r| crate::layout::row_map(r)).ok()?;
    let strip_secrets = player.is_some();
    if !strip_secrets && world.get("obsidian_vault_path").and_then(|v| v.as_str()).is_some_and(|p| !py_strip(p).is_empty()) {
        return None;
    }
    let visible = match player {
        Some(uid) => format!(" AND (visible_to_players IS NOT 0 OR id IN (SELECT entity_id FROM entity_player_access WHERE user_id = {uid}))"),
        None => String::new(),
    };
    let id_of = |e: &Map<String, Value>| e.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
    let is_note = |e: &Map<String, Value>| e.get("kind").and_then(|v| v.as_str()) == Some("note");
    let entities = find_relevant_entities(db, wid, query, entity_limit, player.map_or((0, true), |u| (u, false)))?;
    let mut notes: Vec<Map<String, Value>> = entities.iter().filter(|e| is_note(e)).cloned().collect();
    let mut non_notes: Vec<Map<String, Value>> = entities.iter().filter(|e| !is_note(e)).cloned().collect();
    let matched: std::collections::HashSet<i64> = entities.iter().map(id_of).collect();
    let rows = |sql: String| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(&sql).ok()?;
        let r: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        Some(r)
    };
    if entity_limit > 0 && non_notes.len() < entity_limit {
        let not_in = if matched.is_empty() { String::new() } else {
            format!(" AND id NOT IN ({})", matched.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","))
        };
        non_notes.extend(rows(format!("SELECT * FROM entities WHERE world_id = ?1 AND kind != 'note'{not_in}{visible} ORDER BY kind, name LIMIT {}", entity_limit - non_notes.len()))?);
    }
    if notes_limit > 0 {
        let latest = rows(format!("SELECT * FROM entities WHERE world_id = ?1 AND kind = 'note'{visible} ORDER BY updated_at DESC LIMIT {notes_limit}"))?;
        notes.extend(latest.into_iter().filter(|e| !matched.contains(&id_of(e))));
    }
    let mut display: Vec<Map<String, Value>> = vec![];
    display.extend(non_notes.iter().filter(|e| matched.contains(&id_of(e))).cloned());
    display.extend(notes.iter().filter(|e| matched.contains(&id_of(e))).cloned());
    display.extend(non_notes.iter().filter(|e| !matched.contains(&id_of(e))).cloned());
    display.extend(notes.iter().filter(|e| !matched.contains(&id_of(e))).cloned());
    let q_words = query_words(query)?;
    let named = |e: &Map<String, Value>| -> Option<bool> {
        let name = lower(&text(e, "name").unwrap_or_default())?;
        let hit = q_words.iter().filter(|w| names_word(&name, w)).count();
        Some(hit >= 2 && hit as f64 >= 0.75 * q_words.len() as f64)
    };
    let mut exact = vec![];
    for e in &display {
        if matched.contains(&id_of(e)) && (named(e)? || has_matching_heading(&text(e, "body").unwrap_or_default(), &q_words)?) {
            exact.push(id_of(e));
        }
    }
    if !exact.is_empty() {
        let mut front: Vec<Map<String, Value>> = display.iter().filter(|e| exact.contains(&id_of(e))).cloned().collect();
        front.extend(display.into_iter().filter(|e| !exact.contains(&id_of(e))));
        display = front;
    }
    let context = format_context_eligible(db, &display, strip_secrets, query, Some(&matched))?;
    let rules = rules_context_as(&world, query, RULES_SECTION_LIMIT, !strip_secrets)?;
    let priority = priority_entities_context_as(db, wid, query, &matched, player)?;
    let chars = characters_context_as(db, &world, query, player)?;
    let extra = [rules, priority, chars].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n");
    let context = if extra.is_empty() { context } else if context.is_empty() { extra } else { format!("{extra}\n\n{context}") };
    Some((context, non_notes, notes))
}
