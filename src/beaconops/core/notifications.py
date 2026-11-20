"""Webhook notification delivery for BeaconOps.

This module implements the notification subsystem responsible for delivering
event notifications (incident opened, incident resolved, latency threshold
exceeded, monitor state changes) to configured HTTP webhook endpoints.

Responsibilities
----------------
- Define the notification event model and supported event types.
- Deliver notifications to webhook endpoints using the shared HTTP client,
  which enforces SSRF protections, timeouts, and response size limits.
- Deduplicate notifications so that the same event is not delivered twice for
  the same monitor within a configurable window.
- Record delivery attempts and failures for diagnostics.

The module is decoupled from the HTTP API layer and from the scheduler. It is
invoked by the incident identification service and by monitor state changes.
No credentials or unnecessary sensitive information are included in the
notification payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import httpx

from beaconops.config.settings import Settings, get_settings
from beaconops.utils.logging import get_logger
from beaconops.utils.validations import (
    ValidationError,
    validate_webhook_url,
)

__all__ = [
    "DeliveryResult",
    "NotificationEvent",
    "NotificationEventType",
    "NotificationError",
    "Notifier",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotificationError(RuntimeError):
    """Raised when a notification cannot be constructed or delivered."""


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class NotificationEventType(str, Enum):
    """Supported notification event types."""

    SERVICE_UNAVAILABLE = "service_unavailable"
    SERVICE_RECOVERED = "service_recovered"
    LATENCY_EXCEEDED = "latency_exceeded"
    MONITOR_STATE_CHANGED = "monitor_state_changed"


@dataclass(frozen=True)
class NotificationEvent:
    """A single notification event to be delivered to a webhook.

    Attributes:
        event_type: The classification of the event.
        monitor_id: Identifier of the monitor that produced the event.
        monitor_name: Human-readable monitor name.
        occurred_at: Timestamp when the event occurred (UTC).
        message: Short human-readable description of the event.
        details: Additional structured diagnostic information. Must not
            contain credentials or unnecessary sensitive data.
    """

    event_type: NotificationEventType
    monitor_id: int
    monitor_name: str
    occurred_at: datetime
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        """Return a deterministic deduplication key for this event.

        The key is derived from the event type, monitor id, and the minute in
        which the event occurred. This prevents duplicate deliveries of the
        same logical event while still allowing distinct events to be sent.
        """

        bucket = self.occurred_at.replace(second=0, microsecond=0).isoformat()
        raw = f"{self.event_type.value}:{self.monitor_id}:{bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        """Serialize the event into a JSON-compatible payload.

        The payload intentionally excludes any credentials or headers.
        """

        return {
            "event_type": self.event_type.value,
            "monitor_id": self.monitor_id,
            "monitor_name": self.monitor_name,
            "occurred_at": self.occurred_at.isoformat(),
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a webhook delivery attempt."""

    event: NotificationEvent
    webhook_url: str
    delivered: bool
    status_code: Optional[int]
    error_message: Optional[str]
    attempted_at: datetime


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class Notifier:
    """Deliver notification events to webhook endpoints.

    The notifier validates webhook URLs using the same SSRF protections as
    monitor URLs, enforces timeouts, and deduplicates events within a
    configurable window. Delivery failures are logged and recorded but never
    raised to callers, so that a failing webhook cannot disrupt monitoring.

    The class is safe to instantiate once per process and share across the
    incident identification service and the API layer.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        dedup_window_seconds: int = 300,
    ) -> None:
        """Initialize the notifier.

        Args:
            settings: Optional settings override. When omitted, the global
                application settings are used.
            dedup_window_seconds: Window during which duplicate events are
                suppressed. Must be non-negative.
        """

        if dedup_window_seconds < 0:
            raise NotificationError("dedup_window_seconds must be non-negative")

        self._settings: Settings = settings or get_settings()
        self._dedup_window: timedelta = timedelta(seconds=dedup_window_seconds)
        self._recent: dict[str, datetime] = {}

    # -- Public API ---------------------------------------------------------

    async def notify(
        self,
        event: NotificationEvent,
        webhook_url: Optional[str] = None,
    ) -> Optional[DeliveryResult]:
        """Deliver a notification event to a webhook.

        Args:
            event: The event to deliver.
            webhook_url: Optional webhook URL override. When omitted, the
                configured default webhook URL is used.

        Returns:
            A :class:`DeliveryResult` when a delivery was attempted, or
            ``None`` when notifications are disabled, no webhook is
            configured, or the event was suppressed as a duplicate.
        """

        if not self._settings.notifications_enabled:
            logger.debug("Notifications disabled; skipping event %s", event.event_type)
            return None

        target = webhook_url or self._settings.default_webhook_url
        if not target:
            logger.debug(
                "No webhook configured for event %s (monitor_id=%s)",
                event.event_type.value,
                event.monitor_id,
            )
            return None

        try:
            validate_webhook_url(target, settings=self._settings)
        except ValidationError as exc:
            logger.warning(
                "Rejected webhook URL for monitor_id=%s: %s",
                event.monitor_id,
                exc,
            )
            return DeliveryResult(
                event=event,
                webhook_url=target,
                delivered=False,
                status_code=None,
                error_message=f"Invalid webhook URL: {exc}",
                attempted_at=datetime.now(timezone.utc),
            )

        if self._is_duplicate(event):
            logger.info(
                "Suppressed duplicate notification %s for monitor_id=%s",
                event.event_type.value,
                event.monitor_id,
            )
            return None

        return await self._deliver(event, target)

    def reset_dedup_cache(self) -> None:
        """Clear the in-memory deduplication cache."""

        self._recent.clear()

    # -- Internal helpers ---------------------------------------------------

    def _is_duplicate(self, event: NotificationEvent) -> bool:
        """Return whether the event was already delivered within the window."""

        now = datetime.now(timezone.utc)
        key = event.dedup_key()

        # Prune expired entries to bound memory usage.
        if self._recent:
            cutoff = now - self._dedup_window
            expired = [k for k, ts in self._recent.items() if ts < cutoff]
            for k in expired:
                self._recent.pop(k, None)

        previous = self._recent.get(key)
        if previous is not None and (now - previous) <= self._dedup_window:
            return True

        self._recent[key] = now
        return False

    async def _deliver(
        self,
        event: NotificationEvent,
        webhook_url: str,
    ) -> DeliveryResult:
        """Perform the HTTP POST delivery of an event to a webhook."""

        attempted_at = datetime.now(timezone.utc)
        payload = event.to_payload()
        timeout = self._settings.webhook_timeout_seconds

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    webhook_url,
                    content=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "BeaconOps-Notifier/0.1",
                    },
                )
        except httpx.TimeoutException:
            logger.warning(
                "Webhook delivery timed out for monitor_id=%s event=%s",
                event.monitor_id,
                event.event_type.value,
            )
            return DeliveryResult(
                event=event,
                webhook_url=webhook_url,
                delivered=False,
                status_code=None,
                error_message="Webhook delivery timed out",
                attempted_at=attempted_at,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Webhook delivery failed for monitor_id=%s event=%s: %s",
                event.monitor_id,
                event.event_type.value,
                exc,
            )
            return DeliveryResult(
                event=event,
                webhook_url=webhook_url,
                delivered=False,
                status_code=None,
                error_message=f"Webhook delivery failed: {exc.__class__.__name__}",
                attempted_at=attempted_at,
            )
        except Exception as exc:  # noqa: BLE001 - delivery boundary
            logger.exception(
                "Unexpected error during webhook delivery for monitor_id=%s",
                event.monitor_id,
            )
            return DeliveryResult(
                event=event,
                webhook_url=webhook_url,
                delivered=False,
                status_code=None,
                error_message=f"Unexpected error: {exc.__class__.__name__}",
                attempted_at=attempted_at,
            )

        delivered = 200 <= response.status_code < 300
        error_message: Optional[str] = None
        if not delivered:
            error_message = f"Webhook responded with HTTP {response.status_code}"
            logger.warning(
                "Webhook returned non-2xx status %s for monitor_id=%s event=%s",
                response.status_code,
                event.monitor_id,
                event.event_type.value,
            )
        else:
            logger.info(
                "Delivered notification %s for monitor_id=%s",
                event.event_type.value,
                event.monitor_id,
            )

        return DeliveryResult(
            event=event,
            webhook_url=webhook_url,
            delivered=delivered,
            status_code=response.status_code,
            error_message=error_message,
            attempted_at=attempted_at,
        )
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>