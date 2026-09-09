// ============================================================
// FEDIVERSE INTERACTION CLIENT (issue follow-up) — reply to/Like
// interactions received on the org's own Fediverse posts, and compose
// ad-hoc posts. Available to an org admin or a fediverse_operator (see
// routers/fediverse.py's require_fediverse_access); anyone else gets the
// #fediverse-not-available card instead, same 403-caught-into-a-friendly-
// empty-state precedent as other role-gated pages.
//
// Offline handling (issue follow-up): unlike a check-in, every action here
// (reply/Like/post) is a live call out to a remote Fediverse server with
// nothing sensible to queue for later -- so rather than let a reply/Like/
// post silently fail with a network-error toast, the whole interactive
// section is swapped for #fediverse-offline whenever there's no
// connection. Checked THREE ways, since navigator.onLine only tracks "is
// there a network interface at all" and is well known to still read true
// on a WiFi/hotspot connection with no real backhaul: (1) up front, so
// opening the page while offline shows the right message instead of a
// misleading "not available" permission-style one; (2) live, via the
// 'online'/'offline' listeners below (same "plain events, no polling"
// convention static/js/app.js's #app-offline-banner and checkins.js's
// offline queue already use), so losing/regaining connectivity mid-
// session swaps the view immediately; (3) reactively, in
// loadFediverseClient()'s and every action's own catch block, treating a
// TypeError (a fetch() that never got a response at all -- the "lying
// WiFi" case navigator.onLine misses) the same as a detected offline
// state, same distinction auth.js's resolveCurrentUserOffline() already
// makes for the page's own login gate.
//
// IMPORTANT: an interaction's content_html (a reply someone left on our
// post) is PLAIN TEXT, already sanitized server-side
// (activitypub_delivery.sanitize_remote_content — see its own docstring
// for why: it originates from an untrusted remote Fediverse server) —
// always render it via esc(), never as raw innerHTML. A POST's own
// content_html (our own outgoing posts, "Recent Posts" below) is real
// HTML we generated ourselves and is safe to render directly, same as
// every other page that shows an ActivityPubPost's content.
// ============================================================

let fediverseOrgId = null;

async function loadFediverseClient() {
  const offline = document.getElementById('fediverse-offline');
  const notAvailable = document.getElementById('fediverse-not-available');
  const content = document.getElementById('fediverse-content');

  if (!navigator.onLine) {
    offline.style.display = '';
    notAvailable.style.display = 'none';
    content.style.display = 'none';
    return;
  }
  offline.style.display = 'none';

  // currentUser isn't set until initPage()'s own auth check resolves -- the
  // 'online'/'offline' listeners below are registered at script load, so a
  // stray event firing in that brief window (unusual, but not impossible)
  // should just no-op rather than throw; initPage() calls this again once
  // currentUser is actually populated.
  if (!currentUser) return;

  fediverseOrgId = currentUser.current_org_id;
  if (!fediverseOrgId) {
    notAvailable.style.display = '';
    content.style.display = 'none';
    return;
  }
  try {
    const [interactions, posts] = await Promise.all([
      apiFetch(`/orgs/${fediverseOrgId}/fediverse/interactions`),
      apiFetch(`/orgs/${fediverseOrgId}/fediverse/posts`),
    ]);
    notAvailable.style.display = 'none';
    content.style.display = '';
    renderFediverseInteractions(interactions);
    renderFediversePosts(posts);
  } catch (e) {
    // navigator.onLine only tracks "is there a network interface at all,"
    // not "can I actually reach the server" -- it stays true on a WiFi/
    // hotspot connection with no real backhaul, and (per testing) can even
    // briefly lag the real state right after a fresh navigation. A plain
    // fetch() failure in that situation throws TypeError before apiFetch
    // ever sees a response to reject on, same distinction
    // resolveCurrentUserOffline() (auth.js) already makes -- treat that as
    // "offline" too, not as "no access here," which would otherwise show a
    // misleading permission-denied message for what's actually a
    // connectivity problem.
    if (e instanceof TypeError) {
      offline.style.display = '';
      notAvailable.style.display = 'none';
    } else {
      // A real rejection from the server -- 403 (no access, not an org
      // admin or fediverse_operator here) or 404 (org gone) -- a friendly
      // empty state, not a toast.
      notAvailable.style.display = '';
    }
    content.style.display = 'none';
  }
}

// Re-evaluate immediately on either transition -- going offline mid-
// session swaps live content for the offline card right away rather than
// leaving stale data on screen with buttons that would just fail; coming
// back online re-fetches automatically instead of waiting for a manual
// reload.
window.addEventListener('online', loadFediverseClient);
window.addEventListener('offline', loadFediverseClient);

function renderFediverseInteractions(interactions) {
  const list = document.getElementById('fediverse-interactions-list');
  const empty = document.getElementById('fediverse-interactions-empty');
  if (!interactions.length) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = interactions.map(renderInteractionCard).join('');
}

function renderInteractionCard(i) {
  const who = esc(i.remote_actor_name || i.remote_actor_handle || '?');
  const handle = i.remote_actor_handle ? ` <span class="text-muted" style="font-size:11px">@${esc(i.remote_actor_handle)}</span>` : '';
  const when = esc(new Date(i.received_at).toLocaleString());

  if (i.kind === 'like') {
    return `
    <div class="card" style="margin-bottom:10px">
      <div style="font-size:12px"><strong>${who}</strong>${handle} <span class="text-muted">${t('liked your post')}</span></div>
      <div class="text-muted" style="font-size:11px;margin-top:4px">${when}</div>
    </div>`;
  }

  const likeAction = i.liked_at
    ? `<span class="text-muted" style="font-size:11px" data-i18n="❤️ Liked">${t('❤️ Liked')}</span>`
    : `<button class="btn btn-ghost btn-sm" onclick="likeFediverseInteraction(${i.id})" data-i18n="❤️ Like">${t('❤️ Like')}</button>`;

  return `
  <div class="card" style="margin-bottom:10px">
    <div style="font-size:12px"><strong>${who}</strong>${handle}</div>
    <div style="font-size:13px;margin:6px 0;white-space:pre-wrap">${esc(i.content_html)}</div>
    <div class="text-muted" style="font-size:11px;margin-bottom:8px">${when}</div>
    <div style="display:flex;gap:8px;align-items:center">
      ${likeAction}
      <button class="btn btn-primary btn-sm" onclick="toggleFediverseReplyBox(${i.id})" data-i18n="Reply">${t('Reply')}</button>
    </div>
    <div id="fediverse-reply-box-${i.id}" style="display:none;margin-top:8px">
      <textarea class="form-control" id="fediverse-reply-text-${i.id}" rows="2" placeholder="${esc(t('Write a reply…'))}" style="font-size:13px"></textarea>
      <button class="btn btn-primary btn-sm" style="margin-top:6px" onclick="sendFediverseReply(${i.id})" data-i18n="Send Reply">${t('Send Reply')}</button>
    </div>
  </div>`;
}

function toggleFediverseReplyBox(interactionId) {
  const box = document.getElementById(`fediverse-reply-box-${interactionId}`);
  if (box) box.style.display = box.style.display === 'none' ? '' : 'none';
}

// Shared by every action below (reply/Like/compose) -- a TypeError means
// the request never reached the server at all (same distinction
// resolveCurrentUserOffline() and loadFediverseClient() already make), so
// a plain e.message toast would show Chromium's raw "Failed to fetch"
// rather than something a net control operator in the field can act on.
// Also re-runs loadFediverseClient() in that case so the view swaps to
// the offline card immediately -- connectivity dropped mid-action is
// exactly the moment stale content with dead buttons would otherwise sit
// on screen.
function _handleFediverseActionError(e) {
  if (e instanceof TypeError) {
    toast(t("You're offline — try again once you're connected"), 'error');
    loadFediverseClient();
  } else {
    toast(e.message, 'error');
  }
}

async function sendFediverseReply(interactionId) {
  const textEl = document.getElementById(`fediverse-reply-text-${interactionId}`);
  const contentText = textEl.value.trim();
  if (!contentText) return toast(t('Reply text is required'), 'error');
  try {
    await apiFetch(`/orgs/${fediverseOrgId}/fediverse/interactions/${interactionId}/reply`, {
      method: 'POST', body: JSON.stringify({ content: contentText }),
    });
    toast(t('Reply sent'), 'success');
    await loadFediverseClient();
  } catch (e) { _handleFediverseActionError(e); }
}

async function likeFediverseInteraction(interactionId) {
  try {
    await apiFetch(`/orgs/${fediverseOrgId}/fediverse/interactions/${interactionId}/like`, { method: 'POST' });
    toast(t('Liked'), 'success');
    await loadFediverseClient();
  } catch (e) { _handleFediverseActionError(e); }
}

function renderFediversePosts(posts) {
  const list = document.getElementById('fediverse-posts-list');
  const empty = document.getElementById('fediverse-posts-empty');
  if (!posts.length) {
    list.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  const KIND_LABEL = { start: '📡', end: '🏁', reply: '↩️', manual: '📝' };
  list.innerHTML = posts.map(p => `
    <div class="card" style="margin-bottom:10px">
      <div class="text-muted" style="font-size:11px;margin-bottom:4px">${KIND_LABEL[p.kind] || '📢'} ${esc(new Date(p.published_at).toLocaleString())}</div>
      <div style="font-size:13px">${p.content_html}</div>
    </div>`).join('');
}

async function postFediverseCompose() {
  const textEl = document.getElementById('fediverse-compose-text');
  const contentText = textEl.value.trim();
  if (!contentText) return toast(t('Post text is required'), 'error');
  try {
    await apiFetch(`/orgs/${fediverseOrgId}/fediverse/posts`, {
      method: 'POST', body: JSON.stringify({ content: contentText }),
    });
    textEl.value = '';
    toast(t('Posted'), 'success');
    await loadFediverseClient();
  } catch (e) { _handleFediverseActionError(e); }
}
