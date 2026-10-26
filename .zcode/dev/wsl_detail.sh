#!/bin/bash
f=/mnt/f/GLM/nd-world/tests/_data/shard2/pytest.log
grep -A 20 'def test_pages_are_the_same' "$f" | grep -E 'E |>' | head -10
