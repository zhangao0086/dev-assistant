(function () {
  // Inject header CSS
  const style = document.createElement('style');
  style.textContent = `
    header { padding: 16px 24px; border-bottom: 1px solid #1e2433; display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 18px; font-weight: 600; }
    header h1 a { color: inherit; text-decoration: none; }
    header h1 a:hover { opacity: 0.8; }
    .header-links { margin-left: auto; display: flex; align-items: center; gap: 16px; }
    .header-links a { color: #94a3b8; text-decoration: none; font-size: 13px; }
    .header-links a:hover { text-decoration: underline; }
    .header-links a.active { color: #60a5fa; }
    #app-header { min-height: 55px; }
    html { scrollbar-gutter: stable; overflow-y: scroll; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0a0d14; }
    ::-webkit-scrollbar-thumb { background: #2d3748; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #3d4758; }
    .repo-selector { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 6px; color: #e2e8f0; font-size: 12px; padding: 5px 10px; outline: none; cursor: pointer; max-width: 200px; }
    .repo-selector:focus { border-color: #3b82f6; }
    .repo-selector option { background: #1a1f2e; color: #e2e8f0; }
  `;
  document.head.appendChild(style);

  // Render header HTML with active state
  const NAV = [
    { href: '/',                 label: 'Tasks' },
    { href: '/cron.html',        label: 'Cron Jobs' },
    { href: '/cost-center.html', label: 'Cost Center' },
    { href: '/settings.html',    label: 'Settings' },
  ];

  const current = location.pathname === '/' ? '/' : location.pathname;

  const links = NAV.map(({ href, label }) =>
    `<a href="${href}"${href === current ? ' class="active"' : ''}>${label}</a>`
  ).join('\n    ');

  document.getElementById('app-header').outerHTML = `
<header>
  <h1><a href="/">Dev Assistant</a></h1>
  <select id="repo-selector" class="repo-selector" onchange="window._onRepoChange(this.value)">
    <option value="">All Repositories</option>
  </select>
  <nav class="header-links">
    ${links}
  </nav>
</header>`;

  // Repo selector logic
  window.repos = [];
  window.selectedRepoId = localStorage.getItem('dev-assistant-repo') || '';

  window._onRepoChange = function (repoId) {
    window.selectedRepoId = repoId;
    localStorage.setItem('dev-assistant-repo', repoId);
    if (window.onRepoChanged) window.onRepoChanged(repoId);
  };

  window._loadRepos = async function () {
    try {
      const res = await fetch('/repos');
      if (!res.ok) return;
      window.repos = await res.json();
      const sel = document.getElementById('repo-selector');
      if (!sel) return;

      const saved = window.selectedRepoId;
      sel.innerHTML = '<option value="">All Repositories</option>' +
        window.repos.map(r =>
          `<option value="${r.id}"${r.id === saved ? ' selected' : ''}>${r.name}</option>`
        ).join('');

      // If saved repo no longer exists, reset
      if (saved && !window.repos.find(r => r.id === saved)) {
        window.selectedRepoId = '';
        localStorage.removeItem('dev-assistant-repo');
        sel.value = '';
      }
    } catch (e) {}
  };

  window._loadRepos();
})();
