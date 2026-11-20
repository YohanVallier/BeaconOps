"""Pydantic request and response models for the BeaconOps API.

This module defines the request and response contracts exposed by the
BeaconOps REST API. The models are used by FastAPI to validate incoming
payloads and to serialize outgoing responses, and they are the single source
of truth for the API's OpenAPI schema.

Responsibilities
----------------
- Define request models for creating and updating monitors.
- Define response models for monitors, checks, incidents, metrics, and health.
- Provide conversion helpers between API models and domain objects
  (:class:`beaconops.core.monitor.MonitorInput`, ORM entities).
- Enforce input validation constraints that mirror the domain rules so that
  invalid payloads are rejected before reaching the service layer.

The module depends on the domain enums and input types defined in
:mod:`beaconops.core.monitor` and
:mod:`beaconops.services.incident_identification`, but it must never be
imported by them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from beaconops.core.monitor import (
    CheckStatus,
    Monitor,
    MonitorInput,
)
from beaconops.services.incident_identification import (
    IncidentStatus,
)

__all__ = [
    "CheckListResponse",
    "CheckResponse",
    "HealthResponse",
    "IncidentListResponse",
    "IncidentResponse",
    "MetricsResponse",
    "MonitorCreateRequest",
    "MonitorListResponse",
    "MonitorResponse",
    "MonitorUpdateRequest",
]


# ---------------------------------------------------------------------------
# Shared constraints
# ---------------------------------------------------------------------------

_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
)
_MIN_INTERVAL_SECONDS = 5
_MAX_INTERVAL_SECONDS = 86_400
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_NAME_LENGTH = 255
_MAX_URL_LENGTH = 2048


# ---------------------------------------------------------------------------
# Monitor requests
# ---------------------------------------------------------------------------


class MonitorCreateRequest(BaseModel):
    """Payload for creating a new monitor.

    All fields are validated against the same constraints enforced by the
    domain layer. URL validation against the SSRF policy is performed by the
    service layer, which has access to the resolved settings.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_MAX_NAME_LENGTH,
            description="Human-readable monitor name.",
        ),
    ]
    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_MAX_URL_LENGTH,
            description="Target URL. Only http and https are accepted.",
        ),
    ]
    method: Annotated[
        str,
        Field(
            default="GET",
            description="HTTP method used for checks.",
        ),
    ] = "GET"
    interval_seconds: Annotated[
        int,
        Field(
            default=60,
            ge=_MIN_INTERVAL_SECONDS,
            le=_MAX_INTERVAL_SECONDS,
            description="Interval between automatic checks, in seconds.",
        ),
    ] = 60
    timeout_seconds: Annotated[
        float,
        Field(
            default=10.0,
            ge=_MIN_TIMEOUT_SECONDS,
            le=_MAX_TIMEOUT_SECONDS,
            description="Per-check timeout, in seconds.",
        ),
    ] = 10.0
    expected_status: Annotated[
        int,
        Field(
            default=200,
            ge=100,
            le=599,
            description="HTTP status code considered successful.",
        ),
    ] = 200
    latency_threshold_ms: Annotated[
        Optional[float],
        Field(
            default=None,
            gt=0,
            description="Optional latency threshold, in milliseconds.",
        ),
    ] = None
    response_contains: Annotated[
        Optional[str],
        Field(
            default=None,
            max_length=4096,
            description="Optional substring that must appear in the response body.",
        ),
    ] = None
    active: Annotated[
        bool,
        Field(
            default=True,
            description="Whether the monitor is active.",
        ),
    ] = True

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        """Normalize and validate the HTTP method."""

        normalized = value.strip().upper()
        if normalized not in _ALLOWED_METHODS:
            raise ValueError(
                f"Unsupported HTTP method {value!r}; "
                f"allowed: {sorted(_ALLOWED_METHODS)}"
            )
        return normalized

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, value: str) -> str:
        """Perform a cheap structural check on the URL scheme.

        Full SSRF validation (DNS resolution, IP classification, port
        restrictions) is performed by the service layer, which has access to
        the resolved settings.
        """

        lowered = value.strip().lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("URL must use the http or https scheme")
        return value.strip()

    def to_input(self) -> MonitorInput:
        """Convert the request into a domain :class:`MonitorInput`."""

        return MonitorInput(
            name=self.name,
            url=self.url,
            method=self.method,
            interval_seconds=self.interval_seconds,
            timeout_seconds=self.timeout_seconds,
            expected_status=self.expected_status,
            latency_threshold_ms=self.latency_threshold_ms,
            response_contains=self.response_contains,
            active=self.active,
        )


class MonitorUpdateRequest(BaseModel):
    """Payload for updating an existing monitor.

    All fields are optional. Omitted fields retain their current values. The
    :meth:`merge` helper combines the partial update with the existing monitor
    to produce a complete :class:`MonitorInput`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[
        Optional[str],
        Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH),
    ] = None
    url: Annotated[
        Optional[str],
        Field(default=None, min_length=1, max_length=_MAX_URL_LENGTH),
    ] = None
    method: Annotated[Optional[str], Field(default=None)] = None
    interval_seconds: Annotated[
        Optional[int],
        Field(
            default=None,
            ge=_MIN_INTERVAL_SECONDS,
            le=_MAX_INTERVAL_SECONDS,
        ),
    ] = None
    timeout_seconds: Annotated[
        Optional[float],
        Field(
            default=None,
            ge=_MIN_TIMEOUT_SECONDS,
            le=_MAX_TIMEOUT_SECONDS,
        ),
    ] = None
    expected_status: Annotated[
        Optional[int],
        Field(default=None, ge=100, le=599),
    ] = None
    latency_threshold_ms: Annotated[
        Optional[float],
        Field(default=None, gt=0),
    ] = None
    response_contains: Annotated[
        Optional[str],
        Field(default=None, max_length=4096),
    ] = None
    active: Annotated[Optional[bool], Field(default=None)] = None

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: Optional[str]) -> Optional[str]:
        """Normalize and validate the HTTP method when provided."""

        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_METHODS:
            raise ValueError(
                f"Unsupported HTTP method {value!r}; "
                f"allowed: {sorted(_ALLOWED_METHODS)}"
            )
        return normalized

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, value: Optional[str]) -> Optional[str]:
        """Perform a cheap structural check on the URL scheme when provided."""

        if value is None:
            return None
        stripped = value.strip()
        lowered = stripped.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("URL must use the http or https scheme")
        return stripped

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "MonitorUpdateRequest":
        """Reject empty update payloads."""

        if not self.model_fields_set:
            raise ValueError("At least one field must be provided for update")
        return self

    def merge(self, existing: Monitor) -> MonitorInput:
        """Merge the partial update with an existing monitor.

        Args:
            existing: The current monitor ORM object.

        Returns:
            A complete :class:`MonitorInput` reflecting the merged values.
        """

        return MonitorInput(
            name=self.name if self.name is not None else existing.name,
            url=self.url if self.url is not None else existing.url,
            method=self.method if self.method is not None else existing.method,
            interval_seconds=(
                self.interval_seconds
                if self.interval_seconds is not None
                else existing.interval_seconds
            ),
            timeout_seconds=(
                self.timeout_seconds
                if self.timeout_seconds is not None
                else existing.timeout_seconds
            ),
            expected_status=(
                self.expected_status
                if self.expected_status is not None
                else existing.expected_status
            ),
            latency_threshold_ms=(
                self.latency_threshold_ms
                if "latency_threshold_ms" in self.model_fields_set
                else existing.latency_threshold_ms
            ),
            response_contains=(
                self.response_contains
                if "response_contains" in self.model_fields_set
                else existing.response_contains
            ),
            active=self.active if self.active is not None else existing.active,
        )


# ---------------------------------------------------------------------------
# Monitor responses
# ---------------------------------------------------------------------------


class MonitorResponse(BaseModel):
    """Serialized representation of a monitor."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    method: str
    interval_seconds: int
    timeout_seconds: float
    expected_status: int
    latency_threshold_ms: Optional[float]
    response_contains: Optional[str]
    active: bool
    created_at: datetime
    updated_at: datetime


class MonitorListResponse(BaseModel):
    """Paginated list of monitors."""

    items: list[MonitorResponse]
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Check responses
# ---------------------------------------------------------------------------


class CheckResponse(BaseModel):
    """Serialized representation of a single monitor check."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    monitor_id: int
    checked_at: datetime
    status: CheckStatus
    http_status: Optional[int]
    response_time_ms: Optional[float]
    error_message: Optional[str]
    diagnostic: Optional[str]


class CheckListResponse(BaseModel):
    """Paginated list of monitor checks."""

    items: list[CheckResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Incident responses
# ---------------------------------------------------------------------------


class IncidentResponse(BaseModel):
    """Serialized representation of an incident."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    monitor_id: int
    status: IncidentStatus
    opened_at: datetime
    closed_at: Optional[datetime]
    trigger_check_id: Optional[int]
    resolution_check_id: Optional[int]
    reason: Optional[str]
    resolution_reason: Optional[str]
    duration_seconds: Optional[float]


class IncidentListResponse(BaseModel):
    """Paginated list of incidents."""

    items: list[IncidentResponse]
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Metrics response
# ---------------------------------------------------------------------------


class MetricsResponse(BaseModel):
    """Operational indicators for a monitor over a reference period.

    All values are computed from persisted check records. When no checks exist
    in the period, ratio and latency fields are ``None`` rather than zero, so
    that "no data" is distinguishable from "zero".
    """

    monitor_id: int
    period: str
    since: datetime
    total_checks: int
    successful_checks: int
    failed_checks: int
    uptime_ratio: Optional[float]
    downtime_ratio: Optional[float]
    average_latency_ms: Optional[float]
    max_latency_ms: Optional[float]
    open_incidents: int


# ---------------------------------------------------------------------------
# Health response
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Application health status."""

    status: str
    version: str
    environment: str
    scheduler_running: bool
    timestamp: datetime
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>