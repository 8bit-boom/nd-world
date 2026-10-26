"""Player cockpit part B: allowlist, template flag consumption, JS gating."""
import re

# ── 1. main.py — player-safe entries ─────────────────────────────────────────
p = 'app/main.py'
src = open(p, encoding='utf-8').read()

def rep(old, new, tag):
    global src
    if new in src:
        print("SKIP", tag)
        return
    assert src.count(old) == 1, tag + " count=" + str(src.count(old))
    src = src.replace(old, new, 1)
    print("OK  ", tag)

rep('''    if path == "/api/live":
        # Live-sync change counter (app/live.py). Payload is a bare integer''',
'''    if path == "/cockpit":
        # The cockpit route adapts by role: GMs get the full workspace,
        # players get a player-mode shell whose data comes from player-safe
        # endpoints only (visibility enforced per panel). Workspace saving
        # stays GM-only (those routes are NOT allowlisted here).
        return True
    if path == "/api/cockpit/player-board":
        # Player cockpit's live data: the caller's own parties (vitals) and
        # player-visible quests. Handler enforces visibility per row.
        return True
    if path == "/api/live":
        # Live-sync change counter (app/live.py). Payload is a bare integer''',
    "main: player cockpit allowlist")
open(p, 'w', encoding='utf-8', newline='\n').write(src)

# ── 2. cockpit.html — pass the flag to JS ────────────────────────────────────
p = 'app/templates/cockpit.html'
src = open(p, encoding='utf-8').read()
rep("""const CK_PLAYER_MODE = {{ 'true' if player_mode else 'false' }};""",
    """const CK_PLAYER_MODE = {{ 'true' if player_mode else 'false' }};""",
    "html flag (already present?)")

# GM-only UI hidden in player mode: Find AI button, and the welcome copy
rep('''      <button type="button" class="ck-btn" id="ck-find-btn" title="Find entities & notes connected to a query — or to a focused entity (AI, thinking + RAG)">🔍 Find AI</button>''',
    '''      {% if not player_mode %}<button type="button" class="ck-btn" id="ck-find-btn" title="Find entities & notes connected to a query — or to a focused entity (AI, thinking + RAG)">🔍 Find AI</button>{% endif %}''',
    "html: find button gm-only")

# ── 3. cockpit.js — player-mode gating ───────────────────────────────────────
p = 'static/js/cockpit.js'
src = open(p, encoding='utf-8').read()

# 3a. player-mode panel set + persistence + save suppression, right after COMPACT
rep("""  const COMPACT = window.matchMedia('(max-width: 900px)');""",
"""  const COMPACT = window.matchMedia('(max-width: 900px)');
  // Player mode (the same route, adapted server-side): a tailored panel set
  // and localStorage-only persistence — the workspace API stays GM-only,
  // because the server-side workspace is ONE shared layout per world and a
  // player saving would stomp the GM's arrangement.
  const PLAYER = CK_PLAYER_MODE === true;
  const PLAYER_TYPES = ['map', 'wmap', 'dice', 'party', 'quests', 'entity',
    'ecard', 'notes', 'ai_chat', 'timer', 'calendar', 'gallery', 'audio', 'video'];
  function panelTypes() {
    if (!PLAYER) return CK_TYPES;
    const out = {};
    PLAYER_TYPES.forEach(function (k) { if (CK_TYPES[k]) out[k] = CK_TYPES[k]; });
    return out;
  }""",
    "js: player mode consts")

# 3b. openAdd uses the filtered set
rep("""    addTypes.innerHTML = '';
    Object.keys(CK_TYPES).forEach(function (key) {""",
"""    addTypes.innerHTML = '';
    Object.keys(panelTypes()).forEach(function (key) {""",
    "openAdd filtered types")

# 3c. load(): player mode skips the server fetch entirely
rep("""  async function load() {
    // Instant paint: the localStorage mirror renders immediately, so the""",
"""  async function load() {
    if (PLAYER) {
      // Players: localStorage-only persistence (server workspace is GM-only).
      let mirror = null;
      try { mirror = JSON.parse(localStorage.getItem('nd_cockpit_ws_' + CK_WORLD) || 'null'); } catch (e) {}
      adopt(mirror);
      clampX();
      return;
    }
    // Instant paint: the localStorage mirror renders immediately, so the""",
    "load(): player branch")

# 3d. saveNow(): player mode keeps localStorage, skips the POST
rep("""    fetch('/api/cockpit/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ws),
    }).then(function () { lastSavedAt = Date.now(); }).catch(function () {});""",
"""    if (PLAYER) return;  // server workspace is GM-only
    fetch('/api/cockpit/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ws),
    }).then(function () { lastSavedAt = Date.now(); }).catch(function () {});""",
    "saveNow: player skips POST")

# 3e. live panels: quests data source switches to the player board
rep("""    } else {
      try {
        const r = await fetch('/api/quests/board');
        if (!r.ok) throw new Error();
        renderQuests(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Quest board unavailable.</p>'; }
    }
  }""",
"""    } else if (PLAYER) {
      try {
        const r = await fetch('/api/cockpit/player-board');
        if (!r.ok) throw new Error();
        renderQuests(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Quest board unavailable.</p>'; }
    } else {
      try {
        const r = await fetch('/api/quests/board');
        if (!r.ok) throw new Error();
        renderQuests(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Quest board unavailable.</p>'; }
    }
  }""",
    "loadLive: quests via player board")

# 3f. renderQuests: player board nests quests under {parties, quests}
rep("""  function renderQuests(d, live) {
    if (!(d.quests || []).length) {""",
"""  function renderQuests(d, live) {
    if (d.parties) d = { quests: d.quests || [] };  // player-board payload
    if (!(d.quests || []).length) {""",
    "renderQuests player payload")

# 3g. party vitals in player mode already work: /api/parties/{id}/vitals is
# player-safe with visibility enforced server-side.

open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("PART B DONE")
