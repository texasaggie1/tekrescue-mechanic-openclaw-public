# tekRESCUE Mechanic for OpenClaw

## What it is

OpenClaw is an extremely useful product, but it seems to break every time we update it. This tool checks for updates nightly at 2 a.m., makes a backup, attempts the update, runs OpenClaw Doctor at least once, and if the verify step fails it tells you exactly how to roll back. Results are reported back via your chat channel.

**Mechanic backs up before every update. More importantly, Mechanic knows when to stop.** After three consecutive failed nights, it folds its arms and stops touching OpenClaw entirely, rather than doing the same broken thing seventeen more times on top of an already-broken state. That is the difference between one bad morning and a compounded mess you cannot recover from.

**Mechanic also refuses to be first.** A new OpenClaw release has to sit on the npm registry for a week before Mechanic will install it, so a poisoned release (a bad commit on main, a hijacked publish token) has time to be noticed and pulled by the people who watch these things for a living. Mechanic's own Python dependencies are locked under the same rule. See [The one-week waiting period](#the-one-week-waiting-period).

**Since v0.2.0 Mechanic looks after [Hermes Agent](https://hermes-agent.nousresearch.com/) too.** Set `TARGETS=openclaw`, `TARGETS=hermes`, or both. Hermes gets the same treatment: a backup, a pin to the newest release that is a week old, its own doctor, a gateway restart, a verify, and a line in the morning report. See [Hermes](#hermes).

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

Run started:  2026-09-13T07:00:02+00:00
Run finished: 2026-09-13T07:09:41+00:00

OpenClaw version: 2026.8.2 -> 2026.9.2

Snapshot taken: 2026-09-13T07-00-02Z
Update:   openclaw update --tag 2026.9.2 exit 0 (reports 2026.9.2)
Install modified: yes
Doctor:   exit 0, matched 0 known prompt(s)
Verify:   healthy (2026.9.2, exit 0, 128 ms)
Last success: 2026-09-13T07:09:41+00:00
Last known good: 2026-09-13T07-09-40Z

Notes:
  - Waiting period (MIN_UPDATE_AGE_DAYS=7): 2026.9.2 (published 2026-09-05, 7 days old) is the newest release at least 7 days old; 2026.9.4 (published 2026-09-11, 1 day old) waits. Next: 2026.9.3 becomes eligible 2026-09-15T13:06Z.
```

Most nights there is nothing old enough that you do not already have,
and the report says so instead:

```
Update:   skipped (waiting period; see notes)
Install modified: no
...
Notes:
  - Waiting period (MIN_UPDATE_AGE_DAYS=7): 2026.9.4 (published 2026-09-11, 1 day old) is too new; Mechanic waits until a release is 7 days old. Installed 2026.9.2 is already the newest release old enough. Next: 2026.9.3 becomes eligible 2026-09-15T13:06Z.
```

### Heartbeat (every 4 hours, all day, all night)

```
tekRESCUE Mechanic: heartbeat

OpenClaw 2026.5.20 healthy (130 ms).
No updates available.
```

When OpenClaw publishes a new version, the heartbeat says what the
nightly will do about it, and when:

```
OpenClaw 2026.9.2 healthy (130 ms).
Update available: 2026.9.4, waiting. 2026.9.4 (published 2026-09-11, 1 day old) is too new; Mechanic waits until a release is 7 days old. Installed 2026.9.2 is already the newest release old enough. Next: 2026.9.3 becomes eligible 2026-09-15T13:06Z.
```

Once a release you do not have yet is old enough:

```
OpenClaw 2026.8.2 healthy (130 ms).
Update available: 2026.9.4. Mechanic will install 2026.9.2 at 02:00 local: 2026.9.2 (published 2026-09-05, 7 days old) is the newest release at least 7 days old; 2026.9.4 (published 2026-09-11, 1 day old) waits. Next: 2026.9.3 becomes eligible 2026-09-15T13:06Z.
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

The blast radius is small but real. **Back up `~/.openclaw` (and run
`hermes backup` if you use Hermes) before you run install.sh the first
time.** Mechanic does not take that first backup for you; it snapshots
before every nightly run after that, but the first install is a cliff.

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
pip install --require-hashes -r requirements.txt
pip install --no-build-isolation --no-deps -e .
./install.sh
```

The two `pip install` lines are deliberate. The first installs exactly
the dependency versions this release was tested with, verified by
sha256 hash, nothing newer; the second installs Mechanic itself without
letting pip fetch anything else. A plain `pip install -e .` also works,
but it takes whatever is newest on PyPI that day, which is the thing the
[waiting period](#the-one-week-waiting-period) exists to avoid.

install.sh prints a warning block, asks you to confirm you've backed
up `~/.openclaw`, writes the two LaunchAgent plists, loads them, then
foreground-test-fires both so any macOS permission dialog appears
while you're at the keyboard.

After it finishes, edit `~/.config/tekrescue-mechanic/.env`: set
`TARGETS` to what you run (`openclaw`, `hermes`, or `openclaw,hermes`),
point `OPENCLAW_BIN_PATH` and `OPENCLAW_CONFIG_PATH` at your OpenClaw
install if it is listed, and check the Hermes defaults if Hermes is.
Then run `mechanic status` to confirm everything's wired up and
`mechanic plan` to see exactly what the first nightly would do.

If you're driving the install via Claude Code or a similar assistant,
just point it at this repo. It can read the rest of this README and
run the steps for you.

## Configuration

Everything is environment variables, loaded from
`~/.config/tekrescue-mechanic/.env`. The file gets `chmod 600` on
install. Edit it with any text editor; the supervisor and updater pick
up changes on their next scheduled invocation.

### Targets

| Variable | Default | Description |
|---|---|---|
| `TARGETS` | `openclaw` | Which products Mechanic looks after: `openclaw`, `hermes`, or `openclaw,hermes`. A product not listed is never touched, not even probed. |

### OpenClaw (required when `openclaw` is in `TARGETS`)

| Variable | Description |
|---|---|
| `OPENCLAW_BIN_PATH` | Absolute path to the `openclaw` executable. Find yours with `which openclaw`. |
| `OPENCLAW_CONFIG_PATH` | Absolute path to OpenClaw's config directory (the one you back up). Usually `~/.openclaw`. |
| `OPENCLAW_MIN_UPDATE_AGE_DAYS` | Optional. Overrides `MIN_UPDATE_AGE_DAYS` for OpenClaw only. |
| `OPENCLAW_SKIP_VERSIONS` | Optional. Comma-separated versions Mechanic must never install. Versions npm marks deprecated are skipped on their own. |

### Hermes (used when `hermes` is in `TARGETS`)

| Variable | Default | Description |
|---|---|---|
| `HERMES_BIN_PATH` | `~/.local/bin/hermes` | The `hermes` launcher. |
| `HERMES_HOME` | `~/.hermes` | Hermes's data directory (config, state, skills, sessions). Mechanic snapshots it. |
| `HERMES_SOURCE_DIR` | `~/.hermes/hermes-agent` | The git checkout a source install runs from. A Desktop bundle has none and cannot be pinned. |
| `HERMES_UPDATE_MODE` | `release-tag` | `release-tag` pins the checkout to a week-old release and rebuilds with `hermes pm install`. `hermes-update` waits the same period, then runs `hermes update --yes`, which installs the tip of the channel. |
| `HERMES_UPDATE_CHANNEL` | empty | `hermes-update` mode only: `--channel` to pass (`stable`, `canary`, `main`). |
| `HERMES_MIN_UPDATE_AGE_DAYS` | `MIN_UPDATE_AGE_DAYS` | Overrides the shared waiting period for Hermes only. |
| `HERMES_SKIP_TAGS` | empty | Comma-separated release tags Mechanic must never pin to. |
| `HERMES_DOCTOR_FIX` | `true` | Run `hermes doctor --fix` after an update (safe config migrations, unattended). `false` runs read-only `hermes doctor`. |
| `HERMES_GATEWAY_RESTART_TIMEOUT_SECONDS` | `2400` | How long `hermes gateway restart` may take; Hermes drains in-flight work first, up to 30 minutes by default. |

### Optional, with defaults

| Variable | Default | Description |
|---|---|---|
| `UPDATE_TIME` | `02:00` | Local time the nightly routine fires. 24-hour. |
| `MIN_UPDATE_AGE_DAYS` | `7` | The waiting period for every target. OpenClaw: a release must have been public on npm this long, and Mechanic installs the newest one that is (via `openclaw update --tag`). Hermes: a release tag must have been in Mechanic's sight this long. `0` turns the wait off. Per-target overrides above. |
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

## The one-week waiting period

OpenClaw ships a new release every two or three days, built from a
repository with hundreds of contributors. That pace is great for
features and terrible for anyone whose machine installs whatever is
newest at 2 a.m. If a release is ever poisoned, whether by a bad commit
that slipped onto main, a hijacked publish token, or a compromised
build machine, it is usually noticed and pulled within days. The people
who install it on day one are the ones who get hurt.

So Mechanic waits. By default (`MIN_UPDATE_AGE_DAYS=7`) it will not
install any OpenClaw release until it has been on the npm registry for
seven days. Because the newest release is almost never a week old,
"wait for latest" would mean "never update"; instead, every night
Mechanic works out the newest release that IS old enough and installs
that one, using OpenClaw's own `openclaw update --yes --tag <version>`.
You end up riding the release train about a week behind the front car,
with OpenClaw's normal update path doing the actual work (doctor,
migrations, plugin sync, restart), just aimed at a slightly older
version.

How it decides:

- Publish dates come from the npm registry, read through your own
  `npm` (`npm view openclaw time versions dist-tags --json`), so any
  registry mirror, proxy, or token in your `.npmrc` applies exactly as
  it does for `openclaw update`. It is a read-only query.
- Only real releases count. Betas and hotfix-style prereleases
  (`2026.9.1-beta.1`, `2026.2.2-1`) are never picked, and nothing above
  what npm tags as `latest` is either.
- Mechanic never downgrades. If you updated by hand to something newer
  than the oldest-eligible release, it leaves you there and says so.
- If it cannot establish dates (no `npm` on the machine, registry
  unreachable), it installs nothing and the morning report tells you
  why. Unknown means wait.
- The heartbeat and the morning report always name the version chosen,
  its publish date, and when the next one becomes eligible, so you are
  never guessing what Mechanic will do tonight.

If you run OpenClaw on the beta, extended-stable, or dev channel,
Mechanic still applies the wait to the version OpenClaw reports, but
does not pick intermediate versions for you (that selection belongs to
OpenClaw on those channels).

What the wait does not cover, honestly: OpenClaw's own npm dependency
tree. The published `openclaw` package has 65 direct dependencies and
ships no lockfile, so `openclaw update` resolves those fresh at install
time, and a week-old OpenClaw can still pull in a day-old transitive
dependency. Mechanic cannot fix that from the outside without changing
how OpenClaw installs itself. If that matters to you, keep an eye on
`npm config set before=<date>`, which makes npm resolve everything as of
a given date; it is untested with OpenClaw's updater, so we have not
turned it on for you.

Mechanic holds itself to the same rule. `requirements.txt` pins every
Python package Mechanic installs, with sha256 hashes, and it is only
regenerated by `scripts/deps/relock.sh`, which refuses releases younger
than seven days. `python3 scripts/deps/check_pin_age.py` verifies the
pins against PyPI's upload dates any time you like.

To wait longer, raise `MIN_UPDATE_AGE_DAYS`. To go back to installing
the newest release the night it lands, set it to `0`.

## Hermes

[Hermes Agent](https://hermes-agent.nousresearch.com/) is a source
install: a git checkout at `~/.hermes/hermes-agent` that tracks `main`,
with your data in `~/.hermes`. Its own updater, `hermes update`,
fast-forwards to the tip of `main` and has no way to ask for an older
release. The maintainers do mark releases, though: plain tags such as
`v2026.9.24` land on `main` every few days, carry the date that
`hermes --version` prints, and run the project's release-gate tests.

So Mechanic pins. Each nightly with `HERMES_UPDATE_MODE=release-tag`:

1. Asks origin for its tags (`git ls-remote`) and mirrors them into a
   namespace of its own inside the checkout. Hermes's own tags are never
   written by Mechanic.
2. Records in a ledger when **Mechanic** first saw each tag and which
   commit it pointed at. Age is counted from first sight, not from the
   tag's own date, because git tag dates are typed by whoever runs
   `git tag` and a hijacked account could backdate one straight past the
   waiting period. The very first scan on a fresh install trusts the
   tags' dates once, so you do not wait a week for a release that is
   months old; `mechanic plan` shows you what that scan concluded.
3. Refuses any tag whose commit has changed since first sight. Tags are
   supposed to be immutable; a moved one is treated as hostile and named
   in every report until you deal with it.
4. Picks the newest eligible tag and compares it with your checkout by
   git ancestry. It installs only if your commit is an ancestor of the
   tag. Already there means "current". Ahead of it (someone ran
   `hermes update`, or the checkout sits on `main`'s tip) means "wait for
   a newer tag to age"; Mechanic never downgrades. Anything else means
   "diverged, leaving it alone".
5. Runs `hermes backup --quick`, takes its own archive of `~/.hermes`
   (minus the checkout, the package manager's stores, caches, and browser
   profiles, the same list `hermes backup` skips), then `git checkout
   --detach <tag>`, `hermes pm install` (Hermes's documented repair
   command, the same code its updater runs), `hermes doctor --fix`,
   and a gateway restart through launchd if one was running and you have
   not disabled it. Verify checks `hermes --version`, that the checkout
   landed on the expected commit, `hermes pm status`, and `hermes gateway
   status`.

If `hermes pm install` fails, the checkout goes back to the commit it
was on and the environment is rebuilt for that, so a bad night never
leaves Hermes half-moved. `mechanic restore --target hermes <snapshot>`
puts both your data and the code back.

`HERMES_UPDATE_MODE=hermes-update` keeps Hermes's own updater instead:
the same waiting period on the newest tag, then `hermes update --yes`,
which installs the tip of the configured channel. A trigger delay, not
a pin, but every step is Hermes-supported. Nous has designed a `stable`
channel that would make `--channel stable` a real pin; as of October
2026 its published record does not exist yet, so leave
`HERMES_UPDATE_CHANNEL` empty until it does.

Two things to know. First, this is a path Nous does not test for you,
so run the first nightly supervised: `mechanic plan`, then
`mechanic run-now --target hermes` while you watch, with a fresh
`hermes backup` in hand. Second, Hermes already quarantines its own
Python dependencies for 14 days (`exclude-newer` in its lockfile), so on
Hermes the application code is the only thing left to wait on, which is
exactly what the pin covers.

## Verify it's working

`mechanic status` prints a snapshot of the current install. Healthy
output looks like this:

```
tekRESCUE Mechanic
  version: 0.2.0

Configuration:
  file:                /Users/you/.config/tekrescue-mechanic/.env (ok)
  targets:             openclaw, hermes
  update time:         02:00 local
  min update age:      7 days (default for every target)
  supervisor interval: 240 min
  snapshot retention:  14 days
  min free disk:       500 MB
  max failures:        3
  pause on rollback:   yes
  log level:           INFO
  notifier:            telegram

OpenClaw:
  openclaw bin:        /opt/homebrew/bin/openclaw (ok)
  openclaw config:     /Users/you/.openclaw (ok)
  min update age:      7 days (npm: /opt/homebrew/bin/npm)
  prompt mode:         STRICT
  gateway:             ai.openclaw.gateway loaded
  state:               0 consecutive failures, last success 2026-10-04T07:09:41+00:00

Hermes:
  hermes bin:          /Users/you/.local/bin/hermes (ok)
  hermes home:         /Users/you/.hermes (ok)
  source checkout:     /Users/you/.hermes/hermes-agent (ok)
  update mode:         release-tag
  min update age:      7 days
  doctor --fix:        yes
  gateway:             ai.hermes.gateway loaded
  state:               0 consecutive failures

Launchd:
  supervisor agent:    loaded
  updater agent:       loaded

Snapshots:
  openclaw:  /Users/you/Library/Application Support/tekrescue-mechanic/snapshots/last-known-good
  hermes:    /Users/you/Library/Application Support/tekrescue-mechanic/snapshots/hermes/last-known-good

Recent log (~/Library/Logs/tekrescue-mechanic/mechanic.log):
  ...
```

The `(ok)` markers next to file paths are the load-bearing ones. If any
of them says `missing`, edit `~/.config/tekrescue-mechanic/.env` and
re-run `mechanic status`. OpenClaw's `min update age` line should name
an `npm`; if it says `npm NOT found`, the waiting period cannot read
publish dates and no update will install until it can (see
Troubleshooting). A gateway line that says `DISABLED by operator` means
you switched that product off with `launchctl disable` and Mechanic is
leaving it alone.

Then run `mechanic plan`. It prints, per target, what tonight's run
would do and why, without changing anything:

```
== Hermes ==
  health:   healthy (2026.9.7, exit 0, 11 ms)
  Waiting period (HERMES_MIN_UPDATE_AGE_DAYS=7, mode release-tag): v2026.9.21 (first seen 2026-09-21, 13 days ago) is the newest release old enough and newer than the installed v2026.9.7; pinning the checkout to it. Next: v2026.10.3 becomes eligible 2026-10-10T10:05Z.
  Tonight: snapshot, install v2026.9.21, doctor, verify.
```

Other useful commands:

| Command | What it does |
|---|---|
| `mechanic plan [--target X]` | Show what tonight's run would do for each target, and why. Changes nothing. |
| `mechanic logs -n 200` | Print the last 200 log lines. Add `-f` to follow live. |
| `mechanic test-notifier` | Send a test message through your configured notifier. |
| `mechanic run-now [--target X]` | Run the full nightly routine immediately (takes ~6 to 10 min per target). |
| `mechanic capture-first-good --target X` | Snapshot a target as the pristine pre-Mechanic baseline (refuses if it is currently unhealthy). |
| `mechanic restore --target X <snapshot>` | Roll a target back to a snapshot: `first-known-good`, `last-known-good`, or a nightly id from the log. Stops the gateway, untars, puts Hermes's code back too, restarts. |
| `mechanic resume --target X` | Clear a target's paused state and reset its failure counter. |

With a single target configured, `--target` can be left off.

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

### The heartbeat says an update is available but Mechanic keeps waiting

That is the [waiting period](#the-one-week-waiting-period) doing its
job. The heartbeat and the morning report name the release Mechanic is
holding back, its publish date, and the exact time the next release
becomes eligible. Nothing to fix. If you want a specific release now,
run `openclaw update --yes --tag <version>` yourself; Mechanic never
downgrades, so it will simply carry on from there. If a week is longer
than you want, lower `MIN_UPDATE_AGE_DAYS` in
`~/.config/tekrescue-mechanic/.env` (or set it to `0` to install the
newest release the night it lands).

### The report says "waiting period could not be applied"

Mechanic reads publish dates with `npm view`, and it looks for `npm`
next to your `openclaw` binary first, then on the LaunchAgent PATH
(`/usr/local/bin`, `/opt/homebrew/bin`, and the system dirs). If
OpenClaw was installed with pnpm or bun and there is no `npm` in those
places, the note in the report says so and Mechanic installs nothing,
because "unknown" means wait. Fix it by installing npm (it comes with
Node.js; `brew install node` gives you one on Homebrew) or by
symlinking your npm into `/usr/local/bin`. `mechanic status` confirms
which npm it found. If the note instead mentions a timeout or a
registry error, the registry was unreachable at 02:00; the next
nightly retries on its own.

### `openclaw update` refuses `--tag`

OpenClaw does not accept `--tag` on the extended-stable channel.
Mechanic skips the flag when `openclaw update status --json` reports
that channel, but if it could not read the channel from that output it
assumes stable and passes `--tag`. Set `MIN_UPDATE_AGE_DAYS=0` to get
updates flowing again, and please open an issue with the output of
`openclaw update status --json` (minus anything private) so we can fix
the detection.

### I shut a gateway down and Mechanic brought it back

OpenClaw's CLI starts its own gateway when anything probes it, including
Mechanic's read-only heartbeat (`openclaw --version`, `openclaw update
status --json`). A plain `launchctl bootout` therefore lasts until the
next supervisor tick. To keep a product down:

1. `launchctl disable gui/$(id -u)/ai.openclaw.gateway` (or
   `ai.hermes.gateway`), then `launchctl bootout` it. Mechanic reads the
   disabled flag and does not run that product's CLI at all; `mechanic
   status` shows `DISABLED by operator` and the heartbeat says so.
2. Or remove the product from `TARGETS`. Mechanic then never constructs
   it.

Do both if you want belt and braces. `launchctl enable` and a `TARGETS`
edit bring it back.

### Hermes: "already ahead of vX, Mechanic never downgrades"

Someone ran `hermes update` (or Hermes ran `/update` from chat), so the
checkout is on the tip of `main`, past every week-old tag. Mechanic
leaves it there and waits for the next tag to age. If you want back on
a pinned release now, `mechanic restore --target hermes last-known-good`
puts the code (and data) back where Mechanic last verified it.

### Hermes: "on different lines of history"

The checkout is on a branch no release tag descends from (a local
feature branch, a fork). Mechanic will not guess. Put it back on `main`
or on a release tag and the next nightly resumes.

### Hermes: `hermes pm install` failed

The report shows the checkout went back to its previous commit and the
environment was rebuilt for it, so Hermes is where it was. Run
`hermes pm status` and `hermes doctor` to see what the rebuild objected
to; the Hermes log at `~/.hermes/logs/` has the detail.

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
python-dotenv, requests), all pinned by exact version and sha256 hash
in `requirements.txt`. It's explicitly NOT another OpenClaw skill
or agent: the whole point is that it keeps working when OpenClaw is
broken, so it shares no dependencies, configs, or runtime with
OpenClaw.

Two LaunchAgents do the work, once per target in `TARGETS`:

- **Supervisor** fires every 4 hours. Probes each product's health
  (`openclaw --version`, `hermes --version`), works out what the nightly
  would do, and sends a heartbeat through your notifier. It skips any
  product whose gateway you have `launchctl disable`d, because OpenClaw
  restarts its gateway when probed. Mechanic itself never mutates
  anything from the heartbeat.
- **Updater** fires once a night at 02:00 local. Snapshots
  `~/.openclaw` as a gzipped tar, checks whether a newer OpenClaw
  actually exists, works out the newest release that has been public
  for at least `MIN_UPDATE_AGE_DAYS`, and only runs `openclaw update
  --yes --tag <that version>` when it is newer than what you have (a
  same-version reinstall would replace every installed file and wipe
  any local patches you have applied, for nothing). It then runs
  `openclaw doctor --fix --non-interactive`, re-runs your
  `POST_UPDATE_HOOK` patch script if a real update landed, verifies
  OpenClaw still responds, and sends the morning report, which always
  says whether the install was modified. If verify fails it does NOT
  auto-roll-back (rolling back requires stopping the live OpenClaw
  daemon; too risky for an unattended job). It tells you exactly which
  snapshot to restore from and waits for you to run the command.
  For Hermes the same routine pins the git checkout to a week-old
  release tag and rebuilds with `hermes pm install`; see [Hermes](#hermes).

Snapshots live under
`~/Library/Application Support/tekrescue-mechanic/snapshots/` (OpenClaw)
and `snapshots/hermes/` (Hermes). The sticky `first-known-good/` and
`last-known-good/` are never auto-pruned; the rolling `nightly/`
directory keeps the last `SNAPSHOT_RETENTION_DAYS` entries. Failure
counters and the pause switch are per target.

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
