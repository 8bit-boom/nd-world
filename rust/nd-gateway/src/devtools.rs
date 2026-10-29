//! The small tool pages: `GET /image-gen` (the players' lean image page, app/main.py `player_image_gen`), `GET /imagestudio`
//! (`imagestudio`) and `GET /tools/code-assist` (app/routers/code_assist.py `code_assist_page`).

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{HeaderMap, Response, Uri};
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::{render_with, section_ok, Pages};

const ALLOWED_EXTS: [&str; 6] = ["py", "html", "js", "css", "md", "txt"];
const MAX_LISTED_FILES: usize = 2000;

fn build(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<(rusqlite::Connection, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    Some((db, p))
}

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

pub fn image_gen(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, mut p) = build(cfg, uri, headers)?;
    let Some(world) = p.world.clone() else { return Some(crate::idpages::refusal(404)) };
    if !p.is_gm && !truthy(world.get("players_can_use_image_gen")) {
        return Some(crate::idpages::refusal(403));
    }
    let wid = world.get("id")?.as_i64()?;
    let mut st = db.prepare("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY name").ok()?;
    let own: Vec<Value> = st.query_map(rusqlite::params![wid, p.user_id], |r| crate::layout::row_map(r)).ok()?
        .collect::<Result<Vec<_>, _>>().ok()?.into_iter().map(Value::Object).collect();
    p.ctx.insert("own_pcs".into(), Value::Array(own));
    p.ctx.insert("is_gm".into(), json!(p.is_gm));
    render_with(pages, "image_gen_player.html", p.ctx, headers)
}

pub fn imagestudio(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (db, mut p) = build(cfg, uri, headers)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "imagestudio")? {
        return Some(crate::idpages::refusal(403));
    }
    let s = crate::layout::app_settings_row_pub(&db)?;
    let col = |k: &str| s.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let saved = col("swarmui_external_url");
    let url = if saved.is_empty() { std::env::var("SWARMUI_EXTERNAL_URL").unwrap_or_default().trim_end_matches('/').to_string() } else { saved.trim_end_matches('/').to_string() };
    if url.is_empty() {
        let key = if col("llm_api_key").is_empty() { std::env::var("UNSLOTH_API_KEY").unwrap_or_default() } else { col("llm_api_key") };
        if !key.is_empty() {
            return None; // Python's redirect here raises (`_q.quote`): its 500
        }
    }
    p.ctx.insert("swarmui_url".into(), json!(url));
    render_with(pages, "imagestudio.html", p.ctx, headers)
}

/// `_list_source_files()`: every allowlisted file under app/ and static/ (relative to the install root), directories pruned of
/// `__pycache__` and dot names, files before subdirectories, 2000 at most
fn list_files(root: &Path, out: &mut Vec<String>, base: &Path) {
    let Ok(rd) = std::fs::read_dir(root) else { return };
    let (mut dirs, mut files): (Vec<String>, Vec<String>) = (vec![], vec![]);
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        match std::fs::metadata(e.path()) {
            Ok(m) if m.is_dir() => dirs.push(name),
            _ => files.push(name),
        }
    }
    dirs.sort();
    files.sort();
    for f in files {
        let p = root.join(&f);
        if p.extension().and_then(|x| x.to_str()).is_some_and(|x| ALLOWED_EXTS.contains(&x.to_lowercase().as_str())) {
            if let Ok(rel) = p.strip_prefix(base) {
                out.push(rel.to_string_lossy().replace('\\', "/"));
                if out.len() >= MAX_LISTED_FILES {
                    return;
                }
            }
        }
    }
    for d in dirs {
        if d == "__pycache__" || d.starts_with('.') {
            continue;
        }
        let p = root.join(&d);
        if std::fs::symlink_metadata(&p).map(|m| m.file_type().is_symlink()).unwrap_or(false) {
            continue; // os.walk lists a symlinked directory but does not enter it
        }
        list_files(&p, out, base);
        if out.len() >= MAX_LISTED_FILES {
            return;
        }
    }
}

pub fn code_assist(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (_db, mut p) = build(cfg, uri, headers)?;
    let world = p.world.clone();
    if !section_ok(&p, world.as_ref(), "code_assist")? {
        return Some(crate::idpages::refusal(403));
    }
    let app_dir = cfg.templates_dir.parent()?;
    let base = app_dir.parent()?;
    let mut files = vec![];
    for root in [app_dir.to_path_buf(), cfg.static_dir.clone()] {
        let root = std::fs::canonicalize(&root).ok()?;
        let base = std::fs::canonicalize(base).ok()?;
        if !root.starts_with(&base) {
            return None; // a static folder elsewhere: Python lists it relative to its own root
        }
        list_files(&root, &mut files, &base);
        if files.len() >= MAX_LISTED_FILES {
            break;
        }
    }
    files.truncate(MAX_LISTED_FILES);
    p.ctx.insert("files".into(), Value::Array(files.into_iter().map(Value::String).collect()));
    render_with(pages, "code_assist.html", p.ctx, headers)
}


const MAX_FILE_BYTES: usize = 80_000;
const TRUNCATION_RATIO_THRESHOLD: f64 = 0.9;

fn bad(code: u16, m: impl Into<String>) -> Response<Body> {
    crate::ai_more::detail(axum::http::StatusCode::from_u16(code).unwrap(), m)
}

/// `_resolve_safe_path(rel)`: the file under app/ or static/, or Python's 400 / 404 (Err); None when it must be Python's
fn resolve_safe_path(cfg: &Config, rel: &str) -> Option<Result<(std::path::PathBuf, String), Response<Body>>> {
    let rel = nd_render::text::py_strip(rel).trim_start_matches('/').to_string();
    if rel.is_empty() || rel.split('/').any(|c| c == "..") {
        return Some(Err(bad(400, "Invalid file path")));
    }
    if rel.contains('\0') {
        return None;
    }
    let ext = Path::new(&rel).extension().and_then(|x| x.to_str()).map(|x| format!(".{}", x.to_lowercase())).unwrap_or_default();
    if !ALLOWED_EXTS.iter().any(|e| format!(".{e}") == ext) {
        return Some(Err(bad(400, "Unsupported file type \u{2014} allowed: .css, .html, .js, .md, .py, .txt")));
    }
    let app_dir = std::fs::canonicalize(cfg.templates_dir.parent()?).ok()?;
    let base = app_dir.parent()?.to_path_buf();
    let static_dir = std::fs::canonicalize(&cfg.static_dir).ok()?;
    let not_found = || Some(Err(bad(404, "File not found")));
    let Ok(candidate) = std::fs::canonicalize(base.join(&rel)) else { return not_found() };
    if !(candidate.starts_with(&app_dir) || candidate.starts_with(&static_dir)) || !candidate.is_file() {
        return not_found();
    }
    let relpath = candidate.strip_prefix(&base).ok()?.to_string_lossy().replace('\\', "/");
    Some(Ok((candidate, relpath)))
}

/// `POST /tools/code-assist/generate`: {job_id} - the file read, the job created and started (a coding model drafts the change)
pub async fn code_assist_generate(cfg: std::sync::Arc<Config>, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let job = tokio::task::spawn_blocking(move || -> Option<Result<i64, Response<Body>>> {
        let (db, p) = {
            let db = crate::writes::open(&cfg)?;
            let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
            let p = crate::layout::build(&db, &cfg, "POST", &uri, &headers, now)?;
            (db, p)
        };
        let form = crate::ai::small_form(&headers, &body)?;
        let file = form.get("file")?.to_string(); // a missing field is pydantic's 422: Python's
        let instruction = form.get("instruction").unwrap_or("").to_string();
        let model = form.get("model").unwrap_or("").to_string();
        let think = crate::ai::form_bool(&form, "think", false)?;
        let Some(wid) = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64()) else { return Some(Err(bad(400, "No active world"))) };
        if nd_render::text::py_strip(&instruction).is_empty() {
            return Some(Err(bad(400, "Describe the change to make")));
        }
        let (path, relpath) = match resolve_safe_path(&cfg, &file)? {
            Ok(x) => x,
            Err(r) => return Some(Err(r)),
        };
        let Ok(content) = std::fs::read(&path).map_err(|_| ()).and_then(|b| String::from_utf8(b).map_err(|_| ())) else {
            return Some(Err(bad(400, "Could not read that file as text")));
        };
        if content.len() > MAX_FILE_BYTES {
            return Some(Err(bad(400, format!("File too large to preview here (over {} KB) \u{2014} a local coding model can't reliably re-emit a file this large in full, so the result would likely be a truncated, misleading diff.", MAX_FILE_BYTES / 1000))));
        }
        if relpath.chars().any(|c| c.is_control()) {
            return None;
        }
        let meta = format!("File: {}", nd_render::text::py_strip(&relpath));
        let id = crate::ai::insert_assist_job(&db, wid, p.user_id, "code_edit", "code_assist", &content, &meta, &instruction, &model, think)?;
        Some(Ok(id))
    }).await.ok()??;
    match job {
        Ok(id) => {
            crate::ai::start_assist_job_pub(id);
            crate::edits::json_response(&json!({"job_id": id}))
        }
        Err(r) => Some(r),
    }
}

/// `GET /tools/code-assist/generate/<id>`: pending, the error, or the drafted file with its unified diff against what was sent
pub fn code_assist_status(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let id_text = uri.path().strip_prefix("/tools/code-assist/generate/")?;
    if id_text.is_empty() || id_text.len() > 15 || !id_text.bytes().all(|b| b.is_ascii_digit()) || (id_text.len() > 1 && id_text.starts_with('0')) {
        return None;
    }
    let id: i64 = id_text.parse().ok()?;
    let (db, p) = build(cfg, uri, headers)?;
    let wid = p.world.as_ref().and_then(|w| w.get("id")).and_then(|v| v.as_i64());
    let Ok(job) = db.query_row("SELECT * FROM audio_jobs WHERE id = ?1", [id], |r| crate::layout::row_map(r)) else { return Some(crate::idpages::refusal(404)) };
    let text = |k: &str| job.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    if text("purpose") != "ai_assist" || wid.is_none() || job.get("world_id").and_then(|v| v.as_i64()) != wid {
        return Some(crate::idpages::refusal(404));
    }
    let params = match serde_json::from_str::<Value>(&text("assist_params_json")) {
        Ok(v) if !text("assist_params_json").is_empty() => v,
        _ if text("assist_params_json").is_empty() => json!({}),
        _ => json!({}),
    };
    if params.get("op").and_then(|v| v.as_str()) != Some("code_edit") {
        return Some(crate::idpages::refusal(404));
    }
    let status = text("status");
    if ["pending", "processing", "summarizing"].contains(&status.as_str()) {
        return crate::edits::json_response(&json!({"status": "pending"}));
    }
    if status != "done" {
        let e = text("error");
        return crate::edits::json_response(&json!({"status": "error", "error": if e.is_empty() { "Generation failed".to_string() } else { e }}));
    }
    let result = serde_json::from_str::<Value>(&text("result_json")).unwrap_or(json!({}));
    let result = if result.is_object() { result } else { return None };
    let revised = match result.get("text") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        _ => return None,
    };
    let original = text("transcript");
    let meta = params.get("meta").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let file_label = match meta.split_once(':') { Some((_, rest)) => nd_render::text::py_strip(rest).to_string(), None => meta.clone() };
    let (a, b) = (nd_render::text::py_splitlines_keepends(&original), nd_render::text::py_splitlines_keepends(&revised));
    let diff = nd_render::difflib::unified_diff(&a, &b, &file_label, &format!("{file_label} (AI suggested)"));
    let ratio = revised.chars().count() as f64 / original.chars().count().max(1) as f64;
    let warning = if ratio < TRUNCATION_RATIO_THRESHOLD {
        let pct = py_round(ratio * 100.0);
        json!(format!("The model returned only about {pct}% of the file's length \u{2014} it likely ran out of output budget rather than intentionally deleting this much. Try a smaller file or a model with more headroom before applying this diff."))
    } else {
        Value::Null
    };
    let model = match result.get("model") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        None | Some(Value::Null) | Some(Value::String(_)) => text("model"),
        _ => return None,
    };
    crate::edits::json_response(&json!({"status": "done", "file": file_label, "original": original, "revised": revised, "diff": diff, "model": model,
        "truncation_warning": warning}))
}

/// Python's `round(x)`: half to even
fn py_round(x: f64) -> i64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && (r as i64) % 2 != 0 { r as i64 - x.signum() as i64 } else { r as i64 }
}
