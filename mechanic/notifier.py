"""Optional outbound notifications.

Supports Telegram, Slack, generic webhook, and SMTP email. Defaults to none,
which keeps everything in local logs. Credentials are loaded from the .env
file and masked in all log output.

The single public entry point is `send(config, subject, body)`. It dispatches
to the configured backend and returns a NotifyResult so the caller knows
whether to log a warning. We never raise on notifier failure: a broken
webhook should not kill the nightly run.
"""

from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

import requests

from .config import Config


_LOG = logging.getLogger(__name__)


@dataclass
class NotifyResult:
    """Outcome of a notifier send. `error` is None on success."""

    kind: str
    delivered: bool
    error: Optional[str] = None


def send(config: Config, subject: str, body: str) -> NotifyResult:
    """Dispatch a notification through the configured backend.

    Never raises. Errors are caught and surfaced via NotifyResult.error so
    the updater can include them in the log without aborting.
    """
    kind = config.notifier.kind
    try:
        if kind == "none":
            return NotifyResult(kind="none", delivered=True)
        if kind == "telegram":
            return _send_telegram(config, subject, body)
        if kind == "slack":
            return _send_slack(config, subject, body)
        if kind == "webhook":
            return _send_webhook(config, subject, body)
        if kind == "email":
            return _send_email(config, subject, body)
        return NotifyResult(kind=kind, delivered=False, error=f"unknown notifier kind {kind!r}")
    except Exception as exc:
        _LOG.warning("notifier %s failed: %s", kind, exc)
        return NotifyResult(kind=kind, delivered=False, error=str(exc))


def _send_telegram(config: Config, subject: str, body: str) -> NotifyResult:
    n = config.notifier
    assert n.telegram_bot_token and n.telegram_chat_id
    url = f"https://api.telegram.org/bot{n.telegram_bot_token}/sendMessage"
    text = f"*{subject}*\n\n{body}"
    resp = requests.post(
        url,
        json={"chat_id": n.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    if resp.status_code != 200:
        return NotifyResult(
            kind="telegram",
            delivered=False,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    return NotifyResult(kind="telegram", delivered=True)


def _send_slack(config: Config, subject: str, body: str) -> NotifyResult:
    n = config.notifier
    assert n.slack_webhook_url
    text = f"*{subject}*\n```\n{body}\n```"
    resp = requests.post(n.slack_webhook_url, json={"text": text}, timeout=10)
    if resp.status_code >= 300:
        return NotifyResult(
            kind="slack",
            delivered=False,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    return NotifyResult(kind="slack", delivered=True)


def _send_webhook(config: Config, subject: str, body: str) -> NotifyResult:
    n = config.notifier
    assert n.webhook_url
    payload = {"subject": subject, "body": body}
    resp = requests.post(
        n.webhook_url,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if resp.status_code >= 300:
        return NotifyResult(
            kind="webhook",
            delivered=False,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    return NotifyResult(kind="webhook", delivered=True)


def _send_email(config: Config, subject: str, body: str) -> NotifyResult:
    n = config.notifier
    assert n.smtp_host and n.smtp_from and n.smtp_to
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = n.smtp_from
    msg["To"] = n.smtp_to
    msg.set_content(body)

    with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=15) as server:
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
        if n.smtp_user and n.smtp_password:
            server.login(n.smtp_user, n.smtp_password)
        server.send_message(msg)
    return NotifyResult(kind="email", delivered=True)
