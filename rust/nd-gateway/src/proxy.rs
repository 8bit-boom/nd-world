//! Everything the gateway does not answer itself goes to the Python server, streamed both ways (uploads, server-sent events, big downloads are
//! never held in memory). Python can ask the gateway to send a file instead of sending it itself: it answers with `X-Nd-Accel-Path` (an absolute
//! path below the data folder) after it has done its own checks, and the gateway streams that file, with ranges and validators.
use crate::{files, Config};
use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, Request, Response, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

const HOP: [&str; 9] = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"];
pub const ACCEL: &str = "x-nd-accel";
pub const ACCEL_PATH: &str = "x-nd-accel-path";

#[derive(Clone)]
pub struct Proxy {
    cfg: Arc<Config>,
    client: Client<HttpConnector, Body>,
}

fn starting_page() -> Response<Body> {
    Response::builder()
        .status(StatusCode::BAD_GATEWAY)
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .header(header::RETRY_AFTER, "2")
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3><title>Starting</title>\
            <body style=\"font-family:sans-serif;background:#111;color:#ddd;padding:2rem\"><h1>nd-world is starting</h1>\
            <p>The application is not answering yet. This page tries again by itself.</p>"))
        .unwrap()
}

/// The answer for a request only the Python app can serve, when there is none (`ND_PYTHON_FREE=1`): 501 with the reason, in JSON for the API and a page otherwise
fn not_in_this_image(method: &Method, path: &str, headers: &HeaderMap) -> Response<Body> {
    let wants_json = path.starts_with("/api/") || path.starts_with("/mcp") || headers.get(header::ACCEPT).and_then(|v| v.to_str().ok()).is_some_and(|a| a.contains("json") && !a.contains("html"));
    // a write the gateway declined is almost always input it could not accept (a refusal Python words in detail): a generic 400, not a 501
    if matches!(*method, Method::POST | Method::PUT | Method::PATCH | Method::DELETE) {
        let msg = "This request could not be completed: check what was sent. (This install has no Python app to explain the refusal in detail.)";
        if wants_json {
            return Response::builder().status(StatusCode::BAD_REQUEST).header(header::CONTENT_TYPE, "application/json").header(header::CACHE_CONTROL, "no-store")
                .body(Body::from(format!("{{\"detail\":\"{msg}\"}}"))).unwrap();
        }
        return Response::builder().status(StatusCode::BAD_REQUEST).header(header::CONTENT_TYPE, "text/html; charset=utf-8").header(header::CACHE_CONTROL, "no-store")
            .body(Body::from(format!("<!doctype html><meta charset=utf-8><title>Could not complete</title><body style=\"font-family:sans-serif;background:#111;color:#ddd;padding:2rem\"><h1>Could not complete</h1><p>{msg}</p><p><a href=\"javascript:history.back()\" style=\"color:#8cf\">Back</a></p>"))).unwrap();
    }
    let msg = "This request needs the Python app, which this image does not include (ND_PYTHON_FREE).";
    if wants_json {
        return Response::builder().status(StatusCode::NOT_IMPLEMENTED).header(header::CONTENT_TYPE, "application/json").header(header::CACHE_CONTROL, "no-store")
            .body(Body::from(format!("{{\"detail\":\"{msg}\"}}"))).unwrap();
    }
    Response::builder().status(StatusCode::NOT_IMPLEMENTED).header(header::CONTENT_TYPE, "text/html; charset=utf-8").header(header::CACHE_CONTROL, "no-store")
        .body(Body::from(format!("<!doctype html><meta charset=utf-8><title>Not available</title><body style=\"font-family:sans-serif;background:#111;color:#ddd;padding:2rem\"><h1>Not available here</h1><p>{msg}</p>"))).unwrap()
}

impl Proxy {
    pub fn new(cfg: Arc<Config>) -> Proxy {
        let mut http = HttpConnector::new();
        http.set_connect_timeout(Some(Duration::from_secs(5)));
        http.set_nodelay(true);
        let client = Client::builder(TokioExecutor::new()).pool_idle_timeout(Duration::from_secs(30)).build(http);
        Proxy { cfg, client }
    }

    pub async fn upstream_up(&self) -> bool {
        matches!(tokio::time::timeout(Duration::from_secs(1), tokio::net::TcpStream::connect(&self.cfg.upstream)).await, Ok(Ok(_)))
    }

    pub async fn forward(&self, req: Request<Body>, peer: Option<SocketAddr>) -> Response<Body> {
        let (mut parts, body) = req.into_parts();
        let method = parts.method.clone();
        let req_headers = parts.headers.clone();

        let pq = parts.uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
        census("PY", method.as_str(), parts.uri.path());
        if crate::config::python_free() {
            return not_in_this_image(&method, parts.uri.path(), &req_headers);
        }
        let uri: hyper::Uri = match format!("http://{}{}", self.cfg.upstream, pq).parse() {
            Ok(u) => u,
            Err(_) => return Response::builder().status(StatusCode::BAD_REQUEST).body(Body::empty()).unwrap(),
        };
        let host = parts.headers.get(header::HOST).cloned();
        scrub_request(&mut parts.headers, &self.cfg, peer);
        if let Some(h) = host {
            parts.headers.insert(header::HOST, h);
        }
        if !self.cfg.accel_token.is_empty() {
            if let Ok(v) = HeaderValue::from_str(&self.cfg.accel_token) {
                parts.headers.insert(HeaderName::from_static(ACCEL), v);
            }
        }
        let mut out = Request::new(body);
        *out.method_mut() = method.clone();
        *out.uri_mut() = uri;
        *out.headers_mut() = parts.headers;

        let resp = match self.client.request(out).await {
            Ok(r) => r,
            Err(_) => return starting_page(),
        };
        let (rp, rbody) = resp.into_parts();
        let mut headers = rp.headers;
        // a file handed over by Python
        let accel = headers.remove(HeaderName::from_static(ACCEL_PATH));
        for h in HOP {
            headers.remove(h);
        }
        if let (Some(path), false) = (accel, self.cfg.accel_token.is_empty()) {
            return self.send_file(&method, &req_headers, path, headers).await;
        }
        let mut r = Response::new(Body::new(rbody));
        *r.status_mut() = rp.status;
        *r.headers_mut() = headers;
        r
    }

    async fn send_file(&self, method: &Method, req_headers: &HeaderMap, path: HeaderValue, mut headers: HeaderMap) -> Response<Body> {
        let not_found = || Response::builder().status(StatusCode::NOT_FOUND).header(header::CONTENT_TYPE, "application/json").body(Body::from("{\"detail\":\"Not Found\"}")).unwrap();
        let Ok(path) = path.to_str() else { return not_found() };
        let Ok(real) = tokio::fs::canonicalize(path).await else { return not_found() };
        let Ok(root) = tokio::fs::canonicalize(&self.cfg.accel_root).await else { return not_found() };
        if !real.starts_with(&root) {
            return not_found();
        }
        let content_type = headers.remove(header::CONTENT_TYPE).and_then(|v| v.to_str().ok().map(str::to_string));
        for h in [header::CONTENT_LENGTH, header::ETAG, header::LAST_MODIFIED, header::ACCEPT_RANGES, header::CONTENT_RANGE] {
            headers.remove(h);
        }
        let extra = files::Extra { headers: headers.into_iter().filter_map(|(k, v)| k.map(|k| (k, v))).collect(), content_type };
        files::send(method, req_headers, &real, extra).await.unwrap_or_else(not_found)
    }
}

fn scrub_request(h: &mut HeaderMap, cfg: &Config, peer: Option<SocketAddr>) {
    for name in HOP {
        h.remove(name);
    }
    h.remove(header::HOST);
    // a client can never claim to be the gateway
    h.remove(ACCEL);
    h.remove(ACCEL_PATH);
    // the address Python sees as the client is the gateway's peer, appended to whatever chain a trusted proxy in front sent
    let chain = h.get("x-forwarded-for").and_then(|v| v.to_str().ok()).map(str::to_string);
    h.remove("x-forwarded-for");
    if let Some(p) = peer {
        let v = match chain {
            Some(c) if cfg.trust_proxy => format!("{c}, {}", p.ip()),
            _ => p.ip().to_string(),
        };
        if let Ok(v) = HeaderValue::from_str(&v) {
            h.insert("x-forwarded-for", v);
        }
    }
    if !cfg.trust_proxy {
        h.remove("x-forwarded-proto");
        h.remove("x-forwarded-host");
        h.remove("x-forwarded-port");
        h.remove("forwarded");
        h.remove("x-real-ip");
    }
}

/// ND_FORWARD_LOG=<file>: one "METHOD path" line per request handed to Python - the census of what is still Python's (phase 6)
pub fn census(who: &str, method: &str, path: &str) {
    static LOG: std::sync::OnceLock<Option<std::sync::Mutex<std::fs::File>>> = std::sync::OnceLock::new();
    let log = LOG.get_or_init(|| std::env::var("ND_FORWARD_LOG").ok().filter(|p| !p.is_empty())
        .and_then(|p| std::fs::OpenOptions::new().create(true).append(true).open(p).ok()).map(std::sync::Mutex::new));
    if let Some(f) = log {
        if let Ok(mut f) = f.lock() {
            use std::io::Write;
            let _ = writeln!(f, "{who} {method} {path}");
        }
    }
}
