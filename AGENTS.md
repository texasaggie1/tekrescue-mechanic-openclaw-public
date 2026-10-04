# AGENTS.md

Instructions for AI assistants working in this repository. This file is the
source of truth for Claude Code, ChatGPT/Codex, Cursor, Copilot, and any
other AI coding tool (CLAUDE.md just points here). Humans are welcome to
read it too; nothing in here is secret handshake material.

Read this file fully before doing any work. Then read SESSIONS.md to learn
what state the project is in.

---

## 1. What this project is

tekRESCUE Mechanic is a supervisor agent that watches over OpenClaw and,
since v0.2.0, Hermes Agent on macOS. `TARGETS` in the .env says which
(default openclaw). It runs two loops as launchd LaunchAgents, once per
target each:

- **Supervisor loop**: a heartbeat every `SUPERVISOR_INTERVAL_MINUTES`
  (default 240). Each tick probes OpenClaw health (`openclaw --version`),
  probes update availability (`openclaw update status --json`), and pings
  the configured notifier. It always pings: a healthy heartbeat is
  informative, and a MISSING heartbeat tells the operator the supervisor
  itself stopped.
- **Updater loop**: every night (default 02:00) it snapshots the target's
  data, installs the newest release that has aged `MIN_UPDATE_AGE_DAYS`
  (default 7, the supply-chain waiting period), runs the product's
  doctor, verifies it still runs, and writes a morning report. OpenClaw:
  npm publish dates, `openclaw update --yes --tag <v>`, `openclaw doctor
  --fix --non-interactive`, POST_UPDATE_HOOK (`release_age.py`). Hermes:
  release tags and a first-sight ledger, `git checkout --detach <tag>`,
  `hermes pm install`, `hermes doctor --fix`, launchd gateway restart
  (`hermes_release.py`).

The differentiator is that Mechanic knows when to stop. Three consecutive
failed nights trips a circuit breaker: Mechanic pauses itself and waits
for a human (`mechanic resume`) instead of compounding the damage.

## 2. The one architectural rule you must not break

Mechanic is a plain Python program with minimal dependencies. It is NOT an
OpenClaw skill, agent, or extension, and not a Hermes skill, plugin, or
cron job either, and it must never become one. The entire point is that
Mechanic keeps working when the product it supervises is broken, so it
cannot share that product's runtime, dependencies, or config. If you
catch yourself thinking "this would be easier as a plugin," stop.

## 3. Components

| File | Job |
|---|---|
| `mechanic/supervisor.py` | heartbeat loop, per target |
| `mechanic/updater.py` | the nightly update routine, per target |
| `mechanic/targets.py` | the Target adapters (OpenClawTarget, HermesTarget): one interface the loops talk to |
| `mechanic/doctor_runner.py` | runs `openclaw doctor --fix` |
| `mechanic/rollback.py` | snapshots, restore, pruning, disk headroom; one SnapshotStore per target |
| `mechanic/verifier.py` | health probe (the authoritative signal) and `openclaw update status` |
| `mechanic/release_age.py` | OpenClaw's waiting period: which npm version is old enough tonight |
| `mechanic/hermes_release.py` | Hermes's waiting period: tag ledger, ancestry check, pin, `hermes pm install`, gateway |
| `mechanic/daemon.py` | launchd control for both gateways, plus the operator-disabled guard |
| `mechanic/state.py` | supervisor state file per target (failure count, pause flag) |
| `mechanic/reporter.py` | formats the morning report, one block per target |
| `mechanic/notifier.py` | Telegram / Slack / webhook / email push |
| `mechanic/config.py` | loads `.env`, validates settings, TARGETS and per-target blocks |
| `mechanic/cli.py` | `mechanic status`, `plan`, `run-now`, `snapshots`, `restore`, `resume`, etc. |

Also in the repo: `tests/` (stdlib `unittest`, run with
`python3 -m unittest discover -s tests` from a venv that has
`requirements.txt` installed), `requirements.txt` (the hash-locked
dependency set) and `scripts/deps/` (`relock.sh` regenerates it,
`check_pin_age.py` verifies it; see section 6).

Disk layout on an installed machine: logs in
`~/Library/Logs/tekrescue-mechanic/`, snapshots in
`~/Library/Application Support/tekrescue-mechanic/snapshots/`
(`first-known-good/`, `last-known-good/`, `nightly/<timestamp>/`; Hermes
under `snapshots/hermes/`), config in `~/.config/tekrescue-mechanic/.env`
(chmod 600), state in `~/Library/Application Support/tekrescue-mechanic/state/`
(`supervisor_state.json` for OpenClaw, `supervisor_state.hermes.json`,
and `hermes_tags.json`, the tag ledger).

## 4. Hard-won production facts (do not relearn these)

- **OpenClaw is a live daemon.** It writes into its config directory
  constantly. Snapshots are `tar -czf` with `--exclude tmp --exclude logs`
  (tar tolerates files vanishing mid-archive; a raw copytree does not).
  Restoring requires stopping the daemon first; the nightly never
  auto-restores.
- **`openclaw update --yes` is not idempotent at the file level.** It
  reinstalls the SAME version when nothing newer exists, replacing every
  installed file and silently wiping operator-applied local patches. That
  is why the updater gates on `openclaw update status --json` and only
  updates when the registry really has something newer, and why
  `POST_UPDATE_HOOK` exists to re-apply patches after real updates.
- **npm 12 blocks install scripts by default.** Without an allowlist
  (`npm config set allow-scripts=openclaw,... --location=user`), OpenClaw's
  postinstall never runs, its install-guard sentinel stays behind, and the
  update verify fails and rolls back forever. See the README
  troubleshooting entry.
- **The verifier is the only authoritative health signal.** Doctor's exit
  code is informational. A run succeeds or fails on whether
  `openclaw --version` works afterwards.
- **Major OpenClaw releases deserve a supervised upgrade.** The 2026.8.1
  ("2.0") jump involved a session-store migration that crash-looped the
  gateway, plugin SDK breakage, capability re-consent, and a crash-loop
  breaker that suppresses channel auto-start and looks exactly like lost
  channel config. An unattended nightly would have tripped the circuit
  breaker at best. When a big release lands, recommend the operator run it
  with you watching, ahead of the 02:00 fire.
- **OpenClaw releases every two or three days** (2026.9.1 on 09-03, 9.2
  on 09-05, 9.3 on 09-08, 9.4 on 09-11), from a repo with hundreds of
  contributors. A waiting period that only installs "latest" once it is
  a week old would therefore never install anything. Mechanic instead
  installs the newest release that IS old enough, via `openclaw update
  --yes --tag <version>` (OpenClaw's one-shot package-target override;
  it does not change the saved channel), and trails the release train
  by `MIN_UPDATE_AGE_DAYS`. Publish dates come from one read-only
  `npm view openclaw time versions dist-tags --json` call through the
  operator's npm (mirrors, proxies, and .npmrc auth apply). npm's own
  `--before` date filter is honoured by `npm install` only; `npm view`
  ignores it (checked 2026-09-12), so the date filtering lives in
  `release_age.py`.
- **The waiting period covers the OpenClaw package, not its dependency
  tree.** The published tarball (200 MB, 10k files, 65 direct npm deps)
  ships no lockfile, so `npm install -g openclaw@<version>` resolves
  those dependencies fresh at install time. A week-old OpenClaw can
  still pull a day-old transitive dependency. Known gap, documented in
  the README; candidates to close it are in SESSIONS.md.
- **`openclaw update status --json` does not document its channel key.**
  `verifier._extract_channel` looks for `channel` at the top level,
  under `availability`, or under `update`, accepts a string or an
  object with `name`, and otherwise reports None, which the planner
  treats as stable. On non-stable channels (beta, extended-stable, dev)
  the planner does not pick intermediate versions; it waits for the
  version OpenClaw reports to age, and skips `--tag` on extended-stable
  and dev because OpenClaw refuses it there. Confirm the real key from a
  live install when you can and pin the extraction.
- **`openclaw --version` is not a liveness check, and the self-heal that
  fixes that needs an off switch.** Four outages in 2026-07 and 2026-09
  (one of 43 hours) were reported "healthy" because the CLI answers
  perfectly with a dead gateway. v0.1.4 added `daemon.check_gateway`:
  `launchctl print` for a live pid, and `bootstrap` + `kickstart -k` to
  restart it, run by the heartbeat and after every nightly. Then, on
  2026-10-03, the operator ran `launchctl bootout` on `ai.openclaw.gateway`
  to take the agent offline, and four hours later, one supervisor
  interval, that self-heal brought it back. Mechanic was doing its job
  with no way to know the shutdown was deliberate. Now it has two:
  `TARGETS` (an unlisted product is never constructed, never probed), and
  the `launchctl print-disabled` guard in `daemon.is_disabled`, which the
  supervisor, the updater, `plan`, `restore`, `ensure_running`, and
  `check_gateway` all honour. Tell the operator to `launchctl disable`
  before `bootout`; a bare bootout does not survive the next tick.
  `OPENCLAW_GATEWAY_AUTOHEAL` defaults on; `HERMES_GATEWAY_AUTOHEAL`
  defaults off because Hermes operators tend to run their own watchdog
  and two healers fighting over one service helps nobody.
- **`openclaw doctor --fix` must run with
  `OPENCLAW_SERVICE_REPAIR_POLICY=external`** (doctor_runner.py, v0.1.4).
  Without it doctor stops the gateway to enter maintenance and then
  refuses to restart it on any host with EDR: before touching its
  LaunchAgent, OpenClaw scans every /Library/LaunchDaemons/*.plist and
  fails closed on one it cannot read, and Elastic Defend's plist reads
  EPERM at the Endpoint Security layer while its mode bits say readable,
  so OpenClaw's "unreadable, skip" branch never fires. Three of the four
  outages above began within a second of the 02:00 doctor step. Upstream
  issue openclaw/openclaw#139813. With the policy, doctor does config
  repair only and never touches the service or the plists.
- **The maintainer's private repo is the development home and this one
  is synced from it** (product files: `mechanic/`, `install.sh`,
  `uninstall.sh`, `.env.example`, `LICENSE`, `.gitignore`,
  `pyproject.toml`, `README.md`, `scripts/recovery/`). That sync lapsed
  between 2026-09-06 and 2026-10-04: private v0.1.4 (gateway self-heal,
  doctor policy) and v0.1.5 (`--no-snapshot`) never reached here while
  public v0.1.4 (the waiting period) and v0.2.0 (Hermes) were built here.
  Both were ported into v0.2.0 on 2026-10-04. Until the private repo
  adopts v0.2.0, the two disagree; a version number alone does not say
  which features an install has, `mechanic --help` does. Before every
  push here, grep the tree for the operator's agent names, client names,
  IPs, and personal emails; the private repo's CLAUDE.md lists them.
- **Hermes facts (read 2026-10-04 from the hermes-agent source).** A
  source install is a git checkout at `~/.hermes/hermes-agent` tracking
  `main`, launcher `~/.local/bin/hermes`, data in `~/.hermes`, gateway
  LaunchAgent `ai.hermes.gateway` (`-<profile>` suffix for extra
  profiles). `hermes update` has `--yes`, `--check`, `--plan`, `--backup`,
  `--no-backup`, `--channel`, `--branch`, `--no-gateway-restart`, and NO
  way to request an older release; it fast-forwards to the branch tip or
  to a published channel's exact commit. The `stable` channel record
  (`https://hermes-assets.nousresearch.com/releases/channels/stable.json`)
  returned 404 on 2026-10-04, so `--channel stable` fails today. Release
  tags are plain `vYYYY.M.D` (every 3 to 7 days); the updater's own
  stable-tag rule excludes that shape, canary builds are `v0.21.x+canary.
  <stamp>`. `hermes pm install` (bare) is the documented repair command:
  tool closure, then venv sync from the checkout's lock; it does no git
  operations, so a pinned checkout stays pinned. `hermes pm status` prints
  the sync receipt as JSON. `hermes doctor --fix` applies safe config
  migrations non-interactively, may install a macOS TCC helper, acquire a
  missing tool through PM, and checkpoint SQLite WAL files; nothing in it
  starts or enables the gateway. `hermes backup --quick` writes a
  SQLite-safe state snapshot under `~/.hermes/state-snapshots/`. Hermes
  quarantines its own Python dependencies with uv `exclude-newer = "14
  days"`. `hermes --version` prints `Hermes Agent v<base>+<n>.g<sha>
  (<release date>)` and may do a passive update check unless
  `updates.check` is false.
- **Git tag dates are not evidence.** They are written by whoever ran
  `git tag`, so Mechanic ages a Hermes tag from the moment Mechanic first
  saw it (`hermes_tags.json`), trusts tag dates only on the very first
  scan, and treats a tag whose commit changed as hostile. Upstream tags
  are mirrored into `refs/mechanic/upstream-tags/` so Hermes's own
  `refs/tags/` is never written by Mechanic.
- **A restore must not delete what the archive excludes.** Hermes's
  archive leaves out the code checkout and the package manager's stores
  (gigabytes, all regenerable, all essential); `restore_snapshot` moves
  the excluded top-level entries back from the pre-restore copy. Found
  in simulation on 2026-10-04, before it ever ran on a real machine.
- **Subprocess hygiene**: every invocation of `openclaw` (or anything it
  spawns: npm, node, tar) must pass `env=clean_subprocess_env()` from
  `mechanic/config.py` and `stdin=subprocess.DEVNULL`. Mechanic and
  OpenClaw both use an `OPENCLAW_CONFIG_PATH` variable that means
  different things to each. Any new Mechanic env var added to
  `.env.example` must also be added to `_MECHANIC_ENV_VARS` in `config.py`
  so it gets stripped.

## 5. Maintenance standing orders

These rules exist because an early version of this maintenance workflow
once lobotomized a production agent: it silently changed the agent's
workspace path, updated OpenClaw past its Node runtime, verified nothing,
and reported nothing. The owner spent an evening on forensics that one
git commit and one line of reporting would have prevented. Every rule
below is a scar, and every rule is also a Mechanic feature. They apply to
YOU whenever you touch a live OpenClaw install, whatever model you are.

1. **Snapshot before surgery.** Before modifying anything, snapshot it:
   git commit in the workspace, tar of the config dir, or both. No
   snapshot, no surgery.
2. **Protected files are read-only.** The agent's identity and memory
   (`SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`, `AGENTS.md`,
   `DREAMS.md`, anything in `memory/` or `secrets/`, and `openclaw.json`)
   are never written without the owner's explicit approval in the session.
   You fix the engine. You do not touch the driver.
3. **Config changes are diff-and-approve.** Show the exact diff, the
   reason, and the rollback path. Wait for a yes. Approval for one change
   is not approval for the next.
4. **Check runtime compatibility BEFORE updating.** Compare
   `npm view openclaw engines` against `node --version`. An update that
   bricks the gateway is worse than no update.
5. **Post-op verification is part of the operation.** Gateway starts
   clean, the agent responds, the agent knows who it is (answers from its
   SOUL.md, not generically), and the workspace path is unchanged.
   "Responds" is not the bar. "Remembers" is the bar.
6. **Report everything you touched.** Files, packages, commands,
   verification results, and anything you noticed but did not touch. If
   you cannot explain what you changed, you changed too much.
7. **Scope discipline.** Fix the reported problem, only the reported
   problem. List anything else you find as recommendations.
8. **When uncertain, stop.** A stalled ticket costs minutes. A
   lobotomized agent costs an evening and trust.

## 6. Code and style conventions

- Python 3.11+, standard library where possible. Pre-approved external
  deps: `pexpect`, `python-dotenv`, `requests`. Anything else needs a
  justification recorded in SESSIONS.md.
- **Dependencies are pinned and quarantined, like OpenClaw.**
  `requirements.txt` pins every package Mechanic installs (runtime deps,
  their transitive deps, and the setuptools/wheel that build the
  editable install) to an exact version with sha256 hashes. It is only
  ever regenerated by `scripts/deps/relock.sh`, which refuses releases
  younger than 7 days, and every regeneration must pass
  `python3 scripts/deps/check_pin_age.py` (PyPI upload dates, yank
  status). Never hand-edit the file, never bump a pin to a release that
  is under a week old, and record every bump in SESSIONS.md with the
  versions moved. `pyproject.toml` keeps loose floors so the package
  metadata stays honest; the lock is what installs.
- Type hints on public functions, docstrings on modules and public
  functions.
- No print statements in production code paths; use the logger. Logs are
  the primary debugging surface: structured, scannable, no spam.
- Never log secrets. Bot tokens, API keys, webhook URLs are masked in all
  output.
- Mechanic never phones home. No telemetry, no analytics. It talks only
  to OpenClaw, the operator's configured notifier, and (read-only,
  through the operator's own npm, for the waiting period) the npm
  registry OpenClaw itself installs from. Nothing about the operator or
  the machine is sent anywhere.
- Mechanic never auto-updates itself. Updating Mechanic is a manual
  `git pull` by the operator.
- **No em dashes anywhere**: not in code, docs, CLI output, or error
  messages. Use commas, colons, parentheses, or full stops.
- Address the user as "you" in docs and CLI output. Casual but precise
  tone. This is a free tool that should feel like a gift, not an
  enterprise manual.
- Product-specific code lives in the Target adapter (`targets.py`) and
  that product's release module. The updater, supervisor, reporter, and
  CLI must stay product-agnostic: if you find yourself writing
  `if target.name == "hermes"` in `updater.py`, it belongs in
  `HermesTarget`. Everything a target can fail to establish resolves to
  "leave the install alone" with the reason in the report.

## 7. The SESSIONS.md protocol (please steal this idea)

SESSIONS.md is a running work log kept IN the repo, newest entry first.
Its job: any AI instance, on any machine, on any day, can `git pull`,
read the top entry, and pick up exactly where the last session ended,
with zero human re-explanation. It has survived model switches (Claude
one day, ChatGPT the next) because the state lives in the repo, not in
any one tool's chat history.

The rules that make it work:

- **Update SESSIONS.md immediately before every `git push`. No
  exceptions**, even for one-line changes. If you finished a task without
  updating it, the task is not done. Doc updates ship in the same commit
  as the work they describe.
- New entries go at the TOP of the file.
- Be honest about half-finished work. The next session trusts the
  "Current state" paragraph completely; a flattering lie there costs
  hours.
- Record decisions AND rejections. "We chose X" is half the value;
  "we rejected Y because Z" is the half that stops the next session from
  re-litigating it.
- Convert relative dates to absolute ones ("Thursday" becomes
  "2026-03-05") because the reader may arrive months later.
- Cap "next session should" at three prioritized tasks.

The entry template lives at the top of SESSIONS.md in this repo.

Division of labor between the two doc files: AGENTS.md holds PERMANENT
project knowledge (architecture, conventions, non-negotiables) and
SESSIONS.md holds IN-FLIGHT state (what just happened, what is next).
When in doubt, ask: "should a brand new AI instance know this even if it
never reads SESSIONS.md?" If yes, it goes here, and you should update
this file as part of the same commit.

## 8. How to work in this repo

1. Read this file, then SESSIONS.md, before any work.
2. Plan first on anything non-trivial, show the plan, get a yes, then
   execute. Especially for anything that touches a live OpenClaw install.
3. One question at a time when you need clarification.
4. Anything user-facing (README copy, CLI output, error messages) is
   sensitive: propose wording, let the human approve it.
5. Run the tests (`python3 -m unittest discover -s tests`) before every
   push that touches `mechanic/`. The Hermes tests build real git repos
   in a temp dir and need `git` and `tar`. If you changed
   `requirements.txt`, it came from `scripts/deps/relock.sh` and
   `check_pin_age.py` passed.
6. Update SESSIONS.md (and this file, when permanent knowledge changed)
   before every push. The session is not over until the commits land.

## 9. Out of scope (v0.2.x)

Linux/Windows support, a GUI, Mechanic auto-updating itself, telemetry,
multi-user or system-level installs, OpenClaw forks, Hermes Desktop
bundles and Docker installs, and any hosted version of Mechanic. Do not
wander into these without the maintainer asking.

---

Maintained by Randy Bryan, tekRESCUE LLC, San Marcos TX
([tekrescue.com](https://tekrescue.com)). Issues and PRs welcome.
