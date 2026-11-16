# tests/unit/test_pricing.py
"""Unit tests for app/domain/pricing.py: totals, breakdown, source tagging, per-person split."""

from datetime import date

import pytest

from app.domain.models import (
    Activity,
    DayPlan,
    Itinerary,
    Money,
    PriceSource,
    TransportLeg,
)
from app.domain.pricing import cost_breakdown, per_person, recompute_total

LIVE, CACHED, ESTIMATED = PriceSource.LIVE, PriceSource.CACHED, PriceSource.ESTIMATED


def money(amount: float, source: PriceSource = LIVE) -> Money:
    """Shorthand so test lines stay short."""
    return Money(amount=amount, source=source)


def make_day(day=1, activities=(), transport=(), stay=None) -> DayPlan:
    """Build one DayPlan from lists of Money values; times/names are irrelevant to pricing."""
    return DayPlan(
        day=day,
        date=date(2026, 12, day),
        city="Jaipur",
        activities=[
            Activity(place_id=f"p{i}", name=f"A{i}", start="10:00", end="11:00", cost=c)
            for i, c in enumerate(activities)
        ],
        transport=[
            TransportLeg(
                mode="train",
                from_city="Delhi",
                to_city="Jaipur",
                depart="06:00",
                arrive="10:00",
                cost=c,
            )
            for c in transport
        ],
        stay_id="h1" if stay else None,
        stay_cost=stay,  # None means no overnight stay that day
    )


def make_itinerary(*days: DayPlan) -> Itinerary:
    return Itinerary(days=list(days), summary="test")


# ------------------------------------------------------------- breakdown --
def test_breakdown_splits_costs_by_category():
    it = make_itinerary(
        make_day(
            activities=[money(500), money(300)],
            transport=[money(1200)],
            stay=money(3000),
        )
    )
    assert cost_breakdown(it) == {
        "activities": 800.0,
        "transport": 1200.0,
        "stays": 3000.0,
    }


def test_breakdown_adds_across_multiple_days():
    it = make_itinerary(
        make_day(day=1, activities=[money(100)], stay=money(2000)),
        make_day(day=2, activities=[money(250)], stay=money(2000)),
    )
    breakdown = cost_breakdown(it)
    assert breakdown["activities"] == 350.0
    assert breakdown["stays"] == 4000.0


def test_day_without_stay_adds_nothing_to_stays():
    it = make_itinerary(make_day(activities=[money(100)], stay=None))
    assert cost_breakdown(it)["stays"] == 0.0


# ----------------------------------------------------------------- total --
def test_total_equals_sum_of_breakdown():
    it = make_itinerary(
        make_day(
            activities=[money(500), money(300)],
            transport=[money(1200)],
            stay=money(3000),
        )
    )
    assert recompute_total(it).amount == sum(cost_breakdown(it).values()) == 5000.0


def test_total_ignores_whatever_total_the_llm_wrote():
    it = make_itinerary(make_day(activities=[money(100)]))
    it.total_cost = money(999999)  # a wrong figure from the LLM
    assert recompute_total(it).amount == 100.0  # recompute_total never reads it


def test_floating_point_noise_is_rounded_away():
    it = make_itinerary(make_day(activities=[money(0.1), money(0.2)]))
    assert (
        recompute_total(it).amount == 0.3
    )  # plain 0.1 + 0.2 would be 0.30000000000000004


def test_empty_itinerary_totals_zero_and_is_estimated():
    total = recompute_total(make_itinerary())
    assert total.amount == 0
    assert total.source == ESTIMATED  # nothing fetched, so no "live" claim


# --------------------------------------------------------- source tagging --
def test_all_live_parts_give_a_live_total():
    it = make_itinerary(
        make_day(activities=[money(100, LIVE)], transport=[money(200, LIVE)])
    )
    assert recompute_total(it).source == LIVE


def test_cached_part_downgrades_total_to_cached():
    it = make_itinerary(
        make_day(activities=[money(100, LIVE)], transport=[money(200, CACHED)])
    )
    assert recompute_total(it).source == CACHED


def test_one_estimated_part_makes_the_whole_total_estimated():
    it = make_itinerary(
        make_day(
            activities=[money(100, LIVE)],
            transport=[money(200, CACHED)],
            stay=money(3000, ESTIMATED),
        )
    )
    assert recompute_total(it).source == ESTIMATED  # the weakest source wins


# ------------------------------------------------------------ per_person --
def test_per_person_splits_evenly_and_keeps_source():
    result = per_person(money(5000, ESTIMATED), 2)
    assert result.amount == 2500.0
    assert result.source == ESTIMATED


def test_per_person_rounds_to_paise():
    assert per_person(money(100), 3).amount == 33.33  # 33.333... rounded to 2 decimals


@pytest.mark.parametrize("travellers", [0, -1])
def test_per_person_rejects_fewer_than_one_traveller(travellers):
    with pytest.raises(ValueError):  # guards against ZeroDivisionError
        per_person(money(100), travellers)
