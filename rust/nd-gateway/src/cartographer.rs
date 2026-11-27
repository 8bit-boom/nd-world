//! The embedded nd-cartographer map studio, compiled into the gateway
//! (`ndcart_server`). No separate binary, download, child process or reverse proxy:
//! the studio's router is mounted under `/cartographer/studio` on this origin and
//! gated by the GM session, so it works even when nd-world runs in a container (the
//! browser never needs a separate loopback port). Always on for the GM; maps live
//! in `{accel_root}/cartographer/data`. Rust-only — no Python counterpart.

use std::path::PathBuf;

use axum::body::Body;
use axum::http::{header, HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Value};

use crate::config::Config;
use crate::pages::Pages;

/// The studio is mounted under this path on the gateway's own origin.
pub const STUDIO_BASE: &str = "/cartographer/studio";
// nd-cartographer's v0.4.0 tag still declares 0.1.0 in its Cargo workspace metadata.
pub const VERSION: &str = "0.4.0";

// The studio's positioned WebGL canvas otherwise paints over its unpositioned
// 2D canvas, hiding all live drawing previews.
const STUDIO_CANVAS_CSS: &str =
    "\n#stage > canvas#gl { z-index: 0; }\n#stage > canvas#canvas { position: relative; z-index: 1; }\n";

pub async fn fix_studio_css(response: Response<Body>) -> Response<Body> {
    if response.status() != StatusCode::OK {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, 1024 * 1024).await else {
        return Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .body(Body::from("Could not load Map Maker styles"))
            .unwrap();
    };
    let mut css = bytes.to_vec();
    css.extend_from_slice(STUDIO_CANVAS_CSS.as_bytes());
    parts.headers.remove(header::ETAG);
    parts.headers.remove(header::CONTENT_LENGTH);
    parts.headers.remove(header::LAST_MODIFIED);
    parts.headers.insert(header::CACHE_CONTROL, header::HeaderValue::from_static("no-store"));
    Response::from_parts(parts, Body::from(css))
}

pub fn base(cfg: &Config) -> PathBuf {
    cfg.accel_root.join("cartographer")
}

pub(crate) fn compatible_playable_export(resolution: &Value, width: i64, height: i64, cell: f64) -> bool {
    resolution["map_origin"]["x"].as_f64() == Some(0.0)
        && resolution["map_origin"]["y"].as_f64() == Some(0.0)
        && resolution["pixels_per_grid"].as_f64() == Some(32.0)
        && (cell - 32.0).abs() <= 0.0001
        && resolution["map_size"]["x"].as_f64().map(|n| n * 32.0) == Some(width as f64)
        && resolution["map_size"]["y"].as_f64().map(|n| n * 32.0) == Some(height as f64)
}

pub(crate) fn compatible_playable_map(
    image: &str, expected_image: &str, size: (i64, i64), exported_size: (i64, i64),
    grid_type: &str, is_html: i64, grid: &Value,
) -> bool {
    image == expected_image && size == exported_size && grid_type == "square" && is_html == 0
        && grid["cell_size"].as_f64() == Some(32.0)
        && grid["offset_x"].as_f64() == Some(0.0)
        && grid["offset_y"].as_f64() == Some(0.0)
}

fn data_dir(cfg: &Config) -> PathBuf {
    base(cfg).join("data")
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Build the studio router once, at startup. The AI helpers call back into
/// nd-world on its own loopback, reusing the caller's GM session — no password is
/// stored. Must run inside the tokio runtime (the studio spawns an autosave task).
pub fn studio_router(cfg: &Config) -> axum::Router {
    let data = data_dir(cfg);
    let _ = std::fs::create_dir_all(&data);
    let port = cfg.listen.rsplit(':').next().unwrap_or("8000");
    let nd_world_base = format!("http://127.0.0.1:{port}");
    ndcart_server::embed_router(data, STUDIO_BASE, Some(nd_world_base))
}

/// The one payload the page and the status route render from. The studio is
/// compiled in, so it is always installed and running for the GM.
pub fn status(cfg: &Config) -> Value {
    json!({
        "installed": true,
        "enabled": true,
        "running": true,
        "autostart": true,
        "url": STUDIO_BASE,
        "version": VERSION,
        "dir": base(cfg).to_string_lossy(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_reports_the_pinned_cartographer_release() {
        let cfg = Config {
            listen: "127.0.0.1:8000".into(),
            static_dir: PathBuf::new(),
            accel_root: PathBuf::from("/tmp/nd-cartographer-test"),
            accel_token: String::new(),
            trust_proxy: false,
            templates_dir: PathBuf::new(),
            rust_routes: vec![],
            db_path: PathBuf::new(),
            secret_key: None,
        };
        assert_eq!(status(&cfg)["version"], VERSION);
    }

    #[tokio::test]
    async fn studio_css_puts_previews_above_webgl_and_invalidates_cached_styles() {
        let response = Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/css")
            .header(header::ETAG, "\"old\"")
            .header(header::CONTENT_LENGTH, "4")
            .body(Body::from("base"))
            .unwrap();
        let fixed = fix_studio_css(response).await;
        assert_eq!(fixed.headers()[header::CACHE_CONTROL], "no-store");
        assert!(!fixed.headers().contains_key(header::ETAG));
        assert!(!fixed.headers().contains_key(header::CONTENT_LENGTH));
        let body = axum::body::to_bytes(fixed.into_body(), 1024).await.unwrap();
        assert_eq!(body.as_ref(), format!("base{STUDIO_CANVAS_CSS}").as_bytes());
    }

    #[test]
    fn existing_workspace_maps_are_scoped_to_the_active_world() {
        let dir = tempfile::tempdir().unwrap();
        let map_dir = dir.path().join("maps");
        std::fs::create_dir(&map_dir).unwrap();
        std::fs::write(map_dir.join("city.json"), r#"{"world_id":1,"name":"City","markers":[{"name":"Market","latitude":2,"longitude":3}]}"#).unwrap();
        std::fs::write(map_dir.join("other.json"), r#"{"world_id":2,"name":"Other"}"#).unwrap();
        std::fs::write(map_dir.join("bad slug.json"), r#"{"world_id":1,"name":"Bad"}"#).unwrap();
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE schematics (id INTEGER, world_id INTEGER, slug TEXT, name TEXT, image_url TEXT, canvas_width INTEGER, canvas_height INTEGER, grid_type TEXT, grid_config_json TEXT, is_html INTEGER);
                          INSERT INTO schematics VALUES (1, 1, 'dungeon', 'Dungeon', '/uploads/schematics/dungeon.png', 640, 480, 'square', '{\"cell_size\":40}', 0),
                                                        (2, 2, 'foreign', 'Foreign', NULL, 640, 480, 'none', '{}', 0),
                                                        (3, 1, '../bad', 'Bad', NULL, 640, 480, 'none', '{}', 0);
                          CREATE TABLE map_overlays (id INTEGER, slug TEXT, custom_markers_json TEXT, custom_regions_json TEXT);
                          INSERT INTO map_overlays VALUES (1, 'city', '[{\"label\":\"Gate\"}]', '[]');").unwrap();
        let cfg = Config {
            listen: "127.0.0.1:8000".into(),
            static_dir: PathBuf::new(),
            accel_root: dir.path().to_path_buf(),
            accel_token: String::new(),
            trust_proxy: false,
            templates_dir: PathBuf::new(),
            rust_routes: vec![],
            db_path: dir.path().join("world.db"),
            secret_key: None,
        };
        assert_eq!(existing_maps(&db, &cfg, 1).unwrap(), json!([
            {"slug": "city", "name": "City", "kind": "map", "image_url": null, "width": 0, "height": 0, "grid_cell": 50, "is_html": false},
            {"slug": "dungeon", "name": "Dungeon", "kind": "schematic", "image_url": "/uploads/schematics/dungeon.png", "image_unsupported": false, "width": 640, "height": 480, "grid_cell": 40.0, "is_html": false}
        ]));
        assert_eq!(workspace_url("map", "city").as_deref(), Some("/cartographer?kind=map&slug=city"));
        assert!(workspace_url("map", "../city").is_none());
        assert!(workspace_url("other", "city").is_none());
        assert_eq!(map_overlay_data(&db, &cfg, 1, "city").unwrap(), json!({
            "markers": [{"label":"Gate"}], "regions": [],
            "original_markers": [{"name":"Market","latitude":2,"longitude":3}]
        }));
        assert_eq!(map_overlay_data(&db, &cfg, 2, "city"), Err("Map not found"));
        assert_eq!(map_overlay_data(&db, &cfg, 1, "../city"), Err("Map not found"));
        db.execute("UPDATE map_overlays SET custom_regions_json = 'broken' WHERE slug = 'city'", []).unwrap();
        assert_eq!(map_overlay_data(&db, &cfg, 1, "city"), Err("Invalid region data"));
        assert!(crate::startup::ensure_cartographer_links(&db).unwrap());
        assert!(!crate::startup::ensure_cartographer_links(&db).unwrap());
        assert!(crate::startup::ensure_cartographer_playable(&db).unwrap());
        assert!(!crate::startup::ensure_cartographer_playable(&db).unwrap());
        let project_dir = data_dir(&cfg).join("doc_test");
        std::fs::create_dir_all(&project_dir).unwrap();
        std::fs::write(project_dir.join("manifest.json"), r#"{"id":"doc_test"}"#).unwrap();
        let link = json!({"project":"doc_test","kind":"schematic","slug":"dungeon"});
        assert!(save_project_link(&db, &cfg, 2, &link).is_err());
        assert!(save_project_link(&db, &cfg, 1, &link).is_ok());
        assert!(save_project_link(&db, &cfg, 1, &link).is_ok());
        assert_eq!(save_project_link(&db, &cfg, 1, &json!({"project":"doc_test","kind":"map","slug":"city"})), Err(LinkError::Conflict));
        assert_eq!(save_project_link(&db, &cfg, 2, &json!({"project":"doc_test","kind":"schematic","slug":"foreign"})), Err(LinkError::Conflict));
        assert_eq!(project_links(&db, 1).unwrap(), json!([link]));
        assert_eq!(project_links(&db, 2).unwrap(), json!([]));
        assert!(save_project_link(&db, &cfg, 1, &json!({"project":"../doc_test","kind":"map","slug":"city"})).is_err());
        db.execute("INSERT INTO cartographer_playable (project_id, floor_id, world_id, schematic_id, image_url) VALUES ('doc_test', 'floor', 1, 1, '/uploads/map.webp')", []).unwrap();
        delete_project_links(&db, "doc_test").unwrap();
        assert_eq!(project_links(&db, 1).unwrap(), json!([]));
        assert_eq!(db.query_row("SELECT count(*) FROM cartographer_playable", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
        db.execute_batch("DROP TABLE cartographer_links").unwrap();
        assert!(matches!(save_project_link(&db, &cfg, 1, &link), Err(LinkError::Internal(_))));
    }

    #[test]
    fn workspace_renders_existing_maps_as_safe_editor_choices() {
        let mut env = minijinja::Environment::new();
        env.add_template("base.html", "{% block content %}{% endblock %}").unwrap();
        env.add_template("cartographer.html", include_str!("../../../assets/templates/cartographer.html")).unwrap();
        env.add_filter("wq", |url: String| url);
        env.add_function("asset_v", |_: String| "test");
        let render = |maps: Value| env.get_template("cartographer.html").unwrap().render(minijinja::context! {
            existing_maps => maps,
            cartographer_links => json!([]),
            cartographer => json!({"version": VERSION, "dir": "/data/cartographer"}),
            cartographer_embed_url => STUDIO_BASE,
        }).unwrap();
        let html = render(json!([
            {"kind":"map", "slug":"city", "name":"<City>", "image_url":null, "width":0, "height":0, "grid_cell":50, "is_html":false},
            {"kind":"schematic", "slug":"dungeon", "name":"Dungeon", "image_url":"/uploads/schematics/dungeon.png", "width":640, "height":480, "grid_cell":40, "is_html":false}
        ]));
        assert!(html.contains("&lt;City&gt; (map)"));
        assert!(html.contains("Dungeon (schematic)"));
        assert!(html.contains("carto-existing-frame"));
        assert!(html.contains("cartographer-workspace.js"));
        assert!(!render(json!([])).contains("carto-existing-frame"));
    }

    #[test]
    fn schematic_import_scopes_floor_groups_and_omits_hidden_portals() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE schematics (id INTEGER, world_id INTEGER, slug TEXT, is_html INTEGER, elements_json TEXT, canvas_bg TEXT);
             CREATE TABLE schematic_floors (schematic_id INTEGER, world_id INTEGER, group_id TEXT, floor_order INTEGER, label TEXT);
             INSERT INTO schematics VALUES
               (1, 1, 'ground', 0, '[{\"type\":\"pin\",\"floor_portal\":true,\"x\":10,\"y\":20,\"label\":\"Stairs\",\"destination_slug\":\"upper\",\"destination_x\":30,\"destination_y\":40},{\"type\":\"pin\",\"floor_portal\":true,\"hidden\":true,\"label\":\"Secret\"},{\"type\":\"pin\",\"x\":4,\"y\":5,\"label\":\"Gate\",\"stroke\":\"#abcdef\"},{\"type\":\"pin\",\"hidden\":1,\"label\":\"Hidden\"},{\"type\":\"path\",\"pts\":[[0,0],[1,1]],\"stroke\":\"#123456\",\"strokeW\":2},{\"type\":\"line\",\"dash\":\"5 3\"},{\"type\":\"path\",\"hidden\":true},{\"type\":\"token\",\"id\":\"token-1\",\"x\":8,\"y\":9,\"name\":\"Scout\",\"stroke\":\"#55ccff\",\"visible_to_players\":true},{\"type\":\"token\",\"id\":\"token-hidden\",\"x\":1,\"y\":1,\"name\":\"Secret\",\"hidden\":true}]', 'light'),
               (2, 1, 'upper', 0, '[]', 'blueprint'),
               (3, 2, 'foreign', 0, '[]', 'dark'),
               (4, 1, 'html', 1, '[]', 'dark');
             INSERT INTO schematic_floors VALUES
               (1,1,'tower',1,'Ground'), (2,1,'tower',2,'Upper'), (3,2,'tower',3,'Foreign');"
        ).unwrap();
        assert_eq!(schematic_floor_data(&db, 1, "ground").unwrap(), json!({"floors":[
            {"slug":"ground","label":"Ground","portals":[{"x":10,"y":20,"label":"Stairs","destination_slug":"upper","destination_x":30,"destination_y":40}],"pins":[{"x":4,"y":5,"label":"Gate","color":"#abcdef"}],"paths":[{"type":"path","points":null,"pts":[[0,0],[1,1]],"x1":null,"y1":null,"x2":null,"y2":null,"stroke":"#123456","strokeW":2,"opacity":null}],"tokens":[{"id":"token-1","x":8,"y":9,"label":"Scout","color":"#55ccff","pc_id":null,"entity_id":null,"combatant_id":null,"source":"manual","inventory":[],"qty":1,"visible":true}],"fog":{},"combat_session_id":null,"background":"light"},
            {"slug":"upper","label":"Upper","portals":[],"pins":[],"paths":[],"tokens":[],"fog":{},"combat_session_id":null,"background":"blueprint"}
        ]}));
        assert_eq!(schematic_floor_data(&db, 1, "foreign"), Err("Schematic not found"));
        assert_eq!(schematic_floor_data(&db, 1, "html"), Err("Schematic not found"));
        db.execute("UPDATE schematics SET elements_json = 'broken' WHERE slug = 'upper'", []).unwrap();
        assert_eq!(schematic_floor_data(&db, 1, "ground"), Err("Invalid schematic elements"));
    }

    #[test]
    fn playable_refresh_requires_matching_origin_image_and_grid() {
        let resolution = json!({"map_origin":{"x":0,"y":0},"pixels_per_grid":32,"map_size":{"x":5,"y":4}});
        assert!(compatible_playable_export(&resolution, 160, 128, 32.0));
        assert!(!compatible_playable_export(&resolution, 161, 128, 32.0));
        let mut shifted = resolution.clone();
        shifted["map_origin"]["x"] = json!(1);
        assert!(!compatible_playable_export(&shifted, 160, 128, 32.0));
        let mut distorted = resolution;
        distorted["map_size"] = json!({"x":4,"y":5});
        assert!(!compatible_playable_export(&distorted, 160, 128, 32.0));
        let grid = json!({"cell_size":32,"offset_x":0,"offset_y":0});
        assert!(compatible_playable_map("a", "a", (160,128), (160,128), "square", 0, &grid));
        assert!(!compatible_playable_map("b", "a", (160,128), (160,128), "square", 0, &grid));
        assert!(!compatible_playable_map("a", "a", (160,128), (160,129), "square", 0, &grid));
        assert!(!compatible_playable_map("a", "a", (160,128), (160,128), "hex", 0, &grid));
        assert!(!compatible_playable_map("a", "a", (160,128), (160,128), "square", 1, &grid));
        assert!(!compatible_playable_map("a", "a", (160,128), (160,128), "square", 0, &json!({"cell_size":32,"offset_x":1,"offset_y":0})));
    }
}

fn collect_context_strings(v: &Value, found: &mut Vec<String>) {
    const WANTED: [&str; 9] = [
        "name",
        "title",
        "text",
        "label",
        "note",
        "summary",
        "description",
        "digest",
        "kind",
    ];
    match v {
        Value::Object(o) => {
            for (k, c) in o {
                if WANTED.contains(&k.as_str()) {
                    if let Some(s) = c.as_str().filter(|s| !s.trim().is_empty()) {
                        found.push(s.trim().to_string());
                    }
                }
                collect_context_strings(c, found);
            }
        }
        Value::Array(a) => {
            for c in a {
                collect_context_strings(c, found);
            }
        }
        _ => {}
    }
}

/// Prompt-ready, read-only context from persisted nd-cartographer projects.
/// GM/AI only; failures and unknown old layouts return an empty context.
pub fn world_context(cfg: &Config, query: &str) -> String {
    let data = base(cfg).join("data");
    let Ok(entries) = std::fs::read_dir(&data) else {
        return String::new();
    };
    let words = crate::retrieval::query_words(query).unwrap_or_default();
    let mut projects: Vec<(i64, String, Vec<String>)> = vec![];
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let manifest = dir.join("manifest.json");
        if !manifest.is_file() {
            continue;
        }
        let Ok(meta) =
            serde_json::from_slice::<Value>(&std::fs::read(&manifest).unwrap_or_default())
        else {
            continue;
        };
        let project_name = meta
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or_else(|| dir.file_name().and_then(|n| n.to_str()).unwrap_or("Map"))
            .to_string();
        let snap = dir.join("snapshot.json");
        let floors = dir.join("floors.json");
        let source = if snap.is_file() { snap } else { floors };
        let Ok(raw) = std::fs::read_to_string(source) else {
            continue;
        };
        let Ok(parsed) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        let mut strings = vec![];
        collect_context_strings(&parsed, &mut strings);
        if let Ok(journal) = std::fs::read_to_string(dir.join("journal.ndjson")) {
            let lines: Vec<&str> = journal.lines().collect();
            for line in lines.iter().rev().take(300).copied() {
                if let Ok(v) = serde_json::from_str::<Value>(line) {
                    collect_context_strings(&v, &mut strings);
                }
            }
        }
        let mut unique: Vec<String> = vec![];
        for s in strings {
            if !unique.iter().any(|x| x == &s) {
                unique.push(s);
            }
        }
        if unique.is_empty() {
            continue;
        }
        let hay = format!("{project_name} {}", unique.join(" ")).to_lowercase();
        let score = words.iter().map(|w| hay.matches(w).count() as i64).sum();
        if words.is_empty() || score > 0 {
            projects.push((score, project_name, unique));
        }
    }
    projects.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| a.1.to_lowercase().cmp(&b.1.to_lowercase()))
    });
    if projects.is_empty() {
        return String::new();
    }
    let mut lines = vec!["Cartographer maps:".to_string()];
    let mut remaining = 6000usize;
    for (_, name, strings) in projects {
        let mut block = format!("- Map: {name}");
        for value in strings.into_iter().take(80) {
            if block.len() + value.len() + 3 > remaining {
                break;
            }
            block.push_str(&format!("\n  {value}"));
        }
        if block.len() <= remaining {
            lines.push(block.clone());
            remaining = remaining.saturating_sub(block.len());
        }
        if remaining == 0 {
            break;
        }
    }
    lines.join("\n")
}

fn reply(code: StatusCode, v: Value) -> Response<Body> {
    Response::builder()
        .status(code)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(v.to_string()))
        .unwrap()
}

/// The GM's active world plus the page context — None when not the GM / no world.
/// The gate for both the management page and the mounted studio router.
pub fn gm_ctx(
    cfg: &Config,
    method: &str,
    uri: &Uri,
    headers: &HeaderMap,
) -> Option<(crate::writes::Pooled, crate::layout::PageCtx)> {
    let db = crate::writes::open(cfg)?;
    let p = crate::layout::build(&db, cfg, method, uri, headers, now_secs())?;
    if !p.is_gm || p.world.is_none() {
        return None;
    }
    Some((db, p))
}

pub(crate) fn workspace_url(kind: &str, slug: &str) -> Option<String> {
    if !matches!(kind, "map" | "schematic")
        || slug.is_empty()
        || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return None;
    }
    Some(format!("/cartographer?kind={kind}&slug={slug}"))
}

fn map_overlay_data(db: &rusqlite::Connection, cfg: &Config, world_id: i64, slug: &str) -> Result<Value, &'static str> {
    if workspace_url("map", slug).is_none() {
        return Err("Map not found");
    }
    let maps = crate::maps::world_maps(db, cfg, world_id).ok_or("Could not read map data")?;
    let (_, map) = maps.into_iter().find(|(s, _)| s == slug).ok_or("Map not found")?;
    let mut statement = db.prepare("SELECT custom_markers_json, custom_regions_json FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1")
        .map_err(|_| "Could not read map overlays")?;
    let mut rows = statement.query([slug]).map_err(|_| "Could not read map overlays")?;
    let (markers, regions) = match rows.next().map_err(|_| "Could not read map overlays")? {
        Some(row) => {
            let m: String = row.get(0).map_err(|_| "Could not read map overlays")?;
            let r: String = row.get(1).map_err(|_| "Could not read map overlays")?;
            (serde_json::from_str::<Value>(&m).map_err(|_| "Invalid marker data")?,
             serde_json::from_str::<Value>(&r).map_err(|_| "Invalid region data")?)
        }
        None => (json!([]), json!([])),
    };
    if !markers.is_array() || !regions.is_array() {
        return Err("Invalid map overlay data");
    }
    let original_markers = match map.get("markers") {
        None => vec![],
        Some(Value::Array(markers)) => markers.iter().map(|marker| {
            let m = marker.as_object().ok_or("Invalid map marker data")?;
            let name = m.get("entity").and_then(Value::as_object).and_then(|e| e.get("name"))
                .or_else(|| m.get("name")).cloned().unwrap_or_else(|| json!("?"));
            Ok(json!({"name": name, "latitude": m.get("latitude"), "longitude": m.get("longitude")}))
        }).collect::<Result<Vec<_>, &'static str>>()?,
        _ => return Err("Invalid map marker data"),
    };
    Ok(json!({"markers": markers, "regions": regions, "original_markers": original_markers}))
}

pub fn import_map_overlays(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Response<Body> {
    let Some((db, page)) = gm_ctx(cfg, "GET", uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world_id) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    let slug = uri.path().strip_prefix("/api/cartographer/map-overlays/").unwrap_or("");
    match map_overlay_data(&db, cfg, world_id, slug) {
        Ok(data) => reply(StatusCode::OK, data),
        Err("Map not found") => reply(StatusCode::NOT_FOUND, json!({"error":"Map not found"})),
        Err(message) => reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":message})),
    }
}

fn schematic_floor_data(db: &rusqlite::Connection, world_id: i64, slug: &str) -> Result<Value, &'static str> {
    if workspace_url("schematic", slug).is_none() {
        return Err("Schematic not found");
    }
    let id: i64 = db.query_row(
        "SELECT id FROM schematics WHERE world_id = ?1 AND slug = ?2 AND COALESCE(is_html, 0) = 0",
        rusqlite::params![world_id, slug], |r| r.get(0),
    ).map_err(|e| if matches!(e, rusqlite::Error::QueryReturnedNoRows) { "Schematic not found" } else { "Could not read schematic" })?;
    use rusqlite::OptionalExtension;
    let group: Option<String> = db.query_row(
        "SELECT group_id FROM schematic_floors WHERE schematic_id = ?1 AND world_id = ?2",
        rusqlite::params![id, world_id], |r| r.get(0),
    ).optional().map_err(|_| "Could not read schematic floors")?;
    let navigation: Vec<(String, String)> = if let Some(group) = group {
        let mut st = db.prepare(
            "SELECT s.slug, f.label FROM schematic_floors f JOIN schematics s ON s.id = f.schematic_id
             WHERE f.world_id = ?1 AND s.world_id = ?1 AND f.group_id = ?2 AND COALESCE(s.is_html, 0) = 0
             ORDER BY f.floor_order, s.id"
        ).map_err(|_| "Could not read schematic floors")?;
        let rows = st.query_map(rusqlite::params![world_id, group], |r| Ok((r.get(0)?, r.get(1)?)))
            .map_err(|_| "Could not read schematic floors")?
            .collect::<Result<Vec<_>, _>>().map_err(|_| "Could not read schematic floors")?;
        rows
    } else {
        vec![(slug.to_string(), "Floor 1".to_string())]
    };
    let mut floors = Vec::new();
    for (floor_slug, label) in navigation {
        if workspace_url("schematic", &floor_slug).is_none() {
            return Err("Invalid schematic floor");
        }
        let (elements, background): (Option<String>, Option<String>) = db.query_row(
            "SELECT elements_json, canvas_bg FROM schematics WHERE world_id = ?1 AND slug = ?2 AND COALESCE(is_html, 0) = 0",
            rusqlite::params![world_id, floor_slug], |r| Ok((r.get(0)?, r.get(1)?)),
        ).map_err(|_| "Could not read schematic floor")?;
        let parsed: Value = serde_json::from_str(elements.as_deref().filter(|s| !s.is_empty()).unwrap_or("[]"))
            .map_err(|_| "Invalid schematic elements")?;
        let elements = parsed.as_array().ok_or("Invalid schematic elements")?;
        let portals: Vec<Value> = elements.iter().filter(|e| {
            e.get("type").and_then(Value::as_str) == Some("pin")
                && e.get("floor_portal").and_then(Value::as_bool) == Some(true)
                && !crate::edits::py_truthy_pub(e.get("hidden"))
        }).map(|e| json!({
            "x": e.get("x"), "y": e.get("y"), "label": e.get("label"),
            "destination_slug": e.get("destination_slug"),
            "destination_x": e.get("destination_x"), "destination_y": e.get("destination_y")
        })).collect();
        let pins: Vec<Value> = elements.iter().filter(|e| {
            e.get("type").and_then(Value::as_str) == Some("pin")
                && e.get("floor_portal").and_then(Value::as_bool) != Some(true)
                && !crate::edits::py_truthy_pub(e.get("hidden"))
        }).map(|e| json!({"x":e.get("x"), "y":e.get("y"), "label":e.get("label"), "color":e.get("stroke")})).collect();
        let paths: Vec<Value> = elements.iter().filter(|e| {
            matches!(e.get("type").and_then(Value::as_str), Some("line" | "path"))
                && !crate::edits::py_truthy_pub(e.get("hidden"))
                && !crate::edits::py_truthy_pub(e.get("dash"))
                && e.get("stroke").and_then(Value::as_str) != Some("none")
        }).map(|e| json!({
            "type":e.get("type"), "points":e.get("points"), "pts":e.get("pts"),
            "x1":e.get("x1"), "y1":e.get("y1"), "x2":e.get("x2"), "y2":e.get("y2"),
            "stroke":e.get("stroke"), "strokeW":e.get("strokeW"), "opacity":e.get("opacity")
        })).collect();
        let tokens: Vec<Value> = elements.iter().filter(|e| {
            e.get("type").and_then(Value::as_str) == Some("token")
                && !crate::edits::py_truthy_pub(e.get("hidden"))
                && e.get("visible_to_players").map_or(true, |v| crate::edits::py_truthy_pub(Some(v)))
        }).map(|e| json!({
            "id":e.get("id"), "x":e.get("x"), "y":e.get("y"),
            "label":e.get("name").or_else(|| e.get("label")).unwrap_or(&json!("Token")),
            "color":e.get("stroke").or_else(|| e.get("color")).unwrap_or(&json!("#00f0ff")),
            "pc_id":e.get("pc_id"), "entity_id":e.get("entity_id"), "combatant_id":e.get("combatant_id"),
            "source":e.get("source").unwrap_or(&json!("manual")),
            "inventory":e.get("inventory").unwrap_or(&json!([])),
            "qty":e.get("qty").unwrap_or(&json!(1)),
            "visible":e.get("visible_to_players").unwrap_or(&json!(true))
        })).collect();
        let fog: Value = db.query_row(
            "SELECT fog_json FROM schematics WHERE world_id = ?1 AND slug = ?2",
            rusqlite::params![world_id, floor_slug], |r| r.get::<_, Option<String>>(0),
        ).ok().flatten().and_then(|raw| serde_json::from_str::<Value>(&raw).ok()).unwrap_or_else(|| json!({}));
        let combat_session_id = db.query_row(
            "SELECT combat_session_id FROM schematics WHERE world_id = ?1 AND slug = ?2",
            rusqlite::params![world_id, floor_slug], |r| r.get::<_, Option<i64>>(0),
        ).ok().flatten().filter(|id| *id > 0);
        floors.push(json!({"slug":floor_slug, "label":label, "portals":portals, "pins":pins, "paths":paths, "tokens":tokens,
            "fog":fog, "combat_session_id":combat_session_id, "background":background}));
    }
    Ok(json!({"floors":floors}))
}

pub fn import_schematic_floors(cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Response<Body> {
    let Some((db, page)) = gm_ctx(cfg, "GET", uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world_id) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    let slug = uri.path().strip_prefix("/api/cartographer/schematic-floors/").unwrap_or("");
    match schematic_floor_data(&db, world_id, slug) {
        Ok(data) => reply(StatusCode::OK, data),
        Err("Schematic not found") => reply(StatusCode::NOT_FOUND, json!({"error":"Schematic not found"})),
        Err(message) => reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":message})),
    }
}

#[derive(Debug)]
enum PlayableFloorError {
    Invalid(&'static str),
    Database(rusqlite::Error),
}

impl From<rusqlite::Error> for PlayableFloorError {
    fn from(error: rusqlite::Error) -> Self { Self::Database(error) }
}

fn group_playable_floors(db: &rusqlite::Connection, world: i64, data: &Value) -> Result<Value, PlayableFloorError> {
    use rusqlite::{OptionalExtension, TransactionBehavior};
    use std::collections::{HashMap, HashSet};
    let invalid = |message| PlayableFloorError::Invalid(message);
    let project = data["project"].as_str().ok_or_else(|| invalid("Project required"))?;
    let floors = data["floors"].as_array().filter(|v| (2..=64).contains(&v.len()))
        .ok_or_else(|| invalid("Choose between 2 and 64 floors"))?;
    let tx = rusqlite::Transaction::new_unchecked(db, TransactionBehavior::Immediate)?;
    let mut found = HashMap::new();
    let mut records = Vec::new();
    for (order, floor) in floors.iter().enumerate() {
        let floor_id = floor["id"].as_str().filter(|v| !v.is_empty()).ok_or_else(|| invalid("Floor id required"))?;
        let label = floor["name"].as_str().map(str::trim).filter(|v| !v.is_empty() && v.len() <= 80 && !v.chars().any(char::is_control))
            .ok_or_else(|| invalid("Invalid floor name"))?;
        let row: Option<(i64, String, i64, i64, String, String, String, String, String, i64)> = tx.query_row(
            "SELECT s.id, s.slug, s.canvas_width, s.canvas_height, s.elements_json,
                    COALESCE(s.image_url, ''), p.image_url, s.grid_type, s.grid_config_json, COALESCE(s.is_html, 0)
             FROM cartographer_playable p JOIN schematics s ON s.id = p.schematic_id
             WHERE p.world_id = ?1 AND s.world_id = ?1 AND p.project_id = ?2 AND p.floor_id = ?3",
            rusqlite::params![world, project, floor_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?)),
        ).optional()?;
        let (id, slug, width, height, elements, image, expected_image, grid_type, grid, is_html) =
            row.ok_or_else(|| invalid("A floor has no playable copy in this world"))?;
        let grid: Value = serde_json::from_str(&grid).map_err(|_| invalid("Invalid playable floor grid"))?;
        if floor["width"].as_i64() != Some(width) || floor["height"].as_i64() != Some(height)
            || floor["image_url"].as_str() != Some(expected_image.as_str())
            || !compatible_playable_map(&image, &expected_image, (width, height), (width, height), &grid_type, is_html, &grid)
        {
            return Err(invalid("A playable floor changed while it was being prepared"));
        }
        let already_grouped: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM schematic_floors WHERE schematic_id = ?1)", [id], |r| r.get(0))?;
        if already_grouped || found.insert(floor_id.to_string(), (slug.clone(), width, height)).is_some() {
            return Err(invalid("A floor is repeated or already belongs to a floor group"));
        }
        let elements: Vec<Value> = serde_json::from_str(&elements).map_err(|_| invalid("Invalid schematic elements"))?;
        records.push((floor_id.to_string(), id, slug, label.to_string(), order, elements));
    }
    let mut all_links = HashSet::new();
    for (floor_id, _, _, _, _, elements) in &mut records {
        let floor = floors.iter().find(|f| f["id"].as_str() == Some(floor_id)).unwrap();
        let links = floor["links"].as_array().ok_or_else(|| invalid("Invalid floor links"))?;
        if links.len() > 5000 { return Err(invalid("Too many floor links")); }
        for link in links {
            let marker_id = link["id"].as_str().filter(|v| !v.is_empty() && v.len() <= 64)
                .ok_or_else(|| invalid("Invalid floor link id"))?;
            if !all_links.insert((floor_id.clone(), marker_id.to_string())) {
                return Err(invalid("Duplicate floor link"));
            }
            let destination = link["destination"].as_str().ok_or_else(|| invalid("Missing floor destination"))?;
            let (destination_slug, dest_width, dest_height) = found.get(destination).ok_or_else(|| invalid("Floor destination is unavailable"))?;
            if destination == floor_id { return Err(invalid("A floor link must lead to another floor")); }
            let (_, width, height) = found.get(floor_id).unwrap();
            let coordinate = |key| link[key].as_f64().filter(|v| v.is_finite()).ok_or_else(|| invalid("Invalid floor link position"));
            let (x, y, dx, dy) = (coordinate("x")?, coordinate("y")?, coordinate("destination_x")?, coordinate("destination_y")?);
            if x < 0.0 || y < 0.0 || dx < 0.0 || dy < 0.0
                || x > *width as f64 || y > *height as f64 || dx > *dest_width as f64 || dy > *dest_height as f64 {
                return Err(invalid("A floor link is outside its playable map"));
            }
            let label = link["label"].as_str().unwrap_or("Stairs").trim();
            if label.is_empty() || label.len() > 200 || label.chars().any(char::is_control) {
                return Err(invalid("Invalid floor link label"));
            }
            elements.push(json!({"id":format!("floor-portal-{:032x}", rand::random::<u128>()),
                "type":"pin", "x":x, "y":y, "label":label, "stroke":"#ffc857", "floor_portal":true,
                "destination_slug":destination_slug, "destination_x":dx, "destination_y":dy}));
        }
    }
    let group = records[0].2.clone();
    for (_, id, _, label, order, elements) in &records {
        tx.execute("INSERT INTO schematic_floors (schematic_id, world_id, group_id, floor_order, label) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![id, world, group, order + 1, label])?;
        tx.execute("UPDATE schematics SET elements_json = ?1 WHERE id = ?2 AND world_id = ?3",
            rusqlite::params![serde_json::to_string(elements).map_err(|_| invalid("Invalid schematic elements"))?, id, world])?;
        crate::orm::bump_scene(&tx, *id, world).ok_or_else(|| invalid("Could not update floor state"))?;
    }
    tx.commit()?;
    Ok(json!({"slug":group}))
}

pub fn update_run(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Response<Body> {
    use rusqlite::OptionalExtension;
    let Some((db, page)) = gm_ctx(cfg, "POST", uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    let Ok(data) = serde_json::from_slice::<Value>(body) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid request"}));
    };
    let action = data["action"].as_str().unwrap_or("");
    match action {
        "start" => {
            let project = data["project"].as_str().filter(|p| p.starts_with("doc_") && p.len() <= 64
                && p.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'));
            let Some(project) = project else { return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid project"})); };
            let Some(revision) = data["revision"].as_i64().filter(|v| *v >= 0) else {
                return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid revision"}));
            };
            let Some(floors) = data["floors"].as_array().filter(|v| v.len() <= 64) else {
                return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid floor list"}));
            };
            let mut records = Vec::new();
            for floor in floors {
                let id = floor["id"].as_str().filter(|v| !v.is_empty() && v.len() <= 64);
                let name = floor["name"].as_str().unwrap_or("Floor").trim();
                let modes = floor["modes"].as_array().cloned().unwrap_or_default();
                let modes: Vec<&str> = modes.iter().filter_map(|v| v.as_str()).filter(|m| matches!(*m, "artwork" | "geometry")).collect();
                let Some(id) = id else { return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid floor"})); };
                if modes.is_empty() { continue; }
                records.push(json!({"id":id,"name":name,"modes":modes,"status":"pending","error":null}));
            }
            if records.is_empty() { return reply(StatusCode::BAD_REQUEST, json!({"error":"No approved updates"})); }
            let id = format!("run_{:032x}", rand::random::<u128>());
            let now = now_secs() as i64;
            let floors_json = serde_json::to_string(&Value::Array(records)).unwrap_or_else(|_| "[]".into());
            if db.execute(
                "INSERT INTO cartographer_update_runs(id, world_id, project_id, revision, status, floors_json, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 'pending', ?5, ?6, ?7)",
                rusqlite::params![id, world, project, revision, floors_json, now, now],
            ).is_err() {
                return reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Could not start update run"}));
            }
            reply(StatusCode::OK, json!({"id":id,"status":"pending","floors":serde_json::from_str::<Value>(&floors_json).unwrap_or(json!([]))}))
        }
        "status" => {
            let project = data["project"].as_str().unwrap_or("");
            let run_id = data["id"].as_str().unwrap_or("");
            let row: Option<(String, i64, String, String, String, i64, i64)> = if run_id.is_empty() {
                db.query_row(
                    "SELECT id, revision, status, floors_json, project_id, created_at, updated_at
                     FROM cartographer_update_runs WHERE world_id = ?1 AND project_id = ?2
                     ORDER BY updated_at DESC LIMIT 1",
                    rusqlite::params![world, project],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)),
                ).optional().unwrap_or(None)
            } else {
                db.query_row(
                    "SELECT id, revision, status, floors_json, project_id, created_at, updated_at
                     FROM cartographer_update_runs WHERE world_id = ?1 AND id = ?2",
                    rusqlite::params![world, run_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)),
                ).optional().unwrap_or(None)
            };
            let Some((id, revision, status, floors_json, project, created, updated)) = row else {
                return reply(StatusCode::NOT_FOUND, json!({"error":"Update run not found"}));
            };
            let floors: Value = serde_json::from_str(&floors_json).unwrap_or(json!([]));
            reply(StatusCode::OK, json!({"id":id,"project":project,"revision":revision,"status":status,"floors":floors,"created_at":created,"updated_at":updated}))
        }
        "mark" => {
            let run_id = data["id"].as_str().unwrap_or("");
            let floor_id = data["floor"].as_str().unwrap_or("");
            let floor_status = data["status"].as_str().unwrap_or("");
            if run_id.is_empty() || floor_id.is_empty()
                || !matches!(floor_status, "pending" | "running" | "done" | "failed" | "cancelled") {
                return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid update run result"}));
            }
            let row: Option<(i64, String, i64)> = db.query_row(
                "SELECT revision, floors_json, updated_at FROM cartographer_update_runs WHERE world_id = ?1 AND id = ?2",
                rusqlite::params![world, run_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            ).optional().unwrap_or(None);
            let Some((revision, floors_json, _)) = row else {
                return reply(StatusCode::NOT_FOUND, json!({"error":"Update run not found"}));
            };
            let Ok(Value::Array(mut floors)) = serde_json::from_str(&floors_json) else {
                return reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Invalid stored update run"}));
            };
            let mut found = false;
            for floor in &mut floors {
                if floor["id"].as_str() == Some(floor_id) {
                    found = true;
                    floor["status"] = json!(floor_status);
                    floor["error"] = data["error"].as_str().map(|message| json!(message)).unwrap_or(Value::Null);
                }
            }
            if !found { return reply(StatusCode::BAD_REQUEST, json!({"error":"Floor is not in this update run"})); }
            let mut overall = "pending";
            if floors.iter().all(|f| f["status"].as_str() == Some("done")) { overall = "done"; }
            else if floors.iter().any(|f| f["status"].as_str() == Some("failed")) { overall = "partial"; }
            else if floors.iter().any(|f| f["status"].as_str() == Some("running")) { overall = "running"; }
            else if floors.iter().all(|f| matches!(f["status"].as_str(), Some("cancelled" | "done" | "failed"))) { overall = "partial"; }
            let now = now_secs() as i64;
            let floors_json = serde_json::to_string(&Value::Array(floors)).unwrap_or_else(|_| "[]".into());
            if db.execute(
                "UPDATE cartographer_update_runs SET status = ?1, floors_json = ?2, updated_at = ?3 WHERE world_id = ?4 AND id = ?5",
                rusqlite::params![overall, floors_json, now, world, run_id],
            ).is_err() { return reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Could not save update result"})); }
            reply(StatusCode::OK, json!({"id":run_id,"revision":revision,"status":overall,"floors":serde_json::from_str::<Value>(&floors_json).unwrap_or(json!([]))}))
        }
        "cancel" => {
            let run_id = data["id"].as_str().unwrap_or("");
            let row: Option<String> = db.query_row(
                "SELECT floors_json FROM cartographer_update_runs WHERE world_id = ?1 AND id = ?2",
                rusqlite::params![world, run_id],
                |r| r.get(0),
            ).optional().unwrap_or(None);
            let Some(floors_json) = row else { return reply(StatusCode::NOT_FOUND, json!({"error":"Update run not found"})); };
            let Ok(Value::Array(mut floors)) = serde_json::from_str(&floors_json) else {
                return reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Invalid stored update run"}));
            };
            for floor in &mut floors {
                if matches!(floor["status"].as_str(), Some("pending" | "running")) {
                    floor["status"] = json!("cancelled");
                }
            }
            let now = now_secs() as i64;
            let floors_json = serde_json::to_string(&Value::Array(floors)).unwrap_or_else(|_| "[]".into());
            let _ = db.execute(
                "UPDATE cartographer_update_runs SET status = 'cancelled', floors_json = ?1, updated_at = ?2 WHERE world_id = ?3 AND id = ?4",
                rusqlite::params![floors_json, now, world, run_id],
            );
            reply(StatusCode::OK, json!({"id":run_id,"status":"cancelled","floors":serde_json::from_str::<Value>(&floors_json).unwrap_or(json!([]))}))
        }
        _ => reply(StatusCode::BAD_REQUEST, json!({"error":"Unknown update run action"})),
    }
}

pub fn playable_preview(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Response<Body> {
    use rusqlite::OptionalExtension;
    let Some((db, page)) = gm_ctx(cfg, "POST", uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    let Ok(data) = serde_json::from_slice::<Value>(body) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid request"}));
    };
    let Some(project) = data["project"].as_str().filter(|p| p.starts_with("doc_") && p.len() <= 64
        && p.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid project"}));
    };
    let manifest = match std::fs::read(data_dir(cfg).join(project).join("manifest.json")) {
        Ok(bytes) => bytes,
        Err(_) => return reply(StatusCode::NOT_FOUND, json!({"error":"Project not found"})),
    };
    let manifest: Value = match serde_json::from_slice(&manifest) {
        Ok(value) => value,
        Err(_) => return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid project manifest"})),
    };
    if manifest["id"].as_str() != Some(project) {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid project manifest"}));
    }
    let revision = data["revision"].as_i64().filter(|v| *v >= 0).unwrap_or(0);
    let floors = data["floors"].as_array().cloned().unwrap_or_default();
    let mut preview = Vec::new();
    for floor in floors {
        let Some(floor_id) = floor["id"].as_str().filter(|v| !v.is_empty() && v.len() <= 64) else {
            continue;
        };
        let name = floor["name"].as_str().unwrap_or("Floor");
        let row: Option<(String, i64, i64, String, String, Option<i64>, Option<String>, Option<String>)> =
            db.query_row(
                "SELECT s.slug, s.canvas_width, s.canvas_height, COALESCE(s.walls_json, '[]'), COALESCE(s.lights_json, '[]'),
                        p.source_rev, p.walls_json, p.lights_json
                 FROM cartographer_playable p JOIN schematics s ON s.id = p.schematic_id
                 WHERE p.world_id = ?1 AND s.world_id = ?1 AND p.project_id = ?2 AND p.floor_id = ?3",
                rusqlite::params![world, project, floor_id],
                |r| Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, Option<i64>>(5)?,
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, Option<String>>(7)?,
                )),
            ).optional().unwrap_or(None);
        let (has_copy, slug, width, height, wall_conflict, light_conflict, source_rev, project_changed) =
            match row {
                Some((slug, width, height, current_walls, current_lights, source_rev, base_walls, base_lights)) => {
                    let json_eq = |baseline: Option<&str>, current: &str| -> Option<bool> {
                        let base = serde_json::from_str::<Value>(baseline?).ok()?;
                        let cur = serde_json::from_str::<Value>(current).ok()?;
                        Some(base == cur)
                    };
                    let wc = json_eq(base_walls.as_deref(), &current_walls).map(|same| !same).unwrap_or(true);
                    let lc = json_eq(base_lights.as_deref(), &current_lights).map(|same| !same).unwrap_or(true);
                    let changed = source_rev.map(|saved| saved != revision).unwrap_or(true);
                    (true, Some(slug), width, height, wc, lc, source_rev, changed)
                }
                None => (false, None, 0, 0, false, false, None, true),
            };
        preview.push(json!({
            "id":floor_id, "name":name, "has_copy":has_copy, "slug":slug, "width":width, "height":height,
            "source_rev":source_rev, "copy_rev":revision, "project_changed":project_changed,
            "artwork":project_changed, "geometry":project_changed,
            "wall_conflict":wall_conflict, "light_conflict":light_conflict,
            "preserved":["tokens","fog","combat","character positions"],
        }));
    }
    reply(StatusCode::OK, json!({"project":project, "revision":revision, "floors":preview}))
}

pub fn playable_floors(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Response<Body> {
    let Some((db, page)) = gm_ctx(cfg, "POST", uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    let Ok(data) = serde_json::from_slice::<Value>(body) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid request"}));
    };
    match group_playable_floors(&db, world, &data) {
        Ok(value) => reply(StatusCode::OK, value),
        Err(PlayableFloorError::Invalid(message)) => reply(StatusCode::BAD_REQUEST, json!({"error":message})),
        Err(PlayableFloorError::Database(error)) => {
            eprintln!("nd-gateway: could not link playable floors: {error}");
            reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Could not link playable floors"}))
        }
    }
}

fn project_links(db: &rusqlite::Connection, world_id: i64) -> Result<Value, String> {
    let mut st = db.prepare("SELECT project_id, kind, slug FROM cartographer_links WHERE world_id = ?1 ORDER BY project_id")
        .map_err(|e| e.to_string())?;
    let rows = st.query_map([world_id], |r| Ok(json!({
        "project": r.get::<_, String>(0)?, "kind": r.get::<_, String>(1)?, "slug": r.get::<_, String>(2)?
    }))).map_err(|e| e.to_string())?;
    Ok(Value::Array(rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())?))
}

pub fn remove_project_links(cfg: &Config, project: &str) -> Result<(), String> {
    let db = crate::writes::open(cfg).ok_or("Could not open database")?;
    delete_project_links(&db, project)
}

fn delete_project_links(db: &rusqlite::Connection, project: &str) -> Result<(), String> {
    let tx = db.unchecked_transaction().map_err(|e| e.to_string())?;
    tx.execute("DELETE FROM cartographer_links WHERE project_id = ?1", [project])
        .map_err(|e| e.to_string())?;
    tx.execute("DELETE FROM cartographer_playable WHERE project_id = ?1", [project])
        .map_err(|e| e.to_string())?;
    tx.commit().map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(Debug, PartialEq)]
enum LinkError {
    Invalid(&'static str),
    Conflict,
    Internal(String),
}

fn save_project_link(db: &rusqlite::Connection, cfg: &Config, world_id: i64, data: &Value) -> Result<(), LinkError> {
    let project = data.get("project").and_then(Value::as_str).ok_or(LinkError::Invalid("Project id required"))?;
    if project.len() > 64 || !project.starts_with("doc_")
        || !project.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return Err(LinkError::Invalid("Invalid project id"));
    }
    let kind = data.get("kind").and_then(Value::as_str).ok_or(LinkError::Invalid("Map kind required"))?;
    let slug = data.get("slug").and_then(Value::as_str).ok_or(LinkError::Invalid("Map slug required"))?;
    if workspace_url(kind, slug).is_none() {
        return Err(LinkError::Invalid("Invalid map"));
    }
    let manifest = std::fs::read(data_dir(cfg).join(project).join("manifest.json"))
        .map_err(|e| if e.kind() == std::io::ErrorKind::NotFound { LinkError::Invalid("Project not found") } else { LinkError::Internal(e.to_string()) })?;
    let manifest: Value = serde_json::from_slice(&manifest).map_err(|_| LinkError::Invalid("Invalid project manifest"))?;
    if manifest.get("id").and_then(Value::as_str) != Some(project) {
        return Err(LinkError::Invalid("Invalid project manifest"));
    }
    let exists = match kind {
        "map" => crate::maps::world_maps(db, cfg, world_id)
            .ok_or_else(|| LinkError::Internal("Could not read maps".into()))?.iter().any(|(s, _)| s == slug),
        "schematic" => db.query_row("SELECT EXISTS(SELECT 1 FROM schematics WHERE world_id = ?1 AND slug = ?2)",
            rusqlite::params![world_id, slug], |r| r.get::<_, bool>(0)).map_err(|e| LinkError::Internal(e.to_string()))?,
        _ => false,
    };
    if !exists {
        return Err(LinkError::Invalid("Map not found in this world"));
    }
    let changed = db.execute(
        "INSERT INTO cartographer_links(project_id, world_id, kind, slug) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(project_id) DO UPDATE SET project_id = excluded.project_id
         WHERE world_id = excluded.world_id AND kind = excluded.kind AND slug = excluded.slug",
        rusqlite::params![project, world_id, kind, slug]).map_err(|e| LinkError::Internal(e.to_string()))?;
    if changed == 0 { Err(LinkError::Conflict) } else { Ok(()) }
}

pub fn links(cfg: &Config, uri: &Uri, headers: &HeaderMap, body: Option<&[u8]>) -> Response<Body> {
    let method = if body.is_some() { "POST" } else { "GET" };
    let Some((mut db, page)) = gm_ctx(cfg, method, uri, headers) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"GM access required"}));
    };
    let Some(world_id) = page.world.as_ref().and_then(|w| w.get("id")).and_then(Value::as_i64) else {
        return reply(StatusCode::FORBIDDEN, json!({"error":"Active world required"}));
    };
    if let Some(body) = body {
        let Ok(data) = serde_json::from_slice::<Value>(body) else {
            return reply(StatusCode::BAD_REQUEST, json!({"error":"Invalid request"}));
        };
        let result = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|e| LinkError::Internal(e.to_string()))
            .and_then(|tx| { save_project_link(&tx, cfg, world_id, &data)?; tx.commit().map_err(|e| LinkError::Internal(e.to_string())) });
        return match result {
            Ok(()) => reply(StatusCode::OK, json!({"ok":true})),
            Err(LinkError::Invalid(message)) => reply(StatusCode::BAD_REQUEST, json!({"error":message})),
            Err(LinkError::Conflict) => reply(StatusCode::CONFLICT, json!({"error":"Project is already linked to another map"})),
            Err(LinkError::Internal(message)) => {
                eprintln!("nd-gateway: could not save Map Maker link: {message}");
                reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Could not save project link"}))
            }
        };
    }
    match project_links(&db, world_id) {
        Ok(items) => reply(StatusCode::OK, json!({"links":items})),
        Err(message) => {
            eprintln!("nd-gateway: could not read Map Maker links: {message}");
            reply(StatusCode::INTERNAL_SERVER_ERROR, json!({"error":"Could not read project links"}))
        }
    }
}

fn existing_maps(db: &rusqlite::Connection, cfg: &Config, world_id: i64) -> Option<Value> {
    let mut maps = vec![];
    for (slug, data) in crate::maps::world_maps(db, cfg, world_id)? {
        if workspace_url("map", &slug).is_some() {
            let name = data.get("name").and_then(Value::as_str).unwrap_or(&slug).to_string();
            maps.push(json!({"slug": slug, "name": name, "kind": "map",
                "image_url": crate::maps::map_image(cfg, &slug)?,
                "width": data.get("width").and_then(Value::as_u64).unwrap_or(0),
                "height": data.get("height").and_then(Value::as_u64).unwrap_or(0), "grid_cell": 50, "is_html": false}));
        }
    }
    let mut query = db.prepare("SELECT slug, name, image_url, canvas_width, canvas_height, grid_type, grid_config_json, is_html FROM schematics WHERE world_id = ?1 ORDER BY name, id").ok()?;
    let rows = query.query_map([world_id], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, Option<String>>(2)?, row.get::<_, Option<i64>>(3)?, row.get::<_, Option<i64>>(4)?, row.get::<_, Option<String>>(5)?, row.get::<_, Option<String>>(6)?, row.get::<_, Option<i64>>(7)?))).ok()?;
    for row in rows {
        let (slug, name, image, width, height, grid_type, grid_config, is_html) = row.ok()?;
        if workspace_url("schematic", &slug).is_some() {
            let image_present = image.as_deref().is_some_and(|url| !url.is_empty());
            let image = image.filter(|url| (url.starts_with("/uploads/") || url.starts_with("/static/"))
                && !url.contains('\\') && !url.split('/').any(|part| part == ".."));
            let grid_cell = grid_config.as_deref().and_then(|s| serde_json::from_str::<Value>(s).ok())
                .and_then(|v| v.get("cell_size").and_then(Value::as_f64))
                .filter(|v| *v > 0.0 && grid_type.as_deref() == Some("square"))
                .unwrap_or(50.0);
            maps.push(json!({"slug": slug, "name": name, "kind": "schematic",
                "image_unsupported": image_present && image.is_none(), "image_url": image, "width": width.unwrap_or(0), "height": height.unwrap_or(0), "grid_cell": grid_cell, "is_html": is_html.unwrap_or(0) != 0}));
        }
    }
    Some(Value::Array(maps))
}

/// `GET /cartographer` (the embed page) and `GET /api/cartographer/status`.
pub fn get(
    pages: &Pages,
    cfg: &Config,
    uri: &Uri,
    headers: &HeaderMap,
    is_page: bool,
) -> Option<Response<Body>> {
    let (db, mut p) = gm_ctx(cfg, "GET", uri, headers)?;
    let st = status(cfg);
    if !is_page {
        return Some(reply(StatusCode::OK, st));
    }
    // The studio is mounted on this same origin; the iframe needs no token (the
    // proxy path is GM-gated by the session).
    let world_id = p.world.as_ref()?.get("id")?.as_i64()?;
    match existing_maps(&db, cfg, world_id) {
        Some(maps) => { p.ctx.insert("existing_maps".into(), maps); }
        None => {
            eprintln!("nd-gateway: could not load existing maps for Map Maker world {world_id}");
            p.ctx.insert("existing_maps".into(), json!([]));
            p.ctx.insert("existing_maps_error".into(), json!("Could not load existing maps. Check the server log and map data."));
        }
    }
    match project_links(&db, world_id) {
        Ok(links) => { p.ctx.insert("cartographer_links".into(), links); }
        Err(error) => {
            eprintln!("nd-gateway: could not load Map Maker links: {error}");
            p.ctx.insert("cartographer_links".into(), json!([]));
            p.ctx.insert("cartographer_links_error".into(), json!("Could not load links to existing maps. Check the server log."));
        }
    }
    p.ctx.insert("cartographer".into(), st);
    p.ctx
        .insert("cartographer_embed_url".into(), json!(STUDIO_BASE));
    crate::pages::render_with(pages, "cartographer.html", p.ctx, headers)
}
