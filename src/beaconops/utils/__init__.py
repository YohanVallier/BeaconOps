"""BeaconOps utilities package.

This package contains cross-cutting utilities used across the application:

- :mod:`beaconops.utils.validations`: URL and HTTP response validation helpers,
  including the SSRF protections applied to monitored endpoints and webhooks.
- :mod:`beaconops.utils.logging`: Centralized logging configuration.

Utilities must not import from :mod:`beaconops.api`, :mod:`beaconops.core`, or
:mod:`beaconops.services` so that they remain free of circular dependencies.
"""

from __future__ import annotations

__all__: list[str] = []
