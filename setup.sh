#!/usr/bin/env bash
# Sets up the First In member bot as a background service (first-in-member-bot)
# that restarts itself and survives reboots. Same pattern as the alert bot's setup.sh.
# Works on Google Cloud (Debian/Ubuntu) and Raspberry Pi OS. Run it as your normal
# user, not root: it asks for your password when it needs sudo.
#
# Safe to run again after a git pull: it stops the running service, updates the
# Python packages, re-runs the preflight check and starts the service again.
# It never prints anything from .env.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"
SERVICE="first-in-member-bot"
UNIT="/etc/systemd/system/${SERVICE}.service"

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

echo "==> Installing Python and sqlite3 (may ask for your password)..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3 >/dev/null

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

# On a re-run, stop the old copy first, so the check (and its database migrations)
# and the single-instance lock don't see it. Stopping an inactive service is harmless.
if [ -f "$UNIT" ]; then
    echo "==> Stopping ${SERVICE} while it's checked and updated..."
    sudo systemctl stop "$SERVICE"
fi

echo "==> Checking the settings, database, Telegram and Stripe..."
if ! .venv/bin/python -m first_in_bot check; then
    echo
    echo "The check failed, so the bot has NOT been started."
    echo "Fix the FAIL lines above (nano .env), then run: bash setup.sh"
    exit 1
fi

echo
echo "==> Only ONE copy of the bot may run per bot token."
echo "    If it's running on your Mac, stop it there first (Ctrl+C in its Terminal window)."
echo "    Two copies fight over Telegram's updates and both send the reminders."
if [ -t 0 ]; then
    read -r -p "    Press Enter once the Mac copy is stopped (Ctrl+C to cancel)... "
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
sleep 3
# status exits non-zero when the service isn't running; the check below reports that.
sudo systemctl --no-pager status "$SERVICE" | head -n 5 || true
echo

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "The service didn't stay running. See why with:"
    echo "    sudo journalctl -u ${SERVICE} -n 50 --no-pager"
    exit 1
fi

echo "All done. The member bot is running in the background as ${SERVICE}."
echo "See what it's doing:   sudo journalctl -u ${SERVICE} -f"
echo "Restart after edits:   sudo systemctl restart ${SERVICE}"
echo "Stop it:               sudo systemctl stop ${SERVICE}"
echo "Check the setup:       .venv/bin/python -m first_in_bot check"
echo
echo "To download a backup, run this ON YOUR MAC (not here), with your VM's name"
echo "and the date you want:"
echo "    gcloud compute scp ${USER_NAME}@<vm-name>:${DIR}/backups/first-in-YYYY-MM-DD.db ."
