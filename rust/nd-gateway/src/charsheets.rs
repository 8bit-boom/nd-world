//! Player-fillable character sheets (app/routers/character_sheets.py): the list, a sheet's page, its sandboxed render and download,
//! and the new / save / edit / delete writes. GM and the owning player only; every other caller gets the same 404 as "no such sheet".

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Response, StatusCode, Uri};
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::layout::PageCtx;
use crate::pages::{render_with, Pages};

const MAX_NAME: usize = 256;
const MAX_DATA_BYTES: usize = 256 * 1024;
const MAX_PER_PLAYER: i64 = 50;
const MAX_PER_WORLD: i64 = 500;

enum Route {
    List,
    New,
    Page(i64),
    Render(i64),
    Download(i64),
    Save(i64),
    Edit(i64),
    Delete(i64),
}

fn route(method: &Method, path: &str) -> Option<Route> {
    let rest = path.strip_prefix("/pages/sheets")?;
    let get = *method == Method::GET;
    let post = *method == Method::POST;
    if rest.is_empty() {
        return get.then_some(Route::List);
    }
    let rest = rest.strip_prefix('/')?;
    if rest == "new" {
        return post.then_some(Route::New);
    }
    let (id, tail) = rest.split_once('/').unwrap_or((rest, ""));
    if id.is_empty() || id.len() > 15 || !id.bytes().all(|b| b.is_ascii_digit()) || (id.len() > 1 && id.starts_with('0')) {
        return None;
    }
    let id: i64 = id.parse().ok()?;
    match (tail, get, post) {
        ("", true, _) => Some(Route::Page(id)),
        ("render", true, _) => Some(Route::Render(id)),
        ("download", true, _) => Some(Route::Download(id)),
        ("save", _, true) => Some(Route::Save(id)),
        ("edit", _, true) => Some(Route::Edit(id)),
        ("delete", _, true) => Some(Route::Delete(id)),
        _ => None,
    }
}

pub fn serves(method: &Method, path: &str) -> bool {
    route(method, path).is_some()
}

fn row(db: &Connection, sql: &str, id: i64) -> Option<Map<String, Value>> {
    db.query_row(sql, [id], |r| crate::layout::row_map(r)).ok()
}

fn rows(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> Option<Vec<Map<String, Value>>> {
    let mut st = db.prepare(sql).ok()?;
    let r = st.query_map(params, |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
    Some(r)
}

fn int(m: &Map<String, Value>, k: &str) -> Option<i64> {
    m.get(k).and_then(|v| v.as_i64())
}

fn not_found() -> Response<Body> {
    crate::idpages::refusal(404)
}

fn bad(code: StatusCode, m: &str) -> Response<Body> {
    crate::ai_more::detail(code, m.to_string())
}

fn redirect(to: &str) -> Response<Body> {
    Response::builder().status(StatusCode::SEE_OTHER).header(header::LOCATION, to).header("x-nd-served-by", "gateway").body(Body::empty()).unwrap()
}

/// `_sheet_or_404`: the sheet of this world, the GM's or the caller's own
fn sheet_or_404(db: &Connection, p: &PageCtx, wid: i64, id: i64) -> Result<Map<String, Value>, ()> {
    let s = row(db, "SELECT * FROM character_sheets WHERE id = ?1", id).ok_or(())?;
    if int(&s, "world_id") != Some(wid) || !(p.is_gm || int(&s, "owner_user_id") == Some(p.user_id)) {
        return Err(());
    }
    Ok(s)
}

/// `_owned_pc_or_400`: Ok(None) for a blank value, the owner's character in this world, else Python's 400 (Err)
fn owned_pc(db: &Connection, wid: i64, owner: i64, raw: Option<&str>) -> Option<Result<Option<Map<String, Value>>, Response<Body>>> {
    let v = crate::edits::py_strip_pub(raw.unwrap_or(""));
    if v.is_empty() {
        return Some(Ok(None));
    }
    if !v.is_ascii() {
        return None; // str.isdigit() beyond ASCII: Python's
    }
    if !v.bytes().all(|b| b.is_ascii_digit()) {
        return Some(Err(bad(StatusCode::BAD_REQUEST, "Invalid character")));
    }
    let id: i64 = v.parse().ok()?;
    let pc = rows(db, "SELECT * FROM player_characters WHERE id = ?1 AND world_id = ?2 AND owner_user_id = ?3 LIMIT 1", &[&id, &wid, &owner])?.into_iter().next();
    Some(match pc {
        Some(pc) => Ok(Some(pc)),
        None => Err(bad(StatusCode::BAD_REQUEST, "That character isn't owned by this sheet's player")),
    })
}

/// `_render_sheet_html`: the template's file with this sheet's saved data and the bridge script put in before </body>; Err = Python's 404
fn sheet_html(cfg: &Config, sheet: &Map<String, Value>, doc: &Map<String, Value>) -> Option<Result<String, ()>> {
    let raw = sheet.get("data_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("{}");
    let data = match serde_json::from_str::<Value>(raw) {
        Ok(v) => v,
        Err(_) if raw.contains("NaN") || raw.contains("Infinity") => return None,
        Err(_) => json!({}),
    };
    let safe = nd_render::py_dumps(&data).replace("</", "<\\/");
    let injected = format!("<script>window.__ND_SHEET_ID__={};window.__ND_SHEET_DATA__={safe};</script><script src=\"/static/js/character-sheet-bridge.js\"></script>",
        int(sheet, "id")?);
    // _read_template_html: the file under uploads/, checked to stay inside it
    let root = std::fs::canonicalize(cfg.db_path.parent()?.join("uploads")).ok();
    let url = doc.get("file_url").and_then(|v| v.as_str()).unwrap_or("");
    let Some(rel) = url.strip_prefix("/uploads/") else { return Some(Err(())) };
    let Some(root) = root else { return Some(Err(())) };
    let Ok(path) = std::fs::canonicalize(root.join(rel)) else { return Some(Err(())) };
    if !path.starts_with(&root) || !path.is_file() {
        return Some(Err(()));
    }
    let html = String::from_utf8(std::fs::read(&path).ok()?).ok()?; // errors="replace" on bad bytes: Python's
    if html.chars().any(|c| c.to_lowercase().count() != 1) {
        return None; // str.lower() changing the length moves Python's index
    }
    let lower = html.to_ascii_lowercase();
    Some(Ok(match lower.rfind("</body>") {
        None => format!("{html}{injected}"),
        Some(i) => format!("{}{injected}{}", &html[..i], &html[i..]),
    }))
}

fn sandboxed(html: String, download: Option<String>) -> Response<Body> {
    let mut b = Response::builder().header(header::CONTENT_TYPE, "text/html; charset=utf-8").header("x-content-type-options", "nosniff")
        .header("content-security-policy", "sandbox allow-scripts allow-popups").header("x-frame-options", "SAMEORIGIN")
        .header("x-nd-served-by", "gateway");
    if let Some(f) = download {
        b = b.header(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{f}.html\""));
    }
    b.body(Body::from(html)).unwrap()
}

pub fn handle(pages: &Pages, cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let r = route(method, uri.path())?;
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let mut p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    let Some(world) = p.world.clone() else { return Some(not_found()) };
    let wid = int(&world, "id")?;
    match r {
        Route::List => {
            let who = if p.is_gm { String::new() } else { format!(" AND owner_user_id = {}", p.user_id) };
            let sheets = rows(&db, &format!("SELECT * FROM character_sheets WHERE world_id = ?1{who} ORDER BY updated_at DESC"), &[&wid])?;
            let names = |table: &str, col: &str| -> Option<Map<String, Value>> {
                let ids: Vec<String> = sheets.iter().filter_map(|s| int(s, col)).filter(|i| *i != 0).map(|i| i.to_string()).collect();
                let mut out = Map::new();
                if !ids.is_empty() {
                    for r in rows(&db, &format!("SELECT id, name FROM {table} WHERE id IN ({})", ids.join(",")), &[])? {
                        out.insert(int(&r, "id")?.to_string(), r.get("name").cloned().unwrap_or(Value::Null));
                    }
                }
                Some(out)
            };
            let (pc_names, template_names) = (names("player_characters", "player_character_id")?, names("page_docs", "template_id")?);
            let mut owners: Vec<(String, Value)> = vec![];
            if p.is_gm {
                let mut order: Vec<i64> = vec![];
                for s in &sheets {
                    let o = int(s, "owner_user_id").unwrap_or(0);
                    if !order.contains(&o) {
                        order.push(o);
                    }
                }
                for o in order {
                    let user = row(&db, "SELECT * FROM users WHERE id = ?1", o);
                    let key = user.as_ref().map(|u| {
                        let d = u.get("display_name").and_then(|v| v.as_str()).unwrap_or("");
                        if d.is_empty() { u.get("email").and_then(|v| v.as_str()).unwrap_or("").to_string() } else { d.to_string() }
                    }).unwrap_or_default();
                    let mine: Vec<Value> = sheets.iter().filter(|s| int(s, "owner_user_id").unwrap_or(0) == o).cloned().map(Value::Object).collect();
                    owners.push((key, json!({"user": user.map_or(Value::Null, Value::Object), "sheets": mine})));
                }
                owners.sort_by(|a, b| a.0.cmp(&b.0));
            }
            p.ctx.insert("sheets".into(), Value::Array(sheets.into_iter().map(Value::Object).collect()));
            p.ctx.insert("owners".into(), Value::Array(owners.into_iter().map(|o| o.1).collect()));
            p.ctx.insert("is_gm".into(), json!(p.is_gm));
            p.ctx.insert("pc_names".into(), json!({"__intkeys__": Value::Object(pc_names)}));
            p.ctx.insert("template_names".into(), json!({"__intkeys__": Value::Object(template_names)}));
            render_with(pages, "character_sheets_list.html", p.ctx, headers)
        }
        Route::New => {
            let form = crate::ai::small_form(headers, body)?;
            let tid = crate::edits::py_strip_pub(form.get("template_id").unwrap_or(""));
            if !tid.is_ascii() {
                return None;
            }
            if tid.is_empty() || !tid.bytes().all(|b| b.is_ascii_digit()) {
                return Some(bad(StatusCode::BAD_REQUEST, "Missing template"));
            }
            let tid: i64 = tid.parse().ok()?;
            let Some(doc) = row(&db, "SELECT * FROM page_docs WHERE id = ?1", tid).filter(|d| int(d, "world_id") == Some(wid)) else { return Some(not_found()) };
            if !crate::edits::py_truthy_pub(doc.get("visible_to_players")) && !p.is_gm {
                return Some(not_found());
            }
            if !crate::edits::py_truthy_pub(doc.get("is_character_sheet_template")) {
                return Some(bad(StatusCode::BAD_REQUEST, "This page isn't a character sheet template"));
            }
            let count = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Option<i64> { db.query_row(sql, params, |r| r.get(0)).ok() };
            if count("SELECT COUNT(*) FROM character_sheets WHERE world_id = ?1 AND owner_user_id = ?2", &[&wid, &p.user_id])? >= MAX_PER_PLAYER {
                return Some(bad(StatusCode::BAD_REQUEST, &format!("You already have the maximum of {MAX_PER_PLAYER} character sheets.")));
            }
            if count("SELECT COUNT(*) FROM character_sheets WHERE world_id = ?1", &[&wid])? >= MAX_PER_WORLD {
                return Some(bad(StatusCode::BAD_REQUEST, &format!("This world already has the maximum of {MAX_PER_WORLD} character sheets.")));
            }
            let pc = match owned_pc(&db, wid, p.user_id, form.get("player_character_id"))? {
                Ok(pc) => pc,
                Err(r) => return Some(r),
            };
            let base = match &pc { Some(pc) => pc.get("name"), None => doc.get("name") }.and_then(|v| v.as_str())?.to_string();
            let name: String = base.chars().take(MAX_NAME).collect();
            let id = crate::orm::insert(&db, "character_sheets", &[("world_id", Sql::Integer(wid)), ("template_id", Sql::Integer(tid)),
                ("owner_user_id", Sql::Integer(p.user_id)), ("player_character_id", pc.as_ref().and_then(|pc| int(pc, "id")).map_or(Sql::Null, Sql::Integer)),
                ("name", Sql::Text(name))])?;
            Some(redirect(&format!("/pages/sheets/{id}")))
        }
        Route::Page(id) => {
            let Ok(sheet) = sheet_or_404(&db, &p, wid, id) else { return Some(not_found()) };
            let doc = int(&sheet, "template_id").and_then(|t| row(&db, "SELECT * FROM page_docs WHERE id = ?1", t));
            let owner = int(&sheet, "owner_user_id").unwrap_or(0);
            let own_pcs = rows(&db, "SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY name", &[&wid, &owner])?;
            let linked = int(&sheet, "player_character_id").filter(|i| *i != 0).and_then(|i| row(&db, "SELECT * FROM player_characters WHERE id = ?1", i));
            p.ctx.insert("sheet".into(), Value::Object(sheet));
            p.ctx.insert("template".into(), doc.map_or(Value::Null, Value::Object));
            p.ctx.insert("own_pcs".into(), Value::Array(own_pcs.into_iter().map(Value::Object).collect()));
            p.ctx.insert("linked_pc".into(), linked.map_or(Value::Null, Value::Object));
            p.ctx.insert("is_gm".into(), json!(p.is_gm));
            render_with(pages, "character_sheet_edit.html", p.ctx, headers)
        }
        Route::Render(id) | Route::Download(id) => {
            let Ok(sheet) = sheet_or_404(&db, &p, wid, id) else { return Some(not_found()) };
            let Some(doc) = int(&sheet, "template_id").and_then(|t| row(&db, "SELECT * FROM page_docs WHERE id = ?1", t)) else { return Some(not_found()) };
            let Ok(html) = sheet_html(cfg, &sheet, &doc)? else { return Some(not_found()) };
            if let Route::Download(_) = r {
                let name = sheet.get("name").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("character-sheet");
                if !name.is_ascii() {
                    return None; // str.isalnum() beyond ASCII: Python's
                }
                let f: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || " -_".contains(*c)).collect();
                return Some(sandboxed(html, Some(if f.is_empty() { "character-sheet".into() } else { f })));
            }
            Some(sandboxed(html, None))
        }
        Route::Save(id) => {
            let Ok(sheet) = sheet_or_404(&db, &p, wid, id) else { return Some(not_found()) };
            // parse_json: a body json.loads refuses, or one that is not an object, is Python's 500
            let b = match serde_json::from_slice::<Value>(body).ok()? {
                Value::Object(o) if crate::edits::plain_numbers(body) => o,
                _ => return None,
            };
            let Some(data) = b.get("data").filter(|d| d.is_object()) else {
                return Some(bad(StatusCode::BAD_REQUEST, "\"data\" must be an object"));
            };
            let encoded = nd_render::py_dumps(data);
            if encoded.len() > MAX_DATA_BYTES {
                return Some(bad(StatusCode::PAYLOAD_TOO_LARGE, "Sheet data too large"));
            }
            crate::orm::update(&db, "character_sheets", int(&sheet, "id")?, &[("data_json", Sql::Text(encoded))])?;
            crate::edits::json_response(&json!({"ok": true}))
        }
        Route::Edit(id) => {
            let Ok(sheet) = sheet_or_404(&db, &p, wid, id) else { return Some(not_found()) };
            let form = crate::ai::small_form(headers, body)?;
            let name: String = crate::edits::py_strip_pub(form.get("name").unwrap_or("")).chars().take(MAX_NAME).collect();
            let pc = match owned_pc(&db, wid, int(&sheet, "owner_user_id").unwrap_or(0), form.get("player_character_id"))? {
                Ok(pc) => pc,
                Err(r) => return Some(r),
            };
            let mut set = vec![("player_character_id", pc.as_ref().and_then(|pc| int(pc, "id")).map_or(Sql::Null, Sql::Integer))];
            if !name.is_empty() {
                set.push(("name", Sql::Text(name)));
            }
            crate::orm::update(&db, "character_sheets", id, &set)?;
            Some(redirect(&format!("/pages/sheets/{id}")))
        }
        Route::Delete(id) => {
            if sheet_or_404(&db, &p, wid, id).is_err() {
                return Some(not_found());
            }
            db.execute("DELETE FROM character_sheets WHERE id = ?1", [id]).ok()?;
            Some(redirect("/pages/sheets"))
        }
    }
}
