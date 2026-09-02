#!/usr/bin/env python3
"""
NetControl Online — database migration runner

Applies all schema changes that SQLAlchemy's create_all() cannot handle
(i.e. adding columns to existing tables).  Safe to re-run at any time;
every statement uses IF NOT EXISTS or is otherwise idempotent.

New tables are normally created automatically by the app on startup (via
Base.metadata.create_all) -- this script's own MIGRATIONS list below only
manages ALTER TABLE work on top of that. But since deploy.sh runs this
script *before* restarting the app, run() also calls that same
create_all() itself first (via database.init_db()), so this script is
self-sufficient against a genuinely empty database too (a fresh install,
or an instance whose schema was just wiped, e.g. the public demo's
periodic reset) rather than depending on run order.

Usage
-----
    python3 migrate.py

Reads DATABASE_URL from the .env file in the same directory (same as
the main app).  Run as the service user so DB permissions match:

    sudo -u netcontrol python3 /opt/netcontrol/migrate.py
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 not found — activate the virtualenv first, or:\n"
             "  pip install psycopg2-binary --break-system-packages")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv not found — activate the virtualenv first.")

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    sys.exit("DATABASE_URL not set in .env")

# ---------------------------------------------------------------------------
# Migration steps
# Each entry is (description, sql).  All statements must be idempotent.
# Add new entries at the bottom when extending the schema.
# ---------------------------------------------------------------------------

MIGRATIONS = [
    # ── Columns added after initial launch ──────────────────────────────────
    ("nets: net type (ham/gmrs)",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS net_type VARCHAR(10) NOT NULL DEFAULT 'ham'"),

    ("nets: ARES net flag",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS is_ares BOOLEAN NOT NULL DEFAULT FALSE"),

    ("checkins: traffic flag",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS has_traffic BOOLEAN NOT NULL DEFAULT FALSE"),

    ("checkins: ARES evacuation zone",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS evac_zone VARCHAR(100)"),

    ("users: registration notification opt-in",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_new_registrations BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── DMR integration ─────────────────────────────────────────────────────
    ("nets: default DMR talk group",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS dmr_talkgroup VARCHAR(20)"),

    ("checkins: DMR talk group",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS dmr_talkgroup VARCHAR(20)"),

    ("checkins: DMR region/country",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS dmr_region VARCHAR(100)"),

    # ── Tables created after some instances were already running ─────────────
    # (create_all handles these on fresh installs; listed here as documentation
    #  and as a safety net for instances that may have missed them)

    ("table: evac_zones",
     """CREATE TABLE IF NOT EXISTS evac_zones (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         callsign VARCHAR(12) NOT NULL,
         zone VARCHAR(100) NOT NULL,
         updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_evac_zone_net_callsign UNIQUE (net_id, callsign))"""),

    ("table: traffic_messages",
     """CREATE TABLE IF NOT EXISTS traffic_messages (
         id SERIAL PRIMARY KEY,
         session_id INTEGER NOT NULL REFERENCES net_sessions(id) ON DELETE CASCADE,
         msg_number VARCHAR(50),
         origin_callsign VARCHAR(12) NOT NULL,
         dest_info VARCHAR(200),
         msg_type VARCHAR(20) NOT NULL DEFAULT 'formal',
         status VARCHAR(20) NOT NULL DEFAULT 'received',
         notes TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    ("table: station_remarks",
     """CREATE TABLE IF NOT EXISTS station_remarks (
         id SERIAL PRIMARY KEY,
         callsign VARCHAR(12) NOT NULL,
         net_id INTEGER REFERENCES nets(id) ON DELETE CASCADE,
         remark TEXT NOT NULL,
         updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
         updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_station_remark_callsign_net UNIQUE (callsign, net_id))"""),

    ("table: net_shares",
     """CREATE TABLE IF NOT EXISTS net_shares (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_net_share_net_user UNIQUE (net_id, user_id))"""),

    ("table: net_schedules",
     """CREATE TABLE IF NOT EXISTS net_schedules (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         day_of_week INTEGER NOT NULL,
         start_time VARCHAR(5) NOT NULL,
         timezone VARCHAR(60) NOT NULL DEFAULT 'UTC',
         notes TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    ("table: system_settings",
     """CREATE TABLE IF NOT EXISTS system_settings (
         key VARCHAR(100) PRIMARY KEY,
         value TEXT,
         updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    ("table: net_control_signups",
     """CREATE TABLE IF NOT EXISTS net_control_signups (
         id SERIAL PRIMARY KEY,
         schedule_id INTEGER NOT NULL REFERENCES net_schedules(id) ON DELETE CASCADE,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         slot_date DATE NOT NULL,
         user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
         callsign VARCHAR(12) NOT NULL,
         name VARCHAR(100),
         email VARCHAR(255),
         notes TEXT,
         signed_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_signup_schedule_date UNIQUE (schedule_id, slot_date))"""),

    ("table: dmr_configs",
     """CREATE TABLE IF NOT EXISTS dmr_configs (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         source_type VARCHAR(20) NOT NULL DEFAULT 'wpsd',
         hotspot_url TEXT,
         talkgroup_id INTEGER,
         filter_callsign VARCHAR(12),
         direct_mode BOOLEAN NOT NULL DEFAULT FALSE,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_dmr_config_net UNIQUE (net_id))"""),

    ("table: api_tokens",
     """CREATE TABLE IF NOT EXISTS api_tokens (
         id SERIAL PRIMARY KEY,
         user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         name VARCHAR(100) NOT NULL,
         token_hash VARCHAR(64) NOT NULL UNIQUE,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         last_used_at TIMESTAMPTZ)"""),

    ("table: callsign_cache",
     """CREATE TABLE IF NOT EXISTS callsign_cache (
         callsign VARCHAR(12) PRIMARY KEY,
         status VARCHAR(10) NOT NULL,
         name VARCHAR(200),
         license_class VARCHAR(10),
         state VARCHAR(10),
         grid VARCHAR(10),
         expires VARCHAR(20),
         source VARCHAR(50),
         cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    # ── GMRS support ─────────────────────────────────────────────────────────
    # Drop the DB-level unique constraint on (session_id, callsign) so that
    # GMRS nets can accept the same callsign multiple times (shared family
    # licence).  Uniqueness for ham nets is enforced at the application layer.
    ("checkins: drop unique-callsign-per-session constraint for GMRS support",
     "ALTER TABLE checkins DROP CONSTRAINT IF EXISTS uq_checkin_session_callsign"),

    # Local copy of FCC ULS GMRS database — populated/refreshed by gmrs_sync.py
    ("table: gmrs_licenses",
     """CREATE TABLE IF NOT EXISTS gmrs_licenses (
         callsign VARCHAR(16) PRIMARY KEY,
         licensee_name VARCHAR(200),
         state VARCHAR(50),
         expires VARCHAR(20),
         status VARCHAR(4),
         synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    ("gmrs_licenses: widen state column to VARCHAR(50)",
     "ALTER TABLE gmrs_licenses ALTER COLUMN state TYPE VARCHAR(50)"),

    ("nets: net control script",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS script TEXT"),

    # ── Broadcaster role (e.g. Amateur Radio Newsline) ─────────────────────────
    ("nets: additional broadcast flag",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS has_broadcast BOOLEAN NOT NULL DEFAULT FALSE"),

    ("nets: broadcast label",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS broadcast_label VARCHAR(100)"),

    ("net_control_signups: role (net_control/broadcaster/both)",
     "ALTER TABLE net_control_signups ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'net_control'"),

    ("net_control_signups: drop single-signup-per-date constraint",
     "ALTER TABLE net_control_signups DROP CONSTRAINT IF EXISTS uq_signup_schedule_date"),

    ("net_control_signups: one signup per date per role",
     """DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_signup_schedule_date_role'
          ) THEN
            ALTER TABLE net_control_signups
              ADD CONSTRAINT uq_signup_schedule_date_role UNIQUE (schedule_id, slot_date, role);
          END IF;
        END $$"""),

    # ── Scheduled net reminders ─────────────────────────────────────────────
    ("nets: reminder emails enabled flag",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS reminder_enabled BOOLEAN NOT NULL DEFAULT FALSE"),

    ("nets: reminder lead time in minutes",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS reminder_minutes_before INTEGER"),

    ("net_control_signups: reminder sent timestamp",
     "ALTER TABLE net_control_signups ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ"),

    # ── Public net directory ────────────────────────────────────────────────
    ("nets: public directory opt-in flag",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS public_listed BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── Traffic "called" persistence (issue #15) ────────────────────────────
    ("checkins: traffic called flag",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS traffic_called BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── Preferred name (issue #14) ──────────────────────────────────────────
    ("station_remarks: preferred name",
     "ALTER TABLE station_remarks ADD COLUMN IF NOT EXISTS preferred_name VARCHAR(100)"),

    ("station_remarks: remark is now optional (preferred name can stand alone)",
     "ALTER TABLE station_remarks ALTER COLUMN remark DROP NOT NULL"),

    # ── Theme engine (issue #2) ─────────────────────────────────────────────
    ("users: theme preference",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(20) NOT NULL DEFAULT 'lcars'"),

    # ── Email verification (tech debt) ──────────────────────────────────────
    # DEFAULT TRUE backfills existing accounts as verified — this closes a gap
    # for new registrations, it doesn't retroactively lock anyone out.
    ("users: email verified flag",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE"),
    ("users: verification token",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(64)"),
    ("users: verification sent timestamp",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_sent_at TIMESTAMPTZ"),

    # ── Net Repository directory metadata (issue #12 follow-up) ─────────────
    ("nets: band",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS band VARCHAR(10)"),
    ("nets: mode",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS mode VARCHAR(20)"),
    ("nets: CTCSS tone",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS ctcss_tone VARCHAR(10)"),
    ("nets: region",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS region VARCHAR(100)"),
    ("nets: state",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS state VARCHAR(50)"),
    ("nets: website",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS website VARCHAR(300)"),

    # ── Broadcaster override at session start (issue #17) ────────────────────
    ("net_sessions: broadcaster override callsign",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS broadcaster_override_callsign VARCHAR(20)"),
    ("net_sessions: broadcaster override name",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS broadcaster_override_name VARCHAR(100)"),

    # ── Separate GMRS callsign on user profiles (issue #23) ──────────────────
    ("users: GMRS callsign",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS gmrs_callsign VARCHAR(12)"),

    # ── Enhanced ARES/ACES activation mode (issue #21) ────────────────────────
    # tactical_positions must be created before the checkins columns below,
    # since one of them references it.
    ("table: tactical_positions",
     """CREATE TABLE IF NOT EXISTS tactical_positions (
         id SERIAL PRIMARY KEY,
         session_id INTEGER NOT NULL REFERENCES net_sessions(id) ON DELETE CASCADE,
         tactical_callsign VARCHAR(50) NOT NULL,
         location VARCHAR(200),
         assigned_callsign VARCHAR(12),
         assigned_name VARCHAR(100),
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),
    ("net_sessions: ARES/ACES activation flag",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS is_activation BOOLEAN NOT NULL DEFAULT FALSE"),
    ("checkins: tactical position link",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS tactical_position_id INTEGER REFERENCES tactical_positions(id) ON DELETE SET NULL"),
    ("checkins: shift sign-off timestamp",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS signed_off_at TIMESTAMPTZ"),

    # ── Tactical position scheduling + trackable Net Control handoff ──────────
    # (issue #21 follow-up — live-testing feedback)
    ("tactical_positions: scheduled sign-on time",
     "ALTER TABLE tactical_positions ADD COLUMN IF NOT EXISTS scheduled_start TIMESTAMPTZ"),
    ("tactical_positions: net control flag",
     "ALTER TABLE tactical_positions ADD COLUMN IF NOT EXISTS is_net_control BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── Net Control rotation schedule (issue #21 follow-up) ───────────────────
    ("table: net_control_shifts",
     """CREATE TABLE IF NOT EXISTS net_control_shifts (
         id SERIAL PRIMARY KEY,
         session_id INTEGER NOT NULL REFERENCES net_sessions(id) ON DELETE CASCADE,
         callsign VARCHAR(12) NOT NULL,
         name VARCHAR(100),
         scheduled_start TIMESTAMPTZ NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    # ── Offline net entry (issue #20) ──────────────────────────────────────────
    ("net_sessions: offline entry flag",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS is_offline BOOLEAN NOT NULL DEFAULT FALSE"),
    ("net_sessions: net control override callsign",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS ncs_override_callsign VARCHAR(20)"),
    ("net_sessions: net control override name",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS ncs_override_name VARCHAR(100)"),

    # ── Offline net entry: closing the log (issue #20 follow-up) ──────────────
    # ended_at is set from creation for an offline entry (marks it non-live),
    # so it can't also mean "no more checkins" the way it does for a normal
    # session -- this is that separate signal.
    ("net_sessions: offline entry locked flag",
     "ALTER TABLE net_sessions ADD COLUMN IF NOT EXISTS is_offline_locked BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── Multi-tenancy (issue #1) ────────────────────────────────────────────
    # create_all() handles these two tables on a fresh install; listed here too
    # as documentation and a safety net for already-running instances, same as
    # every other post-launch table above.
    ("table: organizations",
     """CREATE TABLE IF NOT EXISTS organizations (
         id SERIAL PRIMARY KEY,
         name VARCHAR(200) NOT NULL,
         slug VARCHAR(100) NOT NULL UNIQUE,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),
    ("table: organization_memberships",
     """CREATE TABLE IF NOT EXISTS organization_memberships (
         id SERIAL PRIMARY KEY,
         org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
         user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         role VARCHAR(20) NOT NULL DEFAULT 'member',
         approved BOOLEAN NOT NULL DEFAULT FALSE,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_org_membership_org_user UNIQUE (org_id, user_id))"""),
    ("users: current organization",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL"),
    ("nets: organization",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS org_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE"),

    # Backfill: an instance upgrading from single-tenant gets one auto-created
    # "default" org containing every existing user/net, with access unchanged
    # from before this migration (existing admins become that org's admins too).
    # Guarded so it's a no-op on a fresh install (no users yet) and a no-op on
    # re-run (organizations already exists / rows already backfilled).
    ("multi-tenancy: create default org for pre-existing data",
     """INSERT INTO organizations (name, slug, created_at)
        SELECT COALESCE((SELECT value FROM system_settings WHERE key = 'org_name'), 'Default Organization'),
               'default', NOW()
        WHERE EXISTS (SELECT 1 FROM users)
          AND NOT EXISTS (SELECT 1 FROM organizations WHERE slug = 'default')"""),
    ("multi-tenancy: backfill memberships into default org",
     """INSERT INTO organization_memberships (org_id, user_id, role, approved, created_at)
        SELECT o.id, u.id, CASE WHEN u.is_admin THEN 'admin' ELSE 'member' END, TRUE, NOW()
        FROM users u, organizations o
        WHERE o.slug = 'default'
          AND NOT EXISTS (SELECT 1 FROM organization_memberships m WHERE m.user_id = u.id AND m.org_id = o.id)"""),
    ("multi-tenancy: backfill users.current_org_id into default org",
     """UPDATE users SET current_org_id = (SELECT id FROM organizations WHERE slug = 'default')
        WHERE current_org_id IS NULL
          AND EXISTS (SELECT 1 FROM organizations WHERE slug = 'default')"""),
    ("multi-tenancy: backfill nets.org_id into default org",
     """UPDATE nets SET org_id = (SELECT id FROM organizations WHERE slug = 'default')
        WHERE org_id IS NULL
          AND EXISTS (SELECT 1 FROM organizations WHERE slug = 'default')"""),
    # Safe on both an upgraded instance (every net just got backfilled above)
    # and a brand-new instance (nets table is still empty at this point).
    ("nets: organization is required",
     "ALTER TABLE nets ALTER COLUMN org_id SET NOT NULL"),

    # ── Organization website URL, required for new orgs (issue #1 follow-up) ──
    # Nullable at the DB level -- the pre-existing "Default Organization"
    # backfilled above predates this requirement and has no value. Required
    # for any org created from here on is enforced in main.py.
    ("organizations: website URL",
     "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS website_url VARCHAR(300)"),

    # ── Admin-created operator accounts (issue #1 follow-up) ──
    # Set-password invite token, redeemed via /auth/set-password.
    ("users: password-set invite token",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set_token VARCHAR(64)"),
    ("users: password-set invite sent-at",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set_sent_at TIMESTAMPTZ"),

    # ── Shared users can be granted edit rights, not just view/check-in
    # access (issue follow-up) ──
    ("net_shares: can_edit flag",
     "ALTER TABLE net_shares ADD COLUMN IF NOT EXISTS can_edit BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── APRS station map (issue #22) ──
    ("nets: aprs_map_enabled flag",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS aprs_map_enabled BOOLEAN NOT NULL DEFAULT FALSE"),
    ("table: aprs_configs",
     """CREATE TABLE IF NOT EXISTS aprs_configs (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         source_type VARCHAR(20) NOT NULL DEFAULT 'relay',
         aprs_fi_api_key VARCHAR(100),
         filter_callsign VARCHAR(12),
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         CONSTRAINT uq_aprs_config_net UNIQUE (net_id))"""),

    # Default APRS map viewport (issue follow-up) — where the map opens before
    # any position has been reported, e.g. a regional net's usual coverage area
    # or a wider view for a nationwide/worldwide activation.
    ("nets: aprs_default_lat column",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS aprs_default_lat DOUBLE PRECISION"),
    ("nets: aprs_default_lon column",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS aprs_default_lon DOUBLE PRECISION"),
    ("nets: aprs_default_zoom column",
     "ALTER TABLE nets ADD COLUMN IF NOT EXISTS aprs_default_zoom INTEGER"),

    # ── Welcome first-time check-ins ──
    ("checkins: is_first_checkin flag",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS is_first_checkin BOOLEAN NOT NULL DEFAULT FALSE"),

    # ── Digital voice modes beyond DMR (issue #26) ──
    ("dmr_configs: mode column",
     "ALTER TABLE dmr_configs ADD COLUMN IF NOT EXISTS mode VARCHAR(10) NOT NULL DEFAULT 'dmr'"),

    # ── Welcome messages (org banner + instance-wide login/popup messages) ──
    ("organizations: banner_message column",
     "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS banner_message TEXT"),
    # login_message/welcome_popup_message live in system_settings (key/value,
    # already an existing table) -- no schema change needed for those.

    # ── Per-organization branding (issue follow-up — logo lives on disk,
    #    namespaced per org, no column needed for it) ──
    ("organizations: tagline column",
     "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS tagline VARCHAR(200)"),

    # ── Manually-reported GPS position on a checkin (issue follow-up) ──
    ("checkins: lat/lon columns",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION"),
    ("checkins: lon column",
     "ALTER TABLE checkins ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION"),

    # ── Org-level aprs.fi API key (issue follow-up) — one key per org rather
    # than re-entered into every net's APRS config. The UPDATE below moves
    # any already-configured per-net keys up to their org (picking one
    # arbitrarily if multiple nets in the same org had different keys set —
    # rare, and the org admin can just re-set it via Admin afterward); the
    # old per-net aprs_configs.aprs_fi_api_key column is left in place,
    # unused, rather than dropped -- no migration in this file drops a
    # column, so not starting here either. ──
    ("organizations: aprs_fi_api_key column",
     "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS aprs_fi_api_key VARCHAR(100)"),
    # Guarded in a DO block, not a plain UPDATE (issue follow-up) -- the old
    # per-net aprs_configs.aprs_fi_api_key column this reads from was already
    # removed from models.py by the time this migration was written, so it
    # only ever exists on an instance upgrading from before that change. A
    # fresh install's aprs_configs table (create_all(), now always run first
    # -- see run() above) never has it at all, and Postgres validates a plain
    # UPDATE's column references even when no rows match, which would fail
    # migrate.py with "column ac.aprs_fi_api_key does not exist" on every
    # fresh install/demo reset. PL/pgSQL only validates a branch's SQL when
    # that branch actually executes, so this IF EXISTS skips it cleanly.
    ("organizations: copy up any existing per-net aprs.fi keys",
     """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'aprs_configs' AND column_name = 'aprs_fi_api_key') THEN
                UPDATE organizations o
                SET aprs_fi_api_key = sub.key
                FROM (
                    SELECT DISTINCT ON (n.org_id) n.org_id, ac.aprs_fi_api_key AS key
                    FROM aprs_configs ac
                    JOIN nets n ON n.id = ac.net_id
                    WHERE ac.aprs_fi_api_key IS NOT NULL
                    ORDER BY n.org_id, ac.id
                ) sub
                WHERE o.id = sub.org_id AND o.aprs_fi_api_key IS NULL;
            END IF;
        END $$"""),

    # UI translation via argos-translate (opt-in, TRANSLATION_ENABLED). ──
    ("users: language column",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(10)"),
    ("table: translation_cache",
     """CREATE TABLE IF NOT EXISTS translation_cache (
         id SERIAL PRIMARY KEY,
         cache_key VARCHAR(64) NOT NULL UNIQUE,
         target_lang VARCHAR(10) NOT NULL,
         source_text TEXT NOT NULL,
         translated_text TEXT NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),
    ("index: translation_cache target_lang",
     "CREATE INDEX IF NOT EXISTS ix_translation_cache_target_lang ON translation_cache (target_lang)"),
    ("table: enabled_languages",
     """CREATE TABLE IF NOT EXISTS enabled_languages (
         id SERIAL PRIMARY KEY,
         code VARCHAR(10) NOT NULL UNIQUE,
         display_name VARCHAR(50) NOT NULL,
         model_status VARCHAR(20) NOT NULL DEFAULT 'pending',
         error_message TEXT,
         enabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),

    # Per-org language opt-in (multi-tenancy follow-up) -- separates "is this
    # language's model installed on the server" (enabled_languages, above)
    # from "does this org's admin want it in their users' switcher".
    ("table: org_enabled_languages",
     """CREATE TABLE IF NOT EXISTS org_enabled_languages (
         id SERIAL PRIMARY KEY,
         org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
         code VARCHAR(10) NOT NULL,
         enabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
         UNIQUE (org_id, code))"""),
    ("index: org_enabled_languages org_id",
     "CREATE INDEX IF NOT EXISTS ix_org_enabled_languages_org_id ON org_enabled_languages (org_id)"),
    ("index: org_enabled_languages code",
     "CREATE INDEX IF NOT EXISTS ix_org_enabled_languages_code ON org_enabled_languages (code)"),

    # ── Pre-activation tactical/NC roster planning (issue follow-up) — lets a
    # net admin queue up tactical positions and the Net Control rotation before
    # an activation session exists, instead of only once it's already live.
    # session_id becomes nullable (a planned row has none yet); net_id is new
    # and backfilled from the owning session for every existing row, then
    # required going forward for both planned and already-live rows alike.
    ("tactical_positions: net_id column",
     "ALTER TABLE tactical_positions ADD COLUMN IF NOT EXISTS net_id INTEGER REFERENCES nets(id) ON DELETE CASCADE"),
    ("tactical_positions: backfill net_id from session",
     """UPDATE tactical_positions p SET net_id = s.net_id
        FROM net_sessions s WHERE s.id = p.session_id AND p.net_id IS NULL"""),
    ("tactical_positions: net_id is required",
     "ALTER TABLE tactical_positions ALTER COLUMN net_id SET NOT NULL"),
    ("tactical_positions: session_id becomes optional (planned rows)",
     "ALTER TABLE tactical_positions ALTER COLUMN session_id DROP NOT NULL"),

    ("net_control_shifts: net_id column",
     "ALTER TABLE net_control_shifts ADD COLUMN IF NOT EXISTS net_id INTEGER REFERENCES nets(id) ON DELETE CASCADE"),
    ("net_control_shifts: backfill net_id from session",
     """UPDATE net_control_shifts h SET net_id = s.net_id
        FROM net_sessions s WHERE s.id = h.session_id AND h.net_id IS NULL"""),
    ("net_control_shifts: net_id is required",
     "ALTER TABLE net_control_shifts ALTER COLUMN net_id SET NOT NULL"),
    ("net_control_shifts: session_id becomes optional (planned rows)",
     "ALTER TABLE net_control_shifts ALTER COLUMN session_id DROP NOT NULL"),

    # ── Named, reusable Activation Schedules (issue follow-up) — replaces the
    # single implicit one-time planning queue above with multiple named
    # presets a net admin picks from when starting an activation. Any row
    # already queued under the old model (session_id NULL, no schedule) is
    # backfilled into an auto-created "Migrated Plan" schedule per net rather
    # than silently orphaned/lost.
    ("table: activation_schedules",
     """CREATE TABLE IF NOT EXISTS activation_schedules (
         id SERIAL PRIMARY KEY,
         net_id INTEGER NOT NULL REFERENCES nets(id) ON DELETE CASCADE,
         name VARCHAR(100) NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""),
    ("tactical_positions: activation_schedule_id column",
     "ALTER TABLE tactical_positions ADD COLUMN IF NOT EXISTS activation_schedule_id INTEGER REFERENCES activation_schedules(id) ON DELETE CASCADE"),
    ("net_control_shifts: activation_schedule_id column",
     "ALTER TABLE net_control_shifts ADD COLUMN IF NOT EXISTS activation_schedule_id INTEGER REFERENCES activation_schedules(id) ON DELETE CASCADE"),
    ("activation_schedules: create 'Migrated Plan' for nets with orphaned planned rows",
     """INSERT INTO activation_schedules (net_id, name, created_at)
        SELECT DISTINCT net_id, 'Migrated Plan', NOW() FROM (
            SELECT net_id FROM tactical_positions WHERE session_id IS NULL AND activation_schedule_id IS NULL
            UNION
            SELECT net_id FROM net_control_shifts WHERE session_id IS NULL AND activation_schedule_id IS NULL
        ) orphaned"""),
    ("tactical_positions: attach orphaned planned rows to 'Migrated Plan'",
     """UPDATE tactical_positions p SET activation_schedule_id = sch.id
        FROM activation_schedules sch
        WHERE p.session_id IS NULL AND p.activation_schedule_id IS NULL
          AND sch.net_id = p.net_id AND sch.name = 'Migrated Plan'"""),
    ("net_control_shifts: attach orphaned planned rows to 'Migrated Plan'",
     """UPDATE net_control_shifts h SET activation_schedule_id = sch.id
        FROM activation_schedules sch
        WHERE h.session_id IS NULL AND h.activation_schedule_id IS NULL
          AND sch.net_id = h.net_id AND sch.name = 'Migrated Plan'"""),

    # ── Self-service account fields (issue follow-up) ──
    ("users: phone column",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(30)"),

    # ── Invite-only organizations (issue follow-up) ──
    ("organizations: registration_open column",
     "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS registration_open BOOLEAN NOT NULL DEFAULT TRUE"),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run():
    # Base tables are normally already there from a previous app startup
    # (main.py's lifespan calls this same init_db() via database.py) --
    # every migration below assumes that and only does ALTER TABLE /
    # CREATE TABLE IF NOT EXISTS work on top of it. That assumption breaks
    # for a genuinely empty database: deploy.sh runs this script *before*
    # restarting the app (see deploy.sh), so a fresh install, or an
    # instance whose schema was just wiped (e.g. the public demo's
    # demo_reset.py, which drops and recreates the whole public schema on
    # a cron), can hit migrate.py with zero tables yet -- every statement
    # below would then fail with "relation ... does not exist" (issue
    # follow-up: this is exactly what surfaced on the demo instance).
    # Calling init_db() here first makes this script self-sufficient
    # regardless of run order -- a no-op when tables already exist.
    print("Ensuring base schema exists…")
    try:
        import asyncio
        from database import init_db
        asyncio.run(init_db())
    except Exception as e:
        sys.exit(f"Could not create base tables: {e}")

    print(f"Connecting to database…")
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        sys.exit(f"Could not connect: {e}")

    conn.autocommit = False
    cur = conn.cursor()

    ok = 0
    errors = 0
    for description, sql in MIGRATIONS:
        try:
            cur.execute(sql)
            conn.commit()
            print(f"  ✓  {description}")
            ok += 1
        except Exception as e:
            conn.rollback()
            print(f"  ✗  {description}")
            print(f"     {e}")
            errors += 1

    cur.close()
    conn.close()

    print()
    if errors:
        print(f"Completed with {errors} error(s). Review output above.")
        sys.exit(1)
    else:
        print(f"All {ok} migration(s) applied successfully.")


if __name__ == "__main__":
    run()
