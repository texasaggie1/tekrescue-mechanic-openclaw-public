"""Heartbeat loop.

Fires every SUPERVISOR_INTERVAL_MINUTES via launchd. Each tick, for every
target in TARGETS:

1. If the operator has `launchctl disable`d the target's gateway, the
   target is skipped without running its CLI, and the heartbeat says so.
   This is a scar: a read-only probe of OpenClaw (`openclaw --version`,
   `openclaw update status --json`) had OpenClaw bring back a gateway the
   operator had just booted out (SESSIONS.md 2026-10-04).
2. Probes health via `<bin> --version` (the same probe the verifier uses).
2b. Checks the gateway has a live pid and, where autoheal is on, restarts
   it if not (v0.1.4). `--version` alone is not a liveness check: the CLI
   answers perfectly while the gateway is dead, which once hid a 43-hour
   outage behind "healthy" heartbeats. A gateway the operator has
   disabled is never restarted. A tick that had to restart one says so in
   the subject line, so it does not read like every other heartbeat.
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
    healed: list[str] = []
    for target in build_targets(config):
        lines, ok, restarted = _tick_target(config, target)
        blocks.append("\n".join(lines))
        if not ok:
            unhealthy.append(target.display_name)
        if restarted:
            healed.append(target.display_name)

    if config.notifier.kind != "none":
        if unhealthy:
            subject = f"tekRESCUE Mechanic: {', '.join(unhealthy)} unhealthy"
        elif healed:
            subject = f"tekRESCUE Mechanic: {', '.join(healed)} gateway was down, restarted"
        else:
            subject = "tekRESCUE Mechanic: heartbeat"
        result = notify_send(config, subject, "\n\n".join(blocks))
        if not result.delivered:
            _LOG.warning("supervisor: notifier did not deliver heartbeat: %s", result.error)
    return 1 if unhealthy else 0


def _tick_target(config: Config, target: Target) -> tuple[list[str], bool, bool]:
    """One target's heartbeat: (lines, healthy, gateway_restarted)."""
    name = target.display_name
    prefix = f"{name}: " if len(config.targets) > 1 else ""

    if target.disabled_by_operator():
        _LOG.info("supervisor[%s]: gateway %s disabled by operator; not probing", target.name, target.service_label)
        return [
            f"{prefix}gateway {target.service_label} is disabled by the operator; "
            f"Mechanic is not running {name}'s CLI. Drop {target.name} from TARGETS "
            f"to silence this line."
        ], True, False

    try:
        health = target.probe()
    except Exception as exc:  # noqa: BLE001 - a tick must never die
        _LOG.error("supervisor[%s]: verifier raised: %s", target.name, exc)
        return _unhealthy_lines(prefix, name, summary=f"verifier raised: {exc}", reason=None), False, False

    if not health.healthy:
        _LOG.warning("supervisor[%s]: %s unhealthy: %s", target.name, name, health.short_summary())
        return _unhealthy_lines(prefix, name, summary=health.short_summary(), reason=health.reason), False, False

    # The CLI answering does not mean the gateway is up. Check the service
    # itself (and restart it if allowed) before reporting anything.
    try:
        gateway = target.check_gateway()
    except Exception as exc:  # noqa: BLE001 - never let the heartbeat die on this
        _LOG.error("supervisor[%s]: gateway check raised: %s", target.name, exc)
        from .daemon import GatewayCheck
        gateway = GatewayCheck(label=target.service_label, pid=None, was_down=True, error=str(exc))
    if not gateway.ok:
        _LOG.warning("supervisor[%s]: %s", target.name, gateway.summary())
        if target.gateway_autoheal:
            advice = "Mechanic tried to restart it and will try again next tick and at the nightly."
        else:
            advice = (
                f"Autoheal is off for {name}; restart it yourself, or set "
                f"{target.name.upper()}_GATEWAY_AUTOHEAL=true to let Mechanic."
            )
        return _unhealthy_lines(
            prefix, name,
            summary=f"{health.short_summary()} but the gateway is DOWN",
            reason=gateway.error, advice=advice,
        ), False, False

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
    if gateway.installed:
        lines.insert(1, gateway.summary())
    if prefix:
        lines = [prefix + lines[0]] + lines[1:]
    return lines, True, gateway.healed


def _unhealthy_lines(
    prefix: str, name: str, *, summary: str, reason: Optional[str], advice: Optional[str] = None
) -> list[str]:
    lines = [f"{prefix}Heartbeat probe reports {name} as unhealthy: {summary}"]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append(advice or "Mechanic will attempt repair during the next nightly run.")
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
