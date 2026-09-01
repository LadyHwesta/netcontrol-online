// ============================================================
// BRANDING
// ============================================================
let currentBranding = {};
// Each page's own <title> (e.g. "Admin — NetControl Online") — captured once
// before applyBranding() ever runs, so the no-custom-branding fallback below
// restores the page's actual title instead of stomping it with a single
// generic name every page would otherwise share.
const _defaultPageTitle = document.title;

async function loadBranding() {
  try { currentBranding = await apiFetch('/branding'); } catch { return; }
  applyBranding(currentBranding);
}

function applyBranding(b) {
  // Header title
  const titleEl = document.getElementById('header-title');
  if (b.org_name) {
    titleEl.innerHTML = `<span>${esc(b.org_name)}</span>`;
    document.title = b.org_name + ' — NetControl Online';
  } else {
    titleEl.innerHTML = '<span>NetControl Online</span>';
    document.title = _defaultPageTitle;
  }
  // Tagline
  const tagEl = document.getElementById('header-tagline');
  if (b.tagline) { tagEl.textContent = b.tagline; tagEl.style.display = ''; }
  else { tagEl.style.display = 'none'; }
  // Logo
  const logoEl = document.getElementById('header-logo');
  const antennaEl = document.getElementById('header-antenna');
  if (b.has_logo) {
    logoEl.src = '/logo?' + Date.now(); // cache-bust
    antennaEl.style.display = 'none';
  } else {
    logoEl.style.display = 'none';
    antennaEl.style.display = '';
  }
  // Website link on title
  if (b.website_url) {
    titleEl.style.cursor = 'pointer';
    titleEl.onclick = () => window.open(b.website_url, '_blank');
    titleEl.title = b.website_url;
  } else {
    titleEl.style.cursor = '';
    titleEl.onclick = null;
    titleEl.title = '';
  }
}

// Org-admin-set banner (issue follow-up — welcome messages), shown at the
// top of every authenticated page to that org's members. Distinct from
// instance-wide Branding above -- loaded via /orgs/mine (already scoped to
// the caller's own approved orgs) rather than /branding, and rendered into
// a shared #org-banner element every page includes right below its header.
//
// Also applies per-org branding (issue follow-up) while it's got the org
// object in hand -- one /orgs/mine round trip doing both jobs, called again
// by switchCurrentOrg() so the header updates immediately on an org switch,
// not just on next reload.
async function loadOrgBanner() {
  const el = document.getElementById('org-banner');
  if (!currentUser) return;
  try {
    const mine = await apiFetch('/orgs/mine');
    const org = mine.find(o => o.id === currentUser.current_org_id);
    if (el) {
      if (org && org.banner_message) {
        el.textContent = org.banner_message;
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    }
    applyOrgBranding(org);
  } catch { if (el) el.style.display = 'none'; }
}

// Per-org branding (issue follow-up) -- layers over whatever instance-wide
// Branding (applyBranding() above) already painted: the org's own name
// always wins once logged in (every org has one, unlike tagline/logo),
// tagline/logo only override when the org has actually set its own,
// otherwise the instance-wide default (or built-in fallback) already
// showing is left alone. Mirrored (not shared, since these are standalone
// pages with their own inline scripts) in public.html/directory.html for
// the equivalent per-org public pages.
function applyOrgBranding(org) {
  if (!org) return;
  const titleEl = document.getElementById('header-title');
  titleEl.innerHTML = `<span>${esc(org.name)}</span>`;
  document.title = org.name + ' — NetControl Online';
  if (org.website_url) {
    titleEl.style.cursor = 'pointer';
    titleEl.onclick = () => window.open(org.website_url, '_blank');
    titleEl.title = org.website_url;
  }
  if (org.tagline) {
    const tagEl = document.getElementById('header-tagline');
    tagEl.textContent = org.tagline;
    tagEl.style.display = '';
  }
  if (org.has_logo) {
    const logoEl = document.getElementById('header-logo');
    logoEl.src = `/orgs/${org.id}/logo?` + Date.now();
    logoEl.style.display = '';
    document.getElementById('header-antenna').style.display = 'none';
  }
}

async function loadAdminBranding() {
  try {
    const b = await apiFetch('/branding');
    document.getElementById('branding-org-name').value = b.org_name || '';
    document.getElementById('branding-tagline').value = b.tagline || '';
    document.getElementById('branding-website-url').value = b.website_url || '';
    const deleteBtn = document.getElementById('branding-logo-delete-btn');
    const preview = document.getElementById('branding-logo-preview');
    if (b.has_logo) {
      preview.src = '/logo?' + Date.now();
      deleteBtn.style.display = '';
    } else {
      preview.style.display = 'none';
      deleteBtn.style.display = 'none';
    }
  } catch {}
}

function previewLogo(input) {
  const file = input.files[0];
  if (!file) return;
  const preview = document.getElementById('branding-logo-preview');
  preview.src = URL.createObjectURL(file);
}

async function saveBranding() {
  const org_name = document.getElementById('branding-org-name').value.trim() || null;
  const tagline  = document.getElementById('branding-tagline').value.trim() || null;
  const website_url = document.getElementById('branding-website-url').value.trim() || null;
  try {
    // Save text settings
    await apiFetch('/admin/branding', { method: 'PUT', body: JSON.stringify({ org_name, tagline, website_url }) });
    // Upload logo if a file was selected
    const fileInput = document.getElementById('branding-logo-file');
    if (fileInput.files[0]) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      await fetch('/admin/branding/logo', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: fd,
      }).then(r => { if (!r.ok) throw new Error(t('Logo upload failed')); });
      fileInput.value = '';
    }
    toast(t('Branding saved'));
    await loadBranding();
    await loadAdminBranding();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteLogo() {
  if (!confirm(t('Remove the current logo?'))) return;
  try {
    await apiFetch('/admin/branding/logo', { method: 'DELETE' });
    toast(t('Logo removed'));
    await loadBranding();
    await loadAdminBranding();
  } catch (e) { toast(e.message, 'error'); }
}

onEnter(['branding-org-name', 'branding-website-url', 'branding-tagline'], saveBranding);

