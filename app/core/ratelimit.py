# app/core/ratelimit.py
"""Per-service rate limiting so we stay inside free-tier quotas."""

import asyncio
from dataclasses import dataclass, field

from aiolimiter import AsyncLimiter

from app.core.logging import get_logger
from app.providers.base import ProviderError

log = get_logger(__name__)

# Longest we will wait for a short-window slot before giving up and letting a fallback answer.
MAX_WAIT_S = 5.0


@dataclass
class Policy:
    """The limits for one service."""

    pacing: list[AsyncLimiter] = field(
        default_factory=list
    )  # short windows: wait, then give up
    quota: AsyncLimiter | None = None  # long window: fail fast when empty


def _build_policies() -> dict[str, Policy]:
    # AsyncLimiter(max_rate, time_period): a bucket that holds `max_rate` units and
    # refills continuously over `time_period` seconds. It starts full, so an initial
    # burst of `max_rate` calls is allowed.
    return {
        # Geoapify: stay under 5 req/s; free plan is ~3,000 credits/day (1 call ~ 1 credit)
        "geoapify": Policy(
            pacing=[AsyncLimiter(4, 1)], quota=AsyncLimiter(3000, 86_400)
        ),
        # RailRadar free tier: 10 requests/min burst, 300 requests/day
        "railradar": Policy(
            pacing=[AsyncLimiter(10, 60)], quota=AsyncLimiter(300, 86_400)
        ),
        # Open-Meteo: ~10,000 calls/day for non-commercial use, no per-minute pacing needed
        "open-meteo": Policy(quota=AsyncLimiter(10_000, 86_400)),
    }


_POLICIES: dict[str, Policy] = _build_policies()


def reset_limiters() -> None:
    """Refill every bucket. Tests call this so one test's usage can't affect the next."""
    _POLICIES.clear()
    _POLICIES.update(_build_policies())


async def acquire(service: str) -> None:
    """Call right before an outbound request. Returns when allowed, raises ProviderError if not."""
    policy = _POLICIES.get(service)
    if policy is None:  # a service with no registered limits is unrestricted
        return

    for limiter in policy.pacing:
        try:
            # Wait for a free slot, but never longer than MAX_WAIT_S
            await asyncio.wait_for(limiter.acquire(), timeout=MAX_WAIT_S)
        except TimeoutError:
            log.warning("rate_limited", service=service, waited_s=MAX_WAIT_S)
            raise ProviderError(
                f"{service}: rate limit reached (waited {MAX_WAIT_S}s)"
            ) from None  # `from None` hides the noisy TimeoutError from the traceback

    if policy.quota is not None:
        # Check immediately before acquiring: if the quota is empty, acquire() would
        # block for hours, so refuse instead and let a fallback provider answer.
        if not policy.quota.has_capacity():
            log.warning("quota_exhausted", service=service)
            raise ProviderError(f"{service}: daily quota exhausted")
        await policy.quota.acquire()  # has capacity, so this returns without waiting
