#!/bin/bash
for n in 1 2 3 4; do
  printf "shard%s: " "$n"
  tail -c 200 "/mnt/f/GLM/nd-world/tests/_data/shard$n/pytest.log" 2>/dev/null | tr -d '\r' | grep -oE '[0-9]+%\]' | tail -1
done
