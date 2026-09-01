// ============================================================
// SERVICE WORKER — app-shell offline cache + background check-in sync
// ============================================================
// Two separate jobs, kept deliberately apart:
//   1. Precache index.html's own asset shell so the app still loads with no
//      connection (issue #9's "offline basics"), for the exact URLs listed
//      below only — every other request (all API calls, every other page)
//      passes straight to the network untouched. Never serve a stale
//      cached API response.
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
//   2. Best-effort Background Sync for queued check-ins (see
//      static/js/offline-queue.js, imported below) — a bonus chance to
//      flush if the tab is backgrounded/closed while offline. NOT the
//      primary guarantee: Safari/iOS has no Background Sync API at all, so
//      the foreground online-listener in checkins.js is what actually
//      works everywhere. This is extra credit on browsers that support it.
//
// No build step in this repo — CACHE_NAME and PRECACHE_URLS below are
// hand-maintained. Bump CACHE_NAME whenever PRECACHE_URLS changes, and keep
// the ?v=N query strings here in sync with the versions index.html actually
// loads (see index.html's own <script>/<link> tags) — a stale precache
// entry now only matters for the offline-fallback case, but it's worth
// keeping current regardless.

const CACHE_NAME = 'netcontrol-online-shell-v65';

const PRECACHE_URLS = [
  '/',
  '/static/app.css?v=22',
  '/static/vendor/leaflet/leaflet.css?v=1',
  '/static/js/state.js?v=24',
  '/static/js/i18n.js?v=4',
  '/static/js/utils.js?v=21',
  '/static/js/auth.js?v=30',
  '/static/js/theme.js?v=1',
  '/static/js/views.js?v=19',
  '/static/js/branding.js?v=24',
  '/static/js/report.js?v=21',
  '/static/js/nets.js?v=32',
  '/static/js/sessions.js?v=31',
  '/static/js/checkins.js?v=39',
  '/static/js/history.js?v=22',
  '/static/js/admin.js?v=40',
  '/static/js/schedules.js?v=23',
  '/static/js/tokens.js?v=23',
  '/static/js/dmr.js?v=21',
  '/static/js/aprs.js?v=40',
  '/static/js/aprs-map.js?v=41',
  '/static/vendor/leaflet/leaflet.js?v=1',
  '/static/js/app.js?v=26',
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
  if (!PRECACHE_URLS.includes(key)) return;  // not app shell — network only, untouched

  event.respondWith(
    fetch(event.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      return res;
    }).catch(() => caches.match(event.request))
  );
});

// ── Background Sync (best-effort — see file header) ──────────────────────
importScripts('/static/js/offline-queue.js?v=2');

self.addEventListener('sync', event => {
  if (event.tag === 'sync-checkins') {
    event.waitUntil(flushCheckinQueue());
  }
});
