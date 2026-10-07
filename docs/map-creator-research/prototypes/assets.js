// A first-party prop set drawn in code: top-down, original, no image files, no licences to track. Each prop is a few SVG
// shapes in a local box of 100 units per grid cell, drawn "facing south" (its back is the north edge), so a prop against a wall
// is rotated until its back meets the wall. `sil` is the silhouette used for the drop shadow.
//   ASSETS[id] = { name, w, h (cells), kind: 'furniture' | 'decor' | 'light' | 'nature', blocks: bool, sil, body }
// This is a PROOF that a small, consistent library can be made in-repo (and extended by the AI image tools or by the GM's own
// uploads); it is not finished art. Adding a prop = adding an entry here.
(function (root) {
  'use strict';
  var OUT = '#2b1c10', WOOD = '#9a6a38', WOOD_L = '#b4824a', WOOD_D = '#6f4a26', STONE = '#8d9199', STONE_D = '#5f636b', CREAM = '#e8dfc8';
  function line(x1, y1, x2, y2, c, w) { return '<path d="M' + x1 + ' ' + y1 + 'L' + x2 + ' ' + y2 + '" stroke="' + c + '" stroke-width="' + (w || 3) + '" stroke-linecap="round"/>'; }

  function books(x, y, w, h) {                    // a row of book spines, colours cycle deterministically
    var cols = ['#7a2e2e', '#2e5a7a', '#3e6b3e', '#8a6a2a', '#5a3a7a', '#7a4a2e'], out = '', cx = x, i = 0;
    while (cx < x + w - 4) {
      var bw = 5 + (i * 7) % 6, bh = h - 4 - (i * 5) % 7;
      if (cx + bw > x + w) bw = x + w - cx;
      out += '<rect x="' + cx + '" y="' + (y + 2 + (h - 4 - bh)) + '" width="' + bw + '" height="' + bh + '" fill="' + cols[i % cols.length] + '" stroke="' + OUT + '" stroke-width="1"/>';
      cx += bw + 1; i++;
    }
    return out;
  }

  var ASSETS = {
    table_round: { name: 'Round table', w: 1.5, h: 1.5, kind: 'furniture', blocks: true, sil: '<circle cx="75" cy="75" r="70"/>',
      body: '<circle cx="75" cy="75" r="70" fill="' + WOOD + '" stroke="' + OUT + '" stroke-width="4"/><circle cx="75" cy="75" r="56" fill="none" stroke="' + WOOD_D + '" stroke-width="3"/>' + line(75, 20, 75, 130, '#80562c', 2) + line(20, 75, 130, 75, '#80562c', 2) },
    table_long: { name: 'Long table', w: 3, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="4" y="10" width="292" height="80" rx="6"/>',
      body: '<rect x="4" y="10" width="292" height="80" rx="6" fill="' + WOOD + '" stroke="' + OUT + '" stroke-width="4"/>' + line(10, 36, 290, 36, WOOD_D, 2) + line(10, 64, 290, 64, WOOD_D, 2) + line(100, 14, 100, 86, WOOD_D, 1.5) + line(205, 14, 205, 86, WOOD_D, 1.5) },
    table_small: { name: 'Small table', w: 1, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="8" y="8" width="84" height="84" rx="6"/>',
      body: '<rect x="8" y="8" width="84" height="84" rx="6" fill="' + WOOD + '" stroke="' + OUT + '" stroke-width="4"/>' + line(8, 50, 92, 50, WOOD_D, 2) + line(50, 8, 50, 92, WOOD_D, 1.5) },
    chair: { name: 'Chair', w: 0.5, h: 0.5, kind: 'furniture', blocks: true, sil: '<rect x="6" y="4" width="38" height="40" rx="6"/>',
      body: '<rect x="7" y="12" width="36" height="31" rx="6" fill="' + WOOD_L + '" stroke="' + OUT + '" stroke-width="3"/><rect x="6" y="4" width="38" height="10" rx="4" fill="' + WOOD_D + '" stroke="' + OUT + '" stroke-width="3"/>' },
    stool: { name: 'Stool', w: 0.5, h: 0.5, kind: 'furniture', blocks: true, sil: '<circle cx="25" cy="25" r="20"/>',
      body: '<circle cx="25" cy="25" r="20" fill="' + WOOD_L + '" stroke="' + OUT + '" stroke-width="3"/><circle cx="25" cy="25" r="11" fill="none" stroke="' + WOOD_D + '" stroke-width="2"/>' },
    bed_single: { name: 'Bed', w: 1, h: 2, kind: 'furniture', blocks: true, sil: '<rect x="4" y="4" width="92" height="192" rx="8"/>',
      body: '<rect x="4" y="4" width="92" height="192" rx="8" fill="' + WOOD_D + '" stroke="' + OUT + '" stroke-width="4"/><rect x="11" y="11" width="78" height="178" rx="5" fill="' + CREAM + '"/>' +
        '<rect x="22" y="16" width="56" height="34" rx="12" fill="#fbf7ec" stroke="#a89f88" stroke-width="2"/><rect x="11" y="72" width="78" height="117" rx="5" fill="#6b3b3b" stroke="' + OUT + '" stroke-width="2"/>' + line(11, 86, 89, 86, '#8a5252', 3) + line(50, 90, 50, 186, '#8a5252', 2) },
    chest: { name: 'Chest', w: 1, h: 0.6, kind: 'furniture', blocks: true, sil: '<rect x="6" y="6" width="88" height="48" rx="6"/>',
      body: '<rect x="6" y="6" width="88" height="48" rx="6" fill="' + WOOD + '" stroke="' + OUT + '" stroke-width="3.5"/>' + line(8, 26, 92, 26, WOOD_D, 2.5) + '<rect x="22" y="6" width="8" height="48" fill="#6b7078" stroke="' + OUT + '" stroke-width="1.5"/><rect x="70" y="6" width="8" height="48" fill="#6b7078" stroke="' + OUT + '" stroke-width="1.5"/><circle cx="50" cy="28" r="5" fill="#d8b43a" stroke="' + OUT + '" stroke-width="1.5"/>' },
    crate: { name: 'Crate', w: 1, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="8" y="8" width="84" height="84"/>',
      body: '<rect x="8" y="8" width="84" height="84" fill="' + WOOD_L + '" stroke="' + OUT + '" stroke-width="4"/><rect x="18" y="18" width="64" height="64" fill="none" stroke="' + WOOD_D + '" stroke-width="3"/>' + line(18, 18, 82, 82, WOOD_D, 3) + line(82, 18, 18, 82, WOOD_D, 3) },
    barrel: { name: 'Barrel', w: 0.8, h: 0.8, kind: 'furniture', blocks: true, sil: '<circle cx="40" cy="40" r="36"/>',
      body: '<circle cx="40" cy="40" r="36" fill="' + WOOD + '" stroke="' + OUT + '" stroke-width="3.5"/><circle cx="40" cy="40" r="27" fill="none" stroke="#4a4f56" stroke-width="3"/><circle cx="40" cy="40" r="17" fill="' + WOOD_L + '" stroke="' + WOOD_D + '" stroke-width="2"/><circle cx="40" cy="40" r="3" fill="' + WOOD_D + '"/>' },
    sack: { name: 'Sack', w: 0.6, h: 0.6, kind: 'furniture', blocks: true, sil: '<path d="M30 6 C48 4 56 18 54 34 C52 52 36 56 22 52 C8 48 4 30 12 18 C16 10 22 7 30 6Z"/>',
      body: '<path d="M30 6 C48 4 56 18 54 34 C52 52 36 56 22 52 C8 48 4 30 12 18 C16 10 22 7 30 6Z" fill="#c9b27a" stroke="' + OUT + '" stroke-width="3"/>' + line(26, 12, 36, 12, '#7a6a3a', 3) + line(31, 14, 31, 40, '#a8934f', 2) },
    hay: { name: 'Hay', w: 1, h: 1, kind: 'furniture', blocks: false, sil: '<rect x="10" y="10" width="80" height="80" rx="10"/>',
      body: '<rect x="10" y="10" width="80" height="80" rx="10" fill="#d8bf6a" stroke="#8a7330" stroke-width="3"/>' + line(20, 25, 70, 20, '#a88f3a', 2) + line(24, 45, 80, 40, '#a88f3a', 2) + line(18, 62, 74, 66, '#a88f3a', 2) + line(30, 80, 82, 76, '#a88f3a', 2) },
    bookshelf: { name: 'Bookshelf', w: 2, h: 0.5, kind: 'furniture', blocks: true, sil: '<rect x="4" y="4" width="192" height="42"/>',
      body: '<rect x="4" y="4" width="192" height="42" fill="' + WOOD_D + '" stroke="' + OUT + '" stroke-width="3.5"/>' + books(9, 8, 182, 15) + books(9, 26, 182, 15) },
    pillar: { name: 'Pillar', w: 1, h: 1, kind: 'furniture', blocks: true, sil: '<circle cx="50" cy="50" r="38"/>',
      body: '<circle cx="50" cy="50" r="38" fill="' + STONE + '" stroke="' + OUT + '" stroke-width="4"/><circle cx="50" cy="50" r="28" fill="#a3a7ae" stroke="' + STONE_D + '" stroke-width="2"/><path d="M32 40 A22 22 0 0 1 56 28" stroke="#d8dbe0" stroke-width="3" fill="none" stroke-linecap="round"/>' },
    statue: { name: 'Statue', w: 1, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="14" y="14" width="72" height="72" rx="4"/>',
      body: '<rect x="14" y="14" width="72" height="72" rx="4" fill="' + STONE_D + '" stroke="' + OUT + '" stroke-width="4"/><ellipse cx="50" cy="58" rx="24" ry="14" fill="#b9bdc4" stroke="' + OUT + '" stroke-width="2.5"/><circle cx="50" cy="42" r="12" fill="#cfd2d8" stroke="' + OUT + '" stroke-width="2.5"/>' + line(74, 30, 74, 76, '#e1e3e8', 3.5) },
    rug: { name: 'Rug', w: 3, h: 2, kind: 'decor', blocks: false, sil: '',
      body: '<rect x="4" y="4" width="292" height="192" rx="10" fill="#7a2323" stroke="#d8b43a" stroke-width="5"/><rect x="22" y="22" width="256" height="156" rx="6" fill="none" stroke="#d8b43a" stroke-width="2.5"/><path d="M150 40 L250 100 L150 160 L50 100Z" fill="#9a3a3a" stroke="#d8b43a" stroke-width="3"/><circle cx="150" cy="100" r="16" fill="#d8b43a"/>' },
    fireplace: { name: 'Fireplace', w: 2, h: 0.8, kind: 'furniture', blocks: true, sil: '<rect x="4" y="4" width="192" height="72"/>',
      body: '<rect x="4" y="4" width="192" height="72" fill="' + STONE + '" stroke="' + OUT + '" stroke-width="4"/><rect x="36" y="14" width="128" height="52" fill="#1b1613" stroke="' + OUT + '" stroke-width="2"/><path d="M60 62 C66 36 78 44 82 24 C92 40 100 34 100 18 C112 38 122 36 122 26 C132 40 142 46 140 62Z" fill="#e8742a"/><path d="M80 62 C84 46 92 50 96 38 C104 50 112 52 112 62Z" fill="#f6d04a"/>' },
    altar: { name: 'Altar', w: 2, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="6" y="14" width="188" height="72" rx="6"/>',
      body: '<rect x="6" y="14" width="188" height="72" rx="6" fill="' + STONE + '" stroke="' + OUT + '" stroke-width="4"/><rect x="30" y="14" width="140" height="72" fill="' + CREAM + '" stroke="#d8b43a" stroke-width="3"/><circle cx="16" cy="50" r="6" fill="#f6d04a" stroke="' + OUT + '" stroke-width="1.5"/><circle cx="184" cy="50" r="6" fill="#f6d04a" stroke="' + OUT + '" stroke-width="1.5"/><path d="M100 28 L112 50 L100 72 L88 50Z" fill="#7a2323" stroke="#d8b43a" stroke-width="2"/>' },
    brazier: { name: 'Brazier', w: 0.6, h: 0.6, kind: 'light', blocks: true, sil: '<circle cx="30" cy="30" r="25"/>',
      body: '<circle cx="30" cy="30" r="25" fill="#3b3f46" stroke="' + OUT + '" stroke-width="3"/><circle cx="30" cy="30" r="17" fill="#5a2a1a"/><path d="M20 38 C20 26 28 28 30 14 C36 24 42 24 40 38Z" fill="#e8742a"/><path d="M26 38 C27 30 31 32 32 24 C36 30 37 33 36 38Z" fill="#f6d04a"/>' },
    weapon_rack: { name: 'Weapon rack', w: 2, h: 0.5, kind: 'furniture', blocks: true, sil: '<rect x="4" y="8" width="192" height="32"/>',
      body: '<rect x="4" y="8" width="192" height="32" fill="' + WOOD_D + '" stroke="' + OUT + '" stroke-width="3.5"/>' + line(20, 12, 20, 38, '#c9ccd2', 3) + line(44, 12, 44, 38, '#c9ccd2', 3) + line(68, 12, 68, 38, '#c9ccd2', 3) + '<circle cx="110" cy="24" r="13" fill="#7a2323" stroke="' + OUT + '" stroke-width="2.5"/><circle cx="110" cy="24" r="4" fill="#c9ccd2"/><circle cx="150" cy="24" r="13" fill="#2e5a7a" stroke="' + OUT + '" stroke-width="2.5"/><circle cx="150" cy="24" r="4" fill="#c9ccd2"/>' + line(180, 12, 180, 38, '#c9ccd2', 3) },
    cauldron: { name: 'Cauldron', w: 0.9, h: 0.9, kind: 'furniture', blocks: true, sil: '<circle cx="45" cy="45" r="40"/>',
      body: '<circle cx="45" cy="45" r="40" fill="#2a2d33" stroke="' + OUT + '" stroke-width="3.5"/><circle cx="45" cy="45" r="31" fill="#3d7a3f" stroke="#1b1613" stroke-width="2.5"/><circle cx="36" cy="40" r="5" fill="#7fc47f"/><circle cx="52" cy="50" r="4" fill="#7fc47f"/><circle cx="48" cy="34" r="3" fill="#a8e0a8"/>' },
    well: { name: 'Well', w: 1.6, h: 1.6, kind: 'furniture', blocks: true, sil: '<circle cx="80" cy="80" r="76"/>',
      body: '<circle cx="80" cy="80" r="76" fill="' + STONE + '" stroke="' + OUT + '" stroke-width="4"/><circle cx="80" cy="80" r="56" fill="' + STONE_D + '" stroke="' + OUT + '" stroke-width="3"/><circle cx="80" cy="80" r="48" fill="#2f5f8a"/><path d="M52 70 A30 30 0 0 1 96 54" stroke="#7fb4de" stroke-width="3" fill="none" stroke-linecap="round"/>' },
    stairs_down: { name: 'Stairs down', w: 1, h: 2, kind: 'decor', blocks: false, sil: '',
      body: '<rect x="4" y="4" width="92" height="192" fill="#6e7178" stroke="' + OUT + '" stroke-width="4"/>' + [0, 1, 2, 3, 4, 5, 6, 7].map(function (i) { return '<rect x="4" y="' + (4 + i * 24) + '" width="92" height="24" fill="#000" opacity="' + (0.04 + i * 0.1).toFixed(2) + '"/>' + line(4, 4 + i * 24, 96, 4 + i * 24, OUT, 2.5); }).join('') },
    torch_wall: { name: 'Wall torch', w: 0.3, h: 0.3, kind: 'light', blocks: false, sil: '',
      body: '<circle cx="15" cy="15" r="11" fill="#e8742a" stroke="' + OUT + '" stroke-width="2.5"/><circle cx="15" cy="15" r="6" fill="#f6d04a"/>' },
    bar_counter: { name: 'Bar counter', w: 4, h: 1, kind: 'furniture', blocks: true, sil: '<rect x="4" y="8" width="392" height="84" rx="6"/>',
      body: '<rect x="4" y="8" width="392" height="84" rx="6" fill="' + WOOD_D + '" stroke="' + OUT + '" stroke-width="4"/><rect x="12" y="16" width="376" height="56" rx="4" fill="' + WOOD_L + '"/>' + line(12, 44, 388, 44, WOOD, 2) + '<circle cx="80" cy="40" r="9" fill="#c9ccd2" stroke="' + OUT + '" stroke-width="2"/><circle cx="170" cy="44" r="9" fill="#c9ccd2" stroke="' + OUT + '" stroke-width="2"/><rect x="262" y="30" width="16" height="22" rx="3" fill="#3e6b3e" stroke="' + OUT + '" stroke-width="2"/><rect x="300" y="30" width="16" height="22" rx="3" fill="#7a2e2e" stroke="' + OUT + '" stroke-width="2"/>' },
    rock: { name: 'Rock', w: 1, h: 1, kind: 'nature', blocks: true, sil: '<path d="M22 70 L12 44 L28 18 L58 10 L84 28 L88 58 L68 84 L38 86Z"/>',
      body: '<path d="M22 70 L12 44 L28 18 L58 10 L84 28 L88 58 L68 84 L38 86Z" fill="#7d8188" stroke="' + OUT + '" stroke-width="3.5"/><path d="M28 18 L58 10 L84 28 L56 40 L30 38Z" fill="#a3a7ae"/><path d="M56 40 L84 28 L88 58 L68 84 L52 62Z" fill="#666a71"/>' },
  };

  function svgOf(id, opts) {
    var a = ASSETS[id]; if (!a) return '';
    opts = opts || {};
    var shadow = a.sil && opts.shadow !== false ? '<g transform="translate(5 7)" fill="#000" opacity="0.30">' + a.sil + '</g>' : '';
    return shadow + a.body;
  }

  root.ndMapAssets = { ASSETS: ASSETS, svgOf: svgOf, ids: Object.keys(ASSETS) };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapAssets;
})(typeof window !== 'undefined' ? window : globalThis);
