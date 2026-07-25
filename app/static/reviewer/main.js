const pendingEl = document.getElementById('pending');
const resolverEl = document.getElementById('resolver');

// Auth is handled by the shared /static/auth.js: it attaches the signed-in
// session's Bearer token to every /api/ fetch and bounces to /login on 401.
// No page-local token entry needed — this page just uses the current session.

// Only risk managers/reviewers may resolve (matches the dashboard and the
// server's reviews.resolve permission). Other signed-in staff see the queue
// read-only. An anonymous dev session (no roles) keeps full action, so the
// offline demo still works without logging in.
const roles = (window.ibAuth && window.ibAuth.roles) || [];
const canResolve = !roles.length || roles.includes('risk_manager') || roles.includes('reviewer');

function renderViewOnlyBanner() {
  if (canResolve) return;
  const banner = document.createElement('p');
  banner.className = 'viewonly';
  banner.textContent = 'View only — your role can see the review queue but cannot resolve reviews.';
  pendingEl.parentElement.insertBefore(banner, pendingEl);
}

async function loadPending() {
  pendingEl.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/reviews/pending');
    if (!res.ok) throw new Error('Unauthorized or service error');
    const data = await res.json();
    if (!data.length) {
      pendingEl.innerHTML = '<p>No pending reviews</p>';
      return;
    }
    pendingEl.innerHTML = '';
    data.forEach(r => {
      const card = document.createElement('div');
      card.className = 'review-item';
      card.innerHTML = `<strong>${r.review_id}</strong> — ${r.reason} <br/><small>client: ${r.client_id}</small>`;
      if (canResolve) {
        card.addEventListener('click', () => showResolver(r));
      } else {
        card.style.cursor = 'default';
      }
      pendingEl.appendChild(card);
    });
  } catch (err) {
    pendingEl.innerHTML = `<p class="error">${err.message}</p>`;
  }
}

function showResolver(review) {
  resolverEl.innerHTML = '';
  const html = document.createElement('div');
  html.innerHTML = `
    <h3>Resolve ${review.review_id}</h3>
    <p>${review.reason}</p>
    <label>Decision</label>
    <select id="decisionSel"><option value="approve">approve</option><option value="reject">reject</option></select>
    <label>Reviewer name</label>
    <input id="reviewerName" placeholder="your name" value="${(window.ibAuth && window.ibAuth.user) || ''}" />
    <label>Notes</label>
    <textarea id="notes"></textarea>
    <button id="submitResolve" class="btn pri">Submit</button>
  `;
  resolverEl.appendChild(html);
  document.getElementById('submitResolve').addEventListener('click', () => submitResolve(review.review_id));
}

async function submitResolve(reviewId) {
  const decision = document.getElementById('decisionSel').value;
  const reviewer = document.getElementById('reviewerName').value
    || (window.ibAuth && window.ibAuth.user) || 'unknown';
  const notes = document.getElementById('notes').value || '';
  try {
    const res = await fetch(`/api/reviews/${reviewId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reviewer, notes }),
    });
    if (!res.ok) throw new Error('Failed to resolve');
    alert('Resolved');
    loadPending();
    resolverEl.innerHTML = '<p>Select another review</p>';
  } catch (err) {
    alert(err.message);
  }
}

// initial load
if (!canResolve) {
  resolverEl.innerHTML = '<p>View only — resolving is restricted to reviewers.</p>';
}
renderViewOnlyBanner();
loadPending();
