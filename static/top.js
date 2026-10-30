document.addEventListener('DOMContentLoaded', () => {
  const content = document.querySelector('#top-content');
  if (!content) return;
  const state = document.querySelector('#top-live');
  let source, controller, revision, rendered = content.innerHTML, fetching = false, pending = false, lastSync = 0;
  async function update() {
    if (document.hidden || fetching) { pending = true; return; }
    fetching = true;
    pending = false;
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller?.abort(), 15000);
    try {
      const response = await fetch('/top/status', { cache: 'no-store', credentials: 'same-origin', signal: controller.signal });
      if (response.redirected) { window.location.assign(response.url); return; }
      if (!response.ok) throw new Error('refresh failed');
      const data = await response.json();
      lastSync = Date.now();
      state.textContent = '自动更新';
      if (data.revision === revision) return;
      revision = data.revision;
      if (data.content === rendered) return;
      const opened = new Set([...content.querySelectorAll('details[open]')].map(el => el.dataset.detailsId));
      const focused = document.activeElement?.closest('details')?.dataset.detailsId;
      content.innerHTML = data.content;
      rendered = data.content;
      for (const el of content.querySelectorAll('details')) {
        el.open = opened.has(el.dataset.detailsId);
        if (el.dataset.detailsId === focused) el.querySelector('summary')?.focus({ preventScroll: true });
      }
    } catch {
      state.textContent = '重连中';
    } finally {
      window.clearTimeout(timeout);
      controller = undefined;
      fetching = false;
      if (pending && !document.hidden) window.setTimeout(update, 300);
    }
  }
  function connect() {
    if (source || !('EventSource' in window)) return;
    source = new EventSource('/top/stream');
    source.addEventListener('update', update);
    source.addEventListener('open', update);
    source.addEventListener('error', () => { state.textContent = '重连中'; update(); });
  }
  document.querySelector('#top-refresh').addEventListener('click', update);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
  window.setInterval(() => { if (Date.now() - lastSync > 15000) update(); }, 15000);
  window.addEventListener('pagehide', () => { source?.close(); source = undefined; controller?.abort(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { connect(); update(); } });
  connect();
  update();
});
