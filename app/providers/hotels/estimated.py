# app/providers/hotels/estimated.py
"""Hotel prices that are ESTIMATED and honest about it.

No free source gives real hotel prices, so every price here comes from a formula and is tagged
ESTIMATED. The formula's numbers are ASSUMPTIONS (see the notes on each table), good enough to
keep a plan's budget realistic, not good enough to quote. Real names come from a "directory"
(the next file, geoapify_stays.py); when no directory is available we fall back to generic
"typical N-star hotel" entries so the planner always has something to budget with.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.core.logging import get_logger
from app.domain.models import Money, PriceSource, Stay
from app.providers.base import GeoProvider, ProviderError
from app.providers.geo import normalize

log = get_logger(__name__)

# ---- the formula: price = base(stars) x city x season x weekend, rounded to the nearest 10 rupees ----
# Rupees per room per night. ASSUMPTIONS from the project blueprint; replace them with numbers
# fitted from real observations as soon as you have any.
BASE_BY_STARS = {2: 1500, 3: 2800, 4: 5500, 5: 11000}
# 1.0 for any city not listed. Blueprint guesses: big metros cost more, small pilgrim towns less.
CITY_FACTOR = {
    "Mumbai": 1.4,
    "Delhi": 1.3,
    "Udaipur": 1.15,
    "Jaipur": 1.0,
    "Pushkar": 0.8,
}
PEAK_FACTOR, OFFSEASON_FACTOR = 1.35, 0.9
PEAK_MONTHS = (10, 11, 12, 1, 2)  # the north Indian and Kerala winter season
# Mountains run on the opposite calendar: Ladakh is busy in summer, nearly shut in winter.
PEAK_MONTHS_BY_CITY = {"Leh": (5, 6, 7, 8, 9)}
WEEKEND_FACTOR = (
    1.15  # Friday and Saturday nights (the nights people actually stay for the weekend)
)
DEFAULT_STARS = 3  # a listing with no star rating is assumed to be mid-range

_FACTOR_BY_KEY = {normalize(c): f for c, f in CITY_FACTOR.items()}
_PEAK_BY_KEY = {normalize(c): m for c, m in PEAK_MONTHS_BY_CITY.items()}


def estimate_night_price(city: str, stars: int, night: date) -> Money:
    """Estimated price of one room for the given night, in rupees, tagged ESTIMATED."""
    base = BASE_BY_STARS[
        min(max(stars, 2), 5)
    ]  # price tiers exist for 2-5 stars; 1 prices as 2, 6+ as 5
    key = normalize(city)
    city_factor = _FACTOR_BY_KEY.get(key, 1.0)
    peak = night.month in _PEAK_BY_KEY.get(key, PEAK_MONTHS)
    season = PEAK_FACTOR if peak else OFFSEASON_FACTOR
    weekend = (
        WEEKEND_FACTOR if night.weekday() in (4, 5) else 1.0
    )  # weekday(): Mon=0 ... Fri=4, Sat=5
    return Money(
        amount=round(base * city_factor * season * weekend, -1),
        source=PriceSource.ESTIMATED,
    )


# ---- where the hotel NAMES come from ---------------------------------------------------------
@dataclass(frozen=True)
class Listing:
    """A real place to stay, without a price. A directory (e.g. Geoapify) provides these."""

    id: str
    name: str
    lat: float
    lon: float
    stars: int | None = (
        None  # most directories don't know; None means "assume DEFAULT_STARS"
    )


class ListingSource(Protocol):
    name: str

    async def listings(self, city: str, limit: int) -> list[Listing]: ...


class EstimatedHotels:
    """Implements HotelProvider: real names when a directory has them, estimated prices always."""

    name = "estimated-hotels"

    def __init__(
        self, geo: GeoProvider, directory: ListingSource | None = None
    ) -> None:
        self.geo = geo  # only used for the generic fallback entries
        self.directory = directory  # None = go straight to generic entries

    async def search(self, city: str, night: date, limit: int = 10) -> list[Stay]:
        if limit <= 0:
            return []
        listings: list[Listing] = []
        if self.directory is not None:
            try:
                listings = await self.directory.listings(city, limit)
            except (
                ProviderError
            ) as exc:  # the directory is optional: its failure must not stop planning
                log.warning(
                    "hotel_directory_failed",
                    provider=self.directory.name,
                    error=str(exc),
                )
        if not listings:
            listings = await self._generic(city)

        stays = [
            Stay(
                id=item.id,
                name=item.name,
                city=city,
                lat=item.lat,
                lon=item.lon,
                stars=item.stars or DEFAULT_STARS,
                price_per_night=estimate_night_price(
                    city, item.stars or DEFAULT_STARS, night
                ),
            )
            for item in listings[
                :limit
            ]  # keep the directory's own order for WHICH ones to show...
        ]
        return sorted(
            stays, key=lambda s: (s.price_per_night.amount, s.name)
        )  # ...then cheapest first

    async def _generic(self, city: str) -> list[Listing]:
        """One 'typical' entry per star tier, placed at the city centre. Clearly not a real hotel."""
        lat, lon = await self.geo.coords(
            city
        )  # raises ProviderError for an unknown city
        slug = normalize(city).replace(" ", "-")
        return [
            Listing(
                id=f"generic-{slug}-{stars}star",
                name=f"Typical {stars}-star hotel in {city}",
                lat=lat,
                lon=lon,
                stars=stars,
            )
            for stars in sorted(BASE_BY_STARS)
        ]
