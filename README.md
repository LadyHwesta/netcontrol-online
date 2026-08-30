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

- **Multi-tenancy** — every account belongs to one or more **Organizations**; nets, sessions, and check-ins are scoped so separate organizations sharing the same install never see each other's data. Registration offers create-a-new-organization (name + required website URL; needs a Super Admin's approval before login, since a founder can't approve themselves) or join-an-existing-one (needs that organization's own admin to approve you instead); an org switcher appears for anyone in more than one. Organization admins get a scoped Admin page for their own members; the existing site-wide Super Admin role is unchanged, still sees everything, and is the only registration path that's ever auto-approved (the instance's first-ever user, with no one else to ask)
- **Net & session management** — create nets, start/end sessions, log check-ins with signal reports
- **Log a past net** — a net that formed with no access to the web tool can be backfilled afterward: set its date/time, Net Control, and broadcaster up front, then add check-ins that get stamped with the reported date/time instead of "now". No live-session chrome (clock, Expected Stations, DMR, Net Script) since it was never live to begin with — just the check-in form and roster. Click **🔒 Close Log** when done entering data to stop accepting further check-ins
- **Focused live session view** — sidebar auto-collapses and session navigation hides while a session is live to cut clutter, restoring automatically once it ends; the manual check-in form stays pinned to the top of the screen so it's reachable while scrolling; the checked-in stations roster sits in its own independently-scrollable column on the right (callsign, name, traffic, delete — the 5 most recent highlighted for 20 seconds), so it's always visible without scrolling past the form
- **Callsign lookup** — FCC database lookup with local history suffix search
- **Traffic management** — flag stations with traffic, interactive "called" tracking (persists across a session close/reopen), formal traffic message log
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
- **Configurable branding** — set organization name, tagline, website URL, and logo from the Admin panel
- **Theme engine** — per-account color theme (LCARS, Dark, Light, High Contrast, or System/OS-matched), persisted server-side so it follows you across devices
- **DMR hotspot integration** — connect a net to a WPSD, Pi-Star, or BrandMeister talk group; see a live "last heard" panel during the session, quick-check-in heard stations, and log Talk Group + Region per check-in
- **Keyboard-friendly forms** — Enter submits the primary action from any save/submit form's text fields (multi-line fields like net description and report body are left alone so Enter still inserts a newline)
- **Installable mobile app (PWA)** — installable to a phone's home screen with an offline-capable app shell; a **📱 Net Control** toggle on the live session view strips the check-in screen down to a big callsign field and minimal chrome for one-handed net control. Check-ins submitted with no connection queue locally and send automatically once back online
- **Separate GMRS callsign** — operators holding both an amateur and a GMRS license can set a GMRS callsign under **⚙️ Account**; Net Control on a GMRS net automatically shows it instead of the amateur callsign — duty bar, net script variables, the public live page, and Schedule sign-ups all pick it up with nothing else to configure. Leave it unset to keep using the amateur callsign everywhere

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy (sync), PostgreSQL
- **Frontend**: Vanilla JS SPA (no framework), LCARS-inspired dark theme — CSS in `static/app.css`, JS split into feature modules under `static/js/`
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

Install `requirements-dev.txt` (which pulls in `requirements.txt` plus `pytest`) if this instance's `GIT_BRANCH` is `testing`, or if you plan to pass `deploy.sh --force-tests` — `deploy.sh` runs the test suite as a safety check before restarting in those cases, and fails with `No module named pytest` if only `requirements.txt` was installed. A `main` instance skips the suite by default (see "Branching model" below), so `requirements.txt` alone is enough there; install `requirements-dev.txt` anyway if you're not sure or just want it available.

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
ExecStart=/usr/bin/uvicorn main:app --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nettracker

[Install]
WantedBy=multi-user.target
```

`${PORT}` is substituted by systemd from `PORT` in `.env` (see `.env.example`) — the unit file itself doesn't need to change per instance.

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

#### Branching model

`main` is the stable branch; `testing` is where day-to-day commits land until they've been worked out. Point a stable instance's `deploy.sh` at `main` and a testing instance's at `testing` (via `GIT_BRANCH`, above) to keep the two separate. Merge `testing` into `main` only once a change is confirmed good.

`deploy.sh` only runs the test suite automatically when `GIT_BRANCH` is `testing` — a `main` instance skips it, since a change should already have passed on testing before being merged. Run `./deploy.sh --force-tests` to run the suite anyway on a `main` instance (e.g. right after a hotfix committed straight to `main`).

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
Expected Stations, DMR Last Heard, and secondary table columns are hidden.
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

## DMR Hotspot Integration

Net owners can configure DMR last-heard data in the net's Edit form. Three source types are supported: **WPSD**, **Pi-Star**, and **BrandMeister** (by talk group).

### Fetch modes

| Mode | How it works | Use when |
|------|-------------|----------|
| **Proxy** (default) | Server fetches the hotspot URL | Hotspot is internet-accessible |
| **Direct** | Browser fetches the hotspot directly | Hotspot is on local LAN; browser has CORS/insecure-content permissions set |
| **Relay script** | Small Python script on the LAN pushes data to the server | Hotspot is local-only and CORS is blocked (most common home setup) |

### DMR relay script

If your hotspot is on a local network and browser CORS restrictions prevent direct fetching, download `dmr_relay.py` from the net's DMR config section in the app. It runs on any machine that can reach the hotspot (the Pi itself works well) and pushes last-heard data to the server every 30 seconds.

**Setup:**

1. Go to **🪙 API Tokens** in the sidebar and create a token (e.g. "DMR Relay - shack Pi"). Copy the token — it is shown only once.
2. Download `dmr_relay.py` from the net's DMR config section.
3. Paste the token into the `API_TOKEN` line in the script.
4. Run it on any machine that can reach the hotspot:

```bash
sudo apt install python3-requests   # on Raspberry Pi / WPSD
python3 dmr_relay.py
```

The script uses a long-lived API token (no password stored, no re-authentication needed). Leave it running for the duration of the net. The app shows "Via relay script (Xs ago)" in the DMR panel when using cached relay data. Revoke the token any time from the API Tokens page.

## APRS Station Map

Net owners (ham nets only — GMRS has no APRS allocation) can show a live map of stations reporting APRS beacons, on the net's Edit form under **🗺️ APRS Map**. Two source types are supported:

| Source | How it works | Use when |
|--------|-------------|----------|
| **aprs.fi** | Server polls the [aprs.fi](https://aprs.fi/page/api) API for whoever's currently checked in | You want zero extra infrastructure and don't mind a third-party API key |
| **Relay script** | `aprs_relay.py` speaks real APRS-IS and pushes positions to the server | Online (public APRS-IS network) or offline (local Direwolf/TNC/igate) — same script either way |

A **filter callsign** (defaults to the net owner's callsign) excludes your own NCS station from the map. A separate **"Show map on public live page"** toggle controls whether positions also appear on the public, no-login live page — configuring APRS and exposing it publicly are deliberately two different decisions, since field-team positions can be sensitive. The authenticated live-session map is unaffected either way.

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
