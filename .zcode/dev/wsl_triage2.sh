#!/bin/bash
f=/mnt/f/GLM/nd-world/tests/_data/shard4/pytest.log
echo "=== world_summary_get_gm ==="
sed -n '/def test_world_summary_get_gm/,/^____/p' "$f" | sed -n '5,25p'
echo "=== minifier ==="
sed -n '/def test_the_image_builds_minifier/,/^____/p' "$f" | sed -n '5,25p'
