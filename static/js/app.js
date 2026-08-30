// ============================================================
// SIDEBAR — STATS + COLLAPSE
// ============================================================
async function loadSidebarStats() {
  try {
    const s = await apiFetch('/stats');
    document.getElementById('stat-nets').textContent   = s.total_nets;
    document.getElementById('stat-active').textContent = s.active_sessions;
    document.getElementById('stat-today').textContent  = s.checkins_today;
  } catch { /* non-critical; silently ignore */ }
}

function toggleSidebarCollapse() {
  const sidebar = document.getElementById('sidebar');
  const btn     = document.getElementById('sidebar-collapse-btn');
  const collapsed = sidebar.classList.toggle('collapsed');
  btn.textContent = collapsed ? '▶' : '◀';
  btn.title       = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  localStorage.setItem('nt_sidebar_collapsed', collapsed ? '1' : '0');
}

function collapseSidebar() {
  const sidebar = document.getElementById('sidebar');
  const btn     = document.getElementById('sidebar-collapse-btn');
  if (!sidebar || sidebar.classList.contains('collapsed')) return;
  sidebar.classList.add('collapsed');
  if (btn) { btn.textContent = '▶'; btn.title = 'Expand sidebar'; }
  localStorage.setItem('nt_sidebar_collapsed', '1');
}

function restoreSidebarCollapse() {
  if (localStorage.getItem('nt_sidebar_collapsed') === '1') {
    const sidebar = document.getElementById('sidebar');
    const btn     = document.getElementById('sidebar-collapse-btn');
    if (sidebar) sidebar.classList.add('collapsed');
    if (btn) { btn.textContent = '▶'; btn.title = 'Expand sidebar'; }
  }
}

// ============================================================
// NET CONTROL MODE — simplified, large-tap-target mobile view (issue #10)
// ============================================================
function toggleNetControlMode() {
  const panel = document.getElementById('live-session-panel');
  const btn = document.getElementById('net-control-mode-btn');
  const on = panel.classList.toggle('net-control-mode');
  if (btn) btn.textContent = on ? '📱 Full View' : '📱 Net Control';
  if (on) collapseSidebar();
  localStorage.setItem('nt_net_control_mode', on ? '1' : '0');
}

function restoreNetControlMode() {
  if (localStorage.getItem('nt_net_control_mode') === '1') {
    const panel = document.getElementById('live-session-panel');
    const btn = document.getElementById('net-control-mode-btn');
    if (panel) panel.classList.add('net-control-mode');
    if (btn) btn.textContent = '📱 Full View';
  }
}

// ============================================================
// SET PASSWORD (admin-created account invite link, issue #1 follow-up)
// Landed here from the "set your password" email (?setpw=TOKEN) -- replaces
// the login/register tabs with a one-time password-set form. Checked before
// the auto-login below so the invite flow always shows its own form instead
// of silently dropping into whatever account happens to already be logged
// in on this browser.
// ============================================================
window._setpwToken = new URLSearchParams(window.location.search).get('setpw');
if (window._setpwToken) {
  document.getElementById('auth-tabs').style.display = 'none';
  document.getElementById('tab-login').style.display = 'none';
  document.getElementById('tab-register').style.display = 'none';
  document.getElementById('tab-setpassword').style.display = '';
}

// Auto-login if token stored
if (token && !window._setpwToken) {
  enterApp();
} else {
  loadLoginMessage();   // fire-and-forget; non-blocking — staying on the login screen
}

// Bot protection: Turnstile / reCAPTCHA / ALTCHA (issue #1 follow-up) —
// no-op if CAPTCHA_PROVIDER isn't configured
initCaptcha();

// Enter to submit login
document.getElementById('login-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
document.getElementById('login-user').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
onEnter(['reg-call', 'reg-name', 'reg-email', 'reg-pass'], () => doRegister(document.querySelector('#tab-register button.btn-primary')));
onEnter(['setpw-pass', 'setpw-pass2'], () => doSetPassword(document.querySelector('#tab-setpassword button.btn-primary')));

// ============================================================
// KEYBOARD SHORTCUT  /  → focus callsign input
// ============================================================
document.addEventListener('keydown', e => {
  if (e.key !== '/') return;
  const active = document.activeElement;
  const tag = active ? active.tagName : '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const ciCall = document.getElementById('ci-call');
  if (!ciCall || ciCall.offsetParent === null) return;  // not visible
  e.preventDefault();
  ciCall.focus();
});

// ============================================================
// EMAIL VERIFICATION REDIRECT
// Landed here from the link in a verification email (?verified=1/0) --
// show a toast and strip the param so a page reload doesn't repeat it.
// ============================================================
(function showVerifiedToast() {
  const params = new URLSearchParams(window.location.search);
  const v = params.get('verified');
  if (v === null) return;
  if (v === '1') {
    toast('Email verified! You can log in once your account has been approved.', 'success');
  } else {
    toast('That verification link is invalid or has already been used.', 'error');
  }
  params.delete('verified');
  const rest = params.toString();
  window.history.replaceState({}, '', window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
})();

