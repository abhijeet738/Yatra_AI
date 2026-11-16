# app/domain/models.py
"""Pydantic models: the shared contract between the LLM, validator, API and UI."""

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------- money --
class PriceSource(str, Enum):
    """Where a price came from. The UI shows this as a badge on every price."""

    LIVE = "live"  # fetched from a provider just now
    CACHED = "cached"  # fetched earlier, served from cache
    ESTIMATED = "estimated"  # computed by our own formula (e.g. hotel prices)


class Money(BaseModel):
    amount: float = Field(ge=0)  # ge=0 -> negative prices are rejected
    currency: str = "INR"
    source: PriceSource  # required: no price may exist without a source tag


# -------------------------------------------------------------- request --
class TripRequest(BaseModel):
    """What the user asked for, after the LLM has parsed their free text."""

    origin: str
    destinations: list[str] = Field(
        default_factory=list
    )  # empty -> planner picks cities
    start_date: date
    end_date: date
    travellers: int = Field(ge=1, le=12)  # sanity bounds before anything reaches an LLM
    budget_inr: float = Field(gt=0)  # total budget for the whole group
    interests: list[str] = Field(default_factory=list)  # e.g. "forts", "food"
    pace: Literal["relaxed", "moderate", "packed"] = "moderate"
    avoid: list[str] = Field(default_factory=list)  # e.g. "early mornings", "flights"

    @model_validator(mode="after")  # runs after all fields are individually validated
    def check_dates(self) -> "TripRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (
            self.end_date - self.start_date
        ).days + 1 > 30:  # caps quota use per request
            raise ValueError("trips longer than 30 days are not supported")
        return self


# ------------------------------------------------------ provider records --
class TrainOption(BaseModel):
    number: str  # e.g. "12958"
    name: str
    src_code: str  # station codes, e.g. "NDLS"
    dst_code: str
    depart: str  # "HH:MM", 24-hour, zero-padded
    arrive: str
    duration_min: int
    fare: Money | None = None  # None when the source has no fare data


# Opening hours: weekday number (Monday=0 ... Sunday=6) -> list of (open, close) slots.
# A weekday that is missing from the dict means "closed that day".
OpeningHours = dict[int, list[tuple[str, str]]]


class Place(BaseModel):
    """An attraction or venue returned by a places provider. The agent may only use these."""

    id: str  # stable ID; the LLM must reference this, never invent one
    name: str
    city: str
    lat: float
    lon: float
    category: str = "attraction"
    opening_hours: OpeningHours = Field(default_factory=dict)  # empty -> hours unknown
    ticket: Money | None = None
    description: str | None = None

    def is_open(self, day: date, start: str, end: str) -> bool:
        """True if the visit window [start, end] fits inside an opening slot that day."""
        if not self.opening_hours:  # unknown hours: don't block the plan
            return True
        slots = self.opening_hours.get(day.weekday())  # weekday() -> Monday=0
        if not slots:  # weekday not listed -> closed that day
            return False
        # "HH:MM" strings compare correctly as text because they are zero-padded
        return any(opens <= start and end <= closes for opens, closes in slots)


# ------------------------------------------------------------ itinerary --
class Activity(BaseModel):
    place_id: str  # must match a Place.id from research (validator checks this)
    name: str
    start: str  # "HH:MM"
    end: str
    cost: Money
    notes: str | None = None


class TransportLeg(BaseModel):
    mode: str  # "train" | "flight" | "road"
    from_city: str
    to_city: str
    depart: str
    arrive: str
    ref: str | None = None  # train number or flight code
    cost: Money


class DayPlan(BaseModel):
    day: int  # 1-based day number
    date: date
    city: str
    activities: list[Activity]
    transport: list[TransportLeg] = Field(default_factory=list)
    stay_id: str | None = None
    stay_cost: Money | None = None


class Itinerary(BaseModel):
    days: list[DayPlan]
    # Default lets the LLM omit it; our code recomputes the real total afterwards.
    total_cost: Money = Money(amount=0, source=PriceSource.ESTIMATED)
    summary: str


# --- in app/domain/models.py: replace DailyWeather with this version ---
class DailyWeather(BaseModel):
    date: date
    t_max: float  # daytime high, °C
    t_min: float  # night low, °C
    rain_pct: float | None = None  # rain probability; None if the source lacks it
    source: PriceSource = (
        PriceSource.LIVE
    )  # reuses the live/cached/estimated tag; normals -> estimated


# --- add at the bottom of app/domain/models.py ---
class RouteInfo(BaseModel):
    """Travel between two points by road."""

    distance_km: float = Field(ge=0)
    duration_min: int = Field(ge=0)
    source: PriceSource  # live API, cached, or estimated from straight-line distance


class FlightOption(BaseModel):
    code: str  # e.g. "AI 611"
    airline: str
    src_city: str
    dst_city: str
    depart: str  # "HH:MM"
    arrive: str
    duration_min: int
    fare: Money


class Stay(BaseModel):
    id: str  # stable ID the LLM must reference, like Place.id
    name: str
    city: str
    lat: float
    lon: float
    stars: int = Field(ge=1, le=5)
    price_per_night: Money  # per room per night, in INR
