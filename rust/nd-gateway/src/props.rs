//! The prop library's upload (`POST /api/maps/props/upload`, app/routers/map_props.py + app/prop_images.py): a picture of a map object turned into
//! something safe to show every player. A raster picture is decoded, its first frame taken, rotated by its EXIF orientation, trimmed of transparent
//! margins, shrunk to 1024 px and stored as WebP (libwebp, not Pillow: the bytes differ, the size and the footprint do not). An SVG is rebuilt from the
//! same allow-list, node by node, and written the way ElementTree writes it, so the stored file is byte-identical to Python's.
//! What it leaves to Python (and so, without Python, to the generic refusal): AVIF input (no decoder here) and the odd input Python would report in its own words.

use std::io::Cursor;

use fancy_regex::Regex as FRegex;
use image::{DynamicImage, ImageDecoder};

pub const MAX_RAW_BYTES: usize = 12 * 1024 * 1024;
const MAX_PIXELS: u64 = 40_000_000;
const MAX_SIDE: u32 = 1024;
const MIN_SIDE: u32 = 4;
const MAX_SVG_BYTES: usize = 512 * 1024;
const MAX_SVG_NODES: usize = 5000;

/// The result of `process_prop`
pub struct Prop {
    pub data: Vec<u8>,
    pub ext: &'static str,
    pub width: f64,
    pub height: f64,
}

/// Err(reason) is a `PropError` (shown to the uploader); Ok(None) is "Python's" (declined)
pub type Outcome = Result<Option<Prop>, String>;

pub fn process_prop(raw: &[u8], ext: &str) -> Outcome {
    match ext {
        ".svg" => {
            let clean = match sanitize_svg(raw)? { Some(c) => c, None => return Ok(None) };
            let Some((w, h)) = svg_size(&clean) else { return Ok(None) };
            Ok(Some(Prop { data: clean.bytes, ext: ".svg", width: w, height: h }))
        }
        ".png" | ".jpg" | ".jpeg" | ".webp" | ".gif" | ".bmp" | ".tif" | ".tiff" => process_raster(raw),
        ".avif" => Ok(None),
        _ => Err("Use PNG, JPG, WebP, GIF, AVIF, BMP, TIFF or SVG".into()),
    }
}

// ── raster ──

fn process_raster(raw: &[u8]) -> Outcome {
    if raw.len() > MAX_RAW_BYTES {
        return Err("That file is too large for a map object".into());
    }
    let unreadable = || "That file is not a picture this app can read".to_string();
    let reader = image::ImageReader::new(Cursor::new(raw)).with_guessed_format().map_err(|_| unreadable())?;
    if reader.format() == Some(image::ImageFormat::Avif) {
        return Ok(None);
    }
    let mut decoder = reader.into_decoder().map_err(|_| unreadable())?;
    let (w0, h0) = decoder.dimensions();
    if (w0 as u64) * (h0 as u64) > MAX_PIXELS {
        return Err("That picture has too many pixels".into());
    }
    let orientation = decoder.orientation().unwrap_or(image::metadata::Orientation::NoTransforms);
    let mut img = DynamicImage::from_decoder(decoder).map_err(|_| unreadable())?;
    img.apply_orientation(orientation);
    let mut rgba = img.to_rgba8();
    // trim: the box of the pixels whose alpha is above 8
    let (w, h) = rgba.dimensions();
    let (mut x0, mut y0, mut x1, mut y1) = (w, h, 0u32, 0u32);
    for (x, y, p) in rgba.enumerate_pixels() {
        if p.0[3] > 8 {
            x0 = x0.min(x);
            y0 = y0.min(y);
            x1 = x1.max(x + 1);
            y1 = y1.max(y + 1);
        }
    }
    if x1 <= x0 || y1 <= y0 {
        return Err("The picture is completely transparent".into());
    }
    let mut cropped = image::imageops::crop(&mut rgba, x0, y0, x1 - x0, y1 - y0).to_image();
    let (cw, ch) = cropped.dimensions();
    if cw.min(ch) < MIN_SIDE {
        return Err("The object is too small to use".into());
    }
    if cw.max(ch) > MAX_SIDE {
        let (nw, nh) = thumbnail_size(cw, ch, MAX_SIDE);
        cropped = image::imageops::resize(&cropped, nw, nh, image::imageops::FilterType::Lanczos3);
    }
    let (fw, fh) = cropped.dimensions();
    let data = webp::Encoder::from_rgba(cropped.as_raw(), fw, fh).encode_simple(false, 92.0).map_err(|_| unreadable())?.to_vec();
    Ok(Some(Prop { data, ext: ".webp", width: fw as f64, height: fh as f64 }))
}

/// Pillow's `Image.thumbnail((n, n))` size rule: the longer side becomes `n`, the other keeps the aspect, rounded the way `round_aspect` does
fn thumbnail_size(x: u32, y: u32, n: u32) -> (u32, u32) {
    let (xf, yf) = (x as f64, y as f64);
    let aspect = xf / yf;
    let round_aspect = |number: f64, key: &dyn Fn(f64) -> f64| -> u32 {
        let (fl, ce) = (number.floor(), number.ceil());
        let best = if key(fl) <= key(ce) { fl } else { ce };
        best.max(1.0) as u32
    };
    if xf / n as f64 >= yf / n as f64 {
        let nx = n as f64;
        (n, round_aspect(nx / aspect, &|c| (aspect - nx / c).abs()))
    } else {
        let ny = n as f64;
        (round_aspect(ny * aspect, &|c| (aspect - c / ny).abs()), n)
    }
}

// ── SVG ──

const SVG_NS: &str = "http://www.w3.org/2000/svg";
const XLINK: &str = "http://www.w3.org/1999/xlink";

const ELEMENTS: &[&str] = &["svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "linearGradient", "radialGradient",
    "stop", "clipPath", "mask", "pattern", "title", "desc"];
const ATTRS: &[&str] = &["id", "class", "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "points", "transform", "viewBox",
    "preserveAspectRatio", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray",
    "stroke-dashoffset", "stroke-miterlimit", "opacity", "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "fx", "fy",
    "clip-path", "clip-rule", "mask", "patternUnits", "patternTransform", "patternContentUnits", "maskUnits", "clipPathUnits", "font-size", "font-family",
    "font-weight", "text-anchor", "dominant-baseline", "dx", "dy", "href", "style", "version", "display", "visibility", "color"];

/// A cleaned element, written the way `ET.tostring` writes it
struct Node {
    tag: String,
    attrs: Vec<(String, String)>,
    text: Option<String>,
    children: Vec<Node>,
}

pub struct CleanSvg {
    pub bytes: Vec<u8>,
    root_attrs: Vec<(String, String)>,
}

fn bad_value() -> &'static FRegex {
    static R: std::sync::OnceLock<FRegex> = std::sync::OnceLock::new();
    R.get_or_init(|| FRegex::new(r"(?i)(javascript:|data:|vbscript:|expression\s*\(|@import|<|>|\\)").unwrap())
}

fn url_ref() -> &'static FRegex {
    static R: std::sync::OnceLock<FRegex> = std::sync::OnceLock::new();
    R.get_or_init(|| FRegex::new(r#"(?i)url\(\s*(['"]?)(.*?)\1\s*\)"#).unwrap())
}

fn style_prop() -> &'static FRegex {
    static R: std::sync::OnceLock<FRegex> = std::sync::OnceLock::new();
    R.get_or_init(|| FRegex::new(r"(?i)^\s*(fill|fill-opacity|stroke|stroke-width|stroke-opacity|stroke-linecap|stroke-linejoin|stroke-dasharray|opacity|stop-color|stop-opacity|font-size|font-family|font-weight|display|visibility|clip-path|mask)\s*:\s*([^;]*)$").unwrap())
}

fn check_value(attr: &str, value: &str) -> Result<(), String> {
    if bad_value().is_match(value).map_err(|_| "The SVG is not valid".to_string())? {
        return Err(format!("The SVG contains something that is not allowed ({attr})"));
    }
    for m in url_ref().captures_iter(value) {
        let m = m.map_err(|_| "The SVG is not valid".to_string())?;
        if !m.get(2).map_or("", |g| g.as_str()).starts_with('#') {
            return Err("The SVG refers to something outside itself".into());
        }
    }
    Ok(())
}

fn clean_style(value: &str) -> Result<String, String> {
    let mut keep = vec![];
    for decl in value.split(';') {
        if decl.trim().is_empty() {
            continue;
        }
        let m = style_prop().captures(decl).map_err(|_| "The SVG is not valid".to_string())?;
        let Some(m) = m else { return Err("The SVG uses styling that is not allowed".into()) };
        let val = m.get(2).map_or("", |g| g.as_str());
        check_value("style", val)?;
        keep.push(format!("{}:{}", m.get(1).map_or("", |g| g.as_str()).to_lowercase(), nd_render::text::py_strip(val)));
    }
    Ok(keep.join(";"))
}

/// Ok(Some) cleaned; Ok(None) the input is beyond what is reproduced here (Python's); Err a `PropError`
fn sanitize_svg(raw: &[u8]) -> Result<Option<CleanSvg>, String> {
    if raw.len() > MAX_SVG_BYTES {
        return Err("That SVG is too large".into());
    }
    let low = raw.to_ascii_lowercase();
    let has = |n: &[u8]| low.windows(n.len()).any(|w| w == n);
    if has(b"<!doctype") || has(b"<!entity") {
        return Err("That SVG declares entities, which are not allowed".into());
    }
    let Ok(text) = std::str::from_utf8(raw) else { return Ok(None) };
    let Ok(doc) = roxmltree::Document::parse(text) else { return Err("That SVG is not valid".into()) };
    let root = doc.root_element();
    let ns_ok = match root.tag_name().namespace() { None => true, Some(n) => n == SVG_NS };
    if root.tag_name().name() != "svg" || !ns_ok {
        return Err("That is not an SVG drawing".into());
    }
    let mut count = 0usize;
    let mut cleaned = clean_node(root, &mut count)?;
    cleaned.attrs.push(("xmlns".into(), SVG_NS.into()));
    let root_attrs = cleaned.attrs.clone();
    let mut out = String::from("<?xml version='1.0' encoding='utf-8'?>\n");
    write_node(&cleaned, &mut out);
    Ok(Some(CleanSvg { bytes: out.into_bytes(), root_attrs }))
}

fn clean_node(node: roxmltree::Node, count: &mut usize) -> Result<Node, String> {
    *count += 1;
    if *count > MAX_SVG_NODES {
        return Err("That SVG is too complicated".into());
    }
    let tag = node.tag_name().name().to_string();
    if !ELEMENTS.contains(&tag.as_str()) {
        return Err(format!("The SVG contains an element that is not allowed ({tag})"));
    }
    let mut attrs: Vec<(String, String)> = vec![];
    for a in node.attributes() {
        let name = a.name();
        match a.namespace() {
            Some(ns) if ns != XLINK => continue, // editor metadata (inkscape:, sodipodi:, xml:space ...) is dropped
            _ => {}
        }
        if name.starts_with("xmlns") || name == "space" {
            continue;
        }
        if !ATTRS.contains(&name) {
            if name.starts_with("data-") || name.starts_with("aria-") {
                continue;
            }
            return Err(format!("The SVG contains an attribute that is not allowed ({name})"));
        }
        let value = a.value();
        let value = if name == "href" {
            return Err("The SVG refers to something outside itself".into());
        } else if name == "style" {
            clean_style(value)?
        } else {
            check_value(name, value)?;
            value.to_string()
        };
        match attrs.iter_mut().find(|(k, _)| k == name) {
            Some(slot) => slot.1 = value, // a repeated name: the later one wins, in the first one's place (a dict's rule)
            None => attrs.push((name.to_string(), value)),
        }
    }
    let get = |k: &str| attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str()).unwrap_or("");
    if tag == "svg" && get("viewBox").is_empty() && !(!get("width").is_empty() && !get("height").is_empty()) {
        return Err("The SVG has no size (add a viewBox)".into());
    }
    let mut text = None;
    if matches!(tag.as_str(), "text" | "tspan" | "title" | "desc") {
        if let Some(first) = node.first_child().filter(|c| c.is_text()) {
            let t = first.text().unwrap_or("");
            if !t.is_empty() {
                if t.chars().count() > 500 || t.contains('<') {
                    return Err("The SVG contains text that is not allowed".into());
                }
                text = Some(t.to_string());
            }
        }
    }
    let mut children = vec![];
    for child in node.children().filter(|c| c.is_element()) {
        children.push(clean_node(child, count)?);
    }
    Ok(Node { tag, attrs, text, children })
}

fn esc_cdata(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn esc_attrib(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\r', "&#13;").replace('\n', "&#10;").replace('\t', "&#09;")
}

fn write_node(n: &Node, out: &mut String) {
    out.push('<');
    out.push_str(&n.tag);
    for (k, v) in &n.attrs {
        out.push_str(&format!(" {k}=\"{}\"", esc_attrib(v)));
    }
    if n.text.is_none() && n.children.is_empty() {
        out.push_str(" />");
        return;
    }
    out.push('>');
    if let Some(t) = &n.text {
        out.push_str(&esc_cdata(t));
    }
    for c in &n.children {
        write_node(c, out);
    }
    out.push_str(&format!("</{}>", n.tag));
}

/// `svg_size`: the viewBox's width and height, else the width / height attributes, else 100 x 100; None for what Python raises on (or prints its own way)
fn svg_size(c: &CleanSvg) -> Option<(f64, f64)> {
    let get = |k: &str| c.root_attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str()).unwrap_or("");
    // float(): Err = a ValueError (an underscore is legal in Python's float(), not here: Python's)
    let parse = |t: &str| -> Result<f64, bool> {
        if t.contains('_') {
            return Err(true);
        }
        t.trim().parse::<f64>().map_err(|_| false)
    };
    let vb: Vec<String> = get("viewBox").replace(',', " ").split_whitespace().map(|s| s.to_string()).collect();
    if vb.len() == 4 {
        match (parse(&vb[2]), parse(&vb[3])) {
            (Err(true), _) | (_, Err(true)) => return None,
            (Ok(w), Ok(h)) => {
                if !w.is_finite() || !h.is_finite() {
                    return None;
                }
                if w > 0.0 && h > 0.0 {
                    return Some((w, h));
                }
            }
            _ => {} // a ValueError inside the try: fall through to width / height
        }
    }
    // num(v): ^\s*([0-9.]+) -> float(group), 0.0 when nothing matches; "1.2.3" is a ValueError Python does not catch
    let num = |v: &str| -> Option<f64> {
        let t = v.trim_start();
        let digits: String = t.chars().take_while(|ch| ch.is_ascii_digit() || *ch == '.').collect();
        if digits.is_empty() {
            return Some(0.0);
        }
        digits.parse::<f64>().ok()
    };
    let (w, h) = (num(get("width"))?, num(get("height"))?);
    Some(if w > 0.0 && h > 0.0 { (w, h) } else { (100.0, 100.0) })
}

/// Python's `default_cells(width, height)`: the footprint in squares at about 100 px a square, long side between a quarter and 20 squares
pub fn default_cells(width: f64, height: f64) -> (f64, f64) {
    let (w, h) = (width.max(1.0), height.max(1.0));
    let long_side = (w.max(h) / 100.0).max(0.25).min(20.0);
    let scale = long_side / w.max(h);
    let q = |v: f64| ((v * 4.0).round_ties_even() / 4.0).max(0.25);
    (q(w * scale), q(h * scale))
}
