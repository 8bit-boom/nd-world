# Unsloth Migration — Phase 0 Findings

**Date:** 2026-09-25
**Environment:** Local spike box — Docker Desktop (WSL2), NVIDIA RTX 5090 32 GB, `unsloth/unsloth:latest` (image id `86cac86006cf`).
**Spike container:** `nd-unsloth-phase0` on port 8000. NOTE: GPU-specific items (I-7 sm_70, I-10 V100 training) are NOT settled by this box — they must be re-verified on the TrueNAS V100.

Every answer below was produced by live calls against the running server, not documentation.

---

## I-1 — Image generation over HTTP: **YES → Plan A**

- `POST /v1/images/generations` — OpenAI dialect. Verified end-to-end: request `{"model":"unsloth/z-image-turbo-GGUF","prompt":"…","size":"1024x1024","n":1,"response_format":"b64_json"}` → HTTP 200, `data[0].b64_json` = PNG (~1.2 MB). ~22 s per 1024×1024 on the 5090.
- `size` is a `"WxH"` string. `response_format` supports `b64_json` (verified).
- Richer native endpoint `POST /api/inference/images/generate` accepts: `prompt, negative_prompt, width, height, steps, guidance (float), seed, batch_size, init_image, mask_image, strength, upscale, reference_images, loras, controlnet` — maps almost 1:1 onto nd-world's current SwarmUI param set.
- Progress: `GET /api/inference/images/generate-progress` (and `/load-progress` while loading).
- **An image model must be loaded before generating** (`503 "No image model loaded"` otherwise). Two ways: `POST /api/inference/images/load` (`{model_path, model_kind:"gguf", gguf_variant}` — for single-file GGUF repos the error message demands `model_kind:"gguf"` + `gguf_filename`), or enable `media_auto_switch_model` and pass `model` in the request (verified working).
- `GET /v1/models` lists **chat models only** — image models are NOT in it (checked while z-image was loaded). Model discovery for images must use `/api/inference/models` or the load endpoint's error messages.
- Diffusion companion weights download on first load beyond the GGUF: z-image-turbo Q4_K_M (5 GB GGUF) pulled **13.2 GB** total (text encoder etc.). Budget download time/size accordingly on the V100 box.
- VRAM contention is native: log line `gpu_arbiter: evicting chat for diffusion` — loading the image model auto-evicts the resident chat model, and a later chat request auto-reloads it (verified full cycle, ~36 s including reload).

## I-2 — Embeddings: **YES**

- `POST /v1/embeddings` with `{"model":"unsloth/bge-small-en-v1.5","input":[...]}` → OpenAI shape (`data[i].embedding`). Works with a small embedding model downloaded through the hub. No sidecar needed; vault RAG survives.

## I-3 — Structured output: **YES**

- `response_format:{"type":"json_schema","json_schema":{"name":"…","schema":{…}}}` accepted on `/v1/chat/completions`; returned schema-conformant JSON, `finish_reason:"stop"`. Use it as primary; keep nd-world's defensive prompt+parse as fallback per plan.

## I-4 — Thinking control: **solved, per-request**

- Gemma-4 26B **thinks by default** (`reasoning_content` populated; with `max_tokens:20` the whole budget was eaten by reasoning and `content` came back empty with `finish_reason:"length"` — a real starvation trap for small max_tokens values).
- **Disable per request, either spelling works:**
  - `chat_template_kwargs:{"enable_thinking":false}` (verified — `content:"PONG"`, empty reasoning)
  - top-level `enable_thinking:false` (also verified)
- Reasoning transport: non-stream → `choices[0].message.reasoning_content`; stream → `delta.reasoning_content` (standard OpenAI chunk shape). Maps directly onto nd-world's existing `obj.thinking`/`obj.token` SSE contract.
- `finish_reason:"length"` present and maps to the existing starvation diagnostic.

## I-5 — `/v1/models` + auto-swap: **works, but OFF by default**

- Response shape: `{"object":"list","data":[{"id","object":"model","created","owned_by":"unsloth-studio","loaded":bool,"quant","display_name"}]}` — chat models only (see I-1).
- **Requesting a downloaded-but-unloaded model by name returns `400 "No model loaded… enable Model auto-switch"` unless auto-switch is on.** Enable once:
  `PUT /api/settings/openai-auto-switch {"enabled":true}` (+ `"media_auto_switch_model":true` for image models). After that, name-based load/swap works (13.6 GB GGUF → serving in ~17 s).
- Related fields on the same setting: `auto_unload_idle_seconds` (**default 0 = never** — set e.g. 300 on the 16 GB V100 to get idle-unload behavior the plan assumes), `auto_download_model` (default false — good), `auto_unload_keep_kv`.
- Per-model load overrides: `PUT /api/settings/openai-auto-switch/overrides {model_id, max_seq_length, llama_extra_args, …}` — the Studio-side equivalent of per-model context sizing (§6.3).

## I-6 — Vision: **YES**

- `image_url` content part with a `data:image/png;base64,…` URL verified against gemma-4-26B (mmproj-F16.gguf auto-downloaded with the chat model). Model described a test image correctly. `parse_character_from_images`/`parse_entity_from_images` survive with a b64→data-URL wrap.

## I-7 — V100/torch: **FLAG — sm_70 absent from `latest`**

- Container torch: CUDA **12.8**, `get_arch_list()` = `['sm_75','sm_80','sm_86','sm_90','sm_100','sm_120']` — **no sm_70**. Diffusion on a V100 will fail with this image; even some llama.cpp-adjacent paths need checking.
- llama.cpp server: build 11139, `0.4.1-dev`, "Compiled by the Unsloth team". Its CUDA arch list was not introspectable from strings on this box — **on the TrueNAS box, run `docker exec <c> /opt/unsloth/llama.cpp/build/bin/llama-server --list-devices` (or try loading a tiny model) to confirm CC 7.0 chat serving works, and pin an older unsloth image tag if the current one can't serve/diffuse on sm_70.** This is the migration's biggest remaining environment risk.
- Diffusion auto-selected `dtype: bfloat16` on Blackwell — correct there; on V100 set Precision fp16 per plan §9 (Studio per-model setting).

## I-8 — Key persistence: **restart-safe, recreation-unsafe**

- API key survives `docker restart` (verified: still valid after restart).
- Key does **NOT** survive `docker rm` + fresh `docker run` against the **same** studio volume (verified: "Invalid or expired API key" after recreation). Something key-related lives in container-local state.
- **Operational consequence:** TrueNAS "Custom App" redeploys typically recreate the container → the `.env` key will die on redeploy. Provisioning runbook (§5.3) must include "after any redeploy, re-create the key in Studio → Settings → API and update `.env`". (Also observed: `GET /api/auth/api-keys` shows `expires_at: null` — non-expiring keys exist.)

## I-9 — Extras

- **TTS** `POST /v1/audio/speech`: OpenAI shape (`input, model, voice, response_format, speed, instructions, language, seed`). Schema-verified; no live TTS model tested (non-blocking).
- **STT** `POST /v1/audio/transcriptions`: exists (OpenAI shape; multipart). Unsloth's own STT could one day replace the whisper.cpp sidecar — see plan §14.3.
- **Video** `GET/POST /v1/videos`, `/v1/videos/{id}/content`: exist. (§14.2.)
- **Projects:** `/api/chat/projects` exists — Projects have an API surface (plan expected UI-only). No nd-world v1 dependency; recorded for §14.5.
- **RAG:** Studio has its own `/api/rag/*` (knowledge bases, documents, search) — not needed for v1 (nd-world keeps its own FTS retrieval), recorded.

## Auth, transport & quirks

- Every `/v1/*` call needs `Authorization: Bearer sk-unsloth-…`; 401 body: `{"error":{"message":"Not authenticated","type":"authentication_error"}}`. General error shape `{"error":{"message","type","param","code"}}` — OpenAI-compatible.
- **Quirk #5047 (forced streaming): NOT observed** — explicit `"stream":false` honored. Keep the plan's defensive always-explicit-stream rule anyway (zero cost).
- Studio login: username **`unsloth`** (default), password from `UNSLOTH_STUDIO_PASSWORD`; session auth is JWT `Bearer` (NOT cookies — cookie-jar clients fail); keys minted via `POST /api/auth/api-keys` with the session JWT.
- Hub download progress endpoint **lies** (`/api/hub/download-progress` showed 0 % / 0 bytes for completed downloads) — for progress UI trust container logs or on-disk size, not that endpoint.
- Model ids are case-sensitive repo ids as listed by the hub (`unsloth/gemma-4-26B-A4B-it-GGUF`), with quant selection at download/load time (variant), matching plan §5.2's `UNSLOTH_MODEL` default. `UD-IQ4_NL` = 13.6 GB confirmed on HF.

## Decisions unlocked by these findings

1. **Plan A for image generation** (§7.1) — `/v1/images/generations` + native generate for progress/LoRA.
2. **Vault RAG keeps embeddings in-place** — no sidecar, no feature-gate (I-2 yes).
3. **Structured extraction via `response_format`** (I-3 yes) with existing defensive parse as fallback.
4. **Thinking: `chat_template_kwargs.enable_thinking` per request** (I-4); `_InlineThinkSplitter` kept as safety net per §6.4.
5. **Compose must document auto-switch + idle-unload settings** as one-time Studio provisioning steps (I-5/I-8) — the plan's §5.3 gains these steps.
6. **I-7 remains the top environment risk for the V100** — needs the pinned-tag conversation on the TrueNAS box before cutover.
