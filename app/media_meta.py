"""Best-effort metadata for the files in the audio / video / image libraries - what the file itself says about what it
is: title, artist, album, genre, year, lyrics, comments, duration, dimensions, and, for AI-generated pictures, the prompt
that made them. The AI renamer (app/media_rename.py) reads these as naming clues.

Pure Python for the common containers (MP3 ID3v1/v2, FLAC, Ogg Vorbis/Opus, WAV, MP4/M4A/MOV, PNG/JPEG/WebP via Pillow);
ffprobe, when it is installed, fills in whatever the native readers could not (Matroska/WebM, odd tags). A leaf module
(no app imports) and it never raises: an unreadable or hostile file just yields {} (every read is bounded).

Keys in the returned dict, all optional and only present when non-empty: title, artist, album, genre, year, lyrics,
comment, description, software, created, prompt, keywords, duration_s, width, height, format.
"""
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Optional

_LIMITS = {"lyrics": 4000, "comment": 1000, "description": 1000, "prompt": 1500, "keywords": 300}
_DEFAULT_LIMIT = 200
_MAX_TAG_BYTES = 8 * 1024 * 1024         # an embedded tag bigger than this (cover art, mostly) is not read whole
_MAX_ATOM_BYTES = 32 * 1024 * 1024
_TEXT_KEYS = ("title", "artist", "album", "genre", "year", "lyrics", "comment", "description", "software", "created",
              "prompt", "keywords")


def _clean(value, limit: int) -> str:
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text.strip()[:limit]


def _put(meta: dict, key: str, value, overwrite: bool = False) -> None:
    if key in ("duration_s", "width", "height"):
        try:
            number = float(value) if key == "duration_s" else int(value)
        except (TypeError, ValueError):
            return
        if number > 0 and (overwrite or key not in meta):
            meta[key] = round(number, 1) if key == "duration_s" else number
        return
    text = _clean(value, _LIMITS.get(key, _DEFAULT_LIMIT))
    if text and (overwrite or key not in meta):
        meta[key] = text


# ── ID3 (MP3, and the id3 chunk of WAV / AIFF) ───────────────────────────────────────────────────────

def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _decode_text(enc: int, data: bytes) -> str:
    try:
        if enc == 1:
            return data.decode("utf-16")
        if enc == 2:
            return data.decode("utf-16-be")
        if enc == 3:
            return data.decode("utf-8", "replace")
        return data.decode("latin-1")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def _split_terminated(enc: int, data: bytes):
    """(text before the first terminator, the rest) - a UTF-16 terminator is two zero bytes on an even offset."""
    if enc in (1, 2):
        for i in range(0, len(data) - 1, 2):
            if data[i] == 0 and data[i + 1] == 0:
                return data[:i], data[i + 2:]
        return data, b""
    i = data.find(b"\x00")
    return (data, b"") if i < 0 else (data[:i], data[i + 1:])


_ID3_TEXT = {
    "TIT2": "title", "TT2": "title", "TPE1": "artist", "TP1": "artist", "TALB": "album", "TAL": "album",
    "TCON": "genre", "TCO": "genre", "TYER": "year", "TYE": "year", "TDRC": "year",
}


def _parse_id3v2(data: bytes, meta: dict) -> None:
    if len(data) < 10 or data[:3] != b"ID3":
        return
    version, flags = data[3], data[5]
    end = min(len(data), 10 + _synchsafe(data[6:10]))
    pos = 10
    if flags & 0x40 and version >= 3 and end - pos >= 4:        # extended header: skip it
        ext = _synchsafe(data[pos:pos + 4]) if version == 4 else struct.unpack(">I", data[pos:pos + 4])[0] + 4
        pos += ext
    id_len = 3 if version == 2 else 4
    header_len = 6 if version == 2 else 10
    while pos + header_len <= end:
        frame_id = data[pos:pos + id_len]
        if frame_id[:1] == b"\x00":
            break
        if version == 2:
            size = int.from_bytes(data[pos + 3:pos + 6], "big")
        elif version == 4:
            size = _synchsafe(data[pos + 4:pos + 8])
        else:
            size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + header_len:pos + header_len + size]
        pos += header_len + size
        if not body or size <= 0:
            continue
        fid = frame_id.decode("latin-1", "replace")
        enc, payload = body[0], body[1:]
        if fid in _ID3_TEXT:
            text = _decode_text(enc, payload)
            if _ID3_TEXT[fid] == "genre":
                text = re.sub(r"^\(\d+\)", "", text) or text
            if _ID3_TEXT[fid] == "year":
                text = text[:4]
            _put(meta, _ID3_TEXT[fid], text.split("\x00")[0])
        elif fid == "TLEN":
            _put(meta, "duration_s", (int(re.sub(r"\D", "", _decode_text(enc, payload)) or 0)) / 1000.0)
        elif fid in ("USLT", "ULT"):                      # lang(3), descriptor, then the lyrics
            _desc, lyrics = _split_terminated(enc, payload[3:])
            _put(meta, "lyrics", _decode_text(enc, lyrics))
        elif fid in ("COMM", "COM"):
            _desc, comment = _split_terminated(enc, payload[3:])
            _put(meta, "comment", _decode_text(enc, comment))
        elif fid == "TXXX":
            desc, value = _split_terminated(enc, payload)
            if _decode_text(enc, desc).strip().lower() in ("lyrics", "unsyncedlyrics", "unsynced lyrics"):
                _put(meta, "lyrics", _decode_text(enc, value))


def _parse_id3v1(tail: bytes, meta: dict) -> None:
    if len(tail) != 128 or tail[:3] != b"TAG":
        return
    field = lambda a, b: tail[a:b].split(b"\x00")[0].decode("latin-1", "replace")
    _put(meta, "title", field(3, 33))
    _put(meta, "artist", field(33, 63))
    _put(meta, "album", field(63, 93))
    _put(meta, "year", field(93, 97))


def _read_mp3(f, size: int, meta: dict) -> None:
    f.seek(0)
    head = f.read(10)
    if head[:3] == b"ID3":
        tag_size = min(_synchsafe(head[6:10]), _MAX_TAG_BYTES)
        f.seek(0)
        _parse_id3v2(f.read(10 + tag_size), meta)
    if size >= 128:
        f.seek(size - 128)
        _parse_id3v1(f.read(128), meta)


# ── Vorbis comments (FLAC, Ogg Vorbis, Opus) ────────────────────────────────────────────────────────

_VORBIS_KEYS = {
    "TITLE": "title", "ARTIST": "artist", "ALBUMARTIST": "artist", "ALBUM": "album", "GENRE": "genre",
    "DATE": "year", "YEAR": "year", "LYRICS": "lyrics", "UNSYNCEDLYRICS": "lyrics", "COMMENT": "comment",
    "DESCRIPTION": "description",
}


def _parse_vorbis_comment(data: bytes, meta: dict) -> None:
    try:
        pos = 4 + struct.unpack("<I", data[:4])[0]                # vendor string
        count = struct.unpack("<I", data[pos:pos + 4])[0]
        pos += 4
        for _ in range(min(count, 500)):
            n = struct.unpack("<I", data[pos:pos + 4])[0]
            entry = data[pos + 4:pos + 4 + n].decode("utf-8", "replace")
            pos += 4 + n
            key, _, value = entry.partition("=")
            target = _VORBIS_KEYS.get(key.upper())
            if target:
                _put(meta, target, value[:4] if target == "year" else value)
    except (struct.error, IndexError):
        pass


def _read_flac(f, size: int, meta: dict) -> None:
    f.seek(4)
    for _ in range(64):
        header = f.read(4)
        if len(header) < 4:
            break
        last, kind, length = header[0] & 0x80, header[0] & 0x7F, int.from_bytes(header[1:4], "big")
        if kind == 0 and length >= 18:                             # STREAMINFO: rate(20) channels(3) bits(5) samples(36)
            block = f.read(length)
            packed = int.from_bytes(block[10:18], "big")
            rate, samples = packed >> 44, packed & ((1 << 36) - 1)
            if rate:
                _put(meta, "duration_s", samples / rate)
        elif kind == 4 and length <= _MAX_TAG_BYTES:
            _parse_vorbis_comment(f.read(length), meta)
        else:
            f.seek(length, 1)
        if last:
            break


def _ogg_packets(data: bytes, wanted: int) -> list:
    """The first `wanted` complete packets of an Ogg stream held in `data`."""
    packets, current, pos = [], b"", 0
    while pos + 27 <= len(data) and len(packets) < wanted:
        if data[pos:pos + 4] != b"OggS":
            break
        nseg = data[pos + 26]
        table = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        for lace in table:
            current += data[body:body + lace]
            body += lace
            if lace < 255:
                packets.append(current)
                current = b""
        pos = body
    return packets


def _read_ogg(f, size: int, meta: dict) -> None:
    f.seek(0)
    packets = _ogg_packets(f.read(512 * 1024), 2)
    rate = 0
    if packets:
        first = packets[0]
        if first.startswith(b"OpusHead"):
            rate = 48000
        elif first.startswith(b"\x01vorbis") and len(first) >= 16:
            rate = struct.unpack("<I", first[12:16])[0]
    if len(packets) >= 2:
        tags = packets[1]
        if tags.startswith(b"OpusTags"):
            _parse_vorbis_comment(tags[8:], meta)
        elif tags.startswith(b"\x03vorbis"):
            _parse_vorbis_comment(tags[7:], meta)
    if rate and size > 27:                                          # the last page's granule position is the length
        f.seek(max(0, size - 65536))
        tail = f.read()
        i = tail.rfind(b"OggS")
        if i >= 0 and i + 14 <= len(tail):
            granule = struct.unpack("<q", tail[i + 6:i + 14])[0]
            if granule > 0:
                _put(meta, "duration_s", granule / rate)


# ── WAV ─────────────────────────────────────────────────────────────────────────────────────────────

_WAV_INFO = {b"INAM": "title", b"IART": "artist", b"IPRD": "album", b"ICMT": "comment", b"IGNR": "genre",
             b"ICRD": "year", b"ISFT": "software"}


def _read_wav(f, size: int, meta: dict) -> None:
    f.seek(12)
    byte_rate = 0
    for _ in range(64):
        header = f.read(8)
        if len(header) < 8:
            break
        cid, length = header[:4], struct.unpack("<I", header[4:])[0]
        padded = length + (length & 1)
        if cid == b"fmt " and length >= 12:
            byte_rate = struct.unpack("<I", f.read(length)[8:12])[0]
            f.seek(padded - length, 1)
        elif cid == b"data":
            if byte_rate:
                _put(meta, "duration_s", length / byte_rate)
            f.seek(padded, 1)
        elif cid == b"LIST" and length <= _MAX_TAG_BYTES:
            block = f.read(padded)
            if block[:4] == b"INFO":
                pos = 4
                while pos + 8 <= len(block):
                    sub, n = block[pos:pos + 4], struct.unpack("<I", block[pos + 4:pos + 8])[0]
                    key = _WAV_INFO.get(sub)
                    if key:
                        _put(meta, key, block[pos + 8:pos + 8 + n].split(b"\x00")[0].decode("utf-8", "replace")[:4 if key == "year" else 400])
                    pos += 8 + n + (n & 1)
        elif cid in (b"id3 ", b"ID3 ") and length <= _MAX_TAG_BYTES:
            _parse_id3v2(f.read(padded), meta)
        else:
            f.seek(padded, 1)


# ── MP4 / M4A / MOV ───────────────────────────────────────────────────────────────────────────────────

_MP4_ITEMS = {
    b"\xa9nam": "title", b"\xa9ART": "artist", b"aART": "artist", b"\xa9alb": "album", b"\xa9gen": "genre",
    b"\xa9day": "year", b"\xa9lyr": "lyrics", b"\xa9cmt": "comment", b"desc": "description", b"ldes": "description",
    b"\xa9too": "software",
}
_MP4_CONTAINERS = {b"moov", b"udta", b"trak", b"mdia", b"minf", b"stbl", b"edts"}


def _atoms(data: bytes, start: int = 0, end: Optional[int] = None):
    """Yield (type, payload_start, payload_end) for the atoms in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1 and pos + 16 <= end:
            size, header = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield kind, pos + header, pos + size
        pos += size


def _walk_mp4(data: bytes, start: int, end: int, meta: dict, depth: int = 0) -> None:
    if depth > 8:
        return
    for kind, a, b in _atoms(data, start, end):
        if kind == b"mvhd" and b - a >= 20:
            if data[a] == 1 and b - a >= 32:
                scale, dur = struct.unpack(">IQ", data[a + 20:a + 32])
            else:
                scale, dur = struct.unpack(">II", data[a + 12:a + 20])
            if scale:
                _put(meta, "duration_s", dur / scale)
        elif kind == b"tkhd" and b - a >= 84:
            offset = 76 if data[a] == 0 else 88
            if b - a >= offset + 8:
                w, h = struct.unpack(">II", data[a + offset:a + offset + 8])
                if w >> 16 and h >> 16:
                    _put(meta, "width", w >> 16)
                    _put(meta, "height", h >> 16)
        elif kind == b"meta":
            _walk_mp4(data, a + 4, b, meta, depth + 1)          # a full box: version/flags precede the children
        elif kind == b"ilst":
            for item, ia, ib in _atoms(data, a, b):
                key = _MP4_ITEMS.get(item)
                if not key:
                    continue
                for sub, sa, sb in _atoms(data, ia, ib):
                    if sub == b"data" and sb - sa > 8:
                        _put(meta, key, data[sa + 8:sb].decode("utf-8", "replace")[:4 if key == "year" else 4000])
                        break
        elif kind in _MP4_CONTAINERS:
            _walk_mp4(data, a, b, meta, depth + 1)


def _read_mp4(f, size: int, meta: dict) -> None:
    """Find the moov atom wherever it sits (the start for a "fast start" file, the end otherwise) without reading the
    media data."""
    pos = 0
    for _ in range(64):
        f.seek(pos)
        header = f.read(16)
        if len(header) < 8:
            return
        length, kind = struct.unpack(">I4s", header[:8])
        head = 8
        if length == 1 and len(header) >= 16:
            length, head = struct.unpack(">Q", header[8:16])[0], 16
        elif length == 0:
            length = size - pos
        if length < head:
            return
        if kind == b"moov":
            if length - head > _MAX_ATOM_BYTES:
                return
            f.seek(pos + head)
            body = f.read(length - head)
            _walk_mp4(body, 0, len(body), meta)
            return
        pos += length


# ── ffprobe fallback ──────────────────────────────────────────────────────────────────────────────────

def _ffprobe_json(path: Path) -> Optional[dict]:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, timeout=10, check=False,
        )
        return json.loads(out.stdout.decode("utf-8", "replace")) if out.returncode == 0 else None
    except Exception:
        return None


def _merge_ffprobe(path: Path, meta: dict) -> None:
    """Only fills what is still missing: native readers win, ffprobe covers the other containers and odd tags."""
    data = _ffprobe_json(path)
    if not isinstance(data, dict):
        return
    fmt = data.get("format") or {}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for stream in data.get("streams") or []:
        for k, v in (stream.get("tags") or {}).items():
            tags.setdefault(str(k).lower(), v)
    for key, source in (("title", "title"), ("artist", "artist"), ("album", "album"), ("genre", "genre"),
                        ("year", "date"), ("lyrics", "lyrics"), ("comment", "comment"), ("description", "description"),
                        ("software", "encoder")):
        if tags.get(source):
            _put(meta, key, str(tags[source])[:4] if key == "year" else tags[source])
    _put(meta, "duration_s", fmt.get("duration"))
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width"):
            _put(meta, "width", stream.get("width"))
            _put(meta, "height", stream.get("height"))
            break


# ── images ────────────────────────────────────────────────────────────────────────────────────────────

_XMP_TITLE = re.compile(r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_XMP_DESC = re.compile(r"<dc:description>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)


def _prompt_from_generation_data(info: dict) -> str:
    """The positive prompt out of what AI image tools write into a PNG: A1111 / Forge / SwarmUI "parameters", or a
    ComfyUI "prompt" graph. "" when there is none."""
    params = info.get("parameters")
    if isinstance(params, str) and params.strip():
        text = params.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                inner = data.get("sui_image_params") or data
                if isinstance(inner, dict) and isinstance(inner.get("prompt"), str):
                    return inner["prompt"]
            except ValueError:
                pass
        head = re.split(r"\n\s*Negative prompt:|\n\s*Steps:", text, maxsplit=1)[0]
        return head.strip()
    graph = info.get("prompt")
    if isinstance(graph, str) and graph.strip().startswith("{"):
        try:
            nodes = json.loads(graph)
        except ValueError:
            return ""
        texts = [n["inputs"]["text"] for n in nodes.values()
                 if isinstance(n, dict) and n.get("class_type") == "CLIPTextEncode"
                 and isinstance((n.get("inputs") or {}).get("text"), str) and n["inputs"]["text"].strip()]
        return texts[0] if texts else ""
    return ""


def _xp(value) -> str:
    return value.decode("utf-16-le", "ignore") if isinstance(value, (bytes, bytearray)) else str(value or "")


def _read_image(path: Path, meta: dict) -> None:
    from PIL import Image
    with Image.open(path) as img:
        _put(meta, "format", img.format)
        _put(meta, "width", img.size[0])
        _put(meta, "height", img.size[1])
        info = dict(img.info or {})
        _put(meta, "prompt", _prompt_from_generation_data(info))
        for key, target in (("Title", "title"), ("Description", "description"), ("Comment", "comment"),
                            ("comment", "comment"), ("Author", "artist"), ("Software", "software"),
                            ("Creation Time", "created")):
            if isinstance(info.get(key), (str, bytes)):
                _put(meta, target, info[key].decode("utf-8", "replace") if isinstance(info[key], bytes) else info[key])
        for xmp_key in ("XML:com.adobe.xmp", "xmp"):
            raw = info.get(xmp_key)
            if raw:
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                for pattern, target in ((_XMP_TITLE, "title"), (_XMP_DESC, "description")):
                    m = pattern.search(text)
                    if m:
                        _put(meta, target, re.sub(r"<[^>]+>", "", m.group(1)))
        try:
            exif = img.getexif()
        except Exception:
            exif = {}
        if exif:
            _put(meta, "description", exif.get(0x010E))
            _put(meta, "artist", exif.get(0x013B))
            _put(meta, "software", exif.get(0x0131))
            _put(meta, "title", _xp(exif.get(0x9C9B)))
            _put(meta, "comment", _xp(exif.get(0x9C9C)))
            _put(meta, "keywords", _xp(exif.get(0x9C9E)).replace(";", ", "))
            _put(meta, "description", _xp(exif.get(0x9C9F)))
            try:
                _put(meta, "created", exif.get_ifd(0x8769).get(0x9003))
            except Exception:
                pass


# ── entry point ───────────────────────────────────────────────────────────────────────────────────────

def read_metadata(path, kind: str = "audio") -> dict:
    """What `path` says about itself. kind is "audio", "video" or "image"; returns {} for anything unreadable."""
    meta: dict = {}
    try:
        p = Path(path)
        if not p.is_file():
            return {}
        if kind == "image":
            _read_image(p, meta)
            return meta
        size = p.stat().st_size
        with open(p, "rb") as f:
            head = f.read(12)
            if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
                _read_mp3(f, size, meta)
            elif head[:4] == b"fLaC":
                _read_flac(f, size, meta)
            elif head[:4] == b"OggS":
                _read_ogg(f, size, meta)
            elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                _read_wav(f, size, meta)
            elif head[4:8] == b"ftyp":
                _read_mp4(f, size, meta)
        if "duration_s" not in meta or "title" not in meta or (kind == "video" and "width" not in meta):
            _merge_ffprobe(p, meta)
    except Exception:
        return {k: v for k, v in meta.items()}
    return meta
