//! Hardware-aware recommendations for the Unsloth Studio export: which model and method fit a card, how long a context, which batch.
//! The memory figures are Unsloth's published ones for each model at a 2048 context and batch 1 (ND Lore Forge's catalog uses the same), scaled for a longer
//! context / bigger batch the way Lore Forge's plan does. They are estimates: an out-of-memory error means lower the context or the batch.
use serde_json::{json, Value};

pub struct ModelRow {
    pub name: &'static str,
    pub repo: &'static str,
    /// memory for a 16-bit LoRA / a 4-bit QLoRA at a 2048 context, batch 1, in GB
    pub lora_gb: f64,
    pub qlora_gb: f64,
}

/// In order of preference: the biggest model that still trains with a usable context.
pub const MODELS: [ModelRow; 5] = [
    ModelRow { name: "Qwen3.5 9B", repo: "unsloth/Qwen3.5-9B", lora_gb: 22.0, qlora_gb: 12.0 },
    ModelRow { name: "Gemma 4 E4B", repo: "unsloth/gemma-4-E4B-it", lora_gb: 17.0, qlora_gb: 10.0 },
    ModelRow { name: "Qwen3.5 4B", repo: "unsloth/Qwen3.5-4B", lora_gb: 10.0, qlora_gb: 6.5 },
    ModelRow { name: "Gemma 4 E2B", repo: "unsloth/gemma-4-E2B-it", lora_gb: 10.0, qlora_gb: 8.0 },
    ModelRow { name: "Qwen3.5 2B", repo: "unsloth/Qwen3.5-2B", lora_gb: 5.0, qlora_gb: 3.5 },
];

pub const LADDER: [usize; 5] = [2048, 4096, 8192, 16384, 32768];

/// The cards offered on the page (GB, label). "This machine" (detected) is added by the page.
pub const TIERS: [(f64, &str); 7] = [
    (8.0, "8 GB"),
    (12.0, "12 GB"),
    (16.0, "16 GB (Tesla T10 / V100 16 GB, RTX 4060 Ti 16 GB)"),
    (24.0, "24 GB (RTX 3090 / 4090)"),
    (32.0, "32 GB (RTX 5090, V100 32 GB)"),
    (48.0, "48 GB (RTX 6000 Ada, A6000)"),
    (80.0, "80 GB (A100 / H100)"),
];

/// Memory in GB for training `m` at `ctx` tokens with `batch` sequences per step.
pub fn est_gb(m: &ModelRow, qlora: bool, ctx: usize, batch: usize) -> f64 {
    let base = if qlora { m.qlora_gb } else { m.lora_gb };
    base * (0.85 + 0.15 * (ctx as f64 / 2048.0) * batch as f64)
}

/// What can really be used: the driver and the desktop take a share.
pub fn usable_gb(vram_gb: f64) -> f64 {
    vram_gb * 0.92 - 1.0
}

/// The model for this card: the first (biggest) whose QLoRA fits a 4096 context, else the first that fits 2048 at all.
fn base_model(vram_gb: f64) -> Option<&'static ModelRow> {
    let u = usable_gb(vram_gb);
    MODELS.iter().find(|m| est_gb(m, true, 4096, 1) <= u).or_else(|| MODELS.iter().find(|m| est_gb(m, true, 2048, 1) <= u))
}

/// The longest context this card trains the recommended model at (with QLoRA, the lightest method).
pub fn ctx_cap(vram_gb: f64) -> usize {
    let Some(m) = base_model(vram_gb) else { return 2048 };
    LADDER.iter().copied().filter(|c| est_gb(m, true, *c, 1) <= usable_gb(vram_gb)).last().unwrap_or(2048)
}

/// The context to put in "leave out examples longer than" when the page says "fit my hardware": what fits, but at most 8192 (very long examples are rare and expensive).
pub fn auto_max_tokens(vram_gb: f64) -> usize {
    ctx_cap(vram_gb).min(8192)
}

pub struct Plan {
    pub model: Option<&'static ModelRow>,
    /// "LoRA" (16-bit) or "QLoRA" (4-bit)
    pub method: &'static str,
    pub ctx: usize,
    pub est_gb: f64,
}

/// The recommendation for a card and the context the data needs (`needed_ctx`): the biggest model that fits, LoRA when it fits at that context, else QLoRA,
/// the context shortened to what fits.
pub fn plan(vram_gb: f64, needed_ctx: usize) -> Plan {
    let Some(m) = base_model(vram_gb) else { return Plan { model: None, method: "QLoRA", ctx: 2048, est_gb: 0.0 } };
    let u = usable_gb(vram_gb);
    let ctx = needed_ctx.min(ctx_cap(vram_gb)).max(2048);
    let batch = if ctx > 8192 || vram_gb <= 12.0 { 1 } else { 2 };
    let lora = est_gb(m, false, ctx, batch);
    // 16-bit LoRA only with some room to spare: the figures are estimates, and an out-of-memory error costs a run
    if lora <= u * 0.92 {
        Plan { model: Some(m), method: "LoRA", ctx, est_gb: lora }
    } else {
        Plan { model: Some(m), method: "QLoRA", ctx, est_gb: est_gb(m, true, ctx, batch) }
    }
}

/// What the card is and what to know about it (no bf16 on Volta / Turing, Studio needs NVIDIA).
pub fn notes_for(name: &str) -> Vec<String> {
    let n = name.to_lowercase();
    let mut out = Vec::new();
    if ["v100", "volta", "titan v", "t10", "t4 ", "tesla t4", "rtx 20", "2080", "2070", "2060", "quadro rtx", "gtx 16"].iter().any(|k| n.contains(k)) {
        out.push("This card has no bf16 (Volta / Turing): training runs in fp16, a little slower and less forgiving. If the loss turns into nan, lower the learning rate by half.".to_string());
    }
    if n.contains("radeon") || n.contains("amd") || n.contains("instinct") {
        out.push("Unsloth Studio trains on NVIDIA cards; an AMD card is not supported for training here.".to_string());
    }
    out
}

/// The hardware the settings are for.
#[derive(Clone)]
pub struct Hardware {
    pub vram_gb: f64,
    /// "detected" (nd-world saw the card), "chosen" (picked on the page) or "assumed" (nothing detected, nothing chosen)
    pub source: &'static str,
    pub label: String,
    pub name: String,
    /// what nd-world detected on its own machine, as the Settings page reports it
    pub detected: Value,
}

fn detected_best(detected: &Value) -> Option<(String, f64)> {
    let gpus = detected["gpus"].as_array()?;
    gpus.iter().filter_map(|g| Some((g["name"].as_str().unwrap_or("GPU").to_string(), g["vram_mb"].as_f64()? / 1024.0))).filter(|(_, v)| *v > 0.5)
        .max_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
}

/// `chosen_gb`: the card picked on the page (None / 0 = this machine's own card).
pub fn resolve(detected: &Value, chosen_gb: Option<f64>) -> Hardware {
    if let Some(gb) = chosen_gb.filter(|g| *g > 0.0) {
        let tier = TIERS.iter().find(|(t, _)| (*t - gb).abs() < 0.01).map(|(_, l)| l.to_string()).unwrap_or(format!("{gb:.0} GB"));
        return Hardware { vram_gb: gb, source: "chosen", label: format!("{tier} (chosen)"), name: String::new(), detected: detected.clone() };
    }
    if let Some((name, gb)) = detected_best(detected) {
        return Hardware { vram_gb: gb, source: "detected", label: format!("{name}, {gb:.0} GB (detected on the machine nd-world runs on)"), name, detected: detected.clone() };
    }
    Hardware { vram_gb: 24.0, source: "assumed", label: "24 GB (assumed: no graphics card was detected on this machine)".to_string(), name: String::new(), detected: detected.clone() }
}

/// The hardware part of the page's info.
pub fn info(h: &Hardware) -> Value {
    let gpus: Vec<Value> = h.detected["gpus"].as_array().map(|a| a.iter().map(|g| json!({"name": g["name"], "vram_gb": g["vram_mb"].as_f64().map(|m| (m / 1024.0 * 10.0).round() / 10.0)})).collect()).unwrap_or_default();
    json!({"label": h.label, "vram_gb": h.vram_gb, "source": h.source, "gpus": gpus, "ram_gb": h.detected["ram_total_mb"].as_f64().map(|m| (m / 1024.0).round()),
           "cpu": h.detected["cpu_model"], "notes": notes_for(&h.name)})
}
