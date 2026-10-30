//! `GET /api/kiy/inspirations` (app/routers/lore_extras.py `_fetch_kiy_inspirations`): public reference material for the King in Yellow page, fetched from
//! Wikipedia, DuckDuckGo, Open Library, Archive.org and Project Gutenberg in parallel. Every source is tried on its own and a slow or unreachable one just
//! drops out, so the answer is always a list (empty when there is no network). The result is random and depends on the live web, so it is not compared
//! byte for byte against Python's; the gateway answers it only without Python (ND_PYTHON_FREE), where it would otherwise be a 501.

use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use rand::seq::SliceRandom;
use serde_json::{json, Value};

const AGENT: &str = "NeonDragons-WorldBuilder/1.0 (educational)";

/// The URL as fetched: with ND_TEST_HOOKS a stand-in server replaces the host (`ND_WEB_FETCH_BASE`, e.g. https://127.0.0.1:9443)
fn target(url: &str) -> String {
    if std::env::var_os("ND_TEST_HOOKS").is_some() {
        if let Ok(base) = std::env::var("ND_WEB_FETCH_BASE") {
            if let Some(rest) = url.strip_prefix("https://") {
                let path = rest.find('/').map_or("/", |i| &rest[i..]);
                return format!("{}{}", base.trim_end_matches('/'), path);
            }
        }
    }
    url.to_string()
}

/// httpx's `client.get(url)` with redirects followed: (status, body), None on any error or timeout
async fn get(url: &str, timeout: Duration) -> Option<(u16, Bytes)> {
    let work = async {
        let mut current: hyper::Uri = target(url).parse().ok()?;
        for _ in 0..20 {
            let req = hyper::Request::builder().method("GET").uri(current.clone()).header("user-agent", AGENT).body(Full::new(Bytes::new())).ok()?;
            let resp = crate::net::client().request(req).await.ok()?;
            let status = resp.status().as_u16();
            if matches!(status, 301 | 302 | 303 | 307 | 308) {
                if let Some(loc) = resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                    current = crate::studio_key::resolve_location(&current, loc).ok()?;
                    let _ = resp.into_body().collect().await;
                    continue;
                }
            }
            return Some((status, resp.into_body().collect().await.ok()?.to_bytes()));
        }
        None
    };
    tokio::time::timeout(timeout, work).await.ok().flatten()
}

async fn get_json(url: &str) -> Option<Value> {
    let (status, body) = get(url, Duration::from_secs(9)).await?;
    if status != 200 {
        return None;
    }
    serde_json::from_slice(&body).ok()
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

async fn wiki(title: &str, slug: &str) -> Option<Value> {
    let v = get_json(&format!("https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")).await?;
    let extract = take(v.get("extract").and_then(|e| e.as_str()).unwrap_or(""), 600);
    (!extract.is_empty()).then(|| json!({"source": "Wikipedia", "title": title, "extract": extract}))
}

async fn ddg(title: &str, q: &str) -> Option<Value> {
    let data = get_json(&format!("https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1")).await?;
    let pick = |k: &str| data.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(str::to_string);
    let mut text = pick("AbstractText").or_else(|| pick("Answer")).or_else(|| pick("Definition")).unwrap_or_default();
    if text.is_empty() {
        for t in data.get("RelatedTopics").and_then(|t| t.as_array()).into_iter().flatten() {
            if let Some(s) = t.get("Text").and_then(|s| s.as_str()).filter(|s| !s.is_empty()) {
                text = s.to_string();
                break;
            }
        }
    }
    (text.chars().count() >= 30).then(|| json!({"source": "DuckDuckGo", "title": title, "extract": take(&text, 500)}))
}

async fn open_library() -> Option<Value> {
    let v = get_json("https://openlibrary.org/works/OL15143745W.json").await?;
    let desc = match v.get("description") {
        Some(Value::Object(o)) => o.get("value").and_then(|s| s.as_str()).unwrap_or("").to_string(),
        Some(Value::String(s)) => s.clone(),
        _ => String::new(),
    };
    (desc.chars().count() >= 30).then(|| json!({"source": "Open Library", "title": "The King in Yellow", "extract": take(&desc, 500)}))
}

async fn archive_org(query: &str, label: &str) -> Option<Value> {
    let v = get_json(&format!("https://archive.org/advancedsearch.php?q={query}&fl[]=title&fl[]=description&rows=5&output=json&mediatype=texts")).await?;
    for doc in v.get("response").and_then(|r| r.get("docs")).and_then(|d| d.as_array()).into_iter().flatten() {
        let text = match doc.get("description") {
            Some(Value::Array(a)) => a.first().and_then(|s| s.as_str()).unwrap_or("").to_string(),
            Some(Value::String(s)) => s.clone(),
            _ => String::new(),
        };
        if text.chars().count() >= 30 {
            return Some(json!({"source": "Archive.org", "title": label, "extract": take(&text, 400)}));
        }
    }
    None
}

/// paragraphs of 61..1199 characters, stripped
fn paragraphs(text: &str) -> Vec<String> {
    text.split("\n\n").map(|p| p.trim().to_string()).filter(|p| {
        let n = p.chars().count();
        n > 60 && n < 1200
    }).collect()
}

async fn gutenberg() -> Vec<Value> {
    let mut results = vec![];
    let Some((200, bytes)) = get("https://www.gutenberg.org/cache/epub/8492/pg8492.txt", Duration::from_secs(12)).await else { return results };
    let full = String::from_utf8_lossy(&bytes).replace("\r\n", "\n");
    let body_start = full.find("The Repairer of Reputations");
    let body_end = full.rfind("End of the Project Gutenberg");
    let body: &str = match (body_start, body_end) {
        (Some(s), Some(e)) if s > 0 && e > s => &full[s..e],
        _ => &full,
    };
    let mut rng = rand::thread_rng();
    let all = paragraphs(body);
    if !all.is_empty() {
        let chosen: Vec<&String> = all.choose_multiple(&mut rng, all.len().min(2)).collect();
        let joined = chosen.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(" [...] ");
        results.push(json!({"source": "Chambers (1895)", "title": "The King in Yellow \u{2014} passage", "extract": take(&joined, 700)}));
    }
    for story in ["The Yellow Sign", "In the Court of the Dragon", "The Mask", "The Repairer of Reputations", "The Demoiselle d'Ys"] {
        let Some(pos) = body.find(story) else { continue };
        let segment: String = body[pos..].chars().take(10000).collect();
        let paras = paragraphs(&segment);
        if let Some(p) = paras.iter().take(15).collect::<Vec<_>>().choose(&mut rng) {
            results.push(json!({"source": "Chambers (1895)", "title": story, "extract": take(p, 600)}));
            if results.len() >= 4 {
                break;
            }
        }
    }
    results
}

/// `{"inspirations": [...]}`: the sources that answered, the single-extract ones first, then Gutenberg's passages
pub async fn inspirations() -> Value {
    let scalar = futures_util::future::join_all(vec![
        Box::pin(wiki("The King in Yellow", "The_King_in_Yellow")) as std::pin::Pin<Box<dyn std::future::Future<Output = Option<Value>> + Send>>,
        Box::pin(wiki("Hastur", "Hastur")),
        Box::pin(wiki("Carcosa", "Carcosa")),
        Box::pin(wiki("Robert W. Chambers", "Robert_W._Chambers")),
        Box::pin(wiki("The Yellow Sign", "Yellow_Sign")),
        Box::pin(wiki("Ambrose Bierce", "Ambrose_Bierce")),
        Box::pin(wiki("Weird fiction", "Weird_fiction")),
        Box::pin(wiki("Cosmic horror", "Cosmic_horror")),
        Box::pin(wiki("Lake of Hali", "Lake_of_Hali")),
        Box::pin(ddg("King in Yellow", "King+in+Yellow")),
        Box::pin(ddg("Hastur", "Hastur")),
        Box::pin(ddg("Carcosa", "Carcosa")),
        Box::pin(ddg("Cosmic horror fiction", "Cosmic+horror+fiction")),
        Box::pin(open_library()),
        Box::pin(archive_org("subject%3A%22King+in+Yellow%22", "King in Yellow \u{2014} Archive.org")),
        Box::pin(archive_org("subject%3A%22Hastur%22", "Hastur \u{2014} Archive.org")),
    ]);
    let (scalar, guten) = tokio::join!(scalar, gutenberg());
    let mut out: Vec<Value> = scalar.into_iter().flatten().collect();
    out.extend(guten);
    json!({"inspirations": out})
}
