//! The training run's source rows and job files: the world's text as the worker's RAW rows ({"type":"text","text":…},
//! {"messages":[…]}), the job.json ({"job":"train"} / {"job":"export"}), and the probe questions.
//!
//! Text rows reuse the teacher's docs port (crate::teacher::docs::articles over the SAME members the .ndtrain bundle
//! carries, chunked by chunk_article) — that port is Lore Forge's own text prep, so the shape matches what the desktop
//! app would have trained on: whole world book, GM secrets included as "(GM secret: …)" text, access tags stripped to
//! their content. Chat rows come from the teacher flow's checked examples (mod.rs::teacher_chat_rows).

use std::path::Path;

use serde_json::{json, Value};

use crate::teacher::chunk::chunk_article;
use crate::teacher::docs::{self, Loaded};

/// Chunks are about this many tokens (chunk_article's rough 4-characters-per-token count): comfortably inside the
/// preset max_seq (2048), so long entity pages become several fully-learned rows instead of one mostly-truncated one.
const CHUNK_TOKENS: usize = 1100;
/// Docs shorter than this add noise, not lore (the teacher's own threshold).
const MIN_DOC_CHARS: usize = 200;
const MAX_PROBES: usize = 6;

/// (text rows, chat rows) for one world: every Doc as text rows, plus the teacher flow's kept examples as chat rows.
pub fn train_rows(l: &Loaded, db_dir: &Path, world_id: i64, include_teacher: bool) -> (Vec<Value>, Vec<Value>) {
    let mut text = Vec::new();
    for d in docs::articles(l) {
        if d.article.chars().count() < MIN_DOC_CHARS {
            continue;
        }
        for piece in chunk_article(&d.title, &d.article, CHUNK_TOKENS) {
            text.push(json!({"type": "text", "text": piece, "src": d.source}));
        }
    }
    let chat = if include_teacher {
        super::teacher_chat_rows(db_dir, world_id).0
    } else {
        Vec::new()
    };
    (text, chat)
}

/// Probe questions the worker answers between steps: lore questions about a few entities plus one "describe the world".
pub fn probes(l: &Loaded) -> Vec<Value> {
    let system = crate::teacher::gen::system_prompt(&l.world_name);
    let mut out: Vec<Value> = Vec::new();
    let mut push = |q: String| {
        if out.len() < MAX_PROBES && !q.is_empty() {
            out.push(json!({"system": system, "question": q}));
        }
    };
    let mut asked = 0usize;
    for e in l.entities.iter().filter(|e| e.visible) {
        if asked >= MAX_PROBES - 1 {
            break;
        }
        let who = matches!(e.kind.as_str(), "npc" | "character" | "faction" | "deity");
        let q = if who { format!("Who is {}?", e.name) } else { format!("What is {}?", e.name) };
        push(q);
        asked += 1;
    }
    push(format!("Describe the world \"{}\" in a few sentences.", l.world_name));
    out
}

/// `{"job":"train",…}`: the shape worker/ndforge_worker/__main__.py dispatches on and trainjob.run consumes.
/// `overrides` (the page's "Training Hyperparameters" fields, may be null/empty) wins per key over the preset.
pub fn train_job(model: &Value, preset: &Value, method: &str, overrides: &Value, epochs: Option<i64>, dataset: &Path, out: &Path, probes: &[Value]) -> Value {
    let g = |k: &str| overrides.get(k).filter(|v| !v.is_null()).cloned().unwrap_or_else(|| preset.get(k).cloned().unwrap_or(Value::Null));
    let hyper = json!({
        "rank": g("rank"), "alpha": g("alpha"), "lr": g("lr"),
        "epochs": epochs.filter(|n| *n > 0).unwrap_or_else(|| g("epochs").as_i64().unwrap_or(0)),
        "max_seq": g("max_seq"), "batch": g("batch"), "grad_accum": g("grad_accum"),
        "warmup": 10, "weight_decay": 0.0, "scheduler": "cosine", "seed": 42,
        "save_steps": 200, "logging_steps": 5,
    });
    json!({
        "job": "train",
        "model": {"repo": model.get("repo"), "family": model.get("family")},
        "method": method,
        "backend": "auto",
        "dataset": {"train": dataset.to_string_lossy()},
        "hyper": hyper,
        "out": out.to_string_lossy(),
        "resume": false,
        "probes": probes,
        "probe_every": 20,
    })
}

/// `{"job":"export",…}`: merge the adapter (or take the full model) and write GGUF quant(s) (exportjob.run's shape).
pub fn export_job(run: &Path, method: &str, model: &Value, quant: &str) -> Value {
    let out = super::run_out(run);
    let mut job = json!({
        "job": "export",
        "base": {"repo": model.get("repo"), "family": model.get("family")},
        "out": out.to_string_lossy(),
        "quant": quant,
        "gguf": true,
        "backend": "auto",
    });
    // qlora/lora leave an adapter dir; a full fine-tune leaves the whole model
    let trained = if method == "full" { out.join("model") } else { out.join("adapter") };
    if method == "full" {
        job["full_model"] = json!(trained.to_string_lossy());
    } else {
        job["adapter"] = json!(trained.to_string_lossy());
    }
    job
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::teacher::docs::{Entity, Relation};

    fn loaded() -> Loaded {
        Loaded {
            world_name: "Testworld".into(),
            entities: vec![
                Entity {
                    id: 1, kind: "npc".into(), subtype: "".into(), name: "Mara Vane".into(), aliases: vec!["Vane".into()],
                    tags: vec!["smuggler".into()], summary_raw: "A smuggler with a good heart.".into(),
                    body_raw: String::from("# Mara\n\nShe runs the *Dawn*.\n\n(GM secret: she owes the harbor master 200 coins.)").repeat(30),
                    roleplay_raw: String::new(), visible: true, custom_fields: vec![],
                    notes: vec![],
                },
                Entity { id: 2, kind: "location".into(), subtype: "city".into(), name: "Port Ashen".into(), aliases: vec![],
                    tags: vec![], summary_raw: "The fog city.".into(), body_raw: "A grey harbor town.".to_string().repeat(20),
                    roleplay_raw: String::new(), visible: true, custom_fields: vec![], notes: vec![] },
            ],
            relations: vec![Relation { source: "Mara Vane".into(), target: "Port Ashen".into(), relation: "lives in".into() }],
            rules: "## Dice\n\nRoll a d10.".repeat(30),
            sheet_rules: vec![],
            sessions: vec![json!({"id": 1, "session_num": 1, "session_date": "2030-01-01", "title": "Arrival",
                                   "summary": String::from("The party lands. ").repeat(40), "npcs": [], "loot": []})],
            facts: vec![json!({"id": 1, "content": String::from("The Dawn is fast. ").repeat(30), "visible_to_players": true})],
            quests: vec![],
            tables: vec![],
            characters: vec![],
            instructions: vec![json!({"id": 1, "title": "Tone", "content": "Keep answers short.", "applies_to_players": false})],
        }
    }

    #[test]
    fn text_rows_carry_the_world_in_chunks() {
        let l = loaded();
        let tmp = tempfile::tempdir().unwrap();
        let (text, chat) = train_rows(&l, tmp.path(), 7, false);
        assert!(!text.is_empty());
        assert!(chat.is_empty());
        // every row is a raw text row with a src, and the GM secret rides along (training is local, GM-trusted)
        let first = &text[0];
        assert_eq!(first["type"], json!("text"));
        assert!(first["src"].as_str().unwrap().starts_with("entity:"));
        let all: String = text.iter().filter_map(|r| r["text"].as_str()).collect();
        assert!(all.contains("GM secret"));
        assert!(all.contains("Mara Vane"));
        assert!(all.contains("Port Ashen"));
        assert!(all.contains("Session 1"));
        // the docs port strips access-tag syntax to its content: no raw [gmonly] brackets survive
        assert!(!all.contains("[gmonly]"));
        // chunks stay inside the preset's max_seq (4 chars per token)
        for r in &text {
            let chars = r["text"].as_str().unwrap().chars().count();
            assert!(chars <= 1100 * 4 + 200, "chunk of {chars} chars is too long");
        }
    }

    #[test]
    fn chat_rows_join_when_the_teacher_examples_are_included() {
        let l = loaded();
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("teacher").join("7");
        std::fs::create_dir_all(&dir).unwrap();
        let ex = json!({"id": "ab12", "builder": "t_lore", "status": "keep", "messages": [
            {"role": "system", "content": "s"}, {"role": "user", "content": "Who is Mara Vane?"}, {"role": "assistant", "content": "A smuggler."}]});
        std::fs::write(dir.join("examples.jsonl"), format!("{ex}\n")).unwrap();
        let (text, chat) = train_rows(&l, tmp.path(), 7, true);
        assert!(!text.is_empty());
        assert_eq!(chat.len(), 1);
        assert_eq!(chat[0]["src"], json!("teacher:ab12"));
    }

    #[test]
    fn probes_ask_about_entities_and_the_world() {
        let l = loaded();
        let p = probes(&l);
        assert_eq!(p.len(), 3); // Mara, Port Ashen, describe-the-world
        assert!(p[0]["question"].as_str().unwrap().starts_with("Who is Mara Vane"));
        assert!(p[1]["question"].as_str().unwrap().starts_with("What is Port Ashen"));
        assert_eq!(p[0]["system"], json!(crate::teacher::gen::system_prompt("Testworld")));
    }

    #[test]
    fn train_job_json_matches_the_worker_contract() {
        let model = json!({"repo": "unsloth/Qwen3.5-2B", "family": "qwen3_5"});
        let preset = super::super::preset("quick").unwrap();
        let none = json!({});
        let job = train_job(&model, &preset, "qlora", &none, Some(5), Path::new("/data/lore-forge/runs/r1/train.jsonl"), Path::new("/data/lore-forge/runs/r1/out"), &probes(&loaded()));
        assert_eq!(job["job"], json!("train"));
        assert_eq!(job["model"]["repo"], json!("unsloth/Qwen3.5-2B"));
        assert_eq!(job["method"], json!("qlora"));
        assert_eq!(job["hyper"]["epochs"], json!(5)); // the override wins over the preset's 2
        assert_eq!(job["hyper"]["rank"], json!(16));
        assert_eq!(job["hyper"]["logging_steps"], json!(5));
        assert_eq!(job["dataset"]["train"], json!("/data/lore-forge/runs/r1/train.jsonl"));
        assert_eq!(job["resume"], json!(false));
        // an absent override falls back to the preset
        let job2 = train_job(&model, &preset, "qlora", &none, None, Path::new("d"), Path::new("o"), &[]);
        assert_eq!(job2["hyper"]["epochs"], json!(2));
        // the page's hyperparameter fields win per key over the preset
        let job3 = train_job(&model, &preset, "rs_lora", &json!({"rank": 8, "lr": 5e-5}), None, Path::new("d"), Path::new("o"), &[]);
        assert_eq!(job3["hyper"]["rank"], json!(8));
        assert_eq!(job3["hyper"]["lr"], json!(5e-5));
        assert_eq!(job3["hyper"]["alpha"], json!(32)); // untouched
        assert_eq!(job3["method"], json!("rs_lora"));
    }

    #[test]
    fn export_job_points_at_the_right_artifact() {
        let model = json!({"repo": "unsloth/Qwen3.5-2B", "family": "qwen3_5"});
        let run = Path::new("/data/lore-forge/runs/r1");
        let job = export_job(run, "qlora", &model, "q4_k_m");
        assert_eq!(job["job"], json!("export"));
        assert_eq!(job["adapter"], json!("/data/lore-forge/runs/r1/out/adapter"));
        assert_eq!(job["quant"], json!("q4_k_m"));
        assert_eq!(job["gguf"], json!(true));
        let job_full = export_job(run, "full", &model, "q8_0");
        assert_eq!(job_full["full_model"], json!("/data/lore-forge/runs/r1/out/model"));
        assert!(job_full.get("adapter").is_none());
    }
}
