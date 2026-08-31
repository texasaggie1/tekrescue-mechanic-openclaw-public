"""Configuration loader and well-known paths.

Reads ~/.config/tekrescue-mechanic/.env, validates every value, applies the
documented defaults, and returns a frozen Config dataclass. All other modules
import Config from here. Path constants are exported so callers (install
scripts, the CLI, tests) can reuse them without duplicating string literals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


CONFIG_DIR = Path.home() / ".config" / "tekrescue-mechanic"
CONFIG_FILE = CONFIG_DIR / ".env"

LOG_DIR = Path.home() / "Library" / "Logs" / "tekrescue-mechanic"
LOG_FILE = LOG_DIR / "mechanic.log"

STATE_DIR = Path.home() / "Library" / "Application Support" / "tekrescue-mechanic"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
RUNTIME_STATE_DIR = STATE_DIR / "state"

VALID_PROMPT_MODES = ("STRICT", "ESCALATE", "AUTO_YES")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_NOTIFIERS = ("none", "telegram", "slack", "webhook", "email")

# Mechanic-internal env vars that must NOT leak into subprocesses invoking
# OpenClaw. The OPENCLAW_CONFIG_PATH name in particular collides with
# OpenClaw's own use of that variable (OpenClaw expects it to point at the
# config FILE; Mechanic uses it to point at the config DIRECTORY). Confirmed
# in v0.1.1: leaking Mechanic's value caused `openclaw update` to emit
# "config is invalid (EISDIR on <root> read)".
_MECHANIC_ENV_VARS = (
    "OPENCLAW_BIN_PATH",
    "OPENCLAW_CONFIG_PATH",
    "UPDATE_TIME",
    "SUPERVISOR_INTERVAL_MINUTES",
    "PROMPT_MODE",
    "SNAPSHOT_RETENTION_DAYS",
    "MIN_FREE_DISK_MB_FOR_SNAPSHOT",
    "MAX_CONSECUTIVE_FAILURES",
    "PAUSE_ON_ROLLBACK_FAILURE",
    "POST_UPDATE_HOOK",
    "POST_UPDATE_HOOK_TIMEOUT_SECONDS",
    "LOG_LEVEL",
    "NOTIFIER",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
    "WEBHOOK_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
)


def clean_subprocess_env() -> dict[str, str]:
    """Return os.environ minus the Mechanic-internal vars.

    Always pass this as `env=` to subprocess.run when invoking openclaw,
    tar, or anything openclaw invokes internally (npm, node). It strips
    Mechanic's documented env vars so we never leak them into child
    processes that might interpret the same name differently.
    """
    env = os.environ.copy()
    for key in _MECHANIC_ENV_VARS:
        env.pop(key, None)
    return env


class ConfigError(Exception):
    """Raised when the .env file is missing required values or contains invalid ones."""


@dataclass(frozen=True)
class NotifierSettings:
    """Notifier kind plus any credentials it needs.

    Only the fields relevant to the active kind are populated. Unused fields
    stay at their default of None.
    """

    kind: str
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    webhook_url: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration for Mechanic.

    Construct with load_config(). Treat this as an immutable snapshot of the
    .env file at process start. If the .env changes, the supervisor and
    updater pick it up on their next launchd-scheduled invocation.
    """

    openclaw_bin_path: Path
    openclaw_config_path: Path
    update_time: str
    supervisor_interval_minutes: int
    prompt_mode: str
    snapshot_retention_days: int
    min_free_disk_mb_for_snapshot: int
    max_consecutive_failures: int
    pause_on_rollback_failure: bool
    post_update_hook: Optional[Path]
    post_update_hook_timeout_seconds: int
    log_level: str
    notifier: NotifierSettings

    def secret_values(self) -> list[str]:
        """Return all secret strings that must be masked in log output."""
        candidates = (
            self.notifier.telegram_bot_token,
            self.notifier.slack_webhook_url,
            self.notifier.webhook_url,
            self.notifier.smtp_password,
        )
        return [v for v in candidates if v]


def load_config(env_file: Path | None = None) -> Config:
    """Load and validate Mechanic configuration from a .env file.

    Args:
        env_file: Optional override for the .env path. Defaults to
            ~/.config/tekrescue-mechanic/.env.

    Returns:
        A validated, frozen Config.

    Raises:
        ConfigError: If a required value is missing or any value is invalid.
    """
    path = env_file or CONFIG_FILE
    if path.exists():
        load_dotenv(path, override=False)

    bin_raw = _env("OPENCLAW_BIN_PATH")
    cfg_raw = _env("OPENCLAW_CONFIG_PATH")
    if not bin_raw:
        raise ConfigError(f"OPENCLAW_BIN_PATH is required. Set it in {path}.")
    if not cfg_raw:
        raise ConfigError(f"OPENCLAW_CONFIG_PATH is required. Set it in {path}.")

    update_time = _env("UPDATE_TIME") or "02:00"
    if not _is_valid_hhmm(update_time):
        raise ConfigError(
            f"UPDATE_TIME must be HH:MM in 24h time. Got: {update_time!r}."
        )

    interval = _env_int("SUPERVISOR_INTERVAL_MINUTES", default=240, min_value=1)
    retention = _env_int("SNAPSHOT_RETENTION_DAYS", default=14, min_value=1)
    min_free_mb = _env_int("MIN_FREE_DISK_MB_FOR_SNAPSHOT", default=500, min_value=1)
    max_failures = _env_int("MAX_CONSECUTIVE_FAILURES", default=3, min_value=1)
    pause_on_rollback = _env_bool("PAUSE_ON_ROLLBACK_FAILURE", default=True)

    hook_raw = _env("POST_UPDATE_HOOK")
    post_update_hook = Path(hook_raw).expanduser() if hook_raw else None
    hook_timeout = _env_int(
        "POST_UPDATE_HOOK_TIMEOUT_SECONDS", default=300, min_value=1
    )

    prompt_mode = (_env("PROMPT_MODE") or "STRICT").upper()
    if prompt_mode not in VALID_PROMPT_MODES:
        raise ConfigError(
            f"PROMPT_MODE must be one of {', '.join(VALID_PROMPT_MODES)}. "
            f"Got: {prompt_mode!r}."
        )

    log_level = (_env("LOG_LEVEL") or "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL must be one of {', '.join(VALID_LOG_LEVELS)}. "
            f"Got: {log_level!r}."
        )

    notifier = _load_notifier()

    return Config(
        openclaw_bin_path=Path(bin_raw).expanduser(),
        openclaw_config_path=Path(cfg_raw).expanduser(),
        update_time=update_time,
        supervisor_interval_minutes=interval,
        prompt_mode=prompt_mode,
        snapshot_retention_days=retention,
        min_free_disk_mb_for_snapshot=min_free_mb,
        max_consecutive_failures=max_failures,
        pause_on_rollback_failure=pause_on_rollback,
        post_update_hook=post_update_hook,
        post_update_hook_timeout_seconds=hook_timeout,
        log_level=log_level,
        notifier=notifier,
    )


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_int(name: str, *, default: int, min_value: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer. Got: {raw!r}.")
    if value < min_value:
        raise ConfigError(f"{name} must be at least {min_value}. Got: {value}.")
    return value


_TRUTHY = ("1", "true", "yes", "y", "on")
_FALSY = ("0", "false", "no", "n", "off")


def _env_bool(name: str, *, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigError(
        f"{name} must be a boolean (true/false, yes/no, 1/0, on/off). Got: {raw!r}."
    )


def _is_valid_hhmm(s: str) -> bool:
    if len(s) != 5 or s[2] != ":":
        return False
    try:
        hour = int(s[:2])
        minute = int(s[3:])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _load_notifier() -> NotifierSettings:
    kind = (_env("NOTIFIER") or "none").lower()
    if kind not in VALID_NOTIFIERS:
        raise ConfigError(
            f"NOTIFIER must be one of {', '.join(VALID_NOTIFIERS)}. Got: {kind!r}."
        )

    if kind == "none":
        return NotifierSettings(kind="none")

    if kind == "telegram":
        token = _env("TELEGRAM_BOT_TOKEN")
        chat = _env("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise ConfigError(
                "NOTIFIER=telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
            )
        return NotifierSettings(
            kind="telegram", telegram_bot_token=token, telegram_chat_id=chat
        )

    if kind == "slack":
        url = _env("SLACK_WEBHOOK_URL")
        if not url:
            raise ConfigError("NOTIFIER=slack requires SLACK_WEBHOOK_URL.")
        return NotifierSettings(kind="slack", slack_webhook_url=url)

    if kind == "webhook":
        url = _env("WEBHOOK_URL")
        if not url:
            raise ConfigError("NOTIFIER=webhook requires WEBHOOK_URL.")
        return NotifierSettings(kind="webhook", webhook_url=url)

    # kind == "email"
    host = _env("SMTP_HOST")
    sender = _env("SMTP_FROM")
    recipient = _env("SMTP_TO")
    if not host or not sender or not recipient:
        raise ConfigError(
            "NOTIFIER=email requires SMTP_HOST, SMTP_FROM, and SMTP_TO."
        )
    port = _env_int("SMTP_PORT", default=587, min_value=1)
    return NotifierSettings(
        kind="email",
        smtp_host=host,
        smtp_port=port,
        smtp_user=_env("SMTP_USER") or None,
        smtp_password=_env("SMTP_PASSWORD") or None,
        smtp_from=sender,
        smtp_to=recipient,
    )
