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

// Keeps --header-h in sync with the header's actual rendered height (issue
// follow-up) -- needed so #sidebar's height:calc(100vh - var(--header-h))
// can fill exactly to the bottom of the screen without either overflowing
// past it (forcing an unwanted page scroll on every load) or falling short
// of it. Can't just hardcode a pixel value: an org's own logo/tagline/name
// in the header (#org-banner, custom logo image) makes its real height
// vary per org, not a fixed constant -- confirmed live, one org's branded
// header rendered at over 300px, nowhere near the base 52px. ResizeObserver
// (rather than a resize listener alone) catches every cause of that height
// changing, not just window resizes -- a logo image finishing its load,
// a banner's text wrapping differently, a language switch changing string
// lengths -- all fire it automatically.
(function () {
  // #app > header specifically -- index.html also has a separate, hidden
  // #auth-header for the pre-login screen that stays in the DOM after
  // login; a bare 'header' selector matches that one first (it's earlier
  // in the document) and measures 0, not the real app header.
  const header = document.querySelector('#app > header');
  if (!header || !window.ResizeObserver) return;
  const setHeaderHeight = () => {
    document.documentElement.style.setProperty('--header-h', header.offsetHeight + 'px');
  };
  setHeaderHeight();
  new ResizeObserver(setHeaderHeight).observe(header);
})();

// Scheduled-maintenance banner (nightly deploy cron, see "Nightly deploy" in
// README.md) -- polls GET /maintenance-notice and shows/hides a fixed banner
// accordingly. Runs on every page that loads this file, logged in or not,
// since a restart affects everyone regardless of auth state. No login is
// required for the endpoint itself, so this works pre-login too.
(function () {
  let banner = null;
  function showBanner(message) {
    if (banner) return;
    banner = document.createElement('div');
    banner.id = 'maintenance-notice';
    banner.setAttribute('role', 'status');
    banner.textContent = '🔧 ' + message;
    document.body.prepend(banner);
  }
  function hideBanner() {
    if (!banner) return;
    banner.remove();
    banner = null;
  }
  async function poll() {
    try {
      const res = await fetch('/maintenance-notice');
      if (!res.ok) return;
      const data = await res.json();
      if (data.active) showBanner(data.message); else hideBanner();
    } catch {
      // Offline or request failed -- leave whatever's currently showing
      // alone rather than flicker the banner on a transient network blip.
    }
  }
  poll();
  setInterval(poll, 60000);
})();

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

