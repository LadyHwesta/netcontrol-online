# Quickstart

Get NetControl Online running and take your first check-in in a few minutes. This is the fast path — for the full feature list, production deployment (systemd, Apache, migrations), and every configuration option, see the [README](README.md).

## 1. Prerequisites

- Python 3.11+
- Anything beyond a quick local trial: PostgreSQL 14+ (see [step 4](#4-set-up-a-database))

## 2. Get the code

```bash
git clone https://github.com/LadyHwesta/netcontrol-online.git
cd netcontrol-online
```

## 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Set up a database

**Fastest — SQLite, no server to install.** Good for trying the app out locally. Skip straight to [step 5](#5-configure-env) and set:

```
DATABASE_URL=sqlite:///./netcontrol.db
```

**PostgreSQL — for anything you'll keep running.** `migrate.py` (used for every future schema update) only speaks Postgres, so switch to this before you rely on the data:

```bash
sudo -u postgres psql
```
```sql
CREATE USER netcontrol WITH PASSWORD 'yourpassword';
CREATE DATABASE netcontrol OWNER netcontrol;
\q
```

Then set `DATABASE_URL=postgresql://netcontrol:yourpassword@localhost:5432/netcontrol` in `.env`. Full production setup (systemd service, Apache reverse proxy, migrations) is in the README's [Installation](README.md#installation) and [Deployment](README.md#deployment) sections.

## 5. Configure `.env`

```bash
cp .env.example .env
```

Two settings to fill in before the app will run at all:

- `DATABASE_URL` — from step 4
- `SECRET_KEY` — any long random string, e.g.:
  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```

Everything else in `.env.example` (email, bot protection, DMR, Net Repository — see [below](#setting-up-a-net-repository-api-token)) is optional and can be added later; the app runs fine without any of it.

## 6. Create the schema

```bash
python3 -c "from database import init_db; init_db()"
```

This creates every table fresh — no migration step needed on a new install.

## 7. Run it

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open **http://127.0.0.1:8000**.

## 8. Create your account

Click **Register**. The first account ever created on an instance is automatically approved and made a Super Admin — no one else exists yet to approve it.

You'll also be asked to create or join an **Organization** — nets, sessions, and check-ins are scoped per-organization, so pick **Create new**, give it a name and a website URL, and you're in.

## 9. Log your first net

- **➕ New Net** → give it a name → **Save**.
- Open the net, then **▶ Start New Session**.
- Type a callsign into the check-in box and hit Enter — that's a check-in.
- **■ End Session** when the net's done; you'll get a session summary and a printable ICS-205.

That's the core loop. Everything else — sharing, scheduling, ARES activations, DMR integration, the public directory, PWA install — layers on top of it; see the README's [Features](README.md#features) list for the full picture.

## What's next?

- **Brand it** — Admin → Branding: organization name, tagline, logo.
- **Add your team** — Admin → Operators → **Add Operator** creates an account directly (they get an email to set their own password), or have them self-register and approve them from the same page.
- **List a net publicly** — check **List in Public Net Directory** on a net's Edit form; it shows up at `/directory` with no login required.
- **Turn on email** — fill in `SMTP_*` in `.env` so approvals, reminders, and notifications actually send.
- **Turn on bot protection** — set `CAPTCHA_PROVIDER` in `.env` (Turnstile, reCAPTCHA, or the self-contained ALTCHA — see `.env.example`).
- **Go to production** — systemd service, Apache + Let's Encrypt, and the branching model for a stable + testing instance are all in the README's [Deployment](README.md#deployment) section.

## Setting Up a Net Repository API Token

[Net Repository](https://github.com/LadyHwesta/Net-Repository) is a separate, community-run central directory — nets you've listed in your own Public Net Directory can also be pushed there so they're discoverable beyond this one instance. It's entirely optional.

1. **Point at an instance.** Set `NET_REPOSITORY_URL` in `.env` to the Net Repository instance you want to publish to (ask its admin for the URL, or run your own from the [Net Repository repo](https://github.com/LadyHwesta/Net-Repository)), then restart the app. Leave it blank to keep this feature off entirely — nothing is sent anywhere.
2. **Request a key.** In the app, go to **Admin → Net Repository**, fill in a name (shown to that instance's admin for review), and click **Request API Key**.
3. **Wait for approval.** This enters that Net Repository instance's moderation queue. Come back and click **Check Status** any time — once approved, the key is stored automatically. No `.env` edit or restart needed.
4. **Publish a net.** Check **List in Public Net Directory** on any net's Edit form (and optionally fill in Band/Mode/CTCSS Tone/Region/State/Website) — every save from then on pushes the current details to Net Repository automatically.

Already had public nets before setting this up? Backfill them all in one shot:

```bash
python3 push_to_net_repository.py
```

Safe to re-run — existing listings get refreshed, not duplicated. See the README's [Net Repository Integration](README.md#net-repository-integration) section for the manual (non-self-service) key option and other details.
