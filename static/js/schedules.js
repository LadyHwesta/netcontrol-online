// ============================================================
// SCHEDULES
// ============================================================
let schedules = [];

// Convert a "HH:MM" wall-clock time declared in `tz` on `dateStr` (YYYY-MM-DD)
// into a formatted time string in the viewer's own local timezone. Returns
// null if `tz` isn't a recognized IANA zone. DST-aware since it's anchored to
// a real calendar date, not just a fixed offset. Uses formatToParts rather
// than round-tripping through Date-string parsing, since the latter silently
// depends on the *calling* environment's own local timezone and gives wrong
// answers whenever the viewer isn't in UTC.
function zonedTimeToLocalStr(dateStr, timeStr, tz) {
  try {
    const [year, month, day] = dateStr.split('-').map(Number);
    const [hour, minute] = timeStr.split(':').map(Number);
    const guessUtcMs = Date.UTC(year, month - 1, day, hour, minute);

    const parts = Object.fromEntries(
      new Intl.DateTimeFormat('en-US', {
        timeZone: tz, hourCycle: 'h23',
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).formatToParts(new Date(guessUtcMs)).map(p => [p.type, p.value])
    );
    const asUtcMs = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
    const offsetMs = asUtcMs - guessUtcMs;   // tz's UTC offset at that instant
    const actualUtcMs = guessUtcMs - offsetMs;

    return new Date(actualUtcMs).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  } catch {
    return null;
  }
}

// Next upcoming date (YYYY-MM-DD) for a given weekday (0=Monday..6=Sunday),
// used to anchor a *recurring* schedule's local-time conversion to a real date.
function nextDateForWeekday(dayOfWeek) {
  const today = new Date();
  const todayDow = (today.getDay() + 6) % 7; // JS Sunday=0 -> Monday=0 convention
  const daysAhead = (dayOfWeek - todayDow + 7) % 7;
  const d = new Date(today);
  d.setDate(d.getDate() + daysAhead);
  return d.toISOString().slice(0, 10);
}

function detectedTimezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return null; }
}

// Populate the timezone <datalist> for autocomplete, if the browser supports
// Intl.supportedValuesOf (most do; harmless no-op where it isn't available).
function populateTimezoneList() {
  const list = document.getElementById('sched-tz-list');
  if (!list || list.childElementCount || typeof Intl.supportedValuesOf !== 'function') return;
  try {
    list.innerHTML = Intl.supportedValuesOf('timeZone').map(tz => `<option value="${tz}">`).join('');
  } catch { /* unsupported — free-text input still works */ }
}

async function loadScheduleView() {
  await Promise.all([loadSchedules(), loadRegisteredUsers()]);
  await loadUpcoming();
}

async function loadSchedules() {
  try { schedules = await apiFetch(`/nets/${currentNetId}/schedules`); }
  catch { schedules = []; }
  renderSchedules();
}

function renderSchedules() {
  const el = document.getElementById('schedules-list');
  if (schedules.length === 0) { el.innerHTML = '<p class="text-muted" style="font-size:13px">No schedules yet.</p>'; return; }
  const viewerTz = detectedTimezone();
  el.innerHTML = schedules.map(s => {
    const showLocal = viewerTz && viewerTz !== s.timezone;
    const localStr = showLocal ? zonedTimeToLocalStr(nextDateForWeekday(s.day_of_week), s.start_time, s.timezone) : null;
    return `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--border)">
      <span style="font-size:13px"><strong>${esc(s.day_name)}s</strong> at ${esc(s.start_time)} ${esc(s.timezone)}</span>
      ${localStr ? `<span class="text-muted" style="font-size:12px">(${esc(localStr)} your time)</span>` : ''}
      ${s.notes ? `<span class="text-muted" style="font-size:12px">— ${esc(s.notes)}</span>` : ''}
      <button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="deleteSchedule(${s.id})">${t('Delete')}</button>
    </div>
  `;
  }).join('');
}

// Callsign to pre-fill for the current user on this net — the separate GMRS
// callsign on a GMRS net (issue #23), falling back to the amateur one.
function myCallsignForCurrentNet() {
  if (!currentUser) return '';
  return (currentNetIsGmrs && currentUser.gmrs_callsign) ? currentUser.gmrs_callsign : currentUser.callsign;
}

function toggleScheduleForm() {
  const f = document.getElementById('schedule-form');
  const opening = f.style.display === 'none';
  f.style.display = opening ? '' : 'none';
  // Pre-fill callsign from current user
  if (currentUser) document.getElementById('signup-callsign').value = myCallsignForCurrentNet();
  if (opening) {
    populateTimezoneList();
    const tzField = document.getElementById('sched-tz');
    if (!tzField.value) {
      const detected = detectedTimezone();
      if (detected) tzField.value = detected;
    }
  }
}

async function saveSchedule() {
  const day_of_week = parseInt(document.getElementById('sched-day').value);
  const start_time  = document.getElementById('sched-time').value;
  const timezone    = document.getElementById('sched-tz').value.trim() || 'UTC';
  const notes       = document.getElementById('sched-notes').value.trim() || null;
  if (!start_time) return toast(t('Start time required'), 'error');
  try {
    await apiFetch(`/nets/${currentNetId}/schedules`, {
      method: 'POST',
      body: JSON.stringify({ day_of_week, start_time, timezone, notes }),
    });
    toast(t('Schedule added'));
    document.getElementById('schedule-form').style.display = 'none';
    document.getElementById('sched-notes').value = '';
    await loadScheduleView();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteSchedule(id) {
  if (!confirm(t('Delete this schedule and all its sign-ups?'))) return;
  try {
    await apiFetch(`/schedules/${id}`, { method: 'DELETE' });
    toast(t('Schedule deleted'));
    await loadScheduleView();
  } catch (e) { toast(e.message, 'error'); }
}


// ============================================================
// UPCOMING SLOTS
// ============================================================
async function loadUpcoming() {
  const empty = document.getElementById('upcoming-empty');
  const grid  = document.getElementById('upcoming-slots');
  if (schedules.length === 0) { grid.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';

  let slots = [];
  try { slots = await apiFetch(`/nets/${currentNetId}/upcoming?weeks=8`); }
  catch { slots = []; }

  // Build a lookup of scheduleId → schedule for time display
  const schedMap = Object.fromEntries(schedules.map(s => [s.id, s]));
  const net = nets.find(n => n.id === currentNetId);
  const hasBroadcast = !!(net && net.has_broadcast);
  const broadcastLabel = (net && net.broadcast_label) || 'Broadcaster';
  const isOwner = currentUser && currentNetOwnerId === currentUser.id;

  function roleRow(label, signup, role, slot, dateStr, isPast) {
    if (signup) {
      const canRemove = signup.is_mine || isOwner;
      return `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span style="font-size:10px;color:var(--text-muted);min-width:70px">${esc(label)}</span>
        <span class="slot-claimed">
          <span class="callsign">${esc(signup.callsign)}</span>
          ${signup.name ? `<span class="text-muted"> — ${esc(signup.name)}</span>` : ''}
          ${signup.role === 'both' ? '<span class="text-muted"> (both roles)</span>' : ''}
        </span>
        ${canRemove ? `<button class="btn btn-ghost btn-sm" onclick="removeSignup(${signup.id})">✕</button>` : ''}
      </div>`;
    }
    if (isPast) {
      return `<div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;color:var(--text-muted);min-width:70px">${esc(label)}</span>
        <span class="lookup-notfound">No sign-up</span>
      </div>`;
    }
    return `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
      <span style="font-size:10px;color:var(--text-muted);min-width:70px">${esc(label)}</span>
      <button class="btn btn-success btn-sm" onclick="openSignupModal(${slot.schedule_id}, '${slot.slot_date}', '${esc(dateStr)}', '${role}')">+ Sign Up</button>
      ${isOwner ? `<button class="btn btn-ghost btn-sm" onclick="openAssignModal(${slot.schedule_id}, '${slot.slot_date}', '${esc(dateStr)}', '${role}')">👤 Assign</button>` : ''}
    </div>`;
  }

  const viewerTz = detectedTimezone();

  grid.innerHTML = slots.map(slot => {
    const sched = schedMap[slot.schedule_id] || {};
    const dateObj = new Date(slot.slot_date + 'T00:00:00');
    const dateStr = dateObj.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
    const isPast  = slot.slot_date < new Date().toISOString().slice(0, 10);
    const showLocal = viewerTz && sched.timezone && viewerTz !== sched.timezone;
    const localStr = showLocal ? zonedTimeToLocalStr(slot.slot_date, sched.start_time, sched.timezone) : null;

    const ncSignup = slot.signups.find(s => s.role === 'net_control' || s.role === 'both');
    const bcSignup = slot.signups.find(s => s.role === 'broadcaster' || s.role === 'both');

    let statusHtml = roleRow('Net Control', ncSignup, 'net_control', slot, dateStr, isPast);
    if (hasBroadcast) {
      statusHtml += roleRow(broadcastLabel, bcSignup, 'broadcaster', slot, dateStr, isPast);
      if (!ncSignup && !bcSignup && !isPast) {
        statusHtml += `<div><button class="btn btn-ghost btn-sm" onclick="openSignupModal(${slot.schedule_id}, '${slot.slot_date}', '${esc(dateStr)}', 'both')">+ Cover Both Roles</button></div>`;
      }
    }

    return `<div class="slot-row${isPast ? '" style="opacity:.5' : ''}">
      <span class="slot-date">${dateStr}</span>
      <span class="slot-time text-muted">${esc(sched.start_time || '')} ${esc(sched.timezone || '')}${localStr ? ` (${esc(localStr)} your time)` : ''}</span>
      <span class="slot-status" style="display:flex;flex-direction:column;gap:5px">${statusHtml}</span>
    </div>`;
  }).join('');
}

// ============================================================
// SIGNUP MODAL
// ============================================================
function roleLabelFor(role) {
  const net = nets.find(n => n.id === currentNetId);
  const bcLabel = (net && net.broadcast_label) || t('Broadcaster');
  return { net_control: t('Net Control'), broadcaster: bcLabel, both: `${t('Net Control')} & ${bcLabel}` }[role] || t('Net Control');
}

function openSignupModal(scheduleId, slotDate, dateLabel, role) {
  document.getElementById('signup-schedule-id').value = scheduleId;
  document.getElementById('signup-slot-date').value   = slotDate;
  document.getElementById('signup-role').value        = role || 'net_control';
  document.getElementById('signup-date-label').textContent = dateLabel;
  document.getElementById('signup-modal-title').textContent = `${t('Sign Up for')} ${roleLabelFor(role)}`;
  // Pre-fill from current user
  if (currentUser) {
    document.getElementById('signup-callsign').value = myCallsignForCurrentNet();
    document.getElementById('signup-name').value     = currentUser.name || '';
    document.getElementById('signup-email').value    = currentUser.email || '';
  }
  const modal = document.getElementById('signup-modal');
  modal.style.display = 'flex';
}

function closeSignupModal() {
  document.getElementById('signup-modal').style.display = 'none';
}

async function submitSignup() {
  const schedule_id = parseInt(document.getElementById('signup-schedule-id').value);
  const slot_date   = document.getElementById('signup-slot-date').value;
  const role        = document.getElementById('signup-role').value || 'net_control';
  const callsign    = document.getElementById('signup-callsign').value.trim().toUpperCase();
  const name        = document.getElementById('signup-name').value.trim() || null;
  const email       = document.getElementById('signup-email').value.trim() || null;
  const notes       = document.getElementById('signup-notes').value.trim() || null;
  if (!callsign) return toast(t('Callsign required'), 'error');
  const btn = document.querySelector('#signup-modal .btn-primary');
  btnLoading(btn, true);
  try {
    await apiFetch(`/nets/${currentNetId}/signups`, {
      method: 'POST',
      body: JSON.stringify({ schedule_id, slot_date, role, callsign, name, email, notes }),
    });
    toast(`${callsign} signed up for ${roleLabelFor(role)}`, 'success');
    closeSignupModal();
    await loadUpcoming();
  } catch (e) {
    toast(e.message, 'error');
    btnLoading(btn, false);
  }
}

async function removeSignup(id) {
  if (!confirm(t('Remove this sign-up?'))) return;
  try {
    await apiFetch(`/signups/${id}`, { method: 'DELETE' });
    toast(t('Sign-up removed'));
    await loadUpcoming();
  } catch (e) { toast(e.message, 'error'); }
}

// Close modals on backdrop click
document.getElementById('signup-modal').addEventListener('click', function(e) {
  if (e.target === this) closeSignupModal();
});
document.getElementById('assign-modal').addEventListener('click', function(e) {
  if (e.target === this) closeAssignModal();
});

// ============================================================
// ASSIGN MODAL
// ============================================================
function openAssignModal(scheduleId, slotDate, dateLabel, role) {
  document.getElementById('assign-schedule-id').value = scheduleId;
  document.getElementById('assign-slot-date').value   = slotDate;
  document.getElementById('assign-role').value        = role || 'net_control';
  document.getElementById('assign-date-label').textContent = dateLabel;
  document.getElementById('assign-modal-title').textContent = `${t('Assign')} ${roleLabelFor(role)}`;
  document.getElementById('assign-notes').value = '';
  document.getElementById('assign-preview').style.display = 'none';

  // Populate user dropdown
  const sel = document.getElementById('assign-user-select');
  sel.innerHTML = '<option value="">— choose a registered operator —</option>' +
    registeredUsers.map(u =>
      `<option value="${u.id}">${esc(u.callsign)} — ${esc(u.name)}</option>`
    ).join('');

  document.getElementById('assign-modal').style.display = 'flex';
}

function closeAssignModal() {
  document.getElementById('assign-modal').style.display = 'none';
}

function onAssignUserChange() {
  const sel = document.getElementById('assign-user-select');
  const userId = parseInt(sel.value);
  const preview = document.getElementById('assign-preview');
  if (!userId) { preview.style.display = 'none'; return; }
  const user = registeredUsers.find(u => u.id === userId);
  if (!user) { preview.style.display = 'none'; return; }
  const callsign = (currentNetIsGmrs && user.gmrs_callsign) ? user.gmrs_callsign : user.callsign;
  document.getElementById('assign-preview-call').textContent = callsign;
  document.getElementById('assign-preview-name').textContent = ' — ' + user.name;
  preview.style.display = '';
}

async function submitAssign() {
  const schedule_id      = parseInt(document.getElementById('assign-schedule-id').value);
  const slot_date        = document.getElementById('assign-slot-date').value;
  const role              = document.getElementById('assign-role').value || 'net_control';
  const assigned_user_id = parseInt(document.getElementById('assign-user-select').value);
  const notes            = document.getElementById('assign-notes').value.trim() || null;
  if (!assigned_user_id) return toast(t('Please select an operator'), 'error');
  try {
    await apiFetch(`/nets/${currentNetId}/signups`, {
      method: 'POST',
      body: JSON.stringify({ schedule_id, slot_date, role, assigned_user_id, notes }),
    });
    const user = registeredUsers.find(u => u.id === assigned_user_id);
    toast(`${user ? user.callsign : 'Operator'} assigned as ${roleLabelFor(role)}`, 'success');
    closeAssignModal();
    await loadUpcoming();
  } catch (e) { toast(e.message, 'error'); }
}

onEnter(['sched-time', 'sched-tz', 'sched-notes'], saveSchedule);
onEnter(['signup-callsign', 'signup-name', 'signup-email', 'signup-notes'], submitSignup);
onEnter(['assign-notes'], submitAssign);

