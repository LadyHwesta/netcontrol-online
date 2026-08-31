"""
Runtime UI translation via argos-translate (opt-in, TRANSLATION_ENABLED).

Translation memory, not a key file: the English source text itself is the
cache key (hashed), the same principle gettext/_() has used for decades --
no separate namespace of invented translation keys to keep hand-synced
with the actual wording. One primitive (translate_cached) serves both UI
chrome strings (static/js/i18n.js's t()) and on-demand user-content
translation (net scripts, welcome messages, announcements) -- they're
literally the same operation.

argos-translate itself is imported lazily, only inside _translate_sync,
and only ever called through asyncio.to_thread -- it's a real blocking
neural-MT call (ctranslate2/spacy/stanza under the hood), and this is an
async app with no prior blocking calls in its request path. Getting this
wrong would stall every other user's request while one translation runs.
"""

import asyncio
import hashlib
import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import SessionLocal, get_db
from models import EnabledLanguage, OrgEnabledLanguage, TranslationCache, User
from routers.deps import get_current_user, get_current_user_optional, limiter
from routers.helpers import STATIC_DIR

router = APIRouter()
log = logging.getLogger("translation")

TRANSLATION_ENABLED = os.getenv("TRANSLATION_ENABLED", "").strip().lower() == "true"

KNOWN_STRINGS_PATH = STATIC_DIR / "i18n" / "known_strings.json"


def _translation_configured() -> bool:
    return TRANSLATION_ENABLED


def _cache_key(target_lang: str, source_text: str, context: str = "") -> str:
    return hashlib.sha256(f"{target_lang}|{context}|{source_text}".encode("utf-8")).hexdigest()


def _translate_sync(source_text: str, target_lang: str) -> str:
    """Blocking call — only ever invoked via asyncio.to_thread. Lazy import
    + fail-closed on ImportError, same pattern as _verify_altcha in
    routers/helpers.py for the optional altcha package."""
    try:
        import argostranslate.translate as at_translate
    except ImportError:
        log.error("TRANSLATION_ENABLED=true but argostranslate isn't installed — pip install argostranslate")
        return source_text
    try:
        return at_translate.translate(source_text, "en", target_lang)
    except Exception:
        log.exception("argos-translate failed translating to %s", target_lang)
        return source_text


def _install_model_sync(target_lang: str) -> None:
    """Blocking call — downloads/installs the en->target_lang argos-translate
    package if not already installed. Only invoked via asyncio.to_thread."""
    import argostranslate.package as at_package

    at_package.update_package_index()
    available = at_package.get_available_packages()
    pkg = next((p for p in available if p.from_code == "en" and p.to_code == target_lang), None)
    if pkg is None:
        raise ValueError(f"No argos-translate package found for en → {target_lang}")
    download_path = pkg.download()
    at_package.install_from_path(download_path)


async def translate_cached(db: AsyncSession, source_text: str, target_lang: str, context: str = "") -> str:
    """The one primitive both UI-chrome translation and on-demand
    user-content translation go through. Returns source_text unchanged for
    English or blank input, and whenever translation isn't configured at
    all -- always a safe fallback, never a raw error surfaced to a caller
    just trying to render a page."""
    if target_lang == "en" or not source_text or not source_text.strip():
        return source_text
    if not _translation_configured():
        return source_text

    key = _cache_key(target_lang, source_text, context)
    existing = (await db.execute(select(TranslationCache).filter(TranslationCache.cache_key == key))).scalar_one_or_none()
    if existing:
        return existing.translated_text

    translated = await asyncio.to_thread(_translate_sync, source_text, target_lang)
    db.add(TranslationCache(
        cache_key=key, target_lang=target_lang, source_text=source_text, translated_text=translated,
    ))
    try:
        await db.commit()
    except Exception:
        # Another concurrent request may have already cached the same text —
        # not worth failing this response over, just re-read what's there.
        await db.rollback()
        existing = (await db.execute(select(TranslationCache).filter(TranslationCache.cache_key == key))).scalar_one_or_none()
        if existing:
            return existing.translated_text
    return translated


def _known_strings() -> list[str]:
    """Best-effort read of the dev-maintained strings manifest (see
    extract_i18n_strings.py at repo root). Not a runtime dependency for
    correctness -- an admin's bulk pre-translate simply covers less if this
    file is stale or missing."""
    try:
        with open(KNOWN_STRINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def run_enable_language_job(code: str, display_name: str) -> None:
    """Background job kicked off by POST /admin/languages: installs the
    argos-translate model for this language, then bulk-pretranslates the
    known-strings manifest so real visitors rarely hit a cache miss. Opens
    its own DB session since the request's own session is long closed by
    the time a background task runs."""
    async with SessionLocal() as db:
        row = (await db.execute(select(EnabledLanguage).filter(EnabledLanguage.code == code))).scalar_one_or_none()
        if row is None:
            return
        row.model_status = "installing"
        await db.commit()

        try:
            await asyncio.to_thread(_install_model_sync, code)
            for text in _known_strings():
                await translate_cached(db, text, code)
            row.model_status = "ready"
            row.error_message = None
        except Exception as exc:
            log.exception("Failed enabling language %s", code)
            row.model_status = "error"
            row.error_message = str(exc)[:500]
        await db.commit()


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

class LanguageOut(BaseModel):
    code: str
    display_name: str

    model_config = {"from_attributes": True}


class TranslateBatchRequest(BaseModel):
    lang: str
    texts: list[str]


class TranslateRequest(BaseModel):
    text: str
    target_lang: str


class TranslateResponse(BaseModel):
    translated_text: str


@router.get("/i18n/languages", response_model=list[LanguageOut])
async def list_enabled_languages(
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Languages the language switcher / browser-language auto-detect may
    offer. Not gated on _translation_configured() -- an empty list is
    already the correct no-op response when translation is off.

    Per-org isolation (multi-tenancy follow-up): a logged-in user only sees
    languages their own current org has opted into (OrgEnabledLanguage). An
    anonymous caller -- the login screen, or a public page before any org
    context exists -- instead sees the union of every ready language any
    org on this server has enabled; there's no org to scope by yet, and
    nothing sensitive is exposed by a visitor knowing a language exists."""
    q = select(EnabledLanguage).filter(EnabledLanguage.model_status == "ready")
    if user and user.current_org_id:
        q = q.join(OrgEnabledLanguage, OrgEnabledLanguage.code == EnabledLanguage.code).filter(
            OrgEnabledLanguage.org_id == user.current_org_id
        )
    else:
        q = q.join(OrgEnabledLanguage, OrgEnabledLanguage.code == EnabledLanguage.code).distinct()
    return (await db.execute(q.order_by(EnabledLanguage.display_name))).scalars().all()


@router.get("/i18n/{lang}")
async def get_language_cache(lang: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """The entire cached source_text -> translated_text map for one
    language, for a single bulk client-side preload (static/js/i18n.js)
    instead of a round trip per string."""
    rows = (await db.execute(select(TranslationCache).filter(TranslationCache.target_lang == lang))).scalars().all()
    return {r.source_text: r.translated_text for r in rows}


@router.post("/i18n/translate-batch")
@limiter.limit("20/minute")
async def translate_batch(request: Request, data: TranslateBatchRequest, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Self-healing path: the frontend calls this once per page load with
    whatever strings it had to fall back to English for. Also reused by the
    admin bulk pre-translate job via translate_cached directly (no need to
    round-trip through HTTP for that case)."""
    if not _translation_configured():
        return {t: t for t in data.texts}
    result = {}
    for text in data.texts:
        result[text] = await translate_cached(db, text, data.lang)
    return result


@router.post("/translate", response_model=TranslateResponse)
async def translate_content(
    data: TranslateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On-demand translation of user content (net scripts, welcome
    messages, announcements) -- a preview the caller displays, never an
    automatic overwrite of the original text."""
    if not _translation_configured():
        raise HTTPException(503, "Translation isn't configured on this server")
    translated = await translate_cached(db, data.text, data.target_lang)
    return TranslateResponse(translated_text=translated)
