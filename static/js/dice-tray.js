// The 3D dice tray: real dice on a felt table, thrown with the mouse.
//
// Press on the table and the dice jump into your hand; drag and they follow the pointer; let go and they leave with the
// speed of your hand and tumble under physics (three.js draws, cannon-es simulates; both are vendored under static/vendor,
// no network needed). Which number counts is read from the settled dice (static/js/dice-geometry.js), never decided by
// anything but the physics. A die that comes to rest leaning on a wall or another die is nudged until it lies flat.
//
//   const tray = ndDiceTray.create(container, { getPlan, onGrab, onThrow, onSettled, onError });
//   tray.throwNow()  - throw the plan from the edge of the table without the mouse (the Throw button)
//   tray.clear()     - sweep the table
//   tray.setSound(on)
// getPlan() returns ndDiceGeometry.plan(...) (or {error}); onSettled({plan, readings, labels}) fires once every die is still.
import * as THREE from '/static/vendor/three-0.159.0.module.min.js';
import * as CANNON from '/static/vendor/cannon-es-0.20.0.js';

const G = window.ndDiceGeometry;

const TRAY_W = 18, TRAY_D = 11, CEILING = 16, HOLD_Y = 4.5;
const SIZE = { 4: 1.3, 6: 1.15, 8: 1.25, 10: 1.25, 12: 1.35, 20: 1.45 };       // circumradius of each die on the table
const THEME = {
  4: ['#7b3fe4', '#ffffff'], 6: ['#0b9db8', '#ffffff'], 8: ['#1f9d55', '#ffffff'],
  10: ['#d98a00', '#ffffff'], '10t': ['#a65f00', '#ffffff'], 12: ['#d6336c', '#ffffff'], 20: ['#2f5fd0', '#ffffff'],
};

const atlasCache = {};
function polygonCentroid(pts) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length], f = x0 * y1 - x1 * y0;
    a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f;
  }
  return Math.abs(a) < 1e-12 ? [0, 0] : [cx / (3 * a), cy / (3 * a)];
}
function inradius(pts, c) {
  let best = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    const ex = x1 - x0, ey = y1 - y0, l = Math.hypot(ex, ey) || 1;
    best = Math.min(best, Math.abs((c[0] - x0) * ey - (c[1] - y0) * ex) / l);
  }
  return best;
}

// One texture holds every face of a die type: a cell per face, filled with the die's colour and carrying its number.
function atlasFor(sh, role) {
  const key = sh.sides + (role === 'tens' ? 't' : '');
  if (atlasCache[key]) return atlasCache[key];
  const n = sh.faces.length, cols = Math.ceil(Math.sqrt(n)), rows = Math.ceil(n / cols), cell = 256;
  const canvas = document.createElement('canvas');
  canvas.width = cols * cell; canvas.height = rows * cell;
  const ctx = canvas.getContext('2d');
  const [bg, fg] = THEME[key] || THEME[sh.sides];
  const uvs = [];
  sh.faces.forEach((f, i) => {
    const cx = (i % cols) * cell + cell / 2, cy = Math.floor(i / cols) * cell + cell / 2;
    ctx.fillStyle = bg;
    ctx.fillRect(cx - cell / 2, cy - cell / 2, cell, cell);
    const g = ctx.createRadialGradient(cx, cy, 4, cx, cy, cell * 0.55);
    g.addColorStop(0, 'rgba(255,255,255,.14)'); g.addColorStop(1, 'rgba(0,0,0,.20)');
    ctx.fillStyle = g; ctx.fillRect(cx - cell / 2, cy - cell / 2, cell, cell);
    const outline = G.faceOutline(sh, f);
    const R = Math.max(...outline.map(p => Math.hypot(p[0], p[1])));
    const s = (cell * 0.5 * 0.97) / R;                                          // pixels per unit: the whole face fits its cell
    uvs.push(outline.map(p => [(cx + p[0] * s) / canvas.width, 1 - (cy - p[1] * s) / canvas.height]));
    const c = polygonCentroid(outline), rho = inradius(outline, c) * s;
    ctx.fillStyle = fg; ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.lineJoin = 'round';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    if (sh.kind === 'vertex') {                                                  // d4: the number of each corner, near that corner
      outline.forEach((p, k) => {
        const label = String(sh.vertexLabels[f.idx[k]]);
        const px = cx + (c[0] + (p[0] - c[0]) * 0.58) * s, py = cy - (c[1] + (p[1] - c[1]) * 0.58) * s;
        const dx = p[0] - c[0], dy = -(p[1] - c[1]);
        ctx.save(); ctx.translate(px, py); ctx.rotate(Math.atan2(dx, -dy));
        ctx.font = `bold ${Math.round(rho * 0.95)}px system-ui, sans-serif`;
        ctx.lineWidth = 5; ctx.strokeText(label, 0, 0); ctx.fillText(label, 0, 0);
        ctx.restore();
      });
      return;
    }
    let label = String(f.label);
    if (role === 'tens') label = f.label === 0 ? '00' : f.label + '0';
    else if (f.label === 6 || f.label === 9) label += '.';                      // a 6 and a 9 must not look alike upside down
    const size = Math.round(Math.min(cell * 0.62, rho * (label.length > 2 ? 1.25 : 1.6)));
    ctx.font = `bold ${size}px system-ui, sans-serif`;
    ctx.lineWidth = Math.max(4, size / 9);
    const px = cx + c[0] * s, py = cy - c[1] * s;
    ctx.strokeText(label, px, py); ctx.fillText(label, px, py);
  });
  const tex = new THREE.CanvasTexture(canvas);
  tex.anisotropy = 4;
  tex.colorSpace = THREE.SRGBColorSpace;
  return (atlasCache[key] = { tex, uvs });
}

const geoCache = {};
function geometryFor(sh, role) {
  const key = sh.sides + (role === 'tens' ? 't' : '');
  if (geoCache[key]) return geoCache[key];
  const { uvs } = atlasFor(sh, role);
  const pos = [], uv = [], nor = [];
  sh.faces.forEach((f, i) => {
    const ids = f.idx;
    for (let k = 1; k < ids.length - 1; k++) {
      [0, k, k + 1].forEach(j => {
        const v = sh.vertices[ids[j]];
        pos.push(v[0], v[1], v[2]); nor.push(f.normal[0], f.normal[1], f.normal[2]);
        uv.push(uvs[i][j][0], uvs[i][j][1]);
      });
    }
  });
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
  geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  return (geoCache[key] = geo);
}

function rand(a, b) { return a + Math.random() * (b - a); }
// A big handful gets smaller dice, so the table never turns into a pile (the physics shapes are scaled the same way).
function handScale(n) { return n <= 10 ? 1 : Math.max(0.6, Math.sqrt(10 / n)); }

function felt() {
  const c = document.createElement('canvas'); c.width = c.height = 512;
  const x = c.getContext('2d');
  x.fillStyle = '#1b3a4a'; x.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 9000; i++) {
    x.fillStyle = `rgba(${Math.random() < 0.5 ? '255,255,255' : '0,0,0'},${Math.random() * 0.05})`;
    x.fillRect(Math.random() * 512, Math.random() * 512, 2, 2);
  }
  const t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(5, 3);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function supported() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
  } catch (e) { return false; }
}

function create(container, opts) {
  opts = opts || {};
  if (!supported()) return null;
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); } catch (e) { return null; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const canvas = renderer.domElement;
  canvas.style.cssText = 'display:block;width:100%;height:100%;touch-action:none;cursor:grab;border-radius:6px';
  container.appendChild(canvas);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#0a121a');
  const camera = new THREE.PerspectiveCamera(36, 1, 1, 200);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x556677, 1.25));
  const sun = new THREE.DirectionalLight(0xffffff, 1.1);
  sun.position.set(7, 22, 9); sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024); sun.shadow.radius = 5; sun.shadow.bias = -0.0005;
  Object.assign(sun.shadow.camera, { left: -13, right: 13, top: 10, bottom: -10, near: 5, far: 50 });
  scene.add(sun);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(TRAY_W, TRAY_D), new THREE.MeshStandardMaterial({ map: felt(), roughness: 0.95, metalness: 0 }));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; scene.add(floor);
  const rimMat = new THREE.MeshStandardMaterial({ color: '#0e4a58', roughness: 0.6, emissive: '#06353f' });
  [[0, -TRAY_D / 2 - 0.5, TRAY_W + 2, 1], [0, TRAY_D / 2 + 0.5, TRAY_W + 2, 1]].forEach(([x, z, w, d]) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, 1.6, d), rimMat); m.position.set(x, 0.8, z); scene.add(m);
  });
  [-TRAY_W / 2 - 0.5, TRAY_W / 2 + 0.5].forEach(x => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(1, 1.6, TRAY_D), rimMat); m.position.set(x, 0.8, 0); scene.add(m);
  });

  // ── physics ──
  const world = new CANNON.World({ gravity: new CANNON.Vec3(0, -75, 0) });
  world.allowSleep = true;
  world.solver.iterations = 14;
  const diceMat = new CANNON.Material('dice'), tableMat = new CANNON.Material('table');
  world.addContactMaterial(new CANNON.ContactMaterial(diceMat, tableMat, { friction: 0.42, restitution: 0.32 }));
  world.addContactMaterial(new CANNON.ContactMaterial(diceMat, diceMat, { friction: 0.3, restitution: 0.4 }));
  function plane(rotAxis, angle, x, y, z) {
    const b = new CANNON.Body({ mass: 0, material: tableMat, shape: new CANNON.Plane() });
    b.quaternion.setFromAxisAngle(new CANNON.Vec3(...rotAxis), angle); b.position.set(x, y, z); world.addBody(b);
  }
  plane([1, 0, 0], -Math.PI / 2, 0, 0, 0);                         // floor, facing up
  plane([1, 0, 0], Math.PI / 2, 0, CEILING, 0);                    // ceiling, facing down
  plane([0, 1, 0], Math.PI / 2, -TRAY_W / 2, 0, 0);                // left wall, facing +x
  plane([0, 1, 0], -Math.PI / 2, TRAY_W / 2, 0, 0);                // right wall, facing -x
  plane([0, 1, 0], 0, 0, 0, -TRAY_D / 2);                          // back wall, facing +z
  plane([0, 1, 0], Math.PI, 0, 0, TRAY_D / 2);                     // front wall, facing -z

  // ── the dice on the table ──
  let dice = [];                       // {spec, shape, mesh, body, radius, spin}
  let state = 'idle';                  // idle | held | rolling | settled
  let plan = null, hand = { x: 0, z: 0 }, handTarget = { x: 0, z: 0 }, samples = [];
  let rollStarted = 0, stillSince = 0, nudges = 0, raf = 0, lastT = 0, sound = true;
  const labels = document.createElement('div');
  labels.style.cssText = 'position:absolute;inset:0;pointer-events:none;overflow:hidden';
  container.style.position = 'relative';
  container.appendChild(labels);
  const hint = document.createElement('div');
  hint.style.cssText = 'position:absolute;left:0;right:0;bottom:6px;text-align:center;font:12px system-ui,sans-serif;color:rgba(255,255,255,.55);pointer-events:none;text-shadow:0 1px 2px #000';
  hint.textContent = 'Press on the table, drag, and let go to throw';
  container.appendChild(hint);

  function removeDice() {
    dice.forEach(d => {
      scene.remove(d.mesh);
      if (d.body) world.removeBody(d.body);
      d.mesh.material.dispose();                                       // the shared geometry and atlas stay cached
      d.mesh.children.forEach(c => { c.geometry.dispose(); c.material.dispose(); });
    });
    dice = []; labels.textContent = '';
  }

  function buildDice(p) {
    removeDice();
    p.dice.forEach(spec => {
      const sh = G.shape(spec.sides), r = SIZE[spec.sides] * handScale(p.dice.length);
      const { tex } = atlasFor(sh, spec.role);
      const mat = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.38, metalness: 0.1 });
      const mesh = new THREE.Mesh(geometryFor(sh, spec.role), mat);
      mesh.scale.setScalar(r); mesh.castShadow = true;
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometryFor(sh, spec.role), 5), new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35 }));
      mesh.add(edges);
      mesh.quaternion.setFromEuler(new THREE.Euler(rand(0, 6.28), rand(0, 6.28), rand(0, 6.28)));
      scene.add(mesh);
      dice.push({ spec, shape: sh, mesh, body: null, radius: r, spin: new THREE.Vector3(rand(-1, 1), rand(-1, 1), rand(-1, 1)).multiplyScalar(rand(5, 9)) });
    });
  }

  function handOffset(i, n) {
    if (n === 1) return [0, 0];
    const a = i * 2.399963, rr = 1.55 * handScale(n) * Math.sqrt(i + 0.6);
    return [Math.cos(a) * rr, Math.sin(a) * rr];
  }

  function clampHand(x, z) {
    return [Math.max(-TRAY_W / 2 + 3, Math.min(TRAY_W / 2 - 3, x)), Math.max(-TRAY_D / 2 + 2.5, Math.min(TRAY_D / 2 - 2.5, z))];
  }

  function startLoop() { if (!raf) { lastT = performance.now(); raf = requestAnimationFrame(frame); } }

  function placeHeld(dt, t) {
    hand.x += (handTarget.x - hand.x) * Math.min(1, dt * 16);
    hand.z += (handTarget.z - hand.z) * Math.min(1, dt * 16);
    samples.push({ t, x: hand.x, z: hand.z });
    while (samples.length > 2 && t - samples[0].t > 140) samples.shift();
    dice.forEach((d, i) => {
      const [ox, oz] = handOffset(i, dice.length);
      d.mesh.position.set(hand.x + ox, HOLD_Y + Math.sin(t / 130 + i) * 0.18, hand.z + oz);
      d.mesh.rotateX(d.spin.x * dt); d.mesh.rotateY(d.spin.y * dt); d.mesh.rotateZ(d.spin.z * dt);
    });
  }

  function release(vx, vz) {
    const speed = Math.hypot(vx, vz);
    state = 'rolling'; rollStarted = performance.now(); stillSince = 0; nudges = 0;
    labels.textContent = '';
    dice.forEach((d, i) => {
      const body = new CANNON.Body({
        mass: 1, material: diceMat, linearDamping: 0.08, angularDamping: 0.12,
        sleepSpeedLimit: 0.6, sleepTimeLimit: 0.35,
      });
      const hull = new CANNON.ConvexPolyhedron({
        vertices: d.shape.vertices.map(v => new CANNON.Vec3(v[0] * d.radius, v[1] * d.radius, v[2] * d.radius)),
        faces: d.shape.faces.map(f => f.idx.slice()),
      });
      body.addShape(hull);
      body.position.copy(d.mesh.position);
      body.quaternion.copy(d.mesh.quaternion);
      const k = rand(0.85, 1.15);
      body.velocity.set(vx * k + rand(-2, 2), rand(-6, -2), vz * k + rand(-2, 2));
      const spin = 10 + Math.min(speed, 50) * 0.55;
      body.angularVelocity.set(rand(-1, 1) * spin, rand(-1, 1) * spin, rand(-1, 1) * spin);
      body.addEventListener('collide', e => { try { tick(e.contact.getImpactVelocityAlongNormal()); } catch (err) { /* sound is a bonus */ } });
      world.addBody(body);
      d.body = body;
    });
    hint.style.opacity = 0;
    if (opts.onThrow) opts.onThrow();
    startLoop();
  }

  // a settled die lying on an edge or leaning on another die is lifted and dropped again
  function nudge(d) {
    d.body.wakeUp();
    d.body.velocity.set(rand(-5, 5), 9 + nudges, rand(-5, 5));
    d.body.angularVelocity.set(rand(-8, 8), rand(-8, 8), rand(-8, 8));
  }

  function checkSettled(now) {
    const moving = dice.some(d => d.body.sleepState !== CANNON.Body.SLEEPING && (d.body.velocity.length() > 0.35 || d.body.angularVelocity.length() > 0.5));
    const lost = dice.some(d => d.body.position.y < -3 || Math.abs(d.body.position.x) > TRAY_W || Math.abs(d.body.position.z) > TRAY_D);
    if (lost) {
      dice.forEach(d => { if (d.body.position.y < -3 || Math.abs(d.body.position.x) > TRAY_W || Math.abs(d.body.position.z) > TRAY_D) { d.body.position.set(rand(-3, 3), 6, rand(-2, 2)); d.body.velocity.set(0, 0, 0); d.body.wakeUp(); } });
      return false;
    }
    if (moving) { stillSince = 0; return false; }
    if (!stillSince) stillSince = now;
    if (now - stillSince < 250) return false;
    const reads = dice.map(d => G.readValue(d.shape, [d.body.quaternion.x, d.body.quaternion.y, d.body.quaternion.z, d.body.quaternion.w]));
    const cocked = dice.filter((d, i) => reads[i].flat < 0.965);
    if (cocked.length && nudges < 8 && now - rollStarted < 20000) {
      nudges++; stillSince = 0;
      cocked.forEach(nudge);
      return false;
    }
    finish(reads);
    return true;
  }

  function finish(reads) {
    state = 'settled';
    const result = G.combine(plan, reads);
    showLabels(reads);
    if (opts.onSettled) opts.onSettled({ plan, readings: reads, result });
  }

  // the number each die shows, floating above it (a percentile pair shows its two-digit value on the tens die)
  function showLabels(reads) {
    labels.textContent = '';
    const rect = canvas.getBoundingClientRect(), v = new THREE.Vector3();
    dice.forEach((d, i) => {
      let text = String(reads[i].value);
      if (d.spec.role === 'units') return;
      if (d.spec.role === 'tens') { const t = reads[i].label * 10 + reads[i + 1].label; text = String(t === 0 ? 100 : t); }
      v.copy(d.mesh.position); v.y += d.radius * 1.2; v.project(camera);
      const el = document.createElement('div');
      el.textContent = text;
      el.style.cssText = `position:absolute;left:${(v.x * 0.5 + 0.5) * rect.width}px;top:${(-v.y * 0.5 + 0.5) * rect.height}px;transform:translate(-50%,-100%);` +
        'background:rgba(0,0,0,.72);color:#fff;font:700 13px system-ui,sans-serif;padding:1px 6px;border-radius:9px;border:1px solid rgba(255,255,255,.35)';
      labels.appendChild(el);
    });
  }

  function frame(t) {
    raf = 0;
    if (document.hidden) { if (state === 'held' || state === 'rolling') raf = requestAnimationFrame(frame); return; }
    const dt = Math.min(0.05, (t - lastT) / 1000 || 0.016);
    lastT = t;
    if (state === 'held') placeHeld(dt, t);
    if (state === 'rolling') {
      world.step(1 / 90, dt, 6);
      dice.forEach(d => { d.mesh.position.copy(d.body.position); d.mesh.quaternion.copy(d.body.quaternion); });
      if (checkSettled(t)) { renderer.render(scene, camera); return; }
      if (t - rollStarted > 25000) { finish(dice.map(d => G.readValue(d.shape, [d.body.quaternion.x, d.body.quaternion.y, d.body.quaternion.z, d.body.quaternion.w]))); }
    }
    renderer.render(scene, camera);
    if (state === 'held' || state === 'rolling') raf = requestAnimationFrame(frame);
  }

  // ── sound: a short filtered click, louder for harder hits ──
  let audio = null, lastTick = 0;
  function tick(speed) {
    if (!sound || speed < 2.5) return;
    const now = performance.now();
    if (now - lastTick < 28) return;
    lastTick = now;
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === 'suspended') audio.resume().catch(() => {});
    const len = Math.floor(audio.sampleRate * 0.05), buf = audio.createBuffer(1, len, audio.sampleRate), data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
    const src = audio.createBufferSource(); src.buffer = buf;
    const f = audio.createBiquadFilter(); f.type = 'bandpass'; f.frequency.value = rand(1100, 2600); f.Q.value = 1.4;
    const g = audio.createGain(); g.gain.value = Math.min(0.5, speed * 0.025);
    src.connect(f); f.connect(g); g.connect(audio.destination); src.start();
  }

  // ── camera: the whole table always fits, whatever shape the box is ──
  function fit() {
    const w = Math.max(160, container.clientWidth), h = Math.max(140, container.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    const tilt = (66 * Math.PI) / 180, half = (camera.fov * Math.PI) / 360;
    const needW = (TRAY_W * 1.04) / (2 * Math.tan(half) * camera.aspect), needH = (TRAY_D * 1.25) / (2 * Math.tan(half));
    const dist = Math.max(needW, needH);
    camera.position.set(0, Math.sin(tilt) * dist, Math.cos(tilt) * dist + 0.8);
    camera.lookAt(0, 0, 0.8);
    renderer.render(scene, camera);
  }
  const ro = new ResizeObserver(fit); ro.observe(container);
  fit();

  // ── the mouse ──
  const ray = new THREE.Raycaster(), holdPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -HOLD_Y), hit = new THREE.Vector3();
  function pointToTable(ev) {
    const r = canvas.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -(((ev.clientY - r.top) / r.height) * 2 - 1)), camera);
    return ray.ray.intersectPlane(holdPlane, hit) ? [hit.x, hit.z] : null;
  }

  function pickUp(ev) {
    const p = opts.getPlan ? opts.getPlan() : null;
    if (!p || p.error) { if (opts.onError) opts.onError(p ? p.error : 'Nothing to throw.'); return false; }
    plan = p;
    buildDice(p);
    const at = pointToTable(ev) || [0, 0], [x, z] = clampHand(at[0], at[1]);
    hand = { x, z }; handTarget = { x, z }; samples = [];
    state = 'held';
    hint.style.opacity = 0;
    if (opts.onGrab) opts.onGrab();
    startLoop();
    return true;
  }

  canvas.addEventListener('pointerdown', ev => {
    if (ev.button !== undefined && ev.button !== 0) return;
    ev.preventDefault();
    if (!pickUp(ev)) return;
    canvas.setPointerCapture(ev.pointerId);
    canvas.style.cursor = 'grabbing';
  });
  canvas.addEventListener('pointermove', ev => {
    if (state !== 'held') return;
    const at = pointToTable(ev);
    if (at) { const [x, z] = clampHand(at[0], at[1]); handTarget = { x, z }; }
  });
  function drop(ev) {
    if (state !== 'held') return;
    canvas.style.cursor = 'grab';
    const now = performance.now();
    let vx = 0, vz = 0;
    const first = samples.find(s => now - s.t < 160) || samples[0], last = samples[samples.length - 1];
    if (first && last && last.t > first.t) { vx = (last.x - first.x) / ((last.t - first.t) / 1000); vz = (last.z - first.z) / ((last.t - first.t) / 1000); }
    let speed = Math.hypot(vx, vz);
    if (speed > 70) { vx *= 70 / speed; vz *= 70 / speed; speed = 70; }
    if (speed < 7) {                                    // let go without a flick: a gentle toss in a random direction
      const a = rand(0, Math.PI * 2), s = rand(8, 13);
      vx = Math.cos(a) * s; vz = Math.sin(a) * s;
    }
    release(vx, vz);
  }
  canvas.addEventListener('pointerup', drop);
  canvas.addEventListener('pointercancel', drop);

  return {
    supported: true,
    get state() { return state; },
    get busy() { return state === 'held' || state === 'rolling'; },
    throwNow() {
      const p = opts.getPlan ? opts.getPlan() : null;
      if (!p || p.error) { if (opts.onError) opts.onError(p ? p.error : 'Nothing to throw.'); return false; }
      plan = p; buildDice(p);
      const fromLeft = Math.random() < 0.5, x = (fromLeft ? -1 : 1) * (TRAY_W / 2 - 3.5), z = rand(-3, 3);
      hand = { x, z }; handTarget = { x, z }; samples = [];
      dice.forEach((d, i) => { const [ox, oz] = handOffset(i, dice.length); d.mesh.position.set(x + ox, HOLD_Y, z + oz); });
      if (opts.onGrab) opts.onGrab();
      release((fromLeft ? 1 : -1) * rand(24, 38), rand(-9, 9));
      return true;
    },
    clear() { state = 'idle'; removeDice(); hint.style.opacity = 1; renderer.render(scene, camera); },
    setSound(on) { sound = !!on; },
    render() { renderer.render(scene, camera); },
  };
}

window.ndDiceTray = { create, supported };
window.dispatchEvent(new Event('nd-dice-tray-ready'));
