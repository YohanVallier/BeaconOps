"""BeaconOps services package.

This package contains the application services that integrate BeaconOps with
external systems and implement cross-cutting use cases:

- :mod:`beaconops.services.http_client`: Outbound HTTP checks using HTTPX,
  enforcing SSRF protections, timeouts, and response size limits.
- :mod:`beaconops.services.incident_identification`: Evaluation of check
  results to open, maintain, and close incidents.

Services must not import from :mod:`beaconops.api` so that monitoring
operations remain independent of the HTTP routes.
"""

from __future__ import annotations

__all__: list[str] = []
