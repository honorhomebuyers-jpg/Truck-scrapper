#!/usr/bin/env python3
"""Scrape availability, pricing, and location for Truck Parking Club listings.

Reads listing page URLs from urls.txt, queries the site's public listing API
(the same one the listing page itself calls), and records the results:

  data/history.csv  — one row per listing per run, appended over time
  data/latest.json  — most recent snapshot per listing, overwritten each run

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

    print(f"\n{len(rows) - failures}/{len(rows)} listings scraped -> {history_path}")
    return 1 if failures == len(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
