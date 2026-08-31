// ============================================================
// UI TRANSLATION (argos-translate, opt-in) — mirrors theme.js's
// localStorage + DB-sync pattern. t(text) is translation *memory*, not a
// key file: the English text itself is the lookup key, same principle
// gettext/_() has used for decades -- no separate key namespace to invent
// or keep in sync with the actual wording.
// ============================================================
const LANG_STORAGE_KEY = 'nt_lang';

let currentLang = 'en';
let enabledLanguages = [];        // [{code, display_name}] from GET /i18n/languages
let translationCache = {};        // source_text -> translated_text, for currentLang only
let _pendingTranslateTexts = new Set();
let _pendingTranslateTimer = null;

// Synchronous, safe to call from anywhere a string is rendered (including
// inline inside a template literal, same as esc()/fmt()). Falls back to
// the English text itself whenever nothing better is available yet --
// never returns a raw key or blank string.
function t(text) {
  if (!text || currentLang === 'en') return text;
  const cached = translationCache[text];
  if (cached !== undefined) return cached;
  _queueMissingTranslation(text);
  return text;
}

// Collects strings t() had no cached translation for and, shortly after
// the page settles, sends them once as a batch to /i18n/translate-batch.
// The very first time a brand-new string is hit in a given language it
// shows English for that render; every later view is instant from cache
// (and an admin's "enable a language" pre-translation pass keeps this
// rare in practice).
function _queueMissingTranslation(text) {
  _pendingTranslateTexts.add(text);
  clearTimeout(_pendingTranslateTimer);
  _pendingTranslateTimer = setTimeout(_flushMissingTranslations, 500);
}

async function _flushMissingTranslations() {
  if (currentLang === 'en' || _pendingTranslateTexts.size === 0) return;
  const texts = [..._pendingTranslateTexts];
  _pendingTranslateTexts.clear();
  try {
    const result = await apiFetch('/i18n/translate-batch', {
      method: 'POST',
      body: JSON.stringify({ lang: currentLang, texts }),
    });
    Object.assign(translationCache, result);
    translatePage();  // re-render now that these strings are cached -- otherwise they'd
                       // sit translated-but-unshown until the next full page load
  } catch { /* best-effort — next page load will retry via the bulk preload */ }
}

// Applies a language: loads its full cached-translation map in one request
// (GET /i18n/{lang}) rather than round-tripping per string.
async function applyLang(lang) {
  currentLang = lang || 'en';
  if (currentLang === 'en') { translationCache = {}; return; }
  try {
    translationCache = await apiFetch(`/i18n/${encodeURIComponent(currentLang)}`);
  } catch { translationCache = {}; }
}

// Called once currentUser is known (login response or /auth/me) --
// reconciles the DB source of truth with the local cache, same as
// syncThemeFromUser. Falls back to auto-detecting the browser's language
// only when nothing has ever been chosen (no DB value, no localStorage).
async function syncLangFromUser(user) {
  try { enabledLanguages = await apiFetch('/i18n/languages'); } catch { enabledLanguages = []; }
  const enabledCodes = enabledLanguages.map(l => l.code);

  let lang = (user && user.language) || localStorage.getItem(LANG_STORAGE_KEY);
  if (!lang) {
    const browserLang = (navigator.language || '').split('-')[0];
    if (enabledCodes.includes(browserLang)) lang = browserLang;
  }
  if (lang && !enabledCodes.includes(lang)) lang = null;  // a since-disabled language

  localStorage.setItem(LANG_STORAGE_KEY, lang || 'en');
  await applyLang(lang || 'en');
  _updateLangUi();
  translatePage();
}

// Static HTML has no templating layer to route through t() itself, so
// translatable markup is instead marked with data-i18n(-placeholder/-title)
// attributes (the English text lives right there, same as elsewhere) and
// this walks the DOM applying t() to each one. Safe to call multiple
// times -- re-running it just re-applies the current language.
function translatePage() {
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
}

function _updateLangUi() {
  // A class, not a single id -- most pages have one switcher in the app
  // header, but the pre-login auth screen has its own separate instance
  // (no #app header to sit in yet), so more than one can be on a page.
  const optionsHtml = '<option value="en">English</option>' +
    enabledLanguages.map(l => `<option value="${esc(l.code)}">${esc(l.display_name)}</option>`).join('');
  document.querySelectorAll('.lang-select').forEach(sel => {
    sel.innerHTML = optionsHtml;
    sel.value = currentLang;
    // No point showing a switcher with nothing but English to pick from --
    // matches the rest of the app's "hide the control when the feature
    // behind it isn't configured" convention.
    sel.style.display = enabledLanguages.length ? '' : 'none';
  });
  document.querySelectorAll('.i18n-credit').forEach(credit => {
    credit.style.display = currentLang !== 'en' ? '' : 'none';
  });
}

// User picked a new language from the <select> -- persist to DB (if
// logged in), then re-apply.
async function saveLang(lang) {
  localStorage.setItem(LANG_STORAGE_KEY, lang);
  await applyLang(lang);
  if (typeof token !== 'undefined' && token) {
    try {
      const updated = await apiFetch('/auth/language', { method: 'PATCH', body: JSON.stringify({ language: lang === 'en' ? null : lang }) });
      currentUser = updated;
    } catch { /* localStorage already updated -- non-fatal if the PATCH fails */ }
  }
  location.reload();  // simplest correct re-render given how many places read t() during initial page construction
}
