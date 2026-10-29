//! Database startup for a gateway-only container (batch H, docs/RUST_SWITCH_OFF.md step 4): when the database file
//! doesn't exist (or has no tables), run the post-migration schema snapshot ("schema_sql" in app_data.json — dumped
//! from a fresh Python boot, never retyped) and the seed rows (database.py's _seed: world 1, the bundled HTML
//! schematics, the built-in sheet / entity templates, the starter tables, the GM account from GM_EMAIL / GM_PASSWORD).
//! Idempotent — a database Python already made is left alone (its create_all / _migrate / _seed are, too).

use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::types::Value as Sql;
use serde_json::{json, Value};

use crate::config::Config;

/// The app's data tables (nd-render's app_data.json)
fn data() -> &'static Value {
    static D: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    D.get_or_init(|| nd_render::app::data().clone())
}

/// True when the database file is missing or has no user tables (a fresh container's volume)
fn needs_boot(cfg: &Config) -> bool {
    if !cfg.db_path.exists() {
        return true;
    }
    let Ok(db) = rusqlite::Connection::open(&cfg.db_path) else { return false };
    let n: i64 = db.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'", [], |r| r.get(0)).unwrap_or(1);
    n == 0
}

/// The tables (in app_data.json order — dependencies first), then the indexes / FTS tables / triggers
fn run_schema(db: &rusqlite::Connection) -> Option<()> {
    let stmts = data().get("schema_sql")?.as_array()?;
    if stmts.is_empty() {
        eprintln!("nd-gateway: database boot: schema_sql is empty (app_data.json not loaded?)");
        return None;
    }
    for (i, st) in stmts.iter().enumerate() {
        let Some(text) = st.as_str() else { continue };
        if let Err(e) = db.execute_batch(text) {
            eprintln!("nd-gateway: database boot: statement {i} failed: {e} (was: {})", text.lines().next().unwrap_or(""));
            return None;
        }
    }
    Some(())
}

/// `_seed` (app/database.py): the starter rows. Every insert is guarded by a check, exactly as Python's is, so a
/// database Python (or a previous boot) already seeded is left alone.
fn seed(db: &rusqlite::Connection, cfg: &Config) -> Result<(), String> {
    let d = data();
    // world 1
    if !row_exists(db, "worlds", "slug", "neon-dragons") {
        crate::orm::insert(db, "worlds", &[
            ("id", Sql::Integer(1)),
            ("name", Sql::Text("Neon & Dragons".into())),
            ("slug", Sql::Text("neon-dragons".into())),
            ("description", Sql::Text("Cyberpunk-fantasy dystopia where megacorps and eldritch corruption collide.".into())),
            ("accent", Sql::Text("#00f0ff".into())),
        ]).ok_or_else(|| "worlds insert".to_string())?;
    }
    // the bundled HTML schematics
    for (name, slug, desc, html_file) in [
        ("City of Hue", "city-of-hue", "The sprawling neon-soaked City of Hue.", "city-of-hue-complete.html"),
        ("Hughes Station", "hughes-station", "Hughes Station schematic.", "hughes-station-schematic.html"),
    ] {
        if row_exists(db, "schematics", "slug", slug) {
            continue;
        }
        crate::orm::insert(db, "schematics", &[
            ("world_id", Sql::Integer(1)),
            ("name", Sql::Text(name.into())),
            ("slug", Sql::Text(slug.into())),
            ("description", Sql::Text(desc.into())),
            ("is_html", Sql::Integer(1)),
            ("html_file", Sql::Text(html_file.into())),
        ]).ok_or_else(|| format!("schematics insert {slug}"))?;
    }
    // the built-in sheet templates (their field blobs come straight from the dumped seed data)
    let sheets: [(&str, &str, &str, &str, &str); 3] = [
        ("Neon & Dragons — Default", "nd-default",
         "Standard N&D character sheet: 8 stats, HP/Shock/PP/MP, edges, cyberware, conditions, feats.", "nd",
         "seed_nd_default_fields"),
        ("Asterion", "asterion",
         "City of Nine Thousand Shrines — Gods & Mythborn dice-pool system (Spark Shield/Flesh/Ichor, Glory, Domain Reclamation).",
         "custom", "seed_asterion_fields"),
        ("Hunt in the Moonlight", "hunt-in-the-moonlight",
         "Hunter Ledger — moon-corruption hunter game: Health/Stamina/Hunger/Arcane resource tracks, Moon Phase & Color, Hunter's Oath prompts, and gear/rites/mod tables.",
         "custom", "seed_hitm_fields"),
    ];
    for (name, slug, desc, mode, blob) in sheets {
        if row_exists(db, "sheet_templates", "slug", slug) {
            continue;
        }
        let fields = d.get(blob).and_then(|v| v.as_str()).ok_or_else(|| format!("{blob} missing"))?.to_string();
        crate::orm::insert(db, "sheet_templates", &[
            ("world_id", Sql::Null),
            ("name", Sql::Text(name.into())),
            ("slug", Sql::Text(slug.into())),
            ("description", Sql::Text(desc.into())),
            ("is_builtin", Sql::Integer(1)),
            ("sheet_mode", Sql::Text(mode.into())),
            ("fields_json", Sql::Text(fields)),
        ]).ok_or_else(|| format!("sheet_templates insert {slug}"))?;
    }
    // the built-in entity field templates
    for (name, slug, kind, desc, blob) in [
        ("NPC Details", "npc-details", "character", "Title, age, gender, and alive/dead/missing status for an NPC.", "seed_npc_details_fields"),
        ("Stat Block", "stat-block", "", "Quick combat stats — attack/defense pools, health, armor, speed, abilities. Usable on characters, creatures, or anything else that fights.", "seed_stat_block_fields"),
    ] {
        if row_exists(db, "entity_templates", "slug", slug) {
            continue;
        }
        let fields = d.get(blob).and_then(|v| v.as_str()).ok_or_else(|| format!("{blob} missing"))?.to_string();
        crate::orm::insert(db, "entity_templates", &[
            ("world_id", Sql::Null),
            ("name", Sql::Text(name.into())),
            ("slug", Sql::Text(slug.into())),
            ("kind", if kind.is_empty() { Sql::Null } else { Sql::Text(kind.into()) }),
            ("description", Sql::Text(desc.into())),
            ("is_builtin", Sql::Integer(1)),
            ("fields_json", Sql::Text(fields)),
        ]).ok_or_else(|| format!("entity_templates insert {slug}"))?;
    }
    // the starter tables
    for (name, slug, cat, desc, entries) in [
        ("Loot Rarity", "loot-rarity", "loot", "Quick rarity roll for a found item.",
         json!([{"label": "Common", "weight": 50}, {"label": "Uncommon", "weight": 30}, {"label": "Rare", "weight": 15}, {"label": "Legendary", "weight": 5}])),
        ("Random Encounter", "random-encounter", "encounters", "A d20 roll for what the crew runs into.",
         json!([{"label": "Signs of danger ahead (tracks, wreckage, warnings)", "weight": 20}, {"label": "Hostile encounter", "weight": 20}, {"label": "Friendly or neutral encounter", "weight": 15}, {"label": "Rare/notable event", "weight": 5}])),
    ] {
        if row_exists(db, "random_tables", "slug", slug) {
            continue;
        }
        crate::orm::insert(db, "random_tables", &[
            ("world_id", Sql::Null),
            ("name", Sql::Text(name.into())),
            ("slug", Sql::Text(slug.into())),
            ("category", Sql::Text(cat.into())),
            ("description", Sql::Text(desc.into())),
            ("is_builtin", Sql::Integer(1)),
            ("entries_json", Sql::Text(nd_render::py_dumps(&entries))),
        ]).ok_or_else(|| format!("random_tables insert {slug}"))?;
    }
    // the GM account from GM_EMAIL / GM_PASSWORD, when no GM exists yet (an existing non-GM row with that email is
    // left alone, as Python's seeder logs)
    let gm_email = std::env::var("GM_EMAIL").ok().map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty());
    let gm_password = std::env::var("GM_PASSWORD").ok().filter(|s| !s.is_empty());
    if let (Some(email), Some(password)) = (gm_email, gm_password) {
        let any_gm: bool = db.query_row("SELECT 1 FROM users WHERE is_gm = 1 LIMIT 1", [], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
        if !any_gm {
            let exists: bool = db.query_row("SELECT 1 FROM users WHERE email = ?1", [&email], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
            if !exists {
                let display = std::env::var("GM_NAME").unwrap_or_else(|_| "GM".into());
                crate::orm::insert(db, "users", &[
                    ("email", Sql::Text(email)),
                    ("password_hash", Sql::Text(crate::account::hash_password_pub(&password))),
                    ("display_name", Sql::Text(display)),
                    ("is_gm", Sql::Integer(1)),
                ]).ok_or_else(|| "users insert".to_string())?;
            }
        }
    }
    Ok(())
}

fn row_exists(db: &rusqlite::Connection, table: &str, col: &str, val: &str) -> bool {
    db.query_row(&format!("SELECT 1 FROM {table} WHERE {col} = ?1 LIMIT 1"), [val], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false)
}

/// The boot: outside a transaction on purpose (SQLite's transactional DDL makes each statement safe, and a mixed
/// schema+data transaction over 250 statements is where the failure modes live). Runs once per process.
/// The top-level comma-separated pieces of a CREATE TABLE body (commas inside VARCHAR(256) and the like stay put)
fn split_top(body: &str) -> Vec<String> {
    let (mut out, mut depth, mut cur) = (vec![], 0i32, String::new());
    for ch in body.chars() {
        match ch {
            '(' => { depth += 1; cur.push(ch) }
            ')' => { depth -= 1; cur.push(ch) }
            ',' if depth == 0 => out.push(std::mem::take(&mut cur)),
            _ => cur.push(ch),
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur);
    }
    out.into_iter().map(|s| s.trim().to_string()).collect()
}

/// `DEFAULT <literal>` for a model column that has a plain default (Python's `_render_default`): none for a callable (`__utcnow__`)
fn default_clause(table: &str, col: &str) -> String {
    match data().get("column_defaults").and_then(|d| d.get(table)).and_then(|t| t.get(col)).and_then(|c| c.get("default")) {
        Some(Value::Bool(b)) => format!(" DEFAULT {}", *b as i64),
        Some(Value::Number(n)) => format!(" DEFAULT {n}"),
        Some(Value::String(s)) if s != "__utcnow__" => format!(" DEFAULT '{}'", s.replace('\'', "''")),
        _ => String::new(),
    }
}

/// What `create_all` + `_migrate` + `_heal_table_from_model` do to a database that is already there, for the additive part: a table the
/// schema snapshot has and the file lacks is created, a column it lacks is added (nullable, with the model's plain default, as
/// `_columns_from_model` renders it), and the indexes / triggers it lacks are created. Rebuilds of tables whose NOT NULL flags drifted and
/// the one-time data fixes of `_migrate` are not repeated here: they ran when the Python release that preceded the switch last booted.
/// Returns what it changed (for the log).
pub(crate) fn heal_schema(db: &rusqlite::Connection) -> Result<Vec<String>, String> {
    let stmts = data().get("schema_sql").and_then(|v| v.as_array()).ok_or("schema_sql missing")?;
    let have = |kind: &str, name: &str| -> bool {
        db.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type = ?1 AND name = ?2", [kind, name], |r| r.get::<_, i64>(0)).unwrap_or(0) > 0
    };
    let mut done = vec![];
    for st in stmts.iter().filter_map(|s| s.as_str()) {
        let head: Vec<&str> = st.split_whitespace().take(5).collect();
        let upper: Vec<String> = head.iter().map(|w| w.to_ascii_uppercase()).collect();
        let name_at = |i: usize| head.get(i).map(|n| n.trim_matches(|c| c == '"' || c == '`' || c == '(').to_string()).unwrap_or_default();
        if upper.starts_with(&["CREATE".into(), "TABLE".into()]) {
            let table = name_at(2);
            if !have("table", &table) {
                db.execute_batch(st).map_err(|e| format!("create table {table}: {e}"))?;
                done.push(format!("created table {table}"));
                continue;
            }
            let (Some(open), Some(close)) = (st.find('('), st.rfind(')')) else { continue };
            let existing: Vec<String> = {
                let mut q = db.prepare(&format!("PRAGMA table_info({table})")).map_err(|e| e.to_string())?;
                let rows = q.query_map([], |r| r.get::<_, String>(1)).map_err(|e| e.to_string())?;
                rows.filter_map(|r| r.ok()).collect()
            };
            for item in split_top(&st[open + 1..close]) {
                let first = item.split_whitespace().next().unwrap_or("").to_ascii_uppercase();
                if matches!(first.as_str(), "PRIMARY" | "FOREIGN" | "UNIQUE" | "CHECK" | "CONSTRAINT" | "") {
                    continue;
                }
                let col = item.split_whitespace().next().unwrap_or("").trim_matches(|c| c == '"' || c == '`').to_string();
                if existing.iter().any(|c| *c == col) || item.to_ascii_uppercase().contains("PRIMARY KEY") {
                    continue;
                }
                let ty = item[item.find(char::is_whitespace).unwrap_or(item.len())..].trim();
                let ty = ty.replace(" NOT NULL", "").replace("NOT NULL", "");
                db.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {col} {}{}", ty.trim(), default_clause(&table, &col)))
                    .map_err(|e| format!("add column {table}.{col}: {e}"))?;
                done.push(format!("added column {table}.{col}"));
            }
        } else if upper.first().map(String::as_str) == Some("CREATE") && (upper.get(1).map(String::as_str) == Some("INDEX") || (upper.get(1).map(String::as_str) == Some("UNIQUE") && upper.get(2).map(String::as_str) == Some("INDEX"))) {
            let name = name_at(if upper[1] == "INDEX" { 2 } else { 3 });
            if !have("index", &name) {
                db.execute_batch(st).map_err(|e| format!("create index {name}: {e}"))?;
                done.push(format!("created index {name}"));
            }
        } else if upper.starts_with(&["CREATE".into(), "TRIGGER".into()]) {
            let name = name_at(2);
            if !have("trigger", &name) {
                db.execute_batch(st).map_err(|e| format!("create trigger {name}: {e}"))?;
                done.push(format!("created trigger {name}"));
            }
        }
    }
    Ok(done)
}

pub fn init_db(cfg: &Config) {
    if !needs_boot(cfg) {
        // the additive schema heal is for a container with no Python in it (ND_PYTHON_FREE=1): next to a Python app both would run DDL against
        // the same file at boot, and Python's own create_all / _migrate already does this work
        if !matches!(std::env::var("ND_PYTHON_FREE").unwrap_or_default().trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on") {
            return;
        }
        if let Some(db) = crate::writes::open(cfg) {
            match db.execute_batch("BEGIN").map_err(|e| e.to_string()).and_then(|_| heal_schema(&db)) {
                Ok(done) => {
                    let _ = db.execute_batch("COMMIT");
                    if !done.is_empty() {
                        eprintln!("nd-gateway: database heal: {}", done.join(", "));
                    }
                }
                Err(e) => {
                    let _ = db.execute_batch("ROLLBACK");
                    eprintln!("nd-gateway: database heal failed ({e}); left as it was");
                }
            }
        }
        return;
    }
    // the file may not exist yet (writes::open won't create it — neither does Python's engine on connect, its
    // create_all does): an empty file is the same starting point Python's first boot has
    if !cfg.db_path.exists() {
        if let Some(parent) = cfg.db_path.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        if let Ok(c) = rusqlite::Connection::open(&cfg.db_path) { let _ = c.close(); }
    }
    let Some(db) = crate::writes::open(cfg) else { return };
    if run_schema(&db).is_none() {
        eprintln!("nd-gateway: database boot: the schema snapshot failed; Python (if running) will make the schema");
        return;
    }
    if let Err(e) = seed(&db, cfg) {
        eprintln!("nd-gateway: database boot: seeding failed at {e} (the schema is in place)");
        return;
    }
    eprintln!("nd-gateway: booted a fresh database (schema + seed rows) at {}", cfg.db_path.display());
}

/// The boot sweeps (each engine's `sweep_interrupted_jobs`): any job still mid-flight when the process last stopped
/// uncleanly has no background task in THIS one, so it's marked "interrupted" — the same status a clean shutdown
/// leaves a paused job in. Python's resume_interrupted_jobs re-runs them through its engines; the gateway's own audio
/// runner resumes its audio jobs the same way below.
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

pub fn sweep_interrupted(cfg: &Config) {
    let Some(db) = crate::writes::open(cfg) else { return };
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok().and_then(|d| {
        let secs = d.as_secs();
        let days = secs.div_euclid(86400) as i64;
        let rem = secs.rem_euclid(86400);
        let (y, m, d) = civil_from_days(days);
        Some(format!("{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}.000000", rem / 3600, rem % 3600 / 60, rem % 60))
    });
    let Some(stamp) = now else { return };
    let note = |cur: Option<i64>, total: Option<i64>| -> String {
        match (cur, total) {
            (Some(c), Some(t)) if c > 0 && t > 0 => format!(
                "Paused by a server restart at part {c} of {t} — the work so far is saved; it will resume automatically, or use ▶ Resume."),
            _ => "Paused by a server restart — the work so far is saved; it will resume automatically, or use ▶ Resume.".to_string(),
        }
    };
    // (table, in-progress statuses, note text, with_chunks) — each engine's own sweep fields: audio_jobs carries
    // chunk progress and a finished_at stamp; the others have their own note and no finished_at column
    let engines: [(&str, &[&str], &str, bool); 5] = [
        ("audio_jobs", &["pending", "transcribing", "summarizing"],
         "", true),
        ("image_jobs", &["pending", "generating"],
         "Paused by a server restart — it will restart automatically from the same request.", false),
        ("chat_jobs", &["pending", "generating"],
         "Paused by a server restart — it will restart automatically from the same request.", false),
        ("video_jobs", &["pending", "generating", "fetching"],
         "Paused by a server restart — it will restart automatically from the same request.", false),
        ("media_rename_runs", &["running"],
         "Paused by a server restart — it will resume automatically from the same items.", false),
    ];
    for (table, statuses, plain_note, has_chunks) in engines {
        let in_list: Vec<String> = statuses.iter().map(|s| format!("'{s}'")).collect();
        let sql = if has_chunks {
            format!("SELECT id, chunk_current, chunk_total FROM {table} WHERE status IN ({})", in_list.join(", "))
        } else {
            format!("SELECT id, NULL, NULL FROM {table} WHERE status IN ({})", in_list.join(", "))
        };
        let Ok(mut st) = db.prepare(&sql) else { continue };
        let rows: Vec<(i64, Option<i64>, Option<i64>)> =
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()
                .map(|rows| rows.filter_map(|r| r.ok()).collect()).unwrap_or_default();
        drop(st);
        let rows_len = rows.len();
        if rows_len == 0 {
            continue;
        }
        for (id, cur, total) in rows {
            let error_text = if has_chunks { note(cur, total) } else { plain_note.to_string() };
            let mut set = vec![
                ("status", Sql::Text("interrupted".into())),
                ("error", Sql::Text(error_text)),
            ];
            if has_chunks {
                set.push(("finished_at", Sql::Text(stamp.clone())));
            }
            crate::orm::update(&db, table, id, &set);
        }
        eprintln!("nd-gateway: marked {rows_len} interrupted {table} job(s)");
    }
}

/// `db_upkeep.start` (app/db_upkeep.py): PRAGMA optimize once a day (an hour after boot), plus a one-way switch to
/// incremental auto-vacuum when more than a quarter of the file is free pages, releasing up to 25 000 pages a day.
pub fn db_upkeep(cfg: &Config) {
    let cfg = cfg.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_secs(60 * 60)); // FIRST_DELAY: not while the server boots
        loop {
            if let Ok(con) = rusqlite::Connection::open(&cfg.db_path) {
                let _ = con.busy_timeout(std::time::Duration::from_secs(30));
                let _ = con.execute_batch("PRAGMA optimize");
                let pages: i64 = con.query_row("PRAGMA page_count", [], |r| r.get(0)).unwrap_or(1).max(1);
                let free: i64 = con.query_row("PRAGMA freelist_count", [], |r| r.get(0)).unwrap_or(0);
                let mode: i64 = con.query_row("PRAGMA auto_vacuum", [], |r| r.get(0)).unwrap_or(0);
                if mode == 2 {
                    if free > 0 {
                        let _ = con.execute_batch(&format!("PRAGMA incremental_vacuum({})", free.min(25000)));
                    }
                } else if (free as f64 / pages as f64) > 0.25 {
                    let _ = con.execute_batch("PRAGMA auto_vacuum=INCREMENTAL; VACUUM;");
                }
            }
            std::thread::sleep(std::time::Duration::from_secs(24 * 60 * 60));
        }
    });
}

/// `_backups.start` (app/backups.py): when ND_BACKUP_DIR is configured, snapshot the database once per interval
/// (ND_BACKUP_INTERVAL_SECONDS, at least 60) via VACUUM INTO, keeping ND_BACKUP_KEEP snapshots. A no-op when
/// unconfigured — an unconfigured deployment must not grow surprise writes.
pub fn backups(cfg: &Config) {
    let dir = match std::env::var("ND_BACKUP_DIR") {
        Ok(d) if !d.trim().is_empty() => std::path::PathBuf::from(d),
        _ => return,
    };
    let interval: u64 = std::env::var("ND_BACKUP_INTERVAL_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(86400).max(60);
    let keep: usize = std::env::var("ND_BACKUP_KEEP").ok().and_then(|v| v.parse().ok()).unwrap_or(7);
    let cfg = cfg.clone();
    std::thread::spawn(move || {
        // wait-first: the DB was fine a moment ago; the first snapshot matters after a full interval of use
        std::thread::sleep(std::time::Duration::from_secs(interval));
        loop {
            let _ = run_backup_once(&cfg, &dir, keep);
            std::thread::sleep(std::time::Duration::from_secs(interval));
        }
    });
}

/// `create_snapshot` + `run_backup_once`: VACUUM INTO a `world-<stamp>.db` snapshot (a same-second collision gets
/// a -2 suffix), then prune to the newest `keep`
fn run_backup_once(cfg: &Config, dir: &std::path::Path, keep: usize) -> Option<std::path::PathBuf> {
    let _ = std::fs::create_dir_all(dir);
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?;
    let secs = now.as_secs();
    let (y, m, d) = civil_from_days(secs.div_euclid(86400) as i64);
    let rem = secs.rem_euclid(86400);
    let stamp = format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, rem % 3600 / 60, rem % 60);
    let mut candidate = dir.join(format!("world-{stamp}.db"));
    let mut n = 1;
    while candidate.exists() {
        candidate = dir.join(format!("world-{stamp}-{n}.db"));
        n += 1;
    }
    let raw = rusqlite::Connection::open(&cfg.db_path).ok()?;
    let result = raw.execute("VACUUM INTO ?1", [candidate.to_string_lossy().to_string()]);
    let _ = raw.close();
    result.ok()?;
    prune_snapshots(dir, keep);
    Some(candidate)
}

/// `prune_snapshots`: keep the newest `keep` world-*.db snapshots (name-sorted, newest first)
fn prune_snapshots(dir: &std::path::Path, keep: usize) {
    let mut snaps: Vec<std::path::PathBuf> = match std::fs::read_dir(dir) {
        Ok(entries) => entries.filter_map(|e| e.ok()).map(|e| e.path())
            .filter(|p| p.is_file() && p.file_name().and_then(|n| n.to_str()).map_or(false, |n| n.starts_with("world-") && n.ends_with(".db")))
            .collect(),
        Err(_) => return,
    };
    let mut names: Vec<String> = snaps.iter().filter_map(|p| p.file_name().and_then(|n| n.to_str()).map(str::to_string)).collect();
    names.sort();
    names.reverse();
    for name in names.iter().skip(keep.max(0)) {
        let _ = std::fs::remove_file(dir.join(name));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heal_adds_what_is_missing_and_leaves_the_rest() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        // an old database: app_settings without the newer columns, no api_tokens table at all
        db.execute_batch("CREATE TABLE app_settings (id INTEGER NOT NULL, static_format VARCHAR(16), PRIMARY KEY (id)); INSERT INTO app_settings (id, static_format) VALUES (1, 'webp');").unwrap();
        let done = heal_schema(&db).unwrap();
        assert!(done.iter().any(|d| d == "created table api_tokens"), "{done:?}");
        assert!(done.iter().any(|d| d == "added column app_settings.hover_preview_delay_ms"), "{done:?}");
        // the row survives and an added column takes the model's plain default
        let (fmt, delay): (String, i64) = db.query_row("SELECT static_format, hover_preview_delay_ms FROM app_settings WHERE id = 1", [], |r| Ok((r.get(0)?, r.get(1)?))).unwrap();
        assert_eq!((fmt.as_str(), delay), ("webp", 5000));
        // a second pass has nothing left to do
        assert!(heal_schema(&db).unwrap().is_empty());
    }
}
