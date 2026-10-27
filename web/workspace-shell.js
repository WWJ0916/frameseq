/* Every workspace uses the same server-rendered sidebar, including deep links. */
(() => {
  const shell = document.querySelector('[data-workspace-shell]');
  if (!shell) return;
  const query = new URLSearchParams(location.search);
  const view = ['works', 'tasks'].includes(query.get('view')) ? query.get('view') : 'create';
  for (const link of shell.querySelectorAll('.nav-item')) {
    const url = new URL(link.href, location.href);
    const selected = url.pathname === location.pathname &&
      (!link.dataset.view || link.dataset.view === view) && !url.searchParams.has('panel');
    link.classList.toggle('active', selected);
    if (selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const theme = document.getElementById('theme');
  function applyTheme(value) {
    document.documentElement.dataset.theme = value;
    theme.textContent = value === 'dark' ? '☼' : '☾';
    theme.title = value === 'dark' ? '切换浅色模式' : '切换深色模式';
    theme.setAttribute('aria-label', theme.title);
  }
  let stored = 'dark';
  try { stored = JSON.parse(localStorage.getItem('frameseq-theme')); } catch {}
  applyTheme(stored === 'light' ? 'light' : 'dark');
  theme.addEventListener('click', () => {
    const value = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(value);
    try { localStorage.setItem('frameseq-theme', JSON.stringify(value)); } catch {}
  });
  window.frameseqSession.then(async session => {
    if (!session.authenticated) return;
    document.getElementById('connection').textContent = '服务已连接';
    document.getElementById('status-dot').classList.add('connected');
    if (location.pathname === '/studio') return; // Studio maintains its live counters.
    try {
      const response = await fetch('/api/library?limit=100');
      if (!response.ok) return;
      const data = await response.json();
      document.getElementById('nav-works').textContent = String(data.items.filter(item => item.status === 'succeeded' && item.metrics?.visual_review?.approved !== false).length);
      document.getElementById('nav-active').textContent = String(data.items.filter(item => ['queued', 'running', 'submitting', 'waiting', 'downloading', 'unknown'].includes(item.status)).length);
    } catch { /* Unknown counters stay as dashes; do not invent zeroes. */ }
  }).catch(() => {});
})();
