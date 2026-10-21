//! nd-world gateway (docs/RUST_REWRITE_PLAN.md, phase 1).
//!
//! In front of the Python app it answers what needs no Python: `/static/` (with the build's pre-compressed `.br` / `.gz` twins) and the file
//! itself of an `/uploads/` request - after Python has decided that the request may have it. Everything else is passed on, streamed in both
//! directions, to the Python server. See `docs/RUST_GATEWAY.md`.
pub mod config;
pub mod files;
pub mod pages;
pub mod proxy;
pub mod session;
pub mod static_files;
pub mod writes;

use axum::{body::Body, extract::State, http::Request, response::Response, Router};
use std::sync::Arc;

pub use config::Config;

pub fn router(cfg: Arc<Config>) -> Router {
    let proxy = proxy::Proxy::new(cfg.clone());
    let pages = Arc::new(pages::Pages::new(&cfg));
    let state = App { cfg, proxy, pages };
    Router::new().fallback(handle).with_state(state)
}

#[derive(Clone)]
pub struct App {
    pub cfg: Arc<Config>,
    pub proxy: proxy::Proxy,
    pub pages: Arc<pages::Pages>,
}

async fn handle(State(app): State<App>, req: Request<Body>) -> Response {
    let peer = req.extensions().get::<axum::extract::ConnectInfo<std::net::SocketAddr>>().map(|c| c.0);
    let path = req.uri().path();
    if path == "/__gateway/health" {
        let up = app.proxy.upstream_up().await;
        return Response::builder()
            .header("content-type", "application/json")
            .header("cache-control", "no-store")
            .body(Body::from(format!("{{\"gateway\":\"ok\",\"upstream\":{up}}}")))
            .unwrap();
    }
    if path.starts_with("/static/") {
        let (method, uri, headers) = (req.method().clone(), req.uri().clone(), req.headers().clone());
        if let Some(resp) = static_files::serve(&app.cfg, &method, &uri, &headers).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::POST && req.uri().path() == "/api/dice/roll" && app.cfg.rust_routes.iter().any(|r| r == "dice_roll") {
        return writes::dice_roll(app.cfg.clone(), &app.proxy, req, peer).await;
    }
    if let Some(resp) = app.pages.serve(req.method(), req.uri(), req.headers()) {
        return resp;
    }
    app.proxy.forward(req, peer).await
}
