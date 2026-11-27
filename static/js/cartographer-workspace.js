(function () {
  const params = new URLSearchParams(window.location.search);
  const project = params.get('project');
  function setWorkflowStage(stage) {
    const stages = ['build', 'prepare', 'run'];
    stages.forEach(name => {
      const button = document.getElementById('carto-stage-' + name);
      if (button) button.classList.toggle('active', name === stage);
    });
    const prepare = document.getElementById('carto-prepare-stage');
    const buildExisting = document.getElementById('carto-build-existing');
    const studio = document.getElementById('carto-studio-wrap');
    const existing = document.getElementById('carto-existing-wrap');
    const run = document.getElementById('carto-run-stage');
    if (prepare) prepare.hidden = stage !== 'prepare';
    if (buildExisting) buildExisting.hidden = stage !== 'build';
    if (stage === 'prepare' || stage === 'run') {
      if (studio) studio.hidden = true;
      if (existing) existing.hidden = true;
    }
    if (run) run.hidden = stage !== 'run';
  }
  ['build', 'prepare', 'run'].forEach(stage => {
    const button = document.getElementById('carto-stage-' + stage);
    if (button) button.addEventListener('click', () => setWorkflowStage(stage));
  });
  setWorkflowStage(project ? 'build' : 'build');
  function setRunTarget(slug) {
    window.cartoRunSlug = slug;
    const open = document.getElementById('carto-run-open');
    if (open) {
      open.href = '/maps/schematic/' + encodeURIComponent(slug) + '/view';
      open.hidden = false;
    }
  }
  const runPlay = document.getElementById('carto-run-play');
  if (runPlay) runPlay.addEventListener('click', async () => {
    const slug = window.cartoRunSlug;
    if (!slug) {
      const status = document.getElementById('carto-workspace-status');
      status.hidden = false;
      status.textContent = 'Prepare a playable copy first.';
      return;
    }
    const popup = window.open('', '_blank');
    const status = document.getElementById('carto-workspace-status');
    runPlay.disabled = true;
    try {
      const response = await fetch('/api/maps/schematic/' + encodeURIComponent(slug) + '/play', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Could not start the playable scene.');
      if (popup) {
        popup.location.href = data.tracker || data.player_view || '/maps/schematic/' + slug + '/view';
      }
      status.hidden = false;
      status.textContent = 'Playable scene started.';
      setWorkflowStage('run');
    } catch (error) {
      if (popup) popup.close();
      status.hidden = false;
      status.textContent = 'Could not start the playable scene: ' + error.message;
    } finally {
      runPlay.disabled = false;
    }
  });
  const playable = document.getElementById('carto-playable-copy');
  const refresh = document.getElementById('carto-playable-refresh');
  const geometry = document.getElementById('carto-playable-geometry');
  async function updatePlayable(mode) {
    const status = document.getElementById('carto-workspace-status');
    const link = document.getElementById('carto-playable-link');
    if (document.getElementById('carto-studio-wrap').hidden) {
      status.hidden = false;
      status.textContent = 'Switch to Map Maker to choose the floor to copy.';
      return;
    }
    const studioDoc = document.getElementById('carto-studio-frame').contentDocument;
    const picker = studioDoc && studioDoc.getElementById('doc-select');
    const activeFloor = studioDoc && studioDoc.querySelector('#floor-tabs [role="tab"][aria-selected="true"]');
    const id = picker && picker.value, floor = activeFloor && activeFloor.dataset.floor;
    const projectName = picker && picker.selectedOptions[0] && picker.selectedOptions[0].textContent;
    const floorName = activeFloor && activeFloor.textContent;
    if (!id || !floor || !projectName || !floorName) {
      status.hidden = false;
      status.textContent = 'Wait for Map Maker to finish loading a project and floor.';
      return;
    }
    const message = mode === 'create'
      ? 'Create a new playable schematic from this floor? Finish editing first. The picture is player-filtered but may still contain visible map details. It will not update an existing map. Inspect it before sharing.'
      : mode === 'geometry'
        ? 'Replace the linked playable copy’s walls and lights with this floor’s player-filtered walls and lights? It will be refused if they were edited in the playable copy. Tokens, fog and other gameplay data stay unchanged. Refresh artwork first if it changed.'
        : 'Replace the linked playable copy’s artwork? Its walls, lights, tokens, fog and other gameplay data remain unchanged. The picture is player-filtered but may still contain visible map details; inspect it before sharing.';
    if (!window.confirm(message)) return;
    playable.disabled = true;
    if (refresh) refresh.disabled = true;
    if (geometry) geometry.disabled = true;
    if (playableFloors) playableFloors.disabled = true;
    link.hidden = true;
    status.hidden = false;
    status.textContent = mode === 'create' ? 'Creating playable copy…' : mode === 'geometry' ? 'Refreshing walls and lights…' : 'Refreshing playable artwork…';
    const base = '/cartographer/studio/api/docs/' + encodeURIComponent(id);
    async function request(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error((await response.text()) || 'HTTP ' + response.status);
      return response;
    }
    try {
      await request(base + '/save', { method: 'POST' });
      let richElements = [];
      const exported = await request(base + '/export/uvtt?floor=' + encodeURIComponent(floor) + '&ppc=32&audience=player');
      const data = await exported.blob();
      const form = new FormData();
      form.append('file', data, 'map-maker-floor.dd2vtt');
      form.append('name', projectName + ' - ' + floorName);
      form.append('project', id);
      form.append('floor', floor);
      form.append('mode', mode);
      if ((mode === 'create' || mode === 'ensure') && richElements.length) form.append('elements', JSON.stringify(richElements));
      const importUrl = new URL('/maps/schematic/import-uvtt', window.location.origin);
      if (params.get('w')) importUrl.searchParams.set('w', params.get('w'));
      const imported = await (await request(importUrl.pathname + importUrl.search, { method: 'POST', body: form })).json();
      if (typeof imported.slug !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(imported.slug)) {
        throw new Error('The server did not return a valid schematic. Check Maps before trying again.');
      }
      const destination = new URL('/cartographer', window.location.origin);
      destination.searchParams.set('kind', 'schematic');
      destination.searchParams.set('slug', imported.slug);
      if (params.get('w')) destination.searchParams.set('w', params.get('w'));
      link.href = destination.pathname + destination.search;
      link.hidden = false;
      setRunTarget(imported.slug);
      status.textContent = (mode === 'create' ? (imported.existing ? 'This floor already has a playable copy.' : 'Playable copy created.') : mode === 'geometry' ? 'Playable walls and lights refreshed.' : 'Playable artwork refreshed.') + (imported.warnings?.length ? ' ' + imported.warnings.join(' ') : '');
    } catch (error) {
      status.textContent = 'Could not ' + (mode === 'create' ? 'create playable copy: ' : mode === 'geometry' ? 'refresh walls and lights: ' : 'refresh playable artwork: ') + error.message + ' Check Maps before retrying if the connection was interrupted.';
    } finally {
      playable.disabled = false;
      if (refresh) refresh.disabled = false;
      if (geometry) geometry.disabled = false;
      if (playableFloors) playableFloors.disabled = false;
    }
  }
  if (playable) playable.addEventListener('click', () => updatePlayable('create'));
  if (refresh) refresh.addEventListener('click', () => updatePlayable('refresh'));
  if (geometry) geometry.addEventListener('click', () => updatePlayable('geometry'));
  const updateScene = document.getElementById('carto-update-scene');
  const updateDialog = document.getElementById('carto-update-dialog');
  if (updateScene && updateDialog) {
    const close = document.getElementById('carto-update-close');
    const body = document.getElementById('carto-update-body');
    const summary = document.getElementById('carto-update-summary');
    const apply = document.getElementById('carto-update-apply');
    let updateState;
    close.addEventListener('click', () => updateDialog.close());
    updateScene.addEventListener('click', async () => {
      const status = document.getElementById('carto-workspace-status');
      if (document.getElementById('carto-studio-wrap').hidden) {
        status.hidden = false;
        status.textContent = 'Switch to Map Maker to choose a project.';
        return;
      }
      const studioDoc = document.getElementById('carto-studio-frame').contentDocument;
      const picker = studioDoc && studioDoc.getElementById('doc-select');
      const id = picker && picker.value;
      const name = picker && picker.selectedOptions[0] && picker.selectedOptions[0].textContent;
      if (!id) {
        status.hidden = false;
        status.textContent = 'Wait for Map Maker to finish loading a project.';
        return;
      }
      updateScene.disabled = true;
      body.textContent = 'Inspecting linked playable floors…';
      summary.textContent = '';
      apply.disabled = true;
      async function request(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) throw new Error((await response.text()) || 'HTTP ' + response.status);
        return response;
      }
      try {
        const base = '/cartographer/studio/api/docs/' + encodeURIComponent(id);
        await request(base + '/save', { method: 'POST' });
        const source = await (await request(base)).json();
        if (!Array.isArray(source.floors) || !Number.isSafeInteger(source.rev) || source.rev < 0) {
          throw new Error('The project or revision data is unavailable.');
        }
        const previewUrl = new URL('/api/cartographer/playable-preview', window.location.origin);
        if (params.get('w')) previewUrl.searchParams.set('w', params.get('w'));
        const preview = await (await request(previewUrl.pathname + previewUrl.search, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project: id, revision: source.rev, floors: source.floors }),
        })).json();
        let latestRun = null;
        try {
          const runUrl = new URL('/api/cartographer/update-run', window.location.origin);
          if (params.get('w')) runUrl.searchParams.set('w', params.get('w'));
          latestRun = await (await request(runUrl.pathname + runUrl.search, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'status', project: id }),
          })).json();
          if (!latestRun.id || latestRun.status === 'done') latestRun = null;
        } catch (_) {}
        updateState = { id, name, source, preview, latestRun };
        body.replaceChildren();
        const note = document.createElement('p');
        note.className = 'carto-note';
        note.textContent = 'Changes affect only the approved copies. Tokens, fog, combat, and character positions are preserved. Wall or light updates stop if playable-side edits differ.';
        body.append(note);
        if (latestRun) {
          const retry = document.createElement('p');
          retry.className = 'carto-note';
          const unfinished = Array.isArray(latestRun.floors) ? latestRun.floors.filter(f => f.status !== 'done').length : 0;
          retry.textContent = 'Resumable update ' + latestRun.id + ': ' + unfinished + ' floor(s) unfinished.';
          body.append(retry);
        }
        const table = document.createElement('table');
        table.className = 'carto-update-table';
        const thead = document.createElement('thead');
        const headRow = document.createElement('tr');
        ['Floor','Playable copy','Artwork','Walls & lights','Apply'].forEach(label => {
          const th = document.createElement('th');
          th.textContent = label;
          headRow.append(th);
        });
        thead.append(headRow);
        table.append(thead);
        const tbody = document.createElement('tbody');
        let actionable = 0;
        preview.floors.forEach(floor => {
          const tr = document.createElement('tr');
          const nameCell = document.createElement('td');
          nameCell.textContent = floor.name;
          tr.append(nameCell);
          const copyCell = document.createElement('td');
          const copyBadge = document.createElement('span');
          copyBadge.className = 'carto-update-badge ' + (floor.has_copy ? 'ready' : 'conflict');
          copyBadge.textContent = floor.has_copy ? (floor.slug || 'linked') : 'missing';
          copyCell.append(copyBadge);
          tr.append(copyCell);
          const artworkCell = document.createElement('td');
          const artworkBadge = document.createElement('span');
          artworkBadge.className = 'carto-update-badge ' + (floor.project_changed ? 'change' : 'ready');
          artworkBadge.textContent = floor.project_changed ? 'changed since copy' : 'unchanged';
          artworkCell.append(artworkBadge);
          tr.append(artworkCell);
          const geometryCell = document.createElement('td');
          if (floor.wall_conflict || floor.light_conflict) {
            const conflict = document.createElement('span');
            conflict.className = 'carto-update-badge conflict';
            conflict.textContent = [floor.wall_conflict ? 'walls' : '', floor.light_conflict ? 'lights' : ''].filter(Boolean).join(' + ') + ' edited in play';
            geometryCell.append(conflict);
          } else {
            const geometryBadge = document.createElement('span');
            geometryBadge.className = 'carto-update-badge ' + (floor.project_changed ? 'change' : 'ready');
            geometryBadge.textContent = floor.project_changed ? 'update available' : 'baseline unchanged';
            geometryCell.append(geometryBadge);
          }
          tr.append(geometryCell);
          const applyCell = document.createElement('td');
          if (floor.has_copy && floor.project_changed) {
            actionable++;
            const artwork = document.createElement('label');
            const artworkBox = document.createElement('input');
            artworkBox.type = 'checkbox';
            artworkBox.checked = true;
            artworkBox.dataset.role = 'artwork';
            artworkBox.dataset.floor = floor.id;
            artwork.append(artworkBox, ' artwork ');
            applyCell.append(artwork);
            if (!floor.wall_conflict && !floor.light_conflict) {
              const geometryBox = document.createElement('input');
              geometryBox.type = 'checkbox';
              geometryBox.checked = true;
              geometryBox.dataset.role = 'geometry';
              geometryBox.dataset.floor = floor.id;
              applyCell.append(document.createTextNode(' '));
              const geometryLabel = document.createElement('label');
              geometryLabel.append(geometryBox, ' walls/lights');
              applyCell.append(geometryLabel);
            }
          } else {
            applyCell.textContent = floor.has_copy ? 'no changes' : 'create copy first';
          }
          tr.append(applyCell);
          tbody.append(tr);
        });
        table.append(tbody);
        body.append(table);
        summary.textContent = actionable ? actionable + ' floor(s) can be updated.' : 'No linked playable changes need applying.';
        apply.disabled = !actionable;
        updateDialog.showModal();
      } catch (error) {
        status.hidden = false;
        status.textContent = 'Could not inspect playable scene: ' + error.message;
      } finally {
        updateScene.disabled = false;
      }
    });
    apply.addEventListener('click', async () => {
      if (!updateState) return;
      apply.disabled = true;
      updateScene.disabled = true;
      body.setAttribute('aria-busy', 'true');
      const status = document.getElementById('carto-workspace-status');
      const base = '/cartographer/studio/api/docs/' + encodeURIComponent(updateState.id);
      const importUrl = new URL('/maps/schematic/import-uvtt', window.location.origin);
      if (params.get('w')) importUrl.searchParams.set('w', params.get('w'));
      async function request(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) throw new Error((await response.text()) || 'HTTP ' + response.status);
        return response;
      }
      const results = [];
      try {
        await request(base + '/save', { method: 'POST' });
        const approved = updateState.source.floors.map(floor => {
          const artwork = body.querySelector('input[data-role="artwork"][data-floor="' + floor.id + '"]:checked');
          const geometryChecked = body.querySelector('input[data-role="geometry"][data-floor="' + floor.id + '"]:checked');
          const modes = [];
          if (artwork) modes.push('artwork');
          if (geometryChecked) modes.push('geometry');
          return modes.length ? { id: floor.id, name: floor.name, modes } : null;
        }).filter(Boolean);
        const runUrl = new URL('/api/cartographer/update-run', window.location.origin);
        if (params.get('w')) runUrl.searchParams.set('w', params.get('w'));
        let run;
        const resumable = updateState.latestRun && Array.isArray(updateState.latestRun.floors);
        if (resumable) {
          run = updateState.latestRun;
          for (const floor of run.floors) {
            if (floor.status === 'failed' || floor.status === 'cancelled') {
              await request(runUrl.pathname + runUrl.search, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'mark', id: run.id, floor: floor.id, status: 'pending', error: null }),
              });
            }
          }
        } else {
          run = await (await request(runUrl.pathname + runUrl.search, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'start', project: updateState.id, revision: updateState.source.rev, floors: approved }),
          })).json();
        }
        let failed = 0;
        for (const floor of updateState.source.floors) {
          const prior = resumable && Array.isArray(run.floors)
            ? run.floors.find(record => record.id === floor.id)
            : null;
          if (prior && prior.status === 'done') continue;
          const artwork = body.querySelector('input[data-role="artwork"][data-floor="' + floor.id + '"]:checked');
          const geometryChecked = body.querySelector('input[data-role="geometry"][data-floor="' + floor.id + '"]:checked');
          if (!artwork && !geometryChecked) continue;
          await request(runUrl.pathname + runUrl.search, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'mark', id: run.id, floor: floor.id, status: 'running' }),
          });
          const modes = artwork ? ['refresh'] : [];
          if (geometryChecked) modes.push('geometry');
          let floorFailed = false;
          for (const mode of modes) {
            try {
              const exported = await request(base + '/export/uvtt?floor=' + encodeURIComponent(floor.id) + '&ppc=32&audience=player');
              const form = new FormData();
              form.append('file', await exported.blob(), 'map-maker-floor.dd2vtt');
              form.append('name', (updateState.name || 'Map') + ' - ' + floor.name);
              form.append('project', updateState.id);
              form.append('floor', floor.id);
              form.append('mode', mode);
              form.append('revision', String(updateState.source.rev));
              const imported = await (await request(importUrl.pathname + importUrl.search, { method: 'POST', body: form })).json();
              results.push({ floor: floor.name, mode, ok: true, warnings: imported.warnings || [] });
            } catch (error) {
              failed++;
              floorFailed = true;
              await request(runUrl.pathname + runUrl.search, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'mark', id: run.id, floor: floor.id, status: 'failed', error: error.message }),
              });
              const line = document.createElement('p');
              line.className = 'carto-note';
              line.textContent = floor.name + ' ' + (mode === 'geometry' ? 'walls/lights' : 'artwork') + ' failed: ' + error.message;
              body.append(line);
            }
          }
          if (!floorFailed) {
            await request(runUrl.pathname + runUrl.search, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action: 'mark', id: run.id, floor: floor.id, status: 'done' }),
            });
          }
        }
        summary.textContent = 'Run ' + run.id + ': ' + results.length + ' action(s) updated, ' + failed + ' failed.';
        for (const result of results) {
          const line = document.createElement('p');
          line.className = 'carto-note';
          line.textContent = result.floor + ': ' + (result.mode === 'geometry' ? 'walls/lights' : 'artwork') + ' updated.';
          body.append(line);
        }
        status.hidden = false;
        status.textContent = failed ? 'Playable scene update partially completed; retry the failed floor.' : 'Playable scene update complete.';
      } catch (error) {
        const line = document.createElement('p');
        line.className = 'carto-note';
        line.textContent = 'Update stopped safely: ' + error.message;
        body.append(line);
        status.hidden = false;
        status.textContent = 'Playable scene update stopped safely.';
      } finally {
        body.removeAttribute('aria-busy');
        apply.disabled = false;
        updateScene.disabled = false;
      }
    });
  }
  const playableFloors = document.getElementById('carto-playable-floors');
  if (playableFloors) playableFloors.addEventListener('click', async () => {
    const status = document.getElementById('carto-workspace-status');
    const link = document.getElementById('carto-playable-link');
    if (document.getElementById('carto-studio-wrap').hidden) {
      status.hidden = false;
      status.textContent = 'Switch to Map Maker to choose a project.';
      return;
    }
    const doc = document.getElementById('carto-studio-frame').contentDocument;
    const picker = doc && doc.getElementById('doc-select');
    const id = picker && picker.value;
    const name = picker && picker.selectedOptions[0] && picker.selectedOptions[0].textContent;
    if (!id || !name) {
      status.hidden = false;
      status.textContent = 'Wait for Map Maker to finish loading a project.';
      return;
    }
    if (!window.confirm('Create playable copies of all floors and link their stairs? Existing copies must already have matching artwork. Originals stay unchanged. Inspect the player-filtered pictures before sharing. If the operation fails, copies already created remain in Maps.')) return;
    playableFloors.disabled = true;
    if (playable) playable.disabled = true;
    if (refresh) refresh.disabled = true;
    if (geometry) geometry.disabled = true;
    link.hidden = true;
    status.hidden = false;
    const base = '/cartographer/studio/api/docs/' + encodeURIComponent(id);
    async function request(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error((await response.text()) || 'HTTP ' + response.status);
      return response;
    }
    try {
      await request(base + '/save', { method: 'POST' });
      const source = await (await request(base)).json();
      if (!Array.isArray(source.floors) || source.floors.length < 2 || source.floors.length > 64) throw new Error('The project must have 2–64 floors.');
      if (!Number.isSafeInteger(source.rev) || source.rev < 0) throw new Error('The project revision is unavailable.');
      const ids = new Set(source.floors.map(floor => floor.id));
      if (ids.size !== source.floors.length || source.floors.some(floor => !floor.id || !floor.name || !Array.isArray(floor.markers))) throw new Error('The project floor data is invalid.');
      const floors = source.floors.map(floor => ({
        id: floor.id, name: floor.name,
        encounters: Array.isArray(floor.encounters) ? floor.encounters.map(encounter => ({
          space: encounter.space,
          name: floor.grid && floor.grid.spaces ? (floor.grid.spaces[encounter.space]?.name || encounter.space) : encounter.space,
          creatures: Array.isArray(encounter.creatures) ? encounter.creatures : [],
          loot: Array.isArray(encounter.loot) ? encounter.loot : [],
        })) : [],
        links: floor.markers.filter(marker => marker.links_to).map(marker => {
          const dest = marker.links_to;
          if (!ids.has(dest.floor) || dest.floor === floor.id) throw new Error('A floor link has an invalid destination.');
          const coords = [marker.x, marker.y, dest.x == null ? marker.x : dest.x, dest.y == null ? marker.y : dest.y];
          if (coords.some(n => typeof n !== 'number' || !Number.isFinite(n))) throw new Error('A floor link has invalid coordinates.');
          return { id: marker.id, label: marker.label || 'Stairs', destination: dest.floor,
            x: coords[0] * 32, y: coords[1] * 32, destination_x: coords[2] * 32, destination_y: coords[3] * 32 };
        }),
      }));
      const importUrl = new URL('/maps/schematic/import-uvtt', window.location.origin);
      if (params.get('w')) importUrl.searchParams.set('w', params.get('w'));
      const warnings = [];
      for (const floor of floors) {
        status.textContent = 'Preparing playable floor ' + floor.name + '…';
        const exported = await request(base + '/export/uvtt?floor=' + encodeURIComponent(floor.id) + '&ppc=32&audience=player');
        const form = new FormData();
        form.append('file', await exported.blob(), 'map-maker-floor.dd2vtt');
        form.append('name', name + ' - ' + floor.name);
        form.append('project', id);
        form.append('floor', floor.id);
        form.append('mode', 'ensure');
        form.append('revision', String(source.rev));
        const studioFloor = source.floors.find(item => item.id === floor.id);
        const richElements = (studioFloor?.markers || []).filter(marker => typeof marker.note === 'string' && marker.note.startsWith('NDWORLD-RICH-TOKEN:'))
          .map(marker => {
            try { return JSON.parse(marker.note.slice('NDWORLD-RICH-TOKEN:'.length)); } catch (_) { return null; }
          }).filter(Boolean);
        if (richElements.length) form.append('elements', JSON.stringify(richElements));
        const prepared = await (await request(importUrl.pathname + importUrl.search, { method: 'POST', body: form })).json();
        if (!Number.isInteger(prepared.width) || !Number.isInteger(prepared.height) || typeof prepared.image_url !== 'string') {
          throw new Error('The server did not return the playable floor dimensions.');
        }
        floor.width = prepared.width;
        floor.height = prepared.height;
        floor.image_url = prepared.image_url;
        floor.slug = prepared.slug;
        if (floor.encounters.length) {
          await request('/api/maps/schematic/' + encodeURIComponent(prepared.slug) + '/play-manifest', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rooms: floor.encounters }),
          });
        }
        if (Array.isArray(prepared.warnings)) warnings.push(...prepared.warnings.map(message => floor.name + ': ' + message));
      }
      const current = await (await request(base)).json();
      if (current.rev !== source.rev || !Array.isArray(current.floors) || current.floors.length !== source.floors.length ||
          source.floors.some((floor, index) => current.floors[index].id !== floor.id)) {
        throw new Error('The project changed while its floors were being prepared. Save and try again.');
      }
      const groupUrl = new URL('/api/cartographer/playable-floors', window.location.origin);
      if (params.get('w')) groupUrl.searchParams.set('w', params.get('w'));
      const grouped = await (await request(groupUrl.pathname + groupUrl.search, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project: id, floors }),
      })).json();
      if (typeof grouped.slug !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(grouped.slug)) throw new Error('The server did not return a valid floor group. Check Maps.');
      const destination = new URL('/cartographer', window.location.origin);
      destination.searchParams.set('kind', 'schematic');
      destination.searchParams.set('slug', grouped.slug);
      if (params.get('w')) destination.searchParams.set('w', params.get('w'));
      link.href = destination.pathname + destination.search;
      link.hidden = false;
      setRunTarget(grouped.slug);
      status.textContent = 'Playable floors linked. Inspect them before sharing.' + (warnings.length ? ' ' + warnings.join(' ') : '');
    } catch (error) {
      status.textContent = 'Could not link playable floors: ' + error.message + ' Copies already created remain in Maps; check them before retrying.';
    } finally {
      playableFloors.disabled = false;
      if (playable) playable.disabled = false;
      if (refresh) refresh.disabled = false;
      if (geometry) geometry.disabled = false;
    }
  });
  if (project) {
    const studioFrame = document.getElementById('carto-studio-frame');
    const status = document.getElementById('carto-workspace-status');
    status.hidden = false;
    status.textContent = 'Opening Map Maker project…';
    let started = false;
    let observer;
    const timeout = setTimeout(() => {
      if (observer) observer.disconnect();
      status.textContent = 'Could not load the project list. Open the project from the studio instead.';
    }, 20000);
    const start = () => {
      if (started) return;
      const doc = studioFrame.contentDocument;
      const picker = doc && doc.getElementById('doc-select');
      if (!picker) return;
      started = true;
      observer = new MutationObserver(choose);
      function choose() {
        if (!picker.options.length) return;
        observer.disconnect();
        clearTimeout(timeout);
        const found = Array.from(picker.options).some(option => option.value === project);
        if (!found) {
          status.textContent = 'This Map Maker project was not found.';
          return;
        }
        if (picker.value !== project) {
          picker.value = project;
          picker.dispatchEvent(new studioFrame.contentWindow.Event('change', { bubbles: true }));
        }
        status.hidden = true;
      }
      observer.observe(picker, { childList: true });
      choose();
    };
    studioFrame.addEventListener('load', start);
    start();
  }

  const select = document.getElementById('carto-existing-select');
  if (!select) return;
  const open = document.getElementById('carto-open-existing');
  const tab = document.getElementById('carto-existing-tab');
  const studioButton = document.getElementById('carto-show-studio');
  const studio = document.getElementById('carto-studio-wrap');
  const existing = document.getElementById('carto-existing-wrap');
  const frame = document.getElementById('carto-existing-frame');
  const copy = document.getElementById('carto-copy-background');
  const copyHint = document.getElementById('carto-copy-hint');
  const status = document.getElementById('carto-workspace-status');

  select.addEventListener('change', () => {
    open.disabled = !select.value;
    tab.hidden = !select.value;
    const option = select.selectedOptions[0];
    copy.disabled = !option || option.dataset.html === 'yes' || option.dataset.imageUnsupported === 'yes' || !(Number(option.dataset.width) > 0) || !(Number(option.dataset.height) > 0);
    if (copyHint) {
      let reason = '';
      if (select.value && option) {
        if (option.dataset.html === 'yes') reason = 'HTML schematics cannot be copied to Map Maker.';
        else if (option.dataset.imageUnsupported === 'yes') reason = 'This schematic’s background is not a supported local image.';
        else if (!(Number(option.dataset.width) > 0) || !(Number(option.dataset.height) > 0)) reason = 'This map needs valid dimensions before it can be copied.';
      }
      copyHint.hidden = !reason;
      copyHint.textContent = reason;
    }
    if (select.value) {
      const url = new URL(select.value, window.location.origin);
      url.searchParams.delete('embed');
      tab.href = url.pathname + url.search;
    }
  });
  open.addEventListener('click', () => {
    const url = select.value;
    if (!url) return;
    const previous = frame.getAttribute('src');
    if (previous && previous !== url && !window.confirm('Switch maps? Save any changes in the current map first.')) {
      select.value = previous;
      select.dispatchEvent(new Event('change'));
      return;
    }
    if (previous !== url) {
      frame.title = select.selectedOptions[0].textContent;
      frame.src = url;
    }
    studio.hidden = true;
    existing.hidden = false;
    studioButton.setAttribute('aria-pressed', 'false');
  });
  studioButton.addEventListener('click', () => {
    existing.hidden = true;
    studio.hidden = false;
    studioButton.setAttribute('aria-pressed', 'true');
  });
  copy.addEventListener('click', async () => {
    const option = select.selectedOptions[0];
    if (!option || copy.disabled) return;
    if (!window.confirm('Create a separate Map Maker project? Image-map pins and regions or schematic floors, walls, lights, visible pins and floor links will be copied. Solid lines and pencil paths become approximate paint strokes. Other elements and gameplay data stay in the original. Save any open project first.')) return;
    copy.disabled = true;
    status.hidden = false;
    status.textContent = 'Copying background to a new project…';
    let projectId;
    async function request(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) {
        const body = await response.text();
        throw new Error(body || 'HTTP ' + response.status);
      }
      return response;
    }
    try {
      let floors;
      if (option.dataset.kind === 'map') {
        const overlayUrl = new URL('/api/cartographer/map-overlays/' + encodeURIComponent(option.dataset.slug), window.location.origin);
        if (params.get('w')) overlayUrl.searchParams.set('w', params.get('w'));
        floors = [{ source: option.dataset, overlays: await (await request(overlayUrl.pathname + overlayUrl.search)).json() }];
      } else {
        const floorsUrl = new URL('/api/cartographer/schematic-floors/' + encodeURIComponent(option.dataset.slug), window.location.origin);
        if (params.get('w')) floorsUrl.searchParams.set('w', params.get('w'));
        const group = await (await request(floorsUrl.pathname + floorsUrl.search)).json();
        if (!Array.isArray(group.floors) || !group.floors.length || group.floors.length > 64) {
          throw new Error('The schematic has an invalid or unsupported floor group.');
        }
        floors = [];
        for (const floor of group.floors) {
          const source = Array.from(select.options).find(item => item.dataset.kind === 'schematic' && item.dataset.slug === floor.slug);
          if (!source || source.dataset.html === 'yes' || source.dataset.imageUnsupported === 'yes' || !(Number(source.dataset.width) > 0) || !(Number(source.dataset.height) > 0)) {
            throw new Error('A schematic floor is unavailable or has invalid dimensions.');
          }
          const wallsUrl = new URL('/maps/schematic/' + encodeURIComponent(floor.slug) + '/walls.json', window.location.origin);
          if (params.get('w')) wallsUrl.searchParams.set('w', params.get('w'));
          floors.push({ source: source.dataset, label: floor.label, portals: floor.portals, pins: floor.pins, paths: floor.paths, tokens: floor.tokens, fog: floor.fog, combat_session_id: floor.combat_session_id, background: floor.background,
            overlays: await (await request(wallsUrl.pathname + wallsUrl.search)).json() });
        }
      }
      async function uploadBackground(source, background) {
        let blob;
        let imageUrl;
        if (source.image) {
          imageUrl = new URL(source.image, window.location.origin);
          if (imageUrl.origin !== window.location.origin || !/^\/(uploads|static)\//.test(imageUrl.pathname)) {
            throw new Error('The background is not a local image.');
          }
          blob = await (await request(imageUrl.href)).blob();
        } else {
          const canvas = document.createElement('canvas');
          canvas.width = canvas.height = 1;
          if (source.kind === 'schematic') {
            const colors = { blueprint: '#0d1b2a', 'grid-light': '#1a1a2e', light: '#f0f0f0' };
            const context = canvas.getContext('2d');
            context.fillStyle = colors[background] || '#111111';
            context.fillRect(0, 0, 1, 1);
          }
          blob = await new Promise((resolve, reject) => canvas.toBlob(result => result ? resolve(result) : reject(new Error('Could not create a blank background.')), 'image/png'));
        }
        if (imageUrl && (blob.type === 'image/avif' || imageUrl.pathname.toLowerCase().endsWith('.avif'))) {
          const image = await createImageBitmap(blob);
          try {
            if (image.width * image.height > 40000000 || image.width > 20000 || image.height > 20000) {
              throw new Error('The background exceeds Map Maker’s image size limit.');
            }
            const canvas = document.createElement('canvas');
            canvas.width = image.width;
            canvas.height = image.height;
            canvas.getContext('2d').drawImage(image, 0, 0);
            blob = await new Promise((resolve, reject) => canvas.toBlob(result => result ? resolve(result) : reject(new Error('Could not convert the background.')), 'image/png'));
          } finally {
            image.close();
          }
        }
        return (await (await request('/cartographer/studio/api/assets', { method: 'POST', body: blob })).json()).id;
      }
      const created = await (await request('/cartographer/studio/api/docs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: option.dataset.name + ' (Map Maker copy)', kind: option.dataset.kind === 'map' ? 'region' : 'battle' }),
      })).json();
      projectId = created.id;
      const base = '/cartographer/studio/api/docs/' + encodeURIComponent(projectId);
      const doc = await (await request(base)).json();
      floors[0].id = doc.floors[0].id;
      if (option.dataset.kind === 'schematic') {
        await request(base + '/floors', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'rename', id: floors[0].id, name: floors[0].label }) });
        for (const floor of floors.slice(1)) {
          const added = await (await request(base + '/floors', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', name: floor.label }) })).json();
          floor.id = added.floor;
        }
      }
      const floorIds = new Map(floors.map(floor => [floor.source.slug, floor.id]));
      for (const floor of floors) {
        const asset = await uploadBackground(floor.source, floor.background);
        const underlay = { asset, x: 0, y: 0, w: Number(floor.source.width) / created.cell_size, h: Number(floor.source.height) / created.cell_size };
        if (option.dataset.kind === 'schematic') underlay.floor = floor.id;
        await request(base + '/underlay', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(underlay),
        });
        const ops = option.dataset.kind === 'map'
          ? overlayOperations(floor.overlays, Number(floor.source.height), created.cell_size)
          : schematicOperations(floor.overlays, Number(floor.source.gridCell), created.cell_size, floor.portals, floor.pins, floor.paths, floor.tokens, floorIds);
        for (let i = 0; i < ops.length; i += 500) {
          await request(base + '/ops', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ floor: floor.id, ops: ops.slice(i, i + 500) }),
          });
        }
      }
      const linkUrl = new URL('/api/cartographer/links', window.location.origin);
      if (params.get('w')) linkUrl.searchParams.set('w', params.get('w'));
      await request(linkUrl.pathname + linkUrl.search, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: projectId, kind: option.dataset.kind, slug: option.dataset.slug }),
      });
      const url = new URL('/cartographer', window.location.origin);
      url.searchParams.set('project', projectId);
      if (params.get('w')) url.searchParams.set('w', params.get('w'));
      window.location.assign(url.pathname + url.search);
    } catch (error) {
      let cleanup = '';
      if (projectId) {
        try {
          await request('/cartographer/studio/api/docs/' + encodeURIComponent(projectId), { method: 'DELETE' });
        } catch (failure) {
          cleanup = ' The incomplete project ' + projectId + ' could not be removed: ' + failure.message;
        }
      }
      status.textContent = 'Could not copy the background: ' + error.message + cleanup;
      copy.disabled = false;
    }
  });
  function overlayColor(value, fallback) {
    const c = value || fallback;
    if (typeof c !== 'string') throw new Error('An overlay has an invalid color.');
    if (/^#[0-9a-f]{3}$/i.test(c)) return '#' + c.slice(1).split('').map(x => x + x).join('');
    if (/^#[0-9a-f]{6}$/i.test(c)) return c;
    throw new Error('An overlay color is not supported by Map Maker.');
  }
  function overlayOperations(data, height, cell) {
    if (!Array.isArray(data.markers) || !Array.isArray(data.original_markers) || !Array.isArray(data.regions)) {
      throw new Error('The map overlay data is invalid.');
    }
    if (data.markers.length + data.original_markers.length > 500 || data.regions.length > 500) {
      throw new Error('Map Maker supports at most 500 markers and 500 regions.');
    }
    const number = value => {
      const n = Number(value);
      if (value === null || value === '' || !Number.isFinite(n)) throw new Error('An overlay has an invalid coordinate.');
      return n / cell;
    };
    const ops = [];
    data.original_markers.forEach((m, i) => {
      const name = m.entity?.name || m.name || '?';
      ops.push({ t: 'put_overlay', item: { kind: 'marker', id: 'original-' + i,
        x: number(m.longitude), y: number(m.latitude), label: String(name), color: '#00f0ff' } });
    });
    data.markers.forEach((m, i) => {
      const item = { kind: 'marker', id: 'marker-' + i, x: number(m.lng), y: number(height - numberRaw(m.lat)),
        label: String(m.label || ''), note: String(m.note || ''), color: overlayColor(m.color, '#ff4466') };
      if (m.link_schematic) item.link_schematic = m.link_schematic;
      ops.push({ t: 'put_overlay', item });
    });
    data.regions.forEach((r, i) => {
      if (!Array.isArray(r.points)) throw new Error('A region has invalid points.');
      const points = r.points.map(p => {
        if (!Array.isArray(p) || p.length !== 2) throw new Error('A region has an invalid point.');
        return [number(p[1]), number(height - numberRaw(p[0]))];
      });
      ops.push({ t: 'put_overlay', item: { kind: 'region', id: 'region-' + i, name: String(r.name || ''),
        points, color: overlayColor(r.color, '#00f0ff'), opacity: r.opacity == null ? 0.25 : Number(r.opacity) } });
    });
    function numberRaw(value) {
      if (value === null || value === '' || !Number.isFinite(Number(value))) throw new Error('An overlay has an invalid coordinate.');
      return Number(value);
    }
    return ops;
  }
  function schematicOperations(data, sourceCell, cell, portals, pins, paths, tokens, floorIds) {
    if (!Array.isArray(data.walls) || !Array.isArray(data.lights)) throw new Error('The schematic wall or light data is invalid.');
    if (!Array.isArray(portals)) throw new Error('The schematic floor links are invalid.');
    if (!Array.isArray(pins)) throw new Error('The schematic pins are invalid.');
    if (!Array.isArray(paths)) throw new Error('The schematic paths are invalid.');
    if (!Array.isArray(tokens)) throw new Error('The schematic tokens are invalid.');
    if (pins.length + portals.length > 500) throw new Error('Map Maker supports at most 500 markers per floor.');
    const walls = data.walls.map(wall => ({
      kind: wall.kind, state: wall.state,
      pts: wall.pts.map(point => [Number(point[0]) / cell, Number(point[1]) / cell]),
    }));
    const ops = [{ t: 'set_imported_walls', walls }];
    paths.forEach((path, i) => {
      let points;
      if (path.type === 'path') points = path.pts;
      else if (Array.isArray(path.points) && path.points.length > 2) points = path.points.map(p => [p.x, p.y]);
      else points = [[path.x1, path.y1], [path.x2, path.y2]];
      if (!Array.isArray(points) || points.length < 2 || points.some(p => !Array.isArray(p) || p.length !== 2 ||
          p.some(value => typeof value !== 'number' || !Number.isFinite(value)))) {
        throw new Error('A schematic path has invalid points.');
      }
      const width = path.strokeW == null ? 2 : Number(path.strokeW);
      const opacity = path.opacity == null ? 0.9 : Number(path.opacity);
      if (!Number.isFinite(width) || width < 0 || !Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('A schematic path has invalid styling.');
      if (!width || !opacity) return;
      ops.push({ t: 'add_stroke', stroke: { id: 'path-' + i, kind: 'paint', brush: 'round',
        color: overlayColor(path.stroke, '#00f0ff'), size: Math.max(0.05, width / cell), opacity,
        hardness: 1, roughness: 0, seed: 0, points: points.map(p => ({ x: p[0] / cell, y: p[1] / cell })) } });
        tokens.forEach((token, i) => {
          if (!token || typeof token.x !== 'number' || typeof token.y !== 'number' ||
              !Number.isFinite(token.x) || !Number.isFinite(token.y)) throw new Error('A schematic token has invalid coordinates.');
          const label = typeof token.label === 'string' && token.label.trim() ? token.label.trim().slice(0, 120) : 'Token';
          const rich = { id: token.id || ('token-' + i), x: token.x, y: token.y, name: label,
            source: ['pc','entity','item','merchant','manual'].includes(token.source) ? token.source : 'manual',
            pc_id: token.pc_id, entity_id: token.entity_id, qty: token.qty, inventory: token.inventory, visible: token.visible };
          ops.push({ t: 'put_overlay', item: { kind: 'marker', id: 'token-' + i,
            x: token.x / cell, y: token.y / cell, label,
            note: 'NDWORLD-RICH-TOKEN:' + JSON.stringify(rich),
            color: overlayColor(token.color, '#00f0ff') } });
        });
    });
    data.lights.forEach((light, i) => {
      ops.push({ t: 'add_light', light: {
        id: 'light-' + i, x: light.x, y: light.y, range: light.range * sourceCell / cell,
        color: light.color, intensity: light.intensity, on: light.on, label: light.label,
      } });
    });
    pins.forEach((pin, i) => {
      if ([pin.x, pin.y].some(value => typeof value !== 'number' || !Number.isFinite(value))) {
        throw new Error('A schematic pin has an invalid position.');
      }
      ops.push({ t: 'put_overlay', item: { kind: 'marker', id: 'pin-' + i, x: pin.x / cell,
        y: pin.y / cell, label: String(pin.label || ''), color: overlayColor(pin.color, '#00f0ff') } });
    });
    portals.forEach((portal, i) => {
      const target = floorIds.get(portal.destination_slug);
      const coords = [portal.x, portal.y, portal.destination_x, portal.destination_y];
      if (!target || coords.some(value => typeof value !== 'number' || !Number.isFinite(value))) {
        throw new Error('A floor link has an invalid destination or position.');
      }
      ops.push({ t: 'put_overlay', item: { kind: 'marker', id: 'floor-link-' + i,
        x: portal.x / cell, y: portal.y / cell, label: String(portal.label || 'Stairs'), color: '#ffc857',
        links_to: { floor: target, x: portal.destination_x / cell, y: portal.destination_y / cell } } });
    });
    return ops;
  }
  const linksNode = document.getElementById('carto-links');
  const links = linksNode ? JSON.parse(linksNode.textContent) : [];
  function selectLinkedProject(id) {
    const link = links.find(item => item.project === id);
    const option = link && Array.from(select.options).find(item => item.dataset.kind === link.kind && item.dataset.slug === link.slug);
    select.value = option ? option.value : '';
    select.dispatchEvent(new Event('change'));
  }
  if (project) selectLinkedProject(project);
  const studioFrame = document.getElementById('carto-studio-frame');
  if (studioFrame) {
    let watchedPicker;
    const watchPicker = () => {
      const picker = studioFrame.contentDocument?.getElementById('doc-select');
      if (picker && picker !== watchedPicker) {
        watchedPicker = picker;
        picker.addEventListener('change', () => selectLinkedProject(picker.value));
      }
    };
    studioFrame.addEventListener('load', watchPicker);
    watchPicker();
  }
  const kind = params.get('kind'), slug = params.get('slug');
  if (!project && kind && slug) {
    const option = Array.from(select.options).find(item => item.dataset.kind === kind && item.dataset.slug === slug);
    if (option) {
      select.value = option.value;
      select.dispatchEvent(new Event('change'));
      open.click();
    }
  }
})();
