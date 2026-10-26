#!/bin/bash
f=/mnt/f/GLM/nd-world/tests/_data/shard2/pytest.log
sed -n '/def test_pages_are_the_same/,/^____/p' "$f" | sed -n '10,40p'
