"""Heartbeat loop.

Fires every SUPERVISOR_INTERVAL_MINUTES via launchd. Each tick:

1. Probes OpenClaw health via `openclaw --version` (the same probe the
   verifier uses).
2. If healthy, also asks OpenClaw whether an update is available via
   `openclaw update status --json`. The boolean lives at
   `availability.available` in the JSON payload. When one is, the tick
   also runs the release waiting period (release_age.plan_update) so the
   heartbeat says which version the nightly will actually install, or
   when the advertised one becomes old enough.
3. Pushes a Telegram (or whatever notifier is configured) message with
   the result every tick. Healthy-and-up-to-date is still worth a ping
   because the absence of one tells the operator the supervisor itself
   has stopped firing.

v0.1.1 deliberately does NOT attempt mid-day `openclaw doctor --fix` from
the supervisor. The blast radius of mutating OpenClaw's config while the
operator is actively using it is too high for a heartbeat-frequency loop;
the nightly updater owns repair. Revisit in v0.2 once we have data on how
often the nightly fix would have been "soon enough."

The supervisor runs whether or not Mechanic is paused. Pause stops the
updater from mutating OpenClaw; it does not stop us from watching.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config, LOG_FILE, load_config
from .logging_setup import configure_logging
from .notifier import send as notify_send
from .release_age import plan_update
from .verifier import (
    UpdateAvailability,
    VerifyResult,
    check_for_updates,
    verify_openclaw,
)


_LOG = logging.getLogger(__name__)


def run_supervisor(config: Config) -> int:
    """Run a single heartbeat tick. Returns 0 on healthy, 1 on unhealthy.

    Never raises; transient errors are logged and the tick returns 1.
    Always sends a notifier ping describing the result so the absence of
    a ping tells the operator the supervisor itself has stopped firing.
    """
    try:
        health = verify_openclaw(config)
    except Exception as exc:
        _LOG.error("supervisor: verifier raised: %s", exc)
        _send_unhealthy(config, summary=f"verifier raised: {exc}", reason=None)
        return 1

    if not health.healthy:
        _LOG.warning("supervisor: OpenClaw unhealthy: %s", health.short_summary())
        _send_unhealthy(config, summary=health.short_summary(), reason=health.reason)
        return 1

    update = check_for_updates(config)
    _LOG.info(
        "supervisor: %s; update available=%s%s",
        health.short_summary(),
        update.available,
        f" -> {update.latest_version}" if update.latest_version else "",
    )
    _send_heartbeat(config, health=health, update=update)
    return 0


def _send_heartbeat(
    config: Config,
    *,
    health: VerifyResult,
    update: UpdateAvailability,
) -> None:
    if config.notifier.kind == "none":
        return

    version = health.version or "unknown version"
    if update.error:
        update_line = f"Update check skipped: {update.error}"
    elif update.available:
        update_line = _describe_update(config, health, update)
    else:
        update_line = "No updates available."

    subject = "tekRESCUE Mechanic: heartbeat"
    body_lines = [
        f"OpenClaw {version} healthy ({health.duration_ms} ms).",
        update_line,
    ]
    result = notify_send(config, subject, "\n".join(body_lines))
    if not result.delivered:
        _LOG.warning("supervisor: notifier did not deliver heartbeat: %s", result.error)


def _describe_update(
    config: Config,
    health: VerifyResult,
    update: UpdateAvailability,
) -> str:
    """One heartbeat line saying what the nightly will do about the update."""
    latest = update.latest_version or "newer version"
    plan = plan_update(
        config,
        installed_version=health.version,
        availability=update,
    )
    if plan.install:
        target = plan.target_version or latest
        if config.min_update_age_days <= 0:
            return (
                f"Update available: {latest}. Mechanic will install at "
                f"{config.update_time} local."
            )
        return (
            f"Update available: {latest}. Mechanic will install {target} at "
            f"{config.update_time} local: {plan.reason}"
        )
    if plan.error:
        return f"Update available: {latest}, NOT installing. {plan.reason}"
    return f"Update available: {latest}, waiting. {plan.reason}"


def _send_unhealthy(
    config: Config,
    *,
    summary: str,
    reason: Optional[str],
) -> None:
    if config.notifier.kind == "none":
        return
    subject = "tekRESCUE Mechanic: OpenClaw unhealthy"
    body_lines = [f"Heartbeat probe reports OpenClaw as unhealthy: {summary}"]
    if reason:
        body_lines.append(f"Reason: {reason}")
    body_lines.append("")
    body_lines.append("Mechanic will attempt repair during the next nightly run.")
    body_lines.append("Run `mechanic status` for details.")
    result = notify_send(config, subject, "\n".join(body_lines))
    if not result.delivered:
        _LOG.warning(
            "supervisor: notifier did not deliver unhealthy alert: %s", result.error
        )


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
