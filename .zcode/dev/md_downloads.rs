
/// `_session_download_filename`: the title sanitized to Python's `isalnum` set (an ASCII title; anything beyond stays
/// Python's, file name included), then "-<suffix>.md"
fn session_md_name(title: Option<&str>, suffix: &str) -> Option<String> {
    let raw = title.filter(|t| !t.is_empty()).unwrap_or("session");
    if !raw.chars().all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_') {
        return None;
    }
    let base: String = raw.chars().filter(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-' || c == '_').collect();
    Some(format!("{}-{suffix}.md", if base.is_empty() { "session".to_string() } else { base }))
}

/// the shared shape of `session_download_summary` / `session_download_transcript` (app/routers/sessions.py): the row by
/// id alone, read-gated by the row's own world's "sessions" section, the column's text with [gmonly] stripped for a
/// non-GM, as a text/markdown attachment; the no-content refusals in Python's words
fn session_md_download(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64, suffix: &str) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    let gs = db.query_row("SELECT * FROM game_sessions WHERE id = ?1", [id], |r| crate::layout::row_map(r)).ok();
    let Some(gs) = gs else { return Some(crate::idpages::refusal(404)) };
    let wid = gs.get("world_id")?.as_i64()?;
    let (ok, _) = crate::idpages::row_world_visible_pub(&db, &p, wid, "sessions")?;
    if !ok {
        return Some(crate::idpages::refusal(404));
    }
    let col = if suffix == "summary" { "summary" } else { "live_transcript" };
    let empty_msg = if suffix == "summary" { "This session has no summary yet" } else { "This session has no live transcript yet" };
    let text = match gs.get(col) {
        Some(Value::String(t)) if !t.is_empty() => t.clone(),
        _ => return Some(crate::ai_more::detail(StatusCode::NOT_FOUND, empty_msg)),
    };
    let body = if p.is_gm {
        text
    } else {
        nd_perm::gmonly::strip_gm_only(&text, Some(p.user_id), nd_render::now_minute())?
    };
    let fname = session_md_name(gs.get("title").and_then(|v| v.as_str()), suffix)?;
    let mut r = crate::edits::json_response_raw(&body, "text/markdown; charset=utf-8")?;
    if let Ok(v) = format!("attachment; filename=\"{fname}\"").parse() {
        r.headers_mut().insert("content-disposition", v);
    }
    Some(r)
}

/// `GET /sessions/<id>/summary.md`
fn session_summary_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    session_md_download(cfg, uri, headers, id, "summary")
}

/// `GET /sessions/<id>/transcript.md`
fn session_transcript_md(cfg: &Config, uri: &Uri, headers: &HeaderMap, id: i64) -> Option<Response<Body>> {
    session_md_download(cfg, uri, headers, id, "transcript")
}
