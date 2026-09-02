// ============================================================
// UTILS
// ============================================================
async function apiFetch(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = '', 3000);
}

function fmt(dt) {
  if (!dt) return '—';
  return new Date(dt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function esc(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Returns true only for an http(s) URL — use before rendering any
// user-supplied string as a clickable href (e.g. an org's website URL in the
// admin panel). The backend already restricts these to http(s) at creation
// time; this is a second line of defense so a stray javascript:/data: URI
// never becomes a clickable link in a privileged page.
function safeHttpUrl(url) {
  return /^https?:\/\//i.test(String(url || ''));
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  const open = sidebar.classList.toggle('open');
  if (overlay) overlay.classList.toggle('show', open);
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  const overlay = document.getElementById('sidebar-overlay');
  if (overlay) overlay.classList.remove('show');
}

// Pressing Enter while focused in any of the given input element IDs
// triggers callback — lets a form submit without reaching for the mouse.
// Skips <textarea> automatically (Enter there means newline, not submit).
function onEnter(ids, callback) {
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el && el.tagName !== 'TEXTAREA') {
      el.addEventListener('keydown', e => { if (e.key === 'Enter') callback(); });
    }
  });
}

// Button loading state — call with true to start, false to restore.
// Stores original HTML on the element so restore is always accurate.
function btnLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn._origHTML = btn.innerHTML;
    btn._origDisabled = btn.disabled;
    btn.disabled = true;
    btn.innerHTML = '⏳ Sending…';
  } else {
    btn.disabled = btn._origDisabled || false;
    btn.innerHTML = btn._origHTML || btn.innerHTML;
  }
}

