# app/providers/flights/mock_flights.py
"""Mock flights: believable, repeatable, and clearly fake. There is no good free flight API, so
the agent runs against this and the real thing can replace it later behind the same interface.

Everything it returns is invented: the airline names are fictional on purpose and every fare is
tagged ESTIMATED. The same (seed, route, date) always gives the same answer, in any order.
"""

import hashlib
import random
from datetime import date

from app.domain.models import FlightOption, Money, PriceSource
from app.providers.geo import normalize
from app.providers.routing.haversine import haversine_km

# The mock's own little world: cities that have an airport, with approximate city-centre
# coordinates. Deliberately self-contained so eval runs never depend on other data files.
_AIRPORT_CITIES: dict[str, tuple[float, float]] = {
    "Delhi": (28.6139, 77.2090),
    "Mumbai": (19.0760, 72.8777),
    "Bengaluru": (12.9716, 77.5946),
    "Chennai": (13.0827, 80.2707),
    "Kolkata": (22.5726, 88.3639),
    "Hyderabad": (17.3850, 78.4867),
    "Ahmedabad": (23.0225, 72.5714),
    "Pune": (18.5204, 73.8567),
    "Goa": (15.3808, 73.8314),
    "Jaipur": (26.9124, 75.7873),
    "Jodhpur": (26.2389, 73.0243),
    "Udaipur": (24.5854, 73.7125),
    "Kochi": (9.9312, 76.2673),
    "Thiruvananthapuram": (8.5241, 76.9366),
    "Leh": (34.1526, 77.5771),
}
_ALIASES = {  # other spellings -> the name used above
    "new delhi": "Delhi",
    "bombay": "Mumbai",
    "bangalore": "Bengaluru",
    "madras": "Chennai",
    "calcutta": "Kolkata",
    "cochin": "Kochi",
    "ernakulam": "Kochi",
    "trivandrum": "Thiruvananthapuram",
}
_BY_KEY = {normalize(n): n for n in _AIRPORT_CITIES} | {
    normalize(a): n for a, n in _ALIASES.items()
}

# Fictional on purpose: nobody should mistake these for real schedules.
_AIRLINES = [("Mock Air", "MA"), ("Demo Jet", "DJ"), ("Sample Airways", "SA")]

MIN_FLIGHT_KM = (
    400  # closer than this, people drive or take the train: no flights offered
)
CRUISE_KMH = 700  # average speed in the air
GROUND_MIN = 35  # taxi, take-off and landing on top of the cruise time
BASE_FARE_INR = 2200  # fare = base + per-km, then +/- 15-35% per flight
FARE_PER_KM_INR = 2.2
FIRST_DEPARTURE_MIN = 5 * 60 + 30  # 05:30
LAST_ARRIVAL_MIN = (
    23 * 60 + 30
)  # 23:30: every flight lands the same day (the model has no "+1 day")


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _pick(rng: random.Random, n: int) -> int:
    """A whole number from 0 to n-1, built ONLY from rng.random().

    Python promises that random() gives the same sequence for the same seed on every version, but
    NOT for helpers like randint, choice or sample. Using only random() keeps the "repeatable"
    mock data identical on Python 3.11, 3.12 and later, so recorded eval results stay valid.
    """
    return int(rng.random() * n)


class MockFlights:
    name = "mock-flights"

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def _rng(self, src: str, dst: str, on: date) -> random.Random:
        # hashlib, NOT Python's hash(): hash() of a string changes on every run (security feature),
        # which would make "repeatable" mock data different each time the program starts.
        digest = hashlib.sha256(
            f"{self.seed}|{src}|{dst}|{on.isoformat()}".encode()
        ).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    async def between(
        self, src_city: str, dst_city: str, on: date
    ) -> list[FlightOption]:
        src, dst = _BY_KEY.get(normalize(src_city)), _BY_KEY.get(normalize(dst_city))
        if src is None or dst is None or src == dst:
            return []  # no airport in the mock world, or the same city: an honest "no flights"
        km = haversine_km(*_AIRPORT_CITIES[src], *_AIRPORT_CITIES[dst])
        if km < MIN_FLIGHT_KM:
            return []

        rng = self._rng(
            src, dst, on
        )  # seeded per route and date, so call order never matters
        duration = round(km / CRUISE_KMH * 60) + GROUND_MIN
        latest_departure = (
            LAST_ARRIVAL_MIN - duration
        )  # so the flight lands before midnight
        count = 2 + _pick(rng, 3)  # 2 to 4 flights
        slots = (
            latest_departure - FIRST_DEPARTURE_MIN
        ) // 5 + 1  # possible departure times, 5 minutes apart
        used: set[int] = set()
        options: list[FlightOption] = []
        for _ in range(count):
            number = 100 + _pick(rng, 800)
            while number in used:  # keep flight numbers distinct within the day
                number = 100 + _pick(rng, 800)
            used.add(number)
            airline, prefix = _AIRLINES[_pick(rng, len(_AIRLINES))]
            depart = FIRST_DEPARTURE_MIN + 5 * _pick(rng, slots)
            fare = (BASE_FARE_INR + FARE_PER_KM_INR * km) * (
                0.85 + 0.5 * rng.random()
            )  # 0.85x to 1.35x
            options.append(
                FlightOption(
                    code=f"{prefix} {number}",
                    airline=airline,
                    src_city=src,  # canonical names, so "Cochin" and "Kochi" give identical results
                    dst_city=dst,
                    depart=_hhmm(depart),
                    arrive=_hhmm(depart + duration),
                    duration_min=duration,
                    fare=Money(amount=round(fare, -1), source=PriceSource.ESTIMATED),
                )
            )
        return sorted(options, key=lambda o: o.depart)
