#!/bin/bash
# Reproduce the minifier check on a fresh copy, show the failing file's first import-ish line.
set -e
cd /tmp && rm -rf minjs && mkdir minjs
cd /mnt/f/GLM/nd-world
/root/venv/bin/python - <<'PYEOF'
import sys, shutil, tempfile
sys.path.insert(0, '/mnt/f/GLM/nd-world')
from pathlib import Path
from app import precompress
tmp = Path(tempfile.mkdtemp())
shutil.copytree('static/js', tmp / 'js')
shutil.copy('static/style.css', tmp / 'style.css')
st = precompress.minify_tree(tmp)
print(st)
import subprocess
bad = []
for f in sorted((tmp / 'js').rglob('*.js')):
    text = f.read_text(encoding='utf-8')
    target = f.with_suffix('.mjs') if ('import ' in text and ' from ' in text) or text.lstrip().startswith('export ') else f
    if target != f:
        f.rename(target)
    r = subprocess.run(['node', '--check', str(target)], capture_output=True, text=True)
    if r.returncode:
        bad.append((f.name, r.stderr[:150]))
print('bad:', bad[:2])
PYEOF
