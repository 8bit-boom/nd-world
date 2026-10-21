//! JSON lines in, one JSON answer per line out - for tests/test_rust_layout.py, which puts the same questions to Python.
//! {"op": "clean", "section", "raw"} | {"op": "nav", "is_gm", "role", "world": {...} or null, "dreamlands", "king"}
use nd_render::{app, md};
use serde_json::{json, Value};
use std::io::{BufRead, Write};

fn main() {
    let mut out = std::io::BufWriter::new(std::io::stdout());
    for line in std::io::stdin().lock().lines() {
        let q: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let a = match q["op"].as_str().unwrap() {
            "clean" => json!(app::clean(q["section"].as_str().unwrap(), &q["raw"])),
            "nav" => {
                let world = q["world"].as_object();
                let c = app::NavCtx {
                    is_gm: q["is_gm"].as_bool().unwrap(),
                    role: q["role"].as_str(),
                    world,
                    dreamlands_enabled: q["dreamlands"].as_bool().unwrap(),
                    king_in_yellow_enabled: q["king"].as_bool().unwrap(),
                };
                match app::resolve_nav(&c) {
                    Some((m, u)) => json!([m, u]),
                    None => Value::Null,
                }
            }
            "md" => json!(md::render_md(q["text"].as_str().unwrap())),
            o => panic!("op {o}"),
        };
        writeln!(out, "{a}").unwrap();
    }
}
