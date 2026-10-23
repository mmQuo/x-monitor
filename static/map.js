document.addEventListener('DOMContentLoaded', () => {
  const root = document.querySelector('.map-page');
  if (!root) return;
  const form = document.querySelector('#map-filters');
  const list = document.querySelector('#map-events');
  const state = document.querySelector('#map-live');
  const slider = document.querySelector('#map-time');
  const workspace = document.querySelector('.map-workspace');
  const error = document.querySelector('#map-error');
  const clear = document.querySelector('#map-clear');
  const clock = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false});
  let events = [], visible = [], selected = new Set(), map, markers = [];
  let controller, sequence = 0, timer, anchor = Date.now(), mapFailed = false, stream;
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
  }
  document.querySelectorAll('[data-view][type=button]').forEach(button => button.addEventListener('click', () => view(button.dataset.view)));

  function selectPlaces(places, fly = true) {
    selected = new Set(places.map(place => place.id));
    document.querySelector('#map-list-title').textContent = places.length === 1 ? places[0].name : `${places.length} 个地点`;
    clear.hidden = false;
    renderList();
    renderMarkers();
    if (fly && map && !mapFailed) {
      if (places.length === 1) {
        const p = places[0];
        map.easeTo({center:p.coordinates, zoom:p.level === 'city' ? 6 : p.level === 'country' ? 3.4 : 4.2, duration:450});
      } else {
        const bounds = new maplibregl.LngLatBounds();
        places.forEach(p => bounds.extend(p.coordinates));
        map.fitBounds(bounds, {padding:70, maxZoom:7, duration:450});
      }
    }
    if (matchMedia('(max-width:760px)').matches) view('list');
  }
  function clearSelection() {
    selected.clear(); clear.hidden = true;
    document.querySelector('#map-list-title').textContent = '全部地点';
    renderList(); renderMarkers();
  }
  clear.addEventListener('click', clearSelection);
  function worldView(duration = 0) {
    map?.fitBounds([[-170,-58],[180,76]], {padding:30, maxZoom:2, duration});
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
      const card = el('details', undefined, 'map-event');
      card.dataset.id = event.id; card.open = opened.has(event.id);
      const summary = el('summary');
      const meta = el('div', undefined, 'map-event-meta');
      meta.append(el('span', event.category, 'category-tag'), el('span', event.sources[0]?.source_label || ''));
      const location = el('span', event.locations.map(p => p.name).join(' / '));
      location.title = event.locations.map(p => `${p.name} · ${p.precision}`).join(' / ');
      meta.append(location);
      const time = el('time', stamp.slice(-5)); time.dateTime = event.created_at; meta.append(time);
      summary.append(meta, el('div', event.summary, 'map-event-title')); card.append(summary);
      const chips = el('div', undefined, 'map-event-places');
      for (const place of event.locations) {
        const chip = el('button', `${place.name} · ${place.precision}`, 'place-chip' + (selected.has(place.id) ? ' selected' : ''));
        chip.type = 'button'; chip.addEventListener('click', () => selectPlaces([place])); chips.append(chip);
      }
      card.append(chips);
      for (const place of event.locations) {
        card.append(el('p', `${place.relation === 'scope' ? '涉及地区' : '事件地点'} · ${place.name}：${place.evidence}`, 'map-evidence'));
      }
      if (event.translation && event.translation !== event.text) {
        card.append(el('p', '中文译文', 'map-original-label'), el('div', event.translation, 'map-original'));
      }
      card.append(el('p', '来源原文', 'map-original-label'), el('div', event.text, 'map-original'));
      const sources = el('div', undefined, 'map-event-sources');
      for (const source of event.sources) {
        let url;
        try { url = new URL(source.url); } catch { continue; }
        if (!['http:', 'https:'].includes(url.protocol)) continue;
        const a = el('a', `${source.source_label} ↗`); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; sources.append(a);
      }
      card.append(sources); fragment.append(card);
    }
    if (!shown.length) fragment.append(el('p', selected.size ? '当前时间与筛选条件下，这个地点暂无资讯。' : '当前范围暂无已定位资讯。可调整时间或筛选条件，新内容识别完成后会自动出现。', 'map-empty'));
    list.replaceChildren(fragment);
  }

  function renderMarkers() {
    if (!map || mapFailed) return;
    markers.forEach(marker => marker.remove()); markers = [];
    const places = new Map();
    for (const event of visible) for (const place of event.locations) {
      if (!places.has(place.id)) places.set(place.id, {...place, events:new Set()});
      places.get(place.id).events.add(event.id);
    }
    // Cluster nearby screen positions; the marker counts unique stories.
    const groups = [];
    for (const place of places.values()) {
      const point = map.project(place.coordinates);
      const canvas = map.getCanvas();
      if (point.x < -20 || point.x > canvas.clientWidth + 20 || point.y < -20 || point.y > canvas.clientHeight + 20) continue;
      let group = groups.find(g => Math.hypot(g.x-point.x, g.y-point.y) < 40);
      if (!group) { group = {x:point.x, y:point.y, places:[], events:new Set(), coordinates:place.coordinates}; groups.push(group); }
      group.places.push(place); place.events.forEach(id => group.events.add(id));
    }
    for (const group of groups) {
      const cluster = group.places.length > 1;
      const className = 'map-marker' + (cluster ? ' cluster' : group.places[0].level !== 'city' ? ' scope' : '')
        + (group.places.some(p => selected.has(p.id)) ? ' selected' : '');
      const button = el('button', String(group.events.size), className);
      button.type = 'button';
      button.title = `${group.places.map(p => p.name).join('、')} · ${group.events.size} 条资讯`;
      button.setAttribute('aria-label', button.title);
      button.addEventListener('click', () => selectPlaces(group.places));
      markers.push(new maplibregl.Marker({element:button}).setLngLat(group.coordinates).addTo(map));
    }
  }

  function render() {
    const hours = Number(form.elements.hours.value);
    const end = anchor - (hours - Number(slider.value))*3600000;
    visible = events.filter(event => Date.parse(event.created_at) <= end);
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
  const parameters = new URLSearchParams(location.search);
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
      maxPitch:0, dragRotate:false, touchPitch:false, renderWorldCopies:false,
      style:{version:8, sources:{countries:{type:'geojson', data:'/static/map-countries.geojson'}, basemap:{type:'raster', tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize:256,
        attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>'}},
        layers:[{id:'background', type:'background', paint:{'background-color':'#dce6e7'}},
          {id:'land', type:'fill', source:'countries', paint:{'fill-color':'#f5f6f0'}},
          {id:'basemap', type:'raster', source:'basemap', paint:{'raster-saturation':-0.65, 'raster-opacity':0.85}},
          {id:'boundaries', type:'line', source:'countries', maxzoom:5, paint:{'line-color':'#adb9ae', 'line-width':0.65}}]}});
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({showCompass:false}), 'top-right');
    map.on('load', () => { worldView(); renderMarkers(); }); map.on('moveend', renderMarkers);
    let tileErrors = 0;
    map.on('error', () => {
      if (++tileErrors >= 3) { error.textContent = '底图加载暂缓，事件列表仍可浏览。'; error.hidden = false; }
    });
    map.on('idle', () => { if (map.areTilesLoaded()) { error.hidden = true; tileErrors = 0; } });
    new ResizeObserver(() => map?.resize()).observe(document.querySelector('.map-canvas-panel'));
  } catch {
    mapFailed = true; error.textContent = '当前浏览器无法显示地图，可切换事件列表查看内容。'; error.hidden = false;
  }
  const interval = setInterval(() => refresh(), 30000);
  function connect() {
    if (stream || !('EventSource' in window)) return;
    stream = new EventSource('/map/stream');
    stream.addEventListener('update', () => refresh());
    stream.addEventListener('error', () => { state.textContent = '重连中'; state.classList.remove('connected'); });
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => { clearInterval(interval); controller?.abort(); stream?.close(); stream = undefined; });
  window.addEventListener('pageshow', event => { if (event.persisted) { connect(); refresh(); } });
  connect();
  refresh();
});
