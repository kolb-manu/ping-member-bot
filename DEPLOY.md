# Deploying the First In member bot

The member bot runs as the service `first-in-member-bot` on the Google Cloud VM, next to the alert bot.
It has its own folder `~/first-in-member-bot`, its own `.venv`, `.env` and database. The alert bot
(`fixr-bot` in `~/fixr-bot`) is separate, and nothing here touches it. Later the same steps work on a Raspberry Pi.

Like the alert bot, the service runs as your own login user, so you manage both the same way. That
means each bot could read the other's `.env` and database if one of them were ever compromised.

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
sudo apt-get update && sudo apt-get install -y git    # a new VM may not have git yet
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
check, reminds you to stop the Mac copy (press Enter once it's stopped; it only asks the first time)
and starts the service.
The check prints a list of PASS / WARN / FAIL lines. WARN lines are things to know about, and the setup
carries on. If any line says FAIL the bot is **not** started: fix it (usually `nano .env`) and run
`bash setup.sh` again.

The check also asks Stripe whether the key may read subscriptions, disputes (with their charges),
checkout sessions and customers, and says which permission to add if not. It only reads, so it
can't tell whether the key may also *create* checkouts and customers: try one checkout in test mode.

## Deploy

After a change has been pushed:
```
cd ~/first-in-member-bot
git pull && bash setup.sh
```
`setup.sh` updates the Python packages, stops the bot, saves a copy of the database as
`backups/before-deploy-YYYYmmdd-HHMMSS.db` (UTC time; it keeps the newest 10 and says which file it
made), runs the check and starts the bot again. The check only reads the database: the new version's
database changes (migrations) run when the bot starts, so after a FAIL the database is still as the
old version left it. Once the bot is running, `setup.sh` adds a line to `data/deploy-history.log`
(see Roll back). Then check the log:
```
sudo journalctl -u first-in-member-bot -n 30 --no-pager
```
Look for `Starting @<your bot> | Stripe: TEST | removals enabled: False`.

If `setup.sh` stops part way after it has stopped the bot (a FAIL line, an error, Ctrl+C or a closed
SSH window), it ends with **The bot is STOPPED** in a box of `!!`. Fix the problem and run
`bash setup.sh` again. Or start the service as it was set up before with
`sudo systemctl start first-in-member-bot`: note that this runs the code now in the folder, so to run
the version from before the `git pull`, roll back instead.

After editing only `.env`:
```
.venv/bin/python -m first_in_bot check && sudo systemctl restart first-in-member-bot
```
The check is safe to run while the bot is up.

## Roll back

Go back to the version that ran before the current one:
```
cd ~/first-in-member-bot
tail -n 5 data/deploy-history.log     # one line per version started here, newest last
git checkout "$(tail -n 2 data/deploy-history.log | head -n 1 | cut -d' ' -f2)"
bash setup.sh
```
Each line of `data/deploy-history.log` is `<time (UTC)> <commit> <branch> <before-deploy copy>`, and
`setup.sh` adds one each time it starts a different version. The `git checkout` line picks the commit
from the second-to-last line. Only roll back to commits in this file: they're the versions that really
ran here, whereas `git log` also lists in-between commits that were never meant to run on their own
(some have no `setup.sh` or no single-instance lock). If the file has only one line, there's nothing
earlier to go back to. A rollback adds a line too, so afterwards the second-to-last line is the version
you just left: look at `tail -n 5` before rolling back a second time.

To return to the latest code afterwards:
```
git checkout <branch> && git pull && bash setup.sh
```
While on an old commit, `git pull` does nothing useful, so always `git checkout <branch>` first.

**Rolling back past a migration.** If the check FAILs because the database schema "is newer than this
code", the newer version changed the database when it started, and the old code can't use it. Restore
the before-deploy copy named at the end of the **last** line of `data/deploy-history.log`: `setup.sh`
saved it just before that newer version first started, so it still has the old layout. Follow
[Restore a backup](#restore-a-backup) with that file in the `cp` line, and finish with `bash setup.sh`
instead of `sudo systemctl start`. Anything the bot recorded since that deploy is lost (see the notes
under the restore steps). The other way out is to go back to the latest code.

## Logs and control
```
sudo journalctl -u first-in-member-bot -n 50 --no-pager   # the last 50 lines
sudo journalctl -u first-in-member-bot -f                 # live; Ctrl+C stops watching, not the bot
sudo systemctl status first-in-member-bot                 # is it running?
sudo systemctl restart first-in-member-bot
sudo systemctl stop first-in-member-bot
```

## Download a backup to the Mac

The bot saves a backup every night as `backups/first-in-YYYY-MM-DD.db`. It never overwrites one: if
that day's file is already there (say the bot was restored and backed up again the same day), the new
copy is `first-in-YYYY-MM-DD-2.db`, then `-3` and so on. It keeps the newest 14 (`BACKUP_KEEP`),
counting those extra copies, and deletes older ones. Two other kinds of copy sit in the same folder:
`before-deploy-*.db` from `setup.sh` (it keeps the newest 10) and `before-restore-*.db` from the
restore steps below, which are never deleted automatically (remove old ones with `rm` once you're sure).
They're all on the same machine as the bot, so download one now and then (say weekly).
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
sqlite3 data/first-in.db ".backup backups/before-restore-$(date -u +%Y%m%d-%H%M%S).db"   # the current one, just in case
cp backups/first-in-YYYY-MM-DD.db data/first-in.db
rm -f data/first-in.db-wal data/first-in.db-shm
sqlite3 data/first-in.db "INSERT OR REPLACE INTO kv(key, value) VALUES('maintenance_paused_since', strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))"
.venv/bin/python -m first_in_bot check                         # shows how many users it has
sudo systemctl start first-in-member-bot
```
The safety copy has the time in its name, so going through these steps again (say to try another
date) never overwrites it. The `-wal` and `-shm` files belong to the old database. Left behind, SQLite
would try to apply them to the restored one.

The restored file holds what the bot knew when that backup was taken: whether it was paused, every
`/grant` and `/ban`, and which reminders had been sent. Anything after that is gone from it. The
`INSERT` line makes the bot start paused, as after `/pause`: it removes nobody and sends no reminders,
while joining and paying carry on. Before letting it carry on, in Telegram:

1. `/dryrun` shows who it would remove. Check anyone listed with `/user <id>`.
2. Re-apply any `/grant` or `/ban` you made after the backup was taken.
3. `/resume`.

Subscriptions and trials come back from Stripe by themselves at the next sync (every few minutes).

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
