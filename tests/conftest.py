"""Shared pytest fixtures for the BeaconOps test suite.

This module provides fixtures used across the test suite to set up isolated
test environments. It configures an in-memory SQLite database, overrides
application settings, and provides ready-to-use service instances.

The fixtures intentionally avoid any external services or real credentials so
that the test suite is fully reproducible and hermetic.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from beaconops.config.settings import Settings, reload_settings
from beaconops.core.monitor import (
    Base,
    MonitorService,
    get_engine,
    get_session_factory,
)
from beaconops.services.incident_identification import IncidentIdentifier
from beaconops.utils.logging import configure_logging


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate each test from the ambient environment.

    The fixture clears any ``BEACONOPS_*`` variables that may be present in the
    host environment, sets a deterministic test configuration, and resets the
    settings cache before and after each test.
    """

    for key in list(os.environ):
        if key.startswith("BEACONOPS_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("BEACONOPS_ENV", "test")
    monkeypatch.setenv("BEACONOPS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("BEACONOPS_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("BEACONOPS_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("BEACONOPS_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("BEACONOPS_ALLOW_PRIVATE_TARGETS", "true")
    monkeypatch.setenv("BEACONOPS_ALLOWED_PORTS", "")
    monkeypatch.setenv("BEACONOPS_ALLOWED_HOSTS", "")
    monkeypatch.setenv("BEACONOPS_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("BEACONOPS_RECOVERY_THRESHOLD", "2")
    monkeypatch.setenv("BEACONOPS_MAX_CONCURRENT_CHECKS", "5")

    reload_settings()
    configure_logging(force=True)

    yield

    reload_settings()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    """Return the resolved test settings."""

    return reload_settings()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Any]:
    """Provide a fresh in-memory SQLite engine with the schema created.

    The engine is disposed after the test to release resources.
    """

    eng = get_engine(settings)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture()
def session_factory(engine: Any) -> sessionmaker[Session]:
    """Provide a session factory bound to the test engine."""

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a SQLAlchemy session for direct database assertions."""

    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


@pytest.fixture()
def monitor_service(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> Iterator[MonitorService]:
    """Provide a :class:`MonitorService` bound to the test database."""

    service = MonitorService(settings=settings, session_factory=session_factory)
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def incident_identifier(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> IncidentIdentifier:
    """Provide an :class:`IncidentIdentifier` bound to the test database."""

    return IncidentIdentifier(settings=settings, session_factory=session_factory)
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>