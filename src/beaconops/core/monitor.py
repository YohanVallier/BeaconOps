"""Monitor management and check execution for BeaconOps.

This module implements the domain logic for managing monitored endpoints and
executing availability checks against them. It is intentionally decoupled from
the HTTP API layer: the same operations are used by the scheduler, by manual
check endpoints, and by tests.

Responsibilities
----------------
- Persist and manage monitor definitions (create, read, update, delete).
- Execute a single check for a monitor using the shared HTTP client.
- Persist the resulting check record.
- Delegate incident evaluation to the incident identification service.

The module relies on SQLAlchemy for persistence and on
:mod:`beaconops.services.http_client` for outbound HTTP requests. All outbound
requests are subject to the SSRF protections implemented in
:mod:`beaconops.utils.validations`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from beaconops.config.settings import Settings, get_settings
from beaconops.services.http_client import (
    CheckOutcome,
    HttpClient,
    HttpCheckResult,
)
from beaconops.utils.logging import get_logger
from beaconops.utils.validations import (
    ValidationError,
    validate_monitor_url,
)

__all__ = [
    "Base",
    "CheckResult",
    "CheckStatus",
    "Monitor",
    "MonitorCheck",
    "MonitorNotFoundError",
    "MonitorService",
    "MonitorValidationError",
    "get_engine",
    "get_session_factory",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MonitorValidationError(ValueError):
    """Raised when a monitor configuration is invalid."""


class MonitorNotFoundError(LookupError):
    """Raised when a requested monitor does not exist."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    """Outcome classification of a single monitor check."""

    SUCCESS = "success"
    FAILURE = "failure"


# ---------------------------------------------------------------------------
# SQLAlchemy models
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for all BeaconOps ORM models."""


class Monitor(Base):
    """A monitored HTTP endpoint configuration."""

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    expected_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    latency_threshold_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    response_contains: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default=None
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    checks: Mapped[list["MonitorCheck"]] = relationship(
        "MonitorCheck",
        back_populates="monitor",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Monitor id={self.id} name={self.name!r} active={self.active}>"


class MonitorCheck(Base):
    """A persisted record of a single monitor check execution."""

    __tablename__ = "monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    diagnostic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    monitor: Mapped[Monitor] = relationship("Monitor", back_populates="checks")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<MonitorCheck id={self.id} monitor_id={self.monitor_id} "
            f"status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------


_ENGINE_CACHE: dict[str, Engine] = {}


def get_engine(settings: Optional[Settings] = None) -> Engine:
    """Return a cached SQLAlchemy engine for the configured database URL.

    Args:
        settings: Optional settings override. When omitted, the global
            application settings are used.

    Returns:
        A SQLAlchemy :class:`~sqlalchemy.engine.Engine` instance.
    """

    resolved = settings or get_settings()
    url = resolved.database_url
    engine = _ENGINE_CACHE.get(url)
    if engine is not None:
        return engine

    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    try:
        engine = create_engine(url, future=True, connect_args=connect_args)
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        logger.exception("Failed to create database engine for URL %s", url)
        raise RuntimeError("Unable to create database engine") from exc

    _ENGINE_CACHE[url] = engine
    return engine


def get_session_factory(settings: Optional[Settings] = None) -> sessionmaker[Session]:
    """Return a session factory bound to the configured engine."""

    engine = get_engine(settings)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(settings: Optional[Settings] = None) -> None:
    """Create all tables if they do not yet exist."""

    engine = get_engine(settings)
    try:
        Base.metadata.create_all(engine)
    except SQLAlchemyError:
        logger.exception("Failed to initialize database schema")
        raise


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Result of a single monitor check, as returned to callers."""

    monitor_id: int
    status: CheckStatus
    checked_at: datetime
    http_status: Optional[int]
    response_time_ms: Optional[float]
    error_message: Optional[str]
    diagnostic: Optional[str]


@dataclass(frozen=True)
class MonitorInput:
    """Validated input used to create or update a monitor."""

    name: str
    url: str
    method: str = "GET"
    interval_seconds: int = 60
    timeout_seconds: float = 10.0
    expected_status: int = 200
    latency_threshold_ms: Optional[float] = None
    response_contains: Optional[str] = None
    active: bool = True


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_MIN_INTERVAL_SECONDS = 5
_MAX_INTERVAL_SECONDS = 86_400
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 120.0


def _validate_monitor_input(data: MonitorInput, settings: Settings) -> None:
    """Validate a monitor configuration, raising on the first problem found."""

    if not data.name or not data.name.strip():
        raise MonitorValidationError("Monitor name must not be empty")

    method = data.method.upper()
    if method not in _ALLOWED_METHODS:
        raise MonitorValidationError(
            f"Unsupported HTTP method {data.method!r}; "
            f"allowed: {sorted(_ALLOWED_METHODS)}"
        )

    if not (_MIN_INTERVAL_SECONDS <= data.interval_seconds <= _MAX_INTERVAL_SECONDS):
        raise MonitorValidationError(
            f"interval_seconds must be between {_MIN_INTERVAL_SECONDS} and "
            f"{_MAX_INTERVAL_SECONDS}"
        )

    if not (_MIN_TIMEOUT_SECONDS <= data.timeout_seconds <= _MAX_TIMEOUT_SECONDS):
        raise MonitorValidationError(
            f"timeout_seconds must be between {_MIN_TIMEOUT_SECONDS} and "
            f"{_MAX_TIMEOUT_SECONDS}"
        )

    if not (100 <= data.expected_status <= 599):
        raise MonitorValidationError("expected_status must be a valid HTTP status code")

    if data.latency_threshold_ms is not None and data.latency_threshold_ms <= 0:
        raise MonitorValidationError("latency_threshold_ms must be positive when set")

    try:
        validate_monitor_url(data.url, settings=settings)
    except ValidationError as exc:
        raise MonitorValidationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Monitor service
# ---------------------------------------------------------------------------


class MonitorService:
    """Application service for managing monitors and executing checks.

    The service owns a SQLAlchemy session factory and an HTTP client. It is
    safe to instantiate once per process and share across the scheduler and
    the API layer.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        http_client: Optional[HttpClient] = None,
        session_factory: Optional[sessionmaker[Session]] = None,
    ) -> None:
        """Initialize the service.

        Args:
            settings: Optional settings override.
            http_client: Optional HTTP client override (useful for tests).
            session_factory: Optional session factory override.
        """

        self._settings: Settings = settings or get_settings()
        self._session_factory: sessionmaker[Session] = (
            session_factory or get_session_factory(self._settings)
        )
        self._http_client: HttpClient = http_client or HttpClient(self._settings)

    # -- CRUD ---------------------------------------------------------------

    def create_monitor(self, data: MonitorInput) -> Monitor:
        """Create and persist a new monitor.

        Raises:
            MonitorValidationError: If the configuration is invalid.
        """

        _validate_monitor_input(data, self._settings)

        monitor = Monitor(
            name=data.name.strip(),
            url=data.url,
            method=data.method.upper(),
            interval_seconds=data.interval_seconds,
            timeout_seconds=data.timeout_seconds,
            expected_status=data.expected_status,
            latency_threshold_ms=data.latency_threshold_ms,
            response_contains=data.response_contains,
            active=data.active,
        )

        with self._session_factory() as session:
            try:
                session.add(monitor)
                session.commit()
                session.refresh(monitor)
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to persist monitor %r", data.name)
                raise

        logger.info("Created monitor id=%s name=%r", monitor.id, monitor.name)
        return monitor

    def get_monitor(self, monitor_id: int) -> Monitor:
        """Return a monitor by id.

        Raises:
            MonitorNotFoundError: If no monitor with the given id exists.
        """

        with self._session_factory() as session:
            monitor = session.get(Monitor, monitor_id)
            if monitor is None:
                raise MonitorNotFoundError(f"Monitor {monitor_id} not found")
            return monitor

    def list_monitors(
        self,
        *,
        active: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Monitor]:
        """List monitors with optional filtering and pagination."""

        if limit <= 0 or limit > 1000:
            raise MonitorValidationError("limit must be between 1 and 1000")
        if offset < 0:
            raise MonitorValidationError("offset must be non-negative")

        stmt = select(Monitor).order_by(Monitor.id.asc()).limit(limit).offset(offset)
        if active is not None:
            stmt = stmt.where(Monitor.active.is_(active))

        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def list_active_monitors(self) -> Sequence[Monitor]:
        """Return all active monitors (used by the scheduler)."""

        stmt = select(Monitor).where(Monitor.active.is_(True)).order_by(Monitor.id.asc())
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def update_monitor(self, monitor_id: int, data: MonitorInput) -> Monitor:
        """Update an existing monitor.

        Raises:
            MonitorNotFoundError: If the monitor does not exist.
            MonitorValidationError: If the new configuration is invalid.
        """

        _validate_monitor_input(data, self._settings)

        with self._session_factory() as session:
            monitor = session.get(Monitor, monitor_id)
            if monitor is None:
                raise MonitorNotFoundError(f"Monitor {monitor_id} not found")

            monitor.name = data.name.strip()
            monitor.url = data.url
            monitor.method = data.method.upper()
            monitor.interval_seconds = data.interval_seconds
            monitor.timeout_seconds = data.timeout_seconds
            monitor.expected_status = data.expected_status
            monitor.latency_threshold_ms = data.latency_threshold_ms
            monitor.response_contains = data.response_contains
            monitor.active = data.active
            monitor.updated_at = datetime.now(timezone.utc)

            try:
                session.commit()
                session.refresh(monitor)
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to update monitor id=%s", monitor_id)
                raise

        logger.info("Updated monitor id=%s", monitor_id)
        return monitor

    def set_active(self, monitor_id: int, active: bool) -> Monitor:
        """Activate or deactivate a monitor."""

        with self._session_factory() as session:
            monitor = session.get(Monitor, monitor_id)
            if monitor is None:
                raise MonitorNotFoundError(f"Monitor {monitor_id} not found")
            monitor.active = active
            monitor.updated_at = datetime.now(timezone.utc)
            try:
                session.commit()
                session.refresh(monitor)
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to toggle monitor id=%s", monitor_id)
                raise

        logger.info("Set monitor id=%s active=%s", monitor_id, active)
        return monitor

    def delete_monitor(self, monitor_id: int) -> None:
        """Delete a monitor and its associated checks."""

        with self._session_factory() as session:
            monitor = session.get(Monitor, monitor_id)
            if monitor is None:
                raise MonitorNotFoundError(f"Monitor {monitor_id} not found")
            try:
                session.delete(monitor)
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to delete monitor id=%s", monitor_id)
                raise

        logger.info("Deleted monitor id=%s", monitor_id)

    # -- Check execution ----------------------------------------------------

    async def run_check(self, monitor_id: int) -> CheckResult:
        """Execute a single check for the given monitor and persist the result.

        The check is performed using the shared HTTP client, which enforces
        SSRF protections, timeouts, and response size limits. The result is
        always persisted, even when the request fails.

        Raises:
            MonitorNotFoundError: If the monitor does not exist.
        """

        monitor = self.get_monitor(monitor_id)
        checked_at = datetime.now(timezone.utc)

        try:
            http_result: HttpCheckResult = await self._http_client.check(
                url=monitor.url,
                method=monitor.method,
                timeout_seconds=monitor.timeout_seconds,
                expected_status=monitor.expected_status,
                response_contains=monitor.response_contains,
            )
        except ValueError as exc:
            logger.exception("Validation error while checking monitor id=%s", monitor_id)
            http_result = HttpCheckResult(
                outcome=CheckOutcome.VALIDATION_ERROR,
                status_code=None,
                response_time_ms=None,
                error_message=f"Validation error: {exc}",
                body_excerpt=None,
            )
        except Exception as exc:  # noqa: BLE001 - defensive boundary
            logger.exception("Unexpected error while checking monitor id=%s", monitor_id)
            http_result = HttpCheckResult(
                outcome=CheckOutcome.ERROR,
                status_code=None,
                response_time_ms=None,
                error_message=f"Unexpected error: {exc.__class__.__name__}",
                body_excerpt=None,
            )

        status = (
            CheckStatus.SUCCESS
            if http_result.outcome is CheckOutcome.SUCCESS
            else CheckStatus.FAILURE
        )

        diagnostic_parts: list[str] = [f"outcome={http_result.outcome.value}"]
        if monitor.latency_threshold_ms is not None and http_result.response_time_ms is not None:
            exceeded = http_result.response_time_ms > monitor.latency_threshold_ms
            diagnostic_parts.append(f"latency_threshold_exceeded={exceeded}")
        if http_result.body_excerpt:
            diagnostic_parts.append(f"body_excerpt={http_result.body_excerpt[:200]!r}")

        record = MonitorCheck(
            monitor_id=monitor.id,
            checked_at=checked_at,
            status=status.value,
            http_status=http_result.status_code,
            response_time_ms=http_result.response_time_ms,
            error_message=http_result.error_message,
            diagnostic="; ".join(diagnostic_parts),
        )

        with self._session_factory() as session:
            try:
                session.add(record)
                session.commit()
                session.refresh(record)
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to persist check for monitor id=%s", monitor_id)
                raise

        logger.info(
            "Check completed monitor_id=%s status=%s http_status=%s latency_ms=%s",
            monitor_id,
            status.value,
            http_result.status_code,
            http_result.response_time_ms,
        )

        return CheckResult(
            monitor_id=monitor.id,
            status=status,
            checked_at=checked_at,
            http_status=http_result.status_code,
            response_time_ms=http_result.response_time_ms,
            error_message=http_result.error_message,
            diagnostic=record.diagnostic,
        )

    # -- History ------------------------------------------------------------

    def list_checks(
        self,
        monitor_id: int,
        *,
        status: Optional[CheckStatus] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MonitorCheck]:
        """Return the check history for a monitor with filters and pagination."""

        if limit <= 0 or limit > 1000:
            raise MonitorValidationError("limit must be between 1 and 1000")
        if offset < 0:
            raise MonitorValidationError("offset must be non-negative")

        stmt = (
            select(MonitorCheck)
            .where(MonitorCheck.monitor_id == monitor_id)
            .order_by(MonitorCheck.checked_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(MonitorCheck.status == status.value)
        if since is not None:
            stmt = stmt.where(MonitorCheck.checked_at >= since)
        if until is not None:
            stmt = stmt.where(MonitorCheck.checked_at <= until)

        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def count_checks(self, monitor_id: int) -> int:
        """Return the total number of persisted checks for a monitor."""

        stmt = select(func.count(MonitorCheck.id)).where(
            MonitorCheck.monitor_id == monitor_id
        )
        with self._session_factory() as session:
            return int(session.scalar(stmt) or 0)

    def purge_checks_before(self, monitor_id: int, cutoff: datetime) -> int:
        """Delete checks older than ``cutoff`` for a monitor.

        Returns:
            The number of deleted rows.
        """

        stmt = delete(MonitorCheck).where(
            MonitorCheck.monitor_id == monitor_id,
            MonitorCheck.checked_at < cutoff,
        )
        with self._session_factory() as session:
            try:
                result = session.execute(stmt)
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.exception("Failed to purge checks for monitor id=%s", monitor_id)
                raise
            deleted = int(result.rowcount or 0)

        logger.info("Purged %s checks for monitor id=%s", deleted, monitor_id)
        return deleted

    # -- Lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Release resources held by the service (HTTP client)."""

        await self._http_client.aclose()

    def close(self) -> None:
        """Synchronously release resources held by the service."""

        self._http_client.close()


def iter_active_monitor_ids(service: MonitorService) -> Iterable[int]:
    """Yield the ids of all active monitors.

    Convenience helper used by the scheduler to enqueue checks without
    materializing full ORM objects.
    """

    for monitor in service.list_active_monitors():
        yield monitor.id
