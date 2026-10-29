//! `unified_diff` against what CPython's difflib wrote for 48 pairs of line lists (up to 1500 lines, repeated lines, the autojunk
//! threshold, a last line without its newline).

#[test]
fn unified_diff_is_pythons() {
    let text = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/difflib_cases.json")).unwrap();
    let cases: serde_json::Value = serde_json::from_str(&text).unwrap();
    for (n, c) in cases.as_array().unwrap().iter().enumerate() {
        let list = |k: &str| c[k].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect::<Vec<_>>();
        let (a, b) = (list("a"), list("b"));
        let (ar, br): (Vec<&str>, Vec<&str>) = (a.iter().map(|s| s.as_str()).collect(), b.iter().map(|s| s.as_str()).collect());
        let got = nd_render::difflib::unified_diff(&ar, &br, "app/x.py", "app/x.py (AI suggested)");
        assert_eq!(got, c["diff"].as_str().unwrap(), "case {n}");
    }
}
