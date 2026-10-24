//! The setup QR code module for module as Python's qrcode package draws it (tests/qr_matrices.json: 40 otpauth URIs, versions 6-14,
//! made with `qrcode.QRCode(box_size=6, border=2)`, `add_data`, `make(fit=True)`).

#[test]
fn qr_matrices_are_pythons() {
    let text = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/qr_matrices.json")).unwrap();
    let cases: serde_json::Value = serde_json::from_str(&text).unwrap();
    for case in cases.as_array().unwrap() {
        let data = case["data"].as_str().unwrap();
        let want: Vec<String> = case["rows"].as_array().unwrap().iter().map(|r| r.as_str().unwrap().to_string()).collect();
        let got: Vec<String> = nd_gateway::qr::matrix(data.as_bytes()).unwrap().iter()
            .map(|r| r.iter().map(|d| if *d { '1' } else { '0' }).collect()).collect();
        assert_eq!(got, want, "{data}");
    }
}
