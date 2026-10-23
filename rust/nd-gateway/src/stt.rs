//! Speech-to-text for the gateway's audio jobs (app/ai.py `transcribe_audio` and its helpers, app/unsloth_extras.py `stt`): a recording
//! longer than STT_CHUNK_SECONDS x 1.5 is split by ffmpeg into ~STT_CHUNK_SECONDS pieces, each piece goes to Unsloth Studio's
//! `/v1/audio/transcriptions` (re-encoded to Opus - or MP3 when Studio refuses Opus - and split again when it would pass Studio's 25 MiB
//! limit), a checkpoint is written after each piece, and runs of 4 or more identical lines are collapsed.

use http_body_util::BodyExt;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::ai::Backend;

const STUDIO_MAX_BYTES: u64 = 23 * 1024 * 1024;
const STUDIO_CHUNK_SECONDS: f64 = 600.0;

/// `SttError`: the message, and the transcript of the pieces done before it
pub struct SttError {
    pub message: String,
    pub partial: String,
}

fn err(message: impl Into<String>) -> SttError {
    SttError { message: message.into(), partial: String::new() }
}

/// STT_CHUNK_SECONDS (or WHISPER_CHUNK_SECONDS), at least 60, default 10 minutes
pub fn chunk_seconds() -> f64 {
    let v = std::env::var("STT_CHUNK_SECONDS").ok().filter(|v| !v.is_empty()).or_else(|| std::env::var("WHISPER_CHUNK_SECONDS").ok().filter(|v| !v.is_empty()));
    v.and_then(|v| v.parse::<f64>().ok()).unwrap_or(600.0).max(60.0)
}

/// `_collapse_repeated_transcript_lines`: a run of 4+ identical lines kept once
pub fn collapse_repeats(text: &str) -> String {
    let lines: Vec<&str> = text.split('\n').collect();
    let (mut out, mut i) = (vec![], 0);
    while i < lines.len() {
        let mut j = i;
        while j < lines.len() && lines[j] == lines[i] {
            j += 1;
        }
        if j - i >= 4 { out.push(lines[i]) } else { out.extend(&lines[i..j]) }
        i = j;
    }
    out.join("\n")
}

/// `_probe_audio_duration`: seconds, via ffprobe
pub async fn duration(path: &Path) -> Option<f64> {
    let out = tokio::process::Command::new("ffprobe").args(["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]).arg(path)
        .stderr(std::process::Stdio::null()).output().await.ok()?;
    if !out.status.success() {
        return None;
    }
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
}

/// `_split_audio_into_chunks`: the pieces (stream-copied) and their folder, or just the file when ffmpeg cannot split it
async fn split(path: &Path, seconds: f64) -> (Vec<PathBuf>, Option<tempfile::TempDir>) {
    let Ok(dir) = tempfile::Builder::new().prefix("nd-stt-chunks-").tempdir() else { return (vec![path.to_path_buf()], None) };
    let ext = path.extension().map(|e| format!(".{}", e.to_string_lossy())).unwrap_or_default();
    let pattern = dir.path().join(format!("chunk_%04d{ext}"));
    let ok = tokio::process::Command::new("ffmpeg").args(["-y", "-i"]).arg(path).args(["-f", "segment", "-segment_time", &(seconds as i64).to_string(),
        "-reset_timestamps", "1", "-c", "copy"]).arg(&pattern).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
        .status().await.map(|s| s.success()).unwrap_or(false);
    if !ok {
        return (vec![path.to_path_buf()], None);
    }
    let mut chunks: Vec<PathBuf> = std::fs::read_dir(dir.path()).map(|r| r.filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.file_name().map(|n| { let n = n.to_string_lossy(); n.starts_with("chunk_") && n.ends_with(&ext) }).unwrap_or(false)).collect()).unwrap_or_default();
    chunks.sort();
    if chunks.is_empty() {
        return (vec![path.to_path_buf()], None);
    }
    (chunks, Some(dir))
}

/// Studio refused an Opus re-encode once (MP3 got through): MP3 from then on, until restart
static OPUS_REJECTED: AtomicBool = AtomicBool::new(false);

fn upload_format() -> &'static str {
    if OPUS_REJECTED.load(Ordering::Relaxed) || std::env::var("STT_UPLOAD_FORMAT").map(|f| f.trim().to_lowercase() == "mp3").unwrap_or(false) { "mp3" } else { "opus" }
}

fn opus_kbps() -> i64 {
    let v = std::env::var("STT_OPUS_BITRATE").unwrap_or_default().trim().to_lowercase();
    match v.strip_suffix('k').filter(|d| !d.is_empty() && d.len() <= 3 && d.bytes().all(|b| b.is_ascii_digit())).and_then(|d| d.parse::<i64>().ok()) {
        Some(n) if n >= 8 => n.min(128),
        _ => 24,
    }
}

enum Failure {
    Stt(SttError),
    OpusUnusable,
}

async fn transcode(path: &Path, dir: &Path, fmt: &str) -> Result<PathBuf, Failure> {
    let stem = path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let (out, args): (PathBuf, Vec<String>) = if fmt == "opus" {
        (dir.join(format!("{stem}-nd-stt.ogg")), ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a"].iter().map(|s| s.to_string())
            .chain([format!("{}k", opus_kbps()), "-application".into(), "voip".into(), "-f".into(), "ogg".into()]).collect())
    } else {
        (dir.join(format!("{stem}-nd-stt.mp3")), ["-ac", "1", "-ar", "16000", "-b:a", "64k"].iter().map(|s| s.to_string()).collect())
    };
    let status = tokio::process::Command::new("ffmpeg").args(["-y", "-v", "error", "-i"]).arg(path).args(&args).arg(&out)
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status().await;
    match status {
        Err(_) if fmt == "opus" => Err(Failure::OpusUnusable),
        Err(_) => Err(Failure::Stt(err("ffmpeg isn't available in this deployment, so the oversized audio can't be prepared for Studio's 25 MiB limit — convert it to MP3/OGG manually."))),
        Ok(s) if s.success() && out.is_file() => Ok(out),
        Ok(_) if fmt == "opus" => Err(Failure::OpusUnusable),
        Ok(_) => Err(Failure::Stt(err(format!("Re-encoding {} for Studio's 25 MiB limit failed (ffmpeg returned an error — it may be built without the MP3 encoder). Convert it to MP3/OGG manually.",
            path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default())))),
    }
}

/// `unsloth_extras._error_message`
pub fn studio_message_pub(bytes: &[u8], status: u16) -> String {
    studio_message(bytes, status)
}

fn studio_message(bytes: &[u8], status: u16) -> String {
    let py = |v: &Value| match v { Value::String(s) => s.clone(), other => other.to_string() };
    let msg = match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(o)) => {
            let mut m = match o.get("error") {
                Some(Value::Object(e)) => e.get("message").filter(|m| crate::edits::py_truthy_pub(Some(m))).map(py).unwrap_or_default(),
                Some(Value::String(s)) => s.clone(),
                Some(Value::Array(a)) if !a.is_empty() => a.iter().map(py).collect::<Vec<_>>().join("; "),
                _ => String::new(),
            };
            if m.is_empty() {
                m = match o.get("detail") {
                    Some(Value::String(s)) => s.clone(),
                    Some(Value::Array(a)) if !a.is_empty() => a.iter().map(|d| d.get("msg").map(py).unwrap_or_else(|| py(d))).collect::<Vec<_>>().join("; "),
                    Some(d @ Value::Object(_)) => d.get("message").filter(|m| crate::edits::py_truthy_pub(Some(m))).map(py).unwrap_or_else(|| d.to_string()),
                    _ => String::new(),
                };
            }
            if m.is_empty() && !o.is_empty() { Value::Object(o).to_string() } else { m }
        }
        Ok(Value::String(s)) if !s.is_empty() => s,
        Ok(v @ Value::Array(_)) if v.as_array().is_some_and(|a| !a.is_empty()) => v.to_string(),
        _ => String::new(),
    };
    let msg = if msg.is_empty() { crate::edits::py_strip_pub(&String::from_utf8_lossy(bytes)) } else { msg };
    if msg.is_empty() { format!("HTTP {status}") } else { msg.chars().take(300).collect() }
}

/// `unsloth_extras.stt`: one file to /v1/audio/transcriptions (multipart), its text; Err((status, message))
async fn stt_call(b: &Backend, audio: Vec<u8>, filename: &str, model: &str) -> Result<String, (u16, String, bool)> {
    if audio.is_empty() {
        return Err((400, "No audio to transcribe".into(), false));
    }
    let boundary = format!("nd-{:032x}", rand::random::<u128>());
    let mut body = Vec::with_capacity(audio.len() + 512);
    body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n").as_bytes());
    body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n",
        filename.replace('"', "%22")).as_bytes());
    body.extend_from_slice(&audio);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    let req = hyper::Request::builder().method("POST").uri(format!("http://{}{}/v1/audio/transcriptions", b.host, b.base_path))
        .header("authorization", format!("Bearer {}", b.key)).header("content-type", format!("multipart/form-data; boundary={boundary}"))
        .body(http_body_util::Full::new(bytes::Bytes::from(body))).map_err(|e| (503, e.to_string(), true))?;
    let resp = crate::ai::client().request(req).await.map_err(|e| (503, format!("Unsloth Studio unreachable: ConnectError: {e}"), true))?;
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.map_err(|e| (503, format!("Unsloth Studio unreachable: ReadError: {e}"), true))?.to_bytes();
    if status >= 400 {
        return Err((status, studio_message(&bytes, status), false));
    }
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(Value::Object(o)) => Ok(o.get("text").and_then(|t| t.as_str()).unwrap_or("").to_string()),
        Ok(_) => Err((502, "Studio returned an unexpected transcription response".into(), false)),
        Err(_) => Err((502, "Studio returned a non-JSON transcription response".into(), false)),
    }
}

/// `_stt_send_parts`: each part in order, the texts joined by newlines
async fn send_parts(b: &Backend, parts: &[PathBuf], opus: bool, model: &str) -> Result<String, Failure> {
    let mut texts = vec![];
    for p in parts {
        let audio = tokio::fs::read(p).await.unwrap_or_default();
        let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        match stt_call(b, audio, &name, model).await {
            Ok(t) => texts.push(crate::edits::py_strip_pub(&t)),
            Err((status, _, _)) if opus && [400, 415, 422, 500].contains(&status) => return Err(Failure::OpusUnusable),
            Err((_, message, _)) => return Err(Failure::Stt(err(format!("Unsloth Studio STT: {message}")))),
        }
    }
    Ok(texts.into_iter().filter(|t| !t.is_empty()).collect::<Vec<_>>().join("\n"))
}

async fn reencoded(b: &Backend, path: &Path, duration: f64, fmt: &str, model: &str) -> Result<String, Failure> {
    let dir = tempfile::Builder::new().prefix("nd-stt-").tempdir().map_err(|e| Failure::Stt(err(e.to_string())))?;
    let small = transcode(path, dir.path(), fmt).await?;
    let size = std::fs::metadata(&small).map(|m| m.len()).unwrap_or(0);
    let mut _split_dir = None;
    let parts = if size <= STUDIO_MAX_BYTES && duration <= STUDIO_CHUNK_SECONDS * 1.5 {
        vec![small]
    } else {
        let plan = if size <= STUDIO_MAX_BYTES { None } else { Some((duration * (STUDIO_MAX_BYTES as f64 / size as f64)).max(30.0)) };
        let (pieces, d) = split(&small, plan.unwrap_or(STUDIO_CHUNK_SECONDS).min(STUDIO_CHUNK_SECONDS)).await;
        _split_dir = d;
        pieces
    };
    send_parts(b, &parts, fmt == "opus", model).await
}

/// `_transcribe_one_file`
async fn one_file(b: &Backend, path: &Path, model: &str) -> Result<String, SttError> {
    if !path.is_file() {
        return Err(err(format!("Audio file not found: {}", path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default())));
    }
    let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let dur = duration(path).await;
    let unwrap = |r: Result<String, Failure>| match r { Ok(t) => Ok(t), Err(Failure::Stt(e)) => Err(e), Err(Failure::OpusUnusable) => Err(err("Unsloth Studio STT: the audio was refused")) };
    if !(size > STUDIO_MAX_BYTES || dur.is_some_and(|d| d > STUDIO_CHUNK_SECONDS * 1.5)) {
        return unwrap(send_parts(b, &[path.to_path_buf()], false, model).await);
    }
    let Some(d) = dur.filter(|d| *d > 0.0) else {
        return Err(err(format!("{} is {} MiB — over Unsloth Studio's 25 MiB transcription request limit — and its duration couldn't be read to split it (ffmpeg/ffprobe missing?). Convert it to MP3/OGG first.",
            path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(), (size + 1048575) / 1048576)));
    };
    if upload_format() == "opus" {
        match reencoded(b, path, d, "opus", model).await {
            Err(Failure::OpusUnusable) => {}
            other => return unwrap(other),
        }
        let text = unwrap(reencoded(b, path, d, "mp3", model).await)?;
        OPUS_REJECTED.store(true, Ordering::Relaxed);
        return Ok(text);
    }
    unwrap(reencoded(b, path, d, "mp3", model).await)
}

/// `transcribe_audio`: the recording's transcript (a checkpoint after each piece of a long one, resumed from `resume`)
pub async fn transcribe(b: &Backend, path: &Path, model: &str, resume: Option<&Value>, progress: &(dyn Fn(usize, usize) + Sync),
                        checkpoint: &(dyn Fn(Value) + Sync)) -> Result<String, SttError> {
    let secs = chunk_seconds();
    let dur = duration(path).await;
    if !dur.is_some_and(|d| d > (15.0 * 60.0f64).max(secs * 1.5)) {
        return one_file(b, path, model).await.map(|t| collapse_repeats(&t));
    }
    let (chunks, _dir) = split(path, secs).await;
    if chunks.len() == 1 {
        return one_file(b, &chunks[0], model).await.map(|t| collapse_repeats(&t));
    }
    let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let (mut start, mut parts) = (0usize, vec![]);
    if let Some(r) = resume {
        if r.get("phase").and_then(|v| v.as_str()) == Some("transcribe") && r.get("chunk_total").and_then(|v| v.as_u64()) == Some(chunks.len() as u64)
            && r.get("chunk_seconds").and_then(|v| v.as_f64()) == Some(secs) && r.get("audio_size").and_then(|v| v.as_u64()) == Some(size) {
            start = r.get("chunks_done").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            parts.push(r.get("text").and_then(|v| v.as_str()).unwrap_or("").to_string());
        }
    }
    let joined = |parts: &[String]| collapse_repeats(&parts.iter().filter(|p| !p.is_empty()).cloned().collect::<Vec<_>>().join("\n"));
    for i in start..chunks.len() {
        progress(i + 1, chunks.len());
        match one_file(b, &chunks[i], model).await {
            Ok(t) => parts.push(t),
            Err(e) if !parts.is_empty() => {
                return Err(SttError { message: format!("Speech-to-text failed on part {} of {}: {}. The first {i} part(s) were transcribed and have been saved — you can re-summarize from the partial transcript, or re-upload to redo the whole recording.",
                    i + 1, chunks.len(), e.message), partial: joined(&parts) });
            }
            Err(e) => return Err(e),
        }
        checkpoint(json!({"phase": "transcribe", "chunks_done": i + 1, "chunk_total": chunks.len(), "chunk_seconds": secs, "audio_size": size, "text": joined(&parts)}));
    }
    Ok(joined(&parts))
}

/// `get_stt_model()`: ai_models.json's stt_model, else "small"
pub fn stt_model(cfg: &crate::config::Config) -> String {
    cfg.db_path.parent().map(|p| p.join("ai_models.json")).and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str::<Value>(&t).ok()).and_then(|v| v.get("stt_model").and_then(|m| m.as_str()).filter(|m| !m.is_empty()).map(str::to_string))
        .unwrap_or_else(|| "small".into())
}
