//! A world with thousands of names makes the autolink alternation huge; fancy-regex's default backtrack limit used to panic on ordinary
//! non-ASCII text (a Russian paragraph), which took the entity page down. It must now answer, and never panic.
use nd_render::text::autolink_entities;

#[test]
fn big_name_list_and_cyrillic_text_does_not_panic() {
    let names: Vec<(String, i64)> = (0..2000).map(|i| (format!("Name{i} Of House{i}"), i as i64 + 1)).collect();
    let body = "Основатель , впервые появился на глазах публики после неудачной экспедиции на кладбище, вернул тела погибших членов экспедиции на поверхность. ".repeat(4);
    let html = format!("<p>{body}</p>");
    let out = autolink_entities(&html, &names).expect("must answer, not decline");
    assert!(out.contains("Основатель"));
}

#[test]
fn still_links_a_name() {
    let names = vec![("Adrianna".to_string(), 7)];
    let out = autolink_entities("<p>Meet Adrianna now</p>", &names).unwrap();
    assert!(out.contains(r#"<a href="/entity/7" class="auto-entity-link">Adrianna</a>"#));
}
