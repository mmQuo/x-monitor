document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-report-day]');
  if (!page) return;
  const content = document.querySelector('#report-content');
  const history = document.querySelector('#report-history');
  const state = document.querySelector('#report-live-state');
  const params = new URLSearchParams({ date: page.dataset.reportDay });
  let fetching = false;
  let pending = false;
  let source;
  let controller;
  let lastSync = 0;
  let revision;
  let renderedContent;
  let renderedHistory;
  const dateInput = document.querySelector('#report-date');
  dateInput.addEventListener('change', () => {
    if (dateInput.checkValidity()) dateInput.form.requestSubmit();
  });

  function updateElapsed() {
    if (document.hidden) return;
    for (const el of content.querySelectorAll('[data-report-started]')) {
      const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(el.dataset.reportStarted)) / 1000));
      if (Number.isFinite(seconds)) el.textContent = `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
    }
  }

  async function update() {
    if (document.hidden || fetching) { pending = true; return; }
    fetching = true;
    pending = false;
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller?.abort(), 15000);
    try {
      const response = await fetch(`/reports/status?${params}`, { cache: 'no-store', credentials: 'same-origin', signal: controller.signal });
      if (response.redirected) { window.location.assign(response.url); return; }
      if (!response.ok) throw new Error('refresh failed');
      const data = await response.json();
      lastSync = Date.now();
      state.textContent = '自动更新';
      if (data.revision && data.revision === revision) { updateElapsed(); return; }
      if (data.content !== renderedContent) {
        const opened = new Set([...content.querySelectorAll('details[open]')].map(el => el.dataset.detailsId));
        const activeButton = document.activeElement?.closest('#report-content button');
        content.innerHTML = data.content;
        renderedContent = data.content;
        for (const el of content.querySelectorAll('details')) {
          el.open = opened.has(el.dataset.detailsId);
        }
        if (activeButton) content.querySelector('button')?.focus({ preventScroll: true });
      }
      if (data.history !== renderedHistory) {
        history.innerHTML = data.history;
        renderedHistory = data.history;
      }
      revision = data.revision;
      updateElapsed();
    } catch {
      state.textContent = '重连中';
    } finally {
      window.clearTimeout(timeout);
      controller = undefined;
      fetching = false;
      if (pending && !document.hidden) window.setTimeout(update, 300);
    }
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
  function connect() {
    if (source || !('EventSource' in window)) return;
    source = new EventSource(`/reports/stream?${params}`);
    source.addEventListener('update', update);
    source.addEventListener('open', update);
    source.addEventListener('error', update);
  }
  // Also check while SSE is connected: a silently stalled connection emits no error.
  window.setInterval(() => { if (Date.now() - lastSync >= 10000) update(); }, 10000);
  window.setInterval(updateElapsed, 1000);
  window.addEventListener('pagehide', () => {
    source?.close();
    source = undefined;
    controller?.abort();
  });
  window.addEventListener('pageshow', event => { if (event.persisted) { connect(); update(); } });
  connect();
  update();
  updateElapsed();
});
