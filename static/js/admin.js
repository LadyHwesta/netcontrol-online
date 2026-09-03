// ============================================================
// ADMIN
// ============================================================

// ============================================================
// YOUR ORGANIZATION (issue #1 follow-up)
// ============================================================
// Previously there was no way to rename an org (or fix its website) after
// creation at all -- changing the instance-wide Branding settings doesn't
// retroactively rename any org, since an org's name is its own property now.
async function loadOrgEditForm() {
  try {
    const orgs = await apiFetch('/orgs');
    const org = orgs.find(o => o.id === currentUser.current_org_id);
    if (!org) return;
    document.getElementById('org-edit-name').value = org.name;
    document.getElementById('org-edit-website').value = org.website_url || '';
    document.getElementById('org-edit-tagline').value = org.tagline || '';
    document.getElementById('org-edit-banner').value = org.banner_message || '';
    document.getElementById('org-edit-registration-open').checked = org.registration_open;
    document.getElementById('org-edit-name').dataset.orgId = org.id;
    const deleteBtn = document.getElementById('org-edit-logo-delete-btn');
    const preview = document.getElementById('org-edit-logo-preview');
    if (org.has_logo) {
      preview.src = `/orgs/${org.id}/logo?` + Date.now();
      deleteBtn.style.display = '';
    } else {
      preview.style.display = 'none';
      deleteBtn.style.display = 'none';
    }
    // Separate endpoint -- the aprs.fi key is a real secret, deliberately
    // not part of OrganizationOut/GET /orgs above (see routers/orgs.py).
    try {
      const aprsKey = await apiFetch(`/orgs/${org.id}/aprs-key`);
      document.getElementById('org-edit-aprs-key').value = aprsKey.aprs_fi_api_key || '';
    } catch { /* not an org admin for this org, or none set -- leave blank */ }
  } catch (e) { toast(e.message, 'error'); }
}

function previewOrgLogo(input) {
  const file = input.files[0];
  if (!file) return;
  const preview = document.getElementById('org-edit-logo-preview');
  preview.src = URL.createObjectURL(file);
}

async function deleteOrgLogo() {
  const orgId = document.getElementById('org-edit-name').dataset.orgId;
  if (!orgId || !confirm(t('Remove this organization\'s logo?'))) return;
  try {
    await apiFetch(`/orgs/${orgId}/logo`, { method: 'DELETE' });
    toast(t('Logo removed'));
    await loadOrgEditForm();
    loadOrgBanner();   // re-applies branding for the header too
  } catch (e) { toast(e.message, 'error'); }
}

async function saveOrgEdit() {
  const orgId = document.getElementById('org-edit-name').dataset.orgId;
  if (!orgId) return;
  const name = document.getElementById('org-edit-name').value.trim();
  if (!name) return toast(t('Organization name is required'), 'error');
  const websiteUrl = document.getElementById('org-edit-website').value.trim();
  const tagline = document.getElementById('org-edit-tagline').value.trim();
  const bannerMessage = document.getElementById('org-edit-banner').value.trim();
  const aprsKey = document.getElementById('org-edit-aprs-key').value.trim();
  const registrationOpen = document.getElementById('org-edit-registration-open').checked;
  try {
    await apiFetch(`/orgs/${orgId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name, website_url: websiteUrl || null, banner_message: bannerMessage || null, tagline: tagline || null, registration_open: registrationOpen }),
    });
    await apiFetch(`/orgs/${orgId}/aprs-key`, {
      method: 'PUT',
      body: JSON.stringify({ aprs_fi_api_key: aprsKey || null }),
    });
    // Upload logo if a file was selected -- same two-step shape as the
    // instance-wide saveBranding() in branding.js.
    const fileInput = document.getElementById('org-edit-logo-file');
    if (fileInput.files[0]) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      await fetch(`/orgs/${orgId}/logo`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: fd,
      }).then(r => { if (!r.ok) throw new Error(t('Logo upload failed')); });
      fileInput.value = '';
    }
    toast(t('Organization saved'), 'success');
    await loadOrgEditForm();
    loadOrgBanner();   // re-applies branding for the header too
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// FEDIVERSE / ACTIVITYPUB (issue follow-up)
// ============================================================
// Separate card/endpoints from the org-edit form above -- a status widget
// (live handle + follower count) rather than a plain saved field, same
// reasoning as why the aprs.fi key already gets its own dedicated
// GET/PUT pair instead of folding into PATCH /orgs/{id}.
async function loadOrgActivityPubStatus() {
  const orgId = currentUser.current_org_id;
  if (!orgId) return;
  document.getElementById('org-activitypub-enabled').dataset.orgId = orgId;
  try {
    const status = await apiFetch(`/orgs/${orgId}/activitypub`);
    document.getElementById('org-activitypub-enabled').checked = status.enabled;
    const statusEl = document.getElementById('org-activitypub-status');
    if (status.enabled) {
      document.getElementById('org-activitypub-handle').textContent = '@' + status.handle;
      document.getElementById('org-activitypub-followers').textContent = status.follower_count;
      statusEl.style.display = '';
    } else {
      statusEl.style.display = 'none';
    }
  } catch { /* not an org admin for this org -- leave the toggle at its default (off) */ }
}

async function toggleOrgActivityPub(enabled) {
  const orgId = document.getElementById('org-activitypub-enabled').dataset.orgId;
  if (!orgId) return;
  try {
    await apiFetch(`/orgs/${orgId}/activitypub`, { method: 'PUT', body: JSON.stringify({ enabled }) });
    toast(enabled ? t('Fediverse participation enabled') : t('Fediverse participation disabled'), 'success');
    await loadOrgActivityPubStatus();
  } catch (e) {
    document.getElementById('org-activitypub-enabled').checked = !enabled;
    toast(e.message, 'error');
  }
}

// ============================================================
// ADD OPERATOR (issue #1 follow-up) — admin-created accounts, auto-approved.
// An org admin always seeds into their own current org (no picker -- that's
// the only org they can act on anyway). A super admin gets an org picker
// here too (issue follow-up), defaulting to their own current org but
// changeable to any existing org, or "+ Create New Organization" to found
// one on the spot -- posts to /admin/users instead of /orgs/{id}/users in
// that case. See loadAddOperatorOrgPicker(), called only for super admins.
// ============================================================
async function loadAddOperatorOrgPicker() {
  const picker = document.getElementById('addop-org-picker');
  let orgs = [];
  try { orgs = await apiFetch('/orgs'); } catch (e) { toast(e.message, 'error'); }
  const orgOptions = orgs.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
  const select = document.getElementById('addop-org-select');
  select.innerHTML = orgOptions + `<option value="__new__">${t('+ Create New Organization')}</option>`;
  if (orgs.some(o => o.id === currentUser.current_org_id)) select.value = currentUser.current_org_id;
  picker.style.display = '';
  onAddOpOrgChange();
}

function onAddOpOrgChange() {
  const isNew = document.getElementById('addop-org-select').value === '__new__';
  document.getElementById('addop-neworg-fields').style.display = isNew ? '' : 'none';
  // A brand new org always needs an admin (server-side enforced too, same
  // rule self-registration already applies to a founder) -- the role
  // picker only matters when joining an org that already has one.
  const roleSelect = document.getElementById('addop-role');
  roleSelect.disabled = isNew;
  if (isNew) roleSelect.value = 'admin';
}

async function addOperator(btn) {
  const callsign = document.getElementById('addop-callsign').value.trim().toUpperCase();
  const name = document.getElementById('addop-name').value.trim();
  const email = document.getElementById('addop-email').value.trim();
  const gmrs_callsign = document.getElementById('addop-gmrs').value.trim().toUpperCase() || null;
  const role = document.getElementById('addop-role').value;
  if (!callsign || !name || !email) return toast(t('Fill in callsign, name, and email'), 'error');

  // Super admin with the org picker showing: POST /admin/users, targeting
  // whichever org is selected (or founding a new one). Everyone else
  // (org admins, and a super admin before the picker has loaded): unchanged
  // -- POST /orgs/{their current org}/users, exactly as before this feature.
  let url = `/orgs/${currentUser.current_org_id}/users`;
  let body = { callsign, name, email, gmrs_callsign, role };
  const orgPickerVisible = document.getElementById('addop-org-picker').style.display !== 'none';
  if (orgPickerVisible) {
    const orgSelectValue = document.getElementById('addop-org-select').value;
    if (!orgSelectValue) return toast(t('Choose an organization'), 'error');
    url = '/admin/users';
    if (orgSelectValue === '__new__') {
      const org_name = document.getElementById('addop-neworg-name').value.trim();
      const org_website_url = document.getElementById('addop-neworg-website').value.trim();
      if (!org_name || !org_website_url) return toast(t('New organization needs a name and a website URL'), 'error');
      body = { ...body, org_name, org_website_url };
    } else {
      body = { ...body, org_id: Number(orgSelectValue) };
    }
  }

  btnLoading(btn, true);
  try {
    const result = await apiFetch(url, { method: 'POST', body: JSON.stringify(body) });
    toast(`${callsign} ${t('added to')} ${result.org_name} — ${t("they'll receive an email to set their password")}`, 'success');
    document.getElementById('addop-callsign').value = '';
    document.getElementById('addop-name').value = '';
    document.getElementById('addop-email').value = '';
    document.getElementById('addop-gmrs').value = '';
    document.getElementById('addop-role').value = 'member';
    document.getElementById('addop-role').disabled = false;
    document.getElementById('addop-neworg-name').value = '';
    document.getElementById('addop-neworg-website').value = '';
    if (window.isOrgAdminOnly) loadOrgOperators(); else loadAdminUsers();
    if (orgPickerVisible) loadAddOperatorOrgPicker();  // refresh in case a new org was just created
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function loadAdminUsers() {
  // Load email status and users in parallel
  const [users, emailStatus] = await Promise.all([
    apiFetch('/admin/users').catch(e => { toast(e.message, 'error'); return null; }),
    apiFetch('/admin/email-status').catch(() => null),
  ]);
  if (!users) return;

  // Render email status card
  const emailEl = document.getElementById('admin-email-status');
  if (emailStatus) {
    if (emailStatus.configured) {
      emailEl.innerHTML = `<span style="color:var(--lc-green)">✓ ${t('SMTP configured')}</span>
        <span class="text-muted" style="margin-left:10px;font-size:12px">${t('From:')} ${esc(emailStatus.from_address || emailStatus.host)}</span>`;
      document.getElementById('admin-email-config-hint').style.display = 'none';
    } else {
      emailEl.innerHTML = `<span style="color:var(--lc-red)">✗ ${t('SMTP not configured')}</span>
        <span class="text-muted" style="margin-left:10px;font-size:12px">${t('Emails will not be sent.')}</span>`;
    }
  }

  const pending = users.filter(u => !u.is_active);
  const pendingEl = document.getElementById('admin-pending-list');
  if (pending.length === 0) {
    pendingEl.innerHTML = `<p class="text-muted" style="font-size:13px">${t('No pending registrations.')}</p>`;
  } else {
    pendingEl.innerHTML = pending.map(u => {
      const verifiedBadge = u.email_verified
        ? `<span class="badge badge-green" title="${t('Email address confirmed by the user')}">✓ ${t('Verified')}</span>`
        : `<span class="badge badge-gray" title="${t('User has not yet clicked the verification link in their email')}">${t('Unverified')}</span>`;
      // Organization (issue #1 follow-up) — shown so a super admin reviewing
      // a registration that's founding a brand new org can verify its website
      // before approving. safeHttpUrl() guards against a non-http(s) URL (the
      // backend already rejects those at creation time, but this is a second
      // line of defense against rendering something like a javascript: URI as
      // a clickable link in an admin-privileged page).
      const orgLine = u.org_name ? `
        <div style="width:100%;font-size:11px;color:var(--text-muted);margin-top:2px">
          ${t('Org:')} <strong>${esc(u.org_name)}</strong>${u.org_website_url && safeHttpUrl(u.org_website_url)
            ? ` — <a href="${esc(u.org_website_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--lc-blue)">${esc(u.org_website_url)}</a>`
            : (u.org_website_url ? ` — ${esc(u.org_website_url)}` : '')}
        </div>` : '';
      return `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <span class="callsign">${esc(u.callsign)}</span>
        <span>${esc(u.name)}</span>
        <span class="text-muted" style="font-size:12px">${esc(u.email)}</span>
        ${verifiedBadge}
        <span class="text-muted" style="font-size:11px">${t('Registered')} ${fmt(u.created_at)}</span>
        <div style="margin-left:auto;display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" onclick="adminApprove(${u.id}, this)">✓ ${t('Approve')}</button>
          <button class="btn btn-danger btn-sm" onclick="adminReject(${u.id}, '${esc(u.callsign)}')">✕ ${t('Reject')}</button>
        </div>
        ${orgLine}
      </div>
    `;
    }).join('');
  }

  const tbody = document.getElementById('admin-users-tbody');
  const empty = document.getElementById('admin-users-empty');
  if (users.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = users.map(u => {
    const isMe = u.id === currentUser.id;
    const roleBadge = u.is_admin
      ? `<span class="badge badge-blue">${t('Admin')}</span>`
      : `<span class="badge badge-gray">${t('Operator')}</span>`;
    const statusBadge = u.is_active
      ? `<span class="badge badge-green">${t('Active')}</span>`
      : `<span class="badge badge-gray">${t('Pending')}</span>`;

    // Notify toggle — only meaningful for admins
    const notifyCell = u.is_admin
      ? `<button class="btn btn-sm ${u.notify_new_registrations ? 'btn-primary' : 'btn-ghost'}"
           title="${u.notify_new_registrations ? t('Click to stop notifications') : t('Click to receive registration emails')}"
           onclick="adminToggleNotify(${u.id})" style="font-size:12px;padding:3px 8px">
           ${u.notify_new_registrations ? '📧 ' + t('On') : '✉ ' + t('Off')}
         </button>`
      : '<span class="text-muted" style="font-size:11px">—</span>';

    const actions = isMe
      ? `<span class="text-muted" style="font-size:11px">${t('you')}</span>`
      : `<div style="display:flex;gap:4px;flex-wrap:wrap">
          ${!u.is_active ? `<button class="btn btn-primary btn-sm" onclick="adminApprove(${u.id}, this)">${t('Approve')}</button>` : ''}
          ${u.is_active  ? `<button class="btn btn-ghost btn-sm" onclick="adminDeactivate(${u.id})">${t('Deactivate')}</button>` : ''}
          ${!u.is_admin  ? `<button class="btn btn-ghost btn-sm" onclick="adminMakeAdmin(${u.id})">${t('Make Admin')}</button>` : ''}
          <button class="btn btn-danger btn-sm" onclick="adminDelete(${u.id})">${t('Delete')}</button>
        </div>`;
    return `<tr>
      <td><span class="callsign">${esc(u.callsign)}</span></td>
      <td>${esc(u.name)}</td>
      <td class="text-muted" style="font-size:12px">${esc(u.email)}</td>
      <td>${roleBadge}</td>
      <td>${statusBadge}</td>
      <td style="text-align:center">${notifyCell}</td>
      <td class="text-muted" style="font-size:12px">${fmt(u.created_at)}</td>
      <td>${actions}</td>
    </tr>`;
  }).join('');
}

async function adminToggleNotify(userId) {
  try {
    await apiFetch(`/admin/users/${userId}/notify`, { method: 'PATCH' });
    loadAdminUsers();
  } catch (e) { toast(e.message, 'error'); }
}

async function adminApprove(userId, btn) {
  btnLoading(btn, true);
  try {
    await apiFetch(`/admin/users/${userId}/approve`, { method: 'PATCH' });
    toast(t('Operator approved'), 'success');
    loadAdminUsers();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function adminDeactivate(userId) {
  if (!confirm(t('Deactivate this account? They will no longer be able to log in.'))) return;
  try {
    await apiFetch(`/admin/users/${userId}/deactivate`, { method: 'PATCH' });
    toast(t('Account deactivated'));
    loadAdminUsers();
  } catch (e) { toast(e.message, 'error'); }
}

async function adminMakeAdmin(userId) {
  if (!confirm(t('Grant admin privileges to this operator?'))) return;
  try {
    await apiFetch(`/admin/users/${userId}/make-admin`, { method: 'PATCH' });
    toast(t('Admin access granted'), 'success');
    loadAdminUsers();
  } catch (e) { toast(e.message, 'error'); }
}

async function adminReject(userId, callsign) {
  // Show a small inline modal for optional rejection message
  const existing = document.getElementById('reject-modal');
  if (existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'reject-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;display:flex;align-items:center;justify-content:center;padding:20px';
  modal.innerHTML = `
    <div style="background:var(--surface);border:2px solid var(--lc-red);border-radius:10px;padding:24px;max-width:440px;width:100%">
      <h3 style="margin:0 0 8px;color:var(--lc-red)">${t('Reject Registration')}</h3>
      <p style="margin:0 0 14px;font-size:13px;color:var(--text-muted)">
        ${t('Rejecting')} <strong>${esc(callsign)}</strong> ${t('will send them a notification email and permanently remove their account.')}
      </p>
      <div class="form-group" style="margin-bottom:14px">
        <label style="font-size:12px">${t('Custom message')} <span style="color:var(--text-muted)">${t('(optional — included in the rejection email)')}</span></label>
        <textarea id="reject-message" class="form-control" rows="3"
          placeholder="e.g. This net is limited to licensed operators in the W7XYZ club area."></textarea>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn btn-ghost" onclick="document.getElementById('reject-modal').remove()">${t('Cancel')}</button>
        <button class="btn btn-danger" onclick="submitReject(${userId})">${t('Send Rejection & Delete')}</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.getElementById('reject-message').focus();
}

async function submitReject(userId) {
  const message = document.getElementById('reject-message')?.value.trim() || null;
  const btn = document.querySelector('#reject-modal .btn-danger');
  btnLoading(btn, true);
  try {
    await apiFetch(`/admin/users/${userId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });
    document.getElementById('reject-modal')?.remove();
    toast(t('Rejection sent and account removed'));
    loadAdminUsers();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function adminDelete(userId) {
  if (!confirm(t('Permanently delete this account? This cannot be undone.'))) return;
  try {
    await apiFetch(`/admin/users/${userId}`, { method: 'DELETE' });
    toast(t('Account deleted'));
    loadAdminUsers();
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// ORG-SCOPED OPERATORS VIEW (issue #1 — org admins, not just super admins)
// ============================================================
// Reuses the same Operators panel markup as loadAdminUsers() above, just
// fed from the org-scoped endpoints and with a much smaller action set —
// deactivate/make-admin/delete are global-account actions an org admin
// shouldn't have.
let orgMembersCache = [];   // last-loaded /orgs/{id}/members rows -- orgToggleExtraRole below reads current roles from here

async function loadOrgOperators() {
  const orgId = currentUser.current_org_id;
  const [pending, members] = await Promise.all([
    apiFetch(`/orgs/${orgId}/pending-members`).catch(e => { toast(e.message, 'error'); return []; }),
    apiFetch(`/orgs/${orgId}/members`).catch(e => { toast(e.message, 'error'); return []; }),
  ]);
  orgMembersCache = members;

  const pendingEl = document.getElementById('admin-pending-list');
  if (pending.length === 0) {
    pendingEl.innerHTML = `<p class="text-muted" style="font-size:13px">${t('No pending registrations.')}</p>`;
  } else {
    // Role revamp (issue follow-up): the two self-service roles are pre-checked
    // from the registrant's own requested_roles hint, editable before approving
    // -- admin/net_control_op are still granted separately (role toggle below,
    // once approved) since they're the single base role, not additive.
    pendingEl.innerHTML = pending.map(m => `
      <div style="padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="callsign">${esc(m.callsign)}</span>
          <span>${esc(m.name)}</span>
          <span class="text-muted" style="font-size:12px">${esc(m.email)}</span>
          <span class="text-muted" style="font-size:11px">${t('Requested')} ${fmt(m.requested_at)}</span>
          <div style="margin-left:auto;display:flex;gap:6px">
            <button class="btn btn-primary btn-sm" onclick="orgApproveMember(${orgId}, ${m.user_id}, this)">✓ ${t('Approve')}</button>
            <button class="btn btn-danger btn-sm" onclick="orgRejectMember(${orgId}, ${m.user_id}, '${esc(m.callsign)}')">✕ ${t('Reject')}</button>
          </div>
        </div>
        <div style="display:flex;gap:14px;margin-top:6px;font-size:11px;color:var(--text-muted)">
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-weight:normal">
            <input type="checkbox" id="pending-role-tactical_operator-${m.user_id}" style="width:auto" ${m.requested_roles.includes('tactical_operator') ? 'checked' : ''}>
            ${t('Tactical Operator')}
          </label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-weight:normal">
            <input type="checkbox" id="pending-role-broadcaster-${m.user_id}" style="width:auto" ${m.requested_roles.includes('broadcaster') ? 'checked' : ''}>
            ${t('Broadcaster')}
          </label>
        </div>
      </div>
    `).join('');
  }

  const tbody = document.getElementById('admin-users-tbody');
  const empty = document.getElementById('admin-users-empty');
  if (members.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = members.map(m => {
    const isMe = m.user_id === currentUser.id;
    // Every other org admin action here is org-scoped already (approve/
    // reject); this one crosses into "can this member manage the org too" --
    // let an org admin add/remove peers, but not touch their own role, so a
    // single self-demote can't leave the org with zero admins.
    const roleAction = isMe
      ? `<span class="text-muted" style="font-size:11px">${t('you')}</span>`
      : (m.role === 'admin'
          ? `<button class="btn btn-ghost btn-sm" onclick="orgSetMemberRole(${orgId}, ${m.user_id}, 'member', '${esc(m.callsign)}')">${t('Remove Admin')}</button>`
          : `<button class="btn btn-ghost btn-sm" onclick="orgSetMemberRole(${orgId}, ${m.user_id}, 'admin', '${esc(m.callsign)}')">${t('Make Admin')}</button>`);
    // Role revamp (issue follow-up): base role badge (rename "Member" ->
    // "Net Control Op" is display-only, see ORG_ROLE_DISPLAY) plus a clickable
    // badge per extra role that toggles it on/off for this member.
    const baseBadge = m.role === 'admin'
      ? `<span class="badge badge-blue">${t('Org Admin')}</span>`
      : `<span class="badge badge-gray">${t('Net Control Op')}</span>`;
    const extraBadges = ['tactical_operator', 'broadcaster'].map(r => {
      const held = m.roles.includes(r);
      const label = r === 'tactical_operator' ? t('Tactical Op') : t('Broadcaster');
      return `<span class="badge ${held ? 'badge-green' : 'badge-gray'}" style="cursor:pointer" title="${t('Click to toggle')}"
        onclick="orgToggleExtraRole(${orgId}, ${m.user_id}, '${r}', ${held}, '${esc(m.callsign)}')">${held ? '✓ ' : ''}${label}</span>`;
    }).join(' ');
    return `<tr>
    <td><span class="callsign">${esc(m.callsign)}</span></td>
    <td>${esc(m.name)}</td>
    <td class="text-muted" style="font-size:12px">${esc(m.email)}</td>
    <td style="display:flex;gap:4px;flex-wrap:wrap">${baseBadge} ${extraBadges}</td>
    <td><span class="badge badge-green">${t('Active')}</span></td>
    <td class="text-muted" style="font-size:11px;text-align:center">—</td>
    <td class="text-muted" style="font-size:12px">${fmt(m.requested_at)}</td>
    <td>${roleAction}</td>
  </tr>`;
  }).join('');
}

async function orgToggleExtraRole(orgId, userId, role, currentlyHeld, callsign) {
  // Full replace (issue follow-up) -- fetch the member's current extra roles
  // from the already-loaded table rather than a round trip, then flip just
  // this one and PUT the whole set back.
  const row = orgMembersCache.find(m => m.user_id === userId);
  const current = new Set(row ? row.roles.filter(r => r === 'tactical_operator' || r === 'broadcaster') : []);
  if (currentlyHeld) current.delete(role); else current.add(role);
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/extra-roles`, { method: 'PUT', body: JSON.stringify({ roles: [...current] }) });
    toast(`${callsign} ${currentlyHeld ? t('role removed') : t('role granted')}`, 'success');
    loadOrgOperators();
  } catch (e) { toast(e.message, 'error'); }
}

async function orgApproveMember(orgId, userId, btn) {
  btnLoading(btn, true);
  const roles = ['tactical_operator', 'broadcaster'].filter(r => {
    const el = document.getElementById(`pending-role-${r}-${userId}`);
    return el && el.checked;
  });
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/approve`, { method: 'PATCH', body: JSON.stringify({ roles }) });
    toast(t('Member approved'), 'success');
    loadOrgOperators();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function orgRejectMember(orgId, userId, callsign) {
  if (!confirm(`${t('Reject')} ${callsign} ${t("'s request to join?")}`)) return;
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/reject`, { method: 'POST' });
    toast(t('Request rejected'));
    loadOrgOperators();
  } catch (e) { toast(e.message, 'error'); }
}

async function orgSetMemberRole(orgId, userId, role, callsign) {
  const msg = role === 'admin' ? `${t('Grant org admin to')} ${callsign}?` : `${t('Remove org admin from')} ${callsign}?`;
  if (!confirm(msg)) return;
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/role`, { method: 'PATCH', body: JSON.stringify({ role }) });
    toast(role === 'admin' ? `${callsign} ${t('is now an org admin')}` : `${callsign} ${t('is now a Net Control Op')}`, 'success');
    loadOrgOperators();
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// NETS IN YOUR ORG (org-admin-only view) — reassign a net's owner within
// this org. Super admins have the equivalent, any-org version in the
// Reassign tab instead (issue follow-up).
// ============================================================
async function loadOrgNets() {
  const orgId = currentUser.current_org_id;
  const [nets, members] = await Promise.all([
    apiFetch(`/orgs/${orgId}/nets`).catch(e => { toast(e.message, 'error'); return []; }),
    apiFetch(`/orgs/${orgId}/members`).catch(() => []),
  ]);
  document.getElementById('org-net-select').innerHTML = nets.map(n =>
    `<option value="${n.id}">${esc(n.name)} (${t('owner')} ${esc(n.owner_callsign || '?')})</option>`
  ).join('');
  document.getElementById('org-net-owner-select').innerHTML = members.map(m =>
    `<option value="${m.user_id}">${esc(m.callsign)} — ${esc(m.name)}</option>`
  ).join('');
}

async function submitOrgNetOwner(btn) {
  const netSelect = document.getElementById('org-net-select');
  const ownerSelect = document.getElementById('org-net-owner-select');
  const netId = netSelect.value;
  const ownerId = ownerSelect.value;
  if (!netId || !ownerId) return toast(t('Select a net and a new owner'), 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || t('this net');
  const ownerLabel = ownerSelect.selectedOptions[0]?.textContent || t('the selected user');
  if (!confirm(`${t('Change the owner of')} ${netLabel} ${t('to')} ${ownerLabel}?`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/nets/${netId}/owner`, { method: 'PATCH', body: JSON.stringify({ owner_id: Number(ownerId) }) });
    toast(t('Net owner changed'), 'success');
    loadOrgNets();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

// ============================================================
// REASSIGN — move a user or net into a different org (super admin only).
// Lets a deployment that started single-tenant split into per-region orgs
// after the fact, without users re-registering or nets losing history
// (issue #1 follow-up).
// ============================================================
let _reassignNets = [];
let _reassignOrgsById = {};

async function loadReassignTab() {
  const previousFilter = document.getElementById('reassign-net-org-filter').value;
  const previousOwnerFilter = document.getElementById('reassign-owner-net-org-filter').value;
  const [users, nets, orgs] = await Promise.all([
    apiFetch('/admin/users').catch(e => { toast(e.message, 'error'); return []; }),
    apiFetch('/nets').catch(e => { toast(e.message, 'error'); return []; }),
    apiFetch('/orgs').catch(e => { toast(e.message, 'error'); return []; }),
  ]);
  _reassignNets = nets;
  _reassignOrgsById = Object.fromEntries(orgs.map(o => [o.id, o.name]));

  const orgOptions = orgs.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
  document.getElementById('reassign-user-org-select').innerHTML = orgOptions;
  document.getElementById('reassign-net-org-select').innerHTML = orgOptions;
  document.getElementById('addmembership-org-select').innerHTML = orgOptions;
  document.getElementById('reassign-net-org-filter').innerHTML =
    `<option value="">${t('All Organizations')}</option>` + orgOptions;
  document.getElementById('reassign-net-org-filter').value = previousFilter;
  document.getElementById('reassign-owner-net-org-filter').innerHTML =
    `<option value="">${t('All Organizations')}</option>` + orgOptions;
  document.getElementById('reassign-owner-net-org-filter').value = previousOwnerFilter;

  const userOptions = users.map(u =>
    `<option value="${u.id}">${esc(u.callsign)} — ${esc(u.name)} (${t('currently:')} ${esc(u.org_name || t('no org'))})</option>`
  ).join('');
  document.getElementById('reassign-user-select').innerHTML = userOptions;
  document.getElementById('addmembership-user-select').innerHTML = userOptions;

  filterReassignNets();
  filterReassignOwnerNets();
}

// Nets aren't refetched here -- just re-rendered from the already-loaded
// _reassignNets against whichever org the filter dropdown is set to (super
// admins can see nets across every org, which gets long fast).
function filterReassignNets() {
  const orgFilter = document.getElementById('reassign-net-org-filter').value;
  const filtered = orgFilter
    ? _reassignNets.filter(n => String(n.org_id) === orgFilter)
    : _reassignNets;
  document.getElementById('reassign-net-select').innerHTML = filtered.map(n =>
    `<option value="${n.id}">${esc(n.name)} — ${esc(_reassignOrgsById[n.org_id] || t('unknown org'))} (${t('owner')} ${esc(n.owner_callsign || '?')})</option>`
  ).join('');
}

function filterReassignOwnerNets() {
  const orgFilter = document.getElementById('reassign-owner-net-org-filter').value;
  const filtered = orgFilter
    ? _reassignNets.filter(n => String(n.org_id) === orgFilter)
    : _reassignNets;
  document.getElementById('reassign-owner-net-select').innerHTML = filtered.map(n =>
    `<option value="${n.id}">${esc(n.name)} — ${esc(_reassignOrgsById[n.org_id] || t('unknown org'))} (${t('owner')} ${esc(n.owner_callsign || '?')})</option>`
  ).join('');
  onReassignOwnerNetChange();
}

// The new-owner picker must be scoped to the SELECTED net's own org (the
// backend requires the new owner to already be an approved member of it),
// so it's re-fetched every time the net selection changes.
async function onReassignOwnerNetChange() {
  const netId = document.getElementById('reassign-owner-net-select').value;
  const select = document.getElementById('reassign-owner-user-select');
  const net = _reassignNets.find(n => n.id === Number(netId));
  if (!net) { select.innerHTML = ''; return; }
  try {
    const members = await apiFetch(`/orgs/${net.org_id}/members`);
    select.innerHTML = members.map(m => `<option value="${m.user_id}">${esc(m.callsign)} — ${esc(m.name)}</option>`).join('');
  } catch (e) {
    select.innerHTML = '';
    toast(e.message, 'error');
  }
}

async function submitReassignUser(btn) {
  const userSelect = document.getElementById('reassign-user-select');
  const orgSelect = document.getElementById('reassign-user-org-select');
  const role = document.getElementById('reassign-user-role-select').value;
  const userId = userSelect.value;
  const orgId = orgSelect.value;
  if (!userId || !orgId) return toast(t('Select a user and target organization'), 'error');
  const userLabel = userSelect.selectedOptions[0]?.textContent || t('this user');
  const orgLabel = orgSelect.selectedOptions[0]?.textContent || t('the selected organization');
  if (!confirm(`${t('Move')} ${userLabel} ${t('to')} ${orgLabel}? ${t('They will be removed from every other organization they belong to.')}`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/admin/users/${userId}/org`, {
      method: 'PATCH',
      body: JSON.stringify({ org_id: Number(orgId), role }),
    });
    toast(t('User moved'), 'success');
    loadReassignTab();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

// Additive, not destructive (unlike Move User above) -- no confirm() needed,
// same as the other non-destructive admin actions on this page.
async function submitAddMembership(btn) {
  const userSelect = document.getElementById('addmembership-user-select');
  const orgSelect = document.getElementById('addmembership-org-select');
  const role = document.getElementById('addmembership-role-select').value;
  const userId = userSelect.value;
  const orgId = orgSelect.value;
  if (!userId || !orgId) return toast(t('Select a user and an organization'), 'error');
  const userLabel = userSelect.selectedOptions[0]?.textContent.split(' — ')[0] || t('User');
  btnLoading(btn, true);
  try {
    const result = await apiFetch(`/admin/users/${userId}/orgs`, {
      method: 'POST',
      body: JSON.stringify({ org_id: Number(orgId), role }),
    });
    toast(`${userLabel} ${t('added to')} ${result.org_name}`, 'success');
    loadReassignTab();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function submitReassignNet(btn) {
  const netSelect = document.getElementById('reassign-net-select');
  const orgSelect = document.getElementById('reassign-net-org-select');
  const netId = netSelect.value;
  const orgId = orgSelect.value;
  if (!netId || !orgId) return toast(t('Select a net and target organization'), 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || t('this net');
  const orgLabel = orgSelect.selectedOptions[0]?.textContent || t('the selected organization');
  if (!confirm(`${t('Move')} ${netLabel} ${t('to')} ${orgLabel}?`)) return;
  btnLoading(btn, true);
  try {
    const result = await apiFetch(`/admin/nets/${netId}/org`, {
      method: 'PATCH',
      body: JSON.stringify({ org_id: Number(orgId) }),
    });
    toast(result.owner_not_member
      ? `${t('Net moved — its owner isn\'t a member of')} ${result.org_name}, ${t("so they won't be able to manage it themselves until added")}`
      : t('Net moved'), 'success');
    loadReassignTab();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

async function submitReassignNetOwner(btn) {
  const netSelect = document.getElementById('reassign-owner-net-select');
  const ownerSelect = document.getElementById('reassign-owner-user-select');
  const netId = netSelect.value;
  const ownerId = ownerSelect.value;
  if (!netId || !ownerId) return toast(t('Select a net and a new owner'), 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || t('this net');
  const ownerLabel = ownerSelect.selectedOptions[0]?.textContent || t('the selected user');
  if (!confirm(`${t('Change the owner of')} ${netLabel} ${t('to')} ${ownerLabel}?`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/nets/${netId}/owner`, { method: 'PATCH', body: JSON.stringify({ owner_id: Number(ownerId) }) });
    toast(t('Net owner changed'), 'success');
    loadReassignTab();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

// ============================================================
// SUB-TABS (Sessions / Schedule)
// ============================================================
function switchSubTab(tab) {
  document.getElementById('sessions-panel').style.display      = tab === 'sessions' ? '' : 'none';
  document.getElementById('live-session-panel').style.display  = (tab === 'sessions' && currentSessionId) ? '' : 'none';
  document.getElementById('schedule-panel').style.display      = tab === 'schedule' ? '' : 'none';
  document.getElementById('sub-tab-sessions').classList.toggle('active', tab === 'sessions');
  document.getElementById('sub-tab-schedule').classList.toggle('active', tab === 'schedule');
  if (tab === 'schedule') loadScheduleView();
}

// ============================================================
// USER LIST (for assignment dropdown)
// ============================================================
let registeredUsers = [];

async function loadRegisteredUsers() {
  // Only ever called from the schedule view (schedules.js), always with a net
  // open -- scope to that net's own org, not the viewer's current_org_id
  // (issue #1 follow-up: matters once a net has been moved to a different
  // org than the one the caller happens to be working as right now).
  const q = currentNetId ? `?net_id=${currentNetId}` : '';
  try { registeredUsers = await apiFetch('/users' + q); }
  catch { registeredUsers = []; }
}

// ============================================================
// NET REPOSITORY
// ============================================================
let _lastNetRepoKey = null;

async function loadNetRepoStatus() {
  const statusEl = document.getElementById('netrepo-status');
  const noUrlHint = document.getElementById('netrepo-no-url-hint');
  const requestForm = document.getElementById('netrepo-request-form');
  const pendingActions = document.getElementById('netrepo-pending-actions');
  const clearActions = document.getElementById('netrepo-clear-actions');
  // The raw key is only ever known right after check-status claims it fresh
  // (see checkNetRepoKeyStatus) -- any other status refresh hides it again.
  document.getElementById('netrepo-key-reveal').style.display = 'none';

  let data;
  try {
    data = await apiFetch('/admin/net-repository/status');
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--lc-red)">✗ ${esc(e.message)}</span>`;
    return;
  }

  noUrlHint.style.display = data.url_configured ? 'none' : '';
  requestForm.style.display = 'none';
  pendingActions.style.display = 'none';
  clearActions.style.display = 'none';

  if (!data.url_configured) {
    statusEl.innerHTML = `<span style="color:var(--lc-red)">✗ ${t('Not configured')}</span>`;
    return;
  }

  if (data.has_key) {
    const sourceLabel = data.key_source === 'env' ? t('via .env') : t('self-service');
    statusEl.innerHTML = `<span style="color:var(--lc-green)">✓ ${t('Configured')}</span>
      <span class="text-muted" style="margin-left:10px;font-size:12px">${esc(sourceLabel)}</span>`;
    if (data.key_source === 'self-service') clearActions.style.display = '';
    return;
  }

  if (data.request_status === 'pending') {
    statusEl.innerHTML = `<span style="color:var(--lc-gold)">⏳ ${t('Key request pending admin review')}</span>`;
    pendingActions.style.display = '';
    return;
  }

  if (data.request_status === 'rejected') {
    statusEl.innerHTML = `<span style="color:var(--lc-red)">✗ ${t('Key request was rejected')}</span>`;
    requestForm.style.display = '';
    return;
  }

  statusEl.innerHTML = `<span class="text-muted">${t('No API key configured yet')}</span>`;
  requestForm.style.display = '';
}

async function requestNetRepoKey() {
  const name = document.getElementById('netrepo-req-name').value.trim();
  if (!name) return toast(t('Name is required'), 'error');
  const body = {
    name,
    contact_callsign: document.getElementById('netrepo-req-callsign').value.trim() || null,
    instance_url: document.getElementById('netrepo-req-url').value.trim() || null,
    request_notes: document.getElementById('netrepo-req-notes').value.trim() || null,
  };
  try {
    const result = await apiFetch('/admin/net-repository/request-key', { method: 'POST', body: JSON.stringify(body) });
    toast(result.message, result.ok ? 'success' : 'error');
    if (result.ok) loadNetRepoStatus();
  } catch (e) { toast(e.message, 'error'); }
}

async function checkNetRepoKeyStatus() {
  try {
    const result = await apiFetch('/admin/net-repository/check-status', { method: 'POST' });
    const toastType = (result.status === 'rejected' || result.status === 'unknown') ? 'error' : 'success';
    toast(result.message || result.status, toastType);
    await loadNetRepoStatus();
    // Present only on the one poll that freshly claims an approved key --
    // show it now, since this is the only chance to copy it.
    if (result.api_key) {
      _lastNetRepoKey = result.api_key;
      document.getElementById('netrepo-key-value').textContent = result.api_key;
      document.getElementById('netrepo-key-reveal').style.display = '';
    }
  } catch (e) { toast(e.message, 'error'); }
}

function copyNetRepoKey() {
  if (!_lastNetRepoKey) return;
  navigator.clipboard.writeText(_lastNetRepoKey).then(() => toast(t('Key copied to clipboard')));
}

async function clearNetRepoKey() {
  if (!confirm(t('Forget the stored Net Repository key? Nets will stop pushing until a new key is configured.'))) return;
  try {
    await apiFetch('/admin/net-repository/key', { method: 'DELETE' });
    _lastNetRepoKey = null;
    toast(t('Key forgotten'));
    loadNetRepoStatus();
  } catch (e) { toast(e.message, 'error'); }
}

onEnter(['netrepo-req-name', 'netrepo-req-callsign', 'netrepo-req-url', 'netrepo-req-notes'], requestNetRepoKey);

async function loadDbStats() {
  const sqliteNote = document.getElementById('dbstats-sqlite-note');
  const summary = document.getElementById('dbstats-summary');
  const tablesCard = document.getElementById('dbstats-tables-card');
  const slowCard = document.getElementById('dbstats-slow-card');
  const slowNote = document.getElementById('dbstats-slow-note');
  const slowTable = document.getElementById('dbstats-slow-table');

  let data;
  try {
    data = await apiFetch('/admin/db-stats');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }

  if (data.dialect !== 'postgresql') {
    sqliteNote.style.display = '';
    summary.style.display = 'none';
    tablesCard.style.display = 'none';
    slowCard.style.display = 'none';
    return;
  }
  sqliteNote.style.display = 'none';

  summary.style.display = 'flex';
  document.getElementById('dbstats-size').textContent = data.database_size || '—';
  const c = data.connections;
  document.getElementById('dbstats-connections').textContent = c
    ? `${c.total} (${c.active} ${t('active')}, ${c.idle} ${t('idle')})`
    : '—';

  tablesCard.style.display = '';
  const tbody = document.querySelector('#dbstats-tables-table tbody');
  tbody.innerHTML = data.tables.map(row => `
    <tr><td>${esc(row.name)}</td><td>${esc(row.size)}</td><td>${row.row_estimate.toLocaleString()}</td></tr>
  `).join('') || `<tr><td colspan="3" class="text-muted">${t('No tables found')}</td></tr>`;

  slowCard.style.display = '';
  if (data.slow_queries_note) {
    slowNote.textContent = data.slow_queries_note;
    slowNote.style.display = '';
  } else {
    slowNote.style.display = 'none';
  }
  if (data.slow_queries.length) {
    slowTable.style.display = '';
    const slowBody = document.querySelector('#dbstats-slow-table tbody');
    slowBody.innerHTML = data.slow_queries.map(q => `
      <tr>
        <td style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace;font-size:11px" title="${esc(q.query)}">${esc(q.query)}</td>
        <td>${q.calls.toLocaleString()}</td>
        <td>${q.mean_time_ms.toLocaleString()}</td>
        <td>${q.total_time_ms.toLocaleString()}</td>
      </tr>
    `).join('');
  } else {
    slowTable.style.display = 'none';
  }
}

// ============================================================
// LANGUAGES (UI translation via argos-translate, opt-in TRANSLATION_ENABLED)
// Org-scoped (multi-tenancy follow-up): every org admin manages their own
// org's enabled-languages list independently via /orgs/{org_id}/languages --
// a super admin viewing this tab acts on their own current_org_id too, same
// as the Organization edit form above, not some separate instance-wide list.
// ============================================================
const LANG_STATUS_BADGE = () => ({
  pending: `<span class="badge badge-gray">${t('Pending')}</span>`,
  installing: `<span class="badge badge-blue">${t('Installing…')}</span>`,
  ready: `<span class="badge badge-green">${t('Ready')}</span>`,
  error: `<span class="badge badge-red">${t('Error')}</span>`,
});

let _languagesPollTimer = null;

async function loadLanguages() {
  const table = document.getElementById('languages-table');
  const empty = document.getElementById('languages-empty');
  const addCard = document.getElementById('languages-add-card');
  const disabledNote = document.getElementById('languages-disabled-note');
  clearTimeout(_languagesPollTimer);

  let rows;
  try {
    rows = await apiFetch(`/orgs/${currentUser.current_org_id}/languages`);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }

  // This endpoint itself doesn't say whether TRANSLATION_ENABLED is set --
  // an empty list either means "not configured yet" or "configured, nothing
  // enabled yet for this org". POST 503s with a clear message either way, so
  // the add form stays available and simply surfaces that on first use
  // rather than a separate status check here.
  disabledNote.style.display = 'none';
  addCard.style.display = '';

  if (!rows.length) {
    table.style.display = 'none';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  table.style.display = '';

  document.getElementById('languages-tbody').innerHTML = rows.map(l => `
    <tr>
      <td>${esc(l.display_name)}</td>
      <td class="mono">${esc(l.code)}</td>
      <td>${LANG_STATUS_BADGE()[l.model_status] || esc(l.model_status)}${l.error_message ? ` <span class="text-muted" style="font-size:11px" title="${esc(l.error_message)}">ⓘ</span>` : ''}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="disableLanguage('${esc(l.code)}')">${t('Disable')}</button></td>
    </tr>
  `).join('');

  // Model install + bulk pre-translation run in the background server-side --
  // poll while anything is still pending/installing so the admin sees it
  // flip to Ready without a manual refresh.
  if (rows.some(l => l.model_status === 'pending' || l.model_status === 'installing')) {
    _languagesPollTimer = setTimeout(loadLanguages, 5000);
  }
}

async function enableLanguage() {
  const code = document.getElementById('lang-add-code').value.trim().toLowerCase();
  const name = document.getElementById('lang-add-name').value.trim();
  if (!code || !name) return toast(t('Enter both a language code and a display name'), 'error');
  try {
    await apiFetch(`/orgs/${currentUser.current_org_id}/languages`, { method: 'POST', body: JSON.stringify({ code, display_name: name }) });
    document.getElementById('lang-add-code').value = '';
    document.getElementById('lang-add-name').value = '';
    toast(`${t('Enabling')} ${name} — ${t('installing its model and pre-translating in the background')}`, 'success');
    loadLanguages();
  } catch (e) { toast(e.message, 'error'); }
}

async function disableLanguage(code) {
  if (!confirm(`${t('Disable')} ${code}? ${t("It will stop appearing in your organization's language switcher. Already-translated text stays cached and re-enabling later is instant.")}`)) return;
  try {
    await apiFetch(`/orgs/${currentUser.current_org_id}/languages/${encodeURIComponent(code)}`, { method: 'DELETE' });
    loadLanguages();
  } catch (e) { toast(e.message, 'error'); }
}

async function loadAnnouncements() {
  try {
    const data = await apiFetch('/system/announcements');
    document.getElementById('announce-login').value = data.login_message || '';
    document.getElementById('announce-popup').value = data.welcome_popup_message || '';
  } catch (e) { toast(e.message, 'error'); }
}

async function saveAnnouncements() {
  const loginMessage = document.getElementById('announce-login').value.trim();
  const welcomePopupMessage = document.getElementById('announce-popup').value.trim();
  try {
    await apiFetch('/admin/announcements', {
      method: 'PUT',
      body: JSON.stringify({ login_message: loginMessage || null, welcome_popup_message: welcomePopupMessage || null }),
    });
    toast(t('Announcements saved'), 'success');
  } catch (e) { toast(e.message, 'error'); }
}

