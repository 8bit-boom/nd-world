# Unsloth API contract (nd-world and ND Lore Forge)

Kept in sync with the same file in the ND Lore Forge repository (`docs/UNSLOTH_API_CONTRACT.md`): the same shapes, the same fixtures
(`tests/fixtures/unsloth/*.json`). There is no shared crate (decision: two small clients, nd-world async hyper with Python parity rules, Lore Forge sync `ureq`).
Evidence marks: **[V]** read in Unsloth's source at backend commit `1bbf9c3f` (2026-11-13), **[A]** assumption, **[Desktop]** needs a real Unsloth Desktop (and a GPU) to confirm.
Studio and Desktop run the same backend, so one contract covers both.

## Calls

Base URL `B` (no trailing slash). Every call sends `Authorization: Bearer <sk-unsloth-...>` except the two unauthenticated probes. Timeouts: connect 5 s, read 30 s (uploads 300 s, export calls 3600 s).

| Purpose | Call | Expect |
|---|---|---|
| Is it Unsloth? (no key) | `GET B/api/health` | 200 `{"status":"healthy","service":"Unsloth UI Backend","studio_root_id",...,"desktop_protocol_version":1,"desktop_owner"?:{...},"chat_only"}` (`health_unauth.json`) [V] |
| Cheap liveness (no key) | `GET B/api/liveness` | `{"status":"alive","service":"Unsloth UI Backend",...}` [V] |
| Key valid + version | `GET B/api/health` with a key | adds `version`, `studio_version`, `server_url`, `cloudflare_url`, `secure`, `cpu_training`, `chat_only_reason` (`health_auth.json`). A bad key gets the unauthenticated body (no `version`), **not** 401 [V]: no `version` with a key sent means "key refused" |
| Key check that 401s | `GET B/v1/models` | 401 on a bad or revoked key [V docs] |
| Fine-tuning present | `GET B/v1/fine_tuning/jobs?limit=1` | 200 list; 404 = too old [V] |
| Card | `GET B/api/train/hardware` | `{"devices":[{"name","vram_total_gb",...}]}` (`hardware.json`) [V] |
| Upload | `POST B/v1/files` multipart `purpose=fine-tune`, `file=<name>.jsonl` | `{"id":"file-<hex>"}` [V] |
| Start | `POST B/v1/fine_tuning/jobs` | 200 job object, `status` `validating_files` or `queued` (id `ftjob_q<hex>`, kept in Studio's memory only) (`job_queued.json`) [V] |
| Follow | `GET B/v1/fine_tuning/jobs/{id}`, `GET .../events?limit=100`, `GET .../checkpoints` | statuses `validating_files` `queued` `running` `succeeded` `cancelled` `failed`; events newest first (`events.json`), `type:"metrics"` carries `step total_steps train_loss learning_rate grad_norm valid_loss epoch` [V] |
| List | `GET B/v1/fine_tuning/jobs?limit=N` | `{"data":[job...]}`, each job has `suffix` (= the project name we sent) [V] |
| Stop | `POST B/v1/fine_tuning/jobs/{id}/cancel` | 200 job `cancelled`; 400 already finished; 409 not running [V] |
| Pause / resume | `POST .../pause`, `.../resume` | **always 400** "Pausing and resuming fine-tuning jobs is not supported; use cancel." [V]. Neither product offers them |
| Run folder paths | `GET B/api/models/checkpoints` | `{"outputs_dir","models":[{"name","checkpoints":[{"display_name","path","loss"}],...}]}` (`checkpoints.json`) [V]; host paths for API-key callers rest on Unsloth's own MCP code comments [A] |
| Export | `POST B/api/export/load-checkpoint` then `POST B/api/export/export/{lora,merged,gguf,base}`, `GET B/api/export/status` | one export at a time [V]; not used by nd-world (see "Deviations" in `docs/UNSLOTH_DESKTOP.md`) |

Job body: nd-world's `teacher/train.rs::job_body`. Besides the OpenAI-shaped keys (`model`, `training_file`, `validation_file`, `hyperparameters`, `method`, `suffix`, `seed`) any
`TrainingStartRequest` field may be sent as an extra top-level key (`training_start_fields.json`). An unknown key, or one of the managed ones (`model_name`, `local_datasets`,
`local_eval_datasets`, `start_request_id`, `project_name`), is 400 `Unknown parameter(s): a, b`. `load_in_4bit` **defaults to true** in Studio: always send it (`false` for LoRA).
Full fine-tuning and two-stage runs cannot be expressed.

## Failure messages (wording adapted to each UI)

| Situation | Detect | Message |
|---|---|---|
| Not running / wrong port | connection refused or timeout | "Unsloth does not answer at <url>. Open Unsloth Desktop (it serves only while it is open) and check the address in Unsloth -> Settings -> API." |
| Not Unsloth | 200 but `service` differs | "Something else answers at <url> (not Unsloth)." |
| LAN off | refused from a non-loopback address | the Remote & LAN hint |
| Key refused | 401/403 on `/v1/*`, or authed health without `version` | "Unsloth refused the API key. Create a new one in Unsloth (Settings -> API); Unsloth shows it only once." |
| Too old | 404 on `/v1/fine_tuning/jobs` | "This Unsloth has no training API for other programs. Update it, or use the zip." |
| Queued | status `queued` / `ftjob_q` id | "Unsloth is busy; this job waits." |
| Queue lost | 404 on a `ftjob_q` id | "Unsloth restarted and forgot the waiting job. Start it again." |
| Unknown parameter | 400 `Unknown parameter(s): x` | name the keys only |

## Tested versions

| Unsloth | Checked by | Result |
|---|---|---|
| backend commit `1bbf9c3f` (source read, 2026-11-13) | nd-world stand-in tests | contract above |
| first real Desktop version | owner's checklist (`docs/UNSLOTH_DESKTOP.md`) | not yet checked |

Probe features, not versions: a newer Unsloth than the table is tried anyway and the card only notes that it is newer than tested.
