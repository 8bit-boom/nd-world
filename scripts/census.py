"""Who answers what: fold the gateway's forward logs (ND_FORWARD_LOG, one "GW|PY METHOD path" line per request; scripts/gateway_suite.sh
writes tests/_data/shard<N>/forwarded.log) into the two lists docs/RUST_SWITCH_OFF.md works from.

    python3 scripts/census.py tests/_data/shard*/forwarded.log            # print the summary
    python3 scripts/census.py --write tests/_data/shard*/forwarded.log    # also rewrite docs/census/python_only.txt and mixed.txt

Ids are folded to {}, /static/ and /uploads/ paths to *; the test-only reset hook is left out."""
import re
import sys
from collections import Counter
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs" / "census"


def fold(path: str) -> str:
    path = path.split("?", 1)[0]
    for prefix in ("/static/", "/uploads/"):
        if path.startswith(prefix):
            return prefix + "*"
    return re.sub(r"/\d+(?=/|$|\.)", "/{}", path)


def main(argv):
    write = "--write" in argv
    files = [a for a in argv if a != "--write"]
    gw, py = Counter(), Counter()
    for f in files:
        for line in Path(f).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) != 3 or parts[0] not in ("GW", "PY") or parts[2].startswith("/__test__/"):
                continue
            key = f"{parts[1]} {fold(parts[2])}"
            (gw if parts[0] == "GW" else py)[key] += 1
    only_py = sorted(((n, k) for k, n in py.items() if k not in gw), key=lambda x: (-x[0], x[1]))
    mixed = sorted(((n, gw[k], k) for k, n in py.items() if k in gw), key=lambda x: (-x[0], x[2]))
    only_gw = [k for k in gw if k not in py]
    print(f"gateway only: {len(only_gw)} routes; mixed: {len(mixed)} routes, {sum(m[0] for m in mixed)} requests to Python; "
          f"Python only: {len(only_py)} routes, {sum(n for n, _ in only_py)} requests")
    if write:
        (DOCS / "python_only.txt").write_text("".join(f"{n:6d} {k}\n" for n, k in only_py), encoding="utf-8")
        (DOCS / "mixed.txt").write_text("".join(f"{f'{n}/{g}':<10} {k}\n" for n, g, k in mixed), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
