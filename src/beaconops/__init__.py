"""BeaconOps — self-managed API availability and performance monitoring platform.

BeaconOps provides a modular platform for monitoring HTTP endpoints, recording
check history, identifying incidents, and delivering webhook notifications.

The package is organized into the following subpackages:

- ``beaconops.api``: FastAPI routes and Pydantic request/response models.
- ``beaconops.config``: Centralized application settings.
- ``beaconops.core``: Domain logic for monitors, scheduling, and notifications.
- ``beaconops.services``: HTTP client and incident identification services.
- ``beaconops.utils``: Validation and logging utilities.
"""

from __future__ import annotations

__all__ = ["__description__", "__title__", "__version__"]

__title__: str = "BeaconOps"
__version__: str = "0.1.0"
__description__: str = "Self-managed API availability and performance monitoring platform."
