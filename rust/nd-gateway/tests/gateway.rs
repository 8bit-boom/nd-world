//! The gateway against a stand-in for the Python server: what it answers itself, what it passes on, and what it must never let through.
use axum::{body::Body, extract::Request, http::HeaderMap, routing::any, Router};
use nd_gateway::{router, session, Config};
use std::net::SocketAddr;
use std::path::Path;
use std::sync::Arc;

const TOKEN: &str = "test-token";

/// Python stand-in: echoes what it was sent as headers, and hands out files on `/uploads/...` like app/accel.py.
async fn upstream_handler(req: Request, root: std::path::PathBuf) -> axum::response::Response {
    let path = req.uri().path().to_string();
    let h: &HeaderMap = req.headers();
    let get = |n: &str| h.get(n).and_then(|v| v.to_str().ok()).unwrap_or("-").to_string();
    let mut b = axum::response::Response::builder()
        .header("x-seen-accel", get("x-nd-accel"))
        .header("x-seen-xff", get("x-forwarded-for"))
        .header("x-seen-proto", get("x-forwarded-proto"))
        .header("x-seen-host", get("host"))
        .header("x-seen-method", req.method().as_str());
    if let Some(rest) = path.strip_prefix("/uploads/") {
        let root = root.display();
        b = b
            .header("x-nd-accel-path", format!("{root}/{rest}"))
            .header("content-type", "image/png")
            .header("cache-control", "private, max-age=31536000, immutable")
            .header("content-length", "0");
        return b.body(Body::empty()).unwrap();
    }
    if path == "/stream" {
        let s = futures_util::stream::iter((0..3).map(|i| Ok::<_, std::io::Error>(bytes::Bytes::from(format!("chunk{i};")))));
        return b.body(Body::from_stream(s)).unwrap();
    }
    let body = axum::body::to_bytes(req.into_body(), 10_000_000).await.unwrap();
    b.header("x-seen-body-len", body.len()).body(Body::from(format!("python:{path}"))).unwrap()
}

struct Rig {
    base: String,
    _dir: tempfile::TempDir,
    root: std::path::PathBuf,
}

async fn spawn(app: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap() });
    a
}

async fn rig(token: &str, trust: bool, upstream_up: bool) -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().to_path_buf();
    std::fs::create_dir_all(root.join("static/js")).unwrap();
    std::fs::create_dir_all(root.join("static/maps")).unwrap();
    std::fs::create_dir_all(root.join("data/uploads")).unwrap();
    let uploads = root.join("data/uploads");
    let up = if upstream_up {
        spawn(Router::new().fallback(any(move |req: Request| upstream_handler(req, uploads.clone())))).await.to_string()
    } else {
        "127.0.0.1:1".to_string()
    };
    let cfg = Config {
        listen: String::new(),
        upstream: up,
        static_dir: root.join("static"),
        accel_root: root.join("data"),
        accel_token: token.to_string(),
        trust_proxy: trust,
        templates_dir: root.join("templates"),
        rust_routes: vec![],
        db_path: std::path::PathBuf::from("/nonexistent"),
        secret_key: None,
    };
    let gw = spawn(router(Arc::new(cfg))).await;
    Rig { base: format!("http://{gw}"), _dir: dir, root }
}

fn write(p: &Path, s: &[u8]) {
    std::fs::write(p, s).unwrap();
}

fn client() -> reqwest::Client {
    reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).no_gzip().no_brotli().no_deflate().build().unwrap()
}

#[tokio::test]
async fn health_answers_without_python_and_says_whether_it_is_up() {
    let r = rig(TOKEN, false, false).await;
    let t = client().get(format!("{}/__gateway/health", r.base)).send().await.unwrap().text().await.unwrap();
    assert_eq!(t, "{\"gateway\":\"ok\",\"upstream\":false}");
}

#[tokio::test]
async fn static_files_come_from_disk_with_a_year_long_cache() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("static/js/a.js"), b"var a=1;");
    let resp = client().get(format!("{}/static/js/a.js", r.base)).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    assert!(resp.headers()["cache-control"].to_str().unwrap().contains("immutable"));
    assert!(resp.headers()["content-type"].to_str().unwrap().starts_with("text/javascript") || resp.headers()["content-type"].to_str().unwrap().starts_with("application/javascript"));
    assert!(resp.headers().get("x-seen-accel").is_none(), "python was not asked");
    assert_eq!(resp.text().await.unwrap(), "var a=1;");
}

#[tokio::test]
async fn a_precompressed_twin_is_sent_when_accepted_and_only_when_fresh() {
    let r = rig(TOKEN, false, true).await;
    let src = r.root.join("static/js/big.js");
    write(&src, &vec![b'x'; 5000]);
    std::thread::sleep(std::time::Duration::from_millis(30));
    write(&r.root.join("static/js/big.js.br"), b"BROTLI");
    write(&r.root.join("static/js/big.js.gz"), b"GZIP");
    let get = |ae: &'static str| {
        let u = format!("{}/static/js/big.js", r.base);
        async move { client().get(u).header("accept-encoding", ae).send().await.unwrap() }
    };
    let resp = get("gzip, br").await;
    assert_eq!(resp.headers()["content-encoding"], "br");
    assert_eq!(resp.headers()["vary"], "Accept-Encoding");
    assert!(resp.headers()["content-type"].to_str().unwrap().contains("javascript"));
    assert_eq!(resp.text().await.unwrap(), "BROTLI");
    let resp = get("gzip").await;
    assert_eq!(resp.headers()["content-encoding"], "gzip");
    assert_eq!(resp.text().await.unwrap(), "GZIP");
    std::thread::sleep(std::time::Duration::from_millis(30));
    // the source changed after the twins were made: they are stale, so the gateway compresses it itself, as Python's GZipMiddleware did
    write(&src, &vec![b'y'; 5000]);
    let resp = get("br").await;
    assert!(resp.headers().get("content-encoding").is_none());
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.text().await.unwrap().len(), 5000, "a browser that only takes br gets the plain file");
    let resp = get("gzip").await;
    assert!(resp.headers().get("x-seen-method").is_none(), "python was not asked");
    assert_eq!(resp.headers()["content-encoding"], "gzip");
    assert_eq!(resp.headers()["vary"], "Accept-Encoding");
    let gz = resp.bytes().await.unwrap();
    let mut plain = String::new();
    std::io::Read::read_to_string(&mut flate2::read::GzDecoder::new(&gz[..]), &mut plain).unwrap();
    assert_eq!(plain, "y".repeat(5000), "the stale twin is not used; the fresh file, gzipped");
}

#[tokio::test]
async fn conditional_and_range_requests_work_on_static_files() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("static/pic.png"), b"0123456789");
    let c = client();
    let url = format!("{}/static/pic.png", r.base);
    let first = c.get(&url).send().await.unwrap();
    let etag = first.headers()["etag"].to_str().unwrap().to_string();
    let again = c.get(&url).header("if-none-match", &etag).send().await.unwrap();
    assert_eq!(again.status(), 304);
    let part = c.get(&url).header("range", "bytes=2-4").send().await.unwrap();
    assert_eq!(part.status(), 206);
    assert_eq!(part.headers()["content-range"], "bytes 2-4/10");
    assert_eq!(part.text().await.unwrap(), "234");
    let tail = c.get(&url).header("range", "bytes=-3").send().await.unwrap();
    assert_eq!(tail.text().await.unwrap(), "789");
    let bad = c.get(&url).header("range", "bytes=50-60").send().await.unwrap();
    assert_eq!(bad.status(), 416);
    let head = c.head(&url).send().await.unwrap();
    assert_eq!(head.status(), 200);
    assert_eq!(head.headers()["content-length"], "10");
}

#[tokio::test]
async fn missing_odd_and_escaping_static_paths_go_to_python() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("secret.txt"), b"never");
    write(&r.root.join("static/js/a.js.br"), b"x");
    let c = client();
    for p in ["/static/nope.js", "/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/js/%2e%2e/%2e%2e/secret.txt", "/static/js/a.js.br", "/static/"] {
        let resp = c.get(format!("{}{p}", r.base)).send().await.unwrap();
        assert!(resp.headers().get("x-seen-method").is_some(), "{p} must be answered by python");
        assert!(!resp.text().await.unwrap().contains("never"));
    }
}

#[tokio::test]
async fn a_symlink_out_of_static_is_not_followed() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("secret.txt"), b"never");
    std::os::unix::fs::symlink(r.root.join("secret.txt"), r.root.join("static/leak.txt")).unwrap();
    let resp = client().get(format!("{}/static/leak.txt", r.base)).send().await.unwrap();
    assert!(resp.headers().get("x-seen-method").is_some());
}

#[tokio::test]
async fn old_map_links_redirect_to_the_converted_picture() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("static/maps/town.avif"), b"avif");
    let resp = client().get(format!("{}/static/maps/town.jpg", r.base)).send().await.unwrap();
    assert_eq!(resp.status(), 301);
    assert_eq!(resp.headers()["location"], "/static/maps/town.avif");
}

#[tokio::test]
async fn requests_are_passed_on_with_the_peer_address_and_no_forged_internal_headers() {
    let r = rig(TOKEN, false, true).await;
    let resp = client()
        .post(format!("{}/login?next=%2F", r.base))
        .header("x-nd-accel", "forged")
        .header("x-nd-accel-path", "/etc/passwd")
        .header("x-forwarded-for", "6.6.6.6")
        .header("x-forwarded-proto", "https")
        .body(vec![b'a'; 300_000])
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["x-seen-accel"], TOKEN, "the gateway's own token, never the client's");
    assert_eq!(resp.headers()["x-seen-xff"], "127.0.0.1", "a client cannot choose the address python sees");
    assert_eq!(resp.headers()["x-seen-proto"], "-");
    assert_eq!(resp.headers()["x-seen-method"], "POST");
    assert_eq!(resp.headers()["x-seen-body-len"], "300000");
    assert_eq!(resp.text().await.unwrap(), "python:/login");
}

#[tokio::test]
async fn behind_a_trusted_proxy_the_chain_is_kept_and_extended() {
    let r = rig(TOKEN, true, true).await;
    let resp = client().get(format!("{}/x", r.base)).header("x-forwarded-for", "9.9.9.9").header("x-forwarded-proto", "https").send().await.unwrap();
    assert_eq!(resp.headers()["x-seen-xff"], "9.9.9.9, 127.0.0.1");
    assert_eq!(resp.headers()["x-seen-proto"], "https");
}

#[tokio::test]
async fn responses_are_streamed_through() {
    let r = rig(TOKEN, false, true).await;
    let text = client().get(format!("{}/stream", r.base)).send().await.unwrap().text().await.unwrap();
    assert_eq!(text, "chunk0;chunk1;chunk2;");
}

#[tokio::test]
async fn when_python_is_down_a_page_that_retries_is_shown() {
    let r = rig(TOKEN, false, false).await;
    let resp = client().get(format!("{}/", r.base)).send().await.unwrap();
    assert_eq!(resp.status(), 502);
    assert_eq!(resp.headers()["retry-after"], "2");
    assert!(resp.text().await.unwrap().contains("refresh"));
}

#[tokio::test]
async fn a_file_python_hands_over_is_sent_by_the_gateway_with_pythons_headers_and_ranges() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("data/uploads/p.png"), b"0123456789");
    let c = client();
    let resp = c.get(format!("{}/uploads/p.png", r.base)).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.headers()["content-type"], "image/png");
    assert_eq!(resp.headers()["cache-control"], "private, max-age=31536000, immutable");
    assert!(resp.headers().get("x-nd-accel-path").is_none(), "the internal header never reaches a browser");
    assert_eq!(resp.headers()["content-length"], "10");
    assert_eq!(resp.text().await.unwrap(), "0123456789");
    let part = c.get(format!("{}/uploads/p.png", r.base)).header("range", "bytes=4-").send().await.unwrap();
    assert_eq!(part.status(), 206);
    assert_eq!(part.text().await.unwrap(), "456789");
    let head = c.head(format!("{}/uploads/p.png", r.base)).send().await.unwrap();
    assert_eq!(head.headers()["content-length"], "10");
}

#[tokio::test]
async fn python_cannot_point_the_gateway_outside_the_data_folder() {
    let r = rig(TOKEN, false, true).await;
    write(&r.root.join("outside.txt"), b"never");
    // /uploads/../../outside.txt -> TEST_ROOT/../../outside.txt resolves to the temp dir, which is not below data/
    let resp = client().get(format!("{}/uploads/%2e%2e/%2e%2e/outside.txt", r.base)).send().await.unwrap();
    assert!(resp.status() == 404 || !resp.text().await.unwrap().contains("never"));
}

#[tokio::test]
async fn without_a_token_the_accel_header_from_python_is_not_trusted() {
    let r = rig("", false, true).await;
    write(&r.root.join("data/uploads/p.png"), b"0123456789");
    let resp = client().get(format!("{}/uploads/p.png", r.base)).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    assert!(resp.headers().get("x-nd-accel-path").is_none());
    assert_ne!(resp.text().await.unwrap(), "0123456789");
}

#[test]
fn session_cookies_made_by_python_are_read_and_forgeries_are_not() {
    let fx: serde_json::Value = serde_json::from_str(include_str!("fixtures/session.json")).unwrap();
    let secret = fx["secret"].as_str().unwrap();
    let at = fx["signed_at"].as_u64().unwrap();
    for case in fx["cases"].as_array().unwrap() {
        let cookie = case["cookie"].as_str().unwrap();
        assert_eq!(session::read(cookie, secret, at + 10, session::MAX_AGE_SECONDS), Some(case["session"].clone()), "{cookie}");
        assert_eq!(session::read(cookie, "another-secret", at + 10, session::MAX_AGE_SECONDS), None, "wrong key");
        assert_eq!(session::read(cookie, secret, at + session::MAX_AGE_SECONDS + 1, session::MAX_AGE_SECONDS), None, "expired");
        let mut bad = cookie.to_string();
        bad.replace_range(0..1, if bad.starts_with('A') { "B" } else { "A" });
        assert_eq!(session::read(&bad, secret, at + 10, session::MAX_AGE_SECONDS), None, "tampered payload");
    }
    let first = fx["cases"][0]["cookie"].as_str().unwrap();
    let header = format!("theme=dark; session={first}; other=1");
    assert_eq!(session::from_cookie_header(&header, secret, at + 10), Some(fx["cases"][0]["session"].clone()));
    assert_eq!(session::read("garbage", secret, at, 100), None);
    assert_eq!(session::read("a.b.c", secret, at, 100), None);
}

#[test]
fn signing_reproduces_the_cookie_python_made() {
    let fx: serde_json::Value = serde_json::from_str(include_str!("fixtures/session.json")).unwrap();
    let case = &fx["cases"][0];
    let text = "{\"user_id\": 7, \"world_id\": 3}"; // json.dumps' spelling
    assert_eq!(session::sign(text, fx["secret"].as_str().unwrap(), fx["signed_at"].as_u64().unwrap()), case["cookie"].as_str().unwrap());
}
