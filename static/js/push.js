// ============================================================
// WEB PUSH NOTIFICATIONS (issue follow-up)
// ============================================================
// Account-page-only (tokens.html) -- a one-time, permission-driven
// preference, same reasoning as why profile/photo/GMRS callsign live here
// rather than in the sidebar. Subscribing/unsubscribing talks to
// POST/DELETE /push/subscribe (routers/push.py); the actual reminder sends
// are driven entirely server-side by send_reminders.py, not from here.

// Standard helper for converting a VAPID public key (base64url string) into
// the Uint8Array pushManager.subscribe() expects.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) outputArray[i] = rawData.charCodeAt(i);
  return outputArray;
}

let _pushVapidKey = null;

async function initPushToggle() {
  const card = document.getElementById('push-card');
  const toggle = document.getElementById('push-toggle');
  const testBtn = document.getElementById('push-test-btn');
  if (!card) return;

  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    card.style.display = 'none';
    return;
  }

  try {
    const res = await fetch('/push/vapid-public-key');
    if (!res.ok) { card.style.display = 'none'; return; }
    _pushVapidKey = (await res.json()).public_key;
  } catch {
    card.style.display = 'none';
    return;
  }

  card.style.display = '';

  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    toggle.checked = !!sub;
    testBtn.style.display = sub ? '' : 'none';
  } catch {
    // Leave the toggle at its default (off) -- e.g. no service worker
    // controlling this page yet on a very first visit.
  }
}

async function togglePushNotifications(enabled) {
  const toggle = document.getElementById('push-toggle');
  const testBtn = document.getElementById('push-test-btn');
  try {
    const reg = await navigator.serviceWorker.ready;
    if (enabled) {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        toggle.checked = false;
        toast(t('Notifications permission was not granted'), 'error');
        return;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(_pushVapidKey),
      });
      await apiFetch('/push/subscribe', { method: 'POST', body: JSON.stringify(sub.toJSON()) });
      testBtn.style.display = '';
      toast(t('Push notifications enabled'), 'success');
    } else {
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await apiFetch('/push/subscribe', { method: 'DELETE', body: JSON.stringify({ endpoint: sub.endpoint }) });
        await sub.unsubscribe();
      }
      testBtn.style.display = 'none';
      toast(t('Push notifications disabled'), 'success');
    }
  } catch (e) {
    toggle.checked = !enabled;
    toast(e.message, 'error');
  }
}

async function testPushNotification() {
  try {
    await apiFetch('/push/test', { method: 'POST' });
    toast(t('Test notification sent — check for it now'), 'success');
  } catch (e) { toast(e.message, 'error'); }
}

initPushToggle();
