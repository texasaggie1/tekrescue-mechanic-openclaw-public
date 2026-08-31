# tekRESCUE Mechanic for OpenClaw

## What it is

OpenClaw is an extremely useful product, but it seems to break every time we update it. This tool checks for updates nightly at 2 a.m., makes a backup, attempts the update, runs OpenClaw Doctor at least once, and if the verify step fails it tells you exactly how to roll back. Results are reported back via your chat channel.

**Mechanic backs up before every update. More importantly, Mechanic knows when to stop.** After three consecutive failed nights, it folds its arms and stops touching OpenClaw entirely, rather than doing the same broken thing seventeen more times on top of an already-broken state. That is the difference between one bad morning and a compounded mess you cannot recover from.

## What you'll get every morning

Mechanic sends one DM to your chat channel after the 02:00 routine
finishes (typically around 02:10). If everything went right, you get a
short success report. If something fell off, the same channel tells you
exactly what broke and how to recover, with the exact command to run.

You also get a heartbeat every 4 hours so you know Mechanic is still
alive. The absence of one is itself a signal.

### Happy morning (this is what you'll see most days)

```
tekRESCUE Mechanic: success

STATUS: SUCCESS

Run started:  2026-05-23T21:48:38+00:00
Run finished: 2026-05-23T21:58:14+00:00

OpenClaw version: 2026.5.20 -> 2026.5.20

Snapshot taken: 2026-05-23T21-48-38Z
Update:   openclaw update exit 0 (reports 2026.5.20)
Doctor:   exit 0, matched 0 known prompt(s)
Verify:   healthy (2026.5.20, exit 0, 128 ms)
Last success: 2026-05-23T21:58:14+00:00
Last known good: 2026-05-23T21-56-13Z
```

### Heartbeat (every 4 hours, all day, all night)

```
tekRESCUE Mechanic: heartbeat

OpenClaw 2026.5.20 healthy (130 ms).
No updates available.
```

When OpenClaw publishes a new version, the heartbeat reads:

```
OpenClaw 2026.5.20 healthy (130 ms).
Update available: 2026.5.21. Mechanic will install at 02:00 local.
```

### Bad morning (Mechanic auto-paused itself)

If three nightly runs fail in a row, Mechanic stops touching OpenClaw
until you clear the pause. The morning report leads with the alert and
tells you the one command to bring it back:

```
tekRESCUE Mechanic: PAUSED (3 consecutive verify failures)

STATUS: PAUSED
  reason: 3 consecutive verify failures
  since: 2026-05-23T02:01:10Z
  resume with: mechanic resume

Run started:  2026-05-23T02:00:00+00:00
Run finished: 2026-05-23T02:01:10+00:00

Consecutive failures: 3
Last success: 2026-05-20T02:03:14+00:00
Last known good: 2026-05-20T02-00-00Z

Notes:
  - Doctor matched 1 of 1 prompts before verify failed.
  - To roll back, run: mechanic restore 2026-05-22T02-00-00Z
```

You decide what to do next. Mechanic doesn't auto-roll-back; it tells
you the exact command.

## Before you install

Mechanic schedules nightly maintenance against OpenClaw. It runs
`openclaw update` and `openclaw doctor --fix` while you sleep, verifies
the result, and tells you exactly how to roll back if something looks
wrong.

The blast radius is small but real. **Back up `~/.openclaw` before you
run install.sh the first time.** Mechanic does not take that first
backup for you; it snapshots before every nightly run after that, but
the first install is a cliff.

The fastest way to back up:

```bash
tar --exclude='.openclaw/tmp' --exclude='.openclaw/logs' \
    -czf ~/openclaw-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
    -C ~ .openclaw
```

This software ships with NO WARRANTY. We are not responsible if
Godzilla tears down your server farm or if this script deletes
everything on your machine. Use responsibly.

## Quickstart install

Tested on a fresh M-series Mac running macOS 15+. Requires Python 3.11
or newer (Homebrew's `python@3.14` works) and `openclaw` already
installed and healthy.

Honest recommendation: this whole thing goes best with a good AI coding
tool driving (Claude Code, Codex, Cursor, or whatever you already use).
Clone the repo, point your tool at it, and say "install this and verify
it works." It will read [AGENTS.md](AGENTS.md), run the commands below,
handle whatever your machine throws at it, and know the safety rules
for touching a live OpenClaw. Doing it by hand works fine too; the
commands are right here.

```bash
git clone https://github.com/texasaggie1/tekrescue-mechanic-openclaw-public.git && cd tekrescue-mechanic-openclaw-public
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
./install.sh
```

install.sh prints a warning block, asks you to confirm you've backed
up `~/.openclaw`, writes the two LaunchAgent plists, loads them, then
foreground-test-fires both so any macOS permission dialog appears
while you're at the keyboard.

After it finishes, edit `~/.config/tekrescue-mechanic/.env` to point
`OPENCLAW_BIN_PATH` and `OPENCLAW_CONFIG_PATH` at your install, then
run `mechanic status` to confirm everything's wired up.

If you're driving the install via Claude Code or a similar assistant,
just point it at this repo. It can read the rest of this README and
run the steps for you.

## Configuration

Everything is environment variables, loaded from
`~/.config/tekrescue-mechanic/.env`. The file gets `chmod 600` on
install. Edit it with any text editor; the supervisor and updater pick
up changes on their next scheduled invocation.

### Required

| Variable | Description |
|---|---|
| `OPENCLAW_BIN_PATH` | Absolute path to the `openclaw` executable. Find yours with `which openclaw`. |
| `OPENCLAW_CONFIG_PATH` | Absolute path to OpenClaw's config directory (the one you back up). Usually `~/.openclaw`. |

### Optional, with defaults

| Variable | Default | Description |
|---|---|---|
| `UPDATE_TIME` | `02:00` | Local time the nightly routine fires. 24-hour. |
| `SUPERVISOR_INTERVAL_MINUTES` | `240` | Heartbeat cadence, in minutes. 240 = 6 pings/day. |
| `SNAPSHOT_RETENTION_DAYS` | `14` | Rolling nightly snapshots kept under `snapshots/nightly/`. Sticky snapshots (`first-known-good`, `last-known-good`) are never pruned. |
| `MIN_FREE_DISK_MB_FOR_SNAPSHOT` | `500` | Free disk floor for a run. If the snapshot volume is short, Mechanic first deletes its own oldest nightly snapshots to make room (never the first or last known good, and always keeping the 3 newest), and only refuses to start if that is still not enough. |
| `MAX_CONSECUTIVE_FAILURES` | `3` | Failed nightly runs in a row before Mechanic auto-pauses. |
| `POST_UPDATE_HOOK` | empty | Optional path to a script that re-applies your local OpenClaw patches after a real update modifies the install. Runs before the verify step; result lands in the morning report. |
| `POST_UPDATE_HOOK_TIMEOUT_SECONDS` | `300` | How long the hook may run before Mechanic gives up on it. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `NOTIFIER` | `none` | `none`, `telegram`, `slack`, `webhook`, `email`. See Notifications. |

`PAUSE_ON_ROLLBACK_FAILURE` and `PROMPT_MODE` are documented in
`.env.example` but dormant in v0.1.1 (no auto-rollback; doctor runs
`--non-interactive`). Reserved for v0.2.

## Verify it's working

`mechanic status` prints a snapshot of the current install. Healthy
output looks like this:

```
tekRESCUE Mechanic for OpenClaw
  version: 0.1.1

Configuration:
  file:                /Users/you/.config/tekrescue-mechanic/.env (ok)
  openclaw bin:        /opt/homebrew/bin/openclaw (ok)
  openclaw config:     /Users/you/.openclaw (ok)
  update time:         02:00 local
  supervisor interval: 240 min
  prompt mode:         STRICT
  snapshot retention:  14 days
  min free disk:       500 MB
  max failures:        3
  pause on rollback:   yes
  log level:           INFO
  notifier:            telegram

Launchd:
  supervisor agent:    loaded
  updater agent:       loaded

Snapshots:
  latest: /Users/you/Library/Application Support/tekrescue-mechanic/snapshots/last-known-good

Recent log (~/Library/Logs/tekrescue-mechanic/mechanic.log):
  ...
```

The three `(ok)` markers next to file paths are the load-bearing ones.
If any of them says `missing`, edit `~/.config/tekrescue-mechanic/.env`
and re-run `mechanic status`.

Other useful commands:

| Command | What it does |
|---|---|
| `mechanic logs -n 200` | Print the last 200 log lines. Add `-f` to follow live. |
| `mechanic test-notifier` | Send a test message through your configured notifier. |
| `mechanic run-now` | Run the full nightly routine immediately (takes ~6 to 10 min). |
| `mechanic capture-first-good` | Snapshot OpenClaw as the pristine pre-Mechanic baseline (refuses if OpenClaw is currently unhealthy). |
| `mechanic restore <target>` | Roll OpenClaw back to a snapshot. Target is `first-known-good`, `last-known-good`, or a nightly id from the log. Stops the gateway, untars, restarts. |
| `mechanic resume` | Clear paused state and reset the failure counter. |

## Notifications (optional)

By default Mechanic logs locally and stays off the network. To get a
DM (or Slack message, or email) for the morning report and the
heartbeat, set `NOTIFIER` in `.env` to one of `telegram`, `slack`,
`webhook`, `email`, and add the matching credentials.

### Telegram

If you already have an OpenClaw Telegram bot, you can reuse it.
Otherwise create one via [@BotFather](https://t.me/BotFather) (one
command: `/newbot`) and copy the token it gives you.

```bash
# In ~/.config/tekrescue-mechanic/.env:
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=<the token from BotFather>
TELEGRAM_CHAT_ID=<your numeric Telegram user id>
```

To find your `TELEGRAM_CHAT_ID`, message the bot once and check
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, or DM
[@userinfobot](https://t.me/userinfobot).

Confirm with `mechanic test-notifier`. You should see "Delivered." in
the terminal and a DM from the bot.

### Slack

```bash
NOTIFIER=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Use an Incoming Webhook from a Slack app you control. Messages post to
whichever channel the webhook is bound to.

### Webhook (generic JSON POST)

```bash
NOTIFIER=webhook
WEBHOOK_URL=https://your.endpoint/path
```

Mechanic POSTs `{"subject": "...", "body": "..."}` with
`Content-Type: application/json`. Anything that accepts a JSON POST
works (Zapier, n8n, your own server).

### Email (SMTP)

```bash
NOTIFIER=email
SMTP_HOST=smtp.fastmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASSWORD=<app password>
SMTP_FROM=you@example.com
SMTP_TO=you@example.com
```

STARTTLS is used if the server advertises it. Use an app password, not
your real account password.

### One bot, two channels

If you already use OpenClaw with Telegram, Mechanic can ride on the
same bot. Messages will come from your existing OpenClaw bot identity.
If you want a separate identity for Mechanic alerts, create a new bot
via @BotFather and use its token instead.

## Troubleshooting

### macOS asks for permission on first run

The first time the supervisor or updater fires, macOS may show one or
two prompts. install.sh tries to surface these in the foreground via a
post-install test-fire so you see them while you're at the keyboard;
if you ran with `--skip-test-fire`, or if the prompt is delayed to a
later launch, here's what you'll see and what to click:

**Gatekeeper ("Python wasn't checked for malicious software")**

The `python` interpreter that runs Mechanic was installed by Homebrew
and is signed by the Python Software Foundation, but the first time a
LaunchAgent (not your shell) invokes it, macOS may still ask. The
dialog button is **Allow** (or **Open Anyway** on older macOS).

If you clicked **Don't Allow** by reflex: open **System Settings ->
Privacy & Security**, scroll to the **Security** section near the
bottom, and click **Allow Anyway** next to the message about Python
being blocked. Then re-run the test-fire:

```bash
mechanic-supervisor
mechanic-updater --test-fire
```

**Full Disk Access ("Mechanic wants to access files in...")**

If macOS prompts for Full Disk Access for the Python interpreter, the
answer is **Allow**. The supervisor only reads `~/.openclaw`, but
because launchd-invoked processes run in a stricter sandbox than your
shell, macOS sometimes asks anyway.

If you missed the prompt or clicked Don't Allow: open **System
Settings -> Privacy & Security -> Full Disk Access**, find the entry
for the Python binary (the path will start with `/opt/homebrew/Cellar`
or `/Users/.../.venv/bin`), and toggle it on. Then re-run the
test-fire.

**Notification Center captured a prompt I never saw**

If macOS shows you a "Mechanic was blocked" notification with no
visible dialog, the prompt is waiting in Notification Center. Click
it, accept the permission, and re-run the test-fire. If the
notification has expired: System Settings -> Privacy & Security ->
Full Disk Access is your one-stop fix.

### `mechanic status` reports the openclaw bin or config as `missing`

Open `~/.config/tekrescue-mechanic/.env` and confirm `OPENCLAW_BIN_PATH`
points at the actual binary (`which openclaw` shows you where) and
`OPENCLAW_CONFIG_PATH` points at the directory OpenClaw uses (usually
`~/.openclaw`, not the JSON file inside it). Save, then re-run
`mechanic status`. No restart of the LaunchAgents needed; they reload
config on every fire.

### The morning report says verify failed and Mechanic auto-paused

Three nightly verify failures in a row will pause the updater (the
supervisor heartbeat keeps running). The report tells you the
snapshot id to roll back to. To recover:

```bash
mechanic restore <snapshot-id>      # the id is in the morning report
mechanic resume                     # clears the pause + failure counter
```

`mechanic restore` stops the OpenClaw gateway, untars the snapshot
over `~/.openclaw`, and restarts the gateway. The whole thing takes
about a minute.

### The 02:00 nightly never fires

Three things to check, in order:

1. `launchctl list | grep tekrescue` should show both agents loaded.
   If not, re-run `./install.sh` from the repo.
2. `mechanic logs -n 200` for the most recent updater attempt and any
   error from the LaunchAgent stdout (`~/Library/Logs/tekrescue-mechanic/updater.stdout.log`).
3. macOS Energy Saver: if your Mac is asleep at 02:00 and Power Nap is
   off, launchd holds the fire until the machine wakes. Either keep
   the Mac plugged in with Power Nap on, or accept that the routine
   runs the first time you open the lid in the morning.

### `openclaw update --yes` exits non-zero in the report

Mechanic captures the full stdout and stderr from `openclaw update`
and surfaces them in the morning report and the log. If the failure
is OpenClaw-internal (e.g., npm registry timeout), retry overnight or
run `mechanic run-now` manually. If the failure repeats and the
verify step still passes, OpenClaw is healthy but stuck on its
current version; investigate via `openclaw update status --json` and
`openclaw doctor --lint`.

### Update fails with "unexpected packaged dist file dist/openclaw-install-guard"

You're on npm 12, which blocks package install scripts unless the
package is on an allowlist. OpenClaw's postinstall never runs, the
2.0-era install-guard sentinel stays behind, and the updater's verify
correctly fails and rolls back. OpenClaw stays healthy on its old
version, but no update will ever complete until you allow the
scripts. One command fixes it permanently (run it as the user
OpenClaw runs as):

```bash
npm config set allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs --location=user
```

Then either wait for the next nightly or update by hand. If you're
jumping a major release (like 2026.7.x to 2026.8.1 "2.0"), expect
`openclaw doctor --fix` to have real migration work afterwards
(session store to SQLite, config schema, plugin updates with a new
capability-consent prompt), and know that OpenClaw's crash-loop
breaker suppresses channel auto-start for a few minutes after
repeated failed boots. A channel showing "not configured" right
after an upgrade is usually that breaker, not lost config: wait five
minutes, restart the gateway once, and check again.

## Uninstall

```bash
./uninstall.sh           # remove LaunchAgents and config
./uninstall.sh --purge   # also wipe logs and snapshots
```

Uninstall does NOT touch OpenClaw. If you want to roll OpenClaw back
to its pre-Mechanic state before walking away, run
`mechanic restore first-known-good` first.

## How it works

Mechanic is a plain Python program with three dependencies (pexpect,
python-dotenv, requests). It's explicitly NOT another OpenClaw skill
or agent: the whole point is that it keeps working when OpenClaw is
broken, so it shares no dependencies, configs, or runtime with
OpenClaw.

Two LaunchAgents do the work:

- **Supervisor** fires every 4 hours. Probes OpenClaw with
  `openclaw --version`, asks `openclaw update status --json` whether
  an update is available, and sends a heartbeat through your notifier.
  Read-only; never mutates anything.
- **Updater** fires once a night at 02:00 local. Snapshots
  `~/.openclaw` as a gzipped tar, checks whether a newer OpenClaw
  actually exists, and only runs `openclaw update --yes` when it does
  (a same-version reinstall would replace every installed file and
  wipe any local patches you have applied, for nothing). It then runs
  `openclaw doctor --fix --non-interactive`, re-runs your
  `POST_UPDATE_HOOK` patch script if a real update landed, verifies
  OpenClaw still responds, and sends the morning report, which always
  says whether the install was modified. If verify fails it does NOT
  auto-roll-back (rolling back requires stopping the live OpenClaw
  daemon; too risky for an unattended job). It tells you exactly which
  snapshot to restore from and waits for you to run the command.

Snapshots live under
`~/Library/Application Support/tekrescue-mechanic/snapshots/`. The
sticky `first-known-good/` and `last-known-good/` are never auto-pruned;
the rolling `nightly/` directory keeps the last
`SNAPSHOT_RETENTION_DAYS` entries.

## Working on this with an AI assistant

This repo is built to be driven by AI coding tools, and honestly, that
is the best way to run it: install, troubleshooting, and especially the
occasional big OpenClaw release all go smoother with Claude Code,
Codex, or another capable coding agent at the wheel. We have run this
workflow with both Claude and ChatGPT and it holds up.

[AGENTS.md](AGENTS.md) carries full instructions for your assistant
(Claude Code, ChatGPT/Codex, Cursor, and friends all pick it up;
CLAUDE.md points there too): the architecture rules, the maintenance
safety orders for touching a live OpenClaw install, and the
[SESSIONS.md](SESSIONS.md) protocol we use so any AI session can pick
up exactly where the last one stopped. Point your assistant at this
repo and it will know how to behave.

## Credits and license

Built by [tekRESCUE LLC](https://tekrescue.com), San Marcos TX.

MIT licensed. See [LICENSE](LICENSE) for the full text. We ship with
no warranty; the install.sh warning block is not a joke.

Bug reports, prompt-capture contributions, and notifier-backend pull
requests welcome at
[github.com/texasaggie1/tekrescue-mechanic-openclaw-public](https://github.com/texasaggie1/tekrescue-mechanic-openclaw-public).

## About tekRESCUE

If Mechanic saved you a headache, we do more of this kind of work at
[tekRESCUE](https://tekrescue.com). Managed IT and security for small
and medium businesses, mostly in central Texas, but the tools we
build are free for anyone to use. Not a sales pitch. Just a
handshake.
