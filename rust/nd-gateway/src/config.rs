use std::path::PathBuf;

/// Everything comes from the environment, so the same image runs with or without the gateway (`ND_GATEWAY=0`, docker/entrypoint.sh).
#[derive(Clone, Debug)]
pub struct Config {
    /// Where the gateway listens (the port people connect to).
    pub listen: String,
    /// The Python server's address (`host:port`).
    pub upstream: String,
    /// `static/` of the app.
    pub static_dir: PathBuf,
    /// Files Python may hand over by path (`X-Nd-Accel-Path`) must be below this folder (the data folder, which holds `uploads/`).
    pub accel_root: PathBuf,
    /// Secret shared with Python: sent as `X-Nd-Accel` so Python knows it is talking to the gateway.
    pub accel_token: String,
    /// Pass a client's `X-Forwarded-Proto` / `-Host` on (set when another reverse proxy is in front that sets them).
    pub trust_proxy: bool,
    /// The session signing key (`SECRET_KEY`), for the cookie reader (not used to gate anything in phase 1).
    pub secret_key: Option<String>,
}

impl Config {
    pub fn from_env() -> Config {
        let get = |k: &str, d: &str| std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| d.to_string());
        Config {
            listen: get("ND_GATEWAY_LISTEN", "0.0.0.0:8000"),
            upstream: get("ND_UPSTREAM", "127.0.0.1:8001"),
            static_dir: PathBuf::from(get("ND_STATIC_DIR", "/app/static")),
            accel_root: PathBuf::from(get("ND_ACCEL_ROOT", "/data")),
            accel_token: get("ND_ACCEL_TOKEN", ""),
            trust_proxy: matches!(get("ND_GATEWAY_TRUST_PROXY", "0").as_str(), "1" | "true" | "yes"),
            secret_key: std::env::var("SECRET_KEY").ok().filter(|v| !v.is_empty()),
        }
    }
}
