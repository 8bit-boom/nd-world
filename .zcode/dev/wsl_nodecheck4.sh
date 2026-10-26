#!/bin/bash
# node20 --check on the minified dice-tray.js: still ESM-in-.js?
cd /tmp/minjs 2>/dev/null || exit 1
node --check dice-tray.js 2>&1 | head -4
echo "exit: $?"
