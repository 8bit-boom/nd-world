# Rust port audit: how much is converted, what is left, what to leave in Python

State: `main` at `72dc020` (every test green: 6497 in-process, four gateway shards, plus the same shards under `TZ=Europe/Budapest`).
Numbers below come from a clean full gateway run (`scripts/census.py`, forwarded-request logs deleted first), the Python-free crawl
(`scripts/python_free_crawl.sh`), and a line count. They measure what the tests exercise, not production traffic.

## 1. The size of the job

| | Python app | Rust gateway (nd-gateway + nd-render + nd-perm) |
|---|---|---|
| Source | ~70,500 lines in 139 files | ~54,000 lines |
| Routes | 761 route/method pairs (302 GET, 437 POST, 20 DELETE, 1 PUT, 1 PATCH) | |

### Who answers the traffic (full test suite through the gateway)

| | routes | share |
|---|---|---|
| Answered only by the gateway | 641 | 54 % |
| Mixed: the gateway answers the normal case, Python only unusual input or refusals | 445 | 37 % |
| Python only | 109 | 9 % |

Of the 109 Python-only routes, 55 are GET and 53 POST. Almost all of them are the parity tests' own junk-input probes (a missing id, a
bad token, `/api/ai/whisper/*` which does not exist), not features. By request count Python still receives about 2,300 requests against
about 14,800 the gateway answers itself: ~13 % of the test traffic, and nearly all of it error edges (the 400/404/422 answers
Python words itself). A decline-trace sweep of the 5,987 feature tests found 316 declines, all from bad or unusual input except the home
dashboard's refusals (now ported).

### Python-free crawl (what a container with no Python can serve)

18 of the app's 179 parameterless GET routes answer 501 on a UTC host (161 = 90 % work). The 18: FastAPI's `/docs`, `/redoc`,
`/openapi.json`, `/docs/oauth2-redirect`; the legacy Ollama / Hugging Face AI routes (`/api/ai/models|resident|hardware|debug|test-chat|`
`ollama/hf-*|imagegen/models/hf-files|imagegen/models/downloaded|unsloth/hub/download-progress|gguf-variants|prompt-presets`; `prompt-presets` and `models/downloaded` are in the list only because the crawl sends no required parameter, which is Python's 422); `/api/kiy/inspirations`;
`/export/foundry.json`. A real container run (Docker, Ubuntu / WSL 2) confirmed: build first try, 746 MB against 1.1 GB, ~26 MiB
against ~346 MiB of memory, and the flows create world / entity / upload / invite / join / dice / character / restart all work.

## 2. What is left, sorted by value

### Do (small, worth it)

| Item | Why | Effort |
|---|---|---|
| Re-run the container checks on the new build: `/characters`, `/characters/{id}`, `/worlds/{id}/edit`, `/uploads` signed out, crawl under a non-UTC TZ | the report listed them 501; `/worlds/{id}/edit` and `/uploads` are fixed, the character pages answer here but the report's host had `TZ=Europe/Budapest` (local time is now handled) | minutes |
| The manual browser flows and the upgrade check on a COPY of the real data volume (`docs/PYTHON_FREE_IMAGE_BUILD.md` section 7) | only a real volume shows what a hand-made test database cannot | an hour |
| Run the decline-trace sweep against your own usage (`ND_DECLINE_TRACE=1` on the gateway for a week) | the only way to find what YOU hit that the tests do not | none |

### Do only if you use the feature

| Item | Needs | Effort |
|---|---|---|
| A TLS client in the gateway (`rustls`) | Hugging Face search / downloads, King in Yellow web fetches, a Studio or Ollama URL that is `https://`. Today those need Python; Python-free they answer 501 | medium (a client, a proxy setting, tests against a local TLS server) |
| Character PDF export (`GET /characters/{id}/export.pdf`, reportlab) and PDF text for AI attachments (pypdf) | a PDF writer / reader in Rust (`printpdf` / `lopdf`) and byte-level care, or keep a tiny Python sidecar for these two | large for exact output; or drop the feature |
| Maps whose picture is AVIF / GIF / animated, and video conversion | the `image` crate has no AVIF decoder here; ffmpeg is already in the image and can do it | small-medium (shell out to ffmpeg) |

### Leave in Python (or drop)

| Item | Reason |
|---|---|
| FastAPI's `/docs`, `/redoc`, `/openapi.json` | generated from Python types; no user needs them in production |
| The legacy Ollama back end's status / hardware / test-chat routes | the Unsloth Studio path is ported; porting a back end you no longer use is wasted work. Decide whether to delete it |
| World Foundry export (`/export/foundry.json`) | its HTML must be byte-identical to `markdown2`'s output and `pulldown-cmark` is not; only a byte-exact markdown2 port unlocks it. The per-character Foundry export is done |
| The tail of 445 "mixed" routes (unusual-input refusals) | on a Python-free install they answer a generic 400; porting each wording buys nothing a user sees. Port one only when the sweep on real use shows someone hits it |
| The build-time Python stage of `Dockerfile.gateway` (thumbnails, AVIF maps, tile pyramids, minify and compress) | runs once at build, never in the running container; not worth a Rust rewrite |
| The test harness (`tests/`, `pytest`, `tests/serve.py`) and `Dockerfile` (the Python + gateway image) | the Python app is the oracle every parity test compares against, and the fallback image. Keep both for as long as you may need to roll back |

## 3. Honest answer: how much conversion do you need

- For a Python-free install used the way the tests use the app, the conversion is essentially done: ~90 % of routes work, and the 10 % left
  is documentation pages, the legacy back end, outbound HTTPS, PDFs and one export, none of them the core of running a table.
- If you do NOT use Hugging Face search, KIY fetches, character PDFs or the world Foundry export, you need **no more code** before
  switching: the remaining work is verification (section 2, first table).
- If you DO use https back ends or web fetches: one medium item (a TLS client) covers most of them.
- Convert nothing else "for completeness". The remaining 109 / 445 routes are cheap to leave as 400 / 501 and costly to port byte-for-byte.

## 4. Risks to keep in view

- A wrong answer is worse than a decline: every port compares against Python on both servers; keep that rule.
- Python-free means a request only Python could serve returns 501 (GET) or a generic 400 (write), not a real error message. Watch for those after
  the switch (`docker logs` shows each; `ND_DECLINE_TRACE=1` lists them with their bodies).
- Time zones: the gateway reads TZ the way the C library does; Python uses `datetime`. They agree except across a daylight-saving gap; the shards
  pass under `Europe/Budapest`, but a host in another zone has not been run.
- Do not point the new image at the production volume before the copy test; keep `/admin/backup.zip` before any switch.
