# AI-surface audit — 2026-09-30

Full read-only audit of every AI surface (five parallel passes: core
chat/streaming, per-feature routers, RAG/embeddings, image generation,
and the Unsloth integration as a system), followed by fixes. Companion
docs: STT_LIVE_AUDIT_2026-09.md (speech, same week) and
LIVE_RECORDING_AUDIT.md. Deployment context throughout: single-GM
TrueNAS, CPU-only Unsloth Studio behind a Cloudflare tunnel (~100 s
response ceiling).

## Fixed in this pass

**Leaks / data loss (wave 1):**
- Character-creator RAG ran UNFILTERED (GM visibility) for player callers,
  default-on — generated backstories could echo hidden entities, GM-only
  notes, and un-stripped [gmonly] lore. Now forced off for non-GMs, same
  rule as every other player AI surface.
- /api/ai/chat/compact returned failure sentinels as the "summary"; the
  client spliced "[AI error: …]" over the GM's older turns and SAVED it.
  Now a 502, history untouched.
- The 401 key-death banner never saw the MAIN chat path (only
  unsloth_extras' own requests) — a dead key 401'd every chat invisibly,
  and a fixed key kept the banner armed forever. Wired into both chat
  choke points in llm_client.
- The banner also armed/cleared on PUBLIC endpoints (/version etc.) —
  now only auth-required paths count.
- init_image (img2img) accepted free-form paths and read ANY file the
  process can read, exfiltrating it to the remote Studio. Containment-
  checked like _swarmui_model_path.
- prep/generate (+siblings) leaked another world's session recap to an
  assistant via bare-id + section-matrix mismatch. Membership check added.
- Faction-graph generators served hidden organizations and [gmonly]
  text to players/assistants. Caller-visibility filtered, with
  view-local copies (never mutating the ORM rows).
- King-in-Yellow generate saved failure sentinels as play text and had
  no heartbeat. Error events + heartbeat + bounded model resolve.

**Robustness (wave 2):** stt() no longer leaks raw httpx exceptions
through the "never raises" health check; the Studio Models tab's
"loaded" badges work (AttributeError fix); video-job polling survives
transient Studio errors (30 consecutive) and its state matching is
word-boundary based; health-check routes answer within
UNSLOTH_HEALTHCHECK_TIMEOUT_SECONDS (75 s default) instead of hanging
to 30 min; resolve_model is bounded (10 s) so no SSE surface zero-byte
freezes on a cold Studio; cockpit find poll re-checks GM; condense-chat
gains the recap family's num_predict degeneration guard; the facts
sync routes use their surface model defaults; raw request.json() parse
failures are 400s; board slug lookups are world-scoped; malformed JSON
on auto-tag/video-job starts is a 400; image-gallery fetch notes 401s.

**Pool/perf (wave 3):** ai_stream releases its pooled connection before
streaming; the direct imagegen route takes the same single-slot
semaphore jobs hold; SwarmUI model downloads clean their .part file on
client disconnect (finally, not except); the /v1 image fallback uses
the GM's PICKED model instead of silently swapping the env default;
vault sync refuses to swap a good index for an all-failed one and
reports embed_failures; the smart-context route clamps limits and caps
its query like the player route; prep_generate releases its connection
across the AI await; the Studio iframe gained a sandbox attribute and
the direct link rel="noopener noreferrer".

## Known gaps — deliberately deferred (with reasons)

- **RAG-1: embedding model/backend switch never invalidates stored
  vectors.** A dim mismatch silently returns [] (same-dimension
  switches silently degrade). Workaround TODAY: re-run the Knowledge
  sync after switching embedding backends. Full fix wants a per-chunk
  model fingerprint + re-sync prompt — deferred as its own change.
- **RAG-3/4: vault sync and vector_search hold the caller's session
  across embedding HTTP calls** (up to 300 s each). Single-user
  deployment keeps this survivable; deferred with the RAG-1 rework.
- **RAG-7: no per-chunk size cap** — a heading-less mega-note embeds
  only its first ~512 tokens. Deferred (chunking semantics).
- **Chat-5: .wav attachments under Unsloth send a note promising audio
  was passed to the model while the shim drops it.** Deferred pending a
  real input_audio-capable model to verify against.
- **Chat-7: direct structured callers bypass generate_chat's
  response_format fallback** — a refactor routing ~10 call sites
  through one helper; deferred as its own change.
- **Chat-8/9/11/12: smaller core-chat internals** (unclosed <think>
  trailing block, per-model num_predict guard clobbering, stream
  format-retry guards, _with_heartbeat exception masking) — each small
  but in the hottest path; deferred for focused tests rather than a
  batch landing.
- **Imagegen-1/5/8: orphaned files on mid-batch failure, no Unsloth
  generation progress wiring, player-cap TOCTOU** — first is rare and
  self-limiting, second wants live verification against Studio, third
  needs a lock design.
- **Unsloth-6/9/10: hub-download cancel has no route/button; video
  re-attach crash window; clip/job double-commit window** — UI/feature
  work more than defects.
- **Routers-3/4/10/11: expand-notes, vision sheet import, transcript-
  retry, folk-tale lack background-job variants or input caps** —
  feature additions; the job pattern to copy is documented in each
  finding.
