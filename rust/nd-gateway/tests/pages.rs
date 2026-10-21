//! The pages the gateway builds itself, with the app's real templates.
use nd_gateway::{router, session, Config};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

const SECRET: &str = "pages-test-secret";

async fn gateway(routes: &[&str], secret: Option<&str>) -> String {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    // Python stand-in: answers anything with a marker so a request that was passed on is visible
    let up = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let up_addr = up.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(up, axum::Router::new().fallback(|| async { "python" })).await.unwrap() });
    let cfg = Config {
        listen: String::new(),
        upstream: up_addr.to_string(),
        static_dir: repo.join("static"),
        accel_root: repo.join("tests"),
        accel_token: String::new(),
        trust_proxy: false,
        templates_dir: repo.join("app/templates"),
        rust_routes: routes.iter().map(|r| r.to_string()).collect(),
        secret_key: secret.map(str::to_string),
    };
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, router(Arc::new(cfg)).into_make_service_with_connect_info::<SocketAddr>()).await.unwrap() });
    format!("http://{a}")
}

fn client() -> reqwest::Client {
    reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).build().unwrap()
}

fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs()
}

#[tokio::test]
async fn the_login_form_is_built_by_the_gateway_with_the_real_template() {
    let base = gateway(&["login"], Some(SECRET)).await;
    let r = client().get(format!("{base}/login?next=%2Fentity%2F3%3Fa%3D1")).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(r.headers()["content-type"], "text/html; charset=utf-8");
    let body = r.text().await.unwrap();
    assert!(body.contains("<title>Sign in — N&amp;D World</title>"));
    assert!(body.contains("name=\"next\" value=\"/entity/3?a=1\""), "{body}");
    assert!(!body.contains("auth-error\">"), "no error shown");
    assert!(body.contains("/static/style.css?v=") && !body.contains("?v=0\""), "asset_v is the file's hash");
}

#[tokio::test]
async fn next_is_clamped_to_this_site_and_html_escaped() {
    let base = gateway(&["login"], Some(SECRET)).await;
    for (q, want) in [("//evil.example", "value=\"/\""), ("https://evil.example", "value=\"/\""), ("/%5Cevil", "value=\"/\""), ("", "value=\"/\""),
                      ("%2Fok%22%3E%3Cscript%3E", "value=\"/ok&#34;&gt;&lt;script&gt;\"")] {
        let body = client().get(format!("{base}/login?next={q}")).send().await.unwrap().text().await.unwrap();
        assert!(body.contains(&format!("name=\"next\" {want}")), "{q} -> {body}");
    }
    // given twice, the last one wins (as in FastAPI)
    let body = client().get(format!("{base}/login?next=/a&next=/b")).send().await.unwrap().text().await.unwrap();
    assert!(body.contains("value=\"/b\""));
}

#[tokio::test]
async fn a_signed_in_browser_is_sent_on_and_a_forged_cookie_is_not_believed() {
    let base = gateway(&["login"], Some(SECRET)).await;
    let good = session::sign("{\"user_id\": 7, \"session_version\": 1}", SECRET, now());
    let r = client().get(format!("{base}/login?next=/dice%20roll")).header("cookie", format!("session={good}")).send().await.unwrap();
    assert_eq!(r.status(), 303);
    assert_eq!(r.headers()["location"], "/dice%20roll");
    for bad in [session::sign("{\"user_id\": 7}", "another-secret", now()), session::sign("{\"user_id\": 7}", SECRET, now() - 15 * 24 * 3600), "x.y.z".to_string()] {
        let r = client().get(format!("{base}/login")).header("cookie", format!("session={bad}")).send().await.unwrap();
        assert_eq!(r.status(), 200, "{bad}");
    }
    let empty = session::sign("{}", SECRET, now());
    assert_eq!(client().get(format!("{base}/login")).header("cookie", format!("session={empty}")).send().await.unwrap().status(), 200);
}

#[tokio::test]
async fn what_is_not_switched_on_or_cannot_be_checked_goes_to_python() {
    for (routes, secret) in [(vec![], Some(SECRET)), (vec!["login"], None)] {
        let base = gateway(&routes, secret).await;
        assert_eq!(client().get(format!("{base}/login")).send().await.unwrap().text().await.unwrap(), "python");
    }
    let base = gateway(&["login"], Some(SECRET)).await;
    assert_eq!(client().post(format!("{base}/login")).body("email=a").send().await.unwrap().text().await.unwrap(), "python", "the login POST is always Python's");
    assert_eq!(client().get(format!("{base}/login/2fa")).send().await.unwrap().text().await.unwrap(), "python");
}

#[tokio::test]
async fn a_page_is_gzipped_for_a_browser_that_accepts_it() {
    let base = gateway(&["login"], Some(SECRET)).await;
    let plain = reqwest::Client::builder().no_gzip().build().unwrap();
    let gz = plain.get(format!("{base}/login")).header("accept-encoding", "gzip, br").send().await.unwrap();
    assert_eq!(gz.headers()["content-encoding"], "gzip");
    assert_eq!(gz.headers()["vary"], "Accept-Encoding");
    let raw = plain.get(format!("{base}/login")).header("accept-encoding", "identity").send().await.unwrap();
    assert!(raw.headers().get("content-encoding").is_none());
    let text = raw.text().await.unwrap();
    // the gzipped bytes inflate to the same page
    let bytes = gz.bytes().await.unwrap();
    let mut out = String::new();
    std::io::Read::read_to_string(&mut flate2::read::GzDecoder::new(&bytes[..]), &mut out).unwrap();
    assert_eq!(out, text);
    assert!(bytes.len() < text.len());
}
