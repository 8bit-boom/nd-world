use nd_gateway::{router, Config};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

#[tokio::main]
async fn main() {
    if std::env::args().any(|a| a == "--version") {
        println!("nd-gateway {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let cfg = Arc::new(Config::from_env());
    nd_render::set_paths(cfg.static_dir.clone(), cfg.db_path.parent().map(|p| p.join("uploads")).unwrap_or_default());
    let listener = match tokio::net::TcpListener::bind(&cfg.listen).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("nd-gateway: cannot listen on {}: {e}", cfg.listen);
            std::process::exit(1);
        }
    };
    eprintln!("nd-gateway: {} -> {} (static: {})", cfg.listen, cfg.upstream, cfg.static_dir.display());
    // boot a fresh database (schema snapshot + seed rows) when the volume has none — in the Python-free
    // image this is the only boot that will ever happen
    nd_gateway::startup::init_db(&cfg);
    // the boot sweep: any job mid-flight when the last process stopped is marked interrupted (idempotent)
    nd_gateway::startup::sweep_interrupted(&cfg);
    nd_gateway::startup::db_upkeep(&cfg);
    nd_gateway::startup::backups(&cfg);
    let ai_jobs = cfg.rust_routes.iter().any(|r| r == "ai");
    let fans = cfg.rust_routes.iter().any(|r| r == "fans").then(|| cfg.clone());
    let app = router(cfg).into_make_service_with_connect_info::<SocketAddr>();
    if let Some(c) = &fans {
        // "follow the GPU temperature", when it was saved as on (docs/RUST_FANS.md)
        nd_gateway::fans::start(c);
    }
    if ai_jobs {
        // the background jobs a previous gateway held (docs/RUST_JOBS.md)
        tokio::task::spawn_blocking(nd_gateway::jobs::adopt);
    }
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let serve = axum::serve(listener, app).with_graceful_shutdown(async move {
        let _ = rx.await;
    });
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = tx.send(());
        // running jobs are marked interrupted (their ownership kept) so the next start resumes them
        let _ = tokio::task::spawn_blocking(nd_gateway::jobs::stop_all).await;
        // no fan this app holds is left slow while another container may still load the GPU
        if let Some(c) = fans {
            let _ = tokio::task::spawn_blocking(move || nd_gateway::fans::shutdown_safe(&c)).await;
        }
        // long-lived streams (server-sent events) would keep a graceful shutdown waiting for ever; Python has its own drain
        tokio::time::sleep(Duration::from_secs(8)).await;
        std::process::exit(0);
    });
    if let Err(e) = serve.await {
        eprintln!("nd-gateway: {e}");
        std::process::exit(1);
    }
}

async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).expect("signal");
    let mut int = signal(SignalKind::interrupt()).expect("signal");
    tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
}
