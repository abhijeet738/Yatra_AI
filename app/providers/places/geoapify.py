# app/providers/places/geoapify.py
"""Live places from Geoapify's Places API (needs GEOAPIFY_API_KEY).

Gives names, coordinates and categories for almost anywhere. Opening hours are present only
where OpenStreetMap volunteers entered them; ticket prices are never available (ticket=None).
"""

import re
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.ratelimit import acquire
from app.domain.models import OpeningHours, Place
from app.providers.base import GeoProvider, ProviderError
from app.providers.http_utils import error_reason, http_retry, scrub
from app.providers.places.curated import (
    _norm,
    parse_hours,
)  # shared helpers (see the response notes)

log = get_logger(__name__)

BASE_URL = "https://api.geoapify.com/v2/places"
SERVICE = "geoapify"  # all Geoapify endpoints share one quota (see core/ratelimit.py)
DEFAULT_RADIUS_M = (
    15_000  # search circle around the city centre; Amber Fort is ~11 km out of Jaipur
)
MAX_FETCH = 100  # most places we ask for in one request

# Geoapify category names we have seen in its documentation. We deliberately ask only for these
# broad ones and sort the results into OUR categories locally (classify below), instead of
# guessing finer Geoapify names that might not exist.
DEFAULT_CATEGORIES = ["tourism", "entertainment.museum"]
_REQUEST_CATEGORY = {
    "museum": "entertainment.museum",
    "food": "catering.restaurant",
}  # all else -> "tourism"


# ----------------------------------------------------------------- classify
# First match wins. \b = word boundary, so "fort" matches "Mehrangarh Fort" but not "Comfort Inn".
_NAME_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), cat)
    for p, cat in [
        (r"\bfort\b|\bqila\b|garh\b", "fort"),
        (r"\bpalace\b|\bmahal\b", "palace"),
        (r"\bhaveli\b", "haveli"),
        (r"\bchurch\b|\bcathedral\b|\bbasilica\b", "church"),
        (
            r"\btemple\b|\bmandir\b|\bgurudwara\b|\bgurdwara\b|\bmosque\b|\bmasjid\b|\bdargah\b",
            "temple",
        ),
        (r"\blake\b|\bsagar\b|\btalab\b|\bsarovar\b", "lake"),
        (r"\bgardens?\b|\bbagh\b|\bpark\b", "garden"),
        (r"\bbazaar\b|\bbazar\b|\bmarket\b", "market"),
        (r"\bbeach\b", "beach"),
        (r"\bwaterfalls?\b|\bfalls\b", "nature"),
        (r"\bstepwell\b|\bbaoli\b|\btomb\b|\bcenotaph\b|\bchhatri\b", "monument"),
    ]
]


def classify(name: str, categories: list[str]) -> str:
    """Our category for a Geoapify result, from its category tags and its name (a heuristic)."""
    tags = " ".join(categories).lower()
    if "entertainment.museum" in tags or re.search(r"\bmuseum\b", name, re.IGNORECASE):
        return "museum"
    if "catering" in tags:
        return "food"
    if "viewpoint" in tags:
        return "viewpoint"
    for pattern, category in _NAME_RULES:
        if pattern.search(name):
            return category
    if "castle" in tags:
        return "fort"
    if "place_of_worship" in tags or "religion" in tags:
        return "temple"  # here "temple" means "place of worship"
    if any(word in tags for word in ("memorial", "monument", "ruin")):
        return "monument"
    return "attraction"


# ------------------------------------------------------------ opening hours
_OSM_DAYS = {
    "Mo": "Mon",
    "Tu": "Tue",
    "We": "Wed",
    "Th": "Thu",
    "Fr": "Fri",
    "Sa": "Sat",
    "Su": "Sun",
}


def osm_to_hours(text: str | None) -> OpeningHours:
    """Convert the simple cases of OpenStreetMap's opening_hours syntax; anything else -> {} (unknown).

    Handled: "24/7", "Mo-Fr 09:00-17:00; Sa 10:00-14:00; Su off", plain "09:00-17:00", and
    "PH ..." segments (public holidays) are ignored. Months, sunrise/sunset, week numbers,
    comments and overnight ranges are NOT handled: the hours are then reported as unknown,
    which the validator treats as "open", so we never wrongly rule a place out.
    """
    if not text or not text.strip():
        return {}
    if text.strip() == "24/7":
        return parse_hours("Daily 00:00-23:59")
    segments: list[str] = []
    try:
        for raw in (s.strip() for s in text.split(";")):
            if not raw:
                continue
            days_part, _, rest = raw.partition(" ")
            if raw[0].isdigit():  # no weekdays given: the times apply every day
                days_part, rest = "Daily", raw
            if days_part.startswith("PH"):
                continue  # public holidays can't be mapped to weekdays
            days = re.sub(
                r"[A-Z][a-z]",
                lambda m: _OSM_DAYS.get(m.group(0), m.group(0)),
                days_part,
            )
            rest = rest.strip().replace("24:00", "23:59")
            segments.append(f"{days} {'closed' if rest.lower() == 'off' else rest}")
        return parse_hours("; ".join(segments))
    except ValueError:  # unsupported syntax (parse_hours reports it as ValueError)
        return {}


# -------------------------------------------------------------------- provider
class GeoapifyPlaces:
    name = "geoapify-places"

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
        self.geo = geo  # turns the city name into coordinates (the Places API needs a position)
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

    async def search(
        self, city: str, categories: list[str], limit: int = 30
    ) -> list[Place]:
        if limit <= 0:
            return []
        if not self._key:  # e.g. a fresh .env: fail clearly so the chain falls back
            raise ProviderError(f"{self.name}: GEOAPIFY_API_KEY is not set")
        lat, lon = await self.geo.coords(
            city
        )  # may raise ProviderError (unknown city, ...)

        wanted = {_norm(c) for c in categories}  # empty set -> no category filter
        asked = (
            sorted({_REQUEST_CATEGORY.get(w, "tourism") for w in wanted})
            or DEFAULT_CATEGORIES
        )
        params = {
            "categories": ",".join(asked),
            # NOTE longitude comes FIRST in Geoapify's filter/bias syntax (the opposite of lat,lon)
            "filter": f"circle:{lon},{lat},{self.radius_m}",
            "bias": f"proximity:{lon},{lat}",  # nearest first
            "conditions": "named",  # skip the many unnamed OpenStreetMap objects
            # Ask for extra: we sort results into our categories locally and some get filtered out
            "limit": str(min(MAX_FETCH, max(limit * 3, 30))),
            "apiKey": self._key,
        }
        data = await self._get(params)
        places = self._parse(data, city)
        if wanted:
            places = [p for p in places if _norm(p.category) in wanted]
        return places[:limit]

    @staticmethod
    def _parse(data: dict[str, Any], city: str) -> list[Place]:
        try:
            features = list(data["features"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                f"{GeoapifyPlaces.name}: unexpected response shape: {exc!r}"
            ) from None

        places: list[Place] = []
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
                place = Place(
                    id=f"geoapify-{props['place_id']}",  # prefix shows where the id came from
                    name=name,
                    city=city,
                    lat=float(props["lat"]),
                    lon=float(props["lon"]),
                    category=classify(name, list(props.get("categories") or [])),
                    opening_hours=osm_to_hours(props.get("opening_hours")),
                    ticket=None,  # Geoapify has no prices: unknown, not zero
                )
            except (
                KeyError,
                TypeError,
                ValueError,
            ):  # one malformed record must not sink the rest
                skipped += 1
                continue
            seen.add(name.casefold())
            places.append(place)
        if skipped:
            log.debug(
                "places_records_skipped", provider="geoapify-places", skipped=skipped
            )
        return places
