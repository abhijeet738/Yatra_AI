# scripts/seed_data.py
"""Build data/climate_normals.csv from Open-Meteo's historical archive.

Run from the repo root:   uv run python scripts/seed_data.py
Needs internet. One request per city; cities already in the CSV are skipped, so a run that
stops half way (e.g. rate limit) can simply be started again.
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import fmean
from typing import Any

import httpx

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"  # historical reanalysis, 1940 -> now
DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum"
RAIN_THRESHOLD_MM = 0.1  # a "rainy day" = at least 0.1 mm, the same threshold Open-Meteo's forecast uses
FIELDS = ["city", "lat", "lon", "month", "t_max", "t_min", "rain_day_pct", "years"]
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "climate_normals.csv"

# The cities (and their coordinates) live in ONE place: data/cities.csv. Add a row there and
# rerun this script: only the new city is downloaded.
DEFAULT_CITIES = Path(__file__).resolve().parent.parent / "data" / "cities.csv"


class SeedError(Exception):
    """Something went wrong fetching or reading data; the message says what to do."""


def load_cities(path: Path) -> dict[str, tuple[float, float]]:
    """City name -> (lat, lon), read from data/cities.csv (columns name, lat, lon are used)."""
    try:
        with path.open(newline="", encoding="utf-8") as f:
            cities = {
                row["name"].strip(): (float(row["lat"]), float(row["lon"]))
                for row in csv.DictReader(f)
            }
    except OSError as exc:
        raise SeedError(f"cannot read {path} ({exc.strerror})") from exc
    except (KeyError, ValueError) as exc:  # missing column, or a non-numeric coordinate
        raise SeedError(f"{path} is malformed: {exc!r}") from exc
    if not cities:
        raise SeedError(f"{path} has no cities")
    return cities


def fetch_city(
    client: httpx.Client, lat: float, lon: float, start_year: int, end_year: int
) -> dict[str, Any]:
    """Download daily history for one location; returns the response's "daily" block."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": f"{start_year}-01-01",
        "end_date": f"{end_year}-12-31",
        "daily": DAILY_VARS,
        "timezone": "auto",
    }
    for attempt in range(1, 4):  # up to 3 tries for network errors and 5xx
        try:
            r = client.get(
                ARCHIVE_URL, params=params, timeout=60
            )  # ten years of days: allow a slow reply
        except httpx.TransportError as exc:
            if attempt == 3:
                raise SeedError(f"network error: {exc!r}") from exc
            time.sleep(2 * attempt)
            continue
        if r.status_code == 429:
            raise SeedError(
                "rate limited by Open-Meteo (HTTP 429); wait a while and rerun"
            )
        if r.status_code >= 500 and attempt < 3:
            time.sleep(2 * attempt)
            continue
        if r.status_code != 200:
            raise SeedError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            daily: dict[str, Any] = r.json()["daily"]
            for key in (
                "time",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
            ):
                daily[key]
        except (KeyError, ValueError) as exc:
            raise SeedError(f"unexpected response shape: {exc!r}") from exc
        return daily
    raise SeedError(
        "gave up after 3 attempts"
    )  # only reachable if every attempt was a 5xx


def aggregate_months(daily: dict[str, list[Any]]) -> dict[int, dict[str, float | None]]:
    """Turn daily history into one average row per calendar month (1-12)."""
    highs: dict[int, list[float]] = defaultdict(list)
    lows: dict[int, list[float]] = defaultdict(list)
    rainy: dict[int, list[bool]] = defaultdict(list)
    rows = zip(
        daily["time"],
        daily["temperature_2m_max"],
        daily["temperature_2m_min"],
        daily["precipitation_sum"],
    )
    for day, hi, lo, rain in rows:
        month = int(day[5:7])  # "2024-03-15" -> 3
        if hi is not None:  # the archive can contain gaps (null); skip those days
            highs[month].append(hi)
        if lo is not None:
            lows[month].append(lo)
        if rain is not None:
            rainy[month].append(rain >= RAIN_THRESHOLD_MM)

    result: dict[int, dict[str, float | None]] = {}
    for month in range(1, 13):
        if not highs[month] or not lows[month]:
            raise SeedError(f"no temperature data at all for month {month}")
        result[month] = {
            "t_max": round(fmean(highs[month]), 1),
            "t_min": round(fmean(lows[month]), 1),
            # share of rainy days, as a percentage; None (blank in the CSV) if no rain data existed
            "rain_day_pct": round(100 * sum(rainy[month]) / len(rainy[month]), 1)
            if rainy[month]
            else None,
        }
    return result


def read_existing(path: Path) -> list[dict[str, Any]]:
    """Rows already in the CSV (so finished cities are kept). Missing/empty file -> no rows."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not set(FIELDS) <= set(reader.fieldnames or []):
            print(f"note: {path} has unexpected columns; starting from scratch")
            return []
        return [dict(row) for row in reader]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write via a temp file + rename, so a crash mid-write can't leave a half-written CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["city"], int(r["month"]))))
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build data/climate_normals.csv from Open-Meteo history."
    )
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="how many past full years to average (default 10)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output CSV path")
    parser.add_argument(
        "--cities",
        type=Path,
        default=DEFAULT_CITIES,
        help="city list (default data/cities.csv)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download cities that are already in the file",
    )
    args = parser.parse_args(argv)

    try:
        cities = load_cities(args.cities)
    except SeedError as exc:
        print(f"Stopped: {exc}")
        return 1

    end_year = date.today().year - 1  # last COMPLETE year
    start_year = end_year - args.years + 1
    rows = read_existing(args.out)
    have = {r["city"] for r in rows}
    todo = [c for c in cities if args.refresh or c not in have]
    if not todo:
        print(
            f"Nothing to do: all {len(cities)} cities are already in {args.out} (use --refresh to redo them)."
        )
        return 0

    print(f"Fetching {start_year}-{end_year} for {len(todo)} cities...")
    with httpx.Client() as client:
        for i, city in enumerate(todo, 1):
            lat, lon = cities[city]
            print(f"[{i}/{len(todo)}] {city} ...", end=" ", flush=True)
            try:
                daily = fetch_city(client, lat, lon, start_year, end_year)
                months = aggregate_months(daily)
            except SeedError as exc:
                print("failed")
                print(
                    f"Stopped: {exc}\nCities finished so far are saved; rerun to continue."
                )
                return 1
            years = len(
                {d[:4] for d in daily["time"]}
            )  # how many distinct years were really averaged
            rows = [
                r for r in rows if r["city"] != city
            ]  # replace any old rows for this city
            rows += [
                {
                    "city": city,
                    "lat": lat,
                    "lon": lon,
                    "month": m,
                    "years": years,
                    **vals,
                }
                for m, vals in months.items()
            ]
            write_rows(
                args.out, rows
            )  # save after EVERY city, so progress survives a failure
            print("ok")
            time.sleep(1.0)  # be polite to a free API
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
