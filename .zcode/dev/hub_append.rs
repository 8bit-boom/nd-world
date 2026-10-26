
/// `session_media.viewer_media(db, gs, is_gm)`: everything of the session the viewer may see (the player side, is_gm=false,
/// is what the keepsakes tab keeps from)
pub(crate) fn viewer_media(db: &Connection, gs: &Map<String, Value>, gm: bool) -> Option<Value> {
    let wid = int(gs, "world_id")?;
    let visible = |r: &Map<String, Value>| gm || int(r, "visible_to_players").unwrap_or(0) != 0;
    let mut audio = vec![];
    for cid in sm_clip_ids(gs)? {
        if let Some(c) = row(db, "SELECT * FROM audio_clips WHERE id = ?1", cid).filter(|c| int(c, "world_id") == Some(wid)) {
            if visible(&c) {
                audio.push(sm_audio_dict(&c));
            }
        }
    }
    let mut videos = vec![];
    let mut images = vec![];
    for it in sm_items(gs)? {
        match it.get("kind").and_then(|k| k.as_str()) {
            Some("video") => {
                let cid = it.get("clip_id")?.as_i64()?;
                if let Some(c) = row(db, "SELECT * FROM video_clips WHERE id = ?1", cid).filter(|c| int(c, "world_id") == Some(wid)) {
                    if visible(&c) {
                        videos.push(sm_video_dict(&c, it.get("uid")?.as_str()?));
                    }
                }
            }
            Some("image") if gm || it.get("visible") == Some(&json!(true)) => {
                images.push(json!({"uid": it.get("uid"), "url": it.get("url"), "caption": it.get("caption"),
                    "visible_to_players": it.get("visible") == Some(&json!(true)), "own": it.get("own") == Some(&json!(true))}));
            }
            _ => {}
        }
    }
    Some(json!({"audio": audio, "videos": videos, "images": images}))
}
