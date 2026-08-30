// ============================================================
// NETS
// ============================================================
async function loadNets() {
  try { nets = await apiFetch('/nets'); } catch { nets = []; }
  renderNets();
}

function renderNets() {
  const el = document.getElementById('nets-list');
  if (nets.length === 0) {
    el.innerHTML = '<div class="empty"><p>No nets yet. Create your first net above.</p></div>';
    return;
  }
  el.innerHTML = nets.map(n => {
    const aresBadge = n.is_ares
      ? ' <span style="font-size:11px;background:var(--lc-orange);color:#000;border-radius:10px;padding:2px 8px;font-weight:700;vertical-align:middle">ARES</span>'
      : '';
    const gmrsBadge = n.net_type === 'gmrs'
      ? ' <span style="font-size:11px;background:#22c55e;color:#000;border-radius:10px;padding:2px 8px;font-weight:700;vertical-align:middle">GMRS</span>'
      : '';
    // Shared badge — shown for nets not owned by current user; "Editor"
    // instead of "Shared" when this viewer's own share grants edit rights.
    const sharedBadge = !n.is_owner
      ? ` <span style="font-size:11px;background:var(--lc-blue);color:#000;border-radius:10px;padding:2px 8px;font-weight:700;vertical-align:middle">${n.can_edit ? 'Editor' : 'Shared'}</span>`
      : '';
    // Share status line shown under the net name for owners
    let shareInfo = '';
    if (n.is_owner) {
      if (n.shared_with_all) {
        shareInfo = '<div style="font-size:11px;color:var(--lc-blue);margin-top:2px">🔗 Shared with all users</div>';
      } else if (n.shared_user_ids && n.shared_user_ids.length > 0) {
        shareInfo = `<div style="font-size:11px;color:var(--lc-blue);margin-top:2px">🔗 Shared with ${n.shared_user_ids.length} user${n.shared_user_ids.length > 1 ? 's' : ''}</div>`;
      }
    } else if (n.owner_callsign) {
      shareInfo = `<div style="font-size:11px;color:var(--lc-muted);margin-top:2px">Owner: ${esc(n.owner_callsign)}</div>`;
    }
    // Edit is available to the owner, an admin, or an editor-rights share;
    // Delete stays owner/admin-only regardless (destructive, see
    // _get_owned_net vs _get_editable_net in main.py).
    const editDeleteBtns = (n.can_edit ? `<button class="btn btn-ghost btn-sm" onclick="editNet(${n.id})">Edit</button>` : '')
      + (n.is_owner ? `<button class="btn btn-danger btn-sm" onclick="deleteNet(${n.id})">Delete</button>` : '');
    return `
    <div class="card" style="max-width:600px">
      <div class="card-header">
        <div>
          <h2>${esc(n.name)}${gmrsBadge}${aresBadge}${sharedBadge}</h2>
          ${n.frequency ? `<span class="text-muted" style="font-size:12px">📡 ${esc(n.frequency)}</span>` : ''}
          ${shareInfo}
        </div>
        <div class="actions">
          <button class="btn btn-primary btn-sm" onclick="openNet(${n.id})">Open</button>
          ${editDeleteBtns}
        </div>
      </div>
      ${n.description ? `<p class="text-muted" style="font-size:13px">${esc(n.description)}</p>` : ''}
    </div>`;
  }).join('');
}

function showNetForm() {
  editNetId = null;
  document.getElementById('net-form-title').textContent = 'New Net';
  document.getElementById('net-name').value = '';
  document.getElementById('net-freq').value = '';
  document.getElementById('net-dmr-tg').value = '';
  document.getElementById('net-desc').value = '';
  document.getElementById('net-script').value = '';
  document.getElementById('net-ares').checked = false;
  document.getElementById('net-has-broadcast').checked = false;
  document.getElementById('net-broadcast-label').value = '';
  onBroadcastToggle();
  document.getElementById('net-reminder-enabled').checked = false;
  document.getElementById('net-reminder-minutes').value = '';
  onReminderToggle();
  document.getElementById('net-public-listed').checked = false;
  document.getElementById('net-band').value = '';
  document.getElementById('net-mode').value = '';
  document.getElementById('net-ctcss-tone').value = '';
  document.getElementById('net-region').value = '';
  document.getElementById('net-state').value = '';
  document.getElementById('net-website').value = '';
  onPublicListedToggle();
  document.querySelector('input[name="net-type"][value="ham"]').checked = true;
  document.getElementById('net-sharing-section').style.display = 'none';
  document.getElementById('net-dmr-section').style.display = 'none';
  document.getElementById('net-aprs-section').style.display = 'none';
  document.getElementById('net-aprs-map-enabled').checked = false;
  document.getElementById('net-form-card').style.display = '';
  onNetTypeChange();
  switchNetFormTab('details');
}

function onBroadcastToggle() {
  const on = document.getElementById('net-has-broadcast').checked;
  document.getElementById('net-broadcast-label-group').style.display = on ? '' : 'none';
}

function onReminderToggle() {
  const on = document.getElementById('net-reminder-enabled').checked;
  document.getElementById('net-reminder-minutes-group').style.display = on ? '' : 'none';
}

function onPublicListedToggle() {
  const on = document.getElementById('net-public-listed').checked;
  document.getElementById('net-repo-metadata-section').style.display = on ? '' : 'none';
}

function onNetTypeChange() {
  const isGmrs = document.querySelector('input[name="net-type"]:checked')?.value === 'gmrs';
  document.getElementById('net-ares-section').style.display = isGmrs ? 'none' : '';
  document.getElementById('net-dmr-tg-group').style.display = isGmrs ? 'none' : '';
  if (isGmrs) {
    document.getElementById('net-ares').checked = false;
    document.getElementById('net-dmr-tg').value = '';
    // DMR/APRS integration sections should also stay hidden for GMRS —
    // neither has a GMRS allocation.
    document.getElementById('net-dmr-section').style.display = 'none';
    document.getElementById('net-aprs-section').style.display = 'none';
    document.getElementById('net-aprs-map-enabled').checked = false;
  }
}

// Net Script tab (issue #24) — the script editor used to be one cramped field
// among many in Details; it now gets its own tab with room for a formatting
// toolbar, a clickable variable reference, and a live preview.
function switchNetFormTab(tab) {
  document.getElementById('net-form-details-panel').style.display = tab === 'details' ? '' : 'none';
  document.getElementById('net-form-script-panel').style.display = tab === 'script' ? '' : 'none';
  document.getElementById('net-form-tab-details-btn').classList.toggle('active', tab === 'details');
  document.getElementById('net-form-tab-script-btn').classList.toggle('active', tab === 'script');
  // The Details tab was designed/laid out at a narrower width; give the script
  // editor (toolbar + big textarea + preview) more room to breathe.
  document.getElementById('net-form-card').style.maxWidth = tab === 'script' ? '960px' : '500px';
  if (tab === 'script') renderScriptPreview();
}

function toggleScriptVarsPanel() {
  const body = document.getElementById('script-vars-body');
  const icon = document.getElementById('script-vars-toggle-icon');
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  icon.textContent = open ? '▼' : '▶';
}

// Replaces the current selection with `text` (or inserts at the cursor if
// nothing's selected) — used for variable insertion, which should never wrap
// existing text the way Bold/Italic do.
function insertScriptText(text) {
  const ta = document.getElementById('net-script');
  const start = ta.selectionStart, end = ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  const pos = start + text.length;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  renderScriptPreview();
}

// Wraps the current selection in before/after (Bold/Italic) — with no
// selection, just places the cursor between the two so typing continues inline.
function wrapScriptSelection(before, after) {
  const ta = document.getElementById('net-script');
  const start = ta.selectionStart, end = ta.selectionEnd;
  const selected = ta.value.slice(start, end);
  const insertion = before + selected + after;
  ta.value = ta.value.slice(0, start) + insertion + ta.value.slice(end);
  const pos = selected ? start + insertion.length : start + before.length;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  renderScriptPreview();
}

// Inserts `prefix` at the start of the current line (heading levels, bullet list).
function insertScriptLinePrefix(prefix) {
  const ta = document.getElementById('net-script');
  const start = ta.selectionStart;
  const value = ta.value;
  const lineStart = value.lastIndexOf('\n', start - 1) + 1;
  ta.value = value.slice(0, lineStart) + prefix + value.slice(lineStart);
  const pos = start + prefix.length;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  renderScriptPreview();
}

// Inserts a horizontal rule on its own line at the cursor.
function insertScriptRule() {
  const ta = document.getElementById('net-script');
  const start = ta.selectionStart;
  const value = ta.value;
  const needsNewlineBefore = start > 0 && value[start - 1] !== '\n';
  const insertion = (needsNewlineBefore ? '\n' : '') + '---\n';
  ta.value = value.slice(0, start) + insertion + value.slice(start);
  const pos = start + insertion.length;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  renderScriptPreview();
}

// Sample values for the preview — there's no live session to pull real duty-bar
// data from while editing a net, so these are clearly-fake placeholders. Order
// (name — callsign) matches combineNameCallsign() in sessions.js so the preview
// looks exactly like the real render.
const NET_SCRIPT_PREVIEW_VARS = {
  net_control: 'Jane Operator — W1AW',
  net_control_callsign: 'W1AW',
  net_control_name: 'Jane Operator',
  broadcaster: 'Bob Smith — K7ABC',
  broadcaster_callsign: 'K7ABC',
  broadcaster_name: 'Bob Smith',
  broadcast_label: 'Amateur Radio Newsline',
  net_control_next: 'Sam Lee — N7XYZ',
  net_control_next_callsign: 'N7XYZ',
  net_control_next_name: 'Sam Lee',
  broadcaster_next: 'Alex Kim — K9DEF',
  broadcaster_next_callsign: 'K9DEF',
  broadcaster_next_name: 'Alex Kim',
};

// Renders the textarea's current content through the same {{variable}}
// substitution + markup pipeline the live session uses (escapeHtml /
// scriptBlockFormat, defined in sessions.js — loaded on this page too).
const NET_SCRIPT_MIN_HEIGHT = 240; // px — floor so the editor doesn't collapse when the script is short/empty

function renderScriptPreview() {
  const previewEl = document.getElementById('net-script-preview');
  const ta = document.getElementById('net-script');
  if (!previewEl || !ta) return;
  const text = ta.value;
  if (!text.trim()) {
    previewEl.innerHTML = '<p class="text-muted" style="font-size:12px;margin:0">Nothing to preview yet — start typing above.</p>';
  } else {
    const netName = document.getElementById('net-name').value.trim() || 'Your Net';
    const vars = { net_name: netName, ...NET_SCRIPT_PREVIEW_VARS };
    let escaped = escapeHtml(text);
    escaped = escaped.replace(/\{\{\s*(\w+)\s*\}\}/g, (match, key) => (key in vars ? escapeHtml(vars[key]) : match));
    previewEl.innerHTML = scriptBlockFormat(escaped);
  }
  // Auto-grow the editor to fit its own content (issue #24 follow-up) —
  // resetting to auto first is what lets scrollHeight shrink back down when
  // text is deleted, not just grow.
  ta.style.height = 'auto';
  ta.style.height = Math.max(ta.scrollHeight, NET_SCRIPT_MIN_HEIGHT) + 'px';
}

function cancelNetForm() {
  document.getElementById('net-form-card').style.display = 'none';
  document.getElementById('net-sharing-section').style.display = 'none';
  document.getElementById('net-dmr-section').style.display = 'none';
  document.getElementById('net-aprs-section').style.display = 'none';
  editNetId = null;
  shareState = { share_with_all: false, can_edit_all: false, user_ids: [], editor_user_ids: [] };
  switchNetFormTab('details');
}

async function editNet(id) {
  const n = nets.find(x => x.id === id);
  if (!n) return;
  editNetId = id;
  document.getElementById('net-form-title').textContent = 'Edit Net';
  document.getElementById('net-name').value = n.name;
  document.getElementById('net-freq').value = n.frequency || '';
  document.getElementById('net-dmr-tg').value = n.dmr_talkgroup || '';
  document.getElementById('net-desc').value = n.description || '';
  document.getElementById('net-script').value = n.script || '';
  document.getElementById('net-ares').checked = !!n.is_ares;
  document.getElementById('net-has-broadcast').checked = !!n.has_broadcast;
  document.getElementById('net-broadcast-label').value = n.broadcast_label || '';
  onBroadcastToggle();
  document.getElementById('net-reminder-enabled').checked = !!n.reminder_enabled;
  document.getElementById('net-reminder-minutes').value = n.reminder_minutes_before || '';
  onReminderToggle();
  document.getElementById('net-public-listed').checked = !!n.public_listed;
  document.getElementById('net-band').value = n.band || '';
  document.getElementById('net-mode').value = n.mode || '';
  document.getElementById('net-ctcss-tone').value = n.ctcss_tone || '';
  document.getElementById('net-region').value = n.region || '';
  document.getElementById('net-state').value = n.state || '';
  document.getElementById('net-website').value = n.website || '';
  document.getElementById('net-aprs-map-enabled').checked = !!n.aprs_map_enabled;
  onPublicListedToggle();
  const netType = n.net_type || 'ham';
  const typeRadio = document.querySelector(`input[name="net-type"][value="${netType}"]`);
  if (typeRadio) typeRadio.checked = true;
  onNetTypeChange();
  document.getElementById('net-form-card').style.display = '';
  switchNetFormTab('details');
  // Sharing management stays owner/admin-only (an editor granting further
  // access would be a privilege-escalation chain -- see _get_owned_net vs
  // _get_editable_net in main.py). DMR config is available to anyone with
  // edit rights, including an editor-rights share, same as the rest of
  // this form (ham nets only).
  document.getElementById('net-sharing-section').style.display = n.is_owner ? '' : 'none';
  if (n.is_owner) await loadSharesForNet(id);
  if (n.can_edit && netType === 'ham') {
    await loadDmrConfig(id);
    await loadAprsConfig(id);
  } else {
    document.getElementById('net-dmr-section').style.display = 'none';
    document.getElementById('net-aprs-section').style.display = 'none';
  }
}

async function saveNet() {
  const name = document.getElementById('net-name').value.trim();
  const frequency = document.getElementById('net-freq').value.trim() || null;
  const description = document.getElementById('net-desc').value.trim() || null;
  const script = document.getElementById('net-script').value.trim() || null;
  const net_type = document.querySelector('input[name="net-type"]:checked')?.value || 'ham';
  const is_gmrs = net_type === 'gmrs';
  const is_ares = is_gmrs ? false : document.getElementById('net-ares').checked;
  const dmr_talkgroup = is_gmrs ? null : (document.getElementById('net-dmr-tg').value.trim() || null);
  const has_broadcast = document.getElementById('net-has-broadcast').checked;
  const broadcast_label = has_broadcast ? (document.getElementById('net-broadcast-label').value.trim() || null) : null;
  const reminder_enabled = document.getElementById('net-reminder-enabled').checked;
  const reminderMinutesRaw = document.getElementById('net-reminder-minutes').value.trim();
  const reminder_minutes_before = reminder_enabled ? (parseInt(reminderMinutesRaw, 10) || 30) : null;
  const public_listed = document.getElementById('net-public-listed').checked;
  const band = document.getElementById('net-band').value.trim() || null;
  const mode = document.getElementById('net-mode').value.trim() || null;
  const ctcss_tone = document.getElementById('net-ctcss-tone').value.trim() || null;
  const region = document.getElementById('net-region').value.trim() || null;
  const state = document.getElementById('net-state').value.trim() || null;
  const website = document.getElementById('net-website').value.trim() || null;
  const aprs_map_enabled = is_gmrs ? false : document.getElementById('net-aprs-map-enabled').checked;
  if (!name) return toast('Net name is required', 'error');
  try {
    if (editNetId) {
      await apiFetch(`/nets/${editNetId}`, { method: 'PUT', body: JSON.stringify({ name, frequency, dmr_talkgroup, description, script, net_type, is_ares, has_broadcast, broadcast_label, reminder_enabled, reminder_minutes_before, public_listed, aprs_map_enabled, band, mode, ctcss_tone, region, state, website }) });
      // Sharing also has its own "Save Sharing" button below, but folding it into
      // this main save too means checking a share box and clicking the obvious
      // "save the form" button actually persists it -- previously that button
      // only saved the net's other fields, silently dropping any sharing change
      // that hadn't separately been saved. APRS config is folded in the same way
      // from the start, rather than needing the same fix later.
      if (document.getElementById('net-sharing-section').style.display !== 'none') {
        await apiFetch(`/nets/${editNetId}/shares`, { method: 'PUT', body: JSON.stringify(_shareStatePayload()) });
      }
      await saveAprsConfigIfVisible(editNetId);
      toast('Net updated');
    } else {
      await apiFetch('/nets', { method: 'POST', body: JSON.stringify({ name, frequency, dmr_talkgroup, description, script, net_type, is_ares, has_broadcast, broadcast_label, reminder_enabled, reminder_minutes_before, public_listed, aprs_map_enabled, band, mode, ctcss_tone, region, state, website }) });
      toast('Net created');
    }
    cancelNetForm();
    await loadNets();
  } catch (e) { toast(e.message, 'error'); }
}

// --- Sharing helpers ---

async function loadSharesForNet(netId) {
  try {
    // Load users and current shares in parallel
    const [shares, users] = await Promise.all([
      apiFetch(`/nets/${netId}/shares`),
      apiFetch(`/users?net_id=${netId}`),
    ]);
    allUsers = users;
    shareState = {
      share_with_all: shares.share_with_all,
      can_edit_all: shares.can_edit_all || false,
      user_ids: shares.user_ids || [],
      editor_user_ids: shares.editor_user_ids || [],
    };
    document.getElementById('net-share-all').checked = shareState.share_with_all;
    document.getElementById('net-share-all-edit').checked = shareState.can_edit_all;
    document.getElementById('net-share-all-edit-wrap').style.display = shareState.share_with_all ? '' : 'none';
    renderShareUserList();
    document.getElementById('net-share-users').style.display = shareState.share_with_all ? 'none' : '';
  } catch (e) {
    console.warn('Could not load shares:', e);
  }
}

function renderShareUserList() {
  const el = document.getElementById('net-share-user-list');
  if (allUsers.length === 0) {
    el.innerHTML = '<div class="text-muted" style="font-size:12px">No other registered users found.</div>';
    return;
  }
  el.innerHTML = allUsers.map(u => {
    const shared = shareState.user_ids.includes(u.id);
    const canEdit = shareState.editor_user_ids.includes(u.id);
    return `
    <div style="display:flex;align-items:center;gap:12px;padding:3px 0;font-size:13px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1;min-width:0">
        <input type="checkbox" data-uid="${u.id}" style="accent-color:var(--lc-blue);flex-shrink:0"
          ${shared ? 'checked' : ''} onchange="toggleShareUser(${u.id}, this.checked)" />
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(u.callsign)} — ${esc(u.name)}</span>
      </label>
      <label data-edit-row="${u.id}" style="display:${shared ? 'flex' : 'none'};align-items:center;gap:5px;cursor:pointer;font-size:11px;color:var(--text-muted);flex-shrink:0">
        <input type="checkbox" style="accent-color:var(--lc-orange)"
          ${canEdit ? 'checked' : ''} onchange="toggleShareUserEdit(${u.id}, this.checked)" />
        Can edit
      </label>
    </div>`;
  }).join('');
}

function onShareAllChanged() {
  shareState.share_with_all = document.getElementById('net-share-all').checked;
  document.getElementById('net-share-users').style.display = shareState.share_with_all ? 'none' : '';
  document.getElementById('net-share-all-edit-wrap').style.display = shareState.share_with_all ? '' : 'none';
}

function onShareAllEditChanged() {
  shareState.can_edit_all = document.getElementById('net-share-all-edit').checked;
}

function toggleShareUser(uid, checked) {
  if (checked) {
    if (!shareState.user_ids.includes(uid)) shareState.user_ids.push(uid);
  } else {
    shareState.user_ids = shareState.user_ids.filter(id => id !== uid);
    // Losing sharing loses edit rights too -- can't edit a net you can't see.
    shareState.editor_user_ids = shareState.editor_user_ids.filter(id => id !== uid);
  }
  const row = document.querySelector(`[data-edit-row="${uid}"]`);
  if (row) {
    row.style.display = checked ? 'flex' : 'none';
    if (!checked) row.querySelector('input').checked = false;
  }
}

function toggleShareUserEdit(uid, checked) {
  if (checked) {
    if (!shareState.editor_user_ids.includes(uid)) shareState.editor_user_ids.push(uid);
  } else {
    shareState.editor_user_ids = shareState.editor_user_ids.filter(id => id !== uid);
  }
}

function _shareStatePayload() {
  return {
    share_with_all: shareState.share_with_all,
    can_edit_all: shareState.can_edit_all,
    user_ids: shareState.user_ids,
    editor_user_ids: shareState.editor_user_ids,
  };
}

async function saveSharing() {
  if (!editNetId) return;
  try {
    await apiFetch(`/nets/${editNetId}/shares`, { method: 'PUT', body: JSON.stringify(_shareStatePayload()) });
    toast('Sharing saved');
    await loadNets();
    // Re-render to update share info on card without closing form
    const n = nets.find(x => x.id === editNetId);
    if (n) {
      shareState.share_with_all = n.shared_with_all;
      shareState.can_edit_all = n.can_edit_all;
      shareState.user_ids = n.shared_user_ids || [];
      shareState.editor_user_ids = n.editor_user_ids || [];
    }
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteNet(id) {
  if (!confirm('Delete this net and ALL its sessions and check-ins? This cannot be undone.')) return;
  try {
    await apiFetch(`/nets/${id}`, { method: 'DELETE' });
    toast('Net deleted');
    await loadNets();
  } catch (e) { toast(e.message, 'error'); }
}

onEnter(['net-name', 'net-freq', 'net-dmr-tg', 'net-broadcast-label', 'net-reminder-minutes',
         'net-band', 'net-mode', 'net-ctcss-tone', 'net-region', 'net-state', 'net-website',
         'aprs-fi-key', 'aprs-filter'], saveNet);

