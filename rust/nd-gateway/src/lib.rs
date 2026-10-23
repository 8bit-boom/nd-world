//! nd-world gateway (docs/RUST_REWRITE_PLAN.md, phase 1).
//!
//! In front of the Python app it answers what needs no Python: `/static/` (with the build's pre-compressed `.br` / `.gz` twins) and the file
//! itself of an `/uploads/` request - after Python has decided that the request may have it. Everything else is passed on, streamed in both
//! directions, to the Python server. See `docs/RUST_GATEWAY.md`.
pub mod boards;
pub mod calendar;
pub mod config;
pub mod strict;
pub mod rooms;
pub mod sheets;
pub mod retrieval;
pub mod ai;
pub mod jobs;
pub mod idpages;
pub mod edits;
pub mod files;
pub mod form;
pub mod gallery;
pub mod home;
pub mod imaging;
pub mod markup;
pub mod layout;
pub mod live;
pub mod maps;
pub mod orm;
pub mod media;
pub mod pages;
pub mod proxy;
pub mod search;
pub mod session;
pub mod static_files;
pub mod uploads;
pub mod writes;

use axum::{body::Body, extract::State, http::Request, response::Response, Router};
use std::sync::Arc;

pub use config::Config;

pub fn router(cfg: Arc<Config>) -> Router {
    let proxy = proxy::Proxy::new(cfg.clone());
    let pages = Arc::new(pages::Pages::new(&cfg));
    jobs::init(cfg.clone());
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
    if app.cfg.rust_routes.iter().any(|r| r == "writes") && edits::handles_upload(req.method(), req.uri().path(), req.headers()) {
        return edits::serve_upload(app.cfg.clone(), &app.proxy, req, peer).await;
    }
    if app.cfg.rust_routes.iter().any(|r| r == "writes") && edits::handles(req.method(), req.uri().path()) {
        return edits::serve(app.cfg.clone(), &app.proxy, req, peer).await;
    }
    if req.method() == axum::http::Method::GET && req.uri().path() == "/api/display/stream" && app.cfg.rust_routes.iter().any(|r| r == "live") {
        if let Some(resp) = live::serve_display(app.cfg.clone(), req.uri(), req.headers()).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && req.uri().path() == "/api/live" && app.cfg.rust_routes.iter().any(|r| r == "live") {
        if let Some(resp) = live::serve(app.cfg.clone(), req.uri(), req.headers()).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        if let Some(i) = pages::SIMPLE.iter().position(|s| s.path == req.uri().path()) {
            let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
            if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::simple(&pages_, &cfg, &pages::SIMPLE[i], &uri, &headers)).await {
                return resp;
            }
        }
    }
    if req.method() == axum::http::Method::GET && req.uri().path().starts_with("/entity/") && req.uri().path()[8..].bytes().all(|b| b.is_ascii_digit())
        && req.uri().path().len() > 8 && app.cfg.rust_routes.iter().any(|r| r == "pages")
    {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::entity_detail(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && matches!(req.uri().path(), "/sessions" | "/quests" | "/parties" | "/facts" | "/tables" | "/combat" | "/calendar" | "/facts/fragment" | "/calendar/agenda" | "/calendar/year" | "/calendar/config") && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        let f = match uri.path() {
            "/sessions" => pages::sessions_list,
            "/quests" => pages::quests_list,
            "/parties" => pages::parties_list,
            "/tables" => pages::tables_list,
            "/combat" => pages::combat_list,
            "/calendar" => crate::calendar::calendar_view,
            "/calendar/agenda" => crate::calendar::calendar_agenda,
            "/calendar/year" => crate::calendar::calendar_year,
            "/calendar/config" => crate::calendar::calendar_config,
            _ => pages::facts_list,
        };
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "pages") && idpages::route(req.uri().path()).is_some() {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || idpages::serve(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "pages")
        && (req.uri().path() == "/pages" || req.uri().path().starts_with("/pages/albums/"))
    {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || media::pages_library(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let lib = match req.uri().path() {
            "/audio" => Some(&media::AUDIO),
            "/video" => Some(&media::VIDEO),
            p if p.starts_with("/audio/albums/") => Some(&media::AUDIO),
            p if p.starts_with("/video/albums/") => Some(&media::VIDEO),
            _ => None,
        };
        if let Some(lib) = lib {
            let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
            if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || media::clip_library(lib, &pages_, &cfg, &uri, &headers)).await {
                return resp;
            }
        }
    }
    if req.method() == axum::http::Method::GET && (req.uri().path() == "/images" || req.uri().path().starts_with("/images/albums/"))
        && app.cfg.rust_routes.iter().any(|r| r == "pages")
    {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        let f = if uri.path() == "/images" { gallery::images_gallery } else { gallery::album_detail };
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && (req.uri().path() == "/maps" || req.uri().path().starts_with("/maps/"))
        && app.cfg.rust_routes.iter().any(|r| r == "pages")
    {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        let f = if uri.path() == "/maps" { maps::maps_page } else { maps::map_viewer };
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && req.uri().path().starts_with("/boards/") && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        // /boards/<slug>/export stays Python's: board_export.html fills a dict with `.update()`, which MiniJinja's values cannot do
        let f = if uri.path().ends_with("/notes") {
            Some(boards::board_notes as fn(&pages::Pages, &config::Config, &axum::http::Uri, &axum::http::HeaderMap) -> Option<Response<Body>>)
        } else if uri.path().ends_with("/export") {
            None
        } else {
            Some(boards::board_view as fn(&pages::Pages, &config::Config, &axum::http::Uri, &axum::http::HeaderMap) -> Option<Response<Body>>)
        };
        if let Some(f) = f {
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
        }
    }
    if req.method() == axum::http::Method::GET && matches!(req.uri().path(), "/" | "/new" | "/rules" | "/search") && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        let f = match uri.path() {
            "/new" => idpages::new_entity_form,
            "/search" => search::search_page,
            "/" => home::home,
            _ => idpages::rules_page,
        };
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && req.uri().path().starts_with("/kind/") && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let (pages_, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::kind_list(&pages_, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && ((req.uri().path().starts_with("/maps/schematic/") && (req.uri().path().ends_with("/view") || req.uri().path().ends_with("/view.json")))
            || (req.uri().path().starts_with("/display/map/") && req.uri().path().ends_with("/data.json"))) {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::schematic_views(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && (req.uri().path().starts_with("/api/combat/") || req.uri().path().starts_with("/api/display/combat/")) && req.uri().path().ends_with("/state") {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::combat_state_json(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::POST && req.uri().path().ends_with("/cancel") && app.cfg.rust_routes.iter().any(|r| r == "ai") {
        let resp = if req.uri().path().starts_with("/api/audio-jobs/") {
            ai::audio_job_cancel(app.cfg.clone(), req.uri().clone(), req.headers().clone()).await
        } else if req.uri().path().starts_with("/api/ai/chat/jobs/") {
            ai::chat_job_cancel(app.cfg.clone(), req.uri().clone(), req.headers().clone()).await
        } else {
            None
        };
        if let Some(resp) = resp {
            return resp;
        }
    }
    if req.method() == axum::http::Method::POST && (matches!(req.uri().path(), "/api/chronicler/ask" | "/api/ai/assist-job" | "/api/ai/chat/jobs" | "/api/ai/world-summary" | "/api/sessions/ai/condense-job" | "/api/facts/parse-job" | "/api/ai/stream" | "/api/ai/assist") || (req.uri().path().starts_with("/api/npc-talk/") && req.uri().path().ends_with("/stream")) || (req.uri().path().starts_with("/api/session-log/") && req.uri().path().ends_with("/recap")) || (req.uri().path().starts_with("/api/sessions/") && req.uri().path().ends_with("/ai/summarize-live-transcript-job"))) && app.cfg.rust_routes.iter().any(|r| r == "ai") {
        let (parts, body) = req.into_parts();
        let Ok(bytes) = axum::body::to_bytes(body, 1 << 20).await else {
            return Response::builder().status(axum::http::StatusCode::PAYLOAD_TOO_LARGE).body(Body::empty()).unwrap();
        };
        let served = if parts.uri.path().ends_with("/ai/summarize-live-transcript-job") {
            ai::text_recap_job_create(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path().starts_with("/api/session-log/") {
            ai::session_log_recap(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path().starts_with("/api/npc-talk/") {
            ai::npc_talk_stream(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/facts/parse-job" {
            ai::facts_parse_job_create(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/sessions/ai/condense-job" {
            ai::condense_job_create(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/ai/world-summary" {
            ai::world_summary_create(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/ai/chat/jobs" {
            ai::chat_job_create(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/ai/assist-job" {
            ai::ai_assist_job(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/ai/assist" {
            ai::ai_assist(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else if parts.uri.path() == "/api/ai/stream" {
            ai::ai_stream(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        } else {
            ai::chronicler_ask(app.cfg.clone(), parts.uri.clone(), parts.headers.clone(), bytes.clone()).await
        };
        if let Some(resp) = served {
            return resp;
        }
        return app.proxy.forward(Request::from_parts(parts, Body::from(bytes)), peer).await;
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && req.uri().path().starts_with("/maps/schematic/") && req.uri().path().ends_with("/bg.webp") {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::schematic_masked_background(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && req.uri().path().strip_prefix("/characters/").is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())) {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::character_sheet(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && req.uri().path().strip_prefix("/combat/").is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())) {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::combat_tracker(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && app.cfg.rust_routes.iter().any(|r| r == "live")
        && req.uri().path().strip_prefix("/maps/schematic/").is_some_and(|s| !s.is_empty() && !s.contains('/')) {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::schematic_editor(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && (req.uri().path() == "/display" || req.uri().path().starts_with("/display/map/") || req.uri().path().starts_with("/display/combat/"))
        && !req.uri().path().ends_with("/data.json") && app.cfg.rust_routes.iter().any(|r| r == "live") {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::display_pages(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && matches!(req.uri().path(), "/cockpit" | "/player-cockpit") && app.cfg.rust_routes.iter().any(|r| r == "live") {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        let f = if uri.path() == "/cockpit" { pages::cockpit } else { pages::player_cockpit };
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || f(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if req.method() == axum::http::Method::GET && req.uri().path() == "/dice" && app.cfg.rust_routes.iter().any(|r| r == "pages") {
        let (pages, cfg, uri, headers) = (app.pages.clone(), app.cfg.clone(), req.uri().clone(), req.headers().clone());
        if let Ok(Some(resp)) = tokio::task::spawn_blocking(move || pages::dice(&pages, &cfg, &uri, &headers)).await {
            return resp;
        }
    }
    if let Some(resp) = app.pages.serve(req.method(), req.uri(), req.headers()) {
        return resp;
    }
    app.proxy.forward(req, peer).await
}
