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
pub fn init_db(cfg: &Config) {
    if !needs_boot(cfg) {
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
