//! The in-memory AI jobs the gateway runs (app/live_jobs.py + `ai_queue.serialized(label, "job", store=...)`): a map build, the
//! character draft, an auto-tag run, ... Each kind keeps Python's own status dict per job id (what its poll route reads); each run takes
//! the AI slot for its whole length and is recorded for the Background Jobs page, which can cancel, restart and remove it. The gateway's
//! job ids start at GATEWAY_IDS, far above the per-process counters Python numbers its own from, so a poll route knows which process
//! holds a job. Like Python's, everything here is in memory: a restart forgets it.

use axum::body::Body;
use axum::http::{Response, StatusCode};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

pub const GATEWAY_IDS: i64 = 1_000_000_000;
const RETAIN_SECONDS: f64 = 6.0 * 3600.0;
const MAX_RETAINED: usize = 30;

pub type JobFut = Pin<Box<dyn Future<Output = ()> + Send>>;
/// Starts a run of a job (its id) again from the arguments it was first started with
pub type Rerun = Arc<dyn Fn(i64) -> JobFut + Send + Sync>;

struct Record {
    label: String,
    kind: &'static str,
    job_id: i64,
    initial: Value,
    status: &'static str,
    started: f64,
    finished: Option<f64>,
    error: String,
    abort: Option<tokio::task::AbortHandle>,
    rerun: Rerun,
}

#[derive(Default)]
struct Store {
    /// (kind, job id) -> the kind's status dict
    entries: HashMap<(&'static str, i64), Value>,
    /// record key ("gw..." ) -> record
    records: HashMap<String, Record>,
    /// per-kind job id counters
    next: HashMap<&'static str, i64>,
}

fn store() -> &'static Mutex<Store> {
    static S: OnceLock<Mutex<Store>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(Store::default()))
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn sweep(s: &mut Store) {
    let t = now();
    s.records.retain(|_, r| r.finished.map_or(true, |f| t - f <= RETAIN_SECONDS));
    let mut done: Vec<(String, f64)> = s.records.iter().filter_map(|(k, r)| r.finished.map(|f| (k.clone(), f))).collect();
    done.sort_by(|a, b| a.1.total_cmp(&b.1));
    let excess = done.len().saturating_sub(MAX_RETAINED);
    for (k, _) in done.into_iter().take(excess) {
        s.records.remove(&k);
    }
}

/// The kind's status dict for a job of this process (None: not the gateway's)
pub fn entry(kind: &'static str, id: i64) -> Option<Value> {
    store().lock().ok()?.entries.get(&(kind, id)).cloned()
}

/// Change a job's status dict (the runner's own `store[job_id].update(...)`)
pub fn update(kind: &'static str, id: i64, f: impl FnOnce(&mut Map<String, Value>)) {
    if let Ok(mut s) = store().lock() {
        if let Some(Value::Object(e)) = s.entries.get_mut(&(kind, id)) {
            f(e);
        }
    }
}

/// A new job id of this kind, its status dict set to `initial`; `keep_done` finished entries of the kind are kept (the routes' own
/// pruning: map markers keep 12, ...), oldest dropped first
pub fn new_job(kind: &'static str, initial: Value, keep_done: usize) -> i64 {
    let mut s = store().lock().unwrap_or_else(|e| e.into_inner());
    let n = s.next.entry(kind).or_insert(GATEWAY_IDS);
    *n += 1;
    let id = *n;
    s.entries.insert((kind, id), initial);
    let mut done: Vec<i64> = s.entries.iter().filter(|((k, _), v)| *k == kind && v.get("status").and_then(|x| x.as_str()) != Some("running"))
        .map(|((_, i), _)| *i).collect();
    done.sort();
    let excess = done.len().saturating_sub(keep_done);
    for i in done.into_iter().take(excess) {
        s.entries.remove(&(kind, i));
    }
    id
}

/// Run `rerun(id)` as job `id` of `kind`, under the AI slot, recorded for the Background Jobs page (`serialized(label, "job", store)`)
pub fn run(kind: &'static str, label: &str, id: i64, rerun: Rerun) {
    let key = format!("gw{}", (0..11).map(|_| { let c = rand::random::<u8>() % 62; (match c { 0..=9 => b'0' + c, 10..=35 => b'a' + c - 10, _ => b'A' + c - 36 }) as char }).collect::<String>());
    let initial = entry(kind, id).filter(|v| v.is_object()).unwrap_or_else(|| json!({"status": "running", "error": ""}));
    let work = rerun(id);
    let k2 = key.clone();
    let handle = tokio::spawn(async move {
        struct OnCancel(String, &'static str, i64);
        impl Drop for OnCancel {
            // an aborted task (the GM's Cancel, the server stopping) never reaches the end below
            fn drop(&mut self) {
                if let Ok(mut s) = store().lock() {
                    let running = s.records.get(&self.0).is_some_and(|r| matches!(r.status, "queued" | "running"));
                    if running {
                        if let Some(Value::Object(e)) = s.entries.get_mut(&(self.1, self.2)) {
                            if e.get("status").and_then(|v| v.as_str()) == Some("running") {
                                e.insert("status".into(), json!("error"));
                                e.insert("error".into(), json!("Cancelled."));
                            }
                        }
                        if let Some(r) = s.records.get_mut(&self.0) {
                            r.status = "cancelled";
                            r.error = "Cancelled.".into();
                            r.finished = Some(now());
                        }
                    }
                }
            }
        }
        let _guard = OnCancel(k2.clone(), kind, id);
        let slot = crate::jobs::slot(kind).await;
        if let Ok(mut s) = store().lock() {
            if let Some(r) = s.records.get_mut(&k2) {
                r.status = "running";
            }
        }
        work.await;
        drop(slot);
        // done(rec): the job's own status dict says whether it worked
        if let Ok(mut s) = store().lock() {
            let err = s.entries.get(&(kind, id)).filter(|e| e.get("status").and_then(|v| v.as_str()) == Some("error"))
                .map(|e| match e.get("error") { Some(v) if crate::edits::py_truthy_pub(Some(v)) => v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string()), _ => "Failed.".into() });
            if let Some(r) = s.records.get_mut(&k2) {
                r.status = if err.is_some() { "error" } else { "done" };
                r.error = err.unwrap_or_default();
                r.finished = Some(now());
            }
        }
    });
    let mut s = store().lock().unwrap_or_else(|e| e.into_inner());
    sweep(&mut s);
    s.records.insert(key, Record { label: label.into(), kind, job_id: id, initial, status: "queued", started: now(), finished: None, error: String::new(),
        abort: Some(handle.abort_handle()), rerun });
}

/// The gateway's rows for `GET /api/live-jobs` ("m:" in-memory jobs)
pub fn rows() -> Vec<Value> {
    let mut s = store().lock().unwrap_or_else(|e| e.into_inner());
    sweep(&mut s);
    s.records.iter().map(|(k, r)| {
        let live = matches!(r.status, "queued" | "running");
        let end = r.finished.unwrap_or_else(now);
        json!({"key": format!("m:{k}"), "source": "memory", "label": r.label, "detail": "", "status": r.status, "error": r.error, "started": r.started,
            "elapsed": ((end - r.started) as i64).max(0), "position": 0, "progress": "", "can_cancel": live, "can_restart": !live, "can_remove": !live})
    }).collect()
}

/// `POST /api/live-jobs/m:<gw key>/cancel|restart`, `DELETE /api/live-jobs/m:<gw key>`: Some(answer) for the gateway's own records
pub fn action(key: &str, action: &str) -> Option<Response<Body>> {
    let ok = || crate::edits::json_response(&json!({"ok": true}));
    let refuse = |m: &str| Some(Response::builder().status(StatusCode::BAD_REQUEST).header("content-type", "application/json").header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_compact(&json!({"detail": m})))).unwrap());
    let mut s = store().lock().ok()?;
    let rec = s.records.get(key)?;
    let live = matches!(rec.status, "queued" | "running");
    match action {
        "cancel" => {
            if !live || rec.abort.as_ref().is_none_or(|a| a.is_finished()) {
                return refuse("That job is not running any more.");
            }
            let h = rec.abort.clone();
            drop(s);
            if let Some(h) = h {
                h.abort();
            }
            ok()
        }
        "restart" => {
            if live {
                return refuse("That job cannot be restarted (it is still running, or the server no longer has it).");
            }
            let rec = s.records.remove(key)?;
            let mut initial = rec.initial.clone();
            if let Value::Object(o) = &mut initial {
                o.insert("status".into(), json!("running"));
                o.insert("error".into(), json!(""));
                o.insert("started".into(), json!(now()));
            }
            s.entries.insert((rec.kind, rec.job_id), initial);
            drop(s);
            run(rec.kind, &rec.label, rec.job_id, rec.rerun);
            ok()
        }
        _ => {
            if live {
                return refuse("Cancel it first, or it is already gone.");
            }
            s.records.remove(key);
            ok()
        }
    }
}

/// A poll route's answer for one of the gateway's jobs: the status dict, or None (Python's job, or an unknown id: Python's 404)
pub fn poll(kind: &'static str, id: i64) -> Option<Map<String, Value>> {
    if id <= GATEWAY_IDS {
        return None;
    }
    match entry(kind, id)? {
        Value::Object(o) => Some(o),
        _ => None,
    }
}
