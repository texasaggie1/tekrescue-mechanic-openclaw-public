"""Rotating file logging with secret masking.

Every Mechanic process configures the root logger via configure_logging(). Log
records are written to ~/Library/Logs/tekrescue-mechanic/mechanic.log with
daily rotation and a 7 day retention. A filter scrubs any known secret value
(bot tokens, webhook URLs, SMTP passwords) before records are emitted, as a
defense in depth on top of the rule that we never log secrets in the first
place.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Iterable


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_REDACTED = "[REDACTED]"


class SecretMaskingFilter(logging.Filter):
    """Replace any occurrence of a known secret value in the formatted message.

    The filter runs after %-formatting, so it sees the fully expanded text
    regardless of whether the caller used logger.info('x=%s', secret) or an
    f-string. record.args is cleared on mask hits to keep downstream handlers
    consistent.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        masked = message
        for secret in self._secrets:
            if secret and secret in masked:
                masked = masked.replace(secret, _REDACTED)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


def configure_logging(
    log_file: Path,
    level: str = "INFO",
    secrets: Iterable[str] = (),
    also_stderr: bool = False,
) -> None:
    """Configure the root logger for Mechanic.

    Args:
        log_file: Absolute path to the log file. The parent directory is
            created if it does not exist.
        level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        secrets: Iterable of secret strings to redact from log output.
        also_stderr: If True, also emit log records to stderr. Used by the CLI
            for foreground commands so the operator sees what happened.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    for existing in list(root.handlers):
        root.removeHandler(existing)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    mask_filter = SecretMaskingFilter(secrets)

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(mask_filter)
    root.addHandler(file_handler)

    if also_stderr:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(mask_filter)
        root.addHandler(stream_handler)
