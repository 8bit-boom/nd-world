//! `GET /api/live` (app/live.py `live_stream`): the per-world server-sent-event stream every open page keeps, served by the gateway so an
//! open browser no longer holds a Python connection. Same events as Python: `retry: 3000`, `event: version` when the world's change counter
//! moves (and once at the start), `event: signal` for each named signal that moved, `: keep-alive` every 15 s otherwise.
//!
//! The counters live in SQLite (`shared_state`, docs/RUST_MIGRATION_STATE.md). One background task reads them every 250 ms for every stream
//! (Python refreshes its copy at the same rate); a stream looks at that copy once a second, like Python's loop.
use crate::{writes, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const POLL: Duration = Duration::from_secs(1);
const HEARTBEAT_EVERY: u32 = 15;

type Counters = Arc<Mutex<HashMap<String, i64>>>;

fn counters(cfg: &Arc<Config>) -> Counters {
    static C: OnceLock<Counters> = OnceLock::new();
    C.get_or_init(|| {
        let map: Counters = Arc::default();
        let (m, cfg) = (map.clone(), cfg.clone());
        tokio::spawn(async move {
            loop {
                let cfg2 = cfg.clone();
                let rows = tokio::task::spawn_blocking(move || -> Option<Vec<(String, i64)>> {
                    let db = writes::open(&cfg2)?;
                    let mut st = db.prepare("SELECT key, n FROM shared_state WHERE key NOT LIKE 'stage:%'").ok()?;
                    let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0)))).ok()?.collect::<Result<Vec<_>, _>>().ok();
                    r
                })
                .await
                .ok()
                .flatten();
                if let Some(rows) = rows {
                    *m.lock().unwrap() = rows.into_iter().collect(); // the table as it is now (a key that went away reads 0, as in Python)
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        });
        map
    })
    .clone()
}

/// (version, {signal name: (every-world counter, this world's counter)}) - `live.version` and `live.signals`.
fn read(c: &Counters, world: i64) -> (i64, HashMap<String, (i64, i64)>) {
    let g = c.lock().unwrap();
    let version = *g.get(&format!("live:{world}")).unwrap_or(&0);
    let mut sig: HashMap<String, (i64, i64)> = HashMap::new();
    for (k, v) in g.iter() {
        if let Some(name) = k.strip_prefix("sigall:") {
            sig.entry(name.to_string()).or_insert((0, 0)).0 = *v;
        }
    }
    let prefix = format!("sig:{world}:");
    for (k, v) in g.iter() {
        if let Some(name) = k.strip_prefix(&prefix) {
            sig.entry(name.to_string()).or_insert((0, 0)).1 = *v;
        }
    }
    (version, sig)
}

/// The stream, or `None` to let Python answer (no usable session, no world: Python's 401 / redirect / 404).
pub async fn serve(cfg: Arc<Config>, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let (cfg2, uri2, headers2) = (cfg.clone(), uri.clone(), headers.clone());
    let actor = tokio::task::spawn_blocking(move || {
        let db = writes::open(&cfg2)?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        writes::actor_for(&db, &cfg2, "GET", &uri2, &headers2, now, None)
    })
    .await
    .ok()??;
    let c = counters(&cfg);
    // the first read must be current, not 250 ms old: wait for one refresh round so a page opened right after a change sees it
    tokio::time::sleep(Duration::from_millis(260)).await;
    let world = actor.world_id;
    let stream = async_stream(c, world);
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/event-stream; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-cache")
        .header("x-accel-buffering", "no")
        .header("x-nd-served-by", "gateway")
        .body(Body::from_stream(stream))
        .ok()
}

fn async_stream(c: Counters, world: i64) -> impl futures_util::Stream<Item = Result<bytes::Bytes, std::io::Error>> {
    // (counters, world, round, last version, last signals, seconds since the last frame)
    let state = (c, world, 0u64, None::<i64>, HashMap::<String, (i64, i64)>::new(), 0u32);
    futures_util::stream::unfold(state, |(c, world, round, last, last_sig, since_beat)| async move {
        if round == 0 {
            // signals are news only from now on: the page fetched its state when it loaded
            let (_, sig) = read(&c, world);
            return Some((Ok(bytes::Bytes::from_static(b"retry: 3000\n\n")), (c, world, 1, last, sig, since_beat)));
        }
        if round > 1 {
            tokio::time::sleep(POLL).await; // Python: check, send, sleep a second
        }
        let (v, sig) = read(&c, world);
        let mut changed: Vec<&String> = sig.iter().filter(|(n, val)| last_sig.get(*n) != Some(*val)).map(|(n, _)| n).collect();
        changed.sort();
        let mut out = String::new();
        for name in &changed {
            out.push_str(&format!("event: signal\ndata: {name}\n\n"));
        }
        let (mut last, mut since_beat) = (last, since_beat);
        if Some(v) != last {
            last = Some(v);
            out.push_str(&format!("event: version\ndata: {v}\n\n"));
            since_beat = 0;
        } else if !changed.is_empty() {
            since_beat = 0;
        } else {
            since_beat += 1;
            if since_beat >= HEARTBEAT_EVERY {
                since_beat = 0;
                out.push_str(": keep-alive\n\n");
            }
        }
        Some((Ok(bytes::Bytes::from(out)), (c, world, round + 1, last, sig, since_beat)))
    })
}
