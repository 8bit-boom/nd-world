//! Text to speech on Unsloth Studio (app/unsloth_extras.py `tts`): POST /v1/audio/speech for a WAV, turned into Ogg Opus by ffmpeg
//! (TTS_OUTPUT_FORMAT=wav keeps the WAV), and saved as an audio clip. Used by `/api/ai/tts`, the NPC voice and the recap song.

use http_body_util::BodyExt;
use serde_json::{json, Value};
use std::time::Duration;

use crate::ai::{backend_request, Backend};
use crate::config::Config;

pub const OPUS_CONTENT_TYPE: &str = "audio/ogg; codecs=opus";
const DEFAULT_TTS_MODEL: &str = "unsloth/orpheus-3b";

/// The TTS settings from ai_models.json: (model, voice, instructions, language)
pub fn prefs(cfg: &Config) -> (String, String, String, String) {
    let data = cfg.db_path.parent().map(|p| p.join("ai_models.json")).and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str::<Value>(&t).ok()).unwrap_or(Value::Null);
    let get = |k: &str| match data.get(k) {
        Some(v) if crate::edits::py_truthy_pub(Some(v)) => match v { Value::String(s) => s.clone(), other => other.to_string() },
        _ => String::new(),
    };
    let model = get("tts_model");
    (if model.is_empty() { DEFAULT_TTS_MODEL.into() } else { model }, get("tts_voice"), get("tts_instructions"), get("tts_language"))
}

fn is_wav(audio: &[u8]) -> bool {
    audio.len() >= 12 && &audio[..4] == b"RIFF" && &audio[8..12] == b"WAVE"
}

/// `wav_info(audio)["channels"]`
fn wav_channels(audio: &[u8]) -> Option<u32> {
    if !is_wav(audio) {
        return None;
    }
    let mut pos = 12;
    let mut channels = None;
    while pos + 8 <= audio.len() {
        let id = &audio[pos..pos + 4];
        let size = u32::from_le_bytes(audio[pos + 4..pos + 8].try_into().ok()?) as usize;
        let body = pos + 8;
        if id == b"fmt " {
            if size < 16 || body + 16 > audio.len() {
                return None;
            }
            let c = u16::from_le_bytes(audio[body + 2..body + 4].try_into().ok()?) as u32;
            let rate = u32::from_le_bytes(audio[body + 4..body + 8].try_into().ok()?);
            let bits = u16::from_le_bytes(audio[body + 14..body + 16].try_into().ok()?);
            if c < 1 || rate < 1 || bits < 1 {
                return None;
            }
            channels = Some(c);
        } else if id == b"data" {
            return channels;
        }
        pos = body.checked_add(size)?.checked_add(size & 1)?;
    }
    None
}

/// `_opus_bitrate(channels)`: TTS_OPUS_BITRATE ("48k" … "512k", "max"), capped at 256k a channel
fn opus_bitrate(channels: u32) -> String {
    let raw = std::env::var("TTS_OPUS_BITRATE").unwrap_or_default().trim().to_lowercase();
    let requested = if raw == "max" { 512 } else {
        match raw.strip_suffix('k').filter(|d| (1..=4).contains(&d.len()) && d.bytes().all(|b| b.is_ascii_digit())).and_then(|d| d.parse::<u32>().ok()) {
            Some(n) if n >= 6 => n.min(512),
            _ => 48,
        }
    };
    format!("{}k", requested.min(256 * channels.max(1)))
}

/// `_wav_to_opus`: ffmpeg stdin → stdout, None when it cannot (the WAV is kept)
async fn wav_to_opus(wav: &[u8]) -> Option<Vec<u8>> {
    use tokio::io::AsyncWriteExt;
    let mut child = tokio::process::Command::new("ffmpeg")
        .args(["-v", "error", "-f", "wav", "-i", "pipe:0", "-vn", "-c:a", "libopus", "-b:a", &opus_bitrate(wav_channels(wav).unwrap_or(1)), "-f", "ogg", "pipe:1"])
        .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null()).kill_on_drop(true).spawn().ok()?;
    let mut stdin = child.stdin.take()?;
    let input = wav.to_vec();
    let writer = tokio::spawn(async move {
        let _ = stdin.write_all(&input).await;
    });
    let out = tokio::time::timeout(Duration::from_secs(120), child.wait_with_output()).await.ok()?.ok()?;
    let _ = writer.await;
    (out.status.success() && out.stdout.starts_with(b"OggS")).then_some(out.stdout)
}

/// `audio_extension(content_type)`
pub fn audio_extension(content_type: &str) -> &'static str {
    let ct = content_type.to_lowercase();
    if ct.contains("opus") || ct.contains("ogg") { ".opus" } else if ct.contains("wav") { ".wav" } else { ".mp3" }
}

/// `tts(...)`: (audio, content type) or (HTTP status, Studio's message)
pub async fn speech(b: &Backend, text: &str, model: &str, voice: &str, instructions: &str, language: &str) -> Result<(Vec<u8>, String), (u16, String)> {
    if text.trim().is_empty() {
        return Err((400, "No text to speak".into()));
    }
    if model.trim().is_empty() {
        return Err((400, "No TTS model configured — set one in Settings → System (Studio manages TTS models under its own Settings → Voice)".into()));
    }
    let mut body = json!({"model": model, "input": text, "response_format": "wav", "speed": 1.0});
    if !voice.is_empty() {
        body["voice"] = json!(voice);
    }
    if !instructions.trim().is_empty() {
        body["instructions"] = json!(instructions.trim());
    }
    if !language.trim().is_empty() {
        body["language"] = json!(language.trim());
    }
    let req = backend_request(b, "POST", "/v1/audio/speech", Some(&body)).ok_or((503, "Unsloth Studio unreachable: request".to_string()))?;
    let call = async {
        let resp = crate::ai::client().request(req).await.map_err(|e| (503, format!("Unsloth Studio unreachable: ConnectError: {e}")))?;
        let status = resp.status().as_u16();
        let ctype = resp.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let bytes = resp.into_body().collect().await.map_err(|e| (503, format!("Unsloth Studio unreachable: ReadError: {e}")))?.to_bytes();
        Ok::<_, (u16, String)>((status, ctype, bytes))
    };
    let (status, ctype, bytes) = tokio::time::timeout(Duration::from_secs(600), call).await
        .map_err(|_| (503, "Unsloth Studio unreachable: ReadTimeout: ".to_string()))??;
    if status >= 400 {
        return Err((status, crate::stt::studio_message_pub(&bytes, status)));
    }
    if bytes.is_empty() {
        return Err((502, "Studio returned no audio for this TTS request".into()));
    }
    let audio = bytes.to_vec();
    let ctype = if is_wav(&audio) { "audio/wav".to_string() } else if ctype.is_empty() { "audio/wav".into() } else { ctype };
    // _finish_tts_audio
    let opus_wanted = std::env::var("TTS_OUTPUT_FORMAT").unwrap_or_default().trim().to_lowercase() != "wav";
    if is_wav(&audio) && opus_wanted {
        if let Some(opus) = wav_to_opus(&audio).await {
            return Ok((opus, OPUS_CONTENT_TYPE.into()));
        }
    }
    Ok((audio, ctype))
}

/// Write the audio to uploads/audio as `unique_upload_filename(f"{prefix}-<8 hex>{ext}", ext)`: the file URL
pub fn save(cfg: &Config, audio: &[u8], prefix: &str, ext: &str) -> Option<String> {
    let dir = cfg.db_path.parent()?.join("uploads").join("audio");
    std::fs::create_dir_all(&dir).ok()?;
    let short = format!("{:08x}", rand::random::<u32>());
    let hex = format!("{:032x}", rand::random::<u128>());
    let name = format!("{}-{prefix}-{short}{ext}", &hex[..12]);
    std::fs::write(dir.join(&name), audio).ok()?;
    Some(format!("/uploads/audio/{name}"))
}

/// `time.strftime("%H:%M")` (the server's clock, UTC in the container)
pub fn hh_mm() -> String {
    let s = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    format!("{:02}:{:02}", (s / 3600) % 24, (s / 60) % 60)
}
