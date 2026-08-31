// ============================================================
// PWA — service worker registration
// ============================================================
// Loaded on every page so the app is installable everywhere, not just the
// check-in flow. No-op, no error surfaced, on browsers without support.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').then(reg => {
      // Browsers only check for a changed /sw.js on their own on a fairly
      // long, heavily-throttled schedule (observed: not even on every fresh
      // navigation) -- confirmed by reproducing this directly: three
      // separate page loads against an already-installed worker never
      // triggered an update check at all, only an explicit call to
      // registration.update() did. Without this, a deploy could sit
      // unnoticed by an already-visited browser far longer than any amount
      // of manual reloading would fix -- this line is what actually makes
      // "reload the page" a reliable way to pick up a new version.
      reg.update();
    }).catch(() => {
      // Registration failing (unsupported context, blocked, etc.) shouldn't
      // interrupt using the app — it's an enhancement, not a requirement.
    });
  });
}
