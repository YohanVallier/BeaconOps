"""Incident identification for BeaconOps.

This module evaluates the outcome of monitor checks and maintains the incident
state of each monitor. It implements the transition rules that decide when a
service becomes unavailable (opening an incident) and when it recovers
(closing the incident), based on configurable consecutive-failure and
consecutive-recovery thresholds.

Responsibilities
----------------
- Persist incident records and the current availability state of monitors.
- Evaluate each check result against the configured thresholds.
- Open an incident on the available -> unavailable transition.
- Close an incident on the unavailable -> available transition.
- Emit notification events for incident lifecycle transitions and latency
  threshold violations, delegating delivery to
  :class:`beaconops.core.notifications.Notifier`.
- Avoid opening a new incident for every failed check of an already
  unavailable service.

The module is decoupled from the HTTP API layer and from the scheduler. It is
invoked by the monitor service after each check is persisted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, sessionmaker

from beaconops.config.settings import Settings, get_settings
from beaconops.core.monitor import (
    Base,
    CheckResult,
    CheckStatus,
    Monitor,
    MonitorNotFoundError,
)
from beaconops.core.notifications import (
    NotificationEvent,
    NotificationEventType,
    Notifier,
)
from beaconops.utils.logging import get_logger

__all__ = [
    "Incident",
    "IncidentEvaluation",
    "IncidentIdentifier",
    "IncidentStatus",
    "MonitorState",
    "MonitorStateStatus",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class IncidentStatus(StrEnum):
    """Lifecycle status of an incident."""

    OPEN = "open"
    CLOSED = "closed"


class MonitorStateStatus(StrEnum):
    """Current availability state of a monitor."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# SQLAlchemy models
# ---------------------------------------------------------------------------


class MonitorState(Base):
    """Persisted availability state and consecutive counters for a monitor.

    A single row exists per monitor. The row tracks the current availability
    status, the number of consecutive failures and successes observed, and the
    identifier of the currently open incident (if any).
    """

    __tablename__ = "monitor_states"

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MonitorStateStatus.AVAILABLE.value
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    monitor: Mapped[Monitor] = relationship("Monitor")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<MonitorState monitor_id={self.monitor_id} status={self.status} "
            f"failures={self.consecutive_failures} "
            f"successes={self.consecutive_successes}>"
        )


class Incident(Base):
    """A persisted incident representing a period of unavailability."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IncidentStatus.OPEN.value, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    trigger_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    resolution_check_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    notified_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    monitor: Mapped[Monitor] = relationship("Monitor")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Incident id={self.id} monitor_id={self.monitor_id} status={self.status}>"

    @property
    def duration_seconds(self) -> float | None:
        """Return the incident duration in seconds, or ``None`` if still open."""

        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncidentEvaluation:
    """Outcome of evaluating a single check against incident rules.

    Attributes:
        monitor_id: Identifier of the evaluated monitor.
        previous_status: The monitor state before evaluation.
        current_status: The monitor state after evaluation.
        incident_opened: Whether a new incident was opened.
        incident_closed: Whether an open incident was closed.
        incident_id: The identifier of the incident affected by this
            evaluation, when applicable.
        latency_exceeded: Whether the check latency exceeded the monitor's
            configured threshold.
    """

    monitor_id: int
    previous_status: MonitorStateStatus
    current_status: MonitorStateStatus
    incident_opened: bool
    incident_closed: bool
    incident_id: int | None
    latency_exceeded: bool


# ---------------------------------------------------------------------------
# Incident identifier
# ---------------------------------------------------------------------------


class IncidentIdentifier:
    """Evaluate check results and maintain incident state.

    The identifier owns a SQLAlchemy session factory and a notifier. It is
    safe to instantiate once per process and share across the monitor service
    and the API layer.

    Transition rules
    ----------------
    - A monitor starts in the ``available`` state.
    - Each failed check increments ``consecutive_failures`` and resets
      ``consecutive_successes``.
    - When ``consecutive_failures`` reaches ``failure_threshold`` while the
      monitor is ``available``, an incident is opened and the monitor becomes
      ``unavailable``.
    - Each successful check increments ``consecutive_successes`` and resets
      ``consecutive_failures``.
    - When ``consecutive_successes`` reaches ``recovery_threshold`` while the
      monitor is ``unavailable``, the open incident is closed and the monitor
      becomes ``available``.
    - No new incident is opened while the monitor is already ``unavailable``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        """Initialize the identifier.

        Args:
            settings: Optional settings override. When omitted, the global
                application settings are used.
            session_factory: Optional session factory override. When omitted,
                a factory bound to the configured engine is created.
            notifier: Optional notifier override. When omitted, a default
                notifier is created from the settings.
        """

        self._settings: Settings = settings or get_settings()
        if session_factory is None:
            from beaconops.core.monitor import get_session_factory

            session_factory = get_session_factory(self._settings)
        self._session_factory: sessionmaker[Session] = session_factory
        self._notifier: Notifier = notifier or Notifier(self._settings)

    # -- Public API ---------------------------------------------------------

    async def evaluate(
        self,
        monitor: Monitor,
        check: CheckResult,
        *,
        check_id: int | None = None,
    ) -> IncidentEvaluation:
        """Evaluate a check result and update incident state accordingly.

        Args:
            monitor: The monitor that produced the check.
            check: The result of the check.
            check_id: Optional identifier of the persisted check record, used
                to link incidents to the triggering check.

        Returns:
            An :class:`IncidentEvaluation` describing the transitions that
            occurred.

        Raises:
            MonitorNotFoundError: If the monitor no longer exists.
        """

        now = datetime.now(UTC)
        latency_exceeded = self._is_latency_exceeded(monitor, check)

        with self._session_factory() as session:
            state = self._get_or_create_state(session, monitor.id)
            previous_status = MonitorStateStatus(state.status)

            incident_opened = False
            incident_closed = False
            incident_id: int | None = state.open_incident_id

            if check.status is CheckStatus.SUCCESS:
                state.consecutive_successes += 1
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
                state.consecutive_successes = 0

            if (
                previous_status is MonitorStateStatus.AVAILABLE
                and state.consecutive_failures >= self._settings.failure_threshold
            ):
                incident = self._open_incident(
                    session,
                    monitor=monitor,
                    check=check,
                    check_id=check_id,
                    opened_at=now,
                )
                state.status = MonitorStateStatus.UNAVAILABLE.value
                state.open_incident_id = incident.id
                incident_id = incident.id
                incident_opened = True
                logger.info(
                    "Opened incident id=%s for monitor id=%s after %s failures",
                    incident.id,
                    monitor.id,
                    state.consecutive_failures,
                )

            elif (
                previous_status is MonitorStateStatus.UNAVAILABLE
                and state.consecutive_successes >= self._settings.recovery_threshold
            ):
                closed = self._close_incident(
                    session,
                    monitor_id=monitor.id,
                    incident_id=state.open_incident_id,
                    check_id=check_id,
                    closed_at=now,
                )
                state.status = MonitorStateStatus.AVAILABLE.value
                state.open_incident_id = None
                incident_id = closed.id if closed is not None else incident_id
                incident_closed = closed is not None
                if incident_closed:
                    logger.info(
                        "Closed incident id=%s for monitor id=%s after %s successes",
                        incident_id,
                        monitor.id,
                        state.consecutive_successes,
                    )

            state.updated_at = now

            try:
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.exception(
                    "Failed to persist incident state for monitor id=%s",
                    monitor.id,
                )
                raise

            current_status = MonitorStateStatus(state.status)

        evaluation = IncidentEvaluation(
            monitor_id=monitor.id,
            previous_status=previous_status,
            current_status=current_status,
            incident_opened=incident_opened,
            incident_closed=incident_closed,
            incident_id=incident_id,
            latency_exceeded=latency_exceeded,
        )

        await self._dispatch_notifications(monitor, evaluation, check)
        return evaluation

    def get_state(self, monitor_id: int) -> MonitorState | None:
        """Return the persisted state for a monitor, if any."""

        with self._session_factory() as session:
            return session.get(MonitorState, monitor_id)

    def list_incidents(
        self,
        *,
        monitor_id: int | None = None,
        status: IncidentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Incident]:
        """List incidents with optional filters and pagination."""

        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        stmt = select(Incident).order_by(Incident.opened_at.desc()).limit(limit).offset(offset)
        if monitor_id is not None:
            stmt = stmt.where(Incident.monitor_id == monitor_id)
        if status is not None:
            stmt = stmt.where(Incident.status == status.value)

        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def get_incident(self, incident_id: int) -> Incident:
        """Return an incident by id.

        Raises:
            MonitorNotFoundError: If the incident does not exist.
        """

        with self._session_factory() as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise MonitorNotFoundError(f"Incident {incident_id} not found")
            return incident

    def count_open_incidents(self, monitor_id: int | None = None) -> int:
        """Return the number of currently open incidents."""

        from sqlalchemy import func

        stmt = select(func.count(Incident.id)).where(Incident.status == IncidentStatus.OPEN.value)
        if monitor_id is not None:
            stmt = stmt.where(Incident.monitor_id == monitor_id)

        with self._session_factory() as session:
            return int(session.scalar(stmt) or 0)

    # -- Internal helpers ---------------------------------------------------

    def _get_or_create_state(self, session: Session, monitor_id: int) -> MonitorState:
        """Return the state row for a monitor, creating it if necessary."""

        state = session.get(MonitorState, monitor_id)
        if state is not None:
            return state

        state = MonitorState(
            monitor_id=monitor_id,
            status=MonitorStateStatus.AVAILABLE.value,
            consecutive_failures=0,
            consecutive_successes=0,
            open_incident_id=None,
        )
        session.add(state)
        session.flush()
        return state

    def _open_incident(
        self,
        session: Session,
        *,
        monitor: Monitor,
        check: CheckResult,
        check_id: int | None,
        opened_at: datetime,
    ) -> Incident:
        """Create and persist a new open incident."""

        reason = check.error_message or "Consecutive check failures exceeded threshold"
        incident = Incident(
            monitor_id=monitor.id,
            status=IncidentStatus.OPEN.value,
            opened_at=opened_at,
            closed_at=None,
            trigger_check_id=check_id,
            reason=reason,
        )
        session.add(incident)
        session.flush()
        return incident

    def _close_incident(
        self,
        session: Session,
        *,
        monitor_id: int,
        incident_id: int | None,
        check_id: int | None,
        closed_at: datetime,
    ) -> Incident | None:
        """Close the currently open incident for a monitor, if any."""

        incident: Incident | None = None
        if incident_id is not None:
            incident = session.get(Incident, incident_id)

        if incident is None:
            stmt = (
                select(Incident)
                .where(
                    Incident.monitor_id == monitor_id,
                    Incident.status == IncidentStatus.OPEN.value,
                )
                .order_by(Incident.opened_at.desc())
                .limit(1)
            )
            incident = session.scalars(stmt).first()

        if incident is None:
            logger.warning("No open incident found to close for monitor id=%s", monitor_id)
            return None

        incident.status = IncidentStatus.CLOSED.value
        incident.closed_at = closed_at
        incident.resolution_check_id = check_id
        incident.resolution_reason = "Consecutive successful checks reached recovery threshold"
        session.flush()
        return incident

    @staticmethod
    def _is_latency_exceeded(monitor: Monitor, check: CheckResult) -> bool:
        """Return whether the check latency exceeded the monitor threshold."""

        if monitor.latency_threshold_ms is None:
            return False
        if check.response_time_ms is None:
            return False
        return check.response_time_ms > monitor.latency_threshold_ms

    async def _dispatch_notifications(
        self,
        monitor: Monitor,
        evaluation: IncidentEvaluation,
        check: CheckResult,
    ) -> None:
        """Emit notification events for the evaluation outcome.

        Notification failures are logged and swallowed so that a failing
        webhook cannot disrupt monitoring.
        """

        now = datetime.now(UTC)

        if evaluation.incident_opened:
            event = NotificationEvent(
                event_type=NotificationEventType.SERVICE_UNAVAILABLE,
                monitor_id=monitor.id,
                monitor_name=monitor.name,
                occurred_at=now,
                message=f"Service {monitor.name!r} is unavailable",
                details={
                    "incident_id": evaluation.incident_id,
                    "http_status": check.http_status,
                    "error_message": check.error_message,
                },
            )
            await self._safe_notify(event)

        if evaluation.incident_closed:
            event = NotificationEvent(
                event_type=NotificationEventType.SERVICE_RECOVERED,
                monitor_id=monitor.id,
                monitor_name=monitor.name,
                occurred_at=now,
                message=f"Service {monitor.name!r} has recovered",
                details={
                    "incident_id": evaluation.incident_id,
                    "http_status": check.http_status,
                    "response_time_ms": check.response_time_ms,
                },
            )
            await self._safe_notify(event)

        if evaluation.latency_exceeded:
            event = NotificationEvent(
                event_type=NotificationEventType.LATENCY_EXCEEDED,
                monitor_id=monitor.id,
                monitor_name=monitor.name,
                occurred_at=now,
                message=(
                    f"Service {monitor.name!r} exceeded latency threshold "
                    f"of {monitor.latency_threshold_ms}ms"
                ),
                details={
                    "response_time_ms": check.response_time_ms,
                    "latency_threshold_ms": monitor.latency_threshold_ms,
                },
            )
            await self._safe_notify(event)

    async def _safe_notify(self, event: NotificationEvent) -> None:
        """Deliver a notification, swallowing and logging any failure."""

        try:
            await self._notifier.notify(event)
        except Exception:
            logger.exception(
                "Failed to dispatch notification %s for monitor id=%s",
                event.event_type.value,
                event.monitor_id,
            )
