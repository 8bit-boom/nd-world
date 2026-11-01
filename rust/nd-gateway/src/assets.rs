//! The image build's static-file work (what `python -m app.static_thumbs --avif-maps`, `app.map_tiles` and `app.precompress --minify` do for the normal
//! image), as one Rust program - `nd-assets <static dir> <app/maps dir>` - so the Python-free image needs no Python stage at all:
//!   1. bundled map pictures re-saved as AVIF when smaller (not above 40 MP; the original goes),
//!   2. a `_thumb.avif` for every picture under static/maps and static/races,
//!   3. the map tile pyramids into static/map_tiles (the gateway's own cutter, same keys as at run time),
//!   4. CSS comments / white space removed in place (JavaScript is left as written: no safe minifier without renaming or parsing it all),
//!   5. a `.gz` and `.br` twin beside every text asset of 1 KB or more, when smaller.

use std::io::Write;
use std::path::{Path, PathBuf};

const PICTURES: [&str; 6] = ["jpg", "jpeg", "png", "webp", "avif", "gif"];
const MAX_MAP_PIXELS: u64 = 40_000_000;

fn files_under(root: &Path) -> Vec<PathBuf> {
    let mut out = vec![];
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                stack.push(p);
            } else {
                out.push(p);
            }
        }
    }
    out.sort();
    out
}

fn ext(p: &Path) -> String {
    p.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase()
}

fn is_thumb(p: &Path) -> bool {
    p.file_stem().and_then(|s| s.to_str()).map_or(false, |s| s.ends_with("_thumb"))
}

/// 1. `compact_maps(static/maps, "avif")`: (files converted, bytes before, bytes after). Pictures are encoded a few at a time (the encoder is the
/// slow part of the image build)
pub fn compact_maps(folder: &Path) -> (u32, u64, u64) {
    let Ok(rd) = std::fs::read_dir(folder) else { return (0, 0, 0) };
    let mut srcs: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
    srcs.sort();
    srcs.retain(|s| s.is_file() && ["jpg", "jpeg", "png", "webp"].contains(&ext(s).as_str()) && !is_thumb(s));
    let next = std::sync::atomic::AtomicUsize::new(0);
    let totals = std::sync::Mutex::new((0u32, 0u64, 0u64));
    let workers = std::thread::available_parallelism().map_or(2, |n| n.get()).clamp(1, 4);
    std::thread::scope(|sc| {
        for _ in 0..workers {
            sc.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let Some(src) = srcs.get(i) else { break };
                if let Some((a, b)) = compact_one(src) {
                    if let Ok(mut t) = totals.lock() {
                        *t = (t.0 + 1, t.1 + a, t.2 + b);
                    }
                }
            });
        }
    });
    totals.into_inner().unwrap_or((0, 0, 0))
}

/// one picture to AVIF when that is smaller (the original goes): Some((bytes before, bytes after))
fn compact_one(src: &Path) -> Option<(u64, u64)> {
    let dest = src.with_extension("avif");
    if dest.exists() {
        return None;
    }
    let (w, h) = image::image_dimensions(src).ok()?;
    if w as u64 * h as u64 > MAX_MAP_PIXELS {
        return None;
    }
    let img = crate::imaging::readable(src)?;
    let framed = crate::imaging::build_frame(&img);
    drop(img);
    if crate::imaging::build_encode(&framed, "avif", 80, &dest).is_none() {
        let _ = std::fs::remove_file(&dest);
        return None;
    }
    let (a, b) = (std::fs::metadata(src).map(|m| m.len()).unwrap_or(0), std::fs::metadata(&dest).map(|m| m.len()).unwrap_or(u64::MAX));
    if b < a {
        let _ = std::fs::remove_file(src);
        Some((a, b))
    } else {
        let _ = std::fs::remove_file(&dest);
        None
    }
}

fn fresh_thumb(src: &Path) -> bool {
    let stem = src.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    let at = std::fs::metadata(src).and_then(|m| m.modified()).ok();
    ["_thumb.avif", "_thumb.webp"].iter().any(|s| {
        std::fs::metadata(src.with_file_name(format!("{stem}{s}"))).and_then(|m| m.modified()).ok().zip(at).map_or(false, |(t, a)| t >= a)
    })
}

/// 2. `static_thumbs.ensure(...)`: how many thumbnails were written
pub fn thumbnails(roots: &[PathBuf]) -> u32 {
    let mut n = 0;
    for root in roots {
        for src in files_under(root) {
            if PICTURES.contains(&ext(&src).as_str()) && !is_thumb(&src) && !fresh_thumb(&src) && crate::imaging::build_thumbnail(&src).is_some() {
                n += 1;
            }
        }
    }
    n
}

/// 3. `python -m app.map_tiles`: a pyramid for every `app/maps/<slug>.json` that has a picture in static/maps
pub fn tiles(static_dir: &Path, maps_json: &Path) -> u32 {
    std::env::set_var("ND_STATIC_DIR", static_dir);
    std::env::set_var("DB_PATH", static_dir.join("world.db")); // the cutter writes beside the DB: <static>/map_tiles
    let cfg = crate::config::Config::from_env();
    let (mut done, mut jsons) = (0, std::fs::read_dir(maps_json).map(|r| r.flatten().map(|e| e.path()).collect::<Vec<_>>()).unwrap_or_default());
    jsons.sort();
    for jf in jsons.into_iter().filter(|p| ext(p) == "json") {
        let Some(slug) = jf.file_stem().and_then(|s| s.to_str()).map(str::to_string) else { continue };
        let Some(d) = std::fs::read_to_string(&jf).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()) else { continue };
        let dim = |k: &str| d.get(k).and_then(|v| v.as_i64()).filter(|n| *n != 0).unwrap_or(1);
        let pics: Vec<PathBuf> = files_under(&static_dir.join("maps")).into_iter()
            .filter(|p| p.parent() == Some(&static_dir.join("maps")) && p.file_stem().and_then(|s| s.to_str()) == Some(&slug) && ["webp", "jpg", "jpeg", "png", "avif"].contains(&ext(p).as_str()))
            .collect();
        if let Some(pic) = pics.first() {
            if crate::tiles::build(&cfg, &slug, pic, dim("width"), dim("height")).is_some() {
                done += 1;
            }
        }
    }
    done
}

/// 4. CSS minified in place (comments and white space outside strings; `url(...)` and strings are copied as written)
pub fn minify_css(text: &str) -> String {
    let b: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    let mut pending_space = false;
    while i < b.len() {
        let c = b[i];
        if c == '/' && b.get(i + 1) == Some(&'*') {
            let end = (i + 2..b.len().saturating_sub(1)).find(|&j| b[j] == '*' && b[j + 1] == '/');
            match end {
                Some(j) => {
                    i = j + 2;
                    pending_space = true;
                    continue;
                }
                None => return text.to_string(), // an unterminated comment: leave the file alone
            }
        }
        if c == '"' || c == '\'' {
            let mut j = i + 1;
            while j < b.len() && b[j] != c {
                if b[j] == '\\' {
                    j += 1;
                }
                j += 1;
            }
            if pending_space && !out.is_empty() && !out.ends_with(['{', '}', ';', ':', ',', '>', '+', '~', '(']) {
                out.push(' ');
            }
            pending_space = false;
            out.extend(b[i..(j + 1).min(b.len())].iter());
            i = j + 1;
            continue;
        }
        if c.is_whitespace() {
            pending_space = true;
            i += 1;
            continue;
        }
        if matches!(c, '{' | '}' | ';' | ',' | '>' | ')') {
            pending_space = false;
            out.push(c);
            i += 1;
            continue;
        }
        if pending_space && !out.is_empty() && !out.ends_with(['{', '}', ';', ',', '>', '(']) {
            out.push(' ');
        }
        pending_space = false;
        out.push(c);
        i += 1;
    }
    // the last ';' before a '}' is not needed
    out.replace(";}", "}")
}

pub fn minify_tree(root: &Path) -> (u32, u64, u64) {
    let (mut n, mut a, mut b) = (0, 0, 0);
    for p in files_under(root) {
        if ext(&p) != "css" {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&p) else { continue };
        let out = minify_css(&text);
        if out.len() < text.len() {
            let _ = std::fs::write(&p, &out);
        }
        n += 1;
        a += text.len() as u64;
        b += out.len().min(text.len()) as u64;
    }
    (n, a, b)
}

/// 5. `precompress.compress_tree`: (files, .br written, .gz written)
pub fn precompress(root: &Path) -> (u32, u32, u32) {
    const EXTS: [&str; 8] = ["js", "css", "json", "svg", "html", "txt", "map", "mjs"];
    let (mut files, mut br, mut gz) = (0, 0, 0);
    for src in files_under(root) {
        if !EXTS.contains(&ext(&src).as_str()) {
            continue;
        }
        let Ok(data) = std::fs::read(&src) else { continue };
        if data.len() < 1024 {
            continue;
        }
        files += 1;
        let name = src.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        let blobs: [(&str, Vec<u8>); 2] = [
            (".br", {
                let mut out = vec![];
                {
                    let mut w = brotli::CompressorWriter::new(&mut out, 4096, 11, 22);
                    let _ = w.write_all(&data);
                }
                out
            }),
            (".gz", {
                let mut e = flate2::write::GzEncoder::new(vec![], flate2::Compression::best());
                let _ = e.write_all(&data);
                e.finish().unwrap_or_default()
            }),
        ];
        for (suffix, blob) in blobs {
            let twin = src.with_file_name(format!("{name}{suffix}"));
            if blob.is_empty() || blob.len() >= data.len() {
                let _ = std::fs::remove_file(&twin);
                continue;
            }
            if std::fs::write(&twin, blob).is_ok() {
                if suffix == ".br" {
                    br += 1
                } else {
                    gz += 1
                }
            }
        }
    }
    (files, br, gz)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn css_loses_comments_and_space_only() {
        let css = "/* c */\n.a  >  .b ,\n.c {\n  color : red;\n  content: \"a  b /* x */\";\n  background: url(\"x y.png\") ;\n}\n";
        assert_eq!(minify_css(css), ".a>.b,.c{color : red;content:\"a  b /* x */\";background: url(\"x y.png\")}");
    }
}
