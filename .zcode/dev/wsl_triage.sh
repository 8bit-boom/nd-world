#!/bin/bash
for sh in 1 2 3 4; do
  f="/mnt/f/GLM/nd-world/tests/_data/shard$sh/pytest.log"
  [ -f "$f" ] || continue
  echo "=== shard$sh ==="
  grep -E '^FAILED' "$f" | head -5
done
echo "=== character_new detail (whichever shard) ==="
for sh in 1 2 3 4; do
  f="/mnt/f/GLM/nd-world/tests/_data/shard$sh/pytest.log"
  [ -f "$f" ] || continue
  if grep -q 'test_character_new_and_edit_forms' "$f"; then
    grep -A 12 'def test_character_new_and_edit_forms' "$f" | grep -E 'E |assert' | head -6
  fi
done
