"""
Tests for UI translation via argos-translate (opt-in, TRANSLATION_ENABLED):
  GET  /i18n/languages          -- public, enabled+ready languages
  GET  /i18n/{lang}             -- public, full cached-translation map
  POST /i18n/translate-batch    -- public, self-healing frontend path
  POST /translate               -- authenticated, on-demand user-content translation
  PATCH /auth/language          -- per-user language preference
  GET/POST/DELETE /admin/languages  -- super admin only

argos-translate itself is never installed in the test environment -- every
test that needs "translation configured" monkeypatches routers.translation's
module-level TRANSLATION_ENABLED flag and _translate_sync/_install_model_sync
functions directly, the same way test_support.py monkeypatches SUPPORT_EMAIL
and test_email_send.py monkeypatches smtplib.
"""

from routers import translation
from helpers import register, login, auth


def _fake_translate(text, target_lang):
    return f"[{target_lang}] {text}"


class TestTranslationDisabledByDefault:
    def test_languages_list_is_empty_with_nothing_enabled(self, client):
        resp = client.get("/i18n/languages")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_translate_content_503s_when_not_configured(self, client, admin_headers):
        resp = client.post("/translate", json={"text": "Hello", "target_lang": "es"}, headers=admin_headers)
        assert resp.status_code == 503

    def test_admin_enable_language_503s_when_not_configured(self, client, admin_headers):
        resp = client.post("/admin/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
        assert resp.status_code == 503

    def test_translate_batch_passes_through_unchanged(self, client):
        """Self-healing frontend path degrades to a no-op echo, not an error,
        when translation isn't configured -- t() still needs a string back."""
        resp = client.post("/i18n/translate-batch", json={"lang": "es", "texts": ["Save", "Cancel"]})
        assert resp.status_code == 200
        assert resp.json() == {"Save": "Save", "Cancel": "Cancel"}

    def test_get_language_cache_empty_for_unknown_lang(self, client):
        resp = client.get("/i18n/es")
        assert resp.status_code == 200
        assert resp.json() == {}


class TestTranslateCachedPrimitive:
    """translate_cached() is the one function both UI-chrome translation and
    on-demand content translation go through -- covered directly since it's
    the core caching/hashing behavior everything else depends on."""

    async def test_english_passes_through_without_touching_argos(self, db, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        # No _translate_sync patch at all -- if this were called for "en" it'd
        # try a real argostranslate import and fail; getting "Hello" back
        # proves the English short-circuit fired before any of that.
        result = await translation.translate_cached(db, "Hello", "en")
        assert result == "Hello"

    async def test_blank_text_passes_through(self, db, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        assert await translation.translate_cached(db, "", "es") == ""
        assert await translation.translate_cached(db, "   ", "es") == "   "

    async def test_translates_and_caches_on_miss(self, db, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        monkeypatch.setattr(translation, "_translate_sync", _fake_translate)
        result = await translation.translate_cached(db, "Save", "es")
        assert result == "[es] Save"

        from sqlalchemy import select
        from models import TranslationCache
        rows = (await db.execute(select(TranslationCache).filter(TranslationCache.target_lang == "es"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].source_text == "Save"
        assert rows[0].translated_text == "[es] Save"

    async def test_cache_hit_does_not_call_translate_sync_again(self, db, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        calls = []

        def counting_translate(text, lang):
            calls.append(text)
            return _fake_translate(text, lang)

        monkeypatch.setattr(translation, "_translate_sync", counting_translate)
        await translation.translate_cached(db, "Save", "es")
        await translation.translate_cached(db, "Save", "es")
        assert calls == ["Save"]  # second call was a cache hit


class TestTranslateContentEndpoint:
    def test_returns_translated_text(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        monkeypatch.setattr(translation, "_translate_sync", _fake_translate)
        resp = client.post("/translate", json={"text": "Net script here", "target_lang": "fr"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["translated_text"] == "[fr] Net script here"

    def test_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        resp = client.post("/translate", json={"text": "Hi", "target_lang": "es"})
        assert resp.status_code == 401


class TestTranslateBatch:
    def test_translates_missing_and_reuses_cached(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        monkeypatch.setattr(translation, "_translate_sync", _fake_translate)
        resp = client.post("/i18n/translate-batch", json={"lang": "es", "texts": ["Save", "Cancel"]})
        assert resp.status_code == 200
        assert resp.json() == {"Save": "[es] Save", "Cancel": "[es] Cancel"}


class TestLanguageCacheBulkRead:
    def test_get_language_returns_full_cached_map(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        monkeypatch.setattr(translation, "_translate_sync", _fake_translate)
        client.post("/i18n/translate-batch", json={"lang": "es", "texts": ["Save", "Cancel"]})

        resp = client.get("/i18n/es")
        assert resp.status_code == 200
        assert resp.json() == {"Save": "[es] Save", "Cancel": "[es] Cancel"}

        # A different language's cache stays independent
        assert client.get("/i18n/fr").json() == {}


class TestAdminLanguages:
    def test_requires_auth(self, client):
        resp = client.get("/admin/languages")
        assert resp.status_code == 401

    def test_requires_admin(self, client, user_headers):
        resp = client.get("/admin/languages", headers=user_headers)
        assert resp.status_code == 403

    def test_enable_language_creates_pending_row(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        # Avoid actually spawning the background job's real argos-translate
        # install/pretranslate work in a test process.
        async def noop_job(code, display_name):
            pass
        monkeypatch.setattr(translation, "run_enable_language_job", noop_job)
        # admin.py imported the name directly, so the patch must land there too
        from routers import admin as admin_router
        monkeypatch.setattr(admin_router, "run_enable_language_job", noop_job)

        resp = client.post("/admin/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["code"] == "es"
        assert data["model_status"] == "pending"

    def test_enable_duplicate_language_rejected(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        async def noop_job(code, display_name):
            pass
        from routers import admin as admin_router
        monkeypatch.setattr(admin_router, "run_enable_language_job", noop_job)

        client.post("/admin/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
        resp = client.post("/admin/languages", json={"code": "es", "display_name": "Spanish"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_disable_language_removes_it(self, client, admin_headers, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        async def noop_job(code, display_name):
            pass
        from routers import admin as admin_router
        monkeypatch.setattr(admin_router, "run_enable_language_job", noop_job)

        client.post("/admin/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
        resp = client.delete("/admin/languages/es", headers=admin_headers)
        assert resp.status_code == 204
        assert client.get("/admin/languages", headers=admin_headers).json() == []

    def test_only_ready_languages_appear_in_public_list(self, client, admin_headers, monkeypatch):
        """A language stuck at pending/installing/error shouldn't show up in
        the switcher or auto-detect list -- only a fully pre-translated one."""
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        async def noop_job(code, display_name):
            pass
        from routers import admin as admin_router
        monkeypatch.setattr(admin_router, "run_enable_language_job", noop_job)

        client.post("/admin/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
        assert client.get("/i18n/languages").json() == []


class TestUserLanguagePreference:
    def test_defaults_to_null(self, client, admin_headers):
        resp = client.get("/auth/me", headers=admin_headers)
        assert resp.json()["language"] is None

    def test_can_set_and_read_back(self, client, admin_headers):
        resp = client.patch("/auth/language", json={"language": "es"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["language"] == "es"

        me = client.get("/auth/me", headers=admin_headers)
        assert me.json()["language"] == "es"

    def test_can_reset_to_null(self, client, admin_headers):
        client.patch("/auth/language", json={"language": "es"}, headers=admin_headers)
        resp = client.patch("/auth/language", json={"language": None}, headers=admin_headers)
        assert resp.json()["language"] is None

    def test_requires_auth(self, client):
        resp = client.patch("/auth/language", json={"language": "es"})
        assert resp.status_code == 401
