#!/bin/bash
# After minification: does dice-tray.js still contain " from "? Show its head.
set -e
cd /tmp && rm -rf minjs && mkdir minjs && cd minjs
cp /mnt/f/GLM/nd-world/static/js/dice-tray.js .
cd /mnt/f/GLM/nd-world && /root/venv/bin/python - <<'PYEOF'
from pathlib import Path
from app import precompress
import sys
sys.path.insert(0, '/mnt/f/GLM/nd-world')
# minify just this one file via the same code path: emulate by minify_tree on a tree with only it
import shutil, tempfile
tmp = Path(tempfile.mkdtemp())
(tmp / 'js').mkdir()
shutil.copy('/mnt/f/GLM/nd-world/static/js/dice-tray.js', tmp / 'js' / 'dice-tray.js')
shutil.copy('/mnt/f/GLM/nd-world/static/style.css', tmp / 'style.css')
precompress.minify_tree(tmp)
out = (tmp / 'js' / 'dice-tray.js').read_text()
print('len:', len(out))
print('has " from ":', ' from ' in out)
print('starts:', out[:80].replace('\n', '\\n'))
print('import lines:', [l for l in out.splitlines() if 'import' in l][:3])
PYEOF
