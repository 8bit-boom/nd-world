//! The embedded nd-cartographer map studio, compiled into the gateway
//! (`ndcart_server`). No separate binary, download, child process or reverse proxy:
//! the studio's router is mounted under `/cartographer/studio` on this origin and
//! gated by the GM session, so it works even when nd-world runs in a container (the
//! browser never needs a separate loopback port). Always on for the GM; maps live
//! in `{accel_root}/cartographer/data`. Rust-only — no Python counterpart.

use std::path::PathBuf;

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::Pages;

/// The studio is mounted under this path on the gateway's own origin.
pub const STUDIO_BASE: &str = "/cartographer/studio";

pub fn base(cfg: &Config) -> PathBuf {
    cfg.accel_root.join("cartographer")
}
fn data_dir(cfg: &Config) -> PathBuf {
    base(cfg).join("data")
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Build the studio router once, at startup. The AI helpers call back into
/// nd-world on its own loopback, reusing the caller's GM session — no password is
/// stored. Must run inside the tokio runtime (the studio spawns an autosave task).
pub fn studio_router(cfg: &Config) -> axum::Router {
    let data = data_dir(cfg);
    let _ = std::fs::create_dir_all(&data);
    let port = cfg.listen.rsplit(':').next().unwrap_or("8000");
    let nd_world_base = format!("http://127.0.0.1:{port}");
    ndcart_server::embed_router(data, STUDIO_BASE, Some(nd_world_base))
}

/// The one payload the page and the status route render from. The studio is
/// compiled in, so it is always installed and running for the GM.
pub fn status(cfg: &Config) -> Value {
    json!({
        "installed": true,
        "enabled": true,
        "running": true,
        "autostart": true,
        "url": STUDIO_BASE,
        "version": ndcart_server::VERSION,
        "dir": base(cfg).to_string_lossy(),
    })
}

fn collect_context_strings(v: &Value, found: &mut Vec<String>) {
    const WANTED: [&str; 9] = [
        "name",
        "title",
        "text",
        "label",
        "note",
        "summary",
        "description",
        "digest",
        "kind",
    ];
    match v {
        Value::Object(o) => {
            for (k, c) in o {
                if WANTED.contains(&k.as_str()) {
                    if let Some(s) = c.as_str().filter(|s| !s.trim().is_empty()) {
                        found.push(s.trim().to_string());
                    }
                }
                collect_context_strings(c, found);
            }
        }
        Value::Array(a) => {
            for c in a {
                collect_context_strings(c, found);
            }
        }
        _ => {}
    }
}

/// Prompt-ready, read-only context from persisted nd-cartographer projects.
/// GM/AI only; failures and unknown old layouts return an empty context.
pub fn world_context(cfg: &Config, query: &str) -> String {
    let data = base(cfg).join("data");
    let Ok(entries) = std::fs::read_dir(&data) else {
        return String::new();
    };
    let words = crate::retrieval::query_words(query).unwrap_or_default();
    let mut projects: Vec<(i64, String, Vec<String>)> = vec![];
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let manifest = dir.join("manifest.json");
        if !manifest.is_file() {
            continue;
        }
        let Ok(meta) =
            serde_json::from_slice::<Value>(&std::fs::read(&manifest).unwrap_or_default())
        else {
            continue;
        };
        let project_name = meta
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or_else(|| dir.file_name().and_then(|n| n.to_str()).unwrap_or("Map"))
            .to_string();
        let snap = dir.join("snapshot.json");
        let floors = dir.join("floors.json");
        let source = if snap.is_file() { snap } else { floors };
        let Ok(raw) = std::fs::read_to_string(source) else {
            continue;
        };
        let Ok(parsed) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        let mut strings = vec![];
        collect_context_strings(&parsed, &mut strings);
        if let Ok(journal) = std::fs::read_to_string(dir.join("journal.ndjson")) {
            let lines: Vec<&str> = journal.lines().collect();
            for line in lines.iter().rev().take(300).copied() {
                if let Ok(v) = serde_json::from_str::<Value>(line) {
                    collect_context_strings(&v, &mut strings);
                }
            }
        }
        let mut unique: Vec<String> = vec![];
        for s in strings {
            if !unique.iter().any(|x| x == &s) {
                unique.push(s);
            }
        }
        if unique.is_empty() {
            continue;
        }
        let hay = format!("{project_name} {}", unique.join(" ")).to_lowercase();
        let score = words.iter().map(|w| hay.matches(w).count() as i64).sum();
        if words.is_empty() || score > 0 {
            projects.push((score, project_name, unique));
        }
    }
    projects.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| a.1.to_lowercase().cmp(&b.1.to_lowercase()))
    });
    if projects.is_empty() {
        return String::new();
    }
    let mut lines = vec!["Cartographer maps:".to_string()];
    let mut remaining = 6000usize;
    for (_, name, strings) in projects {
        let mut block = format!("- Map: {name}");
        for value in strings.into_iter().take(80) {
            if block.len() + value.len() + 3 > remaining {
                break;
            }
            block.push_str(&format!("\n  {value}"));
        }
        if block.len() <= remaining {
            lines.push(block.clone());
            remaining = remaining.saturating_sub(block.len());
        }
        if remaining == 0 {
            break;
        }
    }
    lines.join("\n")
}

fn patch_app_js_source(js: &str) -> String {
    let old = r#"function queuePaint(cellX, cellY) {
  if (!state.painting) state.painting = new Set();
  for (const [mx, my] of mirrored(cellX, cellY)) state.painting.add(`${mx},${my}`);
  drawStrokePreview();
  clearTimeout(state.pendingFlush);
  state.pendingFlush = setTimeout(flushStroke, 120);
}"#;
    let live = r#"function queuePaint(cellX, cellY) {
  if (!state.painting) state.painting = new Set();
  for (const [mx, my] of mirrored(cellX, cellY)) state.painting.add(`${mx},${my}`);
  render();
  clearTimeout(state.pendingFlush);
}"#;
    js.replace(old, live)
}

pub async fn patch_app_js(resp: Response<Body>) -> Response<Body> {
    use http_body_util::BodyExt;
    let (parts, body) = resp.into_parts();
    let Ok(bytes) = body.collect().await.map(|c| c.to_bytes()) else {
        return Response::from_parts(parts, Body::from(Vec::<u8>::new()));
    };
    let Ok(js) = std::str::from_utf8(&bytes) else {
        return Response::from_parts(parts, Body::from(bytes));
    };
    let patched = patch_app_js_source(js);
    Response::from_parts(parts, Body::from(patched))
}

#[cfg(test)]
mod live_paint_tests {
    use super::patch_app_js_source;

    #[test]
    fn queue_paint_rerenders_without_mid_drag_flush() {
        let source = r#"function queuePaint(cellX, cellY) {
  if (!state.painting) state.painting = new Set();
  for (const [mx, my] of mirrored(cellX, cellY)) state.painting.add(`${mx},${my}`);
  drawStrokePreview();
  clearTimeout(state.pendingFlush);
  state.pendingFlush = setTimeout(flushStroke, 120);
}"#;
        let patched = patch_app_js_source(source);
        assert!(patched.contains("  render();\n  clearTimeout(state.pendingFlush);"));
        assert!(!patched.contains("setTimeout(flushStroke"));
    }
}

fn reply(code: StatusCode, v: Value) -> Response<Body> {
    Response::builder()
        .status(code)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(v.to_string()))
        .unwrap()
}

/// The GM's active world plus the page context — None when not the GM / no world.
/// The gate for both the management page and the mounted studio router.
pub fn gm_ctx(
    cfg: &Config,
    method: &str,
    uri: &Uri,
    headers: &HeaderMap,
) -> Option<(crate::writes::Pooled, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let p = crate::layout::build(&db, cfg, method, uri, headers, now_secs())?;
    if !p.is_gm || p.world.is_none() {
        return None;
    }
    Some((db, p))
}

/// `GET /cartographer` (the embed page) and `GET /api/cartographer/status`.
pub fn get(
    pages: &Pages,
    cfg: &Config,
    uri: &Uri,
    headers: &HeaderMap,
    is_page: bool,
) -> Option<Response<Body>> {
    let (_db, mut p) = gm_ctx(cfg, "GET", uri, headers)?;
    let st = status(cfg);
    if !is_page {
        return Some(reply(StatusCode::OK, st));
    }
    // The studio is mounted on this same origin; the iframe needs no token (the
    // proxy path is GM-gated by the session).
    p.ctx.insert("cartographer".into(), st);
    p.ctx
        .insert("cartographer_embed_url".into(), json!(STUDIO_BASE));
    crate::pages::render_with(pages, "cartographer.html", p.ctx, headers)
}
