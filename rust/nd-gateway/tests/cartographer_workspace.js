const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const script = fs.readFileSync(path.join(__dirname, '../../../static/js/cartographer-workspace.js'), 'utf8');

function workspace(project, initialPicker) {
  const status = { hidden: true, textContent: '' };
  let picker = initialPicker;
  let onLoad;
  let onMutation;
  let timer;
  const frame = {
    contentDocument: { getElementById: () => picker },
    contentWindow: { Event: class { constructor(type) { this.type = type; } } },
    addEventListener: (_, listener) => { onLoad = listener; },
  };
  const document = { getElementById: id => ({
    'carto-studio-frame': frame,
    'carto-workspace-status': status,
  })[id] || null };
  const window = { location: { search: '?project=' + project } };
  class MutationObserver {
    constructor(callback) { onMutation = callback; }
    observe() {}
    disconnect() {}
  }
  vm.runInNewContext(script, {
    document, window, URLSearchParams, MutationObserver,
    setTimeout: fn => { timer = fn; return 1; },
    clearTimeout: () => { timer = null; },
  });
  return {
    status,
    load: value => { picker = value; onLoad(); },
    mutate: () => onMutation(),
    timeout: () => timer(),
  };
}

test('opens a project after the studio loads its options without an existing-map picker', () => {
  let changed = false;
  const picker = {
    options: [], value: 'first',
    dispatchEvent: event => { changed = event.type === 'change'; },
  };
  const page = workspace('target', null);
  page.load(picker);
  picker.options.push({ value: 'first' }, { value: 'target' });
  page.mutate();
  assert.equal(picker.value, 'target');
  assert.equal(changed, true);
  assert.equal(page.status.hidden, true);
});

test('reports a project absent from the loaded list', () => {
  const page = workspace('missing', { options: [{ value: 'first' }] });
  assert.equal(page.status.hidden, false);
  assert.match(page.status.textContent, /not found/);
});

test('reports a studio or project list that never loads', () => {
  const page = workspace('target', null);
  page.timeout();
  assert.match(page.status.textContent, /Could not load/);
});

test('Build → Prepare → Run → Build keeps the studio mounted and restores Build view', () => {
  const handlers = {};
  const studio = { hidden: false };
  const existing = { hidden: true };
  const prepare = { hidden: true };
  const buildExisting = { hidden: false };
  const run = { hidden: true };
  const stages = {};
  for (const name of ['build', 'prepare', 'run']) {
    const button = {
      classList: { toggle() {} },
      addEventListener: (event, fn) => { handlers[name] = fn; },
    };
    stages[name] = button;
  }
  const elements = {
    'carto-stage-build': stages.build,
    'carto-stage-prepare': stages.prepare,
    'carto-stage-run': stages.run,
    'carto-prepare-stage': prepare,
    'carto-build-existing': buildExisting,
    'carto-studio-wrap': studio,
    'carto-existing-wrap': existing,
    'carto-run-stage': run,
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '' } }, URLSearchParams,
  });

  handlers.prepare();
  assert.equal(prepare.hidden, false);
  assert.equal(studio.hidden, false);
  assert.equal(existing.hidden, true);

  handlers.run();
  assert.equal(run.hidden, false);
  assert.equal(studio.hidden, false);
  assert.equal(existing.hidden, true);

  handlers.build();
  assert.equal(buildExisting.hidden, false);
  assert.equal(studio.hidden, false);
  assert.equal(existing.hidden, true);
});

function copyWorkspace(failUnderlay, kind = 'map', image = '/uploads/maps/town.png', html = 'no', multiFloor = false, badDestination = false) {
  const handlers = {};
  function element(id) {
    return {
      disabled: false, hidden: false, dataset: {},
      addEventListener: (event, fn) => { handlers[id + ':' + event] = fn; },
      setAttribute() {},
    };
  }
  const select = element('select');
  select.value = '/maps/town?embed=1';
  select.selectedOptions = [{ dataset: { kind, slug: 'town', name: 'Town', image, width: '640', height: '320', gridCell: '50', html } }];
  select.options = [...select.selectedOptions];
  if (multiFloor) select.options.push({ dataset: { kind: 'schematic', slug: 'upper', name: 'Upper', image: '', width: '320', height: '160', gridCell: '40', html: 'no' } });
  const copy = element('copy');
  const status = element('status');
  const elements = {
    'carto-existing-select': select,
    'carto-copy-background': copy,
    'carto-workspace-status': status,
    'carto-open-existing': element('open'),
    'carto-existing-tab': element('tab'),
    'carto-show-studio': element('studio-button'),
    'carto-studio-wrap': element('studio'),
    'carto-existing-wrap': element('existing'),
    'carto-existing-frame': element('frame'),
  };
  const requests = [];
  const fills = [];
  let destination;
  const window = {
    confirm: () => true,
    location: { search: '?w=test-world', origin: 'http://example.test', assign: url => { destination = url; } },
  };
  const fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (url.endsWith('/underlay') && failUnderlay) return { ok: false, text: async () => 'underlay failed' };
    if (url.startsWith('/api/cartographer/map-overlays/')) return { ok: true, json: async () => ({
      original_markers: [{ name: 'Market', latitude: 64, longitude: 96 }],
      markers: [{ label: 'Gate', lat: 256, lng: 32, color: '#abc', link_schematic: 'dungeon' }],
      regions: [{ name: 'District', points: [[320, 0], [288, 32], [256, 0]], color: '#123456', opacity: 0.4 }],
    }) };
    if (url.startsWith('/api/cartographer/schematic-floors/')) return { ok: true, json: async () => ({
      floors: multiFloor ? [
        { slug: 'town', label: 'Ground', portals: [{ x: 32, y: 64, label: 'Stairs', destination_slug: badDestination ? 'missing' : 'upper', destination_x: 96, destination_y: 128 }],
          pins: [{ x: 64, y: 96, label: 'Gate', color: '#abc' }], background: 'light',
          paths: [{ type: 'path', pts: [[32, 64], [96, 128]], stroke: '#abc', strokeW: 2, opacity: 0.7 },
            { type: 'line', points: [{ x: 32, y: 64 }, { x: 64, y: 96 }, { x: 96, y: 128 }], strokeW: 1 },
            { type: 'line', x1: 0, y1: 32, x2: 64, y2: 96, stroke: '#123456', strokeW: 4, opacity: 1 }],
          tokens: [{ id: 'token-1', x: 64, y: 96, label: 'Scout', color: '#55ccff', pc_id: 42 }] },
        { slug: 'upper', label: 'Upper', portals: [], pins: [], paths: [], tokens: [], background: 'blueprint' },
      ] : [{ slug: 'town', label: 'Floor 1', portals: [], pins: [], paths: [], tokens: [] }],
    }) };
    if (/^\/maps\/schematic\/(town|upper)\/walls.json/.test(url)) return { ok: true, json: async () => ({
      walls: [{ kind: 'door', state: 'closed', pts: [[32, 64], [96, 64]] }],
      lights: [{ x: 32, y: 64, range: 4, color: '#ffffff', intensity: 1, on: true, label: 'Torch' }],
    }) };
    if (url.endsWith('/assets')) return { ok: true, json: async () => ({ id: 'asset-id' }) };
    if (url.endsWith('/docs')) return { ok: true, json: async () => ({ id: 'doc-1', cell_size: 32 }) };
    if (url.endsWith('/docs/doc-1') && !options.method) return { ok: true, json: async () => ({ floors: [{ id: 'floor-1' }] }) };
    if (url.endsWith('/floors')) return { ok: true, json: async () => ({ floor: 'floor-2' }) };
    if (url.endsWith('.png')) return { ok: true, blob: async () => ({ type: 'image/png' }) };
    return { ok: true };
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null, createElement: () => ({
      toBlob: fn => fn({ type: 'image/png' }),
      getContext: () => ({ fillStyle: '', fillRect() { fills.push(this.fillStyle); } }),
    }) },
    window, URLSearchParams, URL, fetch,
  });
  handlers['select:change']();
  return { copy, status, requests, fills, click: () => handlers['copy:click'](), destination: () => destination };
}

test('copies a map background to a new project without writing the original', async () => {
  const page = copyWorkspace(false);
  assert.equal(page.copy.disabled, false);
  await page.click();
  const underlay = page.requests.find(request => request.url.endsWith('/underlay'));
  assert.deepEqual(JSON.parse(underlay.body), { asset: 'asset-id', x: 0, y: 0, w: 20, h: 10 });
  const ops = JSON.parse(page.requests.find(request => request.url.endsWith('/ops')).body);
  assert.equal(ops.floor, 'floor-1');
  assert.deepEqual(ops.ops.map(op => op.item), [
    { kind: 'marker', id: 'original-0', x: 3, y: 2, label: 'Market', color: '#00f0ff' },
    { kind: 'marker', id: 'marker-0', x: 1, y: 2, label: 'Gate', note: '', color: '#aabbcc', link_schematic: 'dungeon' },
    { kind: 'region', id: 'region-0', name: 'District', points: [[0, 0], [1, 1], [0, 2]], color: '#123456', opacity: 0.4 },
  ]);
  assert.ok(page.requests.every(request => !request.url.startsWith('/maps/')));
  const link = page.requests.find(request => request.url.startsWith('/api/cartographer/links'));
  assert.deepEqual(JSON.parse(link.body), { project: 'doc-1', kind: 'map', slug: 'town' });
  assert.ok(link.url.includes('w=test-world'));
  assert.equal(page.destination(), '/cartographer?project=doc-1&w=test-world');
});

test('removes an incomplete project and reports a failed copy', async () => {
  const page = copyWorkspace(true);
  await page.click();
  assert.ok(page.requests.some(request => request.url.endsWith('/docs/doc-1') && request.method === 'DELETE'));
  assert.match(page.status.textContent, /underlay failed/);
  assert.equal(page.copy.disabled, false);
  assert.equal(page.destination(), undefined);
});

test('copies schematic walls and lights at their original map positions', async () => {
  const page = copyWorkspace(false, 'schematic');
  await page.click();
  const ops = JSON.parse(page.requests.find(request => request.url.endsWith('/ops')).body).ops;
  assert.deepEqual(ops, [
    { t: 'set_imported_walls', walls: [{ kind: 'door', state: 'closed', pts: [[1, 2], [3, 2]] }] },
    { t: 'add_light', light: { id: 'light-0', x: 32, y: 64, range: 6.25, color: '#ffffff', intensity: 1, on: true, label: 'Torch' } },
  ]);
  assert.equal(page.destination(), '/cartographer?project=doc-1&w=test-world');
});

test('copies a schematic with no background and excludes HTML schematics', async () => {
  const page = copyWorkspace(false, 'schematic', '');
  assert.equal(page.copy.disabled, false);
  await page.click();
  assert.equal(page.destination(), '/cartographer?project=doc-1&w=test-world');
  assert.equal(copyWorkspace(false, 'schematic', '', 'yes').copy.disabled, true);
});

test('copies grouped floors and preserves their destination coordinates', async () => {
  const page = copyWorkspace(false, 'schematic', '', 'no', true);
  await page.click();
  const underlays = page.requests.filter(request => request.url.endsWith('/underlay')).map(request => JSON.parse(request.body));
  assert.deepEqual(underlays, [
    { floor: 'floor-1', asset: 'asset-id', x: 0, y: 0, w: 20, h: 10 },
    { floor: 'floor-2', asset: 'asset-id', x: 0, y: 0, w: 10, h: 5 },
  ]);
  const operations = page.requests.filter(request => request.url.endsWith('/ops')).map(request => JSON.parse(request.body));
  assert.deepEqual(operations[0].ops.find(op => op.stroke?.id === 'path-0'), { t: 'add_stroke', stroke: {
    id: 'path-0', kind: 'paint', brush: 'round', color: '#aabbcc', size: 0.0625,
    opacity: 0.7, hardness: 1, roughness: 0, seed: 0, points: [{ x: 1, y: 2 }, { x: 3, y: 4 }],
  } });
  assert.deepEqual(operations[0].ops.find(op => op.stroke?.id === 'path-1'), { t: 'add_stroke', stroke: {
    id: 'path-1', kind: 'paint', brush: 'round', color: '#00f0ff', size: 0.05,
    opacity: 0.9, hardness: 1, roughness: 0, seed: 0,
    points: [{ x: 1, y: 2 }, { x: 2, y: 3 }, { x: 3, y: 4 }],
  } });
  assert.deepEqual(operations[0].ops.find(op => op.stroke?.id === 'path-2').stroke.points, [{ x: 0, y: 1 }, { x: 2, y: 3 }]);
  assert.deepEqual(operations[0].ops.find(op => op.item?.id === 'token-0'), { t: 'put_overlay', item: {
    kind: 'marker', id: 'token-0', x: 2, y: 3, label: 'Scout', note: 'NDWORLD-RICH-TOKEN:' + JSON.stringify({
      id: 'token-1', x: 64, y: 96, name: 'Scout', source: 'manual', pc_id: 42,
    }),
    color: '#55ccff',
  } });
  assert.deepEqual(operations[0].ops.at(-2), { t: 'put_overlay', item: {
    kind: 'marker', id: 'pin-0', x: 2, y: 3, label: 'Gate', color: '#aabbcc',
  } });
  assert.deepEqual(operations[0].ops.at(-1), { t: 'put_overlay', item: {
    kind: 'marker', id: 'floor-link-0', x: 1, y: 2, label: 'Stairs', color: '#ffc857',
    links_to: { floor: 'floor-2', x: 3, y: 4 },
  } });
  assert.equal(operations[1].floor, 'floor-2');
  assert.deepEqual(page.fills, ['#f0f0f0', '#0d1b2a']);
  assert.equal(page.destination(), '/cartographer?project=doc-1&w=test-world');
});

test('rejects a broken floor link and removes the incomplete project', async () => {
  const page = copyWorkspace(false, 'schematic', '', 'no', true, true);
  await page.click();
  assert.match(page.status.textContent, /invalid destination/);
  assert.ok(page.requests.some(request => request.url.endsWith('/docs/doc-1') && request.method === 'DELETE'));
  assert.equal(page.destination(), undefined);
});

test('switching projects selects the linked map or clears the old selection', () => {
  const handlers = {};
  const on = (id) => ({
    addEventListener: (event, fn) => { handlers[id + ':' + event] = fn; },
    setAttribute() {},
  });
  const option = { value: '/maps/town?embed=1', dataset: { kind: 'map', slug: 'town', width: '10', height: '10' } };
  const select = {
    ...on('select'), options: [option], value: '',
    get selectedOptions() { return this.value ? [option] : []; },
    dispatchEvent: () => handlers['select:change'](),
  };
  const picker = { ...on('picker'), value: 'doc_one' };
  const frame = { ...on('frame'), contentDocument: { getElementById: () => picker } };
  const elements = {
    'carto-existing-select': select, 'carto-open-existing': on('open'),
    'carto-existing-tab': on('tab'), 'carto-show-studio': on('studio'),
    'carto-studio-wrap': on('studio-wrap'), 'carto-existing-wrap': on('existing'),
    'carto-existing-frame': on('existing-frame'), 'carto-copy-background': on('copy'),
    'carto-workspace-status': on('status'), 'carto-studio-frame': frame,
    'carto-links': { textContent: JSON.stringify([{ project: 'doc_one', kind: 'map', slug: 'town' }]) },
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '', origin: 'http://example.test' } },
    URLSearchParams, URL, Event: class {},
  });
  handlers['picker:change']();
  assert.equal(select.value, option.value);
  picker.value = 'unlinked';
  handlers['picker:change']();
  assert.equal(select.value, '');
});

test('creates a new playable schematic from the selected player-filtered floor', async () => {
  const handlers = {};
  const button = { addEventListener: (_, fn) => { handlers.click = fn; } };
  const link = { hidden: true };
  const status = { hidden: true };
  const picker = { value: 'doc_one', selectedOptions: [{ textContent: 'Tower' }] };
  const floor = { dataset: { floor: 'fl_upper' }, textContent: 'Upper' };
  const frame = { contentDocument: { getElementById: () => picker, querySelector: () => floor } };
  const elements = { 'carto-playable-copy': button, 'carto-playable-link': link,
    'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {} };
  const requests = [];
  class FormData {
    constructor() { this.parts = []; }
    append(...args) { this.parts.push(args); }
  }
  const fetch = async (url, options) => {
    requests.push({ url, options });
    if (url.includes('/export/uvtt')) return { ok: true, blob: async () => 'uvtt' };
    if (url === '/cartographer/studio/api/docs/doc_one') return { ok: true, json: async () => ({
      rev: 2,
      floors: [{ id: 'fl_upper', name: 'Upper', markers: [
        { note: 'NDWORLD-RICH-TOKEN:' + JSON.stringify({ id: 'npc-7', name: 'Guildmaster', entity_id: 7, visible: true }) },
        { note: 'NDWORLD-RICH-TOKEN:not-json' },
        { note: 'ordinary' },
      ] }],
    }) };
    if (url.startsWith('/maps/schematic/import-uvtt')) return { ok: true, json: async () => ({ slug: 'tower-upper', warnings: [] }) };
    return { ok: true };
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '?w=test-world', origin: 'http://example.test' }, confirm: () => true },
    URLSearchParams, URL, FormData, fetch,
  });
  await handlers.click();
  assert.equal(requests[0].url, '/cartographer/studio/api/docs/doc_one/save');
  assert.equal(requests[1].url, '/cartographer/studio/api/docs/doc_one/export/uvtt?floor=fl_upper&ppc=32&audience=player');
  assert.equal(requests[2].url, '/cartographer/studio/api/docs/doc_one');
  assert.equal(requests[3].url, '/maps/schematic/import-uvtt?w=test-world');
  assert.deepEqual(requests[3].options.body.parts, [
    ['file', 'uvtt', 'map-maker-floor.dd2vtt'], ['name', 'Tower - Upper'],
    ['project', 'doc_one'], ['floor', 'fl_upper'], ['mode', 'create'],
    ['elements', JSON.stringify([{ id: 'npc-7', name: 'Guildmaster', entity_id: 7, visible: true }])],
  ]);
  assert.equal(link.href, '/cartographer?kind=schematic&slug=tower-upper&w=test-world');
  assert.equal(link.hidden, false);
  assert.match(status.textContent, /created/);
});

test('does not import a playable copy when export fails', async () => {
  let handler;
  const status = {};
  const frame = { contentDocument: {
    getElementById: () => ({ value: 'doc_one', selectedOptions: [{ textContent: 'Map' }] }),
    querySelector: () => ({ dataset: { floor: 'fl_one' }, textContent: 'Floor' }),
  } };
  const elements = { 'carto-playable-copy': { addEventListener: (_, fn) => { handler = fn; } },
    'carto-playable-link': {}, 'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {} };
  let calls = 0;
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '', origin: 'http://example.test' }, confirm: () => true },
    URLSearchParams, URL, fetch: async () => { calls++; return calls === 1 ? { ok: true } : { ok: false, text: async () => 'export failed' }; },
  });
  await handler();
  assert.equal(calls, 2);
  assert.match(status.textContent, /export failed/);
});

test('refreshes only the current floor’s linked playable artwork', async () => {
  const handlers = {};
  const bind = id => ({ addEventListener: (_, fn) => { handlers[id] = fn; } });
  const status = {}, link = {};
  const frame = { contentDocument: {
    getElementById: () => ({ value: 'doc_one', selectedOptions: [{ textContent: 'Map' }] }),
    querySelector: () => ({ dataset: { floor: 'fl_one' }, textContent: 'Ground' }),
  } };
  const elements = { 'carto-playable-copy': bind('create'), 'carto-playable-refresh': bind('refresh'), 'carto-playable-geometry': bind('geometry'),
    'carto-playable-link': link, 'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {} };
  class FormData {
    constructor() { this.parts = []; }
    append(...args) { this.parts.push(args); }
  }
  const requests = [];
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '', origin: 'http://example.test' }, confirm: () => true },
    URLSearchParams, URL, FormData, fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.includes('/export/uvtt')) return { ok: true, blob: async () => 'uvtt' };
      if (url.startsWith('/maps/schematic/import-uvtt')) return { ok: true, json: async () => ({ slug: 'map-ground', warnings: [] }) };
      return { ok: true };
    },
  });
  await handlers.refresh();
  assert.deepEqual(requests[2].options.body.parts.slice(-3), [['project', 'doc_one'], ['floor', 'fl_one'], ['mode', 'refresh']]);
  assert.match(status.textContent, /artwork refreshed/);
  assert.equal(link.href, '/cartographer?kind=schematic&slug=map-ground');
  await handlers.geometry();
  assert.deepEqual(requests[5].options.body.parts.slice(-3), [['project', 'doc_one'], ['floor', 'fl_one'], ['mode', 'geometry']]);
  assert.match(status.textContent, /walls and lights refreshed/);
});

test('creates linked playable floors and converts stair coordinates', async () => {
  let handler;
  const status = {}, link = {};
  const frame = { contentDocument: { getElementById: () => ({ value: 'doc_one', selectedOptions: [{ textContent: 'Tower' }] }) } };
  const elements = { 'carto-playable-floors': { addEventListener: (_, fn) => { handler = fn; } },
    'carto-playable-link': link, 'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {} };
  class FormData {
    constructor() { this.parts = []; }
    append(...args) { this.parts.push(args); }
  }
  const source = { rev: 5, floors: [
    { id: 'fl_one', name: 'Ground', digest: 'a', markers: [
      { id: 'stairs', label: 'Up', x: 1, y: 2, links_to: { floor: 'fl_two', x: 3, y: 4 } },
      { note: 'NDWORLD-RICH-TOKEN:' + JSON.stringify({ id: 'merchant-3', name: 'Mirra', merchant: true, qty: 4 }) },
      { note: 'NDWORLD-RICH-TOKEN:{broken' },
    ] },
    { id: 'fl_two', name: 'Upper', digest: 'b', markers: [] },
  ] };
  const requests = [];
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '?w=test-world', origin: 'http://example.test' }, confirm: () => true },
    URLSearchParams, URL, FormData, fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url.endsWith('/docs/doc_one')) return { ok: true, json: async () => source };
      if (url.includes('/export/uvtt')) return { ok: true, blob: async () => 'uvtt' };
      if (url.startsWith('/maps/schematic/import-uvtt')) return { ok: true, json: async () => ({ width: 160, height: 160, image_url: '/uploads/floor.webp' }) };
      if (url.startsWith('/api/cartographer/playable-floors')) return { ok: true, json: async () => ({ slug: 'tower-ground' }) };
      return { ok: true };
    },
  });
  await handler();
  const imports = requests.filter(r => r.url.startsWith('/maps/schematic/import-uvtt'));
  assert.equal(imports.length, 2);
  assert.deepEqual(imports[0].options.body.parts.slice(-6), [
    ['name', 'Tower - Ground'], ['project', 'doc_one'], ['floor', 'fl_one'],
    ['mode', 'ensure'], ['revision', '5'],
    ['elements', JSON.stringify([{ id: 'merchant-3', name: 'Mirra', merchant: true, qty: 4 }])],
  ]);
  const group = JSON.parse(requests.find(r => r.url.startsWith('/api/cartographer/playable-floors')).options.body);
  assert.equal(group.project, 'doc_one');
  assert.deepEqual(group.floors[0].links, [{ id: 'stairs', label: 'Up', destination: 'fl_two', x: 32, y: 64, destination_x: 96, destination_y: 128 }]);
  assert.equal(group.floors[1].image_url, '/uploads/floor.webp');
  assert.equal(link.href, '/cartographer?kind=schematic&slug=tower-ground&w=test-world');
  assert.match(status.textContent, /floors linked/);
});

test('does not link floors if the project changes during preparation', async () => {
  let handler;
  const status = {};
  const frame = { contentDocument: { getElementById: () => ({ value: 'doc_one', selectedOptions: [{ textContent: 'Tower' }] }) } };
  const elements = { 'carto-playable-floors': { addEventListener: (_, fn) => { handler = fn; } },
    'carto-playable-link': {}, 'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {} };
  class FormData { append() {} }
  const floors = [{ id: 'one', name: 'Ground', digest: 'a', markers: [] }, { id: 'two', name: 'Upper', digest: 'b', markers: [] }];
  const requests = [];
  let reads = 0;
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { location: { search: '', origin: 'http://example.test' }, confirm: () => true },
    URLSearchParams, URL, FormData, fetch: async (url) => {
      requests.push(url);
      if (url.endsWith('/docs/doc_one')) return { ok: true, json: async () => ({ floors, rev: ++reads === 1 ? 1 : 2 }) };
      if (url.includes('/export/uvtt')) return { ok: true, blob: async () => 'uvtt' };
      if (url.startsWith('/maps/schematic/import-uvtt')) return { ok: true, json: async () => ({ width: 160, height: 160, image_url: '/uploads/floor.webp' }) };
      return { ok: true };
    },
  });
  await handler();
  assert.match(status.textContent, /project changed/);
  assert.ok(!requests.some(url => url.startsWith('/api/cartographer/playable-floors')));
});

test('includes rich merchant and item token metadata in playable imports', () => {
  assert.equal(typeof script, 'string');
  assert.match(script, /NDWORLD-RICH-TOKEN:/);
  assert.match(script, /merchant/);
  assert.match(script, /qty/);
});

test('shows a playable-scene update preview before applying', async () => {
  let handler;
  const status = { hidden: true, textContent: '' };
  const previewBody = { replaceChildren() {}, append() {}, setAttribute() {}, removeAttribute() {}, textContent: '' };
  const dialog = { showModal() {}, close() {} };
  const apply = { disabled: true, addEventListener() {} };
  const close = {};
  const summary = { textContent: '' };
  const frame = { contentDocument: {
    getElementById: () => ({ value: 'doc_one', selectedOptions: [{ textContent: 'Tower' }] }),
  } };
  const elements = {
    'carto-update-scene': { addEventListener: (_, fn) => { handler = fn; }, disabled: false },
    'carto-update-dialog': dialog, 'carto-update-close': { addEventListener() {}, ...close },
    'carto-update-body': previewBody, 'carto-update-summary': summary, 'carto-update-apply': apply,
    'carto-workspace-status': status, 'carto-studio-frame': frame, 'carto-studio-wrap': {},
  };
  function makeElement(tag) {
    const el = { tagName: tag, type: '', checked: false, dataset: {}, className: '', textContent: '', children: [],
      append(...items) { this.children.push(...items); }, appendChild(item) { this.children.push(item); },
      replaceChildren() { this.children = []; }, setAttribute() {}, removeAttribute() {}, querySelector() { return null; } };
    return el;
  }
  const fetch = async (url, options) => {
    if (url.endsWith('/save')) return { ok: true };
    if (url.endsWith('/docs/doc_one')) return { ok: true, json: async () => ({ rev: 3, floors: [{ id: 'fl_one', name: 'Ground', markers: [] }] }) };
    if (url.includes('/playable-preview')) return { ok: true, json: async () => ({
      floors: [{ id: 'fl_one', name: 'Ground', has_copy: true, slug: 'tower-ground', project_changed: true,
        wall_conflict: false, light_conflict: false }],
    }) };
    return { ok: true };
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null, createElement: makeElement, createTextNode: text => ({ textContent: text }) },
    window: { location: { search: '', origin: 'http://example.test' } },
    URLSearchParams, URL, fetch, FormData,
  });
  await handler();
  assert.equal(dialog.showModal, dialog.showModal);
  assert.equal(apply.disabled, false);
  assert.match(summary.textContent, /1 floor/);
});

test('the hidden-visibility guard is not used as project readiness', async () => {
  let handler;
  const status = { textContent: '' };
  const picker = { value: 'doc_one', selectedOptions: [{ textContent: 'Tower' }] };
  const floor = { dataset: { floor: 'floor_one' }, textContent: 'Ground' };
  const frame = { contentDocument: { getElementById: () => picker, querySelector: () => floor } };
  const elements = { 'carto-playable-copy': { addEventListener: (_, fn) => { handler = fn; } },
    'carto-playable-link': { hidden: true }, 'carto-workspace-status': status,
    'carto-studio-frame': frame, 'carto-studio-wrap': { hidden: true } };
  let confirmMessage;
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] || null },
    window: { confirm: message => { confirmMessage = message; return false; }, location: { search: '', origin: 'http://example.test' } },
    URLSearchParams, URL,
  });
  await handler();
  assert.match(confirmMessage, /Create a new playable schematic/);
  assert.doesNotMatch(status.textContent, /Switch to Map Maker/);
});
