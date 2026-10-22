//! `GET /search` (app/main.py `search` and its helpers, app/retrieval.py's FTS search): entities first (FTS5 with the same column
//! weights, or the substring scan for a query with no 2-letter word), then characters, quests, sessions and entity notes, each with a
//! highlighted snippet. A database whose FTS index fails is Python's (it falls back to its own scan). `None` = not sure.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{HeaderMap, Response, Uri},
};
use regex::Regex;
use rusqlite::{Connection, ToSql};
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::pages::{query_param, render_with, Pages};

const CAP: usize = 25;

fn py_lower(s: &str) -> String {
    s.to_lowercase()
}

/// `retrieval._stem`
fn stem(w: &str) -> String {
    if w.ends_with('s') && w.chars().count() > 3 {
        w[..w.len() - 1].to_string()
    } else {
        w.to_string()
    }
}

/// `retrieval._fts_term`
fn fts_term(w: &str) -> String {
    let esc = w.replace('"', "\"\"");
    let s = stem(w);
    if s == w {
        format!("\"{esc}\"*")
    } else {
        format!("(\"{esc}\"* OR \"{}\"*)", s.replace('"', "\"\""))
    }
}

/// `main._snippet`: up to 120 characters around the first match, escaped, the query marked
fn snippet(text: &str, q: &str) -> String {
    if text.is_empty() || q.is_empty() {
        return String::new();
    }
    let low: Vec<char> = py_lower(text).chars().collect();
    let ql: Vec<char> = py_lower(q).chars().collect();
    let Some(idx) = low.windows(ql.len()).position(|w| w == ql.as_slice()) else { return String::new() };
    let chars: Vec<char> = text.chars().collect();
    let start = idx.saturating_sub(60);
    let end = (idx + 60).min(chars.len());
    let mid: String = chars[start.min(chars.len())..end.max(start.min(chars.len()))].iter().collect();
    let snip = format!("{}{}{}", if start > 0 { "…" } else { "" }, nd_render::text::py_strip(&mid), if end < chars.len() { "…" } else { "" });
    let escaped = nd_render::rules::html_escape(&snip);
    let pat = format!("(?i){}", regex::escape(&nd_render::rules::html_escape(q)));
    match Regex::new(&pat) {
        Ok(rx) => rx.replace_all(&escaped, |c: &regex::Captures| format!("<mark>{}</mark>", &c[0])).into_owned(),
        Err(_) => escaped,
    }
}

fn s<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

fn rows(db: &Connection, sql: &str, params: &[&dyn ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok();
    r
}

pub fn search_page(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let wid = p.world.as_ref()?.get("id")?.as_i64()?;
    let q = query_param(uri, "q").unwrap_or_default();
    let kind = query_param(uri, "kind").unwrap_or_default();
    let is_gm = p.is_gm;
    let uid = p.user_id;
    let minute = nd_render::now_minute();
    let strip = |t: &str| -> Option<String> { nd_perm::gmonly::strip_gm_only(t, Some(uid), minute) };
    let mut results: Vec<Map<String, Value>> = vec![];
    let mut grouped: Vec<(String, Vec<Value>)> = vec![];
    let mut snippets = Map::new();
    let mut other: Vec<(String, Vec<Value>)> = vec![];
    if !q.is_empty() {
        let ql = py_lower(&q);
        let words: Vec<String> = Regex::new(r"\W+").ok()?.split(&ql).filter(|w| w.chars().count() >= 2).map(|w| w.to_string()).collect();
        let vis = " AND (entities.visible_to_players IS NOT 0 OR entities.id IN (SELECT entity_id FROM entity_player_access WHERE user_id = ?9))";
        let probe: Vec<String>;
        if !words.is_empty() {
            let fts: String = words.iter().map(|w| fts_term(w)).collect::<Vec<_>>().join(" OR ");
            let fetch: i64 = if is_gm { CAP as i64 } else { 125 };
            let mut sql = String::from("SELECT entities.id FROM entity_fts JOIN entities ON entities.id = entity_fts.rowid WHERE entity_fts MATCH ?1 AND entities.world_id = ?2 ");
            if !kind.is_empty() {
                sql.push_str("AND entities.kind = ?3 ");
            } else {
                sql.push_str("AND ?3 IS NOT NULL ");
            }
            sql.push_str("ORDER BY bm25(entity_fts, 10.0, 2.0, 1.0, 5.0, 8.0) LIMIT ?4");
            // an FTS index that fails here makes Python fall back to its own scan: that page is Python's
            let mut st = db.prepare(&sql).ok()?;
            let ids: Vec<i64> = st.query_map(rusqlite::params![fts, wid, kind, fetch], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
            for id in ids {
                let found = if is_gm {
                    rows(&db, "SELECT * FROM entities WHERE id = ?1", &[&id])?
                } else {
                    rows(&db, &format!("SELECT * FROM entities WHERE id = ?1{}", vis.replace("?9", "?2")), &[&id, &uid])?
                };
                results.extend(found);
            }
            probe = words.clone();
        } else {
            let like = format!("%{q}%");
            let mut sql = String::from(
                "SELECT * FROM entities WHERE entities.world_id = ?1 AND (lower(entities.name) LIKE lower(?2) OR lower(entities.tags) LIKE lower(?2) \
                 OR lower(entities.summary) LIKE lower(?2) OR lower(entities.body) LIKE lower(?2))",
            );
            if !is_gm {
                sql.push_str(&vis.replace("?9", "?3"));
            } else {
                sql.push_str(" AND ?3 IS NOT NULL");
            }
            if !kind.is_empty() {
                sql.push_str(" AND entities.kind = ?4");
            } else {
                sql.push_str(" AND ?4 IS NOT NULL");
            }
            sql.push_str(" ORDER BY entities.kind, entities.name LIMIT 25");
            results = rows(&db, &sql, &[&wid, &like, &uid, &kind])?;
            probe = vec![ql.clone()];
        }
        // retrieval.drop_secret_only_matches: a player never sees an entity whose only match is text they cannot see
        if !is_gm {
            let tag = Regex::new(r"(?i)\[(?:gmonly|for=|reveal=)").ok()?;
            let mut keep = vec![];
            for e in results {
                let raw = format!("{}\n{}", s(&e, "summary"), s(&e, "body"));
                if !tag.is_match(&raw) {
                    keep.push(e);
                    continue;
                }
                let seen = py_lower(&[s(&e, "name").to_string(), s(&e, "tags").to_string(), s(&e, "aliases").to_string(), strip(s(&e, "summary"))?, strip(s(&e, "body"))?].join(" "));
                if probe.iter().any(|w| seen.contains(w.as_str()) || seen.contains(stem(w).as_str())) {
                    keep.push(e);
                }
            }
            results = keep;
        }
        results.truncate(CAP);
        for e in &results {
            let k = s(e, "kind").to_string();
            match grouped.iter_mut().find(|(x, _)| *x == k) {
                Some(g) => g.1.push(Value::Object(e.clone())),
                None => grouped.push((k, vec![Value::Object(e.clone())])),
            }
            if !py_lower(s(e, "name")).contains(&ql) && !py_lower(s(e, "summary")).contains(&ql) {
                let body = if is_gm { s(e, "body").to_string() } else { strip(s(e, "body"))? };
                snippets.insert(e.get("id")?.as_i64()?.to_string(), json!(snippet(&body, &q)));
            }
        }
        if kind.is_empty() {
            let like = format!("%{q}%");
            // characters: a player sees their own, and the party's when the world lets them
            let mut chars = vec![];
            let mut sql = String::from(
                "SELECT * FROM player_characters WHERE player_characters.world_id = ?1 AND (lower(name) LIKE lower(?2) OR lower(player_name) LIKE lower(?2) \
                 OR lower(race) LIKE lower(?2) OR lower(char_class) LIKE lower(?2) OR lower(backstory) LIKE lower(?2) OR lower(notes) LIKE lower(?2))",
            );
            let see_party = p.world.as_ref()?.get("players_see_party").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
            if !is_gm {
                sql.push_str(if see_party { " AND (owner_user_id = ?3 OR owner_user_id IS NOT NULL)" } else { " AND owner_user_id = ?3" });
            } else {
                sql.push_str(" AND ?3 IS NOT NULL");
            }
            sql.push_str(" ORDER BY name LIMIT 25");
            for pc in rows(&db, &sql, &[&wid, &like, &uid])? {
                let sub: Vec<&str> = [s(&pc, "race"), s(&pc, "char_class")].into_iter().filter(|x| !x.is_empty()).collect();
                let snip = if py_lower(s(&pc, "name")).contains(&ql) {
                    String::new()
                } else {
                    let src = if !s(&pc, "backstory").is_empty() { s(&pc, "backstory") } else { s(&pc, "notes") };
                    snippet(src, &q)
                };
                chars.push(json!({"title": pc.get("name")?, "subtitle": sub.join(" / "), "url": format!("/characters/{}", pc.get("id")?), "icon": "🎲", "snippet": snip}));
            }
            let mut sections = vec![("Characters".to_string(), chars)];
            if is_gm {
                let mut quests = vec![];
                for qu in rows(
                    &db,
                    "SELECT * FROM quests WHERE quests.world_id = ?1 AND (lower(title) LIKE lower(?2) OR lower(summary) LIKE lower(?2) OR lower(body) LIKE lower(?2)) ORDER BY title LIMIT 25",
                    &[&wid, &like],
                )? {
                    let snip = if py_lower(s(&qu, "title")).contains(&ql) {
                        String::new()
                    } else {
                        snippet(if !s(&qu, "body").is_empty() { s(&qu, "body") } else { s(&qu, "summary") }, &q)
                    };
                    let status = s(&qu, "status");
                    let mut cap: String = status.chars().take(1).flat_map(|c| c.to_uppercase()).collect();
                    cap.push_str(&status.chars().skip(1).collect::<String>().to_lowercase());
                    quests.push(json!({"title": qu.get("title")?, "subtitle": cap, "url": format!("/quests/{}", qu.get("id")?), "icon": "📜", "snippet": snip}));
                }
                let mut sess = vec![];
                for gs in rows(
                    &db,
                    "SELECT * FROM game_sessions WHERE game_sessions.world_id = ?1 AND (lower(title) LIKE lower(?2) OR lower(summary) LIKE lower(?2)) ORDER BY session_num DESC LIMIT 25",
                    &[&wid, &like],
                )? {
                    let snip = if py_lower(s(&gs, "title")).contains(&ql) { String::new() } else { snippet(s(&gs, "summary"), &q) };
                    let num = crate::pages::py_str_value(gs.get("session_num")?)?;
                    let title = crate::pages::py_str_value(gs.get("title")?)?;
                    let date = gs.get("session_date").filter(|v| !v.is_null() && v.as_str() != Some("")).cloned().unwrap_or(json!(""));
                    sess.push(json!({"title": format!("Session #{num}: {title}"), "subtitle": date, "url": format!("/sessions/{}", gs.get("id")?), "icon": "📓", "snippet": snip}));
                }
                sections.push(("Quests".into(), quests));
                sections.push(("Sessions".into(), sess));
            }
            let mut notes = vec![];
            let vis_n = if is_gm { " AND ?3 IS NOT NULL" } else { " AND entity_notes.visible_to_players = 1 AND ?3 IS NOT NULL" };
            let sql = format!(
                "SELECT entity_notes.content AS content, entities.name AS ent_name, entities.id AS ent_id FROM entity_notes JOIN entities ON entities.id = entity_notes.entity_id \
                 WHERE entities.world_id = ?1 AND lower(entity_notes.content) LIKE lower(?2){vis_n} LIMIT 25"
            );
            for n in rows(&db, &sql, &[&wid, &like, &uid])? {
                let mut content = s(&n, "content").to_string();
                if !is_gm {
                    content = strip(&content)?;
                    if !py_lower(&content).contains(&ql) {
                        continue;
                    }
                }
                notes.push(json!({"title": n.get("ent_name")?, "subtitle": "Note", "url": format!("/entity/{}", n.get("ent_id")?), "icon": "🔒", "snippet": snippet(&content, &q)}));
            }
            sections.push(("Notes".into(), notes));
            other = sections.into_iter().filter(|(_, v)| !v.is_empty()).collect();
        }
    }
    let mut summaries = Map::new();
    for e in &results {
        let sm = if is_gm { s(e, "summary").to_string() } else { strip(s(e, "summary"))? };
        summaries.insert(e.get("id")?.as_i64()?.to_string(), json!(sm));
    }
    let c = &mut p.ctx;
    c.insert("results".into(), Value::Array(results.into_iter().map(Value::Object).collect()));
    c.insert("grouped".into(), Value::Object(grouped.into_iter().map(|(k, v)| (k, Value::Array(v))).collect()));
    c.insert("other_grouped".into(), Value::Object(other.into_iter().map(|(k, v)| (k, Value::Array(v))).collect()));
    c.insert("snippets".into(), json!({"__intkeys__": Value::Object(snippets)}));
    c.insert("summaries".into(), json!({"__intkeys__": Value::Object(summaries)}));
    c.insert("q".into(), json!(q));
    c.insert("kind_filter".into(), json!(kind));
    render_with(pages, "search.html", p.ctx, headers)
}
