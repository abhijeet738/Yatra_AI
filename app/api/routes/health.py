"""Health and readiness endpoints."""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request, Response

from app.core.logging import get_logger

router = APIRouter()
log = get_logger(__name__)

CHECK_TIMEOUT_S = 2.0
ReadinessCheck = Callable[[], Awaitable[None]]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    checks: dict[str, str] = {}
    readiness_checks: dict[str, ReadinessCheck] = getattr(
        request.app.state, "readiness_checks", {}
    )

    for name, check in readiness_checks.items():
        try:
            await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_S)
        except Exception:
            checks[name] = "fail"
            log.exception("readiness_check_failed", check=name)
        else:
            checks[name] = "ok"

    is_ready = all(status == "ok" for status in checks.values())
    response.status_code = 200 if is_ready else 503
    return {
        "status": "ready" if is_ready else "not_ready",
        "checks": checks,
    }
