use nd_render::rules::strict_unescape;

/// every case's expected value was computed by Python's html.unescape (tests/unescape_cases.json)
#[test]
fn matches_python_html_unescape() {
    let cases: Vec<(String, String)> = serde_json::from_str(include_str!("unescape_cases.json")).unwrap();
    let mut bad = Vec::new();
    for (input, want) in &cases {
        let got = strict_unescape(input).unwrap();
        if &got != want {
            bad.push(format!("{input:?}: got {got:?}, python {want:?}"));
        }
    }
    assert!(bad.is_empty(), "{} of {} differ:\n{}", bad.len(), cases.len(), bad.join("\n"));
}
