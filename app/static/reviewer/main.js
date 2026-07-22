const pendingEl = document.getElementById('pending');
const resolverEl = document.getElementById('resolver');
const saveBtn = document.getElementById('saveToken');
const tokenInput = document.getElementById('authToken');

function getAuthHeader() {
  const t = localStorage.getItem('auth_token');
  return t ? { Authorization: 'Bearer ' + t } : {};
}

saveBtn.addEventListener('click', () => {
  localStorage.setItem('auth_token', tokenInput.value.trim());
  loadPending();
});

async function loadPending() {
  pendingEl.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/reviews/pending', { headers: getAuthHeader() });
    if (!res.ok) throw new Error('Unauthorized or service error');
    const data = await res.json();
    if (!data.length) {
      pendingEl.innerHTML = '<p>No pending reviews</p>';
      return;
    }
    pendingEl.innerHTML = '';
    data.forEach(r => {
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `<strong>${r.review_id}</strong> — ${r.reason} <br/><small>client: ${r.client_id}</small>`;
      card.addEventListener('click', () => showResolver(r));
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
    <input id="reviewerName" placeholder="your name" />
    <label>Notes</label>
    <textarea id="notes"></textarea>
    <button id="submitResolve">Submit</button>
  `;
  resolverEl.appendChild(html);
  document.getElementById('submitResolve').addEventListener('click', () => submitResolve(review.review_id));
}

async function submitResolve(reviewId) {
  const decision = document.getElementById('decisionSel').value;
  const reviewer = document.getElementById('reviewerName').value || 'unknown';
  const notes = document.getElementById('notes').value || '';
  try {
    const res = await fetch(`/api/reviews/${reviewId}/resolve`, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeader()),
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
loadPending();
