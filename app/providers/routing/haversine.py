# app/providers/routing/haversine.py
"""Fallback routing: estimate road distance and travel time from straight-line distance.

Needs no network and no API key, so it always answers. Its results are tagged ESTIMATED.
"""

import math

from app.domain.models import PriceSource, RouteInfo
from app.providers.base import ProviderError

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius

# --- Tunable assumptions (NOT measured values) -------------------------------------------
# Roads wind, so the drive is longer than the straight line. Typical ratio is 1.2-1.4.
ROAD_FACTOR = 1.25
# Average speed over a whole trip, including slow stretches and stops.
AVG_SPEED_KMH = 45.0
# Fixed time added to any real trip: leaving, parking, finding the entrance.
OVERHEAD_MIN = 5
# Closer than this, two points count as the same spot: no travel needed.
SAME_SPOT_KM = 0.1


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (straight-line over the Earth's surface) distance between two points, in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)  # the formula works in radians
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    # min(1.0, ...) guards against float rounding pushing the root's argument just past 1
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _check(lat: float, lon: float) -> None:
    # `not (-90 <= lat <= 90)` is also True for NaN, which a plain `lat > 90` test would miss
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ProviderError(f"haversine-estimate: invalid coordinates ({lat}, {lon})")


def estimate_route(
    src_lat: float,
    src_lon: float,
    dst_lat: float,
    dst_lon: float,
    *,
    road_factor: float = ROAD_FACTOR,
    speed_kmh: float = AVG_SPEED_KMH,
    overhead_min: int = OVERHEAD_MIN,
) -> RouteInfo:
    """Synchronous version, so plain code (e.g. the validator) can use it without `await`."""
    _check(src_lat, src_lon)
    _check(dst_lat, dst_lon)
    straight = haversine_km(src_lat, src_lon, dst_lat, dst_lon)
    if straight < SAME_SPOT_KM:
        return RouteInfo(distance_km=0.0, duration_min=0, source=PriceSource.ESTIMATED)
    road_km = straight * road_factor
    # ceil: round UP, so we under-promise. A too-short estimate would let the validator
    # accept a schedule that is actually impossible.
    minutes = math.ceil(overhead_min + road_km / speed_kmh * 60)
    return RouteInfo(
        distance_km=round(road_km, 1),
        duration_min=minutes,
        source=PriceSource.ESTIMATED,
    )


class HaversineRouting:
    name = "haversine-estimate"

    def __init__(
        self,
        road_factor: float = ROAD_FACTOR,
        speed_kmh: float = AVG_SPEED_KMH,
        overhead_min: int = OVERHEAD_MIN,
    ) -> None:
        self.road_factor = road_factor
        self.speed_kmh = speed_kmh
        self.overhead_min = overhead_min

    async def route(
        self, src_lat: float, src_lon: float, dst_lat: float, dst_lon: float
    ) -> RouteInfo:
        # async only to satisfy the RoutingProvider interface; the work itself is instant
        return estimate_route(
            src_lat,
            src_lon,
            dst_lat,
            dst_lon,
            road_factor=self.road_factor,
            speed_kmh=self.speed_kmh,
            overhead_min=self.overhead_min,
        )
