# NetControl Online

A web-based net control logging application for amateur radio and GMRS operators. Designed for Net Control Stations (NCS) to efficiently manage check-ins, track traffic, and log net sessions.

![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)

## Live Demo

A public demo is available at **[demo.netcontrol.online](https://demo.netcontrol.online)**. Feel free to explore all features — the database resets automatically every 4 hours.

| Field | Value |
|-------|-------|
| URL | https://demo.netcontrol.online |
| Callsign | `D3MO` |
| Password | `Abcd1234` |

> The demo account has full admin access. Any nets, sessions, or check-ins you create will be wiped on the next reset.

Setting up your own instance? See **[QUICKSTART.md](QUICKSTART.md)** for the fast path — running locally and taking your first check-in in a few minutes, including how to get a Net Repository API token. This README is the full reference.

## Features

- **Multi-tenancy** — every account belongs to one or more **Organizations**; nets, sessions, and check-ins are scoped so separate organizations sharing the same install never see each other's data. Registration offers create-a-new-organization (name + required website URL; needs a Super Admin's approval before login, since a founder can't approve themselves) or join-an-existing-one (needs that organization's own admin to approve you instead); an org switcher appears in the sidebar for anyone in more than one. Organization admins get a scoped Admin page for their own members; the existing site-wide Super Admin role is unchanged, still sees everything, and is the only registration path that's ever auto-approved (the instance's first-ever user, with no one else to ask). An org can also be made **invite-only** (Admin → Organization) — hidden from the public join picker, with self-registration into it blocked outright; an org admin brings people in directly instead via **Add Operator**, which creates the account immediately (approved, no round trip) and emails a link to set its own password
- **Net & session management** — create nets, start/end sessions, log check-ins with signal reports
- **Log a past net** — a net that formed with no access to the web tool can be backfilled afterward: set its date/time, Net Control, and broadcaster up front, then add check-ins that get stamped with the reported date/time instead of "now". No live-session chrome (clock, Expected Stations, Digital Voice, Net Script) since it was never live to begin with — just the check-in form and roster. Click **🔒 Close Log** when done entering data to stop accepting further check-ins
- **Focused live session view** — sidebar auto-collapses and session navigation hides while a session is live to cut clutter, restoring automatically once it ends; the manual check-in form stays pinned to the top of the screen so it's reachable while scrolling; the checked-in stations roster sits in its own independently-scrollable column on the right (callsign, name, traffic, delete — the 5 most recent highlighted for 20 seconds), so it's always visible without scrolling past the form
- **Callsign lookup** — FCC database lookup with local history suffix search
- **Traffic management** — flag stations with traffic, interactive "called" tracking (persists across a session close/reopen), formal traffic message log with per-message **ICS-213 export** two ways: plain text ready to paste into a Winlink message or attach as a .txt file for relay over radio, or a printable form opened in a new tab to print/save as PDF for a local paper copy
- **Welcome first-time check-ins** — a green banner lists any station checking in for the first time ever on that net (across all its sessions), with a matching 👋 badge next to their callsign in the roster, so net control can greet new operators on the air
- **ARES/ACES mode** — evacuation zone tracking per station, zone roster panel
- **ARES/ACES activation mode** — mark an individual session on an ARES net as an activation to unlock tactical assignments: define tactical positions (callsign, location, planned operator, optional scheduled sign-on time) on a dedicated Station Schedule tab, then sign operators on and off each position in shifts from the live check-in screen (with full shift history per position) instead of the routine per-callsign roster. A vacant position past its scheduled sign-on time is flagged. A station checking in without a tactical assignment shows its evacuation zone instead. **Net Control gets its own rotation schedule** — queue up planned shifts (callsign, name, sign-on time) ahead of time; handing off auto-fills the incoming operator from the next scheduled shift (still editable for last-minute changes) and consumes it from the queue, with the duty bar and net script reflecting the current holder immediately. Routine sessions on the same net are completely unaffected — this is opt-in per session, not a net-wide setting
- **Expected stations** — pre-built check-in list from historical attendees with pre-flag support
- **Station remarks & preferred names** — persistent per-net notes on any callsign, plus a preferred name that overrides the FCC/GMRS-looked-up name on the Expected Stations list, Checkin History, and net reports; editable from the check-in form, Expected Stations, or Checkin History (including after a net has closed)
- **Session summary & ICS-205** — automatic summary card on session end, printable net log export
- **Net control script** — attach a script to a net with basic formatting and live `{{variable}}` substitution (Net Control / Broadcaster callsign and name), pinned to the top of the live check-in screen so you don't need a second window open. Written in its own tab of the net form, with a formatting toolbar, a clickable variable reference, and a live rendering preview using sample values
- **Session clock** — live local/UTC time and elapsed session timer
- **Net sharing** — share nets with individual operators or all registered users
- **Scheduling** — weekly recurring time slots with sign-ups for Net Control and, on nets with an additional broadcast (e.g. Amateur Radio Newsline), a separate Broadcaster role; confirmation emails include a `.ics` calendar attachment. Timezone auto-detects to the browser's own when adding a schedule, and every displayed time shows a "(your time)" conversion for anyone viewing from a different zone
- **Scheduled reminder emails** — configurable per-net lead time; reminds whoever signed up as Net Control / Broadcaster shortly before their net starts (needs a cron job — see below)
- **Session history** — attendance statistics, filtering, and CSV export
- **Public live page** — unauthenticated `/live/<org>` page showing that organization's active nets and check-in rosters in real time; bare `/live` shows a picker across every organization with at least one publicly listed net
- **Public net directory** — opt-in per net; unauthenticated `/directory/<org>` page listing name, frequency, description, and weekly schedule for anyone to browse; bare `/directory` shows the same organization picker as `/live`
- **SEO for the public directory** — `/robots.txt` and `/sitemap.xml` steer search engines toward each organization's `/directory/<org>` page and away from anything requiring login; that page gets a server-rendered title, meta description, Open Graph tags, and schema.org `Organization` structured data per organization, so it's actually discoverable and links to it preview correctly in Slack/Discord/social media. `/live/<org>` is marked `noindex` (real-time content isn't worth indexing)
- **Net Repository integration** — optionally push publicly-listed nets to a central, community-run directory ([Net Repository](https://github.com/LadyHwesta/Net-Repository)) so they're discoverable beyond this instance; request an API key self-service from Admin, no manual key exchange needed
- **In-app problem reporting** — users can submit bug reports and enhancement requests directly to the administrator
- **User management** — registration with org-admin approval, email verification (when SMTP is configured), email notifications, admin panel
- **Bot protection (optional)** — Cloudflare Turnstile, Google reCAPTCHA, or ALTCHA (open source, self-contained — no third-party service) on registration and login, off by default; set `CAPTCHA_PROVIDER` plus that provider's keys to enable
- **Configurable branding** — an instance-wide name/tagline/logo (Admin → Branding, super admin only), plus a separate tagline and logo any org admin can set for just their own organization (Admin → Organization → Your Organization) — shown in the header and on that org's own public Directory/Live pages, falling back to the instance-wide branding for whichever piece an org hasn't set its own
- **Database stats (Postgres deployments)** — Admin → DB Stats shows database size, connection counts, and largest tables; also surfaces slowest queries if the [`pg_stat_statements`](https://www.postgresql.org/docs/current/pgstatstatements.html) extension is enabled on the server (one-time server-level setup — see "Database Stats (Admin)" below). A no-op on SQLite deployments
- **Welcome messages** — a super admin can set an instance-wide login screen message (Admin → Announcements) and a post-login welcome popup, each shown once per distinct message (editing the text re-notifies everyone). An org admin can separately set a banner message for just their own org's members, shown inline in the header (set from the "Your Organization" card in Admin → Organization)
- **Theme engine** — per-account color theme (LCARS, Dark, Light, High Contrast, Pink, Purple, Blue, Matrix, Earth, or System/OS-matched), persisted server-side so it follows you across devices; the picker lives at the top of the sidebar, along with the account switcher, language picker, and Logout
- **Digital voice integration** — connect a net to a WPSD/Pi-Star hotspot or a BrandMeister talk group; covers DMR, D-Star, YSF, NXDN, P25, and M17. See a live "last heard" panel during the session, quick-check-in heard stations, and log Talk Group + Region per check-in
- **Keyboard-friendly forms** — Enter submits the primary action from any save/submit form's text fields (multi-line fields like net description and report body are left alone so Enter still inserts a newline)
- **Installable mobile app (PWA)** — installable to a phone's home screen with an offline-capable app shell; a **📱 Net Control** toggle on the live session view strips the check-in screen down to a big callsign field and minimal chrome for one-handed net control. Check-ins submitted with no connection queue locally and send automatically once back online
- **Separate GMRS callsign** — operators holding both an amateur and a GMRS license can set a GMRS callsign under **⚙️ Account**; Net Control on a GMRS net automatically shows it instead of the amateur callsign — duty bar, net script variables, the public live page, and Schedule sign-ups all pick it up with nothing else to configure. Leave it unset to keep using the amateur callsign everywhere
- **Self-service profile** — edit your own name, email, callsign, and phone number from **⚙️ Account** (an email change re-verifies the new address before you can log in again, when SMTP is configured). The phone number is shown to whoever's coordinating a net's schedule sign-ups, so they can call you if you haven't shown up for your shift — never shown publicly. A profile photo (crop/reposition it right after picking a file, or re-crop an existing one any time) shows on the public Live page next to whoever's running the net as Net Control or the assigned broadcaster

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0 (async, via `asyncpg`/`aiosqlite`), PostgreSQL
- **Frontend**: Vanilla JS SPA (no framework), 9 selectable color themes (LCARS-inspired by default) — CSS in `static/app.css`, JS split into feature modules under `static/js/`
- **Auth**: JWT (PyJWT), bcrypt passwords
- **Email**: SMTP (configurable — Gmail, SendGrid, local Postfix, etc.)
- **Deployment**: systemd + Apache reverse proxy + Let's Encrypt

## Requirements

- Python 3.11+
- PostgreSQL 14+
- Apache2 (or any reverse proxy)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/LadyHwesta/netcontrol-online.git
cd netcontrol-online
```

### 2. Install Python dependencies

With a virtual environment (recommended — `deploy.sh` uses `venv/bin/python3` automatically if it exists):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

Or without one:

```bash
pip install -r requirements-dev.txt
```

Either file works for this first-time setup — `deploy.sh` re-installs from `requirements.txt` (and `requirements-dev.txt` too, on a `testing` instance or with `--force-tests`) on every run, so dependencies added by a later pull are picked up automatically without a manual reinstall. Install `requirements-dev.txt` here if you want `pytest` available locally (e.g. to run the suite yourself before pushing) or this instance's `GIT_BRANCH` is `testing`; `requirements.txt` alone is enough for a `main` instance, which skips the suite by default (see "Branching model" below).

### 3. Create the database

```bash
sudo -u postgres psql
```

```sql
CREATE USER netcontrol WITH PASSWORD 'yourpassword';
CREATE DATABASE netcontrol OWNER netcontrol;
\q
```

> **Note:** The initial `CREATE USER` and `CREATE DATABASE` must be run as the `postgres` superuser. All subsequent commands — including migrations — should be run as the `netcontrol` user (`sudo -u netcontrol psql netcontrol`) so that created tables are automatically owned by the app user and no GRANTs are needed.

### 4. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in your `DATABASE_URL`, `SECRET_KEY`, and SMTP settings. See `.env.example` for all options.

### 5. Initialize the database schema

```bash
python3 -c "from database import init_db; init_db()"
```

### 6. Run the application

**Development:**
```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

**Production (systemd):** See [Deployment](#deployment) below.

## Database Migrations

> **Fresh installs:** the app creates the full current schema automatically on first startup. No migration step needed.

**Upgrading an existing install** — `./deploy.sh` runs `migrate.py` automatically as part of every deploy, so no manual step is needed if you use it. Running it by hand:

```bash
sudo -u netcontrol python3 /opt/netcontrol/migrate.py
```

`migrate.py` is the single source of truth for all schema changes. It is safe to re-run at any time — every step uses `IF NOT EXISTS` or is otherwise idempotent. When adding a new column or table to `models.py`, add the corresponding statement to `MIGRATIONS` in `migrate.py` — that's the only file that needs updating.

## Database Stats (Admin)

**Admin → DB Stats** (Postgres deployments only — a no-op on SQLite) shows database size, connection counts, and the largest tables with no setup at all. **Slowest queries** additionally needs the [`pg_stat_statements`](https://www.postgresql.org/docs/current/pgstatstatements.html) extension enabled on the Postgres *server* — this is a one-time, superuser-level step outside the app itself, not something `migrate.py` can do for you.

On a self-managed Postgres (Ubuntu/Debian):

```bash
# 1. Add it to shared_preload_libraries -- find your config file with
#    `sudo -u postgres psql -c "SHOW config_file"` if this path doesn't match.
sudo nano /etc/postgresql/*/main/postgresql.conf
#   shared_preload_libraries = 'pg_stat_statements'
#   (append with a comma if the line already lists other libraries)

# 2. Restart Postgres -- shared_preload_libraries only takes effect at
#    startup, a reload isn't enough.
sudo systemctl restart postgresql

# 3. Enable the extension in this app's own database (once).
sudo -u postgres psql -d ham_net_tracker -c "CREATE EXTENSION pg_stat_statements;"

# 4. If the app connects as its own, non-superuser role (recommended --
#    and the common case, since a superuser role for the app itself is a
#    bad idea) rather than the postgres superuser, that role also needs
#    permission to actually READ the query *text* of statements it looks
#    up here -- without this, the panel populates but every row's query
#    column shows the literal text "<insufficient privilege>" instead
#    (call counts/timings are unaffected either way; only the query text
#    itself is gated). Grant it once, using your app's actual role name
#    from DATABASE_URL:
sudo -u postgres psql -d ham_net_tracker -c "GRANT pg_read_all_stats TO your_app_db_role;"
```

Use your actual database name from `DATABASE_URL` in `.env` if it's not `ham_net_tracker`. Once enabled, the Slow Queries panel starts populating immediately — no app restart or `migrate.py` run needed, it reads `pg_stat_statements` directly.

**Managed/cloud Postgres** (RDS, DigitalOcean Managed Databases, Cloud SQL, etc.) usually can't have `postgresql.conf` edited directly — `pg_stat_statements` is typically enabled instead through the provider's parameter group or extension allow-list in their dashboard, then `CREATE EXTENSION pg_stat_statements;` still needs to be run once against the database as shown above. Check your provider's docs for the parameter-group step; most major providers support this extension.

## Deployment

### systemd service

Create `/etc/systemd/system/nettracker.service`:

```ini
[Unit]
Description=NetControl Online
After=network.target postgresql.service

[Service]
Type=simple
User=netcontrol
WorkingDirectory=/opt/netcontrol
EnvironmentFile=/opt/netcontrol/.env
ExecStart=/opt/netcontrol/venv/bin/uvicorn main:app --host 127.0.0.1 --port ${PORT} --workers ${WORKERS}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nettracker

[Install]
WantedBy=multi-user.target
```

`${PORT}`/`${WORKERS}` are substituted by systemd from `.env` (see `.env.example`) — the unit file itself doesn't need to change per instance. `WORKERS` has no implicit default the way a shell script's `${VAR:-1}` would give it (systemd's own `$VAR` substitution is plain literal replacement, nothing bash-like), so `.env.example` always sets it explicitly — `WORKERS=1` is today's single-process behavior; see "Running with multiple workers" below before raising it.

`ExecStart` deliberately points at the checkout's own `venv/bin/uvicorn`, not a system-wide one — a bare `uvicorn` (or `/usr/bin/uvicorn`) runs under whatever Python environment it happens to be installed in, which won't have this app's dependencies (`fastapi`, `sqlalchemy`, `asyncpg`, ...) unless that happens to be the same venv. `deploy.sh` follows the same rule for the same reason (prefers `venv/bin/python3` over a bare `python3`) — see its own comments.

```bash
sudo systemctl daemon-reload
sudo systemctl enable nettracker
sudo systemctl start nettracker
```

#### Running more than one instance on the same server

The template above, plus `PORT`, `SYSTEMD_SERVICE`, and `GIT_BRANCH` in `.env`, is all `deploy.sh` needs to work unmodified for multiple instances (e.g. a stable site and a testing site) checked out separately on one server — no per-instance edits to `deploy.sh` or the unit file's `ExecStart` line. For each instance:

1. Check out the repo to its own directory (e.g. `/opt/netcontrol` and `/opt/netcontrol-testing`).
2. Give each its own `.env` with a distinct `DATABASE_URL`, `PORT`, `SYSTEMD_SERVICE` (e.g. `nettracker` and `nettrackertesting`), and `GIT_BRANCH` (`main` for the stable instance, `testing` for the testing instance).
3. Copy the unit file template above to `/etc/systemd/system/<SYSTEMD_SERVICE>.service` for each instance — the file contents are identical except the description/working directory; only `.env` needs to differ.
4. Add a matching sudoers `NOPASSWD` line for each `SYSTEMD_SERVICE` value (see the prerequisite comment at the top of `deploy.sh`) — `deploy.sh` reads `SYSTEMD_SERVICE` and `GIT_BRANCH` from the `.env` in its own checkout, checks out and pulls exactly that branch, and restarts exactly that unit.
5. Point each Apache vhost's reverse proxy at the matching `PORT`.

#### Running with multiple workers

`WORKERS` in `.env` (default `1`, see `.env.example`) sets how many uvicorn worker processes this one instance runs, via `--workers ${WORKERS}` in the systemd unit's `ExecStart`. Raising it lets one instance use more than one CPU core — but two pieces of this app hold their own state in an in-memory Python dict, private to whichever single process happens to be running: the rate limiter (`slowapi`, `routers/deps.py`) and the DMR/APRS relay-push caches (`routers/digital_voice.py`/`routers/aprs.py`). With `WORKERS=1` (today's default) that's harmless — there's only ever one process. The moment `WORKERS` goes above 1, both break in different ways:

- **The rate limiter** becomes per-worker, which quietly multiplies every configured limit by the worker count — `5/minute` on `/auth/register` becomes ~15–20/minute spread across 3–4 invisible counters. This is a real correctness gap, not just a performance nit.
- **The relay-push caches** become per-worker too — a push landing on worker 1 isn't visible to a request served by worker 2 until it falls back to the slower database-backed copy.

Setting `REDIS_URL` in `.env` fixes both: the rate limiter uses `slowapi`/`limits`' built-in Redis storage backend instead of its in-memory default, and the relay-push caches use Redis as a shared tier consulted first (see `routers/helpers.py`'s Redis cache section) instead of relying on each worker's own private copy. **Don't set `WORKERS` above `1` without also setting `REDIS_URL`** — nothing stops you from doing so (no startup check refuses it; a uvicorn worker has no reliable way to know how many siblings it has), so this is on the operator to get right.

Redis is otherwise unused by this app — it's not a general cache, a session store, or anything else, just the shared backing for these two specific pieces of per-worker state. Leave `REDIS_URL` unset for a single-worker instance; there's nothing to gain from running Redis alongside it. The `redis` Python package is already in `requirements.txt` (see its own comment there) — no separate `pip install` step; a Redis *server* is the only thing you need to stand up yourself, then point `.env` at it:

```
WORKERS=4
REDIS_URL=redis://localhost:6379
REDIS_DB=0
```

**Sharing one Redis server across multiple instances:** since [one server can already run several independent instances](#running-more-than-one-instance-on-the-same-server) of this app (main/testing/demo), nothing stops them sharing one Redis server too — set a different `REDIS_DB` (Redis's own numbered logical databases, `0`–`15` by default) on each instance pointed at it, so their cache/rate-limit data can't collide. As a second, independent layer of the same protection, every key this app writes to Redis is also automatically namespaced by `SYSTEMD_SERVICE` — already required to be unique per instance for the systemd unit itself to work — so two instances still can't collide even if `REDIS_DB` is left the same on both by mistake. `REDIS_DB` is the real isolation (a genuinely separate keyspace, inspectable on its own via `redis-cli -n N`); the automatic key prefix is a safety net under it, not a substitute for setting it correctly.

#### Branching model

`main` is the stable branch; `testing` is where day-to-day commits land until they've been worked out. Point a stable instance's `deploy.sh` at `main` and a testing instance's at `testing` (via `GIT_BRANCH`, above) to keep the two separate. Merge `testing` into `main` only once a change is confirmed good.

`deploy.sh` only runs the test suite automatically when `GIT_BRANCH` is `testing` — a `main` instance skips it, since a change should already have passed on testing before being merged. Run `./deploy.sh --force-tests` to run the suite anyway on a `main` instance (e.g. right after a hotfix committed straight to `main`).

#### Pre-deploy database backups

Every `deploy.sh` run on a `main` instance (`GIT_BRANCH=main`) backs its own database up into `<checkout>/backups/` before touching anything — a `pg_dump | gzip` for Postgres, a plain file copy for SQLite — keeping the 14 most recent. `testing`/other instances skip this: their data is expected to be disposable (see "Branching model" above), so there's nothing there worth spending backup time/storage on.

This isn't a substitute for a real off-server backup strategy (it lives on the same disk as the database it's backing up, and 14 deploys' worth of history is only ever as deep as your deploy cadence), but it's cheap insurance against the two most common ways to lose data outright: a migration gone wrong, or a maintenance script (`demo_reset.py`, which drops and recreates the entire schema) run against the wrong instance's `.env` by mistake. `backups/` is gitignored — never committed, and safe to prune or move to real off-server storage by hand at any time.

To restore a Postgres backup:
```bash
gunzip -c backups/<service>-<timestamp>.sql.gz | psql "$DATABASE_URL"
```
(onto an empty database — this doesn't clear existing tables first).

#### Running the public demo (demo_reset.py)

If you're not running the public demo, you can skip this section entirely — `demo_reset.py` has no reason to exist on a production or testing instance, and the guard below is what stops it from doing anything if it's ever run there by mistake.

`demo_reset.py` (`DROP SCHEMA public CASCADE`, then recreates a clean database with a single demo account — meant to run every few hours via cron so the public demo always looks fresh) refuses to run at all unless the checkout's own `.env` sets:
```
DEMO_INSTANCE=true
```
This is a deliberate, required opt-in with no default — a checkout is *not* the demo just because of its path or which branch it tracks. When run from a terminal (not cron), it also prints the actual database name it resolved and requires typing it back before doing anything, catching "right box, wrong terminal session" even on a correctly-flagged instance; cron runs (no TTY) skip that prompt automatically. Neither check can be bypassed with a flag — set `DEMO_INSTANCE=true` only in the one `.env` you actually want this script erasing on a schedule, and nowhere else.

A real crontab entry already runs with no controlling terminal, so the confirmation prompt above is a non-issue for it — nothing extra is needed beyond `DEMO_INSTANCE=true`. Redirect stdin from `/dev/null` explicitly anyway (see the cron line in the script's own docstring) so this stays true even if the same line is ever copied into something that *does* keep a terminal attached, rather than depending on cron's default behavior specifically.

#### Troubleshooting a failed deploy

First, always check the service logs — `deploy.sh` finishing without error only means the *deploy steps* succeeded (git pull, pip install, migrate.py, systemd restart); it doesn't mean the app actually started:

```bash
sudo journalctl -u <SYSTEMD_SERVICE> -n 50 --no-pager
```

Two dependency-related failures have come up in practice, both fixed the same way — re-running the dependency install:

- **`ModuleNotFoundError: No module named 'asyncpg'`** (or any other app dependency) with a traceback pointing at a path like `~/.local/lib/python3.X/site-packages/...` instead of `<checkout>/venv/lib/...` — there's no `venv/` in this checkout, so `deploy.sh` silently fell back to a bare `python3` and installed packages outside any virtualenv. Fix:
  ```bash
  sudo -u netcontrol python3 -m venv venv
  sudo -u netcontrol ./deploy.sh
  ```
- **`ValueError: the greenlet library is required to use this function. No module named 'greenlet'`**, raised from inside SQLAlchemy during startup (`init_db()` / `engine.begin()`) — `requirements.txt` pins `sqlalchemy[asyncio]` specifically so a fresh install pulls in `greenlet` (the async-to-sync bridging library SQLAlchemy's async engine needs at runtime); a checkout on a version of `requirements.txt` from before this was added won't have it. Fix: pull the latest `requirements.txt` and reinstall:
  ```bash
  sudo -u netcontrol venv/bin/pip install -r requirements.txt
  sudo systemctl restart <SYSTEMD_SERVICE>
  ```

If a redeploy still doesn't come up, run `venv/bin/pip check` in the checkout to catch any other dependency mismatch, and confirm `ExecStart` in the systemd unit points at `<checkout>/venv/bin/uvicorn` (not a bare `uvicorn`) as shown above.

### Apache reverse proxy

See the `apache/` directory for a ready-to-use virtual host configuration with Let's Encrypt SSL.

```bash
sudo cp apache/netcontrol.example.conf /etc/apache2/sites-available/mysite.conf
# Edit ServerName and paths, then:
sudo a2ensite mysite
sudo systemctl reload apache2
sudo certbot --apache -d yourdomain.example.com
```

## First Run

The first user to register is automatically granted admin privileges. Subsequent registrations require admin approval before login is permitted.

## Public Live Page

A public, unauthenticated page showing all currently active nets is available at `/live`. Share this URL with club members or post it on your club website — it auto-refreshes every 30 seconds and shows the real-time check-in roster for each active net.

## Public Net Directory

A public, unauthenticated directory of nets is available at `/directory` — for browsing what nets exist and when they meet, as opposed to `/live`'s real-time check-in view. Net owners opt in per net from the Edit form (**List in Public Net Directory**); listed nets show their name, net type, frequency, description, weekly schedule, and owner callsign to anyone browsing, no login required. A net stays out of the directory by default.

## Net Repository Integration

Nets opted into the Public Net Directory (above) can also be pushed to [Net Repository](https://github.com/LadyHwesta/Net-Repository) — a separate, community-run central directory that multiple NetControl Online instances (and other tools) can publish to and be discovered from.

**Setup** — set `NET_REPOSITORY_URL` in `.env` (see `.env.example`); this app has no way to request or use a key without knowing which instance to talk to. Leave it blank to disable the integration entirely; nothing is sent anywhere. For the API key, two options:

- **Self-service (recommended)** — in **Admin → Net Repository**, fill in a name and submit **Request API Key**. This enters that Net Repository instance's admin review queue; click **Check Status** any time afterward, and once approved, the issued key is stored automatically and pushes start working immediately — no `.env` edit or restart needed. Use **Forget This Key** to clear a self-service key and start over.
- **Manual** — have the Net Repository instance's admin issue a key directly and set it as `NET_REPOSITORY_API_KEY` in `.env`. This always takes precedence over a self-service key if both are present.

Either way, the key's `instance_url` (configured on Net Repository's side, either by its admin or from what you submitted in the request form) is what Net Repository uses to tell instances apart and prevent duplicate submissions — not anything this app sends per-request.

**Once configured:**
- Creating or editing a net with **List in Public Net Directory** checked pushes it — every save, not just the first one, so schedule changes, a new description, and the fields below all stay in sync with the directory automatically.
- Re-pushing a net Net Repository already knows about (matched by this instance + the net's local ID) updates that entry in place — directly if it's already published, or in the moderation queue if it's still pending review.
- The push is fire-and-forget — a Net Repository outage or misconfiguration never blocks creating or editing a net locally; failures are logged, not surfaced to the user.

**Optional directory metadata** — all preferred, none required, available on the Edit form: **Band** (e.g. "2m"), **Mode** (e.g. "FM"), **CTCSS Tone**, **Region** (e.g. a county or metro area), **State**, and a **Website**. Leaving Website blank falls back to the org-wide website set in Admin → Branding.

**Session stats** — ending a session on a public-listed net also logs that session's check-in count and date to Net Repository (`POST /nets/stats`), the same fire-and-forget way as the net listing push itself. Net Repository rolls these up into each net's session count, average check-ins, and last-session date on its directory page. Only meaningful once the net is actually published there — if it's still pending moderation (or the push hasn't happened yet), Net Repository returns a 404 that's logged quietly and never surfaces to the user or blocks ending the session.

**Backfilling nets that existed before this integration:**

```bash
python3 push_to_net_repository.py
```

Pushes every currently-public net. Safe to re-run — nets already on Net Repository get their listing refreshed rather than duplicated.

## Mobile App (PWA) & Net Control Mode

The app is installable — most mobile and desktop browsers offer an "Add to
Home Screen" / "Install" prompt, and it launches full-screen with no browser
chrome. A service worker precaches the app shell so it still loads with no
connection, and installability is site-wide (every page registers it), not
just the check-in flow.

**📱 Net Control** — a toggle button on the live session view (next to End
Session) strips the check-in screen down to a big callsign field, a large
Check In button, and a minimal check-ins list — the net script panel,
Expected Stations, Digital Voice Last Heard, and secondary table columns are hidden.
Meant for running a net one-handed from a phone. The preference persists
across sessions (per device).

**Offline check-ins** — a check-in submitted with no connection is queued
locally rather than lost, shown in a banner with a manual **Retry Now**
button, and sent automatically the moment the connection returns (an
`online` event listener, checked every 15 seconds while a session is live —
works on every browser, including iOS Safari). On browsers that support the
Background Sync API, the service worker also gets a best-effort extra chance
to flush the queue if the tab is backgrounded or closed while offline — a
bonus, not the primary guarantee. A check-in that fails for a reason retrying
won't fix (an expired session, expired login) stops retrying and surfaces in
the banner for manual attention instead of queuing forever.

## Net Control Script

Net owners can attach a script to a net from its **Edit** form — a **Net Script** field below Description. It's shown in a collapsible **📜 NET SCRIPT** panel pinned to the top of the live check-in screen (open by default whenever a script is set), so you don't need a second window or a printed sheet next to the keyboard.

### Markup

A small, deliberately limited set of formatting is supported — write it in any external editor and paste it in:

| Syntax | Result |
|--------|--------|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `# Heading`, `## Heading`, `### Heading` | three heading sizes |
| `- item` or `* item` | bullet list |
| `---` or `===` (alone on a line) | horizontal rule |

Anything else — blank lines, indentation, plain text — renders exactly as typed. This is not full Markdown or HTML; unrecognized syntax (including literal `<` / `>`) is shown as plain text rather than interpreted, so a script can never inject markup or scripts of its own.

### Variables

`{{variable}}` placeholders are substituted with live session info when the script is displayed:

| Variable | Value |
|----------|-------|
| `{{net_name}}` | The net's name |
| `{{net_control}}` | Net Control name — callsign |
| `{{net_control_callsign}}` / `{{net_control_name}}` | Just the callsign / just the name |
| `{{broadcaster}}` | Broadcaster name — callsign (only meaningful on nets with [Additional Broadcast](#broadcaster-role-additional-broadcast) enabled) |
| `{{broadcaster_callsign}}` / `{{broadcaster_name}}` | Just the callsign / just the name |
| `{{broadcast_label}}` | The net's custom broadcast name (e.g. "Amateur Radio Newsline") |
| `{{net_control_next}}` | **Next week's** Net Control name — callsign, from the Schedule tab |
| `{{net_control_next_callsign}}` / `{{net_control_next_name}}` | Just the callsign / just the name |
| `{{broadcaster_next}}` | **Next week's** Broadcaster name — callsign |
| `{{broadcaster_next_callsign}}` / `{{broadcaster_next_name}}` | Just the callsign / just the name |

Net Control falls back to whoever actually started the session if no one signed up on the Schedule tab for that date; Broadcaster is only filled from a Schedule sign-up (there's no session-operator fallback, since a session has one operator but two possible roles). The `_next` variables look at the date exactly one week after this session and are **never** filled by fallback — next week hasn't happened yet, so there's no operator to fall back to — they stay blank until someone actually signs up on the Schedule tab. An unrecognized `{{...}}` is left as-is rather than silently dropped, so a typo is easy to spot. For example:

```
# Monday Night Net Script

Good evening, this is **{{net_control}}**, your net control operator
for the {{net_name}}.

Coming up: tonight's {{broadcast_label}} segment, read by {{broadcaster}}.

- If you would like to check in, please call now with your callsign.
- Traffic? Let us know when you check in.

---

Next week's net control will be {{net_control_next}}.

Thank you all for checking in. This net is now closed.
```

Leave the field blank to hide the panel entirely.

## Roles & Self-Service Sign-Up

Every registered operator has a base org-management tier — **Admin** (approving members, branding, etc.) or plain member — separate from three participant roles they can hold in any combination: **Net Control Op** (everyday net access; this is what "Member" used to be called, with identical privileges), **Tactical Operator**, and **Broadcaster**. All three are independently toggleable, for anyone, from the Operators list in Admin — click a role badge to grant or revoke it, the same for all three (an Admin can hold Net Control Op too, e.g. a founder who also runs nets). The registration form lets a new operator flag which they're interested in as a hint; the org admin decides what's actually granted, defaulting Net Control Op to checked (the normal case) on the pending-approval queue.

Holding a role at the org level only makes it *offerable* — it still has to be granted per net via that net's **🔗 Sharing** section (Edit Net → Sharing), the same place edit rights (`Can edit`, i.e. Net Control Op access) are already granted. A user can hold different roles on different nets: Net Control Op on one, Tactical Operator on another, both on a third. Tactical Operator/Broadcaster sharing only ever offers a role the org admin already approved for that user — greyed out otherwise, right in the sharing picker — so revoking it at the org level takes it away everywhere at once. `Can edit` (Net Control Op's net-level grant) is the one exception and stays ungated, exactly as it's always been, independent of any org-level role.

A Tactical Operator or Broadcaster share is deliberately minimal:
- **Tactical Operator** can sign themselves on (and off) an available tactical position during a live activation on that net — see **🎯 My Assignments** in the sidebar — but, unlike a full Net Control Op share, can't assign the position to anyone else or bump whoever's already signed on to it.
- **Broadcaster** can see that net's schedule and self-signup for the **Broadcaster** slot specifically (not Net Control, not "Cover Both Roles") — also from **🎯 My Assignments**.

A Net Control Op share keeps every capability it already had — full net configuration, schedule management, assigning either role to someone else — nothing changes there.

## Broadcaster Role (Additional Broadcast)

Some nets carry a second segment alongside net control — for example, a member reading the latest **Amateur Radio Newsline** bulletin. Enable **Additional Broadcast** in the net's Edit form and give it a name (e.g. "Amateur Radio Newsline"); this adds a **Broadcaster** role to that net's Schedule sign-ups, separate from Net Control.

On the Schedule tab, each upcoming date shows Net Control and Broadcaster as independent sign-up slots — different operators can claim each one, or a single operator can claim **Cover Both Roles**. The net owner can assign either role to a registered operator the same way they assign Net Control today.

Whoever is signed up for a date appears — callsign and name — in the duty bar on the live check-in screen and on the public `/live` page, so anyone checking in (or watching the public page) can see who's running the net and who's carrying the broadcast that day. If no one has signed up for a date, Net Control on the public page falls back to whoever actually started the session.

**Broadcaster override at session start** — when the broadcaster isn't known until the net is about to begin, the **▶ Start New Session** form for a broadcast-enabled net includes an optional Broadcaster Override (callsign + name). Set it there and it takes precedence over that date's schedule sign-up for the duty bar, the public `/live` page, and `{{broadcaster}}`/`{{broadcaster_callsign}}`/`{{broadcaster_name}}` net script variables — leave it blank to use whoever's signed up as usual.

## Scheduled Net Reminders

Enable **Reminder Emails** in a net's Edit form and set a lead time (e.g. 30 minutes) to email whoever's signed up as Net Control or Broadcaster on the Schedule tab shortly before their net starts. Each role is reminded independently, using the email address they gave when signing up — there's no reminder if nobody's signed up for that date, since there's no one to email.

This is driven by `send_reminders.py`, a standalone script (not part of the running web app) meant to run frequently from cron:

```bash
python3 /opt/netcontrol/send_reminders.py
```

```cron
*/5 * * * *  /opt/netcontrol/venv/bin/python3 /opt/netcontrol/send_reminders.py \
             >> /var/log/nettracker/reminders.log 2>&1
```

It's safe to run every few minutes — each signup is only ever reminded once, tracked via a `reminder_sent_at` timestamp set the first time its reminder window is caught, so overlapping cron runs don't double-send. Uses the same `SMTP_*` settings in `.env` as the rest of the app; reminders are silently skipped (logged, not sent) if SMTP isn't configured.

## Web Push Notifications

A second, app-native channel alongside the email reminders above — real browser/OS notifications, reaching a user even when the app isn't open (as long as their browser is running). Two occasions:

- **The same signup reminder email covers** — anyone signed up as Net Control or Broadcaster gets a push too, using the exact same **Reminder Emails**/lead-time setting on the net's Edit form. No separate toggle for push; it's on whenever email reminders are.
- **Net Control rotation shift changes during an ARES/ACES activation** — push-only, since there's no signup email address for this occasion. Whoever's up next in the rotation queue gets a push shortly before their shift starts, matched to their account by callsign (a queued shift is always free-text callsign/name, same as the rotation itself already is — if the callsign doesn't match a registered account, there's simply no one to push to, same as today).

Users opt in themselves from **⚙️ Account** — a **Notifications** card with an **Enable push notifications** checkbox (hidden entirely if the server hasn't set up push at all, or the browser doesn't support it) and a **Send Test Notification** button to confirm it's working right away, without waiting for a real reminder.

Setup (one time, server-side):

```bash
pip install pywebpush
python3 -c "
from py_vapid import Vapid01
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import base64
v = Vapid01(); v.generate_keys()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
print('VAPID_PUBLIC_KEY=' + b64(v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)))
print('VAPID_PRIVATE_KEY=' + b64(v.private_key.private_numbers().private_value.to_bytes(32, 'big')))
"
```

Paste the two printed lines into `.env`, plus `VAPID_CONTACT_EMAIL` (a `mailto:` address the browser push service can reach you at — required by the Web Push protocol, never shown to users). No app restart needed beyond picking up the new `.env` values; existing `send_reminders.py` cron setup automatically starts sending push alongside email once these are set. Leave all three unset to disable entirely — the Account page's Notifications card hides itself and nothing else changes.

## Fediverse (ActivityPub)

Each organization can have its own Fediverse account (works with Mastodon and any other ActivityPub-speaking server) that posts automatically:

- **When a net session starts** — an announcement with the net's name and frequency.
- **When it ends** — a summary (check-in count, duration).

Both use the same **Reminder Emails**-adjacent settings on a net's Edit form: a new **Announce on Fediverse** checkbox, independent of email reminders. Skipped entirely for a backfilled/logged-past ("Log a Past Net") session — announcing something that already happened days ago would be misleading on a public timeline.

This is a real, native ActivityPub actor (`@orgslug@yourdomain`, not a bridge through a third-party Mastodon account) — WebFinger discovery, a signed actor document, and HTTP-Signature-verified follows, so anyone on Mastodon can search for and follow it directly.

**Requires `APP_BASE_URL`** to be set (see above) — actor and post URLs must be stable absolute HTTPS links, so this is the one feature that makes that setting mandatory rather than optional. With it set, an org admin turns Fediverse participation on from **Admin → Organization**'s **🐘 Fediverse** card, which generates the org's signing keypair the first time and shows its `@handle` and follower count. Turning it back off just stops posting — the keypair and follower list are kept, so turning it on again later resumes posting to the same followers rather than starting over.

Delivery is **best-effort, single-attempt** — a follower whose server is briefly unreachable simply misses that one post; there's no retry queue. This never blocks or fails a session start/end either way.

## Digital Voice Integration

Net owners can configure digital voice last-heard data in the net's Edit form, under **📻 Digital Voice Integration**. Covers **DMR, D-Star, YSF (Yaesu Fusion), NXDN, P25, and M17** — pick a **Mode**, then a source: **WPSD** or **Pi-Star** hotspot (any mode), or **BrandMeister** (DMR-only network API, by talk group). A WPSD/Pi-Star hotspot's last-heard feed reports whichever mode(s) it actually hears, tagged per-entry, so switching Mode on an already-configured hotspot just changes what's shown — no reconfiguration of the hotspot itself needed.

### Fetch modes

| Mode | How it works | Use when |
|------|-------------|----------|
| **Proxy** (default) | Server fetches the hotspot URL | Hotspot is internet-accessible |
| **Direct** | Browser fetches the hotspot directly | Hotspot is on local LAN; browser has CORS/insecure-content permissions set |
| **Relay script** | Small Python script on the LAN pushes data to the server | Hotspot is local-only and CORS is blocked (most common home setup) |

### Relay script

If your hotspot is on a local network and browser CORS restrictions prevent direct fetching, download `dmr_relay.py` from the net's Digital Voice config section in the app. It runs on any machine that can reach the hotspot (the Pi itself works well) and pushes last-heard data to the server every 30 seconds — for whichever mode(s) the hotspot reports; the server filters to what the net is configured for.

**Setup:**

1. Go to **🪙 API Tokens** in the sidebar and create a token (e.g. "Digital Voice Relay - shack Pi"). Copy the token — it is shown only once.
2. Download `dmr_relay.py` from the net's Digital Voice config section.
3. Paste the token into the `API_TOKEN` line in the script.
4. Run it on any machine that can reach the hotspot:

```bash
sudo apt install python3-requests   # on Raspberry Pi / WPSD
python3 dmr_relay.py
```

The script uses a long-lived API token (no password stored, no re-authentication needed). Leave it running for the duration of the net. The app shows "Via relay script (Xs ago)" in the panel when using cached relay data. Revoke the token any time from the API Tokens page.

## APRS Station Map

Net owners (ham nets only — GMRS has no APRS allocation) can show a live map of stations reporting APRS beacons, on the net's Edit form under **🗺️ APRS Map**. Two source types are supported:

| Source | How it works | Use when |
|--------|-------------|----------|
| **aprs.fi** | Server polls the [aprs.fi](https://aprs.fi/page/api) API for whoever's currently checked in | You want zero extra infrastructure. Needs a free API key — see below |
| **Relay script** | `aprs_relay.py` speaks real APRS-IS and pushes positions to the server | Online (public APRS-IS network) or offline (local Direwolf/TNC/igate) — same script either way |

A **filter callsign** (defaults to the net owner's callsign) excludes your own NCS station from the map. A separate **"Show map on public live page"** toggle controls whether positions also appear on the public, no-login live page — configuring APRS and exposing it publicly are deliberately two different decisions, since field-team positions can be sensitive. The authenticated live-session map shows regardless — it works even on a net with no APRS source configured at all, for manually-reported positions (below).

**aprs.fi API key** is set once per organization, not per net — under **Admin → Organization → Your Organization**, shared by every net in your org using aprs.fi as its source. [aprs.fi's terms](https://aprs.fi/page/api) require crediting them as the data source with a link back whenever their data is shown; the map does this automatically (bottom-right corner, alongside the OpenStreetMap credit) whenever a net's source is aprs.fi — no separate setup needed.

**Manually-reported positions** — an operator with no APRS capability who can read off their own GPS coordinates over the air can still show up on the map. Click the 📍 next to any checked-in callsign (in the live check-in list) to set or edit their position — works independently of APRS setup, and after the check-in itself, not just at check-in time. Shown on the map in a distinct color from real APRS-tracked stations.

### APRS relay script

Download `aprs_relay.py` from the net's APRS config section — it comes pre-filled with this server's URL, the net ID, and your callsign, so you only need to paste an API token.

**Setup:**

1. Go to **🪙 API Tokens** in the sidebar and create a token (e.g. "APRS Relay"). Copy the token — it is shown only once.
2. Download `aprs_relay.py` from the net's APRS config section.
3. Run it, pointed at either the public APRS-IS network or a local igate:

```bash
pip install requests   # or: sudo apt install python3-requests

# Online — public APRS-IS network, watching specific field-team callsigns
python3 aprs_relay.py --token nt_YOUR_TOKEN --my-callsign W1AW \
    --callsigns W1AW-9,K1ABC-9,N1XYZ-9

# Offline — local Direwolf/TNC/igate on the LAN
python3 aprs_relay.py --token nt_YOUR_TOKEN --my-callsign W1AW \
    --host 192.168.1.10 --port 8001
```

`--callsigns` builds an APRS-IS server-side buddy filter so the feed isn't a firehose — omit it only when pointed at a small local igate feed you already trust. Leave it running for the duration of the net; revoke the token any time from the API Tokens page.

Only standard *uncompressed* position packets are parsed in this version — compressed-format and Mic-E packets aren't decoded yet, and the map always shows each station's latest known position rather than a historical track.

### API Tokens

Long-lived API tokens are available for service accounts and scripts. They are prefixed with `nt_` and work anywhere a Bearer token is accepted. Tokens are stored as SHA-256 hashes — the raw value is shown only at creation.

- **Create:** `POST /auth/tokens` `{"name": "label"}`
- **List:** `GET /auth/tokens`
- **Revoke:** `DELETE /auth/tokens/{id}`

### Application Logs

By default the app logs to stdout/stderr only — under systemd that means `journalctl -u nettracker`. Set `LOG_FILE=/var/log/nettracker/app.log` in `.env` to also write every log line to a plain file, for tailing/grepping without journalctl access (auto-reopens if logrotate moves the file out from under it). `LOG_LEVEL` (default `INFO`) controls verbosity either way — `DEBUG`/`INFO`/`WARNING`/`ERROR`.

### fail2ban integration

Set `AUTH_LOG_FILE=/var/log/nettracker/auth.log` in `.env` to write structured auth failure lines:

```
2026-08-13T19:42:01 AUTH_FAIL ip=1.2.3.4 reason=bad_credentials username='W1AW'
```

Example fail2ban filter (`/etc/fail2ban/filter.d/nettracker.conf`):

```ini
[Definition]
failregex = AUTH_FAIL ip=<HOST>
ignoreregex =
```

And jail entry:

```ini
[nettracker]
enabled  = true
port     = http,https
filter   = nettracker
logpath  = /var/log/nettracker/auth.log
maxretry = 5
bantime  = 600
```

Create the log directory first: `sudo mkdir -p /var/log/nettracker && sudo chown netcontrol: /var/log/nettracker`

## Evacuation Zone Data (issue #27)

ARES/ACES nets track which evacuation zone each checked-in station reported (Zone Roster panel, per-callsign free text) — that keeps working exactly as before. On top of it, a net can now sync the *actual* current zone boundaries from an external government GIS API, so operators pick from a real, authoritative list instead of only whatever's been typed before, and the zone map shows the real shape of each zone.

Two different kinds of source, and a net can sync from both at once:

- **Statewide, active-incidents-only** — currently **California** ([data.ca.gov's evacuation aggregation layer](https://data.ca.gov/dataset/california-evacuation-aggregation-layer)), a public ArcGIS FeatureServer, no API key. Set the net's **State** field to `CA` (or `California`). This feed only ever contains a zone while it's *actively* under some status (order/warning/shelter-in-place) — it's empty the moment nothing's happening, which is correct for "what's active right now" but not useful for picking a zone on an ordinary, non-activation net.
- **County/city, full static catalog** — currently **Sonoma County, CA**. Set the net's **Region** field to `Sonoma` (or `Sonoma County`). Unlike the statewide feed, these always contain every predefined zone — hundreds of them, each showing `Normal` status outside an incident — so it's what makes zone selection actually useful on a routine net, not just during an activation. Two separate sources both match a Region of "Sonoma County" and sync together: the county's own "Know Your Zone" GIS system, and the **City of Santa Rosa**'s own Zonehaven-based system — Santa Rosa runs its own separate system and is explicitly excluded from the county's own layer ("...for all unincorporated areas and cities with the exception of zones for the City of Santa Rosa," straight from that layer's own description), so both are needed together for full coverage of every city in the county. A handful of zones in the source data have no name at all — those fall back to a short boundary description ("North of City Limits, East of...") pulled from the same data, rather than showing nothing but a bare status.

Both are matched from the same **State**/**Region** fields on the net's Edit form (free text; `Region` tolerates a trailing "County"). Sync is **on-demand only, not automatic** — click **🔄 Sync Evacuation Zones** in the Zone Roster panel on the live check-in screen. The statewide source updates roughly every 5 minutes during an active incident, so a scheduled/cron sync would routinely be stale right when it matters most; a manual sync always pulls the current data at the moment you need it. Each sync replaces the net's stored zones per source (retired/merged zones simply disappear); syncing again is always safe.

**Adding another source**: `evac_zone_sources.py` holds a small registry (`SOURCES`) of one function per data source — write a `fetch_*` function for the new API (its own field names baked in, since these vary) and register it, alongside an entry in `STATE_ALIASES` (statewide) or `COUNTY_ALIASES` (county-level) mapping the net's **State**/**Region** field to it. No schema or router changes needed — a net can pull from any number of matching sources at once, since `EvacZoneBoundary` is keyed on `(net, source, external_id)` specifically so sources never collide. There's no single "all zones, everywhere" resource to point at — each county or city that runs this kind of system publishes its own separate service (and a county's own layer may not cover every city inside it — confirmed firsthand: Sonoma County's own layer explicitly excludes the City of Santa Rosa, which needed its own separate source added), so this list only grows one hand-verified jurisdiction at a time.

**Your county/state isn't covered, or a zone is missing?** Open a GitHub issue with your zone's name and jurisdiction (city/county/state) — [see #29](https://github.com/LadyHwesta/netcontrol-online/issues/29) for the intake path. Each new source needs its GIS service hand-verified (URL, field names, and whether it's the full catalog or active-incidents-only) the same way California, Sonoma County, and Santa Rosa already were.

## Incident Reporting (issue #28)

Tracks an incident that doesn't need a full net activation — a localized fire, flooding, a road closure — without spinning up a `NetSession` at all. Lives under the new **🚨 Incidents** sidebar link.

**Affected area** is one or more of a net's already-synced real evacuation zone boundaries (see Evacuation Zone Data above), selected when creating or editing an incident — never a freehand-drawn shape, so the geometry is always real, current, government-sourced data. Sync a net's zones first (Zone Roster panel on the live check-in screen) if the zone picker is empty.

**"🔄 Scan for Affected Stations"** looks for potentially affected stations using two signals, since there's no reliable persisted "where is this station right now" data anywhere in this app otherwise:

- **Zone report** — the free-text zone name a station reported at check-in (Zone Roster's own per-callsign roster) matched against the incident's selected zone(s). Works for any net with check-in history, no coordinates needed.
- **Position** — real point-in-polygon matching against each callsign's most recent manually-reported GPS position (`PATCH /checkins/{id}/position`), searched across the whole organization's check-in history from the last 14 days — not just the currently-open session. Live APRS positions are **not** matched in this version — they're only available while a session happens to be open, which an incident that doesn't need a full activation often won't have.

A scan never overwrites or removes a station you've already added or edited — re-scanning as new check-ins come in is always safe. Stations can also be added or removed by hand. Each station has a status (`Not Contacted` → `Attempted` → `Contacted` → `Confirmed Safe` / `Needs Assistance`) and a free-text notes field for their situation, both editable independently — this is the "mini net" checklist.

**Public vs. backend split**: the public **🚨 Public Incident Map** (`/incident-map`, same org-slug convention as `/live` and `/directory`) shows every active incident's affected area on a map plus a station **count** — never a callsign, never contact info. The authenticated `/incidents` page is where the actual station list, status, and notes live.

## UI Translation (argos-translate)

Optional, off by default. Translates the app's own UI into other languages using [Argos Translate](https://github.com/argosopentech/argos-translate), an offline neural machine translation library — no third-party API, no data leaving the server.

**This is a genuinely heavy dependency.** `argostranslate` pulls in `ctranslate2`, `spacy`, and `stanza` — a real ML stack, not a lightweight dictionary lookup, next to everything else in `requirements.txt`. Each language you enable also downloads its own translation model. Test on a non-production instance first if you're on a small or resource-constrained server, and expect a noticeably larger install and higher RAM usage than the rest of this app needs.

**Enabling it:**

1. Set `TRANSLATION_ENABLED=true` in `.env` and install the optional dependency:
   ```bash
   venv/bin/pip install argostranslate
   ```
   (already listed, commented as optional, in `requirements.txt` — a plain `pip install -r requirements.txt` skips nothing extra unless you also flip the env var and actually enable a language)

   `argostranslate` pulls in `torch` transitively (via `spacy`/`stanza`) — a 500MB+ wheel on its own. `pip` downloads it into `$TMPDIR` (usually `/tmp`) before installing, and on many cloud VPS images `/tmp` is its own small `tmpfs` mount (RAM-backed, often under 1GB) completely separate from the disk space `df -h` shows free on `/` — `pip install` can fail with `[Errno 28] No space left on device` even with tens of GB free overall. If that happens, point `TMPDIR` at a disk-backed directory for the install:
   ```bash
   mkdir -p ~/pip-tmp
   TMPDIR=~/pip-tmp venv/bin/pip install argostranslate
   rm -rf ~/pip-tmp
   ```
2. Restart the app, then run `migrate.py` if this is an existing install (adds `translation_cache`, `enabled_languages`, `org_enabled_languages`, and a `language` column on `users` — a fresh install creates all four automatically on first startup).
3. Any **org admin** — not just a super admin — can enable a language for their own organization: in **Admin → Languages**, enter a language code (e.g. `es`, `fr`, `de` — any [argos-translate-supported](https://github.com/argosopentech/argos-translate#supported-languages) code) and a display name, then click **Enable Language**. This kicks off a background job that downloads that language's model and pre-translates the app's known UI strings — can take a few minutes the first time *any* org on this server enables that language; a second org enabling the same code reuses the already-installed model instantly, no re-download. The tab shows Pending → Installing → Ready.

**Per-organization, not shared:** enabling a language is scoped to one org — Org A turning on Spanish doesn't put Spanish in Org B's switcher, and disabling it only removes Org A's own access. The underlying installed model and its translated-text cache are shared server-wide (so the work of installing/pre-translating a language only ever happens once), but which languages actually show up for a given org's users is fully independent. A super admin can additionally see the full server-wide catalog of installed models (which orgs use each one) and hard-uninstall one entirely via `GET`/`DELETE /admin/languages`.

**How it works:** translation is a cache, not a build step — the English text itself is the cache key (hashed), so there's no separate translation-key file to keep in sync with the actual wording. Once a language is Ready, its strings serve instantly from the database; any brand-new string not yet in the cache falls back to English for that one view and translates itself in the background for next time. A visitor's browser language is auto-detected and applied automatically the first time they visit, but only if that language has already been enabled for whichever org context applies (or by any org, for a visitor with no org context yet, like the login screen) — nothing is ever auto-translated into a language nobody turned on.

**Coverage today** is the login/registration screen and the shared navigation — not yet the full app (nets, sessions, check-in flow, admin panel, etc.). See `TECH_DEBT.md` for what's tracked as follow-up. Net scripts, welcome messages, and announcements can also be translated on demand via a Translate button wherever they're edited, independent of the UI-chrome coverage above.

Machine translation isn't perfect, especially for ham-radio-specific terms (ARES, ICS-205, callsign, etc.) — a small "🌐 via Argos Translate" credit appears next to the language switcher whenever a translated language is active, so it's always clear the wording was machine-generated.

## Contributing

Pull requests are welcome. For major changes please open an issue first to discuss what you'd like to change.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -am 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a pull request

## License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

See the [LICENSE](LICENSE) file for the full license text.

## 73

Built for the ham radio community. If you deploy this for your club, we'd love to hear about it — open an issue and tell us where it's running!

## ☕ Support Hosting

This app is free to use. If it's been helpful to you, a small contribution helps keep the demo and hosting running:

- **Venmo**: [@TiesaMM](https://venmo.com/TiesaMM)
- **PayPal**: [paypal.com/ncp/payment/RJ645T8FJA8KU](https://www.paypal.com/ncp/payment/RJ645T8FJA8KU)
