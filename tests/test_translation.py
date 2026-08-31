"""
Tests for UI translation via argos-translate (opt-in, TRANSLATION_ENABLED):
  GET  /i18n/languages          -- public, enabled+ready languages (org-scoped when logged in)
  GET  /i18n/{lang}             -- public, full cached-translation map
  POST /i18n/translate-batch    -- public, self-healing frontend path
  POST /translate               -- authenticated, on-demand user-content translation
  PATCH /auth/language          -- per-user language preference
  GET/POST/DELETE /orgs/{org_id}/languages  -- per-org opt-in, any org admin (multi-tenancy follow-up)
  GET/DELETE /admin/languages               -- server-wide catalog view + hard uninstall, super admin only

Every test that needs a specific "translation configured" state monkeypatches
routers.translation's module-level TRANSLATION_ENABLED flag and
_translate_sync/_install_model_sync functions directly, the same way
test_support.py monkeypatches SUPPORT_EMAIL and test_email_send.py
monkeypatches smtplib -- deliberately never relying on whatever
TRANSLATION_ENABLED happens to be in the ambient .env this process loaded.
That's not hypothetical: this whole suite runs for real (via deploy.sh)
against a server's actual .env, which may well have TRANSLATION_ENABLED=true
and a real argos-translate model installed -- a "disabled" test that only
assumed the env var was unset, rather than forcing it off, silently passed
in development and failed the moment it ran somewhere translation was
genuinely turned on.
"""

import pytest

from routers import translation
from routers import orgs as orgs_router
from helpers import login, auth
from test_organizations import _bootstrap_super_admin, _org_owner


def _create_org(client, super_token, callsign, org_slug, org_name, website="https://example.org"):
    """Founds a new org (approved by the super admin) and returns (org_id, admin_token)."""
    token = _org_owner(client, super_token, callsign, org_slug, org_name, website)
    orgs = client.get("/orgs").json()
    org = next(o for o in orgs if o["slug"] == org_slug)
    return org["id"], token


def _noop_job_patch(monkeypatch):
    """Avoid actually spawning the background job's real argos-translate
    install/pretranslate work in a test process -- both modules that
    imported run_enable_language_job need patching, since each holds its
    own reference to the original function."""
    async def noop_job(code, display_name):
        pass
    monkeypatch.setattr(translation, "run_enable_language_job", noop_job)
    monkeypatch.setattr(orgs_router, "run_enable_language_job", noop_job)


def _fake_translate(text, target_lang):
    return f"[{target_lang}] {text}"


class TestTranslationDisabledByDefault:
    @pytest.fixture(autouse=True)
    def _force_disabled(self, monkeypatch):
        """Every test in this class is specifically about the
        not-configured behavior -- force it off rather than assume it,
        regardless of what this process's own .env happens to have set."""
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", False)

    def test_languages_list_is_empty_with_nothing_enabled(self, client):
        resp = client.get("/i18n/languages")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_translate_content_503s_when_not_configured(self, client, admin_headers):
        resp = client.post("/translate", json={"text": "Hello", "target_lang": "es"}, headers=admin_headers)
        assert resp.status_code == 503

    def test_org_enable_language_503s_when_not_configured(self, client, admin_headers):
        # admin_headers' user is the instance's first-registered super admin,
        # who is also a member of (and admin of) their own default org --
        # require_org_admin lets a super admin act on any org_id anyway.
        me = client.get("/auth/me", headers=admin_headers).json()
        resp = client.post(f"/orgs/{me['current_org_id']}/languages", json={"code": "es", "display_name": "Español"}, headers=admin_headers)
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


class TestOrgLanguages:
    """GET/POST/DELETE /orgs/{org_id}/languages -- any org admin manages
    their own org's enabled languages independently (multi-tenancy
    follow-up), no super admin required."""

    def test_requires_auth(self, client):
        resp = client.get("/orgs/1/languages")
        assert resp.status_code == 401

    def test_requires_org_admin(self, client):
        super_token = _bootstrap_super_admin(client)
        org_id, _admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")
        # A regular (non-admin) member of that same org is rejected
        resp = client.post("/auth/register", json={
            "callsign": "W2A", "name": "Member", "email": "w2a@example.com",
            "password": "testpass123", "org_slug": "org-a",
        })
        assert resp.status_code == 201, resp.text
        client.patch(f"/orgs/{org_id}/members/{resp.json()['id']}/approve", headers=auth(_admin_token))
        member_token = login(client, "W2A")
        resp = client.get(f"/orgs/{org_id}/languages", headers=auth(member_token))
        assert resp.status_code == 403

    def test_enable_creates_pending_catalog_row_and_opts_org_in(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")

        resp = client.post(f"/orgs/{org_id}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_token))
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["code"] == "es"
        assert data["model_status"] == "pending"

    def test_enable_duplicate_for_same_org_rejected(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")

        client.post(f"/orgs/{org_id}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_token))
        resp = client.post(f"/orgs/{org_id}/languages", json={"code": "es", "display_name": "Spanish"}, headers=auth(admin_token))
        assert resp.status_code == 400

    def test_disable_removes_only_this_orgs_opt_in(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")

        client.post(f"/orgs/{org_id}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_token))
        resp = client.delete(f"/orgs/{org_id}/languages/es", headers=auth(admin_token))
        assert resp.status_code == 204
        assert client.get(f"/orgs/{org_id}/languages", headers=auth(admin_token)).json() == []

    def test_second_org_enabling_same_code_reuses_catalog_no_reinstall(self, client, monkeypatch):
        """Org B enabling a code Org A already installed piggybacks on the
        existing catalog row -- no second install job, no re-download."""
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        install_calls = []

        async def counting_job(code, display_name):
            install_calls.append(code)
        monkeypatch.setattr(translation, "run_enable_language_job", counting_job)
        monkeypatch.setattr(orgs_router, "run_enable_language_job", counting_job)

        super_token = _bootstrap_super_admin(client)
        org_a, admin_a = _create_org(client, super_token, "W1A", "org-a", "Org A")
        org_b, admin_b = _create_org(client, super_token, "W1B", "org-b", "Org B")

        resp_a = client.post(f"/orgs/{org_a}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_a))
        assert resp_a.status_code == 201, resp_a.text
        resp_b = client.post(f"/orgs/{org_b}/languages", json={"code": "es", "display_name": "Spanish"}, headers=auth(admin_b))
        assert resp_b.status_code == 201, resp_b.text

        assert install_calls == ["es"]  # only installed once, not per org

    def test_org_a_enabling_language_does_not_affect_org_b(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_a, admin_a = _create_org(client, super_token, "W1A", "org-a", "Org A")
        org_b, admin_b = _create_org(client, super_token, "W1B", "org-b", "Org B")

        client.post(f"/orgs/{org_a}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_a))
        assert client.get(f"/orgs/{org_a}/languages", headers=auth(admin_a)).json() != []
        assert client.get(f"/orgs/{org_b}/languages", headers=auth(admin_b)).json() == []

    def test_only_ready_languages_appear_in_that_orgs_public_list(self, client, monkeypatch):
        """A language stuck at pending/installing/error shouldn't show up in
        the switcher or auto-detect list -- only a fully pre-translated one."""
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_id, admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")

        client.post(f"/orgs/{org_id}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_token))
        assert client.get("/i18n/languages", headers=auth(admin_token)).json() == []

    async def test_ready_language_appears_only_for_its_own_org(self, client, db, monkeypatch):
        """Once a language is actually ready, GET /i18n/languages for a
        logged-in user only offers it if their own current org opted in --
        this is the actual end-user-facing isolation guarantee, not just the
        admin management screen."""
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_a, admin_a = _create_org(client, super_token, "W1A", "org-a", "Org A")
        org_b, admin_b = _create_org(client, super_token, "W1B", "org-b", "Org B")

        client.post(f"/orgs/{org_a}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_a))

        from sqlalchemy import select
        from models import EnabledLanguage
        row = (await db.execute(select(EnabledLanguage).filter(EnabledLanguage.code == "es"))).scalar_one()
        row.model_status = "ready"
        await db.commit()

        assert [l["code"] for l in client.get("/i18n/languages", headers=auth(admin_a)).json()] == ["es"]
        assert client.get("/i18n/languages", headers=auth(admin_b)).json() == []
        # Anonymous (no org context yet, e.g. the login screen) sees the union of any org's ready languages
        assert [l["code"] for l in client.get("/i18n/languages").json()] == ["es"]


class TestAdminLanguageCatalog:
    """GET/DELETE /admin/languages -- the server-wide installed-model
    catalog, super admin only. Enabling for actual use is org-scoped
    (TestOrgLanguages above); this is operational visibility + hard
    uninstall across every org at once."""

    def test_requires_super_admin(self, client):
        super_token = _bootstrap_super_admin(client)
        _org_id, admin_token = _create_org(client, super_token, "W1A", "org-a", "Org A")
        resp = client.get("/admin/languages", headers=auth(admin_token))
        assert resp.status_code == 403

    def test_catalog_reports_org_count_across_orgs(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_a, admin_a = _create_org(client, super_token, "W1A", "org-a", "Org A")
        org_b, admin_b = _create_org(client, super_token, "W1B", "org-b", "Org B")

        client.post(f"/orgs/{org_a}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_a))
        client.post(f"/orgs/{org_b}/languages", json={"code": "es", "display_name": "Spanish"}, headers=auth(admin_b))

        rows = client.get("/admin/languages", headers=auth(super_token)).json()
        assert len(rows) == 1
        assert rows[0]["code"] == "es"
        assert rows[0]["org_count"] == 2

    def test_hard_uninstall_removes_from_every_orgs_list(self, client, monkeypatch):
        monkeypatch.setattr(translation, "TRANSLATION_ENABLED", True)
        _noop_job_patch(monkeypatch)
        super_token = _bootstrap_super_admin(client)
        org_a, admin_a = _create_org(client, super_token, "W1A", "org-a", "Org A")
        org_b, admin_b = _create_org(client, super_token, "W1B", "org-b", "Org B")

        client.post(f"/orgs/{org_a}/languages", json={"code": "es", "display_name": "Español"}, headers=auth(admin_a))
        client.post(f"/orgs/{org_b}/languages", json={"code": "es", "display_name": "Spanish"}, headers=auth(admin_b))

        resp = client.delete("/admin/languages/es", headers=auth(super_token))
        assert resp.status_code == 204
        assert client.get(f"/orgs/{org_a}/languages", headers=auth(admin_a)).json() == []
        assert client.get(f"/orgs/{org_b}/languages", headers=auth(admin_b)).json() == []
        assert client.get("/admin/languages", headers=auth(super_token)).json() == []


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
