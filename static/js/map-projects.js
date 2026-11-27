(async function () {
  const status = document.getElementById('carto-project-status');
  const grid = document.querySelector('.maps-grid[data-pl="maps"]');
  if (!status || !grid) return;
  try {
    const response = await fetch('/cartographer/studio/api/docs');
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (!Array.isArray(data.docs)) throw new Error('invalid project list');
    const projects = data.docs.filter(item =>
      item && typeof item.id === 'string' && typeof item.name === 'string');
    projects.sort((a, b) => a.name.localeCompare(b.name));
    for (const project of projects) {
      const url = new URL('/cartographer', location.origin);
      url.searchParams.set('project', project.id);
      if (status.dataset.world) url.searchParams.set('w', status.dataset.world);
      const card = document.createElement('div');
      card.className = 'map-card';
      const picture = document.createElement('div');
      picture.className = 'map-img-wrap';
      const imageLink = document.createElement('a');
      imageLink.href = url.pathname + url.search;
      const icon = document.createElement('div');
      icon.className = 'map-placeholder';
      icon.textContent = '🗺';
      imageLink.appendChild(icon);
      picture.appendChild(imageLink);
      const body = document.createElement('div');
      body.className = 'map-card-body';
      const name = document.createElement('div');
      name.className = 'map-name';
      const link = document.createElement('a');
      link.href = imageLink.href;
      link.style.cssText = 'color:inherit;text-decoration:none';
      link.textContent = project.name;
      name.appendChild(link);
      const meta = document.createElement('div');
      meta.className = 'map-meta';
      const kind = { region: 'world', battle: 'battle', schematic: 'schematic' }[project.kind];
      meta.textContent = 'Map Maker project' + (kind ? ' · ' + kind : '');
      body.append(name, meta);
      card.append(picture, body);
      grid.appendChild(card);
    }
    if (projects.length) {
      grid.querySelector('.empty')?.remove();
      status.textContent = 'Map Maker projects are shared across worlds.';
    } else {
      status.textContent = '';
    }
  } catch (error) {
    status.textContent = 'Could not load Map Maker projects: ' + error.message;
  }
})();
