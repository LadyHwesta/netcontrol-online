// ============================================================
// SESSIONS
// ============================================================
async function openNet(netId) {
  closeSidebar();
  currentNetId = netId;
  currentSessionId = null;
  const net = nets.find(n => n.id === netId);
  currentNetOwnerId = net ? net.owner_id : null;
  currentNetIsAres = net ? !!net.is_ares : false;
  currentNetIsGmrs = net ? net.net_type === 'gmrs' : false;
  // Pre-fill default TG from net settings (ham only)
  const netDefaultTg = (!currentNetIsGmrs && net) ? (net.dmr_talkgroup || '') : '';
  document.getElementById('ci-dmr-tg').value = netDefaultTg;
  document.getElementById('session-net-name').textContent = net ? net.name : 'Net';

  // Show/hide ARES-specific UI elements (ham only)
  document.getElementById('ci-zone-group').style.display = currentNetIsAres ? '' : 'none';
  document.getElementById('zone-roster-panel').style.display = currentNetIsAres ? '' : 'none';
  document.getElementById('traffic-log-panel').style.display = currentNetIsAres ? '' : 'none';

  // Hide DMR/APRS panels entirely for GMRS nets — neither has a GMRS allocation
  document.getElementById('dmr-heard-panel').style.display = currentNetIsGmrs ? 'none' : '';
  document.getElementById('aprs-map-panel').style.display = 'none';  // shown by initAprsForSession once we know APRS is configured

  // Stash the raw script text; it's rendered (with variable substitution) per-session
  // in loadSessionLive, since {{net_control}}/{{broadcaster}} depend on that session's duty.
  currentNetScript = (net && net.script && net.script.trim()) ? net.script : null;

  // Update callsign input placeholder to match net type
  const ciCall = document.getElementById('ci-call');
  if (ciCall) ciCall.placeholder = currentNetIsGmrs ? 'WSMC512' : 'W1AW or suffix';

  // Load evac zones for this net
  evacZones = {};
  if (currentNetIsAres) {
    try {
      const zones = await apiFetch(`/nets/${netId}/evac-zones`);
      zones.forEach(z => { evacZones[z.callsign] = z.zone; });
      populateKnownZonesList();
    } catch {}
  }
  document.getElementById('nav-history').style.display = '';
  document.getElementById('live-session-panel').style.display = 'none';
  document.getElementById('sessions-list-container').style.display = '';
  document.getElementById('session-tab-bar').style.display = '';
  document.getElementById('back-to-sessions-btn').style.display = 'none';
  showView('session');
  switchSubTab('sessions');
  await loadSessions();
}

// Step back from a live session to this net's Sessions list, without ending
// the session or leaving the net entirely (issue #19).
function backToSessions() {
  stopClock();
  stopDmrPolling();
  stopAprsMapPolling();
  openNet(currentNetId);
}

function toggleNetScriptPanel() {
  const body = document.getElementById('net-script-body');
  const icon = document.getElementById('net-script-toggle-icon');
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  icon.textContent = open ? '▼' : '▶';
}

// ============================================================
// NET SCRIPT — {{variable}} substitution + basic markup rendering
// ============================================================
// Supported syntax (deliberately small — this is rendered via innerHTML, so every
// value that reaches the page must go through escapeHtml first):
//   **bold**   *italic*
//   # / ## / ###  headings
//   - item  or  * item   bullet list
//   ---  or  ===  (3+ chars, alone on a line)   horizontal rule
//   {{net_control}} {{net_control_callsign}} {{net_control_name}}
//   {{broadcaster}} {{broadcaster_callsign}} {{broadcaster_name}} {{broadcast_label}}
//   {{net_control_next}} {{net_control_next_callsign}} {{net_control_next_name}}
//   {{broadcaster_next}} {{broadcaster_next_callsign}} {{broadcaster_next_name}}
//   {{net_name}}

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function combineNameCallsign(callsign, name) {
  return callsign ? (name ? `${name} — ${callsign}` : callsign) : '';
}

function scriptVarsForSession(s) {
  const net = nets.find(n => n.id === currentNetId);
  return {
    net_name: net ? net.name : '',
    net_control: combineNameCallsign(s.ncs_callsign, s.ncs_name),
    net_control_callsign: s.ncs_callsign || '',
    net_control_name: s.ncs_name || '',
    broadcaster: combineNameCallsign(s.broadcaster_callsign, s.broadcaster_name),
    broadcaster_callsign: s.broadcaster_callsign || '',
    broadcaster_name: s.broadcaster_name || '',
    broadcast_label: s.broadcast_label || 'Broadcaster',
    net_control_next: combineNameCallsign(s.next_ncs_callsign, s.next_ncs_name),
    net_control_next_callsign: s.next_ncs_callsign || '',
    net_control_next_name: s.next_ncs_name || '',
    broadcaster_next: combineNameCallsign(s.next_broadcaster_callsign, s.next_broadcaster_name),
    broadcaster_next_callsign: s.next_broadcaster_callsign || '',
    broadcaster_next_name: s.next_broadcaster_name || '',
  };
}

// Bold/italic — applied to text that is already HTML-escaped.
function scriptInlineFormat(s) {
  return s
    .replace(/\*\*([^\n*]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^\n*]+?)\*/g, '<em>$1</em>');
}

const SCRIPT_HEADING_STYLES = [
  'font-size:16px;color:var(--lc-orange);font-weight:700;margin:10px 0 4px;letter-spacing:.03em',
  'font-size:14px;color:var(--lc-orange);font-weight:700;margin:8px 0 4px;letter-spacing:.03em',
  'font-size:13px;color:var(--lc-blue);font-weight:700;margin:6px 0 2px;letter-spacing:.03em',
];

// Headings / rules / bullet lists — line-based, applied to already-escaped text.
function scriptBlockFormat(escapedText) {
  const lines = escapedText.split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };

  for (const line of lines) {
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const bullet = line.match(/^[-*]\s+(.*)$/);
    const rule = /^(-{3,}|={3,})\s*$/.test(line);

    if (bullet) {
      if (!inList) { out.push('<ul style="margin:4px 0 4px 20px;padding:0">'); inList = true; }
      out.push(`<li style="margin:2px 0">${scriptInlineFormat(bullet[1])}</li>`);
      continue;
    }
    closeList();

    if (heading) {
      out.push(`<div style="${SCRIPT_HEADING_STYLES[heading[1].length - 1]}">${scriptInlineFormat(heading[2])}</div>`);
    } else if (rule) {
      out.push('<hr style="border:none;border-top:1px solid var(--lc-blue);margin:8px 0;opacity:.5">');
    } else {
      out.push(scriptInlineFormat(line));
    }
  }
  closeList();
  return out.join('\n');
}

function renderNetScript(session) {
  const panel = document.getElementById('net-script-panel');
  if (!currentNetScript) { panel.style.display = 'none'; return; }

  const vars = scriptVarsForSession(session);
  let text = escapeHtml(currentNetScript);
  // Substitute after escaping so {{...}} values (callsign/name) can't inject markup.
  text = text.replace(/\{\{\s*(\w+)\s*\}\}/g, (match, key) => (key in vars ? escapeHtml(vars[key]) : match));

  document.getElementById('net-script-text').innerHTML = scriptBlockFormat(text);
  document.getElementById('net-script-body').style.display = '';
  document.getElementById('net-script-toggle-icon').textContent = '▼';
  panel.style.display = '';
}

// For an activation session, Net Control is tracked as its own tactical position
// (auto-created at session start) and can hand off mid-activation the same way any
// other position does — so the duty bar and net script should reflect whoever
// currently holds it, not the day's schedule sign-up baked into the session at start
// (issue #21 follow-up). Falls back to the session's own ncs fields until tactical
// positions have loaded, and for non-activation/routine sessions, unchanged.
function effectiveSession(s) {
  if (!currentSessionIsActivation) return s;
  const ncPosition = tacticalPositions.find(p => p.is_net_control);
  if (!ncPosition) return s;
  return { ...s, ncs_callsign: ncPosition.current_callsign || null, ncs_name: ncPosition.current_name || null };
}

function renderDutyBar(s) {
  const bar = document.getElementById('duty-bar');
  const ncEl = document.getElementById('duty-nc');
  const bcEl = document.getElementById('duty-bc');
  if (s.ncs_callsign) {
    ncEl.querySelector('strong').textContent = s.ncs_name ? `${s.ncs_callsign} — ${s.ncs_name}` : s.ncs_callsign;
    ncEl.style.display = '';
  } else {
    ncEl.style.display = 'none';
  }
  if (s.broadcaster_callsign) {
    bcEl.querySelector('.duty-bc-label').textContent = s.broadcast_label || t('Broadcaster');
    bcEl.querySelector('strong').textContent = s.broadcaster_name ? `${s.broadcaster_callsign} — ${s.broadcaster_name}` : s.broadcaster_callsign;
    bcEl.style.display = '';
  } else {
    bcEl.style.display = 'none';
  }
  bar.style.display = (s.ncs_callsign || s.broadcaster_callsign) ? '' : 'none';
}

// Session-mode tabs (issue #21) — hides the currently-visible "Check-In"
// elements when switching to Station Schedule, remembering exactly which
// ones were showing so switching back restores that (rather than blindly
// re-showing everything, which would ignore each panel's own visibility
// rule — ended session, DMR config presence, etc.).
function switchSessionTab(tab) {
  const contentEls = document.querySelectorAll('.session-checkin-tab-content');
  if (tab === 'schedule') {
    contentEls.forEach(el => {
      if (el.style.display !== 'none') {
        el.dataset.wasVisible = '1';
        el.style.display = 'none';
      }
    });
    document.getElementById('station-schedule-panel').style.display = '';
    switchStationScheduleSubTab('tactical');
    setDefaultMonthDay('tac-pos-scheduled-month', 'tac-pos-scheduled-day');
    setDefaultMonthDay('nc-shift-month', 'nc-shift-day');
    loadTacticalPositions();
  } else {
    contentEls.forEach(el => {
      if (el.dataset.wasVisible) {
        el.style.display = '';
        delete el.dataset.wasVisible;
      }
    });
    document.getElementById('station-schedule-panel').style.display = 'none';
  }
  document.getElementById('session-tab-checkin-btn').classList.toggle('active', tab === 'checkin');
  document.getElementById('session-tab-schedule-btn').classList.toggle('active', tab === 'schedule');
}

// Station Schedule sub-tabs (issue #21 follow-up): Tactical Stations (one-off
// field positions) vs. Net Control (its own rotation schedule) — Net Control
// classically hands off on a fixed cadence throughout an activation, unlike a
// typical tactical station, so it gets a separate planning queue.
function switchStationScheduleSubTab(tab) {
  document.getElementById('tactical-stations-subpanel').style.display = tab === 'tactical' ? '' : 'none';
  document.getElementById('net-control-subpanel').style.display = tab === 'netcontrol' ? '' : 'none';
  document.getElementById('schedule-subtab-tactical-btn').classList.toggle('active', tab === 'tactical');
  document.getElementById('schedule-subtab-netcontrol-btn').classList.toggle('active', tab === 'netcontrol');
}

// Defaults a Scheduled Sign-On month/day pair to today (issue #21 follow-up) —
// used for both the tactical-position and Net Control shift "add" forms.
function setDefaultMonthDay(monthId, dayId) {
  const now = new Date();
  const monthEl = document.getElementById(monthId);
  const dayEl = document.getElementById(dayId);
  if (monthEl) monthEl.value = String(now.getMonth() + 1);
  if (dayEl) dayEl.value = String(now.getDate());
}

async function loadSessions() {
  let sessions = [];
  try { sessions = await apiFetch(`/nets/${currentNetId}/sessions`); } catch {}
  renderSessions(sessions);
}

function renderSessions(sessions) {
  const el = document.getElementById('sessions-list-container');
  if (sessions.length === 0) {
    el.innerHTML = '<div class="empty"><p>No sessions yet. Start one above.</p></div>';
    return;
  }
  el.innerHTML = `<table class="tbl"><thead><tr>
    <th>Session</th><th>Started</th><th class="col-sess-ended">Ended</th><th class="col-sess-checkins">Check-ins</th><th>Status</th><th></th>
  </tr></thead><tbody>` +
  sessions.map(s => {
    const label = s.name ? esc(s.name) : `Session ${s.id}`;
    return `<tr>
      <td>
        <a href="#" onclick="loadSessionLive(${s.id}); return false;" style="color:var(--accent-hover);text-decoration:none">${label}</a>
        <button class="btn btn-ghost btn-sm" style="margin-left:6px;font-size:11px;padding:1px 7px" onclick="promptRenameSession(${s.id}, ${JSON.stringify(s.name || '')})">✏️</button>
      </td>
      <td>${fmt(s.started_at)}</td>
      <td class="col-sess-ended">${fmt(s.ended_at)}</td>
      <td class="col-sess-checkins"><span class="badge badge-blue">${s.checkin_count}</span></td>
      <td>${s.ended_at ? '<span class="badge badge-gray">Ended</span>' : '<span class="badge badge-green">Live</span>'}</td>
      <td><button class="btn btn-danger btn-sm" onclick="deleteSession(${s.id})">${t('Delete')}</button></td>
    </tr>`;
  }).join('') + '</tbody></table>';
}

function toggleStartSessionForm() {
  const f = document.getElementById('start-session-form');
  f.style.display = f.style.display === 'none' ? '' : 'none';
  if (f.style.display !== 'none') {
    document.getElementById('log-offline-form').style.display = 'none';
    document.getElementById('new-session-name').focus();
    const net = nets.find(n => n.id === currentNetId);
    const bcGroup = document.getElementById('start-session-broadcaster-group');
    bcGroup.style.display = (net && net.has_broadcast) ? '' : 'none';
    if (net && net.has_broadcast && net.broadcast_label) {
      document.getElementById('start-session-broadcaster-label').innerHTML =
        `${esc(net.broadcast_label)} ${t('Override')} <span class="text-muted" style="font-size:11px">${t('(optional — overrides today\'s sign-up)')}</span>`;
    }
    // ARES/ACES activation checkbox — only offered for ARES-enabled nets (issue #21)
    document.getElementById('start-session-activation-group').style.display = (net && net.is_ares) ? '' : 'none';
    document.getElementById('new-session-is-activation').checked = false;
  }
}

async function startSession() {
  const name = document.getElementById('new-session-name').value.trim() || null;
  const broadcaster_override_callsign = document.getElementById('new-session-broadcaster-callsign').value.trim().toUpperCase() || null;
  const broadcaster_override_name = document.getElementById('new-session-broadcaster-name').value.trim() || null;
  const is_activation = document.getElementById('new-session-is-activation').checked;
  try {
    const s = await apiFetch(`/nets/${currentNetId}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ name, broadcaster_override_callsign, broadcaster_override_name, is_activation }),
    });
    document.getElementById('new-session-name').value = '';
    document.getElementById('new-session-broadcaster-callsign').value = '';
    document.getElementById('new-session-broadcaster-name').value = '';
    document.getElementById('new-session-is-activation').checked = false;
    document.getElementById('start-session-form').style.display = 'none';
    toast(t('Session started'));
    await loadSessions();
    await loadSessionLive(s.id);
  } catch (e) { toast(e.message, 'error'); }
}

// Log a net that already happened (issue #20) — no access to the web tool at
// the time, backfilled afterward. Mirrors toggleStartSessionForm()/startSession()
// but collects a required date/time instead of assuming "now", plus a direct
// Net Control override (whoever backfills the log usually isn't who ran it).
function toggleLogOfflineForm() {
  const f = document.getElementById('log-offline-form');
  f.style.display = f.style.display === 'none' ? '' : 'none';
  if (f.style.display !== 'none') {
    document.getElementById('start-session-form').style.display = 'none';
    document.getElementById('log-offline-name').focus();
    setDefaultMonthDay('log-offline-month', 'log-offline-day');
    document.getElementById('log-offline-time').value = '';
    const net = nets.find(n => n.id === currentNetId);
    const bcGroup = document.getElementById('log-offline-broadcaster-group');
    bcGroup.style.display = (net && net.has_broadcast) ? '' : 'none';
    if (net && net.has_broadcast && net.broadcast_label) {
      document.getElementById('log-offline-broadcaster-label').innerHTML =
        `${esc(net.broadcast_label)} <span class="text-muted" style="font-size:11px">${t('(optional)')}</span>`;
    }
  }
}

async function logOfflineNet() {
  const name = document.getElementById('log-offline-name').value.trim() || null;
  const month = document.getElementById('log-offline-month').value;
  const day = document.getElementById('log-offline-day').value;
  const time = document.getElementById('log-offline-time').value;
  if (!month || !day || !time) return toast(t('Date and time of the net are required'), 'error');
  const year = new Date().getFullYear();
  const occurred_at = new Date(`${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}T${time}`).toISOString();
  const ncs_override_callsign = document.getElementById('log-offline-ncs-callsign').value.trim().toUpperCase() || null;
  const ncs_override_name = document.getElementById('log-offline-ncs-name').value.trim() || null;
  const broadcaster_override_callsign = document.getElementById('log-offline-broadcaster-callsign').value.trim().toUpperCase() || null;
  const broadcaster_override_name = document.getElementById('log-offline-broadcaster-name').value.trim() || null;
  try {
    const s = await apiFetch(`/nets/${currentNetId}/sessions`, {
      method: 'POST',
      body: JSON.stringify({
        name, is_offline: true, occurred_at, ncs_override_callsign, ncs_override_name,
        broadcaster_override_callsign, broadcaster_override_name,
      }),
    });
    document.getElementById('log-offline-name').value = '';
    document.getElementById('log-offline-ncs-callsign').value = '';
    document.getElementById('log-offline-ncs-name').value = '';
    document.getElementById('log-offline-broadcaster-callsign').value = '';
    document.getElementById('log-offline-broadcaster-name').value = '';
    document.getElementById('log-offline-form').style.display = 'none';
    toast(t('Net logged'));
    await loadSessions();
    await loadSessionLive(s.id);
  } catch (e) { toast(e.message, 'error'); }
}

async function promptRenameSession(id, currentName) {
  const name = prompt('Session name:', currentName);
  if (name === null) return; // cancelled
  try {
    await apiFetch(`/sessions/${id}/rename`, { method: 'PATCH', body: JSON.stringify({ name: name.trim() || null }) });
    toast(t('Session renamed'));
    await loadSessions();
    if (currentSessionId === id) await loadSessionLive(id);
  } catch (e) { toast(e.message, 'error'); }
}

async function loadSessionLive(sessionId) {
  currentSessionId = sessionId;
  // Reset expected stations list for fresh session
  expectedStations = [];
  lastKnownCheckins = [];
  pendingTrafficCallsigns.clear();
  document.getElementById('expected-list').innerHTML = '<p class="text-muted" style="font-size:12px;margin:0">Set filter and click Load List to see previously active stations.</p>';
  document.getElementById('expected-panel-body').style.display = 'none';
  document.getElementById('expected-toggle-icon').textContent = '▶';
  document.getElementById('traffic-banner').style.display = 'none';
  try {
    const s = await apiFetch(`/sessions/${sessionId}`);
    const ended = !!s.ended_at;
    const offline = !!s.is_offline;
    // ended_at is set from creation for an offline entry (it's never live), so
    // it can't double as "done entering data" the way it does for a normal
    // session — is_offline_locked is that separate signal (issue #20 follow-up).
    const offlineLocked = offline && !!s.is_offline_locked;
    document.getElementById('live-session-panel').style.display = '';
    // Hide the Sessions/Schedule tabs and the sessions toolbar while a session is live
    // to cut clutter; restore them once it ends (helpful when reviewing a closed session).
    document.getElementById('session-tab-bar').style.display = ended ? '' : 'none';
    document.getElementById('sessions-panel').style.display = ended ? '' : 'none';
    document.getElementById('back-to-sessions-btn').style.display = ended ? 'none' : '';
    document.getElementById('schedule-panel').style.display = 'none';
    if (ended) {
      document.getElementById('sub-tab-sessions').classList.add('active');
      document.getElementById('sub-tab-schedule').classList.remove('active');
    } else {
      collapseSidebar();
    }
    document.getElementById('session-status-dot').className = 'status-dot' + (ended ? ' ended' : '');
    const sessionLabel = s.name ? s.name : `Session ${sessionId}`;
    // An offline entry (issue #20) is never "live" and never really "ends" —
    // it's a single logged point in time, not a duration — so its status/meta
    // text avoids implying either.
    document.getElementById('session-status-label').textContent = offline
      ? `${sessionLabel} — Logged${offlineLocked ? ' (Closed)' : ''}`
      : `${sessionLabel} — ${ended ? 'Ended' : 'Live'}`;
    document.getElementById('session-meta').textContent = offline
      ? `Logged for ${fmt(s.started_at)}`
      : `Started ${fmt(s.started_at)}${ended ? ' · Ended ' + fmt(s.ended_at) : ''}`;
    // Offline entries are always already "ended" (no live view) but stay open to
    // new check-ins until explicitly closed — the same button/endpoint as Ending
    // a normal session, just relabeled and checked against a different flag.
    const endBtn = document.getElementById('end-session-btn');
    endBtn.textContent = offline ? '🔒 Close Log' : '■ End Session';
    endBtn.style.display = offline ? (offlineLocked ? 'none' : '') : (ended ? 'none' : '');
    document.getElementById('checkin-form-area').style.display = (offline ? offlineLocked : ended) ? 'none' : '';
    // ARES/ACES activation session-mode tabs (issue #21) — only while live; reset to
    // the Check-In tab on every (re)load without touching the other panels' own
    // visibility rules (DMR config presence, is_ares, etc.), which are already
    // correctly applied by the code above/below this point.
    currentSessionIsActivation = !!s.is_activation;
    document.getElementById('session-mode-tab-bar').style.display = (s.is_activation && !ended) ? '' : 'none';
    document.getElementById('station-schedule-panel').style.display = 'none';
    document.getElementById('session-tab-checkin-btn').classList.add('active');
    document.getElementById('session-tab-schedule-btn').classList.remove('active');
    currentSessionData = s;
    // Load tactical positions up front (not just when the Schedule tab/Tactical
    // Assignments panel is opened) so the duty bar/net script show the live Net
    // Control handoff state from the very first paint (issue #21 follow-up).
    if (currentSessionIsActivation) await loadTacticalPositions(); else tacticalPositions = [];
    renderDutyBar(effectiveSession(s));
    renderNetScript(effectiveSession(s));
    // An offline entry (issue #20) has no live view to speak of — Expected
    // Stations (historical-attendance list) doesn't apply to backfilling a net
    // that already happened, unlike a normal ended session where it stays
    // visible for later review. Set explicitly either way (not just when
    // offline) since this is a single-page app -- without an explicit reset,
    // an earlier offline session's hidden state would otherwise leak into the
    // next (non-offline) session viewed in the same page load. Net Script
    // needs no such reset: renderNetScript() above already redecides its
    // visibility from scratch on every call.
    document.getElementById('expected-stations-wrapper').style.display = offline ? 'none' : '';
    if (offline) document.getElementById('net-script-panel').style.display = 'none';
    if (!ended) startClock(s.started_at); else stopClock();
    trafficMessages = [];
    renderTrafficMessages();
    await loadCheckins();
    if (!ended) await refreshOfflineQueueBanner();
    await loadTrafficMessages();
    // DMR: init or stop polling based on session state
    if (!ended) {
      await initDmrForSession(currentNetId);
      await initAprsForSession(currentNetId);
    } else {
      stopDmrPolling();
      document.getElementById('dmr-heard-panel').style.display = 'none';
      document.getElementById('ci-dmr-tg-group').style.display = 'none';
      document.getElementById('ci-dmr-region-group').style.display = 'none';
      stopAprsMapPolling();
      document.getElementById('aprs-map-panel').style.display = 'none';
    }
  } catch (e) { toast(e.message, 'error'); }
}

async function endSession() {
  // Same endpoint closes both a normal live session and an offline entry
  // (issue #20 follow-up) — the backend checks is_offline_locked instead of
  // ended_at for the latter, since ended_at is already set from creation.
  const offline = !!(currentSessionData && currentSessionData.is_offline);
  const confirmMsg = offline
    ? 'Close this log? No more check-ins can be added once closed.'
    : 'End this session? No more check-ins can be added after ending.';
  if (!confirm(confirmMsg)) return;
  try {
    const sid = currentSessionId;
    await apiFetch(`/sessions/${sid}/end`, { method: 'PATCH', body: '{}' });
    toast(offline ? 'Log closed' : 'Session ended');
    stopClock();
    stopDmrPolling();
    stopAprsMapPolling();
    currentSessionId = null;
    document.getElementById('live-session-panel').style.display = 'none';
    document.getElementById('sessions-list-container').style.display = '';
    document.getElementById('session-tab-bar').style.display = '';
    document.getElementById('sessions-panel').style.display = '';
    await loadSessions();
    // Show session summary
    try {
      const summary = await apiFetch(`/sessions/${sid}/summary`);
      showSessionSummary(summary);
    } catch {}
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteSession(id) {
  if (!confirm(t('Delete this session and all its check-ins?'))) return;
  try {
    await apiFetch(`/sessions/${id}`, { method: 'DELETE' });
    toast(t('Session deleted'));
    if (currentSessionId === id) {
      document.getElementById('live-session-panel').style.display = 'none';
      document.getElementById('sessions-list-container').style.display = '';
      currentSessionId = null;
    }
    await loadSessions();
  } catch (e) { toast(e.message, 'error'); }
}


// CSV EXPORT
// ============================================================
function triggerDownload(url) {
  fetch(url, { headers: { Authorization: 'Bearer ' + token } })
    .then(res => {
      const cd = res.headers.get('content-disposition') || '';
      const match = cd.match(/filename="?([^"]+)"?/);
      const filename = match ? match[1] : 'export.csv';
      return res.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
    })
    .catch(e => toast(t('Export failed:') + ' ' + e.message, 'error'));
}


// SESSION CLOCK & TIMER
// ============================================================
let sessionStartedAt = null;   // Date object, set when session loads
let clockInterval = null;

function startClock(startedAt) {
  sessionStartedAt = startedAt ? new Date(startedAt) : null;
  const bar = document.getElementById('session-clock-bar');
  bar.style.display = 'flex';
  if (clockInterval) clearInterval(clockInterval);
  clockInterval = setInterval(tickClock, 1000);
  tickClock();
}

function stopClock() {
  if (clockInterval) { clearInterval(clockInterval); clockInterval = null; }
  const bar = document.getElementById('session-clock-bar');
  if (bar) bar.style.display = 'none';
}

function tickClock() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  document.getElementById('clk-local').textContent =
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  document.getElementById('clk-utc').textContent =
    `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())}`;
  if (sessionStartedAt) {
    const elapsed = Math.floor((now - sessionStartedAt) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    document.getElementById('clk-elapsed').textContent =
      h > 0 ? `${h}h ${pad(m)}m ${pad(s)}s` : `${pad(m)}m ${pad(s)}s`;
  }
}


// ============================================================
// SESSION SUMMARY MODAL
// ============================================================
function showSessionSummary(summary) {
  const existing = document.getElementById('session-summary-modal');
  if (existing) existing.remove();

  const dur = summary.duration_minutes != null
    ? `${Math.floor(summary.duration_minutes / 60)}h ${summary.duration_minutes % 60}m`
    : '—';

  const modal = document.createElement('div');
  modal.id = 'session-summary-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;display:flex;align-items:center;justify-content:center;padding:20px';
  modal.innerHTML = `
    <div class="session-summary-card">
      <h2 style="margin:0 0 4px;color:var(--lc-orange);font-size:18px">Session Complete</h2>
      <p style="margin:0 0 20px;color:var(--text-muted);font-size:13px">${esc(summary.net_name)}</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
        <div style="background:var(--bg);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:var(--lc-blue)">${summary.total_checkins}</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Total Check-Ins</div>
        </div>
        <div style="background:var(--bg);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:var(--lc-orange)">${dur}</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Duration</div>
        </div>
        <div style="background:var(--bg);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:var(--lc-red)">${summary.traffic_count}</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:2px">With Traffic</div>
        </div>
        <div style="background:var(--bg);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:28px;font-weight:700;color:var(--success)">${summary.new_stations}</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:2px">New Stations</div>
        </div>
      </div>
      ${summary.net_frequency ? `<p style="font-size:12px;color:var(--text-muted);margin:0 0 16px">Frequency: ${esc(summary.net_frequency)}</p>` : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="openICS205(${summary.session_id})">📄 ICS-205 / Net Log</button>
        <button class="btn btn-ghost" onclick="exportSessionById(${summary.session_id})">⬇ CSV Export</button>
        <button class="btn btn-ghost" onclick="document.getElementById('session-summary-modal').remove()">Close</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

async function openICS205(sessionId) {
  try {
    const res = await fetch(`/sessions/${sessionId}/ics205`, {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!res.ok) throw new Error('Failed to load net log (' + res.status + ')');
    const html = await res.text();
    const blob = new Blob([html], { type: 'text/html' });
    window.open(URL.createObjectURL(blob), '_blank');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function exportSessionById(sessionId) {
  apiFetch(`/sessions/${sessionId}/export`)
    .then(data => {
      const blob = new Blob([data], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `session_${sessionId}.csv`; a.click();
    })
    .catch(e => toast(t('Export failed:') + ' ' + e.message, 'error'));
}
