#!/bin/bash
# Fresh minified tree, then node20 --check on the renamed and unrenamed files.
set -e
export PATH=/root/node20/bin:$PATH
cd /mnt/f/GLM/nd-world
/root/venv/bin/python - <<'PYEOF'
import sys, shutil, tempfile, subprocess
sys.path.insert(0, '/mnt/f/GLM/nd-world')
from pathlib import Path
from app import precompress
tmp = Path(tempfile.mkdtemp())
shutil.copytree('static/js', tmp / 'js')
shutil.copy('static/style.css', tmp / 'style.css')
precompress.minify_tree(tmp)
bad = []
for f in sorted((tmp / 'js').rglob('*.js')):
    text = f.read_text(encoding='utf-8')
    target = f.with_suffix('.mjs') if ('import ' in text and ' from ' in text) or text.lstrip().startswith('export ') else f
    if target != f:
        f.rename(target)
    r = subprocess.run(['node', '--check', str(target)], capture_output=True, text=True)
    if r.returncode:
        bad.append((f.name, r.stderr[:100]))
print('bad files:', bad[:3])
print('total bad:', len(bad))
PYEOF
