document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-report-day]');
  if (!page) return;
  const content = document.querySelector('#report-content');
  const history = document.querySelector('#report-history');
  const state = document.querySelector('#report-live-state');
  const params = new URLSearchParams({ date: page.dataset.reportDay });
  let fetching = false;
  let pending = false;
  let fallback;
  const dateInput = document.querySelector('#report-date');
  dateInput.addEventListener('change', () => {
    if (dateInput.checkValidity()) dateInput.form.requestSubmit();
  });

  async function update() {
    if (document.hidden || fetching) { pending = true; return; }
    fetching = true;
    pending = false;
    try {
      const response = await fetch(`/reports/status?${params}`, { cache: 'no-store', credentials: 'same-origin' });
      if (response.redirected) { window.location.assign(response.url); return; }
      if (!response.ok) throw new Error('refresh failed');
      const data = await response.json();
      const opened = new Set([...content.querySelectorAll('details[open]')].map(el => el.dataset.detailsId));
      const activeButton = document.activeElement?.closest('#report-content button');
      content.innerHTML = data.content;
      history.innerHTML = data.history;
      for (const el of content.querySelectorAll('details')) {
        el.open = opened.has(el.dataset.detailsId);
      }
      if (activeButton) content.querySelector('button')?.focus({ preventScroll: true });
      state.textContent = '自动更新';
    } catch {
      state.textContent = '重连中';
      startFallback();
    } finally {
      fetching = false;
      if (pending && !document.hidden) window.setTimeout(update, 300);
    }
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
  function startFallback() {
    if (!fallback) fallback = window.setInterval(update, 8000);
  }
  if ('EventSource' in window) {
    const source = new EventSource(`/reports/stream?${params}`);
    source.addEventListener('update', update);
    source.addEventListener('open', () => {
      window.clearInterval(fallback);
      fallback = undefined;
      state.textContent = '自动更新';
      if (pending) update();
    });
    source.addEventListener('error', () => { state.textContent = '重连中'; startFallback(); });
    window.addEventListener('pagehide', () => source.close());
  } else startFallback();
});
