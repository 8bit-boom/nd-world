'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { makeRng, hashSeed } = require('./rng.js');

test('the same seed gives the same sequence, another seed another one', () => {
  const a = makeRng(42), b = makeRng(42), c = makeRng(43);
  const sa = Array.from({ length: 8 }, () => a.next()), sb = Array.from({ length: 8 }, () => b.next()), sc = Array.from({ length: 8 }, () => c.next());
  assert.deepEqual(sa, sb);
  assert.notDeepEqual(sa, sc);
});

test('a word works as a seed and always means the same number', () => {
  assert.equal(hashSeed('smugglers-den'), hashSeed('smugglers-den'));
  assert.notEqual(hashSeed('smugglers-den'), hashSeed('smugglers-dan'));
  assert.deepEqual(makeRng('x').next(), makeRng('x').next());
});

test('int() is inclusive at both ends and stays in range', () => {
  const r = makeRng(1), seen = new Set();
  for (let i = 0; i < 2000; i++) { const v = r.int(3, 6); assert.ok(v >= 3 && v <= 6); seen.add(v); }
  assert.deepEqual([...seen].sort(), [3, 4, 5, 6]);
});

test('shuffle keeps every element and does not touch its input', () => {
  const r = makeRng(5), src = [1, 2, 3, 4, 5, 6], out = r.shuffle(src);
  assert.deepEqual(src, [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(out.slice().sort(), src);
});
