//! app/fan_control.py owned by the gateway (`fans` in ND_RUST_ROUTES; Python's own loop stands down with ND_FANS_OWNER=gateway): the
//! motherboard fan headers through Linux hwmon (FAN_HWMON_ROOT, default /sys/class/hwmon), a fan held at a speed or given back to the
//! BIOS, the "is this output a fan that really speeds up?" check, and "follow the GPU temperature" - a loop every 5 s that moves the
//! chosen fans along a curve, at 100 % when the GPU is unreadable or at/over 80 C, slowing down gently. The saved settings
//! (fan_control.json) and what the BIOS had set (fan_control_orig.json, per boot) are the same files beside the database Python uses.
//! On shutdown every fan this app holds below 60 % is raised to 60 %.

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, Uri};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use crate::config::Config;

const MIN_PERCENT: i64 = 20;
const PANIC_TEMP: f64 = 80.0;
const TOP_PERCENT: i64 = 80;
const TOP_MARGIN: i64 = 5;
const LOOP_SECONDS: u64 = 5;
const RAMP_DOWN: i64 = 5;
const SHUTDOWN_PERCENT: i64 = 60;
const CHECK_SECONDS: f64 = 6.0;
const HOLD_SETTLE: f64 = 1.2;
const MAX_POINTS: usize = 8;

#[derive(Default)]
struct State {
    /// fan id -> (verdict, time) of this run's checks
    checked: Vec<(String, String)>,
    /// fan id -> the raw duty last written
    written: HashMap<String, i64>,
    /// the last follow tick, for the page
    last: Value,
    /// the follow loop
    task: Option<tokio::task::JoinHandle<()>>,
}

fn st() -> &'static Mutex<State> {
    static S: OnceLock<Mutex<State>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(State { last: Value::Null, ..Default::default() }))
}

fn root() -> String {
    std::env::var("FAN_HWMON_ROOT").ok().filter(|r| !r.is_empty()).unwrap_or_else(|| "/sys/class/hwmon".into())
}

fn data_dir(cfg: &Config) -> PathBuf {
    cfg.db_path.parent().map(|p| p.to_path_buf()).unwrap_or_default()
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `round()`: ties to even
fn pyround(x: f64) -> i64 {
    x.round_ties_even() as i64
}

fn read(p: &str) -> String {
    use std::io::Read;
    let mut buf = vec![0u8; 4096];
    match std::fs::File::open(p).and_then(|mut f| f.read(&mut buf)) {
        Ok(n) => String::from_utf8_lossy(&buf[..n]).trim().to_string(),
        Err(_) => String::new(),
    }
}

fn int(t: &str) -> Option<i64> {
    t.trim().parse::<i64>().ok()
}

fn write(p: &str, v: i64) -> std::io::Result<()> {
    std::fs::write(p, v.to_string())
}

fn clean_name(t: &str) -> String {
    let s: String = t.chars().map(|c| if c.is_ascii_alphanumeric() || "_.+-".contains(c) { c } else { '_' }).take(40).collect();
    if s.is_empty() { "chip".into() } else { s }
}

fn writable(p: &str) -> bool {
    let Ok(c) = std::ffi::CString::new(p) else { return false };
    unsafe { libc::access(c.as_ptr(), libc::W_OK) == 0 }
}

/// `list_fans`: every PWM output hwmon exposes
fn list_fans(root: &str) -> Vec<Map<String, Value>> {
    let mut out = vec![];
    let Ok(rd) = std::fs::read_dir(root) else { return out };
    let mut chips: Vec<String> = rd.flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect();
    chips.sort();
    for chip in chips {
        let base = format!("{root}/{chip}");
        let name = Some(read(&format!("{base}/name"))).filter(|n| !n.is_empty()).unwrap_or_else(|| chip.clone());
        let device = std::fs::read_link(format!("{base}/device")).ok()
            .and_then(|l| Path::new(l.to_string_lossy().trim_end_matches('/')).file_name().map(|f| f.to_string_lossy().to_string())).unwrap_or_default();
        let place = if device.is_empty() { chip.clone() } else { device };
        let Ok(files) = std::fs::read_dir(&base) else { continue };
        let mut files: Vec<String> = files.flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect();
        files.sort();
        for f in files {
            let Some(n) = f.strip_prefix("pwm").filter(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit())) else { continue };
            let Some(raw) = int(&read(&format!("{base}/pwm{n}"))) else { continue };
            let enable = int(&read(&format!("{base}/pwm{n}_enable")));
            let label = read(&format!("{base}/fan{n}_label"));
            let mut m = Map::new();
            m.insert("id".into(), json!(format!("{}@{}/pwm{n}", clean_name(&name), clean_name(&place))));
            m.insert("dir".into(), json!(chip));
            m.insert("chip".into(), json!(name));
            m.insert("channel".into(), json!(n.parse::<i64>().unwrap_or(0)));
            m.insert("label".into(), json!(if label.is_empty() { format!("Fan {n}") } else { label }));
            m.insert("rpm".into(), json!(int(&read(&format!("{base}/fan{n}_input")))));
            m.insert("percent".into(), json!(pyround(raw.clamp(0, 255) as f64 * 100.0 / 255.0)));
            m.insert("raw".into(), json!(raw));
            m.insert("enable".into(), json!(enable));
            m.insert("manual".into(), json!(enable == Some(1)));
            m.insert("writable".into(), json!(writable(&format!("{base}/pwm{n}"))));
            out.push(m);
        }
    }
    out
}

/// `_find`: (directory, channel text, info) of a listed fan
fn find(fan: &str, root: &str) -> Option<(String, String, Map<String, Value>)> {
    list_fans(root).into_iter().find(|f| f["id"] == json!(fan)).map(|f| {
        let n = f["id"].as_str().and_then(|i| i.rsplit_once("/pwm")).map(|(_, n)| n.to_string()).unwrap_or_default();
        (format!("{root}/{}", f["dir"].as_str().unwrap_or("")), n, f)
    })
}

fn boot_id() -> String {
    Some(read("/proc/sys/kernel/random/boot_id")).filter(|b| !b.is_empty()).unwrap_or_else(|| "unknown".into())
}

fn load_orig(dir: &Path) -> Map<String, Value> {
    match std::fs::read_to_string(dir.join("fan_control_orig.json")).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
        Some(Value::Object(d)) if d.get("boot") == Some(&json!(boot_id())) => d.get("fans").and_then(|f| f.as_object()).cloned().unwrap_or_default(),
        _ => Map::new(),
    }
}

fn save_orig(dir: &Path, fans: &Map<String, Value>) -> std::io::Result<()> {
    let tmp = dir.join("fan_control_orig.tmp");
    std::fs::write(&tmp, nd_render::py_dumps(&json!({"boot": boot_id(), "fans": fans})))?;
    std::fs::rename(tmp, dir.join("fan_control_orig.json"))
}

fn oserr(e: &std::io::Error) -> String {
    // Python's str(OSError): "[Errno 13] Permission denied: '<path>'" - here the errno and its text
    let t = format!("[Errno {}] {}", e.raw_os_error().unwrap_or(0), e.to_string().split(" (os error").next().unwrap_or(""));
    t.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(120).collect()
}

/// `_apply`: manual mode at `raw` (the mode written only when it is not manual already)
fn apply(base: &str, n: &str, raw: i64) -> std::io::Result<()> {
    let en = format!("{base}/pwm{n}_enable");
    if Path::new(&en).exists() && int(&read(&en)) != Some(1) {
        write(&en, 1)?;
    }
    write(&format!("{base}/pwm{n}"), raw)
}

/// `set_percent(fan_id, percent, settle=...)`
fn set_percent(dir: &Path, fan: &str, percent: Option<&Value>, settle: f64) -> Value {
    let p = match percent {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(f64::NAN),
        _ => return json!({"ok": false, "message": "percent must be a number."}),
    };
    let percent = pyround(p).clamp(MIN_PERCENT, 100);
    let r = root();
    let Some((base, n, info)) = find(fan, &r) else { return json!({"ok": false, "message": "There is no such fan (the driver may not be loaded)."}) };
    if info["writable"] != json!(true) {
        return json!({"ok": false, "message": "This fan is read-only here - the container needs write access to the fan chip's folder (see docs/GPU_SETUP.md, Fan control)."});
    }
    let target = pyround(percent as f64 * 255.0 / 100.0);
    let mut saved = load_orig(dir);
    if !saved.contains_key(fan) {
        saved.insert(fan.into(), json!({"enable": info["enable"], "pwm": info["raw"]}));
        if let Err(e) = save_orig(dir, &saved) {
            return json!({"ok": false, "message": format!("Could not write down what the BIOS had set ({}), so the fan was left alone.", oserr(&e))});
        }
    }
    if let Err(e) = apply(&base, &n, target) {
        return json!({"ok": false, "message": format!("The fan chip refused the change: {}", oserr(&e))});
    }
    if let Ok(mut s) = st().lock() {
        s.written.insert(fan.into(), target);
    }
    if settle > 0.0 {
        std::thread::sleep(Duration::from_secs_f64(settle));
    }
    if let Some(got) = int(&read(&format!("{base}/pwm{n}"))) {
        if (got - target).abs() > 8 {
            let later = if settle > 0.0 { format!(" {} s later", nd_render::py_float_repr(settle).trim_end_matches(".0")) } else { String::new() };
            return json!({"ok": true, "warning": true, "percent": percent, "message": format!("Asked for {percent} % but the chip reads {} %{later} - the BIOS / the board's firmware is changing this header, so it will not hold.", pyround(got as f64 * 100.0 / 255.0))});
        }
    }
    json!({"ok": true, "message": format!("Fan held at {percent} %."), "percent": percent})
}

/// A stored duty or mode as `int(value)` reads it
fn as_int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::String(s) => int(s),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

/// `give_back`: the header put back as the BIOS had it
fn give_back(dir: &Path, fan: &str) -> Value {
    let r = root();
    let Some((base, n, info)) = find(fan, &r) else { return json!({"ok": false, "message": "There is no such fan (the driver may not be loaded)."}) };
    if info["writable"] != json!(true) {
        return json!({"ok": false, "message": "This fan is read-only here."});
    }
    let mut saved = load_orig(dir);
    let Some(note) = saved.get(fan).cloned() else {
        return json!({"ok": true, "message": "This fan is not held by nd-world; it is on whatever the BIOS set. (If you changed it by hand, a reboot gives it back.)"});
    };
    let res = (|| -> std::io::Result<()> {
        if let Some(p) = note.get("pwm").filter(|v| !v.is_null()).and_then(as_int) {
            write(&format!("{base}/pwm{n}"), p)?;
        }
        let en = format!("{base}/pwm{n}_enable");
        if let Some(e) = note.get("enable").filter(|v| !v.is_null()).and_then(as_int) {
            if Path::new(&en).exists() {
                write(&en, e)?;
            }
        }
        Ok(())
    })();
    if let Err(e) = res {
        return json!({"ok": false, "message": format!("The fan chip refused the change: {}", oserr(&e))});
    }
    saved.remove(fan);
    if let Ok(mut s) = st().lock() {
        s.written.remove(fan);
    }
    let _ = save_orig(dir, &saved);
    json!({"ok": true, "message": "Put back as the BIOS had it."})
}

fn set_checked(fan: &str, verdict: &str) {
    if let Ok(mut s) = st().lock() {
        s.checked.retain(|(f, _)| f != fan);
        s.checked.push((fan.into(), verdict.into()));
    }
}

fn verdict(fan: &str) -> String {
    st().lock().ok().and_then(|s| s.checked.iter().find(|(f, _)| f == fan).map(|(_, v)| v.clone())).unwrap_or_default()
}

/// `check_fan`: 100 % for a few seconds, the rpm compared, the output put back exactly as it was
fn check_fan(dir: &Path, fan: &str) -> Value {
    let r = root();
    let Some((base, n, info)) = find(fan, &r) else { return json!({"ok": false, "verdict": "", "message": "There is no such fan (the driver may not be loaded)."}) };
    if info["writable"] != json!(true) {
        return json!({"ok": false, "verdict": "", "message": "This fan is read-only here."});
    }
    let cfg = load_config(dir);
    if cfg["follow_gpu"] == json!(true) && cfg["fan_ids"].as_array().is_some_and(|a| a.contains(&json!(fan))) {
        return json!({"ok": false, "verdict": "", "message": "This fan is following the GPU right now; stop that first so the check is not fought."});
    }
    let Some(before) = info["rpm"].as_i64() else {
        return json!({"ok": false, "verdict": "no_signal", "message": "This output has no speed (rpm) signal, so it cannot be checked here - listen to the fan while you test it."});
    };
    if before == 0 {
        return json!({"ok": false, "verdict": "no_signal", "message": "0 rpm: nothing spinning on this output (or its speed wire is not connected)."});
    }
    if info["percent"].as_i64().unwrap_or(0) >= 95 {
        return json!({"ok": false, "verdict": "", "message": "It is already at full speed, so a speed-up cannot be seen. Lower it first (not a pump!) or check it when it is slower."});
    }
    let (prev_enable, prev_raw) = (info["enable"].as_i64(), info["raw"].as_i64().unwrap_or(0));
    let steps = (pyround(CHECK_SECONDS)).max(1);
    if let Err(e) = apply(&base, &n, 255) {
        return json!({"ok": false, "verdict": "", "message": format!("The fan chip refused the change: {}", oserr(&e))});
    }
    let mut drifted = None;
    for _ in 0..steps {
        std::thread::sleep(Duration::from_secs_f64(CHECK_SECONDS / steps as f64));
        if let Some(now) = int(&read(&format!("{base}/pwm{n}"))) {
            if (now - 255).abs() > 8 {
                drifted = Some(now);
            }
        }
    }
    let after = int(&read(&format!("{base}/fan{n}_input")));
    let _ = write(&format!("{base}/pwm{n}"), prev_raw);
    let en = format!("{base}/pwm{n}_enable");
    if let Some(e) = prev_enable {
        if Path::new(&en).exists() {
            let _ = write(&en, e);
        }
    }
    if let Some(d) = drifted {
        set_checked(fan, "not_held");
        return json!({"ok": true, "verdict": "not_held", "rpm_before": before, "rpm_after": after,
            "message": format!("I set 100 % but the chip read {} % while I watched: the board's firmware is changing this output, so software cannot hold it. Do not use it to cool anything.", pyround(d as f64 * 100.0 / 255.0))});
    }
    let Some(after) = after else { return json!({"ok": false, "verdict": "no_signal", "message": "The speed signal disappeared during the check."}) };
    let ratio = after as f64 / before as f64;
    let (v, msg) = if ratio >= 1.10 {
        ("speeds_up", format!("{before} → {after} rpm (+{} %): this output speeds the fan up when asked.", pyround((ratio - 1.0) * 100.0)))
    } else if ratio <= 0.90 {
        ("inverted", format!("{before} → {after} rpm: it got SLOWER at 100 %. This output looks inverted - do not use it to cool anything."))
    } else {
        ("no_change", format!("{before} → {after} rpm: no clear change. It may ignore PWM (a pump, or a fixed-speed fan) - do not rely on it."))
    };
    set_checked(fan, v);
    json!({"ok": true, "verdict": v, "message": msg, "rpm_before": before, "rpm_after": after})
}

/// `clean_curve`
fn clean_curve(raw: Option<&Value>) -> Option<Vec<[i64; 2]>> {
    let Some(Value::Array(list)) = raw else { return None };
    if !(2..=MAX_POINTS).contains(&list.len()) {
        return None;
    }
    let mut pts = vec![];
    for p in list {
        let Value::Array(pair) = p else { return None };
        if pair.len() != 2 || !pair.iter().all(|v| v.is_number()) {
            return None;
        }
        let (t, pct) = (pair[0].as_f64()?, pair[1].as_f64()?);
        if !(20.0..=110.0).contains(&t) {
            return None;
        }
        pts.push([pyround(t), pyround(pct).clamp(MIN_PERCENT, 100)]);
    }
    pts.sort_by_key(|q| q[0]);
    if pts.windows(2).any(|w| w[1][0] <= w[0][0] || w[1][1] < w[0][1]) {
        return None;
    }
    let last = pts[pts.len() - 1];
    if last[1] < TOP_PERCENT || last[0] as f64 > PANIC_TEMP - TOP_MARGIN as f64 {
        return None;
    }
    Some(pts)
}

fn default_curve() -> Vec<[i64; 2]> {
    vec![[45, 35], [55, 55], [65, 80], [72, 100]]
}

/// `percent_for`
fn percent_for(temp: Option<f64>, curve: &[[i64; 2]]) -> i64 {
    let Some(t) = temp.filter(|t| !t.is_nan() && *t < PANIC_TEMP) else { return 100 };
    if t <= curve[0][0] as f64 {
        return curve[0][1];
    }
    for w in curve.windows(2) {
        let ([t0, p0], [t1, p1]) = (w[0], w[1]);
        if t <= t1 as f64 {
            return pyround(p0 as f64 + (p1 - p0) as f64 * (t - t0 as f64) / (t1 - t0) as f64);
        }
    }
    curve[curve.len() - 1][1]
}

fn smooth(current: Option<i64>, target: i64) -> i64 {
    match current {
        Some(c) if target < c => target.max(c - RAMP_DOWN),
        _ => target,
    }
}

/// `load_config`: {follow_gpu, fan_ids, curve}
fn load_config(dir: &Path) -> Value {
    let data = match std::fs::read_to_string(dir.join("fan_control.json")).ok().and_then(|t| crate::map_layout::py_loads(&t).ok()) {
        Some(Value::Object(d)) => d,
        _ => Map::new(),
    };
    let ids: Vec<Value> = match data.get("fan_ids") {
        Some(Value::Array(a)) => a.iter().filter(|x| x.is_string()).take(8).cloned().collect(),
        Some(Value::String(s)) => s.chars().take(8).map(|c| json!(c.to_string())).collect(),
        Some(Value::Object(o)) => o.keys().take(8).map(|k| json!(k)).collect(),
        _ => vec![],
    };
    json!({"follow_gpu": crate::edits::py_truthy_pub(data.get("follow_gpu")), "fan_ids": ids,
        "curve": clean_curve(data.get("curve")).unwrap_or_else(default_curve)})
}

fn save_config(dir: &Path, cfg: &Value) -> std::io::Result<()> {
    let tmp = dir.join("fan_control.tmp");
    std::fs::write(&tmp, nd_render::py_dumps(cfg))?;
    std::fs::rename(tmp, dir.join("fan_control.json"))
}

fn curve_of(cfg: &Value) -> Vec<[i64; 2]> {
    clean_curve(cfg.get("curve")).unwrap_or_else(default_curve)
}

/// `apply_once(gpu_temp)`: one tick of "follow the GPU"
fn apply_once(dir: &Path, temp: Option<f64>) -> Option<i64> {
    let cfg = load_config(dir);
    let ids: Vec<String> = cfg["fan_ids"].as_array()?.iter().filter_map(|v| v.as_str().map(str::to_string)).collect();
    if cfg["follow_gpu"] != json!(true) || ids.is_empty() {
        return None;
    }
    let target = percent_for(temp, &curve_of(&cfg));
    let by_id: HashMap<String, Map<String, Value>> = list_fans(&root()).into_iter().map(|f| (f["id"].as_str().unwrap_or("").to_string(), f)).collect();
    let (mut applied, mut errors, mut warnings) = (Map::new(), vec![], vec![]);
    for fid in &ids {
        let Some(f) = by_id.get(fid) else {
            errors.push(format!("{fid}: not found (is the fan-chip driver loaded?)"));
            continue;
        };
        let manual = f["manual"] == json!(true);
        let (pct, raw) = (f["percent"].as_i64().unwrap_or(0), f["raw"].as_i64().unwrap_or(0));
        let last = st().lock().ok().and_then(|s| s.written.get(fid).copied());
        if let Some(l) = last {
            if !manual || (raw - l).abs() > 8 {
                warnings.push(format!("{fid}: the chip reads {pct} % but nd-world set {} % a moment ago - something else (the BIOS / the board's firmware) is changing this output, so it may not be holding", pyround(l as f64 * 100.0 / 255.0)));
            }
        }
        let current = manual.then_some(pct);
        let want = smooth(current, target);
        if current.is_some_and(|c| (want - c).abs() <= 1) {
            applied.insert(fid.clone(), json!(current));
            continue;
        }
        let r = set_percent(dir, fid, Some(&json!(want)), 0.0);
        if r["ok"] == json!(true) {
            applied.insert(fid.clone(), json!(want));
        } else {
            errors.push(format!("{fid}: {}", r["message"].as_str().unwrap_or("")));
        }
    }
    if let Ok(mut s) = st().lock() {
        s.last = json!({"t": now(), "gpu_temp": temp, "target": target, "applied": applied, "errors": errors, "warnings": warnings});
    }
    Some(target)
}

/// `gpu_temperature`: the hottest GPU's die temperature, or None
fn gpu_temperature() -> Option<f64> {
    let out = std::process::Command::new("nvidia-smi").args(["--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"])
        .stderr(std::process::Stdio::null()).output().ok().filter(|o| o.status.success())?;
    String::from_utf8_lossy(&out.stdout).lines().filter_map(|l| l.trim().parse::<f64>().ok()).filter(|f| f.is_finite()).fold(None, |m: Option<f64>, t| Some(m.map_or(t, |m| m.max(t))))
}

fn running() -> bool {
    st().lock().ok().is_some_and(|s| s.task.as_ref().is_some_and(|t| !t.is_finished()))
}

/// `start()`: the follow loop, when it is saved as on
pub fn start(cfg: &Config) {
    let dir = data_dir(cfg);
    if running() || load_config(&dir)["follow_gpu"] != json!(true) {
        return;
    }
    let task = tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(LOOP_SECONDS)).await;
            let d = dir.clone();
            let more = tokio::task::spawn_blocking(move || {
                if load_config(&d)["follow_gpu"] != json!(true) {
                    return false; // switched off: the loop ends
                }
                apply_once(&d, gpu_temperature());
                true
            }).await.unwrap_or(true);
            if !more {
                break;
            }
        }
    });
    if let Ok(mut s) = st().lock() {
        s.task = Some(task);
    }
}

fn stop() {
    if let Ok(mut s) = st().lock() {
        if let Some(t) = s.task.take() {
            t.abort();
        }
    }
}

/// `shutdown_safe()`: no fan this app holds is left under SHUTDOWN_PERCENT
pub fn shutdown_safe(cfg: &Config) {
    let dir = data_dir(cfg);
    let held: Vec<String> = load_orig(&dir).keys().cloned().collect();
    if held.is_empty() {
        return;
    }
    let by_id: HashMap<String, i64> = list_fans(&root()).into_iter().map(|f| (f["id"].as_str().unwrap_or("").to_string(), f["percent"].as_i64().unwrap_or(0))).collect();
    for fid in held {
        if by_id.get(&fid).is_some_and(|p| *p < SHUTDOWN_PERCENT) {
            set_percent(&dir, &fid, Some(&json!(SHUTDOWN_PERCENT)), 0.0);
        }
    }
}

/// `status()`
fn status(dir: &Path) -> Value {
    let cfg = load_config(dir);
    let r = root();
    let mut fans = list_fans(&r);
    let held = load_orig(dir);
    for f in fans.iter_mut() {
        let id = f["id"].as_str().unwrap_or("").to_string();
        f.insert("held".into(), json!(held.contains_key(&id)));
    }
    let ids: Vec<Value> = fans.iter().map(|f| f["id"].clone()).collect();
    let missing: Vec<Value> = cfg["fan_ids"].as_array().cloned().unwrap_or_default().into_iter().filter(|i| !ids.contains(i)).collect();
    let (checked, last) = st().lock().map(|s| (s.checked.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<Map<String, Value>>(), s.last.clone()))
        .unwrap_or_default();
    json!({"fans": fans, "follow_gpu": cfg["follow_gpu"], "fan_ids": cfg["fan_ids"], "curve": cfg["curve"], "missing": missing,
        "any_writable": fans.iter().any(|f| f["writable"] == json!(true)), "min_percent": MIN_PERCENT, "panic_temp": PANIC_TEMP,
        "top_percent": TOP_PERCENT, "top_margin": TOP_MARGIN, "shutdown_percent": SHUTDOWN_PERCENT, "checked": checked,
        "root_exists": Path::new(&r).is_dir(), "state": last, "running": running(), "time": now()})
}

fn answer(status: StatusCode, v: &Value, no_store: bool) -> Response<Body> {
    let mut b = Response::builder().status(status).header("content-type", "application/json").header("x-nd-served-by", "gateway");
    if no_store {
        b = b.header("cache-control", "no-store");
    }
    b.body(Body::from(serde_json::to_string(v).unwrap_or_default())).unwrap()
}

fn refuse(msg: &str) -> Response<Body> {
    answer(StatusCode::BAD_REQUEST, &json!({"detail": msg}), false)
}

/// `/api/system/fans` (GET, POST) and `/api/system/fans/check` for a GM; None for anyone else or a body that is not JSON (Python's)
pub async fn route(cfg: Arc<Config>, method: axum::http::Method, uri: Uri, headers: HeaderMap, body: bytes::Bytes) -> Option<Response<Body>> {
    let (c, u, h, m) = (cfg.clone(), uri.clone(), headers.clone(), method.to_string());
    let gm = tokio::task::spawn_blocking(move || -> Option<bool> {
        let db = crate::writes::open(&c)?;
        let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
        Some(crate::layout::build(&db, &c, &m, &u, &h, n)?.is_gm)
    }).await.ok()??;
    if !gm {
        return None; // the 403: Python's
    }
    let dir = data_dir(&cfg);
    if method == axum::http::Method::GET {
        return Some(answer(StatusCode::OK, &tokio::task::spawn_blocking(move || status(&dir)).await.ok()?, true));
    }
    let Ok(Value::Object(b)) = serde_json::from_slice::<Value>(&body) else { return None };
    let check = uri.path().ends_with("/check");
    let c2 = cfg.clone();
    tokio::task::spawn_blocking(move || -> Option<Response<Body>> {
        let res = |v: Value| { let ok = v["ok"] == json!(true); answer(if ok { StatusCode::OK } else { StatusCode::BAD_REQUEST }, &v, false) };
        if check {
            let Some(Value::String(fan)) = b.get("fan") else { return Some(refuse("fan is required")) };
            return Some(res(check_fan(&dir, fan)));
        }
        if let Some(follow) = b.get("follow_gpu") {
            let prev = load_config(&dir);
            let ids = match b.get("fans") {
                None => prev["fan_ids"].as_array().cloned().unwrap_or_default(),
                Some(Value::Array(a)) if a.iter().all(|i| i.is_string()) => a.clone(),
                _ => return Some(refuse("fans must be a list of fan ids")),
            };
            let on = crate::edits::py_truthy_pub(Some(follow));
            if on {
                let known: Vec<Value> = list_fans(&root()).iter().map(|f| f["id"].clone()).collect();
                if ids.iter().any(|i| !known.contains(i)) {
                    return Some(refuse("Unknown fan (is the fan-chip driver loaded?)"));
                }
            }
            let curve = if b.contains_key("curve") { clean_curve(b.get("curve")) } else { Some(curve_of(&prev)) };
            let Some(curve) = curve else {
                return Some(refuse(&format!("The curve needs 2-8 points: rising temperatures and fan speeds that never fall, and it must reach at least {TOP_PERCENT} % by {} C.",
                    PANIC_TEMP as i64 - TOP_MARGIN)));
            };
            if on && ids.is_empty() {
                return Some(refuse("Pick at least one fan to follow the GPU."));
            }
            if on && ids.iter().any(|i| matches!(verdict(i.as_str().unwrap_or("")).as_str(), "inverted" | "not_held")) {
                return Some(refuse("The check found that output unusable (its rpm falls at a higher duty, or the board's firmware changes the value back) - it must not follow the GPU."));
            }
            save_config(&dir, &json!({"follow_gpu": on, "fan_ids": ids, "curve": curve})).ok()?;
            let mut notes: Vec<String> = vec![];
            if prev["follow_gpu"] == json!(true) {
                for fid in prev["fan_ids"].as_array().cloned().unwrap_or_default() {
                    if !on || !ids.contains(&fid) {
                        notes.push(give_back(&dir, fid.as_str().unwrap_or(""))["message"].as_str().unwrap_or("").to_string());
                    }
                }
            }
            let msg = if on {
                apply_once(&dir, gpu_temperature());
                "Following the GPU temperature."
            } else {
                stop();
                "Stopped following the GPU."
            };
            notes.sort();
            notes.dedup();
            let mut parts = vec![msg.to_string()];
            parts.extend(notes);
            if on {
                let c3 = c2.clone();
                tokio::runtime::Handle::current().spawn(async move { start(&c3) });
            }
            return Some(answer(StatusCode::OK, &json!({"ok": true, "message": parts.join(" ")}), false));
        }
        let Some(Value::String(fan)) = b.get("fan") else { return Some(refuse("fan is required")) };
        let before = load_config(&dir);
        // _drop_from_following: the loop must not undo what is about to be done
        let mut was_following = false;
        if let Some(list) = before["fan_ids"].as_array().filter(|l| l.contains(&json!(fan))) {
            let rest: Vec<Value> = list.iter().filter(|i| *i != &json!(fan)).cloned().collect();
            let mut cfg2 = before.clone();
            cfg2["follow_gpu"] = json!(before["follow_gpu"] == json!(true) && !rest.is_empty());
            cfg2["fan_ids"] = json!(rest);
            save_config(&dir, &cfg2).ok()?;
            was_following = before["follow_gpu"] == json!(true);
        }
        let result = if crate::edits::py_truthy_pub(b.get("auto")) {
            give_back(&dir, fan)
        } else {
            let mut r = set_percent(&dir, fan, b.get("percent"), HOLD_SETTLE);
            if r["ok"] == json!(true) && was_following {
                let m = format!("{} It no longer follows the GPU.", r["message"].as_str().unwrap_or(""));
                r["message"] = json!(m);
            }
            r
        };
        if result["ok"] != json!(true) && was_following {
            save_config(&dir, &before).ok()?;
        }
        Some(res(result))
    }).await.ok()?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn curves() {
        assert_eq!(clean_curve(Some(&json!([[45, 35], [72.4, 100]]))), Some(vec![[45, 35], [72, 100]]));
        assert_eq!(clean_curve(Some(&json!([[45, 35], [80, 100]]))), None);
        assert_eq!(clean_curve(Some(&json!([[45, true], [70, 100]]))), None);
        let c = default_curve();
        assert_eq!((percent_for(None, &c), percent_for(Some(30.0), &c), percent_for(Some(50.0), &c), percent_for(Some(85.0), &c)), (100, 35, 45, 100));
        assert_eq!((smooth(Some(90), 50), smooth(Some(40), 70), smooth(None, 30)), (85, 70, 30));
    }
}
