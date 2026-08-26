// ============================================================
// AUTH
// ============================================================
function switchAuthTab(tab) {
  document.querySelectorAll('.auth-tab').forEach((el, i) => {
    el.classList.toggle('active', (i === 0 && tab === 'login') || (i === 1 && tab === 'register'));
  });
  document.getElementById('tab-login').style.display = tab === 'login' ? '' : 'none';
  document.getElementById('tab-register').style.display = tab === 'register' ? '' : 'none';
  document.getElementById('auth-error').style.display = 'none';
  if (tab === 'register') loadRegOrgPicker();
}

// Multi-tenancy (issue #1) — the registration form's "create new" vs "join
// existing" organization choice. No auth required for GET /orgs; safe to
// call before login.
async function loadRegOrgPicker() {
  const select = document.getElementById('reg-org-select');
  if (select.dataset.loaded) return;
  try {
    const orgs = await apiFetch('/orgs');
    select.innerHTML = orgs.map(o => `<option value="${o.slug}">${esc(o.name)}</option>`).join('');
    select.dataset.loaded = '1';
  } catch { /* leave empty — join is a no-op if this fails, create still works */ }
}

function updateRegOrgChoice() {
  const create = document.querySelector('input[name="reg-org-action"]:checked').value === 'create';
  document.getElementById('reg-org-name').style.display = create ? '' : 'none';
  document.getElementById('reg-org-website').style.display = create ? '' : 'none';
  document.getElementById('reg-org-select').style.display = create ? 'none' : '';
  document.getElementById('reg-org-hint').textContent = create
    ? "You'll be this organization's admin, pending a super admin's approval before you can log in."
    : "An admin of that organization must approve you before you can log in.";
}

// ============================================================
// CLOUDFLARE TURNSTILE (bot protection on login/register)
// ============================================================
// Opt-in server-side (see /auth/config) — the widgets stay hidden and no
// script is ever loaded from Cloudflare unless an admin has actually
// configured TURNSTILE_SITE_KEY/SECRET_KEY.
let turnstileLoginWidgetId = null;
let turnstileRegWidgetId = null;

async function initTurnstile() {
  let config;
  try { config = await apiFetch('/auth/config'); } catch { return; }
  if (!config.turnstile_enabled) return;

  window._onTurnstileLoad = () => {
    turnstileLoginWidgetId = turnstile.render('#login-turnstile', { sitekey: config.turnstile_site_key });
    turnstileRegWidgetId = turnstile.render('#reg-turnstile', { sitekey: config.turnstile_site_key });
    document.getElementById('login-turnstile').style.display = '';
    document.getElementById('reg-turnstile').style.display = '';
  };
  const script = document.createElement('script');
  script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_onTurnstileLoad&render=explicit';
  script.async = true;
  script.defer = true;
  document.head.appendChild(script);
}

async function doLogin() {
  clearAuthError();
  const user = document.getElementById('login-user').value.trim();
  const pass = document.getElementById('login-pass').value;
  if (!user || !pass) return showAuthError('Fill in all fields');
  try {
    const params = { username: user, password: pass };
    if (turnstileLoginWidgetId !== null) params.turnstile_token = turnstile.getResponse(turnstileLoginWidgetId) || '';
    const form = new URLSearchParams(params);
    const res = await fetch(API + '/auth/login', {
      method: 'POST', body: form,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Login failed');
    token = data.access_token;
    currentUser = data.user;
    localStorage.setItem('nt_token', token);
    enterApp();
  } catch (e) {
    showAuthError(e.message);
    // Turnstile tokens are single-use -- must reset before another attempt.
    if (turnstileLoginWidgetId !== null) turnstile.reset(turnstileLoginWidgetId);
  }
}

async function doRegister(btn) {
  clearAuthError();
  const callsign = document.getElementById('reg-call').value.trim().toUpperCase();
  const name = document.getElementById('reg-name').value.trim();
  const email = document.getElementById('reg-email').value.trim();
  const password = document.getElementById('reg-pass').value;
  if (!callsign || !name || !email || !password) return showAuthError('Fill in all fields');
  // Multi-tenancy (issue #1) — which org to create or join
  const creatingOrg = document.querySelector('input[name="reg-org-action"]:checked').value === 'create';
  const body = { callsign, name, email, password };
  if (creatingOrg) {
    const orgName = document.getElementById('reg-org-name').value.trim();
    if (!orgName) return showAuthError('Enter a name for your new organization');
    const orgWebsite = document.getElementById('reg-org-website').value.trim();
    if (!orgWebsite) return showAuthError("Enter your new organization's website URL");
    body.org_name = orgName;
    body.org_website_url = orgWebsite;
  } else {
    const orgSlug = document.getElementById('reg-org-select').value;
    if (!orgSlug) return showAuthError('Select an organization to join, or switch to "Create new"');
    body.org_slug = orgSlug;
  }
  if (turnstileRegWidgetId !== null) body.turnstile_token = turnstile.getResponse(turnstileRegWidgetId) || '';
  btnLoading(btn, true);
  try {
    const newUser = await apiFetch('/auth/register', { method: 'POST', body: JSON.stringify(body) });
    btnLoading(btn, false);
    if (newUser.is_active) {
      // Only the instance's literal first-ever user is auto-approved — there's
      // no one else to ask. Everyone else (including a new org's founder) needs
      // approval first — go straight to login
      toast('Account created — please log in', 'success');
      switchAuthTab('login');
      document.getElementById('login-user').value = callsign;
    } else {
      // Show a confirmation in place of the form
      document.getElementById('tab-register').innerHTML = `
        <div style="text-align:center;padding:12px 0">
          <div style="font-size:36px;margin-bottom:12px">✅</div>
          <h3 style="margin:0 0 10px;color:var(--lc-green)">Registration Submitted!</h3>
          <p style="font-size:13px;color:var(--text-muted);line-height:1.6;margin:0 0 16px">
            Your account request for <strong>${callsign}</strong> has been received.<br>
            An admin will review it and you will receive an email at<br>
            <strong>${email}</strong> once your account has been approved.<br>
            <span style="font-size:11px">Don't see it? Check your spam/junk folder.</span>
          </p>
          <button class="btn btn-ghost btn-sm" onclick="switchAuthTab('login')">← Back to Login</button>
        </div>`;
      if (newUser.email_verified === false) {
        toast('Check your inbox for a verification email — look in spam/junk if it doesn\'t show up in a few minutes.', 'success');
      }
    }
  } catch (e) {
    showAuthError(e.message);
    btnLoading(btn, false);
    // Turnstile tokens are single-use -- must reset before another attempt.
    if (turnstileRegWidgetId !== null) turnstile.reset(turnstileRegWidgetId);
  }
}

// Admin-created account invite link (issue #1 follow-up) -- redeems the
// token from window._setpwToken (set in app.js from ?setpw=) and logs
// straight in, same as a normal login.
async function doSetPassword(btn) {
  clearAuthError();
  const pass = document.getElementById('setpw-pass').value;
  const pass2 = document.getElementById('setpw-pass2').value;
  if (!pass || !pass2) return showAuthError('Fill in both password fields');
  if (pass !== pass2) return showAuthError('Passwords do not match');
  if (pass.length < 8) return showAuthError('Password must be at least 8 characters');
  btnLoading(btn, true);
  try {
    const res = await fetch(API + '/auth/set-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: window._setpwToken, password: pass }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not set password');
    token = data.access_token;
    currentUser = data.user;
    localStorage.setItem('nt_token', token);
    toast('Password set — welcome!', 'success');
    enterApp();
  } catch (e) {
    showAuthError(e.message);
    btnLoading(btn, false);
  }
}

function showAuthError(msg) {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  // #auth-error's base CSS rule is `display: none` -- clearing the inline
  // style (display = '') just falls back to that, so the element never
  // actually becomes visible. Needs an explicit override.
  el.style.display = 'block';
}
function clearAuthError() { document.getElementById('auth-error').style.display = 'none'; }

function logout() {
  token = null;
  currentUser = null;
  localStorage.removeItem('nt_token');
  location.reload();
}

async function enterApp() {
  if (!currentUser) {
    try { currentUser = await apiFetch('/auth/me'); } catch { logout(); return; }
  }
  syncThemeFromUser(currentUser);
  document.getElementById('auth-page').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  document.getElementById('header-callsign').textContent = currentUser.callsign;
  // Mobile: show just the callsign badge in the header
  const shortEl = document.getElementById('header-callsign-short');
  if (shortEl) shortEl.textContent = currentUser.callsign;
  await loadBranding();
  restoreSidebarCollapse();
  restoreNetControlMode();
  loadOrgSwitcher();   // fire-and-forget; non-blocking — also decides nav-admin visibility
  await loadNets();
  loadSidebarStats();   // fire-and-forget; non-blocking
  showView('nets');
}

// ============================================================
// ORGANIZATIONS (issue #1 — multi-tenancy)
// ============================================================

// Populates the header org switcher with the user's own approved orgs, and
// decides whether the Admin nav link is shown — a super admin always sees
// it; an org admin (not super) sees it only while working as an org they
// actually admin (admin.html itself re-derives this and enforces it server-side
// too, this is just so the link isn't shown where it would just bounce them).
async function loadOrgSwitcher() {
  const select = document.getElementById('org-switcher');
  const navAdmin = document.getElementById('nav-admin');
  let orgs = [];
  try { orgs = await apiFetch('/orgs/mine'); } catch { /* switcher/nav-admin just stay at their defaults */ }

  if (navAdmin) {
    const currentMembership = orgs.find(o => o.id === currentUser.current_org_id);
    navAdmin.style.display = (currentUser.is_admin || (currentMembership && currentMembership.role === 'admin')) ? '' : 'none';
  }

  if (orgs.length <= 1) {
    select.style.display = 'none';
    return;
  }
  select.innerHTML = orgs.map(o =>
    `<option value="${o.id}" ${o.id === currentUser.current_org_id ? 'selected' : ''}>${esc(o.name)}</option>`
  ).join('');
  select.style.display = '';
}

async function switchCurrentOrg(orgId) {
  try {
    currentUser = await apiFetch('/auth/current-org', { method: 'PATCH', body: JSON.stringify({ org_id: Number(orgId) }) });
    toast('Switched organization', 'success');
    loadOrgSwitcher();   // re-derive nav-admin visibility for the new org
    await loadNets();
    showView('nets');
  } catch (e) {
    toast(e.message, 'error');
    loadOrgSwitcher();   // revert the dropdown to the actual current org
  }
}

// Auto-login if token stored
