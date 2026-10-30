document.addEventListener('DOMContentLoaded', () => {
  const root = document.querySelector('.map-page');
  if (!root) return;
  const form = document.querySelector('#map-filters');
  const list = document.querySelector('#map-events');
  const state = document.querySelector('#map-live');
  const slider = document.querySelector('#map-time');
  const workspace = document.querySelector('.map-workspace');
  const error = document.querySelector('#map-error');
  const errorText = document.querySelector('#map-error-text');
  const retryButton = document.querySelector('#map-retry');
  const tileUrls = {satellite:'/map/tiles/satellite/{z}/{x}/{y}.jpg', basemap:'https://tile.openstreetmap.org/{z}/{x}/{y}.png'};
  const clear = document.querySelector('#map-clear');
  const parameters = new URLSearchParams(location.search);
  let savedView = {};
  try { savedView = JSON.parse(localStorage.getItem('xmonitor-map-view') || '{}') || {}; } catch { /* Storage may be disabled. */ }
  const basemaps = new Set(['streets', 'satellite']);
  let basemap = parameters.get('basemap') || savedView.basemap;
  if (!basemaps.has(basemap)) basemap = 'satellite';
  const basemapSelect = document.querySelector('#map-basemap');
  basemapSelect.value = basemap;
  root.dataset.basemap = basemap;
  const projections = new Set(['mercator', 'globe']);
  let projection = parameters.get('projection') || savedView.projection;
  if (!projections.has(projection)) projection = 'globe';
  const clock = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false});
  let events = [], visible = [], locatedPlaces = [], selected = new Set(), focusedPlaces = new Set(), focusedEvent, map;
  const markers = new Map();
  let mapReady = false, overview = true, pendingFocus;
  let controller, sequence = 0, timer, anchor = Date.now(), mapFailed = false, stream;
  let tileRetryTimer, tileWatchdog, tileAttempts = 0, tileFailed = false, tileReceived = false;
  const activeTileSource = () => basemap === 'satellite' ? 'satellite' : 'basemap';

  function clearTileTimers() {
    clearTimeout(tileRetryTimer); tileRetryTimer = undefined;
    clearTimeout(tileWatchdog); tileWatchdog = undefined;
  }
  function watchTiles() {
    if (!tileWatchdog) tileWatchdog = setTimeout(() => {
      tileWatchdog = undefined;
      if (!document.hidden) tileFailure();
    }, 20000);
  }
  function tileFailure() {
    tileFailed = true;
    error.hidden = false;
    const name = basemap === 'satellite' ? '卫星影像' : '彩色底图';
    errorText.textContent = tileAttempts < 3 ? `${name}加载暂缓，正在重试…` : `${name}暂时无法加载，可点击重新加载。`;
    if (!tileRetryTimer && tileAttempts < 3) tileRetryTimer = setTimeout(() => {
      tileAttempts++;
      reloadTiles();
    }, 2000 * 2 ** tileAttempts);
  }
  function reloadTiles(manual = false) {
    if (!mapReady || mapFailed) return;
    clearTileTimers();
    if (manual) tileAttempts = 0;
    tileFailed = false; tileReceived = false; error.hidden = true;
    const source = activeTileSource();
    // MapLibre 5.6 can render failed raster tiles without textures after setTiles().
    // Recreate only this source/layer so retries keep the camera and news state.
    const style = map.getStyle();
    const layer = style.layers.find(item => item.id === source);
    const specification = style.sources[source];
    map.removeLayer(source);
    map.removeSource(source);
    map.addSource(source, {...specification, tiles:[tileUrls[source]]});
    map.addLayer(layer, 'boundaries');
    watchTiles();
  }
  function tilesSettled() {
    // Failed tiles can also count as "loaded" in MapLibre. Keep the error visible.
    if (mapReady && tileReceived && !tileFailed && map.areTilesLoaded()) {
      clearTileTimers(); tileAttempts = 0; error.hidden = true;
    }
  }
  retryButton.addEventListener('click', () => reloadTiles(true));
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const dateLabel = value => clock.format(new Date(value)).replaceAll('/', '-');
  function view(name) {
    workspace.dataset.view = name;
    document.querySelectorAll('[data-view][type=button]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === name)));
    if (name === 'map') requestAnimationFrame(() => map?.resize());
    else map?.stop();
  }
  document.querySelectorAll('[data-view][type=button]').forEach(button => button.addEventListener('click', () => view(button.dataset.view)));

  function saveView() {
    try { localStorage.setItem('xmonitor-map-view', JSON.stringify({projection, basemap})); } catch { /* Keep the current view without persistence. */ }
  }

  function projectionControls() {
    root.dataset.projection = projection;
    document.querySelectorAll('button[data-projection]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.projection === projection)));
    document.querySelector('#event-map').setAttribute('aria-label', projection === 'globe' ? '新闻事件 3D 地球' : '新闻事件平面地图');
    document.querySelector('#map-gesture').hidden = projection !== 'globe';
  }
  document.querySelectorAll('button[data-projection]').forEach(button => button.addEventListener('click', () => {
    const next = button.dataset.projection;
    if (next === projection) return;
    projection = next;
    projectionControls();
    saveView();
    const url = new URL(location.href); url.searchParams.set('projection', projection);
    history.replaceState(null, '', url);
    if (mapReady && !mapFailed) {
      map.stop();
      map.setProjection({type:projection});
      if (overview) worldView();
      renderMarkers();
    }
  }));
  projectionControls();

  function basemapLayers() {
    if (!mapReady || mapFailed) return;
    map.setLayoutProperty('basemap', 'visibility', basemap === 'streets' ? 'visible' : 'none');
    map.setLayoutProperty('satellite', 'visibility', basemap === 'satellite' ? 'visible' : 'none');
    map.setPaintProperty('boundaries', 'line-color', basemap === 'satellite' ? '#ffffff' : '#8b9b83');
    map.setPaintProperty('boundaries', 'line-opacity', basemap === 'satellite' ? 0.45 : 0.6);
  }
  basemapSelect.addEventListener('change', () => {
    clearTileTimers(); tileAttempts = 0; tileFailed = false; tileReceived = false;
    if (!mapFailed) error.hidden = true;
    basemap = basemapSelect.value;
    root.dataset.basemap = basemap;
    saveView();
    const url = new URL(location.href); url.searchParams.set('basemap', basemap);
    history.replaceState(null, '', url);
    basemapLayers();
    if (mapReady && !mapFailed) reloadTiles();
  });

  function focusPlaces(places) {
    if (!mapReady) { pendingFocus = places; return; }
    pendingFocus = undefined;
    if (places.length && mapReady && !mapFailed) {
      overview = false;
      const duration = matchMedia('(max-width:760px), (prefers-reduced-motion:reduce)').matches ? 0 : 850;
      map.stop();
      if (places.length === 1) {
        const p = places[0];
        map.flyTo({center:p.coordinates, zoom:p.level === 'city' ? 6 : p.level === 'country' ? 3.4 : 4.2, duration});
      } else {
        // Use the shortest longitude span, including events across the date line.
        const longitudes = places.map(p => (p.coordinates[0] + 360) % 360).sort((a,b) => a-b);
        let gap = -1, start = longitudes[0];
        longitudes.forEach((lng, i) => {
          const next = i === longitudes.length - 1 ? longitudes[0] + 360 : longitudes[i+1];
          if (next-lng > gap) { gap = next-lng; start = next % 360; }
        });
        const bounds = new maplibregl.LngLatBounds();
        places.forEach(p => {
          let lng = (p.coordinates[0] + 360) % 360;
          if (lng < start) lng += 360;
          bounds.extend([lng, p.coordinates[1]]);
        });
        map.fitBounds(bounds, {padding:70, maxZoom:5, duration});
      }
    }
  }
  function selectPlaces(places, fly = true) {
    selected = new Set(places.map(place => place.id));
    focusedPlaces.clear(); focusedEvent = undefined;
    document.querySelector('#map-list-title').textContent = places.length === 1 ? places[0].name : `${places.length} 个地点`;
    clear.hidden = false;
    renderList();
    renderMarkers();
    if (fly) focusPlaces(places);
    if (matchMedia('(max-width:760px)').matches) view('list');
  }
  function clearSelection() {
    selected.clear(); clear.hidden = true;
    pendingFocus = undefined;
    focusedPlaces.clear(); focusedEvent = undefined;
    document.querySelector('#map-list-title').textContent = '全部地点';
    renderList(); renderMarkers();
  }
  clear.addEventListener('click', clearSelection);
  function worldView(duration = 0) {
    if (!map || !mapReady || mapFailed) return;
    overview = true;
    if (projection === 'globe') {
      const canvas = map.getCanvas();
      if (!canvas.clientWidth || !canvas.clientHeight) return;
      // Fit the entire sphere between the toolbar and legend, also on phones.
      const radius = Math.max(40, Math.min(canvas.clientWidth - 40, canvas.clientHeight - 120) / 2);
      const camera = canvas.clientHeight / (2 * Math.tan(map.getVerticalFieldOfView() * Math.PI / 360));
      const globeRadius = (radius * radius + radius * Math.hypot(radius, camera)) / camera;
      const zoom = Math.log2(2 * Math.PI * globeRadius * Math.cos(23 * Math.PI / 180) / 512);
      map.easeTo({center:[25,23], zoom, bearing:0, pitch:0, duration});
    } else {
      map.fitBounds([[-170,-58],[180,76]], {padding:30, maxZoom:2, duration});
    }
  }
  document.querySelector('#map-reset').addEventListener('click', () => {
    clearSelection();
    worldView(500);
  });

  function renderList() {
    const opened = new Set([...list.querySelectorAll('details[open]')].map(node => node.dataset.id));
    const shown = visible.filter(event => !selected.size || event.locations.some(p => selected.has(p.id)));
    document.querySelector('#map-list-count').textContent = `${shown.length} 条`;
    document.querySelector('#mobile-count').textContent = shown.length;
    const fragment = document.createDocumentFragment();
    let day;
    for (const event of shown) {
      const stamp = dateLabel(event.created_at);
      if (day !== stamp.slice(0,5)) {
        day = stamp.slice(0,5);
        fragment.append(el('div', day + ' · 北京时间', 'map-day'));
      }
      const card = el('article', undefined, 'map-event');
      card.dataset.id = event.id;
      card.classList.toggle('focused', focusedEvent === event.id);
      const focus = el('button', undefined, 'map-event-focus');
      focus.type = 'button';
      focus.title = '在地图上定位';
      focus.addEventListener('click', () => {
        focusedEvent = event.id;
        focusedPlaces = new Set(event.locations.map(p => p.id));
        list.querySelectorAll('.map-event').forEach(row => row.classList.toggle('focused', row.dataset.id === event.id));
        focusPlaces(event.locations);
        renderMarkers();
      });
      const meta = el('span', undefined, 'map-event-meta');
      meta.append(el('span', event.category, 'category-tag'), el('span', event.sources[0]?.source_label || ''));
      const location = el('span', event.locations.map(p => p.name).join(' / '));
      location.title = event.locations.map(p => `${p.name} · ${p.precision}`).join(' / ');
      meta.append(location);
      const time = el('time', stamp.slice(-5)); time.dateTime = event.created_at; meta.append(time);
      focus.append(meta, el('span', event.summary, 'map-event-title')); card.append(focus);
      const details = el('details', undefined, 'map-event-details');
      details.dataset.id = event.id; details.open = opened.has(event.id);
      const toggle = el('summary');
      toggle.append(el('span', '查看详情', 'expand-label'), el('span', '收起详情', 'collapse-label'));
      details.append(toggle);
      const chips = el('div', undefined, 'map-event-places');
      for (const place of event.locations) {
        const chip = el('button', `${place.name} · ${place.precision}`, 'place-chip' + (selected.has(place.id) ? ' selected' : ''));
        chip.type = 'button'; chip.addEventListener('click', () => selectPlaces([place])); chips.append(chip);
      }
      details.append(chips);
      for (const place of event.locations) {
        details.append(el('p', `${place.relation === 'scope' ? '涉及地区' : '事件地点'} · ${place.name}：${place.evidence}`, 'map-evidence'));
      }
      if (event.translation && event.translation !== event.text) {
        details.append(el('p', '中文译文', 'map-original-label'), el('div', event.translation, 'map-original'));
      }
      details.append(el('p', '来源原文', 'map-original-label'), el('div', event.text, 'map-original'));
      const sources = el('div', undefined, 'map-event-sources');
      for (const source of event.sources) {
        let url;
        try { url = new URL(source.url); } catch { continue; }
        if (!['http:', 'https:'].includes(url.protocol)) continue;
        const a = el('a', `${source.source_label} ↗`); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; sources.append(a);
      }
      details.append(sources); card.append(details); fragment.append(card);
    }
    if (!shown.length) fragment.append(el('p', selected.size ? '当前时间与筛选条件下，这个地点暂无资讯。' : '当前范围暂无已定位资讯。可调整时间或筛选条件，新内容识别完成后会自动出现。', 'map-empty'));
    list.replaceChildren(fragment);
  }

  function renderMarkers() {
    if (!map || !mapReady || mapFailed) return;
    // Cluster nearby screen positions; the marker counts unique stories.
    const groups = [];
    const canvas = map.getCanvas();
    if (!canvas.clientWidth || !canvas.clientHeight) return;
    for (const place of locatedPlaces) {
      // Use the same horizon test as Marker in our pinned MapLibre version.
      // Exclude the back of the globe BEFORE clustering and counting stories.
      if (projection === 'globe' && map.transform.isLocationOccluded(maplibregl.LngLat.convert(place.coordinates))) continue;
      const point = map.project(place.coordinates);
      if (point.x < -20 || point.x > canvas.clientWidth + 20 || point.y < -20 || point.y > canvas.clientHeight + 20) continue;
      let group = groups.find(g => Math.hypot(g.x-point.x, g.y-point.y) < 40);
      if (!group) { group = {x:point.x, y:point.y, places:[], events:new Set(), coordinates:place.coordinates}; groups.push(group); }
      group.places.push(place); place.events.forEach(id => group.events.add(id));
    }
    const retained = new Set();
    for (const group of groups) {
      const key = group.places.map(p => p.id).sort().join('|');
      retained.add(key);
      const cluster = group.places.length > 1;
      const active = group.places.some(p => selected.has(p.id) || focusedPlaces.has(p.id));
      const className = 'map-marker' + (cluster ? ' cluster' : group.places[0].level !== 'city' ? ' scope' : '')
        + (active ? ' selected' : '');
      const title = `${group.places.map(p => p.name).join('、')} · ${group.events.size} 条资讯`;
      let entry = markers.get(key);
      if (!entry) {
        const button = el('button'); button.type = 'button';
        entry = {button, places:group.places, marker:new maplibregl.Marker({element:button, opacityWhenCovered:'0', subpixelPositioning:true}).setLngLat(group.coordinates).addTo(map)};
        button.addEventListener('click', () => selectPlaces(entry.places));
        markers.set(key, entry);
      }
      entry.places = group.places;
      const coordinates = entry.marker.getLngLat();
      if (coordinates.lng !== group.coordinates[0] || coordinates.lat !== group.coordinates[1]) entry.marker.setLngLat(group.coordinates);
      if (entry.className !== className) {
        // Preserve the engine's positioning classes when selection changes.
        entry.button.classList.toggle('map-marker', true);
        entry.button.classList.toggle('cluster', cluster);
        entry.button.classList.toggle('scope', !cluster && group.places[0].level !== 'city');
        entry.button.classList.toggle('selected', active);
        entry.className = className;
      }
      if (entry.button.textContent !== String(group.events.size)) entry.button.textContent = String(group.events.size);
      if (entry.button.title !== title) { entry.button.title = title; entry.button.setAttribute('aria-label', title); }
    }
    for (const [key, entry] of markers) {
      if (!retained.has(key)) { entry.marker.remove(); markers.delete(key); }
    }
  }

  function render() {
    const hours = Number(form.elements.hours.value);
    const end = anchor - (hours - Number(slider.value))*3600000;
    visible = events.filter(event => Date.parse(event.created_at) <= end);
    if (focusedEvent && !visible.some(event => event.id === focusedEvent)) { focusedEvent = undefined; focusedPlaces.clear(); }
    const places = new Map();
    for (const event of visible) for (const place of event.locations) {
      if (!places.has(place.id)) places.set(place.id, {...place, events:new Set()});
      places.get(place.id).events.add(event.id);
    }
    locatedPlaces = [...places.values()];
    document.querySelector('#map-time-label').textContent = Number(slider.value) === hours ? '现在' : dateLabel(end);
    renderList(); renderMarkers();
  }
  slider.addEventListener('input', render);
  document.querySelector('#map-now').addEventListener('click', () => { slider.value = slider.max; render(); });

  async function refresh(reset = false) {
    if (document.hidden) return;
    controller?.abort(); controller = new AbortController();
    const currentController = controller;
    const ticket = ++sequence;
    const timeout = setTimeout(() => currentController.abort(), 20000);
    const params = new URLSearchParams(new FormData(form));
    try {
      const response = await fetch(`/map/data?${params}`, {signal:controller.signal, credentials:'same-origin', cache:'no-store'});
      if (response.redirected) { location.assign(response.url); return; }
      if (!response.ok) throw new Error('request failed');
      const data = await response.json();
      if (ticket !== sequence) return;
      events = data.events; anchor = Date.now();
      const hours = Number(form.elements.hours.value);
      if (reset) { slider.max = hours; slider.value = hours; clearSelection(); }
      const {located, unlocated, pending, failed} = data.stats;
      document.querySelector('#map-count').textContent = `${located} 条已定位 · ${unlocated} 条暂无明确地点${pending ? ` · ${pending} 条待识别` : ''}${failed ? ` · ${failed} 条识别暂缓` : ''}`;
      state.textContent = '自动更新'; state.classList.add('connected');
      render();
    } catch (failure) {
      if (ticket !== sequence) return;
      state.textContent = '更新暂缓'; state.classList.remove('connected');
      if (!events.length) list.replaceChildren(el('p', '读取暂时失败，可点击刷新重试。', 'map-empty'));
    } finally { clearTimeout(timeout); }
  }
  form.addEventListener('submit', event => { event.preventDefault(); refresh(); });
  form.querySelectorAll('select').forEach(input => input.addEventListener('change', () => refresh(true)));
  form.elements.q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => refresh(true), 250); });
  for (const input of form.elements) {
    if (input.name && parameters.has(input.name)) {
      const value = parameters.get(input.name);
      if (input.tagName !== 'SELECT' || [...input.options].some(option => option.value === value)) input.value = value;
    }
  }
  slider.max = form.elements.hours.value; slider.value = slider.max;
  try {
    maplibregl.setWorkerUrl(root.dataset.workerUrl);
    map = new maplibregl.Map({container:'event-map', center:[25,23], zoom:0, minZoom:-1, maxZoom:10,
      maxPitch:0, dragRotate:false, touchPitch:false, renderWorldCopies:false, trackResize:false,
      style:{version:8, projection:{type:projection}, sources:{
        countries:{type:'geojson', data:'/static/map-countries.geojson'},
        basemap:{type:'raster', tiles:[tileUrls.basemap], tileSize:256,
          attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>'},
        satellite:{type:'raster', tiles:[tileUrls.satellite], tileSize:256, maxzoom:18,
          attribution:'Imagery © <a href="https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9" target="_blank" rel="noopener">Esri, Vantor, Earthstar Geographics & GIS User Community</a>'}},
        layers:[{id:'background', type:'background', paint:{'background-color':'#aad3df'}},
          {id:'land', type:'fill', source:'countries', paint:{'fill-color':'#dce7c9'}},
          {id:'basemap', type:'raster', source:'basemap', layout:{visibility:basemap === 'streets' ? 'visible' : 'none'}},
          {id:'satellite', type:'raster', source:'satellite', layout:{visibility:basemap === 'satellite' ? 'visible' : 'none'}},
          {id:'boundaries', type:'line', source:'countries', maxzoom:5, paint:{'line-color':'#8b9b83', 'line-width':0.65, 'line-opacity':0.6}}]}});
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({showCompass:false}), 'top-right');
    map.once('style.load', () => {
      mapReady = true; map.setProjection({type:projection}); basemapLayers(); worldView();
      if (pendingFocus) focusPlaces(pendingFocus);
      renderMarkers();
      watchTiles();
    });
    map.on('movestart', event => { if (event.originalEvent) overview = false; });
    map.on('moveend', renderMarkers);
    // Reuse DOM markers during rotation, zoom and projection transitions.
    map.on('render', renderMarkers);
    map.on('error', event => {
      if (event.sourceId === activeTileSource()) tileFailure();
    });
    map.on('sourcedataloading', event => { if (event.sourceId === activeTileSource()) watchTiles(); });
    map.on('sourcedata', event => {
      if (event.sourceId === activeTileSource() && event.tile?.state === 'loaded') tileReceived = true;
      tilesSettled();
    });
    map.on('idle', tilesSettled);
    new ResizeObserver(() => {
      const panel = document.querySelector('.map-canvas-panel');
      if (!panel.clientWidth || !panel.clientHeight) return;
      map.resize();
      if (overview) worldView();
      renderMarkers();
    }).observe(document.querySelector('.map-canvas-panel'));
  } catch {
    mapFailed = true; clearTileTimers(); retryButton.hidden = true;
    errorText.textContent = '当前浏览器无法显示地图，可切换事件列表查看内容。'; error.hidden = false;
  }
  const interval = setInterval(() => refresh(), 30000);
  function connect() {
    if (stream || !('EventSource' in window)) return;
    stream = new EventSource('/map/stream');
    stream.addEventListener('update', () => refresh());
    stream.addEventListener('error', () => { state.textContent = '重连中'; state.classList.remove('connected'); });
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => { clearInterval(interval); clearTileTimers(); controller?.abort(); stream?.close(); stream = undefined; });
  window.addEventListener('pageshow', event => { if (event.persisted) { connect(); refresh(); } });
  connect();
  refresh();
});
