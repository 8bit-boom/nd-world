/// A pre-rendered body with a content type and the gateway's header (downloads Python's StreamingResponse sends)
pub fn json_response_raw(body: &str, content_type: &str) -> Option<Response<Body>> {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, content_type)
        .header("x-nd-served-by", "gateway")
        .body(Body::from(body.to_string()))
        .ok()
}

/// `adv_options` for the read routes (apis.rs)
pub fn adv_options_pub(c: &Ctx, pc: &Map<String, Value>) -> Option<Value> {
    adv_options(c, pc)
}

/// `ability_cards` for the read routes (apis.rs)
pub fn ability_cards_pub(c: &Ctx, pc: &Map<String, Value>) -> Option<Vec<Value>> {
    ability_cards(c, pc)
}
