document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('form.filters');
  if (!form) return;
  const rows = document.querySelector('#post-rows');
  const chart = document.querySelector('#activity-chart');
  const count = document.querySelector('#visible-count');
  const today = document.querySelector('#today-posts');
  const total = document.querySelector('#total-posts');
  const aiCount = document.querySelector('#ai-count');
  const checked = document.querySelector('#last-checked');
  const accountList = document.querySelector('#account-list');
  const liveState = document.querySelector('#live-state');
  let controller;
  let latestRequest = 0;
  let refreshTimer;
  let fallbackTimer;
  let pendingLive = false;
  let pendingAccounts = false;

  function startFallback() {
    if (fallbackTimer) return;
    fallbackTimer = window.setInterval(() => {
      if (!document.hidden) update(false, { appliedUrl: true, refreshAccounts: true });
    }, 30000);
  }

  function selectedUrl() {
    const url = new URL(form.action, window.location.href);
    for (const [name, value] of new FormData(form)) {
      if (String(value).trim()) url.searchParams.set(name, String(value).trim());
    }
    return url;
  }

  async function update(pushHistory, { appliedUrl = false, refreshAccounts = false } = {}) {
    const url = appliedUrl ? new URL(window.location.href) : selectedUrl();
    if (pushHistory && url.href !== window.location.href) {
      window.history.pushState(null, '', url);
    }
    controller?.abort();
    controller = new AbortController();
    const requestNumber = ++latestRequest;
    form.setAttribute('aria-busy', 'true');
    const partialUrl = new URL(url);
    partialUrl.searchParams.set('_partial', '1');
    try {
      const response = await fetch(partialUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (requestNumber !== latestRequest) return;
      const opened = new Set([...rows.querySelectorAll('tr[data-post-id] details[open]')]
        .map((item) => item.closest('tr').dataset.postId));
      rows.innerHTML = data.rows_html;
      for (const item of rows.querySelectorAll('tr[data-post-id] details')) {
        if (opened.has(item.closest('tr').dataset.postId)) item.open = true;
      }
      chart.innerHTML = data.chart_html;
      count.textContent = String(data.count);
      today.textContent = String(data.today);
      total.textContent = String(data.total_posts);
      aiCount.textContent = String(data.ai_count);
      checked.textContent = data.last_checked;
      if (refreshAccounts && !accountList.querySelector('.account-manage[open]')) {
        accountList.innerHTML = data.accounts_html;
      }
    } catch (error) {
      if (error.name !== 'AbortError' && requestNumber === latestRequest) {
        window.location.assign(url.href);
      }
    } finally {
      if (requestNumber === latestRequest) form.removeAttribute('aria-busy');
    }
  }

  for (const name of ['account', 'category']) {
    const select = form.elements.namedItem(name);
    select.addEventListener('change', () => update(true));
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    update(true);
  });
  window.addEventListener('popstate', () => {
    const params = new URL(window.location.href).searchParams;
    for (const name of ['account', 'category', 'q']) {
      form.elements.namedItem(name).value = params.get(name) || '';
    }
    update(false);
  });

  function scheduleLiveUpdate(change) {
    if (change.targets) {
      window.location.reload();
      return;
    }
    pendingLive = true;
    pendingAccounts ||= Boolean(change.posts || change.status);
    if (document.hidden || refreshTimer) return;
    refreshTimer = window.setTimeout(() => {
      refreshTimer = undefined;
      if (document.hidden) return;
      pendingLive = false;
      const refreshAccounts = pendingAccounts;
      pendingAccounts = false;
      update(false, { appliedUrl: true, refreshAccounts });
    }, 1000);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && pendingLive) scheduleLiveUpdate({});
  });

  if ('EventSource' in window) {
    const source = new EventSource('/events');
    let hasConnected = false;
    source.addEventListener('open', () => {
      liveState.textContent = '自动更新';
      liveState.classList.add('connected');
      if (fallbackTimer) window.clearInterval(fallbackTimer);
      fallbackTimer = undefined;
      if (hasConnected) scheduleLiveUpdate({ posts: true, status: true });
      hasConnected = true;
    });
    source.addEventListener('update', (event) => {
      try {
        scheduleLiveUpdate(JSON.parse(event.data));
      } catch {
        scheduleLiveUpdate({});
      }
    });
    source.addEventListener('error', () => {
      liveState.textContent = '重连中';
      liveState.classList.remove('connected');
      startFallback();
    });
  } else {
    liveState.textContent = '定时更新';
    startFallback();
  }
});
