"""Image Gen sizes: a generation can be up to 2048 x 2048 and an upscale / Highres-fix target up to 4096 x 4096 (Studio takes sides up to 6272), and the Upscaling panel
says what comes out (and warns over 4096 px on a side)."""
import re

from .conftest import GM_PASSWORD, login


def _options(html, select_id):
    block = re.search(r'<select id="%s"[^>]*>(.*?)</select>' % select_id, html, re.S).group(1)
    return [int(v) for v in re.findall(r'<option value="(\d+)"', block)]


def test_image_gen_offers_2048_generations_and_4096_upscale_targets(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    for select_id in ("ig-width", "ig-height"):
        sizes = _options(r.text, select_id)
        assert max(sizes) == 2048 and {1536, 1792, 2048} <= set(sizes), (select_id, sizes)
    for select_id in ("ig-hires-w", "ig-hires-h"):
        sizes = _options(r.text, select_id)
        assert max(sizes) == 4096 and {2560, 3072, 3584, 4096} <= set(sizes), (select_id, sizes)
    assert "igSetSize(1536,1536,this)" in r.text and "igSetSize(2048,2048,this)" in r.text
    assert 'id="ig-upscale-out"' in r.text
    js = client.get("/static/js/ai-chat-image.js").text
    assert "function igUpdateOut(" in js and "IG_OUT_WARN_SIDE = 4096" in js
