//! Reading a Universal VTT file (.dd2vtt / .uvtt / .df2vtt) into what a schematic needs - app/uvtt_import.py ported: a background picture, a square grid, walls
//! and doors, lights and a darkness level. The file is untrusted: finite numbers only, the picture decoded and re-encoded (libwebp here, Pillow there: the
//! bytes differ, the size does not), sizes and counts capped, unknown parts ignored with a warning. `Ok(None)` = something Python would print or read its own way.

use base64::Engine;
use serde_json::{json, Map, Value};

const MAX_FILE_BYTES: usize = 80 * 1024 * 1024;
const MAX_IMAGE_BYTES: usize = 60 * 1024 * 1024;
const MAX_PIXELS: u64 = 40_000_000;
const MAX_SIDE: f64 = 20000.0;
const MIN_SIDE: f64 = 100.0;
const MAX_POLYLINES: usize = 5000;
const MAX_POINTS: usize = 20000;

pub struct Parsed {
    pub image: image::DynamicImage,
    pub width: i64,
    pub height: i64,
    pub cell: f64,
    pub walls: Vec<Value>,
    pub warnings: Vec<String>,
    pub lights: Vec<Value>,
    pub darkness: f64,
    pub stats: Map<String, Value>,
}

/// Err = a `UvttError` (its message is shown to the GM); Ok(None) = Python's
pub type Outcome = Result<Option<Parsed>, String>;

fn num(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}

fn pt(p: Option<&Value>) -> Option<(f64, f64)> {
    let o = p?.as_object()?;
    Some((num(o.get("x"))?, num(o.get("y"))?))
}

fn rep(f: f64) -> String {
    nd_render::py_float_repr(f)
}

/// Douglas-Peucker; the end points are kept
fn simplify(points: &[(f64, f64)], tol: f64) -> Vec<(f64, f64)> {
    if points.len() <= 2 || tol <= 0.0 {
        return points.to_vec();
    }
    let mut keep = vec![false; points.len()];
    keep[0] = true;
    *keep.last_mut().unwrap() = true;
    let mut stack = vec![(0usize, points.len() - 1)];
    while let Some((lo, hi)) = stack.pop() {
        let (ax, ay) = points[lo];
        let (bx, by) = points[hi];
        let (dx, dy) = (bx - ax, by - ay);
        let l2 = dx * dx + dy * dy;
        let (mut worst, mut at) = (-1.0f64, 0usize);
        for i in lo + 1..hi {
            let (px, py) = points[i];
            let d = if l2 == 0.0 { (px - ax).hypot(py - ay) } else { ((px - ax) * dy - (py - ay) * dx).abs() / l2.sqrt() };
            if d > worst {
                worst = d;
                at = i;
            }
        }
        if worst > tol {
            keep[at] = true;
            stack.push((lo, at));
            stack.push((at, hi));
        }
    }
    points.iter().zip(keep).filter(|(_, k)| *k).map(|(p, _)| *p).collect()
}

fn hex8(s: &str) -> bool {
    s.len() == 8 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn parse_uvtt(raw: &[u8]) -> Outcome {
    if raw.is_empty() {
        return Err("The file is empty".into());
    }
    if raw.len() > MAX_FILE_BYTES {
        return Err("That file is too large".into());
    }
    if raw.windows(3).any(|w| w == b"NaN") || raw.windows(8).any(|w| w == b"Infinity") {
        return Ok(None); // Python reads these as numbers
    }
    let text = match std::str::from_utf8(raw.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(raw)) {
        Ok(t) => t,
        Err(_) => return Err("That is not a Universal VTT file (it is not JSON)".into()),
    };
    let d: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) if e.is_syntax() || e.is_eof() || e.is_data() => return Err("That is not a Universal VTT file (it is not JSON)".into()),
        Err(_) => return Ok(None),
    };
    let Some(d) = d.as_object() else { return Err("That is not a Universal VTT file (no resolution)".into()) };
    let Some(res) = d.get("resolution").and_then(|v| v.as_object()) else { return Err("That is not a Universal VTT file (no resolution)".into()) };
    let mut warnings: Vec<String> = vec![];
    if let Some(fmt) = num(d.get("format")) {
        if fmt > 0.3 {
            warnings.push(format!("The file is format {}, newer than 0.3; parts this app does not know are ignored.", rep(fmt)));
        }
    }
    let size = pt(res.get("map_size"));
    let ppg = num(res.get("pixels_per_grid"));
    let origin = pt(res.get("map_origin")).unwrap_or((0.0, 0.0));
    let Some(size) = size.filter(|s| s.0 > 0.0 && s.1 > 0.0 && s.0 <= 1000.0 && s.1 <= 1000.0) else { return Err("resolution.map_size is missing or not sensible".into()) };
    let Some(ppg) = ppg.filter(|p| (4.0..=2000.0).contains(p)) else { return Err("resolution.pixels_per_grid is missing or not sensible".into()) };

    // the embedded picture
    let b64 = match d.get("image") {
        Some(Value::String(s)) if !s.trim().is_empty() => s,
        _ => return Err("The file has no embedded picture, so there is no map to draw. Export it again with the image included.".into()),
    };
    let cleaned: Vec<u8> = b64.bytes().filter(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'/' | b'=')).collect();
    let engine = base64::engine::GeneralPurpose::new(&base64::alphabet::STANDARD, base64::engine::GeneralPurposeConfig::new().with_decode_padding_mode(base64::engine::DecodePaddingMode::Indifferent).with_decode_allow_trailing_bits(true));
    let Ok(bytes) = engine.decode(&cleaned) else { return Err("The embedded picture is not valid base64".into()) };
    if bytes.is_empty() || bytes.len() > MAX_IMAGE_BYTES {
        return Err("The embedded picture is missing or too large".into());
    }
    let unreadable = || "The embedded picture is not an image this app can read".to_string();
    let reader = image::ImageReader::new(std::io::Cursor::new(&bytes)).with_guessed_format().map_err(|_| unreadable())?;
    if reader.format() == Some(image::ImageFormat::Avif) {
        return Ok(None);
    }
    let decoder = image::ImageReader::into_decoder(reader).map_err(|_| unreadable())?;
    let (w0, h0) = image::ImageDecoder::dimensions(&decoder);
    if (w0 as u64) * (h0 as u64) > MAX_PIXELS {
        return Err("The map picture has too many pixels".into());
    }
    let mut im = image::DynamicImage::from_decoder(decoder).map_err(|_| unreadable())?;
    let (mut iw, mut ih) = (w0 as f64, h0 as f64);
    // the picture is the truth about pixels: squares are mapped onto it, whatever the header claims
    let (mut sx, mut sy) = (iw / size.0, ih / size.1);
    if (sx - ppg).abs() > 1.0 || (sy - ppg).abs() > 1.0 {
        warnings.push(format!("The picture is {}x{}px but the header says {}x{} squares at {}px; the picture was used as is.", w0, h0, rep(size.0), rep(size.1), rep(ppg)));
    }
    // the schematic canvas holds at most MAX_SIDE px; a bigger picture is scaled down together with the walls
    let k = (MAX_SIDE / iw.max(ih)).min(1.0);
    if (iw * k).max(ih * k) < MIN_SIDE {
        return Err("The map picture is too small".into());
    }
    if k < 1.0 {
        let (nw, nh) = (((iw * k).round_ties_even() as u32).max(1), ((ih * k).round_ties_even() as u32).max(1));
        warnings.push(format!("The picture was larger than {}px, so it was scaled down to {}x{}.", MAX_SIDE as i64, (iw * k).round_ties_even() as i64, (ih * k).round_ties_even() as i64));
        im = im.resize_exact(nw, nh, image::imageops::FilterType::Lanczos3);
        iw = nw as f64;
        ih = nh as f64;
        sx = iw / size.0;
        sy = ih / size.1;
    }
    let cell = (sx + sy) / 2.0;
    let r2 = |x: f64| crate::edits::py_round_pub(x, 2);
    let px = |p: (f64, f64)| -> Option<Value> { Some(json!([r2((p.0 - origin.0) * sx)?, r2((p.1 - origin.1) * sy)?])) };

    let (mut walls, mut total, mut dropped): (Vec<Value>, usize, usize) = (vec![], 0, 0);
    let mut add = |points: Vec<Value>, kind: &str, state: Option<&str>| {
        if walls.len() >= MAX_POLYLINES || total + points.len() > MAX_POINTS {
            dropped += 1;
            return;
        }
        if points.len() < 2 {
            return;
        }
        let n = points.len();
        let mut w = Map::new();
        w.insert("id".into(), json!(format!("u{}", walls.len() + 1)));
        w.insert("pts".into(), Value::Array(points));
        w.insert("kind".into(), json!(kind));
        if let Some(s) = state {
            w.insert("state".into(), json!(s));
        }
        walls.push(Value::Object(w));
        total += n;
    };
    let mut polylines = |key: &str, tol: f64, label: &str, warnings: &mut Vec<String>| -> Option<i64> {
        let lst = match d.get(key) {
            None | Some(Value::Null) => return Some(0),
            Some(Value::Array(a)) => a,
            Some(_) => {
                warnings.push(format!("{label} was not a list and was skipped."));
                return Some(0);
            }
        };
        let mut n = 0;
        for line in lst {
            let Value::Array(line) = line else { continue };
            let pts: Vec<Option<(f64, f64)>> = line.iter().map(|p| pt(Some(p))).collect();
            if pts.len() < 2 || pts.iter().any(|p| p.is_none()) {
                continue;
            }
            let mut pts: Vec<(f64, f64)> = pts.into_iter().flatten().collect();
            if tol > 0.0 {
                pts = simplify(&pts, tol);
            }
            let mut out = vec![];
            for p in pts {
                out.push(px(p)?);
            }
            add(out, "wall", None);
            n += 1;
        }
        Some(n)
    };
    let Some(n_walls) = polylines("line_of_sight", 0.0, "line_of_sight", &mut warnings) else { return Ok(None) };
    let Some(n_objects) = polylines("objects_line_of_sight", 0.05, "objects_line_of_sight", &mut warnings) else { return Ok(None) };

    let mut n_doors = 0i64;
    if let Some(Value::Array(portals)) = d.get("portals") {
        for p in portals {
            let Some(p) = p.as_object() else { continue };
            let Some(Value::Array(bounds)) = p.get("bounds") else { continue };
            if bounds.len() != 2 {
                continue;
            }
            let (Some(a), Some(b)) = (pt(bounds.first()), pt(bounds.get(1))) else { continue };
            if a == b {
                continue;
            }
            // `p.get("closed", True) is not False`
            let closed = !matches!(p.get("closed"), Some(Value::Bool(false)));
            add(vec![match px(a) { Some(v) => v, None => return Ok(None) }, match px(b) { Some(v) => v, None => return Ok(None) }], "door", Some(if closed { "closed" } else { "open" }));
            n_doors += 1;
        }
    }
    if n_doors > 0 {
        warnings.push("Windows are doors in this format, so any window in the file is now a door you can open.".into());
    }
    let mut lights: Vec<Value> = vec![];
    let empty = vec![];
    let raw_lights = match d.get("lights") { Some(Value::Array(a)) => a, _ => &empty };
    for l in raw_lights.iter().take(60) {
        let Some(l) = l.as_object() else { continue };
        let (pos, rng, kk) = (pt(l.get("position")), num(l.get("range")), num(l.get("intensity")));
        let (Some(pos), Some(rng)) = (pos, rng) else { continue };
        if rng <= 0.0 {
            continue;
        }
        let colour = match l.get("color") {
            Some(Value::String(c)) if hex8(c) => format!("#{}", c[2..].to_lowercase()),
            _ => "#ffd9a0".to_string(),
        };
        let (Some(x), Some(y)) = (r2((pos.0 - origin.0) * sx), r2((pos.1 - origin.1) * sy)) else { return Ok(None) };
        lights.push(json!({"id": format!("u{}", lights.len() + 1), "x": x, "y": y, "range": rng.min(60.0), "color": colour,
                           "intensity": match kk { None => 1.0, Some(k) => k.max(0.1).min(1.0) }, "on": true}));
    }
    if raw_lights.len() > 60 {
        warnings.push("Only the first 60 lights were imported.".into());
    }
    // the map's darkness: how dim "no light" is. A file with baked lighting already has it in the picture, so none is added.
    let mut darkness = 0.0f64;
    let env = d.get("environment").and_then(|v| v.as_object());
    let baked = env.map_or(false, |e| crate::edits::py_truthy_pub(e.get("baked_lighting")));
    if let Some(Value::String(amb)) = env.and_then(|e| e.get("ambient_light")) {
        if hex8(amb) && !baked {
            let c = |i: usize| i64::from_str_radix(&amb[i..i + 2], 16).unwrap_or(0);
            let sum = c(2) + c(4) + c(6);
            darkness = match crate::edits::py_round_pub((1.0 - sum as f64 / 765.0).max(0.0).min(0.95), 2) { Some(v) => v, None => return Ok(None) };
        }
    }
    if !lights.is_empty() && baked {
        warnings.push("The picture already has its lighting painted in; the file's lights were imported but add on top of it.".into());
    } else if !lights.is_empty() && darkness == 0.0 {
        warnings.push(format!("The file has {} light(s); they show once you raise Darkness in Walls & fog.", lights.len()));
    }
    if dropped > 0 {
        warnings.push(format!("{dropped} wall line(s) were skipped because the limits ({MAX_POLYLINES} lines / {MAX_POINTS} points) were reached."));
    }
    if walls.is_empty() {
        warnings.push("The file has no walls, so fog of war has nothing to work with.".into());
    }
    let mut stats = Map::new();
    stats.insert("walls".into(), json!(n_walls));
    stats.insert("object_outlines".into(), json!(n_objects));
    stats.insert("doors".into(), json!(n_doors));
    stats.insert("lights".into(), json!(lights.len()));
    stats.insert("squares".into(), json!([size.0, size.1]));
    let cell = match r2(cell) { Some(c) => c, None => return Ok(None) };
    Ok(Some(Parsed { image: im, width: iw as i64, height: ih as i64, cell, walls, warnings, lights, darkness, stats }))
}

/// `encode_image`: the map picture as WebP (RGBA when the picture has an alpha channel or a palette, else RGB), quality 92
pub fn encode_webp(im: &image::DynamicImage, had_alpha: bool) -> Option<Vec<u8>> {
    Some(if had_alpha {
        let b = im.to_rgba8();
        webp::Encoder::from_rgba(b.as_raw(), b.width(), b.height()).encode_simple(false, 92.0).ok()?.to_vec()
    } else {
        let b = im.to_rgb8();
        webp::Encoder::from_rgb(b.as_raw(), b.width(), b.height()).encode_simple(false, 92.0).ok()?.to_vec()
    })
}
