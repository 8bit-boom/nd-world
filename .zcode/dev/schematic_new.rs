
/// `GET /maps/schematic/new` (app/main.py `schematic_new_form`): the empty schematic form (player-safe, no section check)
pub fn schematic_new_form(pages: &Pages, cfg: &Config, uri: &Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, "GET", uri, headers, now)?;
    p.ctx.insert("schematic".into(), Value::Null);
    render_with(pages, "schematic_form.html", p.ctx, headers)
}
