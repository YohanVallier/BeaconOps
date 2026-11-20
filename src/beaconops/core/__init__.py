"""BeaconOps core package.

This package contains the domain logic of BeaconOps, decoupled from the HTTP
API layer:

- :mod:`beaconops.core.monitor`: Monitor management and check execution.
- :mod:`beaconops.core.schedule`: Periodic scheduling of monitor checks.
- :mod:`beaconops.core.notifications`: Webhook notification delivery.

The modules in this package must not import from :mod:`beaconops.api` so that
monitoring operations remain independent of the HTTP routes.
"""

from __future__ import annotations

__all__: list[str] = []
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>