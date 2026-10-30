//! The outbound HTTP client: http:// and https:// back ends (Unsloth Studio, Hugging Face, web fetches). TLS is rustls (ring), trusting the
//! system's CA store (the image's ca-certificates, SSL_CERT_FILE / SSL_CERT_DIR honoured) and, when that store cannot be read, the bundled web roots.

use std::sync::OnceLock;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::Full;
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;

pub type HttpsClient = Client<HttpsConnector<HttpConnector>, Full<Bytes>>;

/// A connector for both schemes, with `connect_timeout` on the TCP connect
pub fn connector(connect_timeout: Option<Duration>) -> HttpsConnector<HttpConnector> {
    let provider = rustls::crypto::ring::default_provider();
    let builder = match HttpsConnectorBuilder::new().with_provider_and_native_roots(provider.clone()) {
        Ok(b) => b,
        Err(_) => HttpsConnectorBuilder::new().with_provider_and_webpki_roots(provider).expect("webpki roots"),
    };
    let mut http = HttpConnector::new();
    http.enforce_http(false);
    http.set_nodelay(true);
    http.set_connect_timeout(connect_timeout);
    builder.https_or_http().enable_http1().wrap_connector(http)
}

/// The shared client (no connect timeout of its own: callers wrap their requests in one)
pub fn client() -> &'static HttpsClient {
    static C: OnceLock<HttpsClient> = OnceLock::new();
    C.get_or_init(|| Client::builder(TokioExecutor::new()).build(connector(None)))
}
