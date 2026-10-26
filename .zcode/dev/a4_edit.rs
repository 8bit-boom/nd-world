
/// `GET /characters/<id>/edit` (app/routers/characters.py `character_edit_form`): the edit form for a character the viewer
/// manages; a custom-mode sheet is edited in place, so Python just redirects to the character page
fn character_edit(pages: &Pages, db: &Connection, mut p: PageCtx, id: i64, headers: &HeaderMap) -> Option<Response<Body>> {
    let pc = row(db, "SELECT * FROM player_characters WHERE id = ?1", id)?;
    if !(p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(p.user_id)) {
        return Some(refusal(403));
    }
    let chosen_tpl = match pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0) {
        Some(tid) => crate::pages::sheet_tpl_by(db, "id", &tid.to_string())?.filter(|t| {
            // a template Python would read differently stays Python's: Tpl::load fails -> fall through
            true
        }),
        None => None,
    };
    if chosen_tpl.is_some() {
        let t = chosen_tpl.as_ref()?;
        if t.get("sheet_mode").and_then(|v| v.as_str()) == Some("custom") {
            return Some(crate::edits::redirect(&format!("/characters/{id}"))?);
        }
        if crate::sheets::Tpl::load(t.clone()).is_none() {
            return None;
        }
    }
    let ctx = crate::pages::character_form_ctx(db, &mut p, &pc, chosen_tpl)?;
    render_with(pages, "characters/form.html", ctx, headers)
}
