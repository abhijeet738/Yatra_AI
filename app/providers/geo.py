# app/providers/geo.py
"""Turn a city name into coordinates: our own city table first, live geocoding for everything else.

Two providers, both satisfying the GeoProvider interface (coords(city) -> (lat, lon)):
  CuratedGeo   - reads data/cities.csv. Offline, instant, knows spelling variants ("Cochin" = Kochi).
  GeoapifyGeo  - Geoapify's geocoding API, for cities that are not in the table.
Chain them as FallbackChain(CuratedGeo(...), CachedProvider(GeoapifyGeo(...), cache, TTL_GEO)).
"""

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.ratelimit import acquire
from app.providers.base import ProviderError
from app.providers.http_utils import error_reason, http_retry, scrub

GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"
SERVICE = "geoapify"  # all Geoapify endpoints share one quota (see core/ratelimit.py)
# The docs' own example treats a confidence below 0.2 as "not confirmed"; we reject those matches.
MIN_CONFIDENCE = 0.2
_REQUIRED_COLUMNS = {"name", "lat", "lon"}  # state and aliases are optional
_EXPECTED_HEADER = "name,state,lat,lon,aliases"


def normalize(name: str) -> str:
    """Lookup key: lower-case, punctuation and extra spaces removed. 'Mt. Abu' == 'mt abu'."""
    return re.sub(
        r"[\W_]+", " ", name.casefold()
    ).strip()  # \W also keeps non-English letters intact


@dataclass(frozen=True)
class City:
    name: str  # the canonical spelling, the one used in data/attractions.csv
    state: str
    lat: float
    lon: float


# ================================================================ our own table
class CuratedGeo:
    """Cities from data/cities.csv: `name,state,lat,lon,aliases` (aliases separated by '|')."""

    name = "curated-cities"

    def __init__(self, csv_path: str | Path) -> None:
        self.path = Path(csv_path)
        self._cities: list[City] | None = (
            None  # loaded on first use, not here (see below)
        )
        self._index: dict[str, City] = {}  # normalised name or alias -> city

    def _load(self) -> dict[str, City]:
        if (
            self._cities is None
        ):  # a failed load stays None, so fixing the file needs no restart
            self._cities, self._index = self._read()
        return self._index

    def _read(self) -> tuple[list[City], dict[str, City]]:
        try:
            with self.path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                columns = set(reader.fieldnames or [])
                rows = list(reader)
        except OSError as exc:
            raise ProviderError(
                f"{self.name}: cannot read {self.path} ({exc.strerror})"
            ) from exc
        missing = _REQUIRED_COLUMNS - columns
        if columns and missing:
            raise ProviderError(
                f"{self.name}: {self.path} is missing column(s) {sorted(missing)}; "
                f"expected header: {_EXPECTED_HEADER}"
            )
        if not rows:
            raise ProviderError(f"{self.name}: {self.path} has no data rows")

        cities: list[City] = []
        index: dict[str, City] = {}
        for line_no, row in enumerate(rows, start=2):  # line 1 is the header
            try:
                city = City(
                    name=row["name"].strip(),
                    state=(row.get("state") or "").strip(),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
                if not city.name:
                    raise ValueError("empty name")
                if not (
                    math.isfinite(city.lat)
                    and math.isfinite(city.lon)
                    and -90 <= city.lat <= 90
                    and -180 <= city.lon <= 180
                ):
                    raise ValueError(
                        f"coordinates out of range ({city.lat}, {city.lon})"
                    )
                keys = {normalize(city.name)} | {
                    normalize(a)
                    for a in (row.get("aliases") or "").split("|")
                    if a.strip()
                }
                for key in keys:
                    if (
                        index.get(key, city) != city
                    ):  # same spelling already points at another city
                        raise ValueError(
                            f"{key!r} already belongs to {index[key].name!r}"
                        )
                    index[key] = city
            except (KeyError, ValueError, TypeError) as exc:
                raise ProviderError(
                    f"{self.name}: {self.path} line {line_no} ({row.get('name')!r}): "
                    f"{type(exc).__name__}: {exc}"
                ) from None
            cities.append(city)
        return cities, index

    def _lookup(self, name: str) -> City | None:
        index = self._load()
        found = index.get(normalize(name))
        if found is None and "," in name:  # "Jaipur, Rajasthan" -> try just "Jaipur"
            found = index.get(normalize(name.split(",", 1)[0]))
        return found

    def canonical(self, name: str) -> str | None:
        """The canonical spelling for a name or alias ('Cochin' -> 'Kochi'), or None if unknown."""
        city = self._lookup(name)
        return city.name if city else None

    def names(self) -> list[str]:
        """Every canonical city name, in file order (handy for telling the LLM which cities we know)."""
        self._load()
        return [c.name for c in self._cities or []]

    async def coords(self, city: str) -> tuple[float, float]:
        found = self._lookup(city)
        if found is None:
            raise ProviderError(
                f"{self.name}: unknown city {city!r}"
            )  # the chain moves on to live geocoding
        return (found.lat, found.lon)


# ================================================================ live geocoding
class GeoapifyGeo:
    name = "geoapify-geocoding"

    def __init__(
        self, client: httpx.AsyncClient, api_key: str, country_code: str | None = "in"
    ) -> None:
        self.client = client
        self._key = (
            api_key  # underscore: internal. It must never appear in logs or errors.
        )
        self.country_code = (
            country_code  # restrict matches to one country; None = worldwide
        )

    @http_retry()
    async def _fetch(self, params: dict[str, str]) -> dict[str, Any]:
        await acquire(
            SERVICE
        )  # inside the retry: every attempt spends quota, so every attempt checks it
        r = await self.client.get(GEOAPIFY_URL, params=params, timeout=10)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def coords(self, city: str) -> tuple[float, float]:
        text = city.strip()
        if not text:
            raise ProviderError(f"{self.name}: empty city name")
        if not self._key:  # e.g. a fresh .env: fail clearly
            raise ProviderError(f"{self.name}: GEOAPIFY_API_KEY is not set")
        params = {
            "text": text,
            "type": "locality",  # cities, districts and states, but not streets or shops
            "limit": "1",  # results come sorted by relevance; we only want the best
            "format": "json",  # the default is GeoJSON
            "apiKey": self._key,
        }
        if self.country_code:
            # Without this the API prefers results near the SERVER's country, so "Pushkar" or
            # "Jaipur" could resolve to a namesake abroad.
            params["filter"] = f"countrycode:{self.country_code}"

        # Every `from None` is deliberate: the original exception's text contains the request URL,
        # key included, and `from exc` would print it in tracebacks and logs.
        try:
            data = await self._fetch(params)
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
        return self._parse(data, city)

    @staticmethod
    def _parse(data: dict[str, Any], city: str) -> tuple[float, float]:
        name = GeoapifyGeo.name
        try:
            results = data["results"]
            if not results:
                raise ProviderError(f"{name}: no match for {city!r}")
            best = results[0]
            lat, lon = float(best["lat"]), float(best["lon"])
            confidence = (best.get("rank") or {}).get(
                "confidence"
            )  # 0..1; may be absent
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"{name}: unexpected response shape: {exc!r}") from None
        if confidence is not None and float(confidence) < MIN_CONFIDENCE:
            raise ProviderError(
                f"{name}: low-confidence match for {city!r} (confidence {confidence})"
            )
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            raise ProviderError(f"{name}: out-of-range coordinates for {city!r}")
        return (lat, lon)
