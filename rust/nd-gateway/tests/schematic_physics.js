const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const script = fs.readFileSync(path.join(__dirname, '../../../static/js/schematic-physics.js'), 'utf8');
const sandbox = { module: { exports: {} }, exports: {}, console };
vm.runInNewContext(script, sandbox);
const physics = sandbox.module.exports;

test('schematic physics decisions normalize integer room state and step bounds', () => {
  const state = physics.normalizeState({
    enabled: true,
    revision: 7,
    tick: 2,
    rooms: {
      '1': { water: 999, fire: -4, gas: 12.9, heat: 'bad', pressure: 40 },
    },
  });
  assert.equal(state.version, 1);
  assert.equal(state.enabled, true);
  assert.equal(state.revision, 7);
  assert.equal(state.rooms['1'].water, 100);
  assert.equal(state.rooms['1'].fire, 0);
  assert.equal(state.rooms['1'].gas, 12);
  assert.equal(state.rooms['1'].heat, 0);
  assert.equal(state.rooms['1'].pressure, 40);
  assert.equal(physics.normalizeSteps(1), 1);
  assert.equal(physics.normalizeSteps(16), 16);
  assert.throws(() => physics.normalizeSteps(0), /between 1 and 16/);
  assert.throws(() => physics.normalizeSteps(17), /between 1 and 16/);
});

test('schematic physics ticks are simultaneous and closed edges do not spread', () => {
  const open = physics.normalizeState({
    enabled: true,
    revision: 3,
    tick: 0,
    rooms: {
      '1': { water: 0, fire: 100, gas: 0, heat: 100, pressure: 0 },
      '2': { water: 0, fire: 0, gas: 0, heat: 0, pressure: 0 },
    },
    graph: {
      nodes: [{ id: '1', bounds: [0, 0, 50, 50] }, { id: '2', bounds: [50, 0, 50, 50] }],
      edges: [{ a: '1', b: '2', open: true }],
    },
  });
  const next = physics.tickState(open, 1);
  assert.equal(next.rooms['1'].heat, 75);
  assert.equal(next.rooms['2'].heat, 25);
  assert.ok(next.rooms['1'].fire < 100);
  assert.ok(next.rooms['2'].fire > 0);
  assert.equal(next.tick, 1);
  assert.equal(next.revision, 4);

  const closed = physics.normalizeState({
    enabled: true,
    revision: 3,
    rooms: {
      '1': { heat: 100, fire: 100 },
      '2': { heat: 0, fire: 0 },
    },
    graph: { nodes: [], edges: [{ a: '1', b: '2', open: false }] },
  });
  const stayed = physics.tickState(closed, 1);
  assert.equal(stayed.rooms['1'].heat, 100);
  assert.equal(stayed.rooms['2'].heat, 0);
  assert.ok(stayed.rooms['1'].fire < 100);
  assert.equal(stayed.rooms['2'].fire, 0);
});
