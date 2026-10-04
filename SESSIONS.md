# Session log

This file is a running work log, newest entry first. Any AI assistant (or
human) can read the top entry after a `git pull` and continue exactly
where the last working session stopped, on any machine, with any tool.
See AGENTS.md section 7 for the full protocol; the short version:

- Append a new entry at the TOP before every `git push`, no exceptions.
- Be honest about half-finished work; the next session trusts
  "Current state" completely.
- Record decisions AND rejections, so nothing gets re-litigated.
- Use absolute dates.

Template for new entries:

```markdown
## YYYY-MM-DD HH:MM (TZ) - branch: <branch>

### What we did
- Bullet list of concrete changes in this session

### Current state
One paragraph: what works, what doesn't, what to expect on next clone.
Be honest about half-finished work.

### Decisions made
- Anything worth remembering that isn't obvious from the code
- Things we explicitly rejected and why

### Open questions
- Things we haven't decided yet, or things we punted

### Next session should
1. First concrete task
2. Second concrete task
3. Third concrete task (max three, prioritized)
```

---

## 2026-10-04 (UTC) - branch: claude/open-claw-version-delay-gr91ha - v0.2.0, Hermes Agent as a second target

### What we did
- Mechanic is multi-target. `TARGETS=openclaw|hermes|openclaw,hermes`
  (default openclaw, so a v0.1 .env keeps working). `targets.py` holds the
  two adapters; updater, supervisor, reporter, and CLI are product-agnostic
  and loop over targets. Per-target snapshot stores (OpenClaw keeps the
  v0.1 layout; Hermes under `snapshots/hermes/`), per-target state files,
  failure counters, and pause. One morning report with a block per target.
- Hermes support (`hermes_release.py`), mode `release-tag` by default:
  `git ls-remote` for upstream tags, mirrored into Mechanic's own ref
  namespace; a ledger (`hermes_tags.json`) that ages tags from MECHANIC's
  first sight (git tag dates can be backdated) with one-time trust of tag
  dates on the bootstrap scan; moved tags refused forever; `HERMES_SKIP_TAGS`
  blocklist; the newest eligible tag is installed only when HEAD is its
  git ancestor (never downgrade, leave a diverged checkout alone). Install
  is `git checkout --detach <tag>`, `hermes pm install` (Hermes's documented
  repair command; does no git), `hermes doctor --fix` (safe config
  migrations), then `hermes gateway restart` only if a gateway was running
  and not operator-disabled, in that order, as `hermes update` itself
  orders things. `hermes pm install` failure puts the checkout back and
  rebuilds for it. Verify adds HEAD == expected commit, `hermes pm status`,
  `hermes gateway status`. `hermes backup --quick` runs before Mechanic's
  tar so a SQLite-safe state copy rides inside the archive. Mode
  `hermes-update` instead waits the same period then runs `hermes update
  --yes [--channel X]` (a trigger delay; Hermes cannot pin).
- The operator off switch for the 2026-10-03 incident: a target whose
  gateway is `launchctl disable`d is skipped entirely (no CLI call) by the
  supervisor, the updater, `plan`, and `restore`; `daemon.start` refuses
  to start a disabled service. Plus, a product not in TARGETS is never
  constructed.
- OpenClaw planner: versions npm marks deprecated are skipped (one
  `npm view openclaw@<v> deprecated` per examined candidate), and
  `OPENCLAW_SKIP_VERSIONS` is the operator blocklist; both fall through to
  the next eligible version. `version_key` now ignores a leading `v`.
- `restore_snapshot` keeps a store's excluded top-level entries in place
  (OpenClaw's tmp/ and logs/; Hermes's checkout and PM stores). Without
  this the first simulated Hermes restore deleted the checkout.
- New `mechanic plan [--target]`: per-target dry run of tonight's decision,
  the thing to read before the first supervised nightly. `--target` on
  run-now, resume, restore, capture-first-good (optional with one target).
  `mechanic status` shows every target, gateway state including DISABLED,
  and per-target failure state.
- Tests: 59 (22 OpenClaw planner, 20 Hermes on real temporary git repos,
  config/daemon/gates, reporter, rollback). Simulated two-target nightly
  with fake openclaw/npm/hermes/launchctl and a real git origin: first
  night installed OpenClaw 2026.9.6 via `--tag` and pinned Hermes to
  v2026.9.21; second night both waited; disabled OpenClaw gateway ->
  SKIPPED with zero openclaw invocations and the heartbeat says so;
  `mechanic restore --target hermes last-known-good` restored data, kept
  the checkout and stores, re-pinned the code, restarted the gateway;
  legacy OpenClaw-only .env unchanged in behaviour.
- Version 0.2.0. README (Hermes section, targets, commands, three Hermes
  troubleshooting entries, the "I shut a gateway down" entry), AGENTS.md
  (architecture, Hermes facts, the probe-restarts-gateway scar, the tag
  date rule, the restore rule), .env.example, install.sh text.
- Later the same day, during the supervised walkthrough: discovered the
  Mac mini runs the maintainer's PRIVATE repo at 0.1.5, and the
  private-to-public sync had lapsed since 2026-09-06. Ported private
  v0.1.4 and v0.1.5 into v0.2.0, target-aware: `daemon.running_pid` /
  `check_gateway` / `ensure_running` (launchctl print, bootstrap,
  kickstart -k), the gateway state folded into every Verify line and
  heartbeat, `healthy` requires a live gateway, `OPENCLAW_GATEWAY_AUTOHEAL`
  (default on) and `HERMES_GATEWAY_AUTOHEAL` (default off), the self-heal
  refusing an operator-disabled service, `OPENCLAW_SERVICE_REPAIR_POLICY=
  external` on doctor, `--no-snapshot` on mechanic-updater and run-now,
  and a new `mechanic snapshots` listing (the private code referenced it
  but never had it). Corrected the 2026-10-03 diagnosis: it was the
  private v0.1.4 self-heal in the supervisor, not OpenClaw's CLI, that
  restarted the booted-out gateway. Scrubbed the agent's name from this
  file per the private repo's sync rule. 66 tests.
- Round 3 of the walkthrough, before the first nightly: `hermes doctor` on
  the Mac showed state.db at 638 MB and `hermes backup --quick` keeps 20
  copies, so the plan to run it before every tar was a disk bomb. Replaced
  with Mechanic's own SQLite online backup of the declared databases
  (`sqlite_globs`), gzipped into `<snapshot>/sqlite/`, live db and
  sidecars excluded from the tar, `state-snapshots/` excluded, restore
  puts the copies back. Tested with a WAL database held open by a writer.
  `mechanic plan --target hermes` on the real Mac: healthy, "already
  ahead of v2026.9.24, waits for a newer tag", nothing to install; no
  newer tag exists upstream as of 2026-10-04. The operator's gateway
  watchdog (every 5 min, hourly cooldown, kickstart only when the process
  is gone or a platform connection has failed for 15 min) coexists with
  Mechanic's drain-first restart; HERMES_GATEWAY_AUTOHEAL stays off.

### Current state
Code complete, unit-tested, and simulated. NOT run against a live Hermes
or a live OpenClaw. Confirmed on the Mac mini 2026-10-04 (round 1 of the
supervised walkthrough): Hermes IS a source install (launcher
/Users/openclaw/.local/bin/hermes, checkout ~/.hermes/hermes-agent, git
method, Python 3.14.7), clean working tree, HEAD 98d8ea7 at release date
2026.9.24 and 430 commits behind main; `git describe` there preferred a
canary tag, so describe_head now excludes `*+*` and `*-*`. The OpenClaw
gateway (ai.openclaw.gateway) was still running and enabled. Two operator
LaunchAgents sit beside Mechanic: com.texasaggie1.hermes-gateway-watchdog
and com.texasaggie1.michael-hermes-backup (last exit 1).
Unverified on a real Mac: that `hermes pm install` after a bare checkout
leaves the launcher and the launchd plist pointing at a working
environment generation; that `hermes --version` reports the tag after a
detached checkout; whether the install checkout is a partial clone (the
tag fetch then lazily pulls blobs). The 2026-09-12 OpenClaw caveats
(`--tag` path, channel key) still stand. To take the OpenClaw agent offline now:
`TARGETS=hermes` in the .env (or `launchctl disable` before `bootout`).

### Decisions made
- Pin Hermes via git + `hermes pm install` rather than only trigger-delay
  `hermes update --yes` (rejected as the default: it installs the tip of
  main, so the week buys nothing against the code itself). Both modes
  ship; release-tag is the default.
- Age from first sight, not tag date; tags mirrored into
  `refs/mechanic/upstream-tags/` rather than `refs/tags/` (rejected:
  writing Hermes's own tag namespace, and the "would clobber" dance).
- Ancestry, not version strings, decides upgrade/current/ahead/diverged
  for Hermes; version order only ranks candidates.
- Gateway restart after doctor, not before (Hermes's own order).
- `hermes doctor --fix` kept in the unattended path (`HERMES_DOCTOR_FIX`
  turns it off) because it is the only unattended route to config
  migration; its side effects are listed in .env.example.
- Did not add a first-seen ledger for OpenClaw: npm's `time` map is
  server-side, so the registry date is sound there.
- Did not rename the repository or the console scripts; "Mechanic for
  OpenClaw" stays the name, Hermes is documented as a second target.

### Open questions
- The private repo must adopt v0.2.0 (copy the product files from its
  `public/` checkout and commit there as v0.2.0) so the two lines agree
  again; the maintainer owns that commit. Until then the Mac's venv can
  point at the public checkout (`pip install -e public/`) and back.
- Does `hermes pm install` after `git checkout --detach` republish the
  launcher / plist for a new Python generation, or is `hermes gateway
  restart` enough? If not, `_update_takeover.publish_launchers` is the
  internal that does it; find its CLI surface.
- Is Randy's Hermes a source install, and which channel/version is it on?
- When Nous publishes the stable channel record, should release-tag mode
  switch to pinning the stable head instead of date tags?

### Next session should
1. On the Mac mini, with Randy watching: set `TARGETS=hermes` (plus
   openclaw if wanted), `pip install --require-hashes -r requirements.txt`
   and `pip install --no-build-isolation --no-deps -e .` in the venv,
   `mechanic status`, `mechanic plan`, `hermes backup`, then
   `mechanic run-now --target hermes`. Confirm the pin, `hermes --version`,
   `hermes gateway status`, and the report; capture the real
   `hermes --version` and `hermes pm status` output for the test fixtures.
2. Merge to main once the supervised run passes, then disable the OpenClaw gateway
   with `launchctl disable` + `bootout` and confirm the next heartbeat
   reports it as operator-disabled.
3. Run `scripts/deps/relock.sh` for the idna/urllib3 bumps noted on
   2026-10-04 and record the moved versions.

---

## 2026-10-04 (UTC) - branch: main - v0.1.4 merged to main

### What we did
- Merged `claude/open-claw-version-delay-gr91ha` (commit 080aa19, v0.1.4:
  one-week waiting period for OpenClaw updates, hash-locked Python
  dependencies, first tests) into main as a fast-forward. No pull request
  was used; main had not moved since 2026-08-31, so the history stays
  linear.
- CLAUDE.md now repeats the dependency-quarantine rule next to the two
  rules it already carried (SESSIONS.md before every push; never an
  OpenClaw skill).
- Re-verified before merging, on 2026-10-04: all 11 pins in
  `requirements.txt` still pass `scripts/deps/check_pin_age.py` (ages 47
  days to 5.8 years, nothing yanked); the 22 unit tests pass.
- Checked the OpenClaw registry the same day: `latest` is 2026.9.8
  (published 2026-10-03), `extended-stable` moved to 2026.8.35, and
  backports on the 2026.7.x and 2026.8.x lines now interleave with the
  2026.9.x train (2026.7.34, 7.35, 9.6, 8.33, 9.7, 8.34, 8.35, 9.8 over
  two weeks). Against that registry the planner picks 2026.9.6 (published
  2026-09-23, 10 days old): 9.7 and 9.8 are too young and the backports
  lose on version order. That is the designed behaviour.

### Current state
main carries v0.1.4. Everything in the 2026-09-12 entry's "Current state"
still holds: the waiting period is tested in simulation and by unit tests
only, the `openclaw update --yes --tag <version>` path has not been
exercised on a live install, and the channel key in `openclaw update
status --json` is unconfirmed (defensive read, default stable). Two pins
have newer releases that are now old enough to take, idna 3.20 and
urllib3 2.8.0; not bumped here because a merge is not the place for it.

### Decisions made
- Fast-forward merge rather than a merge commit: the public repo has had
  a single linear history since v0.1.3 and the SESSIONS.md entries are
  the record of what landed when.
- No version bump for the merge itself; v0.1.4 is what was on the branch.
- Dependency bumps (idna, urllib3) deferred to a session that runs
  `scripts/deps/relock.sh` and records the moved versions, per AGENTS.md
  section 6.

### Open questions
- Unchanged from 2026-09-12: live `--tag` behaviour, the channel key, and
  OpenClaw's own unlocked npm dependency tree.

### Next session should
1. Run the first nightly with the waiting period supervised on the Mac
   mini (`mechanic run-now` while watching), confirm the `--tag` install
   and the morning report, then let the 02:00 fire take over.
2. Save the real `openclaw update status --json` output (redacted) and
   make `_extract_channel` exact; add it to the test fixture.
3. Run `scripts/deps/relock.sh`, confirm it moves idna and urllib3 only,
   run `check_pin_age.py`, and record the bump here.

---

## 2026-09-12 (UTC) - branch: claude/open-claw-version-delay-gr91ha - v0.1.4, one-week waiting period + locked dependencies

### What we did
- New supply-chain waiting period for OpenClaw updates: `MIN_UPDATE_AGE_DAYS`
  (default 7, 0 disables). The nightly no longer installs whatever
  `openclaw update` picks; `mechanic/release_age.py` reads OpenClaw's
  publish dates from the npm registry (one read-only `npm view openclaw
  time versions dist-tags --json` through the operator's npm), chooses the
  newest real release that has been public for at least that long, and
  installs exactly it with `openclaw update --yes --tag <version>`. Never
  downgrades, never picks prereleases or anything above npm's `latest`
  tag, and any failure to establish dates (no npm, registry down,
  unknown installed version) means "install nothing" with the reason in
  the morning report.
- Why it installs an OLDER release rather than waiting for latest: OpenClaw
  ships every 2 to 3 days (2026.9.1 09-03, 9.2 09-05, 9.3 09-08, 9.4
  09-11), so "latest is a week old" would never happen. Mechanic now
  trails the release train by a week instead.
- Non-stable channels (beta, extended-stable, dev): the wait applies to
  the version OpenClaw reports, no intermediate-version picking, and no
  `--tag` on extended-stable/dev because OpenClaw refuses it there.
  Channel comes from a best-effort read of `openclaw update status
  --json` (`verifier._extract_channel`), defaulting to stable.
- Supervisor heartbeat now says which version the nightly will install
  and why, or when the held-back release becomes eligible. `mechanic
  status` shows `min update age` and which npm it found. Morning report
  carries a "Waiting period" note on every run that had an update.
- Mechanic's own dependencies locked the same way: `requirements.txt`
  pins all 11 packages (runtime + transitive + setuptools/wheel for the
  editable build) with sha256 hashes, generated by
  `scripts/deps/relock.sh` (uv with `--exclude-newer` = now minus 7 days)
  and verified by `scripts/deps/check_pin_age.py` (stdlib only, asks PyPI
  for upload dates and yank status, exits 1 on a pin under 7 days old).
  README quickstart is now the two-step
  `pip install --require-hashes -r requirements.txt` then
  `pip install --no-build-isolation --no-deps -e .`.
- First tests in the repo: `tests/test_release_age.py` (22 stdlib
  unittest cases over a trimmed copy of the real registry payload:
  release-train walk, backport on an older line, prerelease exclusion,
  above-latest exclusion, no-downgrade, unknown version, npm failure,
  other channels, channel extraction). All pass.
- Verified end to end with a fake `openclaw` and `npm` in a scratch HOME:
  installed 2026.8.2 -> nightly ran `openclaw update --yes --tag
  2026.9.2`, hook ran, verify passed; installed 2026.9.2 -> "skipped
  (waiting period)" with no update call; npm removed -> "could not be
  applied", no update call; `MIN_UPDATE_AGE_DAYS=0` -> plain `openclaw
  update --yes` to 2026.9.4; `--test-fire` still skips the update step.
- Version bumped to 0.1.4 (pyproject + `mechanic/__init__.py`). README,
  AGENTS.md, `.env.example` updated.

### Current state
Code complete and tested against simulations only. NOT yet run against a
live OpenClaw install: the `openclaw update --yes --tag <version>` path
is documented by OpenClaw but has not been exercised on the Mac mini,
and the exact JSON key for the channel in `openclaw update status
--json` is unconfirmed (extraction is defensive, default stable). Fresh
clones install from `requirements.txt` per the README. The pins are the
newest release of every package as of 2026-09-05 (all between 25 days
and 5 years old). `SNAPSHOT_DIR` configurability, previously pencilled
in as v0.1.4, is still open and moves to a later patch.

### Decisions made
- Install the newest release that is old enough, via `--tag`, rather
  than waiting for `latest` to age (rejected: with a 2 to 3 day cadence
  it would never update).
- Registry publish time is the age signal, via npm, not a direct HTTP
  call to registry.npmjs.org (rejected: bypasses the operator's .npmrc
  mirror/proxy/auth and stretches the "never phones home" rule) and not
  a Mechanic-side "first seen" timestamp (rejected: stacks delay after
  any downtime, needs more state, and the registry already knows).
- `npm view` ignores npm's `--before` date filter (tested), so the
  filtering is done in Python; the version ordering is plain semver,
  which OpenClaw's calendar versions satisfy.
- Unknown means wait: every failure mode of the age check resolves to
  "do not touch the install", matching the v0.1.2 availability gate.
- Hash-locked `requirements.txt` regenerated by tool only. uv chosen for
  relock because `--exclude-newer` enforces the age rule during
  resolution; pip-tools cannot. `pyproject.toml` keeps loose floors.
- setuptools/wheel are in the lock and the README uses
  `--no-build-isolation` so the editable install does not fetch an
  unpinned build backend.
- Did NOT enable `npm config set before=...` to quarantine OpenClaw's own
  65 floating npm dependencies: it would also filter the `openclaw`
  package itself, interacts with OpenClaw's updater in ways we cannot
  test here, and OpenClaw may install via pnpm/bun. Documented as a gap.
- Version bump to 0.1.4 follows the one-feature-per-patch convention.

### Open questions
- Does a real `openclaw update --yes --tag 2026.9.x` behave like a normal
  update (doctor, plugin sync, restart) on the Mac mini? Docs say yes.
- What is the channel key in `openclaw update status --json`? Capture
  the real payload once and pin `_extract_channel`.
- Should the waiting period also cover OpenClaw's dependency tree
  (`npm_config_before` during `openclaw update`, or a check that the
  resolved tree contains nothing under 7 days old)?

### Next session should
1. Run the first nightly with the waiting period supervised on the Mac
   mini (`mechanic run-now` while watching), confirm the `--tag` install
   and the morning report, then let the 02:00 fire take over.
2. Save the real `openclaw update status --json` output (redacted) and
   make `_extract_channel` exact; add it to the test fixture.
3. Decide on the OpenClaw dependency-tree gap above; if `npm_config_before`
   works with `openclaw update --tag`, wire it in behind the same
   `MIN_UPDATE_AGE_DAYS`.

---

## 2026-08-31 (CDT) - branch: main - recommend AI-driven install

### What we did
- README now openly recommends running the install (and later
  maintenance) with a good AI coding tool: a short note at the top of
  Quickstart and a strengthened "Working on this with an AI assistant"
  section. Both Claude and ChatGPT have been through this workflow.

### Current state
No code changes; docs only.

### Decisions made
- The recommendation lives in the README (for humans); AGENTS.md stays
  addressed to the tools themselves.

### Open questions
- None new.

### Next session should
1. Carry on from the entries below.

## 2026-08-31 (CDT) - branch: main - quickstart clone URL fix

### What we did
- Fixed the README Quickstart: it cloned the maintainer's private
  development repo (a 404 for everyone else). It now clones this repo.

### Current state
Quickstart works end to end from a fresh clone of this repo.

### Decisions made
- None beyond the fix.

### Open questions
- None new.

### Next session should
1. Carry on from the release entry below.

## 2026-08-31 (CDT) - branch: main - v0.1.3, initial public release

### What we did
- First public release of tekRESCUE Mechanic for OpenClaw, split out from
  the private development repo with a fresh history. Code arrives at
  v0.1.3.
- What v0.1.x already contains, learned in production over the summer of
  2026: tar-based snapshots that tolerate OpenClaw's live daemon
  (v0.1.1), an update-availability gate so a no-op `openclaw update`
  cannot silently wipe local patches, plus the POST_UPDATE_HOOK to
  re-apply them after real updates (v0.1.2), and emergency disk pruning
  so a full snapshot volume prunes Mechanic's own oldest nightlies
  instead of failing the run (v0.1.3).
- The README troubleshooting section includes the fix for the update
  failure a lot of people are hitting right now: npm 12 blocks OpenClaw's
  install scripts, which strands the 2.0 install-guard sentinel and makes
  every update verify fail and roll back.

### Current state
Everything in the repo is running nightly in production on an Apple
Silicon Mac mini supervising a live OpenClaw install, including through
the OpenClaw 2026.8.1 ("2.0") major upgrade. Fresh installs should follow
the README quickstart. The interactive-doctor machinery
(`KNOWN_PROMPTS`, `PROMPT_MODE`, pexpect) is present but dormant: the
nightly uses `openclaw doctor --fix --non-interactive`.

### Decisions made
- Public repo has a clean single-commit starting history; development
  history stays in the private repo.
- AGENTS.md is the canonical AI-instruction file (works for ChatGPT,
  Codex, Cursor, and friends); CLAUDE.md is a pointer to it.
- No auto-rollback in the nightly: restoring requires stopping the
  OpenClaw daemon, which is too invasive for an unattended job. The
  morning report tells the operator how to `mechanic restore`.

### Open questions
- v0.1.4: make the snapshot store location (`SNAPSHOT_DIR`) and tar
  exclude list (`SNAPSHOT_EXTRA_EXCLUDES`) configurable. OpenClaw config
  dirs grow without bound (session logs, plugin caches), and archives
  grow with them.
- v0.2 candidates: a deeper verifier that incorporates
  `openclaw doctor --lint` (catches crash-looped channels, pending
  migrations, and expiring model auth that `openclaw --version` cannot
  see), a runtime compatibility precheck before updates, and surfacing
  supervisor state in `mechanic status`.

### Next session should
1. Whatever the top entry above this one says. If you are reading this as
   the top entry: pick from the open questions, or fix what the community
   reports.
