//! Pure, bounded schematic-map physics.
//!
//! The database stores only the normalized v1 state.  Room boundaries and
//! doors/windows come from the map's existing `rooms_json` / `walls_json`
//! columns, so the graph is derived again for every read and tick.  There is
//! deliberately no thread, timer or fluid-particle runtime here.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};

pub const SCHEMA_VERSION: i64 = 1;
pub const MAX_ROOM_VALUE: i64 = 100;
pub const MIN_STEPS: i64 = 1;
pub const MAX_STEPS: i64 = 16;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoomPhysics {
    pub water: i64,
    pub fire: i64,
    pub gas: i64,
    pub heat: i64,
    pub pressure: i64,
}

impl Default for RoomPhysics {
    fn default() -> Self {
        Self { water: 0, fire: 0, gas: 0, heat: 0, pressure: 0 }
    }
}

impl RoomPhysics {
    fn integer(v: Option<&Value>) -> Result<Option<i64>, String> {
        match v {
            None | Some(Value::Null) => Ok(None),
            Some(Value::Number(n)) if n.is_i64() => Ok(n.as_i64()),
            Some(Value::Number(n)) if n.is_u64() => n.as_u64()
                .and_then(|n| i64::try_from(n).ok())
                .map(Some)
                .ok_or_else(|| "physics values must be whole numbers".to_string()),
            Some(_) => Err("physics values must be whole numbers".to_string()),
        }
    }

    fn field(v: Option<&Value>) -> Result<i64, String> {
        Ok(Self::integer(v)?.unwrap_or(0).clamp(0, MAX_ROOM_VALUE))
    }

    pub fn normalize_value(value: &Value) -> Result<Self, String> {
        let object = value.as_object().ok_or_else(|| "room physics must be an object".to_string())?;
        Ok(Self {
            water: Self::field(object.get("water"))?,
            fire: Self::field(object.get("fire"))?,
            gas: Self::field(object.get("gas"))?,
            heat: Self::field(object.get("heat"))?,
            pressure: Self::field(object.get("pressure"))?,
        })
    }

    pub fn apply_patch(&mut self, patch: &Map<String, Value>) -> Result<(), String> {
        for (key, field) in [
            ("water", &mut self.water),
            ("fire", &mut self.fire),
            ("gas", &mut self.gas),
            ("heat", &mut self.heat),
            ("pressure", &mut self.pressure),
        ] {
            if let Some(v) = patch.get(key) {
                *field = match v {
                    Value::Number(n) if n.is_i64() => n.as_i64().unwrap_or(0).clamp(0, MAX_ROOM_VALUE),
                    Value::Number(n) if n.is_u64() => i64::try_from(n.as_u64().unwrap_or(0))
                        .unwrap_or(MAX_ROOM_VALUE)
                        .clamp(0, MAX_ROOM_VALUE),
                    _ => return Err(format!("{key} must be a whole number")),
                };
            }
        }
        Ok(())
    }

    pub fn to_value(&self) -> Value {
        json!({
            "water": self.water,
            "fire": self.fire,
            "gas": self.gas,
            "heat": self.heat,
            "pressure": self.pressure
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PhysicsState {
    pub version: i64,
    pub enabled: bool,
    pub revision: i64,
    pub tick: i64,
    pub rooms: BTreeMap<String, RoomPhysics>,
}

impl Default for PhysicsState {
    fn default() -> Self {
        Self { version: SCHEMA_VERSION, enabled: false, revision: 0, tick: 0, rooms: BTreeMap::new() }
    }
}

fn nonnegative_i64(v: Option<&Value>) -> i64 {
    match v {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64().unwrap_or(0).max(0),
        Some(Value::Number(n)) if n.is_u64() => i64::try_from(n.as_u64().unwrap_or(0)).unwrap_or(i64::MAX).max(0),
        _ => 0,
    }
}

fn room_id(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => {
            let s = s.trim();
            (!s.is_empty()).then(|| s.to_string())
        }
        Value::Number(n) if n.is_i64() => n.as_i64().map(|n| n.to_string()),
        Value::Number(n) if n.is_u64() => n.as_u64().map(|n| n.to_string()),
        _ => None,
    }
}

impl PhysicsState {
    pub fn normalize_value(value: &Value) -> Result<Self, String> {
        let object = value.as_object().ok_or_else(|| "physics_json must be an object".to_string())?;
        if let Some(v) = object.get("version") {
            match v {
                Value::Number(n) if n.as_i64() == Some(SCHEMA_VERSION) => {}
                _ => return Err("unsupported physics schema version".to_string()),
            }
        }
        let enabled = match object.get("enabled") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(v)) => *v,
            _ => return Err("enabled must be true or false".to_string()),
        };
        let mut rooms = BTreeMap::new();
        if let Some(Value::Object(map)) = object.get("rooms") {
            for (id, value) in map {
                if id.trim().is_empty() {
                    continue;
                }
                rooms.insert(id.clone(), RoomPhysics::normalize_value(value)?);
            }
        } else if let Some(Value::Array(list)) = object.get("rooms") {
            for item in list {
                let object = item.as_object().ok_or_else(|| "room physics entries must be objects".to_string())?;
                let id = object.get("id").or_else(|| object.get("room_id")).and_then(room_id)
                    .ok_or_else(|| "room physics entries need an id".to_string())?;
                rooms.insert(id, RoomPhysics::normalize_value(item)?);
            }
        }
        Ok(Self {
            version: SCHEMA_VERSION,
            enabled,
            revision: nonnegative_i64(object.get("revision").or_else(|| object.get("rev"))),
            tick: nonnegative_i64(object.get("tick")),
            rooms,
        })
    }

    pub fn normalize_json(raw: Option<&str>) -> Result<Self, String> {
        let Some(raw) = raw.filter(|raw| !raw.trim().is_empty()) else {
            return Ok(Self::default());
        };
        let value: Value = serde_json::from_str(raw).map_err(|_| "physics_json is invalid".to_string())?;
        Self::normalize_value(&value)
    }

    pub fn safe_normalize_json(raw: Option<&str>) -> Self {
        Self::normalize_json(raw).unwrap_or_default()
    }

    pub fn apply_rooms(&mut self, value: &Value, known: &BTreeSet<String>) -> Result<(), String> {
        let mut patches: Vec<(String, Value)> = vec![];
        match value {
            Value::Object(map) => {
                for (id, patch) in map {
                    patches.push((id.clone(), patch.clone()));
                }
            }
            Value::Array(list) => {
                for item in list {
                    let object = item.as_object().ok_or_else(|| "room physics entries must be objects".to_string())?;
                    let id = object.get("id").or_else(|| object.get("room_id")).and_then(room_id)
                        .ok_or_else(|| "room physics entries need an id".to_string())?;
                    patches.push((id, item.clone()));
                }
            }
            _ => return Err("rooms must be an object or a list".to_string()),
        }
        for (id, patch) in patches {
            if !known.contains(&id) {
                return Err(format!("Unknown physics room id: {id}"));
            }
            let state = match patch.as_object() {
                Some(object) => object,
                None => return Err("room physics must be an object".to_string()),
            };
            let mut room = self.rooms.get(&id).cloned().unwrap_or_default();
            room.apply_patch(state)?;
            self.rooms.insert(id, room);
        }
        Ok(())
    }

    pub fn synchronize_rooms(&mut self, graph: &PhysicsGraph) {
        let known: BTreeSet<String> = graph.nodes.iter().map(|node| node.id.clone()).collect();
        self.rooms.retain(|id, _| known.contains(id));
        for id in known {
            self.rooms.entry(id).or_default();
        }
    }

    pub fn reset(&mut self, graph: &PhysicsGraph) {
        self.rooms.clear();
        for node in &graph.nodes {
            self.rooms.insert(node.id.clone(), RoomPhysics::default());
        }
        self.tick = 0;
        self.revision = self.revision.saturating_add(1);
    }

    pub fn bump_revision(&mut self) {
        self.revision = self.revision.saturating_add(1);
    }

    pub fn to_value(&self) -> Value {
        let mut rooms = Map::new();
        for (id, room) in &self.rooms {
            rooms.insert(id.clone(), room.to_value());
        }
        json!({
            "version": SCHEMA_VERSION,
            "enabled": self.enabled,
            "revision": self.revision,
            "tick": self.tick,
            "rooms": Value::Object(rooms)
        })
    }
}

#[derive(Clone, Debug)]
pub struct PhysicsNode {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl PhysicsNode {
    pub fn to_value(&self) -> Value {
        json!({
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "bounds": [self.x, self.y, self.w, self.h]
        })
    }
}

#[derive(Clone, Debug)]
pub struct PhysicsEdge {
    pub a: String,
    pub b: String,
    pub kind: String,
    pub open: bool,
}

impl PhysicsEdge {
    pub fn to_value(&self) -> Value {
        json!({
            "a": self.a,
            "b": self.b,
            "from": self.a,
            "to": self.b,
            "kind": self.kind,
            "open": self.open
        })
    }
}

#[derive(Clone, Debug, Default)]
pub struct PhysicsGraph {
    pub nodes: Vec<PhysicsNode>,
    pub edges: Vec<PhysicsEdge>,
}

impl PhysicsGraph {
    pub fn to_value(&self) -> Value {
        json!({
            "nodes": self.nodes.iter().map(PhysicsNode::to_value).collect::<Vec<_>>(),
            "edges": self.edges.iter().map(PhysicsEdge::to_value).collect::<Vec<_>>()
        })
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }
}

fn number(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64().filter(|n| n.is_finite()),
        _ => None,
    }
}

fn whole(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) if n.is_i64() => n.as_i64(),
        Some(Value::Number(n)) if n.is_u64() => i64::try_from(n.as_u64().unwrap_or(0)).ok(),
        _ => None,
    }
}

fn unpack_rooms(rooms: &Value) -> Option<(f64, f64, f64, i64, i64, Vec<i64>, BTreeMap<i64, (String, String)>)> {
    let object = rooms.as_object()?;
    if object.is_empty() {
        return None;
    }
    let cell = number(object.get("cell")).filter(|c| (8.0..=400.0).contains(c))?;
    let ox = number(object.get("ox")).unwrap_or(0.0).clamp(0.0, cell);
    let oy = number(object.get("oy")).unwrap_or(0.0).clamp(0.0, cell);
    let cols = whole(object.get("cols")).filter(|c| (1..=400).contains(c))?;
    let rows = whole(object.get("rows")).filter(|r| (1..=400).contains(r))?;
    if cols.checked_mul(rows)? > 40000 {
        return None;
    }
    let total = (cols * rows) as usize;
    let mut ids = vec![0i64; total];
    let raw = object.get("cells")?.as_array()?;
    if raw.len() % 2 != 0 {
        return None;
    }
    let mut pos = 0usize;
    for pair in raw.chunks(2) {
        let id = whole(pair.first()).unwrap_or(-1);
        let run = whole(pair.get(1)).unwrap_or(0);
        if id < 0 || run < 1 {
            return None;
        }
        for _ in 0..run {
            if pos >= total {
                return None;
            }
            ids[pos] = id;
            pos += 1;
        }
    }
    if pos != total {
        return None;
    }
    let mut spaces = BTreeMap::new();
    if let Some(Value::Array(list)) = object.get("spaces") {
        for item in list {
            let Some(item) = item.as_object() else { continue };
            let Some(id) = whole(item.get("id")) else { continue };
            let name = item.get("name").and_then(Value::as_str).unwrap_or("").trim();
            let name = if name.is_empty() { format!("Room {id}") } else { name.to_string() };
            let kind = item.get("kind").and_then(Value::as_str).unwrap_or("other");
            spaces.insert(id, (name, kind.to_string()));
        }
    }
    Some((cell, ox, oy, cols, rows, ids, spaces))
}

#[derive(Clone, Debug)]
struct WallSegment {
    kind: String,
    state: String,
    pts: Vec<(f64, f64)>,
}

fn parse_walls(walls: &Value) -> Vec<WallSegment> {
    let mut out = vec![];
    let Some(list) = walls.as_array() else { return out };
    for item in list {
        let Some(item) = item.as_object() else { continue };
        let kind = item.get("kind").and_then(Value::as_str).unwrap_or("wall").to_string();
        let state = item.get("state").and_then(Value::as_str).unwrap_or("closed").to_string();
        let Some(pts) = item.get("pts").and_then(Value::as_array) else { continue };
        let mut parsed = vec![];
        for point in pts {
            let Some(point) = point.as_array().filter(|p| p.len() >= 2) else { continue };
            let (Some(x), Some(y)) = (number(point.first()), number(point.get(1))) else { continue };
            parsed.push((x, y));
        }
        if parsed.len() >= 2 {
            out.push(WallSegment { kind, state, pts: parsed });
        }
    }
    out
}

fn overlap_on_axis(a: f64, b: f64, c: f64, d: f64) -> f64 {
    (a.max(b).min(c.max(d)) - a.min(b).max(c.min(d))).max(0.0)
}

fn segment_matches(a: (f64, f64), b: (f64, f64), c: (f64, f64), d: (f64, f64)) -> bool {
    let eps = 0.75;
    let abx = b.0 - a.0;
    let aby = b.1 - a.1;
    let cd = |p: (f64, f64)| (p.0 - c.0) * aby - (p.1 - c.1) * abx;
    let cdc = cd(c);
    let cdd = cd(d);
    if cdc.abs() > eps || cdd.abs() > eps {
        return false;
    }
    if abx.abs() >= aby.abs() {
        overlap_on_axis(a.0, b.0, c.0, d.0) >= eps
    } else {
        overlap_on_axis(a.1, b.1, c.1, d.1) >= eps
    }
}

fn wall_at_edge(walls: &[WallSegment], a: (f64, f64), b: (f64, f64)) -> Option<&WallSegment> {
    for wall in walls {
        for pair in wall.pts.windows(2) {
            if segment_matches(a, b, pair[0], pair[1]) {
                return Some(wall);
            }
        }
    }
    None
}

fn passable_wall(wall: Option<&WallSegment>) -> (bool, String) {
    let Some(wall) = wall else { return (false, "wall".to_string()) };
    match wall.kind.as_str() {
        "door" | "secret" | "window" => (
            matches!(wall.state.as_str(), "open" | "broken"),
            wall.kind.clone(),
        ),
        "wall" => (false, "wall".to_string()),
        _ => (false, "wall".to_string()),
    }
}

fn mark_edge(mark_kind: Option<&str>, wall: Option<&WallSegment>) -> (bool, String) {
    match mark_kind {
        Some("open") => (true, "open".to_string()),
        Some("door") | Some("secret") | Some("window") => passable_wall(wall),
        _ => passable_wall(wall),
    }
}

/// Derive the passable room graph from the stored room drawing and wall state.
pub fn derive_graph(rooms: &Value, walls: &Value) -> PhysicsGraph {
    let mut graph = PhysicsGraph::default();
    let Some((cell, ox, oy, cols, rows, ids, spaces)) = unpack_rooms(rooms) else {
        return graph;
    };
    let used: BTreeSet<i64> = ids.iter().copied().filter(|id| *id != 0).collect();
    for id in &used {
        let (name, kind) = spaces.get(id).cloned().unwrap_or_else(|| (format!("Room {id}"), "other".to_string()));
        let mut min_c = cols;
        let mut max_c = -1;
        let mut min_r = rows;
        let mut max_r = -1;
        for y in 0..rows {
            for x in 0..cols {
                if ids[(y * cols + x) as usize] == *id {
                    min_c = min_c.min(x);
                    max_c = max_c.max(x);
                    min_r = min_r.min(y);
                    max_r = max_r.max(y);
                }
            }
        }
        if max_c >= min_c && max_r >= min_r {
            let x = ox + min_c as f64 * cell;
            let y = oy + min_r as f64 * cell;
            graph.nodes.push(PhysicsNode {
                id: id.to_string(),
                name,
                kind,
                x,
                y,
                w: (max_c - min_c + 1) as f64 * cell,
                h: (max_r - min_r + 1) as f64 * cell,
            });
        }
    }
    graph.nodes.sort_by(|a, b| a.id.cmp(&b.id));

    let mut marks = BTreeMap::new();
    if let Some(list) = rooms.as_object().and_then(|o| o.get("marks")).and_then(Value::as_array) {
        for mark in list {
            let Some(mark) = mark.as_array().filter(|m| m.len() == 4) else { continue };
            let (Some(x), Some(y)) = (whole(mark.first()), whole(mark.get(1))) else { continue };
            let (Some(axis), Some(kind)) = (mark[2].as_str(), mark[3].as_str()) else { continue };
            if matches!(axis, "h" | "v") {
                marks.insert((x, y, axis.to_string()), kind.to_string());
            }
        }
    }
    let walls = parse_walls(walls);
    let mut edges = BTreeSet::new();
    for y in 0..rows {
        for x in 0..cols {
            let id = ids[(y * cols + x) as usize];
            if id == 0 {
                continue;
            }
            if x + 1 < cols {
                let other = ids[(y * cols + x + 1) as usize];
                if other != 0 && other != id {
                    let a = (ox + (x + 1) as f64 * cell, oy + y as f64 * cell);
                    let b = (ox + (x + 1) as f64 * cell, oy + (y + 1) as f64 * cell);
                    let mk = marks.get(&(x + 1, y, "v".to_string())).map(String::as_str);
                    let wall = wall_at_edge(&walls, a, b);
                    let (open, kind) = mark_edge(mk, wall);
                    if open {
                        let (low, high) = if id < other { (id.to_string(), other.to_string()) } else { (other.to_string(), id.to_string()) };
                        edges.insert((low, high, kind));
                    }
                }
            }
            if y + 1 < rows {
                let other = ids[((y + 1) * cols + x) as usize];
                if other != 0 && other != id {
                    let a = (ox + x as f64 * cell, oy + (y + 1) as f64 * cell);
                    let b = (ox + (x + 1) as f64 * cell, oy + (y + 1) as f64 * cell);
                    let mk = marks.get(&(x, y + 1, "h".to_string())).map(String::as_str);
                    let wall = wall_at_edge(&walls, a, b);
                    let (open, kind) = mark_edge(mk, wall);
                    if open {
                        let (low, high) = if id < other { (id.to_string(), other.to_string()) } else { (other.to_string(), id.to_string()) };
                        edges.insert((low, high, kind));
                    }
                }
            }
        }
    }
    for (a, b, kind) in edges {
        graph.edges.push(PhysicsEdge { a, b, kind, open: true });
    }
    graph
}

pub fn derive_graph_json(rooms_raw: Option<&str>, walls_raw: Option<&str>) -> PhysicsGraph {
    fn parse(raw: Option<&str>) -> Value {
        raw.and_then(|raw| serde_json::from_str(raw).ok()).unwrap_or(json!({}))
    }
    let rooms = parse(rooms_raw);
    let walls = parse(walls_raw);
    let rooms = if rooms.is_object() { rooms } else { json!({}) };
    let walls = if walls.is_array() { walls } else { json!([]) };
    derive_graph(&rooms, &walls)
}

/// Normalize the public route's 1-16 step parameter.
pub fn normalize_steps(value: &Value) -> Result<i64, String> {
    match value {
        Value::Number(n) if n.is_i64() => {
            let n = n.as_i64().unwrap_or(0);
            if (MIN_STEPS..=MAX_STEPS).contains(&n) { Ok(n) } else { Err(format!("steps must be between {MIN_STEPS} and {MAX_STEPS}")) }
        }
        _ => Err(format!("steps must be an integer between {MIN_STEPS} and {MAX_STEPS}")),
    }
}

pub fn normalize_steps_i64(value: i64) -> Result<i64, String> {
    normalize_steps(&json!(value))
}

pub fn clamp_steps(value: i64) -> i64 {
    value.clamp(MIN_STEPS, MAX_STEPS)
}

fn trunc_div(a: i64, b: i64) -> i64 {
    a / b
}

fn field_at(room: &RoomPhysics, field: &str) -> i64 {
    match field {
        "water" => room.water,
        "fire" => room.fire,
        "gas" => room.gas,
        "heat" => room.heat,
        "pressure" => room.pressure,
        _ => 0,
    }
}

fn set_field(room: &mut RoomPhysics, field: &str, value: i64) {
    let value = value.clamp(0, MAX_ROOM_VALUE);
    match field {
        "water" => room.water = value,
        "fire" => room.fire = value,
        "gas" => room.gas = value,
        "heat" => room.heat = value,
        "pressure" => room.pressure = value,
        _ => {}
    }
}

fn tick_field(state: &mut PhysicsState, graph: &PhysicsGraph, field: &str) {
    let mut diff: BTreeMap<String, i64> = state.rooms.keys().map(|id| (id.clone(), 0)).collect();
    let divisor = if field == "fire" { 6 } else { 4 };
    for edge in &graph.edges {
        if !edge.open {
            continue;
        }
        let (Some(a), Some(b)) = (state.rooms.get(&edge.a), state.rooms.get(&edge.b)) else { continue };
        let va = field_at(a, field);
        let vb = field_at(b, field);
        let delta = trunc_div(va - vb, divisor);
        if delta != 0 {
            *diff.entry(edge.a.clone()).or_insert(0) -= delta;
            *diff.entry(edge.b.clone()).or_insert(0) += delta;
        }
    }
    if field == "fire" {
        for (id, room) in &state.rooms {
            if room.fire > 0 {
                *diff.entry(id.clone()).or_insert(0) -= 2;
            }
        }
    }
    let mut next = state.rooms.clone();
    for (id, room) in &mut next {
        let value = field_at(room, field).saturating_add(*diff.get(id).unwrap_or(&0));
        set_field(room, field, value);
    }
    state.rooms = next;
}

/// Run a deterministic simultaneous tick for 1-16 bounded steps.
pub fn tick(state: &mut PhysicsState, graph: &PhysicsGraph, steps: i64) -> Result<(), String> {
    let steps = normalize_steps_i64(steps)?;
    state.synchronize_rooms(graph);
    for _ in 0..steps {
        for field in ["water", "fire", "gas", "heat", "pressure"] {
            tick_field(state, graph, field);
        }
        state.tick = state.tick.saturating_add(1);
    }
    state.bump_revision();
    Ok(())
}

pub fn tick_steps(state: &mut PhysicsState, graph: &PhysicsGraph, steps: i64) -> Result<(), String> {
    tick(state, graph, steps)
}

pub fn tick_physics(state: &mut PhysicsState, graph: &PhysicsGraph, steps: i64) -> Result<(), String> {
    tick(state, graph, steps)
}

pub fn normalize(raw: Option<&str>) -> Result<PhysicsState, String> {
    PhysicsState::normalize_json(raw)
}

pub fn normalize_v1(raw: Option<&str>) -> Result<PhysicsState, String> {
    PhysicsState::normalize_json(raw)
}

pub fn normalize_physics_json(raw: Option<&str>) -> Result<PhysicsState, String> {
    PhysicsState::normalize_json(raw)
}

pub fn to_value(state: &PhysicsState, graph: &PhysicsGraph) -> Value {
    let mut root = state.to_value().as_object().cloned().unwrap_or_default();
    root.insert("graph".into(), graph.to_value());
    Value::Object(root)
}

pub fn player_overlay(state: &PhysicsState, graph: &PhysicsGraph, allowed: Option<&BTreeSet<String>>) -> Value {
    let mut rooms = Map::new();
    let mut nodes = vec![];
    let allowed_all = allowed.is_none();
    for node in &graph.nodes {
        if !allowed_all && !allowed.is_some_and(|set| set.contains(&node.id)) {
            continue;
        }
        if let Some(room) = state.rooms.get(&node.id) {
            rooms.insert(node.id.clone(), room.to_value());
        }
        let mut player_node = node.to_value().as_object().cloned().unwrap_or_default();
        player_node.remove("name");
        nodes.push(Value::Object(player_node));
    }
    let node_ids: BTreeSet<String> = nodes.iter().filter_map(|n| n.get("id").and_then(Value::as_str).map(str::to_string)).collect();
    let edges: Vec<Value> = graph.edges.iter()
        .filter(|edge| node_ids.contains(&edge.a) && node_ids.contains(&edge.b))
        .map(PhysicsEdge::to_value)
        .collect();
    json!({
        "version": SCHEMA_VERSION,
        "enabled": state.enabled,
        "revision": state.revision,
        "tick": state.tick,
        "rooms": Value::Object(rooms),
        "graph": {"nodes": nodes, "edges": edges}
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rooms() -> Value {
        json!({
            "cell":50,"ox":0,"oy":0,"cols":2,"rows":1,
            "cells":[1,1,2,1],
            "spaces":[{"id":1,"name":"Airlock","kind":"hall"},{"id":2,"name":"Engine","kind":"armory"}],
            "marks":[[1,0,"v","door"]]
        })
    }

    fn walls(open: &str) -> Value {
        json!([{
            "id":"rm-m1_0_v","pts":[[50,0],[50,50]],"kind":"door","state":open
        }])
    }

    #[test]
    fn normalizes_v1_and_clamps_integer_room_values() {
        let state = PhysicsState::normalize_json(Some(r#"{"version":1,"enabled":true,"revision":7,"tick":3,"rooms":{"1":{"water":999,"fire":-4,"gas":2.0}}}"#));
        assert!(state.is_err());
        let state = PhysicsState::normalize_json(Some(r#"{"version":1,"enabled":true,"revision":7,"tick":3,"rooms":{"1":{"water":999,"fire":-4,"gas":2}}}"#)).unwrap();
        assert_eq!(state.rooms["1"].water, 100);
        assert_eq!(state.rooms["1"].fire, 0);
        assert_eq!(state.rooms["1"].gas, 2);
        assert_eq!(state.version, 1);
        assert_eq!(state.revision, 7);
    }

    #[test]
    fn step_bounds_are_strict_and_one_to_sixteen() {
        assert_eq!(normalize_steps_i64(1).unwrap(), 1);
        assert_eq!(normalize_steps_i64(16).unwrap(), 16);
        assert!(normalize_steps_i64(0).is_err());
        assert!(normalize_steps_i64(17).is_err());
        assert_eq!(clamp_steps(0), 1);
        assert_eq!(clamp_steps(99), 16);
    }

    #[test]
    fn graph_tracks_open_and_closed_doors() {
        let open = derive_graph(&rooms(), &walls("open"));
        assert_eq!(open.nodes.len(), 2);
        assert_eq!(open.edges.len(), 1);
        assert_eq!(open.edges[0].a, "1");
        assert_eq!(open.edges[0].b, "2");
        let closed = derive_graph(&rooms(), &walls("closed"));
        assert!(closed.edges.is_empty());
    }

    #[test]
    fn tick_is_simultaneous_and_deterministic() {
        let mut state = PhysicsState::default();
        state.rooms.insert("1".into(), RoomPhysics { heat: 100, fire: 100, ..Default::default() });
        state.rooms.insert("2".into(), RoomPhysics::default());
        let graph = derive_graph(&rooms(), &walls("open"));
        state.synchronize_rooms(&graph);
        state.rooms.insert("1".into(), RoomPhysics { heat: 100, fire: 100, ..Default::default() });
        tick(&mut state, &graph, 1).unwrap();
        let a = &state.rooms["1"];
        let b = &state.rooms["2"];
        assert!(a.heat < 100 && b.heat > 0);
        assert!(a.fire < 100 && b.fire > 0);
        assert_eq!(state.tick, 1);
    }

    #[test]
    fn reset_clears_every_room_and_bumps_revision() {
        let graph = derive_graph(&rooms(), &walls("open"));
        let mut state = PhysicsState::default();
        state.revision = 4;
        state.reset(&graph);
        assert_eq!(state.revision, 5);
        assert_eq!(state.tick, 0);
        assert_eq!(state.rooms["1"], RoomPhysics::default());
        assert_eq!(state.rooms["2"], RoomPhysics::default());
    }

    #[test]
    fn player_overlay_can_filter_unexplored_room_ids() {
        let graph = derive_graph(&rooms(), &walls("open"));
        let mut state = PhysicsState::default();
        state.enabled = true;
        state.rooms.insert("1".into(), RoomPhysics { heat: 40, ..Default::default() });
        state.rooms.insert("2".into(), RoomPhysics { heat: 80, ..Default::default() });
        let allowed: BTreeSet<String> = ["1".to_string()].into_iter().collect();
        let payload = player_overlay(&state, &graph, Some(&allowed));
        assert_eq!(payload["rooms"].as_object().unwrap().len(), 1);
        assert_eq!(payload["rooms"]["1"]["heat"], 40);
        assert!(payload["graph"]["nodes"].as_array().unwrap().iter().all(|node| node["id"] == "1"));
        assert!(payload["graph"]["edges"].as_array().unwrap().is_empty());
    }
}
