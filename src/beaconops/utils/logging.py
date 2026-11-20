"""Centralized logging configuration for BeaconOps.

This module provides the logging setup used across the application. It
configures a consistent log format, honors the ``BEACONOPS_LOG_LEVEL`` setting,
and exposes a :func:`get_logger` helper so that every module obtains its logger
through a single, predictable entry point.

Responsibilities
----------------
- Configure the root ``beaconops`` logger exactly once.
- Provide a :func:`get_logger` helper returning namespaced child loggers.
- Avoid duplicate handlers when the configuration is applied more than once.
- Never emit log records to stdout via ``print``; all output flows through the
  standard :mod:`logging` machinery.

The module must not import from :mod:`beaconops.api`, :mod:`beaconops.core`, or
:mod:`beaconops.services` so that it remains free of circular dependencies.
"""

from __future__ import annotations

import contextlib
import logging
import sys

from beaconops.config.settings import Settings, get_settings

__all__ = [
    "DEFAULT_LOG_FORMAT",
    "ROOT_LOGGER_NAME",
    "configure_logging",
    "get_logger",
]

ROOT_LOGGER_NAME: str = "beaconops"

DEFAULT_LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S%z"

_VALID_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _resolve_level(level: str | None, settings: Settings) -> int:
    """Resolve a log level name into a numeric logging level.

    Args:
        level: Optional explicit level name. When omitted, the level from the
            application settings is used.
        settings: The application settings providing the default level.

    Returns:
        The numeric logging level.

    Raises:
        ValueError: If the resolved level name is not a valid logging level.
    """

    candidate = (level or settings.log_level or "INFO").upper()
    if candidate not in _VALID_LEVELS:
        raise ValueError(f"Invalid log level {candidate!r}; allowed: {sorted(_VALID_LEVELS)}")
    return getattr(logging, candidate)


def configure_logging(
    level: str | None = None,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure the ``beaconops`` root logger.

    The configuration is idempotent: calling this function multiple times does
    not add duplicate handlers unless ``force`` is ``True``.

    Args:
        level: Optional explicit log level name (e.g. ``"DEBUG"``). When
            omitted, the level from the application settings is used.
        settings: Optional settings override. When omitted, the global
            application settings are used.
        force: When ``True``, existing handlers on the ``beaconops`` logger are
            removed before configuring new ones.

    Returns:
        The configured ``beaconops`` root logger.

    Raises:
        ValueError: If the resolved log level is invalid.
    """

    resolved_settings = settings or get_settings()
    numeric_level = _resolve_level(level, resolved_settings)

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(numeric_level)
    logger.propagate = False

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()

    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(numeric_level)
        handler.setFormatter(logging.Formatter(fmt=DEFAULT_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)
    else:
        for handler in logger.handlers:
            handler.setLevel(numeric_level)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger for the given module name.

    When ``name`` is provided and does not already start with the
    ``beaconops`` prefix, it is nested under the ``beaconops`` root logger so
    that all application logs share the same configuration.

    Args:
        name: Optional logger name, typically ``__name__``.

    Returns:
        A :class:`logging.Logger` instance.
    """

    if not name:
        return logging.getLogger(ROOT_LOGGER_NAME)

    if name == ROOT_LOGGER_NAME or name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)

    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
