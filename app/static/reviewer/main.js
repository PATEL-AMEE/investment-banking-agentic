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

const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const sevClass = s => s === 'high' ? 'crit' : s === 'medium' ? 'warn' : 'muted';

function detailRow(label, value) {
  return value ? `<div class="rv-row"><dt>${label}</dt><dd>${esc(value)}</dd></div>` : '';
}

function showResolver(review) {
  const d = review.details || {};
  const evidence = Array.isArray(d.evidence) ? d.evidence : [];
  resolverEl.innerHTML = `
    <div class="rv-head">
      <b class="idc">${esc(review.review_id)}</b>
      <span class="pill ${sevClass(review.severity)}">${esc(review.severity || '—')}</span>
    </div>
    <p class="rv-reason">${esc(review.reason || '')}</p>
    <dl class="rv-detail">
      ${detailRow('Client', review.client_id)}
      ${detailRow('Risk level', d.risk_level)}
      ${detailRow('Assessment', d.decision_summary)}
      ${detailRow('Agent recommendation', d.agent_recommendation)}
      ${Array.isArray(d.policy_references) && d.policy_references.length ? detailRow('Policies', d.policy_references.join(', ')) : ''}
    </dl>
    ${evidence.length ? `<div class="rv-ev"><div class="rv-ev-cap">Evidence</div>${evidence.map(e => `<div class="rv-ev-item">${esc(e)}</div>`).join('')}</div>` : ''}
    <label for="notes">Notes (optional)</label>
    <textarea id="notes" placeholder="Add a note for the audit trail…"></textarea>
    <div class="rv-actions">
      <button class="btn approve" id="doApprove" type="button">✓ Approve</button>
      <button class="btn reject" id="doReject" type="button">✕ Reject</button>
    </div>
  `;
  document.getElementById('doApprove').addEventListener('click', () => submitResolve(review.review_id, 'approve'));
  document.getElementById('doReject').addEventListener('click', () => submitResolve(review.review_id, 'reject'));
}

async function submitResolve(reviewId, decision) {
  const reviewer = (window.ibAuth && window.ibAuth.user) || 'reviewer';
  const notesEl = document.getElementById('notes');
  const notes = (notesEl && notesEl.value) || '';
  const buttons = resolverEl.querySelectorAll('.rv-actions button');
  buttons.forEach(b => (b.disabled = true));
  try {
    const res = await fetch(`/api/reviews/${reviewId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reviewer, notes }),
    });
    if (!res.ok) {
      throw new Error(res.status === 403 ? 'You do not have permission to resolve reviews.' : 'Could not resolve (HTTP ' + res.status + ')');
    }
    resolverEl.innerHTML = `<p class="rv-done">✓ ${decision === 'approve' ? 'Approved' : 'Rejected'} ${esc(reviewId)}. Select another review from the list.</p>`;
    loadPending();
  } catch (err) {
    alert(err.message);
    buttons.forEach(b => (b.disabled = false));
  }
}

// initial load
if (!canResolve) {
  resolverEl.innerHTML = '<p>View only — resolving is restricted to reviewers.</p>';
}
renderViewOnlyBanner();
loadPending();
