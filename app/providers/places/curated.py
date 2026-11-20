# app/providers/places/curated.py
"""Places from our own hand-curated CSV (data/attractions.csv). Works offline, hours are reliable.

CSV columns:
    id, name, city, lat, lon, category, hours, ticket_inr, description
- id:         stable slug, e.g. "jaipur-amber-fort" (the LLM must use exactly this)
- category:   one lower-case word, e.g. fort, palace, museum, temple, market, food, lake
- hours:      see parse_hours() below; blank = unknown (treated as "always open")
- ticket_inr: per person, Indian adult rate, in rupees; 0 = free; blank = unknown
Rows are ordered by importance: when `limit` cuts the list, the first rows win.
"""

import csv
import re
from pathlib import Path

from app.domain.models import Money, OpeningHours, Place, PriceSource
from app.providers.base import ProviderError

_REQUIRED_COLUMNS = {
    "id",
    "name",
    "city",
    "lat",
    "lon",
    "category",
}  # hours, ticket_inr, description are optional
_EXPECTED_HEADER = "id,name,city,lat,lon,category,hours,ticket_inr,description"
_DAY_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}  # same as date.weekday()
_TIME_RANGE = re.compile(
    r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$"
)  # "9:30-17:00" or "09:30-17:00"


def _day(name: str) -> int:
    try:
        return _DAY_INDEX[name.strip().lower()[:3]]  # [:3] also accepts "Monday"
    except KeyError:
        raise ValueError(f"unknown day {name!r}") from None


def _days(spec: str) -> list[int]:
    """'Daily', 'Mon-Fri', 'Sat-Mon' (wraps over the weekend) or 'Mon,Wed,Sat' -> weekday numbers."""
    if spec.strip().lower() == "daily":
        return list(range(7))
    days: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            first, last = (_day(p) for p in part.split("-", 1))
            span = (
                last - first
            ) % 7  # the modulo makes Sat-Mon work: 5 -> 0 is a span of 2
            days += [(first + i) % 7 for i in range(span + 1)]
        else:
            days.append(_day(part))
    return days


def _slot(text: str) -> tuple[str, str]:
    m = _TIME_RANGE.match(text.strip())
    if not m:
        raise ValueError(f"bad time range {text!r} (expected HH:MM-HH:MM)")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
        raise ValueError(f"impossible time in {text!r}")
    opens, closes = (
        f"{h1:02d}:{m1:02d}",
        f"{h2:02d}:{m2:02d}",
    )  # zero-padded, so text comparison works
    if opens >= closes:
        raise ValueError(
            f"{text!r}: opening must be before closing (overnight hours are not supported)"
        )
    return opens, closes


def parse_hours(text: str) -> OpeningHours:
    """Parse a hours string into Place.opening_hours (weekday number -> list of (open, close)).

    Grammar: segments separated by ';'. Each segment is DAYS then TIMES, separated by a space.
        "Daily 08:00-17:30"
        "Mon-Fri 09:00-12:00,14:00-17:00; Sat 10:00-13:00"   (a split-shift day has several slots)
        "Daily 09:00-17:00; Fri closed"                       (later segments override earlier ones)
    A weekday that ends up with no slots is closed. Blank text = hours unknown -> {}.
    """
    hours: OpeningHours = {}
    for segment in (s.strip() for s in text.split(";")):
        if not segment:
            continue
        days_part, _, rest = segment.partition(" ")
        days = _days(days_part)
        rest = rest.strip().lower()
        if rest == "closed":
            for d in days:
                hours.pop(d, None)
            continue
        if not rest:
            raise ValueError(f"segment {segment!r} has no times")
        slots = [_slot(t) for t in rest.split(",")]
        for d in days:
            hours[d] = list(slots)
    return hours


def _norm(word: str) -> str:
    """Crude singular form, so the interest 'forts' matches the category 'fort'."""
    w = word.strip().lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"  # galleries -> gallery
    if w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return w[:-2]  # beaches -> beach
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]  # forts -> fort, palaces -> palace
    return w


class CuratedPlaces:
    name = "curated-csv"

    def __init__(self, csv_path: str | Path) -> None:
        self.path = Path(csv_path)
        # Loaded on first use, not here, so a missing/broken file can't stop the app booting.
        self._places: list[Place] | None = None

    def _load(self) -> list[Place]:
        if (
            self._places is None
        ):  # a failed load stays None, so fixing the file needs no restart
            self._places = self._read()
        return self._places

    def _read(self) -> list[Place]:
        try:
            with self.path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                columns = set(reader.fieldnames or [])
                rows = list(reader)
        except OSError as exc:
            raise ProviderError(
                f"{self.name}: cannot read {self.path} ({exc.strerror})"
            ) from exc
        # Check the header first: an old or wrong file should say so, not fail on its first row
        missing = _REQUIRED_COLUMNS - columns
        if columns and missing:
            raise ProviderError(
                f"{self.name}: {self.path} is missing column(s) {sorted(missing)}; "
                f"expected header: {_EXPECTED_HEADER}"
            )
        if not rows:
            raise ProviderError(f"{self.name}: {self.path} has no data rows")

        places: list[Place] = []
        seen: set[str] = set()
        for line_no, row in enumerate(rows, start=2):  # line 1 is the header
            try:
                place = self._to_place(row)
                if place.id in seen:
                    raise ValueError(f"duplicate id {place.id!r}")
            except (
                KeyError,
                ValueError,
                TypeError,
            ) as exc:  # pydantic's ValidationError is a ValueError
                detail = str(exc).splitlines()[0][:150]
                raise ProviderError(
                    f"{self.name}: {self.path} line {line_no} ({row.get('name')!r}): "
                    f"{type(exc).__name__}: {detail}"
                ) from None
            seen.add(place.id)
            places.append(place)
        return places

    @staticmethod
    def _to_place(row: dict[str, str]) -> Place:
        place_id = row["id"].strip()
        if not place_id:
            raise ValueError("empty id")
        ticket_text = (row.get("ticket_inr") or "").strip()
        return Place(
            id=place_id,
            name=row["name"].strip(),
            city=row["city"].strip(),
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            category=row["category"].strip().lower(),
            opening_hours=parse_hours(row.get("hours") or ""),
            # Hand-entered prices are never "live": ESTIMATED keeps the UI badge honest
            ticket=Money(amount=float(ticket_text), source=PriceSource.ESTIMATED)
            if ticket_text
            else None,
            description=(row.get("description") or "").strip() or None,
        )

    async def search(
        self, city: str, categories: list[str], limit: int = 30
    ) -> list[Place]:
        places = self._load()
        wanted_city = (
            city.strip().casefold()
        )  # casefold = a stricter lower(): "Jaipur" == " jaipur "
        wanted = {_norm(c) for c in categories}  # empty set -> no category filter
        matches = [
            p
            for p in places
            if p.city.casefold() == wanted_city
            and (not wanted or _norm(p.category) in wanted)
        ]
        # Copies, so a caller who changes a result can't corrupt our in-memory data for the next call
        return [p.model_copy(deep=True) for p in matches[: max(limit, 0)]]
