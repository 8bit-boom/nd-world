# Unsloth Desktop with nd-world

Unsloth Desktop is a Tauri app that starts the same Studio backend and talks to it over HTTP, so every route nd-world uses for Unsloth Studio (`/v1/files`,
`/v1/fine_tuning/jobs*`, `/v1/models`, `/api/train/hardware`, `/api/health`) is also there in Desktop. The calls and shapes are in `docs/UNSLOTH_API_CONTRACT.md`
(shared with ND Lore Forge). Everything here is Python-free only, like the rest of the Studio export (`docs/UNSLOTH_STUDIO_EXPORT.md`).

## Connecting nd-world (for example on TrueNAS) to a Desktop on your PC

1. In Unsloth Desktop: avatar -> **Settings -> API**: create a key (shown once) and copy it.
2. Same page, **Remote & LAN**: press **Start** on the LAN access card (and switch on *Start automatically*). A plain Desktop listens on that PC only; the LAN card shows the
   address and port to use. The traffic is plain HTTP on your network: keep it to a trusted network, give the PC a fixed address (a DHCP reservation in the router).
   There is **no Cloudflare tunnel** here: it would publish the server to the internet.
3. nd-world **Settings**: Unsloth URL = `http://<PC address>:<port>` (not `127.0.0.1`: inside a container that is the container itself), Unsloth key = the key. Press **Probe Studio**:
   the line says "Unsloth Desktop <version>" or what to do (LAN access off, wrong port, key not accepted, not Unsloth).
4. Desktop serves only while it is open. Keep it open while a job runs. A job waiting in Unsloth's queue is forgotten when Unsloth restarts.

On the same machine as nd-world (no container) `http://127.0.0.1:<port>` works without the LAN card.

## What the card does (and what Desktop cannot do)

- **Train in Unsloth Studio now** probes first (key, fine-tuning API, version) and sends nothing when the probe fails; the card shows "Unsloth <version>" and
  "(not checked by nd-world yet)" until a version is in the tested table (`TESTED_VERSIONS` in `teacher/train.rs` and the contract doc).
- **Jobs in Unsloth**: Unsloth's own job list, each labelled with its world (job suffix `<slug>-<date>`), with a Follow button.
- **Cancel** only. Pause and Resume were removed: Studio always answers 400 "Pausing and resuming fine-tuning jobs is not supported; use cancel." (the two routes remain
  and answer 400 without calling Studio).
- Not built: asking Unsloth to export a GGUF and loading it for AI chat (see "Decisions"). Export in Unsloth's own Export tab.

## Decisions (owner)

- Pause/Resume removed now (it was a bug in the earlier feature).
- The experimental "Make a GGUF in Unsloth + load it for chat" item (plan ND5) is **not built**. No Ollama import either.
- TrueNAS reaches Desktop on the PC's LAN address with Desktop's LAN access on; no tunnel.
- No shared crate with ND Lore Forge: the contract doc and identical fixtures (`tests/fixtures/unsloth/*.json`).

## Deviations from the plan

- The contract doc and fixtures were written here from the plan's section 3.0 and Unsloth's source (the fixture of `TrainingStartRequest` fields was extracted from it at commit
  `1bbf9c3f`), because the Lore Forge copy did not exist yet. They must be reconciled with that repository's copy (same file names; diff them).
- "Newer than nd-world was tested with" cannot be decided without a known tested version, so the card says "not checked by nd-world yet" for any version outside the (still
  empty) tested table.
- The Settings "Find Unsloth" is the existing **Probe Studio** button with the new `unsloth` block, not a second button. The block only exists in the Python-free image;
  Python's probe is unchanged (golden parity test).
- Docs of each step were collected in the docs step, not committed with each step.

## Not verified yet (needs a real Desktop and a GPU)

- Which port Desktop listens on by default, and that its `/api/health` carries `desktop_owner` / `studio_root_id` as read in the source (the probe handles their absence).
- That an API key made in Desktop works on `/v1/models`, `/v1/fine_tuning/jobs` and `/api/train/hardware`, and that a bad key really gets the unauthenticated health body.
- That `load_in_4bit: false` really trains in 16 bit and the QLoRA 27B picks the 4-bit repo.
- Queued text while Desktop chats, cancel, the job list after a Desktop restart, an auto-update between two runs (record the new version in `TESTED_VERSIONS` and the contract doc).
- LAN access from the TrueNAS box to the PC (firewall, address, port).
