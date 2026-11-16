# app/providers/weather/climate_normals.py
"""Fallback weather: typical conditions per city and month, from a small CSV we build ourselves.

CSV columns (written by scripts/seed_data.py):
    city, lat, lon, month, t_max, t_min, rain_day_pct, years
One row per city per calendar month. rain_day_pct = share of days with >= 0.1 mm of rain.
"""

import csv
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.domain.models import DailyWeather, PriceSource
from app.providers.base import ProviderError

# Farther than this from every city in the file, a "typical month" would be a guess.
MAX_DISTANCE_KM = 150.0


@dataclass(frozen=True)
class _Normal:
    """One CSV row, parsed."""

    city: str
    lat: float
    lon: float
    month: int
    t_max: float
    t_min: float
    rain_pct: float | None


def _approx_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance: accurate to a fraction of a percent at city scale, and 3 lines long."""
    km_per_degree = 111.195
    x = (lon2 - lon1) * math.cos(
        math.radians((lat1 + lat2) / 2)
    )  # longitude degrees shrink with latitude
    y = lat2 - lat1
    return math.hypot(x, y) * km_per_degree


class ClimateNormals:
    name = "climate-normals"

    def __init__(
        self, csv_path: str | Path, max_distance_km: float = MAX_DISTANCE_KM
    ) -> None:
        self.path = Path(csv_path)
        self.max_distance_km = max_distance_km
        # Loaded on first use, NOT here: an empty or missing file must not stop the app booting.
        self._rows: list[_Normal] | None = None

    def _load(self) -> list[_Normal]:
        if (
            self._rows is None
        ):  # a failed load leaves this None, so fixing the file needs no restart
            self._rows = self._read()
        return self._rows

    def _read(self) -> list[_Normal]:
        hint = "run scripts/seed_data.py"
        try:
            with self.path.open(
                newline="", encoding="utf-8"
            ) as f:  # small file: a blocking read is fine
                rows = [
                    _Normal(
                        city=r["city"],
                        lat=float(r["lat"]),
                        lon=float(r["lon"]),
                        month=int(r["month"]),
                        t_max=float(r["t_max"]),
                        t_min=float(r["t_min"]),
                        rain_pct=float(r["rain_day_pct"])
                        if r["rain_day_pct"]
                        else None,
                    )
                    for r in csv.DictReader(f)
                ]
        except OSError as exc:  # missing file, permissions, ...
            raise ProviderError(
                f"{self.name}: cannot read {self.path} ({exc.strerror}); {hint}"
            ) from exc
        except (
            KeyError,
            ValueError,
            TypeError,
        ) as exc:  # missing column or non-numeric value
            raise ProviderError(
                f"{self.name}: {self.path} is malformed: {exc!r}"
            ) from exc
        if not rows:
            raise ProviderError(f"{self.name}: {self.path} has no data rows; {hint}")
        return rows

    async def forecast(
        self, lat: float, lon: float, start: date, end: date
    ) -> list[DailyWeather]:
        if end < start:
            raise ProviderError(f"{self.name}: end {end} is before start {start}")
        rows = self._load()

        # The interface passes coordinates, not a city name, so find the nearest city we have data for
        nearest = min(rows, key=lambda r: _approx_km(lat, lon, r.lat, r.lon))
        distance = _approx_km(lat, lon, nearest.lat, nearest.lon)
        if distance > self.max_distance_km:
            raise ProviderError(
                f"{self.name}: nearest city {nearest.city!r} is {distance:.0f} km away "
                f"(limit {self.max_distance_km:.0f} km)"
            )
        by_month = {r.month: r for r in rows if r.city == nearest.city}

        days: list[DailyWeather] = []
        for n in range((end - start).days + 1):
            day = start + timedelta(days=n)
            normal = by_month.get(day.month)
            if normal is None:
                raise ProviderError(
                    f"{self.name}: no data for {nearest.city!r} in month {day.month}"
                )
            days.append(
                DailyWeather(
                    date=day,
                    t_max=normal.t_max,
                    t_min=normal.t_min,
                    rain_pct=normal.rain_pct,
                    source=PriceSource.ESTIMATED,  # "typical for the season", never a live forecast
                )
            )
        return days
