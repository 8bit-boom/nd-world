//! A small PDF writer for the character `.pdf` export (Python uses reportlab's platypus): US Letter, 0.75" margins, the built-in Helvetica family, text
//! wrapped to the page and flowed over as many pages as needed. Same content and reading order as Python's file; the bytes are not reportlab's.

#[derive(Clone, Copy, PartialEq)]
pub enum Font {
    Regular,
    Bold,
    Italic,
}

#[derive(Clone)]
pub enum Piece {
    Word(String, Font),
    Break,
}

pub struct Para {
    pub pieces: Vec<Piece>,
    pub size: f32,
    pub leading: f32,
    pub before: f32,
    pub after: f32,
    pub center: bool,
}

const W: [u16; 95] = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];

/// the character as one WinAnsi byte ('?' for what the base-14 fonts cannot show)
fn win(c: char) -> u8 {
    match c {
        ' '..='~' => c as u8,
        '\u{a0}'..='\u{ff}' => c as u32 as u8,
        '\u{2014}' => 0x97,
        '\u{2013}' => 0x96,
        '\u{2018}' => 0x91,
        '\u{2019}' => 0x92,
        '\u{201c}' => 0x93,
        '\u{201d}' => 0x94,
        '\u{2022}' => 0x95,
        '\u{2026}' => 0x85,
        '\u{20ac}' => 0x80,
        _ => b'?',
    }
}

fn width(b: u8, f: Font, size: f32) -> f32 {
    let w = if (32..=126).contains(&b) { W[(b - 32) as usize] as f32 } else { 556.0 };
    w * if f == Font::Bold { 1.06 } else { 1.0 } * size / 1000.0
}

fn esc(bytes: &[u8]) -> Vec<u8> {
    let mut o = vec![];
    for &b in bytes {
        if matches!(b, b'(' | b')' | b'\\') {
            o.push(b'\\');
        }
        o.push(b);
    }
    o
}

struct Line {
    runs: Vec<(Vec<u8>, Font)>,
    width: f32,
}

/// text of the same font joins the run before it, so a phrase stays one string in the file
fn push_run(runs: &mut Vec<(Vec<u8>, Font)>, bytes: Vec<u8>, f: Font) {
    match runs.last_mut() {
        Some((b, lf)) if *lf == f => b.extend(bytes),
        _ => runs.push((bytes, f)),
    }
}

fn wrap(p: &Para, max: f32) -> Vec<Line> {
    let mut lines = vec![];
    let mut cur = Line { runs: vec![], width: 0.0 };
    let space = |f: Font| width(b' ', f, p.size);
    for piece in &p.pieces {
        match piece {
            Piece::Break => lines.push(std::mem::replace(&mut cur, Line { runs: vec![], width: 0.0 })),
            Piece::Word(w, f) => {
                let bytes: Vec<u8> = w.chars().map(win).collect();
                let mut ww: f32 = bytes.iter().map(|&b| width(b, *f, p.size)).sum();
                if !cur.runs.is_empty() && cur.width + space(*f) + ww > max {
                    lines.push(std::mem::replace(&mut cur, Line { runs: vec![], width: 0.0 }));
                }
                let mut bytes = bytes;
                // a word wider than the line is cut where it overflows
                while ww > max && bytes.len() > 1 {
                    let (mut take, mut acc) = (0, 0.0);
                    for &b in &bytes {
                        let cw = width(b, *f, p.size);
                        if acc + cw > max && take > 0 {
                            break;
                        }
                        acc += cw;
                        take += 1;
                    }
                    let rest = bytes.split_off(take);
                    cur.runs.push((bytes, *f));
                    cur.width += acc;
                    lines.push(std::mem::replace(&mut cur, Line { runs: vec![], width: 0.0 }));
                    bytes = rest;
                    ww = bytes.iter().map(|&b| width(b, *f, p.size)).sum();
                }
                if !cur.runs.is_empty() {
                    push_run(&mut cur.runs, vec![b' '], *f);
                    cur.width += space(*f);
                }
                push_run(&mut cur.runs, bytes, *f);
                cur.width += ww;
            }
        }
    }
    lines.push(cur);
    lines
}

pub fn build(paras: &[Para]) -> Vec<u8> {
    const PW: f32 = 612.0;
    const PH: f32 = 792.0;
    const M: f32 = 54.0;
    let mut pages: Vec<Vec<u8>> = vec![vec![]];
    let mut y = PH - M;
    let mut first_on_page = true;
    for p in paras {
        let lines = wrap(p, PW - 2.0 * M);
        y -= if first_on_page { 0.0 } else { p.before };
        for l in &lines {
            if y - p.leading < M && !first_on_page {
                pages.push(vec![]);
                y = PH - M;
                first_on_page = true;
            }
            y -= p.leading;
            first_on_page = false;
            let out = pages.last_mut().unwrap();
            let x = if p.center { (PW - l.width) / 2.0 } else { M };
            out.extend(format!("BT {x:.2} {:.2} Td ", y + p.leading * 0.22).as_bytes());
            let mut cur_font: Option<Font> = None;
            for (bytes, f) in &l.runs {
                if cur_font != Some(*f) {
                    let n = match f {
                        Font::Regular => 1,
                        Font::Bold => 2,
                        Font::Italic => 3,
                    };
                    out.extend(format!("/F{n} {:.1} Tf ", p.size).as_bytes());
                    cur_font = Some(*f);
                }
                out.push(b'(');
                out.extend(esc(bytes));
                out.extend(b") Tj ");
            }
            out.extend(b"ET\n");
        }
        y -= p.after;
    }
    // objects: 1 catalog, 2 pages, 3-5 fonts, then (page, content) pairs
    let mut objs: Vec<Vec<u8>> = vec![];
    objs.push(b"<< /Type /Catalog /Pages 2 0 R >>".to_vec());
    let kids: Vec<String> = (0..pages.len()).map(|i| format!("{} 0 R", 6 + 2 * i)).collect();
    objs.push(format!("<< /Type /Pages /Kids [{}] /Count {} >>", kids.join(" "), pages.len()).into_bytes());
    for name in ["Helvetica", "Helvetica-Bold", "Helvetica-Oblique"] {
        objs.push(format!("<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>").into_bytes());
    }
    for (i, content) in pages.iter().enumerate() {
        objs.push(format!("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PW} {PH}] /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {} 0 R >>", 7 + 2 * i).into_bytes());
        let mut s = format!("<< /Length {} >>\nstream\n", content.len()).into_bytes();
        s.extend(content);
        s.extend(b"endstream");
        objs.push(s);
    }
    let mut out = b"%PDF-1.4\n".to_vec();
    let mut offs = vec![];
    for (i, o) in objs.iter().enumerate() {
        offs.push(out.len());
        out.extend(format!("{} 0 obj\n", i + 1).as_bytes());
        out.extend(o);
        out.extend(b"\nendobj\n");
    }
    let xref = out.len();
    out.extend(format!("xref\n0 {}\n0000000000 65535 f \n", objs.len() + 1).as_bytes());
    for o in offs {
        out.extend(format!("{o:010} 00000 n \n").as_bytes());
    }
    out.extend(format!("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n", objs.len() + 1).as_bytes());
    out
}
