# Changelog

All notable changes to **BeaconOps** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Web dashboard with historical charts.
- Multi-user authentication and role management.
- Additional notification channels (e-mail, Slack, Telegram).
- Periodic reports.
- TLS certificate and DNS monitoring.
- Monitor grouping.
- Incident escalation policies.
- Advanced metrics (latency percentiles, SLOs).
- Expanded PostgreSQL support.
- Distributed processing (only with concrete operational justification).

---

## [0.1.0] - 2024-01-01

Initial MVP release of BeaconOps — a self-managed platform for API availability
and performance monitoring.

### Added

#### Monitor management
- CRUD operations for monitored endpoints (create, read, update, delete).
- Activation and deactivation of monitors.
- Per-monitor configuration: name, URL, HTTP method, check interval, timeout,
  expected HTTP status, latency threshold, optional response validation
  criterion, activation state, and creation timestamp.
- Strict validation of monitor configuration — invalid configurations are
  rejected.

#### Automated monitoring
- Periodic execution of active monitors via APScheduler, decoupled from the
  HTTP API layer.
- Concurrent checks with a configurable concurrency limit
  (`BEACONOPS_MAX_CONCURRENT_CHECKS`).
- Handling of timeouts, connection failures, HTTP errors, and unexpected
  responses.
- Mandatory timeouts and safe cancellation of in-flight operations.

#### Check history
- Persistence of every check execution with: associated monitor, timestamp,
  result, HTTP status code, response time, error message (when applicable),
  and diagnostic information.
- Explicit distinction between successful and failed checks.

#### Incident identification
- Detection of unavailability based on consecutive failures with a configurable
  threshold (`BEACONOPS_FAILURE_THRESHOLD`).
- Transition from available → unavailable opens an incident.
- Transition from unavailable → available closes the incident (recovery), with
  a configurable threshold (`BEACONOPS_RECOVERY_THRESHOLD`).
- No duplicate incident is opened for each failed check of an already
  unavailable service.

#### Notifications
- Webhook HTTP notifications for relevant events:
  - Service unavailable.
  - Service recovered.
  - Latency above threshold.
  - Relevant monitor state change.
- Deduplication of notifications.
- Recording of delivery failures.
- No credentials or unnecessary sensitive information are transmitted.

#### History and queries
- Check history with filters and pagination.
- Current monitor state.
- Incidents (active and closed).
- Average response time, availability rate, failure occurrences, and downtime
  periods.

#### Operational indicators
- **Uptime**: proportion of successful checks in the reference period.
- **Downtime**: proportion of failed checks in the reference period.
- **Average latency** and **maximum latency** of successful checks.
- **Check count** and **failure count** in the period.
- **Active incidents** at query time.
- **Recovery time**: interval between incident opening and closing.
- All metrics are computed from persisted data — no values are invented or
  estimated.

#### REST API
- FastAPI-based REST API with automatic OpenAPI documentation
  (`/docs`, `/redoc`, `/openapi.json`).
- Endpoints for monitor management, check history, incidents, metrics, manual
  checks, and application health.
- Pydantic models for request and response validation.
- Administrative endpoints protected by an API key (`X-API-Key`).

#### Security
- Rigorous URL validation — only `http` and `https` protocols accepted.
- Blocking of local, private, reserved, and link-local destinations by default
  (`BEACONOPS_ALLOW_PRIVATE_TARGETS=false`).
- Validation of resolved IP addresses, not only URL text.
- Protection against redirects to forbidden destinations.
- Port restriction via `BEACONOPS_ALLOWED_PORTS`.
- Mandatory timeouts and response size limits.
- Webhook validation using the same SSRF rules.
- Access control for administrative endpoints via `BEACONOPS_ADMIN_API_KEY`.

#### Configuration
- Centralized settings via environment variables with `.env` support.
- No hardcoded secrets.
- Support for SQLite (default) and PostgreSQL via `BEACONOPS_DATABASE_URL`.

#### Infrastructure
- Dockerfile for containerized deployment.
- Docker Compose configuration for local orchestration.
- GitHub Actions CI workflow running lint, type checks, and tests with coverage
  on Python 3.11 and 3.12.

#### Testing
- Pytest-based test suite covering monitor registration, URL validation, HTTP
  responses, timeouts, connection failures, periodic execution, consecutive
  failures, incident lifecycle, notification deduplication, webhook failures,
  persistence, historical queries, indicator calculations, and SSRF
  protection.
- Tests use mocks and local HTTP servers — no external services or real
  credentials required.

#### Documentation
- Comprehensive README with problem statement, target audience, features,
  technologies, architecture, monitoring flow, directory structure,
  prerequisites, installation, configuration, environment variables,
  initialization, API endpoints, usage examples, testing, security,
  limitations, roadmap, and license.
- Mermaid diagrams illustrating the monitoring flow and component
  communication.

### Known limitations
- No multi-user authentication in the MVP — only an administrative API key.
- No frontend dashboard — the interface is the documented REST API.
- SQLite is the default; PostgreSQL is supported but the MVP was primarily
  validated on SQLite.
- Notifications only via HTTP Webhook.
- Single-process scheduler — no distributed processing in the MVP.
- No TLS certificate or DNS monitoring.
- The SSRF mitigation strategy does not offer absolute protection; see the
  README security section for details.

---

[Unreleased]: https://github.com/beaconops/beaconops/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/beaconops/beaconops/releases/tag/v0.1.0