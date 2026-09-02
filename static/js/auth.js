// ============================================================
// AUTH
// ============================================================

// Super-admin-set login screen message (issue follow-up — welcome messages).
// Public endpoint -- called while still on the auth page, before signing in.
async function loadLoginMessage() {
  const el = document.getElementById('login-message');
  try {
    const data = await apiFetch('/system/announcements');
    if (data.login_message) {
      el.textContent = data.login_message;
      el.style.display = '';
    }
  } catch { /* leave hidden — a login-screen message failing to load shouldn't block login itself */ }
}

// Post-login welcome popup (same source as the login message above, but the
// welcome_popup_message field). Shown once per distinct message -- tracked
// client-side by comparing the fetched text against the last one dismissed,
// so editing the message in Admin makes it pop up again for everyone, but
// re-logging in with an unchanged message doesn't nag returning users.
async function checkWelcomePopup() {
  try {
    const data = await apiFetch('/system/announcements');
    const msg = data.welcome_popup_message;
    if (msg && msg !== localStorage.getItem('nt_seen_welcome_popup')) {
      document.getElementById('welcome-popup-text').textContent = msg;
      document.getElementById('welcome-popup').style.display = 'flex';
      window._pendingWelcomePopupMsg = msg;   // stashed until dismissed, not written to localStorage yet
    }
  } catch { /* no popup this session — not worth surfacing an error for */ }
}

function closeWelcomePopup() {
  if (window._pendingWelcomePopupMsg) {
    localStorage.setItem('nt_seen_welcome_popup', window._pendingWelcomePopupMsg);
  }
  document.getElementById('welcome-popup').style.display = 'none';
}

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
    // ?registration_open=true (issue follow-up) -- excludes any org an
    // admin has marked invite-only; self-registering into one is blocked
    // server-side too, so showing it here would just be a dead end.
    const orgs = await apiFetch('/orgs?registration_open=true');
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
// BOT PROTECTION (Cloudflare Turnstile / Google reCAPTCHA / ALTCHA)
// ============================================================
// Opt-in server-side (see /auth/config) — the widget containers stay hidden
// and no extra script is ever loaded unless an admin has actually
// configured CAPTCHA_PROVIDER (and, for Turnstile/reCAPTCHA, that
// provider's site/secret keys). Exactly one provider is active at a time.
let captchaProvider = null;
let turnstileLoginWidgetId = null;
let turnstileRegWidgetId = null;
let recaptchaLoginWidgetId = null;
let recaptchaRegWidgetId = null;
let altchaLoginToken = null;
let altchaRegToken = null;

async function initCaptcha() {
  let config;
  try { config = await apiFetch('/auth/config'); } catch { return; }
  captchaProvider = config.captcha_provider;
  if (!captchaProvider) return;

  if (captchaProvider === 'turnstile') {
    window._onTurnstileLoad = () => {
      turnstileLoginWidgetId = turnstile.render('#login-captcha', { sitekey: config.captcha_site_key });
      turnstileRegWidgetId = turnstile.render('#reg-captcha', { sitekey: config.captcha_site_key });
      document.getElementById('login-captcha').style.display = '';
      document.getElementById('reg-captcha').style.display = '';
    };
    const script = document.createElement('script');
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_onTurnstileLoad&render=explicit';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);
  } else if (captchaProvider === 'recaptcha') {
    window._onRecaptchaLoad = () => {
      recaptchaLoginWidgetId = grecaptcha.render('login-captcha', { sitekey: config.captcha_site_key });
      recaptchaRegWidgetId = grecaptcha.render('reg-captcha', { sitekey: config.captcha_site_key });
      document.getElementById('login-captcha').style.display = '';
      document.getElementById('reg-captcha').style.display = '';
    };
    const script = document.createElement('script');
    script.src = 'https://www.google.com/recaptcha/api.js?onload=_onRecaptchaLoad&render=explicit';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);
  } else if (captchaProvider === 'altcha') {
    // Self-hosted widget (static/vendor/altcha) -- no third-party script or
    // verification service at all; it solves a challenge fetched from our
    // own /captcha/altcha-challenge.
    const script = document.createElement('script');
    script.type = 'module';
    script.src = '/static/vendor/altcha/altcha.min.js';
    document.head.appendChild(script);

    ['login', 'reg'].forEach(which => {
      const container = document.getElementById(`${which}-captcha`);
      container.innerHTML = '<altcha-widget challengeurl="/captcha/altcha-challenge" hidefooter></altcha-widget>';
      container.style.display = '';
      container.querySelector('altcha-widget').addEventListener('statechange', e => {
        const solvedToken = e.detail.state === 'verified' ? e.detail.payload : null;
        if (which === 'login') altchaLoginToken = solvedToken;
        else altchaRegToken = solvedToken;
      });
    });
  }
}

// Returns the current solved token for whichever provider is active
// ('login' | 'reg'), or '' if not yet solved / not configured.
function getCaptchaToken(which) {
  if (captchaProvider === 'turnstile') {
    const id = which === 'login' ? turnstileLoginWidgetId : turnstileRegWidgetId;
    return id !== null ? (turnstile.getResponse(id) || '') : '';
  }
  if (captchaProvider === 'recaptcha') {
    const id = which === 'login' ? recaptchaLoginWidgetId : recaptchaRegWidgetId;
    return id !== null ? (grecaptcha.getResponse(id) || '') : '';
  }
  if (captchaProvider === 'altcha') {
    return (which === 'login' ? altchaLoginToken : altchaRegToken) || '';
  }
  return '';
}

// Every provider's token is single-use -- must reset before another attempt.
function resetCaptcha(which) {
  if (captchaProvider === 'turnstile') {
    const id = which === 'login' ? turnstileLoginWidgetId : turnstileRegWidgetId;
    if (id !== null) turnstile.reset(id);
  } else if (captchaProvider === 'recaptcha') {
    const id = which === 'login' ? recaptchaLoginWidgetId : recaptchaRegWidgetId;
    if (id !== null) grecaptcha.reset(id);
  } else if (captchaProvider === 'altcha') {
    if (which === 'login') altchaLoginToken = null; else altchaRegToken = null;
    document.querySelector(`#${which}-captcha altcha-widget`)?.reset?.();
  }
}

async function doLogin() {
  clearAuthError();
  const user = document.getElementById('login-user').value.trim();
  const pass = document.getElementById('login-pass').value;
  if (!user || !pass) return showAuthError('Fill in all fields');
  try {
    const params = { username: user, password: pass };
    if (captchaProvider) params.captcha_token = getCaptchaToken('login');
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
    resetCaptcha('login');
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
  if (captchaProvider) body.captcha_token = getCaptchaToken('reg');
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
    resetCaptcha('reg');
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
  localStorage.removeItem('nt_last_user');
  location.reload();
}

async function enterApp() {
  if (!currentUser) {
    try {
      currentUser = await apiFetch('/auth/me');
      // Cached purely so a later offline reload (below) has something to
      // fall back to -- never read except in that one failure path.
      localStorage.setItem('nt_last_user', JSON.stringify(currentUser));
    } catch (e) {
      // A plain fetch() failure (no network reachable at all) throws
      // TypeError, before apiFetch ever sees a response to reject on --
      // distinct from apiFetch's own `throw new Error(...)` for a real
      // rejection from the server (401 = expired/invalid token). Only the
      // latter should log the user out. Reloading the app, or relaunching
      // it as an installed PWA, with no connection used to clear a
      // perfectly valid token here just because it couldn't be re-verified
      // -- bouncing to a login screen that also can't work offline, and
      // defeating the whole point of the offline check-in queue below.
      if (e instanceof TypeError) {
        const cached = localStorage.getItem('nt_last_user');
        if (!cached) { logout(); return; }  // never verified in this browser -- nothing to fall back to
        currentUser = JSON.parse(cached);
      } else {
        logout();
        return;
      }
    }
  }
  syncThemeFromUser(currentUser);
  document.getElementById('auth-page').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  document.getElementById('sidebar-callsign').textContent = currentUser.callsign;
  updateOfflineBanner();
  await loadBranding();
  loadOrgBanner();       // fire-and-forget; non-blocking
  syncLangFromUser(currentUser);   // fire-and-forget; non-blocking
  checkWelcomePopup();   // fire-and-forget; non-blocking
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
    // #org-switcher now lives on all 5 pages (issue follow-up -- it used to
    // be on the Nets page only), but loadNets()/showView() (nets.js/
    // views.js) are index.html-only -- calling them unconditionally would
    // throw on the other four. Refreshing in place only makes sense where
    // there's a net list to refresh anyway; everywhere else, a reload is
    // both simplest and correct, since that page's own init already
    // re-derives branding/nav-admin/its own content for the new org from
    // scratch on load.
    if (typeof loadNets === 'function' && typeof showView === 'function') {
      loadOrgSwitcher();   // re-derive nav-admin visibility for the new org
      loadOrgBanner();     // re-applies branding + banner message for the new org
      await loadNets();
      showView('nets');
    } else {
      location.reload();
    }
  } catch (e) {
    toast(e.message, 'error');
    loadOrgSwitcher();   // revert the dropdown to the actual current org
  }
}

// Auto-login if token stored
