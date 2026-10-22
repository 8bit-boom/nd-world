//! Rooms found in a map's walls (a port of app/rooms_from_walls.py): the map is cut into grid squares, two neighbours share a room
//! unless a wall runs between their centres, each connected patch is a room (the big open outside is not), and doors / secret doors /
//! windows between two areas become the room tool's marks.

use crate::strict::py_floordiv;
use serde_json::{json, Value};
use std::collections::HashMap;

const MAX_SPACES: usize = 80;
const MIN_ROOM_SQUARES: usize = 2;

fn orient(px: f64, py: f64, qx: f64, qy: f64, rx: f64, ry: f64) -> f64 {
    (qx - px) * (ry - py) - (qy - py) * (rx - px)
}

/// `_cross`: do segments ab and cd properly cross
#[allow(clippy::too_many_arguments)]
fn cross(ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64, dx: f64, dy: f64) -> bool {
    let (d1, d2) = (orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by));
    let (d3, d4) = (orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy));
    ((d1 > 0.0) != (d2 > 0.0)) && ((d3 > 0.0) != (d4 > 0.0)) && d1 != 0.0 && d2 != 0.0 && d3 != 0.0 && d4 != 0.0
}

/// Python's `a % b` for a positive `b` (never -0.0)
fn py_mod(a: f64, b: f64) -> f64 {
    let m = a.rem_euclid(b);
    if m == 0.0 { 0.0 } else { m }
}

pub enum Detected {
    Rooms(Value, Value),
    Refused,
}

/// `detect(walls, canvas_w, canvas_h, cell, ox, oy)` on walls already cleaned (map_walls.clean_walls): (state, info), or Refused for
/// Python's 422 (too many squares, no walls, no enclosed room)
pub fn detect(walls: &[Value], canvas_w: f64, canvas_h: f64, cell: f64, ox: f64, oy: f64) -> Option<Detected> {
    let (ox, oy) = (py_mod(ox, cell), py_mod(oy, cell));
    let (cols, rows) = (py_floordiv(canvas_w - ox, cell) as i64, py_floordiv(canvas_h - oy, cell) as i64);
    if !((1..=400).contains(&cols) && (1..=400).contains(&rows) && cols * rows <= 40000) {
        return Some(Detected::Refused);
    }
    let mut segs: Vec<(f64, f64, f64, f64, String)> = vec![];
    for w in walls {
        let kind = w.get("kind")?.as_str()?.to_string();
        let pts = w.get("pts")?.as_array()?;
        for pair in pts.windows(2) {
            let (a, b) = (pair[0].as_array()?, pair[1].as_array()?);
            segs.push((a[0].as_f64()?, a[1].as_f64()?, b[0].as_f64()?, b[1].as_f64()?, kind.clone()));
        }
    }
    if segs.is_empty() {
        return Some(Detected::Refused);
    }
    let mut near: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, (x1, y1, x2, y2, _)) in segs.iter().enumerate() {
        let (c0, c1) = (py_floordiv(x1.min(*x2) - ox, cell) as i64 - 1, py_floordiv(x1.max(*x2) - ox, cell) as i64 + 1);
        let (r0, r1) = (py_floordiv(y1.min(*y2) - oy, cell) as i64 - 1, py_floordiv(y1.max(*y2) - oy, cell) as i64 + 1);
        for r in 0.max(r0)..=(rows - 1).min(r1) {
            for c in 0.max(c0)..=(cols - 1).min(c1) {
                near.entry(r * cols + c).or_default().push(i);
            }
        }
    }
    let centre = |c: i64, r: i64| (ox + (c as f64 + 0.5) * cell, oy + (r as f64 + 0.5) * cell);
    let blocked = |c0: i64, r0: i64, c1: i64, r1: i64| -> bool {
        let ((ax, ay), (bx, by)) = (centre(c0, r0), centre(c1, r1));
        near.get(&(r0 * cols + c0)).is_some_and(|list| list.iter().any(|&i| {
            let (x1, y1, x2, y2, _) = &segs[i];
            cross(ax, ay, bx, by, *x1, *y1, *x2, *y2)
        }))
    };
    let total = (cols * rows) as usize;
    let mut comp = vec![-1i64; total];
    let (mut sizes, mut border_of): (Vec<usize>, Vec<bool>) = (vec![], vec![]);
    for start in 0..total {
        if comp[start] != -1 {
            continue;
        }
        let n = sizes.len() as i64;
        comp[start] = n;
        let (mut stack, mut size, mut border) = (vec![start as i64], 0usize, false);
        while let Some(cur) = stack.pop() {
            let (c, r) = (cur % cols, cur / cols);
            size += 1;
            if c == 0 || c == cols - 1 || r == 0 || r == rows - 1 {
                border = true;
            }
            for (dc, dr) in [(1, 0), (-1, 0), (0, 1), (0, -1)] {
                let (nc, nr) = (c + dc, r + dr);
                if (0..cols).contains(&nc) && (0..rows).contains(&nr) && comp[(nr * cols + nc) as usize] == -1 && !blocked(c, r, nc, nr) {
                    comp[(nr * cols + nc) as usize] = n;
                    stack.push(nr * cols + nc);
                }
            }
        }
        sizes.push(size);
        border_of.push(border);
    }
    let outside: Vec<usize> = (0..sizes.len()).filter(|&n| border_of[n] && sizes[n] * 2 > total).collect();
    let mut keep: Vec<usize> = (0..sizes.len()).filter(|n| !outside.contains(n) && sizes[*n] >= MIN_ROOM_SQUARES).collect();
    keep.sort_by_key(|&n| std::cmp::Reverse(sizes[n])); // stable, as Python's sorted
    let mut notes = vec![];
    if keep.len() > MAX_SPACES {
        notes.push(format!("Only the {MAX_SPACES} largest of {} areas were kept.", keep.len()));
        keep.truncate(MAX_SPACES);
    }
    if keep.is_empty() {
        return Some(Detected::Refused);
    }
    let mut sorted_keep = keep.clone();
    sorted_keep.sort();
    let sid: HashMap<usize, i64> = sorted_keep.iter().enumerate().map(|(i, &n)| (n, i as i64 + 1)).collect();
    let ids: Vec<i64> = (0..total).map(|i| *sid.get(&(comp[i] as usize)).unwrap_or(&0)).collect();
    let mut runs: Vec<i64> = vec![];
    for &v in &ids {
        if runs.len() >= 2 && runs[runs.len() - 2] == v {
            *runs.last_mut()? += 1;
        } else {
            runs.extend([v, 1]);
        }
    }
    let space_at = |c: i64, r: i64| if (0..cols).contains(&c) && (0..rows).contains(&r) { ids[(r * cols + c) as usize] } else { 0 };
    let (mut marks, mut seen): (Vec<Value>, Vec<(i64, i64, &str)>) = (vec![], vec![]);
    for (x1, y1, x2, y2, kind) in &segs {
        if kind == "wall" {
            continue;
        }
        let (mx, my) = (((x1 + x2) / 2.0 - ox) / cell, ((y1 + y2) / 2.0 - oy) / cell);
        let (ex, ey, axis, sides) = if (x2 - x1).abs() >= (y2 - y1).abs() {
            let (ex, ey) = (mx.floor() as i64, my.round_ties_even() as i64);
            (ex, ey, "h", (space_at(ex, ey - 1), space_at(ex, ey)))
        } else {
            let (ex, ey) = (mx.round_ties_even() as i64, my.floor() as i64);
            (ex, ey, "v", (space_at(ex - 1, ey), space_at(ex, ey)))
        };
        if sides.0 == sides.1 || !((0..=cols).contains(&ex) && (0..=rows).contains(&ey)) || (axis == "h" && ex >= cols) || (axis == "v" && ey >= rows) {
            continue;
        }
        if seen.contains(&(ex, ey, axis)) {
            continue;
        }
        seen.push((ex, ey, axis));
        marks.push(json!([ex, ey, axis, kind]));
    }
    let spaces: Vec<Value> = sorted_keep.iter().map(|n| json!({"id": sid[n], "name": format!("Room {}", sid[n]), "kind": "other"})).collect();
    let r2 = |x: f64| crate::edits::py_round_pub(x, 2);
    let state = json!({"cell": r2(cell)?, "ox": r2(ox)?, "oy": r2(oy)?, "cols": cols, "rows": rows, "cells": runs, "spaces": spaces, "marks": marks, "clear": true});
    let doors = marks.iter().filter(|m| matches!(m[3].as_str(), Some("door" | "secret"))).count();
    let windows = marks.iter().filter(|m| m[3] == "window").count();
    let info = json!({"rooms": keep.len(), "doors": doors, "windows": windows, "outside_squares": outside.iter().map(|&n| sizes[n]).sum::<usize>(), "notes": notes});
    Some(Detected::Rooms(state, info))
}
