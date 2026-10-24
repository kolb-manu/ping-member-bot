#!/usr/bin/env bash
# Sets up the First In member bot as a background service (first-in-member-bot)
# that restarts itself and survives reboots. Same pattern as the alert bot's setup.sh.
# Works on Google Cloud (Debian/Ubuntu) and Raspberry Pi OS. Run it as your normal
# user, not root: it asks for your password when it needs sudo.
#
# Safe to run again after a git pull: it updates the Python packages, stops the
# running service, saves a copy of the database (backups/before-deploy-*.db),
# re-runs the preflight check and starts the service again. Each version it starts
# is added to data/deploy-history.log, which DEPLOY.md's rollback uses.
# It never prints anything from .env.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"
SERVICE="first-in-member-bot"
UNIT="/etc/systemd/system/${SERVICE}.service"
# DB_PATH's default. DEPLOY.md's backup and restore steps use the same path.
DB="data/first-in.db"
HISTORY="data/deploy-history.log"
KEEP_BEFORE_DEPLOY=10
COPY="-"           # this run's before-deploy copy, if it made one

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (bash setup.sh), not as root or with sudo."
    exit 1
fi
# systemd's WorkingDirectory and ExecStart lines can't take a path with spaces.
case "$DIR" in
    *[[:space:]]*)
        echo "The folder path has a space in it ($DIR). Move the folder somewhere without spaces."
        exit 1
        ;;
esac

# Set once this run has stopped a running service, cleared once it has started it
# again. Whatever ends the run in between (a FAIL, an error, Ctrl+C, a closed SSH
# window), the owner is told the bot is down: Restart=always doesn't bring back a
# service that was stopped on purpose.
STOPPED_IT=0
say_if_stopped() {
    if [ "$STOPPED_IT" = 1 ]; then
        echo
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        echo "!!  The bot is STOPPED. Fix the problem and re-run bash setup.sh, or"
        echo "!!  start the old setup with: sudo systemctl start ${SERVICE}"
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    fi
}
trap say_if_stopped EXIT
# Turn the signals into a normal exit, so the EXIT trap above always runs.
trap 'exit 130' INT
trap 'exit 129' HUP
trap 'exit 143' TERM

# Online backup with sqlite3 (never cp: it could catch a write half done), private
# from the start. The name doesn't start with first-in-, so the nightly backups'
# pruning never touches it, and it sorts by time, which the pruning below relies on.
save_before_deploy_copy() {
    if [ ! -f "$DB" ]; then
        echo "==> No database at ${DB} yet, so there's nothing to save first."
        return
    fi
    COPY="backups/before-deploy-$(date -u +%Y%m%d-%H%M%S).db"
    echo "==> Saving a copy of the database first..."
    if ! (umask 077 && sqlite3 "$DB" ".backup ${COPY}"); then
        echo "Couldn't save a copy of the database to ${COPY}."
        exit 1
    fi
    chmod 600 "$COPY"
    echo "    Saved ${COPY} (times are UTC)."
    # Keep the newest few. The glob lists them oldest first.
    local copies=(backups/before-deploy-[0-9]*-[0-9]*.db)
    local extra=$(( ${#copies[@]} - KEEP_BEFORE_DEPLOY ))
    if [ "$extra" -gt 0 ]; then
        rm -f -- "${copies[@]:0:extra}"
    fi
}

# "<UTC time> <commit> <branch> <before-deploy copy>", one line per version
# started, so the second-to-last line is always the version that ran before.
record_deploy() {
    local sha branch last=""
    sha="$(git rev-parse HEAD 2>/dev/null)" || return 0     # not a git clone: nothing to record
    branch="$(git branch --show-current 2>/dev/null)" || branch=""
    if [ -z "$branch" ]; then
        branch="(detached)"                                 # after git checkout <commit>
    fi
    if [ -f "$HISTORY" ]; then
        last="$(tail -n 1 "$HISTORY" | cut -d' ' -f2)" || last=""
    fi
    if [ "$sha" = "$last" ]; then
        return 0
    fi
    # The bot is already running by now, so a failure here mustn't fail the run.
    if echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${sha} ${branch} ${COPY}" >> "$HISTORY"; then
        echo "Recorded ${sha:0:12} (${branch}) in ${HISTORY}."
    else
        echo "Couldn't add this version to ${HISTORY}."
    fi
}

echo "==> Installing Python, sqlite3 and git (may ask for your password)..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3 git >/dev/null

# Debian 12 and Raspberry Pi OS Bookworm ship 3.11; Ubuntu 22.04 only has 3.10.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "The bot needs Python 3.11 or newer, but this machine has $(python3 --version 2>&1)."
    echo "Use Debian 12 or Ubuntu 24.04 (or newer) for the VM. Ubuntu 22.04 is too old."
    exit 1
fi

echo "==> Setting up the bot's Python environment (.venv)..."
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

if [ ! -f .env ]; then
    # install -m 600: the file is private from the moment it exists.
    install -m 600 .env.example .env
    echo
    echo "Created .env from .env.example. Fill in your values, then run this again:"
    echo "    nano .env        (save with Ctrl+O, Enter, then Ctrl+X)"
    echo "    bash setup.sh"
    exit 1
fi
chmod 600 .env

mkdir -p -m 700 data backups
chmod 700 data backups

# Decided before the unit file is written below.
FIRST_INSTALL=1
if [ -f "$UNIT" ]; then
    FIRST_INSTALL=0
fi

# On a re-run, stop the old copy first, so the copy of the database below is
# exactly what the old code left, and the single-instance lock is free for the new one.
if [ "$FIRST_INSTALL" = 0 ]; then
    echo "==> Stopping ${SERVICE} while it's checked and updated..."
    if systemctl is-active --quiet "$SERVICE"; then
        STOPPED_IT=1     # before the stop, so a Ctrl+C during it is reported too
    fi
    sudo systemctl stop "$SERVICE"
fi

save_before_deploy_copy

echo "==> Checking the settings, database, Telegram and Stripe..."
if ! .venv/bin/python -m first_in_bot check; then
    echo
    echo "The check failed, so the bot has NOT been started."
    echo "Fix the FAIL lines above (nano .env), then run: bash setup.sh"
    exit 1
fi

# Only the first time: after that the bot already runs here, not on the Mac.
if [ "$FIRST_INSTALL" = 1 ]; then
    echo
    echo "==> Only ONE copy of the bot may run per bot token."
    echo "    If it's running on your Mac, stop it there first (Ctrl+C in its Terminal window)."
    echo "    Two copies fight over Telegram's updates and both send the reminders."
    if [ -t 0 ]; then
        read -r -p "    Press Enter once the Mac copy is stopped (Ctrl+C to cancel)... "
    fi
fi

echo "==> Installing the background service (${SERVICE})..."
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=First In Exeter member bot
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
WorkingDirectory=${DIR}
ExecStart=${DIR}/.venv/bin/python -m first_in_bot
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
UMask=0077
KillSignal=SIGTERM
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
STOPPED_IT=0     # started again; if it doesn't stay up, the message below says so
sleep 3
# status exits non-zero when the service isn't running; the check below reports that.
sudo systemctl --no-pager status "$SERVICE" | head -n 5 || true
echo

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "The service didn't stay running. See why with:"
    echo "    sudo journalctl -u ${SERVICE} -n 50 --no-pager"
    echo "To go back to the version before, see Roll back in DEPLOY.md."
    exit 1
fi

record_deploy

echo "All done. The member bot is running in the background as ${SERVICE}."
echo "See what it's doing:   sudo journalctl -u ${SERVICE} -f"
echo "Restart after edits:   sudo systemctl restart ${SERVICE}"
echo "Stop it:               sudo systemctl stop ${SERVICE}"
echo "Check the setup:       .venv/bin/python -m first_in_bot check"
echo
echo "To download a backup, run this ON YOUR MAC (not here), with your VM's name"
echo "and the date you want:"
echo "    gcloud compute scp ${USER_NAME}@<vm-name>:${DIR}/backups/first-in-YYYY-MM-DD.db ."
