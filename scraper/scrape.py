#!/usr/bin/env python3
"""Scrape availability, pricing, and location for Truck Parking Club listings.

Reads listing page URLs from urls.txt, queries the site's public listing API
(the same one the listing page itself calls), and records the results:

  data/history.csv       — one row per listing per run, appended over time
  data/latest.json       — most recent snapshot per listing, overwritten each run
  data/listings/<slug>.csv — per-listing history, one file per URL, rebuilt
                             from history.csv every run (so they always match)

Uses only the Python standard library. Run: python3 scrape.py
"""

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api-prod.truckparkingclub.com/api/v1/user/listing/public"

# The API rejects requests without browser-identifying headers (403).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://truckparkingclub.com",
    "Referer": "https://truckparkingclub.com/",
}

# plan_duration values observed in the API's `prices` array.
# Verified against the listing page: 1000/plan 2 = $10/day, 5000/plan 3 =
# $50/wk, 19000/plan 4 = $190/mo. Prices are in cents.
PLAN_DURATIONS = {1: "hourly", 2: "daily", 3: "weekly", 4: "monthly"}

# vehicle_type_allowed codes, verified against listing titles: listings with
# [0] are titled "Truck and Trailer Parking", listings with [1] are titled
# "Bobtail and Box Truck Parking".
VEHICLE_TYPES = {0: "Truck + Trailer", 1: "Bobtail / Box Truck"}

URL_RE = re.compile(
    r"truckparkingclub\.com/truck-parking/(?P<state>[^/]+)/(?P<city>[^/]+)/(?P<details>[^/?#]+)"
)

CSV_COLUMNS = [
    "scraped_at_utc",
    "url",
    "listing_id",
    "title",
    "full_address",
    "city",
    "state",
    "zip_code",
    "lat",
    "lng",
    "status",
    "vehicle_type",
    "total_available",
    "total_spaces_manual",
    "reviews_count",
    "review_rating",
    "available_seats_raw",
    "price_hourly_usd",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
    "prices_raw",
    "error",
]

# The trimmed, human-friendly columns written to the per-listing tables that
# feed the Google Sheet tabs. history.csv always keeps every column.
LISTING_COLUMNS = [
    "scraped_at_utc",
    "full_address",
    "vehicle_type",
    "total_available",
    "total_spaces_manual",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
    "reviews_count",
    "review_rating",
]


def read_urls(path: Path) -> list[str]:
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def fetch_listing(url: str, retries: int = 3) -> dict:
    m = URL_RE.search(url)
    if not m:
        raise ValueError(f"URL does not look like a listing page: {url}")
    params = urllib.parse.urlencode(
        {
            "state": m["state"],
            "city": m["city"],
            "details": m["details"],
            "page_size": 1,
            "review_limit": 0,
        }
    )
    req = urllib.request.Request(f"{API_BASE}?{params}", headers=HEADERS)
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                payload = json.load(res)
            data = payload.get("data") or []
            if not data:
                raise ValueError("API returned no listing for this URL")
            return data[0]
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise last_err


def listing_slug(url: str) -> str:
    """Filename-safe identifier for a listing URL, e.g. 14015-florida-blvd-70819."""
    m = URL_RE.search(url)
    raw = f"{m['city']}-{m['details']}" if m else url
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-").lower()


def write_per_listing_files(history_path: Path, listings_dir: Path) -> int:
    """Rebuild one CSV per listing from history.csv so each URL has its own table."""
    by_url: dict[str, list[dict]] = {}
    with history_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                # Rows scraped before the vehicle_type column existed: the
                # listing titles state the type, so recover it from there.
                if not row.get("vehicle_type"):
                    title = row.get("title") or ""
                    if "Truck and Trailer" in title:
                        row["vehicle_type"] = VEHICLE_TYPES[0]
                    elif "Bobtail and Box Truck" in title:
                        row["vehicle_type"] = VEHICLE_TYPES[1]
                by_url.setdefault(row["url"], []).append(row)
    listings_dir.mkdir(exist_ok=True)
    for url, rows in by_url.items():
        with (listings_dir / f"{listing_slug(url)}.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LISTING_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return len(by_url)


def migrate_history_schema(history_path: Path) -> None:
    """Rewrite history.csv in the current column layout if it predates it.

    Rows keep their values by column name; columns added since are blank.
    Keeps appends aligned when the schema gains a column (e.g. vehicle_type).
    """
    if not history_path.exists():
        return
    with history_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == CSV_COLUMNS:
            return
        rows = list(reader)
    with history_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def load_manual_spaces(history_path: Path) -> dict[str, str]:
    """Latest manually-entered total_spaces_manual per URL from history.csv.

    The column is meant to be filled in by hand; each run carries the most
    recent non-empty value forward into new rows so it only needs typing once.
    """
    manual: dict[str, str] = {}
    if not history_path.exists():
        return manual
    with history_path.open(newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get("total_spaces_manual") or "").strip()
            if value and row.get("url"):
                manual[row["url"]] = value
    return manual


def to_row(url: str, listing: dict, scraped_at: str) -> dict:
    seats = listing.get("available_seats") or []
    total_available = sum(g.get("seats", 0) for g in seats)

    row = {
        "scraped_at_utc": scraped_at,
        "url": url,
        "listing_id": listing.get("listing_id"),
        "title": listing.get("title"),
        "full_address": listing.get("full_address"),
        "city": listing.get("city"),
        "state": listing.get("state"),
        "zip_code": listing.get("zip_code"),
        "lat": listing.get("lat"),
        "lng": listing.get("lng"),
        "status": listing.get("status"),
        "vehicle_type": " & ".join(
            VEHICLE_TYPES.get(v, f"type {v}")
            for v in listing.get("vehicle_type_allowed") or []
        ),
        "total_available": total_available,
        "total_spaces_manual": "",
        "reviews_count": listing.get("reviews_count"),
        "review_rating": listing.get("average_ratings"),
        "available_seats_raw": json.dumps(seats, separators=(",", ":")),
        "prices_raw": json.dumps(listing.get("prices") or [], separators=(",", ":")),
        "error": "",
    }
    for duration_name in PLAN_DURATIONS.values():
        row[f"price_{duration_name}_usd"] = ""
    for price in listing.get("prices") or []:
        name = PLAN_DURATIONS.get(price.get("plan_duration"))
        if name and price.get("price") is not None:
            row[f"price_{name}_usd"] = f"{price['price'] / 100:.2f}"
    return row


def error_row(url: str, err: Exception, scraped_at: str) -> dict:
    row = {col: "" for col in CSV_COLUMNS}
    row.update(scraped_at_utc=scraped_at, url=url, error=f"{type(err).__name__}: {err}")
    return row


def main() -> int:
    base = Path(__file__).resolve().parent
    urls = read_urls(base / "urls.txt")
    if not urls:
        print("No URLs configured in urls.txt", file=sys.stderr)
        return 1

    data_dir = base / "data"
    data_dir.mkdir(exist_ok=True)
    history_path = data_dir / "history.csv"
    latest_path = data_dir / "latest.json"

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    migrate_history_schema(history_path)
    manual_spaces = load_manual_spaces(history_path)
    rows, failures = [], 0
    for url in urls:
        try:
            rows.append(to_row(url, fetch_listing(url), scraped_at))
            print(f"ok    {url}")
        except Exception as err:  # noqa: BLE001 — record and keep going
            failures += 1
            rows.append(error_row(url, err, scraped_at))
            print(f"FAIL  {url}: {err}", file=sys.stderr)
        rows[-1]["total_spaces_manual"] = manual_spaces.get(url, "")
        time.sleep(1)  # be polite between listings

    write_header = not history_path.exists()
    with history_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    latest_path.write_text(json.dumps({r["url"]: r for r in rows}, indent=2) + "\n")

    n_files = write_per_listing_files(history_path, data_dir / "listings")

    print(f"\n{len(rows) - failures}/{len(rows)} listings scraped -> {history_path}")
    print(f"{n_files} per-listing tables -> {data_dir / 'listings'}/")
    return 1 if failures == len(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
