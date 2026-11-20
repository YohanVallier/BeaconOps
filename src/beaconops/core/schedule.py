"""Periodic scheduling of monitor checks for BeaconOps.

This module wraps APScheduler to execute monitor checks at configurable
intervals. It is intentionally decoupled from the HTTP API layer: the
scheduler only depends on :class:`beaconops.core.monitor.MonitorService` and
the application settings.

Responsibilities
----------------
- Register one periodic job per active monitor.
- Execute checks with bounded concurrency so that a slow endpoint does not
  block the processing of other monitors.
- Provide safe start/stop lifecycle management.
- Keep the scheduler resilient to individual job failures.

The scheduler is designed to run inside a single process. Distributed
execution is out of scope for the MVP.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from beaconops.config.settings import Settings, get_settings
from beaconops.core.monitor import (
    MonitorNotFoundError,
    MonitorService,
    MonitorValidationError,
)
from beaconops.utils.logging import get_logger

__all__ = [
    "MonitorScheduler",
    "SchedulerError",
]

logger: logging.Logger = get_logger(__name__)


class SchedulerError(RuntimeError):
    """Raised when the scheduler cannot be started or configured."""


class MonitorScheduler:
    """Manage periodic execution of monitor checks via APScheduler.

    The scheduler registers one interval job per active monitor. Each job
    invokes :meth:`MonitorService.run_check` and is guarded by a semaphore so
    that at most ``max_concurrent_checks`` checks run simultaneously.

    The class is safe to instantiate once per process. It must be started
    explicitly via :meth:`start` and stopped via :meth:`shutdown`.
    """

    def __init__(
        self,
        service: MonitorService,
        settings: Settings | None = None,
    ) -> None:
        """Initialize the scheduler.

        Args:
            service: The monitor service used to execute checks.
            settings: Optional settings override. When omitted, the global
                application settings are used.
        """

        self._service: MonitorService = service
        self._settings: Settings = settings or get_settings()
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            timezone=UTC,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 30,
            },
        )
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(
            max(1, self._settings.max_concurrent_checks)
        )
        self._started: bool = False

    # -- Lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Return whether the underlying scheduler is running."""

        return self._started and self._scheduler.running

    def start(self) -> None:
        """Start the scheduler and register jobs for all active monitors.

        Raises:
            SchedulerError: If the scheduler is already running or fails to
                start.
        """

        if self._started:
            logger.warning("Scheduler already started; ignoring start() call")
            return

        try:
            self._scheduler.start()
        except Exception as exc:
            logger.exception("Failed to start scheduler")
            raise SchedulerError("Unable to start scheduler") from exc

        self._started = True
        logger.info("Scheduler started")

        try:
            self.sync_jobs()
        except Exception:
            logger.exception("Failed to register initial monitor jobs")

    def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler.

        Args:
            wait: When ``True``, wait for running jobs to complete.
        """

        if not self._started:
            return

        try:
            self._scheduler.shutdown(wait=wait)
        except Exception:
            logger.exception("Error while shutting down scheduler")
        finally:
            self._started = False
            logger.info("Scheduler stopped")

    # -- Job management -----------------------------------------------------

    @staticmethod
    def _job_id(monitor_id: int) -> str:
        """Return the deterministic APScheduler job id for a monitor."""

        return f"monitor:{monitor_id}"

    def sync_jobs(self) -> None:
        """Synchronize scheduled jobs with the current set of active monitors.

        Jobs for monitors that are no longer active are removed, and jobs for
        active monitors are added or updated to reflect their configured
        interval.
        """

        if not self._started:
            logger.debug("sync_jobs called before scheduler start; skipping")
            return

        try:
            active_monitors = self._service.list_active_monitors()
        except Exception:
            logger.exception("Failed to load active monitors for scheduling")
            return

        desired: dict[str, int] = {}
        for monitor in active_monitors:
            desired[self._job_id(monitor.id)] = monitor.interval_seconds

        existing_ids = {job.id for job in self._scheduler.get_jobs()}

        for job_id in existing_ids - desired.keys():
            try:
                self._scheduler.remove_job(job_id)
                logger.info("Removed scheduled job %s", job_id)
            except Exception:
                logger.exception("Failed to remove job %s", job_id)

        for job_id, interval_seconds in desired.items():
            monitor_id = int(job_id.split(":", 1)[1])
            try:
                self._scheduler.add_job(
                    self._run_check_job,
                    trigger=IntervalTrigger(seconds=interval_seconds),
                    id=job_id,
                    name=f"Check monitor {monitor_id}",
                    args=[monitor_id],
                    replace_existing=True,
                )
                logger.debug("Scheduled job %s every %ss", job_id, interval_seconds)
            except Exception:
                logger.exception("Failed to schedule job %s", job_id)

    def schedule_monitor(self, monitor_id: int, interval_seconds: int) -> None:
        """Register or update the periodic job for a single monitor.

        Args:
            monitor_id: Identifier of the monitor to schedule.
            interval_seconds: Interval between checks, in seconds.

        Raises:
            MonitorValidationError: If ``interval_seconds`` is not positive.
        """

        if interval_seconds <= 0:
            raise MonitorValidationError("interval_seconds must be positive")

        if not self._started:
            logger.debug("schedule_monitor called before start; job will be added on sync")
            return

        job_id = self._job_id(monitor_id)
        try:
            self._scheduler.add_job(
                self._run_check_job,
                trigger=IntervalTrigger(seconds=interval_seconds),
                id=job_id,
                name=f"Check monitor {monitor_id}",
                args=[monitor_id],
                replace_existing=True,
            )
        except Exception as exc:
            logger.exception("Failed to schedule monitor id=%s", monitor_id)
            raise SchedulerError(f"Unable to schedule monitor {monitor_id}") from exc

        logger.info("Scheduled monitor id=%s every %ss", monitor_id, interval_seconds)

    def unschedule_monitor(self, monitor_id: int) -> None:
        """Remove the periodic job for a monitor, if present."""

        job_id = self._job_id(monitor_id)
        try:
            self._scheduler.remove_job(job_id)
            logger.info("Unscheduled monitor id=%s", monitor_id)
        except Exception:
            logger.debug("No scheduled job to remove for monitor id=%s", monitor_id)

    def list_jobs(self) -> list[tuple[str, datetime | None]]:
        """Return scheduled jobs as ``(job_id, next_run_time)`` tuples."""

        return [(job.id, getattr(job, "next_run_time", None)) for job in self._scheduler.get_jobs()]

    # -- Execution ----------------------------------------------------------

    async def _run_check_job(self, monitor_id: int) -> None:
        """Execute a single scheduled check for a monitor.

        The check is guarded by a semaphore to bound concurrency. Failures are
        logged and swallowed so that one failing monitor does not disrupt the
        scheduler.
        """

        async with self._semaphore:
            try:
                await self._service.run_check(monitor_id)
            except MonitorNotFoundError:
                logger.warning(
                    "Scheduled check skipped: monitor id=%s no longer exists",
                    monitor_id,
                )
                self.unschedule_monitor(monitor_id)
            except MonitorValidationError:
                logger.exception(
                    "Scheduled check failed validation for monitor id=%s",
                    monitor_id,
                )
            except asyncio.CancelledError:
                logger.info("Scheduled check cancelled for monitor id=%s", monitor_id)
                raise
            except Exception:
                logger.exception(
                    "Unexpected error during scheduled check for monitor id=%s",
                    monitor_id,
                )

    async def run_once(self, monitor_id: int) -> None:
        """Execute a check immediately, respecting the concurrency limit.

        This is a convenience entry point for manual checks triggered outside
        the scheduler while still honoring the configured concurrency bound.
        """

        await self._run_check_job(monitor_id)
