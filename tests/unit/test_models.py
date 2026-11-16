# tests/unit/test_models.py
"""Unit tests for app/domain/models.py: validation rules and Place.is_open()."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.domain.models import Itinerary, Money, Place, PriceSource, TripRequest

MONDAY = date(2026, 11, 16)    # weekday() == 0
TUESDAY = date(2026, 11, 17)   # weekday() == 1


def make_request(**overrides) -> TripRequest:
    """Build a valid TripRequest; each test overrides only the field it cares about."""
    data = dict(
        origin="Delhi",
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 5),
        travellers=2,
        budget_inr=40000,
    )
    data.update(overrides)  # a test's own values win over the defaults above
    return TripRequest(**data)


def make_place(**overrides) -> Place:
    """A fort that is open 08:00-17:30 on Mondays only (weekday 0)."""
    data = dict(
        id="p1", name="Amer Fort", city="Jaipur", lat=26.98, lon=75.85,
        opening_hours={0: [("08:00", "17:30")]},
    )
    data.update(overrides)
    return Place(**data)


# ------------------------------------------------------------ TripRequest --
def test_valid_request_gets_defaults():
    req = make_request()
    assert req.pace == "moderate"           # default pace applies
    assert req.destinations == []           # empty list -> planner will choose cities
    assert req.avoid == []


def test_end_date_before_start_is_rejected():
    with pytest.raises(ValidationError):    # the date validator raises ValueError -> ValidationError
        make_request(start_date=date(2026, 12, 5), end_date=date(2026, 12, 1))


def test_trip_of_30_days_is_allowed_but_31_is_not():
    make_request(start_date=date(2026, 12, 1), end_date=date(2026, 12, 30))  # exactly 30 days: ok
    with pytest.raises(ValidationError):
        make_request(start_date=date(2026, 12, 1), end_date=date(2026, 12, 31))  # 31 days: too long


@pytest.mark.parametrize("travellers", [0, 13])  # just outside the 1..12 bounds
def test_traveller_count_out_of_range_is_rejected(travellers):
    with pytest.raises(ValidationError):
        make_request(travellers=travellers)


def test_unknown_pace_is_rejected():
    with pytest.raises(ValidationError):    # Literal type only allows relaxed/moderate/packed
        make_request(pace="frantic")


def test_zero_budget_is_rejected():
    with pytest.raises(ValidationError):    # budget_inr must be strictly greater than 0
        make_request(budget_inr=0)


# ------------------------------------------------------------------ Money --
def test_negative_money_is_rejected():
    with pytest.raises(ValidationError):
        Money(amount=-1, source=PriceSource.LIVE)


# ---------------------------------------------------------- Place.is_open --
def test_open_inside_slot():
    assert make_place().is_open(MONDAY, "09:00", "11:00")


def test_open_when_visit_exactly_fills_the_slot():
    assert make_place().is_open(MONDAY, "08:00", "17:30")   # boundaries are inclusive


def test_closed_when_visit_starts_before_opening():
    assert not make_place().is_open(MONDAY, "07:30", "09:00")


def test_closed_when_visit_ends_after_closing():
    assert not make_place().is_open(MONDAY, "16:00", "18:00")


def test_closed_on_weekday_not_listed():
    assert not make_place().is_open(TUESDAY, "09:00", "11:00")  # only Monday is in the dict


def test_unknown_hours_never_block_the_plan():
    assert make_place(opening_hours={}).is_open(TUESDAY, "03:00", "04:00")  # empty -> assume open


# -------------------------------------------------------------- Itinerary --
def test_itinerary_total_defaults_to_zero_estimated():
    it = Itinerary(days=[], summary="empty")   # LLM may omit total_cost
    assert it.total_cost.amount == 0
    assert it.total_cost.source == PriceSource.ESTIMATED