# app/providers/trains/railradar.py
"""Train timetables from RailRadar's "trains between stations" API (needs RAILRADAR_API_KEY).

Timetable data only: the endpoint gives times, durations and the days a train runs, but NO fares
and no seat availability. (RailRadar is an independent service, not Indian Railways: always tell
the user to confirm travel details with official sources.)
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.core.ratelimit import acquire
from app.domain.models import TrainOption
from app.providers.base import ProviderError
from app.providers.http_utils import error_reason, http_retry, scrub

BASE_URL = "https://api.railradar.in/v1/trains/between"
SERVICE = "railradar"  # free tier: 300 requests/day, 10/minute (see core/ratelimit.py)

_STATION = re.compile(
    r"[A-Za-z0-9]{1,10}"
)  # the docs allow 1-10 characters; we also forbid "/" and friends
_TIME = re.compile(r"\d{2}:\d{2}")
_DAY_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}  # same as date.weekday()


@dataclass(frozen=True)
class TrainRun:
    """One train's timetable entry between two stations, with the extra fields a timetable needs."""

    number: str
    name: str
    type: str  # e.g. "Mail/Express", "Rajdhani Express"
    src_code: str
    dst_code: str
    depart: str  # "HH:MM" at the source station
    arrive: str  # "HH:MM" at the destination; earlier than depart means a later day
    duration_min: (
        int  # the API's own figure: use it, not arrive - depart, for overnight trains
    )
    run_days: tuple[
        int, ...
    ]  # weekdays (Mon=0) as the API reports them; empty if it gave none
    depart_day: (
        int  # journey day number at the source station (1 = the day the train starts)
    )
    distance_km: (
        float | None
    )  # rail distance between the two stations, if the API gave it (useful for fare estimates)


class RailRadarTrains:
    name = "railradar"

    def __init__(self, client: httpx.AsyncClient, api_key: str | None) -> None:
        self.client = client
        self._key = (
            api_key or ""
        )  # None/empty -> every call fails clearly and the chain falls back

    @http_retry()
    async def _fetch(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        await acquire(
            SERVICE
        )  # inside the retry: every attempt spends quota, so every attempt checks it
        # The key goes in a header, not the URL, so it can't show up in request logs
        r = await self.client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=10,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def _search(self, src: str, dst: str, on: date | None) -> list[TrainRun]:
        if not self._key:
            raise ProviderError(f"{self.name}: RAILRADAR_API_KEY is not set")
        src, dst = (
            src.strip().upper(),
            dst.strip().upper(),
        )  # the API is case-insensitive; we normalise
        if not (_STATION.fullmatch(src) and _STATION.fullmatch(dst)):
            raise ProviderError(
                f"{self.name}: invalid station code ({src!r} -> {dst!r})"
            )
        if src == dst:
            return []  # the API answers 400 for this; "no trains" is the honest answer

        # With a date the API returns only trains at the source station on that date; without one, all trains
        params = {"date": on.isoformat()} if on else {}
        try:
            body = await self._fetch(f"{BASE_URL}/{src}/{dst}", params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            hint = " (check RAILRADAR_API_KEY)" if status in (401, 403) else ""
            if status == 404:
                hint = " (unknown station code?)"
            raise ProviderError(
                f"{self.name}: HTTP {status}: {scrub(error_reason(exc.response), self._key)}{hint}"
            ) from None  # from None: keeps the original exception (and anything in it) out of tracebacks
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name}: network error: {type(exc).__name__}"
            ) from None
        except ValueError:  # 200 OK, but the body was not valid JSON
            raise ProviderError(f"{self.name}: response was not valid JSON") from None
        return self._parse(body)

    @staticmethod
    def _parse(body: dict[str, Any]) -> list[TrainRun]:
        name = RailRadarTrains.name
        try:
            if (
                body.get("success") is False
            ):  # the API wraps errors in the same envelope
                raise ProviderError(f"{name}: the API reported an error")
            trains = list(body["data"]["trains"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(f"{name}: unexpected response shape: {exc!r}") from None

        runs: list[TrainRun] = []
        for entry in trains:
            try:
                train, origin, target = entry["train"], entry["from"], entry["to"]
                depart, arrive = str(origin["departure"]), str(target["arrival"])
                if not (_TIME.fullmatch(depart) and _TIME.fullmatch(arrive)):
                    raise ValueError("bad time format")
                run = TrainRun(
                    number=str(train["number"]),
                    name=str(train["name"]),
                    type=str(train.get("type") or ""),
                    src_code=str(origin["code"]).upper(),
                    dst_code=str(target["code"]).upper(),
                    depart=depart,
                    arrive=arrive,
                    duration_min=int(entry["duration"]),
                    run_days=tuple(
                        sorted(
                            {
                                _DAY_INDEX[d]
                                for d in train.get("runDays", [])
                                if d in _DAY_INDEX
                            }
                        )
                    ),
                    depart_day=int(origin.get("day") or 1),
                    distance_km=float(entry["distance"])
                    if entry.get("distance") is not None
                    else None,
                )
                if run.duration_min <= 0:
                    raise ValueError("non-positive duration")
            except (
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
            ):  # one odd entry must not sink the rest
                continue
            runs.append(run)
        return sorted(runs, key=lambda r: (r.depart, r.number))

    async def between(
        self, src_code: str, dst_code: str, on: date
    ) -> list[TrainOption]:
        """The TrainProvider interface: trains that actually run on this date. Fares are unknown (None)."""
        runs = await self._search(src_code, dst_code, on)
        return [
            TrainOption(
                number=r.number,
                name=r.name,
                src_code=r.src_code,
                dst_code=r.dst_code,
                depart=r.depart,
                arrive=r.arrive,
                duration_min=r.duration_min,
                fare=None,
            )
            for r in runs
        ]

    async def timetable(self, src_code: str, dst_code: str) -> list[TrainRun]:
        """Every train between two stations regardless of day, with run days. Used to build the fallback file."""
        return await self._search(src_code, dst_code, None)
