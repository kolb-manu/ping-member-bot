# Deploying the First In member bot

The member bot runs as the service `first-in-member-bot` on the Google Cloud VM, next to the alert bot.
It has its own folder `~/first-in-member-bot`, its own `.venv`, `.env` and database. The alert bot
(`fixr-bot` in `~/fixr-bot`) is separate, and nothing here touches it. Later the same steps work on a Raspberry Pi.

The server folder is a git clone. Code changes are made on a laptop, pushed, then pulled on the server.
Don't edit code on the server. `.env`, `data/` and `backups/` are not in git, so a pull never touches them.

All commands below run **on the VM** (in its SSH window) unless they say "on the Mac".

## Stop the Mac copy first

Only **one** copy of the bot may run per bot token. Telegram hands updates to one copy at a time: when
a second copy starts, Telegram ends the older copy's request and the two keep cutting each other off
(the log says `Conflict: terminated by other getUpdates request`). Both copies would also run the
reminders and the sweep from their own databases, so people could get the same message twice.

On the Mac, press Ctrl+C in the Terminal window where the bot runs. To make sure it's gone:
```
pgrep -lf first_in_bot        # prints nothing when it's stopped
pkill -f first_in_bot         # only if the line above printed something
```
The check (`.venv/bin/python -m first_in_bot check`) can't spot another copy for you. The only way to
look would be to ask Telegram for updates, and that would cut off the running copy instead.

## First-time setup on the VM

Open an SSH window: Google Cloud console > Compute Engine > VM instances > **SSH**, or on the Mac
`gcloud compute ssh <vm-name>`. Then:
```
git clone https://github.com/kolb-manu/ping-member-bot.git ~/first-in-member-bot
cd ~/first-in-member-bot
git checkout <branch>         # the branch you want to run, e.g. main
git branch --show-current     # shows which one you're on
```
The repo is public, so no deploy key is needed.

Now put your `.env` there. Either let the setup make one from `.env.example` and fill it in:
```
bash setup.sh                 # stops after creating .env (only you can read it)
nano .env                     # fill in your values; save with Ctrl+O, Enter, then Ctrl+X
```
or copy your Mac's `.env` across. On the Mac, in the project folder:
```
gcloud compute scp .env <vm-name>:~/first-in-member-bot/.env
```
`gcloud` logs in with your Mac's username. If it can't find the folder (or says permission denied),
the SSH window probably uses a different name: run `whoami` there and write
`<that-name>@<vm-name>:~/first-in-member-bot/...` instead. If gcloud asks for a zone, add
`--zone <zone>` (the zone is on the VM instances page). The same goes for every `gcloud` line below.

Then run the setup:
```
bash setup.sh
```
It installs Python, makes `.venv`, sets `.env` to `chmod 600`, makes `data/` and `backups/`, runs the
check, reminds you to stop the Mac copy (press Enter once it's stopped) and starts the service.
The check prints a list of PASS / WARN / FAIL lines. WARN lines are things to know about, and the setup
carries on. If any line says FAIL the bot is **not** started: fix it (usually `nano .env`) and run
`bash setup.sh` again.

## Deploy

After a change has been pushed:
```
cd ~/first-in-member-bot
sqlite3 data/first-in.db ".backup backups/before-deploy.db"
git pull && bash setup.sh
```
The first line saves the database first, in case the new version has to be rolled back.
`setup.sh` stops the bot, updates the Python packages, runs the check and starts it again. Then check the log:
```
sudo journalctl -u first-in-member-bot -n 30 --no-pager
```
Look for `Starting @<your bot> | Stripe: TEST | removals enabled: False`.

After editing only `.env`:
```
.venv/bin/python -m first_in_bot check && sudo systemctl restart first-in-member-bot
```
The check is safe to run while the bot is up.

## Roll back
```
cd ~/first-in-member-bot
git log --oneline             # find the last good commit
git checkout <old commit>
bash setup.sh
```
To return to the latest code afterwards:
```
git checkout <branch> && git pull && bash setup.sh
```
While on an old commit, `git pull` does nothing useful, so always `git checkout <branch>` first.

If the check says the database schema "is newer than this code", the newer version changed the
database and the old code refuses to use it. Restore `backups/before-deploy.db` (see below), or go
back to the latest code.

## Logs and control
```
sudo journalctl -u first-in-member-bot -n 50 --no-pager   # the last 50 lines
sudo journalctl -u first-in-member-bot -f                 # live; Ctrl+C stops watching, not the bot
sudo systemctl status first-in-member-bot                 # is it running?
sudo systemctl restart first-in-member-bot
sudo systemctl stop first-in-member-bot
```

## Download a backup to the Mac

The bot saves a backup every night as `backups/first-in-YYYY-MM-DD.db` and keeps the last 14.
They're on the same machine as the bot, so download one now and then (say weekly).
`ls ~/first-in-member-bot/backups` on the VM lists them. On the Mac:
```
gcloud compute scp <vm-name>:~/first-in-member-bot/backups/first-in-YYYY-MM-DD.db .
```
(`setup.sh` prints this line with your username and folder filled in.)

Or from the browser SSH window: click **Download file** (top right; on some versions it's under the
gear icon) and enter the full path, e.g. `/home/<you>/first-in-member-bot/backups/first-in-YYYY-MM-DD.db`.
It lands in your Mac's Downloads folder.

From a Raspberry Pi, on the Mac: `scp <user>@<pi-name>.local:~/first-in-member-bot/backups/first-in-YYYY-MM-DD.db .`

A backup holds Telegram IDs, usernames and Stripe customer IDs. Keep it private, and never commit it.

## Restore a backup

To restore one from the Mac, upload it first. On the Mac:
`gcloud compute scp first-in-YYYY-MM-DD.db <vm-name>:~/first-in-member-bot/backups/`. Then on the VM:
```
cd ~/first-in-member-bot
sudo systemctl stop first-in-member-bot
sqlite3 data/first-in.db ".backup backups/before-restore.db"   # keeps the current one, just in case
cp backups/first-in-YYYY-MM-DD.db data/first-in.db
rm -f data/first-in.db-wal data/first-in.db-shm
.venv/bin/python -m first_in_bot check                         # shows how many users it has
sudo systemctl start first-in-member-bot
```
The `-wal` and `-shm` files belong to the old database. Left behind, SQLite would try to apply them to
the restored one.

## Moving to another machine (Mac to VM, or VM to Raspberry Pi)

Stop the old copy first (on the Mac: Ctrl+C; on the VM: `sudo systemctl stop first-in-member-bot`
and then `sudo systemctl disable first-in-member-bot`). To keep its users, save its database in its
folder with `sqlite3 data/first-in.db ".backup first-in-move.db"` (works on the Mac too), copy that file
into the new machine's `backups/` folder and restore it as above, using its name in the `cp` line.
Otherwise the new copy starts with an empty database. Raspberry Pi OS Bookworm has Python 3.11, so `setup.sh` works there unchanged. Ubuntu 22.04
is too old (Python 3.10): use Debian 12 or Ubuntu 24.04 or newer.

## Checkpoint: a few days of dry runs

Leave the bot running on the VM for a few days with `REMOVALS_ENABLED=false` before anything is real:

- The removal sweep runs every 10 minutes but removes nobody. Whenever the list of people it *would*
  remove changes, the owner gets a "Would remove: ..." message. `/dryrun` shows the list at any time.
- Check every name on that list with `/user <id>`: each one should really have no trial, subscription
  or grant. Anyone listed who shouldn't be is a bug to fix before going live.
- Check that the daily report arrives at 09:00 (London time) and that a new file appears in
  `backups/` each night.

`REMOVALS_ENABLED` stays `false` until the owner decides. Switching it on is part of go-live, with the
live Stripe key, and it's the owner's call only. Don't change it as part of a deploy. The check shows
its value every time it runs (a WARN line when it's `true`).
