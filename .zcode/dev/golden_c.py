def test_attachment_audio_jobs_are_pythons(client, seed):
    """The AI attachment background-job routes from the gateway: POST /api/ai/attachments/audio-jobs (a recording stored
    under uploads/ai_attachments and a pending row), the chunk pair (parts reassembled), GET .../audio-jobs/<id> and the
    recent list - a GM always, a player only in a world that opened an AI surface, and a player never seeing anyone
    else's jobs; a world without either surface 403s; job fields exactly Python's (the timestamps as isoformat)."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AudioJob, World, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).players_can_ask_ai = False
        db.get(World, w).players_can_use_ai_chat = False
        jobs = [AudioJob(world_id=w, purpose="attachment", filename=f"memo{i}.webm", status=s,
                         created_by_user_id=seed.player_a.id if i % 2 else seed.gm.id,
                         transcript="t" if s == "done" else None, attachment_url=f"/uploads/ai_attachments/m{i}.webm")
                for i, s in enumerate(("pending", "transcribing", "done", "error"))]
        jobs.append(AudioJob(world_id=w, purpose="session_recap", filename="r.mp3", status="done"))
        jobs.append(AudioJob(world_id=seed.world_b.id, purpose="attachment", filename="b.webm", status="done"))
        db.add_all(jobs)
        db.commit()
        ids = [j.id for j in jobs[:4]] + [999999]
    finally:
        db.close()
    wav = b"RIFF....WAVE junk " * 64
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    # the player's world has neither AI surface: Python's 403 on every route, gateway-declined (403s are forwarded)
    for url in ("/api/ai/attachments/audio-jobs", f"/api/ai/attachments/audio-jobs/{ids[0]}"):
        a, b = pl.get(url), _via(pl, base).get(url)
        assert (a.status_code, a.text) == (b.status_code, b.text) and a.status_code == 403, (url, a.status_code)
    # open a surface: the player may now see their OWN jobs only
    db = SessionLocal()
    try:
        db.get(World, w).players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    for c, who in ((gm, "gm"), (pl, "player")):
        a, b = c.get("/api/ai/attachments/audio-jobs"), _via(c, base).get("/api/ai/attachments/audio-jobs")
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, a.text[:200], b.text[:200])
        assert b.headers.get("x-nd-served-by") == "gateway", who
        if who == "player":
            assert all(str(j["filename"]).startswith("memo") and j["id"] in ids[:4] for j in a.json()), a.json()
            assert [j["id"] for j in a.json()] == [ids[3], ids[1]], a.json()  # own jobs, newest first
        for i in ids:
            url = f"/api/ai/attachments/audio-jobs/{i}"
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, i, a.text[:150], b.text[:150])
    served = sum(1 for c in (gm, pl) for i in ids if _via(c, base).get(f"/api/ai/attachments/audio-jobs/{i}").headers.get("x-nd-served-by") == "gateway")
    assert served > 4, served
    # the status dict's shape: exactly Python's keys
    j = gm.get(f"/api/ai/attachments/audio-jobs/{ids[2]}").json()
    assert set(j) == {"id", "purpose", "filename", "status", "error", "transcript", "attachment_url", "created_at", "updated_at"}, j
    assert j["status"] == "done" and j["transcript"] == "t" and j["purpose"] == "attachment", j
    # a create: the recording stored and a pending row, answered by the gateway
    r = _via(gm, base).post("/api/ai/attachments/audio-jobs", files={"file": ("memo.webm", wav, "audio/webm")})
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") == "gateway", r.text
    new_id = r.json()["job_id"]
    row = _via(gm, base).get(f"/api/ai/attachments/audio-jobs/{new_id}").json()
    assert row["status"] == "pending" and row["filename"] == "memo.webm" and row["purpose"] == "attachment", row
    assert row["attachment_url"].startswith("/uploads/ai_attachments/") and row["attachment_url"].endswith(".webm"), row
    # the same create through Python, on the same world, gives the same shape (both write a row each)
    r2 = gm.post("/api/ai/attachments/audio-jobs", files={"file": ("memo2.webm", wav, "audio/webm")})
    assert r2.status_code == 200 and set(r2.json()) == {"job_id"}, r2.text
    # the chunk pair: two parts reassembled by /complete
    v = _via(gm, base)
    v.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "ab12", "chunk_index": "0"}, files={"file": ("part", wav[:1024], "application/octet-stream")})
    v.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "ab12", "chunk_index": "1"}, files={"file": ("part", wav[1024:], "application/octet-stream")})
    r3 = v.post("/api/ai/attachments/audio-jobs/complete", data={"upload_id": "ab12", "filename": "assembled.webm", "total_chunks": "2"})
    assert r3.status_code == 200 and r3.headers.get("x-nd-served-by") == "gateway", r3.text
    row3 = v.get(f"/api/ai/attachments/audio-jobs/{r3.json()['job_id']}").json()
    assert row3["filename"] == "assembled.webm" and row3["status"] == "pending", row3
    # Python, run the same chunk flow directly, agrees on the row shape
    r4 = gm.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "cd34", "chunk_index": "0"}, files={"file": ("part", wav[:1024], "application/octet-stream")})
    assert r4.status_code == 200 and r4.json() == {"ok": True}, r4.text
    r5 = gm.post("/api/ai/attachments/audio-jobs/complete", data={"upload_id": "cd34", "filename": "py.webm", "total_chunks": "1"})
    assert r5.status_code == 200 and set(r5.json()) == {"job_id"}, r5.text
