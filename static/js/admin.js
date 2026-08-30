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
    document.getElementById('org-edit-name').dataset.orgId = org.id;
  } catch (e) { toast(e.message, 'error'); }
}

async function saveOrgEdit() {
  const orgId = document.getElementById('org-edit-name').dataset.orgId;
  if (!orgId) return;
  const name = document.getElementById('org-edit-name').value.trim();
  if (!name) return toast('Organization name is required', 'error');
  const websiteUrl = document.getElementById('org-edit-website').value.trim();
  try {
    await apiFetch(`/orgs/${orgId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name, website_url: websiteUrl || null }),
    });
    toast('Organization saved', 'success');
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// ADD OPERATOR (issue #1 follow-up) — admin-created accounts, seeded
// directly into the admin's own current org, auto-approved. Shown to org
// admins and super admins alike (both act on their own current org here).
// ============================================================
async function addOperator(btn) {
  const callsign = document.getElementById('addop-callsign').value.trim().toUpperCase();
  const name = document.getElementById('addop-name').value.trim();
  const email = document.getElementById('addop-email').value.trim();
  const gmrs_callsign = document.getElementById('addop-gmrs').value.trim().toUpperCase() || null;
  const role = document.getElementById('addop-role').value;
  if (!callsign || !name || !email) return toast('Fill in callsign, name, and email', 'error');
  btnLoading(btn, true);
  try {
    await apiFetch(`/orgs/${currentUser.current_org_id}/users`, {
      method: 'POST',
      body: JSON.stringify({ callsign, name, email, gmrs_callsign, role }),
    });
    toast(`${callsign} added — they'll receive an email to set their password`, 'success');
    document.getElementById('addop-callsign').value = '';
    document.getElementById('addop-name').value = '';
    document.getElementById('addop-email').value = '';
    document.getElementById('addop-gmrs').value = '';
    document.getElementById('addop-role').value = 'member';
    if (window.isOrgAdminOnly) loadOrgOperators(); else loadAdminUsers();
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
      emailEl.innerHTML = `<span style="color:var(--lc-green)">✓ SMTP configured</span>
        <span class="text-muted" style="margin-left:10px;font-size:12px">From: ${esc(emailStatus.from_address || emailStatus.host)}</span>`;
      document.getElementById('admin-email-config-hint').style.display = 'none';
    } else {
      emailEl.innerHTML = `<span style="color:var(--lc-red)">✗ SMTP not configured</span>
        <span class="text-muted" style="margin-left:10px;font-size:12px">Emails will not be sent.</span>`;
    }
  }

  const pending = users.filter(u => !u.is_active);
  const pendingEl = document.getElementById('admin-pending-list');
  if (pending.length === 0) {
    pendingEl.innerHTML = '<p class="text-muted" style="font-size:13px">No pending registrations.</p>';
  } else {
    pendingEl.innerHTML = pending.map(u => {
      const verifiedBadge = u.email_verified
        ? '<span class="badge badge-green" title="Email address confirmed by the user">✓ Verified</span>'
        : '<span class="badge badge-gray" title="User has not yet clicked the verification link in their email">Unverified</span>';
      // Organization (issue #1 follow-up) — shown so a super admin reviewing
      // a registration that's founding a brand new org can verify its website
      // before approving. safeHttpUrl() guards against a non-http(s) URL (the
      // backend already rejects those at creation time, but this is a second
      // line of defense against rendering something like a javascript: URI as
      // a clickable link in an admin-privileged page).
      const orgLine = u.org_name ? `
        <div style="width:100%;font-size:11px;color:var(--text-muted);margin-top:2px">
          Org: <strong>${esc(u.org_name)}</strong>${u.org_website_url && safeHttpUrl(u.org_website_url)
            ? ` — <a href="${esc(u.org_website_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--lc-blue)">${esc(u.org_website_url)}</a>`
            : (u.org_website_url ? ` — ${esc(u.org_website_url)}` : '')}
        </div>` : '';
      return `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <span class="callsign">${esc(u.callsign)}</span>
        <span>${esc(u.name)}</span>
        <span class="text-muted" style="font-size:12px">${esc(u.email)}</span>
        ${verifiedBadge}
        <span class="text-muted" style="font-size:11px">Registered ${fmt(u.created_at)}</span>
        <div style="margin-left:auto;display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" onclick="adminApprove(${u.id}, this)">✓ Approve</button>
          <button class="btn btn-danger btn-sm" onclick="adminReject(${u.id}, '${esc(u.callsign)}')">✕ Reject</button>
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
      ? '<span class="badge badge-blue">Admin</span>'
      : '<span class="badge badge-gray">Operator</span>';
    const statusBadge = u.is_active
      ? '<span class="badge badge-green">Active</span>'
      : '<span class="badge badge-gray">Pending</span>';

    // Notify toggle — only meaningful for admins
    const notifyCell = u.is_admin
      ? `<button class="btn btn-sm ${u.notify_new_registrations ? 'btn-primary' : 'btn-ghost'}"
           title="${u.notify_new_registrations ? 'Click to stop notifications' : 'Click to receive registration emails'}"
           onclick="adminToggleNotify(${u.id})" style="font-size:12px;padding:3px 8px">
           ${u.notify_new_registrations ? '📧 On' : '✉ Off'}
         </button>`
      : '<span class="text-muted" style="font-size:11px">—</span>';

    const actions = isMe
      ? '<span class="text-muted" style="font-size:11px">you</span>'
      : `<div style="display:flex;gap:4px;flex-wrap:wrap">
          ${!u.is_active ? `<button class="btn btn-primary btn-sm" onclick="adminApprove(${u.id}, this)">Approve</button>` : ''}
          ${u.is_active  ? `<button class="btn btn-ghost btn-sm" onclick="adminDeactivate(${u.id})">Deactivate</button>` : ''}
          ${!u.is_admin  ? `<button class="btn btn-ghost btn-sm" onclick="adminMakeAdmin(${u.id})">Make Admin</button>` : ''}
          <button class="btn btn-danger btn-sm" onclick="adminDelete(${u.id})">Delete</button>
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
    toast('Operator approved', 'success');
    loadAdminUsers();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function adminDeactivate(userId) {
  if (!confirm('Deactivate this account? They will no longer be able to log in.')) return;
  try {
    await apiFetch(`/admin/users/${userId}/deactivate`, { method: 'PATCH' });
    toast('Account deactivated');
    loadAdminUsers();
  } catch (e) { toast(e.message, 'error'); }
}

async function adminMakeAdmin(userId) {
  if (!confirm('Grant admin privileges to this operator?')) return;
  try {
    await apiFetch(`/admin/users/${userId}/make-admin`, { method: 'PATCH' });
    toast('Admin access granted', 'success');
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
      <h3 style="margin:0 0 8px;color:var(--lc-red)">Reject Registration</h3>
      <p style="margin:0 0 14px;font-size:13px;color:var(--text-muted)">
        Rejecting <strong>${esc(callsign)}</strong> will send them a notification email and permanently remove their account.
      </p>
      <div class="form-group" style="margin-bottom:14px">
        <label style="font-size:12px">Custom message <span style="color:var(--text-muted)">(optional — included in the rejection email)</span></label>
        <textarea id="reject-message" class="form-control" rows="3"
          placeholder="e.g. This net is limited to licensed operators in the W7XYZ club area."></textarea>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn btn-ghost" onclick="document.getElementById('reject-modal').remove()">Cancel</button>
        <button class="btn btn-danger" onclick="submitReject(${userId})">Send Rejection & Delete</button>
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
    toast('Rejection sent and account removed');
    loadAdminUsers();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function adminDelete(userId) {
  if (!confirm('Permanently delete this account? This cannot be undone.')) return;
  try {
    await apiFetch(`/admin/users/${userId}`, { method: 'DELETE' });
    toast('Account deleted');
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
async function loadOrgOperators() {
  const orgId = currentUser.current_org_id;
  const [pending, members] = await Promise.all([
    apiFetch(`/orgs/${orgId}/pending-members`).catch(e => { toast(e.message, 'error'); return []; }),
    apiFetch(`/orgs/${orgId}/members`).catch(e => { toast(e.message, 'error'); return []; }),
  ]);

  const pendingEl = document.getElementById('admin-pending-list');
  if (pending.length === 0) {
    pendingEl.innerHTML = '<p class="text-muted" style="font-size:13px">No pending registrations.</p>';
  } else {
    pendingEl.innerHTML = pending.map(m => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <span class="callsign">${esc(m.callsign)}</span>
        <span>${esc(m.name)}</span>
        <span class="text-muted" style="font-size:12px">${esc(m.email)}</span>
        <span class="text-muted" style="font-size:11px">Requested ${fmt(m.requested_at)}</span>
        <div style="margin-left:auto;display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" onclick="orgApproveMember(${orgId}, ${m.user_id}, this)">✓ Approve</button>
          <button class="btn btn-danger btn-sm" onclick="orgRejectMember(${orgId}, ${m.user_id}, '${esc(m.callsign)}')">✕ Reject</button>
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
      ? '<span class="text-muted" style="font-size:11px">you</span>'
      : (m.role === 'admin'
          ? `<button class="btn btn-ghost btn-sm" onclick="orgSetMemberRole(${orgId}, ${m.user_id}, 'member', '${esc(m.callsign)}')">Remove Admin</button>`
          : `<button class="btn btn-ghost btn-sm" onclick="orgSetMemberRole(${orgId}, ${m.user_id}, 'admin', '${esc(m.callsign)}')">Make Admin</button>`);
    return `<tr>
    <td><span class="callsign">${esc(m.callsign)}</span></td>
    <td>${esc(m.name)}</td>
    <td class="text-muted" style="font-size:12px">${esc(m.email)}</td>
    <td>${m.role === 'admin' ? '<span class="badge badge-blue">Org Admin</span>' : '<span class="badge badge-gray">Member</span>'}</td>
    <td><span class="badge badge-green">Active</span></td>
    <td class="text-muted" style="font-size:11px;text-align:center">—</td>
    <td class="text-muted" style="font-size:12px">${fmt(m.requested_at)}</td>
    <td>${roleAction}</td>
  </tr>`;
  }).join('');
}

async function orgApproveMember(orgId, userId, btn) {
  btnLoading(btn, true);
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/approve`, { method: 'PATCH' });
    toast('Member approved', 'success');
    loadOrgOperators();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function orgRejectMember(orgId, userId, callsign) {
  if (!confirm(`Reject ${callsign}'s request to join?`)) return;
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/reject`, { method: 'POST' });
    toast('Request rejected');
    loadOrgOperators();
  } catch (e) { toast(e.message, 'error'); }
}

async function orgSetMemberRole(orgId, userId, role, callsign) {
  const msg = role === 'admin' ? `Grant org admin to ${callsign}?` : `Remove org admin from ${callsign}?`;
  if (!confirm(msg)) return;
  try {
    await apiFetch(`/orgs/${orgId}/members/${userId}/role`, { method: 'PATCH', body: JSON.stringify({ role }) });
    toast(role === 'admin' ? `${callsign} is now an org admin` : `${callsign} is now a member`, 'success');
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
    `<option value="${n.id}">${esc(n.name)} (owner ${esc(n.owner_callsign || '?')})</option>`
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
  if (!netId || !ownerId) return toast('Select a net and a new owner', 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || 'this net';
  const ownerLabel = ownerSelect.selectedOptions[0]?.textContent || 'the selected user';
  if (!confirm(`Change the owner of ${netLabel} to ${ownerLabel}?`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/nets/${netId}/owner`, { method: 'PATCH', body: JSON.stringify({ owner_id: Number(ownerId) }) });
    toast('Net owner changed', 'success');
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
    `<option value="">All Organizations</option>` + orgOptions;
  document.getElementById('reassign-net-org-filter').value = previousFilter;
  document.getElementById('reassign-owner-net-org-filter').innerHTML =
    `<option value="">All Organizations</option>` + orgOptions;
  document.getElementById('reassign-owner-net-org-filter').value = previousOwnerFilter;

  const userOptions = users.map(u =>
    `<option value="${u.id}">${esc(u.callsign)} — ${esc(u.name)} (currently: ${esc(u.org_name || 'no org')})</option>`
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
    `<option value="${n.id}">${esc(n.name)} — ${esc(_reassignOrgsById[n.org_id] || 'unknown org')} (owner ${esc(n.owner_callsign || '?')})</option>`
  ).join('');
}

function filterReassignOwnerNets() {
  const orgFilter = document.getElementById('reassign-owner-net-org-filter').value;
  const filtered = orgFilter
    ? _reassignNets.filter(n => String(n.org_id) === orgFilter)
    : _reassignNets;
  document.getElementById('reassign-owner-net-select').innerHTML = filtered.map(n =>
    `<option value="${n.id}">${esc(n.name)} — ${esc(_reassignOrgsById[n.org_id] || 'unknown org')} (owner ${esc(n.owner_callsign || '?')})</option>`
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
  if (!userId || !orgId) return toast('Select a user and target organization', 'error');
  const userLabel = userSelect.selectedOptions[0]?.textContent || 'this user';
  const orgLabel = orgSelect.selectedOptions[0]?.textContent || 'the selected organization';
  if (!confirm(`Move ${userLabel} to ${orgLabel}? They will be removed from every other organization they belong to.`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/admin/users/${userId}/org`, {
      method: 'PATCH',
      body: JSON.stringify({ org_id: Number(orgId), role }),
    });
    toast('User moved', 'success');
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
  if (!userId || !orgId) return toast('Select a user and an organization', 'error');
  const userLabel = userSelect.selectedOptions[0]?.textContent.split(' — ')[0] || 'User';
  btnLoading(btn, true);
  try {
    const result = await apiFetch(`/admin/users/${userId}/orgs`, {
      method: 'POST',
      body: JSON.stringify({ org_id: Number(orgId), role }),
    });
    toast(`${userLabel} added to ${result.org_name}`, 'success');
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
  if (!netId || !orgId) return toast('Select a net and target organization', 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || 'this net';
  const orgLabel = orgSelect.selectedOptions[0]?.textContent || 'the selected organization';
  if (!confirm(`Move ${netLabel} to ${orgLabel}?`)) return;
  btnLoading(btn, true);
  try {
    const result = await apiFetch(`/admin/nets/${netId}/org`, {
      method: 'PATCH',
      body: JSON.stringify({ org_id: Number(orgId) }),
    });
    toast(result.owner_not_member
      ? `Net moved — its owner isn't a member of ${result.org_name}, so they won't be able to manage it themselves until added`
      : 'Net moved', 'success');
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
  if (!netId || !ownerId) return toast('Select a net and a new owner', 'error');
  const netLabel = netSelect.selectedOptions[0]?.textContent || 'this net';
  const ownerLabel = ownerSelect.selectedOptions[0]?.textContent || 'the selected user';
  if (!confirm(`Change the owner of ${netLabel} to ${ownerLabel}?`)) return;
  btnLoading(btn, true);
  try {
    await apiFetch(`/nets/${netId}/owner`, { method: 'PATCH', body: JSON.stringify({ owner_id: Number(ownerId) }) });
    toast('Net owner changed', 'success');
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
    statusEl.innerHTML = '<span style="color:var(--lc-red)">✗ Not configured</span>';
    return;
  }

  if (data.has_key) {
    const sourceLabel = data.key_source === 'env' ? 'via .env' : 'self-service';
    statusEl.innerHTML = `<span style="color:var(--lc-green)">✓ Configured</span>
      <span class="text-muted" style="margin-left:10px;font-size:12px">${esc(sourceLabel)}</span>`;
    if (data.key_source === 'self-service') clearActions.style.display = '';
    return;
  }

  if (data.request_status === 'pending') {
    statusEl.innerHTML = '<span style="color:var(--lc-gold)">⏳ Key request pending admin review</span>';
    pendingActions.style.display = '';
    return;
  }

  if (data.request_status === 'rejected') {
    statusEl.innerHTML = '<span style="color:var(--lc-red)">✗ Key request was rejected</span>';
    requestForm.style.display = '';
    return;
  }

  statusEl.innerHTML = '<span class="text-muted">No API key configured yet</span>';
  requestForm.style.display = '';
}

async function requestNetRepoKey() {
  const name = document.getElementById('netrepo-req-name').value.trim();
  if (!name) return toast('Name is required', 'error');
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
  navigator.clipboard.writeText(_lastNetRepoKey).then(() => toast('Key copied to clipboard'));
}

async function clearNetRepoKey() {
  if (!confirm('Forget the stored Net Repository key? Nets will stop pushing until a new key is configured.')) return;
  try {
    await apiFetch('/admin/net-repository/key', { method: 'DELETE' });
    _lastNetRepoKey = null;
    toast('Key forgotten');
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
    ? `${c.total} (${c.active} active, ${c.idle} idle)`
    : '—';

  tablesCard.style.display = '';
  const tbody = document.querySelector('#dbstats-tables-table tbody');
  tbody.innerHTML = data.tables.map(t => `
    <tr><td>${esc(t.name)}</td><td>${esc(t.size)}</td><td>${t.row_estimate.toLocaleString()}</td></tr>
  `).join('') || '<tr><td colspan="3" class="text-muted">No tables found</td></tr>';

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

