//! Row writes the way SQLAlchemy makes them (phase 3): Python-side column defaults on INSERT and `onupdate` columns on UPDATE, taken from
//! the models themselves (`column_defaults` in rust/nd-render/data/app_data.json, written by scripts/gen_rust_data.py), so a row the gateway
//! writes is the row Python would have written.

use rusqlite::types::Value as Sql;
use rusqlite::Connection;
use serde_json::Value;

/// Now as SQLAlchemy stores a naive UTC `datetime.utcnow()` in SQLite ("2026-10-22 03:31:09.123456")
pub fn now_stamp() -> String {
    stamp_after(0)
}

/// `datetime.utcnow() + timedelta(seconds=offset)` as SQLAlchemy stores it
pub fn stamp_after(offset_secs: i64) -> String {
    let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let (secs, micros) = (d.as_secs() as i64 + offset_secs, d.subsec_micros());
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let dd = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{dd:02} {:02}:{:02}:{:02}.{micros:06}", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn defaults(table: &str) -> Option<&'static serde_json::Map<String, Value>> {
    nd_render::app::data().get("column_defaults")?.get(table)?.as_object()
}

fn to_sql(v: &Value, now: &str) -> Option<Sql> {
    Some(match v {
        Value::String(s) if s == "__utcnow__" => Sql::Text(now.to_string()),
        Value::String(s) => Sql::Text(s.clone()),
        Value::Bool(b) => Sql::Integer(*b as i64),
        Value::Number(n) if n.is_i64() => Sql::Integer(n.as_i64()?),
        Value::Number(n) => Sql::Real(n.as_f64()?),
        Value::Null => Sql::Null,
        _ => return None,
    })
}

/// INSERT with the model's Python-side defaults for every column not given; the new row's id
pub fn insert(db: &Connection, table: &str, values: &[(&str, Sql)]) -> Option<i64> {
    let now = now_stamp();
    let mut cols: Vec<String> = values.iter().map(|(c, _)| c.to_string()).collect();
    let mut vals: Vec<Sql> = values.iter().map(|(_, v)| v.clone()).collect();
    for (col, spec) in defaults(table)? {
        if let Some(d) = spec.get("default") {
            if !cols.iter().any(|c| c == col) {
                cols.push(col.clone());
                vals.push(to_sql(d, &now)?);
            }
        }
    }
    let marks: Vec<String> = (1..=cols.len()).map(|i| format!("?{i}")).collect();
    let sql = format!("INSERT INTO {table} ({}) VALUES ({})", cols.join(", "), marks.join(", "));
    db.execute(&sql, rusqlite::params_from_iter(vals.iter())).ok()?;
    Some(db.last_insert_rowid())
}

/// UPDATE one row by id, with the model's `onupdate` columns (updated_at) set as SQLAlchemy sets them - only when a column actually
/// changes, as the ORM only issues an UPDATE (and so only fires onupdate) for a dirty row. Returns whether anything was written.
pub fn update(db: &Connection, table: &str, id: i64, values: &[(&str, Sql)]) -> Option<bool> {
    // SQLAlchemy writes only the columns whose value changed; no change at all = no UPDATE (and no updated_at bump)
    let mut changed: Vec<(String, Sql)> = vec![];
    for (col, v) in values {
        let cur: Sql = db.query_row(&format!("SELECT {col} FROM {table} WHERE id = ?1"), [id], |r| r.get(0)).ok()?;
        if !same(&cur, v) {
            changed.push((col.to_string(), v.clone()));
        }
    }
    if changed.is_empty() {
        return Some(false);
    }
    let now = now_stamp();
    for (col, spec) in defaults(table)? {
        if let Some(d) = spec.get("onupdate") {
            if !changed.iter().any(|(c, _)| c == col) {
                changed.push((col.clone(), to_sql(d, &now)?));
            }
        }
    }
    let sets: Vec<String> = changed.iter().enumerate().map(|(i, (c, _))| format!("{c} = ?{}", i + 1)).collect();
    let mut vals: Vec<Sql> = changed.into_iter().map(|(_, v)| v).collect();
    vals.push(Sql::Integer(id));
    let sql = format!("UPDATE {table} SET {} WHERE id = ?{}", sets.join(", "), vals.len());
    db.execute(&sql, rusqlite::params_from_iter(vals.iter())).ok()?;
    Some(true)
}

/// Whether SQLAlchemy would see the stored value and the new one as equal (no change to flush)
fn same(a: &Sql, b: &Sql) -> bool {
    match (a, b) {
        (Sql::Integer(x), Sql::Integer(y)) => x == y,
        (Sql::Real(x), Sql::Real(y)) => x == y,
        (Sql::Integer(x), Sql::Real(y)) | (Sql::Real(y), Sql::Integer(x)) => (*x as f64) == *y,
        (Sql::Text(x), Sql::Text(y)) => x == y,
        (Sql::Null, Sql::Null) => true,
        _ => false,
    }
}

/// `live.touch(world)`: open live pages of the world learn something changed (app/shared_state.py)
pub fn touch(db: &Connection, world_id: i64) -> Option<()> {
    db.execute("INSERT INTO shared_state (key, n) VALUES (?1, 1) ON CONFLICT(key) DO UPDATE SET n = n + 1", [format!("live:{world_id}")]).ok()?;
    Some(())
}

/// `query.filter(...).update({...})`: a bulk UPDATE, which SQLAlchemy sends with the table's `onupdate` columns set too
pub fn bulk_update(db: &Connection, table: &str, set: &[(&str, Sql)], where_sql: &str, where_params: &[Sql]) -> Option<usize> {
    let now = now_stamp();
    let mut cols: Vec<(String, Sql)> = set.iter().map(|(c, v)| (c.to_string(), v.clone())).collect();
    for (col, spec) in defaults(table)? {
        if let Some(d) = spec.get("onupdate") {
            if !cols.iter().any(|(c, _)| c == col) {
                cols.push((col.clone(), to_sql(d, &now)?));
            }
        }
    }
    let sets: Vec<String> = cols.iter().enumerate().map(|(i, (c, _))| format!("{c} = ?{}", i + 1)).collect();
    let mut vals: Vec<Sql> = cols.into_iter().map(|(_, v)| v).collect();
    vals.extend(where_params.iter().cloned());
    // where_sql uses anonymous `?`: SQLite numbers those after the highest ?N of the SET list
    db.execute(&format!("UPDATE {table} SET {} WHERE {where_sql}", sets.join(", ")), rusqlite::params_from_iter(vals.iter())).ok()
}
