"use strict";
// Shared SVG-rendering code for battle-map schematics — used by both the GM
// editor (schematic.html) and the read-only/limited-interactive player view
// (schematic_view.html). Only pure rendering/geometry lives here; editing,
// tool, and dialog logic stays local to schematic.html.
//
// Callers must declare these globals before invoking anything below:
//   gridType, gridConfig, CW, CH, battleGridLayer, layerState
// and may optionally set `activeCombatantId` (string|null) to ring-highlight
// the active-turn token.

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// ── Battle grid (hex/square) ──────────────────────────────────────────────────
// Hex math follows the standard axial-coordinate reference (Red Blob Games):
// https://www.redblobgames.com/grids/hexagons/ — "size" below is the
// center-to-vertex radius, same value the GM enters as "Cell size".
function hexRound(q, r) {
  let x=q, z=r, y=-x-z;
  let rx=Math.round(x), ry=Math.round(y), rz=Math.round(z);
  const dx=Math.abs(rx-x), dy=Math.abs(ry-y), dz=Math.abs(rz-z);
  if (dx>dy && dx>dz) rx=-ry-rz; else if (dy>dz) ry=-rx-rz; else rz=-rx-ry;
  return { q:rx, r:rz };
}
function hexToPixel(q, r, size, orientation) {
  if (orientation === 'flat') {
    return { x: size*(3/2*q), y: size*(Math.sqrt(3)/2*q + Math.sqrt(3)*r) };
  }
  return { x: size*(Math.sqrt(3)*q + Math.sqrt(3)/2*r), y: size*(3/2*r) };
}
function pixelToHex(x, y, size, orientation) {
  if (orientation === 'flat') {
    return hexRound((2/3*x)/size, (-1/3*x + Math.sqrt(3)/3*y)/size);
  }
  return hexRound((Math.sqrt(3)/3*x - 1/3*y)/size, (2/3*y)/size);
}
function snapToGrid(x, y) {
  const cs = gridConfig.cell_size || 50;
  const ox = gridConfig.offset_x || 0, oy = gridConfig.offset_y || 0;
  if (gridType === 'square') {
    return {
      x: Math.round((x-ox)/cs - 0.5)*cs + ox + cs/2,
      y: Math.round((y-oy)/cs - 0.5)*cs + oy + cs/2,
    };
  }
  if (gridType === 'hex') {
    const orientation = gridConfig.orientation === 'flat' ? 'flat' : 'pointy';
    const {q, r} = pixelToHex(x-ox, y-oy, cs, orientation);
    const p = hexToPixel(q, r, cs, orientation);
    return { x: p.x+ox, y: p.y+oy };
  }
  return { x, y };
}
function renderBattleGrid() {
  battleGridLayer.innerHTML = '';
  if (gridType === 'none') return;
  const cs = Math.max(5, gridConfig.cell_size || 50);
  const ox = gridConfig.offset_x || 0, oy = gridConfig.offset_y || 0;
  const stroke = 'rgba(0,240,255,0.4)';
  if (gridType === 'square') {
    let startX = ox % cs; if (startX < 0) startX += cs;
    let startY = oy % cs; if (startY < 0) startY += cs;
    for (let x = startX; x <= CW; x += cs) battleGridLayer.appendChild(svgEl('line', {x1:x,y1:0,x2:x,y2:CH,stroke,'stroke-width':1}));
    for (let y = startY; y <= CH; y += cs) battleGridLayer.appendChild(svgEl('line', {x1:0,y1:y,x2:CW,y2:y,stroke,'stroke-width':1}));
    return;
  }
  // hex
  const orientation = gridConfig.orientation === 'flat' ? 'flat' : 'pointy';
  const w = orientation==='flat' ? cs*2 : Math.sqrt(3)*cs;
  const h = orientation==='flat' ? Math.sqrt(3)*cs : cs*2;
  const hexPts = (cx,cy) => {
    const pts = [];
    for (let i=0;i<6;i++) {
      const angle = Math.PI/180 * (60*i + (orientation==='flat'?0:30));
      pts.push([cx+cs*Math.cos(angle), cy+cs*Math.sin(angle)]);
    }
    return pts.map(p=>p.join(',')).join(' ');
  };
  const qLo=-2, qHi=Math.ceil(CW/w)+2, rLo=-2, rHi=Math.ceil(CH/h)+2;
  for (let r=rLo; r<=rHi; r++) {
    for (let q=qLo; q<=qHi; q++) {
      const p = hexToPixel(q, r, cs, orientation);
      const cx = p.x+ox, cy = p.y+oy;
      if (cx < -w || cx > CW+w || cy < -h || cy > CH+h) continue;
      battleGridLayer.appendChild(svgEl('polygon', {points:hexPts(cx,cy), fill:'none', stroke, 'stroke-width':1}));
    }
  }
}

// Convert a pixel distance to grid units, or null if no grid is configured.
function pxToUnits(px) {
  if (gridType === 'none' || !gridConfig.cell_size) return null;
  const cells = px / gridConfig.cell_size;
  const perCell = gridConfig.unit_per_cell || 1;
  const label = gridConfig.unit_label || 'units';
  return { cells, units: cells * perCell, label };
}

// ── Element bounds ───────────────────────────────────────────────────────────
function getBounds(el) {
  switch(el.type) {
    case 'rect':   return {x:el.x, y:el.y, w:el.w, h:el.h};
    case 'circle': return {x:el.cx-el.rx, y:el.cy-el.ry, w:2*el.rx, h:2*el.ry};
    case 'line': case 'arrow': case 'measure': {
      const mx=Math.min(el.x1,el.x2), my=Math.min(el.y1,el.y2);
      return {x:mx, y:my, w:Math.abs(el.x2-el.x1)||2, h:Math.abs(el.y2-el.y1)||2};
    }
    case 'aoe': {
      const mx=Math.min(el.x1,el.x2), my=Math.min(el.y1,el.y2);
      const dist = Math.hypot(el.x2-el.x1, el.y2-el.y1);
      const pad = el.shape === 'circle' ? dist : dist * 0.5;
      return {x:mx-pad, y:my-pad, w:Math.abs(el.x2-el.x1)+pad*2||2, h:Math.abs(el.y2-el.y1)+pad*2||2};
    }
    case 'poly': case 'path': {
      const pts = el.points || el.pts;
      const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
      const mx=Math.min(...xs), my=Math.min(...ys);
      return {x:mx, y:my, w:Math.max(...xs)-mx, h:Math.max(...ys)-my};
    }
    case 'text':  return {x:el.x, y:el.y-(el.size||20), w:180, h:(el.size||20)+4};
    case 'pin':   return {x:el.x-12, y:el.y-22, w:130, h:34};
    case 'image': return {x:el.x, y:el.y, w:el.w, h:el.h};
    case 'token': { const r=el.r||20; return {x:el.x-r, y:el.y-r, w:r*2, h:r*2}; }
  }
  return null;
}
function getBoundsMulti(els) {
  if (!els.length) return null;
  let minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity;
  els.forEach(el => {
    const b = getBounds(el); if (!b) return;
    minX=Math.min(minX,b.x); minY=Math.min(minY,b.y);
    maxX=Math.max(maxX,b.x+b.w); maxY=Math.max(maxY,b.y+b.h);
  });
  return { x:minX, y:minY, w:maxX-minX, h:maxY-minY };
}

function pointsToPath(pts) {
  if (!pts.length) return '';
  let d = `M${pts[0][0]} ${pts[0][1]}`;
  for (let i=1; i<pts.length; i++) d += ` L${pts[i][0]} ${pts[i][1]}`;
  return d;
}

// ── Waypoint helper ───────────────────────────────────────────────────────────
function elPoints(el) {
  if (el.points && Array.isArray(el.points) && el.points.length >= 2
      && typeof el.points[0] === 'object' && !Array.isArray(el.points[0])) {
    return el.points;
  }
  return [{x:el.x1, y:el.y1}, {x:el.x2, y:el.y2}];
}

// AOE shape geometry shared by makeElSVG's 'aoe' case and any future callers.
const AOE_CONE_SPREAD_DEG = 53;
function aoeConePoints(x1, y1, x2, y2) {
  const dist = Math.hypot(x2-x1, y2-y1);
  const angle = Math.atan2(y2-y1, x2-x1);
  const half = (AOE_CONE_SPREAD_DEG/2) * Math.PI/180;
  const p1 = [x1 + dist*Math.cos(angle-half), y1 + dist*Math.sin(angle-half)];
  const p2 = [x1 + dist*Math.cos(angle+half), y1 + dist*Math.sin(angle+half)];
  return [[x1,y1], p1, p2];
}

// ── Render one element into an SVG <g> ────────────────────────────────────────
function makeElSVG(el) {
  // Layer visibility / lock
  const ls = layerState[el.layer || 'Tracks'];
  if (ls && !ls.vis) return null;
  const lockAttr = (ls && ls.lock) ? 'pointer-events="none"' : '';

  const g = document.createElementNS(NS, 'g');
  g.setAttribute('opacity', el.opacity ?? 0.9);
  if (ls && ls.lock) g.setAttribute('pointer-events', 'none');
  let main = null;

  switch (el.type) {
    case 'rect':
      main = svgEl('rect', { x:el.x, y:el.y, width:el.w, height:el.h, rx:el.rx||0, ry:el.rx||0 });
      break;
    case 'circle':
      main = svgEl('ellipse', { cx:el.cx, cy:el.cy, rx:el.rx, ry:el.ry });
      break;
    case 'line': {
      const pts = elPoints(el);
      if (pts.length === 2) {
        main = svgEl('line', { x1:el.x1, y1:el.y1, x2:el.x2, y2:el.y2 });
      } else {
        const d = pts.map((p,i)=>(i===0?'M':'L')+p.x+','+p.y).join(' ');
        main = svgEl('path', { d, fill:'none' });
      }
      break;
    }
    case 'arrow': {
      const pts = elPoints(el);
      if (pts.length === 2) {
        main = svgEl('line', { x1:el.x1, y1:el.y1, x2:el.x2, y2:el.y2, 'marker-end':'url(#arr-end)' });
      } else {
        const d = pts.map((p,i)=>(i===0?'M':'L')+p.x+','+p.y).join(' ');
        main = svgEl('path', { d, fill:'none', 'marker-end':'url(#arr-end)' });
      }
      break;
    }
    case 'poly':
      main = svgEl('polygon', { points: el.points.map(p=>p.join(',')).join(' ') });
      break;
    case 'path':
      main = svgEl('path', { d: pointsToPath(el.pts) });
      break;
    case 'image':
      main = svgEl('image', { x:el.x, y:el.y, width:el.w, height:el.h, href:el.href, preserveAspectRatio:'none' });
      g.appendChild(main); return g;
    case 'measure': {
      const ln = svgEl('line', { x1:el.x1, y1:el.y1, x2:el.x2, y2:el.y2,
        stroke:'#ffaa44', 'stroke-width':2, 'stroke-dasharray':'6 3' });
      g.appendChild(ln);
      const dist = Math.hypot(el.x2-el.x1, el.y2-el.y1);
      const ang  = Math.atan2(el.y2-el.y1, el.x2-el.x1) * 180/Math.PI;
      const units = pxToUnits(dist);
      const lt = svgEl('text', { x:(el.x1+el.x2)/2, y:(el.y1+el.y2)/2-6,
        'text-anchor':'middle', 'font-size':14, 'font-family':'monospace',
        fill:'#ffaa44', 'pointer-events':'none' });
      lt.textContent = `${Math.round(dist)}px  ${ang.toFixed(1)}°`;
      g.appendChild(lt);
      if (units) {
        const lt2 = svgEl('text', { x:(el.x1+el.x2)/2, y:(el.y1+el.y2)/2+12,
          'text-anchor':'middle', 'font-size':12, 'font-family':'monospace',
          fill:'#ffaa44', 'pointer-events':'none' });
        lt2.textContent = `${units.cells.toFixed(1)} cells (${units.units} ${units.label})`;
        g.appendChild(lt2);
      }
      [[el.x1,el.y1],[el.x2,el.y2]].forEach(([x,y])=>{
        g.appendChild(svgEl('circle',{cx:x,cy:y,r:3,fill:'#ffaa44'}));
      });
      return g;
    }
    case 'aoe': {
      const dist = Math.hypot(el.x2-el.x1, el.y2-el.y1);
      const color = el.color || '#ff6666';
      if (el.shape === 'circle') {
        g.appendChild(svgEl('circle', { cx:el.x1, cy:el.y1, r:dist, fill:color, opacity:0.28, stroke:color, 'stroke-width':2 }));
      } else if (el.shape === 'cone') {
        const pts = aoeConePoints(el.x1, el.y1, el.x2, el.y2);
        g.appendChild(svgEl('polygon', { points: pts.map(p=>p.join(',')).join(' '), fill:color, opacity:0.28, stroke:color, 'stroke-width':2 }));
      } else {
        const width = gridConfig.cell_size || 20;
        g.appendChild(svgEl('line', { x1:el.x1, y1:el.y1, x2:el.x2, y2:el.y2, stroke:color, 'stroke-width':width, opacity:0.28 }));
        g.appendChild(svgEl('line', { x1:el.x1, y1:el.y1, x2:el.x2, y2:el.y2, stroke:color, 'stroke-width':2, opacity:0.8 }));
      }
      const units = pxToUnits(dist);
      const lt = svgEl('text', { x:(el.x1+el.x2)/2, y:(el.y1+el.y2)/2-6,
        'text-anchor':'middle', 'font-size':12, 'font-family':'monospace',
        fill:'#fff', 'pointer-events':'none', style:'paint-order:stroke;stroke:#000;stroke-width:3px' });
      lt.textContent = units ? `${units.units} ${units.label}` : `${Math.round(dist)}px`;
      g.appendChild(lt);
      return g;
    }
    case 'text': {
      const t = svgEl('text', { x:el.x, y:el.y, 'font-size':el.size||20,
        'font-family':'monospace', fill:el.color||'#fff',
        'font-weight': el.bold ? 'bold' : 'normal' });
      t.textContent = el.text || '';
      g.appendChild(t);
      return g;
    }
    case 'pin': {
      g.appendChild(svgEl('circle', { cx:el.x, cy:el.y-10, r:9, fill:el.stroke||'#00f0ff', stroke:'#fff', 'stroke-width':1.5 }));
      g.appendChild(svgEl('line',   { x1:el.x, y1:el.y-1, x2:el.x, y2:el.y+10, stroke:el.stroke||'#00f0ff', 'stroke-width':2 }));
      if (el.label) {
        const lt = svgEl('text', { x:el.x+13, y:el.y-5, 'font-size':13, 'font-family':'monospace', fill:'#fff', 'pointer-events':'none' });
        lt.textContent = el.label; g.appendChild(lt);
      }
      return g;
    }
    case 'token': {
      const r = el.r || 20;
      const isItem = el.source === 'item';
      const dead = !isItem && el.max_hp > 0 && el.hp <= 0;
      const clipId = 'tk-clip-' + el.id;
      if (el.image_url) {
        const clip = svgEl('clipPath', { id: clipId });
        clip.appendChild(svgEl('circle', { cx:el.x, cy:el.y, r:r-2 }));
        g.appendChild(clip);
        g.appendChild(svgEl('image', { x:el.x-r+2, y:el.y-r+2, width:(r-2)*2, height:(r-2)*2,
          href:el.image_url, preserveAspectRatio:'xMidYMid slice', 'clip-path':`url(#${clipId})` }));
      } else {
        const t = svgEl('text', { x:el.x, y:el.y+r*0.35, 'text-anchor':'middle', 'font-size':r*1.1, 'pointer-events':'none' });
        t.textContent = isItem ? '📦' : (el.source === 'pc' ? '🧑' : (el.source === 'entity' ? '👹' : '❖'));
        g.appendChild(svgEl('circle', { cx:el.x, cy:el.y, r, fill:el.color||'#4488ff', opacity:0.35 }));
        g.appendChild(t);
      }
      const isActive = typeof activeCombatantId !== 'undefined' && activeCombatantId && el.combatant_id === activeCombatantId;
      if (isActive) {
        g.appendChild(svgEl('circle', { cx:el.x, cy:el.y, r:r+5, fill:'none',
          stroke:'#ffd700', 'stroke-width':3, opacity:0.9 }));
      }
      g.appendChild(svgEl('circle', { cx:el.x, cy:el.y, r, fill:'none',
        stroke: dead ? '#666' : (el.color||'#4488ff'), 'stroke-width':2.5, opacity: dead?0.6:1 }));
      if (el.name) {
        const lt = svgEl('text', { x:el.x, y:el.y+r+14, 'text-anchor':'middle', 'font-size':12,
          'font-family':'monospace', fill:'#fff', 'pointer-events':'none',
          style:'paint-order:stroke;stroke:#000;stroke-width:3px' });
        lt.textContent = el.name; g.appendChild(lt);
      }
      if (isItem) {
        if (el.qty > 1) {
          const qt = svgEl('text', { x:el.x, y:el.y+r+27, 'text-anchor':'middle', 'font-size':10,
            'font-family':'monospace', fill:'#f0c040', 'pointer-events':'none',
            style:'paint-order:stroke;stroke:#000;stroke-width:3px' });
          qt.textContent = `×${el.qty}`; g.appendChild(qt);
        }
      } else if (el.max_hp) {
        const hpt = svgEl('text', { x:el.x, y:el.y+r+27, 'text-anchor':'middle', 'font-size':10,
          'font-family':'monospace', fill: dead ? '#ff6666' : '#9dffb0', 'pointer-events':'none',
          style:'paint-order:stroke;stroke:#000;stroke-width:3px' });
        hpt.textContent = `${el.hp ?? 0}/${el.max_hp}`; g.appendChild(hpt);
      }
      if (el.conditions && el.conditions.length) {
        const ct = svgEl('text', { x:el.x, y:el.y-r-8, 'text-anchor':'middle', 'font-size':9,
          'font-family':'monospace', fill:'#ffcc66', 'pointer-events':'none',
          style:'paint-order:stroke;stroke:#000;stroke-width:3px' });
        ct.textContent = el.conditions.join(', '); g.appendChild(ct);
      }
      return g;
    }
  }
  if (!main) return null;
  const isLineLike = ['line','arrow','measure'].includes(el.type);
  main.setAttribute('fill',         (isLineLike || el.type==='path') ? 'none' : (el.fill||'#334455'));
  main.setAttribute('stroke',       el.stroke||'#00f0ff');
  main.setAttribute('stroke-width', el.strokeW ?? 2);
  if (el.dash) main.setAttribute('stroke-dasharray', el.dash);
  if (el.type === 'path') main.setAttribute('stroke-linecap', 'round'), main.setAttribute('stroke-linejoin','round');
  g.appendChild(main);
  if (el.label && !isLineLike && el.type !== 'path' && el.type !== 'image') {
    const cx = el.type==='rect'   ? el.x+el.w/2
             : el.type==='circle' ? el.cx
             : el.type==='poly'   ? el.points.reduce((s,p)=>s+p[0],0)/el.points.length
             : 0;
    const cy = el.type==='rect'   ? el.y+el.h/2
             : el.type==='circle' ? el.cy
             : el.type==='poly'   ? el.points.reduce((s,p)=>s+p[1],0)/el.points.length
             : 0;
    const lt = svgEl('text', { x:cx, y:cy+5, 'text-anchor':'middle', 'font-size':13,
      'font-family':'monospace', fill:'#fff', 'pointer-events':'none' });
    lt.textContent = el.label; g.appendChild(lt);
  }
  return g;
}
