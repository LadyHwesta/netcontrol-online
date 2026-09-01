#!/bin/bash
# deploy.sh — pull latest from GitHub and restart the service
#
# Usage:  ./deploy.sh [--force-tests]
#
#   --force-tests   Run the test suite even if GIT_BRANCH isn't "testing".
#                   By default the suite only runs automatically on a testing
#                   instance -- a stable (main) instance skips it, since a
#                   change should already have been proven out on testing
#                   before being merged to main. Use this flag to run it
#                   anyway on a main instance (e.g. after a hotfix straight
#                   to main).
#
# Supports running multiple instances of this app from separate checkouts on
# one server (e.g. a stable instance + a testing instance) — each instance's
# .env sets its own SYSTEMD_SERVICE, PORT (used by the systemd unit's
# ExecStart line), and GIT_BRANCH (which branch this instance tracks — e.g.
# "main" for stable, "testing" for pre-release), so the same deploy.sh works
# unmodified for every instance. Falls back to "nettracker" / "main" if
# unset. See "Deployment" in README.md.
#
# Every run backs up this instance's own database (into ./backups/, keeping
# the most recent 14) before touching anything -- migrate.py (below) only
# ever adds/alters, it never drops data, but a mistyped path or a stray
# script run against the wrong instance (ask us how we know) can still wipe
# a database outright, and this app has no other backup mechanism of its
# own. Cheap insurance for something that otherwise isn't recoverable.
#
# Prerequisites:
#   1. The netcontrol user must be allowed to restart the service without a
#      password. Add to /etc/sudoers (via sudo visudo), substituting your
#      SYSTEMD_SERVICE value:
#        netcontrol ALL=(ALL) NOPASSWD: /bin/systemctl restart nettracker
#   2. Postgres instances need pg_dump on PATH (same package that provides
#      psql -- already required to set the database up in the first place).
#
set -e
# pipefail so a failed `pg_dump | gzip` (below) aborts the deploy rather
# than silently continuing on gzip's own exit status alone -- the whole
# point of the backup step is to stop before anything destructive runs if
# the backup itself couldn't be trusted.
set -o pipefail

FORCE_TESTS=false
for arg in "$@"; do
  case "$arg" in
    --force-tests) FORCE_TESTS=true ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--force-tests]" >&2
      exit 1
      ;;
  esac
done

cd "$(dirname "$0")"

# Read SYSTEMD_SERVICE/GIT_BRANCH from .env without sourcing the whole file —
# values in there aren't guaranteed to be safe shell syntax.
SYSTEMD_SERVICE=$(grep -E '^SYSTEMD_SERVICE=' .env 2>/dev/null | tail -1 | cut -d= -f2-)
SYSTEMD_SERVICE="${SYSTEMD_SERVICE:-nettracker}"

GIT_BRANCH=$(grep -E '^GIT_BRANCH=' .env 2>/dev/null | tail -1 | cut -d= -f2-)
GIT_BRANCH="${GIT_BRANCH:-main}"

DATABASE_URL=$(grep -E '^DATABASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2-)

# Back this instance's database up before anything below can touch it --
# named with SYSTEMD_SERVICE so multiple instances backing up to the same
# shared location (if ever pointed there) can't collide or get pruned into
# each other. gitignored (see .gitignore); never committed.
#
# main only -- a testing instance's data is expected to be disposable (see
# README's "Branching model"), so there's nothing there worth spending
# backup storage/dump time on, and it also means a demo/testing instance's
# data stays unrecoverable if something like this happens to it -- same as
# it always was.
if [ "$GIT_BRANCH" = "main" ]; then
  echo "Backing up database..."
  BACKUP_DIR="backups"
  mkdir -p "$BACKUP_DIR"
  TIMESTAMP=$(date +%Y%m%d-%H%M%S)

  if [[ "$DATABASE_URL" == postgresql://* || "$DATABASE_URL" == postgres://* ]]; then
    BACKUP_FILE="$BACKUP_DIR/${SYSTEMD_SERVICE}-${TIMESTAMP}.sql.gz"
    pg_dump "$DATABASE_URL" | gzip > "$BACKUP_FILE"
    echo "✓ Backed up to $BACKUP_FILE"
  elif [[ "$DATABASE_URL" == sqlite:///* ]]; then
    SQLITE_PATH="${DATABASE_URL#sqlite:///}"
    if [ -f "$SQLITE_PATH" ]; then
      BACKUP_FILE="$BACKUP_DIR/${SYSTEMD_SERVICE}-${TIMESTAMP}.db"
      cp "$SQLITE_PATH" "$BACKUP_FILE"
      echo "✓ Backed up to $BACKUP_FILE"
    else
      echo "No existing SQLite database file at $SQLITE_PATH yet — nothing to back up (first deploy?)."
    fi
  else
    echo "⚠ DATABASE_URL not set or unrecognized in .env — skipping backup." >&2
  fi

  # Keep the most recent 14 backups for this instance -- filenames sort
  # chronologically as strings (YYYYMMDD-HHMMSS), so this is a plain
  # newest-first listing with no timestamp parsing needed. Deploying more
  # than ~14 times between recovery needs would be unusual; lower this if
  # disk space is tight and deploys are frequent. `|| true`: pipefail
  # (above) would otherwise treat grep matching nothing (no backups yet --
  # e.g. the very first deploy) as a pipeline failure and abort the deploy
  # over what is normal, expected, non-fatal state.
  (ls -1 "$BACKUP_DIR" 2>/dev/null | grep "^${SYSTEMD_SERVICE}-" | sort -r | tail -n +15 | \
    while read -r old; do rm -f "$BACKUP_DIR/$old"; done) || true
else
  echo "Skipping database backup (GIT_BRANCH=$GIT_BRANCH) — only main instances back up automatically."
fi

# Use this checkout's own virtualenv if one exists, rather than whatever
# `python3` happens to resolve to on the caller's PATH -- each checkout
# (e.g. production vs. a demo instance) has its own venv with its own
# installed deps, and deploy.sh shouldn't depend on the shell that invoked
# it having activated the right one.
PYTHON=python3
if [ -x venv/bin/python3 ]; then
  PYTHON=venv/bin/python3
fi

echo "Pulling latest from GitHub ($GIT_BRANCH)..."
git fetch origin "$GIT_BRANCH"
git checkout "$GIT_BRANCH" 2>/dev/null || git checkout -t "origin/$GIT_BRANCH"
git pull origin "$GIT_BRANCH"

# Always keep production deps current, not just on first setup -- a pulled
# commit can add/bump requirements.txt (e.g. a new DB driver) that the app
# itself needs to even start, regardless of whether the test suite runs
# below. Cheap when nothing changed: pip no-ops on already-satisfied pins.
echo "Installing/updating dependencies..."
"$PYTHON" -m pip install -q -r requirements.txt
echo "✓ Dependencies up to date"

if [ "$GIT_BRANCH" = "testing" ] || [ "$FORCE_TESTS" = true ]; then
  # Same reasoning as above, for test-only deps -- checking `import pytest`
  # only proves *something* was installed once, not that requirements-dev.txt
  # is current, so a dependency added since the last deploy (pytest-asyncio,
  # aiosqlite, ...) would silently be missing until someone noticed the
  # ImportError and reinstalled by hand.
  "$PYTHON" -m pip install -q -r requirements-dev.txt

  echo "Running test suite..."
  "$PYTHON" -m pytest tests/ -q
  echo "✓ All tests passed"
else
  echo "Skipping test suite (GIT_BRANCH=$GIT_BRANCH) — pass --force-tests to run it anyway."
fi

echo "Applying database migrations..."
"$PYTHON" migrate.py
echo "✓ Migrations applied"

echo "Restarting $SYSTEMD_SERVICE..."
sudo systemctl restart "$SYSTEMD_SERVICE"

echo "✓ Deployed $(git rev-parse --short HEAD) to $SYSTEMD_SERVICE"
