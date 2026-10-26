
/// `POST /maps/schematic/new` (app/main.py `schematic_new`): a fresh canvas - the name's slug (uniquified -2, -3, ...),
/// the canvas bounds 100..=20000, the row inserted, a redirect to the editor. Player-safe; the Form coercions (an int
/// field given text, a missing required name) are Python's 422s - forwarded
fn schematic_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    let name = f.get("name")?.to_string();
    let description = f.get("description").unwrap_or("").to_string();
    let py_int_form = |v: Option<&str>, default: i64| -> Option<i64> {
        match v {
            None => Some(default),
            Some(t) => {
                if t.is_empty() { return None; } // FastAPI's Form: "" for int is its 422
                crate::form::py_int(t)
            }
        }
    };
    let width = py_int_form(f.get("canvas_width"), 2000)?;
    let height = py_int_form(f.get("canvas_height"), 1500)?;
    let bg = f.get("canvas_bg").unwrap_or("dark").to_string();
    if name.chars().any(|ch| !ch.is_ascii()) || description.chars().any(|ch| !ch.is_ascii()) || bg.chars().any(|ch| !ch.is_ascii()) {
        return None; // the slug's lower() and the row's text: ASCII only here
    }
    let slug = py_slug(&name, 256)?;
    if slug.is_empty() {
        return None; // the 400 "Name must contain at least one letter or number": Python's
    }
    if !(100..=20000).contains(&width) || !(100..=20000).contains(&height) {
        return None; // the 400 "Canvas width/height must be between 100 and 20000": Python's
    }
    let mut unique = slug.clone();
    let mut i = 2;
    loop {
        let taken: bool = c.db.query_row("SELECT 1 FROM schematics WHERE slug = ?1", [&unique], |r| r.get::<_, i64>(0)).map(|_| true).unwrap_or(false);
        if !taken {
            break;
        }
        unique = format!("{slug}-{i}");
        i += 1;
    }
    let id = orm::insert(&c.db, "schematics", &[
        ("world_id", Sql::Integer(wid)),
        ("name", s(&name)),
        ("slug", s(&unique)),
        ("description", if description.is_empty() { Sql::Null } else { s(&description) }),
        ("is_html", Sql::Integer(0)),
        ("canvas_width", Sql::Integer(width)),
        ("canvas_height", Sql::Integer(height)),
        ("canvas_bg", s(&bg)),
        ("elements_json", s("[]")),
    ])?;
    let _ = id;
    redirect(&format!("/maps/schematic/{unique}"))
}
