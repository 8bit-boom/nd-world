//! What a media file says about itself (a port of app/media_meta.py): title, artist, album, genre, year, lyrics, comments, duration,
//! dimensions and an AI picture's prompt - the AI renamer's clues. Native readers for MP3 (ID3v1/v2), FLAC, Ogg Vorbis/Opus, WAV and
//! MP4/M4A/MOV; ffprobe fills what they could not. Pictures: PNG text chunks, JPEG / WebP / GIF comments, XMP and EXIF, read the way
//! Pillow reads them. It never fails: an unreadable file gives what was read before the trouble.

use serde_json::{json, Map, Value};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

const MAX_TAG_BYTES: usize = 8 * 1024 * 1024;
const MAX_ATOM_BYTES: u64 = 32 * 1024 * 1024;

pub type Meta = Map<String, Value>;

fn limit_of(key: &str) -> usize {
    match key {
        "lyrics" => 4000,
        "comment" | "description" => 1000,
        "prompt" => 1500,
        "keywords" => 300,
        _ => 200,
    }
}

/// `_clean`
fn clean(value: &str, limit: usize) -> String {
    let text: String = value.chars().filter(|c| !matches!(*c as u32, 0x00..=0x08 | 0x0b | 0x0c | 0x0e..=0x1f | 0x7f)).collect();
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut out = String::new();
    let mut blank = false;
    for c in text.chars() {
        if c == ' ' || c == '\t' {
            if !blank {
                out.push(' ');
            }
            blank = true;
        } else {
            out.push(c);
            blank = false;
        }
    }
    crate::edits::py_strip_pub(&out).chars().take(limit).collect()
}

/// `_put` for text
fn put(meta: &mut Meta, key: &str, value: &str) {
    let text = clean(value, limit_of(key));
    if !text.is_empty() && !meta.contains_key(key) {
        meta.insert(key.into(), json!(text));
    }
}

/// `round(x, 1)` (correctly rounded, ties to even, as Python does)
fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

/// `_put` for duration_s
fn put_duration(meta: &mut Meta, seconds: f64) {
    if seconds > 0.0 && seconds.is_finite() && !meta.contains_key("duration_s") {
        meta.insert("duration_s".into(), json!(round1(seconds)));
    }
}

/// `_put` for width / height
fn put_int(meta: &mut Meta, key: &str, n: i64) {
    if n > 0 && !meta.contains_key(key) {
        meta.insert(key.into(), json!(n));
    }
}

fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

fn utf8_replace(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn utf16(b: &[u8], big: bool) -> Option<String> {
    if b.len() % 2 != 0 {
        return None;
    }
    let units: Vec<u16> = b.chunks(2).map(|p| if big { u16::from_be_bytes([p[0], p[1]]) } else { u16::from_le_bytes([p[0], p[1]]) }).collect();
    String::from_utf16(&units).ok()
}

/// `_decode_text`
fn decode_text(enc: u8, data: &[u8]) -> String {
    let strict = match enc {
        1 => {
            if data.starts_with(&[0xFF, 0xFE]) {
                utf16(&data[2..], false)
            } else if data.starts_with(&[0xFE, 0xFF]) {
                utf16(&data[2..], true)
            } else {
                utf16(data, false)
            }
        }
        2 => utf16(data, true),
        3 => return utf8_replace(data),
        _ => return latin1(data),
    };
    strict.unwrap_or_else(|| utf8_replace(data))
}

/// `_split_terminated`
fn split_terminated(enc: u8, data: &[u8]) -> (Vec<u8>, Vec<u8>) {
    if enc == 1 || enc == 2 {
        let mut i = 0;
        while i + 1 < data.len() {
            if data[i] == 0 && data[i + 1] == 0 {
                return (data[..i].to_vec(), data[i + 2..].to_vec());
            }
            i += 2;
        }
        return (data.to_vec(), vec![]);
    }
    match data.iter().position(|&b| b == 0) {
        Some(i) => (data[..i].to_vec(), data[i + 1..].to_vec()),
        None => (data.to_vec(), vec![]),
    }
}

fn synchsafe(b: &[u8]) -> usize {
    ((b[0] as usize) << 21) | ((b[1] as usize) << 14) | ((b[2] as usize) << 7) | b[3] as usize
}

fn be32(b: &[u8]) -> Option<u32> {
    Some(u32::from_be_bytes(b.get(..4)?.try_into().ok()?))
}

fn le32(b: &[u8]) -> Option<u32> {
    Some(u32::from_le_bytes(b.get(..4)?.try_into().ok()?))
}

fn slice(data: &[u8], a: usize, b: usize) -> &[u8] {
    let a = a.min(data.len());
    &data[a..b.min(data.len()).max(a)]
}

fn id3_key(fid: &str) -> Option<&'static str> {
    Some(match fid {
        "TIT2" | "TT2" => "title",
        "TPE1" | "TP1" => "artist",
        "TALB" | "TAL" => "album",
        "TCON" | "TCO" => "genre",
        "TYER" | "TYE" | "TDRC" => "year",
        _ => return None,
    })
}

/// `_parse_id3v2`; None: Python would have raised here (what was read so far stays)
fn parse_id3v2(data: &[u8], meta: &mut Meta) -> Option<()> {
    if data.len() < 10 || &data[..3] != b"ID3" {
        return Some(());
    }
    let (version, flags) = (data[3], data[5]);
    let end = data.len().min(10 + synchsafe(&data[6..10]));
    let mut pos = 10usize;
    if flags & 0x40 != 0 && version >= 3 && end >= pos + 4 {
        let ext = if version == 4 { synchsafe(&data[pos..pos + 4]) } else { be32(&data[pos..])? as usize + 4 };
        pos += ext;
    }
    let (id_len, header_len) = if version == 2 { (3, 6) } else { (4, 10) };
    while pos + header_len <= end {
        let frame_id = &data[pos..pos + id_len];
        if frame_id[0] == 0 {
            break;
        }
        let size = if version == 2 {
            u32::from_be_bytes([0, data[pos + 3], data[pos + 4], data[pos + 5]]) as usize
        } else if version == 4 {
            synchsafe(&data[pos + 4..pos + 8])
        } else {
            be32(&data[pos + 4..])? as usize
        };
        let body = slice(data, pos + header_len, pos + header_len + size).to_vec();
        pos += header_len + size;
        if body.is_empty() || size == 0 {
            continue;
        }
        let fid = latin1(frame_id);
        let (enc, payload) = (body[0], &body[1..]);
        if let Some(key) = id3_key(&fid) {
            let mut text = decode_text(enc, payload);
            if key == "genre" {
                static G: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
                let cut = G.get_or_init(|| regex::Regex::new(r"^\(\d+\)").unwrap()).replace(&text, "").into_owned();
                if !cut.is_empty() {
                    text = cut;
                }
            }
            if key == "year" {
                text = text.chars().take(4).collect();
            }
            put(meta, key, text.split('\0').next().unwrap_or(""));
        } else if fid == "TLEN" {
            let digits: String = decode_text(enc, payload).chars().filter(|c| c.is_numeric()).collect();
            if !digits.chars().all(|c| c.is_ascii_digit()) {
                return None; // a non-ASCII digit: int() reads it, this port does not
            }
            let ms: f64 = if digits.is_empty() { 0.0 } else { digits.parse().ok()? };
            put_duration(meta, ms / 1000.0);
        } else if fid == "USLT" || fid == "ULT" || fid == "COMM" || fid == "COM" {
            let (_desc, text) = split_terminated(enc, slice(payload, 3, payload.len()));
            put(meta, if fid.starts_with('U') { "lyrics" } else { "comment" }, &decode_text(enc, &text));
        } else if fid == "TXXX" {
            let (desc, value) = split_terminated(enc, payload);
            let d = crate::edits::py_strip_pub(&decode_text(enc, &desc)).to_lowercase();
            if matches!(d.as_str(), "lyrics" | "unsyncedlyrics" | "unsynced lyrics") {
                put(meta, "lyrics", &decode_text(enc, &value));
            }
        }
    }
    Some(())
}

fn read_mp3(f: &mut std::fs::File, size: u64, meta: &mut Meta) -> Option<()> {
    let mut head = [0u8; 10];
    f.seek(SeekFrom::Start(0)).ok()?;
    let n = read_up_to(f, &mut head)?;
    if n >= 10 && &head[..3] == b"ID3" {
        let tag = synchsafe(&head[6..10]).min(MAX_TAG_BYTES);
        f.seek(SeekFrom::Start(0)).ok()?;
        let data = read_n(f, 10 + tag)?;
        parse_id3v2(&data, meta)?;
    }
    if size >= 128 {
        f.seek(SeekFrom::Start(size - 128)).ok()?;
        let tail = read_n(f, 128)?;
        if tail.len() == 128 && &tail[..3] == b"TAG" {
            let field = |a: usize, b: usize| latin1(tail[a..b].split(|&c| c == 0).next().unwrap_or(&[]));
            put(meta, "title", &field(3, 33));
            put(meta, "artist", &field(33, 63));
            put(meta, "album", &field(63, 93));
            put(meta, "year", &field(93, 97));
        }
    }
    Some(())
}

fn read_up_to(f: &mut std::fs::File, buf: &mut [u8]) -> Option<usize> {
    let mut n = 0;
    while n < buf.len() {
        match f.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(_) => return None,
        }
    }
    Some(n)
}

fn read_n(f: &mut std::fs::File, n: usize) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(n.min(1 << 20));
    f.take(n as u64).read_to_end(&mut out).ok()?;
    Some(out)
}

fn vorbis_key(k: &str) -> Option<&'static str> {
    Some(match k {
        "TITLE" => "title",
        "ARTIST" | "ALBUMARTIST" => "artist",
        "ALBUM" => "album",
        "GENRE" => "genre",
        "DATE" | "YEAR" => "year",
        "LYRICS" | "UNSYNCEDLYRICS" => "lyrics",
        "COMMENT" => "comment",
        "DESCRIPTION" => "description",
        _ => return None,
    })
}

/// `_parse_vorbis_comment` (a short read ends it, like Python's struct.error)
fn parse_vorbis_comment(data: &[u8], meta: &mut Meta) {
    let Some(vendor) = le32(data) else { return };
    let mut pos = 4 + vendor as usize;
    let Some(count) = data.get(pos..).and_then(le32) else { return };
    pos += 4;
    for _ in 0..count.min(500) {
        let Some(n) = data.get(pos..).and_then(le32) else { return };
        let n = n as usize;
        let entry = utf8_replace(slice(data, pos + 4, pos + 4 + n));
        pos += 4 + n;
        let (key, value) = entry.split_once('=').unwrap_or((&entry, ""));
        if let Some(target) = vorbis_key(&key.to_uppercase()) {
            let v: String = if target == "year" { value.chars().take(4).collect() } else { value.to_string() };
            put(meta, target, &v);
        }
    }
}

fn read_flac(f: &mut std::fs::File, meta: &mut Meta) -> Option<()> {
    f.seek(SeekFrom::Start(4)).ok()?;
    for _ in 0..64 {
        let header = read_n(f, 4)?;
        if header.len() < 4 {
            break;
        }
        let (last, kind, length) = (header[0] & 0x80, header[0] & 0x7F, u32::from_be_bytes([0, header[1], header[2], header[3]]) as usize);
        if kind == 0 && length >= 18 {
            let block = read_n(f, length)?;
            let mut packed: u64 = 0;
            for &b in slice(&block, 10, 18) {
                packed = (packed << 8) | b as u64;
            }
            let (rate, samples) = (packed >> 44, packed & ((1u64 << 36) - 1));
            if rate != 0 {
                put_duration(meta, samples as f64 / rate as f64);
            }
        } else if kind == 4 && length <= MAX_TAG_BYTES {
            let block = read_n(f, length)?;
            parse_vorbis_comment(&block, meta);
        } else {
            f.seek(SeekFrom::Current(length as i64)).ok()?;
        }
        if last != 0 {
            break;
        }
    }
    Some(())
}

/// `_ogg_packets`
fn ogg_packets(data: &[u8], wanted: usize) -> Vec<Vec<u8>> {
    let (mut packets, mut current, mut pos) = (vec![], vec![], 0usize);
    while pos + 27 <= data.len() && packets.len() < wanted {
        if &data[pos..pos + 4] != b"OggS" {
            break;
        }
        let nseg = data[pos + 26] as usize;
        let table = slice(data, pos + 27, pos + 27 + nseg).to_vec();
        let mut body = pos + 27 + nseg;
        for lace in table {
            current.extend_from_slice(slice(data, body, body + lace as usize));
            body += lace as usize;
            if lace < 255 {
                packets.push(std::mem::take(&mut current));
            }
        }
        pos = body;
    }
    packets
}

fn read_ogg(f: &mut std::fs::File, size: u64, meta: &mut Meta) -> Option<()> {
    f.seek(SeekFrom::Start(0)).ok()?;
    let head = read_n(f, 512 * 1024)?;
    let packets = ogg_packets(&head, 2);
    let mut rate: u64 = 0;
    if let Some(first) = packets.first() {
        if first.starts_with(b"OpusHead") {
            rate = 48000;
        } else if first.starts_with(b"\x01vorbis") && first.len() >= 16 {
            rate = le32(&first[12..16])? as u64;
        }
    }
    if let Some(tags) = packets.get(1) {
        if tags.starts_with(b"OpusTags") {
            parse_vorbis_comment(&tags[8..], meta);
        } else if tags.starts_with(b"\x03vorbis") {
            parse_vorbis_comment(&tags[7..], meta);
        }
    }
    if rate != 0 && size > 27 {
        f.seek(SeekFrom::Start(size.saturating_sub(65536))).ok()?;
        let mut tail = vec![];
        f.read_to_end(&mut tail).ok()?;
        if let Some(i) = tail.windows(4).rposition(|w| w == b"OggS") {
            if i + 14 <= tail.len() {
                let granule = i64::from_le_bytes(tail[i + 6..i + 14].try_into().ok()?);
                if granule > 0 {
                    put_duration(meta, granule as f64 / rate as f64);
                }
            }
        }
    }
    Some(())
}

fn wav_key(id: &[u8]) -> Option<&'static str> {
    Some(match id {
        b"INAM" => "title",
        b"IART" => "artist",
        b"IPRD" => "album",
        b"ICMT" => "comment",
        b"IGNR" => "genre",
        b"ICRD" => "year",
        b"ISFT" => "software",
        _ => return None,
    })
}

fn read_wav(f: &mut std::fs::File, meta: &mut Meta) -> Option<()> {
    f.seek(SeekFrom::Start(12)).ok()?;
    let mut byte_rate: u32 = 0;
    for _ in 0..64 {
        let header = read_n(f, 8)?;
        if header.len() < 8 {
            break;
        }
        let (cid, length) = (&header[..4], le32(&header[4..])? as u64);
        let padded = length + (length & 1);
        if cid == b"fmt " && length >= 12 {
            let fmt = read_n(f, length as usize)?;
            byte_rate = le32(fmt.get(8..12)?)?;
            f.seek(SeekFrom::Current((padded - length) as i64)).ok()?;
        } else if cid == b"data" {
            if byte_rate != 0 {
                put_duration(meta, length as f64 / byte_rate as f64);
            }
            f.seek(SeekFrom::Current(padded as i64)).ok()?;
        } else if cid == b"LIST" && length as usize <= MAX_TAG_BYTES {
            let block = read_n(f, padded as usize)?;
            if block.starts_with(b"INFO") {
                let mut pos = 4;
                while pos + 8 <= block.len() {
                    let (sub, n) = (&block[pos..pos + 4], le32(&block[pos + 4..pos + 8])? as usize);
                    if let Some(key) = wav_key(sub) {
                        let raw = slice(&block, pos + 8, pos + 8 + n);
                        let text = utf8_replace(raw.split(|&c| c == 0).next().unwrap_or(&[]));
                        let text: String = text.chars().take(if key == "year" { 4 } else { 400 }).collect();
                        put(meta, key, &text);
                    }
                    pos += 8 + n + (n & 1);
                }
            }
        } else if (cid == b"id3 " || cid == b"ID3 ") && length as usize <= MAX_TAG_BYTES {
            let block = read_n(f, padded as usize)?;
            parse_id3v2(&block, meta)?;
        } else {
            f.seek(SeekFrom::Current(padded as i64)).ok()?;
        }
    }
    Some(())
}

/// `_atoms`: (type, payload start, payload end) for the atoms in data[start..end]
fn atoms(data: &[u8], start: usize, end: usize) -> Vec<([u8; 4], usize, usize)> {
    let mut out = vec![];
    let mut pos = start;
    while pos + 8 <= end {
        let mut size = be32(&data[pos..]).unwrap_or(0) as u64;
        let kind: [u8; 4] = data[pos + 4..pos + 8].try_into().unwrap_or([0; 4]);
        let mut header = 8u64;
        if size == 1 && pos + 16 <= end {
            size = u64::from_be_bytes(data[pos + 8..pos + 16].try_into().unwrap_or([0; 8]));
            header = 16;
        } else if size == 0 {
            size = (end - pos) as u64;
        }
        if size < header || pos as u64 + size > end as u64 {
            break;
        }
        out.push((kind, pos + header as usize, pos + size as usize));
        pos += size as usize;
    }
    out
}

fn mp4_key(k: &[u8; 4]) -> Option<&'static str> {
    Some(match k {
        b"\xa9nam" => "title",
        b"\xa9ART" | b"aART" => "artist",
        b"\xa9alb" => "album",
        b"\xa9gen" => "genre",
        b"\xa9day" => "year",
        b"\xa9lyr" => "lyrics",
        b"\xa9cmt" => "comment",
        b"desc" | b"ldes" => "description",
        b"\xa9too" => "software",
        _ => return None,
    })
}

fn walk_mp4(data: &[u8], start: usize, end: usize, meta: &mut Meta, depth: usize) {
    if depth > 8 {
        return;
    }
    for (kind, a, b) in atoms(data, start, end) {
        match &kind {
            b"mvhd" if b - a >= 20 => {
                let (scale, dur) = if data[a] == 1 && b - a >= 32 {
                    (be32(&data[a + 20..]).unwrap_or(0) as u64, u64::from_be_bytes(data[a + 24..a + 32].try_into().unwrap_or([0; 8])))
                } else {
                    (be32(&data[a + 12..]).unwrap_or(0) as u64, be32(&data[a + 16..]).unwrap_or(0) as u64)
                };
                if scale != 0 {
                    put_duration(meta, dur as f64 / scale as f64);
                }
            }
            b"tkhd" if b - a >= 84 => {
                let offset = if data[a] == 0 { 76 } else { 88 };
                if b - a >= offset + 8 {
                    let (w, h) = (be32(&data[a + offset..]).unwrap_or(0), be32(&data[a + offset + 4..]).unwrap_or(0));
                    if w >> 16 != 0 && h >> 16 != 0 {
                        put_int(meta, "width", (w >> 16) as i64);
                        put_int(meta, "height", (h >> 16) as i64);
                    }
                }
            }
            b"meta" => walk_mp4(data, a + 4, b, meta, depth + 1),
            b"ilst" => {
                for (item, ia, ib) in atoms(data, a, b) {
                    let Some(key) = mp4_key(&item) else { continue };
                    for (sub, sa, sb) in atoms(data, ia, ib) {
                        if &sub == b"data" && sb - sa > 8 {
                            let text: String = utf8_replace(&data[sa + 8..sb]).chars().take(if key == "year" { 4 } else { 4000 }).collect();
                            put(meta, key, &text);
                            break;
                        }
                    }
                }
            }
            b"moov" | b"udta" | b"trak" | b"mdia" | b"minf" | b"stbl" | b"edts" => walk_mp4(data, a, b, meta, depth + 1),
            _ => {}
        }
    }
}

fn read_mp4(f: &mut std::fs::File, size: u64, meta: &mut Meta) -> Option<()> {
    let mut pos: u64 = 0;
    for _ in 0..64 {
        f.seek(SeekFrom::Start(pos)).ok()?;
        let header = read_n(f, 16)?;
        if header.len() < 8 {
            return Some(());
        }
        let mut length = be32(&header).unwrap_or(0) as u64;
        let kind = &header[4..8];
        let mut head = 8u64;
        if length == 1 && header.len() >= 16 {
            length = u64::from_be_bytes(header[8..16].try_into().ok()?);
            head = 16;
        } else if length == 0 {
            length = size.saturating_sub(pos);
        }
        if length < head {
            return Some(());
        }
        if kind == b"moov" {
            if length - head > MAX_ATOM_BYTES {
                return Some(());
            }
            f.seek(SeekFrom::Start(pos + head)).ok()?;
            let body = read_n(f, (length - head) as usize)?;
            walk_mp4(&body, 0, body.len(), meta, 0);
            return Some(());
        }
        pos += length;
    }
    Some(())
}

/// `_merge_ffprobe`: fills only what is still missing
fn merge_ffprobe(path: &Path, meta: &mut Meta) {
    let out = std::process::Command::new("ffprobe").args(["-v", "error", "-show_format", "-show_streams", "-of", "json"]).arg(path)
        .stdin(std::process::Stdio::null()).stderr(std::process::Stdio::null()).output();
    let Ok(out) = out else { return };
    if !out.status.success() {
        return;
    }
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&utf8_replace(&out.stdout)) else { return };
    let empty = Map::new();
    let fmt = data.get("format").and_then(|v| v.as_object()).unwrap_or(&empty);
    let mut tags: Vec<(String, Value)> = vec![];
    let mut set = |k: String, v: Value, overwrite: bool| match tags.iter_mut().find(|(t, _)| *t == k) {
        Some(e) if overwrite => e.1 = v,
        Some(_) => {}
        None => tags.push((k, v)),
    };
    for (k, v) in fmt.get("tags").and_then(|v| v.as_object()).unwrap_or(&empty) {
        set(k.to_lowercase(), v.clone(), true);
    }
    let streams: Vec<Value> = data.get("streams").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    for s in &streams {
        for (k, v) in s.get("tags").and_then(|v| v.as_object()).unwrap_or(&empty) {
            set(k.to_lowercase(), v.clone(), false);
        }
    }
    let tag = |k: &str| tags.iter().find(|(t, _)| t == k).map(|(_, v)| v.clone()).filter(|v| crate::edits::py_truthy_pub(Some(v)));
    for (key, source) in [("title", "title"), ("artist", "artist"), ("album", "album"), ("genre", "genre"), ("year", "date"), ("lyrics", "lyrics"),
        ("comment", "comment"), ("description", "description"), ("software", "encoder")] {
        if let Some(v) = tag(source) {
            let text = match &v { Value::String(s) => s.clone(), other => crate::ai::py_repr(other) };
            put(meta, key, &if key == "year" { text.chars().take(4).collect() } else { text });
        }
    }
    if let Some(d) = fmt.get("duration").and_then(|d| match d { Value::String(s) => s.trim().parse::<f64>().ok(), Value::Number(n) => n.as_f64(), _ => None }) {
        put_duration(meta, d);
    }
    for s in &streams {
        if s.get("codec_type").and_then(|v| v.as_str()) == Some("video") && crate::edits::py_truthy_pub(s.get("width")) {
            if let Some(w) = s.get("width").and_then(|v| v.as_i64()) {
                put_int(meta, "width", w);
            }
            if let Some(h) = s.get("height").and_then(|v| v.as_i64()) {
                put_int(meta, "height", h);
            }
            break;
        }
    }
}

// ── pictures ─────────────────────────────────────────────────────────────────

/// What Pillow puts in `img.info` that the renamer reads (text values; bytes values as their UTF-8 / UTF-16 text later)
#[derive(Default)]
struct ImageInfo {
    format: String,
    width: i64,
    height: i64,
    text: Vec<(String, Value)>,
    exif: Option<Vec<u8>>,
}

fn png_info(data: &[u8]) -> Option<ImageInfo> {
    let mut info = ImageInfo { format: "PNG".into(), ..Default::default() };
    let mut pos = 8;
    let mut before_idat = true;
    while pos + 12 <= data.len() {
        let len = be32(&data[pos..])? as usize;
        let kind = &data[pos + 4..pos + 8];
        let body = slice(data, pos + 8, pos + 8 + len);
        match kind {
            b"IHDR" if body.len() >= 8 => {
                info.width = be32(body)? as i64;
                info.height = be32(&body[4..])? as i64;
            }
            b"IDAT" => before_idat = false,
            b"tEXt" if before_idat => {
                if let Some(i) = body.iter().position(|&b| b == 0) {
                    let k = latin1(&body[..i]);
                    let v = &body[i + 1..];
                    info.text.push((k.clone(), if k == "exif" { Value::Null } else { json!(latin1(v)) }));
                }
            }
            b"zTXt" if before_idat => {
                if let Some(i) = body.iter().position(|&b| b == 0) {
                    let k = latin1(&body[..i]);
                    let comp = slice(body, i + 2, body.len());
                    let mut v = vec![];
                    if std::io::Read::read_to_end(&mut flate2::read::ZlibDecoder::new(comp), &mut v).is_ok() {
                        info.text.push((k, json!(latin1(&v))));
                    }
                }
            }
            b"iTXt" if before_idat => {
                let i = body.iter().position(|&b| b == 0)?;
                let k = latin1(&body[..i]);
                let (cflag, rest) = (*body.get(i + 1)?, slice(body, i + 3, body.len()));
                let j = rest.iter().position(|&b| b == 0)?;
                let rest = &rest[j + 1..];
                let k2 = rest.iter().position(|&b| b == 0)?;
                let raw = &rest[k2 + 1..];
                let v = if cflag != 0 {
                    let mut out = vec![];
                    if std::io::Read::read_to_end(&mut flate2::read::ZlibDecoder::new(raw), &mut out).is_err() {
                        pos += 12 + len;
                        continue;
                    }
                    out
                } else {
                    raw.to_vec()
                };
                if let Ok(v) = String::from_utf8(v) {
                    info.text.push((k, json!(v))); // undecodable: Pillow skips the chunk
                }
            }
            b"eXIf" => info.exif = Some(body.strip_prefix(b"Exif\0\0").unwrap_or(body).to_vec()),
            b"IEND" => break,
            _ => {}
        }
        pos += 12 + len;
    }
    Some(info)
}

fn jpeg_info(data: &[u8]) -> Option<ImageInfo> {
    let mut info = ImageInfo { format: "JPEG".into(), ..Default::default() };
    let mut pos = 2;
    let mut comments: Vec<u8> = vec![];
    while pos + 4 <= data.len() {
        if data[pos] != 0xFF {
            return None;
        }
        let marker = data[pos + 1];
        if marker == 0xD8 || (0xD0..=0xD7).contains(&marker) || marker == 0x01 || marker == 0xFF {
            pos += if marker == 0xFF { 1 } else { 2 };
            continue;
        }
        let len = u16::from_be_bytes([data[pos + 2], data[pos + 3]]) as usize;
        let body = slice(data, pos + 4, pos + 2 + len);
        match marker {
            0xE1 if body.starts_with(b"Exif\0\0") && info.exif.is_none() => info.exif = Some(body[6..].to_vec()),
            0xE1 if body.starts_with(b"http://ns.adobe.com/xap/1.0/\0") => {
                info.text.push(("xmp".into(), json!(utf8_replace(&body[29..]))));
            }
            0xFE => {
                if comments.is_empty() {
                    comments = body.to_vec();
                }
            }
            0xC0..=0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF if body.len() >= 5 => {
                info.height = u16::from_be_bytes([body[1], body[2]]) as i64;
                info.width = u16::from_be_bytes([body[3], body[4]]) as i64;
            }
            0xDA => break,
            _ => {}
        }
        pos += 2 + len;
    }
    if !comments.is_empty() {
        info.text.push(("comment".into(), json!(utf8_replace(&comments))));
    }
    Some(info)
}

fn webp_info(data: &[u8]) -> Option<ImageInfo> {
    let mut info = ImageInfo { format: "WEBP".into(), ..Default::default() };
    let (w, h) = image::ImageReader::new(std::io::Cursor::new(data)).with_guessed_format().ok()?.into_dimensions().ok()?;
    info.width = w as i64;
    info.height = h as i64;
    let mut pos = 12;
    while pos + 8 <= data.len() {
        let kind = &data[pos..pos + 4];
        let len = le32(&data[pos + 4..])? as usize;
        let body = slice(data, pos + 8, pos + 8 + len);
        match kind {
            b"EXIF" => info.exif = Some(body.strip_prefix(b"Exif\0\0").unwrap_or(body).to_vec()),
            b"XMP " => info.text.push(("xmp".into(), json!(utf8_replace(body)))),
            _ => {}
        }
        pos += 8 + len + (len & 1);
    }
    Some(info)
}

fn gif_info(data: &[u8]) -> Option<ImageInfo> {
    let mut info = ImageInfo { format: "GIF".into(), ..Default::default() };
    info.width = u16::from_le_bytes([*data.get(6)?, *data.get(7)?]) as i64;
    info.height = u16::from_le_bytes([*data.get(8)?, *data.get(9)?]) as i64;
    let flags = *data.get(10)?;
    let mut pos = 13 + if flags & 0x80 != 0 { 3 * (1usize << ((flags & 7) + 1)) } else { 0 };
    while pos < data.len() {
        match data[pos] {
            0x21 => {
                let label = *data.get(pos + 1)?;
                let mut p = pos + 2;
                let mut block = vec![];
                while let Some(&n) = data.get(p) {
                    if n == 0 {
                        break;
                    }
                    block.extend_from_slice(slice(data, p + 1, p + 1 + n as usize));
                    p += 1 + n as usize;
                }
                if label == 0xFE {
                    info.text.push(("comment".into(), json!(utf8_replace(&block))));
                }
                pos = p + 1;
            }
            _ => break, // the first frame: Pillow reads the comment that precedes it
        }
    }
    Some(info)
}

/// EXIF (a TIFF structure): the IFD0 tags the renamer reads, and DateTimeOriginal from the Exif IFD
fn exif_tags(tiff: &[u8]) -> Vec<(u16, Value)> {
    let mut out = vec![];
    if tiff.len() < 8 {
        return out;
    }
    let le = &tiff[..2] == b"II";
    if !le && &tiff[..2] != b"MM" {
        return out;
    }
    let u16_at = |p: usize| tiff.get(p..p + 2).map(|b| if le { u16::from_le_bytes([b[0], b[1]]) } else { u16::from_be_bytes([b[0], b[1]]) });
    let u32_at = |p: usize| tiff.get(p..p + 4).map(|b| { let a: [u8; 4] = b.try_into().unwrap(); if le { u32::from_le_bytes(a) } else { u32::from_be_bytes(a) } });
    let read_ifd = |off: usize, out: &mut Vec<(u16, Value)>, wanted: &[u16]| -> Option<u32> {
        let n = u16_at(off)? as usize;
        let mut sub = None;
        for i in 0..n {
            let e = off + 2 + 12 * i;
            let (tag, typ, count) = (u16_at(e)?, u16_at(e + 2)?, u32_at(e + 4)? as usize);
            if tag == 0x8769 {
                sub = u32_at(e + 8);
                continue;
            }
            if !wanted.contains(&tag) {
                continue;
            }
            let unit = match typ { 1 | 2 | 7 => 1, 3 => 2, 4 | 9 => 4, _ => continue };
            let size = unit * count;
            let at = if size <= 4 { e + 8 } else { u32_at(e + 8)? as usize };
            let raw = tiff.get(at..at + size)?;
            let v = match typ {
                2 => {
                    let raw = raw.strip_suffix(&[0]).unwrap_or(raw);
                    json!(latin1(raw))
                }
                1 | 7 => Value::Array(raw.iter().map(|b| json!(b)).collect()),
                _ => continue,
            };
            out.push((tag, v));
        }
        Some(sub.unwrap_or(0))
    };
    let Some(first) = u32_at(4) else { return out };
    let sub = read_ifd(first as usize, &mut out, &[0x010E, 0x013B, 0x0131, 0x9C9B, 0x9C9C, 0x9C9E, 0x9C9F]).unwrap_or(0);
    if sub != 0 {
        let mut inner = vec![];
        let _ = read_ifd(sub as usize, &mut inner, &[0x9003]);
        out.extend(inner);
    }
    out
}

/// `_prompt_from_generation_data`
fn generation_prompt(info: &[(String, Value)]) -> String {
    let get = |k: &str| info.iter().rev().find(|(t, _)| t == k).map(|(_, v)| v.clone());
    if let Some(Value::String(params)) = get("parameters") {
        if !crate::edits::py_strip_pub(&params).is_empty() {
            let text = crate::edits::py_strip_pub(&params);
            if text.starts_with('{') {
                if let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&text) {
                    let inner = match data.get("sui_image_params") { Some(v) if crate::edits::py_truthy_pub(Some(v)) => v.clone(), _ => Value::Object(data.clone()) };
                    if let Some(Value::String(p)) = inner.get("prompt") {
                        return p.clone();
                    }
                }
            }
            static SPLIT: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
            let re = SPLIT.get_or_init(|| regex::Regex::new(r"\n\s*Negative prompt:|\n\s*Steps:").unwrap());
            let head = re.split(&text).next().unwrap_or("");
            return crate::edits::py_strip_pub(head);
        }
    }
    if let Some(Value::String(graph)) = get("prompt") {
        if crate::edits::py_strip_pub(&graph).starts_with('{') {
            let Ok(Value::Object(nodes)) = serde_json::from_str::<Value>(&graph) else { return String::new() };
            for n in nodes.values() {
                if n.get("class_type").and_then(|v| v.as_str()) == Some("CLIPTextEncode") {
                    if let Some(Value::String(t)) = n.get("inputs").and_then(|i| i.get("text")) {
                        if !crate::edits::py_strip_pub(t).is_empty() {
                            return t.clone();
                        }
                    }
                }
            }
        }
    }
    String::new()
}

fn read_image(path: &Path, meta: &mut Meta) -> Option<()> {
    let data = std::fs::read(path).ok()?;
    let info = if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        png_info(&data)?
    } else if data.starts_with(&[0xFF, 0xD8]) {
        jpeg_info(&data)?
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        webp_info(&data)?
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        gif_info(&data)?
    } else {
        let reader = image::ImageReader::new(std::io::Cursor::new(&data)).with_guessed_format().ok()?;
        let format = match reader.format()? { image::ImageFormat::Avif => "AVIF", image::ImageFormat::Bmp => "BMP", image::ImageFormat::Tiff => "TIFF", _ => return None };
        let (w, h) = reader.into_dimensions().ok()?;
        ImageInfo { format: format.into(), width: w as i64, height: h as i64, ..Default::default() }
    };
    put(meta, "format", &info.format);
    put_int(meta, "width", info.width);
    put_int(meta, "height", info.height);
    put(meta, "prompt", &generation_prompt(&info.text));
    let get = |k: &str| info.text.iter().rev().find(|(t, _)| t == k).map(|(_, v)| v.clone());
    for (key, target) in [("Title", "title"), ("Description", "description"), ("Comment", "comment"), ("comment", "comment"), ("Author", "artist"),
        ("Software", "software"), ("Creation Time", "created")] {
        if let Some(Value::String(v)) = get(key) {
            put(meta, target, &v);
        }
    }
    for xmp_key in ["XML:com.adobe.xmp", "xmp"] {
        if let Some(Value::String(text)) = get(xmp_key).filter(|v| crate::edits::py_truthy_pub(Some(v))) {
            static T: std::sync::OnceLock<[regex::Regex; 3]> = std::sync::OnceLock::new();
            let re = T.get_or_init(|| [
                regex::Regex::new(r"(?s)<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>").unwrap(),
                regex::Regex::new(r"(?s)<dc:description>.*?<rdf:li[^>]*>(.*?)</rdf:li>").unwrap(),
                regex::Regex::new(r"<[^>]+>").unwrap(),
            ]);
            for (pattern, target) in [(&re[0], "title"), (&re[1], "description")] {
                if let Some(m) = pattern.captures(&text).and_then(|c| c.get(1)) {
                    put(meta, target, &re[2].replace_all(m.as_str(), ""));
                }
            }
        }
    }
    if let Some(exif) = &info.exif {
        let tags = exif_tags(exif);
        let tag = |t: u16| tags.iter().find(|(k, _)| *k == t).map(|(_, v)| v.clone());
        let text = |v: Option<Value>| match v { Some(Value::String(s)) => s, Some(Value::Array(_)) | None => String::new(), Some(other) => crate::ai::py_repr(&other) };
        // _xp: the UTF-16LE text of a BYTE tag
        let xp = |v: Option<Value>| match v {
            Some(Value::Array(a)) => {
                let bytes: Vec<u8> = a.iter().filter_map(|b| b.as_u64()).map(|b| b as u8).collect();
                let units: Vec<u16> = bytes.chunks_exact(2).map(|p| u16::from_le_bytes([p[0], p[1]])).collect();
                char::decode_utf16(units).filter_map(|c| c.ok()).collect::<String>()
            }
            Some(Value::String(s)) => s,
            _ => String::new(),
        };
        put(meta, "description", &text(tag(0x010E)));
        put(meta, "artist", &text(tag(0x013B)));
        put(meta, "software", &text(tag(0x0131)));
        put(meta, "title", &xp(tag(0x9C9B)));
        put(meta, "comment", &xp(tag(0x9C9C)));
        put(meta, "keywords", &xp(tag(0x9C9E)).replace(';', ", "));
        put(meta, "description", &xp(tag(0x9C9F)));
        put(meta, "created", &text(tag(0x9003)));
    }
    Some(())
}

/// `read_metadata(path, kind)`
pub fn read_metadata(path: &Path, kind: &str) -> Meta {
    let mut meta = Meta::new();
    if !path.is_file() {
        return meta;
    }
    if kind == "image" {
        let _ = read_image(path, &mut meta);
        return meta;
    }
    let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let read = (|| -> Option<()> {
        let mut f = std::fs::File::open(path).ok()?;
        let mut head = [0u8; 12];
        let n = read_up_to(&mut f, &mut head)?;
        let head = &head[..n];
        if head.starts_with(b"ID3") || (head.len() >= 2 && head[0] == 0xFF && head[1] & 0xE0 == 0xE0) {
            read_mp3(&mut f, size, &mut meta)
        } else if head.starts_with(b"fLaC") {
            read_flac(&mut f, &mut meta)
        } else if head.starts_with(b"OggS") {
            read_ogg(&mut f, size, &mut meta)
        } else if head.len() >= 12 && &head[..4] == b"RIFF" && &head[8..12] == b"WAVE" {
            read_wav(&mut f, &mut meta)
        } else if head.len() >= 8 && &head[4..8] == b"ftyp" {
            read_mp4(&mut f, size, &mut meta)
        } else {
            Some(())
        }
    })();
    if read.is_some() && (!meta.contains_key("duration_s") || !meta.contains_key("title") || (kind == "video" && !meta.contains_key("width"))) {
        merge_ffprobe(path, &mut meta);
    }
    meta
}
