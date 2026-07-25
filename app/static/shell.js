/* Renders the shared staff-app sidebar into <aside id="appSidebar">.
 * The active item comes from <body data-page="dashboard|chat|reviewer|telemetry">.
 * Keeps navigation defined in one place across all pages. */
(function () {
  const page = (document.body && document.body.dataset.page) || "";
  const icon = {
    dashboard: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    chat: '<svg viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
    reviewer: '<svg viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    telemetry: '<svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  };
  const items = [
    { section: "Overview" },
    { href: "/dashboard", key: "dashboard", label: "Dashboard" },
    { href: "/chat", key: "chat", label: "Copilot chat" },
    { href: "/reviewer", key: "reviewer", label: "Review queue" },
  ];
  let html =
    '<div class="brand"><span class="mark" aria-hidden="true">' +
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 13h3.5l2 5 4-13 2.5 8H21"/></svg>' +
    '</span><div class="brand-text"><div class="brand-name">Compliance</div><div class="brand-sub">Agentic AI Platform</div></div></div>';
  for (const it of items) {
    if (it.section) { html += '<div class="nav-label">' + it.section + "</div>"; continue; }
    const active = it.key === page ? " active" : "";
    html += '<a class="nav-item' + active + '" href="' + it.href + '">' + (icon[it.key] || "") + "<span>" + it.label + "</span></a>";
  }
  html += '<div class="side-foot"><span class="badge live"><span class="dot"></span>Systems live</span></div>';
  const el = document.getElementById("appSidebar");
  if (el) el.innerHTML = html;
})();
