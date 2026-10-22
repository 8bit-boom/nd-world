//! Phase 3: the app's form and JSON writes, made by the gateway (docs/RUST_WRITES.md). Same rule as the dice roll: the gateway answers
//! **only the successful case**, after re-doing every check Python does; anything else - a refusal, a missing row, input Python would read
//! differently or reject, a row of another world (whose access rules are not ported) - goes to Python **unchanged**, body included.
//! Rows are written as SQLAlchemy writes them (crate::orm: Python-side defaults, `onupdate`, no UPDATE when nothing changed).

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Request, Response, StatusCode, Uri};
use percent_encoding::utf8_percent_encode;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::config::Config;
use crate::form::{content_type, parse_multipart, py_int, Form};
use crate::layout::PageCtx;
use crate::orm;
use crate::proxy::Proxy;

const MAX_BODY: usize = 256 * 1024;

pub struct Ctx<'a> {
    pub cfg: &'a Config,
    /// a streamed upload's fields and file parts (crate::uploads)
    pub upload: Option<crate::uploads::Upload>,
    /// files this request wrote, removed again when the gateway does not answer after all
    pub created: Vec<std::path::PathBuf>,
    /// files / folders to remove once the answer is committed (an original replaced by its converted copy, a chunk folder)
    pub removals: Vec<std::path::PathBuf>,
    pub db: Connection,
    pub p: PageCtx,
    pub uri: &'a Uri,
    pub headers: &'a HeaderMap,
    pub body: &'a [u8],
}

impl Ctx<'_> {
    /// After the commit: remove what the answer replaced (files, chunk folders)
    fn apply_removals(&self) {
        for p in &self.removals {
            if p.is_dir() {
                let _ = std::fs::remove_dir_all(p);
            } else {
                let _ = std::fs::remove_file(p);
            }
        }
    }

    pub fn form(&self) -> Option<Form> {
        if let Some(u) = &self.upload {
            return Some(u.form.clone());
        }
        if content_type(self.headers) == "multipart/form-data" {
            // request.form() reads multipart the same way; a body with a file in it is not a plain form
            let (f, files) = parse_multipart(self.headers, self.body)?;
            return files.is_empty().then_some(f);
        }
        if self.body.is_empty() && content_type(self.headers).is_empty() {
            return Some(Form(vec![])); // no body at all: Starlette's request.form() is empty
        }
        if content_type(self.headers) != "application/x-www-form-urlencoded" {
            return None;
        }
        Form::parse(self.body)
    }

    /// `parse_json(body)` as an object (anything else makes Python fail - its own answer)
    pub fn json(&self) -> Option<Map<String, Value>> {
        if !plain_numbers(self.body) {
            return None;
        }
        match serde_json::from_slice::<Value>(self.body).ok()? {
            Value::Object(o) => Some(o),
            _ => None,
        }
    }

    /// A scratch file name in the uploads folder (removed by the caller)
    pub fn uploads_scratch(&self) -> Option<std::path::PathBuf> {
        let dir = self.cfg.db_path.parent()?.join("uploads").join("_gateway");
        std::fs::create_dir_all(&dir).ok()?;
        let n = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos();
        Some(dir.join(format!("note-{}-{n}.tmp", std::process::id())))
    }

    pub fn world_id(&self) -> Option<i64> {
        self.p.world.as_ref()?.get("id")?.as_i64()
    }

    pub fn is_assistant(&self) -> bool {
        self.p.role.as_deref() == Some("assistant")
    }

    /// `world_can_edit_section(request, active world, section)`
    pub fn can_edit_section(&self, section: &str) -> Option<bool> {
        crate::pages::edit_level(&self.p, self.p.world.as_ref()?, section)
    }

    /// `world_can_edit_row(request, active world, section, created_by)`
    pub fn can_edit_row(&self, section: &str, created_by: Option<i64>) -> Option<bool> {
        Some(self.can_edit_section(section)? && (self.p.is_gm || self.is_assistant() || created_by == Some(self.p.user_id)))
    }
}

/// Whether every whole number in a JSON text fits in 64 bits (Python keeps a bigger one exact; here it would turn into a float and be
/// written back differently)
fn plain_numbers(raw: &[u8]) -> bool {
    let (mut in_str, mut esc, mut run, mut frac) = (false, false, 0usize, false);
    for &b in raw {
        if in_str {
            if esc {
                esc = false;
            } else if b == b'\\' {
                esc = true;
            } else if b == b'"' {
                in_str = false;
            }
            continue;
        }
        match b {
            b'"' => in_str = true,
            b'0'..=b'9' => {
                run += 1;
                if run > 18 && !frac {
                    return false;
                }
                continue;
            }
            b'.' | b'e' | b'E' if run > 0 => frac = true,
            b'-' | b'+' => {}
            _ => {
                run = 0;
                frac = false;
            }
        }
        if !b.is_ascii_digit() && !matches!(b, b'.' | b'e' | b'E' | b'-' | b'+') {
            run = 0;
            frac = false;
        }
    }
    true
}

/// Starlette's RedirectResponse(url, 303)
pub fn redirect(url: &str) -> Option<Response<Body>> {
    let loc = utf8_percent_encode(url, crate::pages::LOCATION_SAFE).to_string();
    Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(header::LOCATION, loc)
        .header(header::CONTENT_LENGTH, "0")
        .header("x-nd-served-by", "gateway")
        .body(Body::empty())
        .ok()
}

/// FastAPI's JSONResponse: `json.dumps(content, ensure_ascii=False, separators=(",", ":"))`, keys in the handler's order
pub fn json_response(v: &Value) -> Option<Response<Body>> {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-nd-served-by", "gateway")
        .body(Body::from(nd_render::py_dumps_compact(v)))
        .ok()
}

type Handler = fn(&mut Ctx, &[i64]) -> Option<Response<Body>>;

/// (pattern with `{}` for an integer id, handler)
const ROUTES: &[(&str, Handler)] = &[
    ("/quests/new", quest_create),
    ("/quests/{}/edit", quest_edit),
    ("/quests/{}/delete", quest_delete),
    ("/api/quests/{}/status", quest_status),
    ("/facts/new", fact_create),
    ("/tables/import", tables_import),
    ("/api/quests/apply", quests_apply),
    ("/api/facts/bulk", facts_bulk),
    ("/facts/{}/edit", fact_edit),
    ("/facts/{}/delete", fact_delete),
    ("/tables/new", table_create),
    ("/tables/{}/edit", table_update),
    ("/tables/{}/delete", table_delete),
    ("/api/tables/{}/roll", table_roll),
    ("/api/calendar/events", calendar_event_add),
    ("/api/calendar/events/{}/delete", calendar_event_delete),
    ("/api/calendar/advance", calendar_advance),
    ("/calendar/config", calendar_config_save),
    ("/api/calendar/icons/{}/delete", calendar_icon_delete),
    ("/api/calendar/days/{}/icons", calendar_icon_add),
    ("/api/calendar/set-date", calendar_set_date),
    ("/boards/new", board_create),
    ("/sessions/new", session_create),
    ("/sessions/{}/edit", session_edit),
    ("/api/sessions/{}/prep/toggle", prep_toggle),
    ("/api/sessions/{}/prep/add", prep_add),
    ("/api/sessions/{}/prep/{}/delete", prep_delete),
    ("/api/entity/{}/field", entity_field),
    ("/api/entity/{}/rename", entity_rename),
    ("/api/entities/bulk-folder", bulk_folder),
    ("/api/entities/bulk-visibility", bulk_visibility),
    ("/api/entity/{}/alias", entity_alias),
    ("/api/entity/{}/markup", entity_markup),
    ("/api/entity/{}/send-to-player", entity_send_to_player),
    ("/api/characters/{}/hp-async", pc_hp),
    ("/api/characters/{}/shock", pc_shock),
    ("/api/characters/{}/level-up", pc_level_up),
    ("/api/characters/{}/feats", pc_feats),
    ("/api/characters/{}/ability-use", pc_ability_use),
    ("/api/characters/{}/advance", pc_advance),
    ("/api/characters/{}/hub/end-turn", hub_end_turn),
    ("/api/characters/{}/hub/handouts/seen", hub_handouts_seen),
    ("/api/characters/{}/hub/companions", hub_companion_add),
    ("/api/characters/{}/hub/pins", hub_pin_toggle),
    ("/api/characters/{}/hub/rest", hub_rest),
    ("/api/characters/{}/hub/rest/undo", hub_rest_undo),
    ("/api/characters/{}/hub/log/{}/undo", hub_log_undo),
    ("/api/characters/{}/hub/keepsakes", hub_keepsake_add),
    ("/api/characters/{}/hub/loot", hub_loot_claim),
    ("/api/characters/{}/hub/initiative", hub_roll_initiative),
    ("/api/cockpit/workspace", cockpit_save_workspace),
    ("/api/characters/{}/hub/keepsakes/{}", hub_keepsake_edit),
    ("/api/characters/{}/hub/keepsakes/{}/delete", hub_keepsake_delete),
    ("/api/characters/{}/hub/entries", hub_entry_create),
    ("/api/characters/{}/hub/entries/{}", hub_entry_update),
    ("/api/characters/{}/hub/entries/{}/delete", hub_entry_delete),
    ("/api/characters/{}/hub/pass-turn", hub_pass_turn),
    ("/api/characters/{}/resource", pc_resource),
    ("/api/characters/{}/ability-ready", pc_ability_ready),
    ("/api/characters/{}/equipment", pc_equipment),
    ("/api/characters/{}/carry-limit", pc_carry_limit),
    ("/characters/new", pc_new),
    ("/characters/{}/edit", pc_edit),
    ("/characters/{}/owner", pc_owner),
    ("/characters/{}/delete", pc_delete),
    ("/characters/{}/retire-to-npc", pc_retire),
    ("/api/characters/{}/portrait-from-url", pc_portrait_from_url),
    ("/api/worlds/{}/characters/sync", pc_sync_create),
    ("/api/characters/{}/xp", pc_xp),
    ("/api/characters/{}/conditions", pc_conditions),
    ("/api/characters/{}/currency", pc_currency),
    ("/api/characters/{}/pp", pc_pp),
    ("/api/characters/{}/mp", pc_mp),
    ("/sessions/{}/delete", session_delete),
    ("/sessions/{}/recap-publish", session_recap_publish),
    ("/api/sessions/{}/live-transcript/clear", session_live_clear),
    ("/api/sessions/{}/loot/transfer", session_loot),
    ("/api/sessions/{}/xp", session_xp),
    ("/audio/albums/new", audio_album_new),
    ("/audio/albums/{}/rename", audio_album_rename),
    ("/audio/albums/{}/delete", audio_album_delete),
    ("/audio/{}/edit", audio_item_edit),
    ("/audio/{}/delete", audio_item_delete),
    ("/video/albums/new", video_album_new),
    ("/video/albums/{}/rename", video_album_rename),
    ("/video/albums/{}/delete", video_album_delete),
    ("/video/{}/edit", video_item_edit),
    ("/video/{}/delete", video_item_delete),
    ("/pages/albums/new", pages_album_new),
    ("/pages/albums/{}/rename", pages_album_rename),
    ("/pages/albums/{}/delete", pages_album_delete),
    ("/pages/{}/edit", pages_item_edit),
    ("/pages/{}/delete", pages_item_delete),
    ("/images/albums/new", image_album_new),
    ("/images/albums/{}/rename", image_album_rename),
    ("/images/albums/{}/move", image_album_move),
    ("/images/albums/{}/delete", image_album_delete),
    ("/images/albums/{}/add", image_album_add),
    ("/images/albums/{}/remove", image_album_remove),
    ("/api/sessions/{}/recap-audio", sm_audio_attach),
    ("/api/sessions/{}/recap-audio/{}/visibility", sm_audio_visibility),
    ("/api/sessions/{}/recap-audio/{}/remove", sm_audio_remove),
    ("/api/sessions/{}/media/attach", sm_attach),
    ("/api/sessions/{}/media/audio/{}/visibility", sm_audio_item_visibility),
    ("/api/sessions/{}/media/video/{}/visibility", sm_video_item_visibility),
    ("/api/sessions/{}/media/audio/{}/rename", sm_audio_item_rename),
    ("/api/sessions/{}/media/video/{}/rename", sm_video_item_rename),
    ("/api/sessions/{}/media/audio/{}/remove", sm_audio_remove),
    ("/api/sessions/{}/media/video/{}/remove", sm_video_item_remove),
    ("/audio/upload/complete", audio_complete),
    ("/images/albums/upload/complete", gallery_complete),
    ("/pages/upload/complete", pages_complete),
    ("/folders/rename", folder_rename),
    ("/entity-templates/new", entity_template_new),
    ("/entity-templates/{}/edit", entity_template_edit),
    ("/entity-templates/{}/delete", entity_template_delete),
    ("/api/entity/{}/image", entity_set_image),
    ("/races/new", race_new),
    ("/maps/new", map_new),
    ("/combat/new", combat_new),
    ("/api/display/show", display_show),
    ("/api/display/clear", display_clear),
    ("/combat/{}/state", combat_save_state),
    ("/combat/{}/delete", combat_delete),
    ("/combat/{}/link-session", combat_link_session),
    ("/combat/{}/unlink-session", combat_unlink_session),
    ("/api/combat/{}/finish", combat_finish),
    ("/api/combat/{}/reopen", combat_reopen),
    ("/combat/{}/duplicate", combat_duplicate),
    ("/combat/{}/save-template", combat_save_template),
    ("/combat/templates/{}/run", combat_run_template),
    ("/api/combat/{}/sync-characters", combat_sync_characters),
    ("/images/spotlight", spotlight_send),
    ("/images/spotlight/clear", spotlight_clear),
    ("/entity/{}/notes/import", entity_note_import),
    ("/races/add-builtin", race_add_builtin),
    ("/races/add-all-builtin", race_add_all_builtin),
    ("/professions/add-builtin", profession_add_builtin),
    ("/professions/add-all-builtin", profession_add_all_builtin),
    ("/professions/new", profession_new),
    ("/races/{}/delete", race_delete),
    ("/professions/{}/delete", profession_delete),
    ("/worlds/{}/home/edit", home_edit),
    ("/api/worlds/{}/home/quick-link", home_quick_link),
    ("/api/worlds/{}/home/pinned-tile", home_pin_tile),
    ("/api/worlds/{}/home/pinned-tile/remove", home_unpin_tile),
    ("/api/worlds/{}/home/hide-kind", home_hide_kind),
    ("/account/name", account_name),
    ("/account/tokens/{}/revoke", account_token_revoke),
    ("/account/trusted-devices/{}/revoke", account_device_revoke),
    ("/worlds/{}/edit", world_edit),
    ("/worlds/{}/invites/new", invite_new),
    ("/worlds/{}/invites/{}/revoke", invite_revoke),
    ("/worlds/{}/members/{}/remove", member_remove),
    ("/worlds/{}/members/{}/role", member_role),
    ("/worlds/{}/members/{}/clear-2fa", member_clear_2fa),
    ("/worlds/{}/notes/{}/new", private_note_new),
    ("/worlds/{}/notes/{}/{}/delete", private_note_delete),
    ("/worlds/{}/theme/clear", theme_clear),
    ("/worlds/{}/theme/import", theme_import),
    ("/worlds/new", world_create),
    ("/worlds/{}/rules/edit", rules_edit),
    ("/worlds/{}/rules/import", rules_import),
    ("/worlds/{}/kinds/new", kinds_new),
    ("/worlds/{}/kinds/edit", kinds_save),
    ("/worlds/{}/nav-menus/edit", nav_menus_save),
    ("/worlds/{}/ai-instructions/import", ai_instruction_import),
    ("/worlds/{}/ai-instructions/{}/toggle", ai_instruction_toggle),
    ("/worlds/{}/ai-instructions/{}/toggle-players", ai_instruction_toggle_players),
    ("/worlds/{}/ai-instructions/{}/delete", ai_instruction_delete),
    ("/worlds/{}/theme/preset", theme_preset),
    ("/parties/new", party_new),
    ("/parties/{}/edit", party_edit),
    ("/parties/{}/delete", party_delete),
    ("/api/parties/{}/location", party_location),
    ("/api/parties/{}/loot", party_loot),
    ("/api/parties/{}/launch-combat", party_launch_combat),
    ("/api/parties/{}/members/toggle", party_member_toggle),
    ("/new", entity_new),
    ("/entity/{}/edit", entity_edit),
    ("/entity/{}/duplicate", entity_duplicate),
    ("/entity/{}/delete", entity_delete),
    ("/entity/{}/link/{}", entity_link),
    ("/entity/{}/unlink/{}", entity_unlink),
    ("/entity/{}/notes/new", note_add),
    ("/entity/{}/notes/{}/toggle", note_toggle),
    ("/entity/{}/notes/{}/delete", note_delete),
    ("/entity/{}/notes/{}/edit", note_edit),
];

/// The handler for a POST path and the ids in it (digits only, as FastAPI's int converter reads them here)
/// Upload routes: their bodies are streamed to disk first (crate::uploads)
const UPLOAD_ROUTES: &[(&str, Handler)] = &[
    ("/audio/upload/chunk", audio_chunk),
    ("/video/upload/chunk", video_chunk),
    ("/pages/upload/chunk", pages_chunk),
    ("/audio/upload", audio_upload),
    ("/pages/upload", pages_upload),
    ("/images/albums/upload/chunk", gallery_chunk),
    ("/images/albums/{}/upload", gallery_upload),
    ("/api/sessions/{}/media/upload", sm_upload),
    ("/api/upload-image", toolbar_image),
    ("/api/upload-media", toolbar_media),
    ("/api/characters/upload-image", toolbar_pc_image),
    ("/api/characters/upload-media", toolbar_pc_media),
    ("/api/characters/{}/hub/keepsakes/upload", hub_keepsake_upload),
];

/// Form routes that carry an optional picture: streamed like an upload when the body is too big to hold in memory
const PICTURE_FORM_ROUTES: &[&str] = &["/entity/{}/notes/import", "/maps/new", "/worlds/{}/home/edit", "/races/new", "/professions/new", "/new", "/entity/{}/edit", "/characters/new", "/characters/{}/edit", "/api/calendar/days/{}/icons"];

fn picture_route(path: &str) -> Option<(Handler, Vec<i64>)> {
    let (h, ids) = route(path)?;
    let table: Vec<(&str, Handler)> = PICTURE_FORM_ROUTES.iter().map(|p| (*p, h)).collect();
    route_in(&table, path).map(|_| (h, ids))
}

pub fn handles_upload(method: &Method, path: &str, headers: &HeaderMap) -> bool {
    let big = headers.get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<usize>().ok()).map_or(true, |n| n > MAX_BODY);
    method == Method::POST && (route_in(UPLOAD_ROUTES, path).is_some() || map_upload_slug(path).is_some() || sc_upload_route(path).is_some()
        || (big && picture_route(path).is_some()))
}

pub async fn serve_upload(cfg: Arc<Config>, proxy: &Proxy, req: Request<Body>, peer: Option<std::net::SocketAddr>) -> Response<Body> {
    crate::uploads::serve(cfg, proxy, req, peer, |cfg, method, uri, headers, up| {
        let up = up?;
        let slug: Option<(SlugHandler, String)> = map_upload_slug(uri.path()).map(|s| (map_upload as SlugHandler, s)).or_else(|| sc_upload_route(uri.path()));
        let (h, ids) = match &slug {
            Some(_) => (map_new as Handler, vec![]), // unused: the slug handler answers below
            None => route_in(UPLOAD_ROUTES, uri.path()).or_else(|| picture_route(uri.path()))?,
        };
        let db = crate::writes::open(cfg)?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
        let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
        db.execute_batch("BEGIN IMMEDIATE").ok()?;
        let mut ctx = Ctx { cfg, upload: Some(up), created: vec![], removals: vec![], db, p, uri, headers, body: &[] };
        let answer = match &slug {
            Some((sh, sl)) => sh(&mut ctx, sl),
            None => h(&mut ctx, &ids),
        };
        if answer.is_some() && ctx.db.execute_batch("COMMIT").is_ok() {
            ctx.apply_removals();
            return answer;
        }
        let _ = ctx.db.execute_batch("ROLLBACK");
        for f in &ctx.created {
            let _ = std::fs::remove_file(f);
        }
        None
    })
    .await
}

/// PUT routes, same patterns as ROUTES
const PUT_ROUTES: &[(&str, Handler)] = &[("/api/characters/{}/sync", pc_sync_put)];

fn route(path: &str) -> Option<(Handler, Vec<i64>)> {
    route_in(ROUTES, path)
}

fn route_in(table: &[(&str, Handler)], path: &str) -> Option<(Handler, Vec<i64>)> {
    'outer: for (pat, h) in table {
        let (mut rest, mut ids) = (path, vec![]);
        let parts: Vec<&str> = pat.split("{}").collect();
        for (i, part) in parts.iter().enumerate() {
            rest = rest.strip_prefix(part).map_or_else(|| "\u{0}", |r| r);
            if rest == "\u{0}" {
                continue 'outer;
            }
            if i + 1 < parts.len() {
                let n = rest.bytes().take_while(|b| b.is_ascii_digit()).count();
                if n == 0 || n > 15 {
                    continue 'outer;
                }
                ids.push(rest[..n].parse().ok()?);
                rest = &rest[n..];
            }
        }
        if rest.is_empty() {
            return Some((*h, ids));
        }
    }
    None
}

type SlugHandler = fn(&mut Ctx, &str) -> Option<Response<Body>>;

/// (prefix, suffix, handler) for paths addressed by a slug: `<prefix><slug><suffix>`, the slug plain letters, digits, `-` and `_`
const SLUG_ROUTES: &[(&str, &str, SlugHandler)] = &[("/api/maps/schematic/", "/move-token", sc_move_token), ("/api/maps/schematic/", "/pickup-item", sc_pickup),
    ("/api/maps/schematic/", "/buy-item", sc_buy),
    ("/maps/schematic/", "/door", sc_door), ("/maps/schematic/", "/fog", sc_fog),
    ("/maps/schematic/", "/rooms", sc_rooms), ("/maps/schematic/", "/rooms/detect", sc_rooms_detect), ("/maps/schematic/", "/lights", sc_lights), ("/maps/schematic/", "/walls", sc_walls),
    ("/maps/schematic/", "/elements", sc_elements), ("/maps/schematic/", "/rename", sc_rename), ("/maps/schematic/", "/delete", sc_delete),
    ("/maps/schematic/", "/grid", sc_grid), ("/maps/schematic/", "/link-combat", sc_link_combat), ("/maps/schematic/", "/unlink-combat", sc_unlink_combat),
    ("/maps/schematic/", "/pull-combat", sc_pull_combat), ("/maps/schematic/", "/push-combat", sc_push_combat), ("/maps/schematic/", "/light", sc_light), ("/boards/", "/save", board_save), ("/boards/", "/delete", board_delete), ("/kind/", "/bulk-delete", bulk_delete), ("/maps/", "/rename", map_rename), ("/maps/", "/delete", map_delete)];

fn slug_route(path: &str) -> Option<(SlugHandler, String)> {
    // /api/sessions/<id>/media/image/<uid>/<visibility|rename|remove>: the handler reads the session id and uid back from the path
    if let Some(rest) = path.strip_prefix("/api/sessions/") {
        let parts: Vec<&str> = rest.split('/').collect();
        if parts.len() == 5 && parts[1] == "media" && parts[2] == "image" && !parts[0].is_empty() && parts[0].len() <= 15
            && parts[0].bytes().all(|b| b.is_ascii_digit()) && !parts[3].is_empty() && parts[3].bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            let h: SlugHandler = match parts[4] {
                "visibility" => sm_image_visibility,
                "rename" => sm_image_rename,
                "remove" => sm_image_remove,
                _ => return None,
            };
            return Some((h, format!("{}/{}", parts[0], parts[3])));
        }
    }
    // /worlds/<id>/kinds/<kind id>/delete: the handler reads "<world id>/<kind id>"
    if let Some(rest) = path.strip_prefix("/worlds/").and_then(|r| r.strip_suffix("/delete")) {
        if let Some((wid, kid)) = rest.split_once("/kinds/") {
            if !wid.is_empty() && wid.len() <= 15 && wid.bytes().all(|b| b.is_ascii_digit()) && !kid.is_empty()
                && kid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            {
                return Some((kinds_delete, format!("{wid}/{kid}")));
            }
        }
    }
    for (pre, post, h) in SLUG_ROUTES {
        if let Some(slug) = path.strip_prefix(pre).and_then(|r| r.strip_suffix(post)) {
            if !slug.is_empty() && slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
                return Some((*h, slug.to_string()));
            }
        }
    }
    None
}

pub fn handles(method: &Method, path: &str) -> bool {
    (method == Method::POST && (route(path).is_some() || slug_route(path).is_some())) || (method == Method::PUT && route_in(PUT_ROUTES, path).is_some())
        || view_route(method, path).is_some()
}

/// The saved-layout routes (app/routers/view_prefs_api.py): a section name in the path, POST and DELETE
fn view_route(method: &Method, path: &str) -> Option<(SlugHandler, String)> {
    let ok = |s: &str| !s.is_empty() && s.len() <= 40 && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if let Some(sec) = path.strip_prefix("/api/view-prefs/").filter(|s| ok(s)) {
        return match *method {
            Method::POST => Some((view_pref_save as SlugHandler, sec.to_string())),
            Method::DELETE => Some((view_pref_reset as SlugHandler, sec.to_string())),
            _ => None,
        };
    }
    if let Some(rest) = path.strip_prefix("/api/characters/") {
        if let Some((pc, cid)) = rest.split_once("/hub/companions/") {
            let num = |t: &str| !t.is_empty() && t.len() <= 15 && t.bytes().all(|b| b.is_ascii_digit());
            if num(pc) && num(cid) {
                let key = format!("{pc}/{cid}");
                return match *method {
                    Method::PATCH => Some((hub_companion_edit as SlugHandler, key)),
                    Method::DELETE => Some((hub_companion_remove as SlugHandler, key)),
                    _ => None,
                };
            }
        }
    }
    if path == "/api/spotlight" && *method == Method::GET {
        return Some((spotlight_state as SlugHandler, String::new()));
    }
    if let Some(id) = path.strip_prefix("/api/characters/").and_then(|r| r.strip_suffix("/share")) {
        if !id.is_empty() && id.len() <= 15 && id.bytes().all(|b| b.is_ascii_digit()) {
            return match *method {
                Method::POST => Some((share_make as SlugHandler, id.to_string())),
                Method::DELETE => Some((share_revoke as SlugHandler, id.to_string())),
                _ => None,
            };
        }
    }
    if let Some(rest) = path.strip_prefix("/api/saved-views/") {
        if *method == Method::POST && ok(rest) {
            return Some((saved_view_save as SlugHandler, rest.to_string()));
        }
        if let Some((sec, id)) = rest.split_once('/') {
            if *method == Method::DELETE && ok(sec) && !id.is_empty() && id.len() <= 15 && id.bytes().all(|b| b.is_ascii_digit()) {
                return Some((saved_view_delete as SlugHandler, rest.to_string()));
            }
        }
    }
    None
}

fn run(cfg: &Config, method: &Method, uri: &Uri, headers: &HeaderMap, body: &[u8]) -> Option<Response<Body>> {
    let by_id = if method == Method::PUT { route_in(PUT_ROUTES, uri.path()) } else { route(uri.path()) };
    let by_slug = if by_id.is_none() { view_route(method, uri.path()).or_else(|| if *method == Method::POST { slug_route(uri.path()) } else { None }) } else { None };
    if by_id.is_none() && by_slug.is_none() {
        return None;
    }
    let db = crate::writes::open(cfg)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let p = crate::layout::build(&db, cfg, method.as_str(), uri, headers, now)?;
    // One transaction per request, as Python's one commit: a handler that gives up part-way (returns None, and the request goes to
    // Python) must leave nothing behind, or Python would apply the same write a second time
    db.execute_batch(if *method == Method::GET { "BEGIN" } else { "BEGIN IMMEDIATE" }).ok()?;
    let mut ctx = Ctx { cfg, upload: None, created: vec![], removals: vec![], db, p, uri, headers, body };
    let answer = match (by_id, by_slug) {
        (Some((h, ids)), _) => h(&mut ctx, &ids),
        (_, Some((h, slug))) => h(&mut ctx, &slug),
        _ => None,
    };
    if answer.is_some() && ctx.db.execute_batch("COMMIT").is_ok() {
        ctx.apply_removals();
        return answer;
    }
    let _ = ctx.db.execute_batch("ROLLBACK");
    for f in &ctx.created {
        let _ = std::fs::remove_file(f);
    }
    None
}

/// Entry point: reads the (small) body, tries to answer, and otherwise forwards the request - body included - to Python.
pub async fn serve(cfg: Arc<Config>, proxy: &Proxy, req: Request<Body>, peer: Option<std::net::SocketAddr>) -> Response<Body> {
    // a body of known, small length - or none at all (no Content-Length and no Transfer-Encoding: a DELETE, a bare POST)
    let small = match req.headers().get(header::CONTENT_LENGTH) {
        Some(v) => v.to_str().ok().and_then(|v| v.parse::<usize>().ok()).map_or(false, |n| n <= MAX_BODY),
        None => req.headers().get(header::TRANSFER_ENCODING).is_none(),
    };
    if !small {
        return proxy.forward(req, peer).await;
    }
    let (parts, body) = req.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, MAX_BODY).await else {
        return Response::builder().status(StatusCode::BAD_REQUEST).body(Body::empty()).unwrap();
    };
    let (method, uri, headers) = (parts.method.clone(), parts.uri.clone(), parts.headers.clone());
    let b = bytes.clone();
    let answer = tokio::task::spawn_blocking(move || run(&cfg, &method, &uri, &headers, &b)).await.ok().flatten();
    match answer {
        Some(r) => r,
        None => proxy.forward(Request::from_parts(parts, Body::from(bytes)), peer).await,
    }
}

// ── helpers ──────────────────────────────────────────────────────────────────

fn s(t: &str) -> Sql {
    Sql::Text(t.to_string())
}

fn strip(t: &str) -> String {
    nd_render::text::py_strip(t).to_string()
}

/// `str(form.get(name, default)).strip() or fallback`
fn field_or(f: &Form, name: &str, default: &str, fallback: &str) -> String {
    let v = strip(f.get(name).unwrap_or(default));
    if v.is_empty() { fallback.to_string() } else { v }
}

/// `int(form[name]) if form.get(name) else None` (Err: a value Python's int() would read differently - Python's call)
fn opt_int_field(f: &Form, name: &str) -> Result<Option<i64>, ()> {
    match f.get(name) {
        None | Some("") => Ok(None),
        Some(v) => py_int(v).map(Some).ok_or(()),
    }
}

/// `raw if json.loads(raw) succeeds else "[]"` - text this parser rejects but Python might read (NaN...) is Python's call
fn links_json(f: &Form) -> Option<String> {
    let raw = match f.get("linked_entities_json") {
        None | Some("") => "[]",
        Some(v) => v,
    };
    serde_json::from_str::<Value>(raw).ok()?;
    Some(raw.to_string())
}

fn row(db: &Connection, sql: &str, id: i64) -> Option<Map<String, Value>> {
    db.query_row(sql, [id], |r| crate::layout::row_map(r)).ok()
}

// ── quests (app/routers/quests.py) ───────────────────────────────────────────

/// The quest by id, if it belongs to the viewer's active world and they may edit it
fn editable_quest(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let q = row(&c.db, "SELECT * FROM quests WHERE id = ?1", id)?;
    if q.get("world_id")?.as_i64() != c.world_id() {
        return None;
    }
    c.can_edit_row("quests", q.get("created_by_user_id").and_then(|v| v.as_i64()))?.then_some(q)
}

fn quest_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("quests")? {
        return None;
    }
    let parent = opt_int_field(&f, "parent_id").ok()?;
    let party = opt_int_field(&f, "assigned_party_id").ok()?;
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let id = orm::insert(
        &c.db,
        "quests",
        &[
            ("world_id", Sql::Integer(wid)),
            ("title", s(&field_or(&f, "title", "", "Untitled Quest"))),
            ("status", s(&field_or(&f, "status", "active", "active"))),
            ("category", s(&field_or(&f, "category", "main", "main"))),
            ("summary", s(&strip(f.get("summary").unwrap_or("")))),
            ("body", s(f.get("body").unwrap_or(""))),
            ("parent_id", parent.map_or(Sql::Null, Sql::Integer)),
            ("assigned_party_id", party.map_or(Sql::Null, Sql::Integer)),
            ("created_by_user_id", owner),
            ("linked_entities_json", s(&links_json(&f)?)),
        ],
    )?;
    redirect(&format!("/quests/{id}"))
}

fn quest_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let q = editable_quest(c, id)?;
    let old_title = q.get("title")?.as_str()?.to_string();
    let title = field_or(&f, "title", &old_title, &old_title);
    let parent = match f.get("parent_id") {
        None | Some("") => None,
        Some(v) => Some(py_int(v)?).filter(|p| *p != id),
    };
    let party = opt_int_field(&f, "assigned_party_id").ok()?;
    let links = links_json(&f)?;
    orm::update(
        &c.db,
        "quests",
        id,
        &[
            ("title", s(&title)),
            ("status", s(&field_or(&f, "status", "active", "active"))),
            ("category", s(&field_or(&f, "category", "main", "main"))),
            ("summary", s(&strip(f.get("summary").unwrap_or("")))),
            ("body", s(f.get("body").unwrap_or(""))),
            ("parent_id", parent.map_or(Sql::Null, Sql::Integer)),
            ("assigned_party_id", party.map_or(Sql::Null, Sql::Integer)),
            ("linked_entities_json", s(&links)),
        ],
    )?;
    redirect(&format!("/quests/{id}?saved=1"))
}

fn quest_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    editable_quest(c, id)?;
    orm::bulk_update(&c.db, "quests", &[("parent_id", Sql::Null)], "quests.parent_id = ?", &[Sql::Integer(id)])?;
    c.db.execute("DELETE FROM quests WHERE id = ?1", [id]).ok()?;
    redirect("/quests")
}

fn quest_status(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let body = c.json()?;
    let q = editable_quest(c, id)?;
    let status = match body.get("status") {
        None => String::new(),
        Some(Value::String(v)) => strip(v),
        Some(_) => return None, // str() of another JSON type ("None", "True", "[1, 2]"): Python's call
    };
    let mut current = q.get("status").cloned().unwrap_or(Value::Null);
    if !status.is_empty() {
        orm::update(&c.db, "quests", id, &[("status", s(&status))])?;
        orm::touch(&c.db, q.get("world_id")?.as_i64()?)?;
        current = json!(status);
    }
    json_response(&json!({"status": current}))
}

// ── facts (app/routers/facts.py) ─────────────────────────────────────────────

/// `_safe_next`: "/facts" or "/sessions/<digits>", else "/facts" (None: a value Python's regex might read differently)
fn facts_next(f: &Form) -> Option<String> {
    let v = f.get("next").unwrap_or("");
    if !v.is_ascii() {
        return None;
    }
    let ok = v == "/facts" || v.strip_prefix("/sessions/").map_or(false, |d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()));
    Some(if ok { v.to_string() } else { "/facts".to_string() })
}

/// `_normalize_tags_field` for a form value (text or nothing)
fn fact_tags(f: &Form) -> Sql {
    match f.get("tags").map(strip) {
        Some(t) if !t.is_empty() => s(&t),
        _ => Sql::Null,
    }
}

fn fact_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("facts")? {
        return None;
    }
    let next = facts_next(&f)?;
    let content = strip(f.get("content").unwrap_or(""));
    if content.is_empty() {
        return redirect(&next);
    }
    let session = opt_int_field(&f, "game_session_id").ok()?;
    orm::insert(
        &c.db,
        "facts",
        &[
            ("world_id", Sql::Integer(wid)),
            ("game_session_id", session.map_or(Sql::Null, Sql::Integer)),
            ("content", s(&content)),
            ("visible_to_players", Sql::Integer(f.has("visible_to_players") as i64)),
            ("tags", fact_tags(&f)),
            ("author_id", Sql::Integer(c.p.user_id)),
            ("updated_at", s(&orm::now_stamp())),
        ],
    )?;
    redirect(&next)
}

/// The fact by id, in the viewer's active world, and the right to edit facts there
fn editable_fact(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let fact = row(&c.db, "SELECT * FROM facts WHERE id = ?1", id)?;
    if fact.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("facts")? {
        return None;
    }
    Some(fact)
}

fn fact_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    editable_fact(c, id)?;
    let next = facts_next(&f)?;
    let content = strip(f.get("content").unwrap_or(""));
    let session = opt_int_field(&f, "game_session_id").ok()?;
    let mut set: Vec<(&str, Sql)> = vec![];
    if !content.is_empty() {
        set.push(("content", s(&content)));
    }
    set.push(("visible_to_players", Sql::Integer(f.has("visible_to_players") as i64)));
    set.push(("tags", fact_tags(&f)));
    if let Some(sid) = session {
        set.push(("game_session_id", Sql::Integer(sid)));
    }
    set.push(("updated_at", s(&orm::now_stamp())));
    orm::update(&c.db, "facts", id, &set)?;
    redirect(&next)
}

fn fact_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let next = facts_next(&c.form()?)?;
    editable_fact(c, id)?;
    let wid = c.world_id()?;
    c.db.execute("DELETE FROM facts WHERE id = ?1", [id]).ok()?;
    // _bump_recap_content_touch: the world's recap-staleness watermark (a deleted fact leaves no row to timestamp)
    orm::update(&c.db, "worlds", wid, &[("recap_content_touch", s(&orm::now_stamp()))])?;
    redirect(&next)
}

// ── random tables (app/routers/tables.py) ────────────────────────────────────

/// `_slugify`: lowercase ASCII words joined by "-", at most 50 characters, unique over every table ("-2", "-3", ...); None for a non-ASCII
/// name (Python lowercases first, which can turn a non-ASCII letter into an ASCII one)
fn table_slug(db: &Connection, name: &str) -> Option<String> {
    if !name.is_ascii() {
        return None;
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in name.to_ascii_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    let base: String = slug.trim_matches('-').chars().take(50).collect();
    let base = if base.is_empty() { "table".to_string() } else { base };
    let taken = |sl: &str| -> Option<bool> {
        db.query_row("SELECT count(*) FROM random_tables WHERE slug = ?1", [sl], |r| r.get::<_, i64>(0)).ok().map(|n| n > 0)
    };
    let mut slug = base.clone();
    let mut n = 2;
    while taken(&slug)? {
        slug = format!("{base}-{n}");
        n += 1;
    }
    Some(slug)
}

/// `str(form.get("entries_json", "[]") or "[]")` kept when it parses as JSON, else "[]"
fn entries_json(f: &Form) -> Option<String> {
    let raw = match f.get("entries_json") {
        None | Some("") => "[]",
        Some(v) => v,
    };
    serde_json::from_str::<Value>(raw).ok()?;
    Some(raw.to_string())
}

fn table_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("tables")? {
        return None;
    }
    let name = field_or(&f, "name", "", "Unnamed Table");
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let slug = table_slug(&c.db, &name)?;
    let id = orm::insert(
        &c.db,
        "random_tables",
        &[
            ("world_id", Sql::Integer(wid)),
            ("name", s(&name)),
            ("slug", s(&slug)),
            ("category", s(&field_or(&f, "category", "", "general"))),
            ("description", s(&strip(f.get("description").unwrap_or("")))),
            ("is_builtin", Sql::Integer(0)),
            ("entries_json", s(&entries_json(&f)?)),
            ("created_by_user_id", owner),
        ],
    )?;
    redirect(&format!("/tables/{id}/edit"))
}

/// The table by id when it is global or of the viewer's active world and they may edit it (`_can_edit_table_row`)
fn editable_table(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let t = row(&c.db, "SELECT * FROM random_tables WHERE id = ?1", id)?;
    match t.get("world_id").and_then(|v| v.as_i64()) {
        None => {}
        Some(w) if Some(w) == c.world_id() => {}
        Some(_) => return None,
    }
    crate::pages::can_edit_table_row(&c.db, &c.p, &t)?.then_some(t)
}

fn table_update(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let t = editable_table(c, id)?;
    let mut set: Vec<(&str, Sql)> = vec![];
    let name;
    if t.get("is_builtin").and_then(|v| v.as_i64()).unwrap_or(0) == 0 {
        let old = t.get("name")?.as_str()?.to_string();
        name = field_or(&f, "name", &old, &old);
        set.push(("name", s(&name)));
        set.push(("category", s(&field_or(&f, "category", "", "general"))));
        set.push(("description", s(&strip(f.get("description").unwrap_or("")))));
    }
    set.push(("entries_json", s(&entries_json(&f)?)));
    orm::update(&c.db, "random_tables", id, &set)?;
    redirect(&format!("/tables/{id}/edit?saved=1"))
}

fn table_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let t = editable_table(c, id)?;
    if t.get("is_builtin").and_then(|v| v.as_i64()).unwrap_or(0) != 0 {
        return None;
    }
    c.db.execute("DELETE FROM random_tables WHERE id = ?1", [id]).ok()?;
    redirect("/tables")
}

fn table_roll(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    use rand::Rng;
    let t = row(&c.db, "SELECT * FROM random_tables WHERE id = ?1", ids[0])?;
    // world_row_visible: a global table, or one of the viewer's active world with the section open to them
    if let Some(w) = t.get("world_id").and_then(|v| v.as_i64()) {
        let world = c.p.world.clone()?;
        if Some(w) != c.world_id() || !crate::pages::section_ok(&c.p, Some(&world), "tables")? {
            return None;
        }
    }
    let raw = t.get("entries_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let entries = serde_json::from_str::<Value>(raw).ok()?.as_array()?.clone();
    if entries.is_empty() {
        return None;
    }
    // weights: max(0, int(e.get("weight", 1) or 1)) - whole numbers only here
    let mut weights = vec![];
    for e in &entries {
        let e = e.as_object()?;
        let w = match e.get("weight") {
            None | Some(Value::Null) => 1,
            Some(Value::Number(n)) if n.is_i64() => match n.as_i64()? {
                0 => 1,
                x => x.max(0),
            },
            Some(Value::Bool(false)) => 1,
            _ => return None,
        };
        weights.push(w);
    }
    let sum: i64 = weights.iter().sum();
    let total = if sum == 0 { entries.len() as i64 } else { sum };
    if sum == 0 {
        weights = vec![1; entries.len()];
    }
    let mut rng = rand::thread_rng();
    let roll = rng.gen_range(1..=total);
    let pick_sum: i64 = weights.iter().sum();
    let mut x = rng.gen_range(0..pick_sum);
    let mut chosen = &entries[entries.len() - 1];
    for (e, w) in entries.iter().zip(&weights) {
        if x < *w {
            chosen = e;
            break;
        }
        x -= w;
    }
    let label = chosen.get("label").cloned().unwrap_or(json!(""));
    json_response(&json!({"result": label, "roll": roll, "total": total}))
}

// ── calendar (app/routers/calendar.py) ───────────────────────────────────────

/// `_safe_int(value, default)` for a JSON value: whole numbers and missing / null only (anything else - a float, text, a bool - is
/// read by Python's int() in its own way: Python's call)
fn safe_int_json(v: Option<&Value>, default: i64) -> Option<i64> {
    match v {
        None | Some(Value::Null) => Some(default),
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        _ => None,
    }
}

/// `int(body[k]) if body.get(k) else None` for a JSON value
fn opt_int_json(v: Option<&Value>) -> Result<Option<i64>, ()> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) if n.is_i64() => Ok(match n.as_i64() {
            Some(0) => None,
            x => x,
        }),
        Some(Value::String(t)) if t.is_empty() => Ok(None),
        Some(Value::String(t)) => py_int(t).map(Some).ok_or(()),
        Some(Value::Bool(false)) => Ok(None),
        _ => Err(()),
    }
}

/// `str(body.get(k, default))` for text (or a missing key)
fn json_text<'a>(b: &'a Map<String, Value>, k: &str, default: &'a str) -> Option<&'a str> {
    match b.get(k) {
        None => Some(default),
        Some(Value::String(t)) => Some(t),
        Some(_) => None,
    }
}

/// `_can_manage_calendar`: calendar edit rights, GM or assistant
fn can_manage_calendar(c: &Ctx) -> Option<bool> {
    Some(c.can_edit_section("calendar")? && (c.p.is_gm || c.is_assistant()))
}

fn calendar_event_add(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("calendar")? {
        return None;
    }
    let (_, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let day = crate::calendar::clamp_day(&days, safe_int_json(body.get("day"), 1)?);
    let title = strip(json_text(&body, "title", "")?);
    let title = if title.is_empty() { "Event".to_string() } else { title };
    let id_of = |k: &str| -> Option<Sql> { Some(opt_int_json(body.get(k)).ok()?.map_or(Sql::Null, Sql::Integer)) };
    let owner = if c.p.is_gm || c.is_assistant() { Sql::Null } else { Sql::Integer(c.p.user_id) };
    let id = orm::insert(
        &c.db,
        "calendar_events",
        &[
            ("world_id", Sql::Integer(wid)),
            ("day", Sql::Integer(day)),
            ("title", s(&title)),
            ("notes", s(json_text(&body, "notes", "")?)),
            ("entity_id", id_of("entity_id")?),
            ("session_id", id_of("session_id")?),
            ("character_id", id_of("character_id")?),
            ("party_id", id_of("party_id")?),
            ("color", s(json_text(&body, "color", "#4488ff")?)),
            ("created_by_user_id", owner),
        ],
    )?;
    json_response(&json!({"id": id, "day": day, "title": title}))
}

fn calendar_event_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let ev = row(&c.db, "SELECT * FROM calendar_events WHERE id = ?1", ids[0])?;
    if ev.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_row("calendar", ev.get("created_by_user_id").and_then(|v| v.as_i64()))? {
        return None;
    }
    c.db.execute("DELETE FROM calendar_events WHERE id = ?1", [ids[0]]).ok()?;
    json_response(&json!({"ok": true}))
}

/// Writes the calendar's config back as `json.dumps(config)` does
fn save_calendar(db: &Connection, wid: i64, config: &Value) -> Option<()> {
    let id: i64 = db.query_row("SELECT id FROM world_calendars WHERE world_id = ?1 ORDER BY id LIMIT 1", [wid], |r| r.get(0)).ok()?;
    orm::update(db, "world_calendars", id, &[("config_json", s(&nd_render::py_dumps(config)))])?;
    Some(())
}

fn calendar_advance(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let (mut config, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let delta = safe_int_json(body.get("days"), 1)?;
    let stored = safe_int_json(config.get("current_day"), 1)?;
    let day = crate::calendar::clamp_day(&days, stored.checked_add(delta)?);
    config.as_object_mut()?.insert("current_day".into(), json!(day));
    save_calendar(&c.db, wid, &config)?;
    let (y, m, d) = crate::calendar::resolve(&days, day);
    json_response(&json!({"current_day": day, "year": y, "month_idx": m, "dom": d}))
}

fn calendar_set_date(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let (mut config, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let year = safe_int_json(body.get("year"), 1)?.clamp(1, crate::calendar::MAX_YEAR_PUB);
    let idx = safe_int_json(body.get("month_idx"), 0)?.clamp(0, days.len() as i64 - 1) as usize;
    let dom = safe_int_json(body.get("dom"), 1)?.min(days[idx]).max(1);
    let day = crate::calendar::start_of(&days, year, idx) + dom - 1;
    config.as_object_mut()?.insert("current_day".into(), json!(day));
    save_calendar(&c.db, wid, &config)?;
    json_response(&json!({"current_day": day, "year": year, "month_idx": idx, "dom": dom}))
}

// ── investigation boards (app/main.py) ───────────────────────────────────────

/// A FastAPI `Form(default)` parameter: an empty value counts as missing (None = required and missing: FastAPI's 422)
fn form_param<'a>(f: &'a Form, name: &str, default: Option<&'a str>) -> Option<&'a str> {
    match f.get(name) {
        Some(v) if !v.is_empty() => Some(v),
        _ => default,
    }
}

fn board_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("boards")? {
        return None;
    }
    let name = form_param(&f, "name", None)?.to_string();
    if !name.is_ascii() {
        return None; // the slug lowercases first: a non-ASCII letter may turn ASCII in Python
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in name.to_ascii_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    let base = slug.trim_matches('-').to_string();
    let base = if base.is_empty() { "board".to_string() } else { base };
    let taken = |sl: &str| -> Option<bool> { c.db.query_row("SELECT count(*) FROM invest_boards WHERE slug = ?1", [sl], |r| r.get::<_, i64>(0)).ok().map(|n| n > 0) };
    let mut slug = base.clone();
    let mut i = 2;
    while taken(&slug)? {
        slug = format!("{base}-{i}");
        i += 1;
    }
    orm::insert(
        &c.db,
        "invest_boards",
        &[
            ("world_id", Sql::Integer(wid)),
            ("name", s(&name)),
            ("slug", s(&slug)),
            ("description", s(form_param(&f, "description", Some(""))?)),
            ("canvas_bg", s(form_param(&f, "canvas_bg", Some("cork"))?)),
        ],
    )?;
    redirect(&format!("/boards/{slug}"))
}

/// The board by slug, of the viewer's active world, with board edit rights there
fn editable_board(c: &Ctx, slug: &str) -> Option<Map<String, Value>> {
    let b = c.db.query_row("SELECT * FROM invest_boards WHERE slug = ?1 ORDER BY id LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    if b.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("boards")? {
        return None;
    }
    Some(b)
}

fn board_save(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let body = c.json()?;
    let b = editable_board(c, slug)?;
    let pick = |k: &str| body.get(k).cloned().unwrap_or(json!([]));
    let mut nodes = Map::new();
    nodes.insert("nodes".into(), pick("nodes"));
    nodes.insert("groups".into(), pick("groups"));
    orm::update(
        &c.db,
        "invest_boards",
        b.get("id")?.as_i64()?,
        &[("nodes_json", s(&nd_render::py_dumps(&Value::Object(nodes)))), ("edges_json", s(&nd_render::py_dumps(&pick("edges"))))],
    )?;
    json_response(&json!({"ok": true}))
}

fn board_delete(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let b = editable_board(c, slug)?;
    c.db.execute("DELETE FROM invest_boards WHERE id = ?1", [b.get("id")?.as_i64()?]).ok()?;
    redirect("/boards")
}

// ── sessions (app/routers/sessions.py) ───────────────────────────────────────

/// `int(form.get(name) or default)`
fn int_or(f: &Form, name: &str, default: i64) -> Option<i64> {
    match f.get(name) {
        None | Some("") => Some(default),
        Some(v) => py_int(v),
    }
}

fn session_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("sessions")? {
        return None;
    }
    let date = strip(f.get("session_date").unwrap_or(""));
    let id = orm::insert(
        &c.db,
        "game_sessions",
        &[
            ("world_id", Sql::Integer(wid)),
            ("title", s(&field_or(&f, "title", "", "Untitled Session"))),
            ("session_num", Sql::Integer(int_or(&f, "session_num", 1)?)),
            ("session_date", if date.is_empty() { Sql::Null } else { s(&date) }),
            ("summary", s(f.get("summary").unwrap_or(""))),
            ("party_id", opt_int_field(&f, "party_id").ok()?.map_or(Sql::Null, Sql::Integer)),
            ("xp_awarded", Sql::Integer(int_or(&f, "xp_awarded", 0)?)),
        ],
    )?;
    redirect(&format!("/sessions/{id}"))
}

/// The session by id, of the viewer's active world, with session edit rights there (`_require_edit_section` by membership)
fn editable_session(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let gs = row(&c.db, "SELECT * FROM game_sessions WHERE id = ?1", id)?;
    if gs.get("world_id")?.as_i64() != c.world_id() || !c.can_edit_section("sessions")? {
        return None;
    }
    Some(gs)
}

fn session_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let id = ids[0];
    let f = c.form()?;
    let gs = editable_session(c, id)?;
    let old_title = gs.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let old_num = gs.get("session_num").and_then(|v| v.as_i64())?;
    let date = strip(f.get("session_date").unwrap_or(""));
    // npc_entity_ids: entity ids, and "pc:<id>" for player characters (each list in the order given)
    let (mut ents, mut pcs) = (vec![], vec![]);
    for v in f.0.iter().filter(|(k, _)| k == "npc_entity_ids").map(|(_, v)| v.as_str()) {
        match v.strip_prefix("pc:") {
            Some(p) => pcs.push(py_int(p)?),
            None => ents.push(py_int(v)?),
        }
    }
    let name_of = |table: &str, i: i64| -> Value {
        c.db.query_row(&format!("SELECT name FROM {table} WHERE id = ?1"), [i], |r| r.get::<_, Option<String>>(0)).ok().flatten().map_or(json!(""), |n| json!(n))
    };
    let mut npcs = vec![];
    for i in &ents {
        npcs.push(json!({"entity_id": i, "name": name_of("entities", *i), "kind": "entity"}));
    }
    for i in &pcs {
        npcs.push(json!({"entity_id": i, "name": name_of("player_characters", *i), "kind": "player_character"}));
    }
    orm::update(
        &c.db,
        "game_sessions",
        id,
        &[
            ("title", s(&field_or(&f, "title", &old_title, &old_title))),
            ("session_num", Sql::Integer(int_or(&f, "session_num", old_num)?)),
            ("session_date", if date.is_empty() { Sql::Null } else { s(&date) }),
            ("summary", s(f.get("summary").unwrap_or(""))),
            ("party_id", opt_int_field(&f, "party_id").ok()?.map_or(Sql::Null, Sql::Integer)),
            ("npcs_json", s(&nd_render::py_dumps(&Value::Array(npcs)))),
        ],
    )?;
    redirect(&format!("/sessions/{id}?saved=1"))
}

/// The session's prep list (`json.loads(gs.prep_json or "[]")`) - a list, or Python's call
fn prep_list(gs: &Map<String, Value>) -> Option<Vec<Value>> {
    let raw = gs.get("prep_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => Some(a),
        _ => None,
    }
}

fn save_prep(c: &Ctx, id: i64, prep: Vec<Value>) -> Option<Response<Body>> {
    let v = Value::Array(prep);
    orm::update(&c.db, "game_sessions", id, &[("prep_json", s(&nd_render::py_dumps(&v)))])?;
    json_response(&json!({"prep": v}))
}

fn prep_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let gs = editable_session(c, ids[0])?;
    // int(body.get("index", -1)): a missing key is -1; null (int(None)) and other types are Python's
    let idx = match body.get("index") {
        None => -1,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        Some(_) => return None,
    };
    let mut prep = prep_list(&gs)?;
    if idx >= 0 && (idx as usize) < prep.len() {
        let item = prep[idx as usize].as_object_mut()?;
        let done = match item.get("done") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => false,
            Some(Value::Bool(true)) => true,
            _ => return None, // `not` of another value: Python's truthiness rules
        };
        item.insert("done".into(), json!(!done));
    }
    save_prep(c, ids[0], prep)
}

fn prep_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let body = c.json()?;
    let gs = editable_session(c, ids[0])?;
    let task = strip(json_text(&body, "task", "")?);
    let mut prep = prep_list(&gs)?;
    if !task.is_empty() {
        prep.push(json!({"task": task, "done": false}));
    }
    save_prep(c, ids[0], prep)
}

fn prep_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = editable_session(c, ids[0])?;
    let mut prep = prep_list(&gs)?;
    let idx = ids[1];
    if idx >= 0 && (idx as usize) < prep.len() {
        prep.remove(idx as usize);
    }
    save_prep(c, ids[0], prep)
}

// ── entity links and notes (app/main.py; auth_gate lets only the GM / assistants reach these) ──

/// The entity by id, when it is of the viewer's active world (another world's is Python's)
fn world_entity(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let e = row(&c.db, "SELECT * FROM entities WHERE id = ?1", id)?;
    (e.get("world_id")?.as_i64() == c.world_id()).then_some(e)
}

fn entity_link(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (src, tgt) = (world_entity(c, ids[0])?, world_entity(c, ids[1])?);
    if src.get("world_id") != tgt.get("world_id") {
        return None;
    }
    c.db.execute("INSERT OR IGNORE INTO entity_links (source_id, target_id) VALUES (?1, ?2)", [ids[0], ids[1]]).ok()?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn entity_unlink(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    world_entity(c, ids[1])?;
    c.db.execute("DELETE FROM entity_links WHERE source_id = ?1 AND target_id = ?2", [ids[0], ids[1]]).ok()?;
    redirect(&format!("/entity/{}", ids[0]))
}

/// A required FastAPI `Form(...)` text: present and non-empty (an empty value is FastAPI's 422: Python's)
fn required(f: &Form, name: &str) -> Option<String> {
    f.get(name).filter(|v| !v.is_empty()).map(str::to_string)
}

/// `bool(visible)` for an optional `Form(None)`: an empty value counts as missing
fn flag_param(f: &Form, name: &str) -> i64 {
    f.get(name).map_or(0, |v| !v.is_empty() as i64)
}

fn note_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    world_entity(c, ids[0])?;
    let content = strip(&required(&f, "content")?);
    if !content.is_empty() {
        orm::insert(
            &c.db,
            "entity_notes",
            &[
                ("entity_id", Sql::Integer(ids[0])),
                ("author_id", Sql::Integer(c.p.user_id)),
                ("content", s(&content)),
                ("visible_to_players", Sql::Integer(flag_param(&f, "visible"))),
            ],
        )?;
    }
    redirect(&format!("/entity/{}", ids[0]))
}

/// The note, when it belongs to the entity (of the active world)
fn entity_note(c: &Ctx, entity_id: i64, note_id: i64) -> Option<Map<String, Value>> {
    world_entity(c, entity_id)?;
    let n = row(&c.db, "SELECT * FROM entity_notes WHERE id = ?1", note_id)?;
    (n.get("entity_id")?.as_i64() == Some(entity_id)).then_some(n)
}

fn note_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let n = entity_note(c, ids[0], ids[1])?;
    let shown = n.get("visible_to_players").and_then(|v| v.as_i64()).unwrap_or(0) != 0;
    orm::update(&c.db, "entity_notes", ids[1], &[("visible_to_players", Sql::Integer(!shown as i64))])?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn note_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    if let Some(n) = row(&c.db, "SELECT * FROM entity_notes WHERE id = ?1", ids[1]) {
        if n.get("entity_id")?.as_i64() == Some(ids[0]) {
            c.db.execute("DELETE FROM entity_notes WHERE id = ?1", [ids[1]]).ok()?;
        }
    }
    redirect(&format!("/entity/{}", ids[0]))
}

fn note_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    entity_note(c, ids[0], ids[1])?;
    let content = strip(&required(&f, "content")?);
    let mut set: Vec<(&str, Sql)> = vec![];
    if !content.is_empty() {
        set.push(("content", s(&content)));
    }
    set.push(("visible_to_players", Sql::Integer(flag_param(&f, "visible"))));
    orm::update(&c.db, "entity_notes", ids[1], &set)?;
    redirect(&format!("/entity/{}", ids[0]))
}

/// `s[:n]` in code points
fn take_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// A pydantic `str` field: a JSON string (anything else is pydantic's 422: Python's); `default` when absent
fn json_str<'a>(b: &'a Map<String, Value>, name: &str, default: Option<&'a str>) -> Option<&'a str> {
    match b.get(name) {
        None => default,
        Some(Value::String(v)) => Some(v),
        _ => None,
    }
}

/// `deps.can_edit_content`: the GM or a GM-assistant
fn content_editor(c: &Ctx) -> bool {
    c.p.is_gm || c.is_assistant()
}

fn entity_field(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (field, value) = (json_str(&b, "field", None)?, json_str(&b, "value", Some(""))?);
    if !content_editor(c) || field != "subtype" {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let value = take_chars(&nd_render::text::py_split_join(value), 64);
    let old = e.get("subtype").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if !value.is_empty() || e.get("subtype").is_some_and(|v| !v.is_null()) {
        let new = if value.is_empty() { Sql::Null } else { s(&value) };
        orm::update(&c.db, "entities", ids[0], &[("subtype", new)])?;
    }
    json_response(&json!({"ok": true, "id": ids[0], "field": "subtype", "value": value, "old": old}))
}

fn entity_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let name = json_str(&b, "name", None)?;
    if !content_editor(c) {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let name = take_chars(&nd_render::text::py_split_join(name), 256);
    let old = e.get("name").and_then(|v| v.as_str())?.to_string();
    if name.is_empty() || name == old {
        return None; // the 422s: Python's
    }
    orm::update(&c.db, "entities", ids[0], &[("name", s(&name))])?;
    json_response(&json!({"ok": true, "id": ids[0], "name": name, "old": old}))
}

fn bulk_folder(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let world = c.world_id()?;
    // only plain integer ids (Python's int() also takes numeric strings, floats and bools: those requests are Python's)
    let ids: Vec<i64> = match b.get("entity_ids") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.iter().map(|v| v.as_i64()).collect::<Option<_>>()?,
        _ => return None,
    };
    let folder = strip(json_str(&b, "folder", Some(""))?);
    let folder = if folder.is_empty() { Sql::Null } else { s(&folder) };
    let mut hit: Vec<i64> = vec![];
    for id in ids {
        let w: Option<i64> = c.db.query_row("SELECT world_id FROM entities WHERE id = ?1", [id], |r| r.get(0)).ok().flatten();
        if w == Some(world) && !hit.contains(&id) {
            hit.push(id);
        }
    }
    for id in &hit {
        orm::update(&c.db, "entities", *id, &[("folder", folder.clone())])?;
    }
    json_response(&json!({"updated": hit.len()}))
}

/// `urllib.parse.quote(s)` (safe="/")
const QUOTE: &percent_encoding::AsciiSet =
    &percent_encoding::NON_ALPHANUMERIC.remove(b'/').remove(b'_').remove(b'.').remove(b'-').remove(b'~');

/// A FastAPI `Form(default)` text: an empty value counts as missing
fn opt<'a>(f: &'a Form, name: &str, default: &'a str) -> &'a str {
    f.get(name).filter(|v| !v.is_empty()).unwrap_or(default)
}

/// `_normalize_aliases`; None when an alias is not ASCII (its casefold() is Python's)
fn normalize_aliases(raw: &str) -> Option<Option<String>> {
    let (mut seen, mut out): (Vec<String>, Vec<String>) = (vec![], vec![]);
    for part in raw.split(',') {
        let alias = strip(part);
        if alias.is_empty() {
            continue;
        }
        if !alias.is_ascii() {
            return None;
        }
        let alias = take_chars(&alias, 64);
        let key = alias.to_ascii_lowercase();
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        out.push(alias);
        if out.len() >= 20 {
            break;
        }
    }
    Some((!out.is_empty()).then(|| out.join(", ")))
}

/// The columns the create / edit forms set, as Python sets them, and the players for `_sync_entity_access`
struct EntityFields {
    cols: Vec<(&'static str, Sql)>,
    players: Vec<i64>,
    image_url: String,
}

fn entity_fields(f: &Form) -> Option<EntityFields> {
    let (kind, name) = (required(f, "kind")?, required(f, "name")?);
    // List[int] = Form([]): every value must be an int to pydantic (only plain digits here)
    let ids: Vec<i64> = f.all("allowed_player_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
    let template = match opt(f, "template_id", "") {
        "" => Sql::Null,
        v if v.bytes().all(|b| b.is_ascii_digit()) && v.len() <= 15 => Sql::Integer(v.parse().ok()?),
        v if v.is_ascii() => Sql::Null,
        _ => return None, // str.isdigit() knows other scripts' digits
    };
    let custom = opt(f, "custom_fields_json", "{}");
    if serde_json::from_str::<Value>(custom).is_err() {
        return None; // json.loads accepts more (NaN, ...) than serde: Python decides
    }
    let visibility = opt(f, "visibility_mode", "everyone");
    let or_null = |v: &str| if v.is_empty() { Sql::Null } else { s(v) };
    let folder = strip(opt(f, "folder", ""));
    let cols = vec![
        ("kind", s(&kind)),
        ("subtype", or_null(opt(f, "subtype", ""))),
        ("folder", or_null(&folder)),
        ("name", s(&name)),
        ("tags", or_null(opt(f, "tags", ""))),
        ("aliases", normalize_aliases(opt(f, "aliases", ""))?.map_or(Sql::Null, |a| s(&a))),
        ("roleplay_personality", s(&take_chars(&strip(opt(f, "roleplay_personality", "")), 4000))),
        ("summary", or_null(opt(f, "summary", ""))),
        ("body", or_null(opt(f, "body", ""))),
        ("visible_to_players", Sql::Integer((visibility == "everyone") as i64)),
        ("rag_priority", Sql::Integer(flag_param(f, "rag_priority"))),
        ("template_id", template),
        ("custom_fields_json", s(custom)),
    ];
    let mut players = vec![];
    if visibility == "players" {
        for id in ids {
            if !players.contains(&id) {
                players.push(id);
            }
        }
    }
    Some(EntityFields { cols, players, image_url: strip(opt(f, "image_url", "")) })
}

/// `_sync_entity_access`
fn sync_access(db: &Connection, entity_id: i64, players: &[i64]) -> Option<()> {
    db.execute("DELETE FROM entity_player_access WHERE entity_id = ?1", [entity_id]).ok()?;
    for uid in players {
        db.execute("INSERT INTO entity_player_access (entity_id, user_id) VALUES (?1, ?2)", [entity_id, *uid]).ok()?;
    }
    Some(())
}

fn entity_new(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "image_file")?;
    let world = c.world_id()?;
    let slug = c.p.world.as_ref()?.get("slug")?.as_str()?.to_string();
    let e = entity_fields(&f)?;
    let saved = match &pick {
        Some(p) => save_picture(c, p, "", true)?,
        None => None,
    };
    let mut cols = e.cols;
    cols.push(("world_id", Sql::Integer(world)));
    // save_upload(image_file) or (image_url.strip() or None)
    let image = saved.unwrap_or(e.image_url);
    cols.push(("image_url", if image.is_empty() { Sql::Null } else { s(&image) }));
    let id = orm::insert(&c.db, "entities", &cols)?;
    sync_access(&c.db, id, &e.players)?;
    if !opt(&f, "save_and_new", "").is_empty() {
        let q = |v: &str| utf8_percent_encode(v, QUOTE).to_string();
        let kind = required(&f, "kind")?;
        return redirect(&format!("/new?kind={}&folder={}&w={slug}", q(&kind), q(&strip(opt(&f, "folder", "")))));
    }
    redirect(&format!("/entity/{id}"))
}

fn entity_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "image_file")?;
    world_entity(c, ids[0])?;
    let e = entity_fields(&f)?;
    let uploaded = match &pick {
        Some(p) => save_picture(c, p, "", true)?,
        None => None,
    };
    let mut cols = e.cols;
    if let Some(u) = uploaded {
        cols.push(("image_url", s(&u)));
    } else if !e.image_url.is_empty() {
        cols.push(("image_url", s(&e.image_url)));
    } else if !opt(&f, "remove_image", "").is_empty() {
        cols.push(("image_url", Sql::Null));
    }
    orm::update(&c.db, "entities", ids[0], &cols)?;
    sync_access(&c.db, ids[0], &e.players)?;
    redirect(&format!("/entity/{}", ids[0]))
}

fn entity_duplicate(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let src = world_entity(c, ids[0])?;
    let v = |k: &str| -> Option<Sql> {
        Some(match src.get(k)? {
            Value::Null => Sql::Null,
            Value::String(t) => s(t),
            Value::Number(n) if n.is_i64() => Sql::Integer(n.as_i64()?),
            Value::Bool(b) => Sql::Integer(*b as i64),
            _ => return None,
        })
    };
    let name = format!("{} (copy)", src.get("name")?.as_str()?);
    let mut cols: Vec<(&str, Sql)> = vec![("name", s(&name))];
    for k in ["world_id", "kind", "subtype", "folder", "tags", "aliases", "image_url", "summary", "body", "visible_to_players", "template_id",
              "custom_fields_json"] {
        cols.push((k, v(k)?));
    }
    let id = orm::insert(&c.db, "entities", &cols)?;
    redirect(&format!("/entity/{id}"))
}

fn entity_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    world_entity(c, ids[0])?;
    let id = ids[0];
    for sql in [
        "DELETE FROM entity_links WHERE source_id = ?1 OR target_id = ?1",
        "DELETE FROM entity_player_access WHERE entity_id = ?1",
        "DELETE FROM entity_notes WHERE entity_id = ?1",
        "DELETE FROM entity_voice_hints WHERE entity_id = ?1",
        "DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1",
        "DELETE FROM entities WHERE id = ?1",
    ] {
        c.db.execute(sql, [id]).ok()?;
    }
    redirect("/")
}

/// The ids of a JSON list as plain integers (None when one is anything else: Python's int() reads more, so that request is Python's)
fn json_ids(v: Option<&Value>) -> Option<Vec<i64>> {
    match v {
        None | Some(Value::Null) => Some(vec![]),
        Some(Value::Array(a)) => a.iter().map(|v| v.as_i64()).collect(),
        Some(Value::Bool(false)) => Some(vec![]),
        _ => None,
    }
}

/// The ids among `ids` that are entities of `world`, each once (the `Entity.id.in_(ids)` query), in id order
fn world_entity_ids(db: &Connection, world: i64, ids: &[i64], kind: Option<&str>) -> Vec<i64> {
    let mut hit: Vec<i64> = vec![];
    for id in ids {
        let row: Option<(i64, String)> = db.query_row("SELECT world_id, kind FROM entities WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?))).ok();
        if let Some((w, k)) = row {
            if w == world && kind.map_or(true, |kd| kd == k) && !hit.contains(id) {
                hit.push(*id);
            }
        }
    }
    hit.sort();
    hit
}

fn bulk_visibility(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let world = c.world_id()?;
    let ids = json_ids(b.get("entity_ids"))?;
    let mode = strip(json_str(&b, "visibility_mode", Some(""))?);
    if !matches!(mode.as_str(), "everyone" | "gm" | "players") {
        return None; // the 400: Python's
    }
    let players = if mode == "players" { json_ids(b.get("allowed_player_ids"))? } else { vec![] };
    let mut uniq: Vec<i64> = vec![];
    for p in players {
        if !uniq.contains(&p) {
            uniq.push(p);
        }
    }
    let hit = world_entity_ids(&c.db, world, &ids, None);
    for id in &hit {
        orm::update(&c.db, "entities", *id, &[("visible_to_players", Sql::Integer((mode == "everyone") as i64))])?;
    }
    for id in &hit {
        sync_access(&c.db, *id, &uniq)?;
    }
    json_response(&json!({"updated": hit.len()}))
}

fn entity_alias(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let alias = json_str(&b, "alias", None)?;
    if !c.p.is_gm {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let alias = take_chars(&nd_render::text::py_split_join(&alias.replace(',', " ")), 64);
    let name = e.get("name").and_then(|v| v.as_str()).unwrap_or("");
    if alias.chars().count() < 2 || !alias.is_ascii() || !name.is_ascii() || alias.eq_ignore_ascii_case(name) {
        return None; // the 422s, and casefold() beyond ASCII: Python's
    }
    let existing: Vec<String> = e.get("aliases").and_then(|v| v.as_str()).unwrap_or("").split(',').map(strip).filter(|a| !a.is_empty()).collect();
    if existing.iter().any(|a| !a.is_ascii() || a.eq_ignore_ascii_case(&alias)) {
        return None;
    }
    let joined = existing.into_iter().chain([alias]).collect::<Vec<_>>().join(", ");
    let aliases = normalize_aliases(&joined)?;
    let v = aliases.as_ref().map_or(Sql::Null, |a| s(a));
    orm::update(&c.db, "entities", ids[0], &[("aliases", v)])?;
    json_response(&json!({"ok": true, "aliases": aliases}))
}

fn bulk_delete(c: &mut Ctx, kind: &str) -> Option<Response<Body>> {
    let f = c.form()?;
    let world = c.world_id()?;
    let slug = c.p.world.as_ref()?.get("slug")?.as_str()?.to_string();
    let mut ids = vec![];
    for v in f.all("entity_ids") {
        match py_int(v) {
            Some(n) => ids.push(n),
            None if v.is_empty() => {}             // int("") fails: skipped
            None => return None,                    // " 5", "+5", "1_0"...: Python's
        }
    }
    let hit = world_entity_ids(&c.db, world, &ids, Some(kind));
    for id in &hit {
        // as Python's bulk delete: links, access, notes, relations, the entity (voice hints stay, unlike the single delete)
        for sql in [
            "DELETE FROM entity_links WHERE source_id = ?1 OR target_id = ?1",
            "DELETE FROM entity_player_access WHERE entity_id = ?1",
            "DELETE FROM entity_notes WHERE entity_id = ?1",
            "DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1",
            "DELETE FROM entities WHERE id = ?1",
        ] {
            c.db.execute(sql, [id]).ok()?;
        }
    }
    let q = |v: &str| utf8_percent_encode(v, QUOTE).to_string();
    let mut params = vec![];
    if let Some(folder) = f.get("folder") {
        params.push(format!("folder={}", q(folder)));
    }
    let query = f.get("q").unwrap_or("");
    if !query.is_empty() {
        params.push(format!("q={}", q(query)));
    }
    let mut url = format!("/kind/{kind}");
    if !params.is_empty() {
        url += &format!("?{}", params.join("&"));
    }
    url += if url.contains('?') { "&" } else { "?" };
    redirect(&format!("{url}w={slug}"))
}

// ── parties (app/routers/parties.py) ─────────────────────────────────────────

/// `_can_manage_parties`: the GM or an assistant, with parties:edit
fn can_manage_parties(c: &Ctx) -> Option<bool> {
    Some(c.can_edit_section("parties")? && content_editor(c))
}

/// A stored JSON list of plain integers (None for anything else: Python's `in` / `==` would compare it its own way)
fn int_list(v: Option<&Value>) -> Option<Vec<i64>> {
    let raw = v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => a.iter().map(|x| x.as_i64()).collect(),
        _ => None,
    }
}

fn ints_json(ids: &[i64]) -> String {
    nd_render::py_dumps(&json!(ids))
}

/// `_party_for_write` + `_party_edit_level`: the party of the active world and the viewer's level ("full" / "member" / "none")
fn party_for_write(c: &Ctx, id: i64) -> Option<(Map<String, Value>, &'static str)> {
    let p = row(&c.db, "SELECT * FROM parties WHERE id = ?1", id)?;
    if p.get("world_id")?.as_i64() != c.world_id() {
        return None; // a GM's write in another world, or a 404: Python's
    }
    if can_manage_parties(c)? {
        return Some((p, "full"));
    }
    if !c.can_edit_section("parties")? {
        return Some((p, "none"));
    }
    let pcs = int_list(p.get("member_pc_ids_json"))?;
    let mut mine = false;
    for pc in pcs {
        let owner: Option<i64> = c.db.query_row("SELECT owner_user_id FROM player_characters WHERE id = ?1", [pc], |r| r.get(0)).ok().flatten();
        mine |= owner == Some(c.p.user_id);
    }
    Some((p, if mine { "member" } else { "none" }))
}

fn party_new(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let world = c.world_id()?;
    if !can_manage_parties(c)? {
        return None;
    }
    let name = strip(opt(&f, "name", "New Party"));
    let name = if name.is_empty() { "New Party".to_string() } else { name };
    let id = orm::insert(&c.db, "parties", &[("world_id", Sql::Integer(world)), ("name", s(&name))])?;
    orm::touch(&c.db, world)?;
    redirect(&format!("/parties/{id}"))
}

fn party_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let (p, level) = party_for_write(c, ids[0])?;
    let world = p.get("world_id")?.as_i64()?;
    let mut cols: Vec<(&str, Sql)> = vec![("notes", s(&strip(f.get("notes").unwrap_or("")))), ("goals", s(&strip(f.get("goals").unwrap_or(""))))];
    match level {
        "none" => return None,
        "full" => {
            if !c.p.is_gm {
                return None; // an assistant's view of hidden companions (filter_visible_entities): Python's
            }
            let old = p.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let name = strip(f.get("name").unwrap_or(&old));
            cols.push(("name", s(if name.is_empty() { &old } else { &name })));
            let pcs: Vec<i64> = f.all("member_pc_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
            let ents: Vec<i64> = f.all("member_entity_ids").iter().map(|v| py_int(v)).collect::<Option<_>>()?;
            let in_world = |table: &str, id: i64| -> bool {
                c.db.query_row(&format!("SELECT world_id FROM {table} WHERE id = ?1"), [id], |r| r.get::<_, Option<i64>>(0)).ok().flatten() == Some(world)
            };
            let pcs: Vec<i64> = pcs.into_iter().filter(|i| in_world("player_characters", *i)).collect();
            let ents: Vec<i64> = ents.into_iter().filter(|i| in_world("entities", *i)).collect();
            cols.push(("member_pc_ids_json", s(&ints_json(&pcs))));
            cols.push(("member_entity_ids_json", s(&ints_json(&ents))));
        }
        _ => {}
    }
    orm::update(&c.db, "parties", ids[0], &cols)?;
    orm::touch(&c.db, world)?;
    redirect(&format!("/parties/{}", ids[0]))
}

fn party_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    orm::bulk_update(&c.db, "quests", &[("assigned_party_id", Sql::Null)], "assigned_party_id = ?", &[Sql::Integer(ids[0])])?;
    c.db.execute("DELETE FROM parties WHERE id = ?1", [ids[0]]).ok()?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    redirect("/parties")
}

fn party_location(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    let kind = b.get("kind").and_then(|v| v.as_str());
    let loc = match kind {
        Some(k @ ("map" | "schematic")) => {
            let slug = strip(json_str(&b, "slug", Some(""))?);
            if slug.is_empty() {
                return None;
            }
            // float(x) of a JSON number (strings, bools and the rest: Python's)
            let num = |k: &str| -> Option<f64> { b.get(k).filter(|v| v.is_number())?.as_f64() };
            let (a, bk) = if k == "map" { ("lat", "lng") } else { ("x", "y") };
            let mut m = Map::new();
            m.insert("kind".into(), json!(k));
            m.insert("slug".into(), json!(slug));
            m.insert(a.into(), Value::from(num(a)?));
            m.insert(bk.into(), Value::from(num(bk)?));
            Value::Object(m)
        }
        _ if b.get("kind").map_or(true, |v| v.is_string() || v.is_null()) => json!({}),
        _ => return None,
    };
    if !loc.as_object()?.values().all(|v| !v.as_f64().is_some_and(|f| !f.is_finite())) {
        return None;
    }
    orm::update(&c.db, "parties", ids[0], &[("location_json", s(&nd_render::py_dumps(&loc)))])?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    json_response(&json!({"location": loc}))
}

fn party_member_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    let text = |k: &str| -> Option<String> {
        match b.get(k) {
            None | Some(Value::Null) | Some(Value::Bool(false)) => Some(String::new()),
            Some(Value::String(v)) => Some(v.clone()),
            _ => None,
        }
    };
    let kind = text("kind")?;
    if kind != "pc" && (kind != "entity" || !c.p.is_gm) {
        return None; // a bad kind's 400, and a non-GM's view of hidden entities: Python's
    }
    let member_id = match b.get("id") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => 0,
        Some(Value::String(v)) if v.is_empty() => 0,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    let name = strip(&text("name")?);
    let world = p.get("world_id")?.as_i64()?;
    let table = if kind == "pc" { "player_characters" } else { "entities" };
    let found: Option<(i64, Option<i64>)> = if member_id != 0 {
        c.db.query_row(&format!("SELECT id, world_id FROM {table} WHERE id = ?1"), [member_id], |r| Ok((r.get(0)?, r.get(1)?))).ok()
    } else if !name.is_empty() {
        c.db.query_row(&format!("SELECT id, world_id FROM {table} WHERE world_id = ?1 AND name = ?2 LIMIT 1"), rusqlite::params![world, name],
                       |r| Ok((r.get(0)?, r.get(1)?))).ok()
    } else {
        None
    };
    let (mid, mw) = found?;
    if mw != Some(world) {
        return None;
    }
    let field = if kind == "pc" { "member_pc_ids_json" } else { "member_entity_ids_json" };
    let mut list = int_list(p.get(field))?;
    let added = match list.iter().position(|x| *x == mid) {
        Some(i) => {
            list.remove(i);
            false
        }
        None => {
            list.push(mid);
            true
        }
    };
    orm::update(&c.db, "parties", ids[0], &[(field, s(&ints_json(&list)))])?;
    orm::touch(&c.db, world)?;
    json_response(&json!({"ok": true, "added": added}))
}

const LOOT_NAME_MAX: usize = 120;
const LOOT_NOTES_MAX: usize = 500;
const LOOT_QTY_MAX: i64 = 9999;

/// `load_loot` for a stash that is already clean (every item an object with its own string `lid`, text name / notes, an int qty and int
/// claims); None for anything `load_loot` would repair (derived lids, str() of other values): Python's
fn load_loot(p: &Map<String, Value>) -> Option<Vec<Map<String, Value>>> {
    let raw = p.get("loot_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let Value::Array(items) = serde_json::from_str::<Value>(raw).ok()? else { return None };
    let mut seen: Vec<String> = vec![];
    let mut out = vec![];
    for it in items {
        let Value::Object(mut m) = it else { return None };
        let lid = m.get("lid")?.as_str().filter(|l| !l.is_empty() && !seen.iter().any(|x| x == l))?.to_string();
        seen.push(lid.clone());
        let name = match m.get("name") {
            None | Some(Value::Null) => "Item".to_string(),
            Some(Value::String(n)) if n.is_empty() => "Item".to_string(),
            Some(Value::String(n)) => take_chars(n, LOOT_NAME_MAX),
            _ => return None,
        };
        let qty = match m.get("qty") {
            None | Some(Value::Null) => 1,
            Some(Value::Number(n)) => n.as_i64()?.max(1),
            _ => return None,
        };
        let notes = match m.get("notes") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(n)) => take_chars(n, LOOT_NOTES_MAX),
            _ => return None,
        };
        let claimed: Vec<i64> = match m.get("claimed_by") {
            None | Some(Value::Null) => vec![],
            Some(Value::Array(a)) => a.iter().map(|x| x.as_i64()).collect::<Option<_>>()?,
            _ => return None,
        };
        m.insert("lid".into(), json!(lid));
        m.insert("name".into(), json!(name));
        m.insert("qty".into(), json!(qty));
        m.insert("notes".into(), json!(notes));
        m.insert("claimed_by".into(), json!(claimed));
        out.push(m);
    }
    Some(out)
}

/// `_loot_int` for a JSON int (strings, floats and bools are int()'s: Python's; out of range is the 400: Python's)
fn loot_int(b: &Map<String, Value>, key: &str, default: Option<i64>, lo: Option<i64>, hi: Option<i64>) -> Option<i64> {
    let v = match b.get(key) {
        None => default?,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    (lo.map_or(true, |l| v >= l) && hi.map_or(true, |h| v <= h)).then_some(v)
}

fn party_loot(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (p, level) = party_for_write(c, ids[0])?;
    if level == "none" {
        return None;
    }
    let mut loot = load_loot(&p)?;
    let members = int_list(p.get("member_pc_ids_json"))?;
    let index = |loot: &[Map<String, Value>]| -> Option<usize> {
        match b.get("lid") {
            Some(Value::Null) | None => {
                let i = loot_int(&b, "index", Some(-1), None, None)?;
                (0..loot.len() as i64).contains(&i).then_some(i as usize)
            }
            Some(lid) => loot.iter().position(|it| it.get("lid") == Some(lid)), // gone: the 409, Python's
        }
    };
    match b.get("action").and_then(|v| v.as_str()) {
        Some("add") => {
            let name = strip(b.get("name")?.as_str()?);
            if name.is_empty() {
                return None;
            }
            let notes = json_str(&b, "notes", Some(""))?;
            let qty = loot_int(&b, "qty", Some(1), Some(1), Some(LOOT_QTY_MAX))?;
            let lid = format!("{:08x}", rand::random::<u32>());
            let mut m = Map::new();
            m.insert("lid".into(), json!(lid));
            m.insert("name".into(), json!(take_chars(&name, LOOT_NAME_MAX)));
            m.insert("qty".into(), json!(qty));
            m.insert("notes".into(), json!(take_chars(&strip(notes), LOOT_NOTES_MAX)));
            m.insert("claimed_by".into(), json!([]));
            loot.push(m);
        }
        Some("remove") => {
            let i = index(&loot)?;
            loot.remove(i);
        }
        Some(a @ ("claim" | "unclaim")) => {
            let i = index(&loot)?;
            let pc = loot_int(&b, "pc_id", None, None, None)?;
            if !members.contains(&pc) {
                return None;
            }
            if level != "full" {
                let owner: Option<i64> = c.db.query_row("SELECT owner_user_id FROM player_characters WHERE id = ?1", [pc], |r| r.get(0)).ok().flatten();
                if owner != Some(c.p.user_id) {
                    return None;
                }
            }
            let claimed = loot[i].get_mut("claimed_by")?.as_array_mut()?;
            let at = claimed.iter().position(|x| x.as_i64() == Some(pc));
            match (a, at) {
                ("claim", None) => claimed.push(json!(pc)),
                ("unclaim", Some(k)) => {
                    claimed.remove(k);
                }
                _ => {}
            }
        }
        _ => return None, // give (the character's equipment, by its system) and bad actions: Python's
    }
    let list = Value::Array(loot.into_iter().map(Value::Object).collect());
    orm::update(&c.db, "parties", ids[0], &[("loot_json", s(&nd_render::py_dumps(&list)))])?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    json_response(&json!({"loot": list}))
}

// ── characters (app/routers/characters.py, app/pc_stats.py, app/char_extras.py) ──

/// The character by id, when it is of the active world and the viewer may manage it (`_can_manage_character`: the GM or its owner)
fn managed_pc(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id)?;
    if pc.get("world_id")?.as_i64() != c.world_id() {
        return None;
    }
    (c.p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(c.p.user_id)).then_some(pc)
}

fn col_int(m: &Map<String, Value>, k: &str) -> i64 {
    m.get(k).and_then(|v| v.as_i64()).unwrap_or(0)
}

/// `stat_values(pc)`; None for stats Python reads its own way (text values, unhashable ids, JSON serde does not take)
fn stat_values(pc: &Map<String, Value>) -> Option<Vec<(String, i64)>> {
    let raw = pc.get("stats_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let stats = match serde_json::from_str::<Value>(raw) {
        Ok(v) => v,
        Err(_) => return None,
    };
    let mut out: Vec<(String, i64)> = vec![];
    for s in stats.as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let Some(o) = s.as_object() else { continue };
        let id = match o.get("id") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => continue,
            Some(Value::String(x)) if x.is_empty() => continue,
            Some(Value::String(x)) => x.clone(),
            Some(Value::Number(n)) if n.as_f64() == Some(0.0) => continue,
            Some(Value::Number(n)) => format!("\u{0}#{n}"), // a number id: never a stat name
            _ => return None,
        };
        // int(float(value or 0)); float()'s TypeError / ValueError is 0, int() of an infinity or NaN Python's error
        let v = if py_truthy(o.get("value")) {
            match py_float(o.get("value")).ok()? {
                None => 0,
                Some(f) if f.is_finite() && f.abs() < 9e15 => f.trunc() as i64,
                Some(f) if f.is_nan() => 0,
                Some(_) => return None,
            }
        } else {
            0
        };
        out.retain(|(k, _)| *k != id);
        out.push((id, v));
    }
    Some(out)
}

/// `pc_maxima(pc)`: (hp, shock, pp, mp, native)
fn pc_maxima(db: &Connection, pc: &Map<String, Value>) -> Option<(i64, i64, i64, i64, bool)> {
    let stats = stat_values(pc)?;
    let tpl = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0);
    let mut native = !stats.is_empty() || tpl.is_none();
    if native {
        if let Some(t) = tpl {
            let mode: Option<String> = db.query_row("SELECT sheet_mode FROM sheet_templates WHERE id = ?1", [t], |r| r.get(0)).ok().flatten();
            native = mode.as_deref() != Some("custom");
        }
    }
    let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let (phys, ment) = if native {
        (get("str") + get("dex") + get("bod") + get("per"), get("wil") + get("int") + get("cha") + get("itu"))
    } else {
        (0, 0)
    };
    let (hp, shock) = (col_int(pc, "max_hp"), col_int(pc, "shock_max"));
    Some((if hp > 0 { hp } else if native { phys + 10 } else { 0 }, if shock > 0 { shock } else { ment }, phys, ment, native))
}

/// `int_field(body, key)` for a JSON int (floats, numeric text and bools are int()'s: Python's)
fn body_int(b: &Map<String, Value>, key: &str) -> Option<i64> {
    match b.get(key) {
        None => Some(0),
        Some(Value::Number(n)) => n.as_i64().filter(|v| v.abs() < 1 << 53),
        _ => None,
    }
}

/// `char_extras.log` (with `actor_name` of the viewer), keeping the newest 200 per character
fn char_log(c: &Ctx, pc: &Map<String, Value>, kind: &str, text: &str, undo: &Value) -> Option<()> {
    let (name, email): (Option<String>, Option<String>) =
        c.db.query_row("SELECT display_name, email FROM users WHERE id = ?1", [c.p.user_id], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    let actor = [name, email].into_iter().flatten().find(|v| !v.is_empty()).unwrap_or_else(|| "Someone".into());
    let pc_id = pc.get("id")?.as_i64()?;
    orm::insert(
        &c.db,
        "character_log",
        &[
            ("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)),
            ("character_id", Sql::Integer(pc_id)),
            ("actor", s(&take_chars(&actor, 120))),
            ("kind", s(&take_chars(kind, 24))),
            ("text", s(&take_chars(text, 300))),
            ("undo_json", s(&nd_render::py_dumps(undo))),
        ],
    )?;
    c.db.execute(
        "DELETE FROM character_log WHERE id IN (SELECT id FROM character_log WHERE character_id = ?1 ORDER BY id DESC LIMIT -1 OFFSET 200)",
        [pc_id],
    )
    .ok()?;
    Some(())
}

/// `_log_num`: a numeric change, with its undo
fn log_num(c: &Ctx, pc: &Map<String, Value>, op: &str, label: &str, before: i64, after: i64) -> Option<()> {
    if before != after {
        char_log(c, pc, op, &format!("{label} {before} \u{2192} {after}"), &json!({"op": op, "value": before}))?;
    }
    Some(())
}

fn pc_hp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let action = b.get("action").cloned().unwrap_or(json!("set"));
    let val = body_int(&b, "value")?;
    let before = col_int(&pc, "current_hp");
    let (max_hp, ..) = pc_maxima(&c.db, &pc)?;
    let temp = col_int(&pc, "temp_hp");
    let mut set: Vec<(&str, Sql)> = vec![];
    let mut after = pc.clone();
    let cur = match action.as_str() {
        Some("delta") => (before + val).min(max_hp + temp).max(0),
        Some("temp") => {
            set.push(("temp_hp", Sql::Integer(val.max(0))));
            after.insert("temp_hp".into(), json!(val.max(0)));
            before
        }
        Some("max") => {
            set.push(("max_hp", Sql::Integer(val.max(0))));
            after.insert("max_hp".into(), json!(val.max(0)));
            let (new_max, ..) = pc_maxima(&c.db, &after)?;
            if new_max != 0 { before.min(new_max) } else { before }
        }
        _ => val.min(max_hp + temp).max(0),
    };
    if action.as_str() != Some("temp") {
        set.push(("current_hp", Sql::Integer(cur)));
        after.insert("current_hp".into(), json!(cur));
    }
    orm::update(&c.db, "player_characters", ids[0], &set)?;
    log_num(c, &pc, "hp", "HP", before, cur)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let (max_after, ..) = pc_maxima(&c.db, &after)?;
    let field = |k: &str| after.get(k).cloned().unwrap_or(Value::Null);
    let or0 = |k: &str| if after.get(k).is_none_or(|v| v.is_null()) { json!(0) } else { field(k) };
    json_response(&json!({
        "current_hp": field("current_hp"),
        "max_hp": max_after,
        "temp_hp": or0("temp_hp"),
        "death_success": or0("death_saves_success"),
        "death_failure": or0("death_saves_failure"),
        "secondary_current": field("secondary_resource_current"),
    }))
}

/// The Shock / PP / MP steppers: (column, log op, label, which maximum, whether "rest" restores half)
fn pc_pool(c: &mut Ctx, id: i64, col: &str, op: &str, label: &str, which: usize, rest: bool, max_key: &str) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, id)?;
    let action = b.get("action").cloned().unwrap_or(json!("set"));
    let val = body_int(&b, "value")?;
    let before = col_int(&pc, col);
    let m = pc_maxima(&c.db, &pc)?;
    let max = [m.0, m.1, m.2, m.3][which];
    let cur = match action.as_str() {
        Some("delta") => (before + val).min(max).max(0),
        Some("set") => val.min(max).max(0),
        Some("rest") if rest => (before + max.div_euclid(2)).min(max),
        _ => before,
    };
    orm::update(&c.db, "player_characters", id, &[(col, Sql::Integer(cur))])?;
    log_num(c, &pc, op, label, before, cur)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let mut out = Map::new();
    out.insert(col.into(), json!(cur));
    out.insert(max_key.into(), json!(max));
    json_response(&Value::Object(out))
}

fn pc_shock(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "shock_current", "shock", "Shock", 1, false, "shock_max")
}

fn pc_pp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "pp_current", "pp", "PP", 2, true, "pp_max")
}

fn pc_mp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    pc_pool(c, ids[0], "mp_current", "mp", "MP", 3, true, "mp_max")
}

const XP_THRESHOLDS: [i64; 20] = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000, 85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000,
                                  305000, 355000];

fn pc_xp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let delta = match b.get("delta") {
        None => 0,
        Some(Value::Number(n)) => n.as_i64().filter(|v| v.abs() < 1 << 40)?,
        _ => return None,
    };
    let level = pc.get("level")?.as_i64().filter(|l| *l >= 1)?;
    let before = col_int(&pc, "xp");
    let xp = (before + delta).max(0);
    orm::update(&c.db, "player_characters", ids[0], &[("xp", Sql::Integer(xp))])?;
    log_num(c, &pc, "xp", "XP", before, xp)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let lvl = level.min(20) as usize;
    let lo = XP_THRESHOLDS[lvl - 1];
    let hi = (lvl < 20).then(|| XP_THRESHOLDS[lvl]);
    let pct = match hi {
        Some(h) if h > lo => ((xp - lo).max(0) * 100 / (h - lo)).min(100),
        _ => 100,
    };
    json_response(&json!({"xp": xp, "xp_lo": lo, "xp_hi": hi, "xp_pct": pct}))
}

/// `clean_condition` for ASCII text (str.isprintable() beyond ASCII: Python's); None = Python's, Some("") = nothing usable
fn clean_condition(v: &Value) -> Option<String> {
    let Value::String(raw) = v else { return Some(String::new()) };
    if !raw.is_ascii() {
        return None;
    }
    let text: String = raw.chars().filter(|ch| (' '..='~').contains(ch)).collect();
    Some(strip(&take_chars(&nd_render::text::py_split_join(&text), 40)))
}

fn clean_conditions(seq: &Value) -> Option<Vec<String>> {
    let (mut out, mut seen): (Vec<String>, Vec<String>) = (vec![], vec![]);
    for raw in seq.as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let cnd = clean_condition(raw)?;
        if !cnd.is_empty() && !seen.contains(&cnd.to_ascii_lowercase()) {
            seen.push(cnd.to_ascii_lowercase());
            out.push(cnd);
        }
    }
    out.truncate(12);
    Some(out)
}

fn pc_conditions(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let raw = pc.get("conditions_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let current = match serde_json::from_str::<Value>(raw) {
        Ok(v) => clean_conditions(&v)?,
        Err(_) => return None, // json.loads' own idea of bad JSON: Python's
    };
    let action = b.get("action").cloned().unwrap_or(json!("toggle"));
    let new = match action.as_str() {
        Some("set") => {
            let list = b.get("conditions").filter(|v| v.is_array())?;
            clean_conditions(list)?
        }
        Some(a @ ("add" | "remove" | "toggle")) => {
            let name = clean_condition(b.get("name").unwrap_or(&Value::Null))?;
            if name.is_empty() {
                return None;
            }
            let has = current.iter().any(|x| x.eq_ignore_ascii_case(&name));
            if a == "remove" || (a == "toggle" && has) {
                current.iter().filter(|x| !x.eq_ignore_ascii_case(&name)).cloned().collect()
            } else if has {
                current.clone()
            } else if current.len() >= 12 {
                return None;
            } else {
                current.iter().cloned().chain([name]).collect()
            }
        }
        _ => return None,
    };
    if new != current {
        let gained = new.iter().filter(|x| !current.contains(x)).map(|x| format!("+{x}"));
        let lost = current.iter().filter(|x| !new.contains(x)).map(|x| format!("\u{2212}{x}"));
        let text = gained.chain(lost).collect::<Vec<_>>().join(", ");
        char_log(c, &pc, "condition", &text, &json!({"op": "conditions", "value": current}))?;
    }
    orm::update(&c.db, "player_characters", ids[0], &[("conditions_json", s(&nd_render::py_dumps(&json!(new))))])?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"conditions": new}))
}

/// A stored JSON column as Python's json.dumps wrote it (re-dumping it must give the same text, else its numbers or spacing are something
/// serde would change: Python's)
fn canonical_json(raw: &str) -> Option<Value> {
    let v: Value = serde_json::from_str(raw).ok()?;
    (nd_render::py_dumps(&v) == raw).then_some(v)
}

fn pc_currency(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let raw = pc.get("currency_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let mut coins = if raw == "[]" { json!([]) } else { canonical_json(raw)? };
    let key = match b.get("abbr") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::String(v)) if v.is_ascii() => strip(v).to_ascii_lowercase(),
        _ => return None,
    };
    let text_of = |m: &Map<String, Value>, k: &str| -> Option<String> {
        match m.get(k) {
            None | Some(Value::Null) => Some(String::new()),
            Some(Value::String(v)) if v.is_ascii() => Some(v.clone()),
            _ => None,
        }
    };
    let mut hit = None;
    for (i, coin) in coins.as_array()?.iter().enumerate() {
        let Some(m) = coin.as_object() else { continue };
        let (ab, lb) = (text_of(m, "abbr")?, text_of(m, "label")?);
        if !key.is_empty() && (ab.to_ascii_lowercase() == key || lb.to_ascii_lowercase() == key) {
            hit = Some((i, ab, lb));
            break;
        }
    }
    let (i, ab, lb) = hit?;
    let coin = coins.as_array_mut()?.get_mut(i)?.as_object_mut()?;
    let before = match coin.get("value") {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.abs() < 1e15)?.trunc() as i64,
        _ => return None,
    };
    let val = body_int(&b, "value")?;
    let after = if b.get("action").and_then(|v| v.as_str()) == Some("delta") { (before + val).max(0) } else { val.max(0) };
    coin.insert("value".into(), json!(after));
    orm::update(&c.db, "player_characters", ids[0], &[("currency_json", s(&nd_render::py_dumps(&coins)))])?;
    let name = if !ab.is_empty() { ab.clone() } else { lb.clone() };
    if after != before {
        let label = if name.is_empty() { "coins".to_string() } else { name.clone() };
        let undo_abbr = if name.is_empty() { Value::Null } else { json!(name) };
        char_log(c, &pc, "currency", &format!("{label} {before} \u{2192} {after}"), &json!({"op": "currency", "abbr": undo_abbr, "value": before}))?;
    }
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"abbr": if name.is_empty() { Value::Null } else { json!(name) }, "value": after}))
}

/// `gi(k, d)`: `int(data.get(k) or d)` (an int() Python reads its own way - signs, spaces, other digits - is Python's)
fn form_int(f: &Form, k: &str, d: i64) -> Option<i64> {
    match f.get(k).filter(|v| !v.is_empty()) {
        None => Some(d),
        Some(v) => py_int(v),
    }
}

/// `gs(k, d)`: `str(data.get(k) or d).strip()`
fn form_text(f: &Form, k: &str, d: &str) -> String {
    strip(f.get(k).filter(|v| !v.is_empty()).unwrap_or(d))
}

/// `_apply_form(pc, data, partial)`: the columns to write (`cur` = the stored row, empty for a new character)
fn apply_pc_form(db: &Connection, f: &Form, cur: &Map<String, Value>, partial: bool) -> Option<Vec<(&'static str, Sql)>> {
    let has = |k: &str| !partial || f.has(k);
    let text_col = |k: &str| cur.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mut cols: Vec<(&'static str, Sql)> = vec![];
    let mut name = text_col("name");
    for k in ["name", "player_name", "race", "char_class", "backstory", "notes"] {
        if has(k) {
            let v = form_text(f, k, "");
            if k == "name" {
                name = v;
            } else {
                cols.push((k, s(&v)));
            }
        }
    }
    cols.push(("name", s(if name.is_empty() { "Unnamed" } else { &name })));
    cols.push(("race_id", s(&form_text(f, "race_id", &text_col("race_id")))));
    cols.push(("profession_id", s(&form_text(f, "profession_id", &text_col("profession_id")))));
    if has("level") {
        cols.push(("level", Sql::Integer(form_int(f, "level", 1)?.clamp(1, 20))));
    }
    if has("xp") {
        cols.push(("xp", Sql::Integer(form_int(f, "xp", 0)?.max(0))));
    }
    let mut max_hp = col_int(cur, "max_hp");
    if has("max_hp") {
        max_hp = form_int(f, "max_hp", 0)?.max(0);
        cols.push(("max_hp", Sql::Integer(max_hp)));
    }
    let hp_given = has("current_hp") && !strip(f.get("current_hp").unwrap_or("")).is_empty();
    if hp_given {
        cols.push(("current_hp", Sql::Integer(form_int(f, "current_hp", max_hp)?)));
    }
    for k in ["shock_max", "shock_current", "pp_current", "mp_current"] {
        if has(k) {
            cols.push((k, Sql::Integer(form_int(f, k, 0)?.max(0))));
        }
    }
    for k in ["minor_edge", "major_edge"] {
        if has(k) {
            cols.push((k, s(&form_text(f, k, ""))));
        }
    }
    cols.push(("minor_edge_count", Sql::Integer(form_int(f, "minor_edge_count", col_int(cur, "minor_edge_count"))?.max(0))));
    cols.push(("major_edge_count", Sql::Integer(form_int(f, "major_edge_count", col_int(cur, "major_edge_count"))?.max(0))));
    let mut tpl = cur.get("sheet_template_id").and_then(|v| v.as_i64());
    if has("sheet_template_id") {
        tpl = match f.get("sheet_template_id").unwrap_or("") {
            "" => None,
            v if v.bytes().all(|b| b.is_ascii_digit()) && v.len() <= 15 => Some(v.parse().ok()?),
            v if v.is_ascii() => None,
            _ => return None, // str.isdigit() beyond ASCII: Python's
        };
        cols.push(("sheet_template_id", tpl.map_or(Sql::Null, Sql::Integer)));
    }
    for (k, empty) in [("custom_fields_json", "{}"), ("app_extra_json", "{}"), ("stats_json", "[]"), ("skills_json", "[]"), ("currency_json", "[]"),
                       ("equipment_json", "[]"), ("feats_json", "[]"), ("attacks_json", "[]"), ("cyberware_json", "[]"), ("conditions_json", "[]")] {
        if has(k) {
            let raw = f.get(k).filter(|v| !v.is_empty()).unwrap_or(empty);
            serde_json::from_str::<Value>(raw).ok()?; // what serde refuses, json.loads may take (NaN, ...): Python's
            cols.push((k, s(raw)));
        }
    }
    if !partial && !hp_given {
        // pc_maxima of the new, unsaved character: its template relationship is not loaded yet, so a custom sheet counts as native here
        let mut m = Map::new();
        for (k, v) in &cols {
            if let Sql::Text(x) = v {
                m.insert(k.to_string(), json!(x));
            } else if let Sql::Integer(x) = v {
                m.insert(k.to_string(), json!(x));
            }
        }
        let stats = stat_values(&m)?;
        let native = !stats.is_empty() || tpl.filter(|t| *t != 0).is_none();
        let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
        let phys = if native { get("str") + get("dex") + get("bod") + get("per") } else { 0 };
        let _ = db;
        cols.push(("current_hp", Sql::Integer(if max_hp > 0 { max_hp } else if native { phys + 10 } else { 0 })));
    }
    cols.push(("updated_at", s(&orm::now_stamp())));
    Some(cols)
}

fn pc_new(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "portrait")?;
    let world = c.world_id()?;
    let mut owner = Sql::Null;
    if !c.p.is_gm {
        let owned: i64 = c.db.query_row("SELECT COUNT(*) FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2", [world, c.p.user_id], |r| r.get(0)).ok()?;
        let limit = c.p.world.as_ref()?.get("max_characters_per_player").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(1).max(1);
        if owned >= limit {
            return None; // the limit's 400: Python's
        }
        owner = Sql::Integer(c.p.user_id);
    }
    let mut cols = apply_pc_form(&c.db, &f, &Map::new(), false)?;
    cols.push(("world_id", Sql::Integer(world)));
    cols.push(("owner_user_id", owner));
    if let Some(p) = &pick {
        if let Some(url) = save_picture(c, p, "portraits", true)? {
            cols.push(("portrait_url", s(&url)));
        }
    }
    let id = orm::insert(&c.db, "player_characters", &cols)?;
    redirect(&format!("/characters/{id}"))
}

fn pc_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "portrait")?;
    let pc = managed_pc(c, ids[0])?;
    let mut cols = apply_pc_form(&c.db, &f, &pc, true)?;
    if let Some(p) = &pick {
        if let Some(url) = save_picture(c, p, "portraits", true)? {
            cols.push(("portrait_url", s(&url)));
        }
    }
    orm::update(&c.db, "player_characters", ids[0], &cols)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    redirect(&format!("/characters/{}", ids[0]))
}

fn pc_owner(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    if !c.p.is_gm {
        return None;
    }
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?;
    let world = pc.get("world_id")?.as_i64()?;
    if Some(world) != c.world_id() {
        return None;
    }
    let raw = opt(&f, "owner_user_id", "");
    if !raw.is_ascii() {
        return None;
    }
    let want = strip(raw);
    let mut cols: Vec<(&str, Sql)> = vec![];
    if want.is_empty() {
        cols.push(("owner_user_id", Sql::Null));
    } else {
        let uid = py_int(&want).filter(|_| !want.starts_with('-'))?;
        let target: (i64, Option<String>, Option<String>) = c
            .db
            .query_row("SELECT id, display_name, email FROM users WHERE id = ?1 AND is_gm = 0", [uid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .ok()?;
        let member: i64 = c.db.query_row("SELECT COUNT(*) FROM world_memberships WHERE world_id = ?1 AND user_id = ?2", [world, uid], |r| r.get(0)).ok()?;
        let others: i64 = c
            .db
            .query_row("SELECT COUNT(*) FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 AND id != ?3", [world, uid, ids[0]], |r| r.get(0))
            .ok()?;
        let limit = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", world)?.get("max_characters_per_player").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(1).max(1);
        if member == 0 || others >= limit {
            return None; // the 400s: Python's
        }
        cols.push(("owner_user_id", Sql::Integer(target.0)));
        if pc.get("player_name").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
            // `target.display_name or target.email`
            let who = match target.1.filter(|v| !v.is_empty()) {
                Some(n) => Some(n),
                None => target.2,
            };
            cols.push(("player_name", who.map_or(Sql::Null, |w| s(&w))));
        }
    }
    orm::update(&c.db, "player_characters", ids[0], &cols)?;
    redirect(&format!("/characters/{}", ids[0]))
}

fn pc_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = managed_pc(c, ids[0])?;
    let world = pc.get("world_id")?.as_i64()?;
    drop_pc(c, ids[0], world)?;
    orm::touch(&c.db, world)?;
    redirect("/characters")
}

/// What character_delete and retire-to-NPC both do to the character: sheets unlinked, journal / prefs / log / shares / companions /
/// keepsakes dropped, `detach_pc` (party members, loot claims, calendar events), the row deleted
fn drop_pc(c: &Ctx, id: i64, world: i64) -> Option<()> {
    // detach_pc first, read-only, so anything it would rewrite that serde cannot (non-canonical JSON) sends the whole request to Python
    let mut party_writes: Vec<(i64, Vec<(&str, Sql)>)> = vec![];
    let mut stmt = c.db.prepare("SELECT id, member_pc_ids_json, loot_json FROM parties WHERE world_id = ?1").ok()?;
    let parties: Vec<(i64, Option<String>, Option<String>)> =
        stmt.query_map([world], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<_, _>>().ok()?;
    drop(stmt);
    for (pid, members, loot) in parties {
        let mut set: Vec<(&str, Sql)> = vec![];
        let members = members.filter(|m| !m.is_empty()).unwrap_or_else(|| "[]".into());
        let ids_in: Vec<i64> = match serde_json::from_str::<Value>(&members) {
            Ok(Value::Array(a)) => {
                if a.iter().any(|x| x.is_f64() || x.is_boolean()) {
                    return None; // isinstance(x, int) of bools / 1.0: Python's
                }
                a.iter().filter_map(|x| x.as_i64()).collect()
            }
            Ok(_) => vec![],
            Err(_) => return None,
        };
        if ids_in.contains(&id) {
            set.push(("member_pc_ids_json", s(&ints_json(&ids_in.into_iter().filter(|x| *x != id).collect::<Vec<_>>()))));
        }
        let loot = loot.filter(|l| !l.is_empty()).unwrap_or_else(|| "[]".into());
        if let Ok(mut lv) = serde_json::from_str::<Value>(&loot) {
            let mut changed = false;
            if let Value::Array(items) = &mut lv {
                for it in items.iter_mut() {
                    let Some(m) = it.as_object_mut() else { continue };
                    let claimed = match m.get("claimed_by") {
                        None | Some(Value::Null) => continue,
                        Some(Value::Array(a)) if !a.iter().any(|x| x.is_boolean()) => a.clone(),
                        _ => return None,
                    };
                    if claimed.iter().any(|x| x.as_i64() == Some(id) || x.as_f64() == Some(id as f64)) {
                        m.insert("claimed_by".into(), Value::Array(claimed.into_iter().filter(|x| x.as_f64() != Some(id as f64)).collect()));
                        changed = true;
                    }
                }
            }
            if changed {
                canonical_json(&loot)?;
                set.push(("loot_json", s(&nd_render::py_dumps(&lv))));
            }
        } else {
            return None; // bad JSON is no loot to Python, but serde also refuses some JSON json.loads takes: Python's
        }
        if !set.is_empty() {
            party_writes.push((pid, set));
        }
    }
    orm::bulk_update(&c.db, "character_sheets", &[("player_character_id", Sql::Null)], "player_character_id = ?", &[Sql::Integer(id)])?;
    for table in ["character_journal_entries", "character_prefs", "character_log", "character_shares", "character_companions", "character_keepsakes"] {
        c.db.execute(&format!("DELETE FROM {table} WHERE character_id = ?1"), [id]).ok()?;
    }
    for (pid, set) in party_writes {
        orm::update(&c.db, "parties", pid, &set)?;
    }
    orm::bulk_update(&c.db, "calendar_events", &[("character_id", Sql::Null)], "character_id = ?", &[Sql::Integer(id)])?;
    c.db.execute("DELETE FROM player_characters WHERE id = ?1", [id]).ok()?;
    Some(())
}

fn pc_level_up(c: &mut Ctx, _ids: &[i64]) -> Option<Response<Body>> {
    let id = _ids[0];
    let pc = managed_pc(c, id)?;
    let level = pc.get("level")?.as_i64()?;
    let (.., native) = pc_maxima(&c.db, &pc)?;
    // _levelup_ready (a level outside 1..19 or a level Python's list indexing reads its own way: Python's)
    if !(1..20).contains(&level) || !native || col_int(&pc, "xp") < XP_THRESHOLDS[level.min(19) as usize] {
        return None;
    }
    orm::update(&c.db, "player_characters", id, &[("level", Sql::Integer(level + 1))])?;
    log_num(c, &pc, "level", "Level", level, level + 1)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"level": level + 1, "name": pc.get("name").cloned().unwrap_or(Value::Null)}))
}

/// `str(item.get(k, ""))` for text (`str()` of anything else - "None", "5" - is Python's)
fn item_text(item: &Map<String, Value>, k: &str) -> Option<String> {
    match item.get(k) {
        None => Some(String::new()),
        Some(Value::String(v)) => Some(v.clone()),
        _ => None,
    }
}

/// A character's JSON list column (`json.loads(col or "[]")`), when it is canonical `json.dumps` output (or empty)
fn pc_list(pc: &Map<String, Value>, k: &str) -> Option<Vec<Value>> {
    let raw = pc.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match canonical_json(raw)? {
        Value::Array(a) => Some(a),
        _ => None,
    }
}

/// A JSON list index Python's `isinstance(index, int)` accepts (bools are ints there: Python's)
fn list_index(b: &Map<String, Value>, len: usize) -> Option<usize> {
    let i = b.get("index")?.as_i64()?;
    (0..len as i64).contains(&i).then_some(i as usize)
}

fn pc_feats(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let mut feats = pc_list(&pc, "feats_json")?;
    match b.get("action").cloned().unwrap_or(json!("add")).as_str() {
        Some("add") => {
            let item = match b.get("item") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(o)) => o.clone(),
                _ => return None,
            };
            let name = strip(&item_text(&item, "name")?);
            if name.is_empty() {
                return None;
            }
            let mut e = Map::new();
            e.insert("name".into(), json!(name));
            for k in ["type", "rank", "notes"] {
                e.insert(k.into(), json!(item_text(&item, k)?));
            }
            if let Some(v) = item.get("id").filter(|v| !v.is_null()) {
                e.insert("id".into(), v.clone());
            }
            feats.push(Value::Object(e));
        }
        Some("remove") => {
            let i = list_index(&b, feats.len())?;
            feats.remove(i);
        }
        _ => return None,
    }
    let list = Value::Array(feats);
    orm::update(&c.db, "player_characters", ids[0], &[("feats_json", s(&nd_render::py_dumps(&list)))])?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"feats": list}))
}

/// A Python number that remembers whether it is an int or a float (they print differently)
#[derive(Clone, Copy)]
enum PyNum {
    I(i64),
    F(f64),
}

impl PyNum {
    fn f(self) -> f64 {
        match self {
            PyNum::I(i) => i as f64,
            PyNum::F(f) => f,
        }
    }
    fn json(self) -> Value {
        match self {
            PyNum::I(i) => json!(i),
            PyNum::F(f) => json!(f),
        }
    }
    fn mul(self, o: PyNum) -> PyNum {
        match (self, o) {
            (PyNum::I(a), PyNum::I(b)) => PyNum::I(a * b),
            _ => PyNum::F(self.f() * o.f()),
        }
    }
    fn add(self, o: PyNum) -> PyNum {
        match (self, o) {
            (PyNum::I(a), PyNum::I(b)) => PyNum::I(a + b),
            _ => PyNum::F(self.f() + o.f()),
        }
    }
}

/// `_num(v, default)` = float(v), or the default for None / a missing key; None for what float() reads its own way (text, bools)
fn py_num(v: Option<&Value>, default: PyNum) -> Option<PyNum> {
    match v {
        None | Some(Value::Null) => Some(default),
        Some(Value::Number(n)) => Some(PyNum::F(n.as_f64()?)),
        _ => None,
    }
}

/// `char_extras.carry_info(db, pc)` for the equipment list `equipment`
fn carry_info(db: &Connection, pc: &Map<String, Value>, equipment: &[Value]) -> Option<Value> {
    // used += float(weight or 0) * float(1 if qty is None else qty); an item float() refuses is skipped
    let mut used = 0.0f64;
    for it in equipment {
        let Some(m) = it.as_object() else { continue };
        let w = if py_truthy(m.get("weight")) { py_float(m.get("weight")).ok()? } else { Some(0.0) };
        let q = match m.get("qty") {
            None | Some(Value::Null) => Some(1.0),
            v => py_float(v).ok()?,
        };
        if let (Some(w), Some(q)) = (w, q) {
            used += w * q;
        }
    }
    // {str(id): float(value or 0)}: one value float() refuses empties the whole table
    let raw = pc.get("stats_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let mut stats: Vec<(String, f64)> = vec![];
    match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => {
            for x in a {
                let Some(o) = x.as_object() else { continue };
                let v = if py_truthy(o.get("value")) { py_float(o.get("value")).ok()? } else { Some(0.0) };
                let Some(v) = v else {
                    stats.clear();
                    break;
                };
                let Some(id) = o.get("id").and_then(|v| v.as_str()).map(str::to_string) else { continue }; // str() of another id is never "str" / "bod"
                stats.retain(|(k, _)| *k != id);
                stats.push((id, v));
            }
        }
        _ => return None,
    }
    let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0.0, |(_, v)| *v);
    if !used.is_finite() || !(5.0 * (get("str") + get("bod"))).is_finite() {
        return None;
    }
    let auto = (5.0 * (get("str") + get("bod"))).trunc() as i64;
    let custom = pc_prefs(db, pc.get("id")?.as_i64()?)?.get("carry_limit").cloned();
    let custom = match custom {
        Some(Value::Bool(_)) => return None,
        Some(Value::Number(n)) if n.as_f64()? > 0.0 => Some(n.as_f64()?.trunc() as i64),
        _ => None,
    };
    let limit = custom.unwrap_or(auto);
    let rounded = py_round(used, 1)?;
    Some(json!({"used": rounded, "limit": limit, "default": custom.is_none(), "over": limit > 0 && used > limit as f64}))
}

/// `char_extras.prefs(db, pc_id)` (a stored value serde would read differently from json.loads: Python's)
fn pc_prefs(db: &Connection, pc_id: i64) -> Option<Map<String, Value>> {
    let raw: Option<Option<String>> = db.query_row("SELECT prefs_json FROM character_prefs WHERE character_id = ?1 LIMIT 1", [pc_id], |r| r.get(0)).ok();
    let raw = raw.flatten().filter(|s| !s.is_empty()).unwrap_or_else(|| "{}".into());
    match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Object(o)) => Some(o),
        Ok(_) => Some(Map::new()),
        Err(_) => None,
    }
}

fn pc_equipment(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let before = pc.get("equipment_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]").to_string();
    let mut eq = pc_list(&pc, "equipment_json")?;
    let action = b.get("action").cloned().unwrap_or(json!("add"));
    let mut added_name = None;
    match action.as_str() {
        Some("add") => {
            let item = match b.get("item") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(o)) if o.is_empty() => Map::new(),
                Some(Value::Object(o)) => o.clone(),
                _ => return None,
            };
            let raw_name = item_text(&item, "name")?;
            let name = strip(&raw_name);
            if name.is_empty() {
                return None;
            }
            let qty = py_num(item.get("qty"), PyNum::I(1))?;
            let qty = match qty {
                PyNum::I(i) => i,
                PyNum::F(f) if f.is_finite() && f.abs() < 1e15 => f.trunc() as i64,
                _ => return None,
            };
            let mut e = Map::new();
            e.insert("name".into(), json!(name));
            e.insert("qty".into(), json!(qty));
            e.insert("weight".into(), py_num(item.get("weight"), PyNum::I(0))?.json());
            e.insert("equipped".into(), json!(py_truthy(item.get("equipped"))));
            e.insert("notes".into(), json!(item_text(&item, "notes")?));
            for k in ["id", "category", "cost"] {
                if let Some(v) = item.get(k).filter(|v| !v.is_null()) {
                    e.insert(k.into(), v.clone());
                }
            }
            eq.push(Value::Object(e));
            added_name = Some(raw_name);
        }
        Some("remove") => {
            let i = list_index(&b, eq.len())?;
            eq.remove(i);
        }
        Some(a @ ("qty" | "weight")) => {
            let i = list_index(&b, eq.len())?;
            let it = eq.get_mut(i)?.as_object_mut()?;
            if a == "qty" {
                let q = match py_num(it.get("qty"), PyNum::I(1))? {
                    PyNum::I(v) => v,
                    PyNum::F(f) if f.is_finite() && f.abs() < 1e15 => f.trunc() as i64,
                    _ => return None,
                };
                let delta = body_int(&b, "delta")?;
                it.insert("qty".into(), json!((q + delta).min(9999).max(0)));
            } else {
                // max(0, min(9999, float)): the int bound itself when it wins or ties (min / max return their first argument then)
                let v = match py_num(b.get("value"), PyNum::I(0))? {
                    PyNum::I(_) => PyNum::I(0),
                    PyNum::F(f) => {
                        let m = if f >= 9999.0 { PyNum::I(9999) } else { PyNum::F(f) };
                        if m.f() <= 0.0 && !(m.f() > 0.0) { PyNum::I(0) } else { m }
                    }
                };
                it.insert("weight".into(), v.json());
            }
        }
        _ => return None,
    }
    let list = Value::Array(eq.clone());
    let after = nd_render::py_dumps(&list);
    orm::update(&c.db, "player_characters", ids[0], &[("equipment_json", s(&after))])?;
    if after != before {
        let what = match action.as_str() {
            Some("add") => format!("added {}", added_name.unwrap_or_default()),
            Some("remove") => "removed an item".into(),
            _ => "changed a quantity / weight".into(),
        };
        let undo = if before.chars().count() <= 20000 { json!({"op": "equipment", "value": before}) } else { json!({}) };
        char_log(c, &pc, "equipment", &format!("Equipment: {what}"), &undo)?;
    }
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let mut total = PyNum::I(0);
    for it in &eq {
        if let Some(m) = it.as_object() {
            total = total.add(py_num(m.get("weight"), PyNum::I(0))?.mul(py_num(m.get("qty"), PyNum::I(1))?));
        }
    }
    let carry = carry_info(&c.db, &pc, &eq)?;
    json_response(&json!({"equipment": list, "total_weight": total.json(), "carry": carry}))
}

fn pc_carry_limit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let val = body_int(&b, "value")?.clamp(0, 99999);
    let existing: Option<i64> = c.db.query_row("SELECT id FROM character_prefs WHERE character_id = ?1 LIMIT 1", [ids[0]], |r| r.get(0)).ok();
    let mut data = if existing.is_some() { pc_prefs(&c.db, ids[0])? } else { Map::new() };
    if val == 0 {
        data.remove("carry_limit");
    } else {
        data.insert("carry_limit".into(), json!(val));
    }
    let text = nd_render::py_dumps(&Value::Object(data));
    match existing {
        Some(pid) => {
            orm::update(&c.db, "character_prefs", pid, &[("prefs_json", s(&text))])?;
        }
        None => {
            orm::insert(&c.db, "character_prefs", &[("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)), ("character_id", Sql::Integer(ids[0])),
                                                    ("prefs_json", s(&text))])?;
        }
    }
    let eq = pc_list(&pc, "equipment_json")?;
    json_response(&json!({"carry": carry_info(&c.db, &pc, &eq)?}))
}

/// Python's `bool(v)` for a JSON value
pub fn py_truthy_pub(v: Option<&Value>) -> bool {
    py_truthy(v)
}

fn py_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `_pc_to_npc_markdown(pc)`
fn pc_npc_markdown(db: &Connection, pc: &Map<String, Value>) -> Option<String> {
    let txt = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mut parts: Vec<String> = vec![];
    let level = pc.get("level").and_then(|v| v.as_i64()).unwrap_or(0);
    let lvl = if level != 0 && pc_maxima(db, pc)?.4 { format!("Level {level}") } else { String::new() };
    let class_line = [txt("char_class"), txt("race"), lvl].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" \u{b7} ");
    if !class_line.is_empty() {
        parts.push(format!("*{class_line}*"));
    }
    let tagline = [txt("background"), txt("alignment")].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" \u{b7} ");
    if !tagline.is_empty() {
        parts.push(tagline);
    }
    let appearance = [("Age", "age"), ("Height", "height"), ("Weight", "weight_app"), ("Eyes", "eyes"), ("Skin", "skin"), ("Hair", "hair")]
        .iter()
        .filter_map(|(label, k)| {
            let v = match pc.get(*k) {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                Some(Value::Number(n)) if n.as_f64() != Some(0.0) => n.to_string(),
                _ => return None,
            };
            Some(format!("{label}: {v}"))
        })
        .collect::<Vec<_>>()
        .join(", ");
    for (heading, text) in [("Appearance", appearance), ("Personality", txt("personality_traits")), ("Ideals", txt("ideals")), ("Bonds", txt("bonds")),
                            ("Flaws", txt("flaws")), ("Backstory", txt("backstory")), ("Notes", txt("notes"))] {
        if !text.is_empty() {
            parts.extend([String::new(), format!("## {heading}"), String::new(), text]);
        }
    }
    Some(strip(&parts.join("\n")))
}

fn pc_retire(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?;
    let world = pc.get("world_id")?.as_i64()?;
    if Some(world) != c.world_id() {
        return None;
    }
    let player = pc.get("player_name").and_then(|v| v.as_str()).unwrap_or("");
    let portrait = pc.get("portrait_url").and_then(|v| v.as_str()).unwrap_or("");
    let eid = orm::insert(
        &c.db,
        "entities",
        &[
            ("world_id", Sql::Integer(world)),
            ("kind", s("character")),
            ("name", pc.get("name").and_then(|v| v.as_str()).map_or(Sql::Null, s)),
            ("summary", if player.is_empty() { Sql::Null } else { s(&format!("Formerly played by {player}.")) }),
            ("body", s(&pc_npc_markdown(&c.db, &pc)?)),
            ("image_url", if portrait.is_empty() { Sql::Null } else { s(portrait) }),
            ("visible_to_players", Sql::Integer(1)),
        ],
    )?;
    drop_pc(c, ids[0], world)?;
    orm::touch(&c.db, world)?;
    redirect(&format!("/entity/{eid}"))
}

fn pc_portrait_from_url(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let job_id = b.get("job_id")?.as_i64()?;
    let url = strip(b.get("url")?.as_str()?);
    let pc = managed_pc(c, ids[0])?;
    let job = row(&c.db, "SELECT * FROM image_jobs WHERE id = ?1", job_id)?;
    if job.get("world_id") != pc.get("world_id") || !(c.p.is_gm || job.get("created_by_user_id").and_then(|v| v.as_i64()) == Some(c.p.user_id)) {
        return None;
    }
    let raw = job.get("result_urls_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let Ok(Value::Array(urls)) = serde_json::from_str::<Value>(raw) else { return None };
    if !urls.iter().any(|u| u.as_str() == Some(url.as_str())) {
        return None;
    }
    orm::update(&c.db, "player_characters", ids[0], &[("portrait_url", s(&url))])?;
    json_response(&json!({"ok": true}))
}

/// `_apply_sync_json(pc, data)`: the columns to write (`cur` = the stored row, empty for a new character)
fn apply_pc_sync(b: &Map<String, Value>, cur: &Map<String, Value>) -> Option<Vec<(&'static str, Sql)>> {
    // gi: int(data.get(k, d)) for a JSON int (floats, text and bools are int()'s: Python's); None / missing = the default
    let gi = |k: &str, d: i64| -> Option<i64> {
        match b.get(k) {
            None | Some(Value::Null) => Some(d),
            Some(Value::Number(n)) => n.as_i64(),
            _ => None,
        }
    };
    let gs = |k: &str, d: &str| -> Option<String> {
        match b.get(k) {
            None | Some(Value::Null) => Some(strip(d)),
            Some(Value::String(v)) => Some(strip(v)),
            _ => None,
        }
    };
    let text_col = |k: &str| cur.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let int_col = |k: &str| cur.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0);
    let mut cols: Vec<(&'static str, Sql)> = vec![];
    for k in ["name", "player_name", "race", "char_class", "backstory", "notes"] {
        let v = gs(k, "")?;
        cols.push((k, s(if k == "name" && v.is_empty() { "Unnamed" } else { &v })));
    }
    cols.push(("race_id", s(&gs("race_id", &text_col("race_id"))?)));
    cols.push(("profession_id", s(&gs("profession_id", &text_col("profession_id"))?)));
    // the scalar loop has just set pc.level / pc.xp to str() text ("" when absent), so `pc.level or 1` / `pc.xp or 0` are 1 / 0 here
    cols.push(("level", Sql::Integer(gi("level", 1)?.clamp(1, 20))));
    cols.push(("xp", Sql::Integer(gi("xp", 0)?.max(0))));
    let max_hp = gi("max_hp", 0)?.max(0);
    cols.push(("max_hp", Sql::Integer(max_hp)));
    cols.push(("current_hp", Sql::Integer(gi("current_hp", max_hp)?)));
    for k in ["shock_max", "shock_current", "pp_current", "mp_current"] {
        cols.push((k, Sql::Integer(gi(k, 0)?.max(0))));
    }
    cols.push(("minor_edge", s(&gs("minor_edge", "")?)));
    cols.push(("major_edge", s(&gs("major_edge", "")?)));
    cols.push(("minor_edge_count", Sql::Integer(gi("minor_edge_count", int_col("minor_edge_count").unwrap_or(0))?.max(0))));
    cols.push(("major_edge_count", Sql::Integer(gi("major_edge_count", int_col("major_edge_count").unwrap_or(0))?.max(0))));
    cols.push((
        "sheet_template_id",
        match b.get("sheet_template_id") {
            None | Some(Value::Null) | Some(Value::Bool(false)) => Sql::Null,
            Some(Value::Number(n)) if n.as_i64() == Some(0) => Sql::Null,
            Some(Value::Number(n)) => Sql::Integer(n.as_i64()?),
            Some(Value::String(v)) if v.is_empty() => Sql::Null,
            _ => return None,
        },
    ));
    for k in ["custom_fields_json", "app_extra_json"] {
        let v = b.get(k).filter(|v| v.is_object()).cloned().unwrap_or(json!({}));
        cols.push((k, s(&nd_render::py_dumps(&v))));
    }
    for k in ["stats_json", "skills_json", "currency_json", "equipment_json", "feats_json", "attacks_json", "cyberware_json", "conditions_json"] {
        let v = b.get(k).filter(|v| v.is_array()).cloned().unwrap_or(json!([]));
        cols.push((k, s(&nd_render::py_dumps(&v))));
    }
    cols.push(("updated_at", s(&orm::now_stamp())));
    Some(cols)
}

/// `_pc_to_sync_dict(pc)` of the stored row
fn pc_sync_dict(pc: &Map<String, Value>) -> Option<Value> {
    let txt = |k: &str| json!(pc.get(k).and_then(|v| v.as_str()).unwrap_or(""));
    let num = |k: &str| json!(pc.get(k).and_then(|v| v.as_i64()).unwrap_or(0));
    // datetime.isoformat(): "T" between date and time, no fraction when the microseconds are 0
    let updated = pc.get("updated_at").and_then(|v| v.as_str()).map(|u| {
        let iso = u.replacen(' ', "T", 1);
        iso.strip_suffix(".000000").map(str::to_string).unwrap_or(iso)
    });
    let mut d = Map::new();
    d.insert("id".into(), pc.get("id")?.clone());
    d.insert("world_id".into(), pc.get("world_id")?.clone());
    d.insert("updated_at".into(), updated.map_or(Value::Null, Value::String));
    d.insert("portrait_url".into(), txt("portrait_url"));
    d.insert("sheet_template_id".into(), pc.get("sheet_template_id").cloned().unwrap_or(Value::Null));
    for k in ["name", "player_name", "race", "char_class", "backstory", "notes"] {
        d.insert(k.into(), txt(k));
    }
    d.insert("level".into(), json!(pc.get("level").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(1)));
    d.insert("xp".into(), num("xp"));
    d.insert("race_id".into(), txt("race_id"));
    d.insert("profession_id".into(), txt("profession_id"));
    for k in ["max_hp", "current_hp", "shock_max", "shock_current", "pp_current", "mp_current", "minor_edge_count", "major_edge_count"] {
        d.insert(k.into(), num(k));
    }
    d.insert("minor_edge".into(), txt("minor_edge"));
    d.insert("major_edge".into(), txt("major_edge"));
    for (k, empty) in [("custom_fields_json", "{}"), ("app_extra_json", "{}"), ("stats_json", "[]"), ("skills_json", "[]"), ("currency_json", "[]"),
                       ("equipment_json", "[]"), ("feats_json", "[]"), ("attacks_json", "[]"), ("cyberware_json", "[]"), ("conditions_json", "[]")] {
        let raw = pc.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(empty);
        d.insert(k.into(), serde_json::from_str(raw).ok()?);
    }
    Some(Value::Object(d))
}

/// `PUT /api/characters/<id>/sync`
fn pc_sync_put(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let cols = apply_pc_sync(&b, &pc)?;
    orm::update(&c.db, "player_characters", ids[0], &cols)?;
    json_response(&pc_sync_dict(&row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?)?)
}

fn pc_sync_create(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    if Some(ids[0]) != c.world_id() {
        return None; // another world's access check: Python's
    }
    if !c.p.is_gm {
        let owned: i64 = c.db.query_row("SELECT COUNT(*) FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2", [ids[0], c.p.user_id], |r| r.get(0)).ok()?;
        let limit = c.p.world.as_ref()?.get("max_characters_per_player").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(1).max(1);
        if owned >= limit {
            return None;
        }
    }
    let mut cols = apply_pc_sync(&b, &Map::new())?;
    cols.push(("world_id", Sql::Integer(ids[0])));
    cols.push(("owner_user_id", Sql::Integer(c.p.user_id)));
    let id = orm::insert(&c.db, "player_characters", &cols)?;
    json_response(&pc_sync_dict(&row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id)?)?)
}

// ── sessions, continued ──────────────────────────────────────────────────────

fn session_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = editable_session(c, ids[0])?;
    // pictures uploaded for the session go with it (delete_own_pictures: files, thumbnails, display copies): Python's
    let media = gs.get("recap_media_json").and_then(|v| v.as_str()).unwrap_or("");
    if !media.is_empty() {
        match serde_json::from_str::<Value>(media) {
            Ok(Value::Array(items)) => {
                if items.iter().any(|it| it.get("kind").and_then(|k| k.as_str()) == Some("image") && py_truthy(it.get("own"))) {
                    return None;
                }
            }
            Ok(_) => {}
            Err(_) => return None,
        }
    }
    let id = ids[0];
    orm::bulk_update(&c.db, "combat_sessions", &[("game_session_id", Sql::Null)], "game_session_id = ?", &[Sql::Integer(id)])?;
    orm::bulk_update(&c.db, "facts", &[("game_session_id", Sql::Null)], "game_session_id = ?", &[Sql::Integer(id)])?;
    c.db.execute("DELETE FROM game_sessions WHERE id = ?1", [id]).ok()?;
    // the raw-recording archive, <DB_PATH dir>/uploads/live/<id> (shutil.rmtree(..., ignore_errors=True))
    let live = uploads_root(c)?.join("live").join(id.to_string());
    c.removals.push(live);
    redirect("/sessions")
}

fn session_recap_publish(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    editable_session(c, ids[0])?;
    let text = f.get("player_summary").unwrap_or("").to_string();
    let published = !opt(&f, "publish", "").is_empty() && !strip(&text).is_empty();
    orm::update(&c.db, "game_sessions", ids[0], &[("player_summary", s(&text)), ("player_summary_published", Sql::Integer(published as i64))])?;
    redirect(&format!("/sessions/{}", ids[0]))
}

fn session_live_clear(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    editable_session(c, ids[0])?;
    orm::update(&c.db, "game_sessions", ids[0], &[("live_transcript", s("")), ("live_transcript_segments_json", s(""))])?;
    json_response(&json!({"transcript": ""}))
}

/// A stored JSON list column as `json.loads(col or "[]")` reads it (canonical text only, else Python's)
fn stored_list(m: &Map<String, Value>, k: &str) -> Option<Vec<Value>> {
    let raw = m.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    match canonical_json(raw)? {
        Value::Array(a) => Some(a),
        _ => None,
    }
}

fn session_loot(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let gs = editable_session(c, ids[0])?;
    let mut loot = stored_list(&gs, "loot_json")?;
    let action = b.get("action");
    if action.and_then(|v| v.as_str()) == Some("add") {
        let qty = match b.get("qty") {
            None => 1,
            Some(v) if !py_truthy(Some(v)) => 1,
            Some(Value::Number(n)) => n.as_i64()?,
            _ => return None, // int() of text / floats: Python's
        };
        let mut item = Map::new();
        item.insert("name".into(), b.get("name").cloned().unwrap_or(json!("Item")));
        item.insert("qty".into(), json!(qty));
        item.insert("notes".into(), b.get("notes").cloned().unwrap_or(json!("")));
        loot.push(Value::Object(item));
        let list = Value::Array(loot);
        orm::update(&c.db, "game_sessions", ids[0], &[("loot_json", s(&nd_render::py_dumps(&list)))])?;
        return json_response(&json!({"loot": list}));
    }
    let idx = match b.get("index") {
        None => -1,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    if !(0..loot.len() as i64).contains(&idx) {
        return None;
    }
    let item = loot.remove(idx as usize);
    let list = Value::Array(loot);
    orm::update(&c.db, "game_sessions", ids[0], &[("loot_json", s(&nd_render::py_dumps(&list)))])?;
    if action.and_then(|v| v.as_str()) == Some("transfer") {
        if let Some(pid) = gs.get("party_id").and_then(|v| v.as_i64()) {
            if let Some(party) = row(&c.db, "SELECT * FROM parties WHERE id = ?1", pid) {
                let mut pl = stored_list(&party, "loot_json")?;
                pl.push(item);
                orm::update(&c.db, "parties", pid, &[("loot_json", s(&nd_render::py_dumps(&Value::Array(pl))))])?;
            }
        }
    }
    json_response(&json!({"loot": list}))
}

fn session_xp(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let gs = editable_session(c, ids[0])?;
    let delta = match b.get("delta") {
        None => 0,
        Some(Value::Number(n)) => n.as_i64().filter(|v| v.abs() < 1 << 40)?,
        _ => return None,
    };
    let party = gs.get("party_id").and_then(|v| v.as_i64()).and_then(|pid| row(&c.db, "SELECT * FROM parties WHERE id = ?1", pid));
    let given = b.get("pc_ids").cloned().unwrap_or(Value::Null);
    let pc_ids: Vec<i64> = if py_truthy(Some(&given)) {
        given.as_array()?.iter().map(|v| v.as_i64()).collect::<Option<_>>()?
    } else if let Some(p) = &party {
        stored_list(p, "member_pc_ids_json")?.iter().map(|v| v.as_i64()).collect::<Option<_>>()?
    } else {
        vec![]
    };
    let mut pcs: Vec<Map<String, Value>> = vec![];
    let mut seen: Vec<i64> = vec![];
    let mut sorted = pc_ids.clone();
    sorted.sort();
    for id in sorted {
        if seen.contains(&id) {
            continue;
        }
        seen.push(id);
        if let Some(pc) = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id) {
            pcs.push(pc);
        }
    }
    let (mut updated, mut awarded) = (vec![], Map::new());
    for pc in &pcs {
        let xp = pc.get("xp")?.as_i64()?; // a NULL xp is Python's TypeError
        let tpl = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0);
        if tpl.is_some() && !pc_maxima(&c.db, pc)?.4 {
            return None; // a custom system's own XP fields (apply_xp_award): Python's
        }
        orm::update(&c.db, "player_characters", pc.get("id")?.as_i64()?, &[("xp", Sql::Integer((xp + delta).max(0)))])?;
        let name = pc.get("name").cloned().unwrap_or(Value::Null);
        updated.push(name.clone());
        match name {
            Value::String(n) => {
                awarded.insert(n, json!(delta));
            }
            _ => return None, // a None name as a dict key: Python's
        }
    }
    let total = col_int(&gs, "xp_awarded") + delta;
    orm::update(&c.db, "game_sessions", ids[0], &[("xp_awarded", Sql::Integer(total))])?;
    if let (Some(p), false) = (&party, awarded.is_empty()) {
        let mut ledger = stored_list(p, "xp_json")?;
        let now = orm::now_stamp();
        ledger.push(json!({"ts": now[..19].replacen(' ', "T", 1), "amount": delta, "session_id": ids[0], "awarded": Value::Object(awarded)}));
        let keep = ledger.split_off(ledger.len().saturating_sub(100));
        orm::update(&c.db, "parties", p.get("id")?.as_i64()?, &[("xp_json", s(&nd_render::py_dumps(&Value::Array(keep))))])?;
    }
    orm::touch(&c.db, gs.get("world_id")?.as_i64()?)?;
    json_response(&json!({"updated": updated, "xp_awarded": total}))
}

// ── calendar settings (app/routers/calendar.py calendar_config_save, app/calendar_config.py) ──

/// Why a Python cleaner would stop: ValueError, TypeError, or a value this port does not read the way Python would (that request is Python's)
#[derive(Debug, Clone, Copy, PartialEq)]
enum PyErr {
    Value,
    Type,
    Unsure,
}

/// Python's `int(text)` for ASCII text (whitespace, a sign, digits with single underscores); Err(Value) for what int() refuses
fn py_int_text(s: &str) -> Result<i64, PyErr> {
    if !s.is_ascii() {
        return Err(PyErr::Unsure);
    }
    let t = s.trim_matches(|c: char| c.is_ascii_whitespace() || c == '\x0b' || ('\x1c'..='\x1f').contains(&c));
    let body = t.strip_prefix(['+', '-']).unwrap_or(t);
    let ok = !body.is_empty() && !body.starts_with('_') && !body.ends_with('_') && !body.contains("__") && body.bytes().all(|b| b.is_ascii_digit() || b == b'_');
    if !ok {
        return Err(PyErr::Value);
    }
    let digits: String = body.chars().filter(|c| *c != '_').collect();
    if digits.len() > 17 {
        return Err(PyErr::Unsure);
    }
    let n: i64 = digits.parse().map_err(|_| PyErr::Unsure)?;
    Ok(if t.starts_with('-') { -n } else { n })
}

/// Python's `int(v)` for a JSON value
fn py_int_value(v: &Value) -> Result<i64, PyErr> {
    match v {
        Value::Bool(b) => Ok(*b as i64),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Ok(i),
            None => {
                let f = n.as_f64().ok_or(PyErr::Unsure)?;
                if f.is_finite() && f.abs() < 1e17 { Ok(f.trunc() as i64) } else { Err(PyErr::Unsure) }
            }
        },
        Value::String(s) => py_int_text(s),
        _ => Err(PyErr::Type),
    }
}

/// `int(m.get(k) or default)`
fn py_int_or(v: Option<&Value>, default: i64) -> Result<i64, PyErr> {
    match v {
        Some(x) if py_truthy(Some(x)) => py_int_value(x),
        _ => Ok(default),
    }
}

/// `str(m.get(k) or default)` for text (str() of other truthy values: Python's)
fn py_str_or(v: Option<&Value>, default: &str) -> Result<String, PyErr> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Ok(s.clone()),
        Some(x) if py_truthy(Some(x)) => Err(PyErr::Unsure),
        _ => Ok(default.to_string()),
    }
}

/// `clean_color`
fn clean_color(v: Option<&Value>) -> Result<String, PyErr> {
    let text = strip(&py_str_or(v, "")?);
    let b = text.as_bytes();
    let hex = (b.len() == 4 || b.len() == 7) && b[0] == b'#' && b[1..].iter().all(|c| c.is_ascii_hexdigit());
    Ok(if hex { text } else { String::new() })
}

fn clean_months(raw: &Value) -> Result<Vec<Value>, PyErr> {
    let list = raw.as_array().ok_or(PyErr::Value)?;
    let mut out = vec![];
    for (i, m) in list.iter().take(60).enumerate() {
        let m = m.as_object().ok_or(PyErr::Value)?;
        let name = take_chars(&strip(&py_str_or(m.get("name"), "")?), 40);
        let mut e = Map::new();
        e.insert("name".into(), json!(if name.is_empty() { format!("Month {}", i + 1) } else { name }));
        e.insert("days".into(), json!(py_int_or(m.get("days"), 1)?.clamp(1, 1000)));
        let season = take_chars(&strip(&py_str_or(m.get("season"), "")?), 40);
        if !season.is_empty() {
            e.insert("season".into(), json!(season));
        }
        let color = clean_color(m.get("color"))?;
        if !color.is_empty() {
            e.insert("color".into(), json!(color));
        }
        out.push(Value::Object(e));
    }
    Ok(out)
}

fn clean_moons(raw: &Value) -> Result<Vec<Value>, PyErr> {
    let list = raw.as_array().ok_or(PyErr::Value)?;
    let mut out = vec![];
    for m in list.iter().take(8) {
        let m = m.as_object().ok_or(PyErr::Type)?; // m.get on a non-dict: AttributeError (the form path skips the moons)
        let name = take_chars(&strip(&py_str_or(m.get("name"), "Moon")?), 64);
        out.push(json!({
            "name": if name.is_empty() { "Moon".to_string() } else { name },
            "cycle_days": py_int_or(m.get("cycle_days"), 29)?.max(1),
            "offset": py_int_or(m.get("offset"), 0)?,
            "color": take_chars(&py_str_or(m.get("color"), "#cccccc")?, 16),
        }));
    }
    Ok(out)
}

/// `month_index(value, months)` against the month names
fn month_index(v: Option<&Value>, names: &[String]) -> Result<Option<usize>, PyErr> {
    let n = names.len() as i64;
    match v {
        Some(Value::Bool(_)) => Ok(None),
        Some(Value::Number(x)) if x.is_i64() => {
            let i = x.as_i64().unwrap_or(-1);
            Ok((0..n).contains(&i).then_some(i as usize))
        }
        Some(Value::Number(x)) if x.as_f64() == Some(0.0) => Ok(None),
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => {
            let text = strip(s);
            if text.is_empty() {
                return Ok(None);
            }
            if !text.is_ascii() {
                return Err(PyErr::Unsure);
            }
            if text.bytes().all(|b| b.is_ascii_digit()) {
                let i = py_int_text(&text)?;
                return Ok((0..n).contains(&i).then_some(i as usize));
            }
            for (i, name) in names.iter().enumerate() {
                if !name.is_ascii() {
                    return Err(PyErr::Unsure);
                }
                if strip(name).eq_ignore_ascii_case(&text) {
                    return Ok(Some(i));
                }
            }
            Ok(None)
        }
        _ => Err(PyErr::Unsure),
    }
}

fn clean_holidays(raw: &Value, months: &[(String, i64)]) -> Result<Vec<Value>, PyErr> {
    let list = raw.as_array().ok_or(PyErr::Value)?;
    let names: Vec<String> = months.iter().map(|m| m.0.clone()).collect();
    let mut out: Vec<(usize, i64, Value)> = vec![];
    for h in list.iter().take(100) {
        let Some(h) = h.as_object() else { continue };
        let name = take_chars(&strip(&py_str_or(h.get("name"), "")?), 60);
        let idx = month_index(h.get("month"), &names)?;
        let (false, Some(idx)) = (name.is_empty(), idx) else { continue };
        let day = py_int_or(h.get("day"), 1)?.min(months[idx].1).max(1);
        let mut e = Map::new();
        e.insert("name".into(), json!(name));
        e.insert("month".into(), json!(idx));
        e.insert("day".into(), json!(day));
        let notes = take_chars(&strip(&py_str_or(h.get("notes"), "")?), 200);
        if !notes.is_empty() {
            e.insert("notes".into(), json!(notes));
        }
        let color = clean_color(h.get("color"))?;
        if !color.is_empty() {
            e.insert("color".into(), json!(color));
        }
        out.push((idx, day, Value::Object(e)));
    }
    out.sort_by_key(|(m, d, _)| (*m, *d));
    Ok(out.into_iter().map(|(_, _, v)| v).collect())
}

/// `clean_weekday_names(raw, days_per_week)`
fn clean_weekday_names(raw: &Value, dpw: i64) -> Result<Vec<String>, PyErr> {
    let parts: Vec<String> = match raw {
        Value::String(s) => s.split([',', ';', '\n']).map(str::to_string).collect(),
        Value::Array(a) => a.iter().map(|x| x.as_str().map(str::to_string).ok_or(PyErr::Unsure)).collect::<Result<_, _>>()?,
        _ => return Err(PyErr::Value),
    };
    let mut names: Vec<String> = parts.iter().map(|p| take_chars(&strip(p), 24)).collect();
    if names.iter().all(|n| n.is_empty()) {
        return Ok(vec![]);
    }
    names.truncate(dpw as usize);
    while (names.len() as i64) < dpw {
        names.push(String::new());
    }
    Ok(names.into_iter().enumerate().map(|(i, n)| if n.is_empty() { format!("Day {}", i + 1) } else { n }).collect())
}

/// `clean_days_per_week(value)`
fn clean_days_per_week(v: Option<&Value>) -> Result<i64, PyErr> {
    match v.map(py_int_value) {
        None | Some(Err(PyErr::Value)) | Some(Err(PyErr::Type)) => Ok(7),
        Some(Err(e)) => Err(e),
        Some(Ok(n)) => Ok(n.clamp(1, 60)),
    }
}

/// The months of a config as `_months_of` gives them: (name, days); None for months this port cannot read
fn config_months(config: &Map<String, Value>) -> Option<Vec<(String, i64)>> {
    match config.get("months") {
        Some(Value::Array(a)) if !a.is_empty() => a
            .iter()
            .map(|m| Some((m.get("name")?.as_str()?.to_string(), m.get("days")?.as_i64()?)))
            .collect(),
        Some(v) if py_truthy(Some(v)) => None,
        _ => Some(
            ["Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall", "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter",
             "Yearsend"]
                .iter()
                .map(|n| (n.to_string(), 30))
                .collect(),
        ),
    }
}

/// `json.loads(str(form.get(k, "[]") or "[]"))` - None when serde refuses text json.loads might take (Python's)
fn form_json(f: &Form, k: &str) -> Option<Value> {
    let raw = f.get(k).filter(|v| !v.is_empty()).unwrap_or("[]");
    serde_json::from_str(raw).ok()
}

fn calendar_config_save(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let (config, _, _) = crate::calendar::for_write(&c.db, wid)?;
    let mut config = config.as_object()?.clone();
    let era = take_chars(&strip(f.get("era_name").unwrap_or("")), 64);
    config.insert("era_name".into(), json!(if era.is_empty() { "Year".to_string() } else { era }));
    let cur = match f.get("current_day") {
        None => 1,
        Some(v) => match py_int_text(v) {
            Ok(n) => n,
            Err(PyErr::Value) => 1,
            Err(_) => return None,
        },
    };
    config.insert("current_day".into(), json!(cur.max(1)));
    let dpw = clean_days_per_week(f.get("days_per_week").map(|v| json!(v)).as_ref()).ok()?;
    config.insert("days_per_week".into(), json!(dpw));
    // each part on its own: one that fails is skipped (`except Exception: pass`), the stored part kept
    match clean_months(&form_json(&f, "months_json")?) {
        Ok(m) if !m.is_empty() => {
            config.insert("months".into(), Value::Array(m));
        }
        Ok(_) | Err(PyErr::Value) | Err(PyErr::Type) => {}
        Err(PyErr::Unsure) => return None,
    }
    match clean_moons(&form_json(&f, "moons_json")?) {
        Ok(m) => {
            config.insert("moons".into(), Value::Array(m));
        }
        Err(PyErr::Unsure) => return None,
        Err(_) => {}
    }
    let months = config_months(&config)?;
    if f.has("holidays_json") {
        match clean_holidays(&form_json(&f, "holidays_json")?, &months) {
            Ok(h) => {
                config.insert("holidays".into(), Value::Array(h));
            }
            Err(PyErr::Unsure) => return None,
            Err(_) => {}
        }
    }
    if f.has("year_format") {
        let yf = take_chars(&strip(f.get("year_format").unwrap_or("")), 40);
        config.insert("year_format".into(), json!(if yf.contains("{year}") { yf } else { "Year {year}".to_string() }));
    }
    let names = if f.has("weekday_names") {
        json!(f.get("weekday_names").unwrap_or(""))
    } else {
        config.get("weekday_names").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!([]))
    };
    let wn = match clean_weekday_names(&names, dpw) {
        Ok(n) => n,
        Err(PyErr::Value) => {
            // weekday_names_of(config): the stored names fitted to the stored days_per_week, [] when they do not clean
            let stored = config.get("weekday_names").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!([]));
            match clean_weekday_names(&stored, clean_days_per_week(config.get("days_per_week")).ok()?) {
                Ok(n) => n,
                Err(PyErr::Value) => vec![],
                Err(_) => return None,
            }
        }
        Err(_) => return None,
    };
    config.insert("weekday_names".into(), json!(wn));
    let stored_h = config.get("holidays").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!([]));
    let h = match clean_holidays(&stored_h, &months) {
        Ok(h) => h,
        Err(PyErr::Value) => vec![],
        Err(_) => return None, // a TypeError here is Python's 500
    };
    config.insert("holidays".into(), Value::Array(h));
    let days: Vec<i64> = config_months(&config)?.iter().map(|m| m.1).collect();
    let cur = config.get("current_day")?.as_i64()?;
    config.insert("current_day".into(), json!(crate::calendar::clamp_day(&days, cur)));
    save_calendar(&c.db, wid, &Value::Object(config))?;
    redirect("/calendar?saved=1")
}

/// `<DB_PATH dir>/uploads`, where every stored upload lives (Python's UPLOADS_DIR)
fn uploads_root(c: &Ctx) -> Option<std::path::PathBuf> {
    Some(std::path::Path::new(&c.cfg.db_path).parent()?.join("uploads"))
}

/// Delete (after the commit) the file behind an `/uploads/...` url when it resolves inside the uploads folder and is a file
/// (`_delete_icon_file` & co.)
fn remove_upload(c: &mut Ctx, url: &str) {
    let (Some(rel), Some(root)) = (url.strip_prefix("/uploads/"), uploads_root(c).and_then(|r| r.canonicalize().ok())) else { return };
    if let Ok(path) = root.join(rel).canonicalize() {
        if path.starts_with(&root) && path.is_file() {
            c.removals.push(path);
        }
    }
}

fn calendar_icon_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let icon = row(&c.db, "SELECT * FROM calendar_day_icons WHERE id = ?1", ids[0])?;
    if icon.get("world_id")?.as_i64() != Some(wid) {
        return None;
    }
    c.db.execute("DELETE FROM calendar_day_icons WHERE id = ?1", [ids[0]]).ok()?;
    if let Some(url) = icon.get("image_url").and_then(|v| v.as_str()) {
        remove_upload(c, url);
    }
    json_response(&json!({"ok": true}))
}

/// `_ai._normalized_fact_key` for ASCII text (Python's and Rust's lower() can part ways beyond it: None, Python's)
fn fact_key(content: &str) -> Option<String> {
    if !content.is_ascii() {
        return None;
    }
    let low = strip(content).to_ascii_lowercase();
    let collapsed: String = {
        let mut out = String::new();
        let mut ws = false;
        for ch in low.chars() {
            if ch.is_ascii_whitespace() || ch == '\x0b' || ('\x1c'..='\x1f').contains(&ch) {
                if !ws {
                    out.push(' ');
                }
                ws = true;
            } else {
                out.push(ch);
                ws = false;
            }
        }
        out
    };
    Some(collapsed.trim_end_matches(['.', '!', '?']).to_string())
}

/// `_normalize_tags_field` for a JSON value
fn json_tags(v: Option<&Value>) -> Option<Sql> {
    Some(match v {
        Some(Value::Array(a)) => {
            let parts: Vec<String> = a.iter().map(|x| x.as_str().map(strip)).collect::<Option<Vec<_>>>()?.into_iter().filter(|x| !x.is_empty()).collect();
            if parts.is_empty() { Sql::Null } else { s(&parts.join(", ")) }
        }
        Some(Value::String(x)) => {
            let x = strip(x);
            if x.is_empty() { Sql::Null } else { s(&x) }
        }
        _ => Sql::Null,
    })
}

fn facts_bulk(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let wid = c.world_id()?;
    if !c.can_edit_section("facts")? {
        return None;
    }
    let items = b.get("facts")?.as_array().filter(|a| !a.is_empty())?.clone();
    let session = match b.get("game_session_id") {
        Some(v) if py_truthy(Some(v)) => Sql::Integer(py_int_value(v).ok()?),
        _ => Sql::Null,
    };
    let mut seen: Vec<String> = vec![];
    let mut stmt = c.db.prepare("SELECT content FROM facts WHERE world_id = ?1").ok()?;
    let existing: Vec<Option<String>> = stmt.query_map([wid], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
    drop(stmt);
    for e in existing {
        seen.push(fact_key(&e?)?);
    }
    let (mut created, mut skipped) = (0, 0);
    for it in &items {
        let Some(m) = it.as_object() else { continue };
        let content = match m.get("content") {
            None => String::new(),
            Some(Value::String(x)) => strip(x),
            _ => return None, // str() of other values: Python's
        };
        if content.is_empty() {
            continue;
        }
        let key = fact_key(&content)?;
        if seen.contains(&key) {
            skipped += 1;
            continue;
        }
        seen.push(key);
        let visible = m.get("visible_to_players").map_or(true, |v| py_truthy(Some(v)));
        orm::insert(
            &c.db,
            "facts",
            &[
                ("world_id", Sql::Integer(wid)),
                ("game_session_id", session.clone()),
                ("content", s(&content)),
                ("visible_to_players", Sql::Integer(visible as i64)),
                ("tags", json_tags(m.get("tags"))?),
                ("author_id", Sql::Integer(c.p.user_id)),
                ("updated_at", s(&orm::now_stamp())),
            ],
        )?;
        created += 1;
    }
    // job_id: `str(job_id).isdigit()` - an int or digit text
    let job = match b.get("job_id") {
        Some(v) if py_truthy(Some(v)) => match v {
            Value::Number(n) => n.as_i64().filter(|i| *i >= 0),
            Value::String(x) if !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit()) && x.len() < 16 => x.parse().ok(),
            Value::String(x) if !x.is_ascii() => return None,
            Value::Bool(_) | Value::String(_) => None,
            _ => return None,
        },
        _ => None,
    };
    if let Some(jid) = job {
        if let Some(j) = row(&c.db, "SELECT * FROM audio_jobs WHERE id = ?1", jid) {
            if j.get("purpose").and_then(|v| v.as_str()) == Some("facts_parse") && j.get("world_id").and_then(|v| v.as_i64()) == Some(wid) {
                orm::update(&c.db, "audio_jobs", jid, &[("draft_consumed", Sql::Integer(1))])?;
            }
        }
    }
    json_response(&json!({"created": created, "skipped_duplicates": skipped}))
}

fn tables_import(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    if content_type(c.headers) != "multipart/form-data" {
        return None;
    }
    let (f, files) = parse_multipart(c.headers, c.body)?;
    let wid = c.world_id()?;
    if !c.can_edit_section("tables")? || f.has("file") {
        return None;
    }
    // form.get("file"): the last part named "file"
    let file = files.iter().rev().find(|p| p.name == "file")?;
    let text = std::str::from_utf8(&file.data).ok().filter(|t| !t.starts_with('\u{feff}'))?; // json.loads(bytes) also reads UTF-16/32 and BOMs: Python's
    let Value::Array(items) = serde_json::from_str::<Value>(text).ok()? else { return None };
    let owner = if content_editor(c) { Sql::Null } else { Sql::Integer(c.p.user_id) };
    // every slug from the tables stored before the import (Python adds them all, then commits): two alike in one file are Python's
    let mut rows = vec![];
    let mut slugs: Vec<String> = vec![];
    for it in &items {
        let m = it.as_object()?;
        let name = match m.get("name") {
            None => String::new(),
            Some(Value::String(x)) => strip(x),
            _ => return None,
        };
        let name = if name.is_empty() { "Imported Table".to_string() } else { name };
        let text_or = |k: &str, d: &str| -> Option<String> {
            match m.get(k) {
                None => Some(d.to_string()),
                Some(Value::String(x)) => Some(x.clone()),
                _ => None,
            }
        };
        let slug = table_slug(&c.db, &name)?;
        if slugs.contains(&slug) {
            return None;
        }
        slugs.push(slug.clone());
        let entries = nd_render::py_dumps(m.get("entries").unwrap_or(&json!([])));
        rows.push((name, slug, text_or("category", "general")?, text_or("description", "")?, entries));
    }
    for (name, slug, cat, desc, entries) in rows {
        orm::insert(
            &c.db,
            "random_tables",
            &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("slug", s(&slug)), ("category", s(&cat)), ("description", s(&desc)),
              ("is_builtin", Sql::Integer(0)), ("entries_json", s(&entries)), ("created_by_user_id", owner.clone())],
        )?;
    }
    redirect("/tables")
}

fn quests_apply(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let wid = c.world_id()?;
    if !c.p.is_gm {
        return None;
    }
    const STATUSES: [&str; 4] = ["active", "complete", "failed", "secret"];
    const CATEGORIES: [&str; 3] = ["main", "side", "personal"];
    let list = |k: &str| -> Option<Vec<Value>> {
        match b.get(k) {
            Some(v) if py_truthy(Some(v)) => v.as_array().cloned(),
            _ => Some(vec![]),
        }
    };
    let text = |m: &Map<String, Value>, k: &str| -> Option<String> {
        match m.get(k) {
            Some(Value::String(x)) => Some(x.clone()),
            Some(v) if py_truthy(Some(v)) => None, // str() of other values: Python's
            _ => Some(String::new()),
        }
    };
    let pick = |v: Option<&Value>, from: &[&str], d: &str| -> String {
        v.and_then(|x| x.as_str()).filter(|x| from.contains(x)).unwrap_or(d).to_string()
    };
    // updates first (the unit of work flushes UPDATEs before INSERTs), then the new quests
    let mut updated = 0;
    for u in list("quest_updates")?.iter().take(24) {
        let Some(u) = u.as_object() else { continue };
        if u.get("quest_id").is_some_and(|v| v.is_boolean()) {
            return None; // True is quest 1 to Python's isinstance(x, int) and dict lookup
        }
        let Some(qid) = u.get("quest_id").and_then(|v| v.as_i64()) else { continue };
        let Some(q) = row(&c.db, "SELECT * FROM quests WHERE id = ?1", qid) else { continue };
        if q.get("world_id")?.as_i64() != Some(wid) {
            continue;
        }
        let mut set: Vec<(&str, Sql)> = vec![];
        if let Some(st) = u.get("status").and_then(|v| v.as_str()).filter(|x| STATUSES.contains(x)) {
            set.push(("status", s(st)));
        }
        let note = strip(&text(u, "note")?);
        if !note.is_empty() {
            set.push(("summary", s(&take_chars(&note, 512))));
        }
        orm::update(&c.db, "quests", qid, &set)?;
        updated += 1;
    }
    let mut parties: Vec<i64> = vec![];
    let mut stmt = c.db.prepare("SELECT id FROM parties WHERE world_id = ?1").ok()?;
    for r in stmt.query_map([wid], |r| r.get(0)).ok()? {
        parties.push(r.ok()?);
    }
    drop(stmt);
    let mut created = 0;
    for n in list("new_quests")?.iter().take(12) {
        let Some(n) = n.as_object() else { continue };
        let title = strip(&text(n, "title")?);
        if title.is_empty() {
            continue;
        }
        let party = n.get("assigned_party_id").filter(|v| !v.is_boolean()).and_then(|v| v.as_i64()).filter(|p| parties.contains(p));
        orm::insert(
            &c.db,
            "quests",
            &[
                ("world_id", Sql::Integer(wid)),
                ("title", s(&take_chars(&title, 256))),
                ("assigned_party_id", party.map_or(Sql::Null, Sql::Integer)),
                ("status", s(&pick(n.get("status"), &STATUSES, "active"))),
                ("category", s(&pick(n.get("category"), &CATEGORIES, "side"))),
                ("summary", s(&take_chars(&strip(&text(n, "summary")?), 512))),
                ("body", s(&text(n, "body")?)),
                ("visible_to_players", Sql::Integer(n.get("visible_to_players").map_or(true, |v| py_truthy(Some(v))) as i64)),
                ("created_by_user_id", Sql::Integer(c.p.user_id)),
            ],
        )?;
        created += 1;
    }
    orm::touch(&c.db, wid)?;
    json_response(&json!({"created": created, "updated": updated}))
}

// ── media libraries: audio, video, pages (app/routers/audio.py, video.py, pages.py) ──

/// One media library's tables and rules
struct Lib {
    section: &'static str,
    base: &'static str,
    albums: &'static str,
    items: &'static str,
    /// the item's columns holding files it owns (deleted with it)
    files: &'static [&'static str],
    /// an entity can be attached (audio, video)
    entity: bool,
    /// the "now playing" broadcast follows its clips (audio)
    now_playing: bool,
    /// items can be character-sheet templates, and those in use cannot be deleted (pages)
    sheets: bool,
}

const AUDIO: Lib = Lib { section: "audio", base: "/audio", albums: "audio_albums", items: "audio_clips", files: &["file_url"], entity: true, now_playing: true, sheets: false };
const VIDEO: Lib =
    Lib { section: "video", base: "/video", albums: "video_albums", items: "video_clips", files: &["file_url", "poster_url"], entity: true, now_playing: false, sheets: false };
const PAGES: Lib = Lib { section: "pages", base: "/pages", albums: "page_albums", items: "page_docs", files: &["file_url"], entity: false, now_playing: false, sheets: true };

fn audio_album_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_new(c, &AUDIO, ids)
}
fn audio_album_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_rename(c, &AUDIO, ids)
}
fn audio_album_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_delete(c, &AUDIO, ids)
}
fn audio_item_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_edit(c, &AUDIO, ids)
}
fn audio_item_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_delete(c, &AUDIO, ids)
}
fn video_album_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_new(c, &VIDEO, ids)
}
fn video_album_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_rename(c, &VIDEO, ids)
}
fn video_album_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_delete(c, &VIDEO, ids)
}
fn video_item_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_edit(c, &VIDEO, ids)
}
fn video_item_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_delete(c, &VIDEO, ids)
}
fn pages_album_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_new(c, &PAGES, ids)
}
fn pages_album_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_rename(c, &PAGES, ids)
}
fn pages_album_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_album_delete(c, &PAGES, ids)
}
fn pages_item_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_edit(c, &PAGES, ids)
}
fn pages_item_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lib_item_delete(c, &PAGES, ids)
}

/// `require_can_edit` + the library's `_require_edit_section`: the active world's id
fn lib_editor(c: &Ctx, lib: &Lib) -> Option<i64> {
    let wid = c.world_id()?;
    (content_editor(c) && c.can_edit_section(lib.section)?).then_some(wid)
}

/// `str(value).strip()` that is digits (`isdigit()`): Ok(Some(n)); Ok(None) when it is not; Err for text whose isdigit() is Python's
fn digit_field(v: &str) -> Result<Option<i64>, ()> {
    let v = strip(v);
    if !v.is_ascii() {
        return Err(());
    }
    if v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit()) {
        return Ok(None);
    }
    if v.len() > 15 {
        return Err(());
    }
    Ok(v.parse().ok())
}

/// `_album_or_404`: the album id, when it is one of the world's (a 404 is Python's)
fn lib_album(c: &Ctx, lib: &Lib, wid: i64, id: i64) -> Option<Map<String, Value>> {
    let a = row(&c.db, &format!("SELECT * FROM {} WHERE id = ?1", lib.albums), id)?;
    (a.get("world_id")?.as_i64() == Some(wid)).then_some(a)
}

fn lib_album_new(c: &mut Ctx, lib: &Lib, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = lib_editor(c, lib)?;
    let name = take_chars(&strip(f.get("name").unwrap_or("")), 120);
    let name = if name.is_empty() { "Untitled Album".to_string() } else { name };
    let count: i64 = c.db.query_row(&format!("SELECT COUNT(*) FROM {} WHERE world_id = ?1", lib.albums), [wid], |r| r.get(0)).ok()?;
    if count >= 100 {
        return None;
    }
    let parent = match digit_field(f.get("parent_id").unwrap_or("")).ok()? {
        Some(pid) => Sql::Integer(lib_album(c, lib, wid, pid)?.get("id")?.as_i64()?),
        None => Sql::Null,
    };
    let id = orm::insert(&c.db, lib.albums, &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("parent_id", parent)])?;
    redirect(&format!("{}/albums/{id}", lib.base))
}

fn lib_album_rename(c: &mut Ctx, lib: &Lib, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = lib_editor(c, lib)?;
    lib_album(c, lib, wid, ids[0])?;
    let name = take_chars(&strip(f.get("name").unwrap_or("")), 120);
    if !name.is_empty() {
        orm::update(&c.db, lib.albums, ids[0], &[("name", s(&name))])?;
    }
    redirect(&format!("{}/albums/{}", lib.base, ids[0]))
}

/// The files an item owns (`_delete_clip_file` / `_delete_doc_file`)
fn lib_item_files(lib: &Lib, item: &Map<String, Value>) -> Vec<String> {
    lib.files.iter().filter_map(|k| item.get(*k).and_then(|v| v.as_str()).map(str::to_string)).collect()
}

/// `_sheets_referencing_count(db, doc_ids)`
fn sheets_using(c: &Ctx, doc_ids: &[i64]) -> Option<i64> {
    let mut n = 0;
    for id in doc_ids {
        n += c.db.query_row("SELECT COUNT(*) FROM character_sheets WHERE template_id = ?1", [id], |r| r.get::<_, i64>(0)).ok()?;
    }
    Some(n)
}

fn lib_album_delete(c: &mut Ctx, lib: &Lib, ids: &[i64]) -> Option<Response<Body>> {
    let wid = lib_editor(c, lib)?;
    let album = lib_album(c, lib, wid, ids[0])?;
    // the album and everything nested under it (media_albums.descendant_albums)
    let mut all = vec![ids[0]];
    let mut frontier = vec![ids[0]];
    while !frontier.is_empty() {
        let mut next = vec![];
        for pid in &frontier {
            let mut stmt = c.db.prepare(&format!("SELECT id FROM {} WHERE parent_id = ?1", lib.albums)).ok()?;
            for r in stmt.query_map([pid], |r| r.get::<_, i64>(0)).ok()? {
                next.push(r.ok()?);
            }
        }
        all.extend(next.iter().copied());
        frontier = next;
    }
    let mut items = vec![];
    for aid in &all {
        let mut stmt = c.db.prepare(&format!("SELECT * FROM {} WHERE album_id = ?1", lib.items)).ok()?;
        let rows: Vec<Map<String, Value>> = stmt.query_map([aid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        items.extend(rows);
    }
    let item_ids: Vec<i64> = items.iter().filter_map(|i| i.get("id").and_then(|v| v.as_i64())).collect();
    if lib.sheets && sheets_using(c, &item_ids)? > 0 {
        return None; // the 400: Python's
    }
    let mut files = vec![];
    for it in &items {
        files.extend(lib_item_files(lib, it));
        c.db.execute(&format!("DELETE FROM {} WHERE id = ?1", lib.items), [it.get("id")?.as_i64()?]).ok()?;
    }
    for aid in &all {
        c.db.execute(&format!("DELETE FROM {} WHERE id = ?1", lib.albums), [aid]).ok()?;
    }
    for f in files {
        remove_upload(c, &f);
    }
    match album.get("parent_id").and_then(|v| v.as_i64()) {
        Some(p) if p != 0 => redirect(&format!("{}/albums/{p}", lib.base)),
        _ => redirect(lib.base),
    }
}

/// `_clip_or_404` / `_doc_or_404`
fn lib_item(c: &Ctx, lib: &Lib, wid: i64, id: i64) -> Option<Map<String, Value>> {
    let it = row(&c.db, &format!("SELECT * FROM {} WHERE id = ?1", lib.items), id)?;
    (it.get("world_id")?.as_i64() == Some(wid)).then_some(it)
}

/// The world's now-playing broadcast is this item's file
fn now_playing_is(c: &Ctx, wid: i64, item: &Map<String, Value>) -> Option<bool> {
    let w = row(&c.db, "SELECT now_playing_url FROM worlds WHERE id = ?1", wid)?;
    Some(w.get("now_playing_url").filter(|v| !v.is_null()) == item.get("file_url"))
}

fn lib_item_edit(c: &mut Ctx, lib: &Lib, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = lib_editor(c, lib)?;
    let item = lib_item(c, lib, wid, ids[0])?;
    let mut set: Vec<(&str, Sql)> = vec![];
    let name = take_chars(&strip(f.get("name").unwrap_or("")), 256);
    if !name.is_empty() {
        set.push(("name", s(&name)));
    }
    set.push(("description", s(&take_chars(&strip(f.get("description").unwrap_or("")), 512))));
    let visible = flag_param(&f, "visible_to_players");
    set.push(("visible_to_players", Sql::Integer(visible)));
    if lib.sheets {
        set.push(("is_character_sheet_template", Sql::Integer(flag_param(&f, "is_character_sheet_template"))));
    }
    if lib.entity {
        let ent = match digit_field(f.get("entity_id").unwrap_or("")).ok()? {
            Some(eid) => {
                let w: Option<i64> = c.db.query_row("SELECT world_id FROM entities WHERE id = ?1", [eid], |r| r.get(0)).ok().flatten();
                if w == Some(wid) { Sql::Integer(eid) } else { Sql::Null }
            }
            None => Sql::Null,
        };
        set.push(("entity_id", ent));
    }
    if lib.now_playing && visible == 0 && now_playing_is(c, wid, &item)? {
        return None; // hiding the clip players are hearing stops the broadcast and clears Python's in-memory spotlight cache: Python's
    }
    let album = match digit_field(f.get("album_id").unwrap_or("")).ok()? {
        Some(aid) => Some(lib_album(c, lib, wid, aid)?.get("id")?.as_i64()?),
        None => None,
    };
    set.push(("album_id", album.map_or(Sql::Null, Sql::Integer)));
    orm::update(&c.db, lib.items, ids[0], &set)?;
    match album {
        Some(a) => redirect(&format!("{}/albums/{a}", lib.base)),
        None => redirect(lib.base),
    }
}

fn lib_item_delete(c: &mut Ctx, lib: &Lib, ids: &[i64]) -> Option<Response<Body>> {
    let wid = lib_editor(c, lib)?;
    let item = lib_item(c, lib, wid, ids[0])?;
    if lib.sheets && sheets_using(c, &[ids[0]])? > 0 {
        return None;
    }
    if lib.now_playing && now_playing_is(c, wid, &item)? {
        return None; // the broadcast of this clip ends with it: Python's
    }
    c.db.execute(&format!("DELETE FROM {} WHERE id = ?1", lib.items), [ids[0]]).ok()?;
    for f in lib_item_files(lib, &item) {
        remove_upload(c, &f);
    }
    match item.get("album_id").and_then(|v| v.as_i64()) {
        Some(a) if a != 0 => redirect(&format!("{}/albums/{a}", lib.base)),
        _ => redirect(lib.base),
    }
}

// ── image albums (app/routers/gallery.py) ────────────────────────────────────

/// The gallery's library: albums only (image albums hold urls, not files)
const IMAGES: Lib = Lib { section: "images", base: "/images", albums: "image_albums", items: "", files: &[], entity: false, now_playing: false, sheets: false };

/// The gallery's write check (`_require_edit_section`; no require_can_edit there)
fn images_editor(c: &Ctx) -> Option<i64> {
    let wid = c.world_id()?;
    c.can_edit_section("images")?.then_some(wid)
}

/// Every album nested under `root` (media_albums.descendant_albums)
fn album_descendants(c: &Ctx, table: &str, root: i64) -> Option<Vec<i64>> {
    let (mut all, mut frontier) = (vec![], vec![root]);
    while !frontier.is_empty() {
        let mut next = vec![];
        for pid in &frontier {
            let mut stmt = c.db.prepare(&format!("SELECT id FROM {table} WHERE parent_id = ?1")).ok()?;
            for r in stmt.query_map([pid], |r| r.get::<_, i64>(0)).ok()? {
                next.push(r.ok()?);
            }
        }
        all.extend(next.iter().copied());
        frontier = next;
    }
    Some(all)
}

fn image_album_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = images_editor(c)?;
    let name = take_chars(&strip(f.get("name").unwrap_or("")), 120);
    let name = if name.is_empty() { "Untitled Album".to_string() } else { name };
    let count: i64 = c.db.query_row("SELECT COUNT(*) FROM image_albums WHERE world_id = ?1", [wid], |r| r.get(0)).ok()?;
    if count >= 100 {
        return None;
    }
    let parent = match digit_field(f.get("parent_id").unwrap_or("")).ok()? {
        Some(pid) => Sql::Integer(lib_album(c, &IMAGES, wid, pid)?.get("id")?.as_i64()?),
        None => Sql::Null,
    };
    let id = orm::insert(&c.db, "image_albums", &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("image_urls_json", s("[]")), ("parent_id", parent)])?;
    redirect(&format!("/images/albums/{id}"))
}

fn image_album_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = images_editor(c)?;
    lib_album(c, &IMAGES, wid, ids[0])?;
    let name = take_chars(&strip(f.get("name").unwrap_or("")), 120);
    if !name.is_empty() {
        orm::update(&c.db, "image_albums", ids[0], &[("name", s(&name))])?;
    }
    redirect(&format!("/images/albums/{}", ids[0]))
}

fn image_album_move(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = images_editor(c)?;
    lib_album(c, &IMAGES, wid, ids[0])?;
    let raw = strip(f.get("parent_id").unwrap_or(""));
    let parent = if raw.is_empty() {
        Sql::Null
    } else {
        let pid = digit_field(&raw).ok()??; // not digits: the 400, Python's
        let np = lib_album(c, &IMAGES, wid, pid)?.get("id")?.as_i64()?;
        if np == ids[0] || album_descendants(c, "image_albums", ids[0])?.contains(&np) {
            return None;
        }
        Sql::Integer(np)
    };
    orm::update(&c.db, "image_albums", ids[0], &[("parent_id", parent)])?;
    redirect(&format!("/images/albums/{}", ids[0]))
}

fn image_album_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let wid = images_editor(c)?;
    lib_album(c, &IMAGES, wid, ids[0])?;
    for d in album_descendants(c, "image_albums", ids[0])? {
        c.db.execute("DELETE FROM image_albums WHERE id = ?1", [d]).ok()?;
    }
    c.db.execute("DELETE FROM image_albums WHERE id = ?1", [ids[0]]).ok()?;
    redirect("/images")
}

/// `_load_urls(album)` (stored text serde would read differently: Python's)
fn album_urls(album: &Map<String, Value>) -> Option<Vec<Value>> {
    let raw = album.get("image_urls_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    Some(match canonical_json(raw)? {
        Value::Array(a) => a,
        _ => vec![],
    })
}

fn image_album_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let wid = images_editor(c)?;
    let album = lib_album(c, &IMAGES, wid, ids[0])?;
    let urls = b.get("urls")?.as_array()?.clone();
    let mut current = album_urls(&album)?;
    for u in urls {
        if u.as_str().is_some_and(|x| !x.is_empty()) && !current.contains(&u) && current.len() < 500 {
            current.push(u);
        }
    }
    let n = current.len();
    orm::update(&c.db, "image_albums", ids[0], &[("image_urls_json", s(&nd_render::py_dumps(&Value::Array(current))))])?;
    json_response(&json!({"ok": true, "count": n}))
}

fn image_album_remove(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let wid = images_editor(c)?;
    let album = lib_album(c, &IMAGES, wid, ids[0])?;
    let gone: Vec<Value> = match b.get("urls") {
        Some(Value::Array(a)) => a.clone(),
        _ => vec![b.get("url").cloned().unwrap_or(Value::Null)],
    };
    if gone.iter().any(|g| !(g.is_string() || g.is_null())) {
        return None; // set() of other values (unhashable, or == across types): Python's
    }
    let current: Vec<Value> = album_urls(&album)?.into_iter().filter(|u| !gone.contains(u)).collect();
    if current.iter().any(|u| !(u.is_string() || u.is_null())) {
        return None;
    }
    let n = current.len();
    orm::update(&c.db, "image_albums", ids[0], &[("image_urls_json", s(&nd_render::py_dumps(&Value::Array(current))))])?;
    json_response(&json!({"ok": true, "count": n}))
}

// ── session media (app/session_media.py, app/routers/session_media.py, session_audio.py) ──

/// `media_items(gs)`, when every kept item already has its uid (one that would get a fresh random uid, or a value read through
/// str(): Python's)
fn sm_items(gs: &Map<String, Value>) -> Option<Vec<Map<String, Value>>> {
    let raw = gs.get("recap_media_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let list = match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => a,
        _ => vec![],
    };
    let (mut out, mut seen_img, mut seen_vid): (Vec<Map<String, Value>>, Vec<String>, Vec<i64>) = (vec![], vec![], vec![]);
    for it in list {
        let Value::Object(it) = it else { continue };
        let uid = || -> Option<String> {
            match it.get("uid") {
                Some(Value::String(u)) if !u.is_empty() => Some(take_chars(u, 32)),
                _ => None,
            }
        };
        match it.get("kind").and_then(|k| k.as_str()) {
            Some("image") => {
                let Some(url) = it.get("url").and_then(|u| u.as_str()).filter(|u| u.starts_with("/uploads/")) else { continue };
                if seen_img.iter().any(|s| s == url) {
                    continue;
                }
                seen_img.push(url.to_string());
                let caption = match it.get("caption") {
                    Some(Value::String(c)) => take_chars(c, 120),
                    Some(v) if py_truthy(Some(v)) => return None,
                    _ => String::new(),
                };
                let mut m = Map::new();
                m.insert("uid".into(), json!(uid()?));
                m.insert("kind".into(), json!("image"));
                m.insert("url".into(), json!(url));
                m.insert("caption".into(), json!(caption));
                m.insert("visible".into(), json!(py_truthy(it.get("visible"))));
                m.insert("own".into(), json!(py_truthy(it.get("own"))));
                out.push(m);
            }
            Some("video") => {
                let Some(cid) = it.get("clip_id").filter(|v| !v.is_boolean()).and_then(|v| v.as_i64()) else { continue };
                if seen_vid.contains(&cid) {
                    continue;
                }
                seen_vid.push(cid);
                let mut m = Map::new();
                m.insert("uid".into(), json!(uid()?));
                m.insert("kind".into(), json!("video"));
                m.insert("clip_id".into(), json!(cid));
                out.push(m);
            }
            _ => {}
        }
    }
    out.truncate(50);
    Some(out)
}

/// `recap_clip_ids(gs)`
fn sm_clip_ids(gs: &Map<String, Value>) -> Option<Vec<i64>> {
    let raw = gs.get("recap_audio_json").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or("[]");
    let list = match serde_json::from_str::<Value>(raw).ok()? {
        Value::Array(a) => a,
        _ => vec![],
    };
    let mut out = vec![];
    for v in list {
        if let Some(i) = v.as_i64().filter(|_| !v.is_boolean() && !v.is_f64()) {
            if !out.contains(&i) {
                out.push(i);
            }
        }
    }
    Some(out)
}

/// `limit(db)`: Settings -> System's session media maximum (10 by default)
fn sm_limit(c: &Ctx) -> Option<i64> {
    let raw: Option<i64> = c.db.query_row("SELECT session_media_max FROM app_settings LIMIT 1", [], |r| r.get(0)).ok()?;
    Some(raw.filter(|n| *n >= 1).map_or(10, |n| n.min(50)))
}

fn sm_store_media(c: &Ctx, gs_id: i64, items: &[Map<String, Value>]) -> Option<()> {
    let list: Vec<Value> = items.iter().take(50).cloned().map(Value::Object).collect();
    orm::update(&c.db, "game_sessions", gs_id, &[("recap_media_json", s(&nd_render::py_dumps(&Value::Array(list))))])?;
    Some(())
}

fn sm_store_clips(c: &Ctx, gs_id: i64, ids: &[i64]) -> Option<()> {
    let ids: Vec<i64> = ids.iter().take(50).copied().collect();
    orm::update(&c.db, "game_sessions", gs_id, &[("recap_audio_json", s(&ints_json(&ids)))])?;
    Some(())
}

fn sm_audio_dict(c: &Map<String, Value>) -> Value {
    let txt = |k: &str| json!(c.get(k).and_then(|v| v.as_str()).unwrap_or(""));
    json!({"id": c.get("id"), "name": c.get("name"), "description": txt("description"), "file_url": c.get("file_url"),
           "visible_to_players": py_truthy(c.get("visible_to_players")), "transcript": txt("transcript")})
}

fn sm_video_dict(c: &Map<String, Value>, uid: &str) -> Value {
    let txt = |k: &str| json!(c.get(k).and_then(|v| v.as_str()).unwrap_or(""));
    json!({"id": c.get("id"), "uid": uid, "name": c.get("name"), "description": txt("description"), "file_url": c.get("file_url"),
           "poster_url": txt("poster_url"), "visible_to_players": py_truthy(c.get("visible_to_players"))})
}

/// `panel(db, gs)`: the GM's session media panel
fn sm_panel(c: &Ctx, gs_id: i64) -> Option<Response<Body>> {
    json_response(&sm_panel_value(c, gs_id)?)
}

fn sm_panel_value(c: &Ctx, gs_id: i64) -> Option<Value> {
    let gs = row(&c.db, "SELECT * FROM game_sessions WHERE id = ?1", gs_id)?;
    let wid = gs.get("world_id")?.as_i64()?;
    let (ids, items) = (sm_clip_ids(&gs)?, sm_items(&gs)?);
    let clip = |table: &str, id: i64| -> Option<Map<String, Value>> {
        row(&c.db, &format!("SELECT * FROM {table} WHERE id = ?1", table = table), id).filter(|r| r.get("world_id").and_then(|v| v.as_i64()) == Some(wid))
    };
    let clips: Vec<Value> = ids.iter().filter_map(|i| clip("audio_clips", *i)).map(|c| sm_audio_dict(&c)).collect();
    let videos: Vec<Value> = items
        .iter()
        .filter(|i| i.get("kind").and_then(|k| k.as_str()) == Some("video"))
        .filter_map(|i| Some(sm_video_dict(&clip("video_clips", i.get("clip_id")?.as_i64()?)?, i.get("uid")?.as_str()?)))
        .collect();
    let video_ids: Vec<i64> = items.iter().filter_map(|i| (i.get("kind").and_then(|k| k.as_str()) == Some("video")).then(|| i.get("clip_id")?.as_i64()).flatten()).collect();
    let lib = |table: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = c.db.prepare(&format!("SELECT * FROM {table} WHERE world_id = ?1 ORDER BY created_at DESC LIMIT 200")).ok()?;
        let rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        Some(rows)
    };
    let library: Vec<Value> = lib("audio_clips")?
        .iter()
        .filter(|r| !r.get("id").and_then(|v| v.as_i64()).is_some_and(|i| ids.contains(&i)))
        .map(|r| json!({"id": r.get("id"), "name": r.get("name"), "visible_to_players": py_truthy(r.get("visible_to_players")), "file_url": r.get("file_url")}))
        .collect();
    let video_library: Vec<Value> = lib("video_clips")?
        .iter()
        .filter(|r| !r.get("id").and_then(|v| v.as_i64()).is_some_and(|i| video_ids.contains(&i)))
        .map(|r| {
            json!({"id": r.get("id"), "name": r.get("name"), "visible_to_players": py_truthy(r.get("visible_to_players")), "file_url": r.get("file_url"),
                   "poster_url": r.get("poster_url").and_then(|v| v.as_str()).unwrap_or("")})
        })
        .collect();
    let images: Vec<Value> = items
        .iter()
        .filter(|i| i.get("kind").and_then(|k| k.as_str()) == Some("image"))
        .map(|i| json!({"uid": i.get("uid"), "url": i.get("url"), "caption": i.get("caption"), "visible_to_players": i.get("visible"), "own": i.get("own")}))
        .collect();
    Some(json!({"clips": clips, "library": library, "videos": videos, "video_library": video_library, "images": images,
                "max": sm_limit(c)?, "used": ids.len() + items.len()}))
}

/// `_require_gm_edit`: the session of the active world, with sessions:edit
fn sm_session(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let wid = c.world_id()?;
    if !c.can_edit_section("sessions")? {
        return None;
    }
    let gs = row(&c.db, "SELECT * FROM game_sessions WHERE id = ?1", id)?;
    (gs.get("world_id")?.as_i64() == Some(wid)).then_some(gs)
}

/// The JSON body as the media routes read it: bad JSON or a non-object is {} (`parse_json` ValueError -> {}); a body serde refuses
/// that json.loads might take is Python's
fn sm_body(c: &Ctx) -> Option<Map<String, Value>> {
    if !plain_numbers(c.body) {
        return None;
    }
    Some(match serde_json::from_slice::<Value>(c.body) {
        Ok(Value::Object(o)) => o,
        Ok(_) => Map::new(),
        Err(_) if c.body.is_empty() => Map::new(),
        Err(_) => return None,
    })
}

/// `_attach(db, gs, clip, visible)` for an audio clip of the world
fn sm_attach_audio(c: &Ctx, gs: &Map<String, Value>, clip: &Map<String, Value>, vis: Option<bool>) -> Option<()> {
    let gid = gs.get("id")?.as_i64()?;
    let mut ids = sm_clip_ids(gs)?;
    let cid = clip.get("id")?.as_i64()?;
    if !ids.contains(&cid) {
        if (ids.len() + sm_items(gs)?.len()) as i64 >= sm_limit(c)? {
            return None; // require_room's 400: Python's
        }
        ids.push(cid);
        sm_store_clips(c, gid, &ids)?;
    }
    if let Some(v) = vis {
        orm::update(&c.db, "audio_clips", cid, &[("visible_to_players", Sql::Integer(v as i64))])?;
    }
    Some(())
}

/// The world's clip by id
fn world_clip(c: &Ctx, table: &str, wid: i64, id: i64) -> Option<Map<String, Value>> {
    row(&c.db, &format!("SELECT * FROM {table} WHERE id = ?1"), id).filter(|r| r.get("world_id").and_then(|v| v.as_i64()) == Some(wid))
}

fn sm_audio_attach(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let b = sm_body(c)?;
    let cid = b.get("clip_id").filter(|v| !v.is_boolean()).and_then(|v| v.as_i64())?;
    let clip = world_clip(c, "audio_clips", gs.get("world_id")?.as_i64()?, cid)?;
    sm_attach_audio(c, &gs, &clip, b.get("visible_to_players").and_then(|v| v.as_bool()))?;
    sm_panel(c, ids[0])
}

fn sm_set_clip_visibility(c: &mut Ctx, ids: &[i64], table: &str) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let attached = if table == "audio_clips" {
        sm_clip_ids(&gs)?.contains(&ids[1])
    } else {
        sm_items(&gs)?.iter().any(|i| i.get("kind").and_then(|k| k.as_str()) == Some("video") && i.get("clip_id").and_then(|v| v.as_i64()) == Some(ids[1]))
    };
    if !attached {
        return None;
    }
    world_clip(c, table, gs.get("world_id")?.as_i64()?, ids[1])?;
    let vis = sm_body(c)?.get("visible_to_players").and_then(|v| v.as_bool())?;
    orm::update(&c.db, table, ids[1], &[("visible_to_players", Sql::Integer(vis as i64))])?;
    sm_panel(c, ids[0])
}

fn sm_audio_visibility(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    sm_set_clip_visibility(c, ids, "audio_clips")
}

fn sm_audio_item_visibility(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    sm_set_clip_visibility(c, ids, "audio_clips")
}

fn sm_video_item_visibility(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    sm_set_clip_visibility(c, ids, "video_clips")
}

fn sm_audio_remove(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let kept: Vec<i64> = sm_clip_ids(&gs)?.into_iter().filter(|i| *i != ids[1]).collect();
    sm_store_clips(c, ids[0], &kept)?;
    sm_panel(c, ids[0])
}

fn sm_video_item_remove(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let kept: Vec<Map<String, Value>> = sm_items(&gs)?
        .into_iter()
        .filter(|i| !(i.get("kind").and_then(|k| k.as_str()) == Some("video") && i.get("clip_id").and_then(|v| v.as_i64()) == Some(ids[1])))
        .collect();
    sm_store_media(c, ids[0], &kept)?;
    sm_panel(c, ids[0])
}

/// The rename body's name, whitespace collapsed (`" ".join(name.split())`)
fn sm_name(c: &Ctx) -> Option<String> {
    Some(nd_render::text::py_split_join(sm_body(c)?.get("name")?.as_str()?))
}

fn sm_rename_clip(c: &mut Ctx, ids: &[i64], table: &str) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let name = sm_name(c)?;
    if name.is_empty() {
        return None;
    }
    let attached = if table == "audio_clips" {
        sm_clip_ids(&gs)?.contains(&ids[1])
    } else {
        sm_items(&gs)?.iter().any(|i| i.get("kind").and_then(|k| k.as_str()) == Some("video") && i.get("clip_id").and_then(|v| v.as_i64()) == Some(ids[1]))
    };
    if !attached {
        return None;
    }
    world_clip(c, table, gs.get("world_id")?.as_i64()?, ids[1])?;
    orm::update(&c.db, table, ids[1], &[("name", s(&take_chars(&name, 200)))])?;
    sm_panel(c, ids[0])
}

fn sm_audio_item_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    sm_rename_clip(c, ids, "audio_clips")
}

fn sm_video_item_rename(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    sm_rename_clip(c, ids, "video_clips")
}

/// "<session id>/<uid>" from the image routes
fn sm_image_target(c: &Ctx, key: &str) -> Option<(i64, Vec<Map<String, Value>>, usize)> {
    let (sid, uid) = key.split_once('/')?;
    let sid: i64 = sid.parse().ok()?;
    let gs = sm_session(c, sid)?;
    let items = sm_items(&gs)?;
    let at = items.iter().position(|i| i.get("kind").and_then(|k| k.as_str()) == Some("image") && i.get("uid").and_then(|u| u.as_str()) == Some(uid))?;
    Some((sid, items, at))
}

fn sm_image_visibility(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let vis = sm_body(c)?.get("visible_to_players").and_then(|v| v.as_bool())?;
    let (sid, mut items, at) = sm_image_target(c, key)?;
    items[at].insert("visible".into(), json!(vis));
    sm_store_media(c, sid, &items)?;
    sm_panel(c, sid)
}

fn sm_image_rename(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let name = sm_name(c)?;
    let (sid, mut items, at) = sm_image_target(c, key)?;
    items[at].insert("caption".into(), json!(take_chars(&name, 120)));
    sm_store_media(c, sid, &items)?;
    sm_panel(c, sid)
}

fn sm_image_remove(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let (sid, mut items, at) = sm_image_target(c, key)?;
    if py_truthy(items[at].get("own")) {
        return None; // a picture uploaded for the session is deleted with its previews (imaging.derived_paths_for): Python's
    }
    items.remove(at);
    sm_store_media(c, sid, &items)?;
    sm_panel(c, sid)
}

fn sm_attach(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let wid = gs.get("world_id")?.as_i64()?;
    let b = sm_body(c)?;
    let vis = b.get("visible_to_players").and_then(|v| v.as_bool());
    match b.get("kind").and_then(|k| k.as_str()) {
        Some(kind @ ("audio" | "video")) => {
            let table = if kind == "audio" { "audio_clips" } else { "video_clips" };
            let cid = match b.get("id") {
                None | Some(Value::Null) => {
                    let url = b.get("url")?.as_str()?;
                    let url = take_chars(&strip(url), 512);
                    c.db.query_row(&format!("SELECT id FROM {table} WHERE world_id = ?1 AND file_url = ?2 LIMIT 1"), rusqlite::params![wid, url], |r| r.get(0)).ok()?
                }
                Some(v) if !v.is_boolean() => v.as_i64()?,
                _ => return None,
            };
            let clip = world_clip(c, table, wid, cid)?;
            if kind == "audio" {
                sm_attach_audio(c, &gs, &clip, vis)?;
            } else {
                let mut items = sm_items(&gs)?;
                if !items.iter().any(|i| i.get("kind").and_then(|k| k.as_str()) == Some("video") && i.get("clip_id").and_then(|v| v.as_i64()) == Some(cid)) {
                    if (sm_clip_ids(&gs)?.len() + items.len()) as i64 >= sm_limit(c)? {
                        return None;
                    }
                    let mut m = Map::new();
                    m.insert("uid".into(), json!(format!("{:010x}", rand::random::<u64>() & 0xff_ffff_ffff)));
                    m.insert("kind".into(), json!("video"));
                    m.insert("clip_id".into(), json!(cid));
                    items.push(m);
                    sm_store_media(c, ids[0], &items)?;
                }
                if let Some(v) = vis {
                    orm::update(&c.db, "video_clips", cid, &[("visible_to_players", Sql::Integer(v as i64))])?;
                }
            }
        }
        Some("image") => {
            let url = b.get("url")?.as_str()?.to_string();
            let names = crate::gallery::all_world_image_urls(&c.db, wid)?;
            let name = names.iter().find(|e| e.get("url").and_then(|u| u.as_str()) == Some(url.as_str()))?.get("name")?.clone();
            let caption = match name {
                Value::String(n) => take_chars(&n, 120),
                _ => return None,
            };
            let mut items = sm_items(&gs)?;
            if !items.iter().any(|i| i.get("kind").and_then(|k| k.as_str()) == Some("image") && i.get("url").and_then(|u| u.as_str()) == Some(url.as_str())) {
                if (sm_clip_ids(&gs)?.len() + items.len()) as i64 >= sm_limit(c)? {
                    return None;
                }
                let mut m = Map::new();
                m.insert("uid".into(), json!(format!("{:010x}", rand::random::<u64>() & 0xff_ffff_ffff)));
                m.insert("kind".into(), json!("image"));
                m.insert("url".into(), json!(url));
                m.insert("caption".into(), json!(caption));
                m.insert("visible".into(), json!(vis.unwrap_or(false)));
                m.insert("own".into(), json!(false));
                items.push(m);
                sm_store_media(c, ids[0], &items)?;
            }
        }
        _ => return None,
    }
    sm_panel(c, ids[0])
}

// ── uploads: chunked parts, reassembly, direct uploads (app/uploads.py, audio.py, video.py, pages.py) ──

/// `effective_upload_bytes(settings.<col>, int(os.environ.get(env, default)))`
fn upload_cap(c: &Ctx, col: &str, env: &str, default: i64) -> Option<i64> {
    let saved: Option<i64> = c.db.query_row(&format!("SELECT {col} FROM app_settings LIMIT 1"), [], |r| r.get(0)).ok()?;
    match saved.filter(|v| *v > 0) {
        Some(mb) => Some(mb * 1024 * 1024),
        None => env_bytes(env, default),
    }
}

/// `int(os.environ.get(env, default))`
fn env_bytes(env: &str, default: i64) -> Option<i64> {
    match std::env::var(env) {
        Ok(v) => py_int_text(&v).ok(),
        Err(_) => Some(default),
    }
}

/// `Path(filename)`'s (stem, suffix) for a plain file name (one with a "/", or Python's own idea of other paths: None)
fn py_path_parts(filename: &str) -> Option<(String, String)> {
    if filename.contains('/') || filename.contains('\0') {
        return None;
    }
    match filename.rfind('.') {
        Some(i) if i > 0 && i < filename.len() - 1 => Some((filename[..i].to_string(), filename[i..].to_string())),
        _ => Some((filename.to_string(), String::new())),
    }
}

/// `Path(filename).suffix.lower()` (a non-ASCII suffix: Python's lower())
fn py_suffix_lower(filename: &str) -> Option<String> {
    let (_, ext) = py_path_parts(filename)?;
    ext.is_ascii().then(|| ext.to_ascii_lowercase())
}

/// `unique_upload_filename(original, ext)`: "<12 hex>-<slug><ext>", or "<32 hex><ext>" when the name has nothing to slugify
fn unique_upload_filename(original: &str, ext: &str) -> Option<String> {
    let (stem, _) = py_path_parts(original)?;
    if stem.chars().any(|ch| matches!(ch, '\u{212a}' | '\u{130}' | '\u{17f}' | '\u{1e9e}')) {
        return None; // lower() turns these into ASCII letters (Kelvin sign, dotted I, ...): Python's
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in stem.chars().flat_map(|c| c.to_lowercase()) {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    let slug: String = slug.trim_matches('-').chars().take(60).collect();
    let hex = format!("{:032x}", rand::random::<u128>());
    Some(if slug.is_empty() { format!("{hex}{ext}") } else { format!("{}-{slug}{ext}", &hex[..12]) })
}

/// A required `int = Form(...)`: plain digits (pydantic's own reading of other text, and an empty value's 422: Python's)
fn form_int_required(f: &Form, k: &str) -> Option<i64> {
    py_int(f.get(k).filter(|v| !v.is_empty())?)
}

/// `sweep_stale_chunk_sessions(root)`: chunk folders untouched for a day go
fn sweep_chunks(root: &std::path::Path) {
    let Ok(rd) = std::fs::read_dir(root) else { return };
    let cutoff = SystemTime::now() - std::time::Duration::from_secs(24 * 60 * 60);
    for e in rd.flatten() {
        if let Ok(m) = e.metadata() {
            if m.is_dir() && m.modified().map_or(false, |t| t < cutoff) {
                let _ = std::fs::remove_dir_all(e.path());
            }
        }
    }
}

fn chunk_id_ok(id: &str) -> bool {
    id.len() == 32 && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The one file part named `field` (more than one, or none: Python's)
fn one_file<'a>(c: &'a Ctx, field: &str) -> Option<&'a crate::uploads::FileRef> {
    let up = c.upload.as_ref()?;
    let mut it = up.files.iter().filter(|p| p.name == field);
    let first = it.next()?;
    (it.next().is_none() && !up.form.has(field)).then_some(first)
}

/// `save_upload_chunk`: one part of a large file, stashed under <uploads>/<folder>/_chunks/<upload_id>/<index>.part
fn save_chunk(c: &mut Ctx, lib: &Lib, folder: &str, cap: i64) -> Option<Response<Body>> {
    lib_editor(c, lib)?;
    let f = c.form()?;
    let id = f.get("upload_id").filter(|v| !v.is_empty())?.to_string();
    let index = form_int_required(&f, "chunk_index")?;
    if !chunk_id_ok(&id) || !(0..=2000).contains(&index) {
        return None;
    }
    if one_file(c, "file")?.len as i64 > cap {
        return None; // the 413: Python's
    }
    let root = uploads_root(c)?.join(folder).join("_chunks");
    let dir = root.join(&id);
    if index == 0 && !dir.exists() {
        sweep_chunks(&root);
    }
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(format!("{index:06}.part"));
    if !dest.exists() {
        c.created.push(dest.clone());
    }
    let part = one_file(c, "file")?;
    c.upload.as_ref()?.copy_part(part, &dest)?;
    json_response(&json!({"ok": true}))
}

fn audio_chunk(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = upload_cap(c, "max_audio_mb", "MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?;
    save_chunk(c, &AUDIO, "audio", cap)
}

fn video_chunk(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = upload_cap(c, "max_video_mb", "MAX_VIDEO_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024)?;
    save_chunk(c, &VIDEO, "video", cap)
}

fn pages_chunk(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = env_bytes("MAX_PAGE_UPLOAD_BYTES", 50 * 1024 * 1024)?;
    save_chunk(c, &PAGES, "pages", cap)
}

/// The parts of a chunked upload, all present, and their total size (`verify_upload_chunks_present`)
fn chunk_parts(c: &Ctx, folder: &str, id: &str, total: i64) -> Option<(std::path::PathBuf, Vec<std::path::PathBuf>, u64)> {
    if !chunk_id_ok(id) || !(1..=2001).contains(&total) {
        return None;
    }
    let dir = uploads_root(c)?.join(folder).join("_chunks").join(id);
    let parts: Vec<std::path::PathBuf> = (0..total).map(|i| dir.join(format!("{i:06}.part"))).collect();
    let mut size = 0;
    for p in &parts {
        let m = std::fs::metadata(p).ok().filter(|m| m.is_file())?; // a missing part: the 400, Python's
        size += m.len();
    }
    Some((dir, parts, size))
}

/// Concatenate the parts into `dest`
fn reassemble(parts: &[std::path::PathBuf], dest: &std::path::Path) -> Option<()> {
    let mut out = std::fs::File::create(dest).ok()?;
    for p in parts {
        let mut src = std::fs::File::open(p).ok()?;
        std::io::copy(&mut src, &mut out).ok()?;
    }
    Some(())
}

/// Shared by the library uploads: the album a form names ("" / not digits = none; another world's: Python's 404)
fn form_album(c: &Ctx, lib: &Lib, wid: i64, f: &Form) -> Option<Option<i64>> {
    match digit_field(f.get("album_id").unwrap_or("")).ok()? {
        Some(aid) => Some(Some(lib_album(c, lib, wid, aid)?.get("id")?.as_i64()?)),
        None => Some(None),
    }
}

/// Count of a world's items in a library table
fn world_count(c: &Ctx, table: &str, wid: i64) -> Option<i64> {
    c.db.query_row(&format!("SELECT COUNT(*) FROM {table} WHERE world_id = ?1"), [wid], |r| r.get(0)).ok()
}

/// The name a new clip / page gets: the form's, else the file's stem, else `fallback`
fn item_name(f: &Form, filename: &str, fallback: &str) -> Option<String> {
    let n = take_chars(&strip(f.get("name").unwrap_or("")), 256);
    if !n.is_empty() {
        return Some(n);
    }
    let stem = take_chars(&py_path_parts(filename)?.0, 256);
    Some(if stem.is_empty() { fallback.to_string() } else { stem })
}

const AUDIO_EXTS: [&str; 9] = [".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"];
const PAGE_EXTS: [&str; 2] = [".html", ".htm"];

/// Write a new library item from a finished file at `dest` (already in place under <uploads>/<folder>)
fn add_library_item(c: &mut Ctx, lib: &Lib, wid: i64, f: &Form, filename: &str, file_name: &str, album: Option<i64>, with_entity: bool) -> Option<()> {
    let fallback = if lib.sheets { "Untitled page" } else { "Untitled clip" };
    let mut cols: Vec<(&str, Sql)> = vec![
        ("world_id", Sql::Integer(wid)),
        ("name", s(&item_name(f, filename, fallback)?)),
        ("description", s(&take_chars(&strip(f.get("description").unwrap_or("")), 512))),
        ("file_url", s(&format!("/uploads/{}/{file_name}", lib.section))),
        ("visible_to_players", Sql::Integer(flag_param(f, "visible_to_players"))),
        ("album_id", album.map_or(Sql::Null, Sql::Integer)),
    ];
    if lib.sheets {
        cols.push(("is_character_sheet_template", Sql::Integer(flag_param(f, "is_character_sheet_template"))));
    }
    if with_entity {
        let ent = match digit_field(f.get("entity_id").unwrap_or("")).ok()? {
            Some(eid) => {
                let w: Option<i64> = c.db.query_row("SELECT world_id FROM entities WHERE id = ?1", [eid], |r| r.get(0)).ok().flatten();
                if w == Some(wid) { Sql::Integer(eid) } else { Sql::Null }
            }
            None => Sql::Null,
        };
        cols.push(("entity_id", ent));
    }
    orm::insert(&c.db, lib.items, &cols)?;
    Some(())
}

/// The audio / pages `complete` routes: reassemble a chunked upload into the library
fn library_complete(c: &mut Ctx, lib: &Lib, exts: &[&str], max_items: i64, cap: i64) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = lib_editor(c, lib)?;
    let id = f.get("upload_id").filter(|v| !v.is_empty())?.to_string();
    let filename = f.get("filename").filter(|v| !v.is_empty())?.to_string();
    let total = form_int_required(&f, "total_chunks")?;
    let ext = py_suffix_lower(&filename)?;
    if !exts.contains(&ext.as_str()) || world_count(c, lib.items, wid)? >= max_items {
        return None;
    }
    let (dir, parts, size) = chunk_parts(c, lib.section, &id, total)?;
    if size as i64 > cap {
        return None; // the 413 (and its clean-up): Python's
    }
    let album = form_album(c, lib, wid, &f)?;
    let target = uploads_root(c)?.join(lib.section);
    std::fs::create_dir_all(&target).ok()?;
    let name = unique_upload_filename(&filename, &ext)?;
    let dest = target.join(&name);
    c.created.push(dest.clone());
    reassemble(&parts, &dest)?;
    add_library_item(c, lib, wid, &f, &filename, &name, album, false)?;
    c.removals.push(dir);
    json_response(&json!({"ok": true}))
}

fn audio_complete(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = upload_cap(c, "max_audio_mb", "MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?;
    library_complete(c, &AUDIO, &AUDIO_EXTS, 300, cap)
}

fn pages_complete(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = env_bytes("MAX_PAGE_UPLOAD_BYTES", 50 * 1024 * 1024)?;
    library_complete(c, &PAGES, &PAGE_EXTS, 200, cap)
}

/// The audio / pages direct uploads (one request, the file in it)
fn library_upload(c: &mut Ctx, lib: &Lib, exts: &[&str], max_items: i64, cap: i64, with_entity: bool) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = lib_editor(c, lib)?;
    if world_count(c, lib.items, wid)? >= max_items {
        return None;
    }
    let part = one_file(c, "file")?;
    if part.filename.is_empty() || part.len as i64 > cap {
        return None;
    }
    let filename = part.filename.clone();
    let ext = py_suffix_lower(&filename)?;
    if !exts.contains(&ext.as_str()) {
        return None;
    }
    let album = form_album(c, lib, wid, &f)?;
    let target = uploads_root(c)?.join(lib.section);
    std::fs::create_dir_all(&target).ok()?;
    let name = unique_upload_filename(&filename, &ext)?;
    let dest = target.join(&name);
    c.created.push(dest.clone());
    let part = one_file(c, "file")?;
    c.upload.as_ref()?.copy_part(part, &dest)?;
    add_library_item(c, lib, wid, &f, &filename, &name, album, with_entity)?;
    match album {
        Some(a) => redirect(&format!("{}/albums/{a}", lib.base)),
        None => redirect(lib.base),
    }
}

fn audio_upload(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = upload_cap(c, "max_audio_mb", "MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?;
    library_upload(c, &AUDIO, &AUDIO_EXTS, 300, cap, true)
}

fn pages_upload(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let cap = env_bytes("MAX_PAGE_UPLOAD_BYTES", 50 * 1024 * 1024)?;
    library_upload(c, &PAGES, &PAGE_EXTS, 200, cap, false)
}

// ── gallery uploads: pictures converted and thumbnailed (crate::imaging) ─────

const IMAGE_EXTS: [&str; 6] = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"];

/// `_effective_gallery_upload_bytes`: the saved limit, else max(MAX_UPLOAD_BYTES, MAX_GALLERY_UPLOAD_BYTES)
fn gallery_cap(c: &Ctx) -> Option<i64> {
    let saved: Option<i64> = c.db.query_row("SELECT max_gallery_upload_mb FROM app_settings LIMIT 1", [], |r| r.get(0)).ok()?;
    match saved.filter(|v| *v > 0) {
        Some(mb) => Some(mb * 1024 * 1024),
        None => Some(env_bytes("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)?.max(env_bytes("MAX_GALLERY_UPLOAD_BYTES", 500 * 1024 * 1024)?)),
    }
}

/// `_finalize_album_image(dest)`: convert + thumbnail; the url. Files are tracked for clean-up; the original goes once all is well.
fn finalize_picture(c: &mut Ctx, dest: &std::path::Path, folder: &str) -> Option<String> {
    let st: (Option<String>, Option<String>) =
        c.db.query_row("SELECT static_format, preview_format FROM app_settings LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    let static_format = st.0.unwrap_or_else(|| "avif".into());
    let preview = st.1.filter(|p| matches!(p.as_str(), "avif" | "webp" | "original")).unwrap_or_else(|| "avif".into());
    let done = crate::imaging::finish_upload(dest, &static_format, &preview)?;
    c.created.extend(done.written.iter().cloned());
    if let Some(r) = done.remove {
        c.removals.push(r);
    }
    Some(format!("/uploads/{folder}/{}", done.path.file_name()?.to_string_lossy()))
}

fn gallery_upload(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let wid = images_editor(c)?;
    let album = lib_album(c, &IMAGES, wid, ids[0])?;
    let cap = gallery_cap(c)?;
    let part = one_file(c, "file")?;
    if part.filename.is_empty() || part.len as i64 > cap {
        return None;
    }
    let filename = part.filename.clone();
    let ext = py_suffix_lower(&filename)?;
    if !IMAGE_EXTS.contains(&ext.as_str()) {
        return None;
    }
    let target = uploads_root(c)?.join("gallery");
    std::fs::create_dir_all(&target).ok()?;
    let dest = target.join(unique_upload_filename(&filename, &ext)?);
    c.created.push(dest.clone());
    let part = one_file(c, "file")?;
    c.upload.as_ref()?.copy_part(part, &dest)?;
    let url = finalize_picture(c, &dest, "gallery")?;
    let mut current = album_urls(&album)?;
    if !current.iter().any(|u| u.as_str() == Some(url.as_str())) && current.len() < 500 {
        current.push(json!(url));
    }
    orm::update(&c.db, "image_albums", ids[0], &[("image_urls_json", s(&nd_render::py_dumps(&Value::Array(current))))])?;
    redirect(&format!("/images/albums/{}", ids[0]))
}

fn gallery_chunk(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    images_editor(c)?;
    let cap = gallery_cap(c)?.min(128 * 1024 * 1024);
    let f = c.form()?;
    let id = f.get("upload_id").filter(|v| !v.is_empty())?.to_string();
    let index = form_int_required(&f, "chunk_index")?;
    if !chunk_id_ok(&id) || !(0..=2000).contains(&index) || one_file(c, "file")?.len as i64 > cap {
        return None;
    }
    let root = uploads_root(c)?.join("gallery").join("_chunks");
    let dir = root.join(&id);
    if index == 0 && !dir.exists() {
        sweep_chunks(&root);
    }
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(format!("{index:06}.part"));
    if !dest.exists() {
        c.created.push(dest.clone());
    }
    let part = one_file(c, "file")?;
    c.upload.as_ref()?.copy_part(part, &dest)?;
    json_response(&json!({"ok": true}))
}

fn gallery_complete(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = images_editor(c)?;
    let id = f.get("upload_id").filter(|v| !v.is_empty())?.to_string();
    let filename = f.get("filename").filter(|v| !v.is_empty())?.to_string();
    let total = form_int_required(&f, "total_chunks")?;
    let album_id = form_int_required(&f, "album_id")?;
    let ext = py_suffix_lower(&filename)?;
    if !IMAGE_EXTS.contains(&ext.as_str()) {
        return None;
    }
    let album = lib_album(c, &IMAGES, wid, album_id)?;
    let (dir, parts, size) = chunk_parts(c, "gallery", &id, total)?;
    if size as i64 > gallery_cap(c)? || album_urls(&album)?.len() >= 500 {
        return None; // the 413 / "Album is full": Python's
    }
    let target = uploads_root(c)?.join("gallery");
    std::fs::create_dir_all(&target).ok()?;
    let dest = target.join(unique_upload_filename(&filename, &ext)?);
    c.created.push(dest.clone());
    reassemble(&parts, &dest)?;
    let url = finalize_picture(c, &dest, "gallery")?;
    let mut current = album_urls(&album)?;
    current.push(json!(url));
    orm::update(&c.db, "image_albums", album_id, &[("image_urls_json", s(&nd_render::py_dumps(&Value::Array(current))))])?;
    c.removals.push(dir);
    json_response(&json!({"url": url}))
}

// ── picture inputs of ordinary forms (entity image, character portrait, calendar day icon) ──

/// A file part: in memory (a small request) or in the streamed body file (crate::uploads)
enum FilePick {
    Mem(crate::form::FilePart),
    Disk(usize),
}

impl FilePick {
    fn filename<'a>(&'a self, c: &'a Ctx) -> Option<&'a str> {
        match self {
            FilePick::Mem(p) => Some(&p.filename),
            FilePick::Disk(i) => Some(&c.upload.as_ref()?.files.get(*i)?.filename),
        }
    }
    fn len(&self, c: &Ctx) -> Option<u64> {
        match self {
            FilePick::Mem(p) => Some(p.data.len() as u64),
            FilePick::Disk(i) => Some(c.upload.as_ref()?.files.get(*i)?.len),
        }
    }
    fn write_to(&self, c: &Ctx, dest: &std::path::Path) -> Option<()> {
        match self {
            FilePick::Mem(p) => std::fs::write(dest, &p.data).ok(),
            FilePick::Disk(i) => {
                let up = c.upload.as_ref()?;
                up.copy_part(up.files.get(*i)?, dest)
            }
        }
    }
}

/// A form with one optional file input `field`: the fields, and the file when one was chosen (a part with no name and no bytes is
/// the browser's "no file"). Any other file part, or two of it: Python's.
fn form_and_file(c: &Ctx, field: &str) -> Option<(Form, Option<FilePick>)> {
    if let Some(up) = &c.upload {
        if up.form.has(field) || up.files.iter().any(|p| p.name != field) || up.files.len() > 1 {
            return None;
        }
        let pick = up.files.first().filter(|p| !(p.filename.is_empty() && p.len == 0)).map(|_| FilePick::Disk(0));
        return Some((up.form.clone(), pick));
    }
    if content_type(c.headers) == "multipart/form-data" {
        let (f, mut files) = parse_multipart(c.headers, c.body)?;
        if f.has(field) || files.iter().any(|p| p.name != field) || files.len() > 1 {
            return None;
        }
        let pick = files.pop().filter(|p| !(p.filename.is_empty() && p.len == 0)).map(FilePick::Mem);
        return Some((f, pick));
    }
    Some((c.form()?, None))
}

/// `effective_upload_bytes(settings.max_upload_mb, MAX_UPLOAD_BYTES)`
fn general_cap(c: &Ctx) -> Option<i64> {
    upload_cap(c, "max_upload_mb", "MAX_UPLOAD_BYTES", 20 * 1024 * 1024)
}

/// `save_upload(file, subdir)` / `_upload_portrait`: Some(None) for no usable file (no name, not a picture extension); the url of
/// the stored, converted (and, with `thumbnail`, thumbnailed) picture; None when the request is Python's (too big, a picture
/// crate::imaging leaves to Pillow)
fn save_picture(c: &mut Ctx, pick: &FilePick, subdir: &str, thumbnail: bool) -> Option<Option<String>> {
    let filename = pick.filename(c)?.to_string();
    if filename.is_empty() {
        return Some(None);
    }
    let ext = py_suffix_lower(&filename)?;
    if !IMAGE_EXTS.contains(&ext.as_str()) {
        return Some(None);
    }
    if pick.len(c)? as i64 > general_cap(c)? {
        return None;
    }
    let root = uploads_root(c)?;
    let dir = if subdir.is_empty() { root } else { root.join(subdir) };
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(unique_upload_filename(&filename, &ext)?);
    c.created.push(dest.clone());
    pick.write_to(c, &dest)?;
    let st: (Option<String>, Option<String>) =
        c.db.query_row("SELECT static_format, preview_format FROM app_settings LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
    let static_format = st.0.unwrap_or_else(|| "avif".into());
    let preview = st.1.filter(|p| matches!(p.as_str(), "avif" | "webp" | "original")).unwrap_or_else(|| "avif".into());
    let done = if thumbnail { crate::imaging::finish_upload(&dest, &static_format, &preview)? } else { crate::imaging::convert_upload(&dest, &static_format)? };
    c.created.extend(done.written.iter().cloned());
    if let Some(r) = done.remove {
        c.removals.push(r);
    }
    let name = done.path.file_name()?.to_string_lossy().to_string();
    Some(Some(if subdir.is_empty() { format!("/uploads/{name}") } else { format!("/uploads/{subdir}/{name}") }))
}

fn calendar_icon_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "file")?;
    let wid = c.world_id()?;
    if !can_manage_calendar(c)? {
        return None;
    }
    let pick = pick?; // File(...) is required
    let (_, days, _) = crate::calendar::for_write(&c.db, wid)?;
    let day = crate::calendar::clamp_day(&days, ids[0]);
    let ext = py_suffix_lower(pick.filename(c)?)?;
    if !IMAGE_EXTS.contains(&ext.as_str()) {
        return None;
    }
    let n: i64 = c.db.query_row("SELECT COUNT(*) FROM calendar_day_icons WHERE world_id = ?1 AND day = ?2", [wid, day], |r| r.get(0)).ok()?;
    if n >= 24 {
        return None;
    }
    let url = save_picture(c, &pick, "calendar_icons", false)??;
    let label = take_chars(&strip(f.get("label").unwrap_or("")), 120);
    let id = orm::insert(&c.db, "calendar_day_icons", &[("world_id", Sql::Integer(wid)), ("day", Sql::Integer(day)), ("image_url", s(&url)), ("label", s(&label))])?;
    json_response(&json!({"id": id, "day": day, "image_url": url, "label": label}))
}

// ── entity folders, templates and image (app/main.py) ────────────────────────

/// `re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max]` (None for the few letters lower() turns into ASCII: Python's)
fn py_slug(text: &str, max: usize) -> Option<String> {
    if text.chars().any(|ch| matches!(ch, '\u{212a}' | '\u{130}' | '\u{17f}' | '\u{1e9e}')) {
        return None;
    }
    let mut slug = String::new();
    let mut dash = false;
    for ch in text.chars().flat_map(|c| c.to_lowercase()) {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
            dash = false;
        } else if !dash {
            slug.push('-');
            dash = true;
        }
    }
    Some(slug.trim_matches('-').chars().take(max).collect())
}

fn folder_rename(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id()?;
    let kind = required(&f, "kind")?;
    let old = strip(&required(&f, "old_path")?);
    let new = strip(f.get("new_path").unwrap_or(""));
    if old.is_empty() {
        return None;
    }
    // the same SQL filter as Python's (LIKE: its wildcards and ASCII case-folding included)
    let mut st = c.db.prepare("SELECT id, folder FROM entities WHERE world_id = ?1 AND kind = ?2 AND (folder = ?3 OR folder LIKE ?4)").ok()?;
    let rows: Vec<(i64, Option<String>)> =
        st.query_map(rusqlite::params![wid, kind, old, format!("{old}/%")], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    drop(st);
    let n = old.chars().count();
    for (id, folder) in rows {
        let folder = folder.unwrap_or_default();
        let to = if new.is_empty() {
            Sql::Null
        } else if folder == old {
            s(&new)
        } else {
            s(&format!("{new}{}", folder.chars().skip(n).collect::<String>()))
        };
        orm::update(&c.db, "entities", id, &[("folder", to)])?;
    }
    let mut url = format!("/kind/{kind}");
    if !new.is_empty() {
        url += &format!("?folder={}", utf8_percent_encode(&new, QUOTE));
    }
    redirect(&url)
}

/// `effective_kinds(world)[0]` for a world row (None: the built-ins)
fn kinds_of(world: Option<&Map<String, Value>>) -> Option<Vec<String>> {
    nd_perm::sections::effective_kinds(world.and_then(|w| w.get("custom_kinds_json")).and_then(|v| v.as_str()))
}

/// `raw if json.loads(raw) else "[]"` for a fields_json form value (text serde refuses that json.loads may take: Python's)
fn fields_json(f: &Form) -> Option<String> {
    let raw = f.get("fields_json").filter(|v| !v.is_empty()).unwrap_or("[]");
    serde_json::from_str::<Value>(raw).ok()?;
    Some(raw.to_string())
}

fn entity_template_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let world = c.p.world.clone();
    let name = strip(f.get("name").unwrap_or(""));
    let name = if name.is_empty() { "Unnamed Template".to_string() } else { name };
    let desc = strip(f.get("description").unwrap_or(""));
    let kind = strip(f.get("kind").unwrap_or(""));
    let kind = (!kind.is_empty() && kinds_of(world.as_ref())?.contains(&kind)).then_some(kind);
    let raw = fields_json(&f)?;
    let base = py_slug(&name, 50)?;
    let base = if base.is_empty() { "template".to_string() } else { base };
    let (mut slug, mut n) = (base.clone(), 1);
    while c.db.query_row("SELECT COUNT(*) FROM entity_templates WHERE slug = ?1", [&slug], |r| r.get::<_, i64>(0)).ok()? > 0 {
        slug = format!("{base}-{n}");
        n += 1;
    }
    let id = orm::insert(
        &c.db,
        "entity_templates",
        &[
            ("world_id", world.as_ref().and_then(|w| w.get("id")?.as_i64()).map_or(Sql::Null, Sql::Integer)),
            ("name", s(&name)),
            ("slug", s(&slug)),
            ("kind", kind.map_or(Sql::Null, |k| s(&k))),
            ("description", s(&desc)),
            ("is_builtin", Sql::Integer(0)),
            ("fields_json", s(&raw)),
        ],
    )?;
    redirect(&format!("/entity-templates/{id}/edit"))
}

fn entity_template_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let tpl = row(&c.db, "SELECT * FROM entity_templates WHERE id = ?1", ids[0])?;
    let mut set: Vec<(&str, Sql)> = vec![];
    if !py_truthy(tpl.get("is_builtin")) {
        let old = tpl.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let name = strip(f.get("name").unwrap_or(&old));
        set.push(("name", s(if name.is_empty() { &old } else { &name })));
        set.push(("description", s(&strip(f.get("description").unwrap_or("")))));
        let kind = strip(f.get("kind").unwrap_or(""));
        let tpl_world = match tpl.get("world_id").and_then(|v| v.as_i64()).filter(|w| *w != 0) {
            Some(w) => row(&c.db, "SELECT * FROM worlds WHERE id = ?1", w),
            None => None,
        };
        let ok = !kind.is_empty() && kinds_of(tpl_world.as_ref())?.contains(&kind);
        set.push(("kind", if ok { s(&kind) } else { Sql::Null }));
    }
    set.push(("fields_json", s(&fields_json(&f)?)));
    orm::update(&c.db, "entity_templates", ids[0], &set)?;
    redirect(&format!("/entity-templates/{}/edit?saved=1", ids[0]))
}

fn entity_template_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let tpl = row(&c.db, "SELECT * FROM entity_templates WHERE id = ?1", ids[0])?;
    if py_truthy(tpl.get("is_builtin")) {
        return None;
    }
    orm::bulk_update(&c.db, "entities", &[("template_id", Sql::Null)], "template_id = ?", &[Sql::Integer(ids[0])])?;
    c.db.execute("DELETE FROM entity_templates WHERE id = ?1", [ids[0]]).ok()?;
    redirect("/entity-templates")
}

fn entity_set_image(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let url = strip(b.get("image_url")?.as_str()?);
    if !content_editor(c) || url.is_empty() {
        return None;
    }
    world_entity(c, ids[0])?;
    orm::update(&c.db, "entities", ids[0], &[("image_url", s(&url))])?;
    json_response(&json!({"ok": true}))
}

// ── races and professions (app/routers/races.py, professions.py) ─────────────

/// `races_new` / `professions_new`: an entity of `kind` with an optional picture under <uploads>/<folder>
fn lore_entry_new(c: &mut Ctx, kind: &str, folder: &str, back: &str) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "file")?;
    let wid = c.world_id()?;
    let name = strip(&required(&f, "name")?);
    let tier = opt(&f, "tier", "standard");
    let tier = if ["standard", "advanced", "exceptional"].contains(&tier) { tier.to_string() } else { "standard".to_string() };
    let image = match &pick {
        Some(p) => save_picture(c, p, folder, true)?,
        None => None,
    };
    let or_null = |v: String| if v.is_empty() { Sql::Null } else { s(&v) };
    orm::insert(
        &c.db,
        "entities",
        &[
            ("world_id", Sql::Integer(wid)),
            ("kind", s(kind)),
            ("subtype", s(&tier)),
            ("name", s(&name)),
            ("summary", or_null(strip(f.get("summary").unwrap_or("")))),
            ("body", or_null(strip(f.get("body").unwrap_or("")))),
            ("image_url", image.map_or(Sql::Null, |u| s(&u))),
        ],
    )?;
    redirect(back)
}

fn race_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    lore_entry_new(c, "race", "races", "/races")
}

fn profession_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    lore_entry_new(c, "profession", "professions", "/professions")
}

fn lore_entry_delete(c: &mut Ctx, id: i64, kind: &str, back: &str) -> Option<Response<Body>> {
    let e = world_entity(c, id)?;
    if e.get("kind").and_then(|v| v.as_str()) != Some(kind) {
        return None;
    }
    c.db.execute("DELETE FROM entity_relations WHERE source_id = ?1 OR target_id = ?1", [id]).ok()?;
    c.db.execute("DELETE FROM entities WHERE id = ?1", [id]).ok()?;
    redirect(back)
}

fn race_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lore_entry_delete(c, ids[0], "race", "/races")
}

fn profession_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    lore_entry_delete(c, ids[0], "profession", "/professions")
}

// ── saved layouts (app/view_prefs.py, app/routers/view_prefs_api.py) ─────────

/// A section's spec from app_data.json (view_prefs.SECTIONS)
fn view_section(section: &str) -> Option<&'static Map<String, Value>> {
    nd_render::app::data().get("view_sections")?.get(section)?.as_object()
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

/// Python's `str(v)` of a JSON scalar ("" can never be produced by a list / dict, which never match an allowed value)
fn py_str_value(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::Bool(b)) => (if *b { "True" } else { "False" }).into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => i.to_string(),
            None => n.as_f64().map(nd_render::py_float_repr).unwrap_or_default(),
        },
        Some(_) => "\u{0}".into(),
    }
}

fn id_ok(x: &str) -> bool {
    (1..=40).contains(&x.len()) && x.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

/// `view_prefs.clean(section, raw)`; None for an unknown section or a value this port reads differently
fn view_clean(section: &str, raw: Option<&Value>) -> Option<Value> {
    let s = view_section(section)?;
    let empty = Map::new();
    let raw = raw.and_then(|v| v.as_object()).unwrap_or(&empty);
    let modes = str_list(s.get("modes"));
    let mode = raw.get("mode").and_then(|v| v.as_str()).filter(|m| modes.iter().any(|x| x == m)).map(str::to_string)
        .unwrap_or_else(|| s.get("default").and_then(|v| v.as_str()).unwrap_or("").to_string());
    let size = match raw.get("size").map(py_int_value) {
        Some(Ok(n)) => n,
        Some(Err(PyErr::Unsure)) => return None,
        _ => 2,
    };
    let size = if (1..=3).contains(&size) { size } else { 2 };
    let sorts = str_list(s.get("sorts"));
    let default_sort = s.get("default_sort").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let sort = match raw.get("sort") {
        Some(Value::String(x)) => {
            let (desc, key) = match x.strip_prefix('-') { Some(k) => (true, k), None => (false, x.as_str()) };
            if sorts.iter().any(|k| k == key) { format!("{}{key}", if desc { "-" } else { "" }) } else { default_sort }
        }
        _ => default_sort, // str() of anything else is never a sort key
    };
    let groups = str_list(s.get("groups"));
    let group = match raw.get("group") {
        Some(Value::String(g)) if groups.iter().any(|x| x == g) => g.clone(),
        Some(Value::String(g)) if g.is_empty() => String::new(),
        _ => s.get("default_group").and_then(|v| v.as_str()).unwrap_or("").to_string(),
    };
    let allowed_filters = str_list(s.get("filters"));
    let mut filters: Vec<String> = vec![];
    for f in str_list(raw.get("filters")) {
        if allowed_filters.contains(&f) && !filters.contains(&f) {
            filters.push(f);
        }
    }
    if let Some(cap) = s.get("max_filters").and_then(|v| v.as_u64()) {
        filters.truncate(cap as usize);
    }
    let mut out = Map::new();
    out.insert("mode".into(), json!(mode));
    out.insert("size".into(), json!(size));
    out.insert("sort".into(), json!(sort));
    out.insert("group".into(), json!(group));
    out.insert("filters".into(), json!(filters));
    let ids = |v: Option<&Value>, cap: usize| -> Vec<String> {
        let mut got: Vec<String> = vec![];
        for x in v.and_then(|x| x.as_array()).cloned().unwrap_or_default() {
            if let Some(x) = x.as_str().filter(|x| id_ok(x)) {
                if !got.iter().any(|g| g == x) {
                    got.push(x.to_string());
                }
            }
        }
        got.truncate(cap);
        got
    };
    if let Some(order) = nd_render::app::data().get("view_lists_order")?.get(section).and_then(|v| v.as_array()) {
        for name in order.iter().filter_map(|v| v.as_str()) {
            let cap = s.get("lists")?.get(name)?.as_u64()? as usize;
            out.insert(name.into(), json!(ids(raw.get(name), cap)));
        }
    }
    if let Some(order) = nd_render::app::data().get("view_options_order")?.get(section).and_then(|v| v.as_array()) {
        let given = raw.get("options").and_then(|v| v.as_object()).unwrap_or(&empty);
        let mut opts = Map::new();
        for k in order.iter().filter_map(|v| v.as_str()) {
            let allowed = str_list(s.get("options")?.get(k));
            let v = py_str_value(given.get(k));
            opts.insert(k.into(), json!(if allowed.contains(&v) { v } else { allowed.first()?.clone() }));
        }
        out.insert("options".into(), Value::Object(opts));
        out.insert("pins".into(), json!(ids(raw.get("pins"), s.get("max_pins").and_then(|v| v.as_u64()).unwrap_or(6) as usize)));
    }
    Some(Value::Object(out))
}

fn view_pref_save(c: &mut Ctx, section: &str) -> Option<Response<Body>> {
    if section == "site" {
        return None; // remembered in the session cookie too (site_layout.remember): Python's
    }
    let raw: Value = serde_json::from_slice(c.body).ok().filter(|_| plain_numbers(c.body))?;
    let cleaned = view_clean(section, Some(&raw))?;
    let text = nd_render::py_dumps(&cleaned);
    let row: Option<i64> = c.db.query_row("SELECT id FROM user_view_prefs WHERE user_id = ?1 AND section = ?2 LIMIT 1", rusqlite::params![c.p.user_id, section], |r| r.get(0)).ok();
    match row {
        Some(id) => {
            orm::update(&c.db, "user_view_prefs", id, &[("prefs_json", s(&text))])?;
        }
        None => {
            orm::insert(&c.db, "user_view_prefs", &[("user_id", Sql::Integer(c.p.user_id)), ("section", s(section)), ("prefs_json", s(&text))])?;
        }
    }
    json_response(&cleaned)
}

fn view_pref_reset(c: &mut Ctx, section: &str) -> Option<Response<Body>> {
    if section == "site" || view_section(section).is_none() {
        return None;
    }
    c.db.execute("DELETE FROM user_view_prefs WHERE user_id = ?1 AND section = ?2", rusqlite::params![c.p.user_id, section]).ok()?;
    json_response(&json!({"reset": section}))
}

/// `_named(db, user_id, section)`: the person's saved views of a section, by name
fn named_views(c: &Ctx, section: &str) -> Option<Value> {
    let mut st = c.db.prepare("SELECT id, name, prefs_json FROM saved_views WHERE user_id = ?1 AND section = ?2 ORDER BY name").ok()?;
    let rows: Vec<(i64, Option<String>, Option<String>)> =
        st.query_map(rusqlite::params![c.p.user_id, section], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut out = vec![];
    for (id, name, raw) in rows {
        let state = match serde_json::from_str::<Value>(raw.as_deref().filter(|s| !s.is_empty()).unwrap_or("{}")) {
            Ok(v) => v,
            Err(_) => return None, // json.loads' own reading of bad text: Python's
        };
        out.push(json!({"id": id, "name": name, "state": view_clean(section, Some(&state))?}));
    }
    Some(json!({"views": out}))
}

fn saved_view_save(c: &mut Ctx, section: &str) -> Option<Response<Body>> {
    view_section(section)?;
    let b = c.json()?;
    let name = match b.get("name") {
        Some(Value::String(n)) => take_chars(&nd_render::text::py_split_join(n), 40),
        Some(v) if py_truthy(Some(v)) => return None,
        _ => String::new(),
    };
    if name.is_empty() {
        return None;
    }
    let cleaned = view_clean(section, b.get("state"))?;
    let text = nd_render::py_dumps(&cleaned);
    let row: Option<i64> = c
        .db
        .query_row("SELECT id FROM saved_views WHERE user_id = ?1 AND section = ?2 AND name = ?3 LIMIT 1", rusqlite::params![c.p.user_id, section, name], |r| r.get(0))
        .ok();
    match row {
        Some(id) => {
            orm::update(&c.db, "saved_views", id, &[("prefs_json", s(&text))])?;
        }
        None => {
            let n: i64 = c.db.query_row("SELECT COUNT(*) FROM saved_views WHERE user_id = ?1 AND section = ?2", rusqlite::params![c.p.user_id, section], |r| r.get(0)).ok()?;
            if n >= 20 {
                return None;
            }
            orm::insert(&c.db, "saved_views", &[("user_id", Sql::Integer(c.p.user_id)), ("section", s(section)), ("name", s(&name)), ("prefs_json", s(&text))])?;
        }
    }
    json_response(&named_views(c, section)?)
}

fn saved_view_delete(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let (section, id) = key.split_once('/')?;
    let id: i64 = id.parse().ok()?;
    let n = c.db.execute("DELETE FROM saved_views WHERE id = ?1 AND user_id = ?2 AND section = ?3", rusqlite::params![id, c.p.user_id, section]).ok()?;
    if n == 0 {
        return None;
    }
    json_response(&named_views(c, section)?)
}

// ── the home page's content (app/routers/home_content.py) ────────────────────

const HOME_LINK_TYPES: [&str; 8] = ["entity", "session", "quest", "board", "schematic", "map", "kind", "url"];

/// `str(raw.get(k, ""))` for text; anything else (str() of a number, a list...) is Python's
fn text_of(m: &Map<String, Value>, k: &str) -> Option<String> {
    match m.get(k) {
        None => Some(String::new()),
        Some(Value::String(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_sanitize_link(raw, kinds)`: Some(None) = dropped; None = Python's
fn home_link(raw: &Value, kinds: &[String]) -> Option<Option<Value>> {
    let Some(m) = raw.as_object() else { return Some(None) };
    let label = take_chars(&strip(&text_of(m, "label")?), 200);
    let tt = strip(&text_of(m, "target_type")?);
    let tr = strip(&text_of(m, "target_ref")?);
    if label.is_empty() || !HOME_LINK_TYPES.contains(&tt.as_str()) || tr.is_empty() {
        return Some(None);
    }
    if tt == "url" && !(tr.starts_with("http://") || tr.starts_with("https://") || tr.starts_with('/')) {
        return Some(None);
    }
    if tt == "kind" && !kinds.contains(&tr) {
        return Some(None);
    }
    Some(Some(json!({"label": label, "icon": take_chars(&strip(&text_of(m, "icon")?), 32), "target_type": tt, "target_ref": tr,
                     "visible_to_players": m.get("visible_to_players").map_or(true, |v| py_truthy(Some(v)))})))
}

/// `json.loads(raw or "[]")` for the sanitizers: bad JSON is [] there; text serde refuses but json.loads may take is Python's
fn json_list_or_empty(raw: &str) -> Option<Vec<Value>> {
    let raw = if raw.is_empty() { "[]" } else { raw };
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => Some(a),
        Ok(_) => Some(vec![]),
        Err(_) => None,
    }
}

/// `_sanitize_sections(raw, kinds)`
fn home_sections(raw: &str, kinds: &[String]) -> Option<Vec<Value>> {
    let mut out = vec![];
    for sec in json_list_or_empty(raw)?.iter().take(20) {
        let Some(sm) = sec.as_object() else { continue };
        let name = take_chars(&strip(&text_of(sm, "name")?), 100);
        let links = match sm.get("links") {
            None | Some(Value::Null) => vec![],
            Some(v) if !py_truthy(Some(v)) => vec![],
            Some(Value::Array(a)) => a.clone(),
            Some(Value::String(_)) => vec![], // a string's characters are no links
            _ => return None,
        };
        let mut kept = vec![];
        for l in links.iter().take(50) {
            if let Some(v) = home_link(l, kinds)? {
                kept.push(v);
            }
        }
        out.push(json!({"name": if name.is_empty() { "Untitled".to_string() } else { name },
                        "visible_to_players": sm.get("visible_to_players").map_or(true, |v| py_truthy(Some(v))), "links": kept}));
    }
    Some(out)
}

/// `_sanitize_pinned_tiles(raw, kinds)`
fn home_tiles(raw: &str, kinds: &[String]) -> Option<Vec<Value>> {
    let mut out = vec![];
    for l in json_list_or_empty(raw)?.iter().take(24) {
        if let Some(v) = home_link(l, kinds)? {
            out.push(v);
        }
    }
    Some(out)
}

/// `_sanitize_hidden_kinds(raw, kinds)`
fn home_hidden(raw: &str, kinds: &[String]) -> Option<Vec<String>> {
    let mut seen: Vec<String> = vec![];
    for k in json_list_or_empty(raw)? {
        if let Some(k) = k.as_str() {
            if kinds.iter().any(|x| x == k) && !seen.iter().any(|x| x == k) {
                seen.push(k.to_string());
            }
        }
    }
    Some(seen)
}

/// The world of a home route: the active one (another world's: Python's) and its kinds
fn home_world(c: &Ctx, id: i64) -> Option<(Map<String, Value>, Vec<String>)> {
    if Some(id) != c.world_id() {
        return None;
    }
    let w = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", id)?;
    let kinds = kinds_of(Some(&w))?;
    Some((w, kinds))
}

fn world_text(w: &Map<String, Value>, k: &str) -> String {
    w.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn home_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "home_background_file")?;
    let (_, kinds) = home_world(c, ids[0])?;
    let or_null = |v: String| if v.is_empty() { Sql::Null } else { s(&v) };
    let mut set: Vec<(&str, Sql)> = vec![
        ("home_welcome_md", or_null(strip(f.get("home_welcome_md").unwrap_or("")))),
        ("home_sections_json", s(&nd_render::py_dumps(&json!(home_sections(f.get("home_sections_json").unwrap_or("[]"), &kinds)?)))),
        ("home_pinned_tiles_json", s(&nd_render::py_dumps(&json!(home_tiles(f.get("home_pinned_tiles_json").unwrap_or("[]"), &kinds)?)))),
        ("home_hidden_kinds_json", s(&nd_render::py_dumps(&json!(home_hidden(f.get("home_hidden_kinds_json").unwrap_or("[]"), &kinds)?)))),
        ("home_title", or_null(take_chars(&strip(f.get("home_title").unwrap_or("")), 200))),
        ("home_subtitle", or_null(take_chars(&strip(f.get("home_subtitle").unwrap_or("")), 300))),
    ];
    let uploaded = match &pick {
        Some(p) => save_picture(c, p, "home", false)?,
        None => None,
    };
    if let Some(u) = uploaded {
        set.push(("home_background_url", s(&u)));
    } else if !strip(f.get("remove_home_background").unwrap_or("")).is_empty() {
        set.push(("home_background_url", Sql::Null));
    } else {
        let pasted = take_chars(&strip(f.get("home_background_url").unwrap_or("")), 512);
        if !pasted.is_empty() && (pasted.starts_with("http://") || pasted.starts_with("https://") || pasted.starts_with('/')) {
            set.push(("home_background_url", s(&pasted)));
        }
    }
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect("/")
}

/// The link a quick-link / pinned-tile request describes (`_sanitize_link` of its label, type and ref)
fn posted_link(b: &Map<String, Value>, kinds: &[String]) -> Option<Option<Value>> {
    let mut m = Map::new();
    for k in ["label", "target_type", "target_ref"] {
        m.insert(k.into(), b.get(k).cloned().unwrap_or(json!("")));
    }
    m.insert("icon".into(), json!(""));
    m.insert("visible_to_players".into(), json!(true));
    home_link(&Value::Object(m), kinds)
}

fn home_quick_link(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (w, kinds) = home_world(c, ids[0])?;
    let link = posted_link(&b, &kinds)??; // an invalid link's 400: Python's
    let mut sections = home_sections(&world_text(&w, "home_sections_json"), &kinds)?;
    let idx = match b.get("section_index").and_then(|v| v.as_i64()).filter(|_| !b.get("section_index").is_some_and(|v| v.is_boolean())) {
        Some(i) if (0..sections.len() as i64).contains(&i) => i as usize,
        _ => {
            if sections.is_empty() {
                sections.push(json!({"name": "Quick Links", "visible_to_players": true, "links": []}));
            }
            0
        }
    };
    if b.get("section_index").is_some_and(|v| v.is_boolean()) {
        return None; // True is section 1 to Python's isinstance(x, int)
    }
    let links = sections[idx].get_mut("links")?.as_array_mut()?;
    if links.len() >= 50 {
        return None;
    }
    links.push(link);
    orm::update(&c.db, "worlds", ids[0], &[("home_sections_json", s(&nd_render::py_dumps(&json!(sections))))])?;
    json_response(&json!({"ok": true, "section_index": idx}))
}

fn home_pin_tile(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (w, kinds) = home_world(c, ids[0])?;
    let tile = posted_link(&b, &kinds)??;
    let mut tiles = home_tiles(&world_text(&w, "home_pinned_tiles_json"), &kinds)?;
    if tiles.len() >= 24 {
        return None;
    }
    tiles.push(tile.clone());
    orm::update(&c.db, "worlds", ids[0], &[("home_pinned_tiles_json", s(&nd_render::py_dumps(&json!(tiles))))])?;
    json_response(&json!({"ok": true, "tile": tile}))
}

fn home_unpin_tile(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (w, kinds) = home_world(c, ids[0])?;
    let index = b.get("index").filter(|v| !v.is_boolean())?.as_i64()?;
    let mut tiles = home_tiles(&world_text(&w, "home_pinned_tiles_json"), &kinds)?;
    if !(0..tiles.len() as i64).contains(&index) {
        return None;
    }
    tiles.remove(index as usize);
    orm::update(&c.db, "worlds", ids[0], &[("home_pinned_tiles_json", s(&nd_render::py_dumps(&json!(tiles))))])?;
    json_response(&json!({"ok": true}))
}

fn home_hide_kind(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let (w, kinds) = home_world(c, ids[0])?;
    let kind = b.get("kind")?.as_str()?.to_string();
    if !kinds.contains(&kind) {
        return None;
    }
    let mut hidden = home_hidden(&world_text(&w, "home_hidden_kinds_json"), &kinds)?;
    if !hidden.contains(&kind) {
        hidden.push(kind);
    }
    orm::update(&c.db, "worlds", ids[0], &[("home_hidden_kinds_json", s(&nd_render::py_dumps(&json!(hidden))))])?;
    json_response(&json!({"ok": true}))
}

// ── the account page (app/routers/account.py): the plain parts ───────────────

fn account_name(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let name = strip(f.get("display_name").unwrap_or(""));
    if name.is_empty() || name.chars().count() > 256 {
        return None; // the page again with its error: Python's
    }
    orm::update(&c.db, "users", c.p.user_id, &[("display_name", s(&name))])?;
    redirect("/account?saved=name")
}

/// Delete one of the viewer's own rows of `table` (another's, or none: nothing happens), then back to /account
fn account_revoke(c: &mut Ctx, table: &str, id: i64) -> Option<Response<Body>> {
    c.db.execute(&format!("DELETE FROM {table} WHERE id = ?1 AND user_id = ?2"), [id, c.p.user_id]).ok()?;
    redirect("/account")
}

fn account_token_revoke(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    account_revoke(c, "api_tokens", ids[0])
}

fn account_device_revoke(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    account_revoke(c, "trusted_devices", ids[0])
}

// ── world settings (app/main.py world_edit_post) ─────────────────────────────

/// `text.strip().isdigit()` then int(): Some(n) / None (not digits); Err for text whose isdigit() is Python's
fn digits_or_none(v: &str) -> Result<Option<i64>, ()> {
    digit_field(v)
}

fn world_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let w = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", ids[0])?;
    let name = required(&f, "name")?;
    let flag = |k: &str| Sql::Integer(flag_param(&f, k));
    let wtext = |k: &str| w.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let mut set: Vec<(&str, Sql)> = vec![];
    let name = strip(&name);
    set.push(("name", if name.is_empty() { w.get("name").and_then(|v| v.as_str()).map_or(Sql::Null, s) } else { s(&name) }));
    set.push(("description", s(f.get("description").unwrap_or(""))));
    // _sanitize_accent: a #rgb / #rrggbb colour, else the saved one
    let accent = strip(opt(&f, "accent", "#00f0ff"));
    let b = accent.as_bytes();
    let hex = (b.len() == 4 || b.len() == 7) && b[0] == b'#' && b[1..].iter().all(|x| x.is_ascii_hexdigit());
    set.push(("accent", if hex { s(&accent) } else { wtext("accent").map_or(Sql::Null, |a| s(&a)) }));
    // _sanitize_font: "" clears it; a value outside [A-Za-z0-9 ,'-]{1,120} keeps the saved one
    let font = strip(f.get("font").unwrap_or(""));
    let font_ok = font.chars().count() <= 120 && font.bytes().all(|x| x.is_ascii_alphanumeric() || b" ,'-".contains(&x));
    set.push(("font", if font.is_empty() { Sql::Null } else if font_ok { s(&font) } else { wtext("font").map_or(Sql::Null, |v| s(&v)) }));
    // _sanitize_font_size: int() of the value, clamped to 75..150; not a number keeps the saved one (or 100)
    let saved_size = w.get("font_size").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(100);
    let size = match py_int_text(opt(&f, "font_size", "100")) {
        Ok(n) => n.clamp(75, 150),
        Err(PyErr::Value) => saved_size,
        Err(_) => return None,
    };
    set.push(("font_size", Sql::Integer(size)));
    set.push(("players_see_party", flag("players_see_party")));
    if let Some(raw) = f.get("max_characters_per_player").filter(|v| !v.is_empty()) {
        let n = match digits_or_none(raw).ok()? {
            Some(n) => n.clamp(1, 20),
            None => 1,
        };
        set.push(("max_characters_per_player", Sql::Integer(n)));
    }
    for k in ["players_can_download_rules", "players_can_download_entities", "players_can_ask_ai", "players_can_use_ai_chat",
              "players_can_view_world_summary", "players_can_use_image_gen"] {
        set.push((k, flag(k)));
    }
    for k in ["ai_chat_rag_entity_limit", "ai_chat_rag_notes_limit"] {
        let v = digits_or_none(f.get(k).unwrap_or("")).ok()?;
        set.push((k, v.map_or(Sql::Null, Sql::Integer)));
    }
    let vault = strip(f.get("obsidian_vault_path").unwrap_or(""));
    set.push(("obsidian_vault_path", if vault.is_empty() { Sql::Null } else { s(&vault) }));
    let hero = opt(&f, "hero_style", "home");
    if ["off", "home", "everywhere"].contains(&hero) {
        set.push(("hero_style", s(hero)));
    }
    if let Some(p) = f.get("layout_preset").filter(|v| !v.is_empty()) {
        let presets = str_list(nd_render::app::data().get("view_presets"));
        set.push(("layout_preset", if presets.iter().any(|x| x == p) { s(p) } else { Sql::Null }));
    }
    for (k, col) in [("personal_images_max", "personal_images_max"), ("personal_audio_max", "personal_audio_max"), ("personal_video_max", "personal_video_max")] {
        if let Some(raw) = f.get(k).filter(|v| !v.is_empty()) {
            let v = match py_int_text(raw) {
                Ok(n) => Sql::Integer(n.clamp(0, 500)),
                Err(PyErr::Value) => Sql::Null,
                Err(_) => return None,
            };
            set.push((col, v));
        }
    }
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect("/worlds")
}

// ── world invites and members (app/main.py) ──────────────────────────────────

fn invite_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    let days = digit_field(f.get("expires_days").unwrap_or("")).ok()?.filter(|d| *d > 0);
    let expires = match days {
        Some(d) if d > 2_900_000 => return None, // past year 9999: Python's OverflowError
        Some(d) => s(&orm::stamp_after(d * 86_400)),
        None => Sql::Null,
    };
    let max_uses = digit_field(f.get("max_uses").unwrap_or("")).ok()?.filter(|n| *n > 0);
    use base64::Engine;
    let code = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(rand::random::<[u8; 9]>());
    orm::insert(
        &c.db,
        "invite_codes",
        &[("code", s(&code)), ("world_id", Sql::Integer(ids[0])), ("created_by_id", Sql::Integer(c.p.user_id)), ("expires_at", expires),
          ("max_uses", max_uses.map_or(Sql::Null, Sql::Integer))],
    )?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn invite_revoke(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let inv = row(&c.db, "SELECT * FROM invite_codes WHERE id = ?1", ids[1])?;
    if inv.get("world_id")?.as_i64() != Some(ids[0]) {
        return None;
    }
    orm::update(&c.db, "invite_codes", ids[1], &[("revoked", Sql::Integer(1))])?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn membership(c: &Ctx, world: i64, user: i64) -> Option<i64> {
    c.db.query_row("SELECT id FROM world_memberships WHERE world_id = ?1 AND user_id = ?2 LIMIT 1", [world, user], |r| r.get(0)).ok()
}

fn member_remove(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    if let Some(m) = membership(c, ids[0], ids[1]) {
        c.db.execute("DELETE FROM world_memberships WHERE id = ?1", [m]).ok()?;
    }
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn member_role(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let role = required(&f, "role")?;
    if !["player", "assistant", "owner"].contains(&role.as_str()) || (role == "owner" && !c.p.is_gm) {
        return None;
    }
    let m = membership(c, ids[0], ids[1])?;
    orm::update(&c.db, "world_memberships", m, &[("role", s(&role))])?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn member_clear_2fa(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    membership(c, ids[0], ids[1])?;
    let user = row(&c.db, "SELECT * FROM users WHERE id = ?1", ids[1])?;
    let version = user.get("session_version")?.as_i64()?;
    orm::update(
        &c.db,
        "users",
        ids[1],
        &[("totp_enabled", Sql::Integer(0)), ("totp_secret", Sql::Null), ("totp_backup_codes_json", s("[]")), ("session_version", Sql::Integer(version + 1))],
    )?;
    c.db.execute("DELETE FROM trusted_devices WHERE user_id = ?1", [ids[1]]).ok()?;
    redirect(&format!("/worlds/{}/edit?twofa_cleared={}", ids[0], ids[1]))
}

// ── GM <-> player private notes, world themes (app/main.py) ──────────────────

fn private_note_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let content = f.get("content").filter(|v| !v.is_empty())?.to_string();
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    row(&c.db, "SELECT id FROM users WHERE id = ?1", ids[1])?;
    orm::insert(
        &c.db,
        "private_notes",
        &[("world_id", Sql::Integer(ids[0])), ("player_user_id", Sql::Integer(ids[1])), ("author_id", Sql::Integer(c.p.user_id)),
          ("title", s(&strip(f.get("title").unwrap_or("")))), ("content", s(&content))],
    )?;
    redirect(&format!("/worlds/{}/notes/{}", ids[0], ids[1]))
}

fn private_note_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    c.db.execute("DELETE FROM private_notes WHERE id = ?1 AND world_id = ?2 AND player_user_id = ?3", [ids[2], ids[0], ids[1]]).ok()?;
    redirect(&format!("/worlds/{}/notes/{}", ids[0], ids[1]))
}

fn theme_clear(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    orm::update(&c.db, "worlds", ids[0], &[("theme_json", Sql::Null)])?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn theme_preset(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let preset = required(&f, "preset")?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    let pairs = nd_render::app::data().get("theme_presets_clean")?.as_array()?.iter().find(|p| p.get(0).and_then(|k| k.as_str()) == Some(preset.as_str()))?.get(1)?.as_array()?;
    let mut theme = Map::new();
    let mut set: Vec<(&str, Sql)> = vec![];
    for pair in pairs {
        let (k, v) = (pair.get(0)?.as_str()?, pair.get(1)?);
        if k == "accent" {
            set.push(("accent", s(v.as_str()?)));
        } else {
            theme.insert(k.to_string(), v.clone());
        }
    }
    set.push(("theme_json", s(&nd_render::py_dumps(&Value::Object(theme)))));
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

/// `str(v or "")` for a theme field: a string as is, a falsy value as "", anything else (Python's repr of it) is not ported
fn theme_text(v: Option<&Value>) -> Result<String, ()> {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(String::new()),
        Some(Value::String(x)) => Ok(x.clone()),
        Some(Value::Array(a)) if a.is_empty() => Ok(String::new()),
        Some(Value::Object(o)) if o.is_empty() => Ok(String::new()),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => Ok(String::new()),
        _ => Err(()),
    }
}

/// main._sanitize_theme: the whitelisted fields, in Python's order
fn sanitize_theme(data: &Map<String, Value>) -> Option<Map<String, Value>> {
    use std::sync::OnceLock;
    static RES: OnceLock<[regex::Regex; 4]> = OnceLock::new();
    let [hex, font, gf_re, ls_re] = RES.get_or_init(|| [
        regex::Regex::new(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$").unwrap(),
        regex::Regex::new(r"^[A-Za-z0-9 ,'\-]{1,120}$").unwrap(),
        regex::Regex::new(r"^https://fonts\.googleapis\.com/css2\?[A-Za-z0-9:;,._=&+%/@\-]+$").unwrap(),
        regex::Regex::new(r"^-?\d*\.?\d+(em|rem|px)$|^normal$").unwrap(),
    ]);
    let get = |k: &str| theme_text(data.get(k)).ok().map(|t| strip(&t));
    let mut out = Map::new();
    for k in ["accent", "bg", "bg2", "bg3", "border", "neon2", "neon3", "yellow", "text", "text_dim", "hero_glow_color"] {
        let v = get(k)?;
        if !v.is_empty() && hex.is_match(&v) {
            out.insert(k.into(), Value::String(v));
        }
    }
    for k in ["font", "font_heading", "font_display"] {
        let v = get(k)?;
        if !v.is_empty() && font.is_match(&v) {
            out.insert(k.into(), Value::String(v));
        }
    }
    let gf = get("google_fonts_url")?;
    if gf_re.is_match(&gf) {
        out.insert("google_fonts_url".into(), Value::String(gf));
    }
    let ls = get("hero_letter_spacing")?;
    if !ls.is_empty() && ls_re.is_match(&ls) {
        out.insert("hero_letter_spacing".into(), Value::String(ls));
    }
    let graphic = get("hero_graphic")?;
    if !graphic.is_ascii() {
        return None; // str.lower() on other scripts: Python's
    }
    let graphic = graphic.to_ascii_lowercase();
    if ["moon", "circle", "none"].contains(&graphic.as_str()) {
        out.insert("hero_graphic".into(), Value::String(graphic));
    }
    let name = get("name")?;
    if !name.is_empty() {
        out.insert("name".into(), Value::String(take_chars(&name, 120)));
    }
    Some(out)
}

fn theme_import(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    if content_type(c.headers) != "multipart/form-data" {
        return None;
    }
    let (f, files) = parse_multipart(c.headers, c.body)?;
    if f.has("file") {
        return None;
    }
    let file = files.iter().rev().find(|p| p.name == "file")?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    let text = std::str::from_utf8(&file.data).ok().filter(|t| !t.starts_with('\u{feff}'))?;
    let Value::Object(data) = serde_json::from_str::<Value>(text).ok()? else { return None };
    let mut cleaned = sanitize_theme(&data)?;
    if cleaned.is_empty() {
        return None;
    }
    let mut set: Vec<(&str, Sql)> = vec![];
    if let Some(Value::String(a)) = cleaned.shift_remove("accent") {
        set.push(("accent", s(&a)));
    }
    set.push(("theme_json", s(&nd_render::py_dumps(&Value::Object(cleaned)))));
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn world_create(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let name = f.get("name")?.to_string();
    // an empty or non-ASCII name (str.lower/isalnum over other scripts, a cookie value SimpleCookie quotes): Python's
    if name.is_empty() || !name.is_ascii() {
        return None;
    }
    let slug: String = name.to_ascii_lowercase().replace(' ', "-").replace('&', "and").chars().filter(|ch| ch.is_ascii_alphanumeric() || *ch == '-').collect();
    if slug.is_empty() {
        return None;
    }
    let description = f.get("description").unwrap_or("");
    let accent = strip(f.get("accent").unwrap_or("#00f0ff"));
    let hex = regex::Regex::new(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$").ok()?;
    let accent = if hex.is_match(&accent) { accent } else { "#00f0ff".to_string() };
    let desc = if description.is_empty() { Sql::Null } else { s(description) };
    let wid = orm::insert(&c.db, "worlds", &[("name", s(&name)), ("slug", s(&slug)), ("description", desc), ("accent", s(&accent))])?;
    let preset = f.get("calendar_preset").unwrap_or("");
    if let Some(cfg) = nd_render::app::data().get("calendar_presets_world")?.as_array()?.iter().find(|p| p.get(0).and_then(|k| k.as_str()) == Some(preset)) {
        orm::insert(&c.db, "world_calendars", &[("world_id", Sql::Integer(wid)), ("config_json", s(cfg.get(1)?.as_str()?))])?;
    }
    let mut r = redirect("/worlds")?;
    r.headers_mut().insert(header::SET_COOKIE, format!("active_world={slug}; Max-Age=31536000; Path=/; SameSite=lax").parse().ok()?);
    Some(r)
}

/// A parsed overlay whose every `order` is an integer (a float is written by Python's repr: Python's)
fn overlay_plain(v: &Value) -> bool {
    v.get("sections").and_then(|x| x.as_object()).is_none_or(|m| m.values().all(|e| e.get("order").is_none_or(|o| o.is_i64())))
}

fn rules_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    let raw = strip(f.get("rules_json").unwrap_or(""));
    let rules_json = if raw.is_empty() {
        Sql::Null
    } else {
        nd_render::rules::parse_overlay(Some(&raw)).ok()??;
        s(&raw)
    };
    let md = strip(f.get("rules_md").unwrap_or(""));
    orm::update(&c.db, "worlds", ids[0], &[("rules_json", rules_json), ("rules_md", if md.is_empty() { Sql::Null } else { s(&md) })])?;
    redirect("/rules")
}

fn rules_import(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    if content_type(c.headers) != "multipart/form-data" {
        return None;
    }
    let (f, files) = parse_multipart(c.headers, c.body)?;
    if f.has("file") {
        return None;
    }
    let file = files.iter().rev().find(|p| p.name == "file")?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", ids[0])?;
    if file.data.len() as i64 > env_bytes("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)? {
        return None;
    }
    let text = std::str::from_utf8(&file.data).ok().filter(|t| !t.starts_with('\u{feff}'))?;
    let Value::Object(payload) = serde_json::from_str::<Value>(text).ok()? else { return None };
    let md = strip(&theme_text(payload.get("rules_md")).ok()?);
    let overlay = if payload.contains_key("sections") || payload.contains_key("tabs") {
        Some(Value::Object(payload.clone()))
    } else {
        payload.get("rules_json").filter(|v| v.is_object()).cloned()
    };
    if md.is_empty() && overlay.is_none() {
        return None;
    }
    let mut set: Vec<(&str, Sql)> = vec![];
    if !md.is_empty() {
        set.push(("rules_md", s(&md)));
    }
    if let Some(o) = overlay {
        let parsed = nd_render::rules::parse_overlay(Some(&o.to_string())).ok()??;
        if !overlay_plain(&parsed) {
            return None;
        }
        set.push(("rules_json", s(&nd_render::py_dumps(&parsed))));
    }
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect("/rules")
}

// ── custom kinds and nav menus (app/routers/kinds_admin.py, nav_menus_admin.py) ──

/// `re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")`, `or fallback`, first 24 characters
fn underscore_slug(label: &str, fallback: &str) -> String {
    let mut out = String::new();
    let mut gap = false;
    for ch in strip(label).to_lowercase().chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            out.push(ch);
            gap = false;
        } else if !gap {
            out.push('_');
            gap = true;
        }
    }
    let t = out.trim_matches('_');
    take_chars(if t.is_empty() { fallback } else { t }, 24)
}

/// `datetime.now(timezone.utc).isoformat()`
fn utc_isoformat() -> String {
    let st = orm::now_stamp().replacen(' ', "T", 1);
    let st = st.strip_suffix(".000000").map(str::to_string).unwrap_or(st);
    format!("{st}+00:00")
}

/// A form field read with `str(form.get(name, default))`
fn form_str<'a>(f: &'a Form, name: &str, default: &'a str) -> &'a str {
    f.get(name).unwrap_or(default)
}

fn world_row(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    row(&c.db, "SELECT * FROM worlds WHERE id = ?1", id)
}

fn kinds_new(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let w = world_row(c, ids[0])?;
    let label = take_chars(&strip(form_str(&f, "label", "")), 40);
    let mut kinds = crate::idpages::load_custom_kinds(&w);
    if label.is_empty() || kinds.len() >= 25 {
        return None; // Python's 400
    }
    let base = format!("custom_{}", underscore_slug(&label, "kind"));
    let taken = |id: &str| kinds.iter().any(|k| k.get("id").and_then(|v| v.as_str()) == Some(id));
    let mut kid = base.clone();
    let mut n = 2;
    while taken(&kid) {
        kid = format!("{base}_{n}");
        n += 1;
    }
    let icon = take_chars(&strip(form_str(&f, "icon", "")), 8);
    let icon = if icon.is_empty() { "🏷".to_string() } else { icon };
    let mut subtypes: Vec<String> = vec![];
    let raw = form_str(&f, "subtypes", "");
    if !raw.is_empty() {
        for part in raw.split(',') {
            let part = take_chars(&strip(part), 40);
            if !part.is_empty() && !subtypes.contains(&part) {
                subtypes.push(part);
            }
            if subtypes.len() >= 20 {
                break;
            }
        }
    }
    kinds.push(json!({"id": kid, "label": label, "icon": icon, "subtypes": subtypes, "created_at": utc_isoformat()}));
    orm::update(&c.db, "worlds", ids[0], &[("custom_kinds_json", s(&nd_render::py_dumps(&Value::Array(kinds))))])?;
    redirect(&format!("/worlds/{}/kinds/edit", ids[0]))
}

/// `str(v)` of a posted kind field: a string, or the default when missing; anything else (Python's repr) is not ported
fn posted_str(item: &Map<String, Value>, key: &str) -> Option<String> {
    match item.get(key) {
        None => Some(String::new()),
        Some(Value::String(t)) => Some(t.clone()),
        Some(_) => None,
    }
}

fn kinds_save(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let w = world_row(c, ids[0])?;
    let raw = form_str(&f, "custom_kinds_json", "[]");
    let raw = if raw.is_empty() { "[]" } else { raw };
    let posted = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => a,
        Ok(_) => vec![],
        // text serde refuses may be JSON to Python (NaN, Infinity): Python's
        Err(_) => return None,
    };
    let stored = crate::idpages::load_custom_kinds(&w);
    let mut out = vec![];
    let mut seen: Vec<String> = vec![];
    for item in posted.iter().take(25) {
        let Some(item) = item.as_object() else { continue };
        let kid = match item.get("id") {
            Some(Value::String(k)) => k.clone(),
            Some(Value::Array(_)) | Some(Value::Object(_)) => return None, // unhashable: Python's 500
            _ => continue,
        };
        let Some(original) = stored.iter().find(|k| k.get("id").and_then(|v| v.as_str()) == Some(kid.as_str())) else { continue };
        if seen.contains(&kid) {
            continue;
        }
        seen.push(kid.clone());
        let label = take_chars(&strip(&posted_str(item, "label")?), 40);
        let label = if label.is_empty() { original.get("label")?.clone() } else { Value::String(label) };
        let icon = take_chars(&strip(&posted_str(item, "icon")?), 8);
        let icon = if icon.is_empty() { "🏷".to_string() } else { icon };
        let subtypes = match item.get("subtypes") {
            Some(Value::Array(a)) => {
                let mut v = vec![];
                for x in a {
                    let t = strip(x.as_str()?);
                    if !t.is_empty() {
                        v.push(Value::String(take_chars(&t, 40)));
                    }
                }
                v.truncate(20);
                Value::Array(v)
            }
            _ => original.get("subtypes")?.clone(),
        };
        out.push(json!({"id": kid, "label": label, "icon": icon, "subtypes": subtypes, "created_at": original.get("created_at")?}));
    }
    orm::update(&c.db, "worlds", ids[0], &[("custom_kinds_json", s(&nd_render::py_dumps(&Value::Array(out))))])?;
    redirect(&format!("/worlds/{}/kinds/edit?saved=1", ids[0]))
}

fn kinds_delete(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let (wid, kid) = key.split_once('/')?;
    let wid: i64 = wid.parse().ok()?;
    let w = world_row(c, wid)?;
    let kinds = crate::idpages::load_custom_kinds(&w);
    if !kinds.iter().any(|k| k.get("id").and_then(|v| v.as_str()) == Some(kid)) {
        return None;
    }
    let in_use: i64 = c.db.query_row("SELECT COUNT(*) FROM entities WHERE world_id = ?1 AND kind = ?2", rusqlite::params![wid, kid], |r| r.get(0)).ok()?;
    if in_use > 0 {
        return None;
    }
    let rest: Vec<Value> = kinds.into_iter().filter(|k| k.get("id").and_then(|v| v.as_str()) != Some(kid)).collect();
    orm::update(&c.db, "worlds", wid, &[("custom_kinds_json", s(&nd_render::py_dumps(&Value::Array(rest))))])?;
    redirect(&format!("/worlds/{wid}/kinds/edit"))
}

/// `deps.sanitize_section_access`: every section id of the world with both roles' levels (Python walks a set: its key order varies
/// from one process to the next, so the order here is the sorted one)
fn sanitize_section_access(raw: &str, w: &Map<String, Value>) -> Option<String> {
    let data = if raw.is_empty() {
        Map::new()
    } else {
        match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(o)) => o,
            Ok(_) => Map::new(),
            Err(_) => return None,
        }
    };
    let kinds = nd_perm::sections::effective_kinds(w.get("custom_kinds_json").and_then(|v| v.as_str()))?;
    let mut sids: Vec<String> = nd_perm::sections::SECTION_PERMISSION_IDS.iter().map(|x| x.to_string()).collect();
    sids.extend(kinds.iter().map(|k| format!("kind_{k}")));
    sids.sort();
    sids.dedup();
    let mut out = Map::new();
    for sid in sids {
        let entry = data.get(&sid).and_then(|e| e.as_object());
        let mut levels = Map::new();
        for role in ["player", "assistant"] {
            let val = match entry.and_then(|e| e.get(role)).and_then(|v| v.as_str()) {
                Some("read") => "read",
                Some("edit") => "edit",
                _ => "none",
            };
            levels.insert(role.into(), json!(nd_perm::sections::floor(&sid, role, val)));
        }
        out.insert(sid, Value::Object(levels));
    }
    Some(nd_render::py_dumps(&Value::Object(out)))
}

fn nav_menus_save(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let w = world_row(c, ids[0])?;
    let catalog = nd_render::app::build_catalog(Some(&w))?;
    let known = |id: &str| catalog.iter().any(|it| it.get("id").and_then(|v| v.as_str()) == Some(id));
    let raw = form_str(&f, "nav_menus_json", "[]");
    let raw = if raw.is_empty() { "[]" } else { raw };
    let data = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => a,
        Ok(_) => vec![],
        Err(_) => return None,
    };
    let mut out = vec![];
    let mut seen_ids: Vec<String> = vec![];
    let mut seen_items: Vec<String> = vec![];
    for m in data.iter().take(12) {
        let Some(m) = m.as_object() else { continue };
        let label = take_chars(&strip(&posted_str(m, "label")?), 40);
        if label.is_empty() {
            continue;
        }
        let icon = take_chars(&strip(&posted_str(m, "icon")?), 8);
        let icon = if icon.is_empty() { "🗂".to_string() } else { icon };
        let mid = match m.get("id") {
            Some(Value::String(x)) if !x.is_empty() && !seen_ids.contains(x) => x.clone(),
            _ => {
                let base = format!("menu_{}", underscore_slug(&label, "menu"));
                let (mut mid, mut n) = (base.clone(), 2);
                while seen_ids.contains(&mid) {
                    mid = format!("{base}_{n}");
                    n += 1;
                }
                mid
            }
        };
        seen_ids.push(mid.clone());
        let mut items = vec![];
        if let Some(Value::Array(raw_items)) = m.get("item_ids") {
            for iid in raw_items {
                if let Value::String(iid) = iid {
                    if known(iid) && !seen_items.contains(iid) {
                        items.push(json!(iid));
                        seen_items.push(iid.clone());
                    }
                }
            }
        }
        out.push(json!({"id": mid, "label": label, "icon": icon, "item_ids": items}));
    }
    let mut set: Vec<(&str, Sql)> = vec![("nav_menus_json", s(&nd_render::py_dumps(&Value::Array(out))))];
    if f.has("section_access_json") {
        set.push(("section_access_json", s(&sanitize_section_access(form_str(&f, "section_access_json", "{}"), &w)?)));
    }
    orm::update(&c.db, "worlds", ids[0], &set)?;
    redirect("/settings?tab=navigation")
}

// ── AI instructions (app/routers/ai_instructions.py): stored settings, no AI call ──

fn ai_instruction_import(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    if content_type(c.headers) != "multipart/form-data" {
        return None;
    }
    let (f, files) = parse_multipart(c.headers, c.body)?;
    if f.has("file") || c.world_id()? != ids[0] {
        return None;
    }
    let file = files.iter().rev().find(|p| p.name == "file")?;
    let ext = py_suffix_lower(&file.filename)?;
    if ![".md", ".markdown", ".txt"].contains(&ext.as_str()) || file.data.len() > 200 * 1024 {
        return None; // Python's 400 / 413
    }
    let content = strip(&String::from_utf8_lossy(&file.data));
    if !content.is_empty() {
        let title = strip(f.get("title").unwrap_or(""));
        let title = if !title.is_empty() {
            title
        } else {
            let (stem, _) = py_path_parts(&file.filename)?;
            if stem.is_empty() { "Untitled".to_string() } else { stem }
        };
        orm::insert(&c.db, "ai_instructions", &[("world_id", Sql::Integer(ids[0])), ("title", s(&title)), ("content", s(&content)),
            ("enabled", Sql::Integer(1)), ("applies_to_players", Sql::Integer(flag_param(&f, "applies_to_players")))])?;
    }
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn ai_instruction_flip(c: &mut Ctx, ids: &[i64], col: &str) -> Option<Response<Body>> {
    if let Some(r) = row(&c.db, "SELECT * FROM ai_instructions WHERE id = ?1", ids[1]) {
        if r.get("world_id").and_then(|v| v.as_i64()) == Some(ids[0]) {
            let on = py_truthy(r.get(col));
            orm::update(&c.db, "ai_instructions", ids[1], &[(col, Sql::Integer(!on as i64))])?;
        }
    }
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

fn ai_instruction_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    ai_instruction_flip(c, ids, "enabled")
}

fn ai_instruction_toggle_players(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    ai_instruction_flip(c, ids, "applies_to_players")
}

fn ai_instruction_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    c.db.execute("DELETE FROM ai_instructions WHERE id = ?1 AND world_id = ?2", [ids[1], ids[0]]).ok()?;
    redirect(&format!("/worlds/{}/edit", ids[0]))
}

// ── the bundled race / profession catalogs (app/routers/races.py, professions.py `*_add_builtin`) ──

fn builtin(kind: &str) -> Option<&'static Vec<Value>> {
    nd_render::app::data().get(if kind == "race" { "builtin_races" } else { "builtin_professions" })?.as_array()
}

fn or_null(v: Option<&Value>) -> Sql {
    match v.and_then(|x| x.as_str()) {
        Some(t) if !t.is_empty() => s(t),
        _ => Sql::Null,
    }
}

/// The world's entity of this kind named like the catalog entry, adding it when missing: (entity id, added)
fn builtin_entity(c: &Ctx, wid: i64, kind: &str, e: &Value) -> Option<(i64, bool)> {
    let name = e.get("name")?.as_str()?;
    let found: Option<i64> = c.db.query_row("SELECT id FROM entities WHERE world_id = ?1 AND kind = ?2 AND name = ?3 ORDER BY id LIMIT 1",
        rusqlite::params![wid, kind, name], |r| r.get(0)).ok();
    if let Some(id) = found {
        return Some((id, false));
    }
    let id = orm::insert(&c.db, "entities", &[("world_id", Sql::Integer(wid)), ("kind", s(kind)), ("subtype", s(e.get("tier")?.as_str()?)),
        ("name", s(name)), ("summary", or_null(e.get("summary"))), ("body", or_null(e.get("raw"))), ("image_url", or_null(e.get("image_url")))])?;
    Some((id, true))
}

fn add_builtin(c: &mut Ctx, kind: &str) -> Option<Response<Body>> {
    let wid = c.world_id()?;
    let payload = c.json()?;
    let (slug, tier) = (payload.get("slug").and_then(|v| v.as_str())?, payload.get("tier").and_then(|v| v.as_str())?);
    let e = builtin(kind)?.iter().find(|e| e.get("slug").and_then(|v| v.as_str()) == Some(slug) && e.get("tier").and_then(|v| v.as_str()) == Some(tier))?;
    let (id, _) = builtin_entity(c, wid, kind, e)?;
    if kind != "race" {
        return json_response(&json!({"ok": true}));
    }
    let ent = row(&c.db, "SELECT * FROM entities WHERE id = ?1", id)?;
    let text = |k: &str| ent.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let tier = Some(text("subtype")).filter(|t| !t.is_empty()).unwrap_or_else(|| "standard".into());
    let body = text("body");
    // markdown2's HTML for the catalog text (scripts/gen_rust_data.py); a body edited since: Python's
    let body_html = if body.is_empty() { "" } else if Some(body.as_str()) == e.get("raw").and_then(|v| v.as_str()) { e.get("raw_html")?.as_str()? } else { return None };
    json_response(&json!({"ok": true, "entity": {"id": id, "name": text("name"), "tier": tier, "image_url": text("image_url"), "summary": text("summary"),
        "body_html": body_html}}))
}

fn add_all_builtin(c: &mut Ctx, kind: &str, back: &str) -> Option<Response<Body>> {
    let wid = c.world_id()?;
    let mut added = 0;
    for e in builtin(kind)? {
        added += builtin_entity(c, wid, kind, e)?.1 as i64;
    }
    redirect(&format!("{back}?added={added}"))
}

fn race_add_builtin(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    add_builtin(c, "race")
}

fn profession_add_builtin(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    add_builtin(c, "profession")
}

fn race_add_all_builtin(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    add_all_builtin(c, "race", "/races")
}

fn profession_add_all_builtin(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    add_all_builtin(c, "profession", "/professions")
}

// ── marking up the read view (app/main.py api_entity_markup / api_entity_send_to_player, crate::markup) ──

/// The world's player accounts (`_world_player_list` without the GMs): (id, display name), by name
fn world_players(c: &Ctx, wid: i64) -> Option<Vec<(i64, String)>> {
    let mut st = c.db.prepare("SELECT u.id, u.display_name, u.is_gm FROM users u JOIN world_memberships m ON m.user_id = u.id WHERE m.world_id = ?1 ORDER BY u.display_name").ok()?;
    let rows = st.query_map([wid], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<i64>>(2)?))).ok()?;
    let mut out = vec![];
    for r in rows {
        let (id, name, gm) = r.ok()?;
        if gm.unwrap_or(0) == 0 {
            out.push((id, name.unwrap_or_default()));
        }
    }
    Some(out)
}

/// A pydantic `str` field: a string, or the default when missing (anything else is a 422: Python's)
fn body_str(b: &Map<String, Value>, k: &str, default: Option<&str>) -> Option<String> {
    json_str(b, k, default).map(str::to_string)
}

/// A pydantic `int` / `Optional[int]` field given as a JSON integer (strings, floats and booleans pydantic may coerce: Python's)
fn model_int(b: &Map<String, Value>, k: &str, default: Option<Option<i64>>) -> Option<Option<i64>> {
    match b.get(k) {
        None => default,
        Some(Value::Null) => default.filter(|d| d.is_none()),
        Some(Value::Number(n)) => n.as_i64().map(Some),
        _ => None,
    }
}

/// The marked-up field's text and where it lives: (source, note id for a note)
fn markup_source(c: &Ctx, e: &Map<String, Value>, field: &str, note_id: Option<i64>) -> Option<(String, Option<i64>)> {
    let text = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    match field {
        "body" => Some((text("body"), None)),
        "summary" => Some((text("summary"), None)),
        "note" => {
            let n = row(&c.db, "SELECT * FROM entity_notes WHERE id = ?1", note_id.unwrap_or(0))?;
            if n.get("entity_id") != e.get("id") || py_truthy(n.get("content_is_html")) {
                return None;
            }
            Some((n.get("content").and_then(|v| v.as_str()).unwrap_or("").to_string(), n.get("id")?.as_i64()))
        }
        _ => None,
    }
}

fn entity_markup(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let field = body_str(&b, "field", None)?;
    let action = body_str(&b, "action", None)?;
    let note_id = model_int(&b, "note_id", Some(None))?;
    let index = model_int(&b, "index", Some(Some(0)))??;
    let [text, before, after, snippet, replacement, previous, expected] =
        ["text", "before", "after", "snippet", "replacement", "previous", "expected"].map(|k| body_str(&b, k, Some("")));
    let (text, before, after, snippet, replacement, previous, expected) = (text?, before?, after?, snippet?, replacement?, previous?, expected?);
    if !c.p.is_gm {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let (source, note) = markup_source(c, &e, &field, note_id)?;
    if ![&source, &text, &before, &after, &snippet, &replacement].iter().all(|t| crate::markup::plain(t)) {
        return None;
    }
    if let Some(rest) = action.strip_prefix("for:") {
        if !rest.is_ascii() {
            return None;
        }
        let wanted: Vec<i64> = rest.split(|ch: char| !ch.is_ascii_digit()).filter(|x| !x.is_empty()).map(|x| x.parse::<i64>().ok()).collect::<Option<_>>()?;
        let players: Vec<i64> = world_players(c, c.world_id()?)?.into_iter().map(|(id, _)| id).collect();
        if wanted.is_empty() || !wanted.iter().all(|w| players.contains(w)) {
            return None;
        }
    }
    let out = if let Some(tag) = action.strip_prefix("unwrap:") {
        crate::markup::unwrap(&source, tag, index, &snippet)
    } else if action == "replace" {
        crate::markup::replace(&source, &text, &before, &after, &replacement)
    } else if action == "restore" {
        if source == expected { Ok(previous.clone()) } else { Err(()) }
    } else if let Some(pct) = action.strip_prefix("imgsize:").filter(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())) {
        let pct = if pct.len() > 9 { 300 } else { pct.parse().ok()? };
        crate::markup::resize_image(&source, index, &snippet, pct)
    } else {
        crate::markup::wrap(&source, &text, &before, &after, &action)
    };
    let out = out.ok()?; // a refusal: Python's 422 with its message
    match note {
        Some(nid) => orm::update(&c.db, "entity_notes", nid, &[("content", s(&out))])?,
        None => orm::update(&c.db, "entities", ids[0], &[(if field == "body" { "body" } else { "summary" }, s(&out))])?,
    };
    let mut result = json!({"ok": true});
    if action != "restore" && source.chars().count() + out.chars().count() <= 200_000 {
        result["undo"] = json!({"previous": source, "expected": out});
    }
    json_response(&result)
}

fn entity_send_to_player(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let user_id = model_int(&b, "user_id", None)??;
    let field = body_str(&b, "field", Some("body"))?;
    let note_id = model_int(&b, "note_id", Some(None))?;
    let text = body_str(&b, "text", None)?;
    let [before, after, title] = ["before", "after", "title"].map(|k| body_str(&b, k, Some("")));
    let (before, after, title) = (before?, after?, title?);
    if !c.p.is_gm {
        return None;
    }
    let e = world_entity(c, ids[0])?;
    let (source, _) = markup_source(c, &e, &field, note_id)?;
    if ![&source, &text, &before, &after].iter().all(|t| crate::markup::plain(t)) {
        return None;
    }
    let ranges = crate::markup::locate(&source, &text, &before, &after).ok()?;
    let src: Vec<char> = source.chars().collect();
    let mut kept = vec![];
    for (s0, e0) in ranges {
        let (a, z) = crate::markup::widen(&source, s0, e0).ok()?;
        if !crate::markup::inside(&source, a, "gmonly") {
            kept.push(src[a..z].iter().collect::<String>());
        }
    }
    let stripped = nd_perm::gmonly::strip_gm_only(&kept.join("\n\n"), None, nd_render::now_minute())?;
    let body = strip(&stripped);
    if body.is_empty() || body.chars().count() > 8000 {
        return None;
    }
    let wid = c.world_id()?;
    let (pid, pname) = world_players(c, wid)?.into_iter().find(|(id, _)| *id == user_id)?;
    let title = take_chars(&strip(&title), 200);
    let title = if title.is_empty() { format!("From {}", e.get("name").and_then(|v| v.as_str()).unwrap_or("")) } else { title };
    orm::insert(&c.db, "private_notes", &[("world_id", Sql::Integer(wid)), ("player_user_id", Sql::Integer(pid)), ("author_id", Sql::Integer(c.p.user_id)),
        ("title", s(&title)), ("content", s(&body))])?;
    json_response(&json!({"ok": true, "player": pname}))
}

// ── image maps (app/main.py map_rename / map_delete): JSON files next to the database ──

/// The map file and its data, when it belongs to the active world and the viewer may edit maps
fn own_map(c: &Ctx, slug: &str) -> Option<(std::path::PathBuf, Map<String, Value>)> {
    let jf = crate::maps::maps_dir(c.cfg)?.join(format!("{slug}.json"));
    let text = std::fs::read_to_string(&jf).ok()?;
    let Value::Object(data) = serde_json::from_str::<Value>(&text).ok()? else { return None };
    let owner = match data.get("world_id") {
        None => 1,
        Some(Value::Number(n)) => n.as_i64()?,
        _ => return None,
    };
    if Some(owner) != c.world_id() || !c.can_edit_section("maps")? {
        return None;
    }
    Some((jf, data))
}

fn map_rename(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let f = c.form()?;
    let name = strip(f.get("name")?);
    let (jf, mut data) = own_map(c, slug)?;
    if name.is_empty() {
        return None;
    }
    data.insert("name".into(), Value::String(name));
    std::fs::write(&jf, nd_render::py_dumps(&Value::Object(data))).ok()?;
    redirect("/maps")
}

fn map_delete(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let (jf, _) = own_map(c, slug)?;
    c.db.execute("DELETE FROM map_overlays WHERE id = (SELECT id FROM map_overlays WHERE slug = ?1 ORDER BY id LIMIT 1)", [slug]).ok()?;
    c.removals.push(jf);
    let dir = c.cfg.db_path.parent()?.join("uploads").join("maps");
    for ext in [".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"] {
        let img = dir.join(format!("{slug}{ext}"));
        if img.exists() {
            c.removals.push(img);
        }
    }
    redirect("/maps")
}

/// `/maps/<slug>/upload`: the slug
fn map_upload_slug(path: &str) -> Option<String> {
    let slug = path.strip_prefix("/maps/")?.strip_suffix("/upload")?;
    (!slug.is_empty() && slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')).then(|| slug.to_string())
}

const MAP_EXTS: [&str; 6] = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"];

fn map_uploads(c: &Ctx) -> Option<std::path::PathBuf> {
    let dir = c.cfg.db_path.parent()?.join("uploads").join("maps");
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

/// A FastAPI `int` form field: missing / empty -> the default; plain digits -> the number; anything else (pydantic's own reading): Python's
fn form_plain_int(f: &Form, k: &str, default: i64) -> Option<i64> {
    match f.get(k).filter(|v| !v.is_empty()) {
        None => Some(default),
        Some(v) => {
            let body = v.strip_prefix('-').unwrap_or(v);
            if body.is_empty() || body.len() > 15 || !body.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            v.parse().ok()
        }
    }
}

fn map_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let (f, file) = form_and_file(c, "image_file")?;
    let name = f.get("name").filter(|n| !n.is_empty())?.to_string();
    let (width, height) = (form_plain_int(&f, "width", 3072)?, form_plain_int(&f, "height", 3072)?);
    let wid = c.world_id()?;
    if !c.can_edit_section("maps")? || !name.is_ascii() {
        return None;
    }
    let mut slug = String::new();
    for ch in strip(&name.to_ascii_lowercase()).chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            slug.push(ch);
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() {
        return None;
    }
    let dir = crate::maps::maps_dir(c.cfg)?;
    std::fs::create_dir_all(&dir).ok()?;
    let (base, mut slug, mut i) = (slug.clone(), slug, 2);
    while dir.join(format!("{slug}.json")).exists() {
        slug = format!("{base}-{i}");
        i += 1;
    }
    // the picture first: one over the limit is Python's 413 (Python has written the map file by then - its own answer)
    let mut copy: Option<(std::path::PathBuf, Option<std::path::PathBuf>)> = None;
    if let Some(pick) = &file {
        let fname = pick.filename(c)?.to_string();
        if !fname.is_empty() {
            let ext = py_suffix_lower(&fname)?;
            if MAP_EXTS.contains(&ext.as_str()) {
                if pick.len(c)? as i64 > general_cap(c)? {
                    return None;
                }
                copy = Some((map_uploads(c)?.join(format!("{slug}{ext}")), None));
            }
        }
    }
    let url = strip(f.get("image_url").unwrap_or(""));
    if file.as_ref().map_or(true, |p| p.filename(c).map_or(true, str::is_empty)) && !url.is_empty() {
        if let Some(rel) = url.strip_prefix("/uploads/") {
            let root = c.cfg.db_path.parent()?.join("uploads");
            if let (Ok(cand), Ok(root)) = (root.join(rel).canonicalize(), root.canonicalize()) {
                let ext = cand.extension().and_then(|e| e.to_str()).map(|e| format!(".{}", e.to_lowercase())).unwrap_or_default();
                if cand.starts_with(&root) && cand.is_file() && MAP_EXTS.contains(&ext.as_str()) {
                    copy = Some((map_uploads(c)?.join(format!("{slug}{ext}")), Some(cand)));
                }
            }
        }
    }
    let jf = dir.join(format!("{slug}.json"));
    let doc = json!({"name": name, "world_id": wid, "width": width, "height": height, "markers": []});
    std::fs::write(&jf, nd_render::py_dumps(&doc)).ok()?;
    c.created.push(jf);
    if let Some((dest, src)) = copy {
        match src {
            Some(src) => {
                std::fs::copy(&src, &dest).ok()?;
                if let Ok(m) = std::fs::metadata(&src).and_then(|m| m.modified()) {
                    let _ = std::fs::File::options().write(true).open(&dest).and_then(|fh| fh.set_modified(m));
                }
            }
            None => file.as_ref()?.write_to(c, &dest)?,
        }
        c.created.push(dest);
    }
    redirect(&format!("/maps/{slug}"))
}

fn map_upload(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let up = c.upload.as_ref()?;
    if !up.form.0.is_empty() || up.files.len() != 1 || up.files[0].name != "file" {
        return None;
    }
    let fname = up.files[0].filename.clone();
    let len = up.files[0].len;
    own_map(c, slug)?;
    let ext = py_suffix_lower(&fname)?;
    if !MAP_EXTS.contains(&ext.as_str()) || len as i64 > general_cap(c)? {
        return None;
    }
    let dir = map_uploads(c)?;
    // the new picture goes to a side name first; the old ones (and a same-named one) go after the commit
    let tmp = dir.join(format!(".{slug}{ext}.gw-{}", std::process::id()));
    FilePick::Disk(0).write_to(c, &tmp)?;
    c.created.push(tmp.clone());
    for old in MAP_EXTS {
        let p = dir.join(format!("{slug}{old}"));
        if p.exists() && old != ext {
            c.removals.push(p);
        }
    }
    std::fs::rename(&tmp, dir.join(format!("{slug}{ext}"))).ok()?;
    redirect("/maps")
}

/// The schematic editor's two uploads: `/maps/schematic/<slug>/upload` (the background picture) and `/embed-image`
fn sc_upload_route(path: &str) -> Option<(SlugHandler, String)> {
    let rest = path.strip_prefix("/maps/schematic/")?;
    let (slug, h) = if let Some(s) = rest.strip_suffix("/upload") {
        (s, sc_upload as SlugHandler)
    } else {
        (rest.strip_suffix("/embed-image")?, sc_embed_image as SlugHandler)
    };
    (!slug.is_empty() && !slug.contains('/')).then(|| (h, slug.to_string()))
}

/// The one `file` part with an allowed picture extension and within the upload limit: (pick, ext)
fn sc_picture(c: &Ctx) -> Option<(FilePick, String, String)> {
    let (f, pick) = form_and_file(c, "file")?;
    if !f.0.is_empty() {
        return None;
    }
    let pick = pick?;
    let fname = pick.filename(c)?.to_string();
    let ext = py_suffix_lower(&fname)?;
    if !MAP_EXTS.contains(&ext.as_str()) || pick.len(c)? as i64 > general_cap(c)? {
        return None;
    }
    Some((pick, ext, fname))
}

/// `schematic_upload_image`: the map's background picture, stored as uploaded at uploads/schematics/<slug><ext> (any other extension's
/// copy removed)
fn sc_upload(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_manager(c, slug)?;
    let (pick, ext, _) = sc_picture(c)?;
    let dir = c.cfg.db_path.parent()?.join("uploads").join("schematics");
    std::fs::create_dir_all(&dir).ok()?;
    let tmp = dir.join(format!(".{slug}{ext}.gw-{}", std::process::id()));
    pick.write_to(c, &tmp)?;
    c.created.push(tmp.clone());
    for old in MAP_EXTS {
        let p = dir.join(format!("{slug}{old}"));
        if p.exists() && old != ext {
            c.removals.push(p);
        }
    }
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("image_url", s(&format!("/uploads/schematics/{slug}{ext}")))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    std::fs::rename(&tmp, dir.join(format!("{slug}{ext}"))).ok()?;
    redirect(&format!("/maps/schematic/{slug}"))
}

/// `schematic_embed_image`: a picture placed on the map, stored under a unique name (nothing in the database changes)
fn sc_embed_image(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    sc_for_manager(c, slug)?;
    let (pick, ext, fname) = sc_picture(c)?;
    let dir = c.cfg.db_path.parent()?.join("uploads").join("schematics").join("embeds");
    std::fs::create_dir_all(&dir).ok()?;
    let name = unique_upload_filename(&fname, &ext)?;
    let dest = dir.join(&name);
    pick.write_to(c, &dest)?;
    c.created.push(dest);
    json_response(&json!({"url": format!("/uploads/schematics/embeds/{name}")}))
}

// ── the formatting toolbar's uploads (app/main.py api_upload_image / api_upload_media, characters.py's player-reachable pair) ──

/// The one required `file` part, with a name (no name: Python's 400 / 422)
fn toolbar_file(c: &Ctx) -> Option<FilePick> {
    let (_, pick) = form_and_file(c, "file")?;
    let pick = pick?;
    (!pick.filename(c)?.is_empty()).then_some(pick)
}

/// `save_inline_av`: an audio / video file stored as uploaded, (url, kind); Some(None) for another extension
fn save_inline_av(c: &mut Ctx, pick: &FilePick) -> Option<Option<(String, &'static str)>> {
    let filename = pick.filename(c)?.to_string();
    let ext = py_suffix_lower(&filename)?;
    let (kind, cap) = if [".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"].contains(&ext.as_str()) {
        ("audio", env_bytes("MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?)
    } else if [".mp4", ".m4v", ".ogv", ".mov"].contains(&ext.as_str()) {
        ("video", env_bytes("MAX_VIDEO_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024)?)
    } else {
        return Some(None);
    };
    if pick.len(c)? as i64 > cap {
        return None;
    }
    let dir = uploads_root(c)?;
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(unique_upload_filename(&filename, &ext)?);
    c.created.push(dest.clone());
    pick.write_to(c, &dest)?;
    Some(Some((format!("/uploads/{}", dest.file_name()?.to_string_lossy()), kind)))
}

fn toolbar_picture(c: &mut Ctx, subdir: &str) -> Option<Response<Body>> {
    let pick = toolbar_file(c)?;
    let url = save_picture(c, &pick, subdir, true)??;
    json_response(&json!({"url": url}))
}

fn toolbar_any(c: &mut Ctx, subdir: &str) -> Option<Response<Body>> {
    let pick = toolbar_file(c)?;
    let ext = py_suffix_lower(pick.filename(c)?)?;
    if IMAGE_EXTS.contains(&ext.as_str()) {
        let url = save_picture(c, &pick, subdir, true)??;
        return json_response(&json!({"url": url, "kind": "image"}));
    }
    let (url, kind) = save_inline_av(c, &pick)??;
    json_response(&json!({"url": url, "kind": kind}))
}

fn toolbar_image(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    toolbar_picture(c, "")
}

fn toolbar_media(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    toolbar_any(c, "")
}

fn toolbar_pc_image(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    toolbar_picture(c, "portraits")
}

fn toolbar_pc_media(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    toolbar_any(c, "portraits")
}

/// `import_entity_note`: a text file or a picture becomes a note (PDF and HTML, which need pypdf / the HTML converters: Python's)
fn entity_note_import(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (f, pick) = form_and_file(c, "file")?;
    let pick = pick?;
    let e = row(&c.db, "SELECT * FROM entities WHERE id = ?1", ids[0])?;
    let ext = py_suffix_lower(pick.filename(c)?)?;
    let content = if [".png", ".jpg", ".jpeg", ".gif", ".webp"].contains(&ext.as_str()) {
        let url = save_picture(c, &pick, "entity_notes", true)??;
        format!("![{}]({url})", e.get("name").and_then(|v| v.as_str()).unwrap_or(""))
    } else if [".md", ".markdown", ".txt"].contains(&ext.as_str()) {
        if pick.len(c)? as i64 > env_bytes("MAX_NOTE_IMPORT_BYTES", 10 * 1024 * 1024)? {
            return None;
        }
        let tmp;
        let data: &[u8] = match &pick {
            FilePick::Mem(p) => &p.data,
            FilePick::Disk(_) => {
                let t = c.uploads_scratch()?;
                pick.write_to(c, &t)?;
                tmp = std::fs::read(&t).ok()?;
                let _ = std::fs::remove_file(&t);
                &tmp
            }
        };
        // undecodable bytes (errors="replace" may differ in detail): Python's
        strip(std::str::from_utf8(data).ok()?)
    } else {
        return None;
    };
    if !content.is_empty() {
        let author = if c.p.user_id > 0 { Sql::Integer(c.p.user_id) } else { Sql::Null };
        orm::insert(&c.db, "entity_notes", &[("entity_id", Sql::Integer(ids[0])), ("author_id", author), ("content", s(&content)),
            ("visible_to_players", Sql::Integer(flag_param(&f, "visible"))), ("content_is_html", Sql::Integer(0))])?;
    }
    redirect(&format!("/entity/{}", ids[0]))
}

// ── character share links (app/routers/character_share.py) ──

/// `_manageable`: the character id, when the caller owns it or is a GM (anything else is Python's 401 / 404)
fn share_target(c: &Ctx, id: &str) -> Option<i64> {
    let id: i64 = id.parse().ok()?;
    let pc = row(&c.db, "SELECT owner_user_id FROM player_characters WHERE id = ?1", id)?;
    (c.p.is_gm || pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(c.p.user_id)).then_some(id)
}

fn share_revoke_all(c: &Ctx, id: i64) -> Option<()> {
    c.db.execute("UPDATE character_shares SET revoked = 1 WHERE character_id = ?1", [id]).ok().map(|_| ())
}

fn share_make(c: &mut Ctx, id: &str) -> Option<Response<Body>> {
    let id = share_target(c, id)?;
    // `request.base_url`: Python sees http and the client's Host; a forwarded scheme (behind TLS) is Python's to read
    if c.headers.contains_key("x-forwarded-proto") || c.headers.contains_key("forwarded") {
        return None;
    }
    let host = c.headers.get(header::HOST)?.to_str().ok()?.to_string();
    if host.is_empty() || !host.bytes().all(|b| b.is_ascii_alphanumeric() || b"-.:[]".contains(&b)) {
        return None;
    }
    let world: i64 = c.db.query_row("SELECT world_id FROM player_characters WHERE id = ?1", [id], |r| r.get(0)).ok()?;
    share_revoke_all(c, id)?;
    use base64::Engine;
    use rand::RngCore;
    let mut raw = [0u8; 24];
    rand::thread_rng().fill_bytes(&mut raw);
    let token = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw);
    orm::insert(&c.db, "character_shares", &[("world_id", Sql::Integer(world)), ("character_id", Sql::Integer(id)), ("token", s(&token))])?;
    json_response(&json!({"active": true, "url": format!("http://{host}/share/{token}")}))
}

fn share_revoke(c: &mut Ctx, id: &str) -> Option<Response<Body>> {
    let id = share_target(c, id)?;
    share_revoke_all(c, id)?;
    json_response(&json!({"active": false, "url": ""}))
}

/// `media_upload`: a dropped picture (kept on the session) or audio file (added to the audio library and attached); a video, which
/// goes through ffmpeg, is Python's
fn sm_upload(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let gs = sm_session(c, ids[0])?;
    let wid = gs.get("world_id")?.as_i64()?;
    let (f, pick) = form_and_file(c, "file")?;
    let pick = pick?;
    let filename = pick.filename(c)?.to_string();
    if filename.is_empty() {
        return None;
    }
    let ext = py_suffix_lower(&filename)?;
    let mut candidates = vec![];
    if IMAGE_EXTS.contains(&ext.as_str()) {
        candidates.push("image");
    }
    if [".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv", ".avi", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"].contains(&ext.as_str()) {
        candidates.push("video");
    }
    if AUDIO_EXTS.contains(&ext.as_str()) {
        candidates.push("audio");
    }
    let hint = strip(f.get("kind").unwrap_or("")).to_lowercase();
    let kind = *candidates.iter().find(|k| **k == hint).or(candidates.first())?;
    if kind == "video" {
        return None;
    }
    if (sm_clip_ids(&gs)?.len() + sm_items(&gs)?.len()) as i64 >= sm_limit(c)? {
        return None;
    }
    let cap = if kind == "image" {
        gallery_cap(c)?
    } else {
        let n: i64 = c.db.query_row("SELECT COUNT(*) FROM audio_clips WHERE world_id = ?1", [wid], |r| r.get(0)).ok()?;
        if n >= 300 {
            return None;
        }
        upload_cap(c, "max_audio_mb", "MAX_AUDIO_UPLOAD_BYTES", 1024 * 1024 * 1024)?
    };
    if pick.len(c)? as i64 > cap {
        return None;
    }
    let folder = if kind == "image" { "gallery" } else { "audio" };
    let dir = uploads_root(c)?.join(folder);
    std::fs::create_dir_all(&dir).ok()?;
    let dest = dir.join(unique_upload_filename(&filename, &ext)?);
    c.created.push(dest.clone());
    pick.write_to(c, &dest)?;
    let label = strip(f.get("name").unwrap_or(""));
    let (stem, _) = py_path_parts(&filename)?;
    let visible = ["1", "true", "on", "yes"].contains(&strip(f.get("visible_to_players").unwrap_or("")).to_lowercase().as_str());
    let item = if kind == "image" {
        let url = finalize_picture(c, &dest, "gallery")?;
        let caption = take_chars(if label.is_empty() { &stem } else { &label }, 120);
        let uid = format!("{:010x}", rand::random::<u64>() & 0xff_ffff_ffff);
        let mut items = sm_items(&gs)?;
        let mut m = Map::new();
        m.insert("uid".into(), json!(uid));
        m.insert("kind".into(), json!("image"));
        m.insert("url".into(), json!(url));
        m.insert("caption".into(), json!(caption));
        m.insert("visible".into(), json!(visible));
        m.insert("own".into(), json!(true));
        items.push(m);
        sm_store_media(c, ids[0], &items)?;
        json!({"uid": uid, "url": url, "caption": caption, "visible_to_players": visible, "own": true})
    } else {
        let name = [take_chars(&label, 256), take_chars(&stem, 256)].into_iter().find(|n| !n.is_empty()).unwrap_or_else(|| "Session audio".into());
        let title = gs.get("title").and_then(|v| v.as_str()).unwrap_or("");
        let desc = take_chars(&format!("Session audio for {title}"), 512);
        let url = format!("/uploads/audio/{}", dest.file_name()?.to_string_lossy());
        let cid = orm::insert(&c.db, "audio_clips", &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("description", s(&desc)),
            ("file_url", s(&url)), ("visible_to_players", Sql::Integer(visible as i64))])?;
        let clip = row(&c.db, "SELECT * FROM audio_clips WHERE id = ?1", cid)?;
        let gs = row(&c.db, "SELECT * FROM game_sessions WHERE id = ?1", ids[0])?;
        sm_attach_audio(c, &gs, &clip, None)?;
        sm_audio_dict(&clip)
    };
    let Value::Object(mut out) = sm_panel_value(c, ids[0])? else { return None };
    out.insert("kind".into(), json!(kind));
    out.insert("item".into(), item);
    json_response(&Value::Object(out))
}

// ── ability cards (characters.py `_ability_cards`, app/feat_use.py) ──

/// `str(v)` of a JSON id / name: a string, an integer, or None ("None"); anything else (a float's repr...) is Python's
fn py_str_key(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => Some("None".into()),
        Some(Value::String(t)) => Some(t.clone()),
        Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()),
        _ => None,
    }
}

/// `x or y`: the first truthy JSON value
fn py_or<'a>(vals: &[Option<&'a Value>]) -> Option<&'a Value> {
    vals.iter().copied().flatten().find(|v| py_truthy(Some(v)))
}

/// `feat_use.parse_cost`
fn feat_cost(desc: &str) -> Map<String, Value> {
    use std::sync::OnceLock;
    static RE: OnceLock<(regex::Regex, regex::Regex)> = OnceLock::new();
    let (cost, token) = RE.get_or_init(|| {
        (regex::Regex::new(r"(?i)\bCost:\s*([^\n]{0,80})").unwrap(), regex::Regex::new(r"(?i)(?:(\d+)\s*)?\b(PP|MP|Shock|Health|HP)\b").unwrap())
    });
    let text = desc.replace('*', "");
    let mut out = Map::new();
    let Some(m) = cost.captures(&text) else { return out };
    for t in token.captures_iter(&m[1]) {
        let key = match t[2].to_ascii_lowercase().as_str() {
            "pp" => "pp",
            "mp" => "mp",
            "shock" => "shock",
            _ => "hp",
        };
        let n: i64 = t.get(1).map_or(1, |d| d.as_str().parse().unwrap_or(i64::MAX / 4));
        let cur = out.get(key).and_then(|v| v.as_i64()).unwrap_or(0);
        out.insert(key.into(), json!(cur.saturating_add(n)));
    }
    for v in out.values_mut() {
        *v = json!(v.as_i64().unwrap_or(0).min(10));
    }
    out
}

/// `feat_use.parse_limit`
fn feat_limit(desc: &str) -> Option<&'static str> {
    use std::sync::OnceLock;
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"(?i)\bonce\s+(?:per|a|each)\s+(rest|session|day|scene)\b").unwrap());
    let m = re.captures(desc)?;
    Some(if m[1].eq_ignore_ascii_case("session") { "session" } else { "rest" })
}

/// `feat_use.feat_key`
fn feat_key(feat: &Map<String, Value>) -> Option<String> {
    let k = match py_or(&[feat.get("id"), feat.get("name")]) {
        None => String::new(),
        Some(v) => py_str_key(Some(v))?,
    };
    k.is_ascii().then(|| k.to_ascii_lowercase())
}

/// `_ability_cards(db, pc)`; None when a feat holds a value Python would read through str() or a non-ASCII lower()
fn ability_cards(c: &Ctx, pc: &Map<String, Value>) -> Option<Vec<Value>> {
    let look = nd_render::app::data().get("feat_lookup")?;
    let used = pc_prefs(&c.db, pc.get("id")?.as_i64()?)?.get("feat_used").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!({}));
    let used = used.as_object()?.clone();
    let mut cards = vec![];
    for (i, feat) in pc_list(pc, "feats_json")?.iter().enumerate() {
        let Some(feat) = feat.as_object() else { continue };
        let id = py_str_key(feat.get("id"))?;
        let name = py_str_key(Some(feat.get("name").unwrap_or(&json!(""))))?;
        if !name.is_ascii() {
            return None;
        }
        let empty = json!({});
        let cat = look["by_id"].get(&id).or_else(|| look["by_name"].get(name.to_ascii_lowercase())).unwrap_or(&empty);
        let desc = match py_or(&[cat.get("description"), feat.get("notes")]) {
            None => String::new(),
            Some(Value::String(t)) => t.clone(),
            Some(_) => return None,
        };
        if desc.chars().any(|ch| ch.is_numeric() && !ch.is_ascii_digit()) {
            return None;
        }
        let desc = strip(&desc.replace("\n---", ""));
        let key = feat_key(feat)?;
        cards.push(json!({"index": i, "name": py_or(&[feat.get("name")]).cloned().unwrap_or(json!("Feat")),
            "type": py_or(&[feat.get("type"), cat.get("category")]).cloned().unwrap_or(json!("")),
            "rank": py_or(&[feat.get("rank"), cat.get("rank")]).cloned().unwrap_or(json!("")),
            "description": take_chars(&desc, 900), "cost": feat_cost(&desc), "limit": feat_limit(&desc), "used": used.get(&key).cloned().unwrap_or(Value::Null)}));
    }
    Some(cards)
}

/// `char_extras.set_prefs(db, pc, key=value)` (None removes the key)
fn set_pc_pref(c: &Ctx, pc: &Map<String, Value>, key: &str, value: Option<Value>) -> Option<()> {
    let pid = pc.get("id")?.as_i64()?;
    let existing: Option<i64> = c.db.query_row("SELECT id FROM character_prefs WHERE character_id = ?1 LIMIT 1", [pid], |r| r.get(0)).ok();
    let mut data = if existing.is_some() { pc_prefs(&c.db, pid)? } else { Map::new() };
    match value {
        Some(v) => {
            data.insert(key.into(), v);
        }
        None => {
            data.remove(key);
        }
    }
    let text = nd_render::py_dumps(&Value::Object(data));
    match existing {
        Some(id) => {
            orm::update(&c.db, "character_prefs", id, &[("prefs_json", s(&text))])?;
        }
        None => {
            orm::insert(&c.db, "character_prefs", &[("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)), ("character_id", Sql::Integer(pid)), ("prefs_json", s(&text))])?;
        }
    };
    Some(())
}

fn pools(pc: &Map<String, Value>) -> Value {
    json!({"pp": col_int(pc, "pp_current"), "mp": col_int(pc, "mp_current"), "shock": col_int(pc, "shock_current"), "hp": col_int(pc, "current_hp")})
}

fn pc_ability_use(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let index = match b.get("index") {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        _ => return None, // a float, a bool (== an int in Python) or a miss: Python's
    };
    let cards = ability_cards(c, &pc)?;
    let card = cards.iter().find(|k| k["index"].as_i64() == Some(index))?.clone();
    if !card["used"].is_null() && py_truthy(Some(&card["used"])) {
        return None;
    }
    let card_name = card["name"].as_str()?.to_string();
    let cols = [("pp", "pp_current"), ("mp", "mp_current"), ("shock", "shock_current"), ("hp", "current_hp")];
    let mut spend = vec![];
    for (k, _) in cols {
        spend.push(body_int(&b, k)?.clamp(0, 10));
    }
    let mut before = Map::new();
    let mut set: Vec<(&str, Sql)> = vec![];
    for (i, (_, col)) in cols.iter().enumerate() {
        if spend[i] > 0 {
            let have = col_int(&pc, col);
            if spend[i] > have {
                return None;
            }
            before.insert(col.to_string(), json!(have));
            set.push((col, Sql::Integer(have - spend[i])));
        }
    }
    if !set.is_empty() {
        orm::update(&c.db, "player_characters", ids[0], &set)?;
    }
    let feats = pc_list(&pc, "feats_json")?;
    let key = feat_key(feats.get(index as usize)?.as_object()?)?;
    let limit = card["limit"].as_str();
    if let Some(l) = limit {
        let mut used = pc_prefs(&c.db, ids[0])?.get("feat_used").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!({}));
        used.as_object_mut()?.insert(key.clone(), json!(l));
        set_pc_pref(c, &pc, "feat_used", Some(used))?;
    }
    let label = |k: &str| if k == "hp" { "Health".to_string() } else { k.to_uppercase() };
    let paid: Vec<String> = cols.iter().enumerate().filter(|(i, _)| spend[*i] > 0).map(|(i, (k, _))| format!("{} {}", spend[i], label(k))).collect();
    let text = if paid.is_empty() { format!("Used {card_name}") } else { format!("Used {card_name} ({})", paid.join(", ")) };
    char_log(c, &pc, "ability", &text, &json!({"op": "cols", "values": before, "unuse": if limit.is_some() { json!(key) } else { Value::Null }}))?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?;
    json_response(&json!({"ok": true, "cards": ability_cards(c, &pc)?, "pools": pools(&pc)}))
}

fn pc_ability_ready(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let feats = pc_list(&pc, "feats_json")?;
    let i = match b.get("index") {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        _ => return None,
    };
    let feat = feats.get(usize::try_from(i).ok()?)?.as_object()?;
    let key = feat_key(feat)?;
    let mut used = pc_prefs(&c.db, ids[0])?.get("feat_used").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!({}));
    let used_map = used.as_object_mut()?;
    used_map.shift_remove(&key);
    let keep = if used_map.is_empty() { None } else { Some(used.clone()) };
    set_pc_pref(c, &pc, "feat_used", keep)?;
    json_response(&json!({"ok": true, "cards": ability_cards(c, &pc)?}))
}

// ── spending XP (app/advance.py, characters.py character_advance) ──

const ADV_STATS: [(&str, &str); 8] = [("str", "Strength"), ("dex", "Dexterity"), ("bod", "Body"), ("per", "Perception"),
    ("wil", "Willpower"), ("int", "Intellect"), ("cha", "Charisma"), ("itu", "Intuition")];

/// `advance.rank_num`: the first digit of str(rank); a value read through repr, or another script's digit: Python's
fn adv_rank(v: Option<&Value>) -> Option<i64> {
    let t = match py_or(&[v]) {
        None => String::new(),
        Some(Value::String(t)) => t.clone(),
        Some(Value::Number(n)) if n.is_i64() => n.to_string(),
        Some(_) => return None,
    };
    if t.chars().any(|ch| ch.is_numeric() && !ch.is_ascii_digit()) {
        return None;
    }
    Some(t.bytes().find(|b| b.is_ascii_digit()).map_or(0, |b| (b - b'0') as i64))
}

/// `advance.slug`
fn adv_slug(v: Option<&Value>) -> Option<String> {
    let t = match py_or(&[v]) {
        None => String::new(),
        Some(Value::String(t)) if t.is_ascii() => t.to_ascii_lowercase(),
        _ => return None,
    };
    let mut out = String::new();
    for ch in t.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            out.push(ch);
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    Some(out.trim_matches('_').to_string())
}

/// `str(x or "").lower()` for an owned feat's id / name (ASCII only)
fn adv_key(v: Option<&Value>) -> Option<String> {
    let t = match py_or(&[v]) {
        None => String::new(),
        Some(x) => py_str_key(Some(x))?,
    };
    t.is_ascii().then(|| t.to_ascii_lowercase())
}

fn adv_unlocked(ranks: &[i64]) -> i64 {
    let mut top = 1;
    while top < 3 && ranks.iter().filter(|r| **r == top).count() >= 2 {
        top += 1;
    }
    top
}

/// `advance.options(pc, catalog, earned, spent)`
fn adv_options(c: &Ctx, pc: &Map<String, Value>) -> Option<Value> {
    let earned = pc.get("xp").and_then(|v| v.as_i64()).unwrap_or(0);
    let spent = match py_or(&[pc_prefs(&c.db, pc.get("id")?.as_i64()?)?.get("xp_spent")]) {
        None => 0,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        Some(_) => return None,
    };
    let mut stats: std::collections::HashMap<String, i64> = Default::default();
    for st in pc_list(pc, "stats_json")? {
        let Some(o) = st.as_object() else { continue };
        let v = match py_or(&[o.get("value")]) {
            None => 0,
            Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
            Some(_) => return None,
        };
        stats.insert(py_str_key(o.get("id"))?, v);
    }
    let owned = pc_list(pc, "feats_json")?;
    let mut ranks = vec![];
    let mut have: Vec<String> = vec![];
    for f in owned.iter().filter_map(|f| f.as_object()) {
        ranks.push(adv_rank(f.get("rank"))?);
        for k in [adv_key(f.get("id"))?, adv_key(f.get("name"))?] {
            if !k.is_empty() {
                have.push(k);
            }
        }
    }
    let available = (earned - spent).max(0);
    let top = adv_unlocked(&ranks);
    let race = match py_or(&[pc.get("race_id")]) {
        Some(v) => v.clone(),
        None => json!(adv_slug(pc.get("race"))?),
    };
    let prof = match py_or(&[pc.get("profession_id")]) {
        Some(v) => v.clone(),
        None => json!(adv_slug(pc.get("char_class"))?),
    };
    let get = |k: &str| stats.get(k).copied().unwrap_or(0);
    let (phys, ment) = (["str", "dex", "bod", "per"].iter().map(|k| get(k)).sum::<i64>(), ["wil", "int", "cha", "itu"].iter().map(|k| get(k)).sum::<i64>());
    let max_hp = pc.get("max_hp").and_then(|v| v.as_i64()).unwrap_or(0);
    let shock_max = pc.get("shock_max").and_then(|v| v.as_i64()).unwrap_or(0);
    let stat_rows: Vec<Value> = ADV_STATS
        .iter()
        .map(|(sid, label)| {
            let v = get(sid);
            let cost = (v + 1) * 2;
            let mut bits = vec![];
            if ["str", "dex", "bod", "per"].contains(sid) {
                bits.push(format!("PP {phys}\u{2192}{}", phys + 1));
                if max_hp <= 0 {
                    bits.push(format!("HP {}\u{2192}{}", phys + 10, phys + 11));
                }
            } else {
                bits.push(format!("MP {ment}\u{2192}{}", ment + 1));
                if shock_max <= 0 {
                    bits.push(format!("Shock {ment}\u{2192}{}", ment + 1));
                }
            }
            if *sid == "dex" || *sid == "itu" {
                let sp = get("dex") + get("itu");
                bits.push(format!("Speed {sp}\u{2192}{}", sp + 1));
            }
            json!({"id": sid, "label": label, "value": v, "cost": cost, "can": v < 10 && cost <= available, "effect": bits.join(", "), "left": (available - cost).max(0)})
        })
        .collect();
    let mut feats = vec![];
    for f in nd_render::app::data().get("advance_feats")?.as_array()? {
        let cat = f["category"].as_str()?;
        if cat == "Race" && f["race"] != race {
            continue;
        }
        if cat == "Profession" && f["profession"] != prof {
            continue;
        }
        if have.iter().any(|h| Some(h.as_str()) == f["idl"].as_str() || Some(h.as_str()) == f["namel"].as_str()) {
            continue;
        }
        let (rk, cost) = (f["rank"].as_i64()?, f["cost"].as_i64()?);
        let mut why = if rk <= top { String::new() } else { format!("Rank {rk} unlocks after you own two Rank {} feats", rk - 1) };
        if why.is_empty() && cost > available {
            why = format!("needs {} more XP", cost - available);
        }
        let hint = if rk < 3 && top == rk && ranks.iter().filter(|r| **r == rk).count() == 1 { format!("unlocks Rank {} feats", rk + 1) } else { String::new() };
        feats.push(json!({"id": f["id"], "name": f["name"], "category": cat, "rank": rk, "cost": cost, "description": f["description"],
                          "can": why.is_empty(), "why": why, "effect": hint, "left": (available - cost).max(0)}));
    }
    Some(json!({"earned": earned, "spent": spent, "available": available, "unlocked_rank": top, "stats": stat_rows, "feats": feats,
                "race": race, "profession": prof}))
}

fn pc_advance(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    if !pc_maxima(&c.db, &pc)?.4 {
        return None;
    }
    let state = adv_options(c, &pc)?;
    let spent0 = state["spent"].as_i64()?;
    let available = state["available"].as_i64()?;
    let (spent, text) = match b.get("kind").and_then(|k| k.as_str()) {
        Some("spent") => {
            let xp = pc.get("xp").and_then(|v| v.as_i64()).unwrap_or(0);
            let value = body_int(&b, "value")?.min(xp).max(0);
            set_pc_pref(c, &pc, "xp_spent", (value != 0).then(|| json!(value)))?;
            let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?;
            return json_response(&adv_options(c, &pc)?);
        }
        Some("stat") => {
            let want = match b.get("stat") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(t)) => t.clone(),
                _ => return None,
            };
            let row_ = state["stats"].as_array()?.iter().find(|r| r["id"].as_str() == Some(want.as_str()))?;
            let (v, cost, label) = (row_["value"].as_i64()?, row_["cost"].as_i64()?, row_["label"].as_str()?.to_string());
            if v >= 10 || cost > available {
                return None;
            }
            let mut stats = pc_list(&pc, "stats_json")?;
            match stats.iter_mut().find(|x| x.get("id").and_then(|i| i.as_str()) == Some(want.as_str()) && x.is_object()) {
                Some(entry) => {
                    entry.as_object_mut()?.insert("value".into(), json!(v + 1));
                }
                None => stats.push(json!({"id": want, "label": label, "abbr": want.to_uppercase(), "value": v + 1})),
            }
            orm::update(&c.db, "player_characters", ids[0], &[("stats_json", s(&nd_render::py_dumps(&Value::Array(stats))))])?;
            (spent0 + cost, format!("Bought {label} {v} \u{2192} {} for {cost} XP", v + 1))
        }
        Some("feat") => {
            let want = py_str_key(b.get("id"))?;
            let row_ = state["feats"].as_array()?.iter().find(|r| r["id"].as_str() == Some(want.as_str()))?.clone();
            if row_["can"] != json!(true) {
                return None;
            }
            let mut feats = pc_list(&pc, "feats_json")?;
            let rk = row_["rank"].as_i64()?;
            feats.push(json!({"name": row_["name"], "id": row_["id"], "type": row_["category"], "rank": format!("Rank {rk}"), "notes": ""}));
            orm::update(&c.db, "player_characters", ids[0], &[("feats_json", s(&nd_render::py_dumps(&Value::Array(feats))))])?;
            (spent0 + row_["cost"].as_i64()?, format!("Bought the {} feat {} (Rank {rk}) for {} XP", row_["category"].as_str()?, row_["name"].as_str()?, row_["cost"]))
        }
        _ => return None,
    };
    let txt = |k: &str| pc.get(k).and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]").to_string();
    let feats_before = txt("feats_json");
    let undo = if feats_before.chars().count() <= 30000 {
        json!({"op": "advance", "stats_json": txt("stats_json"), "feats_json": feats_before, "spent": spent0})
    } else {
        json!({})
    };
    set_pc_pref(c, &pc, "xp_spent", Some(json!(spent)))?;
    char_log(c, &pc, "advance", &text, &undo)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", ids[0])?;
    json_response(&adv_options(c, &pc)?)
}

// ── custom-sheet resource tracks (characters.py character_resource_async, app/sheet_systems.py) ──

/// `sheet_systems._num` for integers: Ok(Some(n)) / Ok(None) (nothing, or text that is no number); Err for a float (its repr) - Python's
fn sheet_num(v: Option<&Value>) -> Result<Option<i64>, ()> {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(_)) => Ok(None),
        Some(Value::Number(n)) => n.as_i64().map(Some).ok_or(()),
        Some(Value::String(t)) => sheet_num_text(t),
        Some(_) => Err(()),
    }
}

fn sheet_num_text(t: &str) -> Result<Option<i64>, ()> {
    let t = strip(t);
    if t.is_empty() {
        return Ok(None);
    }
    match py_int_text(&t) {
        Ok(n) => Ok(Some(n)),
        Err(PyErr::Value) if t.parse::<f64>().is_err() && t.is_ascii() => Ok(None),
        Err(_) => Err(()),
    }
}

/// `short_label`
fn sheet_short_label(label: &str) -> Option<String> {
    if label.contains('\n') || label.chars().any(|ch| ('\x1c'..='\x1f').contains(&ch)) {
        return None;
    }
    let re = regex::Regex::new(r"\s*[(\[—–].*$").ok()?;
    let cut = strip(&re.replace(label, ""));
    Some(if cut.is_empty() { strip(label) } else { cut })
}

fn pc_resource(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let b = c.json()?;
    let pc = managed_pc(c, ids[0])?;
    let tid = pc.get("sheet_template_id").and_then(|v| v.as_i64()).filter(|t| *t != 0)?;
    let tpl = row(&c.db, "SELECT * FROM sheet_templates WHERE id = ?1", tid)?;
    if tpl.get("sheet_mode").and_then(|v| v.as_str()) != Some("custom") {
        return None;
    }
    let fields = match serde_json::from_str::<Value>(tpl.get("fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a,
        Ok(_) => vec![],
        Err(_) => return None,
    };
    let raw_cf = pc.get("custom_fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}").to_string();
    let mut cf = match canonical_json(&raw_cf) {
        Some(Value::Object(o)) => o,
        Some(_) => Map::new(),
        None => return None,
    };
    let fid = match py_or(&[b.get("field_id")]) {
        None => String::new(),
        Some(Value::String(t)) => t.clone(),
        Some(_) => return None,
    };
    let field = fields.iter().filter_map(|f| f.as_object()).find(|f| {
        f.get("type").and_then(|t| t.as_str()) == Some("resource") && py_truthy(f.get("id")) && f.get("id").and_then(|i| i.as_str()) == Some(fid.as_str())
    })?;
    // an earlier resource field with a non-string id would still be read by Python: only plain templates are ported
    if fields.iter().filter_map(|f| f.as_object()).any(|f| f.get("type").and_then(|t| t.as_str()) == Some("resource") && py_truthy(f.get("id")) && !f["id"].is_string()) {
        return None;
    }
    let dv = match py_or(&[field.get("default_value")]) {
        None => "0/0".to_string(),
        Some(Value::String(t)) => t.clone(),
        Some(_) => return None,
    };
    let mut halves = dv.split('/');
    let dcur = sheet_num_text(halves.next().unwrap_or("")).ok()?.unwrap_or(0);
    let dmax = if dv.contains('/') { sheet_num_text(halves.next().unwrap_or("")).ok()?.unwrap_or(0) } else { 0 };
    let cur = sheet_num(cf.get(&format!("{fid}_current"))).ok()?.unwrap_or(dcur);
    let mx = sheet_num(cf.get(&format!("{fid}_max"))).ok()?.unwrap_or(dmax);
    let label_src = match py_or(&[field.get("label")]) {
        Some(Value::String(t)) => t.clone(),
        Some(_) => String::new(),
        None => fid.clone(),
    };
    let label = sheet_short_label(&label_src)?;
    let val = body_int(&b, "value")?;
    let delta = b.get("action").map_or(false, |a| a.as_str() == Some("delta"));
    if !delta && b.get("action").is_some_and(|a| !a.is_string()) {
        return None;
    }
    let mut new = if delta { (cur + val).max(0) } else { val.max(0) };
    if mx != 0 {
        new = new.min(mx);
    }
    if new != cur {
        let prev = cf.get(&format!("{fid}_current")).cloned().unwrap_or(json!(cur));
        let mut values = Map::new();
        values.insert(format!("{fid}_current"), prev);
        char_log(c, &pc, "resource", &format!("{label} {cur} \u{2192} {new}"), &json!({"op": "cf", "values": values}))?;
    }
    cf.insert(format!("{fid}_current"), json!(new));
    orm::update(&c.db, "player_characters", ids[0], &[("custom_fields_json", s(&nd_render::py_dumps(&Value::Object(cf))))])?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"field_id": fid, "current": new, "max": mx}))
}

// ── the spotlight (gallery.py image_spotlight_send / _clear, main.py api_spotlight) ──
// Python keeps a 2-second in-process cache of GET /api/spotlight that its own writes clear; with the gateway answering that GET as
// well, a spotlight the gateway writes is seen at once.

fn spotlight_state(c: &mut Ctx, _: &str) -> Option<Response<Body>> {
    let w = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", c.world_id()?)?;
    let num = |k: &str| w.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
    let val = |k: &str| w.get(k).cloned().unwrap_or(Value::Null);
    json_response(&json!({"version": num("spotlight_version"), "image_url": val("spotlight_image_url"), "label": val("spotlight_label"),
        "audio_version": num("now_playing_version"), "audio_url": val("now_playing_url"), "audio_label": val("now_playing_label"),
        "audio_loop": py_truthy(w.get("now_playing_loop"))}))
}

/// `live.signal_all("spot")`: open pages fetch the spotlight again
fn signal_spot(c: &Ctx) -> Option<()> {
    c.db.execute("INSERT INTO shared_state (key, n) VALUES ('sigall:spot', 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", []).ok().map(|_| ())
}

fn spotlight_send(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let wid = images_editor(c)?;
    let b = c.json()?;
    let url = b.get("url").and_then(|u| u.as_str()).filter(|u| !u.is_empty())?.to_string();
    let entries = crate::gallery::all_world_image_urls(&c.db, wid)?;
    let label = take_chars(entries.iter().find(|e| e.get("url").and_then(|u| u.as_str()) == Some(url.as_str()))?.get("name")?.as_str()?, 256);
    let w = row(&c.db, "SELECT spotlight_version FROM worlds WHERE id = ?1", wid)?;
    let version = w.get("spotlight_version").and_then(|v| v.as_i64()).unwrap_or(0) + 1;
    orm::update(&c.db, "worlds", wid, &[("spotlight_image_url", s(&url)), ("spotlight_label", s(&label)), ("spotlight_version", Sql::Integer(version))])?;
    let last: Option<String> = c.db.query_row("SELECT url FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 1", [wid], |r| r.get(0)).ok();
    if last.as_deref() != Some(url.as_str()) {
        orm::insert(&c.db, "world_handouts", &[("world_id", Sql::Integer(wid)), ("url", s(&url)), ("label", s(&label))])?;
    }
    signal_spot(c)?;
    json_response(&json!({"ok": true, "version": version}))
}

fn spotlight_clear(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let wid = images_editor(c)?;
    let w = row(&c.db, "SELECT spotlight_version FROM worlds WHERE id = ?1", wid)?;
    let version = w.get("spotlight_version").and_then(|v| v.as_i64()).unwrap_or(0) + 1;
    orm::update(&c.db, "worlds", wid, &[("spotlight_image_url", Sql::Null), ("spotlight_label", Sql::Null), ("spotlight_version", Sql::Integer(version))])?;
    signal_spot(c)?;
    json_response(&json!({"ok": true, "version": version}))
}

// ── the combat tracker (app/routers/combat.py, app/combat_state.py, app/combat_turns.py) ──

/// `combat_state._int(v, default, lo, hi)`: int(v), else int(float(v)), else the default; clamped. Err for text Python's int() / float()
/// might read in its own way (underscores, "inf", other scripts' digits)
fn cs_int(v: Option<&Value>, default: i64, lo: i64, hi: i64) -> Result<i64, ()> {
    let n = match v {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => default,
        Some(Value::Bool(b)) => *b as i64,
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => i,
            None => {
                let f = n.as_f64().ok_or(())?;
                if !f.is_finite() {
                    return Err(());
                }
                f.trunc().clamp(-1e18, 1e18) as i64
            }
        },
        Some(Value::String(t)) => {
            if !t.is_ascii() || t.contains('_') {
                return Err(());
            }
            match py_int_text(t) {
                Ok(i) => i,
                Err(PyErr::Value) => {
                    let st = t.trim();
                    let plain = regex::Regex::new(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$").map_err(|_| ())?;
                    if plain.is_match(st) {
                        let f: f64 = st.parse().map_err(|_| ())?;
                        if !f.is_finite() {
                            return Err(());
                        }
                        f.trunc().clamp(-1e18, 1e18) as i64
                    } else if ["inf", "infinity", "nan"].contains(&st.trim_start_matches(['+', '-']).to_ascii_lowercase().as_str()) {
                        return Err(()); // int(float("inf")) raises, int(float("nan")) too: Python's
                    } else {
                        default
                    }
                }
                Err(_) => return Err(()),
            }
        }
    };
    Ok(n.clamp(lo, hi))
}

/// `" ".join(str(v or default).split())`: a string, an integer or a bool (Python's str() of them); anything else: Err
fn cs_text(v: Option<&Value>, default: &str) -> Result<String, ()> {
    let t = match v {
        None | Some(Value::Null) => default.to_string(),
        Some(Value::Bool(false)) => default.to_string(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::String(s)) if s.is_empty() => default.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) if n.as_i64() == Some(0) => default.to_string(),
        Some(Value::Number(n)) if n.is_i64() => n.to_string(),
        Some(Value::Array(a)) if a.is_empty() => default.to_string(),
        Some(Value::Object(o)) if o.is_empty() => default.to_string(),
        _ => return Err(()),
    };
    Ok(t)
}

fn new_cid() -> String {
    format!("c-{:08x}", rand::random::<u32>())
}

/// `_conditions`: condition names, a name repeated is a stack (up to 9 of one), at most 30
fn cs_conditions(raw: Option<&Value>) -> Result<Vec<String>, ()> {
    let mut out: Vec<String> = vec![];
    let mut count: Vec<(String, usize)> = vec![];
    for c in raw.and_then(|r| r.as_array()).map(|a| a.as_slice()).unwrap_or(&[]) {
        let Some(c) = c.as_str() else { continue };
        let name = take_chars(&nd_render::text::py_split_join(c), 40);
        if !name.is_ascii() {
            return Err(()); // casefold() beyond ASCII: Python's
        }
        let key = name.to_ascii_lowercase();
        let n = count.iter().find(|(k, _)| *k == key).map_or(0, |(_, n)| *n);
        if !name.is_empty() && n < 9 {
            match count.iter_mut().find(|(k, _)| *k == key) {
                Some(e) => e.1 += 1,
                None => count.push((key, 1)),
            }
            out.push(name);
        }
        if out.len() >= 30 {
            break;
        }
    }
    Ok(out)
}

/// `_counts(raw, cap)`
fn cs_counts(raw: Option<&Value>, cap: i64) -> Result<Map<String, Value>, ()> {
    let mut out = Map::new();
    if let Some(Value::Object(o)) = raw {
        for (k, v) in o {
            if !strip(k).is_empty() && out.len() < 12 {
                out.insert(take_chars(&nd_render::text::py_split_join(k), 40), json!(cs_int(Some(v), 0, 0, cap)?));
            }
        }
    }
    Ok(out)
}

/// `normalize_combatant`
fn cs_combatant(c: &Map<String, Value>) -> Result<Map<String, Value>, ()> {
    let source = match c.get("source").and_then(|v| v.as_str()) {
        Some(s @ ("pc" | "entity" | "manual")) => s.to_string(),
        _ => "manual".into(),
    };
    let max_hp = cs_int(c.get("max_hp"), 0, 0, 100_000)?;
    let max_shock = cs_int(c.get("max_shock"), 0, 0, 100_000)?;
    let conditions = cs_conditions(c.get("conditions"))?;
    let mut durations = Map::new();
    if let Some(Value::Object(raw)) = c.get("cond_dur") {
        for (k, v) in raw {
            if !k.is_ascii() {
                return Err(());
            }
            if let Some(name) = conditions.iter().rev().find(|x| x.eq_ignore_ascii_case(k)) {
                let n = cs_int(Some(v), 0, 0, 99)?;
                if n != 0 {
                    durations.insert(name.clone(), json!(n));
                }
            }
        }
    }
    let mut res = Map::new();
    if let Some(Value::Object(raw)) = c.get("res") {
        for (k, v) in raw {
            let Some(v) = v.as_object() else { continue };
            if res.len() >= 12 {
                continue;
            }
            let mx = cs_int(v.get("max"), 0, 0, 9999)?;
            let label = take_chars(&nd_render::text::py_split_join(&cs_text(v.get("label"), k)?), 40);
            res.insert(take_chars(k, 32), json!({"cur": cs_int(v.get("cur"), mx, 0, mx)?, "max": mx, "label": label}));
        }
    }
    let saves = c.get("saves").and_then(|s| s.as_object());
    let save = |k: &str| cs_int(saves.and_then(|s| s.get(k)).or(Some(&json!(0))), 0, 0, 3);
    let id = take_chars(&cs_text(c.get("id"), "")?, 40);
    let name = take_chars(&nd_render::text::py_split_join(&cs_text(c.get("name"), "")?), 120);
    let opt_id = |k: &str| -> Result<Value, ()> {
        let n = cs_int(c.get(k), 0, 0, (1i64 << 31) - 1)?;
        Ok(if n == 0 { Value::Null } else { json!(n) })
    };
    let notes = take_chars(&cs_text(c.get("notes"), "")?, 2000);
    let mut m = Map::new();
    m.insert("id".into(), json!(if id.is_empty() { new_cid() } else { id }));
    m.insert("name".into(), json!(if name.is_empty() { "Combatant".to_string() } else { name }));
    m.insert("source".into(), json!(source));
    m.insert("pc_id".into(), opt_id("pc_id")?);
    m.insert("entity_id".into(), opt_id("entity_id")?);
    m.insert("initiative".into(), json!(cs_int(c.get("initiative"), 0, -9999, 9999)?));
    m.insert("tiebreak".into(), json!(cs_int(c.get("tiebreak"), 0, -9999, 9999)?));
    m.insert("init_bonus".into(), json!(cs_int(c.get("init_bonus"), 0, -99, 99)?));
    m.insert("max_hp".into(), json!(max_hp));
    m.insert("hp".into(), json!(cs_int(c.get("hp"), max_hp, 0, max_hp)?));
    m.insert("temp_hp".into(), json!(cs_int(c.get("temp_hp"), 0, 0, 9999)?));
    m.insert("max_shock".into(), json!(max_shock));
    m.insert("shock".into(), json!(cs_int(c.get("shock"), 0, 0, max_shock)?));
    m.insert("armor".into(), json!(cs_int(c.get("armor"), 0, 0, 999)?));
    m.insert("conditions".into(), json!(conditions));
    m.insert("cond_dur".into(), Value::Object(durations));
    m.insert("res".into(), Value::Object(res));
    m.insert("used".into(), Value::Object(cs_counts(c.get("used"), 9)?));
    m.insert("saves".into(), json!({"ok": save("ok")?, "fail": save("fail")?, "ko": save("ko")?}));
    m.insert("wounds".into(), json!(cs_int(c.get("wounds"), 0, 0, 99)?));
    m.insert("press_on".into(), json!(py_truthy(c.get("press_on"))));
    m.insert("counters".into(), Value::Object(cs_counts(c.get("counters"), 99)?));
    m.insert("notes".into(), json!(notes));
    m.insert("status".into(), json!(if c.get("status").and_then(|v| v.as_str()) == Some("dead") { "dead" } else { "" }));
    m.insert("visible".into(), json!(c.get("visible") != Some(&Value::Bool(false))));
    m.insert("name_known".into(), json!(match c.get("name_known") { None => source == "pc", Some(v) => py_truthy(Some(v)) }));
    Ok(m)
}

/// `normalize_state(body)`: Err(()) both for a body Python refuses (its 400 / 422) and one this port leaves to it
fn cs_normalize(body: &Map<String, Value>) -> Result<Map<String, Value>, ()> {
    let raw = body.get("combatants").and_then(|v| v.as_array()).ok_or(())?;
    if raw.len() > 60 {
        return Err(());
    }
    let mut combatants: Vec<Value> = vec![];
    let mut seen: Vec<String> = vec![];
    for c in raw {
        let mut n = cs_combatant(c.as_object().ok_or(())?)?;
        let id = n["id"].as_str().unwrap_or("").to_string();
        if seen.contains(&id) {
            n.insert("id".into(), json!(new_cid()));
        }
        seen.push(n["id"].as_str().unwrap_or("").to_string());
        combatants.push(Value::Object(n));
    }
    let mut out = Map::new();
    let last = (combatants.len() as i64 - 1).max(0);
    out.insert("round_num".into(), json!(cs_int(body.get("round_num"), 1, 1, 9999)?));
    out.insert("active_idx".into(), json!(cs_int(body.get("active_idx"), 0, 0, last)?));
    if let Some(r) = body.get("ruleset") {
        let known = nd_render::app::data().get("combat_rulesets").and_then(|m| m.as_object()).ok_or(())?;
        out.insert("ruleset".into(), json!(match r.as_str() { Some(s) if s.is_empty() || known.contains_key(s) => s, _ => "" }));
    }
    if body.contains_key("turn") {
        let raw = body.get("turn").and_then(|t| t.as_object()).cloned().unwrap_or_default();
        let ids: Vec<&str> = combatants.iter().filter_map(|c| c["id"].as_str()).collect();
        let mut acted: Vec<String> = vec![];
        for i in raw.get("acted").and_then(|a| a.as_array()).map(|a| a.as_slice()).unwrap_or(&[]) {
            if let Some(i) = i.as_str() {
                if ids.contains(&i) && !acted.iter().any(|a| a == i) {
                    acted.push(i.to_string());
                }
            }
        }
        let pick = |k: &str| raw.get(k).and_then(|v| v.as_str()).filter(|v| ids.contains(v)).map_or(Value::Null, |v| json!(v));
        let mode = match raw.get("mode").and_then(|v| v.as_str()) { Some(m @ ("initiative" | "fluid")) => m, _ => "" };
        out.insert("turn".into(), json!({"active_id": pick("active_id"), "acted": acted, "picker": pick("picker"), "mode": mode}));
    }
    if let Some(Value::String(n)) = body.get("name") {
        if !strip(n).is_empty() {
            out.insert("name".into(), json!(take_chars(&nd_render::text::py_split_join(n), 256)));
        }
    }
    if body.contains_key("log") {
        let mut log = vec![];
        for e in body.get("log").and_then(|l| l.as_array()).map(|a| a.as_slice()).unwrap_or(&[]) {
            let Some(e) = e.as_object() else { continue };
            if !py_truthy(e.get("text")) {
                continue;
            }
            log.push(json!({"t": cs_int(e.get("t"), 0, 0, 1i64 << 53)?, "round": cs_int(e.get("round"), 1, 1, 9999)?,
                            "text": take_chars(&nd_render::text::py_split_join(&cs_text(e.get("text"), "")?), 200)}));
        }
        let skip = log.len().saturating_sub(300);
        out.insert("log".into(), Value::Array(log.split_off(skip)));
    }
    out.insert("combatants".into(), Value::Array(combatants));
    Ok(out)
}

/// `_scoped`: the encounter of the active world, for a GM
fn combat_scoped(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    if !c.p.is_gm {
        return None;
    }
    let cs = row(&c.db, "SELECT * FROM combat_sessions WHERE id = ?1", id)?;
    (cs.get("world_id")?.as_i64() == c.world_id()).then_some(cs)
}

/// `combat_turns.touch(cs)` + commit: the revision moves on, the TV and the other trackers hear "combat"; returns the new revision
fn combat_touch(c: &Ctx, cs: &Map<String, Value>) -> Option<i64> {
    let rev = cs.get("rev").and_then(|v| v.as_i64()).unwrap_or(0) + 1;
    orm::update(&c.db, "combat_sessions", cs.get("id")?.as_i64()?, &[("rev", Sql::Integer(rev))])?;
    orm::signal(&c.db, cs.get("world_id")?.as_i64()?, "combat")?;
    Some(rev)
}

fn combat_new(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let wid = c.world_id().unwrap_or(1);
    let known = nd_render::app::data().get("combat_rulesets")?.as_object()?;
    let wanted = f.get("ruleset").unwrap_or("");
    let rules = if known.contains_key(wanted) {
        wanted.to_string()
    } else {
        // default_ruleset: what the world's characters play (Hunt / Asterion before anything else), else Neon & Dragons
        let by_slug = nd_render::app::data().get("combat_template_rulesets")?.as_object()?;
        let mut st = c.db.prepare("SELECT t.slug FROM player_characters p LEFT JOIN sheet_templates t ON t.id = p.sheet_template_id WHERE p.world_id = ?1 ORDER BY p.id").ok()?;
        let slugs: Vec<Option<String>> = st.query_map([wid], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
        let hit = slugs.iter().flatten().find_map(|s| by_slug.get(s).and_then(|r| r.as_str()).filter(|r| *r == "hunt" || *r == "asterion"));
        match hit {
            Some(r) => r.to_string(),
            None => "nd".to_string(),
        }
    };
    let name = take_chars(&strip(f.get("name").filter(|n| !n.is_empty()).unwrap_or("New Encounter")), 256);
    let name = if name.is_empty() { "New Encounter".to_string() } else { name };
    let id = orm::insert(&c.db, "combat_sessions", &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("ruleset", s(&rules))])?;
    redirect(&format!("/combat/{id}"))
}

fn combat_save_state(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let cs = combat_scoped(c, ids[0])?;
    let body = c.json()?;
    let state = cs_normalize(&body).ok()?;
    let rev = cs.get("rev").and_then(|v| v.as_i64()).unwrap_or(0);
    let sent = match body.get("rev") {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        Some(Value::Bool(b)) => Some(*b as i64),
        _ => None,
    };
    if sent.is_some_and(|r| r != rev) {
        return None; // the 409 with the current state: Python's
    }
    let mut set: Vec<(&str, Sql)> = vec![
        ("combatants_json", s(&nd_render::py_dumps(&state["combatants"]))),
        ("round_num", Sql::Integer(state["round_num"].as_i64()?)),
        ("active_idx", Sql::Integer(state["active_idx"].as_i64()?)),
    ];
    if let Some(n) = state.get("name") {
        set.push(("name", s(n.as_str()?)));
    }
    if let Some(r) = state.get("ruleset") {
        set.push(("ruleset", s(r.as_str()?)));
    }
    if let Some(t) = state.get("turn") {
        set.push(("turn_json", s(&nd_render::py_dumps(t))));
    }
    if let Some(l) = state.get("log") {
        set.push(("log_json", s(&nd_render::py_dumps(l))));
    }
    orm::update(&c.db, "combat_sessions", ids[0], &set)?;
    let rev = combat_touch(c, &cs)?;
    orm::touch(&c.db, cs.get("world_id")?.as_i64()?)?;
    json_response(&json!({"ok": true, "rev": rev}))
}

fn combat_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    combat_scoped(c, ids[0])?;
    c.db.execute("DELETE FROM combat_sessions WHERE id = ?1", [ids[0]]).ok()?;
    redirect("/combat")
}

fn combat_link_session(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    combat_scoped(c, ids[0])?;
    let wid = c.world_id()?;
    let gs = match f.get("game_session_id").filter(|v| !v.is_empty()) {
        Some(v) => {
            let n = match py_int_text(v) {
                Ok(n) => n,
                Err(PyErr::Value) => 0,
                Err(_) => return None,
            };
            let gs: i64 = c.db.query_row("SELECT id FROM game_sessions WHERE id = ?1 AND world_id = ?2", [n, wid], |r| r.get(0)).ok()?;
            Sql::Integer(gs)
        }
        None => Sql::Null,
    };
    orm::update(&c.db, "combat_sessions", ids[0], &[("game_session_id", gs)])?;
    redirect(&format!("/combat/{}", ids[0]))
}

fn combat_unlink_session(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    combat_scoped(c, ids[0])?;
    orm::update(&c.db, "combat_sessions", ids[0], &[("game_session_id", Sql::Null)])?;
    redirect(&format!("/combat/{}", ids[0]))
}

/// `combat_rules.is_out(c, ruleset)` for a stored combatant; None for a value int() would read in its own way
fn combat_is_out(c: &Value, rules: &Value) -> Option<bool> {
    if c.get("status").and_then(|v| v.as_str()) == Some("dead") {
        return Some(true);
    }
    let num = |k: &str| -> Option<i64> {
        match c.get(k) {
            None | Some(Value::Null) | Some(Value::Bool(false)) => Some(0),
            Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
            _ => None,
        }
    };
    let down = num("max_hp")? > 0 && num("hp")? <= 0;
    if !down {
        return Some(false);
    }
    let skip = rules["skip"].as_str()?;
    Some(skip == "all" || (skip == "enemies" && c.get("source").and_then(|v| v.as_str()) != Some("pc")))
}

/// `_summary_text(cs)`
fn combat_summary(cs: &Map<String, Value>) -> Option<String> {
    let all = nd_render::app::data().get("combat_rulesets")?;
    let ruleset = cs.get("ruleset").and_then(|v| v.as_str()).unwrap_or("");
    let rules = all.get(ruleset).unwrap_or(&all["generic"]);
    let combatants: Vec<Value> = match serde_json::from_str::<Value>(cs.get("combatants_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a.into_iter().filter(|c| c.is_object()).collect(),
        Ok(_) => vec![],
        Err(_) => return None,
    };
    let num = |c: &Value, k: &str| -> Option<i64> {
        match c.get(k) {
            None | Some(Value::Null) => Some(0),
            Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
            _ => None,
        }
    };
    let name = |c: &Value| -> Option<String> {
        match c.get("name") {
            Some(Value::String(t)) => Some(t.clone()),
            Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()),
            None | Some(Value::Null) => Some("None".into()),
            _ => None,
        }
    };
    let mut standing = vec![];
    let mut down = vec![];
    let mut dead = vec![];
    for c in &combatants {
        let up = !combat_is_out(c, rules)? && !(num(c, "hp")? <= 0 && num(c, "max_hp")? > 0);
        if up {
            standing.push(c);
        } else if c.get("status").and_then(|v| v.as_str()) == Some("dead") {
            dead.push(c);
        } else {
            down.push(c);
        }
    }
    let hp_label = rules["hp_label"].as_str()?;
    let mut lines = vec![format!("⚔ {} — ended in round {} ({}).", name(&Value::Object(cs.clone()))?,
                                 cs.get("round_num").and_then(|v| v.as_i64()).filter(|n| *n != 0).unwrap_or(1), rules["label"].as_str()?)];
    if !standing.is_empty() {
        let parts: Option<Vec<String>> = standing.iter().map(|c| Some(format!("{} ({}/{} {hp_label})", name(c)?,
            c.get("hp").map_or(Some(0), |v| v.as_i64())?, c.get("max_hp").map_or(Some(0), |v| v.as_i64())?))).collect();
        lines.push(format!("Standing: {}.", parts?.join(", ")));
    }
    if !down.is_empty() {
        lines.push(format!("Down: {}.", down.iter().map(|c| name(c)).collect::<Option<Vec<_>>>()?.join(", ")));
    }
    if !dead.is_empty() {
        lines.push(format!("Dead: {}.", dead.iter().map(|c| name(c)).collect::<Option<Vec<_>>>()?.join(", ")));
    }
    let log: Vec<Value> = match serde_json::from_str::<Value>(cs.get("log_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a,
        Ok(_) => return None, // iterating a non-list: Python's
        Err(_) => vec![],
    };
    let mut notable = vec![];
    for e in &log {
        let Some(o) = e.as_object() else { continue };
        if !py_truthy(o.get("text")) {
            continue;
        }
        let text = o.get("text")?.as_str()?.to_string();
        if ["—", "↶", "↷"].iter().any(|p| text.starts_with(p)) {
            continue;
        }
        if [" is down", " is Broken", "Shattered", "dies", "executed", "Wound", "Presses On", "takes "].iter().any(|w| text.contains(w)) {
            let round = match o.get("round") {
                None => "1".to_string(),
                Some(Value::Number(n)) if n.is_i64() => n.to_string(),
                Some(Value::String(t)) => t.clone(),
                _ => return None,
            };
            notable.push(format!("R{round} {text}"));
        }
    }
    if !notable.is_empty() {
        let skip = notable.len().saturating_sub(10);
        lines.push(format!("Highlights: {}", notable[skip..].join("; ")));
    }
    Some(lines.join("\n"))
}

fn combat_finish(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let cs = combat_scoped(c, ids[0])?;
    if py_truthy(cs.get("is_template")) {
        return None;
    }
    if !cs.get("finished_at").is_none_or(|v| v.is_null()) {
        return json_response(&json!({"ok": true, "already": true}));
    }
    let text = combat_summary(&cs)?;
    let wid = c.world_id()?;
    let now = orm::now_stamp();
    let fact = orm::insert(&c.db, "facts", &[("world_id", Sql::Integer(wid)), ("game_session_id", cs.get("game_session_id").and_then(|v| v.as_i64()).map_or(Sql::Null, Sql::Integer)),
        ("content", s(&text)), ("visible_to_players", Sql::Integer(0)), ("tags", s("combat")), ("author_id", Sql::Integer(c.p.user_id)), ("updated_at", s(&now))])?;
    orm::update(&c.db, "combat_sessions", ids[0], &[("finished_at", s(&now))])?;
    combat_touch(c, &cs)?;
    orm::touch(&c.db, wid)?;
    json_response(&json!({"ok": true, "fact_id": fact}))
}

fn combat_reopen(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let cs = combat_scoped(c, ids[0])?;
    orm::update(&c.db, "combat_sessions", ids[0], &[("finished_at", Sql::Null)])?;
    combat_touch(c, &cs)?;
    json_response(&json!({"ok": true}))
}

/// `_fresh(c, keep_hp)`: a combatant as it starts a fight
fn combat_fresh(c: &Value, keep_hp: bool) -> Option<Value> {
    let mut c = c.as_object()?.clone();
    c.insert("id".into(), json!(new_cid()));
    let pc = c.get("source").and_then(|v| v.as_str()) == Some("pc");
    if !keep_hp {
        let hp = c.get("max_hp").or_else(|| c.get("hp")).cloned().unwrap_or(json!(0));
        c.insert("hp".into(), hp);
        let shock = if pc { c.get("shock").cloned().unwrap_or(json!(0)) } else { c.get("max_shock").cloned().unwrap_or(json!(0)) };
        c.insert("shock".into(), shock);
        if let Some(Value::Object(res)) = c.get_mut("res") {
            for r in res.values_mut() {
                if let Some(r) = r.as_object_mut() {
                    if py_truthy(r.get("max")) {
                        let mx = r.get("max").cloned()?;
                        r.insert("cur".into(), mx);
                    }
                }
            }
        }
    }
    for (k, v) in [("temp_hp", json!(0)), ("conditions", json!([])), ("cond_dur", json!({})), ("used", json!({})),
                   ("saves", json!({"ok": 0, "fail": 0, "ko": 0})), ("status", json!("")), ("press_on", json!(false))] {
        c.insert(k.into(), v);
    }
    if !pc {
        c.insert("initiative".into(), json!(0));
        c.insert("tiebreak".into(), json!(0));
    }
    Some(Value::Object(c))
}

/// `_copy_encounter`: the new encounter's id (`with_party`: the world's characters not in it yet are added - native sheets only)
fn combat_copy(c: &Ctx, src: &Map<String, Value>, name: &str, npcs_only: bool, template: bool) -> Option<i64> {
    combat_copy_with(c, src, name, npcs_only, template, false)
}

/// `pc_to_combatant(pc, db)` for a native (Neon & Dragons / Worm) sheet; None for a custom system's (its vital and extra tracks come
/// from the template's sheet system: Python's)
fn pc_combatant(c: &Ctx, pc: &Map<String, Value>) -> Option<Value> {
    let (hp, shock, pp, mp, native) = pc_maxima(&c.db, pc)?;
    if !native {
        return None;
    }
    let stats = stat_values(pc)?;
    let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let conds: Vec<Value> = match serde_json::from_str::<Value>(pc.get("conditions_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_string()).collect(),
        Ok(_) | Err(_) => vec![],
    };
    Some(json!({"res": {"pp": {"cur": col_int(pc, "pp_current"), "max": pp, "label": "PP"}, "mp": {"cur": col_int(pc, "mp_current"), "max": mp, "label": "MP"}},
                "id": new_cid(), "name": pc.get("name")?, "source": "pc", "pc_id": pc.get("id")?, "entity_id": null, "initiative": 0,
                "init_bonus": get("dex") + get("itu"), "max_hp": hp, "hp": col_int(pc, "current_hp"), "max_shock": shock,
                "shock": col_int(pc, "shock_current"), "armor": 0, "conditions": conds, "notes": ""}))
}

/// `entity_to_combatant`: health / armor / speed read from the entity's fields the way the creature templates name them
fn entity_combatant(e: &Map<String, Value>) -> Option<Value> {
    let raw = e.get("custom_fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
    let Value::Object(fields) = canonical_json(raw)? else { return None }; // not an object: Python's error
    let int_or = |v: Option<&Value>, d: i64| -> Option<i64> {
        match json_py_int(v)? {
            Err(()) => Some(d),
            Ok(n) => i64::try_from(n).ok(),
        }
    };
    let pick = |keys: &[&str], d: i64| -> Option<i64> {
        for k in keys {
            match fields.get(*k) {
                None | Some(Value::Null) => continue,
                Some(Value::String(t)) if t.is_empty() => continue,
                v => return int_or(v, d),
            }
        }
        Some(d)
    };
    let health = pick(&["health", "hp", "flesh", "max_hp", "max_health", "hit_points"], 5)?;
    Some(json!({"id": new_cid(), "name": e.get("name")?, "source": "entity", "pc_id": null, "entity_id": e.get("id")?, "initiative": 0,
                "init_bonus": pick(&["speed", "initiative", "init"], 0)?, "max_hp": health, "hp": health, "max_shock": 0, "shock": 0,
                "armor": int_or(fields.get("armor"), 0)?, "conditions": [], "notes": ""}))
}

/// `party_launch_combat` (full party access): a new encounter with every member in it
fn party_launch_combat(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (p, level) = party_for_write(c, ids[0])?;
    if level != "full" {
        return None;
    }
    let mut combatants = vec![];
    for (table, field) in [("player_characters", "member_pc_ids_json"), ("entities", "member_entity_ids_json")] {
        let ids_in = int_list(p.get(field))?;
        if ids_in.is_empty() {
            continue;
        }
        let marks: Vec<String> = ids_in.iter().map(|i| i.to_string()).collect();
        let mut st = c.db.prepare(&format!("SELECT * FROM {table} WHERE id IN ({}) ORDER BY id", marks.join(","))).ok()?;
        let rows: Vec<Map<String, Value>> = st.query_map([], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        for r in &rows {
            combatants.push(if table == "entities" { entity_combatant(r)? } else { pc_combatant(c, r)? });
        }
    }
    let wid = p.get("world_id")?.as_i64()?;
    let name = format!("{} Encounter", p.get("name").and_then(|v| v.as_str()).unwrap_or("None"));
    let id = orm::insert(&c.db, "combat_sessions", &[("world_id", Sql::Integer(wid)), ("name", s(&name)), ("party_id", Sql::Integer(ids[0])),
        ("combatants_json", s(&nd_render::py_dumps(&Value::Array(combatants))))])?;
    orm::touch(&c.db, wid)?;
    json_response(&json!({"redirect": format!("/combat/{id}")}))
}

fn combat_copy_with(c: &Ctx, src: &Map<String, Value>, name: &str, npcs_only: bool, template: bool, with_party: bool) -> Option<i64> {
    let rows: Vec<Value> = match serde_json::from_str::<Value>(src.get("combatants_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_object()).collect(),
        Ok(_) => vec![],
        Err(_) => return None,
    };
    let mut fresh = vec![];
    for r in rows.iter().filter(|r| !(npcs_only && r.get("source").and_then(|v| v.as_str()) == Some("pc"))) {
        fresh.push(combat_fresh(r, r.get("source").and_then(|v| v.as_str()) == Some("pc"))?);
    }
    if with_party {
        let have: Vec<Value> = fresh.iter().filter_map(|f| f.get("pc_id").filter(|v| py_truthy(Some(v))).cloned()).collect();
        let wid = src.get("world_id")?.as_i64()?;
        let mut st = c.db.prepare("SELECT * FROM player_characters WHERE world_id = ?1 ORDER BY name").ok()?;
        let pcs: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        for pc in &pcs {
            if !have.iter().any(|h| Some(h) == pc.get("id")) {
                fresh.push(pc_combatant(c, pc)?);
            }
        }
    }
    let mut body = Map::new();
    body.insert("combatants".into(), Value::Array(fresh));
    let state = cs_normalize(&body).ok()?;
    let mode = match serde_json::from_str::<Value>(src.get("turn_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")) {
        Ok(Value::Object(t)) => match t.get("mode").and_then(|m| m.as_str()) { Some(m @ ("initiative" | "fluid")) => m.to_string(), _ => String::new() },
        Ok(_) | Err(_) => String::new(),
    };
    let party = if template { Sql::Null } else { src.get("party_id").and_then(|v| v.as_i64()).map_or(Sql::Null, Sql::Integer) };
    orm::insert(&c.db, "combat_sessions", &[("world_id", Sql::Integer(src.get("world_id")?.as_i64()?)), ("name", s(&take_chars(name, 256))),
        ("ruleset", s(src.get("ruleset").and_then(|v| v.as_str()).unwrap_or(""))), ("is_template", Sql::Integer(template as i64)),
        ("combatants_json", s(&nd_render::py_dumps(&state["combatants"]))), ("party_id", party),
        ("turn_json", s(&nd_render::py_dumps(&json!({"active_id": null, "acted": [], "picker": null, "mode": mode}))))])
}

fn combat_duplicate(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let cs = combat_scoped(c, ids[0])?;
    let name = format!("{} (again)", cs.get("name").and_then(|v| v.as_str())?);
    let id = combat_copy(c, &cs, &name, false, false)?;
    redirect(&format!("/combat/{id}"))
}

fn combat_save_template(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let f = c.form()?;
    let cs = combat_scoped(c, ids[0])?;
    let name = nd_render::text::py_split_join(f.get("name").unwrap_or(""));
    let name = if name.is_empty() { cs.get("name").and_then(|v| v.as_str())?.to_string() } else { name };
    combat_copy(c, &cs, &name, true, true)?;
    redirect("/combat")
}

fn combat_run_template(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let tpl = combat_scoped(c, ids[0])?;
    if !py_truthy(tpl.get("is_template")) {
        return None;
    }
    let id = combat_copy_with(c, &tpl, tpl.get("name").and_then(|v| v.as_str())?, false, false, true)?;
    redirect(&format!("/combat/{id}"))
}

/// `_as_int(v, default)`: int(v), else the default (text int() reads its own way: Python's)
fn as_int(v: Option<&Value>, default: i64) -> Option<i64> {
    Some(match v {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => default,
        Some(Value::Bool(b)) => *b as i64,
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => i,
            None => {
                let f = n.as_f64()?;
                if !f.is_finite() || f.abs() > 1e15 {
                    return None;
                }
                f.trunc() as i64
            }
        },
        Some(Value::String(t)) => match py_int_text(t) {
            Ok(i) => i,
            Err(PyErr::Value) => default,
            Err(_) => return None,
        },
    })
}

fn combat_sync_characters(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let cs = combat_scoped(c, ids[0])?;
    let wid = cs.get("world_id")?.as_i64()?;
    let combatants: Vec<Value> = match serde_json::from_str::<Value>(cs.get("combatants_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_object()).collect(),
        Ok(_) => vec![],
        Err(_) => return None,
    };
    let (mut synced, mut skipped) = (vec![], vec![]);
    for cb in &combatants {
        let name = cb.get("name").cloned().unwrap_or(Value::Null);
        if cb.get("source").and_then(|v| v.as_str()) != Some("pc") || !py_truthy(cb.get("pc_id")) {
            skipped.push(name);
            continue;
        }
        let pid = cb.get("pc_id")?.as_i64()?; // a pc_id Python would compare as something else: Python's
        let Some(pc) = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", pid).filter(|p| p.get("world_id").and_then(|v| v.as_i64()) == Some(wid)) else {
            skipped.push(name);
            continue;
        };
        let (mhp, mshock, mpp, mmp, native) = pc_maxima(&c.db, &pc)?;
        if !native {
            return None; // the custom system's tracks: Python's
        }
        let mut set: Vec<(&str, Sql)> = vec![];
        let max_hp = if mhp != 0 { mhp } else { as_int(cb.get("max_hp"), 0)? };
        set.push(("current_hp", Sql::Integer(as_int(cb.get("hp"), col_int(&pc, "current_hp"))?.min(max_hp).max(0))));
        let res = cb.get("res").and_then(|r| r.as_object()).cloned().unwrap_or_default();
        for (key, top, col) in [("pp", mpp, "pp_current"), ("mp", mmp, "mp_current")] {
            if let Some(r) = res.get(key).and_then(|r| r.as_object()) {
                let v = if top != 0 { as_int(r.get("cur"), col_int(&pc, col))?.min(top).max(0) } else { as_int(r.get("cur"), 0)?.max(0) };
                set.push((col, Sql::Integer(v)));
            }
        }
        set.push(("shock_current", Sql::Integer(as_int(cb.get("shock"), col_int(&pc, "shock_current"))?.min(mshock).max(0))));
        let conds = clean_conditions(cb.get("conditions").unwrap_or(&Value::Null))?;
        set.push(("conditions_json", s(&nd_render::py_dumps(&json!(conds)))));
        orm::update(&c.db, "player_characters", pid, &set)?;
        synced.push(pc.get("name").cloned().unwrap_or(Value::Null));
    }
    orm::touch(&c.db, wid)?;
    json_response(&json!({"synced": synced, "skipped": skipped}))
}

// ── the second screen (app/routers/display.py): the stage of each world lives in shared_state under stage:<world> ──

fn stage_blank() -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("seq".into(), json!(0));
    m.insert("current".into(), Value::Null);
    m.insert("recent".into(), json!([]));
    m
}

/// `shared_state.update_json("stage:<w>", fn)` inside the request's transaction; the new state
fn stage_update(c: &Ctx, wid: i64, f: impl FnOnce(&mut Map<String, Value>) -> Option<()>) -> Option<Map<String, Value>> {
    let key = format!("stage:{wid}");
    let row: Option<(Option<String>, i64)> = c.db.query_row("SELECT value, n FROM shared_state WHERE key = ?1", [&key], |r| Ok((r.get(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0)))).ok();
    let mut st = match row.as_ref().and_then(|(v, _)| v.as_deref()).filter(|v| !v.is_empty()) {
        Some(v) => match serde_json::from_str::<Value>(v).ok()? {
            Value::Object(o) => o,
            _ => return None,
        },
        None => stage_blank(),
    };
    f(&mut st)?;
    let blob = nd_render::py_dumps(&Value::Object(st.clone()));
    match row {
        None => c.db.execute("INSERT OR IGNORE INTO shared_state(key, value, n) VALUES (?1, ?2, 1)", rusqlite::params![key, blob]).ok()?,
        Some(_) => c.db.execute("UPDATE shared_state SET value = ?2, n = n + 1 WHERE key = ?1", rusqlite::params![key, blob]).ok()?,
    };
    orm::touch(&c.db, wid)?;
    Some(st)
}

/// `safe_image_url`: a site-local /path or an http(s) address that can only load as a picture, else ""
fn safe_image_url(v: Option<&Value>) -> String {
    let Some(Value::String(u)) = v else { return String::new() };
    let u = strip(u);
    let clean = |t: &str| !t.chars().any(|ch| crate::markup::py_space(ch) || ch < ' ' || ch == '\\');
    let local = u.starts_with('/') && !u.starts_with("//") && clean(&u);
    let lower = u.to_ascii_lowercase();
    let abs = ["http://", "https://"].iter().find(|p| lower.starts_with(*p)).is_some_and(|p| u.len() > p.len() && clean(&u[p.len()..]));
    if local || abs { u } else { String::new() }
}

/// `_text(value, cap)`
fn stage_text(v: Option<&Value>, cap: usize) -> String {
    match v {
        Some(Value::String(t)) => take_chars(&strip(t), cap),
        _ => String::new(),
    }
}

/// `_build_item`; None for a text card (rendered with markdown2: Python's) and every refusal
fn stage_item(c: &Ctx, wid: i64, b: &Map<String, Value>) -> Option<Value> {
    let plain_id = |k: &str| match b.get(k) {
        Some(Value::Number(n)) => n.as_i64(),
        _ => None, // a bool is an int to isinstance(): Python's
    };
    match b.get("kind").and_then(|k| k.as_str())? {
        "combat" => {
            let id = plain_id("combat_id")?;
            let cs = row(&c.db, "SELECT * FROM combat_sessions WHERE id = ?1", id)?;
            if cs.get("world_id")?.as_i64() != Some(wid) || py_truthy(cs.get("is_template")) {
                return None;
            }
            Some(json!({"kind": "map", "url": format!("/display/combat/{id}"), "title": stage_text(cs.get("name"), 200), "source": {"type": "combat", "id": id}}))
        }
        "map" => {
            let slug = b.get("slug")?.as_str()?;
            let sc = c.db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
            if sc.get("world_id")?.as_i64() != Some(wid) || (py_truthy(sc.get("is_html")) && py_truthy(sc.get("html_file"))) {
                return None;
            }
            let sl = sc.get("slug")?.as_str()?;
            Some(json!({"kind": "map", "url": format!("/display/map/{sl}"), "title": stage_text(sc.get("name"), 200), "source": {"type": "map", "slug": sl}}))
        }
        "entity" => {
            let id = plain_id("entity_id")?;
            let e = row(&c.db, "SELECT * FROM entities WHERE id = ?1", id)?;
            if e.get("world_id")?.as_i64() != Some(wid) {
                return None;
            }
            let image = safe_image_url(Some(&json!(e.get("image_url").and_then(|v| v.as_str()).unwrap_or(""))));
            if b.get("mode").and_then(|m| m.as_str()) == Some("text") || image.is_empty() {
                return None; // the text card: Python's
            }
            Some(json!({"kind": "image", "url": image, "title": stage_text(e.get("name"), 200),
                        "caption": stage_text(Some(&json!(e.get("summary").and_then(|v| v.as_str()).unwrap_or(""))), 600), "source": {"type": "entity", "id": id}}))
        }
        "image" => {
            let url = safe_image_url(b.get("url"));
            if url.is_empty() {
                return None;
            }
            Some(json!({"kind": "image", "url": url, "title": stage_text(b.get("title"), 200), "caption": stage_text(b.get("caption"), 600), "source": {"type": "image"}}))
        }
        _ => None,
    }
}

fn display_show(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let wid = c.world_id()?;
    let b = c.json()?;
    let item = stage_item(c, wid, &b)?;
    let st = stage_update(c, wid, |st| {
        let seq = st.get("seq")?.as_i64()? + 1;
        st.insert("seq".into(), json!(seq));
        let mut it = item.as_object()?.clone();
        it.insert("id".into(), json!(seq));
        let ts = orm::now_stamp();
        it.insert("ts".into(), json!(format!("{}Z", ts.get(..19)?.replacen(' ', "T", 1))));
        let basis = it.get("url").or_else(|| it.get("html")).and_then(|v| v.as_str()).unwrap_or("").to_string();
        use sha1::Digest;
        let digest = sha1::Sha1::digest(basis.as_bytes());
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        let key = format!("{}:{}", it.get("kind").and_then(|k| k.as_str()).unwrap_or("None"), &hex[..16]);
        it.insert("key".into(), json!(key));
        let recent: Vec<Value> = st.get("recent")?.as_array()?.iter().filter(|r| r.get("key").and_then(|k| k.as_str()) != Some(key.as_str())).take(11).cloned().collect();
        let mut all = vec![Value::Object(it.clone())];
        all.extend(recent);
        st.insert("current".into(), Value::Object(it));
        st.insert("recent".into(), Value::Array(all));
        Some(())
    })?;
    json_response(&Value::Object(st))
}

fn display_clear(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let wid = c.world_id()?;
    let st = stage_update(c, wid, |st| {
        let seq = st.get("seq")?.as_i64()? + 1;
        st.insert("seq".into(), json!(seq));
        st.insert("current".into(), Value::Null);
        Some(())
    })?;
    json_response(&Value::Object(st))
}

// ── schematic maps, the players' moves (app/main.py schematic_move_own_token / _pickup_item / _buy_item) ──
// Python runs each in BEGIN IMMEDIATE so two players racing for one item cannot both get it; every gateway write runs in BEGIN IMMEDIATE
// too, so the same SQLite lock keeps a request on either server from reading the map while the other writes it.

/// The map by slug and its elements (stored as Python wrote them, every element an object), when the viewer may play on it
/// (`_require_schematic_world_access`: a GM, or a member of the map's world)
fn sc_for_player(c: &Ctx, slug: &str) -> Option<(Map<String, Value>, Vec<Value>)> {
    let sc = c.db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    let wid = sc.get("world_id")?.as_i64()?;
    if !c.p.is_gm {
        let member: Option<i64> = c.db.query_row("SELECT id FROM world_memberships WHERE world_id = ?1 AND user_id = ?2 LIMIT 1", [wid, c.p.user_id], |r| r.get(0)).ok();
        member?;
    }
    let raw = sc.get("elements_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]").to_string();
    let Value::Array(elements) = canonical_json(&raw)? else { return None };
    if !elements.iter().all(|e| e.is_object()) {
        return None;
    }
    Some((sc, elements))
}

/// `map_walls._map_for_edit`: the active world's schematic (not an uploaded HTML map), when the viewer may edit Maps
fn sc_for_edit(c: &Ctx, slug: &str) -> Option<Map<String, Value>> {
    let sc = c.db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    if sc.get("world_id")?.as_i64() != c.world_id() || (py_truthy(sc.get("is_html")) && py_truthy(sc.get("html_file"))) {
        return None;
    }
    c.can_edit_section("maps")?.then_some(sc)
}

pub fn py_round_pub(x: f64, nd: i32) -> Option<f64> {
    py_round(x, nd)
}

/// `round(x, nd)` where Python's correctly rounded result is certain (a value near a half: Python's)
fn py_round(x: f64, nd: i32) -> Option<f64> {
    let k = 10f64.powi(nd);
    let scaled = x * k;
    ((scaled.fract().abs() - 0.5).abs() > 1e-7).then(|| scaled.round() / k)
}

/// `map_walls._num`: a finite JSON number
fn wall_num(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}

/// `map_walls.clean_fog`; None when Python's rounding is not certain
fn clean_fog(d: &Map<String, Value>) -> Option<Map<String, Value>> {
    let rng = wall_num(d.get("range"));
    let dark = wall_num(d.get("darkness"));
    let pers = wall_num(d.get("personal"));
    let epoch = match d.get("epoch") {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64().filter(|e| (0..1_000_000_000).contains(e)).unwrap_or(0),
        Some(Value::Number(n)) if n.is_u64() => 0,
        _ => 0,
    };
    let mut out = Map::new();
    out.insert("enabled".into(), json!(d.get("enabled") == Some(&Value::Bool(true))));
    out.insert("range".into(), json!(rng.map_or(0, |r| r.max(0.0).min(100.0).trunc() as i64)));
    out.insert("epoch".into(), json!(epoch));
    out.insert("darkness".into(), json!(match dark { None => 0.0, Some(x) => py_round(x.max(0.0).min(0.95), 2)? }));
    out.insert("personal".into(), json!(if !d.contains_key("personal") { 2.0 } else { match pers { None => 0.0, Some(x) => py_round(x.max(0.0).min(20.0), 1)? } }));
    out.insert("strict".into(), json!(d.get("strict") == Some(&Value::Bool(true))));
    Some(out)
}

/// `_load(raw, default)`: the stored JSON when it has the default's type, else the default; None for text Python reads its own way
fn sc_load(sc: &Map<String, Value>, col: &str, want_list: bool) -> Option<Value> {
    let raw = sc.get(col).and_then(|v| v.as_str()).unwrap_or("");
    let v = if raw.is_empty() { None } else { canonical_json(raw) };
    if v.is_none() && !raw.is_empty() && serde_json::from_str::<Value>(raw).is_ok() {
        return None;
    }
    Some(match v {
        Some(Value::Array(a)) if want_list => Value::Array(a),
        Some(Value::Object(o)) if !want_list => Value::Object(o),
        _ if want_list => json!([]),
        _ => json!({}),
    })
}

/// `map_walls.clean_lights` -> (lights, warnings); None for what Python spells or rounds its own way
fn clean_lights(data: &[Value], sc: &Map<String, Value>) -> Option<(Vec<Value>, Vec<String>)> {
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64).max(1.0);
    let (cw, ch) = (dim("canvas_width", 2000.0), dim("canvas_height", 1500.0));
    let (mut out, mut warnings, mut seen, mut dropped) = (vec![], vec![], Vec::<String>::new(), 0);
    for (i, l) in data.iter().enumerate() {
        if out.len() >= 60 {
            warnings.push("Only the first 60 lights were kept.".to_string());
            break;
        }
        let Some(l) = l.as_object() else {
            dropped += 1;
            continue;
        };
        let (x, y, r) = (wall_num(l.get("x")), wall_num(l.get("y")), wall_num(l.get("range")));
        let (Some(x), Some(y), Some(r)) = (x, y, r) else {
            dropped += 1;
            continue;
        };
        if r <= 0.0 {
            dropped += 1;
            continue;
        }
        let raw_id = match l.get("id") {
            Some(Value::String(t)) => t.clone(),
            Some(Value::Number(n)) if n.is_i64() => n.to_string(),
            v if !py_truthy(v) => String::new(),
            _ => return None,
        };
        let mut lid: String = raw_id.chars().filter(|ch| ch.is_ascii_alphanumeric() || "_.:-".contains(*ch)).take(40).collect();
        if lid.is_empty() {
            lid = format!("l{}", i + 1);
        }
        while seen.contains(&lid) {
            lid.push('x');
        }
        seen.push(lid.clone());
        let color = match l.get("color") {
            Some(Value::String(t)) if t.len() == 7 && t.starts_with('#') && t[1..].bytes().all(|b| b.is_ascii_hexdigit()) => t.clone(),
            Some(Value::String(t)) if t.len() == 8 && t.ends_with('\n') => return None, // `$` before a final newline: Python's
            _ => "#ffd9a0".to_string(),
        };
        let k = wall_num(l.get("intensity"));
        let mut m = Map::new();
        m.insert("id".into(), json!(lid));
        m.insert("x".into(), json!(py_round(x.max(0.0).min(cw), 2)?));
        m.insert("y".into(), json!(py_round(y.max(0.0).min(ch), 2)?));
        m.insert("range".into(), json!(py_round(r.max(0.5).min(60.0), 2)?));
        m.insert("color".into(), json!(color));
        m.insert("intensity".into(), json!(match k { None => 1.0, Some(k) => py_round(k.max(0.1).min(1.0), 2)? }));
        m.insert("on".into(), json!(l.get("on") != Some(&Value::Bool(false))));
        m.insert("label".into(), json!(keepsake_text(l.get("label"), 60)?));
        out.push(Value::Object(m));
    }
    if dropped > 0 {
        warnings.push(format!("{dropped} light(s) were skipped because they were not usable."));
    }
    Some((out, warnings))
}

/// `map_walls.clean_walls` -> (walls, warnings); None for what Python spells or rounds its own way
fn clean_walls(data: &[Value], sc: &Map<String, Value>) -> Option<(Vec<Value>, Vec<String>)> {
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64).max(1.0);
    let (cw, ch) = (dim("canvas_width", 2000.0), dim("canvas_height", 1500.0));
    let (mut out, mut warnings, mut total, mut seen, mut dropped) = (vec![], vec![], 0usize, Vec::<String>::new(), 0);
    for (i, w) in data.iter().enumerate() {
        if out.len() >= 5000 {
            warnings.push("Only the first 5000 walls were kept.".to_string());
            break;
        }
        let Some((w, raw_pts)) = w.as_object().and_then(|o| Some((o, o.get("pts")?.as_array()?))) else {
            dropped += 1;
            continue;
        };
        let mut pts: Vec<[f64; 2]> = vec![];
        for p in raw_pts.iter().take(200) {
            let Some(p) = p.as_array().filter(|p| p.len() >= 2) else { continue };
            let (Some(x), Some(y)) = (wall_num(p.first()), wall_num(p.get(1))) else { continue };
            let q = [py_round(x.max(0.0).min(cw), 2)?, py_round(y.max(0.0).min(ch), 2)?];
            if pts.last() != Some(&q) {
                pts.push(q);
            }
        }
        if pts.len() < 2 || total + pts.len() > 20000 {
            dropped += 1;
            continue;
        }
        total += pts.len();
        let kind = match w.get("kind") {
            Some(Value::String(k)) if ["wall", "door", "window", "secret"].contains(&k.as_str()) => k.clone(),
            None | Some(Value::Null) | Some(Value::String(_)) | Some(Value::Number(_)) | Some(Value::Bool(_)) => "wall".to_string(),
            _ => return None,
        };
        let raw_id = match w.get("id") {
            Some(Value::String(t)) => t.clone(),
            Some(Value::Number(n)) if n.is_i64() => n.to_string(),
            v if !py_truthy(v) => String::new(),
            _ => return None,
        };
        let mut wid: String = raw_id.chars().filter(|ch| ch.is_ascii_alphanumeric() || "_.:-".contains(*ch)).take(40).collect();
        if wid.is_empty() {
            wid = format!("w{}", i + 1);
        }
        while seen.contains(&wid) {
            wid.push('x');
        }
        seen.push(wid.clone());
        let mut item = Map::new();
        item.insert("id".into(), json!(wid));
        item.insert("pts".into(), json!(pts));
        item.insert("kind".into(), json!(kind));
        if kind == "door" || kind == "secret" {
            item.insert("state".into(), json!(if w.get("state").and_then(|v| v.as_str()) == Some("open") { "open" } else { "closed" }));
        }
        out.push(Value::Object(item));
    }
    if dropped > 0 {
        warnings.push(format!("{dropped} wall(s) were skipped because they were not usable."));
    }
    Some((out, warnings))
}

fn sc_walls(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let (walls, warnings) = clean_walls(body.get("walls")?.as_array()?, &sc)?;
    let v = Value::Array(walls);
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("walls_json", s(&nd_render::py_dumps(&v)))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"walls": v, "warnings": warnings}))
}

/// The editor's own routes check nothing past the GM / Assistant allowlist: the gateway answers only a GM or an Assistant, for a
/// schematic in the active world (anything wider stays Python's)
fn sc_for_manager(c: &Ctx, slug: &str) -> Option<Map<String, Value>> {
    if !(c.p.is_gm || c.is_assistant()) {
        return None;
    }
    let sc = c.db.query_row("SELECT * FROM schematics WHERE slug = ?1 LIMIT 1", [slug], |r| crate::layout::row_map(r)).ok()?;
    (sc.get("world_id")?.as_i64() == c.world_id()).then_some(sc)
}

fn sc_elements(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_manager(c, slug)?;
    let body = c.json()?;
    let elements = match body.get("elements") {
        None => vec![],
        Some(Value::Array(a)) if a.iter().all(|e| e.is_object()) => a.clone(),
        _ => return None,
    };
    let text = nd_render::py_dumps(&Value::Array(elements));
    if text.contains("NaN") || text.contains("Infinity") {
        return None;
    }
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("elements_json", s(&text))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"ok": true}))
}

fn sc_grid(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_manager(c, slug)?;
    let body = c.json()?;
    let grid = body.get("grid_type").and_then(|v| v.as_str()).filter(|g| ["none", "hex", "square"].contains(g)).unwrap_or("none");
    let config = match body.get("config") {
        Some(Value::Object(o)) => Value::Object(o.clone()),
        _ => json!({}),
    };
    let text = nd_render::py_dumps(&config);
    if text.contains("NaN") || text.contains("Infinity") {
        return None;
    }
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("grid_type", s(grid)), ("grid_config_json", s(&text))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"ok": true}))
}

fn sc_link_combat(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let f = c.form()?;
    let raw = f.get("combat_session_id").unwrap_or("");
    let sc = sc_for_manager(c, slug)?;
    let t = raw.trim_matches(|ch: char| ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r');
    if !t.is_empty() {
        if t.len() > 15 || !t.bytes().all(|b| b.is_ascii_digit()) || t != strip(raw) {
            return None; // int()'s other spellings: Python's
        }
        let cs_id: i64 = t.parse().ok()?;
        let found: i64 = c.db.query_row("SELECT id FROM combat_sessions WHERE id = ?1 AND world_id = ?2", [cs_id, sc.get("world_id")?.as_i64()?], |r| r.get(0)).ok()?;
        orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("combat_session_id", Sql::Integer(found))])?;
    }
    redirect(&format!("/maps/schematic/{slug}"))
}

fn sc_unlink_combat(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_manager(c, slug)?;
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("combat_session_id", Sql::Null)])?;
    redirect(&format!("/maps/schematic/{slug}"))
}

/// A stored JSON list of objects exactly as Python reads it (anything else: Python's)
fn obj_list(raw: Option<&Value>) -> Option<Vec<Map<String, Value>>> {
    let raw = raw.and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    let Value::Array(a) = canonical_json(raw)? else { return None };
    a.into_iter().map(|v| match v { Value::Object(o) => Some(o), _ => None }).collect()
}

/// `list(x.get("conditions", []))` for a list (a string's characters, None's error: Python's)
fn cond_list(m: &Map<String, Value>) -> Option<Value> {
    match m.get("conditions") {
        None => Some(json!([])),
        Some(Value::Array(a)) => Some(Value::Array(a.clone())),
        _ => None,
    }
}

/// The linked fight of a schematic the viewer manages
fn sc_linked(c: &Ctx, slug: &str) -> Option<(Map<String, Value>, Map<String, Value>)> {
    let sc = sc_for_manager(c, slug)?;
    let cs_id = sc.get("combat_session_id").and_then(|v| v.as_i64()).filter(|i| *i != 0)?;
    let cs = row(&c.db, "SELECT * FROM combat_sessions WHERE id = ?1", cs_id)?;
    Some((sc, cs))
}

/// `schematic_pull_combat`: a token for every combatant (matched by its combatant_id), refreshed or added
fn sc_pull_combat(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let (sc, cs) = sc_linked(c, slug)?;
    let combatants = obj_list(cs.get("combatants_json"))?;
    let mut elements = obj_list(sc.get("elements_json"))?;
    // by_combatant_id: the LAST token with that id wins, as a dict built in order keeps it
    let mut by_cid: Vec<(String, usize)> = vec![];
    for (i, e) in elements.iter().enumerate() {
        if e.get("type").and_then(|v| v.as_str()) != Some("token") || !py_truthy(e.get("combatant_id")) {
            continue;
        }
        let cid = e.get("combatant_id")?.as_str()?.to_string();
        by_cid.retain(|(k, _)| *k != cid);
        by_cid.push((cid, i));
    }
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64);
    let (cx, cy) = (dim("canvas_width", 2000.0) / 2.0, dim("canvas_height", 1500.0) / 2.0);
    let mut new_count = 0i64;
    for cb in &combatants {
        let id = cb.get("id")?.as_str()?.to_string();
        let conditions = cond_list(cb)?;
        if let Some(&(_, i)) = by_cid.iter().find(|(k, _)| *k == id) {
            let t = &mut elements[i];
            for key in ["name", "hp", "max_hp"] {
                if let Some(v) = cb.get(key) {
                    t.insert(key.into(), v.clone());
                } else {
                    let keep = t.get(key).cloned().unwrap_or(Value::Null);
                    t.insert(key.into(), keep);
                }
            }
            t.insert("conditions".into(), conditions);
        } else {
            let source = cb.get("source").cloned().unwrap_or(json!("manual"));
            let src = source.as_str();
            let mut m = Map::new();
            m.insert("id".into(), json!(format!("{:08x}", rand::random::<u32>())));
            m.insert("type".into(), json!("token"));
            m.insert("x".into(), json!(cx + ((new_count % 5) * 45 - 90) as f64));
            m.insert("y".into(), json!(cy + ((new_count / 5) * 45 - 45) as f64));
            m.insert("r".into(), json!(20));
            m.insert("layer".into(), json!("Tracks"));
            m.insert("combatant_id".into(), json!(id));
            m.insert("source".into(), source.clone());
            m.insert("pc_id".into(), cb.get("pc_id").cloned().unwrap_or(Value::Null));
            m.insert("entity_id".into(), cb.get("entity_id").cloned().unwrap_or(Value::Null));
            m.insert("name".into(), cb.get("name").cloned().unwrap_or(json!("Combatant")));
            m.insert("color".into(), json!(match src { Some("pc") => "#4488ff", Some("entity") => "#e63946", _ => "#888888" }));
            m.insert("hp".into(), cb.get("hp").cloned().unwrap_or(json!(0)));
            m.insert("max_hp".into(), cb.get("max_hp").cloned().unwrap_or(json!(0)));
            m.insert("conditions".into(), conditions);
            m.insert("visible_to_players".into(), json!(matches!(src, Some("pc" | "entity"))));
            elements.push(m);
            new_count += 1;
        }
    }
    let list = Value::Array(elements.into_iter().map(Value::Object).collect());
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("elements_json", s(&nd_render::py_dumps(&list)))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"elements": list}))
}

/// `schematic_push_combat`: each token's hp / max_hp / conditions back onto its combatant
fn sc_push_combat(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let (sc, cs) = sc_linked(c, slug)?;
    let elements = obj_list(sc.get("elements_json"))?;
    let mut combatants = obj_list(cs.get("combatants_json"))?;
    // by_id: every combatant needs a (string) id; the last of equal ids wins
    let mut ids = vec![];
    for cb in &combatants {
        ids.push(cb.get("id")?.as_str()?.to_string());
    }
    let mut synced = vec![];
    for el in &elements {
        if el.get("type").and_then(|v| v.as_str()) != Some("token") || !py_truthy(el.get("combatant_id")) {
            continue;
        }
        let cid = el.get("combatant_id")?.as_str()?;
        let Some(i) = ids.iter().rposition(|k| k == cid) else { continue };
        let conditions = cond_list(el)?;
        let cb = &mut combatants[i];
        for key in ["hp", "max_hp"] {
            let v = el.get(key).cloned().unwrap_or_else(|| cb.get(key).cloned().unwrap_or(Value::Null));
            cb.insert(key.into(), v);
        }
        cb.insert("conditions".into(), conditions);
        synced.push(cb.get("name").cloned().unwrap_or(Value::Null));
    }
    let list = Value::Array(combatants.into_iter().map(Value::Object).collect());
    orm::update(&c.db, "combat_sessions", cs.get("id")?.as_i64()?, &[("combatants_json", s(&nd_render::py_dumps(&list)))])?;
    let cs = row(&c.db, "SELECT * FROM combat_sessions WHERE id = ?1", cs.get("id")?.as_i64()?)?;
    combat_touch(c, &cs)?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"synced": synced}))
}

fn sc_rename(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let f = c.form()?;
    let name = strip(f.get("name")?);
    let sc = sc_for_manager(c, slug)?;
    if name.is_empty() {
        return None;
    }
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("name", s(&name))])?;
    redirect(&format!("/maps/schematic/{slug}"))
}

/// `schematic_delete` of a drawn schematic (an uploaded HTML one's file: Python's): the row, its background picture and its embedded images
fn sc_delete(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_manager(c, slug)?;
    if py_truthy(sc.get("is_html")) && py_truthy(sc.get("html_file")) {
        return None;
    }
    let raw = sc.get("elements_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    let Ok(Value::Array(elements)) = serde_json::from_str::<Value>(raw) else { return None };
    let dir = c.cfg.db_path.parent()?.join("uploads").join("schematics");
    if py_truthy(sc.get("image_url")) {
        for ext in [".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"] {
            let img = dir.join(format!("{slug}{ext}"));
            if img.exists() {
                c.removals.push(img);
            }
        }
    }
    for el in &elements {
        if let Some(href) = el.get("href").and_then(|v| v.as_str()).filter(|h| h.starts_with("/uploads/schematics/embeds/")) {
            let name = href.rsplit('/').next()?;
            if name.is_empty() || name == ".." || name == "." {
                return None;
            }
            let f = dir.join("embeds").join(name);
            if f.exists() {
                c.removals.push(f);
            }
        }
    }
    c.db.execute("DELETE FROM schematics WHERE id = ?1", [sc.get("id")?.as_i64()?]).ok()?;
    redirect("/maps")
}

/// `map_rooms._int`: a JSON whole number (a bool is not one); Err for a number past i64 (Python's)
fn room_int(v: Option<&Value>) -> Result<Option<i64>, ()> {
    match v {
        Some(Value::Number(n)) if n.is_i64() => Ok(n.as_i64()),
        Some(Value::Number(n)) if n.is_u64() => Err(()),
        _ => Ok(None),
    }
}

/// `map_rooms.clean_rooms` for a drawing (the refusals, and anything Python spells or rounds its own way: None)
fn clean_rooms(d: &Map<String, Value>) -> Option<Value> {
    if d.is_empty() {
        return Some(json!({}));
    }
    let cell = wall_num(d.get("cell")).filter(|c| (8.0..=400.0).contains(c))?;
    let ox = wall_num(d.get("ox")).unwrap_or(0.0);
    let oy = wall_num(d.get("oy")).unwrap_or(0.0);
    let (cols, rows) = (room_int(d.get("cols")).ok()??, room_int(d.get("rows")).ok()??);
    if !((1..=400).contains(&cols) && (1..=400).contains(&rows)) || cols * rows > 40000 {
        return None;
    }
    let (ox, oy) = (ox.max(0.0).min(cell), oy.max(0.0).min(cell));
    let mut spaces: Vec<Value> = vec![];
    let mut ids: Vec<i64> = vec![];
    if let Some(Value::Array(list)) = d.get("spaces") {
        for sp in list.iter().take(80) {
            let Some(sp) = sp.as_object() else { continue };
            let Some(sid) = room_int(sp.get("id")).ok()? else { continue };
            if !(1..=9999).contains(&sid) || ids.contains(&sid) {
                continue;
            }
            let name = keepsake_text(sp.get("name"), 60)?;
            let name = if name.is_empty() { format!("Room {sid}") } else { name };
            let kind = match sp.get("kind").and_then(|v| v.as_str()) {
                Some(k) if ["hall", "tavern", "bedroom", "kitchen", "storage", "corridor", "cell", "shrine", "library", "armory", "cave", "outdoor", "other"].contains(&k) => k,
                _ => "other",
            };
            ids.push(sid);
            spaces.push(json!({"id": sid, "name": name, "kind": kind}));
        }
    }
    let raw = d.get("cells")?.as_array()?;
    if raw.len() % 2 != 0 {
        return None;
    }
    let (mut runs, mut total): (Vec<i64>, i64) = (vec![], 0);
    for pair in raw.chunks(2) {
        let (sid, n) = (room_int(pair.first()).ok()??, room_int(pair.get(1)).ok()??);
        if n < 1 || sid < 0 {
            return None;
        }
        total = total.checked_add(n)?;
        if total > cols * rows {
            return None;
        }
        let sid = if sid != 0 && !ids.contains(&sid) { 0 } else { sid };
        if runs.len() >= 2 && runs[runs.len() - 2] == sid {
            *runs.last_mut()? += n;
        } else {
            runs.push(sid);
            runs.push(n);
        }
    }
    if total != cols * rows {
        return None;
    }
    let mut marks: Vec<Value> = vec![];
    let mut seen: Vec<(i64, i64, String)> = vec![];
    if let Some(Value::Array(list)) = d.get("marks") {
        for m in list.iter().take(5000) {
            let Some(m) = m.as_array().filter(|m| m.len() == 4) else { continue };
            let (Some(x), Some(y)) = (room_int(m.first()).ok()?, room_int(m.get(1)).ok()?) else { continue };
            let (Some(axis), Some(kind)) = (m[2].as_str(), m[3].as_str()) else { continue };
            if !(axis == "h" || axis == "v") || !["door", "window", "secret", "open"].contains(&kind) {
                continue;
            }
            if !((0..=cols).contains(&x) && (0..=rows).contains(&y)) || (axis == "h" && x >= cols) || (axis == "v" && y >= rows) {
                continue;
            }
            let key = (x, y, axis.to_string());
            if seen.contains(&key) {
                continue;
            }
            seen.push(key);
            marks.push(json!([x, y, axis, kind]));
        }
    }
    let used: Vec<i64> = runs.iter().step_by(2).copied().filter(|s| *s != 0).collect();
    spaces.retain(|sp| sp["id"].as_i64().is_some_and(|i| used.contains(&i)));
    let mut out = Map::new();
    out.insert("cell".into(), json!(py_round(cell, 2)?));
    out.insert("ox".into(), json!(py_round(ox, 2)?));
    out.insert("oy".into(), json!(py_round(oy, 2)?));
    out.insert("cols".into(), json!(cols));
    out.insert("rows".into(), json!(rows));
    if d.get("clear") == Some(&Value::Bool(true)) {
        out.insert("clear".into(), json!(true));
    }
    out.insert("cells".into(), json!(runs));
    out.insert("spaces".into(), Value::Array(spaces));
    out.insert("marks".into(), Value::Array(marks));
    Some(Value::Object(out))
}

/// `rooms_detect`: the enclosed rooms of the stored walls as room-tool state (nothing stored); a map without a square grid and the
/// detector's refusals are Python's
fn sc_rooms_detect(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let cfg = sc_load(&sc, "grid_config_json", false)?;
    let grid = sc.get("grid_type").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("none");
    let cell = wall_num(cfg.get("cell_size")).filter(|c| *c > 0.0)?;
    if grid != "square" {
        return None;
    }
    let off = |k: &str| -> Option<f64> {
        match cfg.get(k) {
            v if !py_truthy(v) => Some(0.0),
            Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
            _ => None, // `%` of a string or a bool: Python's
        }
    };
    let (ox, oy) = (off("offset_x")?, off("offset_y")?);
    let Value::Array(stored) = sc_load(&sc, "walls_json", true)? else { return None };
    let (walls, _) = clean_walls(&stored, &sc)?;
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64);
    match crate::rooms::detect(&walls, dim("canvas_width", 2000.0), dim("canvas_height", 1500.0), cell, ox, oy)? {
        crate::rooms::Detected::Refused => None,
        crate::rooms::Detected::Rooms(state, info) => {
            let state = clean_rooms(state.as_object()?)?;
            json_response(&json!({"state": state, "info": info}))
        }
    }
}

fn sc_rooms(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let state = clean_rooms(&body)?;
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("rooms_json", s(&nd_render::py_dumps(&state)))])?;
    json_response(&json!({"rooms": state, "warnings": []}))
}

fn sc_lights(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let (lights, warnings) = clean_lights(body.get("lights")?.as_array()?, &sc)?;
    let v = Value::Array(lights);
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("lights_json", s(&nd_render::py_dumps(&v)))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"lights": v, "warnings": warnings}))
}

fn sc_light(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let id = body.get("id")?.as_str()?.to_string();
    let Value::Array(stored) = sc_load(&sc, "lights_json", true)? else { return None };
    let (mut lights, _) = clean_lights(&stored, &sc)?;
    let l = lights.iter_mut().find(|l| l["id"] == json!(id))?.as_object_mut()?;
    let on = match body.get("on") {
        Some(Value::Bool(b)) => *b,
        _ => !l["on"].as_bool()?,
    };
    l.insert("on".into(), json!(on));
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("lights_json", s(&nd_render::py_dumps(&Value::Array(lights))))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"id": id, "on": on}))
}

fn sc_fog(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let cur = clean_fog(sc_load(&sc, "fog_json", false)?.as_object()?)?;
    let mut new = cur.clone();
    for key in ["enabled", "strict"] {
        if let Some(v) = body.get(key) {
            new.insert(key.into(), json!(v.as_bool()?));
        }
    }
    if body.contains_key("range") {
        let mut one = Map::new();
        one.insert("range".into(), body["range"].clone());
        new.insert("range".into(), clean_fog(&one)?["range"].clone());
    }
    for key in ["darkness", "personal"] {
        if let Some(v) = body.get(key) {
            if !v.is_number() {
                return None;
            }
            new.insert(key.into(), v.clone());
        }
    }
    if body.get("reset") == Some(&Value::Bool(true)) {
        new.insert("epoch".into(), json!(cur["epoch"].as_i64()? + 1));
    }
    let new = clean_fog(&new)?;
    let v = Value::Object(new);
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("fog_json", s(&nd_render::py_dumps(&v)))])?;
    orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
    json_response(&v)
}

fn sc_door(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let sc = sc_for_edit(c, slug)?;
    let body = c.json()?;
    let wid = body.get("wall_id")?.as_str()?.to_string();
    let want = body.get("state").and_then(|v| v.as_str()).filter(|v| *v == "open" || *v == "closed").map(str::to_string);
    let Value::Array(mut walls) = sc_load(&sc, "walls_json", true)? else { return None };
    for w in walls.iter_mut() {
        let w = w.as_object_mut()?; // a non-object wall: Python's error
        if w.get("id").and_then(|v| v.as_str()) == Some(wid.as_str()) && matches!(w.get("kind").and_then(|v| v.as_str()), Some("door" | "secret")) {
            let state = want.clone().unwrap_or_else(|| if w.get("state").and_then(|v| v.as_str()) == Some("open") { "closed".into() } else { "open".into() });
            w.insert("state".into(), json!(state));
            orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("walls_json", s(&nd_render::py_dumps(&Value::Array(walls.clone()))))])?;
            orm::touch(&c.db, sc.get("world_id")?.as_i64()?)?;
            return json_response(&json!({"wall_id": wid, "state": state}));
        }
    }
    None
}

/// The viewer's characters in the map's world, oldest first
fn sc_own_pcs(c: &Ctx, wid: i64) -> Option<Vec<Map<String, Value>>> {
    let mut st = c.db.prepare("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map([wid, c.p.user_id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    Some(rows)
}

/// `_active_own_pc`: the viewer's character with a token on the map, else their first
fn sc_active_pc(c: &Ctx, wid: i64, elements: &[Value]) -> Option<Option<Map<String, Value>>> {
    let own = sc_own_pcs(c, wid)?;
    let on_map: Vec<&Value> = elements.iter().filter(|e| e.get("type").and_then(|t| t.as_str()) == Some("token") && py_truthy(e.get("pc_id"))).filter_map(|e| e.get("pc_id")).collect();
    let hit = own.iter().find(|p| on_map.iter().any(|v| v.as_i64().is_some() && v.as_i64() == p.get("id").and_then(|i| i.as_i64()))).or(own.first());
    Some(hit.cloned())
}

/// `_merge_equipment_item(pc, name, qty)`: the new equipment_json (a list Python reads its own way, or a name lower() changes beyond
/// ASCII: None)
fn sc_merge_item(pc: &Map<String, Value>, name: &str, qty: i64) -> Option<String> {
    let raw = pc.get("equipment_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]").to_string();
    let Value::Array(mut eq) = canonical_json(&raw)? else { return None };
    if !name.is_ascii() || !eq.iter().all(|it| it.is_object()) {
        return None;
    }
    let want = strip(name).to_ascii_lowercase();
    let mut hit = None;
    for (i, it) in eq.iter().enumerate() {
        let n = match it.get("name") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(t)) if t.is_ascii() => strip(t).to_ascii_lowercase(),
            Some(Value::String(t)) if t.is_empty() => String::new(),
            _ => return None,
        };
        if n == want {
            hit = Some(i);
            break;
        }
    }
    match hit {
        Some(i) => {
            let it = eq[i].as_object_mut()?;
            let have = match it.get("qty") {
                None | Some(Value::Null) | Some(Value::Bool(false)) => 0,
                Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
                _ => return None,
            };
            it.insert("qty".into(), json!(have + qty));
        }
        None => eq.push(json!({"name": name, "qty": qty, "weight": 0, "equipped": false, "notes": ""})),
    }
    Some(nd_render::py_dumps(&Value::Array(eq)))
}

fn sc_find<'a>(elements: &'a mut [Value], token_id: &str, source: Option<&str>) -> Option<&'a mut Map<String, Value>> {
    elements.iter_mut().filter_map(|e| e.as_object_mut()).find(|e| {
        e.get("id").and_then(|i| i.as_str()) == Some(token_id) && e.get("type").and_then(|t| t.as_str()) == Some("token")
            && source.is_none_or(|s| e.get("source").and_then(|v| v.as_str()) == Some(s))
    })
}

/// `not el.get("visible_to_players", True)` / `el.get("locked")`: the token is hidden from players or locked
fn sc_closed(el: &Map<String, Value>) -> bool {
    el.get("visible_to_players").is_some_and(|v| !py_truthy(Some(v))) || py_truthy(el.get("locked"))
}

fn sc_move_token(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let b = c.json()?;
    let (sc, mut elements) = sc_for_player(c, slug)?;
    let token = b.get("token_id")?.as_str()?.to_string(); // a number id compares by value in Python: Python's
    let num = |k: &str| match b.get(k) {
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
        _ => None, // text float() reads, a bool, a missing value: Python's
    };
    let (x, y) = (num("x")?, num("y")?);
    let cw = sc.get("canvas_width").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(2000) as f64;
    let ch = sc.get("canvas_height").and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(1500) as f64;
    if x == 0.0 || y == 0.0 {
        if x.is_sign_negative() || y.is_sign_negative() {
            return None; // max(-0.0, 0.0) keeps the -0.0 in Python
        }
    }
    let fx = x.max(0.0).min(cw);
    let fy = y.max(0.0).min(ch);
    let wid = sc.get("world_id")?.as_i64()?;
    let own: Vec<i64> = if c.p.is_gm { vec![] } else { sc_own_pcs(c, wid)?.iter().filter_map(|p| p.get("id").and_then(|i| i.as_i64())).collect() };
    let is_gm = c.p.is_gm;
    let el = sc_find(&mut elements, &token, None)?;
    if !is_gm {
        let mine = el.get("pc_id").and_then(|v| v.as_i64()).is_some_and(|p| own.contains(&p)) && !el.get("pc_id").is_some_and(|v| v.is_f64());
        if !mine || sc_closed(el) {
            return None;
        }
    }
    el.insert("x".into(), Value::Number(serde_json::Number::from_f64(fx)?));
    el.insert("y".into(), Value::Number(serde_json::Number::from_f64(fy)?));
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("elements_json", s(&nd_render::py_dumps(&Value::Array(elements))))])?;
    orm::touch(&c.db, wid)?;
    json_response(&json!({"ok": true}))
}

fn sc_pickup(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let b = c.json()?;
    let (sc, mut elements) = sc_for_player(c, slug)?;
    let token = b.get("token_id").filter(|v| py_truthy(Some(v)))?.as_str()?.to_string();
    let wid = sc.get("world_id")?.as_i64()?;
    let pc = sc_active_pc(c, wid, &elements)??;
    let el = sc_find(&mut elements, &token, Some("item"))?;
    if sc_closed(el) {
        return None;
    }
    let qty = match el.get("qty") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => 1,
        Some(Value::Number(n)) if n.as_i64() == Some(0) => 1,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        _ => return None,
    };
    let name = match el.get("name") {
        None | Some(Value::Null) => "Item".to_string(),
        Some(Value::String(t)) if t.is_empty() => "Item".to_string(),
        Some(Value::String(t)) => t.clone(),
        _ => return None,
    };
    let eq = sc_merge_item(&pc, &name, qty)?;
    elements.retain(|e| e.get("id").and_then(|i| i.as_str()) != Some(token.as_str()));
    orm::update(&c.db, "player_characters", pc.get("id")?.as_i64()?, &[("equipment_json", s(&eq))])?;
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("elements_json", s(&nd_render::py_dumps(&Value::Array(elements))))])?;
    orm::touch(&c.db, wid)?;
    json_response(&json!({"ok": true, "name": name, "qty": qty}))
}

/// `float(v or 0)` for a JSON number (text float() reads: Python's)
fn py_float_or0(v: Option<&Value>) -> Option<f64> {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Some(0.0),
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
        Some(Value::String(t)) if t.is_empty() => Some(0.0),
        _ => None,
    }
}

fn sc_buy(c: &mut Ctx, slug: &str) -> Option<Response<Body>> {
    let b = c.json()?;
    let (sc, mut elements) = sc_for_player(c, slug)?;
    let token = b.get("token_id").filter(|v| py_truthy(Some(v)))?.as_str()?.to_string();
    let stock_id = b.get("stock_id").filter(|v| py_truthy(Some(v)))?.as_str()?.to_string();
    let wid = sc.get("world_id")?.as_i64()?;
    let pc = sc_active_pc(c, wid, &elements)??;
    let el = sc_find(&mut elements, &token, Some("merchant"))?;
    if sc_closed(el) {
        return None;
    }
    let inv = match el.get_mut("inventory") {
        Some(Value::Array(a)) => a,
        _ => return None, // no stock (Python's 404), or one Python iterates its own way
    };
    if !inv.iter().all(|r| r.is_object()) {
        return None;
    }
    let stock = inv.iter_mut().find(|r| r.get("id").and_then(|i| i.as_str()) == Some(stock_id.as_str()))?.as_object_mut()?;
    let qty = match stock.get("qty") {
        None => -1,
        Some(Value::Number(n)) if n.is_i64() => n.as_i64()?,
        _ => return None,
    };
    if qty != -1 && qty <= 0 {
        return None;
    }
    let price = py_float_or0(stock.get("price"))?;
    let abbr_raw = match stock.get("currency_abbr") {
        None | Some(Value::Null) => "CR".to_string(),
        Some(Value::String(t)) if t.is_empty() => "CR".to_string(),
        Some(Value::String(t)) => t.clone(),
        _ => return None,
    };
    if !abbr_raw.is_ascii() {
        return None;
    }
    let abbr = strip(&abbr_raw).to_ascii_lowercase();
    let raw_cur = pc.get("currency_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]").to_string();
    let Value::Array(mut currency) = canonical_json(&raw_cur)? else { return None };
    if !currency.iter().all(|e| e.is_object()) {
        return None;
    }
    let mut idx = None;
    for (i, e) in currency.iter().enumerate() {
        let a = match e.get("abbr") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(t)) if t.is_ascii() => strip(t).to_ascii_lowercase(),
            _ => return None,
        };
        if a == abbr {
            idx = Some(i);
            break;
        }
    }
    let entry = currency.get_mut(idx?)?.as_object_mut()?;
    let have = py_float_or0(entry.get("value"))?;
    if have < price {
        return None;
    }
    let left = have - price;
    entry.insert("value".into(), Value::Number(serde_json::Number::from_f64(left)?));
    let entry_abbr = entry.get("abbr").cloned().unwrap_or(Value::Null);
    if qty != -1 {
        stock.insert("qty".into(), json!(qty - 1));
    }
    let item_name = stock.get("name").cloned().unwrap_or(Value::Null);
    let merge_name = match &item_name {
        Value::Null => "Item".to_string(),
        Value::String(t) if t.is_empty() => "Item".to_string(),
        Value::String(t) => t.clone(),
        _ => return None,
    };
    let mut pc2 = pc.clone();
    let cur_text = nd_render::py_dumps(&Value::Array(currency));
    pc2.insert("currency_json".into(), json!(cur_text));
    let eq = sc_merge_item(&pc2, &merge_name, 1)?;
    orm::update(&c.db, "player_characters", pc.get("id")?.as_i64()?, &[("currency_json", s(&cur_text)), ("equipment_json", s(&eq))])?;
    orm::update(&c.db, "schematics", sc.get("id")?.as_i64()?, &[("elements_json", s(&nd_render::py_dumps(&Value::Array(elements))))])?;
    orm::touch(&c.db, wid)?;
    json_response(&json!({"ok": true, "item": {"name": item_name, "qty": 1}, "currency": {"abbr": entry_abbr, "value": left}}))
}

// ── the character hub's turn controls (app/routers/character_hub.py, app/combat_turns.py) ──

/// `combat_turns._init` / `_tie`: int(v or 0), 0 when int() refuses; None for text int() reads its own way
fn turn_num(v: Option<&Value>) -> Option<i64> {
    Some(match v {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(b)) => *b as i64,
        Some(Value::Number(n)) => match n.as_i64() {
            Some(i) => i,
            None => {
                let f = n.as_f64()?;
                if !f.is_finite() || f.abs() > 1e15 {
                    return None;
                }
                f.trunc() as i64
            }
        },
        Some(Value::String(t)) => match py_int_text(t) {
            Ok(i) => i,
            Err(PyErr::Value) => 0,
            Err(_) => return None,
        },
        Some(_) => 0,
    })
}

/// An encounter as the turn helpers read it
struct Fight {
    cs: Map<String, Value>,
    combatants: Vec<Value>,
    rules: Value,
    turn: Map<String, Value>,
    fluid: bool,
}

impl Fight {
    fn load(cs: Map<String, Value>) -> Option<Fight> {
        let combatants: Vec<Value> = match serde_json::from_str::<Value>(cs.get("combatants_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
            Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_object()).collect(),
            Ok(_) => vec![],
            Err(_) => return None,
        };
        let raw = match serde_json::from_str::<Value>(cs.get("turn_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}")) {
            Ok(Value::Object(o)) => o,
            Ok(_) | Err(_) => Map::new(),
        };
        let s_or_null = |k: &str| raw.get(k).filter(|v| v.is_string()).cloned().unwrap_or(Value::Null);
        let acted: Vec<Value> = raw.get("acted").and_then(|a| a.as_array()).map(|a| a.iter().filter(|x| x.is_string()).cloned().collect()).unwrap_or_default();
        let mode = match raw.get("mode").and_then(|m| m.as_str()) { Some(m @ ("initiative" | "fluid")) => m, _ => "" };
        let mut turn = Map::new();
        turn.insert("active_id".into(), s_or_null("active_id"));
        turn.insert("acted".into(), Value::Array(acted));
        turn.insert("picker".into(), s_or_null("picker"));
        turn.insert("mode".into(), json!(mode));
        let all = nd_render::app::data().get("combat_rulesets")?;
        let rules = all.get(cs.get("ruleset").and_then(|v| v.as_str()).unwrap_or("")).unwrap_or(&all["generic"]).clone();
        let fluid = match mode { "fluid" => true, "initiative" => false, _ => rules["fluid"].as_bool()? };
        Some(Fight { cs, combatants, rules, turn, fluid })
    }

    fn order(&self) -> Option<Vec<usize>> {
        let mut keys = vec![];
        for (i, c) in self.combatants.iter().enumerate() {
            keys.push((-turn_num(c.get("initiative"))?, -turn_num(c.get("tiebreak"))?, i));
        }
        keys.sort();
        Some(keys.into_iter().map(|k| k.2).collect())
    }

    fn active_idx(&self) -> i64 {
        self.cs.get("active_idx").and_then(|v| v.as_i64()).unwrap_or(0)
    }

    fn round(&self) -> i64 {
        self.cs.get("round_num").and_then(|v| v.as_i64()).filter(|r| *r != 0).unwrap_or(1)
    }

    /// `current_index`
    fn current(&self) -> Option<Option<usize>> {
        if self.combatants.is_empty() {
            return Some(None);
        }
        if self.fluid {
            let aid = self.turn["active_id"].as_str();
            return Some(aid.and_then(|a| self.combatants.iter().position(|c| c.get("id").and_then(|i| i.as_str()) == Some(a))));
        }
        let n = self.combatants.len() as i64;
        Some(Some(self.order()?[self.active_idx().rem_euclid(n) as usize]))
    }

    fn find_pc(&self, pc_id: i64) -> Option<usize> {
        self.combatants.iter().position(|c| c.get("pc_id").and_then(|v| v.as_i64()) == Some(pc_id) && !c.get("pc_id").is_some_and(|v| v.is_f64()))
    }

    fn out(&self, c: &Value) -> Option<bool> {
        combat_is_out(c, &self.rules)
    }

    fn acted(&self) -> Vec<String> {
        self.turn["acted"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default()
    }

    /// `advance`: (new round, new active_idx); the turn state is updated in place
    fn advance(&mut self) -> Option<(i64, i64)> {
        let mut round = self.round();
        if self.fluid {
            let who = self.turn["active_id"].as_str().map(str::to_string);
            let mut acted = self.acted();
            if let Some(w) = &who {
                if !acted.contains(w) {
                    acted.push(w.clone());
                }
            }
            let mut all = true;
            for c in &self.combatants {
                if !self.out(c)? {
                    let id = c.get("id");
                    if !acted.iter().any(|a| id.and_then(|i| i.as_str()) == Some(a.as_str())) {
                        all = false;
                    }
                }
            }
            if all {
                acted.clear();
                round += 1;
            }
            let picker = match who { Some(w) => json!(w), None => self.turn["picker"].clone() };
            self.turn.insert("acted".into(), json!(acted));
            self.turn.insert("picker".into(), picker);
            self.turn.insert("active_id".into(), Value::Null);
            return Some((round, self.active_idx()));
        }
        if self.combatants.is_empty() {
            return Some((round + 1, self.active_idx()));
        }
        let order = self.order()?;
        let n = self.combatants.len() as i64;
        let mut nxt = self.active_idx() + 1;
        let mut found = false;
        for _ in 0..=n {
            if nxt >= n {
                nxt = 0;
                round += 1;
            }
            if !self.out(&self.combatants[order[nxt as usize]])? {
                found = true;
                break;
            }
            nxt += 1;
        }
        if !found {
            nxt = 0;
        }
        Some((round, nxt))
    }

    fn turn_json(&self) -> String {
        nd_render::py_dumps(&json!({"active_id": self.turn["active_id"], "acted": self.turn["acted"], "picker": self.turn["picker"], "mode": self.turn["mode"]}))
    }
}

/// `str(c.get("name") or "")[:60]`
fn short_name(c: &Value) -> Option<String> {
    Some(take_chars(&cs_text(c.get("name"), "").ok()?, 60))
}

/// `_combat_now(db, pc)` for the encounter `f` holding the character at `mine`
fn hub_combat_now(f: &Fight, pc_id: i64, mine: usize) -> Option<Value> {
    let me = &f.combatants[mine];
    let my_init = turn_num(me.get("initiative"))?;
    let mut out = Map::new();
    out.insert("round".into(), json!(f.round()));
    out.insert("fluid".into(), json!(f.fluid));
    out.insert("next_is_me".into(), json!(false));
    out.insert("my_initiative".into(), json!(my_init));
    out.insert("can_roll_initiative".into(), json!(!f.fluid && my_init == 0));
    out.insert("name".into(), Value::Null);
    out.insert("choose".into(), json!([]));
    let is_me = |c: &Value| c.get("pc_id").and_then(|v| v.as_i64()) == Some(pc_id);
    let is_pc = |c: &Value| c.get("source").and_then(|v| v.as_str()) == Some("pc");
    if f.fluid {
        let cur = f.current()?.map(|i| &f.combatants[i]);
        let picker = f.turn["picker"].as_str();
        if let Some(cur) = cur {
            let turn = if is_me(cur) { "me" } else if is_pc(cur) { "pc" } else { "enemy" };
            out.insert("turn".into(), json!(turn));
            out.insert("name".into(), if turn == "pc" { json!(short_name(cur)?) } else { Value::Null });
        } else if picker.is_some() && picker == me.get("id").and_then(|i| i.as_str()) {
            out.insert("turn".into(), json!("choose"));
            let acted = f.acted();
            let mut choose = vec![];
            for c in &f.combatants {
                let id = c.get("id").and_then(|i| i.as_str());
                if is_pc(c) && c.get("visible") != Some(&Value::Bool(false)) && !id.is_some_and(|i| acted.iter().any(|a| a == i))
                    && c.get("id") != me.get("id") && !f.out(c)? {
                    if c.get("visible").is_some_and(|v| !py_truthy(Some(v))) {
                        continue;
                    }
                    choose.push(json!({"id": c.get("id"), "name": short_name(c)?}));
                }
            }
            out.insert("choose".into(), json!(choose));
        } else {
            let pk = f.combatants.iter().find(|c| picker.is_some() && c.get("id").and_then(|i| i.as_str()) == picker);
            out.insert("turn".into(), json!("choosing"));
            out.insert("name".into(), match pk { Some(p) if is_pc(p) => json!(short_name(p)?), _ => Value::Null });
        }
        return Some(Value::Object(out));
    }
    let order = f.order()?;
    let n = f.combatants.len() as i64;
    let pos = f.active_idx().rem_euclid(n);
    let cur = &f.combatants[order[pos as usize]];
    let nxt = &f.combatants[order[((pos + 1) % n) as usize]];
    let (turn, name) = if is_me(cur) { ("me", Value::Null) } else if is_pc(cur) { ("pc", json!(short_name(cur)?)) } else { ("enemy", Value::Null) };
    out.insert("turn".into(), json!(turn));
    out.insert("name".into(), name);
    out.insert("next_is_me".into(), json!(is_me(nxt) && turn != "me"));
    Some(Value::Object(out))
}

/// `_owned_pc`: the caller's own character, in a world they may still use
fn hub_owned_pc(c: &Ctx, id: i64) -> Option<Map<String, Value>> {
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id)?;
    if pc.get("owner_user_id").and_then(|v| v.as_i64()) != Some(c.p.user_id) {
        return None;
    }
    let wid = pc.get("world_id")?.as_i64()?;
    if !c.p.is_gm {
        let member: Option<i64> = c.db.query_row("SELECT id FROM world_memberships WHERE world_id = ?1 AND user_id = ?2 LIMIT 1", [wid, c.p.user_id], |r| r.get(0)).ok();
        member?;
    }
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", wid)?;
    Some(pc)
}

/// `_pc_encounter`: the freshest encounter of the last 12 hours with this character in it, and its place there
fn hub_encounter(c: &Ctx, pc: &Map<String, Value>) -> Option<Option<(Fight, usize)>> {
    let since = orm::stamp_after(-12 * 3600);
    let mut st = c.db.prepare("SELECT * FROM combat_sessions WHERE world_id = ?1 AND updated_at >= ?2 ORDER BY updated_at DESC LIMIT 8").ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map(rusqlite::params![pc.get("world_id")?.as_i64()?, since], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    let pid = pc.get("id")?.as_i64()?;
    for cs in rows {
        let f = Fight::load(cs)?;
        if let Some(i) = f.find_pc(pid) {
            return Some(Some((f, i)));
        }
    }
    Some(None)
}

/// Writes the fight's turn state, round and active index, and bumps its revision (`combat_turns.touch`)
fn hub_save_fight(c: &Ctx, f: &Fight, round: i64, active_idx: i64) -> Option<()> {
    let id = f.cs.get("id")?.as_i64()?;
    let mut set: Vec<(&str, Sql)> = vec![("round_num", Sql::Integer(round)), ("active_idx", Sql::Integer(active_idx))];
    if f.fluid {
        set.insert(0, ("turn_json", s(&f.turn_json()))); // an initiative-order fight's turn state is never written
    }
    orm::update(&c.db, "combat_sessions", id, &set)?;
    combat_touch(c, &f.cs)?;
    orm::touch(&c.db, f.cs.get("world_id")?.as_i64()?)?;
    Some(())
}

fn hub_after(c: &Ctx, pc: &Map<String, Value>, cs_id: i64) -> Option<Response<Body>> {
    let pid = pc.get("id")?.as_i64()?;
    let cs = row(&c.db, "SELECT * FROM combat_sessions WHERE id = ?1", cs_id)?;
    let round = cs.get("round_num").cloned().unwrap_or(Value::Null);
    let now = match hub_encounter(c, pc)? {
        Some((f, i)) => hub_combat_now(&f, pid, i)?,
        None => Value::Null,
    };
    json_response(&json!({"ok": true, "round": round, "combat": now}))
}

/// `hub_roll_initiative`: Speed (Dexterity + Intuition) + d10 into the live encounter and the shared dice log, once
fn hub_roll_initiative(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    if !pc_maxima(&c.db, &pc)?.4 {
        return None;
    }
    let (mut f, mine) = hub_encounter(c, &pc)??;
    if f.fluid || turn_num(f.combatants[mine].get("initiative"))? != 0 {
        return None;
    }
    let name = pc.get("name").and_then(|v| v.as_str()).unwrap_or("");
    if !name.chars().all(|ch| (' '..='~').contains(&ch)) {
        return None; // _clean_label's printability: Python's
    }
    let stats = stat_values(&pc)?;
    let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let speed = get("dex") + get("itu");
    if speed < 0 {
        return None;
    }
    let notation = if speed != 0 { format!("1d10+{speed}") } else { "1d10".to_string() };
    let rolled = crate::writes::roll(&notation, &mut rand::thread_rng())?;
    let label: String = format!("Initiative \u{2014} {name}").trim().chars().take(80).collect();
    let user_name: Option<String> = c.db.query_row("SELECT display_name FROM users WHERE id = ?1", [c.p.user_id], |r| r.get(0)).ok()?;
    let user_name = user_name.filter(|n| !n.is_empty()).unwrap_or_else(|| "Someone".into());
    let wid = pc.get("world_id")?.as_i64()?;
    let (stored, iso) = crate::writes::timestamps(std::time::SystemTime::now());
    c.db.execute("INSERT INTO dice_rolls (world_id, user_id, user_name, notation, label, breakdown, total, created_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
        rusqlite::params![wid, c.p.user_id, user_name, notation, label, rolled.breakdown_json, rolled.total, stored]).ok()?;
    let roll_id = c.db.last_insert_rowid();
    orm::signal(&c.db, wid, "dice")?;
    // combat_turns.set_initiative: the same combatant stays active after the re-sort
    let holder = f.current()?;
    f.combatants[mine].as_object_mut()?.insert("initiative".into(), json!(rolled.total.max(1)));
    let mut set: Vec<(&str, Sql)> = vec![("combatants_json", s(&nd_render::py_dumps(&Value::Array(f.combatants.clone()))))];
    if let Some(h) = holder {
        let at = f.order()?.iter().position(|i| *i == h)? as i64;
        set.push(("active_idx", Sql::Integer(at)));
    }
    orm::update(&c.db, "combat_sessions", f.cs.get("id")?.as_i64()?, &set)?;
    combat_touch(c, &f.cs)?;
    orm::touch(&c.db, wid)?;
    let breakdown: Value = serde_json::from_str(&rolled.breakdown_json).ok()?;
    json_response(&json!({"initiative": rolled.total, "roll": {"id": roll_id, "user_name": user_name, "notation": notation, "label": label,
        "breakdown": breakdown, "total": rolled.total, "created_at": format!("{iso}Z")}}))
}

fn hub_end_turn(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let (mut f, _) = hub_encounter(c, &pc)??;
    let pid = pc.get("id")?.as_i64()?;
    if f.current()? != f.find_pc(pid) {
        return None;
    }
    let (round, idx) = f.advance()?;
    hub_save_fight(c, &f, round, idx)?;
    hub_after(c, &pc, f.cs.get("id")?.as_i64()?)
}

fn hub_pass_turn(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let (mut f, mine) = hub_encounter(c, &pc)??;
    if !f.fluid || !f.turn["active_id"].is_null() || f.turn["picker"].is_null() || f.turn.get("picker") != f.combatants[mine].get("id") {
        return None;
    }
    let body = match serde_json::from_slice::<Value>(c.body) {
        Ok(Value::Object(o)) => o,
        _ if plain_numbers(c.body) => Map::new(),
        _ => return None,
    };
    let choice = cs_text(body.get("next"), "").ok()?;
    if choice == "gm" {
        f.turn.insert("picker".into(), Value::Null);
    } else {
        let target = f.combatants.iter().find(|x| x.get("id").and_then(|i| i.as_str()) == Some(choice.as_str()))?;
        if target.get("source").and_then(|v| v.as_str()) != Some("pc") || f.acted().contains(&choice) || f.out(target)? {
            return None;
        }
        f.turn.insert("active_id".into(), json!(choice));
        f.turn.insert("picker".into(), Value::Null);
    }
    let (round, idx) = (f.round(), f.active_idx());
    hub_save_fight(c, &f, round, idx)?;
    hub_after(c, &pc, f.cs.get("id")?.as_i64()?)
}

// ── the GM cockpit's saved workspace (app/routers/cockpit.py) ──

/// `str(v or "")`; None for a value str() spells its own way (a float, a list ...)
fn py_str_or_empty(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(t)) => Some(t.clone()),
        v if !py_truthy(v) => Some(String::new()),
        Some(Value::Bool(true)) => Some("True".into()),
        Some(Value::Number(n)) if n.is_i64() => Some(n.to_string()),
        _ => None,
    }
}

/// Python's `int(v)` for a JSON value: Some(Ok(n)), Some(Err(())) for its TypeError / ValueError, None where it might read the value
/// its own way (Unicode digits, underscores) or the value is past what this counts
fn json_py_int(v: Option<&Value>) -> Option<Result<i128, ()>> {
    Some(match v {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => Err(()),
        Some(Value::Bool(b)) => Ok(*b as i128),
        Some(Value::Number(n)) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => Ok(i as i128),
            (None, Some(u)) => Ok(u as i128),
            _ => {
                let f = n.as_f64()?;
                if !f.is_finite() || f.abs() > 1e30 {
                    return None;
                }
                Ok(f.trunc() as i128)
            }
        },
        Some(Value::String(t)) => {
            if !t.is_ascii() || t.contains('_') {
                return None;
            }
            let w = t.trim_matches(re_space);
            let digits = w.strip_prefix(['+', '-']).unwrap_or(w);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                Err(())
            } else if digits.len() > 30 {
                return None;
            } else {
                Ok(w.parse::<i128>().ok()?)
            }
        }
    })
}

/// `_clamp_int(v, lo, hi, default)`
fn clamp_int(v: Option<&Value>, lo: i64, hi: i64, default: i64) -> Option<i64> {
    Some(match json_py_int(v)? {
        Err(()) => default,
        Ok(n) => n.min(hi as i128).max(lo as i128) as i64,
    })
}

/// `_sanitize_vp`
fn cockpit_vp(raw: Option<&Value>) -> Option<Option<Value>> {
    let Some(Value::Object(r)) = raw else { return Some(None) };
    let (Ok(w), Ok(h)) = (json_py_int(r.get("w"))?, json_py_int(r.get("h"))?) else { return Some(None) };
    Some(((200..=20000).contains(&w) && (150..=20000).contains(&h)).then(|| json!({"w": w as i64, "h": h as i64})))
}

/// The `\s` of a str pattern, for ASCII
fn re_space(ch: char) -> bool {
    matches!(ch, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c' | '\x1c'..='\x1f')
}

fn cockpit_panels(raw: Option<&Value>) -> Option<Vec<Value>> {
    const TYPES: [&str; 20] = ["map", "wmap", "dice", "party", "quests", "entity", "ecard", "notes", "ai", "ai_chat", "combat", "tables", "calendar",
        "gallery", "audio", "video", "imagestudio", "find", "timer", "page"];
    let list = raw?.as_array()?;
    if list.len() > 24 {
        return None;
    }
    let mut out = vec![];
    for p in list {
        let Some(p) = p.as_object() else { continue };
        let ptype = match p.get("type") {
            Some(Value::String(t)) if TYPES.contains(&t.as_str()) => t.as_str(),
            Some(Value::Array(_) | Value::Object(_)) => return None,
            _ => continue,
        };
        let ref_ = py_str_or_empty(p.get("ref"))?;
        if ptype == "page" {
            if !ref_.is_ascii() {
                return None;
            }
            let ok = ref_.starts_with('/') && !ref_.starts_with("//") && ref_.len() <= 120 && !ref_.chars().any(|ch| re_space(ch) || ch == '\\');
            if !ok {
                continue;
            }
        }
        let accent = py_str_or_empty(p.get("accent"))?;
        let accent_ok = accent.is_empty() || (accent.len() == 7 && accent.starts_with('#') && accent[1..].bytes().all(|b| b.is_ascii_hexdigit()));
        let mut data = Map::new();
        if let Some(Value::Object(d)) = p.get("data") {
            data.insert("text".into(), json!(take_chars(&py_str_or_empty(d.get("text"))?, 8000)));
            if ptype == "timer" {
                if let Some(Value::Object(t)) = d.get("timer") {
                    let minutes = clamp_int(t.get("minutes"), 1, 600, 5)?;
                    data.insert("timer".into(), json!({
                        "mode": if t.get("mode").and_then(|v| v.as_str()) == Some("up") { "up" } else { "down" },
                        "minutes": minutes, "running": py_truthy(t.get("running")), "done": py_truthy(t.get("done")),
                        "endAt": clamp_int(t.get("endAt"), 0, 4_102_444_800_000, 0)?, "startAt": clamp_int(t.get("startAt"), 0, 4_102_444_800_000, 0)?,
                        "remainMs": clamp_int(t.get("remainMs"), 0, 36_000_000, minutes * 60_000)?, "elapsedMs": clamp_int(t.get("elapsedMs"), 0, 360_000_000, 0)?}));
                }
            }
            let zoom = clamp_int(d.get("zoom"), 50, 200, 100)?;
            if zoom != 100 {
                data.insert("zoom".into(), json!(zoom));
            }
        }
        let mut e = Map::new();
        e.insert("id".into(), json!(take_chars(&py_str_or_empty(p.get("id"))?, 40)));
        e.insert("type".into(), json!(ptype));
        e.insert("ref".into(), json!(take_chars(&ref_, 120)));
        e.insert("title".into(), json!(take_chars(&py_str_or_empty(p.get("title"))?, 120)));
        e.insert("x".into(), json!(clamp_int(p.get("x"), 0, 8000, 0)?));
        e.insert("y".into(), json!(clamp_int(p.get("y"), 0, 8000, 0)?));
        e.insert("w".into(), json!(clamp_int(p.get("w"), 160, 8000, 420)?));
        e.insert("h".into(), json!(clamp_int(p.get("h"), 120, 8000, 300)?));
        e.insert("z".into(), json!(clamp_int(p.get("z"), 0, 100000, 0)?));
        e.insert("collapsed".into(), json!(py_truthy(p.get("collapsed"))));
        e.insert("accent".into(), json!(if accent_ok { accent } else { String::new() }));
        e.insert("data".into(), Value::Object(data));
        if p.get("parked") == Some(&Value::Bool(true)) {
            e.insert("parked".into(), json!(true));
        }
        if let Some(Value::Object(l)) = p.get("lap") {
            e.insert("lap".into(), json!({"x": clamp_int(l.get("x"), 0, 8000, 0)?, "y": clamp_int(l.get("y"), 0, 8000, 0)?,
                "w": clamp_int(l.get("w"), 160, 8000, 420)?, "h": clamp_int(l.get("h"), 120, 8000, 300)?, "z": clamp_int(l.get("z"), 0, 100000, 0)?,
                "collapsed": py_truthy(l.get("collapsed")), "parked": py_truthy(l.get("parked"))}));
        }
        out.push(Value::Object(e));
    }
    Some(out)
}

fn cockpit_layout(raw: Option<&Value>) -> Option<Value> {
    let empty = Map::new();
    let r = match raw {
        Some(Value::Object(o)) => o,
        _ => &empty,
    };
    let mut out = Map::new();
    out.insert("panels".into(), Value::Array(cockpit_panels(r.get("panels"))?));
    for key in ["vp", "lvp"] {
        if let Some(v) = cockpit_vp(r.get(key))? {
            out.insert(key.into(), v);
        }
    }
    Some(Value::Object(out))
}

/// `cockpit_save_workspace` (GM only): the current arrangement and the named presets, sanitized; the refusals stay Python's
fn cockpit_save_workspace(c: &mut Ctx, _: &[i64]) -> Option<Response<Body>> {
    if !c.p.is_gm {
        return None;
    }
    let wid = c.world_id()?;
    let body = c.json()?;
    let mut presets = Map::new();
    match body.get("presets") {
        None | Some(Value::Null) => {}
        Some(Value::Object(o)) => {
            for (name, layout) in o.iter().take(12) {
                let name = take_chars(&strip(name), 40);
                if name.is_empty() {
                    continue;
                }
                presets.insert(name, cockpit_layout(Some(layout))?);
            }
        }
        _ => return None,
    }
    let ws = json!({"current": cockpit_layout(body.get("current"))?, "presets": presets});
    let dumped = nd_render::py_dumps(&ws);
    if dumped.len() > 128 * 1024 {
        return None;
    }
    orm::update(&c.db, "worlds", wid, &[("cockpit_ws_json", s(&dumped))])?;
    json_response(&json!({"ok": true}))
}

// ── the character hub: handouts seen, companions (app/routers/character_hub.py) ──

/// `_json_body_raw`: the body as an object (anything else: Python's 400)
fn hub_body(c: &Ctx) -> Option<Map<String, Value>> {
    c.json()
}

fn hub_handouts_seen(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let newest: Option<i64> = c.db.query_row("SELECT id FROM world_handouts WHERE world_id = ?1 ORDER BY id DESC LIMIT 1", [pc.get("world_id")?.as_i64()?], |r| r.get(0)).ok();
    set_pc_pref(c, &pc, "handouts_seen", newest.map(|n| json!(n)))?;
    json_response(&json!({"ok": true}))
}

/// `_place_rows(world)`: the ids of the pages this world offers players (the navigation bar's visibility, as the PLAYER role)
fn hub_place_ids(w: &Map<String, Value>) -> Option<Vec<String>> {
    const SKIP: [&str; 7] = ["characters", "cockpit", "schedule", "androidapp", "character_sheets", "editor", "studio_console"];
    let wtext = |k: &str| w.get(k).and_then(|v| v.as_str());
    let mut out = vec![];
    for item in nd_render::app::build_catalog(Some(w))? {
        let id = item.get("id")?.as_str()?;
        let cond = item.get("condition").and_then(|v| v.as_str());
        if SKIP.contains(&id) || matches!(cond, Some("dreamlands_enabled") | Some("king_in_yellow_enabled")) {
            continue;
        }
        match item.get("player_section").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
            Some(section) => {
                if nd_perm::sections::section_level(false, Some("player"), wtext("section_access_json"), wtext("custom_kinds_json"), section)? == "none" {
                    continue;
                }
            }
            None if nd_render::app::truthy(item.get("gm_only")) => continue,
            None => {}
        }
        if let Some(cond) = cond {
            if !nd_render::app::truthy(w.get(cond)) {
                continue;
            }
        }
        out.push(id.to_string());
    }
    Some(out)
}

/// `hub_pin_toggle`: pin or unpin one page ({id}); the refusals (a page not open to players, too many pins) stay Python's
fn hub_pin_toggle(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let page = match body.get("id") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(t)) => t.clone(),
        Some(_) => return None,
    };
    let w = row(&c.db, "SELECT * FROM worlds WHERE id = ?1", pc.get("world_id")?.as_i64()?)?;
    let valid = hub_place_ids(&w)?;
    let stored = match pc_prefs(&c.db, ids[0])?.get("pins") {
        None | Some(Value::Null) => vec![],
        Some(Value::Array(a)) => a.clone(),
        Some(_) => return None,
    };
    let mut pins: Vec<String> = stored.iter().filter_map(|v| v.as_str()).filter(|v| valid.iter().any(|x| x == v)).map(str::to_string).collect();
    if let Some(i) = pins.iter().position(|p| *p == page) {
        pins.remove(i);
    } else {
        if !valid.contains(&page) || pins.len() >= 8 {
            return None;
        }
        pins.push(page);
    }
    set_pc_pref(c, &pc, "pins", (!pins.is_empty()).then(|| json!(pins)))?;
    json_response(&json!({"pins": pins}))
}

/// `hub_rest` for an N&D character without a sheet template (`apply_pc_rest`'s native branch): +half PP and MP, all Shock, the
/// once-per-Rest feats back. A character on a template (its system's own Rest rules) stays Python's.
fn hub_rest(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    if body.get("kind").map_or(false, |k| k != "long") || pc.get("sheet_template_id").map_or(false, |v| !v.is_null()) {
        return None;
    }
    let (_, shock, pp, mp, native) = pc_maxima(&c.db, &pc)?;
    if !native {
        return None;
    }
    // _refresh_feats_after_rest
    if let Some(used) = pc_prefs(&c.db, ids[0])?.get("feat_used").filter(|v| py_truthy(Some(v))) {
        let used = used.as_object()?;
        if used.values().any(|v| v == "rest") {
            let keep: Map<String, Value> = used.iter().filter(|(_, v)| *v != "rest").map(|(k, v)| (k.clone(), v.clone())).collect();
            set_pc_pref(c, &pc, "feat_used", (!keep.is_empty()).then(|| Value::Object(keep)))?;
        }
    }
    let (cur_pp, cur_mp, cur_shock) = (col_int(&pc, "pp_current"), col_int(&pc, "mp_current"), col_int(&pc, "shock_current"));
    let (new_pp, new_mp) = (pp.min(cur_pp + pp.div_euclid(2)), mp.min(cur_mp + mp.div_euclid(2)));
    orm::update(&c.db, "player_characters", ids[0], &[("pp_current", Sql::Integer(new_pp)), ("mp_current", Sql::Integer(new_mp)), ("shock_current", Sql::Integer(shock))])?;
    char_log(c, &pc, "rest", "Took a rest", &json!({}))?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    let (id, name) = (pc.get("id").cloned(), pc.get("name").cloned());
    json_response(&json!({"kind": "long",
        "result": {"id": id, "name": name, "pp_current": new_pp, "pp_max": pp, "mp_current": new_mp, "mp_max": mp, "shock_current": shock},
        "snapshot": {"id": id, "name": name, "pp_current": cur_pp, "mp_current": cur_mp, "shock_current": cur_shock}}))
}

/// `hub_rest_undo` (`restore_pc_snapshot`) for a character without a sheet template: PP, MP and Shock put back
fn hub_rest_undo(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let snap = body.get("snapshot")?.as_object()?;
    if pc.get("sheet_template_id").map_or(false, |v| !v.is_null()) || snap.get("id")?.as_i64()? != ids[0] || matches!(snap.get("id"), Some(Value::Bool(_))) {
        return None;
    }
    let mut set = vec![];
    for col in ["pp_current", "mp_current", "shock_current"] {
        let v = match snap.get(col) {
            None => continue,
            Some(Value::Null) | Some(Value::Bool(false)) => 0,
            Some(Value::Bool(true)) => 1,
            Some(Value::Number(n)) => match n.as_i64() {
                Some(i) => i,
                None => {
                    let f = n.as_f64()?.trunc();
                    if !f.is_finite() || f.abs() > 9e15 {
                        return None;
                    }
                    f as i64
                }
            },
            Some(_) => return None,
        };
        set.push((col, Sql::Integer(v.max(0))));
    }
    orm::update(&c.db, "player_characters", ids[0], &set)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"ok": true}))
}

/// `char_extras.undo_spec`: the entry's undo as an object with an op, else empty
fn log_undo_spec(e: &Map<String, Value>) -> Map<String, Value> {
    let raw = e.get("undo_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(o)) if py_truthy(o.get("op")) => o,
        _ => Map::new(),
    }
}

/// `_undo_key`; None = a spec Python reads its own way (a non-string op, values that are not an object)
fn log_undo_key(spec: &Map<String, Value>) -> Option<String> {
    let op = match spec.get("op") {
        Some(Value::String(t)) => t.clone(),
        _ => return None,
    };
    Some(match op.as_str() {
        "currency" => match spec.get("abbr") {
            None | Some(Value::Null) => "currency:".into(),
            Some(Value::String(a)) => format!("currency:{}", a.to_lowercase()),
            Some(_) => return None,
        },
        "cf" | "cols" => {
            let mut keys: Vec<String> = match spec.get("values") {
                Some(Value::Object(o)) => o.keys().cloned().collect(),
                v if !py_truthy(v) => vec![],
                _ => return None,
            };
            keys.sort();
            format!("{op}:{}", keys.join(","))
        }
        _ => op,
    })
}

/// A JSON whole number Python's `isinstance(v, int)` takes (a bool is Python's: forwarded)
fn log_int(v: Option<&Value>) -> Option<Option<i64>> {
    match v {
        Some(Value::Bool(_)) => None,
        Some(Value::Number(n)) if n.is_i64() => Some(n.as_i64()),
        Some(Value::Number(n)) if n.is_u64() => None,
        _ => Some(None),
    }
}

/// `hub_log_undo` (`char_extras.undo`) for the numbers, coins, conditions and resource columns; the refusals, and the equipment /
/// advance / custom-field undos, stay Python's
fn hub_log_undo(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let e = row(&c.db, "SELECT * FROM character_log WHERE id = ?1", ids[1])?;
    if e.get("character_id")?.as_i64()? != ids[0] || py_truthy(e.get("undone")) {
        return None;
    }
    let spec = log_undo_spec(&e);
    let key = log_undo_key(&spec)?;
    // can_undo: nothing later (and not undone) changed the same thing
    let mut st = c.db.prepare("SELECT undo_json FROM character_log WHERE character_id = ?1 AND id > ?2 AND undone = 0").ok()?;
    let later: Vec<Option<String>> = st.query_map([ids[0], ids[1]], |r| r.get(0)).ok()?.collect::<Result<_, _>>().ok()?;
    for raw in later {
        let mut m = Map::new();
        m.insert("undo_json".into(), raw.map_or(Value::Null, Value::String));
        let sp = log_undo_spec(&m);
        if !sp.is_empty() && log_undo_key(&sp)? == key {
            return None;
        }
    }
    let value = spec.get("value");
    let mut set: Vec<(&str, Sql)> = vec![];
    let mut unuse: Option<String> = None;
    match key.split(':').next()? {
        op @ ("hp" | "shock" | "pp" | "mp" | "xp" | "level") => {
            let v = log_int(value)?.filter(|v| *v >= 0)?;
            let col = match op {
                "hp" => "current_hp",
                "shock" => "shock_current",
                "pp" => "pp_current",
                "mp" => "mp_current",
                o => o,
            };
            set.push((col, Sql::Integer(v)));
        }
        "currency" => {
            let v = log_int(value)??;
            let mut coins = match pc.get("currency_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).map(serde_json::from_str::<Value>) {
                None => vec![],
                Some(Ok(Value::Array(a))) => a,
                _ => return None,
            };
            let want = key.strip_prefix("currency:")?.to_string();
            let low = |c: &Map<String, Value>, k: &str| -> Option<String> {
                match c.get(k) {
                    None | Some(Value::Null) => Some(String::new()),
                    Some(Value::String(t)) => Some(t.to_lowercase()),
                    Some(x) if !py_truthy(Some(x)) => Some(String::new()),
                    _ => None,
                }
            };
            let mut hit = None;
            for (i, coin) in coins.iter().enumerate() {
                let Some(o) = coin.as_object() else { continue };
                if low(o, "abbr")? == want || low(o, "label")? == want {
                    hit = Some(i);
                    break;
                }
            }
            coins[hit?].as_object_mut()?.insert("value".into(), json!(v));
            set.push(("currency_json", s(&nd_render::py_dumps(&Value::Array(coins)))));
        }
        "conditions" => {
            let list = value?.as_array()?;
            let mut out = vec![];
            for v in list.iter().take(12) {
                out.push(json!(v.as_str()?.chars().take(40).collect::<String>()));
            }
            if list.iter().skip(12).any(|v| !v.is_string()) {
                return None;
            }
            set.push(("conditions_json", s(&nd_render::py_dumps(&Value::Array(out)))));
        }
        "cols" => {
            let values = spec.get("values")?.as_object()?;
            for (col, v) in values {
                let col = ["pp_current", "mp_current", "shock_current", "current_hp"].into_iter().find(|x| x == col)?;
                if let Some(n) = log_int(Some(v))?.filter(|n| *n >= 0) {
                    set.push((col, Sql::Integer(n)));
                }
            }
            match spec.get("unuse") {
                v if !py_truthy(v) => {}
                Some(Value::String(t)) => unuse = Some(t.clone()),
                _ => return None,
            }
        }
        _ => return None,
    }
    // the dict of values keeps its last write per column, as setattr does
    orm::update(&c.db, "player_characters", ids[0], &set)?;
    if let Some(k) = unuse {
        let used = pc_prefs(&c.db, ids[0])?.get("feat_used").filter(|v| py_truthy(Some(v))).cloned().unwrap_or(json!({}));
        let mut used = used.as_object()?.clone();
        used.shift_remove(&k);
        set_pc_pref(c, &pc, "feat_used", (!used.is_empty()).then(|| Value::Object(used)))?;
    }
    orm::update(&c.db, "character_log", ids[1], &[("undone", Sql::Integer(1))])?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&json!({"ok": true}))
}

/// `_hub_pc`: (pc, is_owner) for the owning player, or a GM looking in
fn hub_viewer_pc(c: &Ctx, id: i64) -> Option<(Map<String, Value>, bool)> {
    let pc = row(&c.db, "SELECT * FROM player_characters WHERE id = ?1", id)?;
    let owner = pc.get("owner_user_id").and_then(|v| v.as_i64()) == Some(c.p.user_id);
    if !owner && !c.p.is_gm {
        return None;
    }
    let wid = pc.get("world_id")?.as_i64()?;
    row(&c.db, "SELECT id FROM worlds WHERE id = ?1", wid)?;
    if !c.p.is_gm {
        let member: Option<i64> = c.db.query_row("SELECT id FROM world_memberships WHERE world_id = ?1 AND user_id = ?2 LIMIT 1", [wid, c.p.user_id], |r| r.get(0)).ok();
        member?;
    }
    Some((pc, owner))
}

/// `_clean_keepsake_text`: " ".join(str(value or "").split())[:limit]; None for a value str() would spell its own way
fn keepsake_text(v: Option<&Value>, limit: usize) -> Option<String> {
    let t = match v {
        Some(Value::String(t)) => t.as_str(),
        v if !py_truthy(v) => "",
        _ => return None,
    };
    if t.chars().any(|ch| ('\u{1c}'..='\u{1f}').contains(&ch) || ch == '\u{85}') {
        return None;
    }
    Some(take_chars(&t.split_whitespace().collect::<Vec<_>>().join(" "), limit))
}

/// `strftime("%Y-%m-%dT%H:%M:%SZ")` of a stored DATETIME (row_map's {"__dt__": ...})
fn py_utc_z(v: Option<&Value>) -> Value {
    match v.and_then(|x| x.get("__dt__").unwrap_or(x).as_str()).filter(|t| t.len() >= 19) {
        Some(t) => json!(format!("{}Z", t[..19].replacen(' ', "T", 1))),
        None => Value::Null,
    }
}

fn keepsake_out(k: &Map<String, Value>) -> Value {
    let txt = |key: &str| k.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string();
    json!({"id": k.get("id"), "kind": k.get("kind"), "url": txt("url"), "text": txt("text"), "name": k.get("name"), "description": txt("description"),
           "by": k.get("saved_by"), "own": py_truthy(k.get("own")), "at": py_utc_z(k.get("created_at"))})
}

/// `hub_keepsake_add` for a handout shown to the table, or a GM's raw file / text; private notes and Session Log media stay Python's
fn hub_keepsake_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let (pc, owner) = hub_viewer_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let src = body.get("source")?.as_object()?;
    let wid = pc.get("world_id")?.as_i64()?;
    let (kind, url, text, default_name) = match src.get("type").and_then(|v| v.as_str())? {
        "handout" => {
            let hid = match src.get("id") {
                Some(Value::Number(n)) => n.as_i64()?,
                _ => return None,
            };
            let h = row(&c.db, "SELECT * FROM world_handouts WHERE id = ?1", hid)?;
            if h.get("world_id")?.as_i64()? != wid {
                return None;
            }
            let label = h.get("label").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("Handout").to_string();
            ("image".to_string(), h.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string(), String::new(), label)
        }
        "raw" if c.p.is_gm => match src.get("kind").and_then(|v| v.as_str())? {
            "text" => {
                let t = match src.get("text") {
                    Some(Value::String(t)) => t.trim().to_string(),
                    _ => return None,
                };
                if t.is_empty() || t.chars().any(|ch| ('\u{1c}'..='\u{1f}').contains(&ch) || ch == '\u{85}') {
                    return None;
                }
                ("text".to_string(), String::new(), take_chars(&t, 4000), String::new())
            }
            what @ ("image" | "video" | "audio") => {
                let u = match src.get("url") {
                    Some(Value::String(t)) => t.trim().to_string(),
                    _ => return None,
                };
                if !u.starts_with("/uploads/") || u.contains("..") || u.chars().count() > 500 || u.chars().any(|ch| ('\u{1c}'..='\u{1f}').contains(&ch) || ch == '\u{85}') {
                    return None;
                }
                let tail = u.rsplit('/').next()?.to_string();
                (what.to_string(), u, String::new(), tail)
            }
            _ => return None,
        },
        _ => return None,
    };
    let name = Some(keepsake_text(body.get("name"), 120)?).filter(|n| !n.is_empty()).or_else(|| keepsake_text(Some(&json!(default_name)), 120)).filter(|n| !n.is_empty())?;
    let description = keepsake_text(body.get("description"), 400)?;
    let n: i64 = c.db.query_row("SELECT COUNT(*) FROM character_keepsakes WHERE character_id = ?1", [ids[0]], |r| r.get(0)).ok()?;
    if n >= 100 {
        return None;
    }
    let by = if c.p.is_gm && !owner { "gm" } else { "player" };
    let id = orm::insert(&c.db, "character_keepsakes", &[("world_id", Sql::Integer(wid)), ("character_id", Sql::Integer(ids[0])), ("kind", s(&kind)), ("url", s(&url)),
        ("text", s(&text)), ("name", s(&name)), ("description", s(&description)), ("saved_by", s(by))])?;
    json_response(&keepsake_out(&row(&c.db, "SELECT * FROM character_keepsakes WHERE id = ?1", id)?))
}

/// `hub_keepsake_upload`: a player's OWN picture, song or video kept on their character, within the world's per-kind limit (checked before
/// the file is written); the owner only
fn hub_keepsake_upload(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let (f, pick) = form_and_file(c, "file")?;
    let pick = pick?;
    let fname = pick.filename(c)?.to_string();
    let ext = py_suffix_lower(&fname)?;
    let (kind, col) = if IMAGE_EXTS.contains(&ext.as_str()) {
        ("image", "personal_images_max")
    } else if [".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"].contains(&ext.as_str()) {
        ("audio", "personal_audio_max")
    } else if [".mp4", ".m4v", ".ogv", ".mov"].contains(&ext.as_str()) {
        ("video", "personal_video_max")
    } else {
        return None;
    };
    let wid = pc.get("world_id")?.as_i64()?;
    let stored: Option<i64> = c.db.query_row(&format!("SELECT {col} FROM worlds WHERE id = ?1"), [wid], |r| r.get(0)).ok()?;
    let cap = stored.map_or(match kind { "image" => 30, "audio" => 10, _ => 5 }, |n| n.clamp(0, 500));
    let have: i64 = c.db.query_row("SELECT COUNT(*) FROM character_keepsakes WHERE character_id = ?1 AND own = 1 AND kind = ?2",
        rusqlite::params![ids[0], kind], |r| r.get(0)).ok()?;
    if cap == 0 || have >= cap {
        return None;
    }
    let url = if kind == "image" {
        save_picture(c, &pick, "portraits", true)??
    } else {
        let (url, saved) = save_inline_av(c, &pick)??;
        if saved != kind {
            return None;
        }
        url
    };
    let text = |k: &str, cap: usize| keepsake_text(f.get(k).map(|v| json!(v)).as_ref(), cap);
    let stem = py_path_parts(if fname.is_empty() { "file" } else { &fname })?.0;
    let label = Some(text("name", 120)?).filter(|t| !t.is_empty())
        .or_else(|| keepsake_text(Some(&json!(stem)), 120).filter(|t| !t.is_empty()))
        .unwrap_or_else(|| "My file".to_string());
    let description = text("description", 400)?;
    let id = orm::insert(&c.db, "character_keepsakes", &[("world_id", Sql::Integer(wid)), ("character_id", Sql::Integer(ids[0])), ("kind", s(kind)),
        ("url", s(&url)), ("name", s(&label)), ("description", s(&description)), ("saved_by", s("player")), ("own", Sql::Integer(1))])?;
    json_response(&keepsake_out(&row(&c.db, "SELECT * FROM character_keepsakes WHERE id = ?1", id)?))
}

fn hub_keepsake_of(c: &Ctx, ids: &[i64]) -> Option<Map<String, Value>> {
    hub_viewer_pc(c, ids[0])?;
    let k = row(&c.db, "SELECT * FROM character_keepsakes WHERE id = ?1", ids[1])?;
    (k.get("character_id")?.as_i64()? == ids[0]).then_some(k)
}

fn hub_keepsake_edit(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let k = hub_keepsake_of(c, ids)?;
    let body = hub_body(c)?;
    let mut set = vec![];
    if body.contains_key("name") {
        let name = keepsake_text(body.get("name"), 120).filter(|n| !n.is_empty())?;
        set.push(("name", s(&name)));
    }
    if body.contains_key("description") {
        set.push(("description", s(&keepsake_text(body.get("description"), 400)?)));
    }
    orm::update(&c.db, "character_keepsakes", ids[1], &set)?;
    let _ = k;
    json_response(&keepsake_out(&row(&c.db, "SELECT * FROM character_keepsakes WHERE id = ?1", ids[1])?))
}

/// `hub_keepsake_delete` of a kept thing that is not the player's own upload (that one's file goes too: Python's)
fn hub_keepsake_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let k = hub_keepsake_of(c, ids)?;
    if py_truthy(k.get("own")) && k.get("url").map_or(false, |u| py_truthy(Some(u))) {
        return None;
    }
    c.db.execute("DELETE FROM character_keepsakes WHERE id = ?1", [ids[1]]).ok()?;
    json_response(&json!({"ok": true}))
}

fn hub_companions_json(c: &Ctx, pc_id: i64) -> Option<Value> {
    let mut st = c.db.prepare("SELECT * FROM character_companions WHERE character_id = ?1 ORDER BY id").ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map([pc_id], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    let list: Vec<Value> = rows.iter().map(|r| json!({"id": r.get("id"), "name": r.get("name"), "kind": r.get("kind"),
        "hp": r.get("hp").and_then(|v| v.as_i64()).unwrap_or(0), "hp_max": r.get("hp_max").and_then(|v| v.as_i64()).unwrap_or(0),
        "notes": r.get("notes").and_then(|v| v.as_str()).unwrap_or("")})).collect();
    Some(json!({"companions": list}))
}

/// `_companion_int(body, key, low, high)`: int(v) clamped; None for a value int() refuses (Python's 400) or reads its own way
fn comp_int(v: Option<&Value>, low: i64, high: i64) -> Option<i64> {
    let n = match v? {
        Value::Bool(b) => *b as i64,
        Value::Number(n) => match n.as_i64() {
            Some(i) => i,
            None => {
                let f = n.as_f64()?;
                if !f.is_finite() || f.abs() > 1e15 {
                    return None;
                }
                f.trunc() as i64
            }
        },
        Value::String(t) => py_int_text(t).ok()?,
        _ => return None,
    };
    Some(n.clamp(low, high))
}

/// `_apply_companion(c, body)` on the stored row `cur`: the columns to write
fn comp_apply(cur: &Map<String, Value>, body: &Map<String, Value>) -> Option<Vec<(&'static str, Sql)>> {
    let mut set: Vec<(&'static str, Sql)> = vec![];
    if body.contains_key("name") {
        let name = take_chars(&nd_render::text::py_split_join(&cs_text(body.get("name"), "").ok()?), 80);
        if name.is_empty() {
            return None;
        }
        set.push(("name", s(&name)));
    }
    if body.contains_key("kind") {
        let k = match body.get("kind").and_then(|v| v.as_str()) {
            Some(k @ ("familiar" | "hireling" | "mount" | "vehicle" | "other")) => k,
            _ => "other",
        };
        set.push(("kind", s(k)));
    }
    if body.contains_key("notes") {
        set.push(("notes", s(&take_chars(&strip(&cs_text(body.get("notes"), "").ok()?), 600))));
    }
    let mut hp_max = cur.get("hp_max").and_then(|v| v.as_i64()).unwrap_or(0);
    let mut hp = cur.get("hp").and_then(|v| v.as_i64()).unwrap_or(0);
    let mut hp_set = false;
    if body.contains_key("hp_max") {
        hp_max = comp_int(body.get("hp_max"), 0, 9999)?;
        set.push(("hp_max", Sql::Integer(hp_max)));
    }
    if body.contains_key("hp") {
        hp = comp_int(body.get("hp"), 0, 9999)?;
        hp_set = true;
    }
    if body.contains_key("hp_delta") {
        hp = (hp + comp_int(body.get("hp_delta"), -9999, 9999)?).clamp(0, 9999);
        hp_set = true;
    }
    if hp_max != 0 && hp > hp_max {
        hp = hp_max;
        hp_set = true;
    }
    if hp_set {
        set.push(("hp", Sql::Integer(hp)));
    }
    Some(set)
}

fn hub_companion_add(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let n: i64 = c.db.query_row("SELECT COUNT(*) FROM character_companions WHERE character_id = ?1", [ids[0]], |r| r.get(0)).ok()?;
    if n >= 12 || !body.contains_key("name") {
        return None;
    }
    let mut full = Map::new();
    full.insert("kind".into(), json!("familiar"));
    for (k, v) in &body {
        full.insert(k.clone(), v.clone());
    }
    let cur = Map::new();
    let set = comp_apply(&cur, &full)?;
    let mut cols: Vec<(&str, Sql)> = vec![("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)), ("character_id", Sql::Integer(ids[0]))];
    if !set.iter().any(|(k, _)| *k == "name") {
        cols.push(("name", s("x")));
    }
    cols.extend(set);
    orm::insert(&c.db, "character_companions", &cols)?;
    orm::touch(&c.db, pc.get("world_id")?.as_i64()?)?;
    json_response(&hub_companions_json(c, ids[0])?)
}

fn hub_companion_target(c: &Ctx, key: &str) -> Option<(i64, Map<String, Value>)> {
    let (pc, cid) = key.split_once('/')?;
    let (pc, cid): (i64, i64) = (pc.parse().ok()?, cid.parse().ok()?);
    hub_owned_pc(c, pc)?;
    let row_ = row(&c.db, "SELECT * FROM character_companions WHERE id = ?1", cid)?;
    (row_.get("character_id")?.as_i64()? == pc).then_some((pc, row_))
}

fn hub_companion_edit(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let (pc, cur) = hub_companion_target(c, key)?;
    let body = hub_body(c)?;
    let set = comp_apply(&cur, &body)?;
    orm::update(&c.db, "character_companions", cur.get("id")?.as_i64()?, &set)?;
    json_response(&hub_companions_json(c, pc)?)
}

fn hub_companion_remove(c: &mut Ctx, key: &str) -> Option<Response<Body>> {
    let (pc, cur) = hub_companion_target(c, key)?;
    c.db.execute("DELETE FROM character_companions WHERE id = ?1", [cur.get("id")?.as_i64()?]).ok()?;
    json_response(&hub_companions_json(c, pc)?)
}

// ── the character hub's journal and goals (character_hub.py hub_entry_*) ──

/// `_party_sessions(db, pc, _party_ids(db, pc))`: the sessions of the parties the character is in, newest number first (60)
fn hub_party_sessions(c: &Ctx, pc: &Map<String, Value>) -> Option<Vec<Map<String, Value>>> {
    let wid = pc.get("world_id")?.as_i64()?;
    let ids = pc_party_ids(c, pc)?;
    if ids.is_empty() {
        return Some(vec![]);
    }
    let marks: Vec<String> = ids.iter().map(|i| i.to_string()).collect();
    let mut st = c.db.prepare(&format!("SELECT * FROM game_sessions WHERE world_id = ?1 AND party_id IN ({}) ORDER BY session_num DESC LIMIT 60", marks.join(","))).ok()?;
    let rows: Vec<Map<String, Value>> = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
    Some(rows)
}

/// `parties_for_pc`: the ids of the world's parties that list the character, name-ordered
fn pc_party_ids(c: &Ctx, pc: &Map<String, Value>) -> Option<Vec<i64>> {
    let wid = pc.get("world_id")?.as_i64()?;
    let pid = pc.get("id")?.as_i64()?;
    let mut st = c.db.prepare("SELECT id, member_pc_ids_json FROM parties WHERE world_id = ?1 ORDER BY name").ok()?;
    let parties: Vec<(i64, Option<String>)> = st.query_map([wid], |r| Ok((r.get(0)?, r.get(1)?))).ok()?.collect::<Result<_, _>>().ok()?;
    let mut ids = vec![];
    for (id, raw) in parties {
        let members = match raw.filter(|t| !t.is_empty()).map(|t| serde_json::from_str::<Value>(&t)) {
            None => continue,
            Some(Ok(Value::Array(a))) => a,
            Some(Ok(_)) | Some(Err(_)) => continue,
        };
        if members.iter().any(|m| m.is_boolean()) {
            return None; // True == 1 in Python's `in`
        }
        if members.iter().any(|m| m.as_i64() == Some(pid) && !m.is_f64()) {
            ids.push(id);
        }
    }
    Some(ids)
}

/// `hub_loot_claim`: claim / unclaim a party-stash item for the character in the URL (owner only)
fn hub_loot_claim(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let action = body.get("action").and_then(|v| v.as_str()).filter(|a| *a == "claim" || *a == "unclaim")?.to_string();
    let lid = body.get("lid").and_then(|v| v.as_str()).filter(|l| !l.is_empty())?.to_string();
    let mut hit = None;
    for party_id in pc_party_ids(c, &pc)? {
        let p = row(&c.db, "SELECT * FROM parties WHERE id = ?1", party_id)?;
        let loot = load_loot(&p)?;
        if let Some(i) = loot.iter().position(|it| it.get("lid").and_then(|v| v.as_str()) == Some(lid.as_str())) {
            hit = Some((p, loot, i));
            break;
        }
    }
    let (p, mut loot, i) = hit?;
    let claimed = loot[i].get_mut("claimed_by")?.as_array_mut()?;
    let at = claimed.iter().position(|x| x.as_i64() == Some(ids[0]));
    match (action.as_str(), at) {
        ("claim", None) => claimed.push(json!(ids[0])),
        ("unclaim", Some(k)) => {
            claimed.remove(k);
        }
        _ => {}
    }
    let list = Value::Array(loot.iter().cloned().map(Value::Object).collect());
    orm::update(&c.db, "parties", p.get("id")?.as_i64()?, &[("loot_json", s(&nd_render::py_dumps(&list)))])?;
    orm::touch(&c.db, p.get("world_id")?.as_i64()?)?;
    let mut names: Vec<(i64, String)> = vec![];
    for it in &loot {
        for cid in it.get("claimed_by")?.as_array()? {
            let cid = cid.as_i64()?;
            if !names.iter().any(|(k, _)| *k == cid) {
                if let Ok(n) = c.db.query_row("SELECT name FROM player_characters WHERE id = ?1", [cid], |r| r.get::<_, String>(0)) {
                    names.push((cid, n));
                }
            }
        }
    }
    let view: Vec<Value> = loot.iter().map(|it| {
        let cl: Vec<i64> = it["claimed_by"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
        json!({"lid": it["lid"], "name": it["name"], "qty": it["qty"], "notes": it["notes"], "claimed": !cl.is_empty(), "mine": cl.contains(&ids[0]),
               "claimers": cl.iter().filter_map(|x| names.iter().find(|(k, _)| k == x).map(|(_, n)| n.clone())).collect::<Vec<_>>()})
    }).collect();
    json_response(&json!({"loot": view}))
}

/// A stored DateTime as Python's `.isoformat()`
fn py_isoformat(v: Option<&Value>) -> Value {
    // row_map wraps DATETIME columns as {"__dt__": "..."}
    match v.and_then(|x| x.get("__dt__").unwrap_or(x).as_str()) {
        Some(t) => {
            let t = t.replacen(' ', "T", 1);
            json!(t.strip_suffix(".000000").map(str::to_string).unwrap_or(t))
        }
        None => Value::Null,
    }
}

fn hub_entry_out(e: &Map<String, Value>, sessions: &[Map<String, Value>]) -> Value {
    let txt = |k: &str| e.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let label = e.get("session_id").and_then(|v| v.as_i64()).and_then(|sid| sessions.iter().find(|s| s.get("id").and_then(|i| i.as_i64()) == Some(sid))).map(|s| {
        let num = s.get("session_num").and_then(|v| v.as_i64()).map_or("None".to_string(), |n| n.to_string());
        let title = s.get("title").and_then(|v| v.as_str()).unwrap_or("None");
        json!(format!("#{num} {title}"))
    });
    json!({"id": e.get("id"), "kind": e.get("kind"), "title": txt("title"), "body": txt("body"), "status": txt("status"),
           "session_id": e.get("session_id"), "session": label.unwrap_or(Value::Null),
           "created_at": py_isoformat(e.get("created_at")), "updated_at": py_isoformat(e.get("updated_at"))})
}

/// `_clean_text(body, key, limit)`: Ok(None) when absent; Err for one too long (Python's 400) or read through str() its own way
fn hub_clean(body: &Map<String, Value>, key: &str, limit: usize) -> Result<Option<String>, ()> {
    if !body.contains_key(key) {
        return Ok(None);
    }
    let v = strip(&cs_text(body.get(key), "")?);
    if v.chars().count() > limit {
        return Err(());
    }
    Ok(Some(v))
}

/// `_session_choice(body, allowed)`: Ok(None) absent, Ok(Some(0)) clear, Ok(Some(id)); Err = Python's 400
fn hub_session_choice(body: &Map<String, Value>, sessions: &[Map<String, Value>]) -> Result<Option<i64>, ()> {
    if !body.contains_key("session_id") {
        return Ok(None);
    }
    let sid = match body.get("session_id") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => return Ok(Some(0)),
        Some(Value::String(t)) if t.is_empty() => return Ok(Some(0)),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => return Ok(Some(0)),
        Some(v) => comp_int(Some(v), i64::MIN, i64::MAX).ok_or(())?,
    };
    if !sessions.iter().any(|s| s.get("id").and_then(|i| i.as_i64()) == Some(sid)) {
        return Err(());
    }
    Ok(Some(sid))
}

const GOAL_STATUSES: [&str; 4] = ["active", "achieved", "failed", "dropped"];

fn hub_entry_create(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let body = hub_body(c)?;
    let kind = match body.get("kind") {
        None => "journal",
        Some(Value::String(k)) if k == "journal" || k == "goal" => k.as_str(),
        _ => return None,
    };
    let title = hub_clean(&body, "title", 200).ok()?.unwrap_or_default();
    let text = hub_clean(&body, "body", 20000).ok()?.unwrap_or_default();
    if title.is_empty() && text.is_empty() {
        return None;
    }
    let sessions = hub_party_sessions(c, &pc)?;
    let (status, sid) = if kind == "goal" {
        let st = match body.get("status") {
            None => "active",
            Some(Value::String(t)) if GOAL_STATUSES.contains(&t.as_str()) => t.as_str(),
            _ => return None,
        };
        (st.to_string(), Sql::Null)
    } else {
        let ch = hub_session_choice(&body, &sessions).ok()?;
        (String::new(), ch.filter(|n| *n != 0).map_or(Sql::Null, Sql::Integer))
    };
    let id = orm::insert(&c.db, "character_journal_entries", &[("world_id", Sql::Integer(pc.get("world_id")?.as_i64()?)), ("character_id", Sql::Integer(ids[0])),
        ("kind", s(kind)), ("title", s(&title)), ("body", s(&text)), ("status", s(&status)), ("session_id", sid)])?;
    let e = row(&c.db, "SELECT * FROM character_journal_entries WHERE id = ?1", id)?;
    json_response(&hub_entry_out(&e, &sessions))
}

fn hub_owned_entry(c: &Ctx, pc_id: i64, entry_id: i64) -> Option<Map<String, Value>> {
    let e = row(&c.db, "SELECT * FROM character_journal_entries WHERE id = ?1", entry_id)?;
    (e.get("character_id")?.as_i64()? == pc_id).then_some(e)
}

fn hub_entry_update(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    let pc = hub_owned_pc(c, ids[0])?;
    let e = hub_owned_entry(c, ids[0], ids[1])?;
    let body = hub_body(c)?;
    let title = hub_clean(&body, "title", 200).ok()?;
    let text = hub_clean(&body, "body", 20000).ok()?;
    let txt = |k: &str| e.get(k).and_then(|v| v.as_str()).map(str::to_string);
    let new_title = title.or_else(|| txt("title"));
    let new_text = text.or_else(|| txt("body"));
    if new_title.as_deref().unwrap_or("").is_empty() && new_text.as_deref().unwrap_or("").is_empty() {
        return None;
    }
    let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
    let mut set: Vec<(&str, Sql)> = vec![];
    if body.contains_key("status") {
        let st = body.get("status").and_then(|v| v.as_str()).filter(|t| GOAL_STATUSES.contains(t));
        if kind != "goal" || st.is_none() {
            return None;
        }
        set.push(("status", s(st?)));
    }
    let sessions = hub_party_sessions(c, &pc)?;
    if body.contains_key("session_id") {
        if kind != "journal" {
            return None;
        }
        let ch = hub_session_choice(&body, &sessions).ok()??;
        set.push(("session_id", if ch == 0 { Sql::Null } else { Sql::Integer(ch) }));
    }
    set.push(("title", new_title.map_or(Sql::Null, |t| s(&t))));
    set.push(("body", new_text.map_or(Sql::Null, |t| s(&t))));
    orm::update(&c.db, "character_journal_entries", ids[1], &set)?;
    let e = row(&c.db, "SELECT * FROM character_journal_entries WHERE id = ?1", ids[1])?;
    json_response(&hub_entry_out(&e, &sessions))
}

fn hub_entry_delete(c: &mut Ctx, ids: &[i64]) -> Option<Response<Body>> {
    hub_owned_pc(c, ids[0])?;
    hub_owned_entry(c, ids[0], ids[1])?;
    c.db.execute("DELETE FROM character_journal_entries WHERE id = ?1", [ids[1]]).ok()?;
    json_response(&json!({"ok": true}))
}

// ── the player cockpit (app/routers/cockpit.py _player_cockpit) ──

/// `party_refs.member_ids`: the int ids of a JSON list column, bad data tolerated (a bool, which `isinstance(int)` takes: Python's)
fn member_ids(raw: Option<&Value>) -> Option<Vec<i64>> {
    let raw = raw.and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    Some(match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(a)) => {
            if a.iter().any(|x| x.is_boolean()) {
                return None;
            }
            a.iter().filter(|x| x.is_i64()).filter_map(|x| x.as_i64()).collect()
        }
        _ => vec![],
    })
}

/// `_member_vitals` for N&D characters (one on a sheet template - its system's tracks - stays Python's), sorted by name
fn member_vitals(db: &Connection, pcs: &[Map<String, Value>]) -> Option<Vec<Value>> {
    let mut sorted: Vec<&Map<String, Value>> = pcs.iter().collect();
    sorted.sort_by(|a, b| a.get("name").and_then(|v| v.as_str()).unwrap_or("").cmp(b.get("name").and_then(|v| v.as_str()).unwrap_or("")));
    let mut out = vec![];
    for pc in sorted {
        if pc.get("sheet_template_id").is_some_and(|v| !v.is_null()) {
            return None;
        }
        let (hp_max, shock, _, _, native) = pc_maxima(db, pc)?;
        if !native {
            return None;
        }
        let conds: Vec<Value> = match serde_json::from_str::<Value>(pc.get("conditions_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
            Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_string()).take(4).collect(),
            Ok(Value::Object(_)) => return None,
            _ => vec![],
        };
        let level = pc.get("level")?.as_i64()?;
        let levelup = if level >= 20 {
            false
        } else if level < 1 {
            return None; // a negative list index: Python's
        } else {
            col_int(pc, "xp") >= XP_THRESHOLDS[level.min(19) as usize]
        };
        let hp = pc.get("current_hp").cloned().unwrap_or(Value::Null);
        out.push(json!({"id": pc.get("id"), "name": pc.get("name"), "native": true, "hp": hp, "max_hp": hp_max, "hp_label": "HP", "hp_id": null,
            "temp_hp": pc.get("temp_hp"), "shock": col_int(pc, "shock_current"), "shock_max": shock, "ac": pc.get("armor_class"), "level": level,
            "system": "", "down": hp_max > 0 && hp.as_i64().unwrap_or(0) <= 0, "levelup": levelup, "conditions": conds, "resources": []}));
    }
    Some(out)
}

/// `GET /player-cockpit`'s data: (parties with member vitals, the viewer's characters with vitals and XP, focus, view_as)
pub fn player_cockpit_data(db: &Connection, world_id: i64, user_id: i64, is_gm: bool, as_pc: i64) -> Option<(Vec<Value>, Vec<Value>, Option<i64>, bool)> {
    let rows = |sql: &str, params: &[i64]| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(rusqlite::params_from_iter(params.iter()), |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    // _viewer_pcs
    let mut owner = user_id;
    let mut mine: Option<Vec<Map<String, Value>>> = None;
    if as_pc != 0 && is_gm {
        if let Some(t) = rows("SELECT * FROM player_characters WHERE id = ?1", &[as_pc])?.into_iter().next() {
            if t.get("world_id").and_then(|v| v.as_i64()) == Some(world_id) {
                match t.get("owner_user_id").and_then(|v| v.as_i64()).filter(|o| *o != 0) {
                    None => mine = Some(vec![t]),
                    Some(o) => owner = o,
                }
            }
        }
    }
    let mine = match mine {
        Some(m) => m,
        None => rows("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY name", &[world_id, owner])?,
    };
    let my_ids: Vec<i64> = mine.iter().filter_map(|p| p.get("id").and_then(|v| v.as_i64())).collect();
    let mut parties = vec![];
    if !my_ids.is_empty() {
        for p in rows("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name", &[world_id])? {
            let ids = member_ids(p.get("member_pc_ids_json"))?;
            if !my_ids.iter().any(|i| ids.contains(i)) {
                continue;
            }
            let members = if ids.is_empty() { vec![] } else {
                let marks: Vec<String> = ids.iter().map(|i| i.to_string()).collect();
                rows(&format!("SELECT * FROM player_characters WHERE id IN ({})", marks.join(",")), &[])?
            };
            parties.push(json!({"id": p.get("id"), "name": p.get("name"), "members": member_vitals(db, &members)?}));
        }
    }
    let vitals = member_vitals(db, &mine)?;
    let my_pcs: Vec<Value> = vitals.iter().map(|v| json!({"id": v["id"], "name": v["name"]})).collect();
    let focus = (as_pc != 0 && my_pcs.iter().any(|m| m["id"].as_i64() == Some(as_pc))).then_some(as_pc);
    let view_as = match focus {
        Some(f) if is_gm => rows("SELECT * FROM player_characters WHERE id = ?1", &[f])?.first().is_some_and(|t| t.get("owner_user_id").and_then(|v| v.as_i64()) != Some(user_id)),
        _ => false,
    };
    Some((parties, my_pcs, focus, view_as))
}

// ── what the table sees of a schematic (app/schematic_payload.py player_scene, main.py _schematic_player_payload) ──

/// The player view of a schematic: everything the player view, view.json and the TV's data.json send
pub struct PlayerScene {
    pub visible: Vec<Value>,
    pub image_url: Option<String>,
    pub fog_part: Map<String, Value>,
    pub own_pc_id: Value,
    pub own_pc_ids: Vec<i64>,
    pub own_pc_currency: Value,
    pub active_combatant_id: Value,
    pub combat_round: Value,
}

/// `player_scene` + `_schematic_player_payload`; None for strict secrecy (what the party has seen is computed and recorded: Python's)
/// and for stored JSON Python reads its own way. `viewer` = (user id, is_gm); None = the TV (no own characters).
pub fn player_scene(db: &Connection, sc: &Map<String, Value>, viewer: Option<(i64, bool)>) -> Option<PlayerScene> {
    let raw = sc.get("elements_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    let Value::Array(elements) = canonical_json(raw)? else { return None };
    // player_visible
    let visible: Vec<Value> = elements.iter().filter(|el| {
        let Some(o) = el.as_object() else { return false };
        !py_truthy(o.get("hidden")) && (o.get("type").and_then(|t| t.as_str()) != Some("token") || o.get("visible_to_players").map_or(true, |v| py_truthy(Some(v))))
    }).cloned().collect();
    if elements.iter().any(|el| el.as_object().is_some_and(|o| o.get("type").is_some_and(|t| t.is_array() || t.is_object()))) {
        return None;
    }
    // fog_payload
    let fog = clean_fog(sc_load(sc, "fog_json", false)?.as_object()?)?;
    let strict = fog["enabled"] == json!(true) && fog["strict"] == json!(true);
    let darkness = fog["darkness"].as_f64()?;
    let active = fog["enabled"] == json!(true) || darkness > 0.0;
    let mut walls = vec![];
    if active {
        for w in sc_load(sc, "walls_json", true)?.as_array()? {
            let Some(w) = w.as_object() else { continue };
            if w.get("kind").and_then(|v| v.as_str()) == Some("secret") {
                let (id, pts) = (w.get("id").cloned().unwrap_or(Value::Null), w.get("pts").cloned().unwrap_or(Value::Null));
                walls.push(if w.get("state").and_then(|v| v.as_str()) == Some("open") {
                    json!({"id": id, "pts": pts, "kind": "door", "state": "open"})
                } else {
                    json!({"id": id, "pts": pts, "kind": "wall"})
                });
            } else {
                let mut m = Map::new();
                for k in ["id", "pts", "kind", "state"] {
                    if let Some(v) = w.get(k) {
                        m.insert(k.into(), v.clone());
                    }
                }
                walls.push(Value::Object(m));
            }
        }
    }
    let mut lights = vec![];
    if darkness > 0.0 {
        let stored = sc_load(sc, "lights_json", true)?;
        let (all, _) = clean_lights(stored.as_array()?, sc)?;
        for l in all.into_iter().filter(|l| l["on"] == json!(true)) {
            let mut m = Map::new();
            for k in ["id", "x", "y", "range", "color", "intensity"] {
                m.insert(k.into(), l[k].clone());
            }
            lights.push(Value::Object(m));
        }
    }
    let mut image_url = sc.get("image_url").and_then(|v| v.as_str()).map(str::to_string);
    let mut visible = visible;
    let mut explored = None;
    if strict {
        // strict secrecy (app/map_strict.py): only what the party has seen; the view records newly explored squares
        let (view, record) = crate::strict::compute(sc, &elements, &fog)?;
        if let Some(rec) = record {
            let _ = db.execute("UPDATE schematics SET explored_json = ?1 WHERE id = ?2", rusqlite::params![rec, sc.get("id")?.as_i64()?]);
        }
        visible = crate::strict::filter_elements(visible, &view);
        walls = crate::strict::filter_walls(walls, &view)?;
        lights = crate::strict::filter_lights(lights, &view);
        let runs = view.runs();
        if image_url.as_deref().is_some_and(|u| !u.is_empty()) {
            image_url = Some(crate::strict::masked_image_url(sc.get("slug")?.as_str()?, &runs["runs"]));
        }
        explored = Some(runs);
    }
    let mut fog_part = Map::new();
    fog_part.insert("fog".into(), Value::Object(fog));
    fog_part.insert("walls".into(), Value::Array(walls));
    fog_part.insert("lights".into(), Value::Array(lights));
    if let Some(e) = explored {
        fog_part.insert("explored".into(), e);
    }
    // the viewer's own characters (a player's)
    let (mut own_pc_id, mut own_pc_ids, mut own_pc_currency) = (Value::Null, vec![], json!([]));
    if let Some((uid, false)) = viewer {
        if !elements.iter().all(|e| e.is_object()) {
            return None; // _active_own_pc's e.get on a non-object: Python's error
        }
        let wid = sc.get("world_id")?.as_i64()?;
        let mut st = db.prepare("SELECT * FROM player_characters WHERE world_id = ?1 AND owner_user_id = ?2 ORDER BY id").ok()?;
        let own: Vec<Map<String, Value>> = st.query_map([wid, uid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<_, _>>().ok()?;
        own_pc_ids = own.iter().filter_map(|p| p.get("id").and_then(|v| v.as_i64())).collect();
        let on_map: Vec<i64> = elements.iter().filter_map(|e| e.as_object())
            .filter(|e| e.get("type").and_then(|t| t.as_str()) == Some("token") && py_truthy(e.get("pc_id")))
            .filter_map(|e| e.get("pc_id")?.as_i64()).collect();
        if let Some(pc) = own.iter().find(|p| p.get("id").and_then(|v| v.as_i64()).is_some_and(|i| on_map.contains(&i))).or(own.first()) {
            own_pc_id = pc.get("id")?.clone();
            let raw = pc.get("currency_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
            own_pc_currency = canonical_json(raw)?;
        }
    }
    // the linked fight's active combatant
    let (mut active_combatant_id, mut combat_round) = (Value::Null, Value::Null);
    if let Some(cs_id) = sc.get("combat_session_id").and_then(|v| v.as_i64()).filter(|i| *i != 0) {
        if let Ok(cs) = db.query_row("SELECT * FROM combat_sessions WHERE id = ?1", [cs_id], |r| crate::layout::row_map(r)) {
            combat_round = cs.get("round_num").cloned().unwrap_or(Value::Null);
            let f = Fight::load(cs)?;
            if let Some(i) = f.current()? {
                active_combatant_id = f.combatants[i].get("id").cloned().unwrap_or(Value::Null);
            }
        }
    }
    Some(PlayerScene { visible, image_url, fog_part, own_pc_id, own_pc_ids, own_pc_currency, active_combatant_id, combat_round })
}

pub fn safe_image(url: &str) -> String {
    safe_image_url(Some(&json!(url)))
}

/// `combat._candidates(world)`'s payloads: (characters, creatures) the fight pickers offer; a character on a custom system (its vital
/// track) makes it Python's
pub fn combat_candidates(db: &Connection, wid: i64) -> Option<(Vec<Value>, Vec<Value>)> {
    let rows = |sql: &str| -> Option<Vec<Map<String, Value>>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map([wid], |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r)
    };
    let mut pcs = vec![];
    for pc in rows("SELECT * FROM player_characters WHERE world_id = ?1 ORDER BY name")? {
        let (hp, shock, pp, mp, native) = pc_maxima(db, &pc)?;
        if !native {
            return None;
        }
        let conds: Vec<Value> = match serde_json::from_str::<Value>(pc.get("conditions_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
            Ok(Value::Array(a)) => a.into_iter().filter(|x| x.is_string()).collect(),
            _ => vec![],
        };
        let stats = stat_values(&pc)?;
        let get = |k: &str| stats.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
        pcs.push(json!({"id": pc.get("id"), "name": pc.get("name"), "max_hp": hp, "hp": col_int(&pc, "current_hp"), "max_shock": shock,
            "shock": col_int(&pc, "shock_current"), "conditions": conds, "portrait_url": pc.get("portrait_url").and_then(|v| v.as_str()).unwrap_or(""),
            "res": {"pp": {"cur": col_int(&pc, "pp_current"), "max": pp, "label": "PP"}, "mp": {"cur": col_int(&pc, "mp_current"), "max": mp, "label": "MP"}},
            "speed": get("dex") + get("itu")}));
    }
    let mut ents = vec![];
    for e in rows("SELECT * FROM entities WHERE world_id = ?1 AND kind IN ('character', 'creature') ORDER BY name")? {
        let pre = entity_combatant(&e)?;
        ents.push(json!({"id": e.get("id"), "name": e.get("name"), "kind": e.get("kind"), "max_hp": pre["max_hp"], "armor": pre["armor"],
            "speed": pre["init_bonus"], "image_url": e.get("image_url").and_then(|v| v.as_str()).unwrap_or("")}));
    }
    Some((pcs, ents))
}

/// The active combatant and round of a fight (`combat_turns.active_combatant_id`, `round_num`)
pub fn fight_now(db: &Connection, cs_id: i64) -> Option<Option<(Map<String, Value>, Value, Value)>> {
    let Ok(cs) = db.query_row("SELECT * FROM combat_sessions WHERE id = ?1", [cs_id], |r| crate::layout::row_map(r)) else { return Some(None) };
    let round = cs.get("round_num").cloned().unwrap_or(Value::Null);
    let f = Fight::load(cs.clone())?;
    let active = match f.current()? {
        Some(i) => f.combatants[i].get("id").cloned().unwrap_or(Value::Null),
        None => Value::Null,
    };
    Some(Some((cs, active, round)))
}

/// `combat._full_state(cs)`: everything the GM's tracker holds (None for stored JSON Python reads its own way)
pub fn combat_full_state(cs: &Map<String, Value>) -> Option<Value> {
    let log = match serde_json::from_str::<Value>(cs.get("log_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]")) {
        Ok(Value::Array(a)) => Value::Array(a),
        Ok(_) => json!([]),
        Err(_) => return None, // a NaN serde cannot read, or the [] Python falls back to: not worth telling apart
    };
    let f = Fight::load(cs.clone())?;
    let active = match f.current()? {
        Some(i) => f.combatants[i].get("id").cloned().unwrap_or(Value::Null),
        None => Value::Null,
    };
    let or = |k: &str, d: i64| cs.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(d);
    Some(json!({"combatants": f.combatants, "round_num": or("round_num", 1), "active_idx": or("active_idx", 0), "active_id": active,
        "rev": or("rev", 0), "name": cs.get("name"), "ruleset": cs.get("ruleset").and_then(|v| v.as_str()).unwrap_or(""),
        "turn": Value::Object(f.turn.clone()), "log": log}))
}

/// `combat._player_state(cs)` (`combat_state.player_view`): what a player may know of a fight - enemies as "Enemy A" with a word for
/// their health, a GM-hidden combatant not mentioned (None for values Python reads its own way)
pub fn combat_player_state(cs: &Map<String, Value>) -> Option<Value> {
    let f = Fight::load(cs.clone())?;
    let order: Vec<usize> = if f.fluid { (0..f.combatants.len()).collect() } else { f.order()? };
    let active_id = match f.current()? {
        Some(i) => f.combatants[i].get("id").cloned().unwrap_or(Value::Null),
        None => Value::Null,
    };
    let acted: Vec<String> = if f.fluid { f.acted() } else { vec![] };
    let picker = if f.fluid { f.turn["picker"].clone() } else { Value::Null };
    let letter = |n: usize| -> String {
        let (mut s, mut n) = (String::new(), n + 1);
        while n > 0 {
            let r = (n - 1) % 26;
            n = (n - 1) / 26;
            s.insert(0, (b'A' + r as u8) as char);
        }
        s
    };
    let (mut rows, mut enemy_n, mut seen_pick, mut shown_active) = (vec![], 0usize, Value::from(""), Value::Null);
    for &i in &order {
        let c = f.combatants[i].as_object()?;
        let id = c.get("id").cloned().unwrap_or(Value::Null);
        if id.is_array() || id.is_object() {
            return None; // `in acted` of an unhashable id: Python's error
        }
        let is_enemy = c.get("source").and_then(|v| v.as_str()) != Some("pc");
        let label = is_enemy.then(|| {
            enemy_n += 1;
            letter(enemy_n - 1)
        });
        let visible = c.get("visible").map_or(true, |v| py_truthy(Some(v)));
        if id == picker && visible && !is_enemy {
            seen_pick = match c.get("name") {
                Some(v) if py_truthy(Some(v)) => v.clone(),
                _ => json!(""),
            };
        }
        if !active_id.is_null() && id == active_id && visible {
            shown_active = active_id.clone();
        }
        if !visible {
            continue;
        }
        let (max_hp, hp) = (cs_int(c.get("max_hp"), 0, 0, i64::MAX).ok()?, cs_int(c.get("hp"), 0, 0, i64::MAX).ok()?);
        let state = if c.get("status").and_then(|v| v.as_str()) == Some("dead") || (max_hp > 0 && hp <= 0) {
            "Down"
        } else if max_hp <= 0 || hp >= max_hp {
            "Unhurt"
        } else if hp * 2 <= max_hp {
            "Bloodied"
        } else {
            "Hurt"
        };
        let is_acted = id.as_str().is_some_and(|s| acted.iter().any(|a| a == s));
        let mut row = Map::new();
        row.insert("id".into(), id.clone());
        row.insert("active".into(), json!(id == active_id));
        row.insert("acted".into(), json!(is_acted));
        row.insert("state".into(), json!(state));
        if is_enemy {
            row.insert("kind".into(), json!("enemy"));
            let name = if py_truthy(c.get("name_known")) { c.get("name").cloned().unwrap_or(Value::Null) } else { json!(format!("Enemy {}", label?)) };
            row.insert("name".into(), name);
        } else {
            row.insert("kind".into(), json!("pc"));
            row.insert("name".into(), c.get("name").cloned().unwrap_or(Value::Null));
            for (k, out) in [("hp", "hp"), ("max_hp", "max_hp"), ("shock", "shock"), ("max_shock", "max_shock")] {
                row.insert(out.into(), c.get(k).cloned().unwrap_or(json!(0)));
            }
            let conds = match c.get("conditions") {
                Some(Value::Array(a)) => Value::Array(a.clone()),
                v if !py_truthy(v) => json!([]),
                _ => return None, // list() of a string or an object: Python's
            };
            row.insert("conditions".into(), conds);
        }
        rows.push(Value::Object(row));
    }
    let or = |k: &str, d: i64| cs.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).unwrap_or(d);
    let combatants: Vec<Value> = order.iter().map(|&i| {
        let c = &f.combatants[i];
        json!({"id": c.get("id").cloned().unwrap_or(Value::Null), "initiative": c.get("initiative").cloned().unwrap_or(json!(0))})
    }).collect();
    Some(json!({"rows": rows, "active_id": shown_active, "picker": seen_pick, "round_num": or("round_num", 1), "rev": or("rev", 0),
        "name": cs.get("name"), "ruleset": cs.get("ruleset").and_then(|v| v.as_str()).unwrap_or(""), "fluid": f.fluid,
        "combatants": combatants, "active_idx": or("active_idx", 0)}))
}

// ── the character sheet page, N&D (app/routers/characters.py character_sheet, _derived, _roll_cfg, character_hub.journey_context) ──

/// Python's `float(v)`: Ok(Some(f)) when it reads, Ok(None) for its TypeError / ValueError, Err where it might read text its own way
fn py_float(v: Option<&Value>) -> Result<Option<f64>, ()> {
    Ok(match v {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => None,
        Some(Value::Bool(b)) => Some(*b as i64 as f64),
        Some(Value::Number(n)) => Some(n.as_f64().ok_or(())?),
        Some(Value::String(t)) => {
            if !t.is_ascii() || t.contains('_') {
                return Err(());
            }
            let w = t.trim_matches(|ch: char| matches!(ch, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c' | '\x1c'..='\x1f'));
            w.parse::<f64>().ok()
        }
    })
}

/// `int(_num(v, 0))` (an infinity or NaN is Python's error)
fn num_int(v: Option<&Value>) -> Option<i64> {
    match py_float(v).ok()? {
        None => Some(0),
        Some(f) if f.is_finite() && f.abs() < 9e15 => Some(f.trunc() as i64),
        Some(_) => None,
    }
}

/// A JSON list column read by `json.loads(col or "[]")` that must be a list (anything else: Python's)
fn list_col(pc: &Map<String, Value>, col: &str) -> Option<Vec<Value>> {
    let raw = pc.get(col).and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    match canonical_json(raw)? {
        Value::Array(a) => Some(a),
        _ => None,
    }
}

/// The context of `characters/sheet.html` for an N&D character without a sheet template, viewed by someone who may see it; None for
/// anything Python's own page would read its own way
pub fn character_sheet_ctx(db: &Connection, p: &crate::layout::PageCtx, pc: &Map<String, Value>) -> Option<Map<String, Value>> {
    if pc.get("sheet_template_id").is_some_and(|v| !v.is_null()) {
        return None;
    }
    let pid = pc.get("id")?.as_i64()?;
    let wid = pc.get("world_id")?.as_i64()?;
    let owner = pc.get("owner_user_id").and_then(|v| v.as_i64());
    let can_manage = p.is_gm || owner == Some(p.user_id);
    if !can_manage {
        // _can_view_character: a member of the character's world, the GM letting players see the party, an owned character
        let see: Option<i64> = db.query_row("SELECT players_see_party FROM worlds WHERE id = ?1", [wid], |r| r.get(0)).ok()?;
        if see.unwrap_or(0) == 0 || owner.is_none() {
            return None;
        }
    }
    // _derived
    let stats = list_col(pc, "stats_json")?;
    let currency = list_col(pc, "currency_json")?;
    let equipment = list_col(pc, "equipment_json")?;
    let feats = list_col(pc, "feats_json")?;
    let attacks = list_col(pc, "attacks_json")?;
    let cyberware = list_col(pc, "cyberware_json")?;
    let conditions = list_col(pc, "conditions_json")?;
    let level = pc.get("level")?.as_i64()?;
    let xp = pc.get("xp")?.as_i64()?;
    if level < 1 {
        return None; // XP_THRESHOLDS[level - 1] with a negative index: Python's
    }
    let lvl = level.min(20);
    let xp_lo = XP_THRESHOLDS[(lvl - 1) as usize];
    let xp_hi = (lvl < 20).then(|| XP_THRESHOLDS[lvl as usize]);
    let xp_pct = match xp_hi {
        Some(hi) if hi > xp_lo => (100.0f64).min(((xp - xp_lo).max(0) as f64 * 100.0 / (hi - xp_lo) as f64).trunc()) as i64,
        _ => 100,
    };
    // total_weight: sum(_num(weight, 0) * _num(qty, 1)) - an int while nothing reads as a float
    let (mut tw, mut tw_float) = (0.0f64, false);
    for it in equipment.iter().filter_map(|i| i.as_object()) {
        let (w, q) = (py_float(it.get("weight")).ok()?, py_float(it.get("qty")).ok()?);
        tw += w.unwrap_or(0.0) * q.unwrap_or(1.0);
        tw_float |= w.is_some() || q.is_some();
    }
    let mut stat_val: Vec<(String, i64)> = vec![];
    for s in stats.iter().filter_map(|s| s.as_object()) {
        let id = match s.get("id") {
            Some(Value::String(t)) if !t.is_empty() => t.clone(),
            v if !py_truthy(v) => continue,
            Some(Value::Array(_)) | Some(Value::Object(_)) => return None,
            _ => continue, // a number id is never "str" / "dex" ...
        };
        let v = num_int(s.get("value"))?;
        stat_val.retain(|(k, _)| *k != id);
        stat_val.push((id, v));
    }
    let sv = |k: &str| stat_val.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v);
    let phys = sv("str") + sv("dex") + sv("bod") + sv("per");
    let ment = sv("wil") + sv("int") + sv("cha") + sv("itu");
    let max_hp = pc.get("max_hp")?.as_i64()?;
    let hp_max = if max_hp > 0 { max_hp } else { phys + 10 };
    let shock_stored = col_int(pc, "shock_max");
    let shock_max = if shock_stored > 0 { shock_stored } else { ment };
    let mut ca_used = 0.0f64;
    for c in cyberware.iter().filter_map(|c| c.as_object()) {
        ca_used += py_float(c.get("ca_cost")).ok()?.unwrap_or(0.0);
    }
    if !ca_used.is_finite() {
        return None;
    }
    let text = |k: &str| pc.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    let mut ctx = Map::new();
    ctx.insert("pc".into(), Value::Object(pc.clone()));
    ctx.insert("stats".into(), Value::Array(stats.clone()));
    ctx.insert("currency".into(), Value::Array(currency));
    ctx.insert("secondary".into(), json!({"name": "Shock", "max": shock_max, "current": col_int(pc, "shock_current")}));
    ctx.insert("xp_lo".into(), json!(xp_lo));
    ctx.insert("xp_hi".into(), json!(xp_hi));
    ctx.insert("xp_pct".into(), json!(xp_pct));
    ctx.insert("equipment".into(), Value::Array(equipment.clone()));
    ctx.insert("feats".into(), Value::Array(feats));
    ctx.insert("attacks".into(), Value::Array(attacks));
    ctx.insert("total_weight".into(), if tw_float { json!(tw) } else { json!(0) });
    ctx.insert("cyberware".into(), Value::Array(cyberware));
    ctx.insert("ca_used".into(), json!(ca_used.trunc() as i64));
    ctx.insert("conditions".into(), Value::Array(conditions));
    ctx.insert("phys".into(), json!(phys));
    ctx.insert("ment".into(), json!(ment));
    ctx.insert("hp_max_derived".into(), json!(phys + 10));
    ctx.insert("hp_max".into(), json!(hp_max));
    ctx.insert("shock_max_derived".into(), json!(ment));
    ctx.insert("ca_derived".into(), json!(sv("wil") + sv("bod")));
    ctx.insert("speed_derived".into(), json!(sv("dex") + sv("itu")));
    ctx.insert("shock_max".into(), json!(shock_max));
    ctx.insert("shock_current".into(), json!(col_int(pc, "shock_current")));
    ctx.insert("pp_current".into(), json!(col_int(pc, "pp_current")));
    ctx.insert("mp_current".into(), json!(col_int(pc, "mp_current")));
    ctx.insert("minor_edge".into(), json!(text("minor_edge")));
    ctx.insert("major_edge".into(), json!(text("major_edge")));
    ctx.insert("chosen_template".into(), Value::Null);
    ctx.insert("tpl_fields".into(), json!([]));
    let cf_raw = pc.get("custom_fields_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("{}");
    ctx.insert("custom_fields".into(), canonical_json(cf_raw)?);
    ctx.insert("condition_presets".into(), json!(["Burn", "Freeze", "Toxin", "Bleeding", "Blind", "Yellow", "Charm", "Daze", "Stunned"]));
    ctx.insert("can_manage".into(), json!(can_manage));
    let rows = |sql: &str, params: &[i64]| -> Option<Vec<Value>> {
        let mut st = db.prepare(sql).ok()?;
        let r = st.query_map(rusqlite::params_from_iter(params.iter()), |r| crate::layout::row_map(r)).ok()?.collect::<Result<Vec<_>, _>>().ok()?;
        Some(r.into_iter().map(Value::Object).collect())
    };
    let members = if p.is_gm {
        rows("SELECT u.* FROM users u JOIN world_memberships m ON m.user_id = u.id WHERE m.world_id = ?1 AND u.is_gm = 0 ORDER BY u.display_name", &[wid])?
    } else {
        vec![]
    };
    ctx.insert("world_members".into(), Value::Array(members));
    let data = nd_render::app::data();
    ctx.insert("equipment_catalog".into(), data.get("catalog_equipment")?.clone());
    ctx.insert("feats_catalog".into(), data.get("catalog_feats")?.clone());
    ctx.insert("linked_sheets".into(), Value::Array(rows("SELECT * FROM character_sheets WHERE player_character_id = ?1", &[pid])?));
    ctx.insert("levelup_ready".into(), json!(level < 20 && xp >= XP_THRESHOLDS[level.min(19) as usize]));
    ctx.insert("carry".into(), carry_info(db, pc, &equipment)?);
    // _roll_cfg (the owner's or the GM's roll sheet: Stat + d10)
    let roll = if can_manage {
        let names = [("str", "Strength"), ("dex", "Dexterity"), ("bod", "Body"), ("per", "Perception"), ("wil", "Willpower"), ("int", "Intellect"), ("cha", "Charisma"), ("itu", "Intuition")];
        let mut by_id: Vec<(String, i64)> = vec![];
        for x in stats.iter().filter_map(|x| x.as_object()) {
            let id = match x.get("id") {
                Some(Value::String(t)) => t.clone(),
                None | Some(Value::Null) => "None".into(),
                _ => return None,
            };
            let v = num_int(x.get("value"))?;
            by_id.retain(|(k, _)| *k != id);
            by_id.push((id, v));
        }
        let list: Vec<Value> = names.iter().map(|(k, label)| json!({"id": k, "label": label,
            "value": by_id.iter().find(|(id, _)| id == k).map_or(0, |(_, v)| *v), "pool": if ["str", "dex", "bod", "per"].contains(k) { "pp" } else { "mp" }})).collect();
        json!({"mode": "stat", "pc_id": pid, "pc_name": pc.get("name"), "stats": list})
    } else {
        Value::Null
    };
    ctx.insert("roll_cfg".into(), roll);
    // the player hub (the owner's, or a GM looking in)
    let hub_mode = if owner == Some(p.user_id) { Some("owner") } else if p.is_gm { Some("gm") } else { None };
    ctx.insert("hub_enabled".into(), json!(hub_mode.is_some()));
    ctx.insert("hub_mode".into(), json!(hub_mode));
    let owner_name = match (hub_mode, owner) {
        (Some("gm"), Some(o)) => db.query_row("SELECT display_name, email FROM users WHERE id = ?1", [o], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?)))
            .ok().map(|(n, e)| json!(n.filter(|t| !t.is_empty()).or(e))).unwrap_or(Value::Null),
        _ => Value::Null,
    };
    ctx.insert("hub_owner_name".into(), owner_name);
    let (mut hub_party, mut hub_loot, mut hub_roster, mut hub_ledger, mut hub_sessions) = (Value::Null, vec![], vec![], vec![], vec![]);
    if hub_mode.is_some() {
        // journey_context: the first party (by name) that lists the character
        let parties = rows("SELECT * FROM parties WHERE world_id = ?1 ORDER BY name", &[wid])?;
        let mut first = None;
        for party in parties {
            let ids = member_ids(party.get("member_pc_ids_json"))?;
            if ids.contains(&pid) {
                first = Some((party, ids));
                break;
            }
        }
        if let Some((party, ids)) = first {
            let po = party.as_object()?;
            let members: Vec<Map<String, Value>> = if ids.is_empty() { vec![] } else {
                let marks: Vec<String> = ids.iter().map(|i| i.to_string()).collect();
                rows(&format!("SELECT * FROM player_characters WHERE id IN ({}) AND world_id = ?1", marks.join(",")), &[wid])?.into_iter().filter_map(|v| match v { Value::Object(o) => Some(o), _ => None }).collect()
            };
            let name_of = |id: i64| members.iter().find(|m| m.get("id").and_then(|v| v.as_i64()) == Some(id)).and_then(|m| m.get("name").cloned());
            for it in load_loot(po)? {
                let cl: Vec<i64> = it["claimed_by"].as_array()?.iter().filter_map(|x| x.as_i64()).collect();
                hub_loot.push(json!({"lid": it["lid"], "name": it["name"], "qty": it["qty"], "notes": it["notes"], "claimed": !cl.is_empty(), "mine": cl.contains(&pid),
                    "claimers": cl.iter().filter_map(|c| name_of(*c)).collect::<Vec<_>>()}));
            }
            let others: Vec<Map<String, Value>> = members.iter().filter(|m| m.get("id").and_then(|v| v.as_i64()) != Some(pid)).cloned().collect();
            for v in member_vitals(db, &others)? {
                let by = others.iter().find(|m| m.get("id") == v.get("id"))?;
                let line: Vec<String> = ["race", "char_class"].iter().filter_map(|k| by.get(*k).and_then(|x| x.as_str()).filter(|t| !t.is_empty()).map(str::to_string)).collect();
                hub_roster.push(json!({"id": v["id"], "name": v["name"], "native": v["native"], "level": v["level"], "line": line.join(" · "),
                    "hp": if py_truthy(Some(&v["hp"])) { v["hp"].clone() } else { json!(0) }, "max_hp": v["max_hp"], "hp_label": v["hp_label"],
                    "down": v["down"], "conditions": v["conditions"]}));
            }
            let raw = po.get("xp_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
            match serde_json::from_str::<Value>(raw) {
                Ok(Value::Array(a)) => hub_ledger = a.into_iter().rev().take(5).collect(),
                Ok(_) => return None,
                Err(_) => {}
            }
            let sees = p.is_gm || match p.world.as_ref() {
                Some(w) => nd_perm::sections::section_level(false, p.role.as_deref(), w.get("section_access_json").and_then(|v| v.as_str()), w.get("custom_kinds_json").and_then(|v| v.as_str()), "session_log")? != "none",
                None => false,
            };
            if sees {
                hub_sessions = rows("SELECT * FROM game_sessions WHERE party_id = ?1 ORDER BY session_num DESC LIMIT 5", &[po.get("id")?.as_i64()?])?;
            }
            hub_party = party;
        }
    }
    ctx.insert("hub_party".into(), hub_party);
    ctx.insert("hub_loot".into(), Value::Array(hub_loot));
    ctx.insert("hub_roster".into(), Value::Array(hub_roster));
    ctx.insert("hub_xp_ledger".into(), Value::Array(hub_ledger));
    ctx.insert("hub_sessions".into(), Value::Array(hub_sessions));
    Some(ctx)
}

/// `schematic_payload.strict_view`: the View when strict secrecy is on (newly explored squares recorded), Some(None) when it is off
pub fn strict_view(db: &Connection, sc: &Map<String, Value>) -> Option<Option<crate::strict::View>> {
    let fog = clean_fog(sc_load(sc, "fog_json", false)?.as_object()?)?;
    if !(fog["enabled"] == json!(true) && fog["strict"] == json!(true)) {
        return Some(None);
    }
    let raw = sc.get("elements_json").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("[]");
    let elements = match canonical_json(raw)? {
        Value::Array(a) => a,
        _ => vec![],
    };
    let (view, record) = crate::strict::compute(sc, &elements, &fog)?;
    if let Some(rec) = record {
        let _ = db.execute("UPDATE schematics SET explored_json = ?1 WHERE id = ?2", rusqlite::params![rec, sc.get("id")?.as_i64()?]);
    }
    Some(Some(view))
}
