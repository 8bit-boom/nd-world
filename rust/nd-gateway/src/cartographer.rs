//! The embedded nd-cartographer map maker: nd-world downloads the release binary,
//! launches it as a managed child process, and embeds the studio in an iframe.
//! GM-only and Rust-only (a subprocess this gateway manages — there is no Python
//! counterpart), modeled on `lore_forge`'s install/runner and nd-cartographer's own
//! desktop sidecar (free port -> spawn -> wait-for-listen -> kill on stop).
//!
//! Durable state is one JSON file (`config.json`) under `{accel_root}/cartographer/`,
//! merged like lore_forge's `state.json`; the binary lives in `bin/`, the studio's
//! projects in `data/`. The child handle is a process-global `Mutex<Option<Child>>`.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::Pages;

const REPO_API: &str = "https://api.github.com/repos/8bit-boom/nd-cartographer/releases/latest";
const MAX_DOWNLOAD: u64 = 256 * 1024 * 1024;
const UA: &str = "nd-world-gateway (cartographer-install)";

/// The running studio, stopped again on uninstall / shutdown. Interior mutability
/// because the exit hook and the request handlers share it.
static CHILD: Mutex<Option<Child>> = Mutex::new(None);

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

// ── paths & durable state ──────────────────────────────────────────────────────

pub fn base(cfg: &Config) -> PathBuf {
    cfg.accel_root.join("cartographer")
}
fn bin_path(base: &Path) -> PathBuf {
    base.join("bin").join(if cfg!(windows) { "ndcart.exe" } else { "ndcart" })
}
fn data_dir(base: &Path) -> PathBuf {
    base.join("data")
}

pub fn read_state(base: &Path) -> Value {
    std::fs::read(base.join("config.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_else(|| json!({}))
}

/// Merge `patch` into config.json (tmp + rename, like lore_forge's state).
pub fn write_state(base: &Path, patch: Value) {
    let mut st = read_state(base);
    if let (Some(o), Some(p)) = (st.as_object_mut(), patch.as_object()) {
        for (k, v) in p {
            o.insert(k.clone(), v.clone());
        }
    }
    let _ = std::fs::create_dir_all(base);
    let tmp = base.join("config.json.tmp");
    if std::fs::write(&tmp, st.to_string()).is_ok() {
        let _ = std::fs::rename(tmp, base.join("config.json"));
    }
}

// ── the child process ──────────────────────────────────────────────────────────

fn child_running() -> bool {
    let mut g = CHILD.lock().expect("cartographer child lock");
    match g.as_mut() {
        Some(c) => matches!(c.try_wait(), Ok(None)),
        None => false,
    }
}

/// Bind-and-drop: the studio binds immediately after and reports its own failure
/// if the port was taken (the desktop shell uses the same trick).
fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8300)
}

fn gen_token() -> String {
    use rand::RngCore;
    let mut buf = [0u8; 24];
    rand::rngs::OsRng.fill_bytes(&mut buf);
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

fn spawn_child(bin: &Path, port: u16, token: &str, data: &Path) -> Result<(), String> {
    std::fs::create_dir_all(data).map_err(|e| format!("cannot create the data folder: {e}"))?;
    let child = Command::new(bin)
        .args([
            "serve",
            "--listen",
            &format!("127.0.0.1:{port}"),
            "--data-dir",
            &data.to_string_lossy(),
        ])
        .env("ND_CARTO_TOKEN", token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("cannot start ndcart: {e}"))?;
    *CHILD.lock().expect("cartographer child lock") = Some(child);
    Ok(())
}

/// Wait until something listens on `port` (the studio binds only when ready).
fn wait_health(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

/// Stop the studio (uninstall, the Stop button, and gateway shutdown).
pub fn stop() {
    let mut g = CHILD.lock().expect("cartographer child lock");
    if let Some(mut c) = g.take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

// ── status & lifecycle ─────────────────────────────────────────────────────────

/// The one payload the page, the status route and the actions render from.
pub fn status(cfg: &Config) -> Value {
    let b = base(cfg);
    let st = read_state(&b);
    let flag = |k: &str| st.get(k).and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    json!({
        "installed": bin_path(&b).is_file(),
        "enabled": flag("enabled"),
        "running": child_running(),
        "autostart": flag("autostart"),
        "url": st.get("url").cloned().unwrap_or(Value::Null),
        "version": st.get("version").cloned().unwrap_or(Value::Null),
        "port": st.get("port").cloned().unwrap_or(Value::Null),
        "dir": b.to_string_lossy(),
    })
}

/// Start the installed studio (the Start button, and boot autostart).
pub fn start(cfg: &Config) -> Result<Value, String> {
    let b = base(cfg);
    let bin = bin_path(&b);
    if !bin.is_file() {
        return Err("The map maker is not installed yet.".into());
    }
    if child_running() {
        return Ok(status(cfg));
    }
    let st = read_state(&b);
    let token = st
        .get("token")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(String::from)
        .unwrap_or_else(gen_token);
    let port = st
        .get("port")
        .and_then(|v| v.as_u64())
        .filter(|p| *p > 0)
        .map(|p| p as u16)
        .unwrap_or_else(free_port);
    spawn_child(&bin, port, &token, &data_dir(&b))?;
    if !wait_health(port, Duration::from_secs(20)) {
        stop();
        return Err("The map maker did not start in time.".into());
    }
    write_state(
        &b,
        json!({"enabled": 1, "url": format!("http://127.0.0.1:{port}"), "token": token, "port": port}),
    );
    Ok(status(cfg))
}

/// Stop the studio but keep it installed (the Stop button).
pub fn stop_keep(cfg: &Config) -> Value {
    stop();
    let b = base(cfg);
    write_state(&b, json!({"enabled": 0}));
    status(cfg)
}

/// Uninstall: stop, clear the registration. The binary and the projects folder are
/// kept (a re-install reuses them); only the running registration is removed.
pub fn uninstall(cfg: &Config) -> Value {
    stop();
    let b = base(cfg);
    write_state(
        &b,
        json!({"enabled": 0, "autostart": 0, "url": Value::Null, "token": Value::Null, "port": Value::Null}),
    );
    status(cfg)
}

/// Boot autostart: if the GM left it enabled, bring the studio back up.
pub fn autostart(cfg: &Config) {
    let st = read_state(&base(cfg));
    let enabled = st.get("enabled").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    let auto = st.get("autostart").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    if enabled && auto {
        if let Err(e) = start(cfg) {
            eprintln!("nd-gateway: cartographer autostart failed: {e}");
        }
    }
}

// ── download the release binary ────────────────────────────────────────────────

/// The release asset suffix for this platform: (suffix, is_zip). The Linux release
/// is musl-static, so it runs inside the gateway's debian-slim container.
fn platform_asset() -> Option<(&'static str, bool)> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Some(("-linux-x86_64.tar.gz", false)),
        ("windows", "x86_64") => Some(("-windows-x86_64.zip", true)),
        ("macos", "aarch64") => Some(("-macos-arm64.tar.gz", false)),
        _ => None,
    }
}

async fn fetch_url(url: &str, max_bytes: u64) -> Result<bytes::Bytes, String> {
    let work = async {
        let mut current: hyper::Uri = url.parse().map_err(|e| format!("bad url: {e}"))?;
        for _ in 0..10 {
            let req = hyper::Request::builder()
                .method("GET")
                .uri(current.clone())
                .header("user-agent", UA)
                .header("accept", "application/vnd.github+json")
                .body(http_body_util::Full::new(bytes::Bytes::new()))
                .map_err(|e| format!("{e}"))?;
            let resp = crate::net::client().request(req).await.map_err(|e| format!("{url}: {e}"))?;
            let status = resp.status().as_u16();
            if matches!(status, 301 | 302 | 303 | 307 | 308) {
                if let Some(loc) = resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                    current = crate::studio_key::resolve_location(&current, loc).map_err(|_| format!("{url}: bad redirect"))?;
                    let _ = http_body_util::BodyExt::collect(resp.into_body()).await;
                    continue;
                }
            }
            if status != 200 {
                return Err(format!("{url}: HTTP {status}"));
            }
            let body = http_body_util::BodyExt::collect(resp.into_body()).await.map_err(|e| format!("{url}: {e}"))?.to_bytes();
            if body.len() as u64 > max_bytes {
                return Err(format!("{url}: past the {max_bytes} byte cap"));
            }
            return Ok(body);
        }
        Err(format!("{url}: too many redirects"))
    };
    tokio::time::timeout(Duration::from_secs(300), work).await.map_err(|_| format!("{url}: timed out"))?
}

/// Extract just the `ndcart` binary from the release archive (tar.gz or zip),
/// refusing any unsafe path. Mirrors lore_forge's `ensure_uv` extraction.
fn extract_binary(body: &[u8], dest: &Path, is_zip: bool) -> Result<(), String> {
    let want = if cfg!(windows) { "ndcart.exe" } else { "ndcart" };
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("creating {}: {e}", parent.display()))?;
    }
    let cursor = std::io::Cursor::new(body);
    if is_zip {
        let mut za = zip::ZipArchive::new(cursor).map_err(|e| format!("the release is not a zip: {e}"))?;
        for i in 0..za.len() {
            let mut f = za.by_index(i).map_err(|e| format!("the release archive: {e}"))?;
            let name = f.name().to_string().replace('\\', "/");
            let stripped = name.rsplit('/').next().unwrap_or(&name);
            if stripped != want || name.split('/').any(|p| p == "..") {
                continue;
            }
            let mut out = std::fs::File::create(dest).map_err(|e| format!("writing ndcart: {e}"))?;
            std::io::copy(&mut f, &mut out).map_err(|e| format!("writing ndcart: {e}"))?;
            return Ok(());
        }
    } else {
        let gz = flate2::read::GzDecoder::new(cursor);
        let mut tar = tar::Archive::new(gz);
        for m in tar.entries().map_err(|e| format!("the release archive: {e}"))? {
            let mut m = m.map_err(|e| format!("the release archive: {e}"))?;
            let name = m.path().map_err(|e| format!("the release archive: {e}"))?.to_string_lossy().replace('\\', "/");
            let stripped = name.rsplit('/').next().unwrap_or(&name);
            if stripped != want || name.split('/').any(|p| p == "..") {
                continue;
            }
            let mut out = std::fs::File::create(dest).map_err(|e| format!("writing ndcart: {e}"))?;
            std::io::copy(&mut m, &mut out).map_err(|e| format!("writing ndcart: {e}"))?;
            return Ok(());
        }
    }
    Err(format!("the release archive holds no {want} binary"))
}

/// Download the latest release's platform asset and extract the binary. Returns
/// the release tag (the installed version).
async fn download_binary(base: &Path) -> Result<String, String> {
    let (suffix, is_zip) = platform_asset().ok_or("This platform has no prebuilt map-maker binary — build nd-cartographer from source and place it in the bin folder.")?;
    let meta = fetch_url(REPO_API, 4 * 1024 * 1024).await?;
    let rel: Value = serde_json::from_slice(&meta).map_err(|e| format!("release metadata: {e}"))?;
    let tag = rel.get("tag_name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let assets = rel.get("assets").and_then(|v| v.as_array()).ok_or("the latest release has no assets")?;
    let asset = assets
        .iter()
        .find(|a| {
            let n = a.get("name").and_then(|v| v.as_str()).unwrap_or("");
            n.starts_with("ndcart-v") && n.ends_with(suffix)
        })
        .ok_or_else(|| format!("no map-maker asset matching *{suffix} in {tag}"))?;
    let url = asset.get("browser_download_url").and_then(|v| v.as_str()).ok_or("the asset has no download url")?;
    let body = fetch_url(url, MAX_DOWNLOAD).await?;
    let dest = bin_path(base);
    let d = dest.clone();
    tokio::task::spawn_blocking(move || extract_binary(&body, &d, is_zip))
        .await
        .map_err(|e| format!("the unpack task died: {e}"))??;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o755));
    }
    Ok(tag)
}

/// Install: download the binary, launch it, register it. Async (the download).
pub async fn install(cfg: Arc<Config>) -> Result<Value, String> {
    let b = base(&cfg);
    std::fs::create_dir_all(b.join("bin")).map_err(|e| format!("cannot create the bin folder: {e}"))?;
    // A prepared binary (ND_CARTOGRAPHER_BIN_DIR) is copied as-is — tests and
    // air-gapped installs; otherwise the latest release is fetched over TLS.
    let version = if let Ok(dir) = std::env::var("ND_CARTOGRAPHER_BIN_DIR") {
        let src = PathBuf::from(dir.trim()).join(if cfg!(windows) { "ndcart.exe" } else { "ndcart" });
        if !src.is_file() {
            return Err(format!("ND_CARTOGRAPHER_BIN_DIR has no ndcart binary: {}", src.display()));
        }
        std::fs::copy(&src, bin_path(&b)).map_err(|e| format!("copying the prepared binary: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(bin_path(&b), std::fs::Permissions::from_mode(0o755));
        }
        std::env::var("ND_CARTOGRAPHER_VERSION").unwrap_or_else(|_| "local".to_string())
    } else {
        download_binary(&b).await?
    };
    let token = gen_token();
    let port = free_port();
    let bin = bin_path(&b);
    let data = data_dir(&b);
    let url = format!("http://127.0.0.1:{port}");
    let (u, v) = (url.clone(), version.clone());
    tokio::task::spawn_blocking(move || -> Result<(), String> {
        spawn_child(&bin, port, &token, &data)?;
        if !wait_health(port, Duration::from_secs(20)) {
            stop();
            return Err("The map maker downloaded but did not start in time.".into());
        }
        write_state(
            &b,
            json!({"enabled": 1, "autostart": 1, "url": u, "token": token, "port": port, "version": v, "installed_at": now_secs()}),
        );
        Ok(())
    })
    .await
    .map_err(|e| format!("the start task died: {e}"))??;
    Ok(status(&cfg))
}

// ── routes (dispatched from lib.rs, GM-only, Rust-only) ─────────────────────────

fn reply(code: StatusCode, v: Value) -> Response<Body> {
    Response::builder()
        .status(code)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(v.to_string()))
        .unwrap()
}
fn detail(code: StatusCode, m: &str) -> Response<Body> {
    reply(code, json!({"detail": m}))
}

/// The GM's active world plus the page context — None when not the GM / no world.
fn gm_ctx(cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap) -> Option<(crate::writes::Pooled, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let p = crate::layout::build(&db, cfg, method, uri, headers, now_secs())?;
    if !p.is_gm || p.world.is_none() {
        return None;
    }
    Some((db, p))
}

/// `GET /cartographer` (the management page + embed) and `GET /api/cartographer/status`.
pub fn get(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap, is_page: bool) -> Option<Response<Body>> {
    let (_db, mut p) = gm_ctx(cfg, "GET", uri, headers)?;
    let st = status(cfg);
    if !is_page {
        return Some(reply(StatusCode::OK, st));
    }
    // The embed URL carries the studio token so the iframe authenticates.
    let embed = st
        .get("url")
        .and_then(|v| v.as_str())
        .filter(|_| st.get("running").and_then(|v| v.as_bool()).unwrap_or(false))
        .map(|u| {
            let token = read_state(&base(cfg)).get("token").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if token.is_empty() { u.to_string() } else { format!("{u}/?token={token}") }
        });
    p.ctx.insert("cartographer".into(), st);
    p.ctx.insert("cartographer_embed_url".into(), json!(embed));
    crate::pages::render_with(pages, "cartographer.html", p.ctx, headers)
}

/// The POST actions: install / uninstall / start / stop. The download is async;
/// the rest run off the async pool.
pub async fn post(cfg: Arc<Config>, uri: Uri, headers: HeaderMap) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let action = path.rsplit('/').next().unwrap_or("").to_string();
    if !matches!(action.as_str(), "install" | "uninstall" | "start" | "stop") {
        return None;
    }
    // GM gate first (off the async pool).
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let gated = tokio::task::spawn_blocking(move || gm_ctx(&c, "POST", &u, &h).map(|_| ())).await.ok()?;
    if gated.is_none() {
        return Some(detail(StatusCode::FORBIDDEN, "GM access required"));
    }
    if action == "install" {
        return Some(match install(cfg).await {
            Ok(st) => reply(StatusCode::OK, st),
            Err(e) => detail(StatusCode::CONFLICT, &e),
        });
    }
    let result = tokio::task::spawn_blocking(move || -> Result<Value, String> {
        match action.as_str() {
            "start" => start(&cfg),
            "stop" => Ok(stop_keep(&cfg)),
            "uninstall" => Ok(uninstall(&cfg)),
            _ => Err("unknown action".into()),
        }
    })
    .await
    .map_err(|e| format!("the action task died: {e}"));
    Some(match result {
        Ok(Ok(st)) => reply(StatusCode::OK, st),
        Ok(Err(e)) => detail(StatusCode::CONFLICT, &e),
        Err(e) => detail(StatusCode::INTERNAL_SERVER_ERROR, &e),
    })
}