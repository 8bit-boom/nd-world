//! Python values templates call methods on: `datetime` (`.strftime()`, `.isoformat()`, `.year` ...). The page builder marks a database
//! DATETIME column as {"__dt__": "<SQLAlchemy text>"} and `to_value` turns it into an object that behaves like Python's.
use minijinja::value::{Enumerator, Object, Value};
use minijinja::{Error, ErrorKind, State};
use std::fmt;
use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct PyDateTime {
    pub y: i64,
    pub mo: u32,
    pub d: u32,
    pub h: u32,
    pub mi: u32,
    pub s: u32,
    pub us: u32,
}

impl PyDateTime {
    /// SQLAlchemy's SQLite text: "YYYY-MM-DD HH:MM:SS[.ffffff]" (or "YYYY-MM-DD" / an ISO "T").
    pub fn parse(t: &str) -> Option<PyDateTime> {
        let t = t.trim();
        let (date, time) = match t.find([' ', 'T']) {
            Some(i) => (&t[..i], &t[i + 1..]),
            None => (t, "00:00:00"),
        };
        let mut dp = date.split('-');
        let (y, mo, d) = (dp.next()?.parse().ok()?, dp.next()?.parse().ok()?, dp.next()?.parse().ok()?);
        let (hms, frac) = time.split_once('.').unwrap_or((time, ""));
        let mut tp = hms.split(':');
        let (h, mi) = (tp.next()?.parse().ok()?, tp.next()?.parse().ok()?);
        let s = tp.next().map(|x| x.parse().ok()).unwrap_or(Some(0))?;
        let us = if frac.is_empty() { 0 } else { format!("{:0<6}", &frac[..frac.len().min(6)]).parse().ok()? };
        Some(PyDateTime { y, mo, d, h, mi, s, us })
    }

    fn weekday(&self) -> u32 {
        // 0 = Monday (Python's weekday())
        let (y, m) = if self.mo < 3 { (self.y - 1, self.mo + 12) } else { (self.y, self.mo) };
        let k = y.rem_euclid(100);
        let j = y.div_euclid(100);
        let h = (self.d as i64 + (13 * (m as i64 + 1)) / 5 + k + k / 4 + j / 4 + 5 * j).rem_euclid(7); // 0 = Saturday
        ((h + 5) % 7) as u32
    }

    /// `strftime` for the codes the templates use; an unknown code is an error (the page then goes to Python).
    pub fn strftime(&self, fmt: &str) -> Result<String, Error> {
        const MON: [&str; 12] = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
        const DAY: [&str; 7] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
        let mut out = String::new();
        let mut it = fmt.chars();
        while let Some(c) = it.next() {
            if c != '%' {
                out.push(c);
                continue;
            }
            match it.next() {
                Some('Y') => out.push_str(&format!("{:04}", self.y)),
                Some('y') => out.push_str(&format!("{:02}", self.y.rem_euclid(100))),
                Some('m') => out.push_str(&format!("{:02}", self.mo)),
                Some('d') => out.push_str(&format!("{:02}", self.d)),
                Some('H') => out.push_str(&format!("{:02}", self.h)),
                Some('I') => out.push_str(&format!("{:02}", if self.h % 12 == 0 { 12 } else { self.h % 12 })),
                Some('p') => out.push_str(if self.h < 12 { "AM" } else { "PM" }),
                Some('M') => out.push_str(&format!("{:02}", self.mi)),
                Some('S') => out.push_str(&format!("{:02}", self.s)),
                Some('f') => out.push_str(&format!("{:06}", self.us)),
                Some('b') => out.push_str(&MON[(self.mo - 1) as usize][..3]),
                Some('B') => out.push_str(MON[(self.mo - 1) as usize]),
                Some('a') => out.push_str(&DAY[self.weekday() as usize][..3]),
                Some('A') => out.push_str(DAY[self.weekday() as usize]),
                Some('%') => out.push('%'),
                other => return Err(Error::new(ErrorKind::InvalidOperation, format!("strftime %{other:?} not ported"))),
            }
        }
        Ok(out)
    }

    pub fn isoformat(&self) -> String {
        let base = format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", self.y, self.mo, self.d, self.h, self.mi, self.s);
        if self.us == 0 { base } else { format!("{base}.{:06}", self.us) }
    }
}

impl fmt::Display for PyDateTime {
    /// `str(datetime)`
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.isoformat().replacen('T', " ", 1))
    }
}

impl Object for PyDateTime {
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        Some(match key.as_str()? {
            "year" => Value::from(self.y),
            "month" => Value::from(self.mo),
            "day" => Value::from(self.d),
            "hour" => Value::from(self.h),
            "minute" => Value::from(self.mi),
            "second" => Value::from(self.s),
            _ => return None,
        })
    }
    fn enumerate(self: &Arc<Self>) -> Enumerator {
        Enumerator::NonEnumerable
    }
    fn call_method(self: &Arc<Self>, _state: &State, name: &str, args: &[Value]) -> Result<Value, Error> {
        match name {
            "strftime" => Ok(Value::from(self.strftime(args.first().and_then(|a| a.as_str()).unwrap_or(""))?)),
            "isoformat" => Ok(Value::from(self.isoformat())),
            "date" => Ok(Value::from_object(PyDateTime { h: 0, mi: 0, s: 0, us: 0, ..(**self).clone() })),
            _ => Err(Error::new(ErrorKind::UnknownMethod, format!("datetime.{name}"))),
        }
    }
    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&**self, f)
    }
}

/// A page context (JSON) as template values, with marked datetimes turned into `PyDateTime`.
pub fn to_value(j: &serde_json::Value) -> Value {
    match j {
        serde_json::Value::Object(o) if o.len() == 1 && o.contains_key("__dt__") => match o["__dt__"].as_str().and_then(PyDateTime::parse) {
            Some(dt) => Value::from_object(dt),
            None => Value::from(()),
        },
        serde_json::Value::Object(o) => Value::from_iter(o.iter().map(|(k, v)| (k.clone(), to_value(v)))),
        serde_json::Value::Array(a) => Value::from_iter(a.iter().map(to_value)),
        other => Value::from_serialize(other),
    }
}
