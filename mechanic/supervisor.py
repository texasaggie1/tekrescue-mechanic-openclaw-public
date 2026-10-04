"""Heartbeat loop.

Fires every SUPERVISOR_INTERVAL_MINUTES via launchd. Each tick, for every
target in TARGETS:

1. If the operator has `launchctl disable`d the target's gateway, the
   target is skipped without running its CLI, and the heartbeat says so.
   This is a scar: a read-only probe of OpenClaw (`openclaw --version`,
   `openclaw update status --json`) had OpenClaw bring back a gateway the
   operator had just booted out (SESSIONS.md 2026-10-04).
2. Probes health via `<bin> --version` (the same probe the verifier uses).
3. If healthy, asks what the nightly would do: OpenClaw's `update status
   --json` plus the npm waiting period, or Hermes's release tags plus the
   first-sight waiting period. The heartbeat names the version the nightly
   will install and why, or when the held-back release becomes eligible.
4. Pushes one notifier message covering every target. Healthy-and-nothing-
   to-do is still worth a ping because the absence of one tells the
   operator the supervisor itself has stopped firing.

The supervisor never mutates a product. Pause stops the updater from
mutating; it does not stop us from watching.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config, LOG_FILE, load_config
from .logging_setup import configure_logging
from .notifier import send as notify_send
from .targets import Target, build_targets
from .verifier import VerifyResult


_LOG = logging.getLogger(__name__)


def run_supervisor(config: Config) -> int:
    """Run a single heartbeat tick. Returns 0 when every target is healthy.

    Never raises; transient errors are logged and the tick returns 1.
    Always sends a notifier ping describing the result so the absence of
    a ping tells the operator the supervisor itself has stopped firing.
    """
    blocks: list[str] = []
    unhealthy: list[str] = []
    for target in build_targets(config):
        lines, ok = _tick_target(config, target)
        blocks.append("\n".join(lines))
        if not ok:
            unhealthy.append(target.display_name)

    if config.notifier.kind != "none":
        if unhealthy:
            subject = f"tekRESCUE Mechanic: {', '.join(unhealthy)} unhealthy"
        else:
            subject = "tekRESCUE Mechanic: heartbeat"
        result = notify_send(config, subject, "\n\n".join(blocks))
        if not result.delivered:
            _LOG.warning("supervisor: notifier did not deliver heartbeat: %s", result.error)
    return 1 if unhealthy else 0


def _tick_target(config: Config, target: Target) -> tuple[list[str], bool]:
    name = target.display_name
    prefix = f"{name}: " if len(config.targets) > 1 else ""

    if target.disabled_by_operator():
        _LOG.info("supervisor[%s]: gateway %s disabled by operator; not probing", target.name, target.service_label)
        return [
            f"{prefix}gateway {target.service_label} is disabled by the operator; "
            f"Mechanic is not running {name}'s CLI. Drop {target.name} from TARGETS "
            f"to silence this line."
        ], True

    try:
        health = target.probe()
    except Exception as exc:  # noqa: BLE001 - a tick must never die
        _LOG.error("supervisor[%s]: verifier raised: %s", target.name, exc)
        return _unhealthy_lines(prefix, name, summary=f"verifier raised: {exc}", reason=None), False

    if not health.healthy:
        _LOG.warning("supervisor[%s]: %s unhealthy: %s", target.name, name, health.short_summary())
        return _unhealthy_lines(prefix, name, summary=health.short_summary(), reason=health.reason), False

    assessment = None
    try:
        assessment = target.assess(health)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("supervisor[%s]: assessment raised: %s", target.name, exc)
    _LOG.info(
        "supervisor[%s]: %s; %s",
        target.name,
        health.short_summary(),
        assessment.reason if assessment else "assessment unavailable",
    )
    lines = target.heartbeat_lines(health, assessment)
    if prefix:
        lines = [prefix + lines[0]] + lines[1:]
    return lines, True


def _unhealthy_lines(prefix: str, name: str, *, summary: str, reason: Optional[str]) -> list[str]:
    lines = [f"{prefix}Heartbeat probe reports {name} as unhealthy: {summary}"]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("Mechanic will attempt repair during the next nightly run.")
    lines.append("Run `mechanic status` for details.")
    return lines


def main() -> int:
    """Launchd-invoked entry point."""
    try:
        config = load_config()
    except Exception as exc:
        configure_logging(LOG_FILE, level="ERROR", secrets=())
        logging.getLogger(__name__).error("supervisor: could not load config: %s", exc)
        return 2

    configure_logging(
        LOG_FILE,
        level=config.log_level,
        secrets=config.secret_values(),
        also_stderr=False,
    )
    return run_supervisor(config)


if __name__ == "__main__":
    raise SystemExit(main())
