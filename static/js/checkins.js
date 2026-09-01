// ============================================================
// CSV IMPORT (issue #26) — bulk check-ins, mainly for "Log a Net That
// Already Happened" (issue #20) where re-typing a whole paper roster one
// row at a time is tedious.
// ============================================================
function openCheckinImportModal() {
  if (!currentSessionId) return;
  document.getElementById('checkin-import-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'checkin-import-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;display:flex;align-items:center;justify-content:center;padding:20px';
  modal.innerHTML = `
    <div style="background:var(--surface);border:2px solid var(--lc-blue);border-radius:10px;padding:24px;max-width:480px;width:100%">
      <h3 style="margin:0 0 8px;color:var(--lc-blue)">📥 ${t('Import Check-ins from CSV')}</h3>
      <p style="margin:0 0 14px;font-size:13px;color:var(--text-muted);line-height:1.5">
        ${t('Upload a CSV of check-ins — handy for a long roster from a net logged after the fact.')}
        ${t('Only')} <strong>${t('Callsign')}</strong> ${t('is required; every other column is optional.')}
      </p>
      <p style="margin:0 0 14px">
        <a href="#" onclick="triggerDownload(API + '/checkins/import-sample'); return false;" style="color:var(--lc-orange);font-size:13px">📄 ${t('Download a sample CSV')}</a>
      </p>
      <div class="form-group" style="margin-bottom:14px">
        <input type="file" id="checkin-import-file" accept=".csv,text/csv" class="form-control" />
      </div>
      <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;font-size:12px;color:var(--text-muted);margin-bottom:14px">
        <input type="checkbox" id="checkin-import-lookup-names" style="accent-color:var(--lc-blue);width:15px;height:15px;margin-top:1px" />
        <span>${t('Look up names left blank in the CSV')} — <span style="color:var(--text)">${t("this net's own check-in history first, then an FCC/GMRS lookup")}</span>. ${t('Slower for a long roster of callsigns the lookup has never cached before.')}</span>
      </label>
      <div id="checkin-import-result"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="btn btn-ghost" onclick="document.getElementById('checkin-import-modal').remove()">${t('Close')}</button>
        <button class="btn btn-primary" onclick="submitCheckinImport(this)">${t('Upload')}</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

async function submitCheckinImport(btn) {
  const fileInput = document.getElementById('checkin-import-file');
  const file = fileInput.files[0];
  if (!file) return toast(t('Choose a CSV file first'), 'error');
  const lookupNames = document.getElementById('checkin-import-lookup-names').checked;
  btnLoading(btn, true);
  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('lookup_missing_names', lookupNames);
    const res = await fetch(`${API}/sessions/${currentSessionId}/checkins/import`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: fd,
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || t('Import failed'));
    renderCheckinImportResult(result);
    let msg = tn(result.imported, 'Imported {n} check-in', 'Imported {n} check-ins');
    if (result.names_looked_up) msg += `, ${tn(result.names_looked_up, '{n} name looked up', '{n} names looked up')}`;
    if (result.skipped) msg += `, ${t('{n} skipped').replace('{n}', result.skipped)}`;
    toast(msg, result.skipped ? 'error' : 'success');
    await loadCheckins();
    renderExpectedList();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btnLoading(btn, false);
  }
}

function renderCheckinImportResult(result) {
  const el = document.getElementById('checkin-import-result');
  if (!el) return;
  let html = `<div style="font-size:13px"><strong style="color:var(--lc-green)">${t('{n} imported').replace('{n}', result.imported)}</strong>`;
  if (result.names_looked_up) html += `, ${tn(result.names_looked_up, '{n} name looked up', '{n} names looked up')}`;
  if (result.skipped > 0) {
    html += `, <strong style="color:var(--lc-red)">${t('{n} skipped').replace('{n}', result.skipped)}</strong></div>`;
    html += `<div style="max-height:160px;overflow-y:auto;margin-top:8px;font-size:12px;border:1px solid var(--lc-border);border-radius:6px;padding:8px">`;
    html += result.errors.map(e => `<div style="padding:2px 0">${t('Row')} ${e.row}${e.callsign ? ` (${esc(e.callsign)})` : ''}: ${esc(e.reason)}</div>`).join('');
    html += `</div>`;
  } else {
    html += `</div>`;
  }
  el.innerHTML = html;
}

// ============================================================
// CALLSIGN LOOKUP
// ============================================================
const lookupCache = {};  // callsign → result, avoids repeat API calls per session
let lastLookedUpCallsign = null;  // guards against a redundant re-lookup wiping an open remark editor

// Bumped by every lookupCallsign()/searchCallsigns() call and by
// clearLookupInfo() -- each in-flight request captures the value at the
// moment it started, and checks it again before touching the DOM once the
// network round-trip completes. Without this, hitting Enter (submitting
// and clearing the form) or typing the next callsign before a slow FCC
// lookup finishes let that stale response land in the now-empty Name
// field for whatever callsign came after it (issue: name field getting
// populated from the previous check-in's lookup).
let lookupGeneration = 0;

async function lookupCallsign(callsign) {
  if (!callsign || callsign.length < 3) { clearLookupInfo(); return; }
  lastLookedUpCallsign = callsign;
  const generation = ++lookupGeneration;
  if (lookupCache[callsign]) { applyLookupResult(lookupCache[callsign]); return; }

  setLookupInfo(`<span class="lookup-spinner"></span><span class="text-muted" style="font-size:11px">${t('Looking up…')}</span>`);
  try {
    const result = await apiFetch(`/callsign/${encodeURIComponent(callsign)}/lookup`);
    if (generation !== lookupGeneration) return;  // superseded by a newer lookup/clear
    lookupCache[callsign] = result;
    applyLookupResult(result);
  } catch {
    if (generation !== lookupGeneration) return;
    clearLookupInfo();
  }
}

function remarkPillText(data) {
  const parts = [];
  if (data && data.preferred_name) parts.push('👤 ' + data.preferred_name);
  if (data && data.remark) parts.push('📝 ' + data.remark);
  return parts.length ? parts.join('   ') : t('+ Name/Remark');
}

function renderRemarkPill(callsign, data) {
  const info = document.getElementById('ci-lookup-info');
  if (!info) return;
  const existing = document.getElementById('remark-pill');
  if (existing) existing.remove();
  const pill = document.createElement('span');
  pill.id = 'remark-pill';
  pill.style.cssText = 'display:inline-flex;align-items:center;gap:5px;margin-left:6px;font-size:11px;cursor:pointer;background:rgba(255,204,0,0.15);border:1px solid rgba(255,204,0,0.4);border-radius:4px;padding:1px 7px;color:var(--lc-orange)';
  pill.title = t('Click to set a preferred name and/or remark for this station');
  pill.innerHTML = `<span>${esc(remarkPillText(data))}</span>`;
  pill.onclick = () => showRemarkEditor(callsign, data);
  info.appendChild(pill);
}

function applyLookupResult(result) {
  if (result.status !== 'found') {
    const notFoundMsg = currentNetIsGmrs
      ? t('Not found in GMRS database')
      : t('Not found in FCC database');
    setLookupInfo(`<span class="lookup-notfound">${notFoundMsg}</span>`);
    // Preferred name/remark isn't tied to a successful FCC/GMRS lookup — always
    // offer the pill so a station missing from the database can still get one.
    const cs = document.getElementById('ci-call').value.trim().toUpperCase();
    if (cs) loadStationRemarks(cs).then(data => renderRemarkPill(cs, data));
    return;
  }

  // Auto-fill name if empty (from the FCC/GMRS lookup — preferred name doesn't
  // override this; it only overrides Expected Stations and reports)
  const nameEl = document.getElementById('ci-name');
  const noteEl = document.getElementById('ci-name-autofill-note');
  if (result.name && !nameEl.value.trim()) {
    nameEl.value = result.name;
    noteEl.style.display = '';
  }

  // Build info pills
  const parts = [];
  if (result.name) parts.push(`<span class="lookup-name">${esc(result.name)}</span>`);
  if (result.license_class) parts.push(`<span class="lookup-pill lookup-pill-class">${esc(result.license_class)}</span>`);
  if (result.state)         parts.push(`<span class="lookup-pill lookup-pill-state">${esc(result.state)}</span>`);
  if (result.grid)          parts.push(`<span class="lookup-pill lookup-pill-grid">${esc(result.grid)}</span>`);
  setLookupInfo(parts.join(' '));
  // Load and display station remark / preferred name
  const callsign = result.callsign || document.getElementById('ci-call').value.trim().toUpperCase();
  loadStationRemarks(callsign).then(data => renderRemarkPill(callsign, data));
}

function setLookupInfo(html) {
  document.getElementById('ci-lookup-info').innerHTML = html;
}

function clearLookupInfo() {
  document.getElementById('ci-lookup-info').innerHTML = '';
  document.getElementById('ci-name-autofill-note').style.display = 'none';
  lastLookedUpCallsign = null;
  lookupGeneration++;  // invalidate any still-in-flight lookup (e.g. form just got cleared/submitted)
}

// ============================================================
// CHECKINS
// ============================================================
async function addCheckin() {
  const callsign = document.getElementById('ci-call').value.trim().toUpperCase();
  const name = document.getElementById('ci-name').value.trim() || null;
  const signal_report = document.getElementById('ci-sig').value.trim() || null;
  const comments = document.getElementById('ci-comments').value.trim() || null;
  const has_traffic = document.getElementById('ci-traffic').checked;
  const evac_zone = currentNetIsAres ? (document.getElementById('ci-zone').value.trim() || null) : null;
  const currentNet = nets.find(n => n.id === currentNetId);
  const dmr_talkgroup = document.getElementById('ci-dmr-tg').value.trim()
    || (currentNet && currentNet.dmr_talkgroup)
    || null;
  const dmr_region = document.getElementById('ci-dmr-region').value.trim() || null;
  if (!callsign) { toast(t('Callsign required'), 'error'); document.getElementById('ci-call').focus(); return; }
  const payload = { callsign, name, signal_report, comments, has_traffic, evac_zone, dmr_talkgroup, dmr_region };
  try {
    const created = await apiFetch(`/sessions/${currentSessionId}/checkins`, { method: 'POST', body: JSON.stringify(payload) });
    _clearCheckinForm();
    toast(`${callsign} ${t('checked in')}`, 'success');
    markRecentCheckin(created.id);
    await loadCheckins();
    renderExpectedList();   // refresh expected list to show new checkin state
  } catch (e) {
    if (e instanceof TypeError) {
      // fetch couldn't reach the server at all — offline. Queue it rather
      // than lose the check-in; static/js/offline-queue.js replays it
      // automatically once back online (see the online listener below).
      await queueCheckin(currentSessionId, payload, token);
      _registerBackgroundSync();
      _clearCheckinForm();
      toast(`${callsign} ${t('queued — offline, will send automatically')}`, 'success');
      await refreshOfflineQueueBanner();
    } else {
      toast(e.message, 'error');
    }
  }
}

function _clearCheckinForm() {
  document.getElementById('ci-call').value = '';
  document.getElementById('ci-name').value = '';
  document.getElementById('ci-sig').value = '';
  document.getElementById('ci-comments').value = '';
  document.getElementById('ci-traffic').checked = false;
  if (currentNetIsAres) document.getElementById('ci-zone').value = '';
  // Keep TG populated (usually same for whole session), clear region
  document.getElementById('ci-dmr-region').value = '';
  clearLookupInfo();
  document.getElementById('ci-call').focus();
}

// ============================================================
// OFFLINE CHECK-IN QUEUE — banner + retry wiring (issue #9)
// ============================================================
async function _registerBackgroundSync() {
  // Best-effort extra layer, not the primary guarantee — see static/sw.js
  // header comment. Silently skipped on browsers without Background Sync
  // (notably Safari/iOS); the online-listener below covers everyone.
  if (!('serviceWorker' in navigator) || !('SyncManager' in window)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    await reg.sync.register('sync-checkins');
  } catch { /* unsupported or registration failed — foreground retry still works */ }
}

async function refreshOfflineQueueBanner() {
  const banner = document.getElementById('offline-queue-banner');
  if (!banner || !currentSessionId) return;
  const pending = await getPendingCheckins(currentSessionId);
  if (pending.length === 0) { banner.style.display = 'none'; return; }
  banner.style.display = '';
  const failedCount = pending.filter(p => p.status === 'failed').length;
  const waitingCount = pending.length - failedCount;
  document.getElementById('offline-queue-summary').textContent =
    (waitingCount ? `⏳ ${tn(waitingCount, '{n} check-in waiting to sync', '{n} check-ins waiting to sync')}` : '')
    + (failedCount ? `${waitingCount ? ' · ' : ''}⚠ ${t('{n} failed').replace('{n}', failedCount)}` : '');
  document.getElementById('offline-queue-list').innerHTML = pending.map(p => {
    if (p.status === 'failed') {
      return `<div style="display:flex;align-items:center;gap:8px;margin-top:3px">
        <span class="callsign" style="color:var(--lc-red)">${esc(p.payload.callsign)}</span>
        <span class="text-muted" style="font-size:11px">${esc(p.last_error || t('failed'))}</span>
        <button class="btn btn-ghost btn-sm" onclick="dismissFailedCheckin('${p.id}')" style="margin-left:auto">${t('Dismiss')}</button>
      </div>`;
    }
    return `<div style="display:flex;align-items:center;gap:8px;margin-top:3px">
      <span class="callsign">${esc(p.payload.callsign)}</span>
      <span class="text-muted" style="font-size:11px">${t('queued')} ${fmt(p.queued_at)}</span>
    </div>`;
  }).join('');
}

async function retryOfflineQueue() {
  const changed = await flushCheckinQueue();
  await refreshOfflineQueueBanner();
  if (changed) {
    const beforeIds = new Set(lastKnownCheckins.map(c => c.id));
    await loadCheckins();
    // Newly-appeared rows just landed via the offline queue — highlight them
    // like a fresh check-in so a late sync doesn't look like old news.
    lastKnownCheckins.filter(c => !beforeIds.has(c.id)).forEach(c => markRecentCheckin(c.id));
    renderExpectedList();
  }
}

async function dismissFailedCheckin(id) {
  await removeFailedCheckin(id);
  await refreshOfflineQueueBanner();
}

// Foreground retry — the guarantee that works on every browser including
// iOS Safari, unlike the Background Sync API used in static/sw.js.
window.addEventListener('online', retryOfflineQueue);
setInterval(() => {
  const panel = document.getElementById('live-session-panel');
  if (panel && panel.style.display !== 'none') retryOfflineQueue();
}, 15000);

// ── Callsign search helpers ──────────────────────────────────
// US amateur pattern: 1-2 prefix letters + digit + 1-3 suffix letters  (e.g. W1AW, KD9XYZ)
const HAM_CS_RE  = /^[A-Z]{1,2}[0-9][A-Z]{1,3}$/;
// GMRS pattern: letter + 2-3 letters + 4 digits  (e.g. WQXH7777, KAB1234)
const GMRS_CS_RE = /^[A-Z]{3,4}\d{3,4}$/;

function isLikelyFullCallsign(val) {
  if (val.length < 4) return false;
  return HAM_CS_RE.test(val) || GMRS_CS_RE.test(val);
}

function clearCallsignDropdown() {
  const dd = document.getElementById('cs-dropdown');
  dd.style.display = 'none';
  dd.innerHTML = '';
}

function showCallsignDropdown(results) {
  const dd = document.getElementById('cs-dropdown');
  dd.innerHTML = results.map(r =>
    `<div onmousedown="selectCallsign('${esc(r.callsign)}')"
          style="padding:7px 12px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px"
          onmouseover="this.style.background='#1a1a1a'" onmouseout="this.style.background=''">
       <span class="callsign" style="min-width:90px;font-size:13px">${esc(r.callsign)}</span>
       <span style="flex:1;font-size:12px;color:var(--text)">${esc(r.name || '')}</span>
       <span style="font-size:11px;color:var(--lc-blue)">${esc(r.license_class || '')}</span>
     </div>`
  ).join('');
  dd.style.display = results.length ? '' : 'none';
}

function selectCallsign(callsign) {
  document.getElementById('ci-call').value = callsign;
  clearCallsignDropdown();
  clearLookupInfo();
  lookupCallsign(callsign);
}

async function searchCallsigns(q) {
  const generation = ++lookupGeneration;
  setLookupInfo(`<span class="lookup-spinner"></span><span class="text-muted" style="font-size:11px">${t('Searching…')}</span>`);
  try {
    const netParam = currentNetId ? `&net_id=${currentNetId}` : '';
    const results = await apiFetch(`/callsign/search?q=${encodeURIComponent(q)}${netParam}`);
    if (generation !== lookupGeneration) return;  // superseded by a newer lookup/search/clear
    clearLookupInfo();
    if (results.length === 0) {
      setLookupInfo(`<span class="lookup-notfound">${t('No matches found')}</span>`);
    } else if (results.length === 1 && results[0].callsign === q) {
      // Exact single match — auto-select
      selectCallsign(results[0].callsign);
    } else {
      showCallsignDropdown(results);
    }
  } catch (err) {
    if (generation !== lookupGeneration) return;
    setLookupInfo(`<span class="lookup-notfound">${t('Search error:')} ${esc(err.message)}</span>`);
  }
}

// Lookup on blur (tab away) or after a short pause while typing
let lookupTimer = null;
document.getElementById('ci-call').addEventListener('blur', e => {
  // Small delay so onmousedown on a dropdown item fires first
  setTimeout(() => {
    const cs = e.target.value.trim().toUpperCase();
    clearCallsignDropdown();
    // Skip re-looking-up a callsign already displayed — this field loses focus
    // whenever the remark pill/editor is clicked, and a redundant lookup here
    // would wipe out the just-opened editor via setLookupInfo()'s innerHTML reset.
    if (cs && isLikelyFullCallsign(cs) && cs !== lastLookedUpCallsign) lookupCallsign(cs);
  }, 150);
});
document.getElementById('ci-call').addEventListener('input', e => {
  clearLookupInfo();
  clearCallsignDropdown();
  clearTimeout(lookupTimer);
  const cs = e.target.value.trim().toUpperCase();
  if (cs.length < 2) return;
  lookupTimer = setTimeout(() => {
    if (isLikelyFullCallsign(cs)) {
      lookupCallsign(cs);
    } else {
      searchCallsigns(cs);
    }
  }, 600);
});

// Submit checkin on Enter in callsign field
document.getElementById('ci-call').addEventListener('keydown', e => {
  if (e.key === 'Escape') { clearCallsignDropdown(); return; }
  if (e.key === 'Enter') addCheckin();
});
onEnter(['ci-name', 'ci-sig', 'ci-comments', 'ci-zone', 'ci-dmr-tg', 'ci-dmr-region'], addCheckin);
onEnter(['tm-dest', 'tm-notes'], addTrafficMessage);

// Close dropdown on click outside
document.addEventListener('click', e => {
  if (!document.getElementById('ci-call').contains(e.target) &&
      !document.getElementById('cs-dropdown').contains(e.target)) {
    clearCallsignDropdown();
  }
});

async function loadCheckins() {
  const checkins = await apiFetch(`/sessions/${currentSessionId}/checkins`).catch(() => []);
  if (currentNetIsAres) {
    try {
      const zones = await apiFetch(`/nets/${currentNetId}/evac-zones`);
      evacZones = {};
      zones.forEach(z => { evacZones[z.callsign] = z.zone; });
      populateKnownZonesList();
      renderZoneRoster();
    } catch {}
  }
  renderCheckins(checkins);
}

async function markTrafficCalled(checkinId) {
  if (checkinId == null) return; // pending (not checked in yet) — nothing to persist
  try {
    const updated = await apiFetch(`/checkins/${checkinId}/traffic-called`, { method: 'PATCH' });
    const idx = lastKnownCheckins.findIndex(c => c.id === checkinId);
    if (idx !== -1) lastKnownCheckins[idx] = updated;
    updateTrafficBanner(lastKnownCheckins);
  } catch (e) { toast(e.message, 'error'); }
}

function updateTrafficBanner(checkins) {
  const confirmedTraffic = checkins.filter(c => c.has_traffic);
  const confirmedCallsigns = new Set(checkins.map(c => c.callsign));
  // Pending: flagged in expected list but not yet checked in
  const pendingTraffic = [...pendingTrafficCallsigns].filter(cs => !confirmedCallsigns.has(cs));
  const banner = document.getElementById('traffic-banner');
  if (confirmedTraffic.length === 0 && pendingTraffic.length === 0) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = '';

  // called is persisted server-side (Checkin.traffic_called) so it survives a
  // session close/reopen; pending stations have no checkin row yet so there's
  // nothing to persist for them.
  const chips = [
    ...confirmedTraffic.map(c => ({ id: c.id, label: c.callsign + (c.name ? ` (${c.name})` : ''), pending: false, called: !!c.traffic_called })),
    ...pendingTraffic.map(cs => ({ id: null, label: `${cs} ⏳`, pending: true, called: false })),
  ];

  document.getElementById('traffic-callsigns').innerHTML = chips.map(({ id, label, pending, called }) => {
    return `<label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;
                background:rgba(0,0,0,0.18);border-radius:5px;padding:2px 8px;
                ${called ? 'opacity:0.45;text-decoration:line-through;' : ''}
                font-weight:700;white-space:nowrap">
      <input type="checkbox" ${called ? 'checked' : ''} ${pending ? `disabled title="${t('Not checked in yet')}"` : ''}
        onchange="markTrafficCalled(${id})"
        style="accent-color:#000;width:13px;height:13px;cursor:pointer" />
      ${esc(label)}
    </label>`;
  }).join('');
}

// Checkin.is_first_checkin is trivially true for every single row on a
// brand-new net's first-ever session (there's no prior checkin anywhere on
// the net for ANY callsign to be compared against) -- without this, "welcome
// new folks" would mean "welcome literally everyone", both as a wall of pills
// in the banner and a 👋 on every row, which conveys nothing and just looks
// broken. SessionOut.net_has_history (server-computed, see
// _net_has_prior_checkin_history) is false only in that specific case, so
// this gates both the banner and the per-row badge below.
function _showWelcomeBadges() {
  return !!(currentSessionData && currentSessionData.net_has_history);
}

function updateWelcomeBanner(checkins) {
  const firstTimers = _showWelcomeBadges() ? checkins.filter(c => c.is_first_checkin) : [];
  const banner = document.getElementById('welcome-banner');
  if (firstTimers.length === 0) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = '';
  document.getElementById('welcome-callsigns').innerHTML = firstTimers.map(c => {
    const label = c.callsign + (c.name ? ` (${c.name})` : '');
    return `<span style="background:rgba(0,0,0,0.18);border-radius:5px;padding:2px 8px;font-weight:700;white-space:nowrap">${esc(label)}</span>`;
  }).join('');
}

// Last 5 check-in ids added/synced, newest last — each highlighted for 20s
// (issue #18). A station drops out of the highlight the moment either the
// 20s window elapses or a 6th newer check-in bumps it off the list.
let recentCheckins = [];

function markRecentCheckin(id) {
  recentCheckins.push({ id, at: Date.now() });
  if (recentCheckins.length > 5) recentCheckins.shift();
  renderCheckins(lastKnownCheckins);
  setTimeout(() => renderCheckins(lastKnownCheckins), 20000);
}

function isRecentCheckin(id) {
  const entry = recentCheckins.find(r => r.id === id);
  return !!entry && (Date.now() - entry.at) < 20000;
}

function renderCheckinsHeader() {
  const header = document.getElementById('checkins-list-header');
  if (!header) return;
  header.innerHTML = currentSessionIsActivation
    ? `<span class="checkin-header-callsign">${t('Tactical')}</span>
       <span class="checkin-header-name">${t('Callsign')}</span>
       <span class="checkin-header-name">${t('Name')}</span>
       <span class="checkin-header-actions"></span>`
    : `<span class="checkin-header-callsign">${t('Callsign')}</span>
       <span class="checkin-header-name">${t('Name')}</span>
       <span class="checkin-header-traffic">${t('Tfc')}</span>
       <span class="checkin-header-actions"></span>`;
}

function renderCheckins(checkins) {
  lastKnownCheckins = checkins;
  const list = document.getElementById('checkins-list');
  const empty = document.getElementById('checkins-empty');
  const count = document.getElementById('checkin-count-label');
  count.textContent = tn(checkins.length, '{n} check-in', '{n} check-ins');

  updateTrafficBanner(checkins);
  updateWelcomeBanner(checkins);
  renderCheckinsHeader();

  if (checkins.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  const hasDmr = !!currentDmrConfig;
  list.innerHTML = checkins.map(c => {
    const details = [
      c.signal_report ? `${t('Signal:')} ${c.signal_report}` : null,
      c.comments ? `${t('Comments:')} ${c.comments}` : null,
      currentNetIsAres && c.evac_zone ? `${t('Zone:')} ${c.evac_zone}` : null,
      hasDmr && c.dmr_talkgroup ? `${t('TG:')} ${c.dmr_talkgroup}` : null,
      hasDmr && c.dmr_region ? `${t('Region:')} ${c.dmr_region}` : null,
      c.tactical_callsign ? `${t('Tactical:')} ${c.tactical_callsign}` : null,
      c.signed_off_at ? `${t('Signed off:')} ${fmt(c.signed_off_at)}` : null,
      `${t('Checked in:')} ${fmt(c.checked_in_at)}`,
    ].filter(Boolean).join(' · ');

    // Manual GPS position badge (issue follow-up) -- always clickable, dim
    // when unset, full opacity + colored when a position has been reported.
    // Shown on both row layouts below since ARES/ACES field team positions
    // are, if anything, the more likely case to need this.
    const hasPos = c.lat != null && c.lon != null;
    const posBadge = ` <span class="checkin-pos-badge" title="${hasPos ? t('Position reported — click to edit') : t('Set GPS position')}"
      style="cursor:pointer;opacity:${hasPos ? 1 : 0.3}"
      onclick="openCheckinPositionModal(${c.id}, '${esc(c.callsign)}', ${hasPos ? c.lat : 'null'}, ${hasPos ? c.lon : 'null'})">📍</span>`;

    // ARES/ACES activation session (issue #21): Tactical / Callsign / First
    // Name, no traffic toggle. A station with no tactical assignment shows
    // its evac zone instead, if it has one. Every other session (including
    // a routine ARES one): unchanged Callsign / Name / Traffic layout.
    if (currentSessionIsActivation) {
      const firstName = (c.name || '').trim().split(/\s+/)[0] || '';
      const tacticalCell = c.tactical_callsign
        ? esc(c.tactical_callsign)
        : (c.evac_zone ? `📍 ${esc(c.evac_zone)}` : '—');
      const welcomeBadge = (c.is_first_checkin && _showWelcomeBadges()) ? ` <span title="${t('First check-in on this net')}" style="font-size:11px">👋</span>` : '';
      return `<div class="checkin-row${isRecentCheckin(c.id) ? ' checkin-recent' : ''}" title="${esc(details)}">
        <span class="tactical-callsign">${tacticalCell}</span>
        <span class="callsign">${esc(c.callsign)}${welcomeBadge}${posBadge}</span>
        <span class="checkin-name">${esc(firstName || '—')}</span>
        <button class="btn btn-danger btn-sm" onclick="removeCheckin(${c.id})">✕</button>
      </div>`;
    }

    const welcomeBadge = (c.is_first_checkin && _showWelcomeBadges()) ? ` <span title="${t('First check-in on this net')}" style="font-size:11px">👋</span>` : '';
    return `<div class="checkin-row${isRecentCheckin(c.id) ? ' checkin-recent' : ''}" title="${esc(details)}">
      <span class="callsign">${esc(c.callsign)}${welcomeBadge}${posBadge}</span>
      <span class="checkin-name">${esc(c.name || '—')}</span>
      <button class="btn btn-sm ${c.has_traffic ? 'btn-danger' : 'btn-ghost'}"
        style="font-size:14px;padding:2px 8px" title="${c.has_traffic ? t('Traffic — click to clear') : t('Click to flag traffic')}"
        onclick="toggleTraffic(${c.id})">${c.has_traffic ? '📢' : '○'}</button>
      <button class="btn btn-danger btn-sm" onclick="removeCheckin(${c.id})">✕</button>
    </div>`;
  }).join('');
}

// ============================================================
// MANUAL GPS POSITION (issue follow-up) — for an operator with no APRS
// capability who can read off their own coordinates over the air. Set
// independently of check-in itself (after the fact, not on the fast
// check-in form), shown on the same station map as APRS-derived positions.
// ============================================================
function openCheckinPositionModal(id, callsign, lat, lon) {
  document.getElementById('checkin-position-id').value = id;
  document.getElementById('checkin-position-callsign').textContent = callsign;
  document.getElementById('checkin-position-lat').value = lat != null ? lat : '';
  document.getElementById('checkin-position-lon').value = lon != null ? lon : '';
  document.getElementById('checkin-position-modal').style.display = 'flex';
}

function closeCheckinPositionModal() {
  document.getElementById('checkin-position-modal').style.display = 'none';
}

async function saveCheckinPosition() {
  const id = document.getElementById('checkin-position-id').value;
  const lat = parseFloat(document.getElementById('checkin-position-lat').value);
  const lon = parseFloat(document.getElementById('checkin-position-lon').value);
  if (isNaN(lat) || isNaN(lon)) return toast(t('Enter both latitude and longitude'), 'error');
  try {
    await apiFetch(`/checkins/${id}/position`, { method: 'PATCH', body: JSON.stringify({ lat, lon }) });
    toast(t('Position saved'));
    closeCheckinPositionModal();
    await loadCheckins();
    refreshAprsMap();   // picks up the new/cleared pin on the map if it's open
  } catch (e) { toast(e.message, 'error'); }
}

async function clearCheckinPosition() {
  const id = document.getElementById('checkin-position-id').value;
  try {
    await apiFetch(`/checkins/${id}/position`, { method: 'PATCH', body: JSON.stringify({ lat: null, lon: null }) });
    toast(t('Position cleared'));
    closeCheckinPositionModal();
    await loadCheckins();
    refreshAprsMap();
  } catch (e) { toast(e.message, 'error'); }
}

async function removeCheckin(id) {
  if (!confirm(t('Remove this check-in?'))) return;
  try {
    await apiFetch(`/checkins/${id}`, { method: 'DELETE' });
    toast(t('Check-in removed'));
    await loadCheckins();
    renderExpectedList();
  } catch (e) { toast(e.message, 'error'); }
}

async function toggleTraffic(checkinId) {
  try {
    const updated = await apiFetch(`/checkins/${checkinId}/traffic`, { method: 'PATCH' });
    // Reload checkins and re-render (simplest approach for consistency)
    await loadCheckins();
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// EXPECTED STATIONS
// ============================================================
let expectedStations = [];        // loaded from /nets/{id}/expected
let lastKnownCheckins = [];       // most recent checkin list for banner refresh
const pendingTrafficCallsigns = new Set();  // traffic flagged in expected list before check-in

function toggleExpectedPanel() {
  const body = document.getElementById('expected-panel-body');
  const icon = document.getElementById('expected-toggle-icon');
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  icon.textContent = open ? '▼' : '▶';
  // ARES/ACES activation (issue #21) — this panel pulls from the Station
  // Schedule tab's tactical positions instead of the historical-attendance
  // list; load them the first time it's opened rather than requiring a
  // separate trip to the Schedule tab first.
  if (open && currentSessionIsActivation) loadTacticalPositions();
}

async function loadExpectedStations() {
  const minCheckins = parseInt(document.getElementById('exp-min').value) || 2;
  const weeks = parseInt(document.getElementById('exp-weeks').value) || 4;
  const listEl = document.getElementById('expected-list');
  listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('Loading…')}</p>`;
  try {
    expectedStations = await apiFetch(`/nets/${currentNetId}/expected?min_checkins=${minCheckins}&weeks=${weeks}`);
    renderExpectedList();
  } catch (e) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0;color:var(--lc-red)">${esc(e.message)}</p>`;
  }
}

// Get set of callsigns currently checked into the session (from the DOM)
function checkedInCallsigns() {
  return new Set(lastKnownCheckins.map(c => c.callsign));
}

function renderExpectedList() {
  // ARES/ACES activation session (issue #21) — replaces the historical-
  // attendance list entirely with the tactical position roster. A routine
  // session on an ARES net (currentNetIsAres but not currentSessionIsActivation)
  // falls through to the unchanged code below.
  document.getElementById('expected-panel-title').textContent = currentSessionIsActivation ? t('TACTICAL ASSIGNMENTS') : t('EXPECTED STATIONS');
  document.getElementById('expected-filter-row').style.display = currentSessionIsActivation ? 'none' : '';
  if (currentSessionIsActivation) { renderTacticalAssignments(); return; }

  const listEl = document.getElementById('expected-list');
  if (!expectedStations.length) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No matching stations found.')}</p>`;
    return;
  }
  const alreadyIn = checkedInCallsigns();
  listEl.innerHTML = expectedStations.map(st => {
    const checked = alreadyIn.has(st.callsign);
    const checkboxGroup = checked
      ? `<label style="display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:var(--lc-orange);white-space:nowrap">
           <input type="checkbox" checked disabled style="accent-color:var(--lc-orange);width:15px;height:15px" />
           ${t('Check In')}
         </label>
         <span style="font-size:11px;color:var(--text-muted);white-space:nowrap">—</span>`
      : `<label style="display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:var(--lc-orange);cursor:pointer;white-space:nowrap">
           <input type="checkbox"
             style="accent-color:var(--lc-orange);width:15px;height:15px;cursor:pointer"
             data-callsign="${esc(st.callsign)}" data-name="${esc(st.name || '')}"
             onchange="if(this.checked) checkInExpected(this, this.dataset.callsign, this.dataset.name)" />
           ${t('Check In')}
         </label>
         <label style="display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:var(--lc-red);cursor:pointer;white-space:nowrap">
           <input type="checkbox" id="exp-traffic-${esc(st.callsign)}"
             style="accent-color:var(--lc-red);width:15px;height:15px;cursor:pointer"
             data-callsign="${esc(st.callsign)}"
             onchange="toggleExpectedTraffic(this.dataset.callsign, this.checked)" />
           ${t('Traffic')}
         </label>`;
    const knownZone = evacZones[st.callsign];
    const zoneBadge = currentNetIsAres
      ? `<span style="font-size:11px;color:var(--lc-blue);white-space:nowrap;min-width:60px;text-align:right"
              title="${t('Last known zone')}">${knownZone ? '📍 ' + esc(knownZone) : ''}</span>`
      : '';
    return `<div class="exp-row" style="display:flex;align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid var(--border);flex-wrap:wrap;${checked ? 'opacity:.45' : ''}">
      ${checkboxGroup}
      <span class="callsign" style="min-width:80px">${esc(st.callsign)}</span>
      <span style="flex:1;display:flex;align-items:center;gap:6px;min-width:120px">
        <span class="exp-name-display" style="color:var(--text-muted);font-size:12px">${esc(st.name || '')}</span>
        <button type="button" title="${t('Set preferred name / remark for this station')}"
          onclick="toggleExpectedRemarkEditor(this, '${esc(st.callsign)}')"
          style="background:none;border:none;color:var(--lc-orange);cursor:pointer;font-size:11px;padding:0 2px;opacity:0.7">✏️</button>
      </span>
      ${zoneBadge}
      <span style="font-size:11px;color:var(--lc-blue);white-space:nowrap" title="${t('Check-ins in window')}">${st.checkin_count}✓</span>
    </div>`;
  }).join('');
}

function toggleExpectedTraffic(callsign, checked) {
  if (checked) {
    pendingTrafficCallsigns.add(callsign);
  } else {
    pendingTrafficCallsigns.delete(callsign);
  }
  updateTrafficBanner(lastKnownCheckins);
}

// ============================================================
// TACTICAL POSITIONS — ARES/ACES activation mode (issue #21)
// ============================================================
// Single load point shared by both surfaces that display this session's
// positions: the Station Schedule tab (setup) and the Tactical Assignments
// panel (renderExpectedList()'s activation branch, above — the live sign-on/
// off surface). Anything that changes a position (add/remove/sign-on/off)
// calls this to refresh both in sync rather than re-deriving state twice.
async function loadTacticalPositions() {
  if (!currentSessionIsActivation) return;
  try {
    tacticalPositions = await apiFetch(`/sessions/${currentSessionId}/tactical-positions`);
  } catch {
    tacticalPositions = [];
  }
  try {
    netControlShifts = await apiFetch(`/sessions/${currentSessionId}/net-control-shifts`);
  } catch {
    netControlShifts = [];
  }
  renderStationSchedule();
  renderNetControlStatusCard();
  renderNetControlShifts();
  renderExpectedList();
  // Net Control hands off through this same tactical position (issue #21 follow-up)
  // — refresh the duty bar/net script so a handoff shows up immediately everywhere
  // it's displayed, not just in this panel.
  if (currentSessionData) {
    renderDutyBar(effectiveSession(currentSessionData));
    renderNetScript(effectiveSession(currentSessionData));
  }
}

async function addTacticalPosition() {
  const tactical_callsign = document.getElementById('tac-pos-callsign').value.trim().toUpperCase();
  const location = document.getElementById('tac-pos-location').value.trim() || null;
  const assigned_callsign = document.getElementById('tac-pos-assigned-callsign').value.trim().toUpperCase() || null;
  const assigned_name = document.getElementById('tac-pos-assigned-name').value.trim() || null;
  // Month + day only -- the year is always the current one (an ARES/ACES activation
  // doesn't span into next year), so there's no year picker to fumble with.
  const month = document.getElementById('tac-pos-scheduled-month').value;
  const day = document.getElementById('tac-pos-scheduled-day').value;
  const time = document.getElementById('tac-pos-scheduled-time').value;
  let scheduled_start = null;
  if (month && day) {
    const year = new Date().getFullYear();
    const localStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}T${time || '00:00'}`;
    scheduled_start = new Date(localStr).toISOString();
  }
  if (!tactical_callsign) return toast(t('Tactical callsign required'), 'error');
  try {
    await apiFetch(`/sessions/${currentSessionId}/tactical-positions`, {
      method: 'POST',
      body: JSON.stringify({ tactical_callsign, location, assigned_callsign, assigned_name, scheduled_start }),
    });
    document.getElementById('tac-pos-callsign').value = '';
    document.getElementById('tac-pos-location').value = '';
    document.getElementById('tac-pos-assigned-callsign').value = '';
    document.getElementById('tac-pos-assigned-name').value = '';
    setDefaultMonthDay('tac-pos-scheduled-month', 'tac-pos-scheduled-day');
    document.getElementById('tac-pos-scheduled-time').value = '';
    toast(t('Position added'), 'success');
    await loadTacticalPositions();
  } catch (e) { toast(e.message, 'error'); }
}

// ── Net Control rotation schedule (issue #21 follow-up) ────────────────────
// A forward-looking queue of planned Net Control shifts, separate from a
// tactical station's single "planned operator" — Net Control classically
// hands off on a fixed cadence throughout a long activation, so it gets its
// own rotation plan rather than one "who's next" slot.
async function addNetControlShift() {
  const callsign = document.getElementById('nc-shift-callsign').value.trim().toUpperCase();
  const name = document.getElementById('nc-shift-name').value.trim() || null;
  const month = document.getElementById('nc-shift-month').value;
  const day = document.getElementById('nc-shift-day').value;
  const time = document.getElementById('nc-shift-time').value;
  if (!callsign) return toast(t('Callsign required'), 'error');
  if (!month || !day) return toast(t('Scheduled sign-on date required'), 'error');
  const year = new Date().getFullYear();
  const scheduled_start = new Date(`${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}T${time || '00:00'}`).toISOString();
  try {
    await apiFetch(`/sessions/${currentSessionId}/net-control-shifts`, {
      method: 'POST',
      body: JSON.stringify({ callsign, name, scheduled_start }),
    });
    document.getElementById('nc-shift-callsign').value = '';
    document.getElementById('nc-shift-name').value = '';
    document.getElementById('nc-shift-time').value = '';
    setDefaultMonthDay('nc-shift-month', 'nc-shift-day');
    toast(t('Shift added'), 'success');
    await loadTacticalPositions();
  } catch (e) { toast(e.message, 'error'); }
}

async function removeNetControlShift(id) {
  try {
    await apiFetch(`/net-control-shifts/${id}`, { method: 'DELETE' });
    toast(t('Shift removed'), 'success');
    await loadTacticalPositions();
  } catch (e) { toast(e.message, 'error'); }
}

function renderNetControlShifts() {
  const listEl = document.getElementById('net-control-shifts-list');
  if (!listEl) return;
  if (!netControlShifts.length) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No planned shifts yet — add one above to queue up the next handoff.')}</p>`;
    return;
  }
  const sorted = [...netControlShifts].sort((a, b) => new Date(a.scheduled_start) - new Date(b.scheduled_start));
  listEl.innerHTML = sorted.map((s, i) => `
    <div class="card" style="padding:10px 12px;margin-bottom:8px;display:flex;align-items:center;gap:12px;flex-wrap:wrap${i === 0 ? ';border-color:var(--lc-blue)' : ''}">
      <div style="flex:1;min-width:160px">
        ${i === 0 ? `<div class="text-muted" style="font-size:10px;color:var(--lc-blue)">${t('NEXT UP')}</div>` : ''}
        <span class="callsign">${esc(s.callsign)}</span>${s.name ? ` <span class="text-muted" style="font-size:12px">— ${esc(s.name)}</span>` : ''}
      </div>
      <span style="font-size:12px;color:var(--text-muted);white-space:nowrap">🕐 ${fmt(s.scheduled_start)}</span>
      <button type="button" class="btn btn-danger btn-sm" onclick="removeNetControlShift(${s.id})">✕ ${t('Remove')}</button>
    </div>`).join('');
}

async function removeTacticalPosition(id) {
  if (!confirm(t('Remove this tactical position? Its shift history is kept, just no longer linked to a position.'))) return;
  try {
    await apiFetch(`/tactical-positions/${id}`, { method: 'DELETE' });
    toast(t('Position removed'), 'success');
    await loadTacticalPositions();
  } catch (e) { toast(e.message, 'error'); }
}

// Station Schedule tab — position setup (callsign/location/planned operator/
// scheduled time, all editable after creation via ✏️ Edit) plus, for Net
// Control specifically, the same sign-on/hand-off/history controls as
// Tactical Assignments -- Net Control has no separate creation form of its
// own (it's auto-created at session start), so this tab is the only place
// its plan can be set, and operators look here first to hand it off too
// (issue #21 follow-up: a plain pointer to the other tab wasn't enough).
// Tactical Stations sub-tab — one-off field positions only. Net Control lives
// on its own sub-tab (renderNetControlStatusCard, below) with its own rotation
// schedule instead of a single planned-operator field (issue #21 follow-up).
function renderStationSchedule() {
  const listEl = document.getElementById('tactical-schedule-list');
  if (!listEl) return;
  const positions = tacticalPositions.filter(p => !p.is_net_control);
  if (!positions.length) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No tactical positions yet — add one above.')}</p>`;
    return;
  }
  listEl.innerHTML = positions.map(p => {
    const occupied = !!p.current_callsign;
    return `<div class="card" style="padding:10px 12px;margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:160px">
          <span class="callsign">${esc(p.tactical_callsign)}</span>
          ${p.location ? `<span class="text-muted" style="font-size:12px;margin-left:8px">📍 ${esc(p.location)}</span>` : ''}
          ${p.assigned_callsign ? `<div class="text-muted" style="font-size:11px;margin-top:2px">${t('Planned:')} ${esc(p.assigned_callsign)}${p.assigned_name ? ' — ' + esc(p.assigned_name) : ''}</div>` : ''}
          ${p.scheduled_start ? `<div class="text-muted" style="font-size:11px;margin-top:2px">🕐 ${t('Sign-on:')} ${fmt(p.scheduled_start)}</div>` : ''}
        </div>
        <span style="font-size:12px;color:${occupied ? 'var(--lc-green)' : 'var(--text-muted)'};white-space:nowrap">
          ${occupied ? `🟢 ${esc(p.current_callsign)}${p.current_name ? ' — ' + esc(p.current_name) : ''}` : `⚪ ${t('Vacant')}`}
        </span>
        <button type="button" class="btn btn-ghost btn-sm" onclick="toggleEditPositionForm(${p.id})">✏️ ${t('Edit')}</button>
        <button type="button" class="btn btn-danger btn-sm" onclick="removeTacticalPosition(${p.id})">✕ ${t('Remove')}</button>
      </div>
      <div id="tac-edit-form-${p.id}" style="display:none;margin-top:8px"></div>
    </div>`;
  }).join('');
}

// Net Control sub-tab — the live status/handoff card. Hand Off Net Control
// auto-fills from the next planned shift in netControlShifts (issue #21
// follow-up), handled in toggleSignOnForm below.
function renderNetControlStatusCard() {
  const container = document.getElementById('net-control-status-card');
  if (!container) return;
  const p = tacticalPositions.find(x => x.is_net_control);
  if (!p) { container.innerHTML = ''; return; }
  const occupied = !!p.current_callsign;
  container.innerHTML = `
    <div class="card" style="padding:10px 12px;margin-bottom:12px;border-color:var(--lc-orange)">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:160px">
          <span class="callsign">🎙 ${t('NET CONTROL')}</span>
        </div>
        <span style="font-size:12px;color:${occupied ? 'var(--lc-green)' : 'var(--text-muted)'};white-space:nowrap">
          ${occupied ? `🟢 ${esc(p.current_callsign)}${p.current_name ? ' — ' + esc(p.current_name) : ''}` : `⚪ ${t('Vacant')}`}
        </span>
        <button type="button" class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 6px" onclick="toggleShiftHistory(${p.id}, 'schedule')">🕐 ${t('History')}</button>
        ${occupied
          ? `<button type="button" class="btn btn-ghost btn-sm" onclick="toggleSignOnForm(${p.id}, 'schedule')">🔄 ${t('Hand Off Net Control')}</button>
             <button type="button" class="btn btn-danger btn-sm" onclick="signOffPosition(${p.id})">${t('Sign Off')}</button>`
          : `<button type="button" class="btn btn-primary btn-sm" onclick="toggleSignOnForm(${p.id}, 'schedule')">${t('Sign On Net Control')}</button>`}
      </div>
      <div id="tac-signon-form-schedule-${p.id}" style="display:none;margin-top:8px"></div>
      <div id="tac-history-schedule-${p.id}" style="display:none;margin-top:8px;font-size:11px"></div>
    </div>`;
}

// Tactical Assignments panel (lives in the Check-In tab's Expected Stations
// slot) — the live sign-on/off surface, pulling from the same tacticalPositions
// data the Station Schedule tab defines.
function renderTacticalAssignments() {
  const listEl = document.getElementById('expected-list');
  if (!tacticalPositions.length) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No tactical positions defined yet — add some on the 🗓 Station Schedule tab.')}</p>`;
    return;
  }
  listEl.innerHTML = tacticalPositions.map(p => {
    const occupied = !!p.current_callsign;
    let dueBadge = '';
    if (!occupied && p.scheduled_start) {
      const overdue = new Date(p.scheduled_start) <= new Date();
      dueBadge = `<span style="font-size:10px;color:${overdue ? 'var(--lc-red)' : 'var(--lc-blue)'};white-space:nowrap">⏰ ${overdue ? t('Due since') : t('Due')} ${fmt(p.scheduled_start)}</span>`;
    }
    return `<div class="exp-row" style="display:block;padding:8px 0;border-bottom:1px solid var(--border)${p.is_net_control ? ';background:rgba(255,153,0,0.06)' : ''}">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="callsign">${p.is_net_control ? '🎙 ' : ''}${esc(p.tactical_callsign)}</span>
        ${p.location ? `<span class="text-muted" style="font-size:11px">📍 ${esc(p.location)}</span>` : ''}
        <span style="flex:1;min-width:140px;font-size:12px;color:${occupied ? 'var(--lc-green)' : 'var(--text-muted)'}">
          ${occupied
            ? `🟢 ${esc(p.current_callsign)}${p.current_name ? ' — ' + esc(p.current_name) : ''} <span class="text-muted" style="font-size:10px">${t('since')} ${fmt(p.signed_on_at)}</span>`
            : `⚪ ${t('Vacant')}`}
        </span>
        ${dueBadge}
        <button type="button" class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 6px" onclick="toggleShiftHistory(${p.id}, 'assign')">🕐 ${t('History')}</button>
        ${occupied
          ? `<button type="button" class="btn btn-ghost btn-sm" onclick="toggleSignOnForm(${p.id}, 'assign')">${p.is_net_control ? '🔄 ' + t('Hand Off Net Control') : '↻ ' + t('Sign Off & Replace')}</button>
             <button type="button" class="btn btn-danger btn-sm" onclick="signOffPosition(${p.id})">${t('Sign Off')}</button>`
          : `<button type="button" class="btn btn-primary btn-sm" onclick="toggleSignOnForm(${p.id}, 'assign')">${p.is_net_control ? t('Sign On Net Control') : t('Sign On')}</button>`}
      </div>
      ${p.is_net_control
        ? `<div class="text-muted" style="font-size:10px;margin-top:3px">🎙 ${t('Net Control is auto-staffed at session start. To hand it to someone else, click')} <strong>${t('Hand Off Net Control')}</strong> ${t("and enter the incoming operator's callsign — the outgoing operator's shift closes immediately and the change shows up in the duty bar at the top of the screen. (Also available on the 🗓 Station Schedule tab.)")}</div>`
        : ''}
      <div id="tac-signon-form-assign-${p.id}" style="display:none;margin-top:8px"></div>
      <div id="tac-history-assign-${p.id}" style="display:none;margin-top:8px;font-size:11px"></div>
    </div>`;
  }).join('');
}

// scope distinguishes which surface rendered this control -- Station Schedule
// ('schedule') and Tactical Assignments ('assign') both list every position
// (including Net Control), so each needs its own container/input ids to avoid
// colliding on the same element id twice in one page.
function toggleSignOnForm(positionId, scope = 'assign') {
  const container = document.getElementById(`tac-signon-form-${scope}-${positionId}`);
  if (!container) return;
  if (container.style.display !== 'none') { container.style.display = 'none'; container.innerHTML = ''; return; }
  const position = tacticalPositions.find(p => p.id === positionId);
  let defaultCallsign = (position && position.assigned_callsign) || '';
  let defaultName = (position && position.assigned_name) || '';
  let sourceShift = null;
  // Net Control auto-fills from the next planned shift in its rotation
  // schedule (soonest scheduled_start) rather than a single planned-operator
  // field -- still fully editable in case of a last-minute change (issue #21
  // follow-up).
  if (position && position.is_net_control && netControlShifts.length) {
    sourceShift = [...netControlShifts].sort((a, b) => new Date(a.scheduled_start) - new Date(b.scheduled_start))[0];
    defaultCallsign = sourceShift.callsign;
    defaultName = sourceShift.name || '';
  }
  container.style.display = '';
  container.dataset.sourceShiftId = sourceShift ? sourceShift.id : '';
  container.innerHTML = `
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <input class="form-control mono" id="tac-signon-callsign-${scope}-${positionId}" placeholder="${t('Callsign')}" value="${esc(defaultCallsign)}" style="width:110px;text-transform:uppercase;font-size:12px" />
      <input class="form-control" id="tac-signon-name-${scope}-${positionId}" placeholder="${t('Name (optional)')}" value="${esc(defaultName)}" style="width:140px;font-size:12px" />
      <button class="btn btn-primary btn-sm" onclick="signOnPosition(${positionId}, '${scope}')">${t('Confirm')}</button>
      <button class="btn btn-ghost btn-sm" onclick="toggleSignOnForm(${positionId}, '${scope}')">${t('Cancel')}</button>
    </div>
    ${sourceShift ? `<div class="text-muted" style="font-size:10px;margin-top:4px">${t('Auto-filled from the next scheduled shift')} (${fmt(sourceShift.scheduled_start)}) — ${t('edit if plans changed.')}</div>` : ''}`;
  document.getElementById(`tac-signon-callsign-${scope}-${positionId}`).focus();
}

async function signOnPosition(positionId, scope = 'assign') {
  const callsignEl = document.getElementById(`tac-signon-callsign-${scope}-${positionId}`);
  const nameEl = document.getElementById(`tac-signon-name-${scope}-${positionId}`);
  const container = document.getElementById(`tac-signon-form-${scope}-${positionId}`);
  const sourceShiftId = container ? container.dataset.sourceShiftId : '';
  const callsign = callsignEl.value.trim().toUpperCase();
  const name = nameEl.value.trim() || null;
  if (!callsign) return toast(t('Callsign required'), 'error');
  try {
    await apiFetch(`/tactical-positions/${positionId}/sign-on`, {
      method: 'POST',
      body: JSON.stringify({ callsign, name }),
    });
    // The consumed shift's scheduled slot has now passed regardless of who
    // actually signed on (the auto-fill may have been overridden) -- clear it
    // so "next" always points at the following planned shift.
    if (sourceShiftId) {
      try { await apiFetch(`/net-control-shifts/${sourceShiftId}`, { method: 'DELETE' }); } catch {}
    }
    toast(`${callsign} ${t('signed on')}`, 'success');
    await loadTacticalPositions();
    await loadCheckins();
  } catch (e) { toast(e.message, 'error'); }
}

async function signOffPosition(positionId) {
  if (!confirm(t('Sign off the current operator from this position?'))) return;
  try {
    await apiFetch(`/tactical-positions/${positionId}/sign-off`, { method: 'POST' });
    toast(t('Signed off'), 'success');
    await loadTacticalPositions();
    await loadCheckins();
  } catch (e) { toast(e.message, 'error'); }
}

async function toggleShiftHistory(positionId, scope = 'assign') {
  const container = document.getElementById(`tac-history-${scope}-${positionId}`);
  if (!container) return;
  if (container.style.display !== 'none') { container.style.display = 'none'; return; }
  container.style.display = '';
  container.innerHTML = `<span class="text-muted">${t('Loading…')}</span>`;
  try {
    const shifts = await apiFetch(`/tactical-positions/${positionId}/shifts`);
    if (!shifts.length) {
      container.innerHTML = `<span class="text-muted">${t('No shifts yet.')}</span>`;
      return;
    }
    container.innerHTML = shifts.map(s => `
      <div class="text-muted" style="padding:2px 0">
        ${esc(s.callsign)}${s.name ? ' — ' + esc(s.name) : ''}:
        ${fmt(s.checked_in_at)} → ${s.signed_off_at ? fmt(s.signed_off_at) : `<span style="color:var(--lc-green)">${t('now')}</span>`}
      </div>`).join('');
  } catch (e) {
    container.innerHTML = `<span style="color:var(--lc-red)">${esc(e.message)}</span>`;
  }
}

// Edit a position's plan — location, planned operator, scheduled sign-on
// (month/day/time, current year). The only path for Net Control specifically,
// since it's auto-created with no creation form of its own to set these on
// (issue #21 follow-up: "Net Control should be schedulable just like a
// tactical station").
const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function toggleEditPositionForm(positionId) {
  const container = document.getElementById(`tac-edit-form-${positionId}`);
  if (!container) return;
  if (container.style.display !== 'none') { container.style.display = 'none'; container.innerHTML = ''; return; }
  const p = tacticalPositions.find(x => x.id === positionId);
  if (!p) return;
  const d = p.scheduled_start ? new Date(p.scheduled_start) : null;
  const monthOpts = MONTH_NAMES.map((name, i) => `<option value="${i + 1}" ${d && d.getMonth() === i ? 'selected' : ''}>${name}</option>`).join('');
  const dayOpts = Array.from({ length: 31 }, (_, i) => i + 1)
    .map(n => `<option value="${n}" ${d && d.getDate() === n ? 'selected' : ''}>${n}</option>`).join('');
  const timeVal = d ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '';
  container.style.display = '';
  container.innerHTML = `
    <div class="form-row">
      <div class="form-group mb-0">
        <label style="font-size:11px">${t('Location')}</label>
        <input class="form-control" id="tac-edit-location-${positionId}" value="${esc(p.location || '')}" style="font-size:13px" />
      </div>
      <div class="form-group mb-0">
        <label style="font-size:11px">${t('Assigned Operator Callsign')}</label>
        <input class="form-control mono" id="tac-edit-assigned-callsign-${positionId}" value="${esc(p.assigned_callsign || '')}" style="text-transform:uppercase;font-size:13px" />
      </div>
    </div>
    <div class="form-row" style="margin-top:8px">
      <div class="form-group mb-0">
        <label style="font-size:11px">${t('Assigned Operator Name')}</label>
        <input class="form-control" id="tac-edit-assigned-name-${positionId}" value="${esc(p.assigned_name || '')}" style="font-size:13px" />
      </div>
      <div class="form-group mb-0">
        <label style="font-size:11px">${t('Scheduled Sign-On')} <span class="text-muted">${t('(this year)')}</span></label>
        <div style="display:flex;gap:6px">
          <select class="form-control" id="tac-edit-month-${positionId}" style="font-size:13px;width:78px"><option value="">${t('Month')}</option>${monthOpts}</select>
          <select class="form-control" id="tac-edit-day-${positionId}" style="font-size:13px;width:66px"><option value="">${t('Day')}</option>${dayOpts}</select>
          <input class="form-control" type="time" id="tac-edit-time-${positionId}" value="${timeVal}" style="font-size:13px;width:110px" />
        </div>
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-top:8px">
      <button type="button" class="btn btn-primary btn-sm" onclick="savePositionEdit(${positionId})">${t('Save')}</button>
      <button type="button" class="btn btn-ghost btn-sm" onclick="toggleEditPositionForm(${positionId})">${t('Cancel')}</button>
    </div>`;
}

async function savePositionEdit(positionId) {
  const location = document.getElementById(`tac-edit-location-${positionId}`).value.trim() || null;
  const assigned_callsign = document.getElementById(`tac-edit-assigned-callsign-${positionId}`).value.trim().toUpperCase() || null;
  const assigned_name = document.getElementById(`tac-edit-assigned-name-${positionId}`).value.trim() || null;
  const month = document.getElementById(`tac-edit-month-${positionId}`).value;
  const day = document.getElementById(`tac-edit-day-${positionId}`).value;
  const time = document.getElementById(`tac-edit-time-${positionId}`).value;
  let scheduled_start = null;
  if (month && day) {
    const year = new Date().getFullYear();
    scheduled_start = new Date(`${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}T${time || '00:00'}`).toISOString();
  }
  try {
    await apiFetch(`/tactical-positions/${positionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ location, assigned_callsign, assigned_name, scheduled_start }),
    });
    toast(t('Position updated'), 'success');
    await loadTacticalPositions();
  } catch (e) { toast(e.message, 'error'); }
}

// ── Zone roster (ARES nets) ──────────────────────────────────
function populateKnownZonesList() {
  const dl = document.getElementById('known-zones-list');
  if (!dl) return;
  const distinctZones = [...new Set(Object.values(evacZones))].sort();
  dl.innerHTML = distinctZones.map(z => `<option value="${esc(z)}">`).join('');
}

function toggleZoneRoster() {
  const body = document.getElementById('zone-roster-body');
  const icon = document.getElementById('zone-roster-toggle-icon');
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  icon.textContent = open ? '▼' : '▶';
}

function callsignSuffix(cs) {
  const m = cs.toUpperCase().match(/\d([A-Z]+)$/);
  return m ? m[1] : cs;
}

function renderZoneRoster() {
  const listEl = document.getElementById('zone-roster-list');
  if (!listEl) return;
  const entries = Object.entries(evacZones); // [callsign, zone]
  if (entries.length === 0) {
    listEl.innerHTML = `<p class="text-muted" style="font-size:12px;margin:0">${t('No zones recorded yet.')}</p>`;
    return;
  }
  // Group by zone
  const byZone = {};
  entries.forEach(([cs, zone]) => {
    if (!byZone[zone]) byZone[zone] = [];
    byZone[zone].push(cs);
  });
  const sortedZones = Object.keys(byZone).sort();
  listEl.innerHTML = sortedZones.map(zone => {
    const callsigns = byZone[zone].slice().sort((a, b) => callsignSuffix(a).localeCompare(callsignSuffix(b)));
    return `<div style="margin-bottom:8px">
      <div style="font-weight:700;color:var(--lc-orange);font-size:12px;margin-bottom:4px;letter-spacing:.05em">
        📍 ${esc(zone)}
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;padding-left:12px">
        ${callsigns.map(cs => `<span class="callsign" style="font-size:12px;background:rgba(255,153,0,0.12);padding:2px 7px;border-radius:4px">${esc(cs)}</span>`).join('')}
      </div>
    </div>`;
  }).join('');
}

async function checkInExpected(checkbox, callsign, name) {
  const trafficEl = document.getElementById(`exp-traffic-${callsign}`);
  const has_traffic = trafficEl ? trafficEl.checked : false;
  const evac_zone = currentNetIsAres ? (evacZones[callsign] || null) : null;
  checkbox.disabled = true;
  try {
    const created = await apiFetch(`/sessions/${currentSessionId}/checkins`, {
      method: 'POST',
      body: JSON.stringify({ callsign, name: name || null, has_traffic, evac_zone })
    });
    pendingTrafficCallsigns.delete(callsign);  // now confirmed in DB, remove from pending
    toast(`${callsign} ${t('checked in')}`, 'success');
    markRecentCheckin(created.id);
    await loadCheckins();
    renderExpectedList();
  } catch (e) {
    toast(e.message, 'error');
    checkbox.checked = false;
    checkbox.disabled = false;
  }
}


// ============================================================
// TRAFFIC MESSAGE LOG
// ============================================================
let trafficMessages = [];

function toggleTrafficLog() {
  const body = document.getElementById('traffic-log-body');
  const icon = document.getElementById('traffic-log-toggle-icon');
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  icon.textContent = open ? '▼' : '▶';
}

async function loadTrafficMessages() {
  if (!currentSessionId) return;
  try {
    trafficMessages = await apiFetch(`/sessions/${currentSessionId}/traffic-messages`);
    renderTrafficMessages();
  } catch {}
}

function renderTrafficMessages() {
  const listEl = document.getElementById('traffic-log-list');
  const countEl = document.getElementById('traffic-log-count');
  countEl.textContent = trafficMessages.length ? `(${trafficMessages.length})` : '';
  if (!trafficMessages.length) {
    listEl.innerHTML = `<p class="text-muted" style="margin:0;font-size:12px">${t('No messages logged yet.')}</p>`;
    return;
  }
  const statusColors = { received:'var(--lc-blue)', relayed:'var(--lc-orange)', delivered:'var(--success)', undeliverable:'var(--lc-red)' };
  const typeLabels = { formal: t('Formal'), informal: t('Informal'), health_welfare: t('Health & Welfare') };
  listEl.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="border-bottom:1px solid var(--border)">
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">#</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Msg #')}</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Origin')}</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Destination')}</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Type')}</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Status')}</th>
      <th style="text-align:left;padding:4px 6px;color:var(--text-muted);font-weight:600">${t('Notes')}</th>
      <th></th>
    </tr></thead>
    <tbody>
    ${trafficMessages.map((m, i) => `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:4px 6px;color:var(--text-muted)">${i+1}</td>
      <td style="padding:4px 6px;font-family:monospace">${esc(m.msg_number || '—')}</td>
      <td style="padding:4px 6px"><span class="callsign" style="font-size:11px">${esc(m.origin_callsign)}</span></td>
      <td style="padding:4px 6px">${esc(m.dest_info || '—')}</td>
      <td style="padding:4px 6px">${esc(typeLabels[m.msg_type] || m.msg_type)}</td>
      <td style="padding:4px 6px">
        <select style="font-size:11px;background:var(--bg);color:${statusColors[m.status]||'inherit'};border:1px solid var(--border);border-radius:4px;padding:2px 4px"
          onchange="updateTrafficStatus(${m.id}, this.value)">
          <option value="received" ${m.status==='received'?'selected':''}>${t('Received')}</option>
          <option value="relayed" ${m.status==='relayed'?'selected':''}>${t('Relayed')}</option>
          <option value="delivered" ${m.status==='delivered'?'selected':''}>${t('Delivered')}</option>
          <option value="undeliverable" ${m.status==='undeliverable'?'selected':''}>${t('Undeliverable')}</option>
        </select>
      </td>
      <td style="padding:4px 6px;color:var(--text-muted)">${esc(m.notes || '')}</td>
      <td style="padding:4px 6px">
        <button class="btn btn-danger btn-sm" onclick="deleteTrafficMessage(${m.id})" style="padding:1px 6px;font-size:11px">✕</button>
      </td>
    </tr>`).join('')}
    </tbody></table>`;
}

async function addTrafficMessage() {
  const origin = document.getElementById('tm-origin').value.trim().toUpperCase();
  if (!origin) { toast(t('Origin callsign required'), 'error'); return; }
  try {
    await apiFetch(`/sessions/${currentSessionId}/traffic-messages`, {
      method: 'POST',
      body: JSON.stringify({
        origin_callsign: origin,
        dest_info: document.getElementById('tm-dest').value.trim() || null,
        msg_number: document.getElementById('tm-number').value.trim() || null,
        msg_type: document.getElementById('tm-type').value,
        notes: document.getElementById('tm-notes').value.trim() || null,
      })
    });
    document.getElementById('tm-origin').value = '';
    document.getElementById('tm-dest').value = '';
    document.getElementById('tm-number').value = '';
    document.getElementById('tm-notes').value = '';
    toast(t('Message logged'), 'success');
    await loadTrafficMessages();
  } catch (e) { toast(e.message, 'error'); }
}

async function updateTrafficStatus(msgId, status) {
  try {
    await apiFetch(`/traffic-messages/${msgId}`, { method: 'PATCH', body: JSON.stringify({ status }) });
    await loadTrafficMessages();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteTrafficMessage(msgId) {
  if (!confirm(t('Remove this message?'))) return;
  try {
    await apiFetch(`/traffic-messages/${msgId}`, { method: 'DELETE' });
    await loadTrafficMessages();
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// STATION REMARKS & PREFERRED NAME
// ============================================================
// A preferred name overrides the FCC/GMRS-looked-up name in the Expected
// Stations list and net reports (ICS-205, CSV exports) — not the live
// check-in form or the stored check-in record itself.

async function loadStationRemarks(callsign) {
  if (!currentNetId || !callsign) return null;
  try {
    return await apiFetch(`/nets/${currentNetId}/stations/${encodeURIComponent(callsign)}/remark`);
  } catch { return null; }
}

async function saveStationRemark(callsign, remark, preferredName) {
  if (!currentNetId) return null;
  return await apiFetch(`/nets/${currentNetId}/stations/${encodeURIComponent(callsign)}/remark`, {
    method: 'PUT', body: JSON.stringify({ remark: remark.trim() || null, preferred_name: preferredName.trim() || null }),
  });
}

function showRemarkEditor(callsign, current) {
  const existing = document.getElementById('remark-editor');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.id = 'remark-editor';
  // Positioned absolutely (like #cs-dropdown) rather than in normal flow —
  // the callsign field's column is only ~140px wide, which was forcing every
  // item onto its own line no matter how the flex layout was tuned. Wraps
  // and caps its own width to the viewport so it can never run off the
  // right edge of a narrow phone screen regardless of where the callsign
  // field itself sits horizontally.
  div.style.cssText = 'position:absolute;top:100%;left:0;z-index:150;margin-top:4px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;background:var(--surface);border:1px solid var(--lc-orange);border-radius:8px;padding:8px;box-shadow:0 4px 12px rgba(0,0,0,.5);max-width:min(360px, calc(100vw - 32px))';
  div.innerHTML = `
    <input id="remark-preferred-name-input" class="form-control" style="width:130px;font-size:12px"
      placeholder="${t('Preferred name')}" value="${esc((current && current.preferred_name) || '')}" />
    <input id="remark-input" class="form-control" style="flex:1;min-width:140px;font-size:12px"
      placeholder="${t('Notes about this station…')}" value="${esc((current && current.remark) || '')}" />
    <button class="btn btn-primary btn-sm" onclick="submitRemark('${esc(callsign)}')">${t('Save')}</button>
    <button class="btn btn-ghost btn-sm" onclick="document.getElementById('remark-editor').remove()">✕</button>`;
  const lookupInfo = document.getElementById('ci-lookup-info');
  lookupInfo.appendChild(div);
  onEnter(['remark-preferred-name-input', 'remark-input'], () => submitRemark(callsign));
  div.querySelector('#remark-preferred-name-input').focus();
}

async function submitRemark(callsign) {
  const remarkVal = document.getElementById('remark-input')?.value || '';
  const preferredNameVal = document.getElementById('remark-preferred-name-input')?.value || '';
  try {
    const saved = await saveStationRemark(callsign, remarkVal, preferredNameVal);
    toast(saved ? t('Saved') : t('Cleared'), 'success');
    document.getElementById('remark-editor')?.remove();
    // Refresh the remark pill in lookup info
    const pill = document.getElementById('remark-pill');
    if (pill) {
      if (saved) { pill.querySelector('span').textContent = remarkPillText(saved); }
      else { pill.remove(); }
    }
  } catch (e) { toast(e.message, 'error'); }
}

// Inline preferred name / remark editor for a row in the Expected Stations
// list — the check-in form's pill only appears once a callsign is typed
// there, so this gives a second, more discoverable entry point.
async function toggleExpectedRemarkEditor(btn, callsign) {
  const row = btn.closest('.exp-row');
  const existing = row.querySelector('.exp-remark-editor');
  if (existing) { existing.remove(); return; }

  const current = await loadStationRemarks(callsign);
  const editor = document.createElement('div');
  editor.className = 'exp-remark-editor';
  editor.style.cssText = 'display:flex;gap:6px;align-items:center;flex-wrap:wrap';
  editor.innerHTML = `
    <input class="form-control exp-pref-input" style="width:120px;font-size:12px"
      placeholder="${t('Preferred name')}" value="${esc((current && current.preferred_name) || '')}" />
    <input class="form-control exp-remark-input" style="width:140px;font-size:12px"
      placeholder="${t('Notes')}" value="${esc((current && current.remark) || '')}" />
    <button class="btn btn-primary btn-sm" type="button">${t('Save')}</button>
    <button class="btn btn-ghost btn-sm" type="button">✕</button>`;
  const prefInput = editor.querySelector('.exp-pref-input');
  const remarkInput = editor.querySelector('.exp-remark-input');
  const [saveBtn, cancelBtn] = editor.querySelectorAll('button');
  const doSave = async () => {
    try {
      await saveStationRemark(callsign, remarkInput.value, prefInput.value);
      toast(t('Saved'), 'success');
      await loadExpectedStations();
    } catch (e) { toast(e.message, 'error'); }
  };
  saveBtn.onclick = doSave;
  cancelBtn.onclick = () => editor.remove();
  [prefInput, remarkInput].forEach(el => el.addEventListener('keydown', e => { if (e.key === 'Enter') doSave(); }));
  row.appendChild(editor);
  prefInput.focus();
}

