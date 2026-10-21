//! Reads JSON lines on stdin, answers one JSON value per line on stdout - for tests/test_rust_permissions.py, which asks Python and this the
//! same questions. {"op": "player"|"assistant"|"owner"|"gate"|"section"|"row"|"strip", ...}
use nd_perm::{allow, gmonly, sections};
use serde_json::{json, Value};
use std::io::{BufRead, Write};

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}
fn os<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k).and_then(|x| x.as_str())
}
fn oi(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_i64())
}

fn main() {
    let stdin = std::io::stdin();
    let mut out = std::io::BufWriter::new(std::io::stdout());
    for line in stdin.lock().lines() {
        let q: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let a = match s(&q, "op") {
            "player" => json!(allow::is_player_safe(s(&q, "method"), s(&q, "path"))),
            "assistant" => json!(allow::is_assistant_safe(s(&q, "method"), s(&q, "path"))),
            "owner" => json!(allow::is_owner_safe(s(&q, "method"), s(&q, "path"), oi(&q, "world_id").unwrap_or(0))),
            "gate" => {
                let v = allow::Viewer { is_gm: q["is_gm"].as_bool().unwrap_or(false), active_role: os(&q, "role").map(str::to_string), active_world_id: oi(&q, "world_id") };
                json!(allow::gate_allows(&v, s(&q, "method"), s(&q, "path")))
            }
            "section" => json!(sections::section_level(q["is_gm"].as_bool().unwrap_or(false), os(&q, "role"), os(&q, "access"), os(&q, "kinds"), s(&q, "section"))),
            "row" => json!(sections::can_edit_row(s(&q, "level"), q["is_gm"].as_bool().unwrap_or(false), q["is_assistant"].as_bool().unwrap_or(false), oi(&q, "user_id"), oi(&q, "created_by"))),
            "strip" => {
                let n = &q["now"];
                let now = (n[0].as_i64().unwrap(), n[1].as_i64().unwrap(), n[2].as_i64().unwrap(), n[3].as_i64().unwrap(), n[4].as_i64().unwrap());
                json!(gmonly::strip_gm_only(s(&q, "text"), oi(&q, "viewer"), now))
            }
            other => panic!("unknown op {other}"),
        };
        writeln!(out, "{a}").unwrap();
    }
}
