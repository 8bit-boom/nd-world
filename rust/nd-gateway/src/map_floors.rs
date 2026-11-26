use serde_json::Value;

pub fn portal_destination(
    elements: &[Value],
    pc_id: i64,
    from_x: f64,
    from_y: f64,
    x: f64,
    y: f64,
    token_radius: f64,
) -> Option<(String, f64, f64)> {
    let reach = (token_radius * 0.5).clamp(16.0, 60.0);
    elements
        .iter()
        .filter_map(Value::as_object)
        .filter(|el| el.get("type").and_then(Value::as_str) == Some("pin"))
        .filter(|el| el.get("floor_portal").and_then(Value::as_bool) == Some(true))
        .filter(|el| el.get("hidden").and_then(Value::as_bool) != Some(true))
        .filter_map(|el| {
            let px = el.get("x")?.as_f64()?;
            let py = el.get("y")?.as_f64()?;
            let slug = el.get("destination_slug")?.as_str()?;
            let tx = el.get("destination_x")?.as_f64()?;
            let ty = el.get("destination_y")?.as_f64()?;
            let entered =
                (from_x - px).hypot(from_y - py) > reach && (x - px).hypot(y - py) <= reach;
            entered.then(|| (slug.to_string(), tx, ty))
        })
        .next()
        .filter(|(slug, _, _)| !slug.is_empty() && pc_id > 0)
}

pub fn remove_pc_tokens(elements: &mut Vec<Value>, pc_id: i64, token_id: &str) {
    elements.retain(|el| {
        let is_token = el.get("type").and_then(Value::as_str) == Some("token");
        let same_token = el.get("id").and_then(Value::as_str) == Some(token_id);
        let same_character = is_token && el.get("pc_id").and_then(Value::as_i64) == Some(pc_id);
        !same_token && !same_character
    });
}

pub fn remove_portals_to(elements: &mut Vec<Value>, destination_slug: &str) -> bool {
    let before = elements.len();
    elements.retain(|el| {
        el.get("type").and_then(Value::as_str) != Some("pin")
            || el.get("floor_portal").and_then(Value::as_bool) != Some(true)
            || el.get("destination_slug").and_then(Value::as_str) != Some(destination_slug)
    });
    elements.len() != before
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn portal() -> Value {
        json!({
            "type": "pin",
            "x": 100.0,
            "y": 120.0,
            "floor_portal": true,
            "destination_slug": "upper-floor",
            "destination_x": 340.0,
            "destination_y": 210.0
        })
    }

    #[test]
    fn destination_is_found_when_a_character_reaches_the_portal() {
        assert_eq!(
            portal_destination(&[portal()], 42, 150.0, 120.0, 110.0, 120.0, 40.0),
            Some(("upper-floor".into(), 340.0, 210.0))
        );
        assert_eq!(
            portal_destination(&[portal()], 42, 100.0, 120.0, 110.0, 120.0, 40.0),
            None,
        );
    }

    #[test]
    fn distant_hidden_and_non_portal_pins_do_not_trigger_travel() {
        let mut hidden = portal();
        hidden["hidden"] = json!(true);
        assert_eq!(
            portal_destination(&[hidden], 42, 150.0, 120.0, 100.0, 120.0, 40.0),
            None
        );

        let mut ordinary = portal();
        ordinary["floor_portal"] = json!(false);
        assert_eq!(
            portal_destination(&[ordinary], 42, 150.0, 120.0, 100.0, 120.0, 40.0),
            None
        );
        assert_eq!(
            portal_destination(&[portal()], 42, 150.0, 120.0, 200.0, 120.0, 40.0),
            None
        );
    }

    #[test]
    fn removing_a_character_token_also_cleans_up_legacy_duplicates() {
        let mut elements = vec![
            json!({"id":"pc-a","type":"token","pc_id":42}),
            json!({"id":"pc-b","type":"token","pc_id":42}),
            json!({"id":"pc-other","type":"token","pc_id":43}),
            json!({"id":"pin","type":"pin","pc_id":42}),
        ];
        remove_pc_tokens(&mut elements, 42, "pc-a");
        assert_eq!(
            elements
                .iter()
                .map(|el| el["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["pc-other", "pin"]
        );
    }

    #[test]
    fn deleting_a_floor_removes_only_its_paired_exit_pins() {
        let mut elements = vec![
            json!({"id":"exit","type":"pin","floor_portal":true,"destination_slug":"old-floor"}),
            json!({"id":"other-exit","type":"pin","floor_portal":true,"destination_slug":"keep-floor"}),
            json!({"id":"pin","type":"pin","destination_slug":"old-floor"}),
        ];
        assert!(remove_portals_to(&mut elements, "old-floor"));
        assert_eq!(
            elements
                .iter()
                .map(|el| el["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["other-exit", "pin"]
        );
        assert!(!remove_portals_to(&mut elements, "old-floor"));
    }
}
