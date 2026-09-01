// ============================================================
// PROFILE — name/email/callsign/phone, photo (issue follow-up)
// ============================================================
async function saveProfile() {
  const name = document.getElementById('profile-name').value.trim();
  if (!name) return toast(t('Name is required'), 'error');
  const email = document.getElementById('profile-email').value.trim();
  const callsign = document.getElementById('profile-callsign').value.trim().toUpperCase();
  const phone = document.getElementById('profile-phone').value.trim() || null;
  const emailChanged = email !== currentUser.email;
  try {
    currentUser = await apiFetch('/auth/profile', {
      method: 'PATCH', body: JSON.stringify({ name, email, callsign, phone }),
    });
    document.getElementById('profile-name').value = currentUser.name || '';
    document.getElementById('profile-email').value = currentUser.email || '';
    document.getElementById('profile-callsign').value = currentUser.callsign || '';
    document.getElementById('profile-phone').value = currentUser.phone || '';
    // Callsign/name changed -- header shows them immediately, no reload needed.
    const cs = document.getElementById('header-callsign');
    if (cs) cs.textContent = currentUser.callsign;
    const csShort = document.getElementById('header-callsign-short');
    if (csShort) csShort.textContent = currentUser.callsign;

    // Upload the photo file, if one was chosen, as a second step -- same
    // two-step shape as branding.js's saveBranding() (text fields via one
    // call, logo/photo via a separate multipart POST).
    const fileInput = document.getElementById('profile-photo-file');
    if (fileInput.files[0]) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      await fetch('/auth/photo', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: fd,
      }).then(r => { if (!r.ok) throw new Error(t('Photo upload failed')); });
      fileInput.value = '';
      document.getElementById('profile-photo-preview').src = `/users/${currentUser.id}/photo?` + Date.now();
    }

    if (emailChanged && !currentUser.email_verified) {
      toast(t("Profile saved — check your new email to verify it. You'll need to confirm it before logging in again."));
    } else {
      toast(t('Profile saved'));
    }
  } catch (e) { toast(e.message, 'error'); }
}

function previewProfilePhoto(input) {
  const file = input.files[0];
  if (!file) return;
  document.getElementById('profile-photo-preview').src = URL.createObjectURL(file);
}

async function deleteProfilePhoto() {
  if (!confirm(t('Remove your profile photo?'))) return;
  try {
    await apiFetch('/auth/photo', { method: 'DELETE' });
    toast(t('Photo removed'));
    document.getElementById('profile-photo-preview').src = `/users/${currentUser.id}/photo?` + Date.now();
  } catch (e) { toast(e.message, 'error'); }
}

async function saveGmrsCallsign() {
  const gmrs_callsign = document.getElementById('profile-gmrs-callsign').value.trim().toUpperCase() || null;
  try {
    currentUser = await apiFetch('/auth/gmrs-callsign', { method: 'PATCH', body: JSON.stringify({ gmrs_callsign }) });
    document.getElementById('profile-gmrs-callsign').value = currentUser.gmrs_callsign || '';
    toast(t('Profile saved'));
  } catch (e) { toast(e.message, 'error'); }
}

// ============================================================
// API TOKENS
// ============================================================
let _lastCreatedToken = null;

async function loadApiTokens() {
  const el = document.getElementById('tokens-list');
  if (!el) return;
  try {
    const tokens = await apiFetch('/auth/tokens');
    if (!tokens || tokens.length === 0) {
      el.innerHTML = `<p style="color:var(--text-muted);font-size:13px;margin:0">${t('No API tokens yet. Create one above.')}</p>`;
      return;
    }
    el.innerHTML = tokens.map(tok => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--lc-border)">
        <div style="flex:1">
          <div style="font-size:13px;font-weight:600">${esc(tok.name)}</div>
          <div style="font-size:11px;color:var(--text-muted)">
            ${t('Created')} ${fmt(tok.created_at)}
            ${tok.last_used_at ? ' · ' + t('Last used') + ' ' + fmt(tok.last_used_at) : ' · ' + t('Never used')}
          </div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="copyRelayScriptForToken(${tok.id}, '${esc(tok.name)}')" title="${t('Download relay script pre-configured for this token')}">📥 ${t('Relay Script')}</button>
        <button class="btn btn-danger btn-sm" onclick="deleteApiToken(${tok.id})">${t('Revoke')}</button>
      </div>
    `).join('');
  } catch(e) {
    el.innerHTML = `<p style="color:var(--lc-red);font-size:13px;margin:0">${t('Error:')} ${esc(e.message)}</p>`;
  }
}

async function createApiToken() {
  const name = document.getElementById('new-token-name').value.trim();
  if (!name) { toast(t('Enter a label for the token'), 'error'); return; }
  try {
    const result = await apiFetch('/auth/tokens', { method: 'POST', body: JSON.stringify({ name }) });
    _lastCreatedToken = result.token;
    document.getElementById('new-token-value').textContent = result.token;
    document.getElementById('new-token-reveal').style.display = '';
    document.getElementById('new-token-name').value = '';
    toast(t('Token created — copy it now!'));
    loadApiTokens();
  } catch(e) {
    toast(t('Error:') + ' ' + e.message, 'error');
  }
}

function copyNewToken() {
  if (!_lastCreatedToken) return;
  navigator.clipboard.writeText(_lastCreatedToken).then(() => toast(t('Token copied to clipboard')));
}

async function deleteApiToken(id) {
  if (!confirm(t('Revoke this token? Any scripts using it will stop working immediately.'))) return;
  try {
    await apiFetch(`/auth/tokens/${id}`, { method: 'DELETE' });
    toast(t('Token revoked'));
    loadApiTokens();
  } catch(e) {
    toast(t('Error:') + ' ' + e.message, 'error');
  }
}

function copyRelayScriptForToken(tokenId, tokenName) {
  // Prompt user to generate a token — we can't retrieve the raw value, so we
  // direct them to create a fresh one and use downloadRelayScript() which will
  // prompt for the token value.
  toast(t("Generate a new token above, copy it, then use the 📥 Relay Script button in the net's DMR config with that token."), 'info');
}

function downloadRelayScript() {
  const src  = currentDmrConfig ? currentDmrConfig.source_type : 'wpsd';
  const url  = currentDmrConfig ? (currentDmrConfig.hotspot_url || 'http://localhost') : 'http://localhost';
  const netId = editNetId || currentNetId || 0;
  const backend = window.location.origin;

  const script = `#!/usr/bin/env python3
"""
Digital Voice Relay Script for NetControl Online
Fetches last-heard data from your local hotspot and pushes it to the net tracker,
bypassing browser CORS restrictions entirely. Covers DMR, D-Star, YSF, NXDN, P25,
and M17 -- a WPSD/Pi-Star hotspot reports whichever mode(s) it hears, tagged
per-entry, so this pushes everything and NetControl Online filters by the mode
configured on the net.

Requirements: pip install requests   (or: sudo apt install python3-requests)

Setup:
  1. Go to the NetControl Online → 🪙 API Tokens page and create a token labelled
     something like "Digital Voice Relay - shack Pi".  Copy the token (shown only once).
  2. Paste the token into API_TOKEN below.
  3. Run: python3 dmr_relay.py
  4. Keep it running during the net (Ctrl+C to stop).
  5. Optionally start at boot on the Pi: @reboot python3 /path/to/dmr_relay.py &
"""
import time, sys, json
try:
    import requests
except ImportError:
    sys.exit("Install requests first:  pip install requests  (or: sudo apt install python3-requests)")

# ── Configuration ──────────────────────────────────────────────
BACKEND   = "${backend}"
NET_ID    = ${netId}
API_TOKEN = "nt_PASTE_YOUR_TOKEN_HERE"   # from 🪙 API Tokens page
SOURCE    = "${src}"                      # wpsd | pistar | brandmeister
HOTSPOT   = "${url}"                     # hotspot base URL (http://localhost if running on the Pi)
INTERVAL  = 30                            # seconds between refreshes
# ───────────────────────────────────────────────────────────────

# Real WPSD/Pi-Star mode strings -> canonical short codes (see NetControl
# Online's main.py _HOTSPOT_MODE_MAP -- kept identical here so entries land
# in the right mode bucket server-side).
HOTSPOT_MODE_MAP = {"D-Star": "dstar", "YSF": "ysf", "P25": "p25", "NXDN": "nxdn", "M17": "m17"}

def fetch_hotspot():
    if SOURCE == "pistar":
        # Real classic Pi-Star endpoint: /api/last_heard.php?num_transmissions=N
        base = HOTSPOT.rstrip("/")
        url = base if base.endswith(".php") else base + "/api/last_heard.php"
        r = requests.get(url, params={"num_transmissions": 30}, timeout=5)
    elif SOURCE == "brandmeister":
        tg = int(HOTSPOT) if HOTSPOT.isdigit() else 0
        r = requests.get("https://api.brandmeister.network/v2/talkgroup/rx/",
                         params={"talkgroup": tg, "limit": 30}, timeout=10)
    else:  # wpsd -- real endpoint: /api?limit=N
        base = HOTSPOT.rstrip("/")
        url = base if "/api" in base else base + "/api/"
        r = requests.get(url, params={"limit": 30}, timeout=5)
    r.raise_for_status()
    raw = r.json()
    return raw if isinstance(raw, list) else []

def normalize(e):
    if SOURCE == "brandmeister":
        return {
            "callsign":   (e.get("callsign") or "").upper() or None,
            "dmr_id":     str(e.get("SourceID") or "") or None,
            "name":       e.get("sourceName") or None,
            "talk_group": str(e.get("DestinationID") or "") or None,
            "timeslot":   f"TS{e['slot']}" if e.get("slot") else None,
            "region":     e.get("sourceState") or e.get("sourceCountry") or None,
            "heard_at":   e.get("start") or None,
            "duration":   str(e["duration"]) if e.get("duration") else None,
            "mode":       "dmr",
        }
    # wpsd/pistar: real field shape is {time_utc, mode, callsign, name,
    # callsign_suffix, target, src, duration} -- no top-level slot/dst/
    # country/start keys exist, and there's no region data at all.
    raw_mode = str(e.get("mode") or "").strip()
    if raw_mode == "POCSAG":
        return None  # paging, not a voice check-in concern
    mode, timeslot = None, None
    if raw_mode.startswith("DMR"):
        mode = "dmr"
        ts = raw_mode.replace("DMR Slot", "").strip()
        if raw_mode.startswith("DMR Slot") and ts:
            timeslot = f"TS{ts}"
    else:
        mode = HOTSPOT_MODE_MAP.get(raw_mode)
    return {
        "callsign":   (e.get("callsign") or "").upper() or None,
        "dmr_id":     str(e.get("callsign_suffix") or "") or None,
        "name":       e.get("name") or None,
        "talk_group": str(e.get("target") or "") or None,
        "timeslot":   timeslot,
        "region":     None,
        "heard_at":   e.get("time_utc") or None,
        "duration":   str(e["duration"]) if e.get("duration") else None,
        "mode":       mode,
    }

print(f"Digital voice relay started — pushing to {BACKEND}/nets/{NET_ID}/dmr/push every {INTERVAL}s")
print("Press Ctrl+C to stop.\\n")

while True:
    try:
        raw     = fetch_hotspot()
        entries = [n for n in (normalize(e) for e in raw) if n and n.get("callsign")]
        r = requests.post(
            f"{BACKEND}/nets/{NET_ID}/dmr/push",
            json={"entries": entries},
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=10,
        )
        if r.status_code == 401:
            print(f"[{time.strftime('%H:%M:%S')}] Auth failed — check API_TOKEN in script")
            sys.exit(1)
        r.raise_for_status()
        print(f"[{time.strftime('%H:%M:%S')}] Pushed {len(entries)} entries")
    except KeyboardInterrupt:
        print("\\nStopped.")
        sys.exit(0)
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] Error: {exc}")
    time.sleep(INTERVAL)
`;

  const blob = new Blob([script], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'dmr_relay.py';
  a.click();
  URL.revokeObjectURL(a.href);
  toast(t('dmr_relay.py downloaded — create an API token under 🪙 API Tokens and paste it into the script.'));
}

onEnter(['new-token-name'], createApiToken);
onEnter(['profile-gmrs-callsign'], saveGmrsCallsign);
onEnter(['profile-name', 'profile-email', 'profile-callsign', 'profile-phone'], saveProfile);

