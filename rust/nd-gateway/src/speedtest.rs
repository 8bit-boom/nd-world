//! The System Monitor's speed report: a self-test of the whole app that ends in a Markdown file the GM can download.
//!
//! `POST /api/system/speed-test {"rounds": 1..10}` starts a run (one at a time, GM only). The run asks the gateway itself, over HTTP on its own port and with the GM's own
//! session cookie, for the pages and read-only APIs a person uses (GET only: nothing is written, nothing is deleted), `rounds` times each, and times every answer to its
//! last byte. It then times the database (a count, a list, a search), the disk (writing and reading 16 MB), Unsloth Studio's answer, and how many requests a second the
//! front pages sustain with four at a time. What it learns is written to `<data folder>/speed-reports/<YYYYMMDD-HHMMSS>.md` (the newest 20 are kept).
//!
//! `GET /api/system/speed-test`: the progress and the list of reports. `GET /api/system/speed-test/<id>.md`: one report as a download.
//! `POST /api/system/speed-test/<id>/delete`: remove one.
use std::fmt::Write as _;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use http_body_util::BodyExt;
use serde_json::{json, Value};

use crate::config::Config;

const KEEP: usize = 20;
const REQUEST_LIMIT: Duration = Duration::from_secs(60);

#[derive(Default)]
struct Progress {
    running: bool,
    done: usize,
    total: usize,
    current: String,
    error: Option<String>,
    latest: Option<String>,
}

fn progress() -> &'static Mutex<Progress> {
    static P: OnceLock<Mutex<Progress>> = OnceLock::new();
    P.get_or_init(Default::default)
}

fn set(f: impl FnOnce(&mut Progress)) {
    f(&mut progress().lock().unwrap_or_else(|e| e.into_inner()));
}

fn reports_dir(cfg: &Config) -> Option<PathBuf> {
    Some(cfg.db_path.parent()?.join("speed-reports"))
}

/// A report id is a time stamp and nothing else, so it can never name another file
fn valid_id(id: &str) -> bool {
    let b = id.as_bytes();
    b.len() == 15 && b[8] == b'-' && b.iter().enumerate().all(|(i, c)| i == 8 || c.is_ascii_digit())
}

fn json_reply(status: StatusCode, v: &Value) -> Option<Response<Body>> {
    let mut r = crate::edits::json_response(v)?;
    *r.status_mut() = status;
    r.headers_mut().insert("cache-control", axum::http::HeaderValue::from_static("no-store"));
    Some(r)
}

/// The routes under /api/system/speed-test. None: not one of them (or not a GM: the caller falls through to the usual refusal).
pub async fn route(cfg: Arc<Config>, method: Method, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let path = uri.path().to_string();
    let rest = path.strip_prefix("/api/system/speed-test")?;
    let (c, u, h) = (cfg.clone(), uri.clone(), headers.clone());
    let is_gm = tokio::task::spawn_blocking(move || {
        let db = crate::writes::open(&c)?;
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
        Some(crate::layout::build(&db, &c, "GET", &u, &h, now)?.is_gm)
    })
    .await
    .ok()
    .flatten()
    .unwrap_or(false);
    if !is_gm {
        return Some(crate::idpages::refusal(403));
    }
    let dir = reports_dir(&cfg)?;
    match (method.clone(), rest) {
        (Method::GET, "") => status_reply(&dir),
        (Method::POST, "") => {
            let rounds = serde_json::from_slice::<Value>(&body).ok().and_then(|v| v.get("rounds").and_then(|r| r.as_i64())).unwrap_or(5).clamp(1, 10) as usize;
            let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            let started = {
                let mut p = progress().lock().unwrap_or_else(|e| e.into_inner());
                if p.running {
                    false
                } else {
                    *p = Progress { running: true, current: "starting".into(), ..Default::default() };
                    true
                }
            };
            if !started {
                return json_reply(StatusCode::CONFLICT, &json!({"detail": "A speed test is already running."}));
            }
            tokio::spawn(async move {
                match run(cfg, cookie, rounds).await {
                    Ok(id) => set(|p| { p.running = false; p.latest = Some(id); p.current = "done".into(); }),
                    Err(e) => set(|p| { p.running = false; p.error = Some(e); p.current = "failed".into(); }),
                }
            });
            json_reply(StatusCode::ACCEPTED, &json!({"started": true, "rounds": rounds}))
        }
        (Method::GET, r) if r.starts_with('/') && r.ends_with(".md") => {
            let id = &r[1..r.len() - 3];
            if !valid_id(id) {
                return Some(crate::idpages::refusal(404));
            }
            let Ok(text) = std::fs::read(dir.join(format!("{id}.md"))) else { return Some(crate::idpages::refusal(404)) };
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "text/markdown; charset=utf-8")
                .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"nd-world-speed-{id}.md\""))
                .header("cache-control", "no-store")
                .header("x-nd-served-by", "gateway")
                .body(Body::from(text))
                .ok()
        }
        (Method::POST, r) if r.starts_with('/') && r.ends_with("/delete") => {
            let id = &r[1..r.len() - 7];
            if !valid_id(id) || std::fs::remove_file(dir.join(format!("{id}.md"))).is_err() {
                return Some(crate::idpages::refusal(404));
            }
            json_reply(StatusCode::OK, &json!({"ok": true}))
        }
        _ => None,
    }
}

/// (id, bytes) of the stored reports, newest first
fn list_reports(dir: &std::path::Path) -> Vec<(String, u64)> {
    let mut out: Vec<(String, u64)> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter_map(|e| {
                    let name = e.file_name().into_string().ok()?;
                    let id = name.strip_suffix(".md")?.to_string();
                    valid_id(&id).then(|| (id, e.metadata().map(|m| m.len()).unwrap_or(0)))
                })
                .collect()
        })
        .unwrap_or_default();
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out
}

fn status_reply(dir: &std::path::Path) -> Option<Response<Body>> {
    let p = progress().lock().unwrap_or_else(|e| e.into_inner());
    let reports: Vec<Value> = list_reports(dir).into_iter().map(|(id, size)| json!({"id": id, "size": size})).collect();
    json_reply(StatusCode::OK, &json!({"running": p.running, "done": p.done, "total": p.total, "current": p.current, "error": p.error, "latest": p.latest, "reports": reports}))
}

// ── the measurements ─────────────────────────────────────────────────────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
struct Sample {
    ms: f64,
    status: u16,
    bytes: usize,
}

struct Stats {
    runs: usize,
    first: f64,
    min: f64,
    median: f64,
    p95: f64,
    max: f64,
    status: u16,
    kib: f64,
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 * p).ceil() as usize).saturating_sub(1).min(sorted.len() - 1);
    sorted[idx]
}

fn stats(samples: &[Sample]) -> Stats {
    let mut ms: Vec<f64> = samples.iter().map(|s| s.ms).collect();
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let worst = samples.iter().map(|s| s.status).max().unwrap_or(0);
    let status = samples.iter().find(|s| s.status >= 400).map(|s| s.status).unwrap_or(worst);
    Stats {
        runs: samples.len(),
        first: samples.first().map(|s| s.ms).unwrap_or(0.0),
        min: ms.first().copied().unwrap_or(0.0),
        median: percentile(&ms, 0.5),
        p95: percentile(&ms, 0.95),
        max: ms.last().copied().unwrap_or(0.0),
        status,
        kib: samples.last().map(|s| s.bytes as f64 / 1024.0).unwrap_or(0.0),
    }
}

/// How a figure reads: against the app's own slow limit (ND_SLOW_MS, 800 ms) and two fixed marks
fn rating(ms: f64) -> &'static str {
    let slow = crate::perf::slow_ms();
    if ms >= slow * 2.0 { "very slow" } else if ms >= slow { "slow" } else if ms >= 300.0 { "ok" } else { "fast" }
}

fn f1(v: f64) -> String {
    format!("{v:.1}")
}

struct Client {
    port: String,
    cookie: String,
}

impl Client {
    /// One GET of `path` on the gateway's own port, timed to the last byte of the body
    async fn get(&self, path: &str) -> Sample {
        let started = Instant::now();
        let req = hyper::Request::builder()
            .method("GET")
            .uri(format!("http://127.0.0.1:{}{}", self.port, path))
            .header(header::COOKIE, self.cookie.as_str())
            .header("x-nd-speedtest", "1")
            .header(header::ACCEPT_ENCODING, "identity")
            .body(http_body_util::Full::new(bytes::Bytes::new()));
        let Ok(req) = req else { return Sample { ms: 0.0, status: 0, bytes: 0 } };
        let call = async {
            let resp = crate::net::client().request(req).await.ok()?;
            let status = resp.status().as_u16();
            let body = resp.into_body().collect().await.ok()?.to_bytes();
            Some((status, body.len()))
        };
        let (status, bytes) = tokio::time::timeout(REQUEST_LIMIT, call).await.ok().flatten().unwrap_or((0, 0));
        Sample { ms: started.elapsed().as_secs_f64() * 1000.0, status, bytes }
    }
}

/// What the world offers to ask for: its first kinds and entity, plus the first style sheet and script of the static folder
struct Found {
    kinds: Vec<String>,
    entity: Option<i64>,
    css: Option<String>,
    js: Option<String>,
}

fn first_file(dir: &std::path::Path, ext: &str) -> Option<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir).ok()?.filter_map(|e| e.ok()).filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| n.ends_with(ext) && !n.ends_with(&format!(".min{ext}")) && n.is_ascii()).collect();
    names.sort();
    names.into_iter().next()
}

fn discover(cfg: &Config, cookie: &str) -> Found {
    let slug = cookie.split(';').filter_map(|p| p.trim().strip_prefix("active_world=")).next().unwrap_or("").to_string();
    let mut found = Found { kinds: vec![], entity: None, css: first_file(&cfg.static_dir.join("css"), ".css"), js: first_file(&cfg.static_dir.join("js"), ".js") };
    let Some(db) = crate::writes::open(cfg) else { return found };
    let wid: Option<i64> = db.query_row("SELECT id FROM worlds WHERE slug = ?1", [&slug], |r| r.get(0)).ok().or_else(|| db.query_row("SELECT id FROM worlds ORDER BY id LIMIT 1", [], |r| r.get(0)).ok());
    let Some(wid) = wid else { return found };
    if let Ok(mut st) = db.prepare("SELECT kind FROM entities WHERE world_id = ?1 GROUP BY kind ORDER BY COUNT(*) DESC LIMIT 3") {
        if let Ok(rows) = st.query_map([wid], |r| r.get::<_, String>(0)) {
            found.kinds = rows.filter_map(|r| r.ok()).filter(|k| !k.is_empty() && k.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')).collect();
        }
    }
    found.entity = db.query_row("SELECT id FROM entities WHERE world_id = ?1 ORDER BY id LIMIT 1", [wid], |r| r.get(0)).ok();
    found
}

/// (label, path) of everything asked for, GET only
fn targets(found: &Found) -> Vec<(String, String)> {
    let mut t: Vec<(String, String)> = vec![];
    for p in ["/", "/maps", "/calendar", "/sessions", "/rules", "/search?q=a", "/dice", "/characters", "/quests", "/parties", "/boards", "/facts", "/ai", "/settings", "/worlds",
              "/images", "/audio", "/pages", "/system"] {
        t.push((p.to_string(), p.to_string()));
    }
    for k in &found.kinds {
        t.push((format!("/kind/{k}"), format!("/kind/{k}")));
    }
    if let Some(id) = found.entity {
        t.push(("/entity/{id}".into(), format!("/entity/{id}")));
    }
    for p in ["/api/me", "/api/spotlight?q=a", "/api/ai/models", "/api/ai/sessions", "/api/ai/projects", "/api/system/stats", "/api/perf/summary", "/api/dice/history", "/api/quests/board"] {
        t.push((p.to_string(), p.to_string()));
    }
    if let Some(c) = &found.css {
        t.push((format!("/static/css/{c}"), format!("/static/css/{c}")));
    }
    if let Some(j) = &found.js {
        t.push((format!("/static/js/{j}"), format!("/static/js/{j}")));
    }
    t.push(("/sw.js".into(), "/sw.js".into()));
    t
}

/// N at a time, `each` requests per worker: (requests per second, 95th percentile ms, errors)
async fn load(client: &Arc<Client>, path: &str, workers: usize, each: usize) -> (f64, f64, usize) {
    let started = Instant::now();
    let mut tasks = vec![];
    for _ in 0..workers {
        let (c, p) = (client.clone(), path.to_string());
        tasks.push(tokio::spawn(async move {
            let mut out = vec![];
            for _ in 0..each {
                out.push(c.get(&p).await);
            }
            out
        }));
    }
    let mut all: Vec<Sample> = vec![];
    for t in tasks {
        if let Ok(v) = t.await {
            all.extend(v);
        }
    }
    let secs = started.elapsed().as_secs_f64().max(0.001);
    let errors = all.iter().filter(|s| s.status == 0 || s.status >= 500).count();
    let mut ms: Vec<f64> = all.iter().map(|s| s.ms).collect();
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    (all.len() as f64 / secs, percentile(&ms, 0.95), errors)
}

/// Times of a database statement run `n` times: (median ms, max ms)
fn db_time(db: &rusqlite::Connection, sql: &str, params: &[&dyn rusqlite::ToSql], n: usize) -> Option<(f64, f64)> {
    let mut ms = vec![];
    for _ in 0..n {
        let t = Instant::now();
        let mut st = db.prepare(sql).ok()?;
        let mut rows = st.query(params).ok()?;
        while rows.next().ok()?.is_some() {}
        ms.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    Some((percentile(&ms, 0.5), ms.last().copied().unwrap_or(0.0)))
}

fn db_section(cfg: &Config, cookie: &str) -> String {
    let mut out = String::from("## Database\n\n");
    let t = Instant::now();
    let Some(db) = crate::writes::open(cfg) else { return out + "The database could not be opened.\n\n" };
    let _ = writeln!(out, "Opening the database: {} ms. File size: {:.1} MiB.\n", f1(t.elapsed().as_secs_f64() * 1000.0),
        std::fs::metadata(&cfg.db_path).map(|m| m.len() as f64 / 1048576.0).unwrap_or(0.0));
    let slug = cookie.split(';').filter_map(|p| p.trim().strip_prefix("active_world=")).next().unwrap_or("").to_string();
    let wid: i64 = db.query_row("SELECT id FROM worlds WHERE slug = ?1", [&slug], |r| r.get(0)).ok().or_else(|| db.query_row("SELECT id FROM worlds ORDER BY id LIMIT 1", [], |r| r.get(0)).ok()).unwrap_or(0);
    let entities: i64 = db.query_row("SELECT COUNT(*) FROM entities WHERE world_id = ?1", [wid], |r| r.get(0)).unwrap_or(0);
    let _ = writeln!(out, "The active world has {entities} entities.\n");
    out.push_str("| Statement | Runs | Median ms | Worst ms |\n|---|---:|---:|---:|\n");
    let cases: [(&str, &str); 4] = [
        ("Count the entities", "SELECT COUNT(*) FROM entities WHERE world_id = ?1"),
        ("List 50 entities by name", "SELECT * FROM entities WHERE world_id = ?1 ORDER BY name LIMIT 50"),
        ("Search names for \"a\"", "SELECT id, name FROM entities WHERE world_id = ?1 AND name LIKE '%a%' LIMIT 50"),
        ("Every entity's name and kind", "SELECT id, name, kind FROM entities WHERE world_id = ?1"),
    ];
    for (label, sql) in cases {
        match db_time(&db, sql, &[&wid], 20) {
            Some((med, worst)) => { let _ = writeln!(out, "| {label} | 20 | {} | {} |", f1(med), f1(worst)); }
            None => { let _ = writeln!(out, "| {label} | – | could not run | |"); }
        }
    }
    out.push('\n');
    out
}

fn disk_section(cfg: &Config) -> String {
    let mut out = String::from("## Disk\n\n");
    let Some(dir) = cfg.db_path.parent() else { return out + "No data folder.\n\n" };
    let file = dir.join(format!("speed-test-{}.tmp", std::process::id()));
    let chunk = vec![0x5Au8; 1024 * 1024];
    let t = Instant::now();
    let wrote = (|| -> std::io::Result<()> {
        use std::io::Write;
        let mut f = std::fs::File::create(&file)?;
        for _ in 0..16 {
            f.write_all(&chunk)?;
        }
        f.sync_all()
    })();
    let write_s = t.elapsed().as_secs_f64().max(0.001);
    let t = Instant::now();
    let read = std::fs::read(&file).map(|b| b.len()).unwrap_or(0);
    let read_s = t.elapsed().as_secs_f64().max(0.001);
    let _ = std::fs::remove_file(&file);
    match wrote {
        Ok(()) => { let _ = writeln!(out, "Writing 16 MiB (flushed to disk): {} MB/s ({} ms). Reading it back: {} MB/s ({} ms; the system may have it cached).\n",
            f1(16.0 * 1.048576 / write_s), f1(write_s * 1000.0), f1(read as f64 / 1_000_000.0 / read_s), f1(read_s * 1000.0)); }
        Err(e) => { let _ = writeln!(out, "The disk test could not write in the data folder: {e}\n"); }
    }
    out
}

async fn studio_section(cfg: &Arc<Config>) -> String {
    let mut out = String::from("## Unsloth Studio\n\n");
    let c = cfg.clone();
    let backend = tokio::task::spawn_blocking(move || crate::writes::open(&c).and_then(|db| crate::ai::backend(&db))).await.ok().flatten();
    let Some(b) = backend else { return out + "No Unsloth Studio key is set, so the AI back end was not tested.\n\n" };
    let mut ms = vec![];
    let mut failed = 0;
    for _ in 0..3 {
        let Ok(uri) = format!("{}/v1/models", b.base_url()).parse::<hyper::Uri>() else { break };
        let t = Instant::now();
        match tokio::time::timeout(Duration::from_secs(15), crate::studio_key::studio_get(uri, &b.key)).await {
            Ok(Ok((status, _))) if status < 400 => ms.push(t.elapsed().as_secs_f64() * 1000.0),
            _ => failed += 1,
        }
    }
    ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    if ms.is_empty() {
        let _ = writeln!(out, "Studio did not answer `/v1/models` in 15 s ({failed} of 3 tries). It is busy (loading a model, drawing a picture) or not running; every AI page waits on it.\n");
    } else {
        let _ = writeln!(out, "`/v1/models`: median {} ms, worst {} ms over {} answers{}.\n", f1(percentile(&ms, 0.5)), f1(ms[ms.len() - 1]), ms.len(),
            if failed > 0 { format!(" ({failed} did not answer)") } else { String::new() });
    }
    out
}

fn system_section() -> String {
    let s = crate::sysstats::snapshot();
    let mut out = String::from("## The machine\n\n");
    let cpu = &s["cpu"];
    let _ = writeln!(out, "- CPU: {} ({} cores), load {} / {} / {}", cpu["model"].as_str().unwrap_or("?"), cpu["cores"], cpu["load"][0], cpu["load"][1], cpu["load"][2]);
    let mem = &s["memory"];
    if let (Some(total), Some(avail)) = (mem["total"].as_f64(), mem["available"].as_f64()) {
        let _ = writeln!(out, "- Memory: {:.0} MiB total, {:.0} MiB available", total / 1048576.0, avail / 1048576.0);
    }
    match s["gpu"]["devices"].as_array() {
        Some(d) if !d.is_empty() => for g in d {
            let _ = writeln!(out, "- GPU: {} — {} % busy, {}/{} MiB memory used", g["name"].as_str().unwrap_or("?"), g["util_percent"], g["mem_used_mib"], g["mem_total_mib"]);
        },
        _ => out.push_str("- GPU: none reported to this container\n"),
    }
    out.push('\n');
    out
}

fn traffic_section() -> String {
    let s = crate::perf::summary();
    let mut out = String::from("## Real traffic since the server started\n\nThe slowest routes among the last 1,000 requests people (and the browser) actually made.\n\n");
    let routes = s["routes"].as_array().cloned().unwrap_or_default();
    if routes.is_empty() {
        return out + "Nothing has been timed yet.\n\n";
    }
    out.push_str("| Route | Calls | Median ms | 95th ms | Worst ms | Errors |\n|---|---:|---:|---:|---:|---:|\n");
    for r in routes.iter().take(15) {
        let _ = writeln!(out, "| `{} {}` | {} | {} | {} | {} | {} |", r["method"].as_str().unwrap_or(""), r["route"].as_str().unwrap_or(""), r["calls"], f1(r["p50_ms"].as_f64().unwrap_or(0.0)),
            f1(r["p95_ms"].as_f64().unwrap_or(0.0)), f1(r["max_ms"].as_f64().unwrap_or(0.0)), r["errors"]);
    }
    out.push('\n');
    out
}

/// Run the whole test and write the report; the id of the report
async fn run(cfg: Arc<Config>, cookie: String, rounds: usize) -> Result<String, String> {
    let dir = reports_dir(&cfg).ok_or("no data folder")?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot make the reports folder: {e}"))?;
    let port = cfg.listen.rsplit(':').next().filter(|p| p.bytes().all(|b| b.is_ascii_digit()) && !p.is_empty()).unwrap_or("8000").to_string();
    let client = Arc::new(Client { port, cookie: cookie.clone() });
    let (c2, ck) = (cfg.clone(), cookie.clone());
    let found = tokio::task::spawn_blocking(move || discover(&c2, &ck)).await.map_err(|e| e.to_string())?;
    let list = targets(&found);
    set(|p| { p.total = list.len() * rounds + 5; });
    let started = Instant::now();
    let started_stamp = crate::orm::now_stamp();

    // the pages and APIs
    let mut rows: Vec<(String, Stats)> = vec![];
    for (label, path) in &list {
        set(|p| p.current = format!("GET {label}"));
        let mut samples = vec![];
        for _ in 0..rounds {
            samples.push(client.get(path).await);
            set(|p| p.done += 1);
        }
        rows.push((label.clone(), stats(&samples)));
    }

    // what four at a time can do
    set(|p| p.current = "four at a time: the home page".into());
    let home = load(&client, "/", 4, 8).await;
    set(|p| p.done += 1);
    set(|p| p.current = "four at a time: /api/me".into());
    let api = load(&client, "/api/me", 4, 25).await;
    set(|p| p.done += 1);

    set(|p| p.current = "database".into());
    let (c3, ck3) = (cfg.clone(), cookie.clone());
    let db_md = tokio::task::spawn_blocking(move || db_section(&c3, &ck3)).await.map_err(|e| e.to_string())?;
    set(|p| p.done += 1);
    set(|p| p.current = "disk".into());
    let c4 = cfg.clone();
    let disk_md = tokio::task::spawn_blocking(move || disk_section(&c4)).await.map_err(|e| e.to_string())?;
    set(|p| p.done += 1);
    set(|p| p.current = "unsloth studio".into());
    let studio_md = studio_section(&cfg).await;
    set(|p| p.done += 1);
    let machine_md = tokio::task::spawn_blocking(system_section).await.map_err(|e| e.to_string())?;
    let traffic_md = tokio::task::spawn_blocking(traffic_section).await.map_err(|e| e.to_string())?;

    // the report
    let build = std::env::var("ND_BUILD").unwrap_or_default();
    let build: String = build.trim().chars().take(7).collect();
    let mut md = String::new();
    let _ = writeln!(md, "# N&D World speed report\n");
    let _ = writeln!(md, "- Run: {} UTC, took {:.1} s", started_stamp.chars().take(19).collect::<String>(), started.elapsed().as_secs_f64());
    let _ = writeln!(md, "- Build: {} ({})", if build.is_empty() { "dev" } else { &build }, if crate::config::python_free() { "Rust, no Python" } else { "Rust gateway with Python" });
    let _ = writeln!(md, "- Each page and API was asked for {rounds} {}, one after another, on the server's own port with your session; times are to the last byte of the answer.", if rounds == 1 { "time" } else { "times" });
    let _ = writeln!(md, "- Rating: fast < 300 ms, ok < {:.0} ms, slow < {:.0} ms, very slow beyond (the app's own slow limit is {:.0} ms).\n", crate::perf::slow_ms(), crate::perf::slow_ms() * 2.0, crate::perf::slow_ms());

    let mut flagged: Vec<(&String, &Stats)> = rows.iter().filter(|(_, s)| s.status < 400 && s.status != 0 && s.p95 >= crate::perf::slow_ms()).map(|(l, s)| (l, s)).collect();
    flagged.sort_by(|a, b| b.1.p95.partial_cmp(&a.1.p95).unwrap_or(std::cmp::Ordering::Equal));
    let failing: Vec<&(String, Stats)> = rows.iter().filter(|(_, s)| s.status == 0 || s.status >= 500).collect();
    md.push_str("## Summary\n\n");
    if flagged.is_empty() && failing.is_empty() {
        md.push_str("Nothing was slow or failing: every page and API answered within the slow limit.\n\n");
    }
    if !flagged.is_empty() {
        md.push_str("Slow (95th percentile at or over the slow limit):\n\n");
        for (l, s) in &flagged {
            let _ = writeln!(md, "- `{l}`: median {} ms, 95th {} ms, worst {} ms", f1(s.median), f1(s.p95), f1(s.max));
        }
        md.push('\n');
    }
    if !failing.is_empty() {
        md.push_str("Failing (no answer or a server error):\n\n");
        for (l, s) in &failing {
            let _ = writeln!(md, "- `{l}`: {}", if s.status == 0 { "no answer within 60 s".to_string() } else { format!("HTTP {}", s.status) });
        }
        md.push('\n');
    }
    let ok_rows: Vec<&(String, Stats)> = rows.iter().filter(|(_, s)| s.status != 0 && s.status < 400).collect();
    if !ok_rows.is_empty() {
        let mut meds: Vec<f64> = ok_rows.iter().map(|(_, s)| s.median).collect();
        meds.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let _ = writeln!(md, "Across the {} answers that worked: median of medians {} ms, slowest median {} ms.\n", ok_rows.len(), f1(percentile(&meds, 0.5)), f1(meds[meds.len() - 1]));
    }

    md.push_str("## Pages and APIs\n\nThe first column of times is the very first ask (cold: nothing cached yet); the others are over all runs.\n\n");
    md.push_str("| Route | Status | First ms | Min ms | Median ms | 95th ms | Max ms | Size KiB | Rating |\n|---|---:|---:|---:|---:|---:|---:|---:|---|\n");
    let mut sorted: Vec<&(String, Stats)> = rows.iter().collect();
    sorted.sort_by(|a, b| b.1.median.partial_cmp(&a.1.median).unwrap_or(std::cmp::Ordering::Equal));
    for (l, s) in sorted {
        let status = if s.status == 0 { "none".to_string() } else { s.status.to_string() };
        let _ = writeln!(md, "| `{l}` | {status} | {} | {} | {} | {} | {} | {} | {} |", f1(s.first), f1(s.min), f1(s.median), f1(s.p95), f1(s.max), f1(s.kib), if s.status == 0 || s.status >= 500 { "failing" } else { rating(s.p95) });
    }
    let _ = writeln!(md, "\n(Rows are sorted slowest first; a status of 303/302 is a redirect, 404 a page this world does not have, 403 one this account may not open. {} per row.)\n", if rounds == 1 { "1 run".to_string() } else { format!("{rounds} runs") });
    let _ = rows.first().map(|(_, s)| s.runs);

    md.push_str("## Several at a time\n\nFour requests in flight together, as when several people (or one busy page) ask at once.\n\n| Route | Requests | Per second | 95th ms | Errors |\n|---|---:|---:|---:|---:|\n");
    let _ = writeln!(md, "| `/` | 32 | {} | {} | {} |", f1(home.0), f1(home.1), home.2);
    let _ = writeln!(md, "| `/api/me` | 100 | {} | {} | {} |\n", f1(api.0), f1(api.1), api.2);

    md.push_str(&db_md);
    md.push_str(&disk_md);
    md.push_str(&studio_md);
    md.push_str(&machine_md);
    md.push_str(&traffic_md);
    md.push_str("---\nMade by the System Monitor's speed test. Only read-only requests were made; nothing was changed.\n");

    let stamp = crate::orm::now_stamp();
    let digits: Vec<char> = stamp.chars().filter(|c| c.is_ascii_digit()).collect();
    let id = format!("{}-{}", digits.iter().take(8).collect::<String>(), digits.iter().skip(8).take(6).collect::<String>());
    if !valid_id(&id) {
        return Err("could not name the report".into());
    }
    std::fs::write(dir.join(format!("{id}.md")), md.as_bytes()).map_err(|e| format!("cannot write the report: {e}"))?;
    for (old, _) in list_reports(&dir).into_iter().skip(KEEP) {
        let _ = std::fs::remove_file(dir.join(format!("{old}.md")));
    }
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn report_ids_are_time_stamps_only() {
        assert!(valid_id("20261117-150000"));
        for bad in ["", "../etc/passwd", "20261117150000", "20261117-15000x", "20261117-1500000", "2026111-7150000"] {
            assert!(!valid_id(bad), "{bad}");
        }
    }

    #[test]
    fn percentiles_and_ratings() {
        let v = [10.0, 20.0, 30.0, 40.0, 100.0];
        assert_eq!(percentile(&v, 0.5), 30.0);
        assert_eq!(percentile(&v, 0.95), 100.0);
        assert_eq!(percentile(&[], 0.5), 0.0);
        assert_eq!(rating(10.0), "fast");
        assert_eq!(rating(500.0), "ok");
        assert_eq!(rating(900.0), "slow");
        assert_eq!(rating(2000.0), "very slow");
    }

    #[test]
    fn stats_report_the_first_ask_and_the_worst_status() {
        let s = stats(&[Sample { ms: 90.0, status: 200, bytes: 2048 }, Sample { ms: 10.0, status: 200, bytes: 2048 }, Sample { ms: 20.0, status: 500, bytes: 0 }]);
        assert_eq!((s.first, s.min, s.max, s.status, s.runs), (90.0, 10.0, 90.0, 500, 3));
    }
}
