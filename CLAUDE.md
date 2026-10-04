# CLAUDE.md

Read [AGENTS.md](AGENTS.md) in full before doing any work in this
repository. It is the source of truth for every AI assistant here,
Claude included: project architecture, non-negotiables, code
conventions, maintenance standing orders, and the SESSIONS.md protocol.

Then read [SESSIONS.md](SESSIONS.md) (top entry first) for the current
state of the project.

Three rules worth repeating even in a pointer file:

- Update SESSIONS.md before every `git push`, in the same commit as the
  work. No exceptions.
- Mechanic must never become an OpenClaw skill, agent, or extension. It
  survives OpenClaw's death; that is the whole product.
- Dependencies are quarantined, like OpenClaw updates. `requirements.txt`
  changes only through `scripts/deps/relock.sh`, never to a release under
  seven days old, and `scripts/deps/check_pin_age.py` must pass before
  the change ships. Never hand-edit the lock.
