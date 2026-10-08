// Writes scene.json (a Foundry v13 scene made by static/js/map-export.js from a small building) for check.ts to type-check.
//   node make_scene.js            (run from this folder)
global.window = global;
const E = require('../../../static/js/map-export.js');
const P = (x, y) => [x * 50, y * 50];
const walls = [
  { id: 'a', kind: 'wall', pts: [P(2, 2), P(9, 2), P(9, 5), P(2, 5), P(2, 2)] }, { id: 'm', kind: 'wall', pts: [P(6, 2), P(6, 3.2)] },
  { id: 'd', kind: 'door', state: 'closed', pts: [P(6, 3.2), P(6, 4.2)] }, { id: 's', kind: 'secret', state: 'open', pts: [P(3, 5), P(4, 5)] },
  { id: 'w', kind: 'window', pts: [P(4, 2), P(5, 2)] }];
const lights = [{ x: 200, y: 200, range: 6, color: '#ff9933', intensity: 0.9, on: true }, { x: 400, y: 300, range: 4, on: false }];
const r = E.toFoundryScene({ name: 'T', canvasW: 600, canvasH: 400, cell: 50, hasGrid: true, walls, lights, darkness: 0.4, imageSrc: 'a.png', gridDistance: 5, gridUnits: 'ft', ppg: 70 });
require('fs').writeFileSync('scene.json', JSON.stringify(r.scene));
console.log('own validator:', E.validateScene(r.scene));
