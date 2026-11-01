use nd_render::md::render_md;

/// markdown2 (the Python app) turns a line of only hashes into a heading whose text is "#"
#[test]
fn bare_hash_lines_like_markdown2() {
    assert_eq!(render_md("##"), "<h1>#</h1>\n");
    assert_eq!(render_md("###"), "<h2>#</h2>\n");
    assert_eq!(render_md("######"), "<h5>#</h5>\n");
    assert_eq!(render_md("#"), "<p>#</p>\n");
    assert!(render_md("a\n\n##\n\nb").contains("<h1>#</h1>"));
    assert_eq!(render_md("## **Origins**"), "<h2><strong>Origins</strong></h2>\n");
}

/// markdown2: the paragraph after an empty bullet and one blank line belongs to the bullet
#[test]
fn empty_bullet_takes_the_next_paragraph() {
    let h = render_md("- \n\n**The Psions,** who rule.\n\nnext");
    assert!(h.contains("<li><strong>The Psions,</strong> who rule.</li>"), "{h}");
    assert!(h.contains("<p>next</p>"), "{h}");
    assert!(!h.contains("<li></li>"), "{h}");
    // two blank lines end the bullet, and fenced code is left alone
    assert!(render_md("- \n\n\nfoo").contains("<p>foo</p>"));
    assert!(render_md("```\n- \n\nfoo\n```").contains("- \n\nfoo"));
}
