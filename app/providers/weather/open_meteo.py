# app/providers/weather/open_meteo.py
"""Live weather from Open-Meteo (free, no API key). Implements the WeatherProvider interface."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.ratelimit import acquire
from app.domain.models import DailyWeather, PriceSource
from app.providers.base import ProviderError

BASE_URL = "https://api.open-meteo.com/v1/forecast"
SERVICE = "open-meteo"  # the key this provider uses in core/ratelimit.py
# The three daily values our DailyWeather model needs
DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_probability_max"
# Docs: forecast_days goes up to 16 -> today plus the next 15 days is the furthest we can ask for
MAX_FORECAST_DAYS = 16
# India has no daylight saving time, so a fixed UTC+05:30 offset is exact (and needs no tz database)
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    """'Today' as Open-Meteo sees it for an Indian location. A function so tests can replace it."""
    return datetime.now(IST).date()


def _is_retryable(exc: BaseException) -> bool:
    """Retry network glitches and server-side (5xx) errors. Never retry 4xx: it won't change."""
    if isinstance(
        exc, httpx.TransportError
    ):  # timeouts, connection resets, DNS failures
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def _reason(response: httpx.Response) -> str:
    """Open-Meteo errors look like {"error": true, "reason": "..."}; fall back to raw text."""
    try:
        return str(response.json()["reason"])
    except Exception:
        return response.text[:200]


class OpenMeteoWeather:
    name = "open-meteo"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = (
            client  # shared client created in main.py's lifespan (connection pooling)
        )

    @retry(
        stop=stop_after_attempt(3),  # at most 3 tries in total
        wait=wait_exponential(min=1, max=8),  # pause 1s, then 2s, ... between tries
        retry=retry_if_exception(_is_retryable),
        reraise=True,  # surface the real error, not tenacity's RetryError
    )
    async def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        await acquire(
            SERVICE
        )  # inside the retry: every attempt spends quota, so every attempt checks it
        r = await self.client.get(BASE_URL, params=params, timeout=10)
        r.raise_for_status()  # 4xx/5xx -> HTTPStatusError
        data: dict[str, Any] = r.json()
        return data

    async def forecast(
        self, lat: float, lon: float, start: date, end: date
    ) -> list[DailyWeather]:
        # The return annotation matters: CachedProvider uses it to rebuild results from JSON.
        if end < start:
            raise ProviderError(f"{self.name}: end {end} is before start {start}")
        last_day = today_ist() + timedelta(days=MAX_FORECAST_DAYS - 1)
        if end > last_day:
            # Don't even call the API: it would answer with an error and we'd waste quota.
            # The fallback chain then uses climate normals for the whole range.
            raise ProviderError(
                f"{self.name}: {end} is beyond the forecast horizon ({last_day})"
            )

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": DAILY_VARS,
            "start_date": start.isoformat(),  # yyyy-mm-dd, as the API expects
            "end_date": end.isoformat(),
            "timezone": "auto",  # required when asking for daily values; auto = from the coordinates
        }
        try:
            data = await self._fetch(params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ProviderError(
                f"{self.name}: HTTP {status}: {_reason(exc.response)}"
            ) from exc
        except httpx.TransportError as exc:  # still failing after all retries
            raise ProviderError(f"{self.name}: network error: {exc!r}") from exc
        return self._parse(data, start, end)

    @staticmethod
    def _parse(data: dict[str, Any], start: date, end: date) -> list[DailyWeather]:
        try:
            daily = data["daily"]
            times = daily["time"]
            # The probability array can be missing or contain nulls; DailyWeather allows None
            rain = daily.get("precipitation_probability_max") or [None] * len(times)
            rows = zip(
                times, daily["temperature_2m_max"], daily["temperature_2m_min"], rain
            )
            days = [
                DailyWeather(
                    date=t, t_max=hi, t_min=lo, rain_pct=p, source=PriceSource.LIVE
                )
                for t, hi, lo, p in rows
                if hi is not None
                and lo is not None  # a day without temperatures is unusable
            ]
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as exc:  # ValueError covers pydantic's ValidationError
            raise ProviderError(
                f"{OpenMeteoWeather.name}: unexpected response shape: {exc!r}"
            ) from exc

        # Never return a partial answer silently: the chain should fall back to normals instead
        wanted = {start + timedelta(days=n) for n in range((end - start).days + 1)}
        if {d.date for d in days} != wanted:
            raise ProviderError(
                f"{OpenMeteoWeather.name}: response did not cover {start}..{end}"
            )
        return sorted(days, key=lambda d: d.date)
