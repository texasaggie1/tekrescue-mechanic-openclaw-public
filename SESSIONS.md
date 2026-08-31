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
