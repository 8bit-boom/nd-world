//! app/map_layout.py: from a PLAN (rooms, kinds, sizes, links - what the model returns in the map AI build's plan mode) to schematic
//! elements, walls and the room tool's state. The layout is seeded from the plan, so it uses the same pseudo-random numbers as
//! Python's `random.Random` (MT19937, `random()`, `randint`, `randrange`) to give the same map. Also `extract_json` with Python's
//! own `json.loads` reading and its error messages (the job's error names them), as CPython 3.13 spells them.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};

use crate::ai::py_repr;

const MAX_ROOMS: usize = 40;
const MAX_LINKS: usize = 120;
const MAX_NAME: usize = 60;
const MAX_NOTES: usize = 200;

const KINDS: [&str; 12] = ["hall", "tavern", "bedroom", "kitchen", "storage", "corridor", "cell", "shrine", "library", "armory", "cave", "other"];
const KIND_ALIASES: &[(&str, &str)] = &[
    ("inn", "tavern"), ("bar", "tavern"), ("pub", "tavern"), ("common room", "tavern"), ("dining", "tavern"), ("dining hall", "hall"),
    ("great hall", "hall"), ("throne room", "hall"), ("lobby", "hall"), ("foyer", "hall"), ("entrance", "hall"), ("courtyard", "hall"),
    ("room", "other"), ("chamber", "other"), ("office", "other"), ("study", "library"), ("archive", "library"),
    ("bedchamber", "bedroom"), ("dormitory", "bedroom"), ("quarters", "bedroom"), ("barracks", "bedroom"), ("guest room", "bedroom"),
    ("pantry", "kitchen"), ("larder", "storage"), ("cellar", "storage"), ("warehouse", "storage"), ("vault", "storage"), ("closet", "storage"),
    ("hallway", "corridor"), ("passage", "corridor"), ("tunnel", "corridor"), ("stairs", "corridor"),
    ("prison", "cell"), ("jail", "cell"), ("dungeon", "cell"), ("temple", "shrine"), ("chapel", "shrine"), ("crypt", "shrine"), ("altar", "shrine"),
    ("weapon room", "armory"), ("smithy", "armory"), ("forge", "armory"), ("workshop", "armory"),
];
const LINK_TYPES: [&str; 4] = ["door", "open", "locked", "secret"];

fn size_of(size: &str) -> (i64, i64) {
    match size { "small" => (4, 4), "large" => (9, 7), _ => (6, 5) }
}

fn corridor_of(size: &str) -> (i64, i64) {
    match size { "small" => (5, 2), "large" => (12, 2), _ => (8, 2) }
}

fn floor_of(kind: &str) -> &'static str {
    match kind {
        "hall" => "#3a3a4a", "tavern" => "#4a3a2a", "bedroom" => "#3a3a52", "kitchen" => "#4a4538", "storage" => "#3c352c", "corridor" => "#2f3138",
        "cell" => "#303236", "shrine" => "#40384f", "library" => "#35402f", "armory" => "#3b3b3b", "cave" => "#3a342a", _ => "#34363f",
    }
}

// ── Python's random.Random ───────────────────────────────────────────────────────────────────────

/// CPython's `random.Random(seed)` for a seed below 2**32: MT19937 seeded with `init_by_array([seed])`
pub struct PyRandom {
    mt: [u32; 624],
    i: usize,
}

impl PyRandom {
    pub fn new(seed: u32) -> Self {
        let mut mt = [0u32; 624];
        mt[0] = 19650218;
        for i in 1..624 {
            mt[i] = 1812433253u32.wrapping_mul(mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_add(i as u32);
        }
        let (mut i, mut j) = (1usize, 0usize);
        let key = [seed];
        for _ in 0..624 {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1664525)).wrapping_add(key[j]).wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..623 {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1566083941)).wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
        }
        mt[0] = 0x8000_0000;
        PyRandom { mt, i: 624 }
    }

    fn next_u32(&mut self) -> u32 {
        if self.i >= 624 {
            for k in 0..624 {
                let y = (self.mt[k] & 0x8000_0000) | (self.mt[(k + 1) % 624] & 0x7fff_ffff);
                self.mt[k] = self.mt[(k + 397) % 624] ^ (y >> 1) ^ if y & 1 != 0 { 0x9908_b0df } else { 0 };
            }
            self.i = 0;
        }
        let mut y = self.mt[self.i];
        self.i += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^ (y >> 18)
    }

    /// `random()`: 53 random bits in [0, 1)
    pub fn random(&mut self) -> f64 {
        let a = (self.next_u32() >> 5) as f64;
        let b = (self.next_u32() >> 6) as f64;
        (a * 67108864.0 + b) * (1.0 / 9007199254740992.0)
    }

    /// `_randbelow(n)` (n >= 1, below 2**32): getrandbits(n.bit_length()) until it is below n
    fn below(&mut self, n: u64) -> u64 {
        let k = 64 - n.leading_zeros();
        loop {
            let r = (self.next_u32() >> (32 - k)) as u64;
            if r < n {
                return r;
            }
        }
    }

    pub fn randrange(&mut self, n: usize) -> usize {
        self.below(n as u64) as usize
    }

    pub fn randint(&mut self, lo: i64, hi: i64) -> i64 {
        lo + self.below((hi - lo + 1) as u64) as i64
    }
}

// ── Python's json.loads ──────────────────────────────────────────────────────────────────────────

/// `json.loads(text)` -> the value, or the JSONDecodeError's text ("Expecting value: line 1 column 1 (char 0)"). NaN and the
/// infinities read as null (a JSON value cannot hold them), lone surrogates as U+FFFD.
pub fn py_loads(text: &str) -> Result<Value, String> {
    let s: Vec<char> = text.chars().collect();
    let err = |msg: &str, pos: usize| -> String {
        let line = s[..pos].iter().filter(|c| **c == '\n').count() + 1;
        let col = match s[..pos].iter().rposition(|c| *c == '\n') { Some(nl) => pos - nl, None => pos + 1 };
        format!("{msg}: line {line} column {col} (char {pos})")
    };
    let mut p = Parser { s: &s };
    let start = p.ws(0);
    let (v, end) = match p.value(start) {
        Ok(x) => x,
        Err(Fail::Stop(at)) => return Err(err("Expecting value", at)),
        Err(Fail::Msg(m, at)) => return Err(err(m, at)),
    };
    let end = p.ws(end);
    if end != s.len() {
        return Err(err("Extra data", end));
    }
    Ok(v)
}

enum Fail {
    Stop(usize),
    Msg(&'static str, usize),
}

struct Parser<'a> {
    s: &'a [char],
}

impl Parser<'_> {
    fn ws(&self, mut i: usize) -> usize {
        while i < self.s.len() && matches!(self.s[i], ' ' | '\t' | '\n' | '\r') {
            i += 1;
        }
        i
    }

    fn at(&self, i: usize) -> Option<char> {
        self.s.get(i).copied()
    }

    fn lit(&self, i: usize, word: &str) -> bool {
        let w: Vec<char> = word.chars().collect();
        self.s.len() >= i + w.len() && self.s[i..i + w.len()] == w[..]
    }

    fn value(&mut self, i: usize) -> Result<(Value, usize), Fail> {
        match self.at(i) {
            None => Err(Fail::Stop(i)),
            Some('"') => self.string(i + 1).map(|(t, e)| (Value::String(t), e)),
            Some('{') => self.object(i + 1),
            Some('[') => self.array(i + 1),
            Some('n') if self.lit(i, "null") => Ok((Value::Null, i + 4)),
            Some('t') if self.lit(i, "true") => Ok((json!(true), i + 4)),
            Some('f') if self.lit(i, "false") => Ok((json!(false), i + 5)),
            Some('N') if self.lit(i, "NaN") => Ok((Value::Null, i + 3)),
            Some('I') if self.lit(i, "Infinity") => Ok((Value::Null, i + 8)),
            Some('-') if self.lit(i, "-Infinity") => Ok((Value::Null, i + 9)),
            _ => self.number(i),
        }
    }

    fn number(&self, start: usize) -> Result<(Value, usize), Fail> {
        let digit = |i: usize| self.at(i).is_some_and(|c| c.is_ascii_digit());
        let mut i = start;
        if self.at(i) == Some('-') {
            i += 1;
        }
        match self.at(i) {
            Some('1'..='9') => while digit(i) { i += 1 },
            Some('0') => i += 1,
            _ => return Err(Fail::Stop(start)),
        }
        let mut float = false;
        if self.at(i) == Some('.') && digit(i + 1) {
            float = true;
            i += 2;
            while digit(i) { i += 1 }
        }
        if matches!(self.at(i), Some('e' | 'E')) {
            let mut j = i + 1;
            if matches!(self.at(j), Some('+' | '-')) {
                j += 1;
            }
            if digit(j) {
                while digit(j) { j += 1 }
                float = true;
                i = j;
            }
        }
        let lit: String = self.s[start..i].iter().collect();
        let v = if float {
            lit.parse::<f64>().ok().filter(|f| f.is_finite()).map_or(Value::Null, |f| json!(f))
        } else {
            match lit.parse::<i64>() { Ok(n) => json!(n), Err(_) => lit.parse::<f64>().map_or(Value::Null, |f| json!(f)) }
        };
        Ok((v, i))
    }

    fn hex4(&self, i: usize) -> Option<u32> {
        if i + 4 > self.s.len() {
            return None;
        }
        let t: String = self.s[i..i + 4].iter().collect();
        if t.chars().all(|c| c.is_ascii_hexdigit()) { u32::from_str_radix(&t, 16).ok() } else { None }
    }

    /// scanstring from just past the opening quote -> (text, index past the closing quote)
    fn string(&self, i0: usize) -> Result<(String, usize), Fail> {
        let begin = i0 - 1;
        let mut out = String::new();
        let mut i = i0;
        loop {
            let Some(c) = self.at(i) else { return Err(Fail::Msg("Unterminated string starting at", begin)) };
            match c {
                '"' => return Ok((out, i + 1)),
                '\\' => {
                    let Some(e) = self.at(i + 1) else { return Err(Fail::Msg("Unterminated string starting at", begin)) };
                    if e != 'u' {
                        out.push(match e {
                            '"' => '"', '\\' => '\\', '/' => '/', 'b' => '\u{8}', 'f' => '\u{c}', 'n' => '\n', 'r' => '\r', 't' => '\t',
                            _ => return Err(Fail::Msg("Invalid \\escape", i)),
                        });
                        i += 2;
                        continue;
                    }
                    let upos = i + 1;
                    let Some(u) = self.hex4(upos + 1) else { return Err(Fail::Msg("Invalid \\uXXXX escape", upos)) };
                    i = upos + 5;
                    if (0xd800..0xdc00).contains(&u) && self.at(i) == Some('\\') && self.at(i + 1) == Some('u') {
                        let Some(u2) = self.hex4(i + 2) else { return Err(Fail::Msg("Invalid \\uXXXX escape", i + 1)) };
                        if (0xdc00..0xe000).contains(&u2) {
                            out.push(char::from_u32(0x10000 + ((u - 0xd800) << 10) + (u2 - 0xdc00)).unwrap_or('\u{fffd}'));
                        } else {
                            out.push('\u{fffd}');
                            out.push(char::from_u32(u2).unwrap_or('\u{fffd}'));
                        }
                        i += 6;
                    } else {
                        out.push(char::from_u32(u).unwrap_or('\u{fffd}'));
                    }
                }
                c if (c as u32) < 0x20 => return Err(Fail::Msg("Invalid control character at", i)),
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
    }

    fn object(&mut self, i0: usize) -> Result<(Value, usize), Fail> {
        let mut m = Map::new();
        let mut i = self.ws(i0);
        if self.at(i) == Some('}') {
            return Ok((Value::Object(m), i + 1));
        }
        loop {
            if self.at(i) != Some('"') {
                return Err(Fail::Msg("Expecting property name enclosed in double quotes", i));
            }
            let (k, e) = self.string(i + 1)?;
            i = self.ws(e);
            if self.at(i) != Some(':') {
                return Err(Fail::Msg("Expecting ':' delimiter", i));
            }
            i = self.ws(i + 1);
            let (v, e) = self.value(i)?;
            m.insert(k, v);
            i = self.ws(e);
            match self.at(i) {
                Some('}') => return Ok((Value::Object(m), i + 1)),
                Some(',') => {}
                _ => return Err(Fail::Msg("Expecting ',' delimiter", i)),
            }
            let comma = i;
            i = self.ws(i + 1);
            if self.at(i) == Some('}') {
                return Err(Fail::Msg("Illegal trailing comma before end of object", comma));
            }
        }
    }

    fn array(&mut self, i0: usize) -> Result<(Value, usize), Fail> {
        let mut out = vec![];
        let mut i = self.ws(i0);
        if self.at(i) == Some(']') {
            return Ok((Value::Array(out), i + 1));
        }
        loop {
            let (v, e) = self.value(i)?;
            out.push(v);
            i = self.ws(e);
            match self.at(i) {
                Some(']') => return Ok((Value::Array(out), i + 1)),
                Some(',') => {}
                _ => return Err(Fail::Msg("Expecting ',' delimiter", i)),
            }
            let comma = i;
            i = self.ws(i + 1);
            if self.at(i) == Some(']') {
                return Err(Fail::Msg("Illegal trailing comma before end of array", comma));
            }
        }
    }
}

/// `extract_json(text)`: the first JSON object in a model reply, tolerating a code fence or chatter around it; Err(the message)
pub fn extract_json(text: &str) -> Result<Value, String> {
    let mut t = crate::edits::py_strip_pub(text);
    if t.starts_with("```") {
        static A: std::sync::OnceLock<[regex::Regex; 2]> = std::sync::OnceLock::new();
        let re = A.get_or_init(|| [regex::Regex::new(r"^```[a-zA-Z]*\n?").unwrap(), regex::Regex::new(r"\n?```$").unwrap()]);
        t = re[1].replace(&re[0].replace(&t, ""), "").into_owned();
    }
    match py_loads(&t) {
        Ok(v) => Ok(v),
        Err(_) => match (t.find('{'), t.rfind('}')) {
            (Some(a), Some(z)) if z > a => py_loads(&t[a..=z]),
            _ => Err("no JSON object in the reply".into()),
        },
    }
}

// ── the spec ─────────────────────────────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct Room {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub size: String,
    pub entity: String,
}

#[derive(Clone)]
pub struct Link {
    a: String,
    b: String,
    ty: String,
}

pub struct Spec {
    pub title: String,
    entry: String,
    rooms: Vec<Room>,
    links: Vec<Link>,
}

fn truthy(v: Option<&Value>) -> bool {
    crate::edits::py_truthy_pub(v)
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `a or b or c ...`: the first truthy value, else the last one
fn or_chain<'a>(vals: &[Option<&'a Value>]) -> Option<&'a Value> {
    vals.iter().copied().find(|v| truthy(*v)).unwrap_or_else(|| vals.last().copied().flatten())
}

/// `str(v or "")`
fn str_or_empty(v: Option<&Value>) -> String {
    if truthy(v) { py_str(v.unwrap()) } else { String::new() }
}

fn slug(v: Option<&Value>) -> String {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"[^a-z0-9]+").unwrap());
    let s: String = re.replace_all(&str_or_empty(v).to_lowercase(), "-").trim_matches('-').chars().take(30).collect();
    if s.is_empty() { "room".into() } else { s }
}

fn clean_text(v: Option<&Value>, cap: usize) -> String {
    str_or_empty(v).split_whitespace().collect::<Vec<_>>().join(" ").chars().take(cap).collect()
}

/// `clean_map_spec(data)` -> (spec, warnings), Err(the message) when there is nothing to build
pub fn clean_map_spec(data: &Value) -> Result<(Spec, Vec<String>), String> {
    let mut warnings = vec![];
    let Value::Object(d) = data else { return Err("The plan is not a JSON object".into()) };
    let raw_rooms = match or_chain(&[d.get("rooms"), d.get("areas"), d.get("spaces")]) {
        Some(Value::Array(a)) => Some(a),
        _ => d.values().find(|v| v.as_array().is_some_and(|a| a.first().is_some_and(|f| f.is_object()))).and_then(|v| v.as_array()),
    };
    let Some(raw_rooms) = raw_rooms.filter(|a| !a.is_empty()) else { return Err("The plan has no rooms".into()) };
    if raw_rooms.len() > MAX_ROOMS {
        warnings.push(format!("Only the first {MAX_ROOMS} rooms were used."));
    }
    let mut rooms: Vec<Room> = vec![];
    let mut ids: HashMap<String, usize> = HashMap::new();
    for (i, r) in raw_rooms.iter().take(MAX_ROOMS).enumerate() {
        let wrapped;
        let r = match r {
            Value::String(_) => { wrapped = json!({"name": r}); wrapped.as_object().unwrap() }
            Value::Object(o) => o,
            _ => continue,
        };
        let fallback = json!(format!("Room {}", i + 1));
        let mut name = clean_text(or_chain(&[r.get("name"), r.get("title"), r.get("label"), Some(&fallback)]), MAX_NAME);
        if name.is_empty() {
            name = format!("Room {}", i + 1);
        }
        let kind_raw = crate::edits::py_strip_pub(&str_or_empty(or_chain(&[r.get("kind"), r.get("type"), r.get("purpose")]))).to_lowercase();
        let kind = if KINDS.contains(&kind_raw.as_str()) { kind_raw.clone() } else {
            match KIND_ALIASES.iter().find(|(a, _)| *a == kind_raw) {
                Some((_, k)) => k.to_string(),
                None => {
                    let low = name.to_lowercase();
                    static W: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
                    let words: HashSet<&str> = W.get_or_init(|| regex::Regex::new(r"[a-z]+").unwrap()).find_iter(&low).map(|m| m.as_str()).collect();
                    KINDS.iter().find(|k| words.contains(*k)).map(|k| k.to_string()).unwrap_or_else(|| {
                        KIND_ALIASES.iter().find(|(a, _)| regex::Regex::new(&format!(r"\b{}\b", regex::escape(a))).is_ok_and(|re| re.is_match(&low)))
                            .map_or("other".into(), |(_, k)| k.to_string())
                    })
                }
            }
        };
        let size_raw = crate::edits::py_strip_pub(&str_or_empty(r.get("size"))).to_lowercase();
        let size = match size_raw.as_str() {
            "small" | "medium" | "large" => size_raw.clone(),
            "tiny" | "little" => "small".into(),
            "big" | "huge" | "grand" | "vast" => "large".into(),
            _ => "medium".into(),
        };
        let name_v = json!(name);
        let base = slug(Some(or_chain(&[r.get("id"), Some(&name_v)]).unwrap()));
        let (mut rid, mut n) = (base.clone(), 2);
        while ids.contains_key(&rid) {
            rid = format!("{base}-{n}");
            n += 1;
        }
        let _notes = clean_text(or_chain(&[r.get("notes"), r.get("description")]), MAX_NOTES);
        let entity = clean_text(or_chain(&[r.get("entity"), r.get("lore")]), MAX_NAME);
        ids.insert(rid.clone(), rooms.len());
        ids.entry(slug(Some(&name_v))).or_insert(rooms.len());
        rooms.push(Room { id: rid, name, kind, size, entity });
    }
    if rooms.is_empty() {
        return Err("The plan has no usable rooms".into());
    }
    let resolve = |r: Option<&Value>| -> Option<String> {
        let r = match r {
            Some(Value::Object(o)) => or_chain(&[o.get("id"), o.get("name")]),
            other => other,
        };
        let r = r.filter(|v| !v.is_null())?;
        ids.get(&slug(Some(r))).map(|i| rooms[*i].id.clone())
    };
    let mut links: Vec<Link> = vec![];
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let raw_links = match or_chain(&[d.get("links"), d.get("connections"), d.get("doors"), d.get("edges")]) {
        Some(Value::Array(a)) => a.clone(),
        _ => vec![],
    };
    for lk in raw_links.iter().take(MAX_LINKS) {
        let listed;
        let lk = match lk {
            Value::Array(a) if a.len() >= 2 => {
                listed = json!({"a": a[0], "b": a[1], "type": a.get(2).cloned().unwrap_or(json!("door"))});
                listed.as_object().unwrap()
            }
            Value::Object(o) => o,
            _ => continue,
        };
        // `lk.get("a", lk.get("from", lk.get("source")))`: a present key wins even when it is null
        let get3 = |a: &str, b: &str, c: &str| lk.get(a).or_else(|| lk.get(b)).or_else(|| lk.get(c));
        let a = resolve(get3("a", "from", "source"));
        let b = resolve(get3("b", "to", "target"));
        let (Some(a), Some(b)) = (a, b) else {
            warnings.push("A connection named a room that is not in the plan and was skipped.".into());
            continue;
        };
        if a == b {
            continue;
        }
        let key = if a < b { (a.clone(), b.clone()) } else { (b.clone(), a.clone()) };
        if !seen.insert(key) {
            continue;
        }
        let t = crate::edits::py_strip_pub(&py_str(or_chain(&[lk.get("type"), lk.get("kind"), Some(&json!("door"))]).unwrap())).to_lowercase();
        let t = if LINK_TYPES.contains(&t.as_str()) { t } else {
            match t.as_str() {
                "doorway" | "arch" | "archway" | "opening" | "gap" => "open",
                "lock" | "locked door" | "barred" => "locked",
                "hidden" | "secret door" | "hidden door" => "secret",
                _ => "door",
            }.to_string()
        };
        links.push(Link { a, b, ty: t });
    }
    let entry = resolve(or_chain(&[d.get("entry"), d.get("entrance"), d.get("start")])).unwrap_or_else(|| rooms[0].id.clone());

    // join anything unreachable from the entrance so the map is one connected place
    let mut adj: HashMap<String, HashSet<String>> = rooms.iter().map(|r| (r.id.clone(), HashSet::new())).collect();
    for lk in &links {
        adj.get_mut(&lk.a).unwrap().insert(lk.b.clone());
        adj.get_mut(&lk.b).unwrap().insert(lk.a.clone());
    }
    let mut reached: HashSet<String> = HashSet::from([entry.clone()]);
    let flood = |reached: &mut HashSet<String>, adj: &HashMap<String, HashSet<String>>, from: &str| {
        let mut stack = vec![from.to_string()];
        while let Some(x) = stack.pop() {
            for nb in &adj[&x] {
                if reached.insert(nb.clone()) {
                    stack.push(nb.clone());
                }
            }
        }
    };
    flood(&mut reached, &adj, &entry);
    for r in &rooms {
        if !reached.contains(&r.id) {
            warnings.push(format!("“{}” had no way in; it was joined to the entrance with an open doorway.", r.name));
            links.push(Link { a: entry.clone(), b: r.id.clone(), ty: "open".into() });
            reached.insert(r.id.clone());
            adj.get_mut(&entry).unwrap().insert(r.id.clone());
            adj.get_mut(&r.id).unwrap().insert(entry.clone());
            flood(&mut reached, &adj, &r.id);
        }
    }
    let title = clean_text(or_chain(&[d.get("title"), d.get("name")]), MAX_NAME);
    Ok((Spec { title, entry, rooms, links }, warnings))
}

// ── placement on a grid of cells ─────────────────────────────────────────────────────────────────

type Rect = (i64, i64, i64, i64);

fn dims(room: &Room, flip: bool) -> (i64, i64) {
    let (w, h) = if room.kind == "corridor" { corridor_of(&room.size) } else {
        let (w, h) = size_of(&room.size);
        if room.kind == "cave" { (w + 1, h + 1) } else { (w, h) }
    };
    if flip { (h, w) } else { (w, h) }
}

fn overlap(a: Rect, b: Rect) -> bool {
    a.0 < b.0 + b.2 && b.0 < a.0 + a.2 && a.1 < b.1 + b.3 && b.1 < a.1 + a.3
}

fn candidates(parent: Rect, w: i64, h: i64) -> Vec<Rect> {
    let (px, py, pw, ph) = parent;
    let mut out = vec![];
    for dy in (-h + 2)..(ph - 1) {
        out.push((px + pw, py + dy, w, h));
        out.push((px - w, py + dy, w, h));
    }
    for dx in (-w + 2)..(pw - 1) {
        out.push((px + dx, py + ph, w, h));
        out.push((px + dx, py - h, w, h));
    }
    out
}

/// The unit edge cells where a and b touch: (kind, fixed, cells)
fn shared(a: Rect, b: Rect) -> Option<(char, i64, Vec<i64>)> {
    let (ax, ay, aw, ah) = a;
    let (bx, by, bw, bh) = b;
    if ax + aw == bx || bx + bw == ax {
        let x = if ax + aw == bx { bx } else { ax };
        let (lo, hi) = (ay.max(by), (ay + ah).min(by + bh));
        if hi - lo >= 1 {
            return Some(('v', x, (lo..hi).collect()));
        }
    }
    if ay + ah == by || by + bh == ay {
        let y = if ay + ah == by { by } else { ay };
        let (lo, hi) = (ax.max(bx), (ax + aw).min(bx + bw));
        if hi - lo >= 1 {
            return Some(('h', y, (lo..hi).collect()));
        }
    }
    None
}

struct Placed(Vec<(String, Rect)>);

impl Placed {
    fn get(&self, id: &str) -> Option<Rect> {
        self.0.iter().find(|(k, _)| k == id).map(|(_, r)| *r)
    }
    fn has(&self, id: &str) -> bool {
        self.0.iter().any(|(k, _)| k == id)
    }
}

fn place_rooms(spec: &Spec, rng: &mut PyRandom) -> (Placed, Vec<String>, Vec<Link>) {
    let (mut warnings, mut extra) = (vec![], vec![]);
    let by_id: HashMap<&str, &Room> = spec.rooms.iter().map(|r| (r.id.as_str(), r)).collect();
    let mut adj: HashMap<&str, Vec<String>> = spec.rooms.iter().map(|r| (r.id.as_str(), vec![])).collect();
    for lk in &spec.links {
        adj.get_mut(lk.a.as_str()).unwrap().push(lk.b.clone());
        adj.get_mut(lk.b.as_str()).unwrap().push(lk.a.clone());
    }
    let mut placed = Placed(vec![]);
    let first = by_id[spec.entry.as_str()];
    let (w, h) = dims(first, false);
    placed.0.push((first.id.clone(), (0, 0, w, h)));
    let mut queue = std::collections::VecDeque::from([first.id.clone()]);
    let mut order: HashMap<String, usize> = HashMap::from([(first.id.clone(), 0)]);

    let best_spot = |room: &Room, hosts: &[String], placed: &Placed, rng: &mut PyRandom| -> Option<Rect> {
        let mut best: Option<(f64, Rect)> = None;
        for host in hosts {
            for flip in [false, true] {
                let (cw, ch) = dims(room, flip);
                for rect in candidates(placed.get(host).unwrap(), cw, ch) {
                    if placed.0.iter().any(|(_, o)| overlap(rect, *o)) {
                        continue;
                    }
                    let all = placed.0.iter().map(|(_, r)| *r).chain([rect]);
                    let (mut x0, mut y0, mut x1, mut y1) = (i64::MAX, i64::MAX, i64::MIN, i64::MIN);
                    for r in all {
                        x0 = x0.min(r.0);
                        y0 = y0.min(r.1);
                        x1 = x1.max(r.0 + r.2);
                        y1 = y1.max(r.1 + r.3);
                    }
                    let cost = ((x1 - x0) * (y1 - y0)) as f64 + rng.random() * 0.5;
                    if best.is_none_or(|b| cost < b.0) {
                        best = Some((cost, rect));
                    }
                }
            }
            if let Some(b) = best {
                return Some(b.1);
            }
        }
        None
    };

    while let Some(pid) = queue.pop_front() {
        let mut kids = adj[pid.as_str()].clone();
        kids.sort_by(|a, b| (order.get(a).copied().unwrap_or(1 << 30), a).cmp(&(order.get(b).copied().unwrap_or(1 << 30), b)));
        for cid in kids {
            if placed.has(&cid) {
                continue;
            }
            let room = by_id[cid.as_str()];
            let mut hosts = vec![pid.clone()];
            hosts.extend(adj[cid.as_str()].iter().filter(|n| placed.has(n) && **n != pid).cloned());
            let mut rect = best_spot(room, &hosts, &placed, rng);
            if rect.is_none() {
                let (px, py, _, _) = placed.get(&pid).unwrap();
                let mut others: Vec<(String, Rect)> = placed.0.iter().filter(|(k, _)| !hosts.contains(k)).cloned().collect();
                others.sort_by_key(|(_, r)| (r.0 - px).abs() + (r.1 - py).abs());
                let names: Vec<String> = others.iter().map(|(k, _)| k.clone()).collect();
                rect = best_spot(room, &names, &placed, rng);
                if let Some(rc) = rect {
                    if let Some((host, _)) = others.iter().find(|(_, r)| shared(rc, *r).is_some_and(|s| !s.2.is_empty())) {
                        extra.push(Link { a: host.clone(), b: cid.clone(), ty: "open".into() });
                        warnings.push(format!("“{}” would not fit next to “{}”, so it was put beside “{}” with an open doorway.",
                            room.name, by_id[pid.as_str()].name, by_id[host.as_str()].name));
                    }
                }
            }
            let Some(rect) = rect else { continue };
            placed.0.push((cid.clone(), rect));
            let n = order.len();
            order.insert(cid.clone(), n);
            queue.push_back(cid);
        }
    }
    for r in &spec.rooms {
        if !placed.has(&r.id) {
            warnings.push(format!("There was no room on the map for “{}”.", r.name));
        }
    }
    (placed, warnings, extra)
}

// ── walls and doors ──────────────────────────────────────────────────────────────────────────────

type Edge = (char, i64, i64);

fn edges_of(r: Rect) -> Vec<Edge> {
    let (x, y, w, h) = r;
    let mut out = vec![];
    for i in 0..w {
        out.push(('h', y, x + i));
        out.push(('h', y + h, x + i));
    }
    for j in 0..h {
        out.push(('v', x, y + j));
        out.push(('v', x + w, y + j));
    }
    out
}

/// Unit edges -> long segments (kind, fixed, start, end)
fn merge(edges: &[Edge]) -> Vec<(char, i64, i64, i64)> {
    let mut segs = vec![];
    for kind in ['h', 'v'] {
        let mut lines: BTreeMap<i64, Vec<i64>> = BTreeMap::new();
        for (k, fixed, pos) in edges {
            if *k == kind {
                lines.entry(*fixed).or_default().push(*pos);
            }
        }
        for (fixed, mut ps) in lines {
            ps.sort();
            let (mut start, mut prev) = (ps[0], ps[0]);
            for p in &ps[1..] {
                if *p != prev + 1 {
                    segs.push((kind, fixed, start, prev + 1));
                    start = *p;
                }
                prev = *p;
            }
            segs.push((kind, fixed, start, prev + 1));
        }
    }
    segs
}

// ── numbers the way Python keeps them (an int stays an int, anything touching a float is one) ───────

#[derive(Clone, Copy)]
enum N {
    I(i64),
    F(f64),
}

impl N {
    fn f(self) -> f64 {
        match self { N::I(i) => i as f64, N::F(f) => f }
    }
    fn v(self) -> Value {
        match self { N::I(i) => json!(i), N::F(f) => json!(f) }
    }
}

impl std::ops::Add for N {
    type Output = N;
    fn add(self, o: N) -> N {
        match (self, o) { (N::I(a), N::I(b)) => N::I(a + b), _ => N::F(self.f() + o.f()) }
    }
}

impl std::ops::Sub for N {
    type Output = N;
    fn sub(self, o: N) -> N {
        match (self, o) { (N::I(a), N::I(b)) => N::I(a - b), _ => N::F(self.f() - o.f()) }
    }
}

impl std::ops::Mul for N {
    type Output = N;
    fn mul(self, o: N) -> N {
        match (self, o) { (N::I(a), N::I(b)) => N::I(a * b), _ => N::F(self.f() * o.f()) }
    }
}

fn i(n: i64) -> N {
    N::I(n)
}

fn fl(f: f64) -> N {
    N::F(f)
}

/// Python's true division
fn div(a: N, b: N) -> N {
    N::F(a.f() / b.f())
}

fn rid(prefix: &str, n: usize) -> String {
    format!("{prefix}{}", &format!("{:032x}", rand::random::<u128>())[..n])
}

// ── furniture ────────────────────────────────────────────────────────────────────────────────────

fn item_of(item: &str) -> ((i64, i64), &'static str) {
    match item {
        "bed" | "cot" => ((1, 2), "bed"),
        "round table" | "barrel" | "statue" | "stool" | "bucket" | "pillar" => ((1, 1), "circle"),
        "bar counter" => ((3, 1), "rect"),
        "stove" | "crate" | "chest" => ((1, 1), "rect"),
        _ => ((2, 1), "rect"), // table, counter, shelf, bookshelf, altar, bench, weapon rack, desk
    }
}

fn colour_of(item: &str) -> &'static str {
    match item {
        "bed" => "#6b4b3a", "cot" => "#5a4a3a", "table" | "round table" => "#7a5a3a", "bar counter" => "#6a4a2a", "counter" => "#6a5a4a",
        "stove" => "#4a4a4f", "barrel" => "#8a5a2b", "crate" => "#8a6a3a", "chest" => "#9a7a2a", "shelf" | "bookshelf" => "#4a3a2a",
        "altar" => "#8c8c9c", "statue" => "#9a9aa4", "bench" => "#6a5030", "stool" => "#7a6040", "weapon rack" => "#555", "bucket" => "#6a6a74",
        "desk" => "#6a4a2a", "pillar" => "#8a8a92", _ => "#777777",
    }
}

fn synonyms(item: &str) -> &'static [&'static str] {
    match item {
        "bed" => &["bed", "cot", "bunk"], "cot" => &["cot", "bed", "bunk"], "table" | "round table" => &["table"], "bar counter" => &["bar", "counter"],
        "counter" => &["counter", "bar"], "stove" => &["stove", "oven", "hearth", "fireplace"], "barrel" => &["barrel", "cask", "keg"],
        "crate" => &["crate", "box"], "chest" => &["chest", "trunk"], "shelf" => &["shelf", "bookcase", "bookshelf"],
        "bookshelf" => &["bookshelf", "bookcase", "shelf"], "altar" => &["altar"], "statue" => &["statue"], "bench" => &["bench", "pew"],
        "stool" => &["stool", "chair"], "weapon rack" => &["rack", "weapon"], "bucket" => &["bucket", "pail"], "desk" => &["desk", "table"],
        "pillar" => &["pillar", "column"], _ => &[],
    }
}

fn furnishing(kind: &str) -> &'static [(&'static str, (i64, i64), &'static str)] {
    match kind {
        "tavern" => &[("bar counter", (1, 1), "wall"), ("table", (2, 4), "centre"), ("stool", (2, 5), "any"), ("barrel", (2, 3), "corner")],
        "hall" => &[("table", (1, 3), "centre"), ("bench", (2, 4), "wall"), ("pillar", (0, 2), "centre")],
        "bedroom" => &[("bed", (1, 3), "wall"), ("chest", (1, 1), "corner"), ("table", (0, 1), "wall")],
        "kitchen" => &[("counter", (2, 3), "wall"), ("stove", (1, 1), "wall"), ("table", (1, 1), "centre"), ("barrel", (1, 2), "corner")],
        "storage" => &[("crate", (3, 6), "any"), ("barrel", (2, 4), "corner"), ("shelf", (1, 3), "wall")],
        "cell" => &[("cot", (1, 2), "wall"), ("bucket", (1, 1), "corner")],
        "shrine" => &[("altar", (1, 1), "centre"), ("statue", (1, 2), "corner"), ("bench", (0, 3), "wall")],
        "library" => &[("bookshelf", (3, 6), "wall"), ("table", (1, 2), "centre"), ("stool", (1, 3), "any")],
        "armory" => &[("weapon rack", (2, 4), "wall"), ("crate", (1, 3), "any"), ("table", (0, 1), "centre")],
        "cave" => &[("barrel", (0, 1), "corner"), ("crate", (0, 2), "any")],
        "other" => &[("table", (0, 1), "centre"), ("chest", (0, 1), "corner")],
        _ => &[],
    }
}

/// A prop of the world's library: (id, name, tags, url, cells_w, cells_h)
pub struct Prop {
    pub id: i64,
    pub name: String,
    pub tags: String,
    pub url: Value,
    pub cells_w: Option<f64>,
    pub cells_h: Option<f64>,
}

fn find_prop<'a>(item: &str, props: &'a [Prop]) -> Option<&'a Prop> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"[a-z]+").unwrap());
    let words = synonyms(item);
    let words: Vec<&str> = if words.is_empty() { vec![item] } else { words.to_vec() };
    props.iter().find(|p| {
        let hay = format!("{} {}", p.name, p.tags).to_lowercase();
        let hay: HashSet<&str> = re.find_iter(&hay).map(|m| m.as_str()).collect();
        words.iter().any(|w| hay.contains(w))
    })
}

#[allow(clippy::too_many_arguments)]
fn item_elements(item: &str, shape: &str, cx: i64, cy: i64, iw: i64, ih: i64, props: &[Prop], cell: i64, ox: i64, oy: i64) -> Vec<Value> {
    let (px, py, pw, ph) = (i(ox + cx * cell), i(oy + cy * cell), i(iw * cell), i(ih * cell));
    let inset = if cell as f64 * 0.08 > 2.0 { fl(cell as f64 * 0.08) } else { i(2) };
    if let Some(prop) = find_prop(item, props) {
        let horizontal = iw >= ih;
        let native_wide = prop.cells_w.filter(|v| *v != 0.0).unwrap_or(1.0) >= prop.cells_h.filter(|v| *v != 0.0).unwrap_or(1.0);
        let rot = if horizontal == native_wide || iw == ih { 0 } else { 90 };
        let (mut w, mut h) = (pw - i(2) * inset, ph - i(2) * inset);
        if rot != 0 {
            std::mem::swap(&mut w, &mut h);
        }
        let (ccx, ccy) = (px + div(pw, i(2)), py + div(ph, i(2)));
        return vec![json!({"id": rid("ai-", 10), "type": "image", "x": (ccx - div(w, i(2))).v(), "y": (ccy - div(h, i(2))).v(), "w": w.v(), "h": h.v(),
            "rot": rot, "href": prop.url, "prop_id": prop.id, "opacity": 1, "fill": "none", "stroke": "none", "strokeW": 0, "label": "", "dash": "", "layer": "Tracks"})];
    }
    let colour = colour_of(item);
    if shape == "circle" {
        let r = div(i(pw.f().min(ph.f()) as i64), i(2)) - inset;
        return vec![json!({"id": rid("ai-", 10), "type": "circle", "cx": (px + div(pw, i(2))).v(), "cy": (py + div(ph, i(2))).v(), "rx": r.v(), "ry": r.v(),
            "fill": colour, "stroke": "#1d1d22", "strokeW": 1, "layer": "Tracks"})];
    }
    let mut els = vec![json!({"id": rid("ai-", 10), "type": "rect", "x": (px + inset).v(), "y": (py + inset).v(), "w": (pw - i(2) * inset).v(),
        "h": (ph - i(2) * inset).v(), "fill": colour, "stroke": "#1d1d22", "strokeW": 1, "layer": "Tracks"})];
    if shape == "bed" {
        let (x, y, w, h) = if ph.f() >= pw.f() {
            (px + pw * fl(0.2), py + inset + i(2), pw * fl(0.6), ph * fl(0.22))
        } else {
            (px + inset + i(2), py + ph * fl(0.2), pw * fl(0.22), ph * fl(0.6))
        };
        els.push(json!({"id": rid("ai-", 10), "type": "rect", "x": x.v(), "y": y.v(), "w": w.v(), "h": h.v(), "fill": "#d8d8e2", "stroke": "#1d1d22",
            "strokeW": 1, "layer": "Tracks"}));
    }
    els
}

#[allow(clippy::too_many_arguments)]
fn furnish_room(room: &Room, rect: Rect, doors: &HashSet<Edge>, rng: &mut PyRandom, props: &[Prop], cell: i64, ox: i64, oy: i64) -> Vec<Value> {
    let (x, y, w, h) = rect;
    let plan = furnishing(&room.kind);
    if plan.is_empty() || w < 3 || h < 3 {
        return vec![];
    }
    let mut free: HashSet<(i64, i64)> = (x..x + w).flat_map(|cx| (y..y + h).map(move |cy| (cx, cy))).collect();
    let inside = |c: (i64, i64)| x <= c.0 && c.0 < x + w && y <= c.1 && c.1 < y + h;
    for (kind, fixed, pos) in doors {
        for (side, step) in [(-1, -1), (0, 1)] {
            let c = if *kind == 'v' { (fixed + side, *pos) } else { (*pos, fixed + side) };
            if inside(c) {
                free.remove(&c);
                free.remove(&if *kind == 'v' { (c.0 + step, c.1) } else { (c.0, c.1 + step) });
            }
        }
    }
    let mut out = vec![];
    let corners = [(x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)];
    for (item, (lo, hi), place) in plan {
        let ((fw, fh), shape) = item_of(item);
        for _ in 0..rng.randint(*lo, *hi) {
            let mut spots: Vec<(i64, i64, i64, i64, Vec<(i64, i64)>)> = vec![];
            let orientations: &[bool] = if fw != fh { &[true, false] } else { &[true] };
            for horizontal in orientations {
                let (iw, ih) = if *horizontal { (fw, fh) } else { (fh, fw) };
                for cx in x..(x + w - iw + 1) {
                    for cy in y..(y + h - ih + 1) {
                        let cells: Vec<(i64, i64)> = (0..iw).flat_map(|a| (0..ih).map(move |b| (cx + a, cy + b))).collect();
                        if !cells.iter().all(|c| free.contains(c)) {
                            continue;
                        }
                        let on_wall = cx == x || cy == y || cx + iw == x + w || cy + ih == y + h;
                        let inner = cx > x && cy > y && cx + iw < x + w && cy + ih < y + h;
                        if *place == "wall" && !on_wall {
                            continue;
                        }
                        if *place == "wall" && iw >= ih && !(cy == y || cy + ih == y + h) {
                            continue;
                        }
                        if *place == "wall" && ih > iw && !(cx == x || cx + iw == x + w) {
                            continue;
                        }
                        if *place == "corner" && !corners.iter().any(|c| cells.contains(c)) {
                            continue;
                        }
                        if *place == "centre" && !inner {
                            continue;
                        }
                        spots.push((cx, cy, iw, ih, cells));
                    }
                }
            }
            if spots.is_empty() {
                continue;
            }
            let k = rng.randrange(spots.len());
            let (cx, cy, iw, ih, cells) = spots.swap_remove(k);
            for c in &cells {
                free.remove(c);
            }
            if *place == "centre" {
                for (a, b) in &cells {
                    for da in -1..=1 {
                        for db in -1..=1 {
                            free.remove(&(a + da, b + db));
                        }
                    }
                }
            }
            out.extend(item_elements(item, shape, cx, cy, iw, ih, props, cell, ox, oy));
        }
    }
    out
}

// ── the whole map ────────────────────────────────────────────────────────────────────────────────

fn seed_of(spec: &Spec) -> u32 {
    use sha1::Digest;
    let rooms: Vec<String> = spec.rooms.iter().map(|r| format!("('{}', '{}', '{}')", r.id, r.kind, r.size)).collect();
    let links: Vec<String> = spec.links.iter().map(|l| format!("('{}', '{}', '{}')", l.a, l.b, l.ty)).collect();
    let blob = format!("[{}][{}]", rooms.join(", "), links.join(", "));
    let d = sha1::Sha1::digest(blob.as_bytes());
    u32::from_be_bytes([d[0], d[1], d[2], d[3]])
}

/// `layout_full(spec, cell, canvas_w, canvas_h, props, origin=origin, state_out=...)` -> (elements, walls, warnings, room state).
/// `want_state` asks for the room tool's state (Some when the plan fits its grid, then with its `grid`).
pub fn layout_full(spec: &Spec, cell: f64, canvas_w: i64, canvas_h: i64, props: &[Prop], origin: (f64, f64), want_state: bool)
                   -> (Vec<Value>, Vec<Value>, Vec<String>, Option<Map<String, Value>>) {
    let mut rng = PyRandom::new(seed_of(spec));
    let mut warnings = vec![];
    let mut cell = (if cell == 0.0 { 50.0 } else { cell }).trunc().max(8.0) as i64;
    let (placed, w1, extra) = place_rooms(spec, &mut rng);
    warnings.extend(w1);
    let links: Vec<Link> = spec.links.iter().cloned().chain(extra).collect();
    let rects: Vec<Rect> = placed.0.iter().map(|(_, r)| *r).collect();
    let x0 = rects.iter().map(|r| r.0).min().unwrap_or(0);
    let y0 = rects.iter().map(|r| r.1).min().unwrap_or(0);
    let bw = rects.iter().map(|r| r.0 + r.2).max().unwrap_or(0) - x0;
    let bh = rects.iter().map(|r| r.1 + r.3).max().unwrap_or(0) - y0;
    if (bw + 2) * cell > canvas_w || (bh + 2) * cell > canvas_h {
        let fit = (canvas_w as f64 / (bw + 2) as f64).min(canvas_h as f64 / (bh + 2) as f64).trunc() as i64;
        let fit = if fit < 8 {
            warnings.push("The plan is too big for this canvas even at the smallest size; make the canvas larger.".into());
            8
        } else {
            warnings.push(format!("The plan is larger than the canvas, so squares were drawn {fit}px instead of {cell}px."));
            fit
        };
        cell = fit;
    }
    let c = cell as f64;
    let ox = ((((canvas_w - bw * cell) as f64 / 2.0) / c).floor() as i64) * cell - x0 * cell + origin.0.rem_euclid(c).trunc() as i64;
    let oy = ((((canvas_h - bh * cell) as f64 / 2.0) / c).floor() as i64) * cell - y0 * cell + origin.1.rem_euclid(c).trunc() as i64;
    let by_id: HashMap<&str, &Room> = spec.rooms.iter().map(|r| (r.id.as_str(), r)).collect();
    let mut els = vec![];
    let (oxm, oym) = (ox.rem_euclid(cell), oy.rem_euclid(cell));
    let (mut pre, mut cols, mut rows, mut shx, mut shy) = ("ai-", 0, 0, 0, 0);
    if want_state {
        cols = (canvas_w - oxm).div_euclid(cell);
        rows = (canvas_h - oym).div_euclid(cell);
        shx = (ox - oxm).div_euclid(cell);
        shy = (oy - oym).div_euclid(cell);
        let fits = (1..=400).contains(&cols) && (1..=400).contains(&rows) && cols * rows <= 40000 && shx >= 0 && shy >= 0
            && rects.iter().all(|r| shx + r.0 >= 0 && shx + r.0 + r.2 <= cols && shy + r.1 >= 0 && shy + r.1 + r.3 <= rows);
        if fits {
            pre = "rm-";
        }
    }
    // floors
    for (id, (x, y, w, h)) in &placed.0 {
        let room = by_id[id.as_str()];
        let mut el = json!({"id": rid(pre, 10), "type": "rect", "x": ox + x * cell, "y": oy + y * cell, "w": w * cell, "h": h * cell,
            "fill": floor_of(&room.kind), "stroke": "none", "strokeW": 0, "label": room.name, "layer": "Background"});
        if !room.entity.is_empty() {
            el["entity_name"] = json!(room.entity);
        }
        els.push(el);
    }
    // doors: one per link between rooms that ended up touching
    let mut edge_owner: HashMap<Edge, usize> = HashMap::new();
    let mut edge_order: Vec<Edge> = vec![];
    for (_, rect) in &placed.0 {
        for e in edges_of(*rect) {
            let n = edge_owner.entry(e).or_insert_with(|| { edge_order.push(e); 0 });
            *n += 1;
        }
    }
    let mut door_cells: BTreeMap<Edge, String> = BTreeMap::new();
    let mut doors_of_room: HashMap<String, HashSet<Edge>> = placed.0.iter().map(|(k, _)| (k.clone(), HashSet::new())).collect();
    for lk in &links {
        let (Some(a), Some(b)) = (placed.get(&lk.a), placed.get(&lk.b)) else { continue };
        let Some((kind, fixed, cells)) = shared(a, b) else {
            warnings.push(format!("“{}” and “{}” do not touch, so there is no door between them.", by_id[lk.a.as_str()].name, by_id[lk.b.as_str()].name));
            continue;
        };
        let d = (kind, fixed, cells[(cells.len() - 1) / 2]);
        door_cells.insert(d, lk.ty.clone());
        doors_of_room.get_mut(&lk.a).unwrap().insert(d);
        doors_of_room.get_mut(&lk.b).unwrap().insert(d);
    }
    // an outside door on the entrance room: the middle of its longest free wall
    let entry_rect = placed.get(&spec.entry).unwrap();
    let free_edges: Vec<Edge> = edges_of(entry_rect).into_iter().filter(|e| edge_owner[e] == 1 && !door_cells.contains_key(e)).collect();
    if !free_edges.is_empty() {
        let mut segs = merge(&free_edges);
        segs.sort_by_key(|s| -(s.3 - s.2));
        let (kind, fixed, a, b) = segs[0];
        let d = (kind, fixed, a + (b - a - 1).div_euclid(2));
        door_cells.insert(d, "door".into());
        doors_of_room.get_mut(&spec.entry).unwrap().insert(d);
    }
    let wall_edges: Vec<Edge> = edge_order.iter().copied().filter(|e| door_cells.get(e).is_none_or(|t| t == "secret")).collect();
    let stroke_w = 3i64.max((c * 0.1).round_ties_even() as i64);
    let mut walls = vec![];
    let plain: Vec<Edge> = wall_edges.iter().copied().filter(|e| door_cells.get(e).map(String::as_str) != Some("secret")).collect();
    for (kind, fixed, a, b) in merge(&plain) {
        let pts = if kind == 'h' { json!([[ox + a * cell, oy + fixed * cell], [ox + b * cell, oy + fixed * cell]]) }
            else { json!([[ox + fixed * cell, oy + a * cell], [ox + fixed * cell, oy + b * cell]]) };
        walls.push(json!({"id": rid(&format!("{pre}w"), 8), "pts": pts, "kind": "wall"}));
    }
    for (kind, fixed, a, b) in merge(&wall_edges) {
        let (x1, y1, x2, y2) = if kind == 'h' { (ox + a * cell, oy + fixed * cell, ox + b * cell, oy + fixed * cell) }
            else { (ox + fixed * cell, oy + a * cell, ox + fixed * cell, oy + b * cell) };
        els.push(json!({"id": rid(pre, 10), "type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "stroke": "#cfd6e6", "strokeW": stroke_w, "layer": "Tracks"}));
    }
    for ((kind, fixed, pos), t) in &door_cells {
        if t == "open" {
            continue;
        }
        let pad = fl(c * 0.12);
        let (x1, y1, x2, y2) = if *kind == 'h' {
            (i(ox + pos * cell) + pad, i(oy + fixed * cell), i(ox + (pos + 1) * cell) - pad, i(oy + fixed * cell))
        } else {
            (i(ox + fixed * cell), i(oy + pos * cell) + pad, i(ox + fixed * cell), i(oy + (pos + 1) * cell) - pad)
        };
        walls.push(json!({"id": rid(&format!("{pre}d"), 8), "pts": [[x1.v(), y1.v()], [x2.v(), y2.v()]], "kind": if t == "secret" { "secret" } else { "door" }, "state": "closed"}));
        let mut d = json!({"id": rid(pre, 10), "type": "line", "x1": x1.v(), "y1": y1.v(), "x2": x2.v(), "y2": y2.v(),
            "stroke": match t.as_str() { "locked" => "#d94b4b", _ => "#c98a3d" }, "strokeW": stroke_w + 2, "layer": "Tracks"});
        if t == "secret" {
            d["hidden"] = json!(true);
            d["dash"] = json!("4 3");
        }
        els.push(d);
    }
    // furniture
    let space_id: HashMap<&str, usize> = placed.0.iter().enumerate().map(|(n, (k, _))| (k.as_str(), n + 1)).collect();
    for (id, rect) in &placed.0 {
        let mut made = furnish_room(by_id[id.as_str()], *rect, &doors_of_room[id], &mut rng, props, cell, ox, oy);
        if pre == "rm-" {
            for (n, e) in made.iter_mut().enumerate() {
                e["id"] = json!(format!("rf-{}-{n}", space_id[id.as_str()]));
            }
        }
        els.extend(made);
    }
    let mut state = None;
    if pre == "rm-" {
        let mut ids = vec![0usize; (cols * rows) as usize];
        for (id, (x, y, w, h)) in &placed.0 {
            for j in 0..*h {
                for k in 0..*w {
                    ids[((shy + y + j) * cols + shx + x + k) as usize] = space_id[id.as_str()];
                }
            }
        }
        let mut runs: Vec<usize> = vec![];
        for v in ids {
            let n = runs.len();
            if n >= 2 && runs[n - 2] == v {
                runs[n - 1] += 1;
            } else {
                runs.extend([v, 1]);
            }
        }
        let marks: Vec<Value> = door_cells.iter().map(|((kind, fixed, pos), t)| {
            let m = match t.as_str() { "secret" => "secret", "open" => "open", _ => "door" };
            if *kind == 'h' { json!([shx + pos, shy + fixed, "h", m]) } else { json!([shx + fixed, shy + pos, "v", m]) }
        }).collect();
        let mut s = Map::new();
        s.insert("cell".into(), json!(cell));
        s.insert("ox".into(), json!(oxm));
        s.insert("oy".into(), json!(oym));
        s.insert("cols".into(), json!(cols));
        s.insert("rows".into(), json!(rows));
        s.insert("cells".into(), json!(runs));
        s.insert("marks".into(), Value::Array(marks));
        s.insert("spaces".into(), Value::Array(placed.0.iter().map(|(k, _)| {
            let r = by_id[k.as_str()];
            json!({"id": space_id[k.as_str()], "name": r.name, "kind": r.kind})
        }).collect()));
        s.insert("grid".into(), json!({"cell_size": cell, "offset_x": oxm, "offset_y": oym}));
        state = Some(s);
    }
    (els, walls, warnings, state)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_random() {
        // random.Random(12345): random(), random(), randint(0, 0), randrange(7), randint(2, 5)
        let mut r = PyRandom::new(12345);
        assert_eq!(r.random(), 0.41661987254534116);
        assert_eq!(r.random(), 0.010169169457068361);
        assert_eq!((r.randint(0, 0), r.randrange(7), r.randint(2, 5)), (0, 6, 4));
        let mut r = PyRandom::new(0);
        assert_eq!(r.random(), 0.8444218515250481);
        let mut r = PyRandom::new(u32::MAX);
        assert_eq!(r.random(), 0.6353574441341173);
        assert_eq!((0..5).map(|_| r.randrange(1000)).collect::<Vec<_>>(), [208, 220, 621, 534, 298]);
    }

    #[test]
    fn json_errors_as_python_spells_them() {
        for (s, want) in [("{\"a\" 1}", "Expecting ':' delimiter: line 1 column 6 (char 5)"), ("[1 2]", "Expecting ',' delimiter: line 1 column 4 (char 3)"),
                          ("{a:1}", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"), ("\"ab", "Unterminated string starting at: line 1 column 1 (char 0)"),
                          ("\"a\\x\"", "Invalid \\escape: line 1 column 3 (char 2)"), ("\"a\\u12g4\"", "Invalid \\uXXXX escape: line 1 column 4 (char 3)"),
                          ("\"a\tb\"", "Invalid control character at: line 1 column 3 (char 2)"), ("", "Expecting value: line 1 column 1 (char 0)"),
                          ("-", "Expecting value: line 1 column 1 (char 0)"), ("1 2", "Extra data: line 1 column 3 (char 2)"), ("{\"a\":}", "Expecting value: line 1 column 6 (char 5)"),
                          ("[", "Expecting value: line 1 column 2 (char 1)"), ("{\"a\":1", "Expecting ',' delimiter: line 1 column 7 (char 6)"),
                          ("\"\\ud800\\u12\"", "Invalid \\uXXXX escape: line 1 column 9 (char 8)"), ("{\"a\":1}\n x", "Extra data: line 2 column 2 (char 9)"),
                          ("{\"a\":1,}", "Illegal trailing comma before end of object: line 1 column 7 (char 6)"), ("[1,\n2,\n]", "Illegal trailing comma before end of array: line 2 column 2 (char 5)"),
                          ("\"\\u12", "Invalid \\uXXXX escape: line 1 column 3 (char 2)"), ("\"\\u1234", "Unterminated string starting at: line 1 column 1 (char 0)"),
                          ("1.", "Extra data: line 1 column 2 (char 1)"), ("01", "Extra data: line 1 column 2 (char 1)"), ("\"é\t\"", "Invalid control character at: line 1 column 3 (char 2)")] {
            assert_eq!(py_loads(s).unwrap_err(), want, "{s:?}");
        }
        assert_eq!(py_loads("{\"a\": [1, 2.5, -0, 1e2, \"\\ud83d\\ude00\"], \"a\": NaN}").unwrap(), json!({"a": null}));
        assert_eq!(py_loads(" [1, 2.5, 1e2, \"\\ud83d\\ude00\"] ").unwrap(), json!([1, 2.5, 100.0, "😀"]));
    }
}
