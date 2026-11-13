//! The five runners — install, setup-env, train, export, install-model — as in-memory live jobs (`crate::runners`),
//! so the Background Jobs page lists, cancels and restarts them and each takes the shared AI slot (`jobs::slot`)
//! for its whole length: one GPU, one training at a time, and no chat model swapping in underneath a run.
//!
//! Every runner is honest in its status dict ({status, stage, error, log, …}, mirrored into state.json's run entries
//! so the page survives a restart) and never rewrites its own job id. A cancelled train/export (the Background Jobs
//! page's abort) kills the worker subprocess (`kill_on_drop`) and marks the run interrupted — the STOP-file stop is
//! the graceful variant that keeps the checkpoint.
//!
//! Test hooks (documented on each use): ND_LORE_FORGE_REPO_DIR (install copies a prepared repo instead of
//! downloading), ND_LORE_FORGE_UV_DIR (setup-env uses a prepared uv instead of downloading), ND_LORE_FORGE_PYTHON
//! (mod.rs env_python), ND_LORE_FORGE_WORKER_ARGS (mod.rs worker_args — a stub worker).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{lf_dir, run_dir, run_out, set_state, update_run, worker_args, DEFAULT_PRESET};

// The runners' kinds — one status dict each, the Background Jobs page's labels in runners::run.
const K_INSTALL: &str = "lore_forge_install";
const K_ENV: &str = "lore_forge_env";
const K_TRAIN: &str = "lore_forge_train";
const K_EXPORT: &str = "lore_forge_export";
const K_MODEL: &str = "lore_forge_model";
const PAGE_KINDS: [(&str, &str); 5] = [
    (K_INSTALL, "Lore Forge: install"),
    (K_ENV, "Lore Forge: training environment"),
    (K_TRAIN, "Lore Forge: training"),
    (K_EXPORT, "Lore Forge: GGUF export"),
    (K_MODEL, "Lore Forge: install model"),
];

const KEEP_DONE: usize = 10;

pub fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Is a job of this kind queued or running right now?
pub fn running(kind: &'static str) -> bool {
    runners_find(kind, |e| e.get("status").and_then(|s| s.as_str()).is_some_and(|s| matches!(s, "running" | "queued"))).is_some()
}

/// Any Lore Forge job still active (uninstall refuses while one is)
pub fn anything_running() -> bool {
    PAGE_KINDS.iter().any(|(k, _)| running(k))
}

fn runners_find(kind: &'static str, pred: impl Fn(&Map<String, Value>) -> bool) -> Option<i64> {
    crate::runners::find(kind, |e| pred(e))
}

fn runners_entry(kind: &'static str, id: i64) -> Option<Value> {
    crate::runners::entry(kind, id)
}

/// The live status dict of a kind for the page: the running job, else the one recorded last in state.json.
fn current_entry(cfg_base: &Path, kind: &'static str) -> Option<Value> {
    if let Some(id) = runners_find(kind, |e| e.get("status").and_then(|s| s.as_str()).is_some_and(|s| matches!(s, "running" | "queued"))) {
        return runners_entry(kind, id);
    }
    let st = super::read_state(cfg_base);
    let id = st.get("jobs").and_then(|j| j.get(kind)).and_then(|j| j.as_i64())?;
    runners_entry(kind, id).filter(|e| e.get("status").and_then(|s| s.as_str()) != Some("running"))
}

fn start(kind: &'static str, label: &str, cfg: &Arc<crate::config::Config>, initial: Value, work: Arc<dyn Fn(Arc<crate::config::Config>, i64) -> sealed::Fut + Send + Sync>) -> i64 {
    let id = crate::runners::new_job(kind, initial, KEEP_DONE);
    // the job id is recorded so the page can show the last finished run of the kind after a restart
    let c2 = cfg.clone();
    if let Some(base) = lf_dir(cfg) {
        let mut st = super::read_state(&base);
        if let Some(o) = st.as_object_mut() {
            let jobs = o.entry("jobs").or_insert_with(|| json!({}));
            if let Some(j) = jobs.as_object_mut() {
                j.insert(kind.to_string(), json!(id));
            }
        }
        super::write_state(&base, &st);
    }
    let rerun: crate::runners::Rerun = Arc::new(move |id| {
        let c = c2.clone();
        let work = work.clone();
        Box::pin(async move { work(c, id).await })
    });
    crate::runners::run(kind, label, id, rerun);
    id
}

/// Everything the futures need, boxed behind one type so `start`'s closure is nameable
mod sealed {
    pub type Fut = std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>;
}

fn fail(kind: &'static str, id: i64, msg: &str) {
    crate::runners::update(kind, id, |e| {
        e.insert("status".into(), json!("error"));
        e.insert("error".into(), json!(msg));
    });
}

fn ok(kind: &'static str, id: i64, patch: Value) {
    crate::runners::update(kind, id, |e| {
        if let Some(p) = patch.as_object() {
            for (k, v) in p {
                e.insert(k.clone(), v.clone());
            }
        }
        e.insert("status".into(), json!("done"));
    });
}

// ----------------------------------------------------------------------------------------------------------------- subprocess plumbing

/// Runs one command, streaming its stdout+stderr lines into the status dict (and any extra sink). Never a shell: the
/// argv is exact. The child dies with the runner (kill_on_drop), so an abort on the Background Jobs page cannot leave
/// an orphan torch process burning the GPU.
async fn stream_cmd(
    argv: &[String],
    cwd: Option<&Path>,
    extra_env: &[(&str, &str)],
    kind: &'static str,
    job_id: i64,
    base: &Path,
    run_id: Option<&str>,
    stage: &str,
) -> Result<i64, String> {
    use tokio::io::AsyncBufReadExt;
    let mut cmd = tokio::process::Command::new(&argv[0]);
    cmd.args(&argv[1..]).stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().map_err(|e| format!("{} could not start: {e}", argv[0]))?;
    crate::runners::update(kind, job_id, |e| {
        e.insert("stage".into(), json!(stage));
        e.insert("pid".into(), json!(child.id()));
    });
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<String>();
    let mut readers = Vec::new();
    if let Some(out) = child.stdout.take() {
        let tx = tx.clone();
        readers.push(tokio::spawn(async move {
            let mut lines = tokio::io::BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if tx.send(line).is_err() {
                    break;
                }
            }
        }));
    }
    if let Some(err) = child.stderr.take() {
        let tx = tx.clone();
        readers.push(tokio::spawn(async move {
            let mut lines = tokio::io::BufReader::new(err).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if tx.send(line).is_err() {
                    break;
                }
            }
        }));
    }
    drop(tx);
    let mut status = crate::runners::entry(kind, job_id).unwrap_or_else(|| json!({"status": "running"}));
    while let Some(line) = rx.recv().await {
        super::apply_event(&mut status, &line);
        let snapshot = status.clone();
        crate::runners::update(kind, job_id, |e| *e = snapshot.as_object().cloned().unwrap_or_default());
        if let Some(rid) = run_id {
            // the page reads state.json, so the live numbers ride along there too
            update_run(base, rid, json!({"live": snapshot, "updated": now_secs()}));
        }
    }
    for r in readers {
        let _ = r.await;
    }
    let rc = child.wait().await.map_err(|e| format!("{} died unreadable: {e}", argv[0]))?;
    Ok(rc.code().map(|c| c as i64).unwrap_or(-1))
}

// ----------------------------------------------------------------------------------------------------------------- install

/// Copies a prepared repo (ND_LORE_FORGE_REPO_DIR) or downloads + extracts the Llm-training tarball.
pub fn start_install(cfg: &Arc<crate::config::Config>) -> Result<i64, String> {
    if running(K_INSTALL) {
        return Err("An install is already running.".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder to install into.")?;
    if super::installed(&base) {
        return Err("Lore Forge is installed already.".into());
    }
    Ok(start(K_INSTALL, "Lore Forge: install", cfg, json!({"status": "running", "stage": "resolve", "error": "", "log": []}), Arc::new(move |c, id| {
        Box::pin(async move { install_run(c, id).await })
    })))
}

async fn install_run(cfg: Arc<crate::config::Config>, id: i64) {
    let Some(base) = lf_dir(&cfg) else { return fail(K_INSTALL, id, "No database folder.") };
    // a prepared repo (tests, air-gapped installs) is copied as-is; otherwise the tarball is fetched then unpacked
    let res: Result<(String, usize), String> = if std::env::var_os("ND_LORE_FORGE_REPO_DIR").is_some() {
        let base2 = base.clone();
        tokio::task::spawn_blocking(move || -> Result<(String, usize), String> {
            let src = PathBuf::from(std::env::var("ND_LORE_FORGE_REPO_DIR").unwrap_or_default().trim());
            let mut n = 0usize;
            for part in ["worker", "catalog"] {
                let from = src.join(part);
                if !from.is_dir() {
                    continue;
                }
                let to = super::repo_dir(&base2).join(part);
                let _ = std::fs::remove_dir_all(&to);
                copy_dir(&from, &to)?;
                n += walk_count(&to);
            }
            Ok(("local".into(), n))
        }).await.unwrap_or_else(|e| Err(format!("the copy task died: {e}")))
    } else {
        async {
            let branch = fetch_branch().await?;
            let archive = fetch_tarball(&branch).await?;
            let n = extract_tarball(&archive, &super::repo_dir(&base))?;
            let _ = std::fs::remove_file(&archive);
            Ok((branch, n))
        }.await
    };
    match res {
        Ok((branch, files)) => {
            if !super::installed(&base) {
                return fail(K_INSTALL, id, "The downloaded repo has no worker/ndforge_worker — install aborted, nothing kept.");
            }
            set_state(&base, json!({"installed": true, "branch": branch, "installed_at": now_secs(), "install_error": Value::Null}));
            ok(K_INSTALL, id, json!({"stage": "", "branch": branch, "files": files}));
        }
        Err(e) => {
            set_state(&base, json!({"install_error": e}));
            fail(K_INSTALL, id, &e);
        }
    }
}

async fn fetch_branch() -> Result<String, String> {
    if let Ok(b) = std::env::var("ND_LORE_FORGE_BRANCH") {
        let b = b.trim().to_string();
        if !b.is_empty() {
            return Ok(b);
        }
    }
    let work = async {
        let req = hyper::Request::builder().method("GET")
            .uri(super::REPO_API_URL).header("user-agent", "NeonDragons-WorldBuilder/1.0")
            .header("accept", "application/vnd.github+json")
            .body(http_body_util::Full::new(bytes::Bytes::new())).ok()?;
        let resp = crate::net::client().request(req).await.ok()?;
        let body = http_body_util::BodyExt::collect(resp.into_body()).await.ok()?.to_bytes();
        serde_json::from_slice::<Value>(&body).ok()?.get("default_branch").and_then(|b| b.as_str()).map(String::from)
    };
    tokio::time::timeout(std::time::Duration::from_secs(20), work).await.ok().flatten().ok_or_else(|| {
        "The repository's default branch could not be read from GitHub (no network?). Set ND_LORE_FORGE_BRANCH or install from a local checkout.".to_string()
    })
}

/// The URL as fetched: with ND_TEST_HOOKS a stand-in server replaces the host (ND_WEB_FETCH_BASE), like kiy.rs
fn target(url: &str) -> String {
    if std::env::var_os("ND_TEST_HOOKS").is_some() {
        if let Ok(base) = std::env::var("ND_WEB_FETCH_BASE") {
            if let Some(rest) = url.strip_prefix("https://") {
                let path = rest.find('/').map_or("/", |i| &rest[i..]);
                return format!("{}{}", base.trim_end_matches('/'), path);
            }
        }
    }
    url.to_string()
}

async fn fetch_url(url: &str, agent: &str, max_bytes: u64) -> Result<bytes::Bytes, String> {
    let work = async {
        let mut current: hyper::Uri = target(url).parse().map_err(|e| format!("bad url: {e}"))?;
        for _ in 0..10 {
            let req = hyper::Request::builder().method("GET").uri(current.clone())
                .header("user-agent", agent).body(http_body_util::Full::new(bytes::Bytes::new())).map_err(|e| format!("{e}"))?;
            let resp = crate::net::client().request(req).await.map_err(|e| format!("{url}: {e}"))?;
            let status = resp.status().as_u16();
            if matches!(status, 301 | 302 | 303 | 307 | 308) {
                if let Some(loc) = resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                    current = crate::studio_key::resolve_location(&current, loc).map_err(|e| format!("{url}: bad redirect"))?;
                    let _ = http_body_util::BodyExt::collect(resp.into_body()).await;
                    continue;
                }
            }
            if status != 200 {
                return Err(format!("{url}: HTTP {status}"));
            }
            let size = resp.headers().get("content-length").and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<u64>().ok());
            if let Some(n) = size {
                if n > max_bytes {
                    return Err(format!("{url}: {n} bytes is past the {max_bytes} byte cap — refusing"));
                }
            }
            let body = http_body_util::BodyExt::collect(resp.into_body()).await.map_err(|e| format!("{url}: {e}"))?.to_bytes();
            if body.len() as u64 > max_bytes {
                return Err(format!("{url}: the body is past the {max_bytes} byte cap — refusing"));
            }
            return Ok(body);
        }
        Err(format!("{url}: too many redirects"))
    };
    tokio::time::timeout(std::time::Duration::from_secs(300), work).await.map_err(|_| format!("{url}: timed out"))?
}

async fn fetch_tarball(branch: &str) -> Result<PathBuf, String> {
    let url = super::TARBALL_URL.replace("{branch}", branch);
    let body = fetch_url(&url, "NeonDragons-WorldBuilder/1.0 (lore-forge)", super::TARBALL_MAX_BYTES).await?;
    let dir = std::env::temp_dir().join(format!("nd-lore-forge-{}", std::process::id()));
    std::fs::create_dir_all(&dir).map_err(|e| format!("temp dir: {e}"))?;
    let archive = dir.join(format!("{branch}.tar.gz"));
    std::fs::write(&archive, &body).map_err(|e| format!("writing the tarball: {e}"))?;
    Ok(archive)
}

/// Extracts only `worker/` and `catalog/` (the tarball prefixes everything with <repo>-<branch>/). Members are checked
/// before extraction: no `..`, no absolute paths, nothing outside the two prefixes.
fn extract_tarball(archive: &Path, dest: &Path) -> Result<usize, String> {
    let f = std::fs::File::open(archive).map_err(|e| format!("opening the tarball: {e}"))?;
    let gz = flate2::read::GzDecoder::new(f);
    let mut tar = tar::Archive::new(gz);
    let mut n = 0usize;
    for member in tar.entries().map_err(|e| format!("reading the tarball: {e}"))? {
        let mut m = member.map_err(|e| format!("reading the tarball: {e}"))?;
        let name = m.path().map_err(|e| format!("reading the tarball: {e}"))?.to_string_lossy().replace('\\', "/");
        let stripped = name.split_once('/').map(|(_, rest)| rest).unwrap_or(&name);
        if stripped.is_empty() || name.ends_with('/') {
            continue;
        }
        if !super::EXTRACT_PREFIXES.iter().any(|p| stripped.starts_with(p)) {
            continue;
        }
        if stripped.starts_with('/') || stripped.split('/').any(|part| part == "..") {
            return Err(format!("the tarball holds an unsafe path: {name}"));
        }
        let target = dest.join(stripped);
        let _ = std::fs::create_dir_all(target.parent().unwrap_or(dest));
        let mut out = std::fs::File::create(&target).map_err(|e| format!("writing {stripped}: {e}"))?;
        std::io::copy(&mut m, &mut out).map_err(|e| format!("writing {stripped}: {e}"))?;
        n += 1;
    }
    Ok(n)
}

fn copy_dir(from: &Path, to: &Path) -> Result<(), String> {
    std::fs::create_dir_all(to).map_err(|e| format!("creating {}: {e}", to.display()))?;
    for entry in std::fs::read_dir(from).map_err(|e| format!("reading {}: {e}", from.display()))? {
        let entry = entry.map_err(|e| format!("reading {}: {e}", from.display()))?;
        let target = to.join(entry.file_name());
        if entry.file_type().map_err(|e| format!("{e}"))?.is_dir() {
            copy_dir(&entry.path(), &target)?;
        } else {
            std::fs::copy(entry.path(), &target).map_err(|e| format!("copying {}: {e}", entry.path().display()))?;
        }
    }
    Ok(())
}

fn walk_count(dir: &Path) -> usize {
    std::fs::read_dir(dir).map(|rd| rd.filter_map(|e| e.ok()).map(|e| if e.file_type().map(|t| t.is_dir()).unwrap_or(false) { walk_count(&e.path()) } else { 1 }).sum()).unwrap_or(0)
}

// ----------------------------------------------------------------------------------------------------------------- setup-env

/// The pinned `uv` release's archive name for this platform: (file name, is_zip) — the python-free image is Linux
/// x86_64, but a dev checkout may be Windows or macOS or ARM.
fn uv_archive() -> Option<(&'static str, bool)> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Some(("uv-x86_64-unknown-linux-gnu.tar.gz", false)),
        ("linux", "aarch64") => Some(("uv-aarch64-unknown-linux-gnu.tar.gz", false)),
        ("windows", "x86_64") => Some(("uv-x86_64-pc-windows-msvc.zip", true)),
        ("macos", "x86_64") => Some(("uv-x86_64-apple-darwin.tar.gz", false)),
        ("macos", "aarch64") => Some(("uv-aarch64-apple-darwin.tar.gz", false)),
        _ => None,
    }
}

fn uv_binary(base: &Path) -> PathBuf {
    let exe = if cfg!(windows) { "uv.exe" } else { "uv" };
    base.join(exe)
}

/// A prepared uv (ND_LORE_FORGE_UV_DIR) or the pinned release downloaded over TLS. The archive is verified by
/// actually running `uv --version` before anything else uses it.
async fn ensure_uv(base: &Path, kind: &'static str, job_id: i64) -> Result<PathBuf, String> {
    if let Ok(dir) = std::env::var("ND_LORE_FORGE_UV_DIR") {
        let p = PathBuf::from(dir.trim()).join(if cfg!(windows) { "uv.exe" } else { "uv" });
        if p.is_file() {
            return Ok(p);
        }
        return Err(format!("ND_LORE_FORGE_UV_DIR has no uv binary: {}", p.display()));
    }
    let (file, is_zip) = uv_archive().ok_or("This platform has no pinned uv build — install uv yourself and set ND_LORE_FORGE_UV_DIR.")?;
    let url = format!("{}/{}/{}", super::UV_RELEASE_BASE, super::UV_VERSION, file);
    let body = fetch_url(&url, "NeonDragons-WorldBuilder/1.0 (lore-forge)", 64 * 1024 * 1024).await?;
    crate::runners::update(kind, job_id, |e| { e.insert("stage".into(), json!("unpacking uv")); });
    let dest = uv_binary(base);
    let unpack_dest = dest.clone();
    let unpack = tokio::task::spawn_blocking(move || -> Result<(), String> {
        let dest = unpack_dest;
        let cursor = std::io::Cursor::new(body);
        if is_zip {
            let mut za = zip::ZipArchive::new(cursor).map_err(|e| format!("the uv archive is not a zip: {e}"))?;
            for i in 0..za.len() {
                let mut f = za.by_index(i).map_err(|e| format!("the uv archive: {e}"))?;
                let name = f.name().to_string();
                let stripped = name.split_once('/').map(|(_, rest)| rest).unwrap_or(&name);
                if stripped != "uv.exe" && stripped != "uv" {
                    continue;
                }
                let mut out = std::fs::File::create(&dest).map_err(|e| format!("writing uv: {e}"))?;
                std::io::copy(&mut f, &mut out).map_err(|e| format!("writing uv: {e}"))?;
            }
        } else {
            let gz = flate2::read::GzDecoder::new(cursor);
            let mut tar = tar::Archive::new(gz);
            for m in tar.entries().map_err(|e| format!("the uv archive: {e}"))? {
                let mut m = m.map_err(|e| format!("the uv archive: {e}"))?;
                let name = m.path().map_err(|e| format!("the uv archive: {e}"))?.to_string_lossy().replace('\\', "/");
                let stripped = name.split_once('/').map(|(_, rest)| rest).unwrap_or(&name);
                if stripped != "uv" && stripped != "uv.exe" {
                    continue;
                }
                let mut out = std::fs::File::create(&dest).map_err(|e| format!("writing uv: {e}"))?;
                std::io::copy(&mut m, &mut out).map_err(|e| format!("writing uv: {e}"))?;
            }
        }
        dest.is_file().then_some(()).ok_or_else(|| "the uv archive had no uv binary".to_string())
    }).await.map_err(|e| format!("the unpack task died: {e}"))??;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o755));
    }
    let probe = vec![dest.to_string_lossy().into_owned(), "--version".into()];
    stream_cmd(&probe, None, &[], kind, job_id, base, None, "checking uv").await?;
    Ok(dest)
}

pub fn start_setup_env(cfg: &Arc<crate::config::Config>) -> Result<i64, String> {
    if running(K_ENV) {
        return Err("The environment setup is already running.".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    if !super::installed(&base) {
        return Err("Install Lore Forge first.".into());
    }
    Ok(start(K_ENV, "Lore Forge: training environment", cfg, json!({"status": "running", "stage": "uv", "error": "", "log": []}), Arc::new(move |c, id| {
        Box::pin(async move { setup_env_run(c, id).await })
    })))
}

async fn setup_env_run(cfg: Arc<crate::config::Config>, id: i64) {
    let Some(base) = lf_dir(&cfg) else { return fail(K_ENV, id, "No database folder.") };
    set_state(&base, json!({"env": {"status": "running"}}));
    let uv = match ensure_uv(&base, K_ENV, id).await {
        Ok(uv) => uv,
        Err(e) => {
            set_state(&base, json!({"env": {"status": "error", "error": e}}));
            return fail(K_ENV, id, &e);
        }
    };
    let uv_s = uv.to_string_lossy().into_owned();
    let pyver = std::env::var("ND_LORE_FORGE_PYTHON_VERSION").unwrap_or_else(|_| super::PYTHON_VERSION.to_string());
    let env = super::env_dir(&base);
    let _ = std::fs::remove_dir_all(&env);
    let argv = vec![uv_s.clone(), "venv".into(), "--python".into(), pyver, env.to_string_lossy().into_owned()];
    if let Err(e) = stream_cmd(&argv, None, &[], K_ENV, id, &base, None, "creating the venv").await {
        set_state(&base, json!({"env": {"status": "error", "error": e}}));
        return fail(K_ENV, id, &e);
    }
    if !super::env_ready(&base) {
        let e = "the venv has no python — uv's output may say why (see the log)";
        set_state(&base, json!({"env": {"status": "error", "error": e}}));
        return fail(K_ENV, id, e);
    }
    let py = super::env_python(&base).unwrap();
    // PyTorch: the default wheels bundle CUDA where a GPU answers nvidia-smi; otherwise the CPU build
    let mut torch = vec![uv_s.clone(), "pip".into(), "install".into(), "--python".into(), py.to_string_lossy().into_owned()];
    if std::env::var_os("ND_LORE_FORGE_FORCE_CPU").is_none() && nvidia_smi_ok().await {
        torch.push("torch".into());
    } else {
        torch.push("--index-url".into());
        torch.push(std::env::var("ND_LORE_FORGE_TORCH_INDEX_URL").unwrap_or_else(|_| "https://download.pytorch.org/whl/cpu".into()));
        torch.push("torch".into());
    }
    if let Err(e) = stream_cmd(&torch, None, &[], K_ENV, id, &base, None, "installing PyTorch").await {
        set_state(&base, json!({"env": {"status": "error", "error": e}}));
        return fail(K_ENV, id, &e);
    }
    let mut rest = vec![uv_s, "pip".into(), "install".into(), "--python".into(), py.to_string_lossy().into_owned()];
    rest.extend(super::ENV_PACKAGES.iter().map(|p| p.to_string()));
    if let Err(e) = stream_cmd(&rest, None, &[], K_ENV, id, &base, None, "installing the training packages").await {
        set_state(&base, json!({"env": {"status": "error", "error": e}}));
        return fail(K_ENV, id, &e);
    }
    set_state(&base, json!({"env": {"status": "ready", "python": py.to_string_lossy(), "finished": now_secs(), "error": Value::Null}}));
    ok(K_ENV, id, json!({"stage": "", "python": py.to_string_lossy()}));
}

async fn nvidia_smi_ok() -> bool {
    tokio::process::Command::new("nvidia-smi").stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status().await
        .map(|s| s.success()).unwrap_or(false)
}

// ----------------------------------------------------------------------------------------------------------------- train / export / install-model

/// What a train/export/model run carries between the POST handler and the runner (and a restart).
#[derive(Clone)]
pub struct RunParams {
    pub run_id: String,
    pub world_id: i64,
    pub world_name: String,
    pub model: Value,
    pub preset_id: String,
    pub method: String,
}

/// Marks a run interrupted when its future dies mid-run (the abort on the Background Jobs page drops it)
struct RunGuard(PathBuf, String);
impl Drop for RunGuard {
    fn drop(&mut self) {
        let st = super::read_state(&self.0);
        if st["runs"][&self.1]["status"] == "running" {
            update_run(&self.0, &self.1, json!({"status": "interrupted", "finished": now_secs(), "text": "The gateway was stopped or the job was cancelled."}));
        }
    }
}

pub fn start_train(cfg: &Arc<crate::config::Config>, params: RunParams) -> Result<i64, String> {
    if running(K_TRAIN) {
        return Err("A training run is already going (one GPU).".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    if !super::installed(&base) {
        return Err("Install Lore Forge first.".into());
    }
    if !super::env_ready(&base) {
        return Err("Set up the training environment first.".into());
    }
    let run = run_dir(&base, &params.run_id).ok_or("bad run id")?;
    if !run.join("job.json").is_file() {
        return Err("The run has no job.json.".into());
    }
    update_run(&base, &params.run_id, json!({"status": "running", "started": now_secs(), "text": "Starting…", "live": Value::Null}));
    let initial = json!({"status": "running", "stage": "start", "run_id": params.run_id, "error": "", "log": [], "samples": []});
    let p = params.clone();
    Ok(start(K_TRAIN, &format!("Lore Forge: train {} ({})", params.model.get("name").and_then(|n| n.as_str()).unwrap_or("model"), params.preset_id), cfg, initial, Arc::new(move |c, id| {
        let p = p.clone();
        Box::pin(async move { train_run(c, id, p).await })
    })))
}

async fn train_run(cfg: Arc<crate::config::Config>, id: i64, params: RunParams) {
    let Some(base) = lf_dir(&cfg) else { return fail(K_TRAIN, id, "No database folder.") };
    let Some(run) = run_dir(&base, &params.run_id) else { return fail(K_TRAIN, id, "bad run id") };
    let _guard = RunGuard(base.clone(), params.run_id.clone());
    let argv = match worker_args(&base) {
        Ok(mut a) => {
            a.push("job.json".into());
            a
        }
        Err(e) => {
            update_run(&base, &params.run_id, json!({"status": "error", "error": e, "finished": now_secs()}));
            return fail(K_TRAIN, id, &e);
        }
    };
    let worker_path = super::repo_dir(&base).join("worker");
    let rc = stream_cmd(&argv, Some(&run), &[("PYTHONPATH", worker_path.to_string_lossy().as_ref())], K_TRAIN, id, &base, Some(&params.run_id), "training").await;
    match rc {
        Ok(0) => {
            let summary = read_json_file(&run_out(&run).join("summary.json"));
            match summary {
                Some(s) => {
                    update_run(&base, &params.run_id, json!({"status": "done", "text": "Finished", "finished": now_secs(), "summary": s}));
                    ok(K_TRAIN, id, json!({"stage": "", "summary": s}));
                }
                None => {
                    let e = "the worker exited 0 but wrote no summary.json";
                    update_run(&base, &params.run_id, json!({"status": "error", "error": e, "finished": now_secs()}));
                    fail(K_TRAIN, id, e);
                }
            }
        }
        Ok(code) => {
            let e = format!("the trainer exited with code {code} (see the log; a STOP file means the run stopped itself and kept its checkpoint)");
            let stopped = super::stop_file(&run).is_file();
            update_run(&base, &params.run_id, json!({"status": if stopped { "stopped" } else { "error" }, "error": if stopped { Value::Null } else { json!(e) }, "text": if stopped { "Stopped (checkpoint kept)" } else { e.as_str() }, "finished": now_secs()}));
            if stopped {
                ok(K_TRAIN, id, json!({"stage": "", "stopped": true}));
            } else {
                fail(K_TRAIN, id, &e);
            }
        }
        Err(e) => {
            update_run(&base, &params.run_id, json!({"status": "error", "error": e, "finished": now_secs()}));
            fail(K_TRAIN, id, &e);
        }
    }
}

pub fn start_export(cfg: &Arc<crate::config::Config>, run_id: &str, quant: &str) -> Result<(i64, String), String> {
    if running(K_EXPORT) {
        return Err("An export is already running.".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    let run = run_dir(&base, run_id).ok_or("bad run id")?;
    if !super::installed(&base) || !super::env_ready(&base) {
        return Err("Install Lore Forge and set up the training environment first.".into());
    }
    let st = super::read_state(&base);
    let meta = st.pointer(&format!("/runs/{run_id}")).ok_or("That run does not exist.")?.clone();
    if meta.get("status").and_then(|s| s.as_str()) != Some("done") {
        return Err("Export a finished run (train it first).".into());
    }
    let method = meta.get("method").and_then(|m| m.as_str()).unwrap_or(super::DEFAULT_METHOD).to_string();
    let model = meta.get("model").cloned().unwrap_or(Value::Null);
    let job = super::rows::export_job(&run, &method, &model, quant);
    std::fs::write(run.join("export.json"), serde_json::to_vec(&job).map_err(|e| format!("{e}"))?).map_err(|e| format!("writing export.json: {e}"))?;
    let params = RunParams {
        run_id: run_id.to_string(),
        world_id: meta.get("world_id").and_then(|w| w.as_i64()).unwrap_or(0),
        world_name: meta.get("world").and_then(|w| w.as_str()).unwrap_or("").to_string(),
        model,
        preset_id: meta.get("preset").and_then(|p| p.as_str()).unwrap_or(DEFAULT_PRESET).to_string(),
        method,
    };
    update_run(&base, run_id, json!({"export_status": "running", "export_quant": quant}));
    let initial = json!({"status": "running", "stage": "merge", "run_id": run_id, "error": "", "log": []});
    let p = params.clone();
    let id = start(K_EXPORT, "Lore Forge: GGUF export", cfg, initial, Arc::new(move |c, id| {
        let p = p.clone();
        Box::pin(async move { export_run(c, id, p).await })
    }));
    Ok((id, run_id.to_string()))
}

async fn export_run(cfg: Arc<crate::config::Config>, id: i64, params: RunParams) {
    let Some(base) = lf_dir(&cfg) else { return fail(K_EXPORT, id, "No database folder.") };
    let Some(run) = run_dir(&base, &params.run_id) else { return fail(K_EXPORT, id, "bad run id") };
    let _guard = RunGuard(base.clone(), params.run_id.clone());
    let argv = match worker_args(&base) {
        Ok(mut a) => {
            a.push("export.json".into());
            a
        }
        Err(e) => {
            update_run(&base, &params.run_id, json!({"export_status": "error", "export_error": e}));
            return fail(K_EXPORT, id, &e);
        }
    };
    let worker_path = super::repo_dir(&base).join("worker");
    let rc = stream_cmd(&argv, Some(&run), &[("PYTHONPATH", worker_path.to_string_lossy().as_ref())], K_EXPORT, id, &base, Some(&params.run_id), "exporting").await;
    match rc {
        Ok(0) => {
            let export = read_json_file(&run_out(&run).join("export.json"));
            match export {
                Some(e) => {
                    update_run(&base, &params.run_id, json!({"export_status": "done", "export": e, "export_finished": now_secs()}));
                    ok(K_EXPORT, id, json!({"stage": "", "export": e}));
                }
                None => {
                    let e = "the exporter exited 0 but wrote no export.json";
                    update_run(&base, &params.run_id, json!({"export_status": "error", "export_error": e}));
                    fail(K_EXPORT, id, e);
                }
            }
        }
        Ok(code) => {
            let e = format!("the exporter exited with code {code} (see the log)");
            update_run(&base, &params.run_id, json!({"export_status": "error", "export_error": e}));
            fail(K_EXPORT, id, &e);
        }
        Err(e) => {
            update_run(&base, &params.run_id, json!({"export_status": "error", "export_error": e}));
            fail(K_EXPORT, id, &e);
        }
    }
}

pub fn start_install_model(cfg: &Arc<crate::config::Config>, run_id: &str, name: &str, targets: &[&str]) -> Result<i64, String> {
    if running(K_MODEL) {
        return Err("A model install is already running.".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    let Some(run) = run_dir(&base, run_id) else { return Err("bad run id".into()) };
    let gguf = exported_gguf(&run).ok_or("Export a GGUF first (no export.json / .gguf in the run's out folder).")?;
    let initial = json!({"status": "running", "stage": "digest", "run_id": run_id, "model_name": name, "error": "", "log": []});
    let name = name.to_string();
    let targets: Vec<String> = targets.to_vec().into_iter().map(String::from).collect();
    let run_id = run_id.to_string();
    Ok(start(K_MODEL, &format!("Lore Forge: install {name}"), cfg, initial, Arc::new(move |c, id| {
        let (gguf, name, targets, run_id) = (gguf.clone(), name.clone(), targets.clone(), run_id.clone());
        Box::pin(async move { install_model_run(c, id, &run_id, &gguf, &name, &targets).await })
    })))
}

/// The run's best GGUF: export.json's quant map (the run's quant first), else the out folder's only .gguf
fn exported_gguf(run: &Path) -> Option<PathBuf> {
    let out = run_out(run);
    if let Some(export) = read_json_file(&out.join("export.json")) {
        if let Some(ggufs) = export.get("ggufs").and_then(|g| g.as_object()) {
            for quant in [super::DEFAULT_QUANT] {
                if let Some(f) = ggufs.get(quant).and_then(|f| f.as_str()) {
                    let p = out.join(f);
                    if p.is_file() {
                        return Some(p);
                    }
                }
            }
            if let Some((_, f)) = ggufs.into_iter().find(|(_, f)| f.as_str().is_some_and(|s| out.join(s).is_file())) {
                return Some(out.join(f.as_str().unwrap_or("")));
            }
        }
        if let Some(f) = export.get("gguf").and_then(|g| g.as_str()) {
            let p = out.join(f);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    std::fs::read_dir(&out).ok()?.flatten().map(|e| e.path()).find(|p| p.extension().and_then(|e| e.to_str()) == Some("gguf"))
}

async fn install_model_run(cfg: Arc<crate::config::Config>, id: i64, run_id: &str, gguf: &Path, name: &str, targets: &[String]) {
    let Some(base) = lf_dir(&cfg) else { return fail(K_MODEL, id, "No database folder.") };
    let mut results = Map::new();
    for target in targets {
        let r = match target.as_str() {
            "ollama" => push_to_ollama(&cfg, gguf, name, K_MODEL, id).await,
            "studio" => copy_to_studio(gguf, name).await,
            other => Err(format!("unknown target {other}")),
        };
        match r {
            Ok(v) => results.insert(target.clone(), v),
            Err(e) => results.insert(target.clone(), json!({"ok": false, "error": e})),
        };
    }
    let any_ok = results.values().any(|v| v.get("ok").and_then(|o| o.as_bool()).unwrap_or(false));
    let all = Value::Object(results);
    update_run(&base, run_id, json!({"model_name": name, "installed": all, "installed_at": now_secs()}));
    if any_ok {
        ok(K_MODEL, id, json!({"stage": "", "installed": all}));
    } else {
        fail(K_MODEL, id, &all.to_string());
    }
}

/// Ollama's import: sha256 the file, POST it to /api/blobs/{digest}, then /api/create names the model — the same two
/// calls Python's import_local_gguf_model makes through the ollama client (verified there against the client's source).
async fn push_to_ollama(cfg: &Arc<crate::config::Config>, gguf: &Path, name: &str, kind: &'static str, job_id: i64) -> Result<Value, String> {
    let url_base = ollama_url(cfg).ok_or("No Ollama address configured (OLLAMA_URL or Settings).")?;
    let gguf_owned = gguf.to_path_buf();
    let (digest, size) = tokio::task::spawn_blocking(move || -> Result<(String, u64), String> {
        let mut file = std::fs::File::open(&gguf_owned).map_err(|e| format!("opening the GGUF: {e}"))?;
        let mut h = Sha256::new();
        let mut n = 0u64;
        let mut buf = vec![0u8; 1 << 20];
        loop {
            let got = std::io::Read::read(&mut file, &mut buf).map_err(|e| format!("reading the GGUF: {e}"))?;
            if got == 0 {
                break;
            }
            h.update(&buf[..got]);
            n += got as u64;
        }
        Ok((format!("{:x}", h.finalize()), n))
    }).await.map_err(|e| format!("the digest task died: {e}"))??;
    crate::runners::update(kind, job_id, |e| {
        e.insert("stage".into(), json!("pushing the blob to Ollama"));
        e.insert("gguf_bytes".into(), json!(size));
    });
    // the blob POST streams the whole file, one frame at a time off the tokio File
    let file = tokio::fs::File::open(gguf).await.map_err(|e| format!("opening the GGUF: {e}"))?;
    let stream = tokio_util::io::ReaderStream::new(file).map(|item| item.map(hyper::body::Frame::data));
    let body = http_body_util::StreamBody::new(stream);
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build(crate::net::connector(None));
    let req = hyper::Request::builder().method("PUT")
        .uri(format!("{url_base}/api/blobs/{digest}"))
        .header("content-type", "application/octet-stream")
        .header("content-length", size.to_string())
        .body(body).map_err(|e| format!("{e}"))?;
    let resp = tokio::time::timeout(std::time::Duration::from_secs(3600), client.request(req)).await
        .map_err(|_| "the blob push timed out (an hour)".to_string())?
        .map_err(|e| format!("the blob push failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("Ollama refused the blob: HTTP {}", resp.status()));
    }
    let _ = http_body_util::BodyExt::collect(resp.into_body()).await;
    let create = json!({"model": name, "files": {gguf.file_name().map(|f| f.to_string_lossy()).unwrap_or_default(): digest}});
    let req = hyper::Request::builder().method("POST")
        .uri(format!("{url_base}/api/create"))
        .header("content-type", "application/json")
        .body(http_body_util::Full::new(bytes::Bytes::from(create.to_string()))).map_err(|e| format!("{e}"))?;
    let resp = tokio::time::timeout(std::time::Duration::from_secs(600), crate::net::client().request(req)).await
        .map_err(|_| "the model create timed out".to_string())?
        .map_err(|e| format!("the model create failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("Ollama refused the model: HTTP {}", resp.status()));
    }
    let _ = http_body_util::BodyExt::collect(resp.into_body()).await;
    Ok(json!({"ok": true, "model": name, "digest": digest, "bytes": size}))
}

/// The legacy Ollama backend's address: Settings' ollama_url, else OLLAMA_URL, else the compose default
fn ollama_url(cfg: &Arc<crate::config::Config>) -> Option<String> {
    if let Some(db) = crate::writes::open(cfg) {
        if let Ok(saved) = db.query_row("SELECT ollama_url FROM app_settings ORDER BY id LIMIT 1", [], |r| r.get::<_, Option<String>>(0)) {
            if let Some(u) = saved.filter(|u| !u.trim().is_empty()) {
                return Some(u.trim_end_matches('/').to_string());
            }
        }
    }
    let env = std::env::var("OLLAMA_URL").unwrap_or_default();
    (!env.trim().is_empty()).then(|| env.trim_end_matches('/').to_string()).or_else(|| Some("http://127.0.0.1:11434".into()))
}

/// The Studio hub dir (ND_STUDIO_HUB_DIR): the GGUF is copied in under lore-forge/, where the hub's cache listing
/// picks GGUFs up. Only answered when the env names a folder this process can actually see — never guessed.
async fn copy_to_studio(gguf: &Path, name: &str) -> Result<Value, String> {
    let dir = std::env::var("ND_STUDIO_HUB_DIR").unwrap_or_default();
    if dir.trim().is_empty() {
        return Err("ND_STUDIO_HUB_DIR is not set, so this process cannot see the Studio hub's folder — copy the GGUF by hand (the path is on the page).".into());
    }
    let dir = PathBuf::from(dir.trim());
    let target_dir = dir.join("lore-forge");
    let file_name = format!("{}.gguf", name.replace(['/', '\\', ':'], "-"));
    let target = target_dir.join(&file_name);
    let gguf_owned = gguf.to_path_buf();
    let target_in = target.clone();
    let size = tokio::task::spawn_blocking(move || -> Result<u64, String> {
        std::fs::create_dir_all(&target_dir).map_err(|e| format!("creating {}: {e}", target_dir.display()))?;
        std::fs::copy(&gguf_owned, &target_in).map_err(|e| format!("copying into {}: {e}", target_dir.display()))
    }).await.map_err(|e| format!("the copy task died: {e}"))??;
    Ok(json!({"ok": true, "path": target.to_string_lossy(), "bytes": size, "note": "copied into the Studio hub dir - the hub lists it after its next scan"}))
}

fn read_json_file(path: &Path) -> Option<Value> {
    let bytes = std::fs::read(path).ok()?;
    serde_json::from_slice(&bytes).ok()
}

// ----------------------------------------------------------------------------------------------------------------- page-facing

/// The jobs part of the page's status payload: each kind's current (running, else last recorded) entry.
pub fn jobs_view(base: &Path) -> Value {
    let mut out = Map::new();
    for (kind, short) in PAGE_KINDS {
        let entry = current_entry(base, kind).map(|mut e| {
            if let Some(o) = e.as_object_mut() {
                o.remove("log");
            }
            e
        }).unwrap_or(Value::Null);
        out.insert(short.to_string(), entry);
    }
    Value::Object(out)
}

/// The graceful stop: the worker's own halting switch — it saves a checkpoint and exits with STOP in place.
pub fn stop_run(cfg: &crate::config::Config, run_id: &str) -> Result<(), String> {
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    let run = run_dir(&base, run_id).ok_or("bad run id")?;
    if !run.is_dir() {
        return Err("That run does not exist.".into());
    }
    super::write_stop(&run);
    update_run(&base, run_id, json!({"text": "Stopping (checkpoint kept)…"}));
    Ok(())
}

pub fn start_uninstall(cfg: &crate::config::Config) -> Result<(), String> {
    if anything_running() {
        return Err("Stop the running Lore Forge job first (the Background Jobs page can cancel it).".into());
    }
    let base = lf_dir(cfg).ok_or("No database folder.")?;
    let _ = std::fs::remove_dir_all(&base);
    Ok(())
}
