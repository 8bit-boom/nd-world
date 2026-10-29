//! Tiled map pictures (app/map_tiles.py): `GET /map-tiles/<key>/<z>/<x>_<y>.webp` and the background cutting of a picture into a pyramid of 256 px WebP
//! tiles laid out on the viewer's own grid (Leaflet CRS.Simple). <data dir>/map_tiles/<key>/<z>/<x>_<y>.webp plus meta.json, written last.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, Response};
use image::{imageops, DynamicImage, RgbaImage};
use serde_json::{json, Value};

use crate::config::Config;

const TILE: i64 = 256;
const MAX_NATIVE_ZOOM: i64 = 4;
const TILE_BYTES_PER_PIXEL: f64 = 10.0;

fn building() -> &'static Mutex<HashSet<String>> {
    static B: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    B.get_or_init(|| Mutex::new(HashSet::new()))
}

/// the one cut at a time (Python's pool has one or two workers; one is the safe floor)
fn cutting() -> &'static Mutex<()> {
    static C: OnceLock<Mutex<()>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(()))
}

fn root_dir(cfg: &Config) -> Option<PathBuf> {
    Some(cfg.db_path.parent()?.join("map_tiles"))
}

/// `_find(key)`: the finished pyramid (prebuilt in static/map_tiles first, then the data dir's)
pub fn find(cfg: &Config, key: &str) -> Option<PathBuf> {
    for base in [cfg.static_dir.join("map_tiles"), root_dir(cfg)?] {
        if base.join(key).join("meta.json").is_file() {
            return Some(base.join(key));
        }
    }
    None
}

/// `_key(slug, src)`: slug + the picture's size and the CRC of its first 64 KiB
pub fn key(slug: &str, src: &Path) -> Option<String> {
    let size = std::fs::metadata(src).ok()?.len();
    let mut head = vec![0u8; 65536];
    let mut f = std::fs::File::open(src).ok()?;
    let mut n = 0;
    loop {
        let k = std::io::Read::read(&mut f, &mut head[n..]).ok()?;
        if k == 0 {
            break;
        }
        n += k;
        if n == head.len() {
            break;
        }
    }
    let mut crc = flate2::Crc::new();
    crc.update(&head[..n]);
    Some(format!("{slug}-{size:x}{:08x}", crc.sum()))
}

/// `plan_levels`: (min zoom, native zoom)
fn plan_levels(img_w: i64, img_h: i64, map_w: i64, map_h: i64) -> (i64, i64) {
    let ratio = (img_w as f64 / map_w.max(1) as f64).max(img_h as f64 / map_h.max(1) as f64);
    let native = if ratio > 1.0 { (ratio.log2().ceil() as i64).clamp(0, MAX_NATIVE_ZOOM) } else { 0 };
    let big = map_w.max(map_h);
    let zmin = if big > TILE { -((big as f64 / TILE as f64).log2().ceil() as i64).max(0) } else { 0 };
    (zmin, native)
}

/// `tile_range`: Leaflet's inclusive x / y ranges at zoom z
fn tile_range(map_w: i64, map_h: i64, z: i64) -> ((i64, i64), (i64, i64)) {
    let s = 2f64.powi(z as i32);
    ((0, (map_w as f64 * s / TILE as f64).ceil() as i64 - 1), ((-(map_h as f64) * s / TILE as f64).floor() as i64, -1))
}

fn save_tile(dir: &Path, tx: i64, ty: i64, region: &RgbaImage, at: (i64, i64)) -> Option<()> {
    let mut tile = RgbaImage::new(TILE as u32, TILE as u32);
    imageops::replace(&mut tile, region, at.0, at.1);
    let bytes = webp::Encoder::from_rgba(tile.as_raw(), TILE as u32, TILE as u32).encode_simple(false, 82.0).ok()?;
    std::fs::write(dir.join(format!("{tx}_{ty}.webp")), &*bytes).ok()
}

/// One level's tiles; `region((x0, y0, x1, y1))` gives that part of the level (in level pixels)
fn cut(zdir: &Path, xr: (i64, i64), yr: (i64, i64), oy: i64, lw: i64, lh: i64, region: &dyn Fn((i64, i64, i64, i64)) -> RgbaImage) -> Option<i64> {
    let mut n = 0;
    for tx in xr.0..=xr.1 {
        for ty in yr.0..=yr.1 {
            let bx = (tx * TILE, ty * TILE + oy, tx * TILE + TILE, ty * TILE + oy + TILE);
            let inner = (bx.0.max(0), bx.1.max(0), bx.2.min(lw), bx.3.min(lh));
            if inner.2 <= inner.0 || inner.3 <= inner.1 {
                continue;
            }
            save_tile(zdir, tx, ty, &region(inner), ((-bx.0).max(0), (-bx.1).max(0)))?;
            n += 1;
        }
    }
    Some(n)
}

/// `build`: cut the pyramid now (blocking); the meta, or None when the picture cannot be read
pub fn build(cfg: &Config, slug: &str, src: &Path, map_w: i64, map_h: i64) -> Option<Value> {
    let k = key(slug, src)?;
    if let Some(found) = find(cfg, &k) {
        return serde_json::from_str(&std::fs::read_to_string(found.join("meta.json")).ok()?).ok();
    }
    let root = root_dir(cfg)?;
    let out = root.join(&k);
    let tmp = root.join(format!("{k}.part"));
    let _ = std::fs::remove_dir_all(&tmp);
    let img: DynamicImage = crate::imaging::readable(src)?;
    let mut prev: RgbaImage = img.to_rgba8();
    drop(img);
    let (zmin, native) = plan_levels(prev.width() as i64, prev.height() as i64, map_w, map_h);
    let mut n = 0;
    for z in (zmin..=native).rev() {
        let s = 2f64.powi(z as i32);
        let (lw, lh) = (((map_w as f64 * s).round_ties_even() as i64).max(1), ((map_h as f64 * s).round_ties_even() as i64).max(1));
        let (xr, yr) = tile_range(map_w, map_h, z);
        let zdir = tmp.join(z.to_string());
        std::fs::create_dir_all(&zdir).ok()?;
        if lw > prev.width() as i64 || lh > prev.height() as i64 {
            // the deepest level is larger than the picture: each tile is resampled straight from its own part of it
            let (fx, fy) = (prev.width() as f64 / lw as f64, prev.height() as f64 / lh as f64);
            let src_img = &prev;
            n += cut(&zdir, xr, yr, lh, lw, lh, &|b| {
                let (x0, y0) = (((b.0 as f64 * fx).floor() as u32).min(src_img.width() - 1), ((b.1 as f64 * fy).floor() as u32).min(src_img.height() - 1));
                let (x1, y1) = (((b.2 as f64 * fx).ceil() as u32).clamp(x0 + 1, src_img.width()), ((b.3 as f64 * fy).ceil() as u32).clamp(y0 + 1, src_img.height()));
                let part = imageops::crop_imm(src_img, x0, y0, x1 - x0, y1 - y0).to_image();
                imageops::resize(&part, (b.2 - b.0) as u32, (b.3 - b.1) as u32, imageops::FilterType::Lanczos3)
            })?;
            continue;
        }
        // every other level is made from the one above it (half the size), and the bigger one is let go
        if (lw, lh) != (prev.width() as i64, prev.height() as i64) {
            prev = imageops::resize(&prev, lw as u32, lh as u32, imageops::FilterType::Lanczos3);
        }
        let level = &prev;
        n += cut(&zdir, xr, yr, lh, lw, lh, &|b| imageops::crop_imm(level, b.0 as u32, b.1 as u32, (b.2 - b.0) as u32, (b.3 - b.1) as u32).to_image())?;
    }
    let meta = json!({"key": k, "w": map_w, "h": map_h, "zmin": zmin, "native": native, "tile": TILE, "tiles": n});
    std::fs::write(tmp.join("meta.json"), nd_render::py_dumps(&meta)).ok()?;
    let _ = std::fs::remove_dir_all(&out);
    std::fs::rename(&tmp, &out).ok()?;
    if let Ok(rd) = std::fs::read_dir(&root) {
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if name != k && !name.ends_with(".part") && name.rsplit_once('-').map(|(a, _)| a) == Some(slug) {
                let _ = std::fs::remove_dir_all(e.path());
            }
        }
    }
    Some(meta)
}

/// Whether cutting `pixels` stays within the memory budget (`workers.tiling_fits`)
fn fits(cfg: &Config, pixels: i64) -> bool {
    let Some(db) = crate::writes::open(cfg) else { return false };
    let (budget_gb, workers) = crate::settings::tile_budget(&db);
    budget_gb == 0.0 || pixels as f64 * TILE_BYTES_PER_PIXEL * workers as f64 <= budget_gb * 2f64.powi(30) * 0.5
}

/// What `meta_for` comes to: a finished pyramid, none yet (a cut queued, or the picture too big to cut: one picture is shown), or Python's call
pub enum Meta {
    Ready(Value),
    None,
    Decline,
}

/// `meta_for(slug, src, map_w, map_h)`: the finished pyramid's meta, else queue a cut in the background
pub fn meta_for(cfg: &Config, slug: &str, src: &Path, map_w: i64, map_h: i64) -> Meta {
    let Some(k) = key(slug, src) else { return Meta::None };
    if let Some(found) = find(cfg, &k) {
        return match std::fs::read_to_string(found.join("meta.json")).ok().and_then(|t| serde_json::from_str(&t).ok()) {
            Some(m) => Meta::Ready(m),
            None => Meta::Decline,
        };
    }
    // a picture this port cannot read exactly (AVIF, GIF, animated, 16-bit) is cut by Python
    if crate::imaging::readable(src).is_none() {
        return Meta::Decline;
    }
    let dims = image::ImageReader::open(src).ok().and_then(|r| r.with_guessed_format().ok()).and_then(|r| r.into_dimensions().ok());
    let Some((w, h)) = dims else { return Meta::Decline };
    if !fits(cfg, w as i64 * h as i64) {
        return Meta::None;
    }
    if !building().lock().map(|mut b| b.insert(k.clone())).unwrap_or(false) {
        return Meta::None;
    }
    let (cfg2, slug2, src2) = (cfg.clone(), slug.to_string(), src.to_path_buf());
    std::thread::spawn(move || {
        {
            let _one = cutting().lock();
            let _ = build(&cfg2, &slug2, &src2, map_w, map_h);
        }
        if let Ok(mut b) = building().lock() {
            b.remove(&k);
        }
    });
    Meta::None
}

/// `GET /map-tiles/<key>/<z>/<x>_<y>.webp` (`serve_map_tile`): one tile of a finished pyramid, or Python's 404
pub async fn serve_tile(cfg: &Config, method: &Method, uri: &axum::http::Uri, headers: &HeaderMap) -> Option<Response<Body>> {
    if method != Method::GET || [header::RANGE, header::IF_NONE_MATCH, header::IF_MODIFIED_SINCE, header::IF_MATCH, header::IF_UNMODIFIED_SINCE, header::IF_RANGE].iter().any(|h| headers.contains_key(h)) {
        return None;
    }
    let rest = uri.path().strip_prefix("/map-tiles/")?;
    let mut parts = rest.split('/');
    let (key, z, tile) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some() || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.') || key.starts_with('.') || key.ends_with(".part") {
        return None;
    }
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) && s.len() < 10;
    let signed = |s: &str| digits(s.strip_prefix('-').unwrap_or(s));
    if !signed(z) {
        return None;
    }
    let stem = tile.strip_suffix(".webp")?;
    let (x, y) = stem.split_once('_').or_else(|| stem.strip_prefix('-').and_then(|r| r.split_once('_')).map(|_| ("", "")))?;
    if x.is_empty() || !signed(x) || !signed(y) {
        return None;
    }
    let base = find(cfg, key)?;
    let path = base.join(z).join(tile);
    let extra = crate::files::Extra {
        content_type: Some("image/webp".into()),
        headers: vec![
            (header::CACHE_CONTROL, HeaderValue::from_static("private, max-age=31536000, immutable")),
            (HeaderName::from_static("x-nd-served-by"), HeaderValue::from_static("gateway")),
        ],
    };
    crate::files::send(method, headers, &path, extra).await
}
