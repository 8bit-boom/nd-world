//! Train in Unsloth Studio from nd-world: upload the export's `train.jsonl` / `eval.jsonl` to Studio's OpenAI-style fine-tuning API (`/v1/files`,
//! `/v1/fine_tuning/jobs`, read from Studio's source: studio/backend/routes/openai_fine_tuning.py) and follow or cancel the job (Studio refuses pause and resume).
//! The same settings as `studio-settings.yaml` ride along as extra fields of the job (any `TrainingStartRequest` field is accepted at the top level).
//! Python-free only; the Studio address and key are the ones Settings already holds.
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::Request as HyperRequest;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};

use super::studio::{Built, Settings};

/// Studio's base learning rate: a job's `learning_rate_multiplier` scales it
const BASE_LR: f64 = 2e-4;

/// The job body for `POST /v1/fine_tuning/jobs`.
pub fn job_body(st: &Settings, slug: &str, training_file: &str, validation_file: Option<&str>) -> Value {
    let suffix: String = slug.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_').take(40).collect();
    let mut v = json!({
        "model": st.repo,
        "training_file": training_file,
        "hyperparameters": {"n_epochs": st.num_epochs, "batch_size": st.batch_size, "learning_rate_multiplier": ((st.learning_rate / BASE_LR) * 1000.0).round() / 1000.0},
        "method": {"type": "supervised"},
        "suffix": if suffix.is_empty() { "nd-world".to_string() } else { suffix },
        "seed": 3407,
        // the rest of studio-settings.yaml
        "training_type": "LoRA/QLoRA",
        "load_in_4bit": st.method == "QLoRA",
        "max_seq_length": st.max_seq_length,
        "gradient_accumulation_steps": st.grad_accum,
        "warmup_steps": st.warmup_steps,
        "max_steps": 0,
        "save_steps": st.save_steps,
        "packing": st.packing,
        "train_on_completions": true,
        "gradient_checkpointing": "unsloth",
        "lora_r": st.lora_r,
        "lora_alpha": st.lora_alpha,
        "lora_dropout": 0.0,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        "finetune_vision_layers": false,
        "finetune_language_layers": true,
        "finetune_attention_modules": true,
        "finetune_mlp_modules": true,
    });
    if let Some(f) = validation_file {
        v["validation_file"] = json!(f);
        v["eval_steps"] = json!(st.eval_steps);
    }
    v
}

/// A multipart/form-data body with a `purpose=fine-tune` field and one file.
fn multipart(filename: &str, data: &[u8]) -> (String, Vec<u8>) {
    let boundary = "----ndworld7f3a9c".to_string();
    let mut b = Vec::new();
    b.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nfine-tune\r\n").as_bytes());
    b.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: application/jsonl\r\n\r\n").as_bytes());
    b.extend_from_slice(data);
    b.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    (format!("multipart/form-data; boundary={boundary}"), b)
}

/// One call to Studio (no redirects): (status, body). Err: what a person can read.
async fn call(method: &str, url: &str, key: &str, content_type: Option<&str>, body: Vec<u8>, secs: u64) -> Result<(u16, Bytes), String> {
    let uri: hyper::Uri = url.parse().map_err(|_| "The Studio address is not a valid URL.".to_string())?;
    let client = Client::builder(TokioExecutor::new()).build(crate::net::connector(Some(Duration::from_secs(8))));
    let mut req = HyperRequest::builder().method(method).uri(uri).header("authorization", format!("Bearer {key}"));
    if let Some(ct) = content_type {
        req = req.header("content-type", ct);
    }
    let req = req.body(Full::new(Bytes::from(body))).map_err(|e| e.to_string())?;
    let resp = tokio::time::timeout(Duration::from_secs(secs), client.request(req))
        .await
        .map_err(|_| "Studio did not answer in time.".to_string())?
        .map_err(|e| if e.is_connect() { "Studio could not be reached - is it running, and is the address in Settings right?".to_string() } else { format!("Studio: {e}") })?;
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.map_err(|e| format!("Studio: {e}"))?.to_bytes();
    Ok((status, bytes))
}

/// Studio's own wording of an error, else the status.
fn problem(status: u16, body: &[u8]) -> String {
    if status == 401 || status == 403 {
        return "Studio refused the API key - create a new one in Studio (Settings -> API) and save it in nd-world's Settings.".to_string();
    }
    if status == 404 {
        return "This Studio has no fine-tuning API (it is older than the one nd-world expects). Download the zip and load it on Studio's Train page instead.".to_string();
    }
    let v: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
    let m = v["error"]["message"].as_str().or_else(|| v["detail"].as_str()).unwrap_or("");
    if m.is_empty() { format!("Studio answered HTTP {status}.") } else { m.chars().take(400).collect() }
}

async fn upload(base: &str, key: &str, name: &str, lines: &[String]) -> Result<String, String> {
    let (ct, body) = multipart(name, (lines.join("\n") + "\n").as_bytes());
    let (s, b) = call("POST", &format!("{base}/v1/files"), key, Some(&ct), body, 300).await?;
    if s != 200 {
        return Err(format!("Upload of {name} failed: {}", problem(s, &b)));
    }
    let v: Value = serde_json::from_slice(&b).map_err(|_| "Studio's answer to the upload could not be read.".to_string())?;
    v["id"].as_str().map(str::to_string).ok_or_else(|| "Studio's answer to the upload had no file id.".to_string())
}

/// Upload both files and start the job. The answer is Studio's job object, with `queued` shown as such when Studio is busy.
pub async fn start(base: &str, key: &str, b: &Built, slug: &str, st: &Settings) -> Result<Value, String> {
    let train = upload(base, key, "train.jsonl", &b.train).await?;
    let eval = if b.eval.is_empty() { None } else { Some(upload(base, key, "eval.jsonl", &b.eval).await?) };
    let body = job_body(st, slug, &train, eval.as_deref());
    let (s, r) = call("POST", &format!("{base}/v1/fine_tuning/jobs"), key, Some("application/json"), body.to_string().into_bytes(), 120).await?;
    if s != 200 {
        return Err(format!("Studio did not start the job: {}", problem(s, &r)));
    }
    serde_json::from_slice(&r).map_err(|_| "Studio's answer to the job could not be read.".to_string())
}

fn safe_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 120 && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-' || c == b'.' || c == b':')
}

/// The job, its latest events and its checkpoint count, for the card.
pub async fn job(base: &str, key: &str, id: &str) -> Result<Value, String> {
    if !safe_id(id) {
        return Err("That is not a job id.".into());
    }
    let (s, b) = call("GET", &format!("{base}/v1/fine_tuning/jobs/{id}"), key, None, Vec::new(), 30).await?;
    if s != 200 {
        return Err(problem(s, &b));
    }
    let mut v: Value = serde_json::from_slice(&b).map_err(|_| "Studio's answer could not be read.".to_string())?;
    if let Ok((200, e)) = call("GET", &format!("{base}/v1/fine_tuning/jobs/{id}/events?limit=8"), key, None, Vec::new(), 30).await {
        if let Ok(ev) = serde_json::from_slice::<Value>(&e) {
            v["events"] = ev["data"].clone();
        }
    }
    if let Ok((200, c)) = call("GET", &format!("{base}/v1/fine_tuning/jobs/{id}/checkpoints"), key, None, Vec::new(), 30).await {
        if let Ok(ck) = serde_json::from_slice::<Value>(&c) {
            v["checkpoints"] = json!(ck["data"].as_array().map(|a| a.len()).unwrap_or(0));
        }
    }
    Ok(v)
}

/// What the old pause / resume links answer: Studio itself always refuses them (400 "not supported; use cancel").
pub const PAUSE_REFUSED: &str = "Unsloth cannot pause a training job; cancel it and start again.";

/// `cancel` only.
pub async fn act(base: &str, key: &str, id: &str, action: &str) -> Result<Value, String> {
    if !safe_id(id) || action != "cancel" {
        return Err("That is not a job action.".into());
    }
    let (s, b) = call("POST", &format!("{base}/v1/fine_tuning/jobs/{id}/{action}"), key, Some("application/json"), b"{}".to_vec(), 60).await?;
    if s != 200 {
        return Err(problem(s, &b));
    }
    serde_json::from_slice(&b).map_err(|_| "Studio's answer could not be read.".to_string())
}

/// Studio's own card(s), in the shape `tuning::detect_hardware` reports ({"gpus": [{"name", "vram_mb"}]}); Err when Studio cannot say.
pub async fn hardware(base: &str, key: &str) -> Result<Value, String> {
    let (s, b) = call("GET", &format!("{base}/api/train/hardware"), key, None, Vec::new(), 30).await?;
    if s != 200 {
        return Err(problem(s, &b));
    }
    let v: Value = serde_json::from_slice(&b).map_err(|_| "Studio's answer could not be read.".to_string())?;
    Ok(parse_hardware(&v))
}

/// Studio's `/api/train/hardware` lists `devices` with a name and a total memory in GB (the field name differs by backend, so the usual ones are tried).
pub fn parse_hardware(v: &Value) -> Value {
    let gb = |d: &Value| ["vram_total_gb", "memory_total_gb", "total_gb"].iter().find_map(|k| d[*k].as_f64()).or_else(|| d["vram_total_mb"].as_f64().map(|m| m / 1024.0));
    let list: Vec<Value> = v["devices"].as_array().cloned().unwrap_or_default();
    let gpus: Vec<Value> = list.iter().filter_map(|d| {
        let g = gb(d).filter(|g| *g > 0.5)?;
        Some(json!({"name": d["name"].as_str().or_else(|| d["gpu_name"].as_str()).unwrap_or("GPU"), "vram_mb": (g * 1024.0).round()}))
    }).collect();
    json!({"gpus": gpus, "from": "studio"})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::teacher::hw;

    fn st() -> Settings {
        Settings { max_seq_length: 4096, num_epochs: 2, learning_rate: 1e-4, batch_size: 2, grad_accum: 4, warmup_steps: 10, save_steps: 100, eval_steps: 0.1, steps_per_epoch: 50, total_steps: 100,
                   model: "Qwen3.5 9B".into(), repo: "unsloth/Qwen3.5-9B".into(), method: "QLoRA", est_gb: 12.0, hw_label: "x".into(), lora_r: 16, lora_alpha: 16, packing: false }
    }

    #[test]
    fn the_job_carries_the_yaml_settings() {
        let _ = &hw::MODELS;
        let j = job_body(&st(), "my world!", "file-a", Some("file-b"));
        assert_eq!(j["model"], "unsloth/Qwen3.5-9B");
        assert_eq!(j["hyperparameters"]["learning_rate_multiplier"], 0.5);
        assert_eq!(j["suffix"], "myworld");
        assert_eq!(j["load_in_4bit"], true);
        assert_eq!(j["finetune_vision_layers"], false);
        assert_eq!(j["validation_file"], "file-b");
        assert!(job_body(&st(), "w", "file-a", None).get("validation_file").is_none());
    }

    #[test]
    fn studios_card_is_read_in_gb_or_mb() {
        let h = parse_hardware(&json!({"devices": [{"name": "NVIDIA RTX 3090", "vram_total_gb": 24.0}, {"gpu_name": "Other", "vram_total_mb": 12288}, {"name": "none"}]}));
        assert_eq!(h["gpus"].as_array().unwrap().len(), 2);
        assert_eq!(h["gpus"][0]["vram_mb"], 24576.0);
        assert_eq!(h["gpus"][1]["name"], "Other");
    }

    #[test]
    fn ids_are_checked() {
        assert!(safe_id("ftjob-abc_1"));
        assert!(!safe_id("../x") && !safe_id("a b") && !safe_id(""));
    }
}
