"""URL and HTTP response validation utilities for BeaconOps.

This module implements the validation helpers used across BeaconOps to protect
against Server-Side Request Forgery (SSRF) and to validate HTTP responses.

Responsibilities
----------------
- Validate monitor URLs and webhook URLs before any outbound request.
- Enforce that only ``http`` and ``https`` schemes are accepted.
- Reject destinations that resolve to loopback, private, link-local, reserved,
  multicast, or unspecified IP ranges, unless explicitly permitted by
  configuration.
- Enforce port restrictions when configured.
- Re-validate redirect targets so that a permitted URL cannot redirect to a
  forbidden destination.
- Provide helpers to validate HTTP response bodies against expected content.
- Implement DNS rebinding mitigation by validating resolved addresses.

Security notes
--------------
Textual URL validation alone is **not** sufficient to prevent SSRF. This
module resolves hostnames and validates the resolved IP addresses, but it does
not offer absolute protection. DNS rebinding, malicious proxies, and overly
permissive administrative configuration can still bypass these checks. See the
project README for the documented limitations.

The module must not import from :mod:`beaconops.api`, :mod:`beaconops.core`, or
:mod:`beaconops.services` so that it remains free of circular dependencies.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from beaconops.config.settings import Settings, get_settings
from beaconops.utils.logging import get_logger

__all__ = [
    "ValidationError",
    "UrlValidationResult",
    "is_private_address",
    "resolve_hostname",
    "validate_monitor_url",
    "validate_redirect_target",
    "validate_response_body",
    "validate_webhook_url",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Raised when a URL or HTTP response fails validation."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
_MAX_URL_LENGTH = 2048
_MAX_HOSTNAME_LENGTH = 253
_DNS_RESOLUTION_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlValidationResult:
    """Result of a successful URL validation.

    Attributes:
        url: The original URL that was validated.
        scheme: The normalized scheme (``http`` or ``https``).
        hostname: The normalized hostname.
        port: The effective port (explicit or scheme default).
        resolved_addresses: The IP addresses the hostname resolved to.
    """

    url: str
    scheme: str
    hostname: str
    port: int
    resolved_addresses: tuple[str, ...]


# ---------------------------------------------------------------------------
# IP classification
# ---------------------------------------------------------------------------


def is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an IP address belongs to a non-public range.

    The following ranges are considered non-public and are therefore rejected
    unless private targets are explicitly permitted:

    - Loopback (``127.0.0.0/8``, ``::1``).
    - Private (``10.0.0.0/8``, ``172.16.0.0/12``, ``192.168.0.0/16``,
      ``fc00::/7``).
    - Link-local (``169.254.0.0/16``, ``fe80::/10``).
    - Reserved and unspecified (``0.0.0.0/8``, ``::``).
    - Multicast (``224.0.0.0/4``, ``ff00::/8``).

    Args:
        address: The IP address to classify.

    Returns:
        ``True`` if the address is non-public, ``False`` otherwise.
    """

    if address.is_loopback:
        return True
    if address.is_private:
        return True
    if address.is_link_local:
        return True
    if address.is_multicast:
        return True
    if address.is_reserved:
        return True
    if address.is_unspecified:
        return True
    return False


# ---------------------------------------------------------------------------
# DNS resolution
# ---------------------------------------------------------------------------


def resolve_hostname(hostname: str) -> tuple[str, ...]:
    """Resolve a hostname to its IP addresses.

    Args:
        hostname: The hostname to resolve.

    Returns:
        A tuple of resolved IP address strings. Empty when resolution fails.

    Raises:
        ValidationError: If the hostname is empty or too long.
    """

    if not hostname:
        raise ValidationError("Hostname must not be empty")
    if len(hostname) > _MAX_HOSTNAME_LENGTH:
        raise ValidationError("Hostname exceeds maximum length")

    try:
        infos = socket.getaddrinfo(
            hostname,
            None,
            proto=socket.IPPROTO_TCP,
            timeout=_DNS_RESOLUTION_TIMEOUT,
        )
    except socket.timeout:
        logger.debug("DNS resolution timed out for hostname %r", hostname)
        return ()
    except socket.gaierror:
        logger.debug("DNS resolution failed for hostname %r", hostname)
        return ()
    except socket.error as exc:
        logger.debug("Socket error while resolving hostname %r: %s", hostname, exc)
        return ()
    except Exception as exc:  # noqa: BLE001 - defensive boundary
        logger.debug("Unexpected error while resolving hostname %r: %s", hostname, exc)
        return ()

    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)

    return tuple(addresses)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def _parse_url(url: str) -> tuple[str, str, int]:
    """Parse and structurally validate a URL.

    Args:
        url: The URL to parse.

    Returns:
        A tuple of ``(scheme, hostname, port)``.

    Raises:
        ValidationError: If the URL is malformed or uses a disallowed scheme.
    """

    if not url or not url.strip():
        raise ValidationError("URL must not be empty")

    if len(url) > _MAX_URL_LENGTH:
        raise ValidationError("URL exceeds maximum length")

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ValidationError(f"Malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValidationError(
            f"Unsupported URL scheme {parts.scheme!r}; "
            f"allowed: {sorted(_ALLOWED_SCHEMES)}"
        )

    hostname = parts.hostname
    if not hostname:
        raise ValidationError("URL must include a hostname")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationError(f"Invalid port in URL: {exc}") from exc

    if port is None:
        port = _DEFAULT_PORTS[scheme]

    if not (1 <= port <= 65535):
        raise ValidationError("Port must be between 1 and 65535")

    return scheme, hostname, port


def _validate_port(port: int, settings: Settings) -> None:
    """Validate the port against the configured allow-list.

    Args:
        port: The effective port.
        settings: The application settings.

    Raises:
        ValidationError: If the port is not permitted.
    """

    allowed_ports = settings.allowed_ports
    if not allowed_ports:
        return
    if port not in allowed_ports:
        raise ValidationError(
            f"Port {port} is not permitted; allowed: {sorted(allowed_ports)}"
        )


def _validate_hostname(hostname: str, settings: Settings) -> None:
    """Validate the hostname against the configured allow-list.

    Args:
        hostname: The normalized hostname.
        settings: The application settings.

    Raises:
        ValidationError: If the hostname is not permitted.
    """

    allowed_hosts = settings.allowed_hosts
    if not allowed_hosts:
        return
    normalized = hostname.lower()
    if normalized not in {host.lower() for host in allowed_hosts}:
        raise ValidationError(
            f"Hostname {hostname!r} is not in the allowed hosts list"
        )


def _validate_addresses(
    hostname: str,
    addresses: Iterable[str],
    settings: Settings,
) -> tuple[str, ...]:
    """Validate resolved IP addresses against the SSRF policy.

    Args:
        hostname: The hostname that was resolved.
        addresses: The resolved IP address strings.
        settings: The application settings.

    Returns:
        The validated addresses as a tuple.

    Raises:
        ValidationError: If any address is forbidden.
    """

    resolved = tuple(addresses)
    if not resolved:
        raise ValidationError(f"Unable to resolve hostname {hostname!r}")

    if settings.allow_private_targets:
        return resolved

    for raw in resolved:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid resolved IP address {raw!r} for hostname {hostname!r}"
            ) from exc

        if is_private_address(address):
            raise ValidationError(
                f"Destination {raw} for hostname {hostname!r} is not permitted "
                "(private, loopback, link-local, or reserved address)"
            )

    return resolved


def _validate_url(url: str, settings: Settings) -> UrlValidationResult:
    """Validate a URL against the full SSRF policy.

    Args:
        url: The URL to validate.
        settings: The application settings.

    Returns:
        A :class:`UrlValidationResult` describing the validated URL.

    Raises:
        ValidationError: If the URL fails any validation step.
    """

    scheme, hostname, port = _parse_url(url)
    _validate_port(port, settings)
    _validate_hostname(hostname, settings)

    # If the hostname is already an IP literal, validate it directly.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = _validate_addresses(hostname, (str(literal),), settings)
    else:
        resolved = resolve_hostname(hostname)
        addresses = _validate_addresses(hostname, resolved, settings)

    return UrlValidationResult(
        url=url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        resolved_addresses=addresses,
    )


def validate_monitor_url(
    url: str,
    settings: Optional[Settings] = None,
) -> UrlValidationResult:
    """Validate a monitor URL against the SSRF policy.

    Args:
        url: The URL to validate.
        settings: Optional settings override. When omitted, the global
            application settings are used.

    Returns:
        A :class:`UrlValidationResult` describing the validated URL.

    Raises:
        ValidationError: If the URL fails any validation step.
    """

    resolved_settings = settings or get_settings()
    return _validate_url(url, resolved_settings)


def validate_webhook_url(
    url: str,
    settings: Optional[Settings] = None,
) -> UrlValidationResult:
    """Validate a webhook URL against the SSRF policy.

    Webhook URLs are subject to the same protections as monitor URLs.

    Args:
        url: The webhook URL to validate.
        settings: Optional settings override. When omitted, the global
            application settings are used.

    Returns:
        A :class:`UrlValidationResult` describing the validated URL.

    Raises:
        ValidationError: If the URL fails any validation step.
    """

    resolved_settings = settings or get_settings()
    return _validate_url(url, resolved_settings)


def validate_redirect_target(
    url: str,
    settings: Optional[Settings] = None,
) -> UrlValidationResult:
    """Validate a redirect target against the SSRF policy.

    Redirect targets are re-validated with the same rules as the original
    request so that a permitted URL cannot redirect to a forbidden
    destination.

    Args:
        url: The redirect target URL.
        settings: Optional settings override. When omitted, the global
            application settings are used.

    Returns:
        A :class:`UrlValidationResult` describing the validated URL.

    Raises:
        ValidationError: If the redirect target fails any validation step.
    """

    resolved_settings = settings or get_settings()
    return _validate_url(url, resolved_settings)


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def validate_response_body(
    body: Optional[str],
    *,
    expected_contains: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> None:
    """Validate an HTTP response body against expected content.

    Args:
        body: The response body text, or ``None`` when unavailable.
        expected_contains: Optional substring that must appear in the body.
        max_bytes: Optional maximum allowed body size in bytes. When the body
            exceeds this limit, validation fails.

    Raises:
        ValidationError: If the body fails validation.
    """

    if max_bytes is not None and max_bytes < 0:
        raise ValidationError("max_bytes must be non-negative")

    if body is None:
        if expected_contains is not None:
            raise ValidationError(
                "Response body is unavailable but content validation was requested"
            )
        return

    if max_bytes is not None:
        encoded_length = len(body.encode("utf-8", errors="replace"))
        if encoded_length > max_bytes:
            raise ValidationError(
                f"Response body exceeds maximum size of {max_bytes} bytes"
            )

    if expected_contains is not None and expected_contains not in body:
        raise ValidationError("Response body did not contain expected content")
