# app/providers/base.py
"""Provider interfaces. Agents depend on these shapes, never on a specific data source."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.domain.models import (
    DailyWeather,
    FlightOption,
    Place,
    RouteInfo,
    Stay,
    TrainOption,
)


class ProviderError(Exception):
    """Raised when a provider (or a whole fallback chain) cannot answer."""


# Each interface is a typing.Protocol: any class with matching methods satisfies it
# automatically ("structural typing"), so real, cached, mock and fallback providers
# can all stand in for each other without sharing a base class.
# `name` is used in logs ("provider_ok provider=open-meteo") and in cache keys.


class GeoProvider(Protocol):
    name: str

    async def coords(self, city: str) -> tuple[float, float]:
        """Return (latitude, longitude) for a city name."""
        ...


class WeatherProvider(Protocol):
    name: str

    async def forecast(
        self, lat: float, lon: float, start: date, end: date
    ) -> list[DailyWeather]:
        """One DailyWeather per day from start to end inclusive."""
        ...


class PlacesProvider(Protocol):
    name: str

    async def search(
        self, city: str, categories: list[str], limit: int = 30
    ) -> list[Place]:
        """Attractions in a city. Every Place.id returned is one the LLM may legally use."""
        ...


class RoutingProvider(Protocol):
    name: str

    async def route(
        self, src_lat: float, src_lon: float, dst_lat: float, dst_lon: float
    ) -> RouteInfo:
        """Road distance and duration between two points."""
        ...


class TrainProvider(Protocol):
    name: str

    async def between(
        self, src_code: str, dst_code: str, on: date
    ) -> list[TrainOption]:
        """Trains from one station code to another on a given date."""
        ...


class FlightProvider(Protocol):
    name: str

    async def between(
        self, src_city: str, dst_city: str, on: date
    ) -> list[FlightOption]:
        """Flights between two cities on a given date (mock in this project)."""
        ...


class HotelProvider(Protocol):
    name: str

    async def search(self, city: str, night: date, limit: int = 10) -> list[Stay]:
        """Stays for one night in a city, each with a price tagged live or estimated."""
        ...


@dataclass(frozen=True)  # frozen: the bundle can't be reassigned after startup
class Providers:
    """Everything the agent nodes need, passed around as one object."""

    geo: GeoProvider
    weather: WeatherProvider
    places: PlacesProvider
    routing: RoutingProvider
    trains: TrainProvider
    flights: FlightProvider
    hotels: HotelProvider
