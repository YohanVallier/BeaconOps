"""Outbound HTTP client for BeaconOps monitor checks.

This module implements the HTTP client used to execute availability checks
against monitored endpoints. It is built on top of HTTPX and enforces the
security and resource constraints required by the platform:

- SSRF protection via :mod:`beaconops.utils.validations` (URL validation,
  resolved-IP validation, redirect re-validation, port restrictions).
- Mandatory timeouts.
- Response body size limits.
- SSL certificate validation.
- Safe handling of connection failures, timeouts, and unexpected errors.

The client is intentionally decoupled from the API layer and from the
scheduler. It exposes a single :meth:`HttpClient.check` coroutine that returns
an :class:`HttpCheckResult` describing the outcome of a check.
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from beaconops.config.settings import Settings, get_settings
from beaconops.utils.logging import get_logger
from beaconops.utils.validations import (
    ValidationError,
    validate_monitor_url,
    validate_redirect_target,
)

__all__ = [
    "CheckOutcome",
    "HttpCheckResult",
    "HttpClient",
    "HttpClientError",
]

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HttpClientError(RuntimeError):
    """Raised when the HTTP client cannot be constructed or used."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class CheckOutcome(StrEnum):
    """Classification of a single HTTP check outcome."""

    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SSL_ERROR = "ssl_error"
    VALIDATION_ERROR = "validation_error"
    ERROR = "error"


@dataclass(frozen=True)
class HttpCheckResult:
    """Result of a single HTTP check.

    Attributes:
        outcome: The classification of the check outcome.
        status_code: The HTTP status code, when a response was received.
        response_time_ms: Elapsed time in milliseconds, when measurable.
        error_message: A short human-readable error description, when the
            check did not succeed.
        body_excerpt: A truncated excerpt of the response body, when
            available and within the configured size limit.
    """

    outcome: CheckOutcome
    status_code: int | None
    response_time_ms: float | None
    error_message: str | None
    body_excerpt: str | None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})


class HttpClient:
    """Execute HTTP checks against monitored endpoints using HTTPX.

    The client enforces SSRF protections, mandatory timeouts, response
    size limits, and SSL certificate validation. It is safe to reuse a single
    instance across many checks; the underlying :class:`httpx.AsyncClient` is
    created lazily and closed via :meth:`aclose` or :meth:`close`.

    The client never raises for network-level failures: those are reported
    through the returned :class:`HttpCheckResult`. It raises only for
    programming errors (e.g. unsupported HTTP method).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize the client.

        Args:
            settings: Optional settings override. When omitted, the global
                application settings are used.
        """

        self._settings: Settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    # -- Lifecycle ----------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the lazily-created HTTPX async client.

        The client is configured with SSL certificate verification enabled
        by default to prevent man-in-the-middle attacks.
        """

        if self._client is None:
            timeout = httpx.Timeout(self._settings.http_timeout_seconds)
            limits = httpx.Limits(
                max_connections=self._settings.max_concurrent_checks,
                max_keepalive_connections=self._settings.max_concurrent_checks,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                headers={"User-Agent": "BeaconOps-Monitor/0.1"},
                verify=True,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTPX client, if open."""

        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.exception("Error while closing HTTP client")
            finally:
                self._client = None

    def close(self) -> None:
        """Synchronously close the underlying HTTPX client, if open.

        This is a best-effort helper for non-async shutdown paths. Prefer
        :meth:`aclose` in async contexts.
        """

        if self._client is not None:
            try:
                self._client._transport = None  # type: ignore[attr-defined]
            except Exception:
                logger.debug("Unable to release HTTP client transport synchronously")
            finally:
                self._client = None

    # -- Check execution ----------------------------------------------------

    async def check(
        self,
        url: str,
        method: str = "GET",
        timeout_seconds: float | None = None,
        expected_status: int = 200,
        response_contains: str | None = None,
    ) -> HttpCheckResult:
        """Execute a single HTTP check against ``url``.

        Args:
            url: The target URL. Validated against SSRF protections.
            method: The HTTP method to use. Must be one of ``GET``, ``HEAD``,
                ``POST``, ``PUT``, ``PATCH``, or ``DELETE``.
            timeout_seconds: Optional per-check timeout override. When
                omitted, the configured default timeout is used.
            expected_status: The HTTP status code considered successful.
            response_contains: Optional substring that must appear in the
                response body for the check to be considered successful.

        Returns:
            An :class:`HttpCheckResult` describing the outcome.

        Raises:
            HttpClientError: If ``method`` is not supported.
        """

        normalized_method = method.upper()
        if normalized_method not in _ALLOWED_METHODS:
            raise HttpClientError(
                f"Unsupported HTTP method {method!r}; allowed: {sorted(_ALLOWED_METHODS)}"
            )

        try:
            validate_monitor_url(url, settings=self._settings)
        except ValidationError as exc:
            logger.warning("Rejected monitor URL %r: %s", url, exc)
            return HttpCheckResult(
                outcome=CheckOutcome.VALIDATION_ERROR,
                status_code=None,
                response_time_ms=None,
                error_message=f"Invalid URL: {exc}",
                body_excerpt=None,
            )

        timeout = (
            timeout_seconds if timeout_seconds is not None else self._settings.http_timeout_seconds
        )

        client = self._get_client()
        started = time.perf_counter()

        try:
            response = await self._request_with_redirects(
                client=client,
                url=url,
                method=normalized_method,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.warning("Check timed out for URL %r after %.1fms", url, elapsed_ms)
            return HttpCheckResult(
                outcome=CheckOutcome.TIMEOUT,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message="Request timed out",
                body_excerpt=None,
            )
        except ssl.SSLError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.warning(
                "SSL certificate validation failed for URL %r: %s",
                url,
                exc.__class__.__name__,
            )
            return HttpCheckResult(
                outcome=CheckOutcome.SSL_ERROR,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message=f"SSL certificate validation failed: {exc.__class__.__name__}",
                body_excerpt=None,
            )
        except httpx.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.warning("Connection error for URL %r: %s", url, exc.__class__.__name__)
            return HttpCheckResult(
                outcome=CheckOutcome.CONNECTION_ERROR,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message=f"Connection error: {exc.__class__.__name__}",
                body_excerpt=None,
            )
        except ValidationError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.warning("Redirect rejected for URL %r: %s", url, exc)
            return HttpCheckResult(
                outcome=CheckOutcome.VALIDATION_ERROR,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message=f"Redirect rejected: {exc}",
                body_excerpt=None,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.exception("Unexpected error during check for URL %r", url)
            return HttpCheckResult(
                outcome=CheckOutcome.ERROR,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message=f"Unexpected error: {exc.__class__.__name__}",
                body_excerpt=None,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        status_code = response.status_code

        body_excerpt = self._read_body_excerpt(response)

        if status_code != expected_status:
            logger.info(
                "Check for URL %r returned HTTP %s (expected %s)",
                url,
                status_code,
                expected_status,
            )
            return HttpCheckResult(
                outcome=CheckOutcome.HTTP_ERROR,
                status_code=status_code,
                response_time_ms=elapsed_ms,
                error_message=(
                    f"Unexpected HTTP status {status_code} (expected {expected_status})"
                ),
                body_excerpt=body_excerpt,
            )

        if response_contains is not None and response_contains not in (body_excerpt or ""):
            logger.info(
                "Check for URL %r succeeded but body did not contain expected text",
                url,
            )
            return HttpCheckResult(
                outcome=CheckOutcome.HTTP_ERROR,
                status_code=status_code,
                response_time_ms=elapsed_ms,
                error_message="Response body did not contain expected content",
                body_excerpt=body_excerpt,
            )

        logger.debug(
            "Check for URL %r succeeded with HTTP %s in %.1fms",
            url,
            status_code,
            elapsed_ms,
        )
        return HttpCheckResult(
            outcome=CheckOutcome.SUCCESS,
            status_code=status_code,
            response_time_ms=elapsed_ms,
            error_message=None,
            body_excerpt=body_excerpt,
        )

    # -- Internal helpers ---------------------------------------------------

    async def _request_with_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        method: str,
        timeout: float,  # noqa: ASYNC109
    ) -> httpx.Response:
        """Perform a request, following redirects with re-validation.

        Each redirect target is validated against the SSRF protections before
        being followed. The number of redirects is bounded by
        ``max_redirects``.

        Raises:
            ValidationError: If a redirect target is rejected.
            httpx.HTTPError: On network-level failures.
        """

        current_url = url
        current_method = method
        max_redirects = max(0, self._settings.max_redirects)

        for _ in range(max_redirects + 1):
            response = await client.request(
                current_method,
                current_url,
                timeout=timeout,
            )

            if not response.is_redirect:
                return response

            location = response.headers.get("location")
            if not location:
                return response

            next_url = str(response.url.join(location))
            validate_redirect_target(next_url, settings=self._settings)

            # Per RFC 7231, 303 always switches to GET; 301/302 commonly do
            # for non-GET methods in practice. We keep the method for 307/308.
            if response.status_code in (301, 302, 303) and current_method not in (
                "GET",
                "HEAD",
            ):
                current_method = "GET"

            current_url = next_url

        raise ValidationError(f"Exceeded maximum of {max_redirects} redirects")

    def _read_body_excerpt(self, response: httpx.Response) -> str | None:
        """Return a truncated excerpt of the response body, if available.

        The excerpt is bounded by ``http_max_response_bytes`` to avoid
        excessive memory consumption. Binary or undecodable content is
        ignored.
        """

        try:
            content = response.content
        except Exception:
            return None

        if not content:
            return None

        limit = max(0, self._settings.http_max_response_bytes)
        truncated = content[:limit]

        try:
            text = truncated.decode(response.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            return None

        return text
