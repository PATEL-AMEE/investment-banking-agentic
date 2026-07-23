/* Shared browser session for the platform UIs.
 *
 * - Reads the JWT stored by /login (sessionStorage) and transparently
 *   attaches it as a Bearer header to every same-origin /api/ fetch, so
 *   individual pages need no auth-specific code.
 * - 401 responses clear the session and bounce to /login (token expired
 *   or REQUIRE_AUTH is on).
 * - Renders a user badge (name · role + sign in/out) into #authslot when
 *   the page provides one.
 */
(function () {
  const TOKEN_KEY = "ib_token";
  const USER_KEY = "ib_user";
  const ROLES_KEY = "ib_roles";

  const token = sessionStorage.getItem(TOKEN_KEY);
  const user = sessionStorage.getItem(USER_KEY);
  let roles = [];
  try { roles = JSON.parse(sessionStorage.getItem(ROLES_KEY) || "[]"); } catch (e) { /* ignore */ }

  const ROLE_LABELS = {
    compliance_analyst: "Compliance Analyst",
    onboarding_officer: "Onboarding Officer",
    risk_manager: "Risk Manager",
    executive: "Executive (view-only)",
    analyst: "Analyst",
    reviewer: "Reviewer",
  };

  function isApiUrl(url) {
    if (typeof url !== "string") return false;
    return url.startsWith("/api/") || url.startsWith(location.origin + "/api/");
  }

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url);
    if (token && isApiUrl(url)) {
      init = init || {};
      const headers = new Headers(init.headers || {});
      if (!headers.has("Authorization")) headers.set("Authorization", "Bearer " + token);
      init.headers = headers;
    }
    return originalFetch.call(this, input, init).then(function (response) {
      if (response.status === 401 && isApiUrl(url) && location.pathname !== "/login") {
        sessionStorage.removeItem(TOKEN_KEY);
        location.href = "/login?next=" + encodeURIComponent(location.pathname);
      }
      return response;
    });
  };

  window.ibAuth = {
    token: token,
    user: user,
    roles: roles,
    roleLabel: function () {
      return roles.length ? roles.map(function (r) { return ROLE_LABELS[r] || r; }).join(", ") : "no role";
    },
    signOut: function () {
      sessionStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(USER_KEY);
      sessionStorage.removeItem(ROLES_KEY);
      location.href = "/login";
    },
  };

  function renderBadge() {
    const slot = document.getElementById("authslot");
    if (!slot) return;
    if (token && user) {
      const badge = document.createElement("span");
      badge.className = "btn";
      badge.style.cursor = "default";
      badge.textContent = "👤 " + user + " · " + window.ibAuth.roleLabel();
      const out = document.createElement("button");
      out.className = "btn";
      out.type = "button";
      out.textContent = "Sign out";
      out.addEventListener("click", window.ibAuth.signOut);
      slot.appendChild(badge);
      slot.appendChild(out);
    } else {
      const login = document.createElement("a");
      login.className = "btn";
      login.href = "/login?next=" + encodeURIComponent(location.pathname);
      login.textContent = "🔐 Sign in";
      slot.appendChild(login);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderBadge);
  } else {
    renderBadge();
  }
})();
