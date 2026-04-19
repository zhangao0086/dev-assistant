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
  <nav class="header-links">
    ${links}
  </nav>
</header>`;
})();
