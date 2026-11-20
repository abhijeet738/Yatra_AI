# app/providers/routing/geoapify_routing.py
"""Road distance and drive time from Geoapify's Routing API (needs GEOAPIFY_API_KEY)."""

import math
from typing import Any

import httpx

from app.core.ratelimit import acquire
from app.domain.models import PriceSource, RouteInfo
from app.providers.base import ProviderError
from app.providers.http_utils import error_reason, http_retry

BASE_URL = "https://api.geoapify.com/v1/routing"
SERVICE = "geoapify"  # all Geoapify endpoints share one quota (see core/ratelimit.py)


class GeoapifyRouting:
    name = "geoapify-routing"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self.client = client
        self._key = (
            api_key  # underscore: internal. It must never appear in logs or errors.
        )

    def _scrub(self, text: str) -> str:
        """Geoapify takes the key as a URL parameter, and httpx error text can contain the URL."""
        return text.replace(self._key, "***") if self._key else text

    @http_retry()
    async def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        await acquire(
            SERVICE
        )  # inside the retry: every attempt spends quota, so every attempt checks it
        r = await self.client.get(BASE_URL, params=params, timeout=10)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def route(
        self, src_lat: float, src_lon: float, dst_lat: float, dst_lon: float
    ) -> RouteInfo:
        if (
            not self._key
        ):  # e.g. a fresh .env: fail clearly so the chain falls back to the estimate
            raise ProviderError(f"{self.name}: GEOAPIFY_API_KEY is not set")
        params = {
            # Geoapify wants "lat,lon|lat,lon": waypoints in visit order, separated by a pipe
            "waypoints": f"{src_lat},{src_lon}|{dst_lat},{dst_lon}",
            "mode": "drive",
            "format": "json",  # the default is GeoJSON; plain JSON is easier to read
            "apiKey": self._key,
        }
        # NOTE every `from None` below is deliberate: the original exception's text contains the
        # request URL, key included, and `from exc` would print it in tracebacks and logs.
        try:
            data = await self._fetch(params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = " (check GEOAPIFY_API_KEY)" if status in (401, 403) else ""
            raise ProviderError(
                f"{self.name}: HTTP {status}: {self._scrub(error_reason(exc.response))}{hint}"
            ) from None
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: network error: {type(exc).__name__}"
            ) from None
        except ValueError:  # 200 OK, but the body was not valid JSON
            raise ProviderError(f"{self.name}: response was not valid JSON") from None
        return self._parse(data)

    @staticmethod
    def _parse(data: dict[str, Any]) -> RouteInfo:
        try:
            if "results" in data:  # the shape for format=json
                result = data["results"][0]
            else:  # GeoJSON shape, as a safety net in case the API answers with its default
                result = data["features"][0]["properties"]
            distance_m = float(result["distance"])  # docs: metres
            time_s = float(result["time"])  # docs: seconds
            return RouteInfo(
                distance_km=round(distance_m / 1000, 1),
                duration_min=math.ceil(time_s / 60),  # round UP, like the estimate does
                source=PriceSource.LIVE,
            )
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            raise ProviderError(
                f"{GeoapifyRouting.name}: unexpected response shape: {exc!r}"
            ) from None
