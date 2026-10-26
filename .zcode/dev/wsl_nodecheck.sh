#!/bin/bash
# What does node 18 dislike about dice-tray.js?
cd /tmp
cp /mnt/f/GLM/nd-world/static/js/dice-tray.js .
node --check dice-tray.js 2>&1 | head -5
echo "=== import lines with bare specifiers? ==="
grep -n "^import" dice-tray.js | head -8
