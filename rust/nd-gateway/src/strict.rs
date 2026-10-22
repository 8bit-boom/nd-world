//! Strict secrecy for a map (a port of app/map_strict.py): what the party has seen, computed with the same angular sweep the browser
//! uses, so players are sent only that. The masked background picture (`/maps/schematic/<slug>/bg.webp`) stays Python's.

use serde_json::{json, Map, Value};
use std::collections::BTreeSet;

const MAX_VIEW_SQUARES: f64 = 30.0;
const ARC_STEPS: usize = 64;
const EPS: f64 = 1e-6;

/// Python's `a // b` for floats (CPython's float_floor_div: fmod, then the correction), not `floor(a / b)`
pub fn py_floordiv(a: f64, b: f64) -> f64 {
    let m = a % b;
    let mut div = (a - m) / b;
    if m != 0.0 && ((b < 0.0) != (m < 0.0)) {
        div -= 1.0;
    }
    if div != 0.0 {
        let mut fl = div.floor();
        if div - fl > 0.5 {
            fl += 1.0;
        }
        fl
    } else {
        (0.0f64).copysign(a / b)
    }
}

/// `int(x)` of an integral float, as an index
fn fidx(x: f64) -> i64 {
    x as i64
}

/// `memory_cell`
fn memory_cell(w: f64, h: f64, cell: f64) -> f64 {
    let mut c = 20.0f64.max(cell);
    while (w / c).ceil() * (h / c).ceil() > 40000.0 {
        c *= 2.0;
    }
    c
}

#[derive(Clone)]
struct Seg {
    x1: f64,
    y1: f64,
    x2: f64,
    y2: f64,
    blocks: bool,
}

fn hit(ox: f64, oy: f64, dx: f64, dy: f64, s: &Seg) -> f64 {
    let (ex, ey) = (s.x2 - s.x1, s.y2 - s.y1);
    let den = dx * ey - dy * ex;
    if -1e-12 < den && den < 1e-12 {
        return f64::INFINITY;
    }
    let (px, py) = (s.x1 - ox, s.y1 - oy);
    let t = (px * ey - py * ex) / den;
    let u = (px * dy - py * dx) / den;
    if t >= 0.0 && (0.0..=1.0).contains(&u) { t } else { f64::INFINITY }
}

/// `visibility_polygon`
fn visibility_polygon(ox: f64, oy: f64, segs: &[Seg], range: f64) -> Vec<(f64, f64)> {
    let (x0, y0, x1, y1) = (ox - range, oy - range, ox + range, oy + range);
    let mut cand: Vec<Seg> = segs.iter().filter(|s| s.blocks && !(s.x1.max(s.x2) < x0 || s.x1.min(s.x2) > x1 || s.y1.max(s.y2) < y0 || s.y1.min(s.y2) > y1)).cloned().collect();
    for (a, b, c, d) in [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)] {
        cand.push(Seg { x1: a, y1: b, x2: c, y2: d, blocks: true });
    }
    let mut seen: Vec<(f64, f64)> = vec![];
    let mut angles = vec![];
    for s in &cand {
        for (px, py) in [(s.x1, s.y1), (s.x2, s.y2)] {
            if seen.iter().any(|q| q.0 == px && q.1 == py) {
                continue;
            }
            seen.push((px, py));
            let a = (py - oy).atan2(px - ox);
            angles.extend([a - EPS, a, a + EPS]);
        }
    }
    for i in 0..ARC_STEPS {
        angles.push(-std::f64::consts::PI + 2.0 * std::f64::consts::PI * i as f64 / ARC_STEPS as f64);
    }
    let mut pts: Vec<(f64, f64, f64)> = angles.iter().map(|&a| {
        let (dx, dy) = (a.cos(), a.sin());
        let mut best = range;
        for c in &cand {
            let t = hit(ox, oy, dx, dy, c);
            if t < best {
                best = t;
            }
        }
        (a, ox + dx * best, oy + dy * best)
    }).collect();
    pts.sort_by(|p, q| p.partial_cmp(q).unwrap_or(std::cmp::Ordering::Equal));
    pts.into_iter().map(|(_, x, y)| (x, y)).collect()
}

/// `cells_in_polygon` (scanline, even-odd)
fn cells_in_polygon(poly: &[(f64, f64)], cell: f64, cols: i64, rows: i64, out: &mut BTreeSet<i64>) {
    let n = poly.len();
    if n < 3 {
        return;
    }
    let miny = poly.iter().map(|p| p.1).fold(f64::INFINITY, f64::min);
    let maxy = poly.iter().map(|p| p.1).fold(f64::NEG_INFINITY, f64::max);
    let (r0, r1) = (0.max(fidx(py_floordiv(miny, cell))), (rows - 1).min(fidx(py_floordiv(maxy, cell))));
    for r in r0..=r1 {
        let cy = (r as f64 + 0.5) * cell;
        let mut xs = vec![];
        for i in 0..n {
            let ((ax, ay), (bx, by)) = (poly[i], poly[(i + 1) % n]);
            if (ay <= cy && cy < by) || (by <= cy && cy < ay) {
                xs.push(ax + (cy - ay) / (by - ay) * (bx - ax));
            }
        }
        xs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mut k = 0;
        while k + 1 < xs.len() {
            let c0 = 0.max((xs[k] / cell - 0.5).ceil() as i64);
            let c1 = (cols - 1).min((xs[k + 1] / cell - 0.5).floor() as i64);
            for c in c0..=c1 {
                out.insert(r * cols + c);
            }
            k += 2;
        }
    }
}

pub fn to_runs(cells: &BTreeSet<i64>) -> Vec<i64> {
    let mut runs = vec![];
    let (mut prev, mut start): (Option<i64>, i64) = (None, 0);
    for &i in cells {
        if prev.is_some_and(|p| i == p + 1) {
            prev = Some(i);
            continue;
        }
        if let Some(p) = prev {
            runs.extend([start, p - start + 1]);
        }
        start = i;
        prev = Some(i);
    }
    if let Some(p) = prev {
        runs.extend([start, p - start + 1]);
    }
    runs
}

/// A JSON number Python's `_num` takes (finite, not a bool)
fn num(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}

pub struct View {
    pub cell: f64,
    pub cols: i64,
    pub rows: i64,
    pub visible: BTreeSet<i64>,
    pub explored: BTreeSet<i64>,
    pub changed: bool,
}

impl View {
    /// `touches(x0, y0, x1, y1, cells)`: does the box overlap a known (or the given) cell
    pub fn touches(&self, x0: f64, y0: f64, x1: f64, y1: f64, only_visible: bool) -> bool {
        if !(x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite()) {
            return false;
        }
        let cells = if only_visible { &self.visible } else { &self.explored }; // explored already includes the visible cells
        let (c0, c1) = (0.max(fidx(py_floordiv(x0, self.cell))), (self.cols - 1).min(fidx(py_floordiv(x1, self.cell))));
        let (r0, r1) = (0.max(fidx(py_floordiv(y0, self.cell))), (self.rows - 1).min(fidx(py_floordiv(y1, self.cell))));
        if c1 < c0 || r1 < r0 {
            return false;
        }
        cells.iter().any(|i| (c0..=c1).contains(&(i % self.cols)) && (r0..=r1).contains(&(i / self.cols)))
    }

    pub fn runs(&self) -> Value {
        json!({"cell": self.cell, "cols": self.cols, "rows": self.rows, "runs": to_runs(&self.explored)})
    }
}

/// `map_walls.segments` of the GM's stored walls; None for a wall Python would read its own way (or fail on)
fn segments(walls: &Value) -> Option<Vec<Seg>> {
    let mut out = vec![];
    for w in walls.as_array()? {
        let w = w.as_object()?;
        let pts: &[Value] = match w.get("pts") {
            None | Some(Value::Null) => &[],
            Some(Value::Array(a)) => a,
            _ => return None,
        };
        let mut kind = match w.get("kind") {
            None => "wall",
            Some(Value::String(k)) => k.as_str(),
            _ => return None,
        };
        let state = match w.get("state") {
            None => "closed",
            Some(Value::String(t)) => t.as_str(),
            Some(_) => "", // not "open": a door that blocks
        };
        if kind == "secret" {
            kind = if state == "open" { "door" } else { "wall" };
        }
        let blocks = match kind {
            "window" => false,
            "door" => state != "open",
            _ => true,
        };
        let mut xy = vec![];
        for p in pts {
            let p = p.as_array().filter(|p| p.len() >= 2)?;
            xy.push((num(p.first())?, num(p.get(1))?));
        }
        for pair in xy.windows(2) {
            out.push(Seg { x1: pair[0].0, y1: pair[0].1, x2: pair[1].0, y2: pair[1].1, blocks });
        }
    }
    Some(out)
}

/// `strict_view` / `compute`: the View for the map's character tokens and walls, merged with the stored explored record, and the record
/// to store when it changed (None for stored data Python would read its own way)
pub fn compute(sc: &Map<String, Value>, elements: &[Value], fog: &Map<String, Value>) -> Option<(View, Option<String>)> {
    let load = |col: &str| -> Option<Value> {
        let raw = sc.get(col).and_then(|v| v.as_str()).unwrap_or("");
        if raw.is_empty() {
            return Some(Value::Null);
        }
        match serde_json::from_str::<Value>(raw) {
            Ok(v) => Some(v),
            Err(_) if raw.contains("NaN") || raw.contains("Infinity") => None,
            Err(_) => Some(Value::Null),
        }
    };
    let dim = |k: &str, d: f64| sc.get(k).and_then(|v| v.as_i64()).filter(|v| *v != 0).map_or(d, |v| v as f64);
    let (cw, ch) = (dim("canvas_width", 2000.0), dim("canvas_height", 1500.0));
    // schematic_payload.grid_cell
    let grid_cfg = load("grid_config_json")?;
    let grid_type = sc.get("grid_type").and_then(|v| v.as_str()).filter(|t| !t.is_empty()).unwrap_or("none");
    let gc = match grid_cfg.get("cell_size") {
        Some(Value::Bool(_)) => return None,
        v => num(v).filter(|c| *c > 0.0 && grid_type != "none").unwrap_or(50.0),
    };
    let cell = memory_cell(cw, ch, gc);
    let (cols, rows) = ((cw / cell).ceil() as i64, (ch / cell).ceil() as i64);
    let squares = fog.get("range").and_then(|v| v.as_i64()).filter(|r| *r != 0).map_or(MAX_VIEW_SQUARES, |r| r as f64);
    let reach = squares.min(MAX_VIEW_SQUARES) * gc;
    let walls = match load("walls_json")? {
        Value::Array(a) => Value::Array(a),
        _ => json!([]),
    };
    let segs = segments(&walls)?;
    let mut visible = BTreeSet::new();
    for e in elements {
        let Some(e) = e.as_object() else { continue };
        if e.get("type").and_then(|v| v.as_str()) != Some("token") || !crate::edits::py_truthy_pub(e.get("pc_id")) {
            continue;
        }
        let (Some(x), Some(y)) = (num(e.get("x")), num(e.get("y"))) else { continue };
        cells_in_polygon(&visibility_polygon(x, y, &segs, reach), cell, cols, rows, &mut visible);
        let (c, r) = (fidx(py_floordiv(x, cell)), fidx(py_floordiv(y, cell)));
        if (0..cols).contains(&c) && (0..rows).contains(&r) {
            visible.insert(r * cols + c);
        }
    }
    let stored = match load("explored_json")? {
        Value::Object(o) => Some(o),
        _ => None,
    };
    let mut explored = BTreeSet::new();
    let mut valid = false;
    if let Some(st) = &stored {
        let same_num = |k: &str, want: f64| match st.get(k) {
            Some(Value::Bool(_)) => None,
            v => Some(num(v) == Some(want)),
        };
        let epoch = fog.get("epoch")?.as_i64()? as f64;
        if same_num("epoch", epoch)? && same_num("cell", cell)? && same_num("cols", cols as f64)? && same_num("rows", rows as f64)? {
            valid = true;
            let size = cols * rows;
            if let Some(Value::Array(runs)) = st.get("runs") {
                let mut i = 0;
                while i + 1 < runs.len() {
                    if let (Some(s), Some(n)) = (runs[i].as_i64().filter(|_| runs[i].is_i64()), runs[i + 1].as_i64().filter(|_| runs[i + 1].is_i64())) {
                        if s >= 0 && n > 0 {
                            for k in s..size.min(s.saturating_add(n)) {
                                explored.insert(k);
                            }
                        }
                    }
                    i += 2;
                }
            }
        }
    }
    let changed = (!valid && !visible.is_empty()) || !visible.is_subset(&explored);
    explored.extend(visible.iter().copied());
    let view = View { cell, cols, rows, visible, explored, changed };
    let record = changed.then(|| nd_render::py_dumps(&json!({"epoch": fog.get("epoch"), "cell": cell, "cols": cols, "rows": rows, "runs": to_runs(&view.explored)})));
    Some((view, record))
}

/// `element_box`: a generous box around an element, or None when it cannot be told (such an element is not sent)
fn element_box(e: &Map<String, Value>) -> Option<(f64, f64, f64, f64)> {
    let g = |k: &str| num(e.get(k));
    match e.get("type").and_then(|v| v.as_str())? {
        t @ ("rect" | "image") => {
            let (x, y, w, h) = (g("x")?, g("y")?, g("w")?, g("h")?);
            if t == "image" && crate::edits::py_truthy_pub(e.get("rot")) {
                let pad = w.abs() + h.abs();
                return Some((x - pad, y - pad, x + w + pad, y + h + pad));
            }
            Some((x.min(x + w), y.min(y + h), x.max(x + w), y.max(y + h)))
        }
        "circle" => {
            let (cx, cy, rx, ry) = (g("cx")?, g("cy")?, g("rx")?, g("ry")?);
            Some((cx - rx.abs(), cy - ry.abs(), cx + rx.abs(), cy + ry.abs()))
        }
        t @ ("line" | "arrow" | "measure" | "aoe") => {
            let (x1, y1, x2, y2) = (g("x1")?, g("y1")?, g("x2")?, g("y2")?);
            let pad = if t == "aoe" { (x2 - x1).hypot(y2 - y1) } else { 0.0 };
            Some((x1.min(x2) - pad, y1.min(y2) - pad, x1.max(x2) + pad, y1.max(y2) + pad))
        }
        "poly" | "path" => {
            let pts = match e.get("points").filter(|v| crate::edits::py_truthy_pub(Some(v))).or(e.get("pts")) {
                Some(Value::Array(a)) => a,
                _ => return None,
            };
            let (mut xs, mut ys) = (vec![], vec![]);
            for p in pts {
                let Some(p) = p.as_array().filter(|p| p.len() >= 2) else { continue };
                xs.push(num(p.first())?);
                ys.push(num(p.get(1))?);
            }
            if xs.is_empty() {
                return None;
            }
            let mn = |v: &[f64]| v.iter().copied().fold(f64::INFINITY, f64::min);
            let mx = |v: &[f64]| v.iter().copied().fold(f64::NEG_INFINITY, f64::max);
            Some((mn(&xs), mn(&ys), mx(&xs), mx(&ys)))
        }
        t @ ("text" | "pin" | "token") => {
            let (x, y) = (g("x")?, g("y")?);
            let r = if t == "token" { 40.0f64.max(num(e.get("r")).filter(|r| *r != 0.0).unwrap_or(20.0) + 4.0) } else { 200.0 };
            Some((x - r, y - r, x + r, y + r))
        }
        _ => None,
    }
}

/// `filter_elements`: a character's token always, a creature only while it stands in a square a character sees now, anything else when
/// its box touches a known square
pub fn filter_elements(elements: Vec<Value>, view: &View) -> Vec<Value> {
    elements.into_iter().filter(|e| {
        let Some(e) = e.as_object() else { return false };
        if e.get("type").and_then(|v| v.as_str()) == Some("token") {
            if crate::edits::py_truthy_pub(e.get("pc_id")) {
                return true;
            }
            return matches!((num(e.get("x")), num(e.get("y"))), (Some(x), Some(y)) if view.touches(x, y, x, y, true));
        }
        element_box(e).is_some_and(|(a, b, c, d)| view.touches(a, b, c, d, false))
    }).collect()
}

/// `filter_walls` (None for points Python would read its own way)
pub fn filter_walls(walls: Vec<Value>, view: &View) -> Option<Vec<Value>> {
    let mut out = vec![];
    for w in walls {
        let pts: Vec<Value> = match w.get("pts") {
            None => vec![],
            Some(Value::Array(a)) => a.clone(),
            _ => return None,
        };
        let mut xs = vec![];
        let mut ys = vec![];
        for p in &pts {
            let p = p.as_array().filter(|p| !p.is_empty())?;
            xs.push(num(p.first())?);
            ys.push(num(p.get(1))?);
        }
        if xs.is_empty() {
            continue;
        }
        let mn = |v: &[f64]| v.iter().copied().fold(f64::INFINITY, f64::min);
        let mx = |v: &[f64]| v.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        if view.touches(mn(&xs) - 1.0, mn(&ys) - 1.0, mx(&xs) + 1.0, mx(&ys) + 1.0, false) {
            out.push(w);
        }
    }
    Some(out)
}

pub fn filter_lights(lights: Vec<Value>, view: &View) -> Vec<Value> {
    lights.into_iter().filter(|l| matches!((l["x"].as_f64(), l["y"].as_f64()), (Some(x), Some(y)) if view.touches(x, y, x, y, false))).collect()
}

/// The masked picture's address: `/maps/schematic/<slug>/bg.webp?v=<sha1 of the explored runs>`
pub fn masked_image_url(slug: &str, runs: &Value) -> String {
    use sha1::{Digest, Sha1};
    let hex: String = Sha1::digest(nd_render::py_dumps(runs).as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
    format!("/maps/schematic/{slug}/bg.webp?v={}", &hex[..10])
}
