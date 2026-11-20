"""FastAPI routes for BeaconOps.

This module defines the HTTP layer of BeaconOps. It exposes a REST API for
managing monitors, querying check history and incidents, computing operational
indicators, executing manual checks, and reporting application health.

Responsibilities
----------------
- Build the FastAPI application and wire its dependencies.
- Define request/response contracts using the Pydantic models in
  :mod:`beaconops.api.pydantic_models`.
- Protect administrative endpoints with an API key (``X-API-Key``).
- Translate domain errors into appropriate HTTP status codes.
- Manage the lifecycle of the scheduler and shared services.

The API layer depends on the core and services packages but is never imported
by them, so that monitoring operations remain independent of the HTTP routes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from beaconops import __version__
from beaconops.api.pydantic_models import (
    CheckListResponse,
    CheckResponse,
    HealthResponse,
    IncidentListResponse,
    IncidentResponse,
    MetricsResponse,
    MonitorCreateRequest,
    MonitorListResponse,
    MonitorResponse,
    MonitorUpdateRequest,
)
from beaconops.config.settings import Settings, get_settings
from beaconops.core.monitor import (
    CheckStatus,
    Monitor,
    MonitorCheck,
    MonitorNotFoundError,
    MonitorService,
    MonitorValidationError,
    init_db,
)
from beaconops.core.schedule import MonitorScheduler, SchedulerError
from beaconops.services.incident_identification import (
    Incident,
    IncidentIdentifier,
    IncidentStatus,
)
from beaconops.utils.logging import configure_logging, get_logger

__all__ = [
    "app",
    "create_app",
    "get_incident_identifier",
    "get_monitor_service",
    "get_scheduler",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Application state helpers
# ---------------------------------------------------------------------------


def get_monitor_service(request: Request) -> MonitorService:
    """Return the shared :class:`MonitorService` from application state."""

    service: MonitorService | None = getattr(request.app.state, "monitor_service", None)
    if service is None:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Monitor service is not initialized",
        )
    return service


def get_incident_identifier(request: Request) -> IncidentIdentifier:
    """Return the shared :class:`IncidentIdentifier` from application state."""

    identifier: IncidentIdentifier | None = getattr(request.app.state, "incident_identifier", None)
    if identifier is None:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident identifier is not initialized",
        )
    return identifier


def get_scheduler(request: Request) -> MonitorScheduler | None:
    """Return the shared :class:`MonitorScheduler`, if enabled."""

    return getattr(request.app.state, "scheduler", None)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def require_admin_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Enforce the administrative API key on protected endpoints.

    The expected key is read from ``BEACONOPS_ADMIN_API_KEY``. When no key is
    configured, administrative endpoints are inaccessible (fail closed) in
    production and open in development/test to ease local usage.

    Raises:
        HTTPException: If the key is missing or invalid.
    """

    settings: Settings = request.app.state.settings
    expected = settings.admin_api_key

    if not expected:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Administrative API key is not configured",
            )
        return

    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


AdminGuard = Depends(require_admin_api_key)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_monitor(monitor: Monitor) -> MonitorResponse:
    """Convert a :class:`Monitor` ORM object into a response model."""

    return MonitorResponse(
        id=monitor.id,
        name=monitor.name,
        url=monitor.url,
        method=monitor.method,
        interval_seconds=monitor.interval_seconds,
        timeout_seconds=monitor.timeout_seconds,
        expected_status=monitor.expected_status,
        latency_threshold_ms=monitor.latency_threshold_ms,
        response_contains=monitor.response_contains,
        active=monitor.active,
        created_at=monitor.created_at,
        updated_at=monitor.updated_at,
    )


def _serialize_check(check: MonitorCheck) -> CheckResponse:
    """Convert a :class:`MonitorCheck` ORM object into a response model."""

    return CheckResponse(
        id=check.id,
        monitor_id=check.monitor_id,
        checked_at=check.checked_at,
        status=CheckStatus(check.status),
        http_status=check.http_status,
        response_time_ms=check.response_time_ms,
        error_message=check.error_message,
        diagnostic=check.diagnostic,
    )


def _serialize_incident(incident: Incident) -> IncidentResponse:
    """Convert an :class:`Incident` ORM object into a response model."""

    return IncidentResponse(
        id=incident.id,
        monitor_id=incident.monitor_id,
        status=IncidentStatus(incident.status),
        opened_at=incident.opened_at,
        closed_at=incident.closed_at,
        trigger_check_id=incident.trigger_check_id,
        resolution_check_id=incident.resolution_check_id,
        reason=incident.reason,
        resolution_reason=incident.resolution_reason,
        duration_seconds=incident.duration_seconds,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the BeaconOps FastAPI application.

    Args:
        settings: Optional settings override. When omitted, the global
            application settings are used.

    Returns:
        A configured :class:`FastAPI` application instance.
    """

    resolved_settings: Settings = settings or get_settings()
    configure_logging(settings=resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Manage application startup and shutdown."""

        logger.info("Starting BeaconOps %s (env=%s)", __version__, resolved_settings.env)

        try:
            init_db(resolved_settings)
        except Exception:
            logger.exception("Failed to initialize database schema")
            raise

        monitor_service = MonitorService(settings=resolved_settings)
        incident_identifier = IncidentIdentifier(settings=resolved_settings)

        app.state.settings = resolved_settings
        app.state.monitor_service = monitor_service
        app.state.incident_identifier = incident_identifier
        app.state.scheduler = None

        if resolved_settings.scheduler_enabled:
            scheduler = MonitorScheduler(monitor_service, resolved_settings)
            try:
                scheduler.start()
                app.state.scheduler = scheduler
            except SchedulerError:
                logger.exception("Failed to start scheduler; continuing without it")

        try:
            yield
        finally:
            scheduler = getattr(app.state, "scheduler", None)
            if scheduler is not None:
                try:
                    scheduler.shutdown(wait=False)
                except Exception:
                    logger.exception("Error while shutting down scheduler")

            try:
                await monitor_service.aclose()
            except Exception:
                logger.exception("Error while closing monitor service")

            logger.info("BeaconOps stopped")

    app = FastAPI(
        title="BeaconOps",
        version=__version__,
        description=("Self-managed API availability and performance monitoring platform."),
        lifespan=lifespan,
    )

    # -- Exception handlers -------------------------------------------------

    @app.exception_handler(MonitorNotFoundError)
    async def _handle_not_found(_request: Request, exc: MonitorNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc)},
        )

    @app.exception_handler(MonitorValidationError)
    async def _handle_validation(_request: Request, exc: MonitorValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    # -- Health -------------------------------------------------------------

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
        summary="Application health check",
    )
    async def health(request: Request) -> HealthResponse:
        """Return the application health status."""

        current_settings: Settings = request.app.state.settings
        scheduler = get_scheduler(request)
        return HealthResponse(
            status="ok",
            version=__version__,
            environment=current_settings.env,
            scheduler_running=bool(scheduler is not None and scheduler.running),
            timestamp=datetime.now(UTC),
        )

    # -- Monitors -----------------------------------------------------------

    @app.post(
        "/monitors",
        response_model=MonitorResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["monitors"],
        summary="Create a monitor",
        dependencies=[AdminGuard],
    )
    async def create_monitor(
        payload: MonitorCreateRequest,
        request: Request,
    ) -> MonitorResponse:
        """Create a new monitor."""

        service = get_monitor_service(request)
        monitor = service.create_monitor(payload.to_input())

        scheduler = get_scheduler(request)
        if scheduler is not None and monitor.active:
            try:
                scheduler.schedule_monitor(monitor.id, monitor.interval_seconds)
            except SchedulerError:
                logger.exception("Failed to schedule newly created monitor id=%s", monitor.id)

        return _serialize_monitor(monitor)

    @app.get(
        "/monitors",
        response_model=MonitorListResponse,
        tags=["monitors"],
        summary="List monitors",
        dependencies=[AdminGuard],
    )
    async def list_monitors(
        request: Request,
        active: Annotated[bool | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MonitorListResponse:
        """List monitors with optional filtering and pagination."""

        service = get_monitor_service(request)
        monitors = service.list_monitors(active=active, limit=limit, offset=offset)
        return MonitorListResponse(
            items=[_serialize_monitor(m) for m in monitors],
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/monitors/{monitor_id}",
        response_model=MonitorResponse,
        tags=["monitors"],
        summary="Get a monitor",
        dependencies=[AdminGuard],
    )
    async def get_monitor(monitor_id: int, request: Request) -> MonitorResponse:
        """Return a single monitor by id."""

        service = get_monitor_service(request)
        return _serialize_monitor(service.get_monitor(monitor_id))

    @app.patch(
        "/monitors/{monitor_id}",
        response_model=MonitorResponse,
        tags=["monitors"],
        summary="Update a monitor",
        dependencies=[AdminGuard],
    )
    async def update_monitor(
        monitor_id: int,
        payload: MonitorUpdateRequest,
        request: Request,
    ) -> MonitorResponse:
        """Update an existing monitor."""

        service = get_monitor_service(request)
        existing = service.get_monitor(monitor_id)
        merged = payload.merge(existing)
        monitor = service.update_monitor(monitor_id, merged)

        scheduler = get_scheduler(request)
        if scheduler is not None:
            if monitor.active:
                try:
                    scheduler.schedule_monitor(monitor.id, monitor.interval_seconds)
                except SchedulerError:
                    logger.exception("Failed to reschedule monitor id=%s", monitor.id)
            else:
                scheduler.unschedule_monitor(monitor.id)

        return _serialize_monitor(monitor)

    @app.delete(
        "/monitors/{monitor_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["monitors"],
        summary="Delete a monitor",
        dependencies=[AdminGuard],
    )
    async def delete_monitor(monitor_id: int, request: Request) -> Response:
        """Delete a monitor and its associated checks."""

        service = get_monitor_service(request)
        scheduler = get_scheduler(request)
        if scheduler is not None:
            scheduler.unschedule_monitor(monitor_id)
        service.delete_monitor(monitor_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- Manual check -------------------------------------------------------

    @app.post(
        "/monitors/{monitor_id}/check",
        response_model=CheckResponse,
        tags=["monitors"],
        summary="Run a manual check",
        dependencies=[AdminGuard],
    )
    async def run_manual_check(monitor_id: int, request: Request) -> CheckResponse:
        """Execute a single check for a monitor and return the result."""

        service = get_monitor_service(request)
        identifier = get_incident_identifier(request)

        monitor = service.get_monitor(monitor_id)
        result = await service.run_check(monitor_id)

        try:
            await identifier.evaluate(monitor, result)
        except Exception:
            logger.exception("Failed to evaluate incident state for monitor id=%s", monitor_id)

        checks = service.list_checks(monitor_id, limit=1, offset=0)
        if not checks:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Check result was not persisted",
            )
        return _serialize_check(checks[0])

    # -- Check history ------------------------------------------------------

    @app.get(
        "/monitors/{monitor_id}/checks",
        response_model=CheckListResponse,
        tags=["checks"],
        summary="List check history",
        dependencies=[AdminGuard],
    )
    async def list_checks(
        monitor_id: int,
        request: Request,
        check_status: Annotated[CheckStatus | None, Query(alias="status")] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> CheckListResponse:
        """Return the check history for a monitor."""

        service = get_monitor_service(request)
        service.get_monitor(monitor_id)
        checks = service.list_checks(
            monitor_id,
            status=check_status,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        total = service.count_checks(monitor_id)
        return CheckListResponse(
            items=[_serialize_check(c) for c in checks],
            total=total,
            limit=limit,
            offset=offset,
        )

    # -- Incidents ----------------------------------------------------------

    @app.get(
        "/incidents",
        response_model=IncidentListResponse,
        tags=["incidents"],
        summary="List incidents",
        dependencies=[AdminGuard],
    )
    async def list_incidents(
        request: Request,
        monitor_id: Annotated[int | None, Query(ge=1)] = None,
        incident_status: Annotated[IncidentStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> IncidentListResponse:
        """List incidents with optional filters and pagination."""

        identifier = get_incident_identifier(request)
        incidents = identifier.list_incidents(
            monitor_id=monitor_id,
            status=incident_status,
            limit=limit,
            offset=offset,
        )
        return IncidentListResponse(
            items=[_serialize_incident(i) for i in incidents],
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/incidents/{incident_id}",
        response_model=IncidentResponse,
        tags=["incidents"],
        summary="Get an incident",
        dependencies=[AdminGuard],
    )
    async def get_incident(incident_id: int, request: Request) -> IncidentResponse:
        """Return a single incident by id."""

        identifier = get_incident_identifier(request)
        return _serialize_incident(identifier.get_incident(incident_id))

    # -- Metrics ------------------------------------------------------------

    @app.get(
        "/monitors/{monitor_id}/metrics",
        response_model=MetricsResponse,
        tags=["metrics"],
        summary="Operational indicators for a monitor",
        dependencies=[AdminGuard],
    )
    async def get_metrics(
        monitor_id: int,
        request: Request,
        period: Annotated[str, Query(pattern=r"^[0-9]+[hd]$")] = "24h",
    ) -> MetricsResponse:
        """Compute operational indicators for a monitor over a period.

        The ``period`` query parameter accepts values such as ``24h`` or
        ``7d``. All indicators are computed from persisted check records.
        """

        service = get_monitor_service(request)
        identifier = get_incident_identifier(request)
        service.get_monitor(monitor_id)

        since = _parse_period(period)
        checks = service.list_checks(monitor_id, since=since, limit=1000, offset=0)

        total = len(checks)
        successes = [c for c in checks if c.status == CheckStatus.SUCCESS.value]
        failures = total - len(successes)

        latencies = [c.response_time_ms for c in successes if c.response_time_ms is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        max_latency = max(latencies) if latencies else None

        uptime = (len(successes) / total) if total else None
        downtime = (failures / total) if total else None

        open_incidents = identifier.count_open_incidents(monitor_id)

        return MetricsResponse(
            monitor_id=monitor_id,
            period=period,
            since=since,
            total_checks=total,
            successful_checks=len(successes),
            failed_checks=failures,
            uptime_ratio=uptime,
            downtime_ratio=downtime,
            average_latency_ms=avg_latency,
            max_latency_ms=max_latency,
            open_incidents=open_incidents,
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_period(period: str) -> datetime:
    """Parse a period string such as ``24h`` or ``7d`` into a start datetime.

    Args:
        period: A string of the form ``<number><h|d>``.

    Returns:
        The UTC datetime corresponding to ``now - period``.

    Raises:
        HTTPException: If the period cannot be parsed.
    """

    try:
        amount = int(period[:-1])
        unit = period[-1]
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid period {period!r}",
        ) from exc

    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period amount must be positive",
        )

    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    else:  # pragma: no cover - guarded by the query pattern
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported period unit {unit!r}",
        )

    return datetime.now(UTC) - delta


# ---------------------------------------------------------------------------
# Module-level application instance
# ---------------------------------------------------------------------------

app: FastAPI = create_app()
