# scripts/seed_trains.py
"""Build data/trains_sample.csv, the offline fallback train timetable, from RailRadar.

Run from the repo root:   uv run python scripts/seed_trains.py
Needs RAILRADAR_API_KEY in .env and internet. One request per station pair, and the free tier
allows 300 a day, so a run does at most --max-requests (default 100) and the next run continues
where this one stopped. Pairs already done are skipped.

Which pairs: the primary station of each city in data/stations.csv, between cities in the same
state, plus every pair that involves Delhi (the hub). Use --all-pairs for everything.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `import app...` works when run as a script

import httpx  # noqa: E402

from app.core import ratelimit  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.providers.trains.railradar import RailRadarTrains, TrainRun  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
FIELDS = ["number", "name", "type", "src_code", "dst_code", "depart", "arrive",
          "duration_min", "distance_km", "run_days", "depart_day"]
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # index = weekday number (Mon=0)
HUB_CITY = "Delhi"  # pairs with this city are always included, whatever the state
DEFAULT_MAX_REQUESTS = 100


class SeedError(Exception):
    """A problem with the input files; the message says what to fix."""


# ------------------------------------------------------------------ input files
def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError as exc:
        raise SeedError(f"cannot read {path} ({exc.strerror})") from exc


def load_primary_stations(path: Path) -> dict[str, str]:
    """City -> its primary station code, from data/stations.csv."""
    try:
        primary = {r["city"].strip(): r["code"].strip().upper() for r in read_csv(path) if r["primary"].strip() == "yes"}
    except KeyError as exc:
        raise SeedError(f"{path} is missing column {exc}; expected city,code,name,primary,note") from exc
    if not primary:
        raise SeedError(f"{path} has no primary stations")
    return primary


def load_states(path: Path) -> dict[str, str]:
    """City -> state, from data/cities.csv."""
    try:
        return {r["name"].strip(): r["state"].strip() for r in read_csv(path)}
    except KeyError as exc:
        raise SeedError(f"{path} is missing column {exc}") from exc


def select_pairs(primary: dict[str, str], states: dict[str, str], all_pairs: bool = False) -> list[tuple[str, str]]:
    """Directed (from, to) station pairs worth fetching, in a stable order."""
    by_code: dict[str, str] = {}  # primary station code -> the first city that uses it
    for city, code in primary.items():
        by_code.setdefault(code, city)
    pairs = []
    for src, city_a in by_code.items():
        for dst, city_b in by_code.items():
            if src == dst:
                continue
            same_state = states.get(city_a) == states.get(city_b)
            involves_hub = HUB_CITY in (city_a, city_b)
            if all_pairs or same_state or involves_hub:
                pairs.append((src, dst))
    return sorted(pairs)


# ------------------------------------------------------------------ output files
def train_row(run: TrainRun) -> dict[str, Any]:
    return {
        "number": run.number, "name": run.name, "type": run.type,
        "src_code": run.src_code, "dst_code": run.dst_code,
        "depart": run.depart, "arrive": run.arrive, "duration_min": run.duration_min,
        "distance_km": "" if run.distance_km is None else run.distance_km,
        "run_days": "|".join(DAY_NAMES[d] for d in run.run_days),  # e.g. "mon|wed|fri"; blank = unknown
        "depart_day": run.depart_day,
    }


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not set(FIELDS) <= set(reader.fieldnames or []):
            print(f"note: {path} has unexpected columns; starting from scratch")
            return []
        return [dict(r) for r in reader]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Temp file + rename, so a crash mid-write can't leave a half-written CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["src_code"], r["dst_code"], r["depart"], r["number"])))
    os.replace(tmp, path)


def read_meta(path: Path) -> dict[str, Any]:
    """Which pairs are finished (a finished pair may have zero trains, so the CSV alone can't tell)."""
    try:
        meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        meta["pairs"] = list(meta.get("pairs", []))
        return meta
    except (OSError, ValueError):
        return {"pairs": []}


def write_meta(path: Path, meta: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------- the run
async def seed(args: argparse.Namespace) -> int:
    key = get_settings().railradar_api_key
    if not key:
        print("Stopped: RAILRADAR_API_KEY is not set (put it in .env).")
        return 1
    try:
        pairs = select_pairs(load_primary_stations(args.stations), load_states(args.cities), args.all_pairs)
    except SeedError as exc:
        print(f"Stopped: {exc}")
        return 1

    meta_path = args.out.with_name(args.out.stem + ".meta.json")
    rows = read_rows(args.out)  # with --refresh these rows are kept until each pair is re-fetched and replaced
    meta = read_meta(meta_path)
    done = set() if args.refresh else set(meta["pairs"])
    todo = [p for p in pairs if f"{p[0]}-{p[1]}" not in done]
    if not todo:
        print(f"Nothing to do: all {len(pairs)} station pairs are already in {args.out} (use --refresh to redo them).")
        return 0
    batch = todo[: args.max_requests]
    print(f"{len(pairs)} pairs in scope, {len(todo)} to fetch, fetching {len(batch)} this run...")

    # The provider's limiter normally gives up after 5 seconds, which suits a web request. A bulk
    # run should instead wait its turn: the free tier allows 10 requests a minute.
    ratelimit.MAX_WAIT_S = 30.0
    problems: list[str] = []
    fetched = 0
    async with httpx.AsyncClient() as client:
        rr = RailRadarTrains(client, key)
        for i, (src, dst) in enumerate(batch, 1):
            print(f"[{i}/{len(batch)}] {src} -> {dst} ...", end=" ", flush=True)
            try:
                runs = await rr.timetable(src, dst)
            except ProviderError as exc:
                if "HTTP 404" in str(exc):  # an unknown station code: note it and carry on with the others
                    problems.append(f"{src}->{dst}: {exc}")
                    print("unknown station")
                    continue
                print("failed")
                print(f"Stopped: {exc}\nPairs finished so far are saved; rerun to continue (tomorrow, if the daily quota is used up).")
                return 1
            rows = [r for r in rows if not (r["src_code"] == src and r["dst_code"] == dst)]  # replace this pair's old rows
            rows += [train_row(r) for r in runs]
            done.add(f"{src}-{dst}")
            meta.update({"pairs": sorted(done), "fetched_on": date.today().isoformat(), "source": "railradar"})
            write_rows(args.out, rows)  # save after EVERY pair, so progress survives a failure
            write_meta(meta_path, meta)
            fetched += 1
            print(f"{len(runs)} trains")

    remaining = len(todo) - fetched - len(problems)
    print(f"Fetched {fetched} pairs into {args.out}. Remaining: {remaining}.")
    if problems:
        print("Check these station codes in data/stations.csv (RailRadar did not recognise them):")
        for p in problems:
            print("  ", p)
    if remaining > 0:
        print("Run the same command again to continue.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build data/trains_sample.csv from RailRadar.")
    parser.add_argument("--stations", type=Path, default=DATA / "stations.csv", help="station table (default data/stations.csv)")
    parser.add_argument("--cities", type=Path, default=DATA / "cities.csv", help="city table, for states (default data/cities.csv)")
    parser.add_argument("--out", type=Path, default=DATA / "trains_sample.csv", help="output CSV path")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help="most API requests in this run (default 100)")
    parser.add_argument("--all-pairs", action="store_true", help="every pair of primary stations, not just same-state and Delhi pairs")
    parser.add_argument("--refresh", action="store_true", help="re-fetch pairs that are already done")
    return asyncio.run(seed(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())