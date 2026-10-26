
/// the civil date of a days-since-epoch count (Howard Hinnant's civil_from_days)
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

/// `GET /admin/backup.zip` (app/main.py `admin_backup`): the whole instance - a VACUUM INTO snapshot of the database,
/// a manifest with the table counts, every uploaded file and every map json - zipped. GM-only. The filename and the
/// manifest's created_at are the build moment; the parity rule compares the entries with those normalized.
pub fn admin_backup(pages: &crate::pages::Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?;
    let p = crate::layout::build(&db, cfg, "GET", uri, headers, now.as_secs())?;
    if !p.is_gm {
        return None; // the auth gate's refusal: Python's
    }
    // datetime.now(timezone.utc).isoformat(): microseconds when there are any, the +00:00 offset
    let iso = |t: std::time::Duration| {
        let secs = t.as_secs();
        let micros = t.subsec_micros();
        let (y, m, d) = civil_from_days(secs.div_euclid(86400));
        let rem = secs.rem_euclid(86400);
        if micros == 0 {
            format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
        } else {
            format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{micros:06}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
        }
    };
    let count = |table: &str| -> Option<i64> { db.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0)).ok() };
    let mut manifest = Map::new();
    manifest.insert("app".into(), json!("nd-world"));
    manifest.insert("created_at".into(), json!(iso(now)));
    let mut tables = Map::new();
    for t in ["worlds", "entities", "users", "world_memberships", "invite_codes", "player_characters", "combat_sessions",
              "quests", "parties", "game_sessions", "schematics", "map_overlays", "invest_boards", "private_notes",
              "entity_notes", "entity_templates"] {
        tables.insert(t.into(), json!(count(t)?));
    }
    manifest.insert("tables".into(), Value::Object(tables));
    // the snapshot: VACUUM INTO a scratch file, zipped then removed
    let snap = cfg.db_path.parent()?.join("uploads").join("_gateway").join(format!("backup-{}-{:x}.db", std::process::id(), now.as_nanos()));
    if let Some(dir) = snap.parent() {
        std::fs::create_dir_all(dir).ok()?;
    }
    {
        let raw = rusqlite::Connection::open(&cfg.db_path).ok()?;
        raw.execute("VACUUM INTO ?1", [snap.to_string_lossy().to_string()]).ok()?;
    }
    let uploads = cfg.db_path.parent()?.join("uploads");
    let maps_dir = crate::maps::maps_dir(cfg)?;
    let mut buf = std::io::Cursor::new(Vec::new());
    {
        let mut zf = zip::ZipWriter::new(&mut buf);
        let opts: zip::write::SimpleFileOptions = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        zf.start_file("world.db", opts).ok()?;
        zf.write_all(&std::fs::read(&snap).ok()?).ok()?;
        zf.start_file("manifest.json", opts).ok()?;
        zf.write_all(nd_render::py_dumps_indent_as(&Value::Object(manifest), 2, true).as_bytes()).ok()?;
        let walk = |zf: &mut zip::ZipWriter<&mut std::io::Cursor<Vec<u8>>>, root: &std::path::Path, prefix: &str| -> Option<()> {
            if !root.exists() {
                return Some(());
            }
            let mut stack = vec![root.to_path_buf()];
            while let Some(dir) = stack.pop() {
                let mut entries: Vec<std::path::PathBuf> = std::fs::read_dir(&dir).ok()?.filter_map(|e| e.ok()).map(|e| e.path()).collect();
                entries.sort();
                for e in entries {
                    if e.is_dir() {
                        stack.push(e);
                    } else if e.is_file() {
                        let rel = e.strip_prefix(root).ok()?;
                        zf.start_file(format!("{prefix}/{}", rel.to_string_lossy()), opts).ok()?;
                        zf.write_all(&std::fs::read(&e).ok()?).ok()?;
                    }
                }
            }
            Some(())
        };
        walk(&mut zf, &uploads, "uploads")?;
        walk(&mut zf, &maps_dir, "maps")?;
        zf.finish().ok()?;
    }
    let _ = std::fs::remove_file(&snap);
    // datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    let (y, m, d) = civil_from_days(now.as_secs().div_euclid(86400));
    let rem = now.as_secs().rem_euclid(86400);
    let stamp = format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, rem % 3600 / 60, rem % 60);
    let _ = pages;
    Response::builder()
        .status(axum::http::StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/zip")
        .header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"nd-world-backup-{stamp}.zip\""))
        .header("x-nd-served-by", "gateway")
        .body(Body::from(buf.into_inner()))
        .ok()
}
