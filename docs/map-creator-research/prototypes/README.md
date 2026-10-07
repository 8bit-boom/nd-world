# Map creator prototypes

Research code for [`docs/MAP_CREATOR_RESEARCH.md`](../../MAP_CREATOR_RESEARCH.md). Plain JavaScript modules (the `ndDiceGeometry` pattern: they load in a browser and under Node) with Node's built-in test runner.
**Not wired into the application; nothing in `tests/` or CI depends on it.**

```bash
node --test "docs/map-creator-research/prototypes/*.test.js"      # 64 tests (Node 22; quote the glob)
```

Read [`../PROTOTYPES.md`](../PROTOTYPES.md) for what each module does, the measurements and the limits. The scripts that need a browser or the app (`fog_spike_harness.py`, `export_image_spike.py`,
`rasterize.py`) use the Chromium that ships with the research sandbox (`/opt/pw-browsers/chromium`) and boot the app on a scratch database; `wall_detect_experiment.py` needs numpy, scipy and Pillow.
`fixtures/` holds a stripped copy of a real Dungeondraft export under its original BSD-3-Clause notice.
