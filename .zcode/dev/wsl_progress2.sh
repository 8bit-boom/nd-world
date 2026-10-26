#!/bin/bash
# Progress with timestamps: is this run writing the logs, or are these stale?
for n in 1 2 3 4; do
  f="/mnt/f/GLM/nd-world/tests/_data/shard$n/pytest.log"
  printf "shard%s: %s | " "$n" "$(date -r "$f" +%H:%M:%S 2>/dev/null || echo missing)"
  tail -c 200 "$f" 2>/dev/null | tr -d '\r' | grep -oE '[0-9]+%\]' | tail -1
done
date
