# Fan control and the hardware panel in the gateway (phase 5)

## Fan control (`fans` in `ND_RUST_ROUTES`)

`app/fan_control.py` drives real hardware - the motherboard fan headers through Linux hwmon - from a background loop, so it cannot be
shared: two processes writing the same `pwmN` file would fight. With `fans` on, the gateway owns the fans completely, and Python must be
told to stand down:

    ND_RUST_ROUTES=...,fans          # the gateway
    ND_FANS_OWNER=gateway            # the Python app: its follow loop and its shutdown raise do nothing

Turn both on (or both off) together. The gateway (`rust/nd-gateway/src/fans.rs`) then does what Python did, with the same files:

| What | How |
|---|---|
| `GET /api/system/fans` | The fan headers hwmon exposes under `FAN_HWMON_ROOT` (default `/sys/class/hwmon`), each with its stable id (`<chip>@<device>/pwmN`, never `hwmonN`), rpm, duty, mode, whether it is writable and whether this app holds it; the saved settings, missing fans, this run's check verdicts and the last follow tick. |
| `POST /api/system/fans` | Hold a fan (`percent`, never under 20 %, re-read after 1.2 s to catch firmware that puts its own value back), give it back to the BIOS (`auto`), or save "follow the GPU temperature" (`follow_gpu`, `fans`, `curve`: 2-8 points, rising, reaching 80 % by 75 C; an output this run's check found inverted or not held is refused). |
| `POST /api/system/fans/check` | 100 % for six seconds, the rpm compared, the output put back exactly as it was. |
| The follow loop | Every 5 s while it is saved as on (started when the gateway starts): the hottest GPU's temperature from `nvidia-smi`, the curve's value (100 % when the GPU cannot be read or is at/over 80 C), speeding up at once and slowing at most 5 points a tick. |
| Shutdown | Every fan this app holds below 60 % is raised to 60 % (another container may still load the GPU). |

The saved settings (`fan_control.json`) and what the BIOS had set (`fan_control_orig.json`, kept per boot) are the same files beside the
database Python uses, so switching the owner keeps them. A non-GM caller and a body that is not JSON still get Python's 403 / 400.

Known difference: when the chip refuses a write, the message names the operating system's error without the file's path.

Tests: `tests/test_gateway_fans.py` runs the same 23 steps against a fake fan-chip tree first on Python, then (the tree and files put
back) on the gateway: the same answers and the same values left on the chip. `tests/test_fan_control.py` still checks Python's module.

## The hardware panel (`GET /api/ai/hardware`, with `ai`)

`app/ollama_tuning.py`'s `detect_hardware` and `recommend_settings` for Settings > System's "Detected hardware" panel: CPU and RAM from
`/proc`, the GPU from `nvidia-smi` or AMD's sysfs, the GM's VRAM override or a planning preset (`?preset=` previews one), and per model
the backend lists a starting-point bundle and the Unsloth quant tier. With a Studio backend `/v1/models` gives names only (no sizes),
exactly as app/llm_client.py reads it. Stays Python's: the legacy Ollama backend, SwarmUI's GPU report (an `IMAGEGEN_TYPE=swarmui`
install), and the Ollama server env file, which the Settings page (Python's) writes. Test: `test_hardware_panel_is_pythons`.
