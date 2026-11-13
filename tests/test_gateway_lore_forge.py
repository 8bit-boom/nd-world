"""Lore Forge, embedded (crate::lore_forge): the one-click-installable trainer answered by the Python-free gateway.

Runs against a stub worker (ND_LORE_FORGE_WORKER_ARGS — a tiny script that plays `python -m ndforge_worker`: it emits
the protocol events and writes summary.json / export.json / a fake GGUF), a stub venv python
(ND_LORE_FORGE_ENV_PYTHON) and a prepared repo (ND_LORE_FORGE_REPO_DIR), so the whole lifecycle — install, train,
export, install-model into the Studio hub dir, use-model, uninstall — is exercised without PyTorch."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from .test_gateway_golden import GM_PASSWORD, _python_free_gateway

pytestmark = pytest.mark.skipif(not os.environ.get("ND_TEST_BASE_URL") and not os.environ.get("DB_PATH"),
                                reason="needs the test database")

WORKER_STUB = r'''
"""The stub worker: plays `python -m ndforge_worker <job.json>` for the gateway tests (no torch)."""
import json, sys, time
from pathlib import Path

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(job["out"]); out.mkdir(parents=True, exist_ok=True)
if job.get("job") == "train":
    def emit(**kw): print(json.dumps(kw), flush=True)
    emit(event="start", stage="start")
    for step in range(1, 5):
        emit(event="metric", loss=2.0 / step, step=step, total=4, eta="1m")
        emit(event="progress", current=step)
    emit(event="sample", answer="Anders de Valliere hunts beasts.")
    (out / "adapter").mkdir(exist_ok=True)
    (out / "adapter" / "adapter_config.json").write_text(json.dumps({"r": 16}), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"backend": "hf", "method": job["method"], "steps": 4, "total_steps": 4,
                                                  "final_loss": 0.5, "train_seconds": 1, "peak_vram_mb": 0,
                                                  "adapter_dir": str(out / "adapter"), "model_dir": None,
                                                  "stopped": False, "data": {}, "model": job["model"], "stages": []}), encoding="utf-8")
    emit(event="result", steps=4, final_loss=0.5)
elif job.get("job") == "export":
    (out / "model-q4_k_m.gguf").write_bytes(b"GGUF-FAKE")
    (out / "export.json").write_text(json.dumps({"gguf": "model-q4_k_m.gguf", "ggufs": {"q4_k_m": "model-q4_k_m.gguf"},
                                                 "quant": job.get("quant"), "size_mb": 1}), encoding="utf-8")
'''
MODELS_TOML = '[[models]]\nid = "stub"\nname = "Stub 2B"\nrepo = "acme/mini"\nfamily = "mini"\nparams_b = 0.1\n'


@pytest.fixture()
def forge(tmp_path):
    repo = tmp_path / "fake-repo"
    (repo / "worker" / "ndforge_worker").mkdir(parents=True)
    (repo / "worker" / "ndforge_worker" / "__main__.py").write_text("# stub\n", encoding="utf-8")
    (repo / "catalog").mkdir()
    (repo / "catalog" / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    stub = tmp_path / "stub_worker.py"
    stub.write_text(WORKER_STUB, encoding="utf-8")
    hub = tmp_path / "studio-hub"
    hub.mkdir()
    env = {
        "ND_LORE_FORGE_REPO_DIR": str(repo),
        "ND_LORE_FORGE_ENV_PYTHON": sys.executable,
        "ND_LORE_FORGE_WORKER_ARGS": json.dumps([sys.executable, str(stub)]),
        "ND_STUDIO_HUB_DIR": str(hub),
    }
    proc, url, gm = _python_free_gateway({"gm_email": "gm@example.com"}, extra_env=env) if False else (None, None, None)
    return env, url, gm, hub


def _wait_for(fn, what, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.4)
    raise AssertionError(f"{what} never happened")


def test_lore_forge_install_train_export_use_and_delete(client, seed, tmp_path):
    """One click installs the trainer, a run trains, exports and installs, nd-world switches to the model, one click deletes it all."""
    from .test_gateway_golden import _python_free_gateway
    repo = tmp_path / "fake-repo"
    (repo / "worker" / "ndforge_worker").mkdir(parents=True)
    (repo / "worker" / "ndforge_worker" / "__main__.py").write_text("# stub\n", encoding="utf-8")
    (repo / "catalog").mkdir()
    (repo / "catalog" / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    stub = tmp_path / "stub_worker.py"
    stub.write_text(WORKER_STUB, encoding="utf-8")
    hub = tmp_path / "studio-hub"
    hub.mkdir()
    proc, url, gm = _python_free_gateway(seed, extra_env={
        "ND_LORE_FORGE_REPO_DIR": str(repo),
        "ND_LORE_FORGE_ENV_PYTHON": sys.executable,
        "ND_LORE_FORGE_WORKER_ARGS": json.dumps([sys.executable, str(stub)]),
        "ND_STUDIO_HUB_DIR": str(hub),
    })
    try:
        # GM only: a player's session cannot reach the page or the API
        player = _player_client(seed, url)
        r = player.get("/lore-forge")
        assert r.status_code != 200
        assert player.get("/lore-forge/status").status_code != 200
        assert player.post("/lore-forge/install").status_code != 202

        # not installed yet: training is refused, the page renders anyway (a leftover from an earlier
        # run of this test is cleaned first — the module's state is a folder next to the test database)
        if gm.get("/lore-forge/status").json().get("installed"):
            assert gm.post("/lore-forge/uninstall").status_code == 200
        r = gm.get("/lore-forge/status")
        assert r.status_code == 200 and r.json()["installed"] is False
        assert gm.get("/lore-forge").status_code == 200 and "Lore Forge" in gm.get("/lore-forge").text
        r = gm.post("/lore-forge/train", json={"model": "acme/mini"})
        assert r.status_code == 409, r.text

        # one click: install (copies the prepared repo), then the env marks itself ready
        r = gm.post("/lore-forge/install")
        assert r.status_code == 202, r.text
        # installed is a file check that flips a beat before state.json records the branch — wait for both
        st = _wait_for(lambda: (lambda s: s if s["installed"] and s.get("branch") == "local" else None)(gm.get("/lore-forge/status").json()), "the install")
        assert [m["repo"] for m in st["models"]] == ["acme/mini"]
        r = gm.post("/lore-forge/setup-env")
        assert r.status_code == 202, r.text
        _wait_for(lambda: gm.get("/lore-forge/status").json()["env_ready"], "the env setup")

        # train: the world's text becomes the worker's source rows and the stub run finishes
        r = gm.post("/lore-forge/train", json={"model": "acme/mini", "method": "lora", "preset": "quick"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert run_id
        state = {"st": None}

        def run_done():
            st = gm.get("/lore-forge/status").json()
            state["st"] = st
            run = next((x for x in st["runs"] if x["id"] == run_id), None)
            return run if run and run.get("status") == "done" else None
        run = _wait_for(run_done, "the training run")
        assert run["model"]["repo"] == "acme/mini" and run["text_rows"] > 0
        assert run["summary"]["steps"] == 4
        job = json.loads((Path(os.environ["DB_PATH"]).parent / "lore-forge" / "runs" / run_id / "job.json").read_text(encoding="utf-8"))
        assert job["job"] == "train" and job["method"] == "lora" and job["hyper"]["epochs"] == 2  # quick's preset
        train = (Path(os.environ["DB_PATH"]).parent / "lore-forge" / "runs" / run_id / "train.jsonl").read_text(encoding="utf-8")
        assert '"type": "text"' in train or '"type":"text"' in train

        # unknown model / method / preset are 422, an unknown run id 404
        assert gm.post("/lore-forge/train", json={"model": "nope"}).status_code == 422
        assert gm.post("/lore-forge/train", json={"model": "acme/mini", "method": "nope"}).status_code == 422
        assert gm.post(f"/lore-forge/runs/{run_id}/export", json={"quant": "q9"}).status_code == 422

        # export: the stub writes a GGUF, export.json records it
        assert gm.post(f"/lore-forge/runs/{run_id}/export", json={"quant": "q4_k_m"}).status_code == 202
        _wait_for(lambda: (lambda st: next((x for x in st["runs"] if x["id"] == run_id), None).get("export_status") == "done"
                           if any(x.get("id") == run_id for x in st["runs"]) else None)(gm.get("/lore-forge/status").json()), "the export")
        gguf = Path(os.environ["DB_PATH"]).parent / "lore-forge" / "runs" / run_id / "out" / "model-q4_k_m.gguf"
        assert gguf.read_bytes() == b"GGUF-FAKE"

        # install-model into the (visible) Studio hub dir, then point nd-world's chat at it
        name = "lore-forge-test-model"
        assert gm.post(f"/lore-forge/runs/{run_id}/install-model", json={"name": name, "target": "studio"}).status_code == 202
        _wait_for(lambda: (lambda st: next((x for x in st["runs"] if x["id"] == run_id), None).get("model_name")
                           if any(x.get("id") == run_id for x in st["runs"]) else None)(gm.get("/lore-forge/status").json()), "the model install")
        assert list(hub.glob("lore-forge/*.gguf")) and gguf.read_bytes() == b"GGUF-FAKE"
        assert gm.post(f"/lore-forge/runs/{run_id}/use-model", json={"name": name}).status_code == 200
        assert gm.get("/lore-forge/status").json()["model"] == name

        # a run id from another user's mouth is refused
        assert gm.post("/lore-forge/runs/../../etc/export").status_code in (400, 404)

        # one click out: everything under lore-forge/ goes, the hub copy stays
        assert gm.post("/lore-forge/uninstall").status_code == 200
        assert not (Path(os.environ["DB_PATH"]).parent / "lore-forge").exists()
        assert gm.get("/lore-forge/status").json()["installed"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _player_client(seed, url):
    import httpx
    from .conftest import PLAYER_PASSWORD
    p = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
    assert p.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"}).status_code in (302, 303)
    p.cookies.set("active_world", seed.world_a.slug)
    return p
