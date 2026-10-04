# CLAUDE.md

Read [AGENTS.md](AGENTS.md) in full before doing any work in this
repository. It is the source of truth for every AI assistant here,
Claude included: project architecture, non-negotiables, code
conventions, maintenance standing orders, and the SESSIONS.md protocol.

Then read [SESSIONS.md](SESSIONS.md) (top entry first) for the current
state of the project. If the top entry has a "Resume here" block, a
deployment or walkthrough was in progress when the last session ended;
follow that block before planning anything new, and keep it current
(rewrite it, do not append to it) whenever the state on the operator's
machine changes.

Four rules worth repeating even in a pointer file:

- Update SESSIONS.md before every `git push`, in the same commit as the
  work. No exceptions. Checkpoint early in a long session; a session can
  die at any moment and the next one trusts SESSIONS.md completely.
- Mechanic must never become an OpenClaw skill, agent, or extension, nor
  a Hermes plugin or skill. It survives the death of whatever it looks
  after; that is the whole product.
- Mechanic never touches a product the operator has not listed in
  `TARGETS`, and never restarts a gateway the operator has `launchctl
  disable`d. A deliberate shutdown is not an outage.
- Dependencies are quarantined, like OpenClaw updates. `requirements.txt`
  changes only through `scripts/deps/relock.sh`, never to a release under
  seven days old, and `scripts/deps/check_pin_age.py` must pass before
  the change ships. Never hand-edit the lock.
