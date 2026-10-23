//! Durable background jobs run by the gateway (docs/RUST_JOBS.md): the AI slot both processes share, the job rows the gateway holds,
//! and its job runner.
//!
//! * One AI slot for the GPU: a job first waits its turn in this process's line (`LOCAL`, first come first served), then for the
//!   shared lease `ai_slot` in shared_state that Python's app/ai_queue.py takes the same way; the holder renews it every
//!   LEASE_SECONDS / 3 and gives it back when done (a dead holder's lease runs out).
//! * A job the gateway runs is marked `jobowner:<table>:<id>` = {"owner": "gateway", "until": ..} in shared_state, renewed while it
//!   runs. Python's sweeps (app/job_owner.py) leave such rows alone; the key is removed when the job ends, and kept when a shutdown
//!   interrupts it so the gateway's next start resumes it (`adopt`).
//! * Each running job has an abort handle here, so the cancel route can stop it; a shutdown stops every job and marks it interrupted.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::config::Config;

pub const LEASE_KEY: &str = "ai_slot";
const OWNER_RENEW: Duration = Duration::from_secs(20);
const OWNER_LEASE_SECS: f64 = 60.0;
/// job_shutdown.MAX_AUTO_RESUMES
pub const MAX_AUTO_RESUMES: i64 = 3;

fn lease_seconds() -> f64 {
    std::env::var("AI_SLOT_LEASE_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(90.0)
}

fn lease_poll() -> Duration {
    Duration::from_secs_f64(std::env::var("AI_SLOT_POLL_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(0.5))
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn owner_id() -> &'static str {
    static ID: OnceLock<String> = OnceLock::new();
    ID.get_or_init(|| format!("gateway:{}:{}", std::process::id(), (now() * 1000.0) as u64))
}

struct State {
    cfg: Arc<Config>,
    /// (table, id) -> the running job's task
    running: Mutex<HashMap<(String, i64), tokio::task::AbortHandle>>,
    local: Arc<tokio::sync::Mutex<()>>,
}

fn state() -> Option<&'static State> {
    STATE.get()
}

static STATE: OnceLock<State> = OnceLock::new();

/// Called once by `router()`; the runner needs the configuration to open the database from its tasks
pub fn init(cfg: Arc<Config>) {
    let _ = STATE.set(State { cfg, running: Mutex::new(HashMap::new()), local: Arc::new(tokio::sync::Mutex::new(())) });
}

pub fn cfg() -> Option<Arc<Config>> {
    state().map(|s| s.cfg.clone())
}

/// `shared_state.update_json(key, fn)`: compare-and-swap on the row's counter; the new value, or None when the database refused
fn update_json(db: &rusqlite::Connection, key: &str, f: impl Fn(Option<Value>) -> Value) -> Option<Value> {
    for _ in 0..50 {
        let row: Option<(Option<String>, i64)> = db.query_row("SELECT value, n FROM shared_state WHERE key = ?1", [key], |r| Ok((r.get(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0)))).ok();
        let old = row.as_ref().and_then(|r| r.0.as_deref()).and_then(|v| serde_json::from_str::<Value>(v).ok());
        let new = f(old);
        let blob = serde_json::to_string(&new).ok()?;
        let got = match row {
            None => db.execute("INSERT OR IGNORE INTO shared_state(key, value, n) VALUES (?1, ?2, 1)", rusqlite::params![key, blob]).ok()?,
            Some((_, n)) => db.execute("UPDATE shared_state SET value = ?1, n = n + 1 WHERE key = ?2 AND n = ?3", rusqlite::params![blob, key, n]).ok()?,
        };
        if got > 0 {
            return Some(new);
        }
    }
    None
}

fn lease_try(cfg: &Config, label: &str) -> bool {
    let Some(db) = crate::writes::open(cfg) else { return true };
    let t = now();
    let me = owner_id();
    let got = update_json(&db, LEASE_KEY, |old| {
        let held_by_other = old.as_ref().is_some_and(|o| o.get("owner").and_then(|v| v.as_str()).is_some_and(|w| w != me)
            && o.get("until").and_then(|v| v.as_f64()).unwrap_or(0.0) > t);
        if held_by_other { old.unwrap() } else { json!({"owner": me, "until": t + lease_seconds(), "label": label}) }
    });
    // no table, or a database that will not take the write: run rather than freeze every AI job (Python does the same)
    got.map_or(true, |v| v.get("owner").and_then(|o| o.as_str()) == Some(me))
}

fn lease_release(cfg: &Config) {
    if let Some(db) = crate::writes::open(cfg) {
        let me = owner_id();
        update_json(&db, LEASE_KEY, |old| match old {
            Some(o) if o.get("owner").and_then(|v| v.as_str()) != Some(me) => o,
            _ => json!({}),
        });
    }
}

/// This process's turn and the shared lease, held until dropped
pub struct Slot {
    _local: tokio::sync::OwnedMutexGuard<()>,
    renew: tokio::task::JoinHandle<()>,
    cfg: Arc<Config>,
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.renew.abort();
        let cfg = self.cfg.clone();
        std::thread::spawn(move || lease_release(&cfg));
    }
}

/// Wait for the AI slot (this process's line, then the lease Python takes too)
pub async fn slot(label: &str) -> Option<Slot> {
    let st = state()?;
    let local = st.local.clone().lock_owned().await;
    let cfg = st.cfg.clone();
    loop {
        let (c, l) = (cfg.clone(), label.to_string());
        if tokio::task::spawn_blocking(move || lease_try(&c, &l)).await.unwrap_or(true) {
            break;
        }
        tokio::time::sleep(lease_poll()).await;
    }
    let (c, l) = (cfg.clone(), label.to_string());
    let renew = tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs_f64(lease_seconds() / 3.0)).await;
            let (c2, l2) = (c.clone(), l.clone());
            let _ = tokio::task::spawn_blocking(move || lease_try(&c2, &l2)).await;
        }
    });
    Some(Slot { _local: local, renew, cfg })
}

fn owner_key(table: &str, id: i64) -> String {
    format!("jobowner:{table}:{id}")
}

fn own(cfg: &Config, table: &str, id: i64) {
    if let Some(db) = crate::writes::open(cfg) {
        update_json(&db, &owner_key(table, id), |_| json!({"owner": "gateway", "until": now() + OWNER_LEASE_SECS}));
    }
}

fn disown(cfg: &Config, table: &str, id: i64) {
    if let Some(db) = crate::writes::open(cfg) {
        let _ = db.execute("DELETE FROM shared_state WHERE key = ?1", [owner_key(table, id)]);
    }
}

/// How a job's run ended, for the runner's bookkeeping
pub enum Ended {
    /// done, error or cancelled: the row is final, the ownership key goes
    Final,
}

/// Run `work` as job (table, id): owned and renewed while it runs, cancellable through `cancel`
pub fn spawn<F>(table: &'static str, id: i64, work: F)
where
    F: Future<Output = Ended> + Send + 'static,
{
    let Some(st) = state() else { return };
    let cfg = st.cfg.clone();
    let task = tokio::spawn(async move {
        let c = cfg.clone();
        let _ = tokio::task::spawn_blocking(move || own(&c, table, id)).await;
        let c = cfg.clone();
        let renew = tokio::spawn(async move {
            loop {
                tokio::time::sleep(OWNER_RENEW).await;
                let c2 = c.clone();
                let _ = tokio::task::spawn_blocking(move || own(&c2, table, id)).await;
            }
        });
        let Ended::Final = work.await;
        renew.abort();
        let c = cfg.clone();
        let _ = tokio::task::spawn_blocking(move || disown(&c, table, id)).await;
        if let Some(st) = state() {
            if let Ok(mut r) = st.running.lock() {
                r.remove(&(table.to_string(), id));
            }
        }
    });
    if let Ok(mut r) = st.running.lock() {
        r.insert((table.to_string(), id), task.abort_handle());
    }
}

/// Whether this process is running job (table, id) right now
pub fn is_running(table: &str, id: i64) -> bool {
    state().and_then(|s| s.running.lock().ok().map(|r| r.get(&(table.to_string(), id)).is_some_and(|h| !h.is_finished()))).unwrap_or(false)
}

/// Stop job (table, id) (the caller writes the row's new status); true when it was running here
pub fn abort(table: &str, id: i64) -> bool {
    let Some(st) = state() else { return false };
    let handle = st.running.lock().ok().and_then(|mut r| r.remove(&(table.to_string(), id)));
    match handle {
        Some(h) if !h.is_finished() => {
            h.abort();
            let cfg = st.cfg.clone();
            let t = table.to_string();
            std::thread::spawn(move || disown(&cfg, &t, id));
            true
        }
        _ => false,
    }
}

/// `_interrupted_note()` without a chunk position
pub const INTERRUPTED_NOTE: &str = "Paused by a server restart — the work so far is saved; it will resume automatically, or use ▶ Resume.";

/// A shutdown: every running job stopped and marked interrupted, its ownership key kept for the next start
pub fn stop_all() {
    let Some(st) = state() else { return };
    let jobs: Vec<((String, i64), tokio::task::AbortHandle)> = st.running.lock().map(|mut r| r.drain().collect()).unwrap_or_default();
    let Some(db) = crate::writes::open(&st.cfg) else { return };
    for ((table, id), h) in jobs {
        h.abort();
        if table == "audio_jobs" {
            let _ = crate::orm::update(&db, "audio_jobs", id, &[("status", rusqlite::types::Value::Text("interrupted".into())),
                ("error", rusqlite::types::Value::Text(INTERRUPTED_NOTE.into())), ("finished_at", rusqlite::types::Value::Text(crate::orm::now_stamp()))]);
        }
    }
}

/// At start: the jobs a previous gateway held - in progress (it died) or interrupted (it stopped) - resumed here, up to
/// MAX_AUTO_RESUMES times each (`sweep_interrupted_jobs` + `resume_interrupted_jobs` for the gateway's own rows)
pub fn adopt() {
    let Some(st) = state() else { return };
    let Some(db) = crate::writes::open(&st.cfg) else { return };
    let keys: Vec<String> = db.prepare("SELECT key FROM shared_state WHERE key LIKE 'jobowner:audio_jobs:%'").ok()
        .and_then(|mut s| s.query_map([], |r| r.get::<_, String>(0)).ok().map(|rows| rows.filter_map(Result::ok).collect())).unwrap_or_default();
    for key in keys {
        let Some(id) = key.rsplit(':').next().and_then(|i| i.parse::<i64>().ok()) else { continue };
        let row: Option<(String, Option<String>, i64)> = db.query_row("SELECT status, purpose, COALESCE(resumed_count, 0) FROM audio_jobs WHERE id = ?1", [id],
            |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(), r.get(1)?, r.get(2)?))).ok();
        let Some((status, purpose, resumed)) = row else {
            let _ = db.execute("DELETE FROM shared_state WHERE key = ?1", [&key]);
            continue;
        };
        if !matches!(status.as_str(), "pending" | "transcribing" | "summarizing" | "interrupted") || purpose.as_deref() != Some("ai_assist") {
            if !matches!(status.as_str(), "pending" | "transcribing" | "summarizing" | "interrupted") {
                let _ = db.execute("DELETE FROM shared_state WHERE key = ?1", [&key]);
            }
            continue;
        }
        use rusqlite::types::Value as Sql;
        if resumed >= MAX_AUTO_RESUMES {
            let _ = crate::orm::update(&db, "audio_jobs", id, &[("status", Sql::Text("error".into())),
                ("error", Sql::Text(format!("Interrupted by a server restart {resumed} times in a row — resume it by hand from Background Jobs once the server is stable."))),
                ("finished_at", Sql::Text(crate::orm::now_stamp()))]);
            let _ = db.execute("DELETE FROM shared_state WHERE key = ?1", [&key]);
            continue;
        }
        crate::ai::resume_assist_job(&db, id, false);
    }
}
