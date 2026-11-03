//! Per-request timing, kept in memory (the gateway's side of app/perf_log.py): for every page / API request (not /static, /uploads or the live event stream) the route template,
//! status and milliseconds - nothing else (no query strings, bodies, cookies or names). The last 1000 live in a ring; `GET /api/perf/recent` and `/api/perf/summary` read it
//! (Settings -> System, the System Monitor page); a request slower than ND_SLOW_MS (default 800) is also written to stderr as one line. Next to Python the routes answer
//! Python's own numbers (the gateway forwards them); this ring serves them without Python. `queries` / `db_ms` are 0: the gateway does not count SQL statements.
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

const RING_SIZE: usize = 1000;

struct Row {
    t: f64,
    method: String,
    route: String,
    status: u16,
    ms: f64,
}

fn ring() -> &'static Mutex<VecDeque<Row>> {
    static R: OnceLock<Mutex<VecDeque<Row>>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(VecDeque::with_capacity(RING_SIZE)))
}

/// ND_SLOW_MS as Python reads it (a number; anything else is the default 800)
pub fn slow_ms() -> f64 {
    match std::env::var("ND_SLOW_MS") {
        Ok(v) if !v.trim().is_empty() => v.trim().parse::<f64>().ok().filter(|n| *n > 0.0).unwrap_or(800.0),
        _ => 800.0,
    }
}

/// The route as a template: numeric segments become `{id}`, long hex / token-like segments `{key}` - so no real id or name is ever kept
pub fn template(path: &str) -> String {
    path.split('/')
        .map(|seg| {
            let numeric = !seg.is_empty() && seg.bytes().all(|b| b.is_ascii_digit());
            let tokenish = seg.len() >= 12 && seg.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') && seg.bytes().any(|b| b.is_ascii_digit());
            if numeric { "{id}" } else if tokenish { "{key}" } else { seg }
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// Not timed: pictures and files, and the event stream every open page keeps
pub fn skip(path: &str) -> bool {
    path.starts_with("/static/") || path.starts_with("/uploads/") || path.starts_with("/map-tiles/") || path == "/api/live" || path.starts_with("/__")
}

pub fn record(method: &str, path: &str, status: u16, ms: f64) {
    let route = template(path);
    if ms >= slow_ms() {
        eprintln!("nd-gateway: slow request {method} {route} -> {status} in {ms:.0} ms");
    }
    let t = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    if let Ok(mut r) = ring().lock() {
        if r.len() == RING_SIZE {
            r.pop_front();
        }
        r.push_back(Row { t: (t * 10.0).round() / 10.0, method: method.to_string(), route, status, ms: (ms * 10.0).round() / 10.0 });
    }
}

/// `{"requests": [...]}`: the newest `limit` timed requests, oldest first
pub fn recent(limit: usize) -> Value {
    let rows: Vec<Value> = match ring().lock() {
        Ok(r) => r.iter().rev().take(limit).rev().map(|x| json!({"t": x.t, "method": x.method, "route": x.route, "status": x.status, "ms": x.ms, "queries": 0, "db_ms": 0.0})).collect(),
        Err(_) => vec![],
    };
    json!({"requests": rows})
}

/// `{"slow_ms", "routes": [...]}`: per route the calls, median / 95th / worst milliseconds, slowest first
pub fn summary() -> Value {
    let mut by: BTreeMap<(String, String), Vec<(f64, u16)>> = BTreeMap::new();
    if let Ok(r) = ring().lock() {
        for x in r.iter() {
            by.entry((x.method.clone(), x.route.clone())).or_default().push((x.ms, x.status));
        }
    }
    let mut out: Vec<Value> = by
        .into_iter()
        .map(|((method, route), rs)| {
            let mut ms: Vec<f64> = rs.iter().map(|r| r.0).collect();
            ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let p95 = ms[((ms.len() as f64 * 0.95) as usize).min(ms.len() - 1)];
            json!({"method": method, "route": route, "calls": rs.len(), "p50_ms": ms[ms.len() / 2], "p95_ms": p95, "max_ms": ms[ms.len() - 1],
                   "max_queries": 0, "avg_db_ms": 0.0, "errors": rs.iter().filter(|r| r.1 >= 500).count()})
        })
        .collect();
    out.sort_by(|a, b| b["p95_ms"].as_f64().partial_cmp(&a["p95_ms"].as_f64()).unwrap_or(std::cmp::Ordering::Equal));
    json!({"slow_ms": slow_ms(), "routes": out})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_are_templates_never_real_ids() {
        assert_eq!(template("/entity/123"), "/entity/{id}");
        assert_eq!(template("/api/ai/imagegen/jobs/45"), "/api/ai/imagegen/jobs/{id}");
        assert_eq!(template("/kind/character"), "/kind/character");
        assert_eq!(template("/"), "/");
        assert_eq!(template("/maps/schematic/abcdef123456"), "/maps/schematic/{key}");
        assert_eq!(template("/worlds/7/home/edit"), "/worlds/{id}/home/edit");
    }
}
