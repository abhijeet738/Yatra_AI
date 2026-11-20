# app/providers/registry.py
"""Fallback chains. (File 12 will add build_providers(), which assembles every provider here.)"""

import asyncio
from collections import Counter
from collections.abc import Sized
from typing import Any

from app.core.logging import get_logger
from app.providers.base import ProviderError

log = get_logger(__name__)

# Attribute names that belong to the chain itself, never forwarded to the providers.
_OWN_ATTRS = {
    "providers",
    "skip_empty",
    "timeout_s",
    "answered",
    "failures",
    "last_provider",
}


def _is_empty(result: Any) -> bool:
    """None, [], "" and {} count as empty. A (lat, lon) tuple or a number does not."""
    return result is None or (isinstance(result, Sized) and len(result) == 0)


class FallbackChain:
    """Tries providers in order and returns the first answer.

    Any provider method can be called on the chain, e.g. chain.forecast(...). The chain asks
    the first provider; if it raises (timeout, 429, 5xx, bad payload, rate limit) or runs
    out of time, it asks the next. If every provider fails, ProviderError lists each failure.

    skip_empty: treat an empty result ([] / None) as "no answer, try the next one". Use it
        where one source may simply not cover a place. If every provider answers empty, the
        empty result is returned (that is a legitimate answer, not a failure).
    timeout_s: optional cap on EACH provider call, so one slow source can't eat the whole
        request's time budget.

    Typing note: like CachedProvider, methods are forwarded dynamically, so build_providers()
    `cast`s the chain back to the right Protocol.
    """

    def __init__(
        self, *providers: Any, skip_empty: bool = False, timeout_s: float | None = None
    ) -> None:
        if not providers:
            raise ValueError("FallbackChain needs at least one provider")
        self.providers = providers
        self.skip_empty = skip_empty
        self.timeout_s = timeout_s
        self.name = " > ".join(
            p.name for p in providers
        )  # e.g. "cached:open-meteo > climate-normals"
        # Counters feed the "provider failures and fallbacks" metric in the eval report.
        self.answered: Counter[str] = (
            Counter()
        )  # provider name -> times it supplied the answer
        self.failures: Counter[str] = Counter()  # provider name -> times it failed
        self.last_provider: str | None = None  # who answered the most recent call

    def __getattr__(self, method: str) -> Any:
        # Only runs when normal lookup fails. Refuse private/own names so copy or pickle
        # (which look up attributes before __init__ has run) can't recurse forever.
        if method.startswith("_") or method in _OWN_ATTRS:
            raise AttributeError(method)
        # Every provider must have the method: a wiring mistake fails loudly here, at first
        # use, instead of being swallowed later as just another "provider failed".
        for p in self.providers:
            if not callable(getattr(p, method, None)):
                raise AttributeError(f"provider {p.name!r} has no method {method!r}")
        call = self._make_call(method)
        setattr(self, method, call)  # store it, so the next access skips __getattr__
        return call

    def _make_call(self, method: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            errors: list[str] = []
            saw_empty = False
            empty_result: Any = None
            for depth, provider in enumerate(self.providers):  # depth 0 = the primary
                try:
                    awaitable = getattr(provider, method)(*args, **kwargs)
                    if self.timeout_s is not None:
                        awaitable = asyncio.wait_for(awaitable, self.timeout_s)
                    result = await awaitable
                except (
                    Exception
                ) as exc:  # broad on purpose: odd payloads should fall back too
                    self.failures[provider.name] += 1
                    # type name first: a bare TimeoutError() has an empty message
                    reason = f"{type(exc).__name__}: {exc}"
                    errors.append(f"{provider.name}: {reason}")
                    log.warning(
                        "provider_failed",
                        provider=provider.name,
                        method=method,
                        error=reason,
                    )
                    continue  # try the next provider
                if self.skip_empty and _is_empty(result):
                    saw_empty, empty_result = True, result
                    log.info("provider_empty", provider=provider.name, method=method)
                    continue
                self.answered[provider.name] += 1
                self.last_provider = provider.name
                log.info(
                    "provider_ok",
                    provider=provider.name,
                    method=method,
                    fallback_depth=depth,
                )
                return result
            if (
                saw_empty
            ):  # nobody failed to answer; the honest answer is "nothing found"
                return empty_result
            raise ProviderError(f"{self.name}.{method} failed: " + "; ".join(errors))

        return call
