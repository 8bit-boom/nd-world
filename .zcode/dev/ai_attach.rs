
// ── AI attachments: the background audio-job routes (app/routers/ai.py) ───────────────────────────────────────

/// `_require_ask_ai_access`: a GM always may; a player only when their active world opted into either AI surface
fn require_ask_ai(c: &Ctx) -> Option<()> {
    if c.p.is_gm {
        return Some(());
    }
    let w = c.p.world.as_ref()?;
    let flag = |k: &str| w.get(k).and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    if flag("players_can_ask_ai") || flag("players_can_use_ai_chat") {
        return Some(());
    }
    None
}

/// `_job_to_dict` for an attachment job
fn attach_job_dict(j: &Map<String, Value>) -> Value {
    json!({
        "id": j.get("id"), "purpose": j.get("purpose"), "filename": j.get("filename"),
        "status": j.get("status"), "error": j.get("error"),
        "transcript": j.get("transcript"), "attachment_url": j.get("attachment_url"),
        "created_at": crate::edits::py_isoformat_pub(j.get("created_at")),
        "updated_at": crate::edits::py_isoformat_pub(j.get("updated_at")),
    })
}

/// `POST /api/ai/attachments/audio-jobs` (app/routers/ai.py `ai_attachment_audio_job_create`): store the recording and
/// start a durable transcription job (the transcribe-only path: no summarization afterwards)
pub(crate) fn attach_audio_job_create(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let id = attach_audio_job_plan(c, AttachSource::Body)?;
    crate::ai::start_audio_job_pub(id);
    json_response(&json!({"job_id": id}))
}

/// where an attachment audio job's bytes come from: the request's own file part, or the chunks .../chunk assembled
enum AttachSource { Body, Chunks }

/// The shared front of the audio-job routes: the ask-AI gate, the world, the file (or the reassembled chunks), the
/// stored recording and the pending job row - the plan's id lands in `plan_job_id`
fn attach_audio_job_plan(c: &mut Ctx, src: AttachSource) -> Option<i64> {
    require_ask_ai(c)?;
    let wid = c.world_id()?;
    let part = one_file(c, "file")?;
    let filename = part.filename.clone();
    if filename.is_empty() {
        return None; // the 400 "No file uploaded": Python's
    }
    let ext = py_suffix_lower(&filename)?;
    if !ATTACH_AUDIO_EXTS.contains(&ext.as_str()) {
        return None; // the 400 with the allowed list: Python's
    }
    let uploads = uploads_root(c)?;
    let target = uploads.join("ai_attachments");
    std::fs::create_dir_all(&target).ok()?;
    let (dest, size) = match src {
        AttachSource::Body => {
            let cap = attach_audio_cap(c)?;
            if part.len as i64 > cap {
                return None; // the 413: Python's
            }
            let name = unique_upload_filename(&filename, &ext)?;
            let dest = target.join(&name);
            c.created.push(dest.clone());
            c.upload.as_ref()?.copy_part(part, &dest)?;
            (dest, part.len as i64)
        }
        AttachSource::Chunks => {
            let f = c.form()?;
            let upload_id = f.get("upload_id")?;
            let total = crate::form::py_int(f.get("total_chunks")?)?;
            let cap = attach_audio_cap(c)?;
            let dir = uploads.join("ai_attachments").join("_chunks").join(upload_id.as_str()?);
            let parts: Vec<std::path::PathBuf> = (0..total).map(|i| dir.join(format!("{i:06}.part"))).collect();
            let mut size = 0i64;
            for part in &parts {
                size += std::fs::metadata(part).ok().filter(|m| m.is_file())?.len() as i64;
            }
            if size > cap || upload_id.as_str()?.is_empty()
                || !upload_id.as_str()?.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
                || upload_id.as_str()?.len() > 64 || !(1..=2001).contains(&total) {
                return None;
            }
            let name = unique_upload_filename(&filename, &ext)?;
            let dest = target.join(&name);
            c.created.push(dest.clone());
            let mut out = std::fs::File::create(&dest).ok()?;
            for part in &parts {
                std::io::copy(&mut std::fs::File::open(part).ok()?, &mut out).is_err().then(|| ())?;
            }
            c.removals.push(dir);
            (dest, size)
        }
    };
    let _ = size;
    let url = format!("/uploads/ai_attachments/{}", dest.file_name()?.to_string_lossy());
    let id = orm::insert(&c.db, "audio_jobs", &[
        ("world_id", Sql::Integer(wid)),
        ("purpose", Sql::Text("attachment".into())),
        ("filename", Sql::Text(filename)),
        ("status", Sql::Text("pending".into())),
        ("created_by_user_id", Sql::Integer(c.p.user_id)),
        ("audio_path", Sql::Text(dest.to_string_lossy().to_string())),
        ("delete_after", Sql::Integer(0)),
        ("attachment_url", Sql::Text(url)),
    ])?;
    Some(id)
}

const ATTACH_AUDIO_EXTS: [&str; 9] = [".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"];

/// `_effective_ai_attachment_bytes(db, "audio")`: the saved AppSettings.max_ai_attachment_mb, else the env default
fn attach_audio_cap(c: &Ctx) -> Option<i64> {
    let saved: Option<i64> = c.db.query_row("SELECT max_ai_attachment_mb FROM app_settings LIMIT 1", [], |r| r.get(0)).ok()?;
    match saved.filter(|v| *v > 0) {
        Some(mb) => Some(mb * 1024 * 1024),
        None => env_bytes("MAX_AUDIO_UPLOAD_BYTES", 3 * 1024 * 1024 * 1024),
    }
}

/// `POST /api/ai/attachments/audio-jobs/chunk`: one part of a large recording, capped like the whole file
pub(crate) fn attach_audio_job_chunk(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    require_ask_ai(c)?;
    let f = c.form()?;
    let upload_id = f.get("upload_id")?.to_string();
    let chunk = crate::form::py_int(f.get("chunk_index")?)?;
    if upload_id.is_empty() || upload_id.len() > 64 || !upload_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') || !(0..=2000).contains(&chunk) {
        return None;
    }
    let part = one_file(c, "file")?;
    if part.filename.is_empty() {
        return None;
    }
    let cap = attach_audio_cap(c)?;
    if part.len as i64 > cap {
        return None;
    }
    let dir = uploads_root(c)?.join("ai_attachments").join("_chunks").join(&upload_id);
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(format!("{chunk:06}.part"));
    c.created.push(dest.clone());
    c.upload.as_ref()?.copy_part(part, &dest)?;
    json_response(&json!({"ok": true}))
}

/// `POST /api/ai/attachments/audio-jobs/complete`: reassemble the parts and start the job
pub(crate) fn attach_audio_job_complete(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let id = attach_audio_job_plan(c, AttachSource::Chunks)?;
    crate::ai::start_audio_job_pub(id);
    json_response(&json!({"job_id": id}))
}

/// `GET /api/ai/attachments/audio-jobs/<id>` (app/routers/ai.py): one job of the active world - a player's own only
pub(crate) fn attach_audio_job_status(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    require_ask_ai(c)?;
    let wid = c.world_id()?;
    let sql = if c.p.is_gm {
        "SELECT * FROM audio_jobs WHERE id = ?1 AND world_id = ?2 AND purpose = 'attachment'"
    } else {
        "SELECT * FROM audio_jobs WHERE id = ?1 AND world_id = ?2 AND purpose = 'attachment' AND created_by_user_id = ?3"
    };
    let mut st = c.db.prepare(sql).ok()?;
    let j: Option<Map<String, Value>> = if c.p.is_gm {
        st.query_row(rusqlite::params![ids[0], wid], |r| crate::layout::row_map(r)).ok()
    } else {
        st.query_row(rusqlite::params![ids[0], wid, c.p.user_id], |r| crate::layout::row_map(r)).ok()
    };
    let Some(j) = j else { return Some(crate::ai_more::detail(StatusCode::NOT_FOUND, "Not Found")) };
    json_response(&attach_job_dict(&j))
}

/// `GET /api/ai/attachments/audio-jobs`: the recent attachment jobs of the active world (a player's own only)
pub(crate) fn attach_audio_job_list(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    require_ask_ai(c)?;
    let wid = c.world_id()?;
    let sql = if c.p.is_gm {
        "SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose = 'attachment' ORDER BY created_at DESC, id DESC LIMIT 20"
    } else {
        "SELECT * FROM audio_jobs WHERE world_id = ?1 AND purpose = 'attachment' AND created_by_user_id = ?2 ORDER BY created_at DESC, id DESC LIMIT 20"
    };
    let mut st = c.db.prepare(sql).ok()?;
    let jobs: Vec<Map<String, Value>> = if c.p.is_gm {
        st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?
    } else {
        st.query_map(rusqlite::params![wid, c.p.user_id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?
    };
    json_response(&Value::Array(jobs.iter().map(attach_job_dict).collect()))
}
