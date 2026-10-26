#!/bin/bash
# Print the scanned upload's parts for the world-import repro (a temp eprintln in scan).
cd /mnt/f/GLM/nd-world
grep -n "pub fn scan" rust/nd-gateway/src/uploads.rs | head -1
grep -n "files.push\|FileRef" rust/nd-gateway/src/uploads.rs | head -6
