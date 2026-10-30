//! Full Backup restore (app/main.py `admin_backup_restore_stage` / `_cancel`, app/database.py `_apply_staged_restore`): an uploaded backup zip is
//! checked and extracted to <data>/restore_staging, and the NEXT start swaps it in. In an image with no Python the gateway does that swap itself.

use std::io::Read;
use std::path::{Path, PathBuf};

use axum::body::Body;
use axum::http::{Response, StatusCode};

use crate::config::Config;
use crate::edits::{redirect, Ctx};

const MAX_ENTRIES: usize = 20000;
const MAX_UNCOMPRESSED: u64 = 8 * 1024 * 1024 * 1024;

fn staging_dir(cfg: &Config) -> Option<PathBuf> {
    Some(cfg.db_path.parent()?.join("restore_staging"))
}

fn bad(msg: &str) -> Option<Response<Body>> {
    Some(crate::ai_more::detail(StatusCode::BAD_REQUEST, msg))
}

/// The path an entry lands on inside the staging folder, as Python's `(STAGING / name).resolve()` + `is_relative_to` leave it
/// (None: skipped because it would escape the folder, or because it names the folder itself)
fn staged_rel(name: &str) -> Option<PathBuf> {
    if name.starts_with('/') {
        return None;
    }
    let mut parts: Vec<&str> = vec![];
    for seg in name.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                parts.pop()?;
            }
            s => parts.push(s),
        }
    }
    (!parts.is_empty()).then(|| parts.iter().collect())
}

/// `POST /admin/backup/restore`: GM only; the active world's name typed to confirm, a Full Backup zip checked and staged
pub fn stage(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let staging = staging_dir(c.cfg)?;
    let up = c.upload.as_ref()?;
    let mut files = up.files.iter().filter(|p| p.name == "file");
    let part = files.next()?;
    if files.next().is_some() || up.form.has("file") || part.filename.is_empty() {
        return None;
    }
    let confirm = up.form.get("confirm_name")?.to_string();
    let expected = match &c.p.world {
        Some(w) => w.get("name").and_then(|v| v.as_str())?.to_string(),
        None => "RESTORE".to_string(),
    };
    if !expected.is_ascii() || !confirm.is_ascii() {
        return None;
    }
    if crate::edits::py_strip_pub(&confirm) != expected {
        return bad(&format!("Type \"{expected}\" exactly to confirm."));
    }
    let scratch = c.uploads_scratch()?;
    let tmp_db = scratch.with_extension("db");
    let result = stage_zip(c, part, &scratch, &tmp_db, &staging);
    let _ = std::fs::remove_file(&scratch);
    let _ = std::fs::remove_file(&tmp_db);
    for ext in ["-wal", "-shm", "-journal"] {
        let _ = std::fs::remove_file(format!("{}{ext}", tmp_db.display()));
    }
    result
}

fn stage_zip(c: &Ctx, part: &crate::uploads::FileRef, scratch: &Path, tmp_db: &Path, staging: &Path) -> Option<Response<Body>> {
    c.upload.as_ref()?.copy_part(part, scratch)?;
    let file = std::fs::File::open(scratch).ok()?;
    let mut zf = match zip::ZipArchive::new(file) {
        Ok(z) => z,
        Err(_) => {
            // no end-of-central-directory record anywhere near the end is what Python calls BadZipFile; any other refusal is Python's to explain
            let bytes = std::fs::read(scratch).ok()?;
            let tail = &bytes[bytes.len().saturating_sub(65_557)..];
            return if tail.windows(4).any(|w| w == b"PK\x05\x06") { None } else { bad("Not a valid zip file") };
        }
    };
    if zf.len() > MAX_ENTRIES {
        return bad(&format!("Backup zip has too many entries (>{MAX_ENTRIES})"));
    }
    let mut names: Vec<String> = Vec::with_capacity(zf.len());
    let mut total: u64 = 0;
    for i in 0..zf.len() {
        let f = zf.by_index_raw(i).ok()?;
        total = total.saturating_add(f.size());
        names.push(f.name().to_string());
    }
    if total > MAX_UNCOMPRESSED {
        return bad("Backup zip decompresses to more than 8 GiB \u{2014} not restorable in place");
    }
    let mut seen = std::collections::HashSet::new();
    if !names.iter().all(|n| seen.insert(n.as_str())) || names.iter().any(|n| n.contains('\0')) {
        return None; // duplicate entries: Python reads the last one, the zip reader the first
    }
    if !names.iter().any(|n| n == "world.db") || !names.iter().any(|n| n == "manifest.json") {
        return bad("Doesn't look like a Full Backup \u{2014} missing world.db/manifest.json");
    }
    {
        let mut db_entry = zf.by_name("world.db").ok()?;
        let mut out = std::fs::File::create(tmp_db).ok()?;
        std::io::copy(&mut db_entry, &mut out).ok()?;
    }
    let verdict = (|| -> Result<bool, rusqlite::Error> {
        let t = rusqlite::Connection::open(tmp_db)?;
        t.query_row("SELECT COUNT(*) FROM worlds", [], |r| r.get::<_, i64>(0))?;
        let mut st = t.prepare("PRAGMA table_info(entities)")?;
        let cols: Vec<String> = st.query_map([], |r| r.get::<_, String>(1))?.collect::<Result<_, _>>()?;
        Ok(cols.iter().any(|n| n == "aliases"))
    })();
    match verdict {
        Err(_) => return bad("world.db inside the zip isn't a valid nd-world database"),
        Ok(false) => {
            return bad("That backup predates the current schema (no entities.aliases) \u{2014} restore it into an older nd-world release first.")
        }
        Ok(true) => {}
    }
    // the staging itself: media first, world.db LAST (a staging folder without it is an incomplete stage the next start discards)
    let _ = std::fs::remove_dir_all(staging);
    std::fs::create_dir_all(staging).ok()?;
    for name in &names {
        if name == "world.db" || name == "manifest.json" || name.ends_with('/') {
            continue;
        }
        let Some(rel) = staged_rel(name) else { continue };
        let target = staging.join(rel);
        std::fs::create_dir_all(target.parent()?).ok()?;
        let mut data = Vec::new();
        zf.by_name(name).ok()?.read_to_end(&mut data).ok()?;
        std::fs::write(&target, data).ok()?;
    }
    if std::fs::rename(tmp_db, staging.join("world.db")).is_err() {
        std::fs::copy(tmp_db, staging.join("world.db")).ok()?;
    }
    let mut manifest = Vec::new();
    zf.by_name("manifest.json").ok()?.read_to_end(&mut manifest).ok()?;
    std::fs::write(staging.join("manifest.json"), manifest).ok()?;
    redirect("/export?restore_staged=1")
}

/// `POST /admin/backup/restore/cancel`: the staged restore dropped
pub fn cancel(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let staging = staging_dir(c.cfg)?;
    if staging.exists() {
        std::fs::remove_dir_all(&staging).ok()?;
    }
    redirect("/export")
}

/// `_apply_staged_restore`, for a start with no Python (ND_PYTHON_FREE): a complete stage replaces world.db (the old one kept aside),
/// its uploads/ and maps/ merged over the live folders; an incomplete one (no world.db) is discarded
pub fn apply_staged(cfg: &Config) {
    let Some(staging) = staging_dir(cfg) else { return };
    let staged_db = staging.join("world.db");
    if !staged_db.exists() {
        if staging.exists() {
            eprintln!("nd-gateway: discarding an incomplete staged restore (no world.db) from {}", staging.display());
            let _ = std::fs::remove_dir_all(&staging);
        }
        return;
    }
    eprintln!("nd-gateway: applying a staged Full Backup restore from {}", staging.display());
    let db_path = &cfg.db_path;
    let Some(dir) = db_path.parent() else { return };
    if db_path.exists() {
        if let Ok(c) = rusqlite::Connection::open(db_path) {
            let _ = c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
        }
        let aside = dir.join(format!("{}.pre-restore-{}", db_path.file_name().and_then(|n| n.to_str()).unwrap_or("world.db"), stamp()));
        if std::fs::copy(db_path, &aside).is_err() {
            eprintln!("nd-gateway: the pre-restore copy failed; the staged restore is left unapplied");
            return;
        }
        for ext in ["-wal", "-shm"] {
            let side = PathBuf::from(format!("{}{ext}", db_path.display()));
            if side.metadata().map_or(false, |m| m.len() > 0) {
                let _ = std::fs::copy(&side, format!("{}{ext}", aside.display()));
            }
        }
        eprintln!("nd-gateway: pre-restore database backed up to {}", aside.display());
    }
    if std::fs::rename(&staged_db, db_path).is_err() {
        eprintln!("nd-gateway: could not swap the staged database in; left unapplied");
        return;
    }
    for ext in ["-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{ext}", db_path.display()));
    }
    for sub in ["uploads", "maps"] {
        merge_dir(&staging.join(sub), &dir.join(sub));
    }
    let _ = std::fs::remove_dir_all(&staging);
    eprintln!("nd-gateway: Full Backup restore applied.");
}

fn merge_dir(from: &Path, to: &Path) {
    let Ok(rd) = std::fs::read_dir(from) else { return };
    for e in rd.flatten() {
        let (src, dst) = (e.path(), to.join(e.file_name()));
        if src.is_dir() {
            merge_dir(&src, &dst);
        } else if src.is_file() {
            let _ = std::fs::create_dir_all(to);
            let _ = std::fs::copy(&src, &dst);
        }
    }
}

fn stamp() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs()) as i64;
    let (y, m, d) = crate::startup::civil_from_days(secs.div_euclid(86400));
    let s = secs.rem_euclid(86400);
    format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", s / 3600, s % 3600 / 60, s % 60)
}
