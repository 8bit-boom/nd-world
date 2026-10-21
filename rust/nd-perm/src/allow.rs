//! app/main.py `_is_player_safe`, `_is_assistant_safe`, `_is_owner_safe` and the decision `auth_gate` makes with them. Kept in the same
//! order and shape as the Python so a change there is easy to mirror here; the differential test catches what is missed.
use crate::{m, py_re};
use fancy_regex::Regex;
use std::sync::OnceLock;

macro_rules! re {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| py_re($p))
    }};
}

pub fn is_player_safe(method: &str, path: &str) -> bool {
    let sw = |p: &str| path.starts_with(p);
    if sw("/characters/templates") {
        return false;
    }
    if sw("/characters") || sw("/api/characters/") {
        return true;
    }
    if path == "/api/me" || path == "/api/ai/tasks" || sw("/api/ai/tasks/") || path == "/api/hover-preview/config" || path == "/api/spotlight" {
        return true;
    }
    if sw("/map-tiles/") || path == "/player-cockpit" || path == "/api/cockpit/player-board" || path == "/api/live" {
        return true;
    }
    if m(re!(r"^/api/parties/\d+/vitals$"), path) || path == "/api/entities/picker" || m(re!(r"^/api/entity/\d+/preview$"), path) {
        return true;
    }
    if path == "/account" || sw("/account/") {
        return true;
    }
    if path == "/api/view-prefs" || m(re!(r"^/api/view-prefs/(preset/)?[a-z_]{1,32}$"), path) {
        return true;
    }
    if m(re!(r"^/api/saved-views/[a-z_]{1,32}(/\d+)?$"), path)
        || m(re!(r"^/api/worlds/\d+/characters/sync$"), path)
        || m(re!(r"^/api/worlds/\d+/content-pack$"), path)
        || m(re!(r"^/api/maps/schematic/[^/]+/move-token$"), path)
        || m(re!(r"^/api/maps/schematic/[^/]+/pickup-item$"), path)
        || m(re!(r"^/api/maps/schematic/[^/]+/buy-item$"), path)
    {
        return true;
    }
    if path == "/api/chronicler/ask" {
        return true;
    }
    if matches!(path, "/api/ai/stream" | "/api/ai/chat/compact" | "/api/ai/world-context-player" | "/api/ai/entity-context") {
        return true;
    }
    if path == "/npc-talk" && method == "GET" {
        return true;
    }
    if m(re!(r"^/api/npc-talk/\d+/history$"), path) && matches!(method, "GET" | "DELETE") {
        return true;
    }
    if m(re!(r"^/api/npc-talk/\d+/export\.md$"), path) && method == "GET" {
        return true;
    }
    if (m(re!(r"^/api/npc-talk/\d+/stream$"), path) || m(re!(r"^/api/npc-talk/\d+/speak$"), path)) && method == "POST" {
        return true;
    }
    if matches!(
        path,
        "/api/ai/attachments/upload"
            | "/api/ai/attachments/upload/chunk"
            | "/api/ai/attachments/upload/complete"
            | "/api/ai/attachments/audio-jobs"
            | "/api/ai/attachments/audio-jobs/chunk"
            | "/api/ai/attachments/audio-jobs/complete"
    ) {
        return true;
    }
    if m(re!(r"^/api/ai/attachments/audio-jobs/\d+$"), path) || m(re!(r"^/api/session-log/\d+/recap$"), path) {
        return true;
    }
    if path == "/pages/sheets/new" || m(re!(r"^/pages/sheets/\d+/(save|edit|delete)$"), path) {
        return true;
    }
    if matches!(path, "/dice" | "/api/dice/roll" | "/api/dice/record" | "/api/dice/history") {
        return true;
    }
    if method == "GET" && (path == "/api/ai/world-summary" || path == "/api/ai/models") {
        return true;
    }
    if path == "/api/ai/imagegen/player/generate" && method == "POST" {
        return true;
    }
    if m(re!(r"^/api/ai/imagegen/player/jobs(/\d+(/cancel)?)?$"), path) && matches!(method, "GET" | "POST" | "DELETE") {
        return true;
    }
    if method == "POST" && m(re!(r"^/api/tables/\d+/roll$"), path) {
        return true;
    }
    if path == "/quests/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if method == "POST" && (m(re!(r"^/quests/\d+/(edit|delete)$"), path) || m(re!(r"^/api/quests/\d+/status$"), path)) {
        return true;
    }
    if path == "/tables/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if method == "GET" && m(re!(r"^/tables/\d+/edit$"), path) {
        return true;
    }
    if method == "POST" && m(re!(r"^/tables/\d+/(edit|delete)$"), path) {
        return true;
    }
    if method == "POST" && path == "/api/calendar/events" {
        return true;
    }
    if path == "/schedule" || path == "/schedule/confirmed.ics" || m(re!(r"^/schedule/plans/\d+\.ics$"), path) {
        return method == "GET";
    }
    if sw("/api/schedule/") {
        return true;
    }
    if method == "GET" && path == "/api/calendar/search" {
        return true;
    }
    if method == "POST"
        && (m(re!(r"^/api/calendar/events/\d+/delete$"), path) || m(re!(r"^/parties/\d+/edit$"), path) || m(re!(r"^/api/parties/\d+/loot$"), path))
    {
        return true;
    }
    if method != "GET" {
        return false;
    }
    if path == "/boards" || m(re!(r"^/boards/(?!new$)[^/]+(/notes)?$"), path) {
        return true;
    }
    if path == "/combat" || m(re!(r"^/combat/\d+$"), path) || m(re!(r"^/api/combat/\d+/state$"), path) {
        return true;
    }
    if path == "/sessions" || m(re!(r"^/sessions/\d+(/summary\.md|/transcript\.md)?$"), path) {
        return true;
    }
    if matches!(
        path,
        "/facts" | "/facts/fragment" | "/images" | "/import" | "/tools/bulk-edit" | "/background-jobs" | "/export" | "/dreamlands" | "/king-in-yellow"
            | "/ai" | "/imagestudio" | "/editor" | "/tools/code-assist"
    ) {
        return true;
    }
    if matches!(
        path,
        "/" | "/rules" | "/rules/download.md" | "/search" | "/maps" | "/races" | "/professions" | "/androidapp" | "/chronicler" | "/session-log"
            | "/audio" | "/video" | "/pages" | "/ai-chat" | "/image-gen"
    ) {
        return true;
    }
    if matches!(path, "/calendar" | "/calendar/agenda" | "/calendar/year" | "/calendar/event-jump" | "/quests" | "/parties" | "/tables")
        || m(re!(r"^/(quests|parties)/\d+$"), path)
        || m(re!(r"^/parties/\d+/(summary|roster)$"), path)
    {
        return true;
    }
    if sw("/kind/") || sw("/uploads/") {
        return true;
    }
    if m(re!(r"^/audio/albums/\d+$"), path) || m(re!(r"^/video/albums/\d+$"), path) || m(re!(r"^/pages/albums/\d+$"), path) || m(re!(r"^/pages/\d+(/download)?$"), path) {
        return true;
    }
    if path == "/pages/sheets" || m(re!(r"^/pages/sheets/\d+(/render|/download)?$"), path) {
        return true;
    }
    if m(re!(r"^/entity/\d+(/download\.md)?$"), path) || m(re!(r"^/session-log/\d+$"), path) {
        return true;
    }
    if sw("/maps/") && !(path == "/maps/schematic" || sw("/maps/schematic/")) && path != "/maps/new" {
        return true;
    }
    if m(re!(r"^/maps/schematic/[^/]+/(view(\.json)?|bg\.webp)$"), path) {
        return true;
    }
    if sw("/worlds/switch/") || m(re!(r"^/worlds/\d+/notes/\d+$"), path) {
        return true;
    }
    false
}

pub fn is_assistant_safe(method: &str, path: &str) -> bool {
    let sw = |p: &str| path.starts_with(p);
    let post = method == "POST";
    if sw("/entity/") && (post || m(re!(r"^/entity/\d+/edit$"), path)) {
        return true;
    }
    if path == "/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if post && (path == "/api/upload-image" || path == "/api/upload-media") {
        return true;
    }
    if post && (m(re!(r"^/api/entity/\d+/image$"), path) || m(re!(r"^/api/entity/\d+/(rename|field)$"), path) || m(re!(r"^/kind/[^/]+/bulk-delete$"), path)) {
        return true;
    }
    if post && (path == "/api/entities/bulk-folder" || path == "/folders/rename") {
        return true;
    }
    if path == "/sessions" || sw("/sessions/") || sw("/api/sessions/") {
        return true;
    }
    if path == "/quests/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if post && (m(re!(r"^/quests/\d+/(edit|delete)$"), path) || m(re!(r"^/api/quests/\d+/status$"), path)) {
        return true;
    }
    if path == "/parties/new" && post {
        return true;
    }
    if post && (m(re!(r"^/parties/\d+/(edit|delete)$"), path) || m(re!(r"^/api/parties/\d+/(loot|location|launch-combat|members/toggle|rest|rest/undo)$"), path)) {
        return true;
    }
    if path == "/facts" || sw("/facts/") || sw("/api/facts/") {
        return true;
    }
    if path == "/calendar" || sw("/calendar/") || sw("/api/calendar/") {
        return true;
    }
    if path == "/media-rename" || sw("/api/media-rename/") {
        return true;
    }
    if path == "/tables" || sw("/tables/") || sw("/api/tables/") {
        return true;
    }
    if path == "/boards" || sw("/boards/") || path == "/api/orgs/graph" {
        return true;
    }
    if path == "/maps/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if post && (m(re!(r"^/maps/[^/]+/(rename|delete|upload)$"), path) || m(re!(r"^/api/maps/[^/]+/overlay$"), path)) {
        return true;
    }
    if path == "/maps/schematic/new" && matches!(method, "GET" | "POST") {
        return true;
    }
    if method == "GET" && (m(re!(r"^/maps/schematic/[^/]+$"), path) || m(re!(r"^/maps/schematic/[^/]+/(walls|rooms)\.json$"), path)) {
        return true;
    }
    if post && sw("/maps/schematic/") {
        return true;
    }
    if path == "/api/maps/props" || sw("/api/maps/props/") {
        return true;
    }
    if path == "/images" || sw("/images/") || path == "/api/gallery/browse" {
        return true;
    }
    if post && (matches!(path, "/pages/upload" | "/pages/upload/chunk" | "/pages/upload/complete") || sw("/pages/albums/") || m(re!(r"^/pages/\d+/(edit|delete)$"), path)) {
        return true;
    }
    if post
        && (matches!(path, "/races/new" | "/races/add-builtin" | "/races/add-all-builtin")
            || m(re!(r"^/races/\d+/delete$"), path)
            || matches!(path, "/professions/new" | "/professions/add-builtin" | "/professions/add-all-builtin")
            || m(re!(r"^/professions/\d+/delete$"), path))
    {
        return true;
    }
    if post
        && (sw("/audio/albums/")
            || matches!(path, "/audio/upload" | "/audio/upload/chunk" | "/audio/upload/complete" | "/audio/now-playing/stop")
            || m(re!(r"^/audio/\d+/(edit|delete|transcribe|play-for-players)$"), path))
    {
        return true;
    }
    if path == "/api/audio/clips" && method == "GET" {
        return true;
    }
    if post && (sw("/video/albums/") || matches!(path, "/video/upload" | "/video/upload/chunk" | "/video/upload/complete") || m(re!(r"^/video/\d+/(edit|delete|transcribe)$"), path)) {
        return true;
    }
    if (path == "/background-jobs" || path == "/api/audio-jobs") && method == "GET" {
        return true;
    }
    if sw("/api/audio-jobs/") || path == "/api/live-jobs" || sw("/api/live-jobs/") {
        return true;
    }
    if path == "/import" && method == "GET" {
        return true;
    }
    if post && (path == "/api/import" || sw("/api/import/")) {
        return true;
    }
    if path == "/tools/bulk-edit" && method == "GET" {
        return true;
    }
    if post && matches!(path, "/api/bulk-edit/parse" | "/api/bulk-edit/preview" | "/api/bulk-edit/apply") {
        return true;
    }
    if post
        && matches!(
            path,
            "/api/ai/generate/entity-smart"
                | "/api/ai/generate/entity"
                | "/api/ai/generate/npc"
                | "/api/ai/generate/location"
                | "/api/ai/generate/quest"
                | "/api/ai/entity-from-text"
                | "/api/ai/entities-from-text-batch"
                | "/api/ai/character-from-images"
                | "/api/ai/entity-from-images"
                | "/api/ai/save-note"
                | "/api/ai/world-context-smart"
                | "/api/ai/assist"
                | "/api/ai/assist-job"
                | "/api/ai/auto-tag/start"
                | "/api/ai/world-summary"
        )
    {
        return true;
    }
    if path == "/api/ai/world-context" && method == "GET" {
        return true;
    }
    if method == "GET" && (path == "/api/ai/world-summary" || m(re!(r"^/api/ai/(assist-job|auto-tag)/\d+$"), path)) {
        return true;
    }
    if method == "DELETE" && path == "/api/ai/world-summary" {
        return true;
    }
    false
}

pub fn is_owner_safe(method: &str, path: &str, world_id: i64) -> bool {
    let prefix = format!("/worlds/{world_id}");
    let get = method == "GET";
    let post = method == "POST";
    let rest = match path.strip_prefix(&prefix) {
        Some(r) => r,
        None => return false,
    };
    // every rule below is the prefix plus one of these tails; `re.escape(prefix)` of "/worlds/<int>" escapes nothing that matters
    let tail_re = |p: &str| m(&py_re(&format!("^{p}$")), rest);
    if rest == "/edit" && (get || post) {
        return true;
    }
    if post && matches!(rest, "/theme/import" | "/theme/clear" | "/theme/preset" | "/invites/new") {
        return true;
    }
    if post && (tail_re(r"/invites/\d+/revoke") || tail_re(r"/members/\d+/(remove|role|reset-password|clear-2fa)")) {
        return true;
    }
    if get && tail_re(r"/notes/\d+") {
        return true;
    }
    if post && (tail_re(r"/notes/\d+/new") || tail_re(r"/notes/\d+/\d+/delete")) {
        return true;
    }
    if get && rest == "/export" {
        return true;
    }
    if post && rest == "/import" {
        return true;
    }
    if rest == "/rules/edit" && (get || post) {
        return true;
    }
    if post && matches!(rest, "/rules/import" | "/rules/overlay/suggest" | "/delete" | "/ai-instructions/import") {
        return true;
    }
    if post && tail_re(r"/ai-instructions/\d+/(toggle|toggle-players|delete)") {
        return true;
    }
    false
}

/// Who the request is from, as `auth_gate` knows it once the session is valid.
#[derive(Clone, Debug)]
pub struct Viewer {
    pub is_gm: bool,
    /// role in the ACTIVE world: "player" / "assistant" / "owner" / None (no membership)
    pub active_role: Option<String>,
    pub active_world_id: Option<i64>,
}

/// `auth_gate`'s allowlist decision for a signed-in user with a valid session: may this request reach its route at all?
pub fn gate_allows(v: &Viewer, method: &str, path: &str) -> bool {
    if v.is_gm || is_player_safe(method, path) {
        return true;
    }
    let is_assistant = matches!(v.active_role.as_deref(), Some("assistant") | Some("owner"));
    let is_owner = v.active_role.as_deref() == Some("owner");
    let assistant_ok = is_assistant && is_assistant_safe(method, path);
    let owner_ok = is_owner && v.active_world_id.map_or(false, |w| is_owner_safe(method, path, w));
    assistant_ok || owner_ok
}
