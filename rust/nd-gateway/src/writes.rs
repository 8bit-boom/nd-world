//! Writes the gateway makes itself (docs/RUST_WRITES.md). The rule for every one of them: the gateway answers **only the successful case**,
//! after re-doing every check Python does; anything else - no or stale session, a user that does not exist, a world the person may not use, a
//! section switched off, input Python would refuse, anything unusual in the request - is passed to Python **unchanged**, which then produces its
//! own error exactly as before. So a mistake in the gateway can never turn a refusal into an answer, only (at worst) an answer into a refusal.
use crate::{proxy::Proxy, session, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Request, Response, StatusCode, Uri};
use rand::Rng;
use regex::Regex;
use rusqlite::{params, Connection, OptionalExtension};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_BODY: usize = 16 * 1024;
const MAX_DICE_PER_TERM: u32 = 50;
const MAX_SIDES: u32 = 1000;
const MAX_TERMS: usize = 8;

// ── dice notation (app/routers/dice.py `_evaluate`) ──────────────────────────

fn notation_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        let term = r"[+-]?\s*(?:\d{0,3}d\d{1,4}|\d{1,6})";
        Regex::new(&format!(r"(?i)^{term}(?:\s*{term})*$")).unwrap()
    })
}

fn term_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)([+-]?)\s*(?:(\d{0,3})d(\d{1,4})|(\d{1,6}))").unwrap())
}

/// One finished roll: the breakdown spelled as Python's `json.dumps` spells it (that is what is stored), and the total.
pub struct Rolled {
    pub breakdown_json: String,
    pub total: i64,
}

/// `None` for anything Python would answer with an error (so the request goes to Python, which words the error).
pub fn roll(notation: &str, rng: &mut impl Rng) -> Option<Rolled> {
    let text = notation.trim_matches(' ');
    if text.is_empty() || text.len() > 120 || !notation_re().is_match(text) {
        return None;
    }
    let terms: Vec<_> = term_re().captures_iter(text).collect();
    if terms.len() > MAX_TERMS {
        return None;
    }
    let mut parts: Vec<String> = vec![];
    let mut total: i64 = 0;
    for c in terms {
        let negative = &c[1] == "-";
        let sign = if negative { "-" } else { "+" };
        if let Some(flat) = c.get(4) {
            let v: i64 = flat.as_str().parse().ok()?;
            let sum = if negative { -v } else { v };
            parts.push(format!("{{\"term\": \"{sign}{}\", \"sum\": {sum}}}", flat.as_str()));
            total += sum;
        } else {
            let n: u32 = match c.get(2).map(|m| m.as_str()).filter(|s| !s.is_empty()) {
                Some(s) => s.parse().ok()?,
                None => 1,
            };
            let sides: u32 = c[3].parse().ok()?;
            if n < 1 || n > MAX_DICE_PER_TERM || sides < 2 || sides > MAX_SIDES {
                return None;
            }
            let rolls: Vec<u32> = (0..n).map(|_| rng.gen_range(1..=sides)).collect();
            let sum: i64 = rolls.iter().map(|r| *r as i64).sum::<i64>() * if negative { -1 } else { 1 };
            let list = rolls.iter().map(u32::to_string).collect::<Vec<_>>().join(", ");
            parts.push(format!("{{\"term\": \"{sign}{n}d{sides}\", \"rolls\": [{list}], \"sum\": {sum}}}"));
            total += sum;
        }
    }
    Some(Rolled { breakdown_json: format!("[{}]", parts.join(", ")), total })
}

// ── who is asking (app/main.py auth_gate + app/deps.py get_world_ctx / world_section_level) ──

#[derive(Debug)]
pub struct Actor {
    pub user_id: i64,
    pub display_name: String,
    pub world_id: i64,
}

fn plain_slug(s: &str) -> bool {
    s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn query_w(uri: &Uri) -> Option<String> {
    crate::pages::query_param(uri, "w")
}

/// The signed-in user, the world this request is about and whether they may use the `dice` section there. `None` = let Python decide.
pub fn actor_for_dice(db: &Connection, cfg: &Config, uri: &Uri, headers: &HeaderMap, now: u64) -> Option<Actor> {
    actor_for(db, cfg, "POST", uri, headers, now, Some("dice"))
}

/// The signed-in user and the active world for a request, after auth_gate's checks (and the section matrix when `section` is given).
pub fn actor_for(db: &Connection, cfg: &Config, method: &str, uri: &Uri, headers: &HeaderMap, now: u64, section: Option<&str>) -> Option<Actor> {
    let secret = cfg.secret_key.as_deref()?;
    let cookie_header = headers.get(header::COOKIE).and_then(|v| v.to_str().ok())?;
    let raw = session::cookie(cookie_header, session::COOKIE_NAME)?;
    let (sess, signed_at) = session::read_with_time(raw, secret, now, session::MAX_AGE_SECONDS)?;
    if now.saturating_sub(signed_at) > session::REFRESH_SECONDS {
        return None; // Python would send a fresh cookie; only it can
    }
    let user_id = sess.get("user_id")?.as_i64().filter(|i| *i != 0)?;
    let (is_gm, display_name, version): (bool, Option<String>, i64) = db
        .query_row("SELECT is_gm, display_name, session_version FROM users WHERE id = ?1", params![user_id], |r| {
            Ok((r.get::<_, Option<bool>>(0)?.unwrap_or(false), r.get(1)?, r.get::<_, Option<i64>>(2)?.unwrap_or(0)))
        })
        .optional()
        .ok()??;
    if sess.get("session_version")?.as_i64()? != version {
        return None;
    }
    // the active world: ?w=<slug> over the active_world cookie, else the first world this person may use
    let wanted = query_w(uri).filter(|s| !s.is_empty()).or_else(|| session::cookie(cookie_header, "active_world").map(str::to_string)).unwrap_or_default();
    if !plain_slug(&wanted) {
        return None;
    }
    // (world id, slug, section_access_json, membership role)
    let rows: Vec<(i64, String, Option<String>, Option<String>)> = if is_gm {
        let mut st = db.prepare_cached("SELECT id, slug, section_access_json, NULL FROM worlds ORDER BY id").ok()?;
        let r = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        r
    } else {
        let mut st = db
            .prepare_cached(
                "SELECT w.id, w.slug, w.section_access_json, m.role FROM world_memberships m JOIN worlds w ON w.id = m.world_id \
                 WHERE m.user_id = ?1 ORDER BY w.id",
            )
            .ok()?;
        let r = st.query_map(params![user_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        r
    };
    let chosen = rows.iter().find(|r| r.1 == wanted).or_else(|| rows.first())?;
    if !is_gm && rows.iter().filter(|r| r.0 == chosen.0).count() != 1 {
        return None; // two membership rows for one world: not a case to guess about
    }
    // auth_gate's allowlists, then the section matrix - both from rust/nd-perm, checked against Python by tests/test_rust_permissions.py
    let viewer = nd_perm::allow::Viewer { is_gm, active_role: chosen.3.clone(), active_world_id: Some(chosen.0) };
    if !nd_perm::allow::gate_allows(&viewer, method, uri.path()) {
        return None;
    }
    if let (false, Some(section)) = (is_gm, section) {
        let role = if matches!(chosen.3.as_deref(), Some("assistant") | Some("owner")) { "assistant" } else { "player" };
        if nd_perm::sections::section_level(false, Some(role), chosen.2.as_deref(), None, section)? == "none" {
            return None;
        }
    }
    Some(Actor { user_id, display_name: display_name.unwrap_or_default(), world_id: chosen.0 })
}

// ── the roll itself ──────────────────────────────────────────────────────────

/// Python's `datetime.isoformat()` for a naive UTC time, and the form SQLAlchemy stores.
pub fn timestamps(now: SystemTime) -> (String, String) {
    let d = now.duration_since(UNIX_EPOCH).unwrap_or_default();
    let (secs, micros) = (d.as_secs() as i64, d.subsec_micros());
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (y, m, dd) = civil_from_days(days);
    let (h, mi, s) = (rem / 3600, rem % 3600 / 60, rem % 60);
    let stored = format!("{y:04}-{m:02}-{dd:02} {h:02}:{mi:02}:{s:02}.{micros:06}");
    let iso = if micros == 0 { format!("{y:04}-{m:02}-{dd:02}T{h:02}:{mi:02}:{s:02}") } else { format!("{y:04}-{m:02}-{dd:02}T{h:02}:{mi:02}:{s:02}.{micros:06}") };
    (stored, iso)
}

/// Days since 1970-01-01 -> (year, month, day) (Howard Hinnant's algorithm).
pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn ascii_printable(s: &str) -> bool {
    s.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

/// Python's str.isprintable() for the label: any printable Unicode is kept by
/// _clean_label, so only controls / separators make it Python's
fn label_printable(s: &str) -> bool {
    s.chars().all(crate::edits::py_printable)
}

/// A connection to the world's database, lent from a small pool and returned when it is dropped. Opening one costs about 2.4 ms (SQLite parses the whole schema
/// the first time); a reused one answers its first query in microseconds, and keeps its prepared statements (`prepare_cached`). Everything a request leaves behind
/// is undone on return (an open transaction is rolled back), the file is checked by identity on every checkout (a restored / replaced database file is never
/// read through an old handle), and at most 16 idle connections are kept.
pub struct Pooled {
    conn: Option<Connection>,
    key: (std::path::PathBuf, u64, u64),
}

impl std::ops::Deref for Pooled {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.conn.as_ref().expect("connection present until drop")
    }
}

impl std::ops::DerefMut for Pooled {
    fn deref_mut(&mut self) -> &mut Connection {
        self.conn.as_mut().expect("connection present until drop")
    }
}

impl Drop for Pooled {
    fn drop(&mut self) {
        let Some(conn) = self.conn.take() else { return };
        if !conn.is_autocommit() && conn.execute_batch("ROLLBACK").is_err() {
            return; // not usable again: closed
        }
        if let Ok(mut pool) = pool().lock() {
            if pool.len() < 16 {
                pool.push((self.key.clone(), conn));
            }
        }
    }
}

type PoolEntry = ((std::path::PathBuf, u64, u64), Connection);

fn pool() -> &'static std::sync::Mutex<Vec<PoolEntry>> {
    static P: OnceLock<std::sync::Mutex<Vec<PoolEntry>>> = OnceLock::new();
    P.get_or_init(|| std::sync::Mutex::new(Vec::new()))
}

/// the database file's identity: a replaced file (a restore) is a different one, whatever its name
fn file_key(path: &std::path::Path) -> Option<(std::path::PathBuf, u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let m = std::fs::metadata(path).ok()?;
    Some((path.to_path_buf(), m.dev(), m.ino()))
}

pub fn open(cfg: &Config) -> Option<Pooled> {
    open_at(&cfg.db_path)
}

fn open_at(path: &std::path::Path) -> Option<Pooled> {
    let key = file_key(path)?;
    if let Ok(mut p) = pool().lock() {
        // a pooled connection to a file that is gone or replaced is dropped (closed) here
        p.retain(|(k, _)| k.0 != key.0 || k == &key);
        if let Some(i) = p.iter().rposition(|(k, _)| k == &key) {
            let (_, conn) = p.swap_remove(i);
            return Some(Pooled { conn: Some(conn), key });
        }
    }
    let db = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
    db.busy_timeout(std::time::Duration::from_millis(30_000)).ok()?;
    db.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=OFF; PRAGMA cache_size=-4000; PRAGMA temp_store=MEMORY; PRAGMA mmap_size=67108864;").ok()?;
    db.set_prepared_statement_cache_capacity(128);
    Some(Pooled { conn: Some(db), key })
}

/// `POST /api/dice/roll`. Returns the answer, or `None` to hand the request to Python.
fn dice_roll_blocking(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let ctype = headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("");
    if ctype.split(';').next().unwrap_or("").trim().to_ascii_lowercase() != "application/json" {
        return None;
    }
    let json: serde_json::Value = serde_json::from_slice(body).ok()?;
    let obj = json.as_object()?;
    let notation = obj.get("notation")?.as_str()?;
    let label = match obj.get("label") {
        None => "",
        Some(v) => v.as_str()?,
    };
    let (id, iso, user_name, notation_stored, label_stored, rolled) = store_roll(cfg, uri, headers, notation, label)?;
    // compact spelling (the stored text uses json.dumps' ", " / ": "; terms and numbers contain neither), keys in Python's order
    let breakdown = rolled.breakdown_json.replace(", ", ",").replace(": ", ":");
    // key order and compact spelling as FastAPI's JSONResponse writes it
    let answer = format!(
        "{{\"id\":{id},\"user_name\":{},\"notation\":{},\"label\":{},\"breakdown\":{},\"total\":{},\"created_at\":\"{iso}Z\"}}",
        serde_json::to_string(&user_name).ok()?,
        serde_json::to_string(&notation_stored).ok()?,
        serde_json::to_string(&label_stored).ok()?,
        breakdown,
        rolled.total
    );
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(answer))
        .ok()
}

/// Rolls `notation` for the signed-in user and stores it in the world's log (the JSON route and the dice page's form share this)
fn store_roll(cfg: &Config, uri: &Uri, headers: &HeaderMap, notation: &str, label: &str) -> Option<(i64, String, String, String, String, Rolled)> {
    if !ascii_printable(notation) {
        return None; // a notation with non-ASCII fails Python's parse with its own message
    }
    if !label_printable(label) {
        return None; // a control / separator character in the label: Python's _clean_label
    }
    let now = SystemTime::now();
    let db = open(cfg)?;
    let actor = actor_for_dice(&db, cfg, uri, headers, now.duration_since(UNIX_EPOCH).ok()?.as_secs())?;
    let rolled = roll(notation, &mut rand::thread_rng())?;
    let notation_stored: String = notation.trim_matches(' ').chars().take(120).collect();
    let label_stored: String = label.trim_matches(' ').chars().take(80).collect();
    let user_name = if actor.display_name.is_empty() { "Someone".to_string() } else { actor.display_name.clone() };
    let (stored_at, iso) = timestamps(now);

    let tx = db.unchecked_transaction().ok()?;
    tx.execute(
        "INSERT INTO dice_rolls (world_id, user_id, user_name, notation, label, breakdown, total, created_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
        params![actor.world_id, actor.user_id, user_name, notation_stored, label_stored, rolled.breakdown_json, rolled.total, stored_at],
    )
    .ok()?;
    let id = tx.last_insert_rowid();
    // live.signal(world, "dice"): open dice pages fetch the log once (app/shared_state.py)
    tx.execute("INSERT INTO shared_state (key, n) VALUES (?1, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", params![format!("sig:{}:dice", actor.world_id)]).ok()?;
    tx.commit().ok()?;
    Some((id, iso, user_name, notation_stored, label_stored, rolled))
}

/// `POST /dice` (the page's form): a stored roll answers with the redirect back to the page; a notation Python rejects is Python's
pub fn dice_form_blocking(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    if crate::form::content_type(headers) != "application/x-www-form-urlencoded" {
        return None;
    }
    let f = crate::form::Form::parse(body)?;
    let notation = f.get("notation").unwrap_or("");
    store_roll(cfg, uri, headers, notation, "")?;
    crate::edits::redirect("/dice")
}

/// Entry point from the gateway: reads the (small) body, tries to answer, and otherwise forwards the request - body included - to Python.
pub async fn dice_roll(cfg: std::sync::Arc<Config>, proxy: &Proxy, req: Request<Body>, peer: Option<std::net::SocketAddr>) -> Response<Body> {
    let small = req.headers().get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<usize>().ok()).map_or(false, |n| n <= MAX_BODY);
    if req.method() != Method::POST || !small {
        return proxy.forward(req, peer).await;
    }
    let (parts, body) = req.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, MAX_BODY).await else {
        return Response::builder().status(StatusCode::BAD_REQUEST).body(Body::empty()).unwrap();
    };
    let (uri, headers) = (parts.uri.clone(), parts.headers.clone());
    let b = bytes.clone();
    let answer = tokio::task::spawn_blocking(move || dice_roll_blocking(&cfg, &uri, &headers, &b)).await.ok().flatten();
    match answer {
        Some(r) => r,
        None => proxy.forward(Request::from_parts(parts, Body::from(bytes)), peer).await,
    }
}

#[cfg(test)]
mod pool_tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("nd-pool-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join("world.db")
    }

    fn make(path: &std::path::Path, value: i64) {
        let c = Connection::open(path).unwrap();
        c.execute_batch(&format!("CREATE TABLE t(v INTEGER); INSERT INTO t VALUES ({value});")).unwrap();
    }

    fn value(db: &Connection) -> i64 {
        db.query_row("SELECT v FROM t ORDER BY v LIMIT 1", [], |r| r.get(0)).unwrap()
    }

    #[test]
    fn a_connection_comes_back_clean_and_is_reused() {
        let path = scratch("reuse");
        make(&path, 1);
        {
            let mut db = open_at(&path).unwrap();
            let tx = db.transaction().unwrap();
            tx.execute("INSERT INTO t VALUES (0)", []).unwrap();
            // dropped with the transaction open: rolled back, not committed and not left open for the next borrower
            std::mem::forget(tx);
        }
        let db = open_at(&path).unwrap();
        assert!(db.is_autocommit());
        assert_eq!(value(&db), 1, "the unfinished transaction must be gone");
        // a statement cached on this connection works again on the next borrow
        let n = |db: &Connection| db.prepare_cached("SELECT count(*) FROM t").unwrap().query_row([], |r| r.get::<_, i64>(0)).unwrap();
        assert_eq!(n(&db), 1);
        drop(db);
        assert_eq!(n(&open_at(&path).unwrap()), 1);
    }

    #[test]
    fn a_replaced_database_file_is_never_read_through_an_old_connection() {
        let path = scratch("replace");
        make(&path, 1);
        drop(open_at(&path).unwrap()); // pooled, pointing at the first file
        let other = path.with_file_name("restored.db");
        make(&other, 2);
        std::fs::rename(&other, &path).unwrap(); // what a restore does: a different file under the same name
        assert_eq!(value(&open_at(&path).unwrap()), 2);
    }

    #[test]
    fn at_most_sixteen_idle_connections_are_kept() {
        let path = scratch("cap");
        make(&path, 1);
        let held: Vec<Pooled> = (0..20).map(|_| open_at(&path).unwrap()).collect();
        drop(held);
        let idle = pool().lock().unwrap().iter().filter(|(k, _)| k.0 == path).count();
        assert!(idle <= 16, "{idle}");
    }
}
