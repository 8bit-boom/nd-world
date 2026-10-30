//! `GET /api/system/stats` (app/system_stats.py): the System Monitor's live reading - CPU (busy percentage between two /proc/stat samples, per
//! core, load, temperature, model), memory (/proc/meminfo, the ZFS ARC) and the GPUs (`nvidia-smi`, read-only). Briefly cached (1 s) like
//! Python's; the CPU percentage needs the previous sample, so the first reading waits a quarter second for a second one.

use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

type Times = (Option<[i64; 2]>, Vec<[i64; 2]>);

static PREV_CPU: Mutex<Option<Times>> = Mutex::new(None);
static CACHE: Mutex<Option<(Instant, f64, Value)>> = Mutex::new(None);

const GPU_FIELDS: [&str; 17] = [
    "index", "name", "uuid", "utilization.gpu", "utilization.memory", "memory.used", "memory.total", "temperature.gpu", "power.draw", "power.limit", "fan.speed",
    "clocks.sm", "clocks.max.sm", "pstate", "power.min_limit", "power.max_limit", "power.default_limit",
];
const APP_FIELDS: [&str; 4] = ["pid", "process_name", "used_memory", "gpu_uuid"];

fn read(path: &str) -> String {
    use std::io::Read;
    let mut buf = Vec::new();
    if let Ok(f) = std::fs::File::open(path) {
        let _ = f.take(262144).read_to_end(&mut buf);
    }
    String::from_utf8_lossy(&buf).into_owned()
}

/// `_num`: a finite float from text such as '61' or '39.12'; None for '[N/A]', '' and the like
fn num(v: &str) -> Option<f64> {
    let t = v.trim();
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit() || matches!(c, b'.' | b'-' | b'+' | b'e' | b'E')) {
        return None;
    }
    t.parse::<f64>().ok().filter(|f| f.is_finite())
}

fn round1(v: f64) -> f64 {
    format!("{v:.1}").parse().unwrap_or(v)
}

/// A float as Python's JSON writes it (a whole one keeps its ".0")
fn f(v: f64) -> Value {
    json!(v)
}

fn opt(v: Option<f64>) -> Value {
    v.map(f).unwrap_or(Value::Null)
}

/// `parse_cpu_times`: ([total, idle] for all CPUs, [[total, idle] per core]); idle includes iowait
fn parse_cpu_times(text: &str) -> Times {
    let (mut all, mut cores) = (None, vec![]);
    for line in text.lines() {
        let Some(rest) = line.strip_prefix("cpu") else { continue };
        let digits = rest.bytes().take_while(|c| c.is_ascii_digit()).count();
        let after = &rest[digits..];
        if !after.starts_with(|c: char| c.is_whitespace()) {
            continue;
        }
        let nums: Vec<i64> = after.split_whitespace().filter(|x| x.bytes().all(|c| c.is_ascii_digit()) && !x.is_empty()).filter_map(|x| x.parse().ok()).take(8).collect();
        if nums.len() < 5 {
            continue;
        }
        let pair = [nums.iter().sum::<i64>(), nums[3] + nums[4]];
        if digits == 0 {
            all = Some(pair);
        } else {
            cores.push(pair);
        }
    }
    (all, cores)
}

fn cpu_percent(prev: Option<[i64; 2]>, cur: Option<[i64; 2]>) -> Option<f64> {
    let (p, c) = (prev?, cur?);
    let (dt, di) = (c[0] - p[0], c[1] - p[1]);
    if dt <= 0 {
        return None;
    }
    Some(round1((100.0 * (dt - di) as f64 / dt as f64).clamp(0.0, 100.0)))
}

fn sorted_dir(path: &str) -> Option<Vec<String>> {
    let mut names: Vec<String> = std::fs::read_dir(path).ok()?.filter_map(|e| e.ok()?.file_name().into_string().ok()).collect();
    names.sort();
    Some(names)
}

/// `_cpu_temperature`: the CPU package temperature in C from the usual hwmon / thermal names
fn cpu_temperature() -> Option<f64> {
    let (mut best, mut preferred): (Option<f64>, bool) = (None, false);
    if let Some(hws) = sorted_dir("/sys/class/hwmon") {
        for hw in hws {
            let base = format!("/sys/class/hwmon/{hw}");
            if !matches!(read(&format!("{base}/name")).trim(), "k10temp" | "coretemp" | "zenpower" | "cpu_thermal" | "cpu-thermal") {
                continue;
            }
            let Some(files) = sorted_dir(&base) else { break };
            for fname in files {
                let is_input = fname.strip_prefix("temp").and_then(|r| r.strip_suffix("_input")).is_some_and(|d| !d.is_empty() && d.bytes().all(|c| c.is_ascii_digit()));
                if !is_input {
                    continue;
                }
                let Some(v) = num(&read(&format!("{base}/{fname}"))) else { continue };
                let label = read(&format!("{base}/{}", fname.replace("_input", "_label")));
                let is_pkg = matches!(label.trim(), "Tctl" | "Tdie" | "Package id 0" | "CPU");
                if best.is_none() || (is_pkg && !preferred) {
                    best = Some(v / 1000.0);
                    preferred = is_pkg;
                }
            }
        }
    }
    if best.is_none() {
        if let Some(zones) = sorted_dir("/sys/class/thermal") {
            for z in zones {
                if z.starts_with("thermal_zone") && matches!(read(&format!("/sys/class/thermal/{z}/type")).trim(), "x86_pkg_temp" | "cpu-thermal" | "cpu_thermal") {
                    if let Some(v) = num(&read(&format!("/sys/class/thermal/{z}/temp"))) {
                        best = Some(v / 1000.0);
                        break;
                    }
                }
            }
        }
    }
    best.filter(|b| *b > -50.0 && *b < 200.0).map(round1)
}

fn cpu_stats() -> Value {
    let (mut all, mut cores) = parse_cpu_times(&read("/proc/stat"));
    let mut state = PREV_CPU.lock().unwrap_or_else(|e| e.into_inner());
    let prev = match state.take() {
        Some(p) => p,
        None => {
            // first reading: take a short second sample so the first answer has a percentage
            std::thread::sleep(Duration::from_millis(250));
            let first = (all, cores.clone());
            let second = parse_cpu_times(&read("/proc/stat"));
            all = second.0;
            cores = second.1;
            first
        }
    };
    *state = Some((all, cores.clone()));
    let per_core: Vec<Value> = prev.1.iter().zip(cores.iter()).map(|(p, c)| opt(cpu_percent(Some(*p), Some(*c)))).collect();
    let load_text = read("/proc/loadavg");
    let load: Vec<&str> = load_text.split_whitespace().collect();
    let model: String = read("/proc/cpuinfo").lines().find(|l| l.to_lowercase().starts_with("model name"))
        .map(|l| nd_render::text::py_strip(l.split_once(':').map(|x| x.1).unwrap_or(l)).to_string())
        .map(|l| l.chars().take(120).collect()).unwrap_or_default();
    json!({
        "percent": opt(cpu_percent(prev.0, all)),
        "cores": if cores.is_empty() { std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0) } else { cores.len() },
        "per_core": per_core,
        "load": if load.len() >= 3 { load[..3].iter().map(|x| opt(num(x))).collect::<Vec<_>>() } else { vec![] },
        "temp_c": opt(cpu_temperature()),
        "model": model,
    })
}

/// `parse_meminfo`: {name: bytes}
fn parse_meminfo(text: &str) -> std::collections::HashMap<String, i64> {
    let mut out = std::collections::HashMap::new();
    for line in text.lines() {
        let Some((name, rest)) = line.split_once(':') else { continue };
        if name.is_empty() || !name.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'(' | b')')) {
            continue;
        }
        let rest = rest.trim_start();
        let digits = rest.bytes().take_while(|c| c.is_ascii_digit()).count();
        if digits == 0 || !rest[digits..].trim_start().starts_with("kB") {
            continue;
        }
        if let Ok(n) = rest[..digits].parse::<i64>() {
            out.insert(name.to_string(), n * 1024);
        }
    }
    out
}

fn memory_stats() -> Value {
    let mi = parse_meminfo(&read("/proc/meminfo"));
    let g = |k: &str| mi.get(k).copied().unwrap_or(0);
    let total = g("MemTotal");
    let avail = mi.get("MemAvailable").copied().unwrap_or_else(|| g("MemFree"));
    let used = (total - avail).max(0);
    let (swap_total, swap_free) = (g("SwapTotal"), g("SwapFree"));
    let mut arc: Option<i64> = None;
    for line in read("/proc/spl/kstat/zfs/arcstats").lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() == 3 && parts[0] == "size" && parts[2].bytes().all(|c| c.is_ascii_digit()) {
            arc = parts[2].parse().ok();
        }
    }
    json!({
        "total": total, "used": used, "available": avail,
        "percent": if total != 0 { f(round1(100.0 * used as f64 / total as f64)) } else { Value::Null },
        "swap_total": swap_total, "swap_used": (swap_total - swap_free).max(0),
        "zfs_arc": arc,
        "percent_without_arc": match arc { Some(a) if total != 0 => f(round1(100.0 * (used - a).max(0) as f64 / total as f64)), _ => Value::Null },
    })
}

fn which(name: &str) -> Option<String> {
    std::env::var("PATH").ok()?.split(':').map(|d| format!("{d}/{name}")).find(|p| {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    })
}

/// (exit code, stdout, stderr) of a fixed argument list, 4 s at most
fn run(exe: &str, args: &[String]) -> std::io::Result<(i32, String, String)> {
    run_for(exe, args, Duration::from_secs(4))
}

fn run_for(exe: &str, args: &[String], limit: Duration) -> std::io::Result<(i32, String, String)> {
    use std::process::{Command, Stdio};
    let mut child = Command::new(exe).args(args).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
    let start = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        if start.elapsed() > limit {
            let _ = child.kill();
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let out = child.wait_with_output()?;
    Ok((out.status.code().unwrap_or(-1), String::from_utf8_lossy(&out.stdout).into_owned(), String::from_utf8_lossy(&out.stderr).into_owned()))
}

/// `_csv_rows(text, width)`: comma-separated lines of exactly `width` fields, each trimmed
fn csv_rows(text: &str, width: usize) -> Vec<Vec<String>> {
    text.lines().filter(|l| !l.trim().is_empty()).map(|l| l.split(',').map(|c| c.trim().to_string()).collect::<Vec<_>>()).filter(|r| r.len() == width).collect()
}

fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn gpu_stats() -> Value {
    let Some(exe) = which("nvidia-smi") else {
        return json!({"available": false, "devices": [],
            "reason": "nvidia-smi is not available inside this container. Give the nd-world service the GPU (see the System Monitor section of docs/GPU_SETUP.md) and restart it."});
    };
    let (code, out, err) = match run(&exe, &[format!("--query-gpu={}", GPU_FIELDS.join(",")), "--format=csv,noheader,nounits".into()]) {
        Ok(r) => r,
        Err(e) => {
            let msg: String = take(&nd_render::text::py_strip(&e.to_string()).split_whitespace().collect::<Vec<_>>().join(" "), 160);
            return json!({"available": false, "devices": [], "reason": format!("nvidia-smi could not be run: {msg}")});
        }
    };
    if code != 0 {
        let raw = if !err.is_empty() { &err } else { &out };
        let why: String = raw.chars().map(|c| if (' '..='~').contains(&c) { c } else { ' ' }).collect::<String>();
        let why = take(&why.split_whitespace().collect::<Vec<_>>().join(" "), 200);
        return json!({"available": false, "devices": [], "reason": format!("nvidia-smi failed{}", if why.is_empty() { ".".to_string() } else { format!(": {why}") })});
    }
    let mut gpus: Vec<serde_json::Map<String, Value>> = vec![];
    for r in csv_rows(&out, GPU_FIELDS.len()) {
        let d = |k: &str| r[GPU_FIELDS.iter().position(|f| *f == k).unwrap_or(0)].clone();
        let n = |k: &str| opt(num(&d(k)));
        let idx = num(&d("index"));
        let pstate = d("pstate");
        let m = json!({
            "index": idx.map(|i| i as i64).unwrap_or(gpus.len() as i64), "name": take(&d("name"), 80), "uuid": take(&d("uuid"), 64),
            "util_percent": n("utilization.gpu"), "mem_util_percent": n("utilization.memory"), "mem_used_mib": n("memory.used"), "mem_total_mib": n("memory.total"),
            "temp_c": n("temperature.gpu"), "power_w": n("power.draw"), "power_limit_w": n("power.limit"), "fan_percent": n("fan.speed"), "clock_mhz": n("clocks.sm"),
            "clock_max_mhz": n("clocks.max.sm"), "pstate": if pstate.starts_with('P') { json!(take(&pstate, 8)) } else { Value::Null },
            "power_min_w": n("power.min_limit"), "power_max_w": n("power.max_limit"), "power_default_w": n("power.default_limit"), "processes": [],
        });
        gpus.push(m.as_object().cloned().unwrap_or_default());
    }
    if gpus.is_empty() {
        return json!({"available": false, "devices": [], "reason": "nvidia-smi reported no GPUs."});
    }
    if let Ok((0, out2, _)) = run(&exe, &[format!("--query-compute-apps={}", APP_FIELDS.join(",")), "--format=csv,noheader,nounits".into()]) {
        for r in csv_rows(&out2, APP_FIELDS.len()) {
            let Some(used) = num(&r[2]) else { continue };
            let name = r[1].rsplit('/').next().unwrap_or("").to_string();
            let proc = json!({"pid": num(&r[0]).map(|p| p as i64).unwrap_or(0), "name": if name.is_empty() { "process".to_string() } else { take(&name, 60) }, "mem_mib": used});
            if let Some(g) = gpus.iter_mut().find(|g| g.get("uuid").and_then(|u| u.as_str()) == Some(r[3].as_str())) {
                if let Some(Value::Array(p)) = g.get_mut("processes") {
                    if p.len() < 8 {
                        p.push(proc);
                    }
                }
            }
        }
    }
    for g in gpus.iter_mut() {
        g.remove("uuid");
    }
    json!({"available": true, "devices": gpus})
}

/// `set_power_limit(index, watts)`: (ok, the JSON answer). The range check uses the card's own reported limits, so nonsense is refused before any command runs.
pub fn set_power_limit(index: &Value, watts: &Value) -> (bool, Value) {
    let fail = |m: String| (false, json!({"ok": false, "message": m}));
    let index = match index {
        Value::Number(n) if n.is_i64() => n.as_i64().unwrap_or(0),
        _ => return fail("index and watts must be numbers.".into()),
    };
    let watts_f = match watts {
        Value::Number(n) => n.as_f64().unwrap_or(f64::NAN),
        _ => return fail("index and watts must be numbers.".into()),
    };
    if !watts_f.is_finite() || watts_f.trunc() != watts_f {
        return fail("watts must be a whole number.".into());
    }
    let watts = watts_f as i64;
    let current = gpu_stats();
    if current["available"] != json!(true) {
        return fail(current.get("reason").and_then(|r| r.as_str()).unwrap_or("No GPU available.").to_string());
    }
    let Some(card) = current["devices"].as_array().and_then(|d| d.iter().find(|g| g["index"].as_i64() == Some(index))) else {
        return fail("There is no GPU with that index.".into());
    };
    let (Some(lo), Some(hi)) = (card["power_min_w"].as_f64(), card["power_max_w"].as_f64()) else {
        return fail("This card does not report a power-limit range, so the limit cannot be set from here.".into());
    };
    if !(lo <= watts as f64 && watts as f64 <= hi) {
        return fail(format!("The limit must be between {} and {} W for this card.", lo.round_ties_even() as i64, hi.round_ties_even() as i64));
    }
    let Some(exe) = which("nvidia-smi") else { return fail("nvidia-smi could not be run: not found".into()) };
    let (code, out, err) = match run_for(&exe, &["-i".into(), index.to_string(), "-pl".into(), watts.to_string()], Duration::from_secs(10)) {
        Ok(r) => r,
        Err(e) => return fail(format!("nvidia-smi could not be run: {}", take(&e.to_string().split_whitespace().collect::<Vec<_>>().join(" "), 160))),
    };
    *CACHE.lock().unwrap_or_else(|e| e.into_inner()) = None; // the next reading must show the new limit
    let joined: String = format!("{out} {err}").chars().map(|c| if c == '\n' || (' '..='~').contains(&c) { c } else { ' ' }).collect();
    let text = nd_render::text::py_strip(&joined).to_string();
    if code != 0 {
        let low = text.to_lowercase();
        let hint = if low.contains("permission") || low.contains("not supported") {
            format!(" The container is not allowed to change it. Set it from the TrueNAS shell instead: sudo nvidia-smi -i {index} -pl {watts} (add it under System \u{2192} Advanced \u{2192} Init/Shutdown Scripts as a post-init command to keep it after a reboot).")
        } else {
            String::new()
        };
        return fail(format!("{}{hint}", if text.is_empty() { "nvidia-smi failed.".to_string() } else { format!("nvidia-smi refused: {}", take(&text, 200)) }));
    }
    (true, json!({"ok": true, "message": format!("Power limit set to {watts} W. It lasts until the driver reloads or the machine restarts."), "watts": watts}))
}

/// `snapshot()`: everything, cached for a second
pub fn snapshot() -> Value {
    let mut cache = CACHE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((at, _, data)) = cache.as_ref() {
        if at.elapsed() < Duration::from_secs(1) {
            return data.clone();
        }
    }
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let data = json!({"time": now, "cpu": cpu_stats(), "memory": memory_stats(), "gpu": gpu_stats()});
    *cache = Some((Instant::now(), now, data.clone()));
    data
}
