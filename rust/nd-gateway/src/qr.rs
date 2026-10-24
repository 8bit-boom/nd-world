//! `totp.qr_code_data_uri`: `qrcode.make(uri, box_size=6, border=2)` as a PNG data URI - module for module the code Python's qrcode
//! package draws: the same segments (`optimal_data_chunks(data, minimum=20)`), the smallest version that fits at level M, and the
//! mask its `best_mask_pattern` / `lost_point` picks (format and version areas left light while masks are scored, as there).

use base64::Engine;
use qrcode::bits::Bits;
use qrcode::canvas::{Canvas, MaskPattern};
use qrcode::types::{Color, EcLevel, Version};

const ALPHA_NUM: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:";

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Num,
    Alpha,
    Byte,
}

/// `_optimal_split(data, pattern)` for "a run of at least `min` bytes matching `is`"
fn split(data: &[u8], min: usize, is: impl Fn(u8) -> bool) -> Vec<(bool, &[u8])> {
    let mut out = vec![];
    let mut rest = data;
    loop {
        let mut found = None;
        let mut i = 0;
        while i < rest.len() {
            if is(rest[i]) {
                let mut j = i;
                while j < rest.len() && is(rest[j]) {
                    j += 1;
                }
                if j - i >= min {
                    found = Some((i, j));
                    break;
                }
                i = j;
            } else {
                i += 1;
            }
        }
        let Some((s, e)) = found else { break };
        if s > 0 {
            out.push((false, &rest[..s]));
        }
        out.push((true, &rest[s..e]));
        rest = &rest[e..];
    }
    if !rest.is_empty() {
        out.push((false, rest));
    }
    out
}

/// `optimal_data_chunks(data, minimum=20)` (data longer than the minimum)
fn chunks(data: &[u8]) -> Vec<(Mode, &[u8])> {
    let mut out = vec![];
    for (num, c) in split(data, 20, |b| b.is_ascii_digit()) {
        if num {
            out.push((Mode::Num, c));
        } else {
            for (alpha, s) in split(c, 20, |b| ALPHA_NUM.contains(&b)) {
                out.push((if alpha { Mode::Alpha } else { Mode::Byte }, s));
            }
        }
    }
    out
}

fn bits_for(data: &[u8], v: i16) -> Option<Bits> {
    let mut bits = Bits::new(Version::Normal(v));
    for (mode, c) in chunks(data) {
        match mode {
            Mode::Num => bits.push_numeric_data(c).ok()?,
            Mode::Alpha => bits.push_alphanumeric_data(c).ok()?,
            Mode::Byte => bits.push_byte_data(c).ok()?,
        }
    }
    bits.push_terminator(EcLevel::M).ok()?;
    Some(bits)
}

const MASKS: [MaskPattern; 8] = [MaskPattern::Checkerboard, MaskPattern::HorizontalLines, MaskPattern::VerticalLines, MaskPattern::DiagonalLines,
    MaskPattern::LargeCheckerboard, MaskPattern::Fields, MaskPattern::Diamonds, MaskPattern::Meadow];

fn rows(canvas: Canvas, n: usize) -> Vec<Vec<bool>> {
    let colors = canvas.into_colors();
    (0..n).map(|y| (0..n).map(|x| colors[y * n + x] == Color::Dark).collect()).collect()
}

/// The matrix as Python scores it: the format bits, the fixed dark module and (version 7+) the version bits all light
fn blank_type_info(m: &mut [Vec<bool>], v: i16) {
    let n = m.len();
    for i in 0..15 {
        let r = if i < 6 { i } else if i < 8 { i + 1 } else { n - 15 + i };
        m[r][8] = false;
        let c = if i < 8 { n - i - 1 } else if i < 9 { 15 - i } else { 15 - i - 1 };
        m[8][c] = false;
    }
    m[n - 8][8] = false;
    if v >= 7 {
        for i in 0..18 {
            m[i / 3][i % 3 + n - 8 - 3] = false;
            m[i % 3 + n - 8 - 3][i / 3] = false;
        }
    }
}

/// `util.lost_point`
fn lost_point(m: &[Vec<bool>]) -> u32 {
    let n = m.len();
    let mut lost = 0u32;
    // level 1: runs of 5+
    let mut container = vec![0u32; n + 1];
    let mut runs = |get: &dyn Fn(usize, usize) -> bool| {
        for a in 0..n {
            let mut prev = get(a, 0);
            let mut len = 0;
            for b in 0..n {
                if get(a, b) == prev {
                    len += 1;
                } else {
                    if len >= 5 {
                        container[len] += 1;
                    }
                    len = 1;
                    prev = get(a, b);
                }
            }
            if len >= 5 {
                container[len] += 1;
            }
        }
    };
    runs(&|r, c| m[r][c]);
    runs(&|c, r| m[r][c]);
    lost += (5..=n).map(|l| container[l] * (l as u32 - 2)).sum::<u32>();
    // level 2: 2x2 blocks (with Python's skip)
    for r in 0..n - 1 {
        let (this, next) = (&m[r], &m[r + 1]);
        let mut c = 0;
        while c < n - 1 {
            let tr = this[c + 1];
            if tr != next[c + 1] {
                c += 2;
                continue;
            } else if tr != this[c] || tr != next[c] {
            } else {
                lost += 3;
            }
            c += 1;
        }
    }
    // level 3: the 1:1:3:1:1 pattern with 4 light modules (with Python's skip)
    let hit = |g: &dyn Fn(usize) -> bool| {
        !g(1) && g(4) && !g(5) && g(6) && !g(9)
            && ((g(0) && g(2) && g(3) && !g(7) && !g(8) && !g(10)) || (!g(0) && !g(2) && !g(3) && g(7) && g(8) && g(10)))
    };
    if n > 10 {
        for a in 0..n {
            for horizontal in [true, false] {
                let mut b = 0;
                while b < n - 10 {
                    let g = |k: usize| if horizontal { m[a][b + k] } else { m[b + k][a] };
                    if hit(&g) {
                        lost += 40;
                    }
                    b += if g(10) { 2 } else { 1 };
                }
            }
        }
    }
    // level 4: balance
    let dark: usize = m.iter().map(|r| r.iter().filter(|x| **x).count()).sum();
    let percent = dark as f64 / (n * n) as f64;
    lost += ((percent * 100.0 - 50.0).abs() / 5.0) as u32 * 10;
    lost
}

/// The module matrix Python's qrcode draws for `data` (rows of dark = true)
pub fn matrix(data: &[u8]) -> Option<Vec<Vec<bool>>> {
    let (v, bits) = (1..=40).find_map(|v| bits_for(data, v).map(|b| (v, b)))?;
    let bytes = bits.into_bytes();
    let (encoded, ec) = qrcode::ec::construct_codewords(&bytes, Version::Normal(v), EcLevel::M).ok()?;
    let n = Version::Normal(v).width() as usize;
    let mut base = Canvas::new(Version::Normal(v), EcLevel::M);
    base.draw_all_functional_patterns();
    base.draw_data(&encoded, &ec);
    let mut best: Option<(u32, usize)> = None;
    for (i, mask) in MASKS.iter().enumerate() {
        let mut c = base.clone();
        c.apply_mask(*mask);
        let mut m = rows(c, n);
        blank_type_info(&mut m, v);
        let lp = lost_point(&m);
        if best.map_or(true, |(b, _)| lp < b) {
            best = Some((lp, i));
        }
    }
    let mut c = base;
    c.apply_mask(MASKS[best?.1]);
    Some(rows(c, n))
}

/// `qr_code_data_uri(uri)`: box size 6, a 2-module border, black on white
pub fn data_uri(text: &str) -> Option<String> {
    let m = matrix(text.as_bytes())?;
    let (box_px, border) = (6usize, 2usize);
    let side = (m.len() + 2 * border) * box_px;
    let mut img = image::GrayImage::from_pixel(side as u32, side as u32, image::Luma([255]));
    for (r, row) in m.iter().enumerate() {
        for (c, dark) in row.iter().enumerate() {
            if *dark {
                for dy in 0..box_px {
                    for dx in 0..box_px {
                        img.put_pixel(((c + border) * box_px + dx) as u32, ((r + border) * box_px + dy) as u32, image::Luma([0]));
                    }
                }
            }
        }
    }
    let mut png = std::io::Cursor::new(Vec::new());
    img.write_to(&mut png, image::ImageFormat::Png).ok()?;
    Some(format!("data:image/png;base64,{}", base64::engine::general_purpose::STANDARD.encode(png.into_inner())))
}
