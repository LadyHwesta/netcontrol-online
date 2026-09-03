// ============================================================
// MY ASSIGNMENTS (role revamp, issue follow-up) — self-service sign-up for
// the Tactical Operator and Broadcaster roles. Sign-on/off and schedule
// signup/cancel reuse the existing tactical-position and net-control-signup
// endpoints unchanged; this page just surfaces "mine, across every net I
// hold the role on" rather than requiring the full My Nets flow.
// ============================================================

async function loadAssignments() {
  let tacticalNets = [];
  let broadcasterSlots = [];
  try {
    [tacticalNets, broadcasterSlots] = await Promise.all([
      apiFetch('/my/tactical-assignments'),
      apiFetch('/my/broadcaster-assignments'),
    ]);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }

  const tacticalSection = document.getElementById('tactical-assignments-section');
  const broadcasterSection = document.getElementById('broadcaster-assignments-section');
  const emptyCard = document.getElementById('assignments-empty');

  if (tacticalNets.length === 0 && broadcasterSlots.length === 0) {
    tacticalSection.style.display = 'none';
    broadcasterSection.style.display = 'none';
    emptyCard.style.display = '';
    return;
  }
  emptyCard.style.display = 'none';

  if (tacticalNets.length) {
    tacticalSection.style.display = '';
    document.getElementById('tactical-assignments-list').innerHTML = tacticalNets.map(renderTacticalNetCard).join('');
  } else {
    tacticalSection.style.display = 'none';
  }

  if (broadcasterSlots.length) {
    broadcasterSection.style.display = '';
    document.getElementById('broadcaster-assignments-list').innerHTML = renderBroadcasterSlotsTable(broadcasterSlots);
  } else {
    broadcasterSection.style.display = 'none';
  }
}

function myOwnCallsign(netType) {
  return (netType === 'gmrs' && currentUser.gmrs_callsign) ? currentUser.gmrs_callsign : currentUser.callsign;
}

function renderTacticalNetCard(net) {
  if (net.note) {
    // net.note is always this one backend-sourced string today (routers/
    // assignments.py) -- called through t() as a literal, not t(net.note),
    // so extract_i18n_strings.py's regex (literal first-arg only) picks it
    // up for pre-translation.
    return `
    <div class="card" style="margin-bottom:12px">
      <h4 style="margin:0 0 4px;font-size:14px">${esc(net.net_name)}</h4>
      <p class="text-muted" style="font-size:12px;margin:0">${esc(t('No activation is currently live on this net'))}</p>
    </div>`;
  }
  const rows = net.positions.map(p => {
    const mine = p.current_callsign && p.current_callsign.toUpperCase() === myOwnCallsign(net.net_type).toUpperCase();
    let action;
    if (!p.current_callsign) {
      action = `<button class="btn btn-primary btn-sm" onclick="signOnAssignment(${p.id})" data-i18n="Sign On">${t('Sign On')}</button>`;
    } else if (mine) {
      action = `<button class="btn btn-ghost btn-sm" onclick="signOffAssignment(${p.id})" data-i18n="Sign Off">${t('Sign Off')}</button>`;
    } else {
      action = `<span class="text-muted" style="font-size:11px">${t('Occupied')}</span>`;
    }
    return `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
      <span class="callsign">${esc(p.tactical_callsign)}</span>
      ${p.location ? `<span class="text-muted" style="font-size:12px">${esc(p.location)}</span>` : ''}
      <span style="margin-left:auto;font-size:12px;color:${mine ? 'var(--lc-green)' : 'var(--text-muted)'}">
        ${p.current_callsign ? esc(p.current_callsign) : t('Vacant')}
      </span>
      ${action}
    </div>`;
  }).join('');
  return `
  <div class="card" style="margin-bottom:12px">
    <h4 style="margin:0 0 8px;font-size:14px">${esc(net.net_name)}</h4>
    ${rows || `<p class="text-muted" style="font-size:12px;margin:0" data-i18n="No tactical positions set up for this activation yet.">${t('No tactical positions set up for this activation yet.')}</p>`}
  </div>`;
}

async function signOnAssignment(positionId) {
  try {
    await apiFetch(`/tactical-positions/${positionId}/sign-on`, {
      method: 'POST',
      body: JSON.stringify({ callsign: currentUser.callsign, name: currentUser.name }),
    });
    toast(t('Signed on'), 'success');
    await loadAssignments();
  } catch (e) { toast(e.message, 'error'); }
}

async function signOffAssignment(positionId) {
  try {
    await apiFetch(`/tactical-positions/${positionId}/sign-off`, { method: 'POST' });
    toast(t('Signed off'));
    await loadAssignments();
  } catch (e) { toast(e.message, 'error'); }
}

function renderBroadcasterSlotsTable(slots) {
  const rows = slots.map(s => {
    let action;
    if (s.is_mine) {
      action = `<button class="btn btn-ghost btn-sm" onclick="cancelBroadcasterSignup(${s.signup_id})" data-i18n="Cancel">${t('Cancel')}</button>`;
    } else if (s.signup_id) {
      action = `<span class="text-muted" style="font-size:11px">${esc(s.signup_callsign)}</span>`;
    } else {
      action = `<button class="btn btn-primary btn-sm" onclick="claimBroadcasterSlot(${s.schedule_id}, '${s.slot_date}', ${s.net_id})" data-i18n="Sign Up">${t('Sign Up')}</button>`;
    }
    return `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
      <span style="font-weight:700;font-size:13px">${esc(s.net_name)}</span>
      <span class="text-muted" style="font-size:12px">${esc(s.day_name)} ${esc(s.slot_date)}</span>
      <span style="margin-left:auto"></span>
      ${action}
    </div>`;
  }).join('');
  return `<div class="card">${rows}</div>`;
}

async function claimBroadcasterSlot(scheduleId, slotDate, netId) {
  try {
    await apiFetch(`/nets/${netId}/signups`, {
      method: 'POST',
      body: JSON.stringify({
        schedule_id: scheduleId, slot_date: slotDate, role: 'broadcaster',
        callsign: currentUser.callsign, name: currentUser.name, email: currentUser.email,
      }),
    });
    toast(t('Signed up'), 'success');
    await loadAssignments();
  } catch (e) { toast(e.message, 'error'); }
}

async function cancelBroadcasterSignup(signupId) {
  try {
    await apiFetch(`/signups/${signupId}`, { method: 'DELETE' });
    toast(t('Sign-up cancelled'));
    await loadAssignments();
  } catch (e) { toast(e.message, 'error'); }
}
