"""Unit and integration tests for BeaconOps core functionality.

This module exercises the real behaviour of the main BeaconOps components:

- Monitor registration and validation.
- URL validation and SSRF protection.
- HTTP checks against a local test server (success, invalid status, timeout,
  connection failure).
- Consecutive-failure handling and incident lifecycle (open/close).
- Notification deduplication and webhook delivery failures.
- Persistence and historical queries.
- Operational indicator calculations.

The tests rely exclusively on the fixtures defined in ``tests/conftest.py``
and on a local HTTP server started within the test process. No external
services or real credentials are used.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from beaconops.config.settings import Settings
from beaconops.core.monitor import (
    CheckStatus,
    MonitorInput,
    MonitorNotFoundError,
    MonitorService,
    MonitorValidationError,
)
from beaconops.core.notifications import (
    NotificationEvent,
    NotificationEventType,
    Notifier,
)
from beaconops.services.http_client import CheckOutcome, HttpClient
from beaconops.services.incident_identification import (
    IncidentIdentifier,
    IncidentStatus,
    MonitorStateStatus,
)
from beaconops.utils.validations import (
    ValidationError,
    is_private_address,
    validate_monitor_url,
    validate_webhook_url,
)

# ---------------------------------------------------------------------------
# Local HTTP test server
# ---------------------------------------------------------------------------


class _TestHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler used to exercise the HTTP client."""

    server_version = "BeaconOpsTest/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr logging of the test server."""

        return

    def do_GET(self) -> None:
        """Serve deterministic responses based on the request path."""

        if self.path == "/ok":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/slow":
            # Sleep long enough to trigger a short client timeout.
            import time

            time.sleep(1.0)
            body = b"slow"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/error":
            body = b"boom"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return

        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Accept webhook deliveries and record them on the server."""

        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else b""
        received = getattr(self.server, "received", None)
        if received is not None:
            received.append(payload)

        if self.path == "/webhook-fail":
            self.send_response(500)
            self.end_headers()
            return

        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def http_server() -> Iterator[tuple[str, ThreadingHTTPServer]]:
    """Start a local HTTP server for the duration of a test.

    Yields:
        A tuple of ``(base_url, server)`` where ``base_url`` is the root URL
        of the running server and ``server`` exposes the ``received`` list of
        webhook payloads.
    """

    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"

    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine to completion in a fresh event loop."""

    return asyncio.run(coro)


def _make_input(url: str, **overrides: Any) -> MonitorInput:
    """Build a :class:`MonitorInput` with sensible defaults for tests."""

    defaults: dict[str, Any] = {
        "name": "Test monitor",
        "url": url,
        "method": "GET",
        "interval_seconds": 60,
        "timeout_seconds": 5.0,
        "expected_status": 200,
        "latency_threshold_ms": None,
        "response_contains": None,
        "active": True,
    }
    defaults.update(overrides)
    return MonitorInput(**defaults)


# ---------------------------------------------------------------------------
# Monitor registration and validation
# ---------------------------------------------------------------------------


def test_create_monitor_persists_configuration(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A valid monitor is persisted with the provided configuration."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok", name="Health endpoint"))

    assert monitor.id is not None
    assert monitor.name == "Health endpoint"
    assert monitor.url == f"{base_url}/ok"
    assert monitor.method == "GET"
    assert monitor.active is True
    assert monitor.created_at is not None

    fetched = monitor_service.get_monitor(monitor.id)
    assert fetched.id == monitor.id
    assert fetched.name == "Health endpoint"


def test_create_monitor_rejects_empty_name(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An empty monitor name is rejected."""

    base_url, _ = http_server
    with pytest.raises(MonitorValidationError):
        monitor_service.create_monitor(_make_input(f"{base_url}/ok", name="   "))


def test_create_monitor_rejects_invalid_interval(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An out-of-range interval is rejected."""

    base_url, _ = http_server
    with pytest.raises(MonitorValidationError):
        monitor_service.create_monitor(_make_input(f"{base_url}/ok", interval_seconds=1))


def test_create_monitor_rejects_unsupported_method(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An unsupported HTTP method is rejected."""

    base_url, _ = http_server
    with pytest.raises(MonitorValidationError):
        monitor_service.create_monitor(_make_input(f"{base_url}/ok", method="TRACE"))


def test_get_missing_monitor_raises(monitor_service: MonitorService) -> None:
    """Requesting a non-existent monitor raises ``MonitorNotFoundError``."""

    with pytest.raises(MonitorNotFoundError):
        monitor_service.get_monitor(999_999)


def test_update_and_delete_monitor(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A monitor can be updated and then deleted."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    updated = monitor_service.update_monitor(
        monitor.id,
        _make_input(f"{base_url}/error", name="Renamed", expected_status=500),
    )
    assert updated.name == "Renamed"
    assert updated.expected_status == 500

    monitor_service.delete_monitor(monitor.id)
    with pytest.raises(MonitorNotFoundError):
        monitor_service.get_monitor(monitor.id)


def test_list_monitors_filters_by_active(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Listing monitors honours the ``active`` filter."""

    base_url, _ = http_server
    active = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))
    inactive = monitor_service.create_monitor(
        _make_input(f"{base_url}/ok", name="Inactive", active=False)
    )

    active_only = monitor_service.list_monitors(active=True)
    assert {m.id for m in active_only} == {active.id}

    inactive_only = monitor_service.list_monitors(active=False)
    assert {m.id for m in inactive_only} == {inactive.id}


# ---------------------------------------------------------------------------
# URL validation and SSRF protection
# ---------------------------------------------------------------------------


def test_validate_monitor_url_accepts_public_https(settings: Settings) -> None:
    """A public HTTPS URL with an allowed port is accepted."""

    result = validate_monitor_url("https://example.com/health", settings=settings)
    assert result.scheme == "https"
    assert result.hostname == "example.com"
    assert result.port == 443


def test_validate_monitor_url_rejects_non_http_scheme(settings: Settings) -> None:
    """Non-HTTP schemes are rejected."""

    with pytest.raises(ValidationError):
        validate_monitor_url("ftp://example.com/file", settings=settings)


def test_validate_monitor_url_rejects_empty(settings: Settings) -> None:
    """Empty URLs are rejected."""

    with pytest.raises(ValidationError):
        validate_monitor_url("", settings=settings)


def test_validate_monitor_url_rejects_loopback_when_private_disallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback destinations are rejected when private targets are disallowed."""

    from beaconops.config.settings import reload_settings

    monkeypatch.setenv("BEACONOPS_ALLOW_PRIVATE_TARGETS", "false")
    monkeypatch.setenv("BEACONOPS_ALLOWED_PORTS", "")
    restricted = reload_settings()

    with pytest.raises(ValidationError):
        validate_monitor_url("http://127.0.0.1/health", settings=restricted)


def test_validate_monitor_url_rejects_disallowed_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ports outside the allow-list are rejected."""

    from beaconops.config.settings import reload_settings

    monkeypatch.setenv("BEACONOPS_ALLOW_PRIVATE_TARGETS", "true")
    monkeypatch.setenv("BEACONOPS_ALLOWED_PORTS", "80,443")
    restricted = reload_settings()

    with pytest.raises(ValidationError):
        validate_monitor_url("http://example.com:8080/health", settings=restricted)


def test_validate_webhook_url_applies_same_rules(settings: Settings) -> None:
    """Webhook URLs are validated with the same SSRF policy."""

    with pytest.raises(ValidationError):
        validate_webhook_url("file:///etc/passwd", settings=settings)


def test_is_private_address_classification() -> None:
    """Private, loopback, and link-local addresses are classified as non-public."""

    import ipaddress

    assert is_private_address(ipaddress.ip_address("127.0.0.1")) is True
    assert is_private_address(ipaddress.ip_address("10.0.0.1")) is True
    assert is_private_address(ipaddress.ip_address("192.168.1.1")) is True
    assert is_private_address(ipaddress.ip_address("169.254.1.1")) is True
    assert is_private_address(ipaddress.ip_address("8.8.8.8")) is False


# ---------------------------------------------------------------------------
# HTTP client behaviour
# ---------------------------------------------------------------------------


def test_http_client_success(
    settings: Settings,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A 200 response with the expected status is reported as success."""

    base_url, _ = http_server
    client = HttpClient(settings=settings)
    try:
        result = _run(client.check(f"{base_url}/ok", expected_status=200))
    finally:
        client.close()

    assert result.outcome is CheckOutcome.SUCCESS
    assert result.status_code == 200
    assert result.response_time_ms is not None
    assert result.error_message is None


def test_http_client_unexpected_status(
    settings: Settings,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A response with an unexpected status is reported as an HTTP error."""

    base_url, _ = http_server
    client = HttpClient(settings=settings)
    try:
        result = _run(client.check(f"{base_url}/error", expected_status=200))
    finally:
        client.close()

    assert result.outcome is CheckOutcome.HTTP_ERROR
    assert result.status_code == 500
    assert result.error_message is not None


def test_http_client_timeout(
    settings: Settings,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A slow endpoint triggers a timeout outcome."""

    base_url, _ = http_server
    client = HttpClient(settings=settings)
    try:
        result = _run(client.check(f"{base_url}/slow", timeout_seconds=0.1))
    finally:
        client.close()

    assert result.outcome is CheckOutcome.TIMEOUT
    assert result.status_code is None


def test_http_client_connection_error(settings: Settings) -> None:
    """A connection to a closed port is reported as a connection error."""

    client = HttpClient(settings=settings)
    try:
        result = _run(client.check("http://127.0.0.1:1/never", timeout_seconds=0.5))
    finally:
        client.close()

    assert result.outcome in {
        CheckOutcome.CONNECTION_ERROR,
        CheckOutcome.TIMEOUT,
    }


def test_http_client_rejects_invalid_url(settings: Settings) -> None:
    """An invalid URL is rejected before any request is made."""

    client = HttpClient(settings=settings)
    try:
        result = _run(client.check("ftp://example.com/file"))
    finally:
        client.close()

    assert result.outcome is CheckOutcome.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# Check execution and persistence
# ---------------------------------------------------------------------------


def test_run_check_persists_success(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A successful check is persisted with a success status."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    result = _run(monitor_service.run_check(monitor.id))
    assert result.status is CheckStatus.SUCCESS
    assert result.http_status == 200

    checks = monitor_service.list_checks(monitor.id)
    assert len(checks) == 1
    assert checks[0].status == CheckStatus.SUCCESS.value
    assert monitor_service.count_checks(monitor.id) == 1


def test_run_check_persists_failure(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A failed check is persisted with a failure status."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/error"))

    result = _run(monitor_service.run_check(monitor.id))
    assert result.status is CheckStatus.FAILURE
    assert result.http_status == 500

    checks = monitor_service.list_checks(monitor.id)
    assert len(checks) == 1
    assert checks[0].status == CheckStatus.FAILURE.value


def test_list_checks_filters_by_status(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Check history can be filtered by status."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    _run(monitor_service.run_check(monitor.id))
    monitor_service.update_monitor(monitor.id, _make_input(f"{base_url}/error"))
    _run(monitor_service.run_check(monitor.id))

    successes = monitor_service.list_checks(monitor.id, status=CheckStatus.SUCCESS)
    failures = monitor_service.list_checks(monitor.id, status=CheckStatus.FAILURE)

    assert len(successes) == 1
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Incident lifecycle
# ---------------------------------------------------------------------------


def test_incident_opens_after_consecutive_failures(
    monitor_service: MonitorService,
    incident_identifier: IncidentIdentifier,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An incident opens once the failure threshold is reached."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/error"))

    evaluations = []
    for _ in range(3):
        result = _run(monitor_service.run_check(monitor.id))
        evaluations.append(_run(incident_identifier.evaluate(monitor, result)))

    assert evaluations[0].incident_opened is False
    assert evaluations[1].incident_opened is False
    assert evaluations[2].incident_opened is True
    assert evaluations[2].current_status is MonitorStateStatus.UNAVAILABLE

    incidents = incident_identifier.list_incidents(monitor_id=monitor.id)
    assert len(incidents) == 1
    assert incidents[0].status == IncidentStatus.OPEN.value


def test_no_duplicate_incident_while_unavailable(
    monitor_service: MonitorService,
    incident_identifier: IncidentIdentifier,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Additional failures do not open a second incident."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/error"))

    for _ in range(5):
        result = _run(monitor_service.run_check(monitor.id))
        _run(incident_identifier.evaluate(monitor, result))

    incidents = incident_identifier.list_incidents(monitor_id=monitor.id)
    assert len(incidents) == 1


def test_incident_closes_after_recovery(
    monitor_service: MonitorService,
    incident_identifier: IncidentIdentifier,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An open incident closes once the recovery threshold is reached."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/error"))

    for _ in range(3):
        result = _run(monitor_service.run_check(monitor.id))
        _run(incident_identifier.evaluate(monitor, result))

    monitor_service.update_monitor(monitor.id, _make_input(f"{base_url}/ok"))

    evaluations = []
    for _ in range(2):
        result = _run(monitor_service.run_check(monitor.id))
        evaluations.append(_run(incident_identifier.evaluate(monitor, result)))

    assert evaluations[0].incident_closed is False
    assert evaluations[1].incident_closed is True
    assert evaluations[1].current_status is MonitorStateStatus.AVAILABLE

    incidents = incident_identifier.list_incidents(monitor_id=monitor.id)
    assert len(incidents) == 1
    assert incidents[0].status == IncidentStatus.CLOSED.value
    assert incidents[0].closed_at is not None
    assert incidents[0].duration_seconds is not None


def test_count_open_incidents(
    monitor_service: MonitorService,
    incident_identifier: IncidentIdentifier,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Open incidents are counted correctly."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/error"))

    for _ in range(3):
        result = _run(monitor_service.run_check(monitor.id))
        _run(incident_identifier.evaluate(monitor, result))

    assert incident_identifier.count_open_incidents(monitor.id) == 1
    assert incident_identifier.count_open_incidents() == 1


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_notifier_deduplicates_events(
    monkeypatch: pytest.MonkeyPatch,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Duplicate events within the dedup window are suppressed."""

    from beaconops.config.settings import reload_settings

    base_url, server = http_server
    monkeypatch.setenv("BEACONOPS_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("BEACONOPS_DEFAULT_WEBHOOK_URL", f"{base_url}/webhook")
    settings = reload_settings()

    notifier = Notifier(settings=settings, dedup_window_seconds=300)
    event = NotificationEvent(
        event_type=NotificationEventType.SERVICE_UNAVAILABLE,
        monitor_id=1,
        monitor_name="Test",
        occurred_at=datetime.now(UTC),
        message="down",
    )

    first = _run(notifier.notify(event))
    second = _run(notifier.notify(event))

    assert first is not None and first.delivered is True
    assert second is None
    assert len(server.received) == 1  # type: ignore[attr-defined]


def test_notifier_records_webhook_failure(
    monkeypatch: pytest.MonkeyPatch,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A non-2xx webhook response is recorded as a delivery failure."""

    from beaconops.config.settings import reload_settings

    base_url, _ = http_server
    monkeypatch.setenv("BEACONOPS_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("BEACONOPS_DEFAULT_WEBHOOK_URL", f"{base_url}/webhook-fail")
    settings = reload_settings()

    notifier = Notifier(settings=settings)
    event = NotificationEvent(
        event_type=NotificationEventType.SERVICE_RECOVERED,
        monitor_id=2,
        monitor_name="Test",
        occurred_at=datetime.now(UTC),
        message="up",
    )

    result = _run(notifier.notify(event))
    assert result is not None
    assert result.delivered is False
    assert result.status_code == 500
    assert result.error_message is not None


def test_notifier_skips_when_disabled(settings: Settings) -> None:
    """No delivery is attempted when notifications are disabled."""

    notifier = Notifier(settings=settings)
    event = NotificationEvent(
        event_type=NotificationEventType.LATENCY_EXCEEDED,
        monitor_id=3,
        monitor_name="Test",
        occurred_at=datetime.now(UTC),
        message="slow",
    )

    assert _run(notifier.notify(event)) is None


def test_notification_payload_excludes_credentials() -> None:
    """The serialized payload contains only the documented fields."""

    event = NotificationEvent(
        event_type=NotificationEventType.SERVICE_UNAVAILABLE,
        monitor_id=4,
        monitor_name="Test",
        occurred_at=datetime.now(UTC),
        message="down",
        details={"http_status": 500},
    )

    payload = event.to_payload()
    assert set(payload) == {
        "event_type",
        "monitor_id",
        "monitor_name",
        "occurred_at",
        "message",
        "details",
    }
    assert "authorization" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_computation(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Operational indicators are computed from persisted checks."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    _run(monitor_service.run_check(monitor.id))
    _run(monitor_service.run_check(monitor.id))

    monitor_service.update_monitor(monitor.id, _make_input(f"{base_url}/error"))
    _run(monitor_service.run_check(monitor.id))

    checks = monitor_service.list_checks(monitor.id, limit=1000)
    total = len(checks)
    successes = [c for c in checks if c.status == CheckStatus.SUCCESS.value]
    failures = total - len(successes)

    assert total == 3
    assert len(successes) == 2
    assert failures == 1

    latencies = [c.response_time_ms for c in successes if c.response_time_ms is not None]
    assert latencies
    average = sum(latencies) / len(latencies)
    assert average > 0

    uptime = len(successes) / total
    assert 0.0 <= uptime <= 1.0


def test_metrics_with_no_checks(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """A monitor without checks yields an empty history."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    checks = monitor_service.list_checks(monitor.id)
    assert checks == []
    assert monitor_service.count_checks(monitor.id) == 0


# ---------------------------------------------------------------------------
# Persistence and history
# ---------------------------------------------------------------------------


def test_purge_checks_before_cutoff(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Checks older than the cutoff are deleted."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))
    _run(monitor_service.run_check(monitor.id))

    future = datetime.now(UTC) + timedelta(hours=1)
    deleted = monitor_service.purge_checks_before(monitor.id, future)

    assert deleted == 1
    assert monitor_service.count_checks(monitor.id) == 0


def test_check_history_pagination(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """Check history supports pagination."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    for _ in range(3):
        _run(monitor_service.run_check(monitor.id))

    first_page = monitor_service.list_checks(monitor.id, limit=2, offset=0)
    second_page = monitor_service.list_checks(monitor.id, limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {c.id for c in first_page}.isdisjoint({c.id for c in second_page})


def test_list_checks_rejects_invalid_limit(
    monitor_service: MonitorService,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """An invalid pagination limit is rejected."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    with pytest.raises(MonitorValidationError):
        monitor_service.list_checks(monitor.id, limit=0)


def test_monitor_state_initialized_on_first_evaluation(
    monitor_service: MonitorService,
    incident_identifier: IncidentIdentifier,
    http_server: tuple[str, ThreadingHTTPServer],
) -> None:
    """The monitor state row is created on the first evaluation."""

    base_url, _ = http_server
    monitor = monitor_service.create_monitor(_make_input(f"{base_url}/ok"))

    assert incident_identifier.get_state(monitor.id) is None

    result = _run(monitor_service.run_check(monitor.id))
    _run(incident_identifier.evaluate(monitor, result))

    state = incident_identifier.get_state(monitor.id)
    assert state is not None
    assert state.status == MonitorStateStatus.AVAILABLE.value
    assert state.consecutive_successes == 1
    assert state.consecutive_failures == 0
