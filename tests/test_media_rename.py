"""The AI media renamer's core (app/media_rename.py): what is known about each audio clip, video clip and image (its name,
file name, tags, lyrics / transcript, where it is used, optionally a look at the picture), the prompt built from that, the
cleaning of what the model answers, and applying / undoing the renames.
"""
import base64
import json
import struct

import pytest

from app import ai as ai_module
from app import media_rename as mr
from app.database import SessionLocal
from app.models import (
    AudioAlbum, AudioClip, Entity, ImageAlbum, MediaRenameLog, MediaTitle, VideoClip, World,
)


# ── looks_generic ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "", "   ", "IMG_2041", "img-0012", "DSC01234", "Screenshot 2024-05-01 at 10.22.33", "Screen Shot 2021-01-01",
    "PXL_20240101_123456789", "untitled", "Untitled 3", "New Recording", "Recording 12", "audio_1693", "Track 07",
    "VID_20230102_101010", "a1b2c3d4e5f6", "1693847221", "clip (2)", "Video", "Copy of video", "Audio Clip",
    "File 3", "Voice 004", "WhatsApp Audio 2023-01-01 at 10.00.00", "00001", "mov0142", "image", "download (3)",
    "tavern_ambience_loop", "goblin.png", "song.MP3", "Image 12 (copy)",
])
def test_generic_names(name):
    assert mr.looks_generic(name) is True


@pytest.mark.parametrize("name", [
    "Ember Waltz", "Tavern Ambience", "Goblin portrait", "The Siege of the Glass Tower", "Session 12 recap",
    "Map of Neon Gate", "Battle theme 2", "Track Day Rally", "Spider-Man", "Lantern Festival Procession",
])
def test_good_names_are_left_alone(name):
    assert mr.looks_generic(name) is False


def test_plain_words_that_match_the_file_name_are_still_a_fine_name():
    assert mr.looks_generic("Door Creak", filename="door creak") is False
    assert mr.looks_generic("old goblin", filename="old goblin") is False
    assert mr.looks_generic("old_goblin", filename="old goblin") is True            # underscores still read as a file name


def test_readable_filename_drops_the_upload_prefix_and_extension():
    assert mr.readable_filename("/uploads/audio/a1b2c3d4e5f6-ember-waltz.mp3") == "ember waltz"
    assert mr.readable_filename("/uploads/calendar_icons/0123456789ab.png") == "0123456789ab"
    assert mr.readable_filename("/uploads/x/Some_File Name.v2.webp") == "Some File Name.v2"
    assert mr.readable_filename("") == ""


# ── clean_suggestion ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ('"Ember Waltz"', "Ember Waltz"),
    ("`Ember Waltz.mp3`", "Ember Waltz"),
    ("Ember_Waltz", "Ember Waltz"),
    ("Title: Ember Waltz", "Ember Waltz"),
    ("name: ember waltz", "ember waltz"),
    ("1. Ember Waltz", "Ember Waltz"),
    ("Ember Waltz.", "Ember Waltz"),
    ("Ember Waltz\nA second line the model added", "Ember Waltz"),
    ("A/B \\ C: D", "A B C D"),
    ("  Ember   Waltz  ", "Ember Waltz"),
    ("1984 Theme", "1984 Theme"),
    ("Goblin Portrait.PNG", "Goblin Portrait"),
    ("", ""), ("...", ""), ("???", ""), (None, ""), (42, "42"),
])
def test_clean_suggestion(raw, expected):
    assert mr.clean_suggestion(raw) == expected


def test_clean_suggestion_cuts_long_names_at_a_word_and_drops_control_characters():
    out = mr.clean_suggestion("word " * 40)
    assert len(out) <= 80 and out.endswith("word") and "  " not in out
    assert mr.clean_suggestion("Bad\x07Name\x00!") == "BadName!"


# ── parse_names ───────────────────────────────────────────────────────────────────────────────────

def test_parse_names_accepts_the_asked_for_shape_and_looser_ones():
    ids = ["1", "2", "3"]
    good = json.dumps({"names": [{"id": "1", "name": "Ember Waltz"}, {"id": 2, "name": "Rain on Tin"}]})
    assert mr.parse_names(good, ids) == {"1": "Ember Waltz", "2": "Rain on Tin"}
    assert mr.parse_names("```json\n" + good + "\n```", ids) == {"1": "Ember Waltz", "2": "Rain on Tin"}
    assert mr.parse_names(json.dumps({"1": "A", "2": "B"}), ids) == {"1": "A", "2": "B"}
    assert mr.parse_names(json.dumps([{"id": "3", "name": "C"}]), ids) == {"3": "C"}
    assert mr.parse_names(json.dumps({"names": {"1": "A"}}), ids) == {"1": "A"}


def test_parse_names_ignores_unknown_ids_empty_names_and_garbage():
    assert mr.parse_names(json.dumps({"names": [{"id": "9", "name": "X"}, {"id": "1", "name": "  "}, {"id": "2", "name": "..."}]}), ["1", "2"]) == {}
    assert mr.parse_names("I would call it Ember Waltz.", ["1"]) == {}
    assert mr.parse_names("", ["1"]) == {}
    assert mr.parse_names(None, ["1"]) == {}
    assert mr.parse_names(json.dumps({"names": [{"id": "1", "name": "Ember_Waltz.mp3"}]}), ["1"]) == {"1": "Ember Waltz"}


# ── prompt ────────────────────────────────────────────────────────────────────────────────────────────

def _sig(**kw):
    base = {"kind": "audio", "id": 1, "url": None, "name": "track_0043", "filename": "track 0043", "album": "Tavern",
            "attached": "The Gilded Flagon", "description": "", "meta": {}, "transcript": "", "uses": [], "albums": [],
            "picture_b64": None, "listened": False}
    base.update(kw)
    return base


def test_describe_item_lists_every_clue():
    text = mr.describe_item(_sig(
        meta={"title": "Ember Waltz", "artist": "Mira Kest", "album": "Ashes", "genre": "Folk", "year": "2019",
              "duration_s": 192.0, "lyrics": "Sing of the lantern " * 80},
        transcript="spoken words here", description="played in the tavern"), "1")
    for needle in ("[1]", "audio", "track_0043", "track 0043", "Tavern", "The Gilded Flagon", "Ember Waltz", "Mira Kest",
                   "Ashes", "Folk", "2019", "3:12", "Sing of the lantern", "spoken words here", "played in the tavern"):
        assert needle in text, needle
    assert len(text) < 2600, "lyrics are cut so one item cannot swamp the prompt"


def test_describe_item_for_a_generated_picture_and_a_used_image():
    text = mr.describe_item(_sig(kind="image", name="a1", filename="a1", album="", attached="",
                                 meta={"prompt": "a neon dragon over a night market", "width": 1024, "height": 1024, "format": "PNG"},
                                 uses=["Kaelen (portrait)"], albums=["Dragons"]), "2")
    for needle in ("image", "neon dragon over a night market", "1024", "Kaelen (portrait)", "Dragons"):
        assert needle in text, needle


def test_build_prompt_names_the_world_the_style_and_every_item():
    system, user = mr.build_prompt([_sig(), _sig(id=2, name="IMG_9")], style="Title Case, no artist names", world_name="Neon Coast",
                                   world_about="A rain-soaked cyberpunk archipelago.")
    assert "JSON" in system and "names" in system
    assert "Neon Coast" in user and "cyberpunk archipelago" in user and "Title Case, no artist names" in user
    assert "[1]" in user and "[2]" in user and "IMG_9" in user
    assert "Never invent" in system or "never invent" in system.lower()


def test_style_presets_and_free_text_combine_and_are_bounded():
    assert mr.style_text("short", "") == mr.STYLE_PRESETS["short"]
    both = mr.style_text("lore", "also mention the region")
    assert mr.STYLE_PRESETS["lore"] in both and "also mention the region" in both
    assert mr.style_text("unknown-preset", "x") == "x"
    assert mr.style_text("", "") == ""
    assert len(mr.style_text("", "y" * 5000)) <= 400


# ── signals from real files ─────────────────────────────────────────────────────────────────────────────

def _uploads():
    from app.main import UPLOADS_DIR
    return UPLOADS_DIR


def _write(rel: str, data: bytes) -> str:
    p = _uploads() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return "/uploads/" + rel


def _png_bytes(size=(64, 48), color="red") -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _mp3_bytes(title="Ember Waltz", artist="Mira Kest", lyrics="") -> bytes:
    def frame(fid, body):
        return fid.encode() + struct.pack(">I", len(body)) + b"\x00\x00" + body
    frames = frame("TIT2", b"\x00" + title.encode()) + frame("TPE1", b"\x00" + artist.encode())
    if lyrics:
        frames += frame("USLT", b"\x03eng\x00" + lyrics.encode())
    n = len(frames)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + size + frames + b"\xff\xfb\x90\x00" + b"\x00" * 200


def _in_album(seed, *urls):
    """Register image URLs in an album of world A (the renamer only reads images that belong to the world)."""
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Test album", image_urls_json=json.dumps(list(urls))))
        db.commit()
    finally:
        db.close()


def _world(seed):
    db = SessionLocal()
    try:
        return db.get(World, seed.world_a.id)
    finally:
        db.close()


def _db_world(db, seed):
    return db.get(World, seed.world_a.id)


@pytest.mark.asyncio
async def test_audio_signals_combine_row_file_tags_lyrics_and_transcript(client, seed):
    url = _write("audio/a1b2c3d4e5f6-track-0043.mp3", _mp3_bytes(lyrics="Sing of the lantern"))
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="location", name="The Gilded Flagon")
        album = AudioAlbum(world_id=seed.world_a.id, name="Tavern")
        db.add_all([ent, album])
        db.commit()
        clip = AudioClip(world_id=seed.world_a.id, name="track_0043", file_url=url, album_id=album.id, entity_id=ent.id,
                         description="loop", transcript="spoken line")
        db.add(clip)
        db.commit()
        sig = await mr.gather_signals(db, _db_world(db, seed), "audio", {"id": clip.id})
    finally:
        db.close()
    assert sig["name"] == "track_0043" and sig["filename"] == "track 0043" and sig["album"] == "Tavern"
    assert sig["attached"] == "The Gilded Flagon" and sig["description"] == "loop" and sig["transcript"] == "spoken line"
    assert sig["meta"]["title"] == "Ember Waltz" and sig["meta"]["artist"] == "Mira Kest" and sig["meta"]["lyrics"] == "Sing of the lantern"
    assert sig["picture_b64"] is None


@pytest.mark.asyncio
async def test_a_missing_or_foreign_clip_gives_no_signals(client, seed):
    db = SessionLocal()
    try:
        other = AudioClip(world_id=seed.world_b.id, name="elsewhere", file_url="/uploads/audio/x.mp3")
        db.add(other)
        db.commit()
        assert await mr.gather_signals(db, _db_world(db, seed), "audio", {"id": other.id}) is None
        assert await mr.gather_signals(db, _db_world(db, seed), "audio", {"id": 99999}) is None
        assert await mr.gather_signals(db, _db_world(db, seed), "nonsense", {"id": 1}) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_image_signals_include_where_it_is_used_and_its_albums_and_the_picture(client, seed):
    url = _write("a1b2c3d4e5f6-kaelen.png", _png_bytes())
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Kaelen", image_url=url))
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Heroes", image_urls_json=json.dumps([url])))
        db.commit()
        world = _db_world(db, seed)
        usage = mr.image_usage(db, world)
        sig = await mr.gather_signals(db, world, "image", {"url": url}, usage=usage, pictures=True)
        plain = await mr.gather_signals(db, world, "image", {"url": url}, usage=usage, pictures=False)
    finally:
        db.close()
    assert sig["uses"] == ["Kaelen"] and sig["albums"] == ["Heroes"] and sig["filename"] == "kaelen"
    assert (sig["meta"]["width"], sig["meta"]["height"]) == (64, 48)
    raw = base64.b64decode(sig["picture_b64"])
    assert raw[:3] == b"\xff\xd8\xff", "sent as a JPEG"
    assert plain["picture_b64"] is None


@pytest.mark.asyncio
async def test_a_big_picture_is_scaled_down_before_it_is_sent(client, seed):
    import io
    from PIL import Image
    url = _write("big.png", _png_bytes(size=(3000, 2000)))
    _in_album(seed, url)
    db = SessionLocal()
    try:
        sig = await mr.gather_signals(db, _db_world(db, seed), "image", {"url": url}, pictures=True)
    finally:
        db.close()
    img = Image.open(io.BytesIO(base64.b64decode(sig["picture_b64"])))
    assert max(img.size) <= mr.PICTURE_MAX_SIDE


@pytest.mark.asyncio
async def test_urls_outside_the_uploads_folder_are_never_read(client, seed, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(_png_bytes())
    db = SessionLocal()
    try:
        world = _db_world(db, seed)
        for bad in ("/uploads/../../" + secret.name, "/etc/passwd", str(secret), "/uploads/", "uploads/x.png", ""):
            sig = await mr.gather_signals(db, world, "image", {"url": bad}, pictures=True)
            assert sig is None or (sig["picture_b64"] is None and sig["meta"] == {}), bad
    finally:
        db.close()


@pytest.mark.asyncio
async def test_video_signals_use_the_poster_frame_for_the_picture(client, seed):
    url = _write("video/a1b2c3d4e5f6-cutscene.mp4", b"\x00\x00\x00\x08free")
    poster = _write("video/a1b2c3d4e5f6-cutscene.mp4.jpg", _png_bytes())
    db = SessionLocal()
    try:
        clip = VideoClip(world_id=seed.world_a.id, name="VID_0001", file_url=url, poster_url=poster, transcript="the hero speaks")
        db.add(clip)
        db.commit()
        sig = await mr.gather_signals(db, _db_world(db, seed), "video", {"id": clip.id}, pictures=True)
    finally:
        db.close()
    assert sig["kind"] == "video" and sig["transcript"] == "the hero speaks" and sig["picture_b64"]


@pytest.mark.asyncio
async def test_listening_transcribes_only_clips_without_a_transcript_and_only_if_short(client, seed, monkeypatch):
    calls = []

    async def fake_transcribe(path):
        calls.append(path.name)
        return "words heard in the clip", ""
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", fake_transcribe)
    short = _write("audio/short.mp3", _mp3_bytes(title=""))
    big = _write("audio/big.mp3", _mp3_bytes(title="") + b"\x00" * (mr.LISTEN_MAX_BYTES + 10))
    db = SessionLocal()
    try:
        a = AudioClip(world_id=seed.world_a.id, name="x", file_url=short)
        b = AudioClip(world_id=seed.world_a.id, name="y", file_url=big)
        c = AudioClip(world_id=seed.world_a.id, name="z", file_url=short, transcript="already transcribed")
        db.add_all([a, b, c])
        db.commit()
        world = _db_world(db, seed)
        sa = await mr.gather_signals(db, world, "audio", {"id": a.id}, listen=True)
        sb = await mr.gather_signals(db, world, "audio", {"id": b.id}, listen=True)
        sc = await mr.gather_signals(db, world, "audio", {"id": c.id}, listen=True)
        sn = await mr.gather_signals(db, world, "audio", {"id": a.id}, listen=False)
        stored = db.get(AudioClip, a.id).transcript
    finally:
        db.close()
    assert sa["transcript"] == "words heard in the clip" and sa["listened"] is True
    assert sb["transcript"] == "" and sc["transcript"] == "already transcribed" and sn["transcript"] == ""
    assert calls == ["short.mp3"], "only the short clip without a transcript was transcribed"
    assert stored in ("", None), "scanning never writes a transcript into the library"


@pytest.mark.asyncio
async def test_a_failing_transcription_is_not_fatal(client, seed, monkeypatch):
    async def boom(path):
        raise RuntimeError("stt down")
    monkeypatch.setattr(ai_module, "transcribe_audio_with_subtitles", boom)
    url = _write("audio/s.mp3", _mp3_bytes(title=""))
    db = SessionLocal()
    try:
        a = AudioClip(world_id=seed.world_a.id, name="x", file_url=url)
        db.add(a)
        db.commit()
        sig = await mr.gather_signals(db, _db_world(db, seed), "audio", {"id": a.id}, listen=True)
    finally:
        db.close()
    assert sig["transcript"] == ""


# ── suggest ─────────────────────────────────────────────────────────────────────────────────────────

class _FakeChat:
    def __init__(self, namer):
        self.calls, self.namer = [], namer

    async def __call__(self, messages, system="", model="", options=None, think=False, format=None):
        self.calls.append({"messages": messages, "system": system, "format": format})
        text = messages[-1]["content"]
        import re
        ids = re.findall(r"^\[(\d+)\]", text, re.M)
        return json.dumps({"names": [{"id": i, "name": self.namer(i, text)} for i in ids]})


def _clips(seed, n, kind="audio", name="track_{i}"):
    db = SessionLocal()
    try:
        rows = []
        for i in range(n):
            cls = AudioClip if kind == "audio" else VideoClip
            row = cls(world_id=seed.world_a.id, name=name.format(i=i), file_url=f"/uploads/{kind}/missing-{i}.bin")
            db.add(row)
            rows.append(row)
        db.commit()
        return [r.id for r in rows]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_suggest_batches_text_items_and_returns_cleaned_names(client, seed, monkeypatch):
    fake = _FakeChat(lambda i, text: f'"Better Name {i}.mp3"')
    monkeypatch.setattr(ai_module, "generate_chat", fake)
    ids = _clips(seed, 20)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "audio", "id": i} for i in ids])
    finally:
        db.close()
    assert len(fake.calls) == 3 and all(c["format"] is not None for c in fake.calls)       # 8 + 8 + 4
    assert len(out["results"]) == 20 and not out["errors"]
    first = out["results"][0]
    assert first["kind"] == "audio" and first["id"] == ids[0] and first["old"] == "track_0"
    assert first["new"].startswith("Better Name") and not first["new"].endswith(".mp3") and first["changed"] is True
    assert "filename" in first["basis"]


@pytest.mark.asyncio
async def test_suggest_marks_unchanged_names_and_missing_answers(client, seed, monkeypatch):
    async def partial(messages, system="", model="", options=None, think=False, format=None):
        return json.dumps({"names": [{"id": "1", "name": "track_0"}]})                  # item 2 gets no answer
    monkeypatch.setattr(ai_module, "generate_chat", partial)
    ids = _clips(seed, 2)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "audio", "id": i} for i in ids])
    finally:
        db.close()
    same, none = out["results"]
    assert same["changed"] is False and same["new"] == "track_0"
    assert none["new"] == "" and none["changed"] is False and none["note"]


@pytest.mark.asyncio
async def test_pictures_go_one_per_call_with_the_image_attached(client, seed, monkeypatch):
    fake = _FakeChat(lambda i, text: "A Red Square")
    monkeypatch.setattr(ai_module, "generate_chat", fake)
    urls = [_write(f"pic-{i}.png", _png_bytes()) for i in range(3)]
    _in_album(seed, *urls)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "image", "url": u} for u in urls], pictures=True)
    finally:
        db.close()
    assert len(fake.calls) == 3
    for call in fake.calls:
        user = call["messages"][-1]
        assert len(user["images"]) == 1 and base64.b64decode(user["images"][0])[:3] == b"\xff\xd8\xff"
    assert all("picture" in r["basis"] for r in out["results"])


@pytest.mark.asyncio
async def test_without_the_picture_option_images_are_named_from_what_the_files_say(client, seed, monkeypatch):
    fake = _FakeChat(lambda i, text: "From Metadata")
    monkeypatch.setattr(ai_module, "generate_chat", fake)
    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "a neon dragon over a night market\nNegative prompt: blur\nSteps: 20")
    import io
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "PNG", pnginfo=info)
    url = _write("gen.png", buf.getvalue())
    _in_album(seed, url)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "image", "url": url}], pictures=False)
    finally:
        db.close()
    assert len(fake.calls) == 1 and "images" not in fake.calls[0]["messages"][-1]
    assert "neon dragon over a night market" in fake.calls[0]["messages"][-1]["content"]
    assert "prompt" in out["results"][0]["basis"]


@pytest.mark.asyncio
async def test_a_failed_model_call_is_reported_not_raised(client, seed, monkeypatch):
    async def down(messages, system="", model="", options=None, think=False, format=None):
        return "[empty response from some-model (no done_reason reported) — try a different model]"
    monkeypatch.setattr(ai_module, "generate_chat", down)
    ids = _clips(seed, 2)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "audio", "id": i} for i in ids])
    finally:
        db.close()
    assert out["errors"] and all(r["new"] == "" for r in out["results"])

    async def raises(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(ai_module, "generate_chat", raises)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "audio", "id": ids[0]}])
    finally:
        db.close()
    assert out["errors"] and "connection refused" in out["errors"][0]


@pytest.mark.asyncio
async def test_unknown_items_are_reported_and_the_world_style_reaches_the_prompt(client, seed, monkeypatch):
    fake = _FakeChat(lambda i, text: "X Y")
    monkeypatch.setattr(ai_module, "generate_chat", fake)
    ids = _clips(seed, 1)
    db = SessionLocal()
    try:
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "audio", "id": ids[0]}, {"kind": "audio", "id": 424242}, {"kind": "bogus", "id": 1}],
                               preset="short", style="no numbers")
    finally:
        db.close()
    assert len(out["results"]) == 1 and len(out["errors"]) == 2
    body = fake.calls[0]["messages"][-1]["content"] + fake.calls[0]["system"]
    assert "World A" in body and "no numbers" in body and mr.STYLE_PRESETS["short"] in body


# ── apply and undo ───────────────────────────────────────────────────────────────────────────────────────

def _apply(db, seed, renames, user_id=None):
    return mr.apply_renames(db, _db_world(db, seed), user_id, renames)


def test_apply_renames_clips_and_images_and_logs_them(client, seed):
    audio_id, = _clips(seed, 1)
    video_id, = _clips(seed, 1, kind="video", name="VID_1")
    url = _write("a1b2c3d4e5f6-x.png", _png_bytes())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Misc", image_urls_json=json.dumps([url])))
        db.commit()
        out = _apply(db, seed, [
            {"kind": "audio", "id": audio_id, "name": "  Ember   Waltz "},
            {"kind": "video", "id": video_id, "name": "Opening Cutscene"},
            {"kind": "image", "url": url, "name": "Harbour at Dusk"},
        ], user_id=seed.gm.id)
        assert out["applied"] == 3 and not out["skipped"] and len(out["batch_id"]) >= 8
        assert db.get(AudioClip, audio_id).name == "Ember Waltz"
        assert db.get(VideoClip, video_id).name == "Opening Cutscene"
        assert db.query(MediaTitle).filter(MediaTitle.url == url).one().title == "Harbour at Dusk"
        logs = db.query(MediaRenameLog).filter(MediaRenameLog.batch_id == out["batch_id"]).all()
        assert {(l.kind, l.old_name, l.new_name) for l in logs} == {
            ("audio", "track_0", "Ember Waltz"), ("video", "VID_1", "Opening Cutscene"), ("image", "", "Harbour at Dusk")}
        from app.gallery import world_image_names
        assert world_image_names(db, _db_world(db, seed), [url])[url] == "Harbour at Dusk"
    finally:
        db.close()


def test_apply_skips_what_it_must_and_says_why(client, seed):
    audio_id, = _clips(seed, 1)
    foreign = SessionLocal()
    try:
        other = AudioClip(world_id=seed.world_b.id, name="theirs", file_url="/uploads/audio/t.mp3")
        foreign.add(other)
        foreign.commit()
        other_id = other.id
    finally:
        foreign.close()
    db = SessionLocal()
    try:
        out = _apply(db, seed, [
            {"kind": "audio", "id": audio_id, "name": "track_0"},               # unchanged
            {"kind": "audio", "id": audio_id, "name": "   "},                    # empty name for a clip
            {"kind": "audio", "id": other_id, "name": "Stolen"},                 # another world's
            {"kind": "audio", "id": 424242, "name": "Nothing"},
            {"kind": "image", "url": "/uploads/not-in-this-world.png", "name": "Nope"},
            {"kind": "image", "url": "/etc/passwd", "name": "Nope"},
            {"kind": "bogus", "id": 1, "name": "x"},
            "junk",
        ])
        assert out["applied"] == 0 and len(out["skipped"]) == 8 and all(s["reason"] for s in out["skipped"])
        assert db.get(AudioClip, other_id).name == "theirs"
    finally:
        db.close()


def test_apply_limits_the_batch_and_the_name_length(client, seed):
    ids = _clips(seed, 3)
    db = SessionLocal()
    try:
        out = _apply(db, seed, [{"kind": "audio", "id": ids[0], "name": "n" * 600}])
        assert out["applied"] == 1 and len(db.get(AudioClip, ids[0]).name) <= 256
        with pytest.raises(ValueError):
            _apply(db, seed, [{"kind": "audio", "id": ids[1], "name": "x"}] * (mr.MAX_APPLY + 1))
    finally:
        db.close()


def test_an_empty_image_name_clears_the_title(client, seed):
    url = _write("a1b2c3d4e5f6-y.png", _png_bytes())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Misc", image_urls_json=json.dumps([url])))
        db.commit()
        _apply(db, seed, [{"kind": "image", "url": url, "name": "Chosen Title"}])
        out = _apply(db, seed, [{"kind": "image", "url": url, "name": ""}])
        assert out["applied"] == 1
        assert db.query(MediaTitle).filter(MediaTitle.url == url).count() == 0
    finally:
        db.close()


def test_undo_restores_old_names_and_skips_what_was_renamed_since(client, seed):
    a, b = _clips(seed, 2)
    url = _write("a1b2c3d4e5f6-z.png", _png_bytes())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Misc", image_urls_json=json.dumps([url])))
        db.commit()
        out = _apply(db, seed, [{"kind": "audio", "id": a, "name": "First"}, {"kind": "audio", "id": b, "name": "Second"},
                                {"kind": "image", "url": url, "name": "Pic"}])
        db.get(AudioClip, b).name = "Edited by hand later"
        db.commit()
        undone = mr.undo_batch(db, _db_world(db, seed), out["batch_id"])
        assert undone["restored"] == 2 and len(undone["skipped"]) == 1
        assert db.get(AudioClip, a).name == "track_0"
        assert db.get(AudioClip, b).name == "Edited by hand later"
        assert db.query(MediaTitle).filter(MediaTitle.url == url).count() == 0
        again = mr.undo_batch(db, _db_world(db, seed), out["batch_id"])
        assert again["restored"] == 0, "a batch is only undone once"
        assert mr.undo_batch(db, _db_world(db, seed), "nope")["restored"] == 0
    finally:
        db.close()


def test_undo_never_reaches_into_another_world(client, seed):
    a, = _clips(seed, 1)
    db = SessionLocal()
    try:
        out = _apply(db, seed, [{"kind": "audio", "id": a, "name": "Renamed"}])
        assert mr.undo_batch(db, db.get(World, seed.world_b.id), out["batch_id"])["restored"] == 0
        assert db.get(AudioClip, a).name == "Renamed"
    finally:
        db.close()


def test_recent_batches_summarise_what_each_did(client, seed):
    a, b = _clips(seed, 2)
    db = SessionLocal()
    try:
        first = _apply(db, seed, [{"kind": "audio", "id": a, "name": "One"}], user_id=seed.gm.id)
        second = _apply(db, seed, [{"kind": "audio", "id": b, "name": "Two"}], user_id=seed.gm.id)
        mr.undo_batch(db, _db_world(db, seed), first["batch_id"])
        rows = mr.recent_batches(db, _db_world(db, seed))
    finally:
        db.close()
    assert [r["batch_id"] for r in rows] == [second["batch_id"], first["batch_id"]]
    assert rows[0]["count"] == 1 and rows[0]["undone"] is False and rows[1]["undone"] is True
    assert rows[0]["by"] and rows[0]["examples"][0] == {"old": "track_1", "new": "Two"}


@pytest.mark.asyncio
async def test_an_image_that_is_not_in_this_world_is_never_read(client, seed):
    url = _write("elsewhere.png", _png_bytes())
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_b.id, name="Theirs", image_urls_json=json.dumps([url])))
        db.commit()
        assert await mr.gather_signals(db, _db_world(db, seed), "image", {"url": url}, pictures=True) is None
        out = await mr.suggest(db, _db_world(db, seed), [{"kind": "image", "url": url}], pictures=True)
    finally:
        db.close()
    assert out["results"] == [] and out["errors"]
