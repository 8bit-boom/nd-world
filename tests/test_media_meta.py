"""app/media_meta.py - what an audio / video / image file says about itself, read without any extra dependency.

Every file here is built byte by byte in the test, so the readers are checked against the container formats themselves
rather than against whatever a tagging tool happened to write.
"""
import io
import json
import struct

import pytest

from app import media_meta as mm


# ── builders ─────────────────────────────────────────────────────────────────────────────────────────

def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3v23_frame(fid: str, body: bytes) -> bytes:
    return fid.encode() + struct.pack(">I", len(body)) + b"\x00\x00" + body


def _id3v24_frame(fid: str, body: bytes) -> bytes:
    return fid.encode() + _synchsafe(len(body)) + b"\x00\x00" + body


def _id3v22_frame(fid: str, body: bytes) -> bytes:
    return fid.encode() + len(body).to_bytes(3, "big") + body


def _id3(version: int, frames: bytes) -> bytes:
    return b"ID3" + bytes([version, 0, 0]) + _synchsafe(len(frames)) + frames


def _text(enc: int, s: str) -> bytes:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[enc]
    return bytes([enc]) + s.encode(codec)


def _mp3(tmp_path, tag: bytes, trailer: bytes = b"") -> str:
    p = tmp_path / "song.mp3"
    p.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 400 + trailer)
    return str(p)


# ── MP3 / ID3 ──────────────────────────────────────────────────────────────────────────────────────

def test_id3v23_title_artist_album_genre_year(tmp_path):
    tag = _id3(3, b"".join([
        _id3v23_frame("TIT2", _text(0, "Ember Waltz")), _id3v23_frame("TPE1", _text(1, "Mira Kest")),
        _id3v23_frame("TALB", _text(3, "Ashes & Lanterns")), _id3v23_frame("TCON", _text(0, "(17)Folk")),
        _id3v23_frame("TYER", _text(0, "2019")),
    ]))
    meta = mm.read_metadata(_mp3(tmp_path, tag), "audio")
    assert meta["title"] == "Ember Waltz" and meta["artist"] == "Mira Kest" and meta["album"] == "Ashes & Lanterns"
    assert meta["genre"] == "Folk" and meta["year"] == "2019"


def test_id3_lyrics_comment_and_utf16_text(tmp_path):
    lyrics = "Sing of the lantern\nsing of the rain"
    uslt = _text(1, "")[:1] + b"eng" + "".encode("utf-16") + b"\x00\x00" + lyrics.encode("utf-16")
    comm = bytes([0]) + b"eng" + b"\x00" + b"Recorded at the old mill"
    tag = _id3(3, _id3v23_frame("USLT", uslt) + _id3v23_frame("COMM", comm) + _id3v23_frame("TIT2", _text(1, "Lämpö — 灯")))
    meta = mm.read_metadata(_mp3(tmp_path, tag), "audio")
    assert meta["lyrics"] == lyrics
    assert meta["comment"] == "Recorded at the old mill"
    assert meta["title"] == "Lämpö — 灯"


def test_id3v24_uses_synchsafe_frame_sizes_and_txxx_lyrics(tmp_path):
    txxx = bytes([3]) + b"LYRICS\x00" + "words in a txxx frame".encode()
    tag = _id3(4, _id3v24_frame("TIT2", _text(3, "x" * 200 + "end")) + _id3v24_frame("TXXX", txxx))
    meta = mm.read_metadata(_mp3(tmp_path, tag), "audio")
    assert meta["title"].startswith("xxx") and len(meta["title"]) == 200          # clipped to the field limit
    assert meta["lyrics"] == "words in a txxx frame"


def test_id3v22_three_character_frames(tmp_path):
    tag = _id3(2, _id3v22_frame("TT2", _text(0, "Old Style")) + _id3v22_frame("TP1", _text(0, "Someone")))
    meta = mm.read_metadata(_mp3(tmp_path, tag), "audio")
    assert (meta["title"], meta["artist"]) == ("Old Style", "Someone")


def test_id3v1_trailer_is_used_when_there_is_no_v2_tag(tmp_path):
    v1 = b"TAG" + b"Tiny Song".ljust(30, b"\x00") + b"Small Band".ljust(30, b"\x00") + b"Little Album".ljust(30, b"\x00") + b"1998" + b"\x00" * 31
    p = tmp_path / "old.mp3"
    p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 300 + v1)
    meta = mm.read_metadata(p, "audio")
    assert (meta["title"], meta["artist"], meta["album"], meta["year"]) == ("Tiny Song", "Small Band", "Little Album", "1998")


def test_id3_tlen_gives_a_duration(tmp_path):
    meta = mm.read_metadata(_mp3(tmp_path, _id3(3, _id3v23_frame("TLEN", _text(0, "185000")))), "audio")
    assert meta["duration_s"] == 185.0


def test_id3_with_an_absurd_declared_size_does_not_hang_or_crash(tmp_path):
    p = tmp_path / "hostile.mp3"
    p.write_bytes(b"ID3\x03\x00\x00" + _synchsafe(0x0FFFFFFF) + _id3v23_frame("TIT2", _text(0, "Short")) + b"\x00" * 50)
    assert mm.read_metadata(p, "audio")["title"] == "Short"


# ── FLAC / Ogg ──────────────────────────────────────────────────────────────────────────────────────

def _vorbis_comment(entries: dict, vendor: bytes = b"test") -> bytes:
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
    for k, v in entries.items():
        e = f"{k}={v}".encode()
        body += struct.pack("<I", len(e)) + e
    return body


def test_flac_comments_and_duration(tmp_path):
    streaminfo = bytearray(34)
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 441000                # 44.1 kHz, mono, 16 bit, 10 s
    streaminfo[10:18] = packed.to_bytes(8, "big")
    comment = _vorbis_comment({"TITLE": "Rain on Tin", "ARTIST": "Field Recordings", "DATE": "2021-05-01", "LYRICS": "la la"})
    data = b"fLaC" + bytes([0]) + len(streaminfo).to_bytes(3, "big") + bytes(streaminfo) \
        + bytes([0x84]) + len(comment).to_bytes(3, "big") + comment + b"\x00" * 100
    p = tmp_path / "rain.flac"
    p.write_bytes(data)
    meta = mm.read_metadata(p, "audio")
    assert meta["title"] == "Rain on Tin" and meta["artist"] == "Field Recordings" and meta["year"] == "2021"
    assert meta["lyrics"] == "la la" and meta["duration_s"] == 10.0


def _ogg_page(packet: bytes, granule: int, seq: int, header_type: int = 0) -> bytes:
    laces, rest = [], len(packet)
    while rest >= 255:
        laces.append(255)
        rest -= 255
    laces.append(rest)
    return b"OggS" + bytes([0, header_type]) + struct.pack("<q", granule) + struct.pack("<III", 1, seq, 0) \
        + bytes([len(laces)]) + bytes(laces) + packet


def test_ogg_opus_comments_and_duration(tmp_path):
    head = b"OpusHead" + b"\x01\x02" + struct.pack("<H", 312) + struct.pack("<I", 48000) + b"\x00\x00\x00"
    tags = b"OpusTags" + _vorbis_comment({"TITLE": "Night Market", "ARTIST": "Lo Fi Crew", "COMMENT": "ambient"})
    data = _ogg_page(head, 0, 0, 2) + _ogg_page(tags, 0, 1) + _ogg_page(b"\x00" * 20, 48000 * 30, 2, 4)
    p = tmp_path / "market.opus"
    p.write_bytes(data)
    meta = mm.read_metadata(p, "audio")
    assert meta["title"] == "Night Market" and meta["artist"] == "Lo Fi Crew" and meta["comment"] == "ambient"
    assert meta["duration_s"] == 30.0


def test_ogg_vorbis_comments_span_a_long_packet(tmp_path):
    ident = b"\x01vorbis" + struct.pack("<I", 0) + b"\x02" + struct.pack("<I", 22050) + b"\x00" * 12
    tags = b"\x03vorbis" + _vorbis_comment({"TITLE": "T" * 120, "LYRICS": "word " * 120}) + b"\x01"       # > 255 bytes
    data = _ogg_page(ident, 0, 0, 2) + _ogg_page(tags, 0, 1) + _ogg_page(b"\x00" * 10, 22050 * 12, 2, 4)
    p = tmp_path / "t.ogg"
    p.write_bytes(data)
    meta = mm.read_metadata(p, "audio")
    assert meta["title"] == "T" * 120 and meta["lyrics"].startswith("word word") and meta["duration_s"] == 12.0


# ── WAV ──────────────────────────────────────────────────────────────────────────────────────────────

def test_wav_info_chunk_and_duration(tmp_path):
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    info = b"INFO" + b"INAM" + struct.pack("<I", 11) + b"Door Creak\x00" + b"\x00" + b"IART" + struct.pack("<I", 6) + b"Foley\x00"
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", len(info)) + info \
        + b"data" + struct.pack("<I", 16000) + b"\x00" * 16000
    p = tmp_path / "door.wav"
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    meta = mm.read_metadata(p, "audio")
    assert meta["title"] == "Door Creak" and meta["artist"] == "Foley" and meta["duration_s"] == 2.0


# ── MP4 / M4A ─────────────────────────────────────────────────────────────────────────────────────────

def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _ilst_item(kind: bytes, text: str) -> bytes:
    return _atom(kind, _atom(b"data", struct.pack(">II", 1, 0) + text.encode()))


def _mp4(tmp_path, moov_last: bool, name="clip.mp4") -> str:
    mvhd = _atom(b"mvhd", bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, 1000, 5000) + b"\x00" * 80)
    tkhd = _atom(b"tkhd", bytes([0, 0, 0, 7]) + struct.pack(">IIII", 0, 0, 1, 0) + struct.pack(">I", 5000) + b"\x00" * 8
                 + struct.pack(">hhhh", 0, 0, 0, 0) + b"\x00" * 36 + struct.pack(">II", 1920 << 16, 1080 << 16))
    ilst = _atom(b"ilst", _ilst_item(b"\xa9nam", "The Ambush at Dawn") + _ilst_item(b"\xa9ART", "Table Crew")
                 + _ilst_item(b"\xa9lyr", "line one\nline two") + _ilst_item(b"\xa9day", "2024-03-02"))
    udta = _atom(b"udta", _atom(b"meta", bytes(4) + _atom(b"hdlr", b"\x00" * 24) + ilst))
    moov = _atom(b"moov", mvhd + _atom(b"trak", tkhd) + udta)
    ftyp = _atom(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    mdat = _atom(b"mdat", b"\x00" * 5000)
    p = tmp_path / name
    p.write_bytes(ftyp + (mdat + moov if moov_last else moov + mdat))
    return str(p)


@pytest.mark.parametrize("moov_last", [False, True])
def test_mp4_tags_duration_and_dimensions(tmp_path, moov_last):
    meta = mm.read_metadata(_mp4(tmp_path, moov_last), "video")
    assert meta["title"] == "The Ambush at Dawn" and meta["artist"] == "Table Crew"
    assert meta["lyrics"] == "line one\nline two" and meta["year"] == "2024"
    assert meta["duration_s"] == 5.0 and (meta["width"], meta["height"]) == (1920, 1080)


# ── images ───────────────────────────────────────────────────────────────────────────────────────────

def _png(tmp_path, name="a.png", size=(64, 48), **text) -> str:
    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    for k, v in text.items():
        info.add_text(k.replace("_", " ") if k == "Creation_Time" else k, v)
    p = tmp_path / name
    Image.new("RGB", size, "red").save(p, pnginfo=info)
    return str(p)


def test_png_dimensions_and_plain_text_chunks(tmp_path):
    meta = mm.read_metadata(_png(tmp_path, Title="Harbour at Dusk", Description="Oil on canvas", Author="R. Vale"), "image")
    assert (meta["width"], meta["height"], meta["format"]) == (64, 48, "PNG")
    assert (meta["title"], meta["description"], meta["artist"]) == ("Harbour at Dusk", "Oil on canvas", "R. Vale")


def test_a1111_style_parameters_give_the_prompt_without_the_settings(tmp_path):
    params = "a lighthouse on a cliff, stormy sea, dramatic light\nNegative prompt: blurry, text\nSteps: 30, Sampler: Euler a, Seed: 5"
    assert mm.read_metadata(_png(tmp_path, parameters=params), "image")["prompt"] == "a lighthouse on a cliff, stormy sea, dramatic light"


def test_swarmui_parameters_json(tmp_path):
    params = json.dumps({"sui_image_params": {"prompt": "a neon dragon over a night market", "model": "x"}})
    assert mm.read_metadata(_png(tmp_path, parameters=params), "image")["prompt"] == "a neon dragon over a night market"


def test_comfyui_prompt_graph(tmp_path):
    graph = {"3": {"class_type": "KSampler", "inputs": {"seed": 1}},
             "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "an ancient library, candlelight"}},
             "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "ugly"}}}
    assert mm.read_metadata(_png(tmp_path, prompt=json.dumps(graph)), "image")["prompt"] == "an ancient library, candlelight"


def test_jpeg_exif_description_and_windows_title(tmp_path):
    from PIL import Image
    exif = Image.Exif()
    exif[0x010E] = "Market square at noon"
    exif[0x013B] = "Photographer Name"
    exif[0x9C9B] = "Square, Noon".encode("utf-16-le")
    p = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), "blue").save(p, exif=exif)
    meta = mm.read_metadata(p, "image")
    assert meta["description"] == "Market square at noon" and meta["artist"] == "Photographer Name"
    assert meta["title"] == "Square, Noon"


def test_a_plain_image_has_only_its_shape(tmp_path):
    from PIL import Image
    p = tmp_path / "plain.webp"
    Image.new("RGB", (10, 20), "green").save(p)
    meta = mm.read_metadata(p, "image")
    assert meta == {"format": "WEBP", "width": 10, "height": 20}


# ── robustness ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["audio", "video", "image"])
def test_unreadable_files_give_an_empty_result_not_an_error(tmp_path, kind):
    (tmp_path / "empty.bin").write_bytes(b"")
    (tmp_path / "noise.bin").write_bytes(bytes(range(256)) * 20)
    (tmp_path / "text.txt").write_text("not media")
    for name in ("empty.bin", "noise.bin", "text.txt", "missing.bin"):
        assert isinstance(mm.read_metadata(tmp_path / name, kind), dict)
    assert mm.read_metadata(tmp_path, kind) == {}                                  # a directory


def test_truncated_containers_keep_what_was_read(tmp_path):
    full = open(_mp4(tmp_path, False), "rb").read()
    p = tmp_path / "cut.mp4"
    p.write_bytes(full[:120])
    assert isinstance(mm.read_metadata(p, "video"), dict)
    flac = tmp_path / "cut.flac"
    flac.write_bytes(b"fLaC\x84\xff\xff\xff" + b"\x01" * 10)
    assert isinstance(mm.read_metadata(flac, "audio"), dict)


def test_control_characters_are_stripped_and_long_text_is_clipped(tmp_path):
    tag = _id3(3, _id3v23_frame("TIT2", _text(0, "Bad\x07Title here")) + _id3v23_frame("COMM", bytes([0]) + b"eng\x00" + b"c" * 5000))
    meta = mm.read_metadata(_mp3(tmp_path, tag), "audio")
    assert meta["title"] == "BadTitle here" and len(meta["comment"]) == 1000


# ── ffprobe fallback ─────────────────────────────────────────────────────────────────────────────────────────

def test_ffprobe_only_fills_what_the_native_reader_missed(tmp_path, monkeypatch):
    p = tmp_path / "movie.mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 50)
    monkeypatch.setattr(mm, "_ffprobe_json", lambda path: {
        "format": {"duration": "61.5", "tags": {"TITLE": "Cutscene One", "comment": "intro"}},
        "streams": [{"codec_type": "video", "width": 1280, "height": 720}]})
    meta = mm.read_metadata(p, "video")
    assert meta == {"title": "Cutscene One", "comment": "intro", "duration_s": 61.5, "width": 1280, "height": 720}
    mp3 = _mp3(tmp_path, _id3(3, _id3v23_frame("TIT2", _text(0, "Native Title"))))
    monkeypatch.setattr(mm, "_ffprobe_json", lambda path: {"format": {"duration": "99", "tags": {"title": "Probe Title"}}})
    meta = mm.read_metadata(mp3, "audio")
    assert meta["title"] == "Native Title" and meta["duration_s"] == 99.0


def test_no_ffprobe_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)
    p = tmp_path / "movie.mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 50)
    assert mm.read_metadata(p, "video") == {}
