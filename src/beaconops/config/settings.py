"""Centralized configuration settings for BeaconOps.

This module defines the application settings loaded from environment variables
(with optional ``.env`` file support). It is the single source of truth for
runtime configuration and is imported by every other module that needs
configuration values.

Responsibilities
----------------
- Load and validate configuration from environment variables.
- Provide typed, immutable settings objects.
- Parse comma-separated list values (hosts, ports) into typed collections.
- Never hardcode secrets: sensitive values must come from the environment.
- Cache the resolved settings so that repeated calls are cheap.

The module must not import from :mod:`beaconops.api`, :mod:`beaconops.core`,
:mod:`beaconops.services`, or :mod:`beaconops.utils` so that it remains free of
circular dependencies.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__all__ = [
    "Settings",
    "SettingsError",
    "get_settings",
    "reload_settings",
]

logger: logging.Logger = logging.getLogger("beaconops.config.settings")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SettingsError(ValueError):
    """Raised when configuration values are invalid."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

_ENV_PREFIX = "BEACONOPS_"

_VALID_ENVIRONMENTS: frozenset[str] = frozenset({"development", "test", "production"})

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _load_dotenv(path: Path) -> None:
    """Load key/value pairs from a ``.env`` file into ``os.environ``.

    Existing environment variables are never overwritten. Blank lines and
    lines starting with ``#`` are ignored. Values may be optionally wrapped in
    single or double quotes.

    Args:
        path: Path to the ``.env`` file. Missing files are silently ignored.
    """

    if not path.is_file():
        return

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Unable to read .env file at %s", path)
        return

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key not in os.environ:
            os.environ[key] = value


def _get_raw(name: str) -> str | None:
    """Return the raw environment value for a prefixed setting name."""

    return os.environ.get(f"{_ENV_PREFIX}{name}")


def _get_str(name: str, default: str) -> str:
    """Return a string setting, falling back to ``default`` when unset."""

    raw = _get_raw(name)
    if raw is None or raw == "":
        return default
    return raw


def _get_optional_str(name: str) -> str | None:
    """Return an optional string setting, or ``None`` when unset/empty."""

    raw = _get_raw(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def _get_bool(name: str, default: bool) -> bool:
    """Return a boolean setting parsed from common truthy/falsy spellings."""

    raw = _get_raw(name)
    if raw is None or raw.strip() == "":
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise SettingsError(f"Invalid boolean value for {_ENV_PREFIX}{name}: {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Return an integer setting, validating an optional lower bound."""

    raw = _get_raw(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"Invalid integer value for {_ENV_PREFIX}{name}: {raw!r}") from exc

    if minimum is not None and value < minimum:
        raise SettingsError(f"{_ENV_PREFIX}{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """Return a float setting, validating an optional lower bound."""

    raw = _get_raw(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"Invalid float value for {_ENV_PREFIX}{name}: {raw!r}") from exc

    if minimum is not None and value < minimum:
        raise SettingsError(f"{_ENV_PREFIX}{name} must be >= {minimum}, got {value}")
    return value


def _get_csv(name: str) -> tuple[str, ...]:
    """Return a comma-separated setting as a tuple of trimmed strings."""

    raw = _get_raw(name)
    if raw is None or raw.strip() == "":
        return ()

    items = [item.strip() for item in raw.split(",")]
    return tuple(item for item in items if item)


def _get_ports(name: str) -> frozenset[int]:
    """Return a comma-separated setting as a frozenset of valid ports."""

    raw_items = _get_csv(name)
    ports: set[int] = set()
    for item in raw_items:
        try:
            port = int(item)
        except ValueError as exc:
            raise SettingsError(f"Invalid port value for {_ENV_PREFIX}{name}: {item!r}") from exc
        if not (1 <= port <= 65535):
            raise SettingsError(f"Port {port} for {_ENV_PREFIX}{name} must be between 1 and 65535")
        ports.add(port)
    return frozenset(ports)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Immutable application settings for BeaconOps.

    All values are resolved from environment variables prefixed with
    ``BEACONOPS_``. Sensitive values (such as the administrative API key) are
    never hardcoded and must be supplied by the environment.
    """

    # Application
    env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "sqlite:///./beaconops.db"

    # HTTP client / monitoring
    http_timeout_seconds: float = 10.0
    http_max_response_bytes: int = 1_048_576
    max_concurrent_checks: int = 10

    # Scheduler
    scheduler_enabled: bool = True
    default_check_interval_seconds: int = 60

    # Incident detection
    failure_threshold: int = 3
    recovery_threshold: int = 2

    # Notifications
    notifications_enabled: bool = True
    default_webhook_url: str | None = None
    webhook_timeout_seconds: float = 5.0

    # Security / SSRF protection
    allow_private_targets: bool = False
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    allowed_ports: frozenset[int] = field(default_factory=frozenset)
    max_redirects: int = 3

    # API security
    admin_api_key: str | None = None

    # Server
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    reload: bool = False

    # -- Derived helpers ----------------------------------------------------

    @property
    def is_production(self) -> bool:
        """Return whether the application runs in production mode."""

        return self.env == "production"

    @property
    def is_test(self) -> bool:
        """Return whether the application runs in test mode."""

        return self.env == "test"

    @property
    def admin_api_key_configured(self) -> bool:
        """Return whether an administrative API key is configured."""

        return bool(self.admin_api_key)

    def __repr__(self) -> str:  # pragma: no cover - avoid leaking secrets
        return (
            f"Settings(env={self.env!r}, log_level={self.log_level!r}, "
            f"database_url={self.database_url!r}, "
            f"scheduler_enabled={self.scheduler_enabled}, "
            f"notifications_enabled={self.notifications_enabled}, "
            f"allow_private_targets={self.allow_private_targets}, "
            f"admin_api_key_configured={self.admin_api_key_configured})"
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _build_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment.

    Returns:
        A validated, immutable settings object.

    Raises:
        SettingsError: If any configuration value is invalid.
    """

    env = _get_str("ENV", "development").strip().lower()
    if env not in _VALID_ENVIRONMENTS:
        raise SettingsError(
            f"Invalid {_ENV_PREFIX}ENV value {env!r}; allowed: {sorted(_VALID_ENVIRONMENTS)}"
        )

    log_level = _get_str("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise SettingsError(
            f"Invalid {_ENV_PREFIX}LOG_LEVEL value {log_level!r}; "
            f"allowed: {sorted(_VALID_LOG_LEVELS)}"
        )

    database_url = _get_str("DATABASE_URL", "sqlite:///./beaconops.db").strip()
    if not database_url:
        raise SettingsError(f"{_ENV_PREFIX}DATABASE_URL must not be empty")

    http_timeout_seconds = _get_float("HTTP_TIMEOUT_SECONDS", 10.0, minimum=0.1)
    http_max_response_bytes = _get_int("HTTP_MAX_RESPONSE_BYTES", 1_048_576, minimum=1)
    max_concurrent_checks = _get_int("MAX_CONCURRENT_CHECKS", 10, minimum=1)

    scheduler_enabled = _get_bool("SCHEDULER_ENABLED", True)
    default_check_interval_seconds = _get_int("DEFAULT_CHECK_INTERVAL_SECONDS", 60, minimum=1)

    failure_threshold = _get_int("FAILURE_THRESHOLD", 3, minimum=1)
    recovery_threshold = _get_int("RECOVERY_THRESHOLD", 2, minimum=1)

    notifications_enabled = _get_bool("NOTIFICATIONS_ENABLED", True)
    default_webhook_url = _get_optional_str("DEFAULT_WEBHOOK_URL")
    webhook_timeout_seconds = _get_float("WEBHOOK_TIMEOUT_SECONDS", 5.0, minimum=0.1)

    allow_private_targets = _get_bool("ALLOW_PRIVATE_TARGETS", False)
    allowed_hosts = _get_csv("ALLOWED_HOSTS")
    allowed_ports = _get_ports("ALLOWED_PORTS")
    max_redirects = _get_int("MAX_REDIRECTS", 3, minimum=0)

    admin_api_key = _get_optional_str("ADMIN_API_KEY")

    host = _get_str("HOST", "0.0.0.0").strip()  # noqa: S104
    port = _get_int("PORT", 8000, minimum=1)
    if port > 65535:
        raise SettingsError(f"{_ENV_PREFIX}PORT must be <= 65535, got {port}")
    reload = _get_bool("RELOAD", False)

    if env == "production" and not admin_api_key:
        logger.warning(
            "Running in production without %sADMIN_API_KEY configured; "
            "administrative endpoints will be inaccessible.",
            _ENV_PREFIX,
        )

    return Settings(
        env=env,
        log_level=log_level,
        database_url=database_url,
        http_timeout_seconds=http_timeout_seconds,
        http_max_response_bytes=http_max_response_bytes,
        max_concurrent_checks=max_concurrent_checks,
        scheduler_enabled=scheduler_enabled,
        default_check_interval_seconds=default_check_interval_seconds,
        failure_threshold=failure_threshold,
        recovery_threshold=recovery_threshold,
        notifications_enabled=notifications_enabled,
        default_webhook_url=default_webhook_url,
        webhook_timeout_seconds=webhook_timeout_seconds,
        allow_private_targets=allow_private_targets,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        max_redirects=max_redirects,
        admin_api_key=admin_api_key,
        host=host,
        port=port,
        reload=reload,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings.

    On first call, the ``.env`` file (if present in the current working
    directory) is loaded into the environment, then settings are built from
    the environment. Subsequent calls return the cached instance.

    Returns:
        The resolved :class:`Settings` instance.

    Raises:
        SettingsError: If any configuration value is invalid.
    """

    _load_dotenv(Path.cwd() / ".env")
    return _build_settings()


def reload_settings() -> Settings:
    """Clear the settings cache and rebuild settings from the environment.

    This is primarily useful in tests and in long-running processes that need
    to pick up configuration changes without a restart.

    Returns:
        The freshly resolved :class:`Settings` instance.

    Raises:
        SettingsError: If any configuration value is invalid.
    """

    get_settings.cache_clear()
    return get_settings()
