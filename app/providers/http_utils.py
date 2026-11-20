# app/providers/http_utils.py
"""Small helpers shared by the providers that call HTTP APIs: one retry policy, one way to read errors."""

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def is_retryable(exc: BaseException) -> bool:
    """Retry network glitches and server-side (5xx) errors. Never 4xx (bad key, bad input, rate limit)."""
    if isinstance(exc, httpx.TransportError):  # timeouts, connection resets, DNS failures
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def http_retry() -> Any:
    """Decorator for the one method that makes the request: up to 3 tries, 1s then 2s apart.

    reraise=True surfaces the real error instead of tenacity's RetryError wrapper.
    (Returns Any because tenacity's own typing for decorators is awkward to repeat here.)
    """
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    )


def error_reason(response: httpx.Response) -> str:
    """The API's own explanation: Open-Meteo sends {"reason": ...}, Geoapify {"message": ...}."""
    try:
        body = response.json()
        return str(body.get("reason") or body.get("message") or response.text[:200])
    except Exception:  # body wasn't JSON (or wasn't an object)
        return response.text[:200]


def scrub(text: str, secret: str) -> str:
    """Hide an API key that travels in the URL: httpx error text can contain the full URL."""
    return text.replace(secret, "***") if secret else text