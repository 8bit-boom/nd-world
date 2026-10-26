"""STT model-name fallback: repo id → basename when Studio 409s/400s.
Exact-match asserts."""
p = 'app/unsloth_extras.py'
src = open(p, encoding='utf-8').read()

start = src.index('''    if not audio:
        raise StudioError("No audio to transcribe", 400)
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_STT_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                                data={"model": model}, headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or \\
                  (body_err.get("detail") if isinstance(body_err.get("detail"), str) else None) or \\
                  f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    data = resp.json()
    return data.get("text") or ""''')

new = '''    if not audio:
        raise StudioError("No audio to transcribe", 400)
    url, key = _base_key()
    # Studio's Voice settings name STT models with its OWN short names
    # ("small", "Qwen3-ASR-1.7B-GGUF" without the hub org) — a full hub
    # repo id like "unslothai/Qwen3-ASR-1.7B-GGUF" gets "not downloaded"
    # (observed live). Try the name as given; on a not-downloaded/unknown
    # rejection, retry with the basename (org prefix stripped).
    tried = []

    async def _post(model_name: str):
        async with _httpx.AsyncClient(timeout=_STT_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/transcriptions",
                                files={"file": (filename, audio)},
                                data={"model": model_name}, headers=_headers(key))
        tried.append(model_name)
        return resp

    resp = await _post(model)
    if resp.status_code in (400, 404, 409) and "/" in model:
        base = model.rsplit("/", 1)[1]
        resp = await _post(base)
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or \\
                  (body_err.get("detail") if isinstance(body_err.get("detail"), str) else None) or \\
                  f"HTTP {resp.status_code}"
        if resp.status_code in (400, 404, 409) and len(tried) == 2 and tried[0] != tried[1]:
            message += f" (also tried model name {tried[0]!r})"
        raise StudioError(message, resp.status_code)
    data = resp.json()
    return data.get("text") or ""'''

src = src[:start] + new + src[start + len(src[start:start]) :]
# the slice above: replace from start to the same end — compute end properly
open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("stt fallback written")
