//! Not a regression test: lists the Python app's write routes (a routes.json made by the probe script) that the gateway's write dispatcher does not take.
//! `ND_ROUTES_JSON=/path/routes.json cargo test --release -p nd-gateway --test route_probe -- --ignored --nocapture`
use axum::http::{HeaderMap, HeaderValue, Method};

fn fill(p: &str) -> String {
    let mut out = String::new();
    let mut rest = p;
    while let Some(i) = rest.find('{') {
        out.push_str(&rest[..i]);
        let j = rest[i..].find('}').unwrap() + i;
        let name = &rest[i + 1..j];
        out.push_str(if name.ends_with("id") || name == "id" || name == "n" || name == "kid" || name == "cid" { "1" } else { "x" });
        rest = &rest[j + 1..];
    }
    out.push_str(rest);
    out
}

#[test]
#[ignore]
fn list_write_routes_the_gateway_does_not_take() {
    let file = std::env::var("ND_ROUTES_JSON").expect("ND_ROUTES_JSON");
    let routes: Vec<(String, String)> = serde_json::from_str::<Vec<Vec<String>>>(&std::fs::read_to_string(file).unwrap()).unwrap().into_iter().map(|r| (r[0].clone(), r[1].clone())).collect();
    let mut mp = HeaderMap::new();
    mp.insert("content-type", HeaderValue::from_static("multipart/form-data; boundary=x"));
    mp.insert("content-length", HeaderValue::from_static("10"));
    let mut missing = vec![];
    for (m, p) in &routes {
        if m == "GET" {
            continue;
        }
        let method = Method::from_bytes(m.as_bytes()).unwrap();
        let path = fill(p);
        if nd_gateway::edits::handles(&method, &path) || nd_gateway::edits::handles_upload(&method, &path, &mp) {
            continue;
        }
        missing.push(format!("{m} {p}"));
    }
    println!("NOT TAKEN BY THE WRITE DISPATCHER: {}", missing.len());
    for l in missing {
        println!("  {l}");
    }
}
