"""Write .test_durations (what pytest-split balances the gateway suite's parts by) from the times a gateway run recorded:

    STORE_DURATIONS=1 scripts/gateway_suite.sh 4 all && python scripts/merge_durations.py

Times are rounded to milliseconds - the file only has to be roughly right, and it stays smaller."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
merged = {}
for f in sorted((ROOT / "tests" / "_data").glob("shard*/durations.json")):
    merged.update(json.loads(f.read_text()))
(ROOT / ".test_durations").write_text(json.dumps({k: round(v, 3) for k, v in sorted(merged.items())}, indent=0) + "\n")
print(f"{len(merged)} tests, {round(sum(merged.values()))} s in all")
