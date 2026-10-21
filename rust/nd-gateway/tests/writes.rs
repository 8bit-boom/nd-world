//! The dice roll the gateway writes itself, against a scratch SQLite file with the app's tables: what it answers, what it stores, and - above all -
//! everything it must NOT answer (those requests reach the Python stand-in untouched).
use nd_gateway::{router, session, writes, Config};
use rand::SeedableRng;
use rusqlite::Connection;
use std::net::SocketAddr;
use std::sync::Arc;

const SECRET: &str = "writes-test-secret";

fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs()
}

struct Rig {
    base: String,
    db: std::path::PathBuf,
    _dir: tempfile::TempDir,
}

fn schema(db: &Connection) {
    db.execute_batch(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, display_name TEXT, is_gm BOOLEAN, session_version INTEGER);
         CREATE TABLE worlds (id INTEGER PRIMARY KEY, slug TEXT, section_access_json TEXT);
         CREATE TABLE world_memberships (id INTEGER PRIMARY KEY, world_id INTEGER, user_id INTEGER, role TEXT);
         CREATE TABLE dice_rolls (id INTEGER PRIMARY KEY, world_id INTEGER, user_id INTEGER, user_name TEXT, notation TEXT, label TEXT, breakdown TEXT, total INTEGER, created_at DATETIME);
         CREATE TABLE shared_state (key TEXT PRIMARY KEY, value TEXT, n INTEGER DEFAULT 0);
         INSERT INTO users VALUES (1,'gm@x','Gamemaster',1,1),(2,'p@x','Pia',0,1),(3,'out@x','Outsider',0,1),(4,'asst@x','',0,1);
         INSERT INTO worlds VALUES (1,'alpha',NULL),(2,'beta','{\"dice\": {\"player\": \"none\", \"assistant\": \"read\"}}');
         INSERT INTO world_memberships VALUES (1,1,2,'player'),(2,2,2,'player'),(3,1,4,'assistant'),(4,2,4,'assistant');",
    )
    .unwrap();
}

async fn spawn(app: axum::Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap() });
    a
}

async fn rig(routes: &[&str]) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("world.db");
    schema(&Connection::open(&db).unwrap());
    let up = spawn(axum::Router::new().fallback(|req: axum::extract::Request| async move {
        let len = axum::body::to_bytes(req.into_body(), 1 << 20).await.unwrap().len();
        axum::response::Response::builder().header("x-python-saw-body", len).status(418).body(axum::body::Body::from("python")).unwrap()
    }))
    .await;
    let cfg = Config {
        listen: String::new(),
        upstream: up.to_string(),
        static_dir: dir.path().join("static"),
        accel_root: dir.path().to_path_buf(),
        accel_token: String::new(),
        trust_proxy: false,
        templates_dir: dir.path().join("t"),
        rust_routes: routes.iter().map(|r| r.to_string()).collect(),
        db_path: db.clone(),
        secret_key: Some(SECRET.to_string()),
    };
    let gw = spawn(router(Arc::new(cfg))).await;
    Rig { base: format!("http://{gw}"), db, _dir: dir }
}

fn cookie(user: i64, version: i64, signed_at: u64) -> String {
    format!("session={}", session::sign(&format!("{{\"user_id\": {user}, \"session_version\": {version}}}"), SECRET, signed_at))
}

async fn post(r: &Rig, cookie_hdr: &str, extra_cookie: &str, query: &str, body: &str) -> reqwest::Response {
    reqwest::Client::new()
        .post(format!("{}/api/dice/roll{query}", r.base))
        .header("cookie", format!("{cookie_hdr}{extra_cookie}"))
        .header("content-type", "application/json")
        .body(body.to_string())
        .send()
        .await
        .unwrap()
}

fn count(r: &Rig) -> i64 {
    Connection::open(&r.db).unwrap().query_row("SELECT count(*) FROM dice_rolls", [], |x| x.get(0)).unwrap()
}

#[tokio::test]
async fn a_gm_rolls_and_the_roll_is_stored_and_announced() {
    let r = rig(&["dice_roll"]).await;
    let resp = post(&r, &cookie(1, 1, now()), "", "", "{\"notation\": \"2d6+3\", \"label\": \" Strength \"}").await;
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.headers()["x-nd-served-by"], "gateway");
    let v: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(v["user_name"], "Gamemaster");
    assert_eq!(v["notation"], "2d6+3");
    assert_eq!(v["label"], "Strength");
    let rolls: Vec<i64> = v["breakdown"][0]["rolls"].as_array().unwrap().iter().map(|x| x.as_i64().unwrap()).collect();
    assert_eq!(rolls.len(), 2);
    assert!(rolls.iter().all(|x| (1..=6).contains(x)));
    assert_eq!(v["breakdown"][0]["term"], "+2d6");
    assert_eq!(v["breakdown"][1], serde_json::json!({"term": "+3", "sum": 3}));
    assert_eq!(v["total"].as_i64().unwrap(), rolls.iter().sum::<i64>() + 3);
    assert!(v["created_at"].as_str().unwrap().ends_with('Z'));
    let db = Connection::open(&r.db).unwrap();
    let (world, user, name, breakdown, total, at): (i64, i64, String, String, i64, String) = db
        .query_row("SELECT world_id, user_id, user_name, breakdown, total, created_at FROM dice_rolls", [], |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?, x.get(4)?, x.get(5)?)))
        .unwrap();
    assert_eq!((world, user, name.as_str(), total), (1, 1, "Gamemaster", v["total"].as_i64().unwrap()));
    assert!(breakdown.starts_with("[{\"term\": \"+2d6\", \"rolls\": [") && breakdown.ends_with("{\"term\": \"+3\", \"sum\": 3}]"), "python's json.dumps spelling: {breakdown}");
    assert_eq!(at.len(), 26, "stored like SQLAlchemy: {at}");
    let n: i64 = db.query_row("SELECT n FROM shared_state WHERE key = 'sig:1:dice'", [], |x| x.get(0)).unwrap();
    assert_eq!(n, 1);
}

#[tokio::test]
async fn players_and_assistants_roll_in_their_world_and_the_matrix_is_honoured() {
    let r = rig(&["dice_roll"]).await;
    let (c2, c4, c3) = (cookie(2, 1, now()), cookie(4, 1, now()), cookie(3, 1, now()));
    // member of both worlds: the cookie picks the world, ?w= wins over it
    assert_eq!(post(&r, &c2, "; active_world=alpha", "", "{\"notation\": \"d20\"}").await.status(), 200);
    assert_eq!(post(&r, &c2, "; active_world=beta", "", "{\"notation\": \"d20\"}").await.status(), 418, "beta switched dice off for players: Python answers");
    assert_eq!(post(&r, &c2, "; active_world=beta", "?w=alpha", "{\"notation\": \"d20\"}").await.status(), 200);
    // an assistant of beta may still roll there (their level is read); a display name of "" is "Someone"
    let v: serde_json::Value = post(&r, &c4, "; active_world=beta", "", "{\"notation\": \"d6\"}").await.json().await.unwrap();
    assert_eq!(v["user_name"], "Someone");
    // not a member of anything: Python answers
    assert_eq!(post(&r, &c3, "", "", "{\"notation\": \"d6\"}").await.status(), 418);
    let db = Connection::open(&r.db).unwrap();
    let worlds: Vec<i64> = db.prepare("SELECT world_id FROM dice_rolls ORDER BY id").unwrap().query_map([], |x| x.get(0)).unwrap().map(|x| x.unwrap()).collect();
    assert_eq!(worlds, vec![1, 1, 2]);
}

#[tokio::test]
async fn whatever_python_would_refuse_reaches_python_unchanged_and_stores_nothing() {
    let r = rig(&["dice_roll"]).await;
    let good = cookie(1, 1, now());
    let cases: Vec<(&str, String, &str)> = vec![
        ("no session", String::new(), "{\"notation\": \"d6\"}"),
        ("wrong session version", cookie(1, 2, now()), "{\"notation\": \"d6\"}"),
        ("unknown user", cookie(99, 1, now()), "{\"notation\": \"d6\"}"),
        ("cookie older than python's refresh", cookie(1, 1, now() - 7 * 3600), "{\"notation\": \"d6\"}"),
        ("cookie signed for another secret", format!("session={}", session::sign("{\"user_id\": 1, \"session_version\": 1}", "other", now())), "{\"notation\": \"d6\"}"),
        ("bad notation", good.clone(), "{\"notation\": \"banana\"}"),
        ("too many dice", good.clone(), "{\"notation\": \"51d6\"}"),
        ("too many terms", good.clone(), "{\"notation\": \"1+1+1+1+1+1+1+1+1\"}"),
        ("one-sided die", good.clone(), "{\"notation\": \"1d1\"}"),
        ("empty", good.clone(), "{\"notation\": \"  \"}"),
        ("not json", good.clone(), "notation=d6"),
        ("notation not a string", good.clone(), "{\"notation\": 6}"),
        ("label not a string", good.clone(), "{\"notation\": \"d6\", \"label\": null}"),
        ("missing notation", good.clone(), "{}"),
        ("unicode label", good.clone(), "{\"notation\": \"d6\", \"label\": \"caf\\u00e9\"}"),
        ("tab in notation", good.clone(), "{\"notation\": \"d6\\t+1\"}"),
    ];
    for (what, c, body) in cases {
        let resp = post(&r, &c, "", "", body).await;
        assert_eq!(resp.status(), 418, "{what}: must reach python");
        assert_eq!(resp.headers()["x-python-saw-body"].to_str().unwrap(), body.len().to_string(), "{what}: the body arrives intact");
    }
    assert_eq!(count(&r), 0, "nothing was stored");
    // not JSON content-type, and a body over the limit, are python's too
    let resp = reqwest::Client::new().post(format!("{}/api/dice/roll", r.base)).header("cookie", &good).body("{\"notation\": \"d6\"}").send().await.unwrap();
    assert_eq!(resp.status(), 418);
    let big = format!("{{\"notation\": \"d6\", \"label\": \"{}\"}}", "x".repeat(20_000));
    assert_eq!(post(&r, &good, "", "", &big).await.status(), 418);
    assert_eq!(count(&r), 0);
}

#[tokio::test]
async fn switched_off_it_is_pythons() {
    let r = rig(&[]).await;
    assert_eq!(post(&r, &cookie(1, 1, now()), "", "", "{\"notation\": \"d6\"}").await.status(), 418);
    assert_eq!(count(&r), 0);
}

#[tokio::test]
async fn many_rolls_at_once_all_land() {
    let r = Arc::new(rig(&["dice_roll"]).await);
    let c = cookie(1, 1, now());
    let tasks: Vec<_> = (0..40)
        .map(|_| {
            let (r, c) = (r.clone(), c.clone());
            tokio::spawn(async move { post(&r, &c, "", "", "{\"notation\": \"d20\"}").await.status() })
        })
        .collect();
    for t in tasks {
        assert_eq!(t.await.unwrap(), 200);
    }
    assert_eq!(count(&r), 40);
    let n: i64 = Connection::open(&r.db).unwrap().query_row("SELECT n FROM shared_state WHERE key = 'sig:1:dice'", [], |x| x.get(0)).unwrap();
    assert_eq!(n, 40, "no lost updates to the live counter");
}

#[test]
fn the_dice_grammar_matches_the_apps() {
    let mut rng = rand::rngs::StdRng::seed_from_u64(7);
    for ok in ["d20", "2d6+3", "1d20-1", "4d8+2d6+1", "  2d6 + 3  ", "D6", "2 6", "+5", "-2", "10", "100d6", "d1000"] {
        assert!(writes::roll(ok, &mut rng).is_some() || ok.trim().starts_with("100d"), "{ok}");
    }
    for bad in ["", " ", "banana", "2d", "d", "1d1", "51d6", "1d1001", "1+1+1+1+1+1+1+1+1", "2d6+", "d0", "0d6", "1d6x", &"1".repeat(121)] {
        assert!(writes::roll(bad, &mut rng).is_none(), "{bad:?}");
    }
    let r = writes::roll("d6-2d4+1", &mut rng).unwrap();
    assert!(r.breakdown_json.starts_with("[{\"term\": \"+1d6\", \"rolls\": ["), "{}", r.breakdown_json);
    assert!(r.breakdown_json.contains("\"term\": \"-2d4\""));
}
