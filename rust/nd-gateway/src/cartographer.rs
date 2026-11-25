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
    p.ctx.insert("cartographer_embed_url".into(), json!(STUDIO_BASE));
    crate::pages::render_with(pages, "cartographer.html", p.ctx, headers)
}