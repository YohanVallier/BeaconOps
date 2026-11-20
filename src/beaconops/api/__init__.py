"""BeaconOps API package.

This package contains the HTTP layer of BeaconOps, built on FastAPI:

- :mod:`beaconops.api.routes`: FastAPI application and route definitions.
- :mod:`beaconops.api.pydantic_models`: Pydantic request/response models used
  to validate API contracts.

The API layer depends on the core and services packages but must never be
imported by them, so that monitoring operations remain independent of the HTTP
routes.
"""

from __future__ import annotations

__all__: list[str] = []
