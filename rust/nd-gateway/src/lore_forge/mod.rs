//! ND Lore Forge, embedded (repository `Llm-training`, whose desktop app consumes nd-world's `.ndtrain` bundle): the
//! optional one-click-installable module that fine-tunes a small model on ONE world's lore, run by the gateway itself.
//!
//! Answered only without Python (`ND_PYTHON_FREE`), like `crate::teacher` — there is no Python counterpart; in hybrid
//! the routes fall through to Python, which 404s them, and the Export & Backup card hides itself.
//!
//! Lifecycle (every long step is a live job through `crate::runners`, so the Background Jobs page lists, cancels and
//! restarts it, and each run takes the shared AI slot — one GPU):
//!   install      download the Llm-training repo tarball from GitHub, extract `worker/` + `catalog/` under
//!                `<DB dir>/lore-forge/repo/` (the Tauri GUI, Rust crates and docs stay out).
//!   setup-env    the SEPARATE, explicit step (multi-GB): download the pinned `uv` binary, create the venv with a
//!                managed Python, install PyTorch (the CUDA build when nvidia-smi is present, the CPU one otherwise)
//!                and Unsloth + transformers/peft/trl.
//!   train run    the active world's text — `teacher::docs::articles` over the same members the bundle uses, plus the
//!                teacher examples as chat rows — is written as the worker's RAW source rows ({"type":"text"} and
//!                {"messages":[...]}: the worker's data.load_training_rows tokenizes itself), a job.json is written and
//!                `python -m ndforge_worker job.json` runs as a subprocess. GM secrets ARE included and access tags are
//!                stripped to their CONTENT by the docs port — the trained model is GM-trusted exactly like the bundle.
//!   export       `{"job":"export",...}` merges the adapter and writes the GGUF quant(s).
//!   install-model  push the GGUF into Ollama (/api/blobs + /api/create, the same two calls Python's
//!                import_local_gguf_model makes) and/or copy it into the Studio hub dir when ND_STUDIO_HUB_DIR names a
//!                folder this process can see — honestly reported, never guessed.
//!   use-model    point nd-world's chat at the trained model (app_settings.llm_model, which the gateway's own
//!                ai::backend reads fresh on every request).
//!
//! Worker contract (verified against worker/ndforge_worker/__main__.py and data.py): a job file is JSON with a "job"
//! key ("train" | "export"); events arrive as JSON lines on stdout (parsed leniently — any of event/type/kind, plus
//! loss/step/total/eta/answer when present); a STOP file in the run's out dir halts a run with its checkpoint kept;
//! summary.json lands at the end of a train and export.json maps quant → GGUF file.
//!
//! All paths live under the database folder (`cfg.db_path.parent()` / "lore-forge"), the same convention as uploads/
//! and teacher/. Durable state is one JSON file (`state.json`), merged like the teacher's; the runs' live progress is
//! mirrored into it from the runners' status dicts. Tests redirect the pieces via env overrides documented on each
//! helper (ND_LORE_FORGE_REPO_DIR, ND_LORE_FORGE_UV_DIR, ND_LORE_FORGE_WORKER_ARGS).
pub mod jobs;
pub mod routes;
pub mod rows;

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::config::Config;

pub const REPO_OWNER: &str = "8bit-boom";
pub const REPO_NAME: &str = "Llm-training";
pub const REPO_API_URL: &str = "https://api.github.com/repos/8bit-boom/Llm-training";
pub const TARBALL_URL: &str = "https://codeload.github.com/8bit-boom/Llm-training/tar.gz/refs/heads/{branch}";
pub const FALLBACK_BRANCH: &str = "main";
/// Extract only what the worker needs — the Tauri GUI, the Rust crates and the docs stay out.
pub const EXTRACT_PREFIXES: [&str; 2] = ["worker/", "catalog/"];
/// The tarball is a few MB; anything past this is refused before it is written.
pub const TARBALL_MAX_BYTES: u64 = 64 * 1024 * 1024;
/// The pinned `uv` release the training venv is built with (uv fetches its own managed Python).
pub const UV_VERSION: &str = "0.8.4";
pub const UV_RELEASE_BASE: &str = "https://github.com/astral-sh/uv/releases/download";
/// The managed Python the venv gets (ND_LORE_FORGE_PYTHON_VERSION overrides).
pub const PYTHON_VERSION: &str = "3.12";

/// Built-in fallback model catalog (mirrors the repo's catalog/models.toml, entries verified on Hugging Face
/// 2026-11): NON-GGUF HF repos — the trainer needs weights, not the GGUF-only repos the chat backend serves.
pub const BUILTIN_MODELS: [(&str, &str, &str, &str, f64); 6] = [
    ("qwen3.5-2b", "Qwen3.5 2B", "unsloth/Qwen3.5-2B", "qwen3_5", 2.27),
    ("qwen3.5-4b", "Qwen3.5 4B", "unsloth/Qwen3.5-4B", "qwen3_5", 4.66),
    ("qwen3.5-9b", "Qwen3.5 9B", "unsloth/Qwen3.5-9B", "qwen3_5", 9.65),
    ("gemma4-e2b", "Gemma 4 E2B", "unsloth/gemma-4-E2B-it", "gemma4", 5.12),
    ("gemma4-e4b", "Gemma 4 E4B", "unsloth/gemma-4-E4B-it", "gemma4", 8.0),
    ("qwen3.8-27b", "Qwen3.8 27B", "unsloth/Qwen3.8-27B", "qwen3_5", 27.78),
];

/// nd-world's presets for the worker's `hyper` block (the desktop app's Rust planner ships its own; these are the
/// nd-world equivalents — LoRA rank/alpha by budget, learning rate falling as the run gets longer). `method` is
/// qlora by default: (id, label, rank, alpha, lr, epochs, max_seq, batch, grad_accum).
pub const PRESETS: [(&str, &str, i64, i64, f64, i64, i64, i64, i64); 3] = [
    ("quick", "Quick", 16, 32, 2e-4, 2, 2048, 2, 4),
    ("normal", "Normal", 32, 64, 1e-4, 3, 2048, 2, 4),
    ("best", "Best", 64, 128, 8e-5, 4, 2048, 2, 4),
];
/// Training methods the worker accepts (Llm-training worker/ndforge_worker: `lora|qlora|full` plus the three the
/// worker learned for this integration — DoRA (LoRA with magnitude multipliers), RS-LoRA (rank-stabilized) and LoftQ
/// (SVD-initialized LoRA, PEFT backend only)). (id, page label, one-line description)
pub const METHODS: [(&str, &str, &str); 6] = [
    ("qlora", "QLoRA", "4-bit quantized LoRA — fits the smallest GPUs"),
    ("lora", "LoRA", "Train with LoRA (16-bit)"),
    ("dora", "DoRA", "LoRA with multipliers"),
    ("rs_lora", "RS-LoRA", "Rank-stabilized LoRA"),
    ("loftq", "LoftQ", "SVD-initialized LoRA (the transformers backend; slower start)"),
    ("full", "Full fine-tune", "Train every weight — needs the most VRAM"),
];
pub const QUANTS: [&str; 5] = ["q4_k_m", "q5_k_m", "q6_k", "q8_0", "f16"];
pub const DEFAULT_PRESET: &str = "normal";
pub const DEFAULT_METHOD: &str = "qlora";
pub const DEFAULT_QUANT: &str = "q4_k_m";

/// Packages for the training venv, after torch. PyTorch: PyPI's default wheels bundle CUDA (Linux/Windows); on a
/// CPU-only box the runner switches to the cpu index instead.
pub const ENV_PACKAGES: [&str; 10] = ["unsloth", "unsloth_zoo", "transformers", "datasets", "accelerate",
    "peft", "trl", "bitsandbytes", "safetensors", "sentencepiece"];

// ----------------------------------------------------------------------------------------------------------------- paths

pub fn db_dir(cfg: &Config) -> Option<PathBuf> {
    cfg.db_path.parent().map(|p| p.to_path_buf())
}

pub fn lf_dir(cfg: &Config) -> Option<PathBuf> {
    db_dir(cfg).map(|d| d.join("lore-forge"))
}

pub fn repo_dir(base: &Path) -> PathBuf {
    base.join("repo")
}

pub fn env_dir(base: &Path) -> PathBuf {
    base.join("env")
}

pub fn runs_dir(base: &Path) -> PathBuf {
    base.join("runs")
}

pub fn run_dir(base: &Path, run_id: &str) -> Option<PathBuf> {
    safe_run_id(run_id).map(|id| runs_dir(base).join(id))
}

/// A run id is data before it is a path: [A-Za-z0-9_-] only.
pub fn safe_run_id(run_id: &str) -> Option<&str> {
    let id = run_id.trim();
    (!id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')).then_some(id)
}

/// A run's out dir — where the worker writes its checkpoints, summary.json and (for export) export.json + GGUFs.
pub fn run_out(run: &Path) -> PathBuf {
    run.join("out")
}

/// The worker's halting switch: a STOP file in the out dir ends the run with its checkpoint kept.
pub fn stop_file(run: &Path) -> PathBuf {
    run_out(run).join("STOP")
}

pub fn write_stop(run: &Path) {
    let out = run_out(run);
    let _ = std::fs::create_dir_all(&out);
    let _ = std::fs::write(stop_file(run), b"stop");
}

// ----------------------------------------------------------------------------------------------------------------- state

pub fn read_state(base: &Path) -> Value {
    std::fs::read(base.join("state.json")).ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_else(|| json!({"installed": false, "env": {}, "runs": {}}))
}

/// Merges `patch` into state.json (tmp + rename, like the teacher's)
pub fn set_state(base: &Path, patch: Value) {
    let mut v = read_state(base);
    if let (Some(o), Some(p)) = (v.as_object_mut(), patch.as_object()) {
        for (k, x) in p {
            o.insert(k.clone(), x.clone());
        }
    }
    write_state(base, &v);
}

pub fn write_state(base: &Path, v: &Value) {
    let _ = std::fs::create_dir_all(base);
    let tmp = base.join("state.json.tmp");
    if std::fs::write(&tmp, serde_json::to_vec(v).unwrap_or_default()).is_ok() {
        let _ = std::fs::rename(tmp, base.join("state.json"));
    }
}

/// Merges `patch` into one run's meta (creating it when missing)
pub fn update_run(base: &Path, run_id: &str, patch: Value) {
    let mut v = read_state(base);
    let (Some(obj), Some(id)) = (v.as_object_mut(), safe_run_id(run_id)) else { return };
    let runs = obj.entry("runs").or_insert_with(|| json!({}));
    if let Some(r) = runs.as_object_mut() {
        let entry = r.entry(id.to_string()).or_insert_with(|| json!({"id": id}));
        if let (Some(e), Some(p)) = (entry.as_object_mut(), patch.as_object()) {
            for (k, x) in p {
                e.insert(k.clone(), x.clone());
            }
        }
    }
    write_state(base, &v);
}

pub fn installed(base: &Path) -> bool {
    repo_dir(base).join("worker").join("ndforge_worker").join("__main__.py").is_file()
}

/// The training venv's python: ND_LORE_FORGE_ENV_PYTHON overrides (tests point it at a stub), else env/{bin,Scripts}/python
pub fn env_python(base: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("ND_LORE_FORGE_ENV_PYTHON") {
        let p = PathBuf::from(p.trim());
        if !p.as_os_str().is_empty() {
            return Some(p);
        }
    }
    let exe = if cfg!(windows) { "python.exe" } else { "python" };
    let p = env_dir(base).join(if cfg!(windows) { "Scripts" } else { "bin" }).join(exe);
    p.is_file().then_some(p)
}

pub fn env_ready(base: &Path) -> bool {
    env_python(base).is_some()
}

// ----------------------------------------------------------------------------------------------------------------- catalog

/// Model entries from the installed repo's catalog/models.toml, falling back to the built-in table (same repos) so the
/// page still works with an older/newer catalog. A hand-edited catalog must not kill the page: bad shapes are skipped.
pub fn catalog(base: &Path) -> Vec<Value> {
    let path = repo_dir(base).join("catalog").join("models.toml");
    if let Ok(text) = std::fs::read_to_string(&path) {
        if let Ok(v) = toml::from_str::<Value>(&text) {
            let entries = v.get("models").and_then(|m| m.as_array()).cloned()
                .or_else(|| v.get("model").and_then(|m| m.as_array()).cloned());
            let models: Vec<Value> = entries.unwrap_or_default().into_iter().filter_map(|e| {
                let repo = e.get("repo").and_then(|r| r.as_str()).filter(|r| !r.is_empty())?;
                Some(json!({
                    "id": e.get("id").and_then(|i| i.as_str()).unwrap_or(repo),
                    "name": e.get("name").and_then(|n| n.as_str()).unwrap_or(repo),
                    "repo": repo,
                    "family": e.get("family").and_then(|f| f.as_str()).unwrap_or(""),
                    "params_b": e.get("params_b").and_then(|p| p.as_f64()).unwrap_or(0.0),
                }))
            }).collect();
            if !models.is_empty() {
                return models;
            }
        }
    }
    BUILTIN_MODELS.iter().map(|(id, name, repo, family, params_b)| json!({
        "id": id, "name": name, "repo": repo, "family": family, "params_b": params_b,
    })).collect()
}

pub fn model_by_repo(models: &[Value], repo: &str) -> Option<Value> {
    models.iter().find(|m| m.get("repo").and_then(|r| r.as_str()) == Some(repo)).cloned()
}

pub fn preset(name: &str) -> Option<Value> {
    PRESETS.iter().find(|p| p.0 == name).map(|(id, label, rank, alpha, lr, epochs, max_seq, batch, grad_accum)| json!({
        "id": id, "label": label, "rank": rank, "alpha": alpha, "lr": lr, "epochs": epochs,
        "max_seq": max_seq, "batch": batch, "grad_accum": grad_accum,
    }))
}

pub fn presets() -> Vec<Value> {
    PRESETS.iter().filter_map(|(id, ..)| preset(id)).collect()
}

// ----------------------------------------------------------------------------------------------------------------- worker args

/// The command prefix the worker runs with: ND_LORE_FORGE_WORKER_ARGS (a JSON array — tests point it at a stub that
/// behaves like `python -m ndforge_worker`), else the venv's python plus the module. The job file path is appended.
pub fn worker_args(base: &Path) -> Result<Vec<String>, String> {
    if let Ok(raw) = std::env::var("ND_LORE_FORGE_WORKER_ARGS") {
        let v: Value = serde_json::from_str(&raw).map_err(|_| "ND_LORE_FORGE_WORKER_ARGS is not a JSON array".to_string())?;
        let args: Vec<String> = v.as_array().ok_or("ND_LORE_FORGE_WORKER_ARGS is not a JSON array")?.iter()
            .map(|x| x.as_str().map(String::from).ok_or("ND_LORE_FORGE_WORKER_ARGS must hold strings"))
            .collect::<Result<_, _>>()?;
        if args.is_empty() {
            return Err("ND_LORE_FORGE_WORKER_ARGS is empty".to_string());
        }
        return Ok(args);
    }
    let py = env_python(base).ok_or("The training environment is not set up yet.")?;
    Ok(vec![py.to_string_lossy().into_owned(), "-m".into(), "ndforge_worker".into()])
}

// ----------------------------------------------------------------------------------------------------------------- event parsing

/// Applies one worker stdout line to a status dict, leniently: the event arrives as JSON whose type is under any of
/// event/type/kind and whose interesting numbers ride as loss / step|current / total|total_steps / eta / answer.
/// The raw line joins the log (last 40) either way.
pub fn apply_event(status: &mut Value, line: &str) {
    let line = line.trim_end_matches(['\r', '\n']);
    if line.is_empty() {
        return;
    }
    let parsed: Option<Value> = serde_json::from_str(line).ok();
    let mut event = parsed;
    if let Some(e) = event.as_mut().and_then(|p| p.as_object_mut()) {
        let kind = ["event", "type", "kind"].iter().find_map(|k| e.get(*k)).and_then(|v| v.as_str()).unwrap_or("").to_string();
        if !kind.is_empty() {
            e.insert("stage".into(), json!(kind));
        }
        for (from, to) in [("loss", "loss"), ("step", "step"), ("current", "step"), ("total", "total"), ("total_steps", "total"), ("eta", "eta"), ("message", "message")] {
            if let Some(v) = e.get(from).cloned() {
                e.insert(to.into(), v);
            }
        }
        let answer = e.get("answer").and_then(|a| a.as_str()).filter(|s| !s.trim().is_empty()).map(String::from);
        if let Some(a) = answer {
            let samples = e.entry("samples").or_insert_with(|| json!([]));
            if let Some(arr) = samples.as_array_mut() {
                arr.push(json!(a));
                let extra = arr.len().saturating_sub(8);
                arr.drain(..extra);
            }
        }
    }
    // the event's fields land on the status dict (the runner's view), the raw line joins the log either way
    if let (Some(o), Some(e)) = (status.as_object_mut(), event.as_ref().and_then(|p| p.as_object())) {
        for (k, v) in e {
            o.insert(k.clone(), v.clone());
        }
    }
    if let Some(o) = status.as_object_mut() {
        let log = o.entry("log").or_insert_with(|| json!([]));
        if let Some(arr) = log.as_array_mut() {
            arr.push(json!(line.chars().take(500).collect::<String>()));
            let extra = arr.len().saturating_sub(40);
            arr.drain(..extra);
        }
    }
}

/// The teacher flow's examples for THIS world, as chat rows: {"messages":[...], "src":"teacher:<id>"} — only what the
/// checking pass kept ("keep"), each ending with its assistant turn (which data.load_training_rows requires).
pub fn teacher_chat_rows(db_dir: &Path, world_id: i64) -> (Vec<Value>, usize, usize) {
    let path = db_dir.join("teacher").join(world_id.to_string()).join("examples.jsonl");
    let mut rows = Vec::new();
    let mut skipped = 0usize;
    let Ok(text) = std::fs::read_to_string(&path) else { return (rows, 0, 0) };
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        if v.get("status").and_then(|s| s.as_str()) != Some("keep") {
            skipped += 1;
            continue;
        }
        let msgs: Vec<Value> = v.get("messages").and_then(|m| m.as_array()).cloned().unwrap_or_default();
        let ok = msgs.last().map(|m| m.get("role").and_then(|r| r.as_str()) == Some("assistant")).unwrap_or(false)
            && msgs.iter().all(|m| m.get("role").and_then(|r| r.as_str()).is_some_and(|r| !r.is_empty())
                && m.get("content").and_then(|c| c.as_str()).is_some_and(|c| !c.trim().is_empty()));
        if !ok || msgs.len() < 2 {
            skipped += 1;
            continue;
        }
        let id = v.get("id").and_then(|i| i.as_str()).unwrap_or("");
        rows.push(json!({"messages": msgs, "src": format!("teacher:{id}")}));
    }
    let kept = rows.len();
    (rows, kept, skipped)
}

// ----------------------------------------------------------------------------------------------------------------- tests

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn run_ids_are_strictly_path_safe() {
        assert_eq!(safe_run_id("20260101-120000-abCd_12"), Some("20260101-120000-abCd_12"));
        for bad in ["", "a/b", "..", "a b", "very long id that exceeds the sixty four character limit for a run id 1234567890", ".hidden"] {
            assert!(safe_run_id(bad).is_none(), "{bad:?} must be refused");
        }
        assert!(run_dir(Path::new("/data"), "../escape").is_none());
    }

    #[test]
    fn state_round_trips_and_runs_merge() {
        let tmp = tempfile::tempdir().unwrap();
        let base = tmp.path();
        assert_eq!(read_state(base)["installed"], json!(false));
        set_state(base, json!({"installed": true, "branch": "main"}));
        update_run(base, "run1", json!({"status": "running", "model": "unsloth/Qwen3.5-2B"}));
        update_run(base, "run1", json!({"status": "done", "loss": 1.5}));
        let st = read_state(base);
        assert_eq!(st["installed"], json!(true));
        assert_eq!(st["runs"]["run1"]["status"], json!("done"));
        assert_eq!(st["runs"]["run1"]["model"], json!("unsloth/Qwen3.5-2B"));
    }

    #[test]
    fn catalog_falls_back_to_builtins_and_parses_a_repo_catalog() {
        let tmp = tempfile::tempdir().unwrap();
        // no catalog file: the built-in table
        let models = catalog(tmp.path());
        assert_eq!(models.len(), 6);
        assert_eq!(models[0]["repo"], json!("unsloth/Qwen3.5-2B"));
        assert!(model_by_repo(&models, "unsloth/gemma-4-E4B-it").is_some());
        assert!(model_by_repo(&models, "nope").is_none());
        // a repo catalog: its entries win (an entry without a repo is skipped, garbage falls back)
        fs::create_dir_all(tmp.path().join("repo").join("catalog")).unwrap();
        fs::write(tmp.path().join("repo").join("catalog").join("models.toml"),
            "[[models]]\nid = \"m1\"\nname = \"Mini\"\nrepo = \"acme/mini\"\nfamily = \"mini\"\nparams_b = 1.5\n\n[[models]]\nname = \"no repo\"\n").unwrap();
        let models = catalog(tmp.path());
        assert_eq!(models.len(), 1);
        assert_eq!(models[0]["repo"], json!("acme/mini"));
        fs::write(tmp.path().join("repo").join("catalog").join("models.toml"), "this is not toml at all [[[").unwrap();
        assert_eq!(catalog(tmp.path()).len(), 6);
    }

    #[test]
    fn presets_cover_the_catalog_ids() {
        assert_eq!(preset("quick").unwrap()["rank"], json!(16));
        assert_eq!(preset("best").unwrap()["alpha"], json!(128));
        assert!(preset("nope").is_none());
        assert_eq!(presets().len(), 3);
    }

    #[test]
    fn events_update_the_status_dict_leniently() {
        let mut st = json!({"status": "running", "log": []});
        apply_event(&mut st, "{\"event\":\"metric\",\"loss\":1.25,\"step\":40,\"total\":300,\"eta\":\"12m\"}");
        assert_eq!(st["stage"], json!("metric"));
        assert_eq!(st["loss"], json!(1.25));
        assert_eq!(st["step"], json!(40));
        assert_eq!(st["total"], json!(300));
        assert_eq!(st["log"].as_array().unwrap().len(), 1);
        apply_event(&mut st, "{\"event\":\"sample\",\"answer\":\"The king is dead.\"}");
        assert_eq!(st["samples"].as_array().unwrap().len(), 1);
        apply_event(&mut st, "not json at all");
        apply_event(&mut st, "");
        assert_eq!(st["log"].as_array().unwrap().len(), 3);
        apply_event(&mut st, "{\"event\":\"progress\",\"current\":7}");
        assert_eq!(st["step"], json!(7));
    }

    #[test]
    fn teacher_rows_keep_only_checked_examples_ending_with_the_assistant() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("teacher").join("7");
        fs::create_dir_all(&dir).unwrap();
        let good = json!({"id": "abc123", "builder": "t_lore", "messages": [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], "status": "keep"});
        let dropped = json!({"id": "d1", "builder": "t_lore", "messages": [
            {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], "status": "drop"});
        let no_assistant = json!({"id": "d2", "builder": "t_lore", "messages": [{"role": "user", "content": "q"}], "status": "keep"});
        let mut out = String::new();
        for e in [&good, &dropped, &no_assistant] {
            out.push_str(&e.to_string());
            out.push('\n');
        }
        fs::write(dir.join("examples.jsonl"), out).unwrap();
        let (rows, kept, skipped) = teacher_chat_rows(tmp.path(), 7);
        assert_eq!((kept, skipped), (1, 2));
        assert_eq!(rows[0]["src"], json!("teacher:abc123"));
        assert_eq!(rows[0]["messages"].as_array().unwrap().len(), 3);
        // no file at all: empty, never an error
        let (rows, kept, _) = teacher_chat_rows(tmp.path(), 99);
        assert!((rows.is_empty() && kept == 0));
    }

    #[test]
    fn worker_args_use_the_override_or_the_venv_python() {
        let tmp = tempfile::tempdir().unwrap();
        std::env::remove_var("ND_LORE_FORGE_WORKER_ARGS");
        // no venv yet: refused with the setup hint
        assert!(worker_args(tmp.path()).unwrap_err().contains("training environment"));
        // the override wins over a missing venv (the tests' stub worker)
        std::env::set_var("ND_LORE_FORGE_WORKER_ARGS", "[\"/usr/bin/python\", \"-m\", \"ndforge_worker\"]");
        let args = worker_args(tmp.path()).unwrap();
        assert_eq!(args, vec!["/usr/bin/python", "-m", "ndforge_worker"]);
        std::env::set_var("ND_LORE_FORGE_WORKER_ARGS", "not json");
        assert!(worker_args(tmp.path()).is_err());
        std::env::set_var("ND_LORE_FORGE_WORKER_ARGS", "[]");
        assert!(worker_args(tmp.path()).is_err());
        std::env::remove_var("ND_LORE_FORGE_WORKER_ARGS");
        // a real venv python is found on both platforms' layout
        let bin = if cfg!(windows) { "Scripts/python.exe" } else { "bin/python" };
        fs::create_dir_all(tmp.path().join("env").join(bin).parent().unwrap()).unwrap();
        fs::write(tmp.path().join("env").join(bin), "").unwrap();
        let args = worker_args(tmp.path()).unwrap();
        assert_eq!(args.len(), 3);
        assert!(args[0].ends_with(bin.split('/').last().unwrap()));
        assert_eq!(&args[1..], &["-m", "ndforge_worker"]);
    }
}
