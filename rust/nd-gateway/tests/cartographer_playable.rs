use axum::{body::{to_bytes, Body}, http::{header, Request, StatusCode}, Router};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn request(app: &Router, method: &str, path: &str, cookie: &str, content_type: &str, body: Vec<u8>) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
    let response = app.clone().oneshot(Request::builder()
        .method(method).uri(path)
        .header(header::COOKIE, cookie)
        .header(header::CONTENT_TYPE, content_type)
        .header(header::CONTENT_LENGTH, body.len())
        .body(Body::from(body)).unwrap()).await.unwrap();
    let (parts, body) = response.into_parts();
    (parts.status, parts.headers, to_bytes(body, 100 * 1024 * 1024).await.unwrap().to_vec())
}

fn multipart(uvtt: &[u8], project: &str, floor: &str, mode: &str) -> Vec<u8> {
    let mut body = Vec::new();
    for (name, value) in [("name", "Test floor"), ("project", project), ("floor", floor), ("mode", mode)] {
        body.extend_from_slice(format!("--boundary\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").as_bytes());
    }
    body.extend_from_slice(b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"floor.dd2vtt\"\r\nContent-Type: application/json\r\n\r\n");
    body.extend_from_slice(uvtt);
    body.extend_from_slice(b"\r\n--boundary--\r\n");
    body
}

#[tokio::test]
async fn playable_refresh_preserves_gameplay_and_refuses_changed_geometry() {
    let dir = tempfile::tempdir().unwrap();
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    std::env::set_var("ND_PYTHON_FREE", "1");
    std::env::set_var("GM_EMAIL", "gm@example.test");
    std::env::set_var("GM_PASSWORD", "isolated-test-password");
    let cfg = nd_gateway::Config {
        listen: "127.0.0.1:8000".into(),
        static_dir: repo.join("static"),
        templates_dir: repo.join("assets/templates"),
        accel_root: dir.path().to_path_buf(),
        db_path: dir.path().join("test.db"),
        accel_token: String::new(),
        trust_proxy: false,
        rust_routes: ["login", "dice_roll", "live", "pages", "writes", "ai", "mcp", "fans", "auth"]
            .iter().map(|s| s.to_string()).collect(),
        secret_key: Some("isolated-test-key".into()),
    };
    nd_gateway::startup::init_db(&cfg);
    let app = nd_gateway::router(std::sync::Arc::new(cfg.clone()));
    let (status, headers, _) = request(&app, "POST", "/api/login", "", "application/json",
        br#"{"email":"gm@example.test","password":"isolated-test-password"}"#.to_vec()).await;
    assert_eq!(status, StatusCode::OK);
    let cookie = headers.get(header::SET_COOKIE).unwrap().to_str().unwrap().split(';').next().unwrap();
    let (status, _, body) = request(&app, "POST", "/cartographer/studio/api/docs", cookie, "application/json",
        br#"{"name":"Test","kind":"battle"}"#.to_vec()).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let project = serde_json::from_slice::<Value>(&body).unwrap()["id"].as_str().unwrap().to_owned();
    let path = format!("/cartographer/studio/api/docs/{project}");
    let (_, _, body) = request(&app, "GET", &path, cookie, "application/json", vec![]).await;
    let floor = serde_json::from_slice::<Value>(&body).unwrap()["floors"][0]["id"].as_str().unwrap().to_owned();
    let cells: Vec<Value> = (0..5).flat_map(|x| (0..5).map(move |y| json!({"x":x,"y":y}))).collect();
    let ops = json!({"floor":floor,"ops":[
        {"t":"upsert_space","space":{"id":"s","name":"Room","color":"#abcdef","kind":"room"}},
        {"t":"paint_cells","cells":cells,"space":"s"}]});
    let (status, _, body) = request(&app, "POST", &format!("{path}/ops"), cookie, "application/json", serde_json::to_vec(&ops).unwrap()).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let (status, _, _) = request(&app, "POST", &format!("{path}/save"), cookie, "application/json", vec![]).await;
    assert_eq!(status, StatusCode::OK);
    let (status, _, uvtt) = request(&app, "GET", &format!("{path}/export/uvtt?floor={floor}&ppc=32&audience=player"), cookie, "application/json", vec![]).await;
    assert_eq!(status, StatusCode::OK);
    let import = |mode| multipart(&uvtt, &project, &floor, mode);
    let (status, _, body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("create")).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let slug = serde_json::from_slice::<Value>(&body).unwrap()["slug"].as_str().unwrap().to_owned();
    let preview_body = json!({"project":project,"revision":0,"floors":[{"id":floor,"name":"Ground"}]});
    let (status, _, body) = request(&app, "POST", "/api/cartographer/playable-preview", cookie, "application/json", serde_json::to_vec(&preview_body).unwrap()).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let preview: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(preview["floors"][0]["has_copy"], true);
    assert_eq!(preview["floors"][0]["project_changed"], false);
    let db = rusqlite::Connection::open(&cfg.db_path).unwrap();
    let original_walls: String = db.query_row("SELECT walls_json FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    let mut changed_geometry: Value = serde_json::from_slice(&uvtt).unwrap();
    changed_geometry["line_of_sight"].as_array_mut().unwrap().push(json!([{"x":1,"y":1},{"x":2,"y":1}]));
    changed_geometry["lights"] = json!([{"position":{"x":2,"y":2},"range":4,"intensity":1,"color":"ffffffff"}]);
    let changed_geometry = serde_json::to_vec(&changed_geometry).unwrap();
    let (status, _, body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary",
        multipart(&changed_geometry, &project, &floor, "geometry")).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    assert_eq!(serde_json::from_slice::<Value>(&body).unwrap()["geometry_refreshed"], true);
    let updated_walls: String = db.query_row("SELECT walls_json FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&updated_walls).unwrap().as_array().unwrap().len(),
        serde_json::from_str::<Value>(&original_walls).unwrap().as_array().unwrap().len() + 1);
    let updated_lights: String = db.query_row("SELECT lights_json FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&updated_lights).unwrap().as_array().unwrap().len(), 1);
    db.execute("UPDATE schematics SET elements_json = ?1, fog_json = ?2, lights_json = ?3 WHERE slug = ?4",
        rusqlite::params![r#"[{"type":"token","id":"pc"}]"#, r#"{"enabled":true}"#, r#"[{"id":"light"}]"#, slug]).unwrap();
    let before: (String, String, String, String, String) = db.query_row(
        "SELECT elements_json, walls_json, fog_json, lights_json, image_url FROM schematics WHERE slug = ?1", [&slug],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).unwrap();
    let (status, _, _) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary",
        multipart(&uvtt, &project, &floor, "geometry")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _, body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("refresh")).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let after: (String, String, String, String, String) = db.query_row(
        "SELECT elements_json, walls_json, fog_json, lights_json, image_url FROM schematics WHERE slug = ?1", [&slug],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).unwrap();
    assert_eq!((&after.0, &after.1, &after.2, &after.3), (&before.0, &before.1, &before.2, &before.3));
    assert_ne!(after.4, before.4);
    let mut changed_uvtt: Value = serde_json::from_slice(&uvtt).unwrap();
    let mut png = std::io::Cursor::new(Vec::new());
    image::DynamicImage::ImageRgba8(image::RgbaImage::from_pixel(160, 160, image::Rgba([255, 0, 0, 255])))
        .write_to(&mut png, image::ImageFormat::Png).unwrap();
    use base64::Engine;
    changed_uvtt["image"] = json!(base64::engine::general_purpose::STANDARD.encode(png.into_inner()));
    let (status, _, _) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary",
        multipart(&serde_json::to_vec(&changed_uvtt).unwrap(), &project, &floor, "ensure")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    db.execute("UPDATE schematics SET canvas_width = canvas_width + 1 WHERE slug = ?1", [&slug]).unwrap();
    let (status, _, _) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("refresh")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _, body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("create")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(serde_json::from_slice::<Value>(&body).unwrap()["existing"], true);
    let image: String = db.query_row("SELECT image_url FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    assert_eq!(image, after.4);
    db.execute("UPDATE schematics SET canvas_width = canvas_width - 1, image_url = '/uploads/schematics/custom.webp' WHERE slug = ?1", [&slug]).unwrap();
    let (status, _, _) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("refresh")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let image: String = db.query_row("SELECT image_url FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    assert_eq!(image, "/uploads/schematics/custom.webp");
    let (status, _, _) = request(&app, "POST", "/maps/schematic/import-uvtt", "", "multipart/form-data; boundary=boundary", import("refresh")).await;
    assert!(!status.is_success());

    db.execute("UPDATE schematics SET image_url = ?1 WHERE slug = ?2", rusqlite::params![after.4, slug]).unwrap();
    let (status, _, body) = request(&app, "POST", &format!("{path}/floors"), cookie, "application/json",
        br#"{"action":"add","name":"Upper"}"#.to_vec()).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let upper = serde_json::from_slice::<Value>(&body).unwrap()["floor"].as_str().unwrap().to_owned();
    let cells: Vec<Value> = (0..5).flat_map(|x| (0..5).map(move |y| json!({"x":x,"y":y}))).collect();
    let ops = json!({"floor":upper,"ops":[
        {"t":"upsert_space","space":{"id":"s","name":"Room","color":"#abcdef","kind":"room"}},
        {"t":"paint_cells","cells":cells,"space":"s"}]});
    let (status, _, _) = request(&app, "POST", &format!("{path}/ops"), cookie, "application/json", serde_json::to_vec(&ops).unwrap()).await;
    assert_eq!(status, StatusCode::OK);
    let (status, _, upper_uvtt) = request(&app, "GET", &format!("{path}/export/uvtt?floor={upper}&ppc=32&audience=player"), cookie, "application/json", vec![]).await;
    assert_eq!(status, StatusCode::OK);
    let (status, _, upper_body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", multipart(&upper_uvtt, &project, &upper, "ensure")).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&upper_body));
    let upper_data: Value = serde_json::from_slice(&upper_body).unwrap();
    let (status, _, first_body) = request(&app, "POST", "/maps/schematic/import-uvtt", cookie, "multipart/form-data; boundary=boundary", import("ensure")).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&first_body));
    let first_data: Value = serde_json::from_slice(&first_body).unwrap();
    let mut group = json!({"project":project,"floors":[
        {"id":floor,"name":"Ground","width":first_data["width"],"height":first_data["height"],"image_url":first_data["image_url"],"links":[
            {"id":"stairs","label":"Up","destination":upper,"x":32,"y":64,"destination_x":96,"destination_y":128}]},
        {"id":upper,"name":"Upper","width":upper_data["width"],"height":upper_data["height"],"image_url":upper_data["image_url"],"links":[]}]});
    group["floors"][0]["links"][0]["destination_x"] = json!(9999);
    let (status, _, _) = request(&app, "POST", "/api/cartographer/playable-floors", cookie, "application/json", serde_json::to_vec(&group).unwrap()).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(db.query_row("SELECT COUNT(*) FROM schematic_floors", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
    group["floors"][0]["links"][0]["destination_x"] = json!(96);
    let (status, _, body) = request(&app, "POST", "/api/cartographer/playable-floors", cookie, "application/json", serde_json::to_vec(&group).unwrap()).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    assert_eq!(db.query_row("SELECT COUNT(*) FROM schematic_floors", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    let elements: String = db.query_row("SELECT elements_json FROM schematics WHERE slug = ?1", [&slug], |r| r.get(0)).unwrap();
    let elements: Vec<Value> = serde_json::from_str(&elements).unwrap();
    assert_eq!(elements[0]["id"], "pc");
    assert_eq!(elements[1]["destination_slug"], upper_data["slug"]);
    assert_eq!(nd_gateway::map_floors::portal_destination(&elements, 42, 0.0, 0.0, 32.0, 64.0, 40.0),
        Some((upper_data["slug"].as_str().unwrap().to_string(), 96.0, 128.0)));
    let (status, _, _) = request(&app, "POST", "/api/cartographer/playable-floors", "", "application/json", serde_json::to_vec(&group).unwrap()).await;
    assert!(!status.is_success());
}
