"""Image generation loads the CHOSEN model itself when Studio has none loaded.

Image generation sends the picked model to Studio and used to rely on Studio's "media auto-switch" to load it by name.
With that off Studio answers 409 "No diffusion model is loaded." and nd-world could only print advice ("enable
auto-switch or load the model from the Models tab") even though the model was already chosen. Now it loads that model
(the right GGUF file for the repo), waits for Studio to finish, and generates — once; a model that cannot load, or a
second refusal, is reported with Studio's own reason instead of looping."""
import pytest

import app.ai as ai_module
from app import unsloth_extras

from .test_unsloth_imagegen import _FakeResponse as _BaseResponse, _PNG_BYTES, _patch_http, unsloth_image_mode  # noqa: F401


class _FakeResponse(_BaseResponse):
    headers: dict = {}          # the real error path reads response headers

CHOSEN = "vantagewithai/Krea-2-Turbo-GGUF"
NO_MODEL = {"detail": "No diffusion model is loaded."}


class Studio:
    """A scripted Studio: records the order of calls, answers each endpoint from simple switches."""

    def __init__(self, *, generate=("409", "ok"), variants=None, load=None, status_after=2, status_error=None):
        self.calls = []
        self.generate_script = list(generate)
        self.variants = variants if variants is not None else {"variants": ["Krea-2-Turbo-Q4_K_M.gguf", "Krea-2-Turbo-Q8_0.gguf"],
                                                               "default_variant": "Krea-2-Turbo-Q4_K_M.gguf"}
        self.load = load or (lambda body: _FakeResponse(200, payload={"loaded": False}))
        self.status_polls = 0
        self.status_after = status_after          # polls before "loaded": true
        self.status_error = status_error
        self.load_body = None

    def __call__(self, method, url, headers, body):
        path = url.split("8000", 1)[-1]
        self.calls.append((method, path.split("?")[0]))
        if path.startswith("/api/inference/images/generate") and method == "POST":
            step = self.generate_script.pop(0)
            if step == "409":
                return _FakeResponse(409, payload=NO_MODEL)
            return _FakeResponse(200, payload={"images": [{"id": "img1", "url": "/api/inference/images/gallery/img1/file"}]})
        if "/gallery/img1/file" in path:
            return _FakeResponse(200, content=_PNG_BYTES)
        if path.startswith("/api/hub/gguf-variants"):
            return _FakeResponse(200, payload=self.variants)
        if path.startswith("/api/inference/images/load") and method == "POST":
            self.load_body = body
            return self.load(body)
        if path.startswith("/api/inference/images/status"):
            self.status_polls += 1
            if self.status_error:
                return _FakeResponse(200, payload={"loaded": False, "error": self.status_error})
            return _FakeResponse(200, payload={"loaded": self.status_polls >= self.status_after, "repo_id": CHOSEN})
        raise AssertionError(f"unexpected call {method} {path}")


async def _generate(tmp_path, model=CHOSEN):
    return await ai_module.imagegen_generate(
        prompt="a dragon", negative="", model=model, width=512, height=512,
        steps=4, cfg=1.0, seed=1, uploads_dir=tmp_path)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(unsloth_extras, "IMAGE_LOAD_POLL_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_loads_the_chosen_model_then_generates(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio()
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    urls = await _generate(tmp_path)
    assert len(urls) == 1 and (tmp_path / "ai-images" / urls[0].rsplit("/", 1)[1]).read_bytes() == _PNG_BYTES
    order = [c for c in studio.calls if c[1] != "/api/inference/images/status"]
    assert order == [
        ("POST", "/api/inference/images/generate"),        # refused: nothing loaded
        ("GET", "/api/hub/gguf-variants"),                 # which file of the repo
        ("POST", "/api/inference/images/load"),
        ("POST", "/api/inference/images/generate"),        # the retry
        ("GET", "/api/inference/images/gallery/img1/file"),
    ]
    assert studio.status_polls >= 2, "must wait for Studio to finish loading before generating"
    # the model that was CHOSEN, with the repo's default GGUF file
    assert studio.load_body == {"model_path": CHOSEN, "model_kind": "gguf", "gguf_filename": "Krea-2-Turbo-Q4_K_M.gguf"}


@pytest.mark.asyncio
async def test_nothing_is_loaded_when_the_first_try_works(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(generate=("ok",))
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    await _generate(tmp_path)
    assert ("POST", "/api/inference/images/load") not in studio.calls


@pytest.mark.asyncio
async def test_a_repo_with_one_file_and_no_default_uses_that_file(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(variants={"variants": ["only-one.gguf"]})
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    await _generate(tmp_path)
    assert studio.load_body["gguf_filename"] == "only-one.gguf"


@pytest.mark.asyncio
async def test_a_repo_with_several_files_and_no_default_still_loads_one(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(variants={"variants": ["m-Q4_K_M.gguf", "m-Q8_0.gguf"]})
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    await _generate(tmp_path)
    assert studio.load_body["gguf_filename"] == "m-Q4_K_M.gguf"        # the first listed, not a refusal


@pytest.mark.asyncio
async def test_studio_without_a_variants_endpoint_is_loaded_without_a_filename(unsloth_image_mode, monkeypatch, tmp_path):
    class NoVariants(Studio):
        def __call__(self, method, url, headers, body):
            if "/api/hub/gguf-variants" in url:
                self.calls.append((method, "/api/hub/gguf-variants"))
                return _FakeResponse(404, payload={"detail": "nope"})
            return super().__call__(method, url, headers, body)

    studio = NoVariants()
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    await _generate(tmp_path)
    assert studio.load_body == {"model_path": CHOSEN, "model_kind": "gguf"}


@pytest.mark.asyncio
async def test_a_model_that_will_not_load_is_reported_with_studios_reason(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(load=lambda body: _FakeResponse(400, payload={"detail": "Not enough VRAM for this model"}))
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path)
    msg = str(e.value)
    assert CHOSEN in msg and "Not enough VRAM" in msg
    assert studio.generate_script == ["ok"], "must not retry generation after a failed load"


@pytest.mark.asyncio
async def test_a_load_that_never_finishes_gives_up_with_a_clear_message(unsloth_image_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(unsloth_extras, "IMAGE_LOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(unsloth_extras, "IMAGE_LOAD_POLL_SECONDS", 0.01)
    studio = Studio(status_after=10**9)
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path)
    assert "still loading" in str(e.value).lower() and CHOSEN in str(e.value)


@pytest.mark.asyncio
async def test_a_load_that_reports_an_error_stops_waiting(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(status_after=10**9, status_error="CUDA out of memory")
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path)
    assert "CUDA out of memory" in str(e.value)


@pytest.mark.asyncio
async def test_a_second_refusal_is_not_retried_forever(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(generate=("409", "409", "409"))
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path)
    assert "No diffusion model is loaded" in str(e.value)
    assert studio.calls.count(("POST", "/api/inference/images/generate")) == 2
    assert studio.calls.count(("POST", "/api/inference/images/load")) == 1


@pytest.mark.asyncio
async def test_with_no_model_chosen_anywhere_the_old_advice_remains(unsloth_image_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "UNSLOTH_IMAGE_MODEL", "")
    monkeypatch.setattr(ai_module, "_get_type", lambda: "unsloth")          # normally "" without a default model
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://unsloth:8000")
    studio = Studio()
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path, model="")
    assert "media auto-switch" in str(e.value)
    assert ("POST", "/api/inference/images/load") not in studio.calls


@pytest.mark.asyncio
async def test_the_older_v1_endpoint_gets_the_same_treatment(unsloth_image_mode, monkeypatch, tmp_path):
    """A Studio build without the native generate endpoint answers 503 'No image model loaded' on /v1."""
    import base64 as _b64
    seen = {"v1": 0, "loaded": False}

    def handler(method, url, headers, body):
        path = url.split("8000", 1)[-1]
        if path.startswith("/api/inference/images/generate"):
            return _FakeResponse(404, payload={"detail": "API endpoint not found"})
        if path.startswith("/v1/images/generations"):
            seen["v1"] += 1
            if not seen["loaded"]:
                return _FakeResponse(503, payload={"error": {"message": "No image model loaded"}})
            return _FakeResponse(200, payload={"data": [{"b64_json": _b64.b64encode(_PNG_BYTES).decode()}]})
        if path.startswith("/api/hub/gguf-variants"):
            return _FakeResponse(200, payload={"variants": ["a.gguf"], "default_variant": "a.gguf"})
        if path.startswith("/api/inference/images/load"):
            seen["loaded"] = True
            return _FakeResponse(200, payload={"loaded": True})
        if path.startswith("/api/inference/images/status"):
            return _FakeResponse(200, payload={"loaded": True})
        raise AssertionError(path)

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    urls = await _generate(tmp_path)
    assert len(urls) == 1 and seen["v1"] == 2


@pytest.mark.asyncio
async def test_a_studio_without_an_image_load_endpoint_keeps_the_old_advice(unsloth_image_mode, monkeypatch, tmp_path):
    studio = Studio(load=lambda body: _FakeResponse(404, payload={"detail": "API endpoint not found"}))
    _patch_http(monkeypatch, unsloth_image_mode, studio)
    with pytest.raises(ValueError) as e:
        await _generate(tmp_path)
    assert "No diffusion model is loaded" in str(e.value) and "media auto-switch" in str(e.value)
    assert studio.calls.count(("POST", "/api/inference/images/generate")) == 1, "nothing was loaded, so nothing to retry"
