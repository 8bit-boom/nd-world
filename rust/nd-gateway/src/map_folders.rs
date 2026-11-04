//! Folders for maps and schematics (the /maps page): a port of app/map_folders.py - same normalising, same counts, same order.
use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Value};

/// The folder path as stored: slashes folded, empty and "." / ".." parts dropped, at most 256 characters.
pub fn norm(raw: &str) -> String {
    let joined = raw.replace('\\', "/").split('/').map(|p| p.trim().to_string()).filter(|p| !p.is_empty() && p != "." && p != "..").collect::<Vec<_>>().join("/");
    let cut: String = joined.chars().take(256).collect();
    cut.trim_matches('/').to_string()
}

fn key(s: &str) -> (String, String) {
    (s.to_lowercase(), s.to_string())
}

/// `{current, crumbs, subfolders, all}` for browsing `current`; `folders` = the folder of every visible item.
pub fn view(current: &str, folders: &[String]) -> Value {
    let current = norm(current);
    let mut counts: BTreeMap<String, i64> = BTreeMap::new();
    let mut every: BTreeSet<String> = BTreeSet::new();
    for f in folders {
        let f = norm(f);
        if f.is_empty() {
            continue;
        }
        let parts: Vec<&str> = f.split('/').collect();
        for i in 0..parts.len() {
            every.insert(parts[..=i].join("/"));
        }
        if !current.is_empty() && f != current && !f.starts_with(&format!("{current}/")) {
            continue;
        }
        let rest: String = if current.is_empty() { f.clone() } else { f.chars().skip(current.chars().count() + 1).collect() };
        if !rest.is_empty() {
            let seg = rest.split('/').next().unwrap_or("").to_string();
            *counts.entry(seg).or_insert(0) += 1;
        }
    }
    let parts: Vec<&str> = if current.is_empty() { vec![] } else { current.split('/').collect() };
    let crumbs: Vec<Value> = (0..parts.len()).map(|i| json!({"name": parts[i], "path": parts[..=i].join("/")})).collect();
    let mut subs: Vec<(String, i64)> = counts.into_iter().collect();
    subs.sort_by(|a, b| key(&a.0).cmp(&key(&b.0)));
    let subfolders: Vec<Value> = subs.into_iter().map(|(n, c)| json!({"name": n, "path": if current.is_empty() { n.clone() } else { format!("{current}/{n}") }, "count": c})).collect();
    let mut all: Vec<String> = every.into_iter().collect();
    all.sort_by(|a, b| key(a).cmp(&key(b)));
    json!({"current": current, "crumbs": crumbs, "subfolders": subfolders, "all": all})
}

/// `item_folder` after renaming folder `old` to `new` (everything under it follows); None when it is not under `old`.
pub fn moved(item_folder: &str, old: &str, new: &str) -> Option<String> {
    let f = norm(item_folder);
    if f == old {
        return Some(new.to_string());
    }
    let rest = f.strip_prefix(&format!("{old}/"))?;
    Some(if new.is_empty() { norm(rest) } else { norm(&format!("{new}/{rest}")) })
}
