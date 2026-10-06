// Physical dice for the 3D tray - pure geometry and reading, no DOM, no three.js, no physics engine, so every decision is
// tested under Node (tests/test_dice_geometry.py). dice-tray.js builds the meshes and the physics bodies from these shapes.
//   shape(sides)            - d4 d6 d8 d10 d12 d20 as vertices + faces (outward normals, counter-clockwise), each face
//                             with the number it shows. Opposite faces add up to sides+1 (d10: labels 0-9, opposite = 9).
//   readValue(shape, quat)  - which number is up after a roll, and how flat the die lies (1 = perfectly flat)
//   parseNotation / plan    - "2d6+3" -> the dice to throw (a d100 is two d10: tens and units) and the modifier
//   combine                 - the thrown values back into per-term rolls and a total, in the shape the roll log stores
// Unit circumradius: the tray scales a shape to the size it wants.
(function (root) {
  'use strict';
  var PHI = (1 + Math.sqrt(5)) / 2;
  var SUPPORTED = [4, 6, 8, 10, 12, 20, 100];
  var MAX_DICE = 20;

  function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function len(a) { return Math.sqrt(dot(a, a)); }
  function norm(a) { var l = len(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; }
  function scale(a, s) { return [a[0] * s, a[1] * s, a[2] * s]; }
  function mean(points) {
    var s = [0, 0, 0];
    points.forEach(function (p) { s[0] += p[0]; s[1] += p[1]; s[2] += p[2]; });
    return scale(s, 1 / points.length);
  }

  function normalizeAll(vs) { return vs.map(norm); }

  // Rotate a vector by a quaternion [x, y, z, w].
  function rotate(q, v) {
    var x = q[0], y = q[1], z = q[2], w = q[3];
    var tx = 2 * (y * v[2] - z * v[1]), ty = 2 * (z * v[0] - x * v[2]), tz = 2 * (x * v[1] - y * v[0]);
    return [v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz), v[2] + w * tz + (x * ty - y * tx)];
  }

  // The vertices a face is made of: those lying on the plane facing `normal` (max projection), ordered counter-clockwise
  // as seen from outside, plus the frame the tray prints the number in: v points "up" the numeral (at `aim`, a point the
  // number's top should point towards - a corner by default, an edge's middle for a square), u = v x n points right.
  function faceFrom(vertices, normal, aim) {
    var n = norm(normal), best = -Infinity;
    vertices.forEach(function (p) { best = Math.max(best, dot(n, p)); });
    var ids = [];
    vertices.forEach(function (p, i) { if (dot(n, p) > best - 1e-6) ids.push(i); });
    var c = mean(ids.map(function (i) { return vertices[i]; }));
    var toward = aim || vertices[ids[0]];
    var up = sub(toward, c);
    up = norm(sub(up, scale(n, dot(up, n))));                 // inside the face's plane
    var u = cross(up, n), v = up;
    ids.sort(function (a, b) {
      var pa = sub(vertices[a], c), pb = sub(vertices[b], c);
      return Math.atan2(dot(pa, v), dot(pa, u)) - Math.atan2(dot(pb, v), dot(pb, u));
    });
    return { idx: ids, normal: n, center: c, u: u, v: v };
  }

  function facesFromNormals(vertices, normals) {
    return normals.map(function (n) { return faceFrom(vertices, n); });
  }

  // Give opposite faces numbers that add up to `sum`: pairs (1, sum-1), (2, sum-2) ... in face order.
  function numberOppositeFaces(faces, pairs) {
    var done = {}, k = 0;
    faces.forEach(function (f, i) {
      if (done[i]) return;
      var o = -1, bestDot = Infinity;
      faces.forEach(function (g, j) { if (j !== i && !done[j] && dot(f.normal, g.normal) < bestDot) { bestDot = dot(f.normal, g.normal); o = j; } });
      f.label = pairs[k][0]; faces[o].label = pairs[k][1];
      done[i] = done[o] = true;
      k++;
    });
  }

  var cache = {};

  function shape(sides) {
    sides = Math.floor(Number(sides));
    if (cache[sides]) return cache[sides];
    var vertices, faces, s;
    if (sides === 4) {
      vertices = normalizeAll([[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]]);
      // a face is the one opposite a vertex; the number the die shows is the vertex that points UP when it lies on that face
      faces = facesFromNormals(vertices, vertices.map(function (p) { return scale(p, -1); }));
      faces.forEach(function (f, i) { f.label = i + 1; });          // = the opposite vertex's number (vertex i is numbered i + 1)
      s = { sides: 4, vertices: vertices, faces: faces, vertexLabels: [1, 2, 3, 4], kind: 'vertex' };
    } else if (sides === 6) {
      vertices = normalizeAll([[1, 1, 1], [1, 1, -1], [1, -1, 1], [1, -1, -1], [-1, 1, 1], [-1, 1, -1], [-1, -1, 1], [-1, -1, -1]]);
      faces = facesFromNormals(vertices, [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]).map(function (f) {
        var a0 = vertices[f.idx[0]], nearest = f.idx.slice(1).reduce(function (bst, i) { return len(sub(vertices[i], a0)) < len(sub(vertices[bst], a0)) ? i : bst; }, f.idx[1]);
        return faceFrom(vertices, f.normal, mean([a0, vertices[nearest]]));
      });
      numberOppositeFaces(faces, [[1, 6], [2, 5], [3, 4]]);
      s = { sides: 6, vertices: vertices, faces: faces, kind: 'face' };
    } else if (sides === 8) {
      vertices = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];
      var cube = [];
      [1, -1].forEach(function (a) { [1, -1].forEach(function (b) { [1, -1].forEach(function (c) { cube.push([a, b, c]); }); }); });
      faces = facesFromNormals(vertices, cube);
      numberOppositeFaces(faces, [[1, 8], [2, 7], [3, 6], [4, 5]]);
      s = { sides: 8, vertices: vertices, faces: faces, kind: 'face' };
    } else if (sides === 12) {
      var a = PHI, b = 1 / PHI, dv = [];
      [1, -1].forEach(function (x) { [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { dv.push([x, y, z]); }); }); });
      [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { dv.push([0, y * a, z * b]); dv.push([y * a, z * b, 0]); dv.push([z * b, 0, y * a]); }); });
      vertices = normalizeAll(dv);
      var ico = [];                       // the icosahedron's vertices are the dodecahedron's face directions
      [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { ico.push([0, y, z * PHI]); ico.push([y, z * PHI, 0]); ico.push([z * PHI, 0, y]); }); });
      faces = facesFromNormals(vertices, ico);
      numberOppositeFaces(faces, [[1, 12], [2, 11], [3, 10], [4, 9], [5, 8], [6, 7]]);
      s = { sides: 12, vertices: vertices, faces: faces, kind: 'face' };
    } else if (sides === 20) {
      var ic = [];
      [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { ic.push([0, y, z * PHI]); ic.push([y, z * PHI, 0]); ic.push([z * PHI, 0, y]); }); });
      vertices = normalizeAll(ic);
      var dod = [];                       // the dodecahedron's vertices are the icosahedron's face directions
      [1, -1].forEach(function (x) { [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { dod.push([x, y, z]); }); }); });
      [1, -1].forEach(function (y) { [1, -1].forEach(function (z) { dod.push([0, y * PHI, z / PHI]); dod.push([y * PHI, z / PHI, 0]); dod.push([z / PHI, 0, y * PHI]); }); });
      faces = facesFromNormals(vertices, dod);
      var pairs = [];
      for (var k = 1; k <= 10; k++) pairs.push([k, 21 - k]);
      numberOppositeFaces(faces, pairs);
      s = { sides: 20, vertices: vertices, faces: faces, kind: 'face' };
    } else if (sides === 10) {
      // Pentagonal trapezohedron: two poles and a zig-zag ring; the ring height that makes every kite flat is H * (1-cos36)/(1+cos36).
      var H = 1, R = 0.9, c36 = Math.cos(Math.PI / 5), h = H * (1 - c36) / (1 + c36), v10 = [[0, H, 0], [0, -H, 0]];
      for (var i = 0; i < 5; i++) {
        v10.push([R * Math.cos(2 * Math.PI * i / 5), h, R * Math.sin(2 * Math.PI * i / 5)]);                 // upper ring U_i  (2 + i)
      }
      for (var j = 0; j < 5; j++) {
        v10.push([R * Math.cos(2 * Math.PI * (j + 0.5) / 5), -h, R * Math.sin(2 * Math.PI * (j + 0.5) / 5)]); // lower ring L_j (7 + j)
      }
      var circ = Math.max.apply(null, v10.map(len));
      vertices = v10.map(function (p) { return scale(p, 1 / circ); });
      var quads = [];
      for (var m = 0; m < 5; m++) {
        quads.push([0, 2 + m, 7 + m, 2 + (m + 1) % 5]);            // upper kite: top pole, U_m, L_m, U_m+1
        quads.push([1, 7 + (m + 4) % 5, 2 + m, 7 + m]);            // lower kite: bottom pole, L_m-1, U_m, L_m
      }
      faces = quads.map(function (q) {
        var pts = q.map(function (i2) { return vertices[i2]; });
        var cc = mean(pts), nn = norm(cross(sub(pts[1], pts[0]), sub(pts[2], pts[0])));
        if (dot(nn, cc) < 0) nn = scale(nn, -1);
        return faceFrom(vertices, nn, vertices[q[0]]);               // q[0] is the pole of the kite
      });
      numberOppositeFaces(faces, [[0, 9], [1, 8], [2, 7], [3, 6], [4, 5]]);
      s = { sides: 10, vertices: vertices, faces: faces, kind: 'face' };
    } else {
      return null;
    }
    // what the number is worth: the d10's 0 face counts 10
    s.faces.forEach(function (f) { f.value = s.sides === 10 && f.label === 0 ? 10 : f.label; });
    s.radius = 1;
    cache[sides] = s;
    return s;
  }

  // Which number is up, from the die's rotation quaternion [x, y, z, w]. A d4 reads the vertex that points up, which is
  // the face lying DOWN's opposite vertex. flat = how squarely the die lies (1 = perfectly flat; < ~0.97 = leaning on something).
  function readValue(sh, quat) {
    var up = [0, 1, 0], best = null, bestDot = -Infinity, sign = sh.kind === 'vertex' ? -1 : 1;
    sh.faces.forEach(function (f) {
      var d = sign * dot(rotate(quat, f.normal), up);
      if (d > bestDot) { bestDot = d; best = f; }
    });
    return { value: best.value, label: best.label, flat: bestDot };
  }

  // The 2D corners of a face in its own plane (for the number texture): [[x, y]...] in unit circumradius units.
  function faceOutline(sh, face) {
    return face.idx.map(function (i) {
      var p = sub(sh.vertices[i], face.center);
      return [dot(p, face.u), dot(p, face.v)];
    });
  }

  // ── what to throw ────────────────────────────────────────────────────────────────────────────────────

  var TERM = /([+-]?)\s*(?:(\d{0,3})d(\d{1,4})|(\d{1,6}))/gi;
  var WHOLE = /^[+-]?\s*(?:\d{0,3}d\d{1,4}|\d{1,6})(?:\s*[+-]?\s*(?:\d{0,3}d\d{1,4}|\d{1,6}))*$/i;

  // "2d6+3" -> {terms: [{sign:1, count:2, sides:6}, {flat:3}]} or {error}. Same grammar as the server's roll route.
  function parseNotation(text) {
    text = String(text == null ? '' : text).trim();
    if (!text) return { error: 'Enter a dice notation like 2d6+3' };
    if (text.length > 120 || !WHOLE.test(text)) return { error: 'Use dice notation like 2d6+3, d20-1, or 4d8+2d6+1' };
    var terms = [], m;
    TERM.lastIndex = 0;
    while ((m = TERM.exec(text)) !== null) {
      var sign = m[1] === '-' ? -1 : 1;
      if (m[4] !== undefined && m[4] !== '') { terms.push({ sign: sign, flat: parseInt(m[4], 10) }); continue; }
      terms.push({ sign: sign, count: m[2] ? parseInt(m[2], 10) : 1, sides: parseInt(m[3], 10) });
    }
    if (terms.length > 8) return { error: 'At most 8 terms per roll' };
    return { terms: terms };
  }

  // The physical dice for a notation. A d100 is a tens die and a units die. Unsupported dice (a d7, a d30) are reported
  // so the page can say "use Roll for that"; more than MAX_DICE dice is too many for the tray.
  function plan(text) {
    var p = parseNotation(text);
    if (p.error) return { error: p.error };
    var dice = [], unsupported = [];
    p.terms.forEach(function (t, ti) {
      if (t.flat !== undefined) return;
      if (t.count < 1) return;
      if (SUPPORTED.indexOf(t.sides) < 0) { unsupported.push(t.sides); return; }
      for (var i = 0; i < t.count; i++) {
        if (t.sides === 100) { dice.push({ term: ti, sides: 10, role: 'tens' }); dice.push({ term: ti, sides: 10, role: 'units' }); }
        else dice.push({ term: ti, sides: t.sides, role: 'normal' });
      }
    });
    if (unsupported.length) return { error: 'The tray has no physical d' + unsupported[0] + ' - press Roll for that one.', unsupported: unsupported };
    if (!dice.length) return { error: 'Add at least one die to throw.' };
    if (dice.length > MAX_DICE) return { error: 'At most ' + MAX_DICE + ' dice fit on the tray - press Roll for bigger handfuls.' };
    return { terms: p.terms, dice: dice };
  }

  // Thrown readings -> the roll log's shape. readings[i] is the value read from plan.dice[i] (a tens die's value is its
  // label 0..9 times 10... the caller passes the die's label: tens labels 0-9 mean 00-90).
  function combine(pl, readings) {
    var perTerm = {}, i = 0;
    while (i < pl.dice.length) {
      var d = pl.dice[i], v;
      if (d.role === 'tens') {
        var tens = readings[i].label * 10, units = readings[i + 1].label;
        v = tens + units === 0 ? 100 : tens + units;
        i += 2;
      } else {
        v = readings[i].value;
        i += 1;
      }
      (perTerm[d.term] = perTerm[d.term] || []).push(v);
    }
    var breakdown = [], total = 0, values = [];
    pl.terms.forEach(function (t, ti) {
      if (t.flat !== undefined) {
        var f = t.sign * t.flat;
        breakdown.push({ term: (t.sign < 0 ? '-' : '+') + t.flat, sum: f });
        total += f;
      } else {
        var rolls = perTerm[ti] || [], sum = rolls.reduce(function (a, b) { return a + b; }, 0) * t.sign;
        breakdown.push({ term: (t.sign < 0 ? '-' : '+') + t.count + 'd' + t.sides, rolls: rolls, sum: sum });
        total += sum;
        values = values.concat(rolls);
      }
    });
    return { breakdown: breakdown, total: total, values: values };
  }

  root.ndDiceGeometry = {
    shape: shape, readValue: readValue, faceOutline: faceOutline, rotate: rotate,
    parseNotation: parseNotation, plan: plan, combine: combine, SUPPORTED: SUPPORTED, MAX_DICE: MAX_DICE,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndDiceGeometry;
})(typeof window !== 'undefined' ? window : globalThis);
