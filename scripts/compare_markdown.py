#!/usr/bin/env python3
"""How often does the Rust markdown renderer (rust/nd-render/src/md.rs, pulldown-cmark) give the same HTML as the app's (markdown2)?
Feeds every markdown file of the repository, cut into its blocks, to both and prints the share that renders the same (HTML compared
after normalising whitespace and attribute order) with a few differences.   python scripts/compare_markdown.py [--show 10]"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", "/tmp/nd-md-compare.db")
BIN = next(p for p in (ROOT / "rust/target/release/nd-render-check", ROOT / "rust/target/debug/nd-render-check") if p.is_file())


def corpus():
    texts = []
    for p in sorted(list(ROOT.glob("docs/*.md")) + list(ROOT.glob("app/**/*.md")) + list(ROOT.glob("lore/**/*.md")) + list(ROOT.glob("*.md"))):
        t = p.read_text(errors="ignore")
        texts.append(t)
        texts += [b for b in re.split(r"\n(?=#)", t) if b.strip()]
    return texts


def main():
    from app.rendering import _render_md
    spec = importlib.util.spec_from_file_location("golden", ROOT / "scripts" / "golden.py")
    golden = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(golden)
    texts = corpus()
    out = subprocess.run([str(BIN)], input="".join(json.dumps({"op": "md", "text": t}) + "\n" for t in texts), capture_output=True, text=True, check=True).stdout.splitlines()
    same, diffs = 0, []
    for t, r in zip(texts, out):
        a, b = golden.normalise("text/html", _render_md(t).encode()), golden.normalise("text/html", json.loads(r).encode())
        if a == b:
            same += 1
        else:
            diffs.append((t, a, b))
    print(f"{same} of {len(texts)} markdown texts render the same ({same * 100 // max(1, len(texts))} %)")
    show = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 3
    import difflib
    for t, a, b in diffs[:show]:
        print("-" * 70)
        print("\n".join(list(difflib.unified_diff(a.splitlines(), b.splitlines(), "markdown2", "rust", n=0, lineterm=""))[:16]))


main()
