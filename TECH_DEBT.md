# Technical Debt

Known issues, shortcuts, and areas for future improvement. Not bugs — the app works — but things worth addressing before this scales or gets wider use.

---

## High Priority

*All high-priority items resolved — see Resolved section below.*

---

## Medium Priority

~~DMR push cache is in-memory only~~ — resolved; see Resolved section.

~~No test suite~~ — resolved; see Resolved section.

~~Migration SQL must be kept in sync manually~~ — resolved; see Resolved section.
~~Single-file frontend (`index.html`)~~ — resolved; see Resolved section.

~~`httpx` imported twice under different names~~ — resolved; see Resolved section.

---

## Low Priority

~~No email verification on registration~~ — resolved; see Resolved section.

~~FCC callsign lookup depends on an external service~~ — resolved; see Resolved section.

~~SQLAlchemy is used synchronously~~ — resolved; see Resolved section.

~~Relay script normalizes WPSD data differently from the backend~~ — resolved; see Resolved section.

---

## Resolved

- ~~`passlib` dependency on an unmaintained library~~ — replaced with direct `bcrypt` calls (2026-08-13)
- ~~`_net_to_out` manually constructs the response object~~ — refactored to `NetOut.model_validate(net)` with sharing metadata patched in (2026-08-13)
- ~~No rate limiting on authentication endpoints~~ — `slowapi` added; `POST /auth/login` limited to 10/minute, `POST /auth/register` to 5/minute (2026-08-13)
- ~~DMR relay script uses expiring JWT tokens~~ — `api_tokens` table + `GET/POST/DELETE /auth/tokens` endpoints added; `get_current_user` accepts `nt_` prefixed tokens; relay script updated to use API token (2026-08-13)
- ~~No fail2ban-compatible auth failure log~~ — `AUTH_LOG_FILE` env var; failed logins write `AUTH_FAIL ip=… reason=…` to a `WatchedFileHandler` log (2026-08-13)
- ~~Migration SQL must be kept in sync manually~~ — `migrate.py` is now the single source of truth; `index.html` and `README.md` updated to point to it (2026-08-14)
- ~~Single-file frontend (`index.html`)~~ — CSS extracted to `static/app.css`; JS split into 15 feature modules under `static/js/`; Admin/Tokens/Help/Report split into standalone HTML pages; `index.html` reduced from 3800 to 624 lines (SPA core only); FastAPI serves `/static` via `StaticFiles` (2026-08-14)
- ~~No test suite~~ — 59-test pytest suite covering auth, nets, sessions, and check-ins; runs against SQLite in-memory via `python -m pytest tests/`; `requirements-dev.txt` has the test deps (2026-08-16)
- ~~DMR push cache is in-memory only~~ — `_dmr_cache_write` now persists each push to `SystemSetting` as JSON; `_dmr_cache_read` falls back to `SystemSetting` on an in-memory miss (e.g., after restart); no new table or migration needed (2026-08-17)
- ~~`httpx` imported twice under different names~~ — duplicate `import httpx as _httpx` removed; all DMR proxy calls use the top-level `httpx` import (2026-08-16)
- ~~FCC callsign lookup depends on an external service~~ — `CallsignCache` table added; results cached for 30 days (found) or 7 days (not_found); `_callsign_cache_read/write` helpers wrap all four return paths in `lookup_callsign`; 4 cache-hit tests added (2026-08-17)
- ~~Relay script normalizes WPSD data differently from the backend~~ — new `POST /nets/{id}/dmr/push/raw` endpoint accepts raw hotspot JSON + `source` tag and normalizes server-side using existing `_dmr_normalize_wpsd/brandmeister()` functions; `dmr_relay.py` added to repo as a thin fetch-and-forward proxy; old `/push` endpoint kept for backward compat; 5 tests added (2026-08-17)
- ~~No email verification on registration~~ — `users.email_verified`/`verification_token`/`verification_sent_at` added; registration sends a verify-your-email link (skipped for the bootstrap first/admin user, and silently skipped like all other email if SMTP isn't configured); `GET /auth/verify-email` consumes the token; login now rejects unverified accounts before checking approval status; pending-approval list in Admin shows a Verified/Unverified badge so an admin can see at a glance. 16 tests added (2026-08-19)
- ~~`create_support_ticket` duplicated `send_email`'s entire SMTP-sending logic~~ (found while working the item above) — just to set a `Reply-To` header `send_email()` didn't support; added a `reply_to` param to `send_email()` instead and removed the ~30-line duplicate implementation (2026-08-19)
- ~~Admin had no way to unblock a user stuck on email verification~~ (found via self-review of the item above) — `PATCH /admin/users/{id}/approve` now also sets `email_verified=True`; an admin's approval is a stronger trust signal than the automated link-click, and it's the only lever available when `APP_BASE_URL` isn't configured (so the verify email has no working link) or the email never arrives. `verification_token` is now stored as a sha256 hash rather than raw (matches the existing `api_tokens.token_hash` pattern) and expires after 7 days. 3 more tests added (2026-08-19)
- ~~`GET /users` was defined twice~~ (found via self-review, pre-existing since the initial commit — unrelated to any recent work) — a second `list_users`/`UserSummary` pair later in `main.py` was fully unreachable (FastAPI matches routes in registration order); deleted the dead route and model (2026-08-19)
- ~~4 unused CSS custom properties~~ (`--accent`, `--danger`, `--warn`, `--lc-teal` — pre-existing, zero references anywhere in the app) — removed from `:root` and all 3 theme blocks added for the theme engine, where they'd been faithfully but pointlessly propagated (2026-08-19)
- ~~No app-wide logging configuration~~ (found while live-verifying the Net Repository push against production — a successful push's `INFO` log line was invisible, with no clue why) — nothing called `logging.basicConfig()`, so only `_auth_log` (which sets up its own optional `AUTH_LOG_FILE` handler) had a real handler; everything else silently relied on Python's WARNING-level "handler of last resort," so `INFO` messages (sent email, pushed net) never appeared anywhere, even in the systemd journal, while the equivalent `WARNING`-level failures already did. Added `logging.basicConfig()` at startup, level configurable via `LOG_LEVEL` (default `INFO`) (2026-08-19)
- ~~SQLAlchemy is used synchronously~~ — full migration to async SQLAlchemy (`create_async_engine`/`AsyncSession`) + `asyncpg` in production, `aiosqlite` in tests. `database.py` auto-rewrites a plain `postgresql://`/`sqlite://` `DATABASE_URL` to `+asyncpg`/`+aiosqlite`, so existing `.env` files need no change. All 148 route handlers in `main.py` are now `async def`; all 179 `db.query(...)` call sites converted to SQLAlchemy 2.0 `select()`/`db.execute()`; all 29 internal helper functions taking a session are now async and properly awaited at every call site. `net_repository.py`, `send_reminders.py`, `push_to_net_repository.py`, and `demo_reset.py` converted too (they share the same engine — an in-memory test DB is per-connection, so a second sync engine would've been invisible to the async one in tests); `migrate.py`/`gmrs_sync.py` (raw `psycopg2`, no ORM) and `dmr_relay.py`/`aprs_relay.py` (pure HTTP clients) needed no changes. `tests/conftest.py` moved off `sqlite:///:memory:`+`StaticPool` to a temp file-based SQLite DB — verified via a standalone repro that the in-memory+StaticPool combination hangs indefinitely once `TestClient` is involved (it drives the ASGI app from a separate thread/event loop via an `anyio` blocking portal, so a single `aiosqlite` connection shared via `StaticPool` ends up driven from two event loops at once, which deadlocks); a file needs no `StaticPool` since it's naturally shared across connections. `pytest-asyncio` added (`asyncio_mode = auto`, `asyncio_default_fixture_loop_scope = session` — the session scope is required for session-scoped async autouse fixtures to work with plain sync test functions at all). Found and fixed along the way: `AsyncSession.delete()` is itself a coroutine (unlike `.add()`, which stays sync) — every call site needed `await`; a couple of `await helper(...).attr` call sites had an operator-precedence bug (needed `(await helper(...)).attr`); one `.first()`→`scalar_one_or_none()` auto-conversion (`_is_first_checkin_for_net`) needed to go back to take-the-first semantics instead, since GMRS nets legitimately allow multiple check-in rows for the same callsign. Full 503-test suite passes; live-verified against a real `uvicorn` + `aiosqlite` file DB (register → login → create net → session → check-in → stats → end session → delete net, zero errors/warnings in the server log) (2026-08-30)
