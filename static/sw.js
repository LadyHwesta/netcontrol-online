// ============================================================
// SERVICE WORKER — app-shell offline cache + background check-in sync
// ============================================================
// Two separate jobs, kept deliberately apart:
//   1. Precache the app shell so it still loads with no connection (issue
//      #9's "offline basics", extended to Chrome's desktop PWA install --
//      same engine, same mechanism, no separate desktop-specific code
//      needed) for the exact URLs listed below only — every other request
//      (all API calls, every other page) passes straight to the network
//      untouched. Never serve a stale cached API response.
//
//      The shell is five pages, not just '/': the SPA itself plus the four
//      standalone pages one click away in its sidebar nav (Admin, Account,
//      Help, Report) — each precached as its own top-level document so
//      opening any of them still works after the tab that installed the
//      service worker goes offline, not only the page that happened to be
//      open at the time. public.html/directory.html are deliberately left
//      out: they're reached fresh via an outside link/QR code, not from the
//      installed app's own nav, and show live net data that's meaningless
//      offline anyway.
//
//      Network-FIRST, falling back to cache only on a failed fetch (e.g.
//      genuinely offline) -- NOT cache-first. This app ships frequent
//      small updates, and cache-first meant a plain reload could silently
//      keep serving an old shell (old JS/CSS, `/` itself) until the new
//      service worker happened to finish its own install/activate cycle in
//      the background -- a real "why do I have to hard-refresh" bug users
//      hit constantly during active development. Network-first fixes that
//      for the common (online) case while still keeping the offline
//      fallback issue #9 exists for.
//
//      A navigation to anything NOT in that five-page shell (a route that's
//      never been visited, or one deliberately left out above) still gets
//      one safety net: on a failed fetch, static/offline.html -- itself
//      precached, so it never depends on the network it's explaining the
//      absence of -- is served instead of the browser's own bare "no
//      internet" page. That page only shows inside an installed app window
//      or a plain offline reload; it's never reachable while online.
//   2. Best-effort Background Sync for queued check-ins (see
//      static/js/offline-queue.js, imported below) — a bonus chance to
//      flush if the tab is backgrounded/closed while offline. NOT the
//      primary guarantee: Safari/iOS has no Background Sync API at all, so
//      the foreground online-listener in checkins.js is what actually
//      works everywhere. This is extra credit on browsers that support it.
//
// No build step in this repo — CACHE_NAME and PRECACHE_URLS below are
// hand-maintained. Bump CACHE_NAME whenever PRECACHE_URLS changes, and keep
// the ?v=N query strings here in sync with the versions actually loaded by
// EVERY precached page's own <script>/<link> tags, not just index.html's --
// admin.html/help.html/tokens.html/report.html each carry their own copies
// of these same tags and drift silently: a shared file's ?v=N bumped only
// in index.html still resolves fine for that page (StaticFiles ignores the
// query string; it's the same file underneath), but a *different* browser
// cache entry than the URL this list intercepts, so the other four pages'
// own asset requests fall through the fetch handler below untouched --
// silently ungated by app-shell caching and unavailable offline. Bumping a
// shared file's version anywhere means bumping it in this list AND in every
// one of the five precached pages' own tags, all to the same number.
const CACHE_NAME = 'netcontrol-online-shell-v94';

const PRECACHE_URLS = [
  '/',
  '/admin',
  '/help',
  '/tokens',
  '/report',
  '/incidents',
  '/assignments',
  '/static/offline.html',
  '/static/app.css?v=30',
  '/static/vendor/leaflet/leaflet.css?v=1',
  '/static/vendor/cropperjs/cropper.min.css?v=1',
  '/static/vendor/cropperjs/cropper.min.js?v=1',
  '/static/js/state.js?v=25',
  '/static/js/i18n.js?v=4',
  '/static/js/utils.js?v=22',
  '/static/js/auth.js?v=38',
  '/static/js/theme.js?v=1',
  '/static/js/views.js?v=19',
  '/static/js/branding.js?v=25',
  '/static/js/report.js?v=21',
  '/static/js/nets.js?v=36',
  '/static/js/sessions.js?v=34',
  '/static/js/checkins.js?v=44',
  '/static/js/history.js?v=23',
  '/static/js/admin.js?v=47',
  '/static/js/schedules.js?v=24',
  '/static/js/tokens.js?v=27',
  '/static/js/push.js?v=1',
  '/static/js/dmr.js?v=21',
  '/static/js/aprs.js?v=40',
  '/static/js/aprs-map.js?v=41',
  '/static/js/evac-zone-map.js?v=2',
  '/static/js/incidents.js?v=2',
  '/static/js/assignments.js?v=1',
  '/static/vendor/leaflet/leaflet.js?v=1',
  '/static/js/app.js?v=28',
  '/static/js/offline-queue.js?v=2',
  '/static/js/pwa.js?v=2',
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-512-maskable.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(names => Promise.all(
        names.filter(name => name !== CACHE_NAME).map(name => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const key = url.pathname + url.search;

  if (PRECACHE_URLS.includes(key)) {
    event.respondWith(
      fetch(event.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
        return res;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // Not part of the app shell — network only, same as always, EXCEPT for a
  // top-level page navigation (an <a href>/typed-URL/bookmark, never an API
  // call — those still pass straight through untouched so app code's own
  // offline handling, e.g. checkins.js's queue, sees the real failure): if
  // the network is unreachable there's no page to show at all, so fall back
  // to the precached offline notice instead of the browser's bare error.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/static/offline.html'))
    );
  }
});

// ── Background Sync (best-effort — see file header) ──────────────────────
importScripts('/static/js/offline-queue.js?v=2');

self.addEventListener('sync', event => {
  if (event.tag === 'sync-checkins') {
    event.waitUntil(flushCheckinQueue());
  }
});

// ── Web push notifications (issue follow-up) ──────────────────────────────
// Payloads are always small reminder JSON ({title, body, url}) sent by
// send_reminders.py / POST /push/test — no rich actions needed here.
self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: 'NetControl Online', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'NetControl Online';
  const options = {
    body: data.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if (client.url.includes(url) && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
