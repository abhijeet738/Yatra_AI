# app/providers/hotels/geoapify_stays.py
"""Real hotel NAMES from Geoapify's Places API (needs GEOAPIFY_API_KEY). Prices come from estimated.py.

Implements the ListingSource interface that EstimatedHotels uses as its "directory".
"""

import re
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.ratelimit import acquire
from app.providers.base import GeoProvider, ProviderError
from app.providers.hotels.estimated import Listing
from app.providers.http_utils import error_reason, http_retry, scrub

log = get_logger(__name__)

BASE_URL = "https://api.geoapify.com/v2/places"
SERVICE = "geoapify"  # all Geoapify endpoints share one quota (see core/ratelimit.py)
DEFAULT_RADIUS_M = 10_000  # stays near the centre are what a tourist wants
MAX_FETCH = 100
# Category names from Geoapify's published category list (checked against its OpenAPI spec).
CATEGORIES = "accommodation.guest_house,accommodation.hostel,accommodation.hotel"
# An unrated hostel or guest house is a budget place: price it like 2 stars instead of the 3-star default.
BUDGET_TAGS = ("accommodation.hostel", "accommodation.guest_house")


def _dig(obj: Any, *path: str) -> Any:
    """obj[path[0]][path[1]]... or None if any step is missing or not a dict."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def parse_stars(props: dict[str, Any]) -> int | None:
    """A 1-5 star rating if the place data has one, else None.

    OpenStreetMap's 'stars' tag is free text ("4", "3S", "4.5"). We look in the raw OSM tags and
    in a possible 'accommodation' details block. Whether Geoapify exposes stars for any given
    hotel is NOT something the docs promise, so None (unrated) is a normal outcome.
    """
    for raw in (
        _dig(props, "accommodation", "stars"),
        _dig(props, "datasource", "raw", "stars"),
    ):
        if raw is None:
            continue
        match = re.match(r"\s*(\d+(?:\.\d+)?)", str(raw))
        if match:
            stars = int(float(match.group(1)))  # "4.5" -> 4: we never round a rating up
            if 1 <= stars <= 5:  # OSM data also has junk like 0 or 7
                return stars
    return None


class GeoapifyStays:
    name = "geoapify-stays"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        geo: GeoProvider,
        radius_m: int = DEFAULT_RADIUS_M,
    ) -> None:
        self.client = client
        self._key = (
            api_key  # underscore: internal. It must never appear in logs or errors.
        )
        self.geo = geo  # city name -> coordinates (the Places API needs a position)
        self.radius_m = radius_m

    @http_retry()
    async def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        await acquire(
            SERVICE
        )  # inside the retry: every attempt spends quota, so every attempt checks it
        r = await self.client.get(BASE_URL, params=params, timeout=15)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        # Every `from None` is deliberate: the original exception's text contains the request URL,
        # key included, and `from exc` would print it in tracebacks and logs.
        try:
            return await self._fetch(params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = " (check GEOAPIFY_API_KEY)" if status in (401, 403) else ""
            raise ProviderError(
                f"{self.name}: HTTP {status}: {scrub(error_reason(exc.response), self._key)}{hint}"
            ) from None
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: network error: {type(exc).__name__}"
            ) from None
        except ValueError:  # 200 OK, but the body was not valid JSON
            raise ProviderError(f"{self.name}: response was not valid JSON") from None

    async def listings(self, city: str, limit: int) -> list[Listing]:
        if limit <= 0:
            return []
        if not self._key:  # e.g. a fresh .env: fail clearly so EstimatedHotels falls back to generic entries
            raise ProviderError(f"{self.name}: GEOAPIFY_API_KEY is not set")
        lat, lon = await self.geo.coords(
            city
        )  # may raise ProviderError (unknown city, ...)
        params = {
            "categories": CATEGORIES,
            # NOTE longitude comes FIRST in Geoapify's filter/bias syntax (the opposite of lat,lon)
            "filter": f"circle:{lon},{lat},{self.radius_m}",
            "bias": f"proximity:{lon},{lat}",  # nearest first
            "conditions": "named",
            "limit": str(
                min(MAX_FETCH, max(limit * 3, 30))
            ),  # extra, because duplicates get dropped
            "apiKey": self._key,
        }
        data = await self._get(params)
        return self._parse(data)[:limit]

    @staticmethod
    def _parse(data: dict[str, Any]) -> list[Listing]:
        try:
            features = list(data["features"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                f"{GeoapifyStays.name}: unexpected response shape: {exc!r}"
            ) from None

        listings: list[Listing] = []
        seen: set[str] = set()
        skipped = 0
        for feature in features:
            try:
                props = feature["properties"]
                name = str(props.get("name") or "").strip()
                if (
                    not name or name.casefold() in seen
                ):  # unnamed, or an OSM duplicate (nearest wins)
                    skipped += 1
                    continue
                stars = parse_stars(props)
                tags = " ".join(props.get("categories") or [])
                if stars is None and any(tag in tags for tag in BUDGET_TAGS):
                    stars = 2
                item = Listing(
                    id=f"geoapify-{props['place_id']}",  # prefix shows where the id came from
                    name=name,
                    lat=float(props["lat"]),
                    lon=float(props["lon"]),
                    stars=stars,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
            ):  # one malformed record must not sink the rest
                skipped += 1
                continue
            seen.add(name.casefold())
            listings.append(item)
        if skipped:
            log.debug(
                "stays_records_skipped", provider="geoapify-stays", skipped=skipped
            )
        return listings
