# app/domain/pricing.py
"""Cost arithmetic. The LLM never does sums: code does (the 'LLM decides, code calculates' rule)."""

from app.domain.models import Itinerary, Money, PriceSource

# Lower rank = more trustworthy. A total takes the WORST (highest-rank) source of its parts.
_SOURCE_RANK = {PriceSource.LIVE: 0, PriceSource.CACHED: 1, PriceSource.ESTIMATED: 2}


def _all_costs(it: Itinerary) -> list[tuple[str, Money]]:
    """Flatten every price in the itinerary into (category, Money) pairs."""
    items: list[tuple[str, Money]] = []
    for day in it.days:
        items += [("activities", a.cost) for a in day.activities]  # entry tickets, meals, etc.
        items += [("transport", t.cost) for t in day.transport]    # trains, flights, road legs
        if day.stay_cost is not None:                              # not every day has a stay
            items.append(("stays", day.stay_cost))
    return items


def _weakest_source(items: list[tuple[str, Money]]) -> PriceSource:
    if not items:                       # nothing to sum: a total of 0 is our own figure
        return PriceSource.ESTIMATED
    # max() by rank picks the least trustworthy source among all the parts
    return max((m.source for _, m in items), key=lambda s: _SOURCE_RANK[s])


def cost_breakdown(it: Itinerary) -> dict[str, float]:
    """Totals per category, each rounded to paise so the parts add up exactly."""
    out = {"activities": 0.0, "transport": 0.0, "stays": 0.0}
    for category, money in _all_costs(it):
        out[category] += money.amount
    return {k: round(v, 2) for k, v in out.items()}  # round each part once, here


def recompute_total(it: Itinerary) -> Money:
    """The real trip total. Overwrites whatever total the LLM wrote."""
    items = _all_costs(it)
    # Sum the already-rounded category figures so total == sum(breakdown) with no drift
    total = round(sum(cost_breakdown(it).values()), 2)
    return Money(amount=total, source=_weakest_source(items))


def per_person(total: Money, travellers: int) -> Money:
    """Split a group total evenly. Source tag carries over from the total."""
    if travellers < 1:                  # guard: avoids a ZeroDivisionError further down
        raise ValueError("travellers must be at least 1")
    return Money(amount=round(total.amount / travellers, 2), source=total.source)