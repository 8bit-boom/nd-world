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
        let mut st = db.prepare("SELECT id, slug, section_access_json, NULL FROM worlds ORDER BY id").ok()?;
        let r = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        r
    } else {
        let mut st = db
            .prepare(
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
    if !nd_perm::allow::gate_allows(&viewer, "POST", uri.path()) {
        return None;
    }
    if !is_gm {
        let role = if matches!(chosen.3.as_deref(), Some("assistant") | Some("owner")) { "assistant" } else { "player" };
        if nd_perm::sections::section_level(false, Some(role), chosen.2.as_deref(), None, "dice")? == "none" {
            return None;
        }
    }
    Some(Actor { user_id, display_name: display_name.unwrap_or_default(), world_id: chosen.0 })
}

// ── the roll itself ──────────────────────────────────────────────────────────

/// Python's `datetime.isoformat()` for a naive UTC time, and the form SQLAlchemy stores.
fn timestamps(now: SystemTime) -> (String, String) {
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
fn civil_from_days(z: i64) -> (i64, i64, i64) {
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

fn open(cfg: &Config) -> Option<Connection> {
    let db = Connection::open_with_flags(&cfg.db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
    db.busy_timeout(std::time::Duration::from_millis(30_000)).ok()?;
    db.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=OFF;").ok()?;
    Some(db)
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
    if !ascii_printable(notation) || !ascii_printable(label) {
        return None; // Unicode whitespace / printability rules are Python's
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
